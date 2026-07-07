"""
Discord bot entry point.

Setup:
  1. Copy .env.example to .env and fill in BOT_TOKEN (and optionally OWNER_ID, DEV_GUILD_ID).
  2. Install dependencies:  pip install -r requirements.txt
  3. Run the bot:           python bot.py

Slash commands:
  - By default, commands sync globally (may take up to 1 hour on first run).
  - Set DEV_GUILD_ID in .env to sync instantly to a single test server during development.
"""

import asyncio
import logging
import threading

from flask import Flask
import discord
from discord.ext import commands

import config

# --- Keep-alive web server for Railway health checks ---
_flask_app = Flask(__name__)


@_flask_app.route("/")
def _index():
    return "I am alive", 200


def _start_web_server() -> None:
    thread = threading.Thread(
        target=lambda: _flask_app.run(host="0.0.0.0", port=10000),
        daemon=True,
    )
    thread.start()
    log.info("Keep-alive web server started on port 10000")


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bot")

COGS = [
    "cogs.moderation",
    "cogs.events",
    "cogs.server_drops_economy",
    "cogs.family_tree",
    "cogs.crush",
    "cogs.dna_test",
    "cogs.profile",
    "cogs.jail",
]


class Bot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()

        # --- Privileged intents (opt-in) ---
        # To enable these, go to https://discord.com/developers/applications
        # → your app → Bot → Privileged Gateway Intents and toggle them on.
        intents.members = True          # family tree, profiles, member events
        intents.message_content = True  # economy drops and event handlers

        super().__init__(
            command_prefix=config.BOT_PREFIX,
            intents=intents,
            owner_id=config.OWNER_ID or None,
            help_command=None,
        )

    async def setup_hook(self) -> None:
        for cog in COGS:
            try:
                await self.load_extension(cog)
                log.info("Loaded cog: %s", cog)
            except Exception as exc:
                log.error("Failed to load cog %s: %s", cog, exc, exc_info=True)

        if config.DEV_GUILD_ID:
            guild = discord.Object(id=config.DEV_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info(
                "Synced %d slash command(s) to dev guild %s",
                len(synced),
                config.DEV_GUILD_ID,
            )
        else:
            synced = await self.tree.sync()
            log.info("Synced %d slash command(s) globally", len(synced))

    async def on_ready(self) -> None:
        assert self.user is not None
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        log.info("Connected to %d guild(s)", len(self.guilds))
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="your family grow",
            )
        )


async def main() -> None:
    _start_web_server()
    bot = Bot()
    async with bot:
        await bot.start(config.BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
