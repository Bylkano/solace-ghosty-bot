"""
crush.py – Crush System + Cheating Detection for Solace Bot
===========================================================
Drop into cogs/ and add "cogs.crush" to COGS in bot.py.

Commands
--------
  /crush   @user   – Set your crush (one at a time).
  /uncrush         – Remove your crush.
  /mycrush         – Privately view your current crush.
  /loyalty [@user] – Show a user's loyalty score.

Cheating Detection
------------------
  Automatically triggered when a married user:
    • Sets a crush on someone other than their spouse  (-15 loyalty)
    • Attempts to propose marriage to someone else      (-25 loyalty)
  The hook for marriage proposals lives here as `fire_cheat_event()`,
  imported and called by family_tree.py.
"""

from __future__ import annotations

import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import discord
from discord import app_commands
from discord.ext import commands

import asyncio

import cogs.crush_db as cdb

# Lazy import to avoid circular dependency; family_tree_db is only needed for
# marriage look-ups inside this cog.
try:
    import cogs.family_tree_db as ftdb
    _FT_AVAILABLE = True
except ImportError:
    _FT_AVAILABLE = False

# ── Loyalty bar helper ────────────────────────────────────────────────────────

def _loyalty_bar(score: int) -> str:
    filled = round(score / 10)
    color  = "🟢" if score >= 70 else ("🟡" if score >= 40 else "🔴")
    return f"{color} `{'█' * filled}{'░' * (10 - filled)}` **{score}/100**"


def _loyalty_label(score: int) -> str:
    if score == 100: return "💯 Perfectly Loyal"
    if score >= 85:  return "😇 Mostly Trustworthy"
    if score >= 70:  return "🤔 Slightly Suspicious"
    if score >= 50:  return "😬 Raising Eyebrows"
    if score >= 30:  return "🚩 Certified Red Flag"
    if score >= 10:  return "😈 Chronically Unfaithful"
    return "💀 Beyond Redemption"


# ── Cheating event (called externally by family_tree.py) ─────────────────────

_CHEAT_QUIPS: list[str] = [
    "The audacity. The absolute audacity.",
    "Local spouse has no idea. *Yet.*",
    "This is why we can't have nice things.",
    "Somewhere, a lawyer is smiling.",
    "Bold move. Extremely bold move.",
    "Science has yet to explain this level of confidence.",
    "Their villain arc is fully unlocked.",
    "A moment of silence for whoever trusted this person.",
    "Historians will study this event for generations.",
    "Chat, are we witnessing the beginning of a telenovela?",
]

_CHEAT_COLORS = {
    "crush":   discord.Color.from_rgb(255, 80, 80),
    "propose": discord.Color.from_rgb(200, 0, 0),
}


async def fire_cheat_event(
    bot: commands.Bot,
    guild_id: int,
    channel: discord.abc.Messageable | None,
    cheater: discord.Member,
    target_id: int,
    spouse_id: int,
    reason: str,  # "crush" | "propose"
) -> None:
    """
    Reduce the cheater's loyalty score and post a public cheating notification.

    Parameters
    ----------
    bot       – The running bot instance.
    guild_id  – Guild where the event occurred.
    channel   – Text channel to post in (skipped if None or no permission).
    cheater   – The Member who cheated.
    target_id – The user they cheated *with*.
    spouse_id – Their current spouse's ID.
    reason    – "crush" or "propose".
    """
    penalty = {"crush": 15, "propose": 25}.get(reason, 10)
    loop = asyncio.get_running_loop()
    new_score = await loop.run_in_executor(None, cdb.reduce_loyalty, guild_id, cheater.id, penalty)

    guild = bot.get_guild(guild_id)
    target = guild.get_member(target_id) if guild else None
    spouse = guild.get_member(spouse_id) if guild else None

    target_str = target.mention if target else f"<@{target_id}>"
    spouse_str = spouse.mention if spouse else f"<@{spouse_id}>"

    if reason == "crush":
        headline = (
            f"💘 **{cheater.display_name}** developed a secret crush on {target_str} "
            f"while married to {spouse_str}!"
        )
    else:
        headline = (
            f"💍 **{cheater.display_name}** tried to propose to {target_str} "
            f"while still married to {spouse_str}!"
        )

    embed = discord.Embed(
        title="🚨 Cheating Detected!",
        description=headline,
        color=_CHEAT_COLORS.get(reason, discord.Color.red()),
    )
    embed.add_field(
        name="💔 Loyalty Score",
        value=_loyalty_bar(new_score),
        inline=False,
    )
    embed.add_field(
        name="⚠️ Suspicious Romantic Activity Detected",
        value=f"*{random.choice(_CHEAT_QUIPS)}*",
        inline=False,
    )
    embed.set_thumbnail(url=cheater.display_avatar.url)
    embed.set_footer(text=f"Loyalty -{penalty} pts")

    if channel:
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass


# ── Cog ───────────────────────────────────────────────────────────────────────

