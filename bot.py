"""
bot.py – Solace Bot entry point
================================
Place this file at the root of your bot repo (same level as the cogs/ folder).

IMPORTANT — duplicate commands fix
-----------------------------------
Commands appearing twice in Discord almost always means the bot is syncing
to BOTH a specific guild AND globally, producing two copies.

The rule: sync once, in setup_hook, globally.
  ✅  await self.tree.sync()           ← global sync, called ONCE in setup_hook
  ❌  await self.tree.sync(guild=...)  ← guild sync (don't mix with global)
  ❌  await bot.tree.sync() in on_ready ← fires on reconnects → double-sync

If you previously double-synced, commands may persist for a few minutes
while Discord propagates the deletion. Wait ~5 min then run the bot fresh.

Environment
-----------
  DISCORD_TOKEN  – your bot token (required)
  DATABASE_URL   – postgres connection string (required by all DB cogs)
"""

import asyncio
import logging
import os

import discord
from discord.ext import commands

log = logging.getLogger("solace")

COGS = [
    "cogs.family_tree",   # /marry /divorce /adopt /disown /child /couple
                          # /family /tree /runaway /familystats /familyconfig
    "cogs.crush",         # /crush /uncrush /mycrush
    "cogs.dna_test",      # /dna
    "cogs.profile",       # /profile /setbio /anniversary
]


class SolaceBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.members = True   # needed to resolve display names in tree renders
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        """
        Called once by discord.py before the bot connects.
        Load cogs and sync the command tree exactly ONCE here.
        Never call tree.sync() in on_ready — that fires on every reconnect.
        """
        for cog in COGS:
            try:
                await self.load_extension(cog)
                log.info("✅ Loaded: %s", cog)
            except Exception as exc:
                log.error("❌ Failed to load %s: %s", cog, exc, exc_info=True)

        synced = await self.tree.sync()
        log.info("Synced %d app command(s) globally.", len(synced))

    async def on_ready(self) -> None:
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="your family grow 🌳",
            )
        )


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise ValueError("DISCORD_TOKEN environment variable not set.")

    bot = SolaceBot()
    async with bot:
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
