"""
PostgreSQL-backed config store for per-guild bot settings.

Environment:
    DATABASE_URL  — PostgreSQL connection string (e.g. from Render or Supabase)

Tables:
    guild_config (guild_id, key, value)
"""

import os
import psycopg2
from psycopg2.extras import RealDictCursor


_DB_URL = os.environ.get("DATABASE_URL")


def _connect():
    if not _DB_URL:
        raise RuntimeError("DATABASE_URL not set in environment")
    return psycopg2.connect(_DB_URL, sslmode="require")


def _init() -> None:
    """Create the config table if it doesn't exist."""
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS guild_config (
                    guild_id BIGINT NOT NULL,
                    key TEXT NOT NULL,
                    value BIGINT,
                    PRIMARY KEY (guild_id, key)
                )
                """
            )
        con.commit()


# Run once on import
_init()


def _get_value(guild_id: int, key: str) -> int | None:
    with _connect() as con:
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT value FROM guild_config WHERE guild_id = %s AND key = %s",
                (guild_id, key),
            )
            row = cur.fetchone()
            return row["value"] if row else None


def _set_value(guild_id: int, key: str, value: int) -> None:
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO guild_config (guild_id, key, value) VALUES (%s, %s, %s)
                ON CONFLICT (guild_id, key) DO UPDATE SET value = EXCLUDED.value
                """,
                (guild_id, key, value),
            )
        con.commit()


# ── Moderation / banning-words channel ───────────────────────────

def get_automod_channel(guild_id: int) -> int | None:
    """Return the saved auto-mod channel ID for a guild, or None if not set."""
    return _get_value(guild_id, "automod_channel_id")


def set_automod_channel(guild_id: int, channel_id: int) -> None:
    """Persist the auto-mod channel ID for a guild."""
    _set_value(guild_id, "automod_channel_id", channel_id)


# ── Economy / drops channel ───────────────────────────────────────

def get_drops_channel(guild_id: int) -> int | None:
    """Return the saved drops channel ID for a guild, or None if not set."""
    return _get_value(guild_id, "drops_channel_id")


def set_drops_channel(guild_id: int, channel_id: int) -> None:
    """Persist the drops channel ID for a guild."""
    _set_value(guild_id, "drops_channel_id", channel_id)
