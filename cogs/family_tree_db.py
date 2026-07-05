"""
family_tree_db.py – PostgreSQL database layer for the Solace Family Tree Bot.
Follows the same sync psycopg2 pattern as store.py.
All tables are prefixed with `ft_` to avoid collisions.
"""

from __future__ import annotations

import logging
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import psycopg2
from psycopg2.extras import RealDictCursor

log = logging.getLogger("bot.family_tree_db")

_DB_URL = os.environ.get("DATABASE_URL", "")


def _connect():
    if not _DB_URL:
        raise RuntimeError("DATABASE_URL not set in environment")
    return psycopg2.connect(_DB_URL, sslmode="require")


# ---------------------------------------------------------------------------
# Schema init – call once on import
# ---------------------------------------------------------------------------

def _init() -> None:
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ft_guilds (
                    guild_id       BIGINT PRIMARY KEY,
                    incest_allowed BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ft_marriages (
                    id          SERIAL PRIMARY KEY,
                    guild_id    BIGINT NOT NULL,
                    user1_id    BIGINT NOT NULL,
                    user2_id    BIGINT NOT NULL,
                    married_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    divorced_at TIMESTAMPTZ DEFAULT NULL,
                    UNIQUE (guild_id, user1_id, user2_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ft_parent_child (
                    id          SERIAL PRIMARY KEY,
                    guild_id    BIGINT NOT NULL,
                    parent_id   BIGINT NOT NULL,
                    child_id    BIGINT NOT NULL,
                    is_adopted  BOOLEAN NOT NULL DEFAULT TRUE,
                    adopted_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    disowned_at TIMESTAMPTZ DEFAULT NULL,
                    UNIQUE (guild_id, parent_id, child_id)
                )
            """)
        con.commit()
    log.info("Family tree tables ready.")


try:
    _init()
except Exception as exc:
    log.error("Failed to initialise family tree DB tables: %s", exc)


# ---------------------------------------------------------------------------
# Guild settings
# ---------------------------------------------------------------------------

def ensure_guild(guild_id: int) -> None:
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO ft_guilds (guild_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (guild_id,),
            )
        con.commit()


def get_incest_allowed(guild_id: int) -> bool:
    with _connect() as con:
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT incest_allowed FROM ft_guilds WHERE guild_id = %s",
                (guild_id,),
            )
            row = cur.fetchone()
            return bool(row["incest_allowed"]) if row else False


def set_incest_allowed(guild_id: int, value: bool) -> None:
    ensure_guild(guild_id)
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "UPDATE ft_guilds SET incest_allowed = %s WHERE guild_id = %s",
                (value, guild_id),
            )
        con.commit()


# ---------------------------------------------------------------------------
# Marriages
# ---------------------------------------------------------------------------

def get_marriage(guild_id: int, user_id: int) -> dict | None:
    """Return the active marriage row for a user, or None."""
    with _connect() as con:
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM ft_marriages
                WHERE guild_id = %s AND divorced_at IS NULL
                  AND (user1_id = %s OR user2_id = %s)
                """,
                (guild_id, user_id, user_id),
            )
            return cur.fetchone()


def get_spouse_id(guild_id: int, user_id: int) -> int | None:
    row = get_marriage(guild_id, user_id)
    if not row:
        return None
    u1, u2 = row["user1_id"], row["user2_id"]
    return u2 if u1 == user_id else u1


def create_marriage(guild_id: int, user1_id: int, user2_id: int) -> None:
    """Insert or reactivate a marriage row (supports remarrying the same person)."""
    u1, u2 = sorted([user1_id, user2_id])
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ft_marriages (guild_id, user1_id, user2_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (guild_id, user1_id, user2_id) DO UPDATE
                    SET married_at = NOW(), divorced_at = NULL
                """,
                (guild_id, u1, u2),
            )
        con.commit()


def divorce(guild_id: int, user_id: int) -> bool:
    """Mark the active marriage as divorced. Returns True if one was found."""
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                UPDATE ft_marriages SET divorced_at = NOW()
                WHERE guild_id = %s AND divorced_at IS NULL
                  AND (user1_id = %s OR user2_id = %s)
                """,
                (guild_id, user_id, user_id),
            )
            affected = cur.rowcount
        con.commit()
    return affected > 0


def get_all_active_marriages(guild_id: int) -> list[dict]:
    with _connect() as con:
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM ft_marriages WHERE guild_id = %s AND divorced_at IS NULL",
                (guild_id,),
            )
            return cur.fetchall()


def get_all_marriages_including_divorced(guild_id: int) -> list[dict]:
    with _connect() as con:
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM ft_marriages WHERE guild_id = %s ORDER BY married_at",
                (guild_id,),
            )
            return cur.fetchall()


# ---------------------------------------------------------------------------
# Relationship stage
# ---------------------------------------------------------------------------

