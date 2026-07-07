"""
profile.py – Solace Profile & Anniversary Cog
==============================================
Drop alongside the other cogs and add "cogs.profile" to the COGS list in bot.py.

Commands
--------
  /profile    [@member]   – Combined social card (marriage + crush + family + bio)
  /setbio     [text]      – Set your profile bio (max 150 chars; omit to clear)
  /anniversary            – Check your wedding anniversary; public celebration on milestones

Depends on: family_tree_db, crush_db, profile_db
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import discord
from discord import app_commands
from discord.ext import commands

import cogs.family_tree_db as ft_db
import cogs.crush_db as crush_db
import cogs.profile_db as profile_db

log = logging.getLogger("bot.profile")

# ---------------------------------------------------------------------------
# Milestone table  (days → (emoji, label))
# ---------------------------------------------------------------------------

_MILESTONES: dict[int, tuple[str, str]] = {
    7:    ("🌸", "1 Week Anniversary"),
    30:   ("🌹", "1 Month Anniversary"),
    100:  ("💫", "100 Days Together"),
    365:  ("🎂", "1 Year Anniversary"),
    500:  ("✨", "500 Days Together"),
    730:  ("👑", "2 Year Anniversary"),
    1000: ("🏆", "1000 Days Together"),
}
_MILESTONE_DAYS = sorted(_MILESTONES)  # [7, 30, 100, 365, 500, 730, 1000]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _avatar_url(member: discord.Member | discord.User) -> str:
    return member.display_avatar.with_format("png").with_size(128).url


async def _run(func, *args):
    """Run a synchronous DB call off the event loop."""
    return await asyncio.get_running_loop().run_in_executor(None, func, *args)


def _normalise_dt(dt) -> datetime:
    """Return a UTC-aware datetime from whatever psycopg2 gives us."""
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class Profile(commands.Cog):
    """Solace Profile – social card, bio, and anniversary celebrations."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /profile ──────────────────────────────────────────────────────────

    @app_commands.command(name="profile", description="👤 View a member's social profile card.")
    @app_commands.describe(member="The member to view (defaults to you).")
    @app_commands.guild_only()
    async def profile(
        self, interaction: discord.Interaction, member: discord.Member | None = None
    ):
        await interaction.response.defer()

        target   = member or interaction.user
        guild_id = interaction.guild_id

        # Fetch all data concurrently
        marriage, crush_info, parents, children, prof = await asyncio.gather(
            _run(ft_db.get_marriage,      guild_id, target.id),
            _run(crush_db.get_crush_info, guild_id, target.id),
            _run(ft_db.get_parents,       guild_id, target.id),
            _run(ft_db.get_children,      guild_id, target.id),
            _run(profile_db.get_profile,  guild_id, target.id),
        )

        bio = (prof["bio"] if prof else "") or ""

        embed = discord.Embed(color=0x7289DA)
        embed.set_author(
            name=f"{target.display_name}'s Profile",
            icon_url=_avatar_url(target),
        )
        embed.set_thumbnail(url=_avatar_url(target))

        if bio:
            embed.description = f"*{bio}*"

        # ── Marriage ───────────────────────────────────────────────────────
        if marriage:
            spouse_id = (
                marriage["user2_id"]
                if marriage["user1_id"] == target.id
                else marriage["user1_id"]
            )
            spouse     = interaction.guild.get_member(spouse_id)
            spouse_str = spouse.mention if spouse else f"<@{spouse_id}>"
            emoji, stage = ft_db.get_relationship_stage(marriage["married_at"])
            married_at   = _normalise_dt(marriage["married_at"])
            days         = (datetime.now(timezone.utc) - married_at).days

            embed.add_field(
                name="💍 Married",
                value=f"{spouse_str}\n{emoji} {stage} · **{days}** day{'s' if days != 1 else ''}",
                inline=True,
            )
            embed.color = 0xFF6B9D
        else:
            embed.add_field(name="💍 Married", value="*Single*", inline=True)

        # ── Crush ──────────────────────────────────────────────────────────
        if crush_info:
            crush_id, days_crush = crush_info
            is_mutual = await _run(crush_db.is_mutual_crush, guild_id, target.id, crush_id)
            crush_m   = interaction.guild.get_member(crush_id)
            crush_str = crush_m.mention if crush_m else f"<@{crush_id}>"

            if is_mutual and days_crush >= 10:
                crush_val = f"💑 Dating {crush_str}!\n*{days_crush} days mutual*"
            elif is_mutual:
                crush_val = f"💞 Mutual with {crush_str}\n*{days_crush} days*"
            else:
                crush_val = f"💕 {crush_str}\n*Secret crush*"
            embed.add_field(name="💕 Crush", value=crush_val, inline=True)
        else:
            embed.add_field(name="💕 Crush", value="*None*", inline=True)

        # ── Family summary ─────────────────────────────────────────────────
        family_lines: list[str] = []
        if parents:
            parent_strs = []
            for pid in parents:
                pm = interaction.guild.get_member(pid)
                parent_strs.append(pm.mention if pm else f"<@{pid}>")
            family_lines.append(f"👨‍👩‍👧 Parents: {', '.join(parent_strs)}")
        if children:
            family_lines.append(f"👶 Children: **{len(children)}**")
        if family_lines:
            embed.add_field(name="🏠 Family", value="\n".join(family_lines), inline=False)

        embed.set_footer(text="Tip: /setbio to add a bio  •  /anniversary to celebrate!")
        await interaction.followup.send(embed=embed)

    # ── /setbio ───────────────────────────────────────────────────────────

    @app_commands.command(
        name="setbio",
        description="✏️ Set your profile bio (max 150 chars). Leave blank to clear.",
    )
    @app_commands.describe(bio="Your bio text (leave empty to clear).")
    @app_commands.guild_only()
    async def setbio(self, interaction: discord.Interaction, bio: str = ""):
        await interaction.response.defer(ephemeral=True)

        bio = bio.strip()
        if len(bio) > 150:
            return await interaction.followup.send(
                f"❌ Bio must be 150 characters or fewer (yours is **{len(bio)}**).",
                ephemeral=True,
            )

        await _run(profile_db.set_bio, interaction.guild_id, interaction.user.id, bio)

        if bio:
            await interaction.followup.send(
                embed=discord.Embed(
                    description=f"✅ Bio updated!\n> *{bio}*",
                    color=0x44BB88,
                ),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                embed=discord.Embed(description="✅ Bio cleared.", color=0x44BB88),
                ephemeral=True,
            )

    # ── /anniversary ──────────────────────────────────────────────────────

    @app_commands.command(
        name="anniversary",
        description="💍 Check your wedding anniversary — milestones get a public celebration!",
    )
    @app_commands.guild_only()
    async def anniversary(self, interaction: discord.Interaction):
        await interaction.response.defer()

        guild_id = interaction.guild_id
        user     = interaction.user

        marriage = await _run(ft_db.get_marriage, guild_id, user.id)
        if not marriage:
            return await interaction.followup.send(
                embed=discord.Embed(
                    description="❌ You're not married! Use `/marry` to find your forever person. 💍",
                    color=0x445566,
                ),
                ephemeral=True,
            )

        spouse_id  = (
            marriage["user2_id"]
            if marriage["user1_id"] == user.id
            else marriage["user1_id"]
        )
        spouse     = interaction.guild.get_member(spouse_id)
        spouse_str = spouse.mention if spouse else f"<@{spouse_id}>"

        married_at   = _normalise_dt(marriage["married_at"])
        now          = datetime.now(timezone.utc)
        days         = (now - married_at).days
        emoji, stage = ft_db.get_relationship_stage(married_at)

        # Check if today is exactly a milestone
        milestone = _MILESTONES.get(days)

        # Next upcoming milestone
        next_ms      = next((m for m in _MILESTONE_DAYS if m > days), None)
        days_to_next = (next_ms - days) if next_ms else None

        if milestone:
            # Public celebration
            ms_emoji, ms_label = milestone
            embed = discord.Embed(
                title=f"{ms_emoji} {ms_label}!",
                description=(
                    f"🎊 {user.mention} and {spouse_str} are celebrating "
                    f"**{days}** day{'s' if days != 1 else ''} together!\n\n"
                    f"{emoji} Stage: **{stage}**\n"
                    f"💍 Married: {discord.utils.format_dt(married_at, style='D')}"
                ),
                color=0xFFD700,
            )
            embed.set_footer(text="Congratulations to this wonderful couple! 🥂")
            await interaction.followup.send(embed=embed)
        else:
            embed = discord.Embed(
                title="💍 Anniversary Stats",
                description=(
                    f"💑 {user.mention} × {spouse_str}\n"
                    f"📅 Married: {discord.utils.format_dt(married_at, style='D')}\n"
                    f"⏳ Together: **{days}** day{'s' if days != 1 else ''}\n"
                    f"{emoji} Stage: **{stage}**"
                ),
                color=0xFF6B9D,
            )
            if days_to_next and next_ms:
                embed.add_field(
                    name="🎯 Next Milestone",
                    value=(
                        f"**{next_ms} days** — only **{days_to_next}** more to go!\n"
                        f"*{_MILESTONES[next_ms][1]}*"
                    ),
                    inline=False,
                )
            embed.set_footer(
                text="Use /anniversary on a milestone day for a public celebration! 🎊"
            )
            await interaction.followup.send(embed=embed)


# ---------------------------------------------------------------------------
# Extension entry point
# ---------------------------------------------------------------------------

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Profile(bot))
    log.info("Profile cog loaded.")
