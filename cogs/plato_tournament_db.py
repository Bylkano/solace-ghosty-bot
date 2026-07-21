"""
plato_tournament_db.py – Persistent storage for Plato group-stage tournament.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Optional

import psycopg2
import psycopg2.extras

log = logging.getLogger("bot.plato_tournament_db")

_DATABASE_URL: str = os.environ.get("DATABASE_URL", "")

VALID_GROUPS = frozenset({"A", "B", "C", "D"})
PLAYERS_PER_GROUP = 7
QUALIFY_TOP = 4


@contextmanager
def _connect():
    if not _DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set in environment")
    con = psycopg2.connect(_DATABASE_URL, sslmode="require")
    try:
        yield con
    finally:
        con.close()


def init_tables() -> None:
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS plato_players (
                    guild_id   BIGINT NOT NULL,
                    group_code CHAR(1) NOT NULL,
                    user_id    BIGINT NOT NULL,
                    PRIMARY KEY (guild_id, group_code, user_id),
                    CHECK (group_code IN ('A', 'B', 'C', 'D'))
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS plato_results (
                    id           BIGSERIAL PRIMARY KEY,
                    guild_id     BIGINT NOT NULL,
                    group_code   CHAR(1) NOT NULL,
                    player_low   BIGINT NOT NULL,
                    player_high  BIGINT NOT NULL,
                    score_low    INT NOT NULL CHECK (score_low BETWEEN 0 AND 2),
                    score_high   INT NOT NULL CHECK (score_high BETWEEN 0 AND 2),
                    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CHECK (group_code IN ('A', 'B', 'C', 'D')),
                    CHECK (player_low < player_high),
                    CHECK (score_low + score_high = 2),
                    UNIQUE (guild_id, group_code, player_low, player_high)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS plato_players_guild_idx
                ON plato_players (guild_id, group_code)
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS plato_results_guild_idx
                ON plato_results (guild_id, group_code)
                """
            )
        con.commit()


try:
    init_tables()
except Exception as exc:
    log.error("Failed to initialise plato tournament tables: %s", exc)


def _norm_pair(a: int, b: int) -> tuple[int, int, bool]:
    """Return (low, high, a_is_low)."""
    if a == b:
        raise ValueError("players must be different")
    if a < b:
        return a, b, True
    return b, a, False


def set_group(guild_id: int, group_code: str, user_ids: list[int]) -> None:
    """
    Save/replace group roster.

    Existing match results between players who remain in the group are kept.
    Results involving a removed player are deleted.
    """
    group_code = group_code.upper()
    if group_code not in VALID_GROUPS:
        raise ValueError("group must be A, B, C, or D")
    if len(user_ids) != PLAYERS_PER_GROUP:
        raise ValueError(f"exactly {PLAYERS_PER_GROUP} players required")
    if len(set(user_ids)) != PLAYERS_PER_GROUP:
        raise ValueError("duplicate players are not allowed")

    new_ids = set(user_ids)
    with _connect() as con:
        with con.cursor() as cur:
            # Drop results only if a player left the group (keeps finished scores)
            cur.execute(
                """
                DELETE FROM plato_results
                WHERE guild_id = %s AND group_code = %s
                  AND (
                        player_low NOT IN %s
                     OR player_high NOT IN %s
                  )
                """,
                (guild_id, group_code, tuple(new_ids), tuple(new_ids)),
            )
            cur.execute(
                """
                DELETE FROM plato_players
                WHERE guild_id = %s AND group_code = %s
                  AND user_id NOT IN %s
                """,
                (guild_id, group_code, tuple(new_ids)),
            )
            for uid in user_ids:
                cur.execute(
                    """
                    INSERT INTO plato_players (guild_id, group_code, user_id)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (guild_id, group_code, user_id) DO NOTHING
                    """,
                    (guild_id, group_code, uid),
                )
        con.commit()


