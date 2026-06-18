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
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS self_roles (
                    guild_id BIGINT NOT NULL,
                    role_id  BIGINT NOT NULL,
                    PRIMARY KEY (guild_id, role_id)
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


# ── Welcome / leave channels ──────────────────────────────────────

def get_welcome_channel(guild_id: int) -> int | None:
    """Return the saved welcome channel ID for a guild, or None if not set."""
    return _get_value(guild_id, "welcome_channel_id")


def set_welcome_channel(guild_id: int, channel_id: int) -> None:
    """Persist the welcome channel ID for a guild."""
    _set_value(guild_id, "welcome_channel_id", channel_id)


def get_leave_channel(guild_id: int) -> int | None:
    """Return the saved leave channel ID for a guild, or None if not set."""
    return _get_value(guild_id, "leave_channel_id")


def set_leave_channel(guild_id: int, channel_id: int) -> None:
    """Persist the leave channel ID for a guild."""
    _set_value(guild_id, "leave_channel_id", channel_id)


# ── Drop trigger threshold ────────────────────────────────────────

_DEFAULT_DROP_TRIGGER = 10

def get_drop_trigger(guild_id: int) -> int:
    """Return how many messages must be sent before a drop fires (default 10)."""
    val = _get_value(guild_id, "drop_trigger")
    return val if val is not None else _DEFAULT_DROP_TRIGGER


def set_drop_trigger(guild_id: int, count: int) -> None:
    """Persist the drop trigger message count for a guild."""
    _set_value(guild_id, "drop_trigger", count)


# ── Self-assignable roles ────────────────────────────────

def add_self_role(guild_id: int, role_id: int) -> None:
    """Mark a role as self-assignable via /role (idempotent)."""
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO self_roles (guild_id, role_id) VALUES (%s, %s) "
                "ON CONFLICT (guild_id, role_id) DO NOTHING",
                (guild_id, role_id),
            )
        con.commit()


def remove_self_role(guild_id: int, role_id: int) -> None:
    """Remove a role from the self-assignable list."""
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "DELETE FROM self_roles WHERE guild_id = %s AND role_id = %s",
                (guild_id, role_id),
            )
        con.commit()


def get_self_roles(guild_id: int) -> list[int]:
    """Return the list of self-assignable role IDs for a guild."""
    with _connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT role_id FROM self_roles WHERE guild_id = %s ORDER BY role_id",
                (guild_id,),
            )
            return [r[0] for r in cur.fetchall()]


# ── Drops ping role ──────────────────────────────────────────────

def get_ping_role(guild_id: int) -> int | None:
    """Return the role ID pinged before each drop, or None if unset/disabled."""
    val = _get_value(guild_id, "drops_ping_role_id")
    return val if val else None


def set_ping_role(guild_id: int, role_id: int) -> None:
    """Persist the role to ping before each economy drop."""
    _set_value(guild_id, "drops_ping_role_id", role_id)


def clear_ping_role(guild_id: int) -> None:
    """Disable drop pings by clearing the saved role."""
    _set_value(guild_id, "drops_ping_role_id", 0)
