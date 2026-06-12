import logging
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import discord
from discord import app_commands
from discord.ext import commands

from store import get_automod_channel, set_automod_channel

log = logging.getLogger("bot.moderation")


class Moderation(commands.Cog):
    """Channel configuration for Ghosty auto-moderation."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

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
        try:
            set_automod_channel(interaction.guild_id, channel.id)
        except Exception as exc:
            log.error("setchannel: failed for guild %s: %s", interaction.guild_id, exc)
            await interaction.response.send_message(
                f"❌ Failed to save channel: `{exc}`", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"✅ Ghosty protection is now watching {channel.mention}.", ephemeral=True
        )

    # ------------------------------------------------------------------ #
    #  Channel lock / unlock                                             #
    # ------------------------------------------------------------------ #
    @app_commands.command(name="lock", description="Lock a channel so members can no longer send messages.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.describe(
        channel="Channel to lock (defaults to the current channel).",
        reason="Optional reason shown to members and in the audit log.",
    )
    async def lock(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        reason: str | None = None,
    ) -> None:
        assert interaction.guild is not None
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message(
                "❌ I can only lock standard text channels.", ephemeral=True
            )
            return

        everyone = interaction.guild.default_role
        overwrite = target.overwrites_for(everyone)
        if overwrite.send_messages is False:
            await interaction.response.send_message(
                f"🔒 {target.mention} is already locked.", ephemeral=True
            )
            return

        audit = f"Locked by {interaction.user} ({interaction.user.id})"
        if reason:
            audit += f" — {reason}"

        overwrite.send_messages = False
        overwrite.send_messages_in_threads = False
        overwrite.create_public_threads = False
        overwrite.create_private_threads = False
        try:
            await target.set_permissions(everyone, overwrite=overwrite, reason=audit)
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I can't edit that channel. I need the **Manage Channels** permission "
                "(and a role positioned above the members you want to lock out).",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            log.error("lock: failed for channel %s: %s", target.id, exc)
            await interaction.response.send_message(
                f"❌ Failed to lock channel: `{exc}`", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"🔒 Locked {target.mention}.", ephemeral=True
        )
        notice = discord.Embed(
            title="🔒 Channel Locked",
            description=reason or "This channel has been locked. Only staff can send messages right now.",
            color=discord.Color.red(),
        )
        notice.set_footer(text=f"Locked by {interaction.user.display_name}")
        try:
            await target.send(embed=notice)
        except discord.HTTPException:
            pass

    @app_commands.command(name="unlock", description="Unlock a channel so members can send messages again.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.describe(
        channel="Channel to unlock (defaults to the current channel).",
        reason="Optional reason shown in the audit log.",
    )
    async def unlock(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        reason: str | None = None,
    ) -> None:
        assert interaction.guild is not None
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message(
                "❌ I can only unlock standard text channels.", ephemeral=True
            )
            return

        everyone = interaction.guild.default_role
        overwrite = target.overwrites_for(everyone)
        if overwrite.send_messages is not False:
            await interaction.response.send_message(
                f"🔓 {target.mention} isn't locked.", ephemeral=True
            )
            return

        audit = f"Unlocked by {interaction.user} ({interaction.user.id})"
        if reason:
            audit += f" — {reason}"

        # Reset to inherit from category/role defaults rather than forcing allow.
        overwrite.send_messages = None
        overwrite.send_messages_in_threads = None
        overwrite.create_public_threads = None
        overwrite.create_private_threads = None
        try:
            await target.set_permissions(everyone, overwrite=overwrite, reason=audit)
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I can't edit that channel. I need the **Manage Channels** permission.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            log.error("unlock: failed for channel %s: %s", target.id, exc)
            await interaction.response.send_message(
                f"❌ Failed to unlock channel: `{exc}`", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"🔓 Unlocked {target.mention}.", ephemeral=True
        )
        notice = discord.Embed(
            title="🔓 Channel Unlocked",
            description="This channel is open again — members can send messages.",
            color=discord.Color.green(),
        )
        notice.set_footer(text=f"Unlocked by {interaction.user.display_name}")
        try:
            await target.send(embed=notice)
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