def get_group_players(guild_id: int, group_code: str) -> list[int]:
    group_code = group_code.upper()
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                SELECT user_id FROM plato_players
                WHERE guild_id = %s AND group_code = %s
                ORDER BY user_id
                """,
                (guild_id, group_code),
            )
            return [int(r[0]) for r in cur.fetchall()]


def find_shared_group(guild_id: int, user_a: int, user_b: int) -> Optional[str]:
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                SELECT a.group_code
                FROM plato_players a
                JOIN plato_players b
                  ON a.guild_id = b.guild_id
                 AND a.group_code = b.group_code
                WHERE a.guild_id = %s
                  AND a.user_id = %s
                  AND b.user_id = %s
                """,
                (guild_id, user_a, user_b),
            )
            row = cur.fetchone()
            return row[0] if row else None


def add_result(
    guild_id: int, player1: int, player2: int, score1: int, score2: int
) -> dict[str, Any]:
    if score1 + score2 != 2 or score1 < 0 or score2 < 0:
        raise ValueError("score must be 2-0, 1-1, or 0-2")
    group = find_shared_group(guild_id, player1, player2)
    if not group:
        raise LookupError("both players must be in the same group")

    low, high, p1_is_low = _norm_pair(player1, player2)
    if p1_is_low:
        score_low, score_high = score1, score2
    else:
        score_low, score_high = score2, score1

    with _connect() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO plato_results
                        (guild_id, group_code, player_low, player_high, score_low, score_high)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (guild_id, group, low, high, score_low, score_high),
                )
            except psycopg2.IntegrityError as exc:
                con.rollback()
                raise ValueError(
                    "a result for that pair already exists — use /editresult or /removeresult"
                ) from exc
            row = dict(cur.fetchone())
        con.commit()
    return row


def edit_result(
    guild_id: int, player1: int, player2: int, score1: int, score2: int
) -> dict[str, Any]:
    if score1 + score2 != 2 or score1 < 0 or score2 < 0:
        raise ValueError("score must be 2-0, 1-1, or 0-2")
    group = find_shared_group(guild_id, player1, player2)
    if not group:
        raise LookupError("both players must be in the same group")

    low, high, p1_is_low = _norm_pair(player1, player2)
    if p1_is_low:
        score_low, score_high = score1, score2
    else:
        score_low, score_high = score2, score1

    with _connect() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                UPDATE plato_results
                SET score_low = %s, score_high = %s, updated_at = NOW()
                WHERE guild_id = %s AND group_code = %s
                  AND player_low = %s AND player_high = %s
                RETURNING *
                """,
                (score_low, score_high, guild_id, group, low, high),
            )
            row = cur.fetchone()
            if not row:
                raise LookupError("no result found for that pair — use /result first")
            out = dict(row)
        con.commit()
    return out


def remove_result(guild_id: int, player1: int, player2: int) -> bool:
    low, high, _ = _norm_pair(player1, player2)
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                DELETE FROM plato_results
                WHERE guild_id = %s
                  AND player_low = %s AND player_high = %s
                """,
                (guild_id, low, high),
            )
            deleted = cur.rowcount > 0
        con.commit()
    return deleted


def get_ranking(guild_id: int, group_code: str) -> list[dict[str, Any]]:
    """Return players sorted by points desc: [{user_id, points, position}, ...]."""
    group_code = group_code.upper()
    if group_code not in VALID_GROUPS:
        raise ValueError("group must be A, B, C, or D")

    with _connect() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT p.user_id,
                       COALESCE(SUM(
                           CASE
                               WHEN r.player_low = p.user_id THEN r.score_low
                               WHEN r.player_high = p.user_id THEN r.score_high
                               ELSE 0
                           END
                       ), 0)::INT AS points
                FROM plato_players p
                LEFT JOIN plato_results r
                  ON r.guild_id = p.guild_id
                 AND r.group_code = p.group_code
                 AND (r.player_low = p.user_id OR r.player_high = p.user_id)
                WHERE p.guild_id = %s AND p.group_code = %s
                GROUP BY p.user_id
                ORDER BY points DESC, p.user_id ASC
                """,
                (guild_id, group_code),
            )
            rows = [dict(r) for r in cur.fetchall()]

    for i, row in enumerate(rows, start=1):
        row["position"] = i
        row["qualified"] = i <= QUALIFY_TOP
    return rows


def reset_tournament(guild_id: int) -> None:
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute("DELETE FROM plato_results WHERE guild_id = %s", (guild_id,))
            cur.execute("DELETE FROM plato_players WHERE guild_id = %s", (guild_id,))
        con.commit()
