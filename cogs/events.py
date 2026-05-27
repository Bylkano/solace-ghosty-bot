import datetime
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import discord
from discord import app_commands
from discord.ext import commands

from store import get_automod_channel

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


class Events(commands.Cog):
    """Handles Discord gateway events and auto-moderation."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Fires when a new member joins the server."""
        system_channel = member.guild.system_channel
        if system_channel is not None:
            embed = discord.Embed(
                title="Welcome!",
                description=f"Welcome to **{member.guild.name}**, {member.mention}! 🎉",
                color=discord.Color.green(),
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_footer(text=f"Member #{member.guild.member_count}")
            await system_channel.send(embed=embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Fires when a member leaves the server."""
        system_channel = member.guild.system_channel
        if system_channel is not None:
            await system_channel.send(
                f"👋 **{member}** has left the server. We now have {member.guild.member_count} members."
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Fires on every message. Avoid heavy logic here."""
        if message.author.bot:
            return

        await self._check_blocked_prefixes(message)
        await self._check_lockdown_phrases(message)
        await self.bot.process_commands(message)

    async def _check_blocked_prefixes(self, message: discord.Message) -> None:
        """Delete the message and timeout the author for 2 minutes."""
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

        try:
            await message.author.timeout(
                datetime.timedelta(minutes=2),
                reason="Automatic: blocked command used",
            )
        except discord.Forbidden:
            pass

    async def _check_lockdown_phrases(self, message: discord.Message) -> None:
        """Delete the message and timeout the author for 2 minutes."""
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

        try:
            await message.author.timeout(
                datetime.timedelta(minutes=2),
                reason="Automatic: lockdown phrase used",
            )
        except discord.Forbidden:
            pass

    @app_commands.command(name="blocklist", description="Show all currently blocked message prefixes.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    async def blocklist(self, interaction: discord.Interaction) -> None:
        """Display the active blocked prefix list."""
        embed = discord.Embed(
            title="🚫 Blocked Prefixes",
            description="Messages starting with any of the following are automatically deleted and the sender is timed out for 2 minutes.",
            color=discord.Color.red(),
        )

        entries = "\n".join(f"`{prefix}`" for prefix in BLOCKED_PREFIXES)
        embed.add_field(name=f"{len(BLOCKED_PREFIXES)} active rule(s)", value=entries, inline=False)
        embed.set_footer(text="Edit cogs/events.py → BLOCKED_PREFIXES to add or remove rules.")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        """Handles prefix command errors."""
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
