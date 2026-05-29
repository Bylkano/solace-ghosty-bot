"""
Lightweight JSON-backed config store for per-guild bot settings.
All reads and writes go through this module — no other file should
touch server_config.json directly.

Keys per guild:
  automod_channel_id  → channel watched by the banning/moderation cog
  drops_channel_id    → channel watched by the economy/drops cog
"""

import json
import pathlib

_CONFIG_PATH = pathlib.Path(__file__).parent / "server_config.json"


def _load() -> dict:
    if _CONFIG_PATH.exists():
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    return {}


def _save(data: dict) -> None:
    _CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ── Moderation / banning-words channel ───────────────────────────

def get_automod_channel(guild_id: int) -> int | None:
    """Return the saved auto-mod channel ID for a guild, or None if not set."""
    return _load().get(str(guild_id), {}).get("automod_channel_id")


def set_automod_channel(guild_id: int, channel_id: int) -> None:
    """Persist the auto-mod channel ID for a guild."""
    data = _load()
    data.setdefault(str(guild_id), {})["automod_channel_id"] = channel_id
    _save(data)


# ── Economy / drops channel ───────────────────────────────────────

def get_drops_channel(guild_id: int) -> int | None:
    """Return the saved drops channel ID for a guild, or None if not set."""
    return _load().get(str(guild_id), {}).get("drops_channel_id")


def set_drops_channel(guild_id: int, channel_id: int) -> None:
    """Persist the drops channel ID for a guild."""
    data = _load()
    data.setdefault(str(guild_id), {})["drops_channel_id"] = channel_id
    _save(data)
