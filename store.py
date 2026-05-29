"""
MongoDB-backed config store for per-guild bot settings.
All reads and writes go through this module — no other file should
touch the database directly.

Collection: guild_config
Document schema per guild:
  _id                 → guild_id (int)
  automod_channel_id  → channel watched by the banning/moderation cog
  drops_channel_id    → channel watched by the economy/drops cog

Requires MONGODB_URI in the environment (see .env.example).
"""

import os
from motor.motor_asyncio import AsyncIOMotorClient

_client: AsyncIOMotorClient | None = None
_col = None  # motor Collection


def _get_collection():
    global _client, _col
    if _col is None:
        uri = os.getenv("MONGODB_URI")
        if not uri:
            raise RuntimeError(
                "MONGODB_URI is not set. Add it to your .env file."
            )
        _client = AsyncIOMotorClient(uri)
        db = _client[os.getenv("MONGODB_DB", "ghosty_bot")]
        _col = db["guild_config"]
    return _col


# ── Moderation / banning-words channel ───────────────────────────

async def get_automod_channel(guild_id: int) -> int | None:
    """Return the saved auto-mod channel ID for a guild, or None if not set."""
    col = _get_collection()
    doc = await col.find_one({"_id": guild_id}, {"automod_channel_id": 1})
    return doc.get("automod_channel_id") if doc else None


async def set_automod_channel(guild_id: int, channel_id: int) -> None:
    """Persist the auto-mod channel ID for a guild."""
    col = _get_collection()
    await col.update_one(
        {"_id": guild_id},
        {"$set": {"automod_channel_id": channel_id}},
        upsert=True,
    )


# ── Economy / drops channel ───────────────────────────────────────

async def get_drops_channel(guild_id: int) -> int | None:
    """Return the saved drops channel ID for a guild, or None if not set."""
    col = _get_collection()
    doc = await col.find_one({"_id": guild_id}, {"drops_channel_id": 1})
    return doc.get("drops_channel_id") if doc else None


async def set_drops_channel(guild_id: int, channel_id: int) -> None:
    """Persist the drops channel ID for a guild."""
    col = _get_collection()
    await col.update_one(
        {"_id": guild_id},
        {"$set": {"drops_channel_id": channel_id}},
        upsert=True,
    )