def get_relationship_stage(married_at) -> tuple[str, str]:
    """
    Return (emoji, label) based on how long the couple has been together.
    `married_at` may be a datetime or ISO string.
    """
    from datetime import datetime, timezone

    if isinstance(married_at, str):
        dt = datetime.fromisoformat(married_at)
    else:
        dt = married_at

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    days = (datetime.now(timezone.utc) - dt).days

    if days < 7:
        return "❤️", "Newly Married"
    if days < 30:
        return "💖", "Loving Couple"
    if days < 180:
        return "💞", "Soulmates"
    return "👑", "Legendary Couple"


# ---------------------------------------------------------------------------
# Parent-Child
# ---------------------------------------------------------------------------

def get_parents(guild_id: int, child_id: int) -> list[int]:
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                SELECT parent_id FROM ft_parent_child
                WHERE guild_id = %s AND child_id = %s AND disowned_at IS NULL
                """,
                (guild_id, child_id),
            )
            return [r[0] for r in cur.fetchall()]


def get_children(guild_id: int, parent_id: int) -> list[int]:
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                SELECT child_id FROM ft_parent_child
                WHERE guild_id = %s AND parent_id = %s AND disowned_at IS NULL
                """,
                (guild_id, parent_id),
            )
            return [r[0] for r in cur.fetchall()]


def get_children_with_details(guild_id: int, parent_id: int) -> list[dict]:
    with _connect() as con:
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT child_id, is_adopted, adopted_at FROM ft_parent_child
                WHERE guild_id = %s AND parent_id = %s AND disowned_at IS NULL
                ORDER BY adopted_at
                """,
                (guild_id, parent_id),
            )
            return cur.fetchall()


def count_shared_children(guild_id: int, p1_id: int, p2_id: int) -> int:
    """Count children that both p1 and p2 are parents of."""
    ch1 = set(get_children(guild_id, p1_id))
    ch2 = set(get_children(guild_id, p2_id))
    return len(ch1 & ch2)


def count_parents_of_child(guild_id: int, child_id: int) -> int:
    return len(get_parents(guild_id, child_id))


def is_already_parent_of(guild_id: int, parent_id: int, child_id: int) -> bool:
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM ft_parent_child
                WHERE guild_id = %s AND parent_id = %s AND child_id = %s
                  AND disowned_at IS NULL
                """,
                (guild_id, parent_id, child_id),
            )
            return cur.fetchone() is not None


def add_parent_child(guild_id: int, parent_id: int, child_id: int, is_adopted: bool = True) -> None:
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ft_parent_child (guild_id, parent_id, child_id, is_adopted)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (guild_id, parent_id, child_id) DO UPDATE
                    SET disowned_at = NULL, is_adopted = EXCLUDED.is_adopted,
                        adopted_at = NOW()
                """,
                (guild_id, parent_id, child_id, is_adopted),
            )
        con.commit()


def disown_child(guild_id: int, parent_id: int, child_id: int) -> bool:
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                UPDATE ft_parent_child SET disowned_at = NOW()
                WHERE guild_id = %s AND parent_id = %s AND child_id = %s
                  AND disowned_at IS NULL
                """,
                (guild_id, parent_id, child_id),
            )
            affected = cur.rowcount
        con.commit()
    return affected > 0


def get_all_active_parent_child(guild_id: int) -> list[dict]:
    with _connect() as con:
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT parent_id, child_id, is_adopted FROM ft_parent_child
                WHERE guild_id = %s AND disowned_at IS NULL
                """,
                (guild_id,),
            )
            return cur.fetchall()


# ---------------------------------------------------------------------------
# Incest / relationship check
# ---------------------------------------------------------------------------

def is_related(guild_id: int, user1_id: int, user2_id: int) -> bool:
    """
    BFS through the blood/adoption tree to see if user1 and user2 share lineage.
    Does NOT consider marriage links — only parent-child edges.
    """
    rows = get_all_active_parent_child(guild_id)
    adj: dict[int, list[int]] = {}
    for r in rows:
        p, c = r["parent_id"], r["child_id"]
        adj.setdefault(p, []).append(c)
        adj.setdefault(c, []).append(p)

    visited = {user1_id}
    queue = [user1_id]
    while queue:
        node = queue.pop(0)
        for nb in adj.get(node, []):
            if nb == user2_id:
                return True
            if nb not in visited:
                visited.add(nb)
                queue.append(nb)
    return False


# ---------------------------------------------------------------------------
# Full guild graph helpers
# ---------------------------------------------------------------------------

def get_all_users_in_guild(guild_id: int) -> set[int]:
    """All user IDs appearing in any active relationship."""
    users: set[int] = set()
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT user1_id, user2_id FROM ft_marriages WHERE guild_id=%s AND divorced_at IS NULL",
                (guild_id,),
            )
            for r in cur.fetchall():
                users.add(r[0])
                users.add(r[1])
            cur.execute(
                "SELECT parent_id, child_id FROM ft_parent_child WHERE guild_id=%s AND disowned_at IS NULL",
                (guild_id,),
            )
            for r in cur.fetchall():
                users.add(r[0])
                users.add(r[1])
    return users
