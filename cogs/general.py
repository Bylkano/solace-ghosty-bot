import time
import discord
from discord import app_commands
from discord.ext import commands


class General(commands.Cog):
    """General-purpose commands available to everyone."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="ping", description="Check the bot's latency.")
    async def ping(self, interaction: discord.Interaction) -> None:
        latency_ms = round(self.bot.latency * 1000)
        await interaction.response.send_message(
            f"🏓 Pong! Latency: **{latency_ms}ms**", ephemeral=True
        )

    @app_commands.command(name="hello", description="The bot says hello.")
    async def hello(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            f"Hey {interaction.user.mention}! 👋", ephemeral=True
        )

    @app_commands.command(name="serverinfo", description="Show information about this server.")
    @app_commands.guild_only()
    async def serverinfo(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        embed = discord.Embed(
            title=guild.name,
            description=guild.description or "No description set.",
            color=discord.Color.blurple(),
        )
        if guild.icon:
            embed.set_thumbnail(url=guild.icon.url)

        embed.add_field(name="Owner", value=f"<@{guild.owner_id}>", inline=True)
        embed.add_field(name="Members", value=str(guild.member_count), inline=True)
        embed.add_field(name="Channels", value=str(len(guild.channels)), inline=True)
        embed.add_field(name="Roles", value=str(len(guild.roles)), inline=True)
        embed.add_field(
            name="Created",
            value=discord.utils.format_dt(guild.created_at, style="D"),
            inline=True,
        )
        embed.set_footer(text=f"Server ID: {guild.id}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="userinfo", description="Show information about a user.")
    @app_commands.guild_only()
    async def userinfo(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        target = member or interaction.user
        if not isinstance(target, discord.Member):
            await interaction.response.send_message("Could not resolve member.", ephemeral=True)
            return

        embed = discord.Embed(
            title=str(target),
            color=target.color if target.color != discord.Color.default() else discord.Color.blurple(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Display Name", value=target.display_name, inline=True)
        embed.add_field(name="Bot", value="Yes" if target.bot else "No", inline=True)
        embed.add_field(
            name="Joined Server",
            value=discord.utils.format_dt(target.joined_at, style="D") if target.joined_at else "Unknown",
            inline=True,
        )
        embed.add_field(
            name="Account Created",
            value=discord.utils.format_dt(target.created_at, style="D"),
            inline=True,
        )
        roles = [r.mention for r in reversed(target.roles) if r.name != "@everyone"]
        if roles:
            embed.add_field(name=f"Roles ({len(roles)})", value=" ".join(roles[:10]), inline=False)
        embed.set_footer(text=f"User ID: {target.id}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="avatar", description="Get a user's avatar.")
    async def avatar(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        target = member or interaction.user
        embed = discord.Embed(title=f"{target}'s avatar", color=discord.Color.blurple())
        embed.set_image(url=target.display_avatar.url)
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(General(bot))
