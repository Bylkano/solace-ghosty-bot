import datetime
import logging
import sys
import pathlib
from collections import defaultdict

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import discord
from discord import app_commands
from discord.ext import commands

from store import get_automod_channel

log = logging.getLogger("bot.events")

LOCKDOWN_PREFIXES: tuple[str, ...] = ("?area", "?c4", "?checkers")

BLOCKED_PREFIXES: tuple[str, ...] = (
    "?send ghosty",
    "?give ghosty",
    "?send <@458302301187342336>",
    "?give <@458302301187342336>",
    "?riddle",
    "?daily",
    "?send 458302301187342336",
    "? send ghosty",
    "? give ghosty",
    "? send <@458302301187342336>",
    "? give <@458302301187342336>",
    "? riddle",
    "? daily",
    "? send 458302301187342336",
    "?give 458302301187342336",
    "? give 458302301187342336",
)

# Warn settings
SHORT_MUTE_MINUTES = 3      # mute duration per warn
LONG_MUTE_MINUTES = 30      # mute duration at max warns
MAX_WARNS = 3               # warns before long mute


class Events(commands.Cog):
    """Handles Discord gateway events and auto-moderation."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # {guild_id: {user_id: warn_count}}
        self._warns: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))

    # ------------------------------------------------------------------
    # Internal warn helper
    # ------------------------------------------------------------------

    async def _apply_warn(self, member: discord.Member, reason: str) -> None:
        """Increment warn counter, mute the member, and DM them a report.

        * 1st / 2nd warn  → 3-minute timeout + DM with warns remaining
        * 3rd warn        → 30-minute timeout, warns reset to 0
        """
        guild_id = member.guild.id
        self._warns[guild_id][member.id] += 1
        warn_count = self._warns[guild_id][member.id]

        if warn_count >= MAX_WARNS:
            self._warns[guild_id][member.id] = 0
            mute_duration = datetime.timedelta(minutes=LONG_MUTE_MINUTES)
            mute_reason = f"Automatic: reached {MAX_WARNS} warns ({reason})"
            dm_embed = discord.Embed(
                title="🔇 You've been muted for 30 minutes",
                description=(
                    f"You reached **{MAX_WARNS} warns** in **{member.guild.name}**.\n\n"
                    f"**Reason:** {reason}\n\n"
                    f"You have been muted for **{LONG_MUTE_MINUTES} minutes**.\n"
                    "Your warn count has been reset to **0**."
                ),
                color=discord.Color.dark_red(),
            )
        else:
            warns_left = MAX_WARNS - warn_count
            mute_duration = datetime.timedelta(minutes=SHORT_MUTE_MINUTES)
            mute_reason = f"Automatic: warn {warn_count}/{MAX_WARNS} ({reason})"
            dm_embed = discord.Embed(
                title=f"⚠️ Warning {warn_count}/{MAX_WARNS}",
                description=(
                    f"You received a warning in **{member.guild.name}**.\n\n"
                    f"**Reason:** {reason}\n\n"
                    f"You have been muted for **{SHORT_MUTE_MINUTES} minutes**.\n\n"
                    f"⚠️ You have **{warns_left} warn(s) remaining** before a "
                    f"**{LONG_MUTE_MINUTES}-minute mute**."
                ),
                color=discord.Color.orange(),
            )

        dm_embed.set_footer(text=f"Server: {member.guild.name}")

        try:
            await member.timeout(mute_duration, reason=mute_reason)
        except discord.Forbidden:
            pass

        try:
            await member.send(embed=dm_embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ------------------------------------------------------------------
    # Event listeners
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        await self._check_blocked_prefixes(message)
        await self._check_lockdown_phrases(message)

    async def _check_blocked_prefixes(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        automod_channel_id = get_automod_channel(message.guild.id)
        if automod_channel_id is None or message.channel.id != automod_channel_id:
            return
        if not message.content.lower().startswith(BLOCKED_PREFIXES):
            return
        if not isinstance(message.author, discord.Member):
            return
        try:
            await message.delete()
        except discord.Forbidden:
            return
        await self._apply_warn(message.author, reason="blocked command used")

    async def _check_lockdown_phrases(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        automod_channel_id = get_automod_channel(message.guild.id)
        if automod_channel_id is None or message.channel.id != automod_channel_id:
            return
        if not message.content.lower().startswith(LOCKDOWN_PREFIXES):
            return
        if not isinstance(message.author, discord.Member):
            return
        try:
            await message.delete()
        except discord.Forbidden:
            return
        await self._apply_warn(message.author, reason="lockdown phrase used")

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    @app_commands.command(name="blocklist", description="Show all currently blocked message prefixes.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    async def blocklist(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="🚫 Blocked Prefixes",
            description=(
                f"Messages starting with any of the following are automatically deleted "
                f"and the sender receives a **warn** + **{SHORT_MUTE_MINUTES}-minute timeout**.\n"
                f"At **{MAX_WARNS} warns** the user is muted for **{LONG_MUTE_MINUTES} minutes** "
                "and their warns reset to 0."
            ),
            color=discord.Color.red(),
        )
        entries = "\n".join(f"`{prefix}`" for prefix in BLOCKED_PREFIXES)
        embed.add_field(name=f"{len(BLOCKED_PREFIXES)} active rule(s)", value=entries, inline=False)
        embed.set_footer(text="Edit cogs/events.py → BLOCKED_PREFIXES to add or remove rules.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="warns", description="Check how many warns a member has.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    async def warns(self, interaction: discord.Interaction, member: discord.Member) -> None:
        count = self._warns[interaction.guild_id][member.id]
        remaining = MAX_WARNS - count
        embed = discord.Embed(
            title=f"⚠️ Warns for {member.display_name}",
            description=(
                f"Current warns: **{count}/{MAX_WARNS}**\n"
                f"Warns until {LONG_MUTE_MINUTES}-min mute: **{remaining}**"
            ),
            color=discord.Color.orange(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="clearwarns", description="Reset a member's warn count to 0.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def clearwarns(self, interaction: discord.Interaction, member: discord.Member) -> None:
        self._warns[interaction.guild_id][member.id] = 0
        await interaction.response.send_message(
            f"✅ Warns for **{member.display_name}** have been reset to 0.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # Error handler
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You don't have permission to use this command.")
        elif isinstance(error, commands.BotMissingPermissions):
            await ctx.send("❌ I don't have the required permissions to do that.")
        else:
            await ctx.send(f"❌ An error occurred: `{error}`")
            raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Events(bot))
