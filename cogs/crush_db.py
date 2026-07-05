"""
crush_db.py – Database layer for the Crush and Loyalty systems.

Tables
------
  ft_crushes  – one row per (guild, user); stores their current crush
  ft_loyalty  – loyalty score (0-100) for each user; created on first write
"""

from __future__ import annotations

import os
import psycopg2
import psycopg2.extras
from contextlib import contextmanager

_DATABASE_URL: str = os.environ["DATABASE_URL"]


@contextmanager
def _connect():
    con = psycopg2.connect(_DATABASE_URL)
    try:
        yield con
    finally:
        con.close()


# ── Schema ────────────────────────────────────────────────────────────────────

def init_tables() -> None:
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ft_crushes (
                    guild_id   BIGINT NOT NULL,
                    user_id    BIGINT NOT NULL,
                    crush_id   BIGINT NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW(),
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ft_loyalty (
                    guild_id   BIGINT NOT NULL,
                    user_id    BIGINT NOT NULL,
                    score      INT    NOT NULL DEFAULT 100,
                    updated_at TIMESTAMP DEFAULT NOW(),
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
        con.commit()


# ── Crush CRUD ────────────────────────────────────────────────────────────────

def set_crush(guild_id: int, user_id: int, crush_id: int) -> str:
    """
    Set user_id's crush to crush_id.

    Returns
    -------
    "mutual"  – both users now crush each other
    "already" – user_id was already crushing crush_id (no change)
    "set"     – crush stored for the first time / updated
    """
    with _connect() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Check existing crush
            cur.execute(
                "SELECT crush_id FROM ft_crushes WHERE guild_id=%s AND user_id=%s",
                (guild_id, user_id),
            )
            row = cur.fetchone()
            if row and row["crush_id"] == crush_id:
                return "already"

            # Upsert
            cur.execute("""
                INSERT INTO ft_crushes (guild_id, user_id, crush_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (guild_id, user_id) DO UPDATE
                    SET crush_id = EXCLUDED.crush_id, created_at = NOW()
            """, (guild_id, user_id, crush_id))

            # Check mutual
            cur.execute(
                "SELECT crush_id FROM ft_crushes WHERE guild_id=%s AND user_id=%s",
                (guild_id, crush_id),
            )
            other = cur.fetchone()
        con.commit()

    if other and other["crush_id"] == user_id:
        return "mutual"
    return "set"


def remove_crush(guild_id: int, user_id: int) -> bool:
    """Remove user_id's crush. Returns True if there was one to remove."""
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "DELETE FROM ft_crushes WHERE guild_id=%s AND user_id=%s",
                (guild_id, user_id),
            )
            deleted = cur.rowcount
        con.commit()
    return deleted > 0


def get_crush(guild_id: int, user_id: int) -> int | None:
    """Return the crush_id of user_id, or None."""
    with _connect() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT crush_id FROM ft_crushes WHERE guild_id=%s AND user_id=%s",
                (guild_id, user_id),
            )
            row = cur.fetchone()
    return row["crush_id"] if row else None


def is_mutual_crush(guild_id: int, user_a: int, user_b: int) -> bool:
    """True if both users currently crush each other."""
    return get_crush(guild_id, user_a) == user_b and get_crush(guild_id, user_b) == user_a


# ── Loyalty ───────────────────────────────────────────────────────────────────

def get_loyalty(guild_id: int, user_id: int) -> int:
    """Return the loyalty score (0-100); defaults to 100 if never set."""
    with _connect() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT score FROM ft_loyalty WHERE guild_id=%s AND user_id=%s",
                (guild_id, user_id),
            )
            row = cur.fetchone()
    return row["score"] if row else 100


def reduce_loyalty(guild_id: int, user_id: int, amount: int) -> int:
    """
    Subtract *amount* from the loyalty score (floor 0).
    Creates the row if it doesn't exist (starts at 100 then deducts amount).
    Returns the new score.
    """
    with _connect() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Pass amount twice: once for the INSERT initial value, once for
            # the UPDATE delta.  Using EXCLUDED.score here would be wrong
            # because EXCLUDED.score = 100 - amount, not amount itself.
            cur.execute("""
                INSERT INTO ft_loyalty (guild_id, user_id, score)
                VALUES (%s, %s, GREATEST(0, 100 - %s))
                ON CONFLICT (guild_id, user_id) DO UPDATE
                    SET score      = GREATEST(0, ft_loyalty.score - %s),
                        updated_at = NOW()
                RETURNING score
            """, (guild_id, user_id, amount, amount))
            new_score = cur.fetchone()["score"]
        con.commit()
    return new_score


def reset_loyalty(guild_id: int, user_id: int) -> None:
    """Reset loyalty to 100 (e.g. after divorce or reform arc)."""
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute("""
                INSERT INTO ft_loyalty (guild_id, user_id, score)
                VALUES (%s, %s, 100)
                ON CONFLICT (guild_id, user_id) DO UPDATE
                    SET score = 100, updated_at = NOW()
            """, (guild_id, user_id))
        con.commit()
