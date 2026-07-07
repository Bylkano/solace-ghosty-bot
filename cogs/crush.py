"""
crush.py – Crush System for Solace Bot
=======================================
Drop into cogs/ and add "cogs.crush" to COGS in bot.py.

Commands
--------
  /crush   @user  – Publicly announce your crush.
  /uncrush        – Quietly remove your crush.
  /mycrush        – Publicly reveal your current crush (and dating status).

Dating Status
-------------
  If you keep the same crush for 10+ days it automatically upgrades
  to 💑 Dating, shown when either user runs /mycrush.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import discord
from discord import app_commands
from discord.ext import commands

import cogs.crush_db as cdb


# ── Cog ───────────────────────────────────────────────────────────────────────

class CrushSystem(commands.Cog):
    """💘 Crush System."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        cdb.init_tables()

    async def _run(self, func, *args):
        """Run a synchronous DB call off the event loop."""
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, func, *args)

    # ── /crush ────────────────────────────────────────────────────────────────

    @app_commands.command(name="crush", description="💘 Announce your crush to the server!")
    @app_commands.describe(member="The person you have a crush on.")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 30.0, key=lambda i: (i.guild_id, i.user.id))
    async def crush(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await interaction.response.defer()

        user = interaction.user

        if member.id == user.id:
            return await interaction.followup.send(
                "💭 You can't crush on yourself!", ephemeral=True
            )
        if member.bot:
            return await interaction.followup.send(
                "🤖 Bots don't have feelings.", ephemeral=True
            )

        result = await self._run(cdb.set_crush, interaction.guild_id, user.id, member.id)

        if result == "already":
            return await interaction.followup.send(
                f"💭 You already have a crush on {member.mention}.",
                ephemeral=True,
            )

        if result == "mutual":
            embed = discord.Embed(
                title="💕 Mutual Crush!",
                description=(
                    f"{user.mention} and {member.mention} have a crush on each other! 👀\n\n"
                    f"*Something special might be starting…*"
                ),
                color=discord.Color.from_rgb(255, 105, 180),
            )
            embed.set_thumbnail(url=user.display_avatar.url)
        else:
            embed = discord.Embed(
                title="💘 New Crush!",
                description=f"{user.mention} has a crush on {member.mention}! 🥺",
                color=discord.Color.from_rgb(255, 150, 200),
            )
            embed.set_thumbnail(url=user.display_avatar.url)

        await interaction.followup.send(embed=embed)

    # ── /uncrush ──────────────────────────────────────────────────────────────

    @app_commands.command(name="uncrush", description="💔 Remove your current crush.")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 15.0, key=lambda i: (i.guild_id, i.user.id))
    async def uncrush(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        had_crush = await self._run(cdb.remove_crush, interaction.guild_id, interaction.user.id)

        if had_crush:
            await interaction.followup.send(
                "💔 Your crush has been removed.", ephemeral=True
            )
        else:
            await interaction.followup.send(
                "🤷 You don't have a crush right now.", ephemeral=True
            )

    # ── /mycrush ──────────────────────────────────────────────────────────────

    @app_commands.command(name="mycrush", description="💑 Show everyone who your crush is.")
    @app_commands.guild_only()
    async def mycrush(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()

        info = await self._run(cdb.get_crush_info, interaction.guild_id, interaction.user.id)

        if info is None:
            return await interaction.followup.send(
                f"{interaction.user.mention} doesn't have a crush right now. 💭"
            )

        crush_id, days = info
        crush_member = interaction.guild.get_member(crush_id)
        crush_str    = crush_member.mention if crush_member else f"<@{crush_id}>"
        is_mutual    = await self._run(
            cdb.is_mutual_crush, interaction.guild_id, interaction.user.id, crush_id
        )

        is_dating = is_mutual and days >= 10

        if is_dating:
            title = "💑 Dating"
            status_line = (
                f"{interaction.user.mention} and {crush_str} have been crushing on each other "
                f"for **{days} days** — they're basically dating now! 💕"
            )
            color = discord.Color.from_rgb(220, 80, 180)
        elif is_mutual:
            title = "💕 Mutual Crush"
            status_line = (
                f"{interaction.user.mention} has a crush on {crush_str} — "
                f"and it's mutual! 👀"
            )
            color = discord.Color.from_rgb(255, 105, 180)
        else:
            title = "💘 Crush"
            status_line = f"{interaction.user.mention} has a crush on {crush_str}! 🥺"
            color = discord.Color.from_rgb(255, 150, 200)

        embed = discord.Embed(title=title, description=status_line, color=color)
        if is_dating:
            embed.set_footer(text=f"💕 {days} days and counting")
        elif is_mutual:
            embed.set_footer(text="Stay together for 10 days to unlock 💑 Dating!")
        else:
            embed.set_footer(text="10 days with the same crush unlocks 💑 Dating!")
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

        await interaction.followup.send(embed=embed)

    # ── Error handler ─────────────────────────────────────────────────────────

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"⏳ Slow down! Try again in **{error.retry_after:.0f}s**."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CrushSystem(bot))
