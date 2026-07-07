"""
crush_db.py – Database layer for the Crush system.

Tables
------
  ft_crushes  – one row per (guild, user); stores their current crush
"""

from __future__ import annotations

import os
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from datetime import datetime

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

            # Upsert (reset created_at so the 10-day timer restarts on change)
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


def get_crush_info(guild_id: int, user_id: int) -> tuple[int, int] | None:
    """
    Return (crush_id, days_since_set) for user_id, or None if no crush.
    days_since_set is used to determine dating status (>= 10 days mutual).
    """
    from datetime import timezone as _tz
    with _connect() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT crush_id, created_at FROM ft_crushes WHERE guild_id=%s AND user_id=%s",
                (guild_id, user_id),
            )
            row = cur.fetchone()
    if row is None:
        return None
    created = row["created_at"]
    # Normalise to UTC-aware regardless of whether psycopg2 returns naive or aware
    if created.tzinfo is None:
        created = created.replace(tzinfo=_tz.utc)
    else:
        created = created.astimezone(_tz.utc)
    now = datetime.now(_tz.utc)
    delta = now - created
    return row["crush_id"], delta.days


def is_mutual_crush(guild_id: int, user_a: int, user_b: int) -> bool:
    """True if both users currently crush each other."""
    return get_crush(guild_id, user_a) == user_b and get_crush(guild_id, user_b) == user_a
