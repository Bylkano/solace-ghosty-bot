import logging
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import discord
from discord import app_commands
from discord.ext import commands

from store import (
    get_welcome_channel, set_welcome_channel,
    get_leave_channel, set_leave_channel,
)

log = logging.getLogger("bot.welcome")


class Welcome(commands.Cog):
    """Configurable welcome and leave message system."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        channel_id = get_welcome_channel(member.guild.id)
        if channel_id is None:
            return
        channel = member.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        embed = discord.Embed(
            title="👋 Welcome!",
            description=(
                f"Welcome to **{member.guild.name}**, {member.mention}!\n\n"
                f"You are member **#{member.guild.member_count}**. "
                f"We're glad to have you here!"
            ),
            color=discord.Color.green(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"{member.guild.name} • Member #{member.guild.member_count}")
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            log.warning("Missing permission to send welcome message in channel %s", channel_id)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        channel_id = get_leave_channel(member.guild.id)
        if channel_id is None:
            return
        channel = member.guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        embed = discord.Embed(
            title="👋 Goodbye!",
            description=(
                f"**{member.display_name}** has left **{member.guild.name}**.\n\n"
                f"We now have **{member.guild.member_count}** members."
            ),
            color=discord.Color.red(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.set_footer(text=f"{member.guild.name}")
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            log.warning("Missing permission to send leave message in channel %s", channel_id)

    # ------------------------------------------------------------------
    # Slash commands — welcome
    # ------------------------------------------------------------------

    @app_commands.command(name="setwelcome", description="Set the channel where welcome messages are sent.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def setwelcome(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        assert interaction.guild_id is not None
        try:
            set_welcome_channel(interaction.guild_id, channel.id)
        except Exception as exc:
            log.error("setwelcome: failed for guild %s: %s", interaction.guild_id, exc)
            await interaction.response.send_message(
                f"❌ Failed to save channel: `{exc}`", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"✅ Welcome messages will now be sent in {channel.mention}.", ephemeral=True
        )

    @app_commands.command(name="getwelcome", description="Show which channel welcome messages are sent to.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def getwelcome(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        channel_id = get_welcome_channel(interaction.guild_id)
        if channel_id is None:
            await interaction.response.send_message(
                "⚠️ No welcome channel set. Use `/setwelcome` to configure one.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"👋 Welcome messages are being sent to <#{channel_id}>.", ephemeral=True
            )

    @app_commands.command(name="disablewelcome", description="Disable welcome messages for this server.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def disablewelcome(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        from store import _set_value
        try:
            from store import _connect
            with _connect() as con:
                with con.cursor() as cur:
                    cur.execute(
                        "DELETE FROM guild_config WHERE guild_id = %s AND key = %s",
                        (interaction.guild_id, "welcome_channel_id"),
                    )
                con.commit()
        except Exception as exc:
            await interaction.response.send_message(f"❌ Failed: `{exc}`", ephemeral=True)
            return
        await interaction.response.send_message("✅ Welcome messages have been disabled.", ephemeral=True)

    # ------------------------------------------------------------------
    # Slash commands — leave
    # ------------------------------------------------------------------

    @app_commands.command(name="setleave", description="Set the channel where leave messages are sent.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def setleave(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        assert interaction.guild_id is not None
        try:
            set_leave_channel(interaction.guild_id, channel.id)
        except Exception as exc:
            log.error("setleave: failed for guild %s: %s", interaction.guild_id, exc)
            await interaction.response.send_message(
                f"❌ Failed to save channel: `{exc}`", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"✅ Leave messages will now be sent in {channel.mention}.", ephemeral=True
        )

    @app_commands.command(name="getleave", description="Show which channel leave messages are sent to.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def getleave(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        channel_id = get_leave_channel(interaction.guild_id)
        if channel_id is None:
            await interaction.response.send_message(
                "⚠️ No leave channel set. Use `/setleave` to configure one.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"🚪 Leave messages are being sent to <#{channel_id}>.", ephemeral=True
            )

    @app_commands.command(name="disableleave", description="Disable leave messages for this server.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def disableleave(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        try:
            from store import _connect
            with _connect() as con:
                with con.cursor() as cur:
                    cur.execute(
                        "DELETE FROM guild_config WHERE guild_id = %s AND key = %s",
                        (interaction.guild_id, "leave_channel_id"),
                    )
                con.commit()
        except Exception as exc:
            await interaction.response.send_message(f"❌ Failed: `{exc}`", ephemeral=True)
            return
        await interaction.response.send_message("✅ Leave messages have been disabled.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Welcome(bot))
