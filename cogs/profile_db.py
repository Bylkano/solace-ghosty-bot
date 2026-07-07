"""
profile_db.py – Database layer for user profiles (bio).

Tables
------
  ft_profiles  – one row per (guild, user); stores their bio
"""

from __future__ import annotations

import logging
import os
import psycopg2
from psycopg2.extras import RealDictCursor

log = logging.getLogger("bot.profile_db")

_DB_URL = os.environ.get("DATABASE_URL", "")


def _connect():
    if not _DB_URL:
        raise RuntimeError("DATABASE_URL not set in environment")
    return psycopg2.connect(_DB_URL, sslmode="require")


# ---------------------------------------------------------------------------
# Schema init – called once on import
# ---------------------------------------------------------------------------

def _init() -> None:
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ft_profiles (
                    guild_id BIGINT  NOT NULL,
                    user_id  BIGINT  NOT NULL,
                    bio      TEXT    NOT NULL DEFAULT '',
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
        con.commit()
    log.info("Profile tables ready.")


try:
    _init()
except Exception as exc:
    log.error("Failed to initialise profile DB tables: %s", exc)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def get_profile(guild_id: int, user_id: int) -> dict | None:
    """Return the profile row for a user, or None if they have no entry."""
    with _connect() as con:
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT bio FROM ft_profiles WHERE guild_id = %s AND user_id = %s",
                (guild_id, user_id),
            )
            return cur.fetchone()


def set_bio(guild_id: int, user_id: int, bio: str) -> None:
    """Upsert the user's bio. Pass an empty string to clear it."""
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ft_profiles (guild_id, user_id, bio)
                VALUES (%s, %s, %s)
                ON CONFLICT (guild_id, user_id) DO UPDATE SET bio = EXCLUDED.bio
                """,
                (guild_id, user_id, bio),
            )
        con.commit()
