import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import discord
from discord import app_commands
from discord.ext import commands

from store import get_automod_channel, set_automod_channel


class Moderation(commands.Cog):
    """Moderation commands. Require appropriate permissions."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="kick", description="Kick a member from the server.")
    @app_commands.guild_only()
    @app_commands.default_permissions(kick_members=True)
    async def kick(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided.",
    ) -> None:
        if member == interaction.user:
            await interaction.response.send_message("You cannot kick yourself.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            return

        if not guild.me.guild_permissions.kick_members:
            await interaction.response.send_message(
                "I don't have permission to kick members.", ephemeral=True
            )
            return

        try:
            await member.kick(reason=f"{interaction.user}: {reason}")
            await interaction.response.send_message(
                f"✅ **{member}** has been kicked.\n**Reason:** {reason}"
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I cannot kick that member (they may have a higher role).", ephemeral=True
            )

    @app_commands.command(name="ban", description="Ban a member from the server.")
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    async def ban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str = "No reason provided.",
        delete_message_days: app_commands.Range[int, 0, 7] = 0,
    ) -> None:
        if member == interaction.user:
            await interaction.response.send_message("You cannot ban yourself.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            return

        if not guild.me.guild_permissions.ban_members:
            await interaction.response.send_message(
                "I don't have permission to ban members.", ephemeral=True
            )
            return

        try:
            await member.ban(
                reason=f"{interaction.user}: {reason}",
                delete_message_days=delete_message_days,
            )
            await interaction.response.send_message(
                f"🔨 **{member}** has been banned.\n**Reason:** {reason}"
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I cannot ban that member (they may have a higher role).", ephemeral=True
            )

    @app_commands.command(name="unban", description="Unban a user by their ID.")
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    async def unban(
        self,
        interaction: discord.Interaction,
        user_id: str,
        reason: str = "No reason provided.",
    ) -> None:
        guild = interaction.guild
        if guild is None:
            return

        try:
            user = await self.bot.fetch_user(int(user_id))
            await guild.unban(user, reason=f"{interaction.user}: {reason}")
            await interaction.response.send_message(
                f"✅ **{user}** has been unbanned.\n**Reason:** {reason}"
            )
        except ValueError:
            await interaction.response.send_message("Invalid user ID.", ephemeral=True)
        except discord.NotFound:
            await interaction.response.send_message(
                "That user is not banned or does not exist.", ephemeral=True
            )

    @app_commands.command(name="timeout", description="Timeout a member (mute them temporarily).")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    async def timeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        minutes: app_commands.Range[int, 1, 40320],
        reason: str = "No reason provided.",
    ) -> None:
        import datetime

        if member == interaction.user:
            await interaction.response.send_message("You cannot timeout yourself.", ephemeral=True)
            return

        try:
            duration = datetime.timedelta(minutes=minutes)
            await member.timeout(duration, reason=f"{interaction.user}: {reason}")
            await interaction.response.send_message(
                f"⏱️ **{member}** has been timed out for **{minutes} minute(s)**.\n**Reason:** {reason}"
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I cannot timeout that member (they may have a higher role).", ephemeral=True
            )

    @app_commands.command(name="purge", description="Delete a number of messages from the channel.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    async def purge(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 100],
    ) -> None:
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "This command can only be used in text channels.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(
            f"🗑️ Deleted **{len(deleted)}** message(s).", ephemeral=True
        )


    @app_commands.command(name="getchannel", description="Show which channel Ghosty protection is currently watching.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def getchannel(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        channel_id = get_automod_channel(interaction.guild_id)
        if channel_id is None:
            await interaction.response.send_message(
                "⚠️ No channel set yet. Use `/setchannel` to configure one.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"👻 Ghosty protection is currently watching <#{channel_id}>.", ephemeral=True
            )

    @app_commands.command(name="setchannel", description="Set the channel where Ghosty protection runs.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def setchannel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        assert interaction.guild_id is not None
        set_automod_channel(interaction.guild_id, channel.id)
        await interaction.response.send_message(
            f"✅ Ghosty protection is now watching {channel.mention}.", ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
