"""
Discord bot entry point.

Setup:
  1. Copy env.example to .env and fill in DISCORD_TOKEN + DATABASE_URL.
  2. Install dependencies:  pip install -r requirements.txt
  3. Run the bot:           python bot.py

On Render, set Environment variables (not .env in the repo):
  DISCORD_TOKEN (or BOT_TOKEN), DATABASE_URL, OWNER_ID, optional DEV_GUILD_ID / DEEPINFRA_TOKEN.

Slash commands:
  - Always sync globally on startup so removed cogs disappear from Discord's
    command list (global updates can take up to ~1 hour to propagate).
  - If DEV_GUILD_ID is set, also sync that guild for an instant local update.
"""

import asyncio
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import discord
from discord.ext import commands

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bot")


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def _start_port_server() -> None:
    """Bind PORT so Render Web Services pass the port scan."""
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("Health server listening on port %s", port)

COGS = [
    "cogs.moderation",
    "cogs.events",
    # Economy / event drops — Solace event finished; leave unloaded.
    # Re-enable by uncommenting: "cogs.server_drops_economy",
    # "cogs.server_drops_economy",
    "cogs.family_tree",
    "cogs.crush",
    "cogs.dna_test",
    "cogs.profile",
    "cogs.jail",
    "cogs.oos_loans",
    "cogs.plato_tournament",
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

        # Always sync globally. Unloaded cogs (e.g. economy drops) are not in
        # the tree, so Discord drops those slash commands from the global list.
        # If we only sync a DEV_GUILD_ID, stale global commands stay visible.
        synced = await self.tree.sync()
        log.info("Synced %d slash command(s) globally", len(synced))

        if config.DEV_GUILD_ID:
            guild = discord.Object(id=config.DEV_GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            guild_synced = await self.tree.sync(guild=guild)
            log.info(
                "Synced %d slash command(s) to guild %s (instant)",
                len(guild_synced),
                config.DEV_GUILD_ID,
            )

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
    _start_port_server()
    bot = Bot()
    async with bot:
        await bot.start(config.BOT_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
