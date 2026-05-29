import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import discord
from discord import app_commands
from discord.ext import commands

from store import get_automod_channel, set_automod_channel


class Moderation(commands.Cog):
    """Channel configuration for Ghosty auto-moderation."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="getchannel", description="Show which channel Ghosty protection is currently watching.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def getchannel(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        channel_id = await get_automod_channel(interaction.guild_id)
        if channel_id is None:
            await interaction.followup.send(
                "⚠️ No channel set yet. Use `/setchannel` to configure one.", ephemeral=True
            )
        else:
            await interaction.followup.send(
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
        await interaction.response.defer(ephemeral=True)
        await set_automod_channel(interaction.guild_id, channel.id)
        await interaction.followup.send(
            f"✅ Ghosty protection is now watching {channel.mention}.", ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