class CrushSystem(commands.Cog):
    """💘 Crush System, Cheating Detection, and Loyalty Scores."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        cdb.init_tables()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _get_spouse_id(self, guild_id: int, user_id: int) -> int | None:
        """Return the spouse's user ID, or None if not married."""
        if not _FT_AVAILABLE:
            return None
        marriage = ftdb.get_marriage(guild_id, user_id)
        if not marriage:
            return None
        return (
            marriage["user2_id"]
            if marriage["user1_id"] == user_id
            else marriage["user1_id"]
        )

    async def _run(self, func, *args):
        """Run a synchronous DB call in a thread pool."""
        import asyncio
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, func, *args)

    # ── /crush ────────────────────────────────────────────────────────────────

    @app_commands.command(name="crush", description="💘 Set your secret crush.")
    @app_commands.describe(member="The person you have a crush on.")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 30.0, key=lambda i: (i.guild_id, i.user.id))
    async def crush(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await interaction.response.defer(ephemeral=True)

        guild_id  = interaction.guild_id
        user      = interaction.user

        if member.id == user.id:
            return await interaction.followup.send("💭 You can't crush on yourself... or can you? (No.)", ephemeral=True)
        if member.bot:
            return await interaction.followup.send("🤖 Bots don't have feelings. (Mostly.)", ephemeral=True)

        # Cheating detection: married user crushing on someone other than spouse
        spouse_id = await self._run(self._get_spouse_id, guild_id, user.id)
        if spouse_id and member.id != spouse_id:
            await fire_cheat_event(
                self.bot, guild_id, interaction.channel,
                user, member.id, spouse_id, "crush",
            )
            return await interaction.followup.send(
                "❌ You're married! Setting a crush on someone else got you caught. "
                "Check the channel for the damage report. 👀",
                ephemeral=True,
            )

        # Set the crush
        result = await self._run(cdb.set_crush, guild_id, user.id, member.id)

        if result == "already":
            return await interaction.followup.send(
                f"💭 You already have a crush on {member.mention}. Nothing changed.",
                ephemeral=True,
            )

        if result == "mutual":
            # Public mutual crush announcement
            embed = discord.Embed(
                title="💕 Mutual Crush!",
                description=(
                    f"{user.mention} and {member.mention} like each other! 👀\n\n"
                    f"*This could be the start of something beautiful… or chaotic.*"
                ),
                color=discord.Color.from_rgb(255, 105, 180),
            )
            embed.set_thumbnail(url=user.display_avatar.url)
            await interaction.channel.send(embed=embed)
            await interaction.followup.send("💕 It's mutual! Check the announcement above.", ephemeral=True)
        else:
            # Private confirmation only
            await interaction.followup.send(
                f"💘 You now have a crush on **{member.display_name}**. "
                f"Your secret is safe with me. 🤫",
                ephemeral=True,
            )

    # ── /uncrush ──────────────────────────────────────────────────────────────

    @app_commands.command(name="uncrush", description="💔 Remove your current crush.")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 15.0, key=lambda i: (i.guild_id, i.user.id))
    async def uncrush(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        had_crush = await self._run(cdb.remove_crush, interaction.guild_id, interaction.user.id)

        if had_crush:
            await interaction.followup.send(
                "💔 Your crush has been removed. Moving on is brave. 🫡",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "🤷 You don't have a crush right now. Nothing to remove.",
                ephemeral=True,
            )

    # ── /mycrush ──────────────────────────────────────────────────────────────

    @app_commands.command(name="mycrush", description="🫣 Privately check who your crush is.")
    @app_commands.guild_only()
    async def mycrush(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)

        crush_id = await self._run(cdb.get_crush, interaction.guild_id, interaction.user.id)

        if crush_id is None:
            return await interaction.followup.send(
                "💭 You don't have a crush right now. Use `/crush @user` to set one.",
                ephemeral=True,
            )

        crush_member = interaction.guild.get_member(crush_id)
        crush_str    = crush_member.mention if crush_member else f"<@{crush_id}>"
        is_mutual    = await self._run(cdb.is_mutual_crush, interaction.guild_id, interaction.user.id, crush_id)

        status = "💕 **Mutual crush!** They like you back." if is_mutual else "🤫 One-sided for now…"

        embed = discord.Embed(
            title="💘 Your Secret Crush",
            description=f"You have a crush on {crush_str}\n\n{status}",
            color=discord.Color.from_rgb(255, 105, 180),
        )
        embed.set_footer(text="Only you can see this.")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /loyalty ──────────────────────────────────────────────────────────────

    @app_commands.command(name="loyalty", description="💯 Check a user's loyalty score.")
    @app_commands.describe(member="Who to check (leave blank for yourself).")
    @app_commands.guild_only()
    async def loyalty(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        await interaction.response.defer()

        target   = member or interaction.user
        guild_id = interaction.guild_id
        score    = await self._run(cdb.get_loyalty, guild_id, target.id)

        # Only check marriage status if family_tree is available
        married_str = ""
        if _FT_AVAILABLE:
            marriage = await self._run(ftdb.get_marriage, guild_id, target.id)
            if marriage:
                spouse_id  = (marriage["user2_id"]
                              if marriage["user1_id"] == target.id
                              else marriage["user1_id"])
                spouse     = interaction.guild.get_member(spouse_id)
                spouse_str = spouse.mention if spouse else f"<@{spouse_id}>"
                married_str = f"\n💍 Married to {spouse_str}"

        embed = discord.Embed(
            title=f"💔 Loyalty Report — {target.display_name}",
            color=(
                discord.Color.green() if score >= 70
                else discord.Color.yellow() if score >= 40
                else discord.Color.red()
            ),
        )
        embed.add_field(name="Score",  value=_loyalty_bar(score), inline=False)
        embed.add_field(name="Status", value=_loyalty_label(score) + married_str, inline=False)
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text="Loyalty decreases when romantic betrayals are detected.")
        await interaction.followup.send(embed=embed)

    # ── Cooldown error handler ────────────────────────────────────────────────

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            msg = f"⏳ Slow down, Romeo. Try again in **{error.retry_after:.0f}s**."
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        else:
            raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CrushSystem(bot))
