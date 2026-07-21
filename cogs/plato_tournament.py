"""
plato_tournament.py – Plato group-stage tournament (slash commands).

Admin:
  /creategroup
  /result
  /editresult
  /removeresult
  /plato-reset

Everyone:
  /ranking
  /matches
  /platohelp
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import re
import sys
from typing import Optional

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import discord
from discord import app_commands
from discord.ext import commands

import cogs.plato_tournament_db as db

log = logging.getLogger("bot.plato_tournament")

COLOR = 0x5B8DEF
MEDALS = ("🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣")
GAMES = (
    "🎳 Bowling",
    "🎱 Pool",
    "⛳ Golf",
    "🎯 Darts",
    "⚽ Soccer",
    "🏹 Archery",
    "🏀 Basketball",
)

SCORE_RE = re.compile(r"^([012])\s*[-:]\s*([012])$")

GROUP_CHOICES = [
    app_commands.Choice(name="Group A", value="A"),
    app_commands.Choice(name="Group B", value="B"),
    app_commands.Choice(name="Group C", value="C"),
    app_commands.Choice(name="Group D", value="D"),
]


def _parse_score(raw: str) -> tuple[int, int]:
    m = SCORE_RE.match(raw.strip())
    if not m:
        raise ValueError("score must look like `2-0`, `1-1`, or `0-2`")
    a, b = int(m.group(1)), int(m.group(2))
    if a + b != 2:
        raise ValueError("scores must add up to 2 (2-0, 1-1, or 0-2)")
    return a, b


def _display_name(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    return member.display_name if member else f"User {user_id}"


class PlatoTournament(commands.Cog):
    """🏆 Plato tournament group stage."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        db.init_tables()

    async def _run(self, func, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, func, *args)

    def _ranking_embed(
        self, guild: discord.Guild, group: str, rows: list[dict]
    ) -> discord.Embed:
        embed = discord.Embed(title=f"🏆 Group {group} Ranking", color=COLOR)
        if not rows:
            embed.description = (
                "No players in this group yet. An admin can use `/creategroup`."
            )
            return embed

        lines: list[str] = []
        for row in rows:
            pos = row["position"]
            medal = MEDALS[pos - 1] if 1 <= pos <= len(MEDALS) else f"{pos}."
            name = _display_name(guild, row["user_id"])
            pts = int(row["points"])
            mark = "✅" if row["qualified"] else "❌"
            pt_label = "pt" if pts == 1 else "pts"
            lines.append(f"{medal} **{name}** — {pts} {pt_label}  {mark}")

        embed.description = "\n".join(lines)
        embed.add_field(
            name="Qualification",
            value="✅ Top 4 qualify · ❌ Bottom 3 eliminated",
            inline=False,
        )
        embed.set_footer(text="Updated automatically after every /result")
        return embed

    # ── /creategroup ──────────────────────────────────────────────────────

    @app_commands.command(
        name="creategroup",
        description="🏆 Admin: create a Plato group with 7 players.",
    )
    @app_commands.describe(
        group="Group letter",
        player1="Player 1",
        player2="Player 2",
        player3="Player 3",
        player4="Player 4",
        player5="Player 5",
        player6="Player 6",
        player7="Player 7",
    )
    @app_commands.choices(group=GROUP_CHOICES)
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def creategroup(
        self,
        interaction: discord.Interaction,
        group: app_commands.Choice[str],
        player1: discord.Member,
        player2: discord.Member,
        player3: discord.Member,
        player4: discord.Member,
        player5: discord.Member,
        player6: discord.Member,
        player7: discord.Member,
    ) -> None:
        await interaction.response.defer()
        code = group.value
        members = [player1, player2, player3, player4, player5, player6, player7]

        if any(m.bot for m in members):
            await interaction.followup.send("❌ Bots can't be tournament players.", ephemeral=True)
            return
        if len({m.id for m in members}) != len(members):
            await interaction.followup.send("❌ Duplicate players are not allowed.", ephemeral=True)
            return

        try:
            await self._run(
                db.set_group, interaction.guild_id, code, [m.id for m in members]
            )
        except ValueError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        except Exception as exc:
            log.exception("creategroup failed")
            await interaction.followup.send(f"❌ Could not save group: {exc}", ephemeral=True)
            return

        names = "\n".join(f"• {m.mention}" for m in members)
        embed = discord.Embed(
            title=f"✅ Group {code} saved",
            description=names,
            color=0x44BB88,
        )
        embed.set_footer(text="Any previous results between these players were kept")
        await interaction.followup.send(embed=embed)

    # ── /result ───────────────────────────────────────────────────────────

    @app_commands.command(
        name="result",
        description="🏆 Admin: record a Plato match result (2-0, 1-1, or 0-2).",
    )
    @app_commands.describe(
        player1="First player (score is theirs)",
        player2="Second player",
        score="Games won, e.g. 2-0, 1-1, or 0-2",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def result(
        self,
        interaction: discord.Interaction,
        player1: discord.Member,
        player2: discord.Member,
        score: str,
    ) -> None:
        await interaction.response.defer()
        try:
            s1, s2 = _parse_score(score)
        except ValueError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        if player1.id == player2.id:
            await interaction.followup.send("❌ Pick two different players.", ephemeral=True)
            return

        try:
            await self._run(
                db.add_result,
                interaction.guild_id,
                player1.id,
                player2.id,
                s1,
                s2,
            )
        except (LookupError, ValueError) as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        except Exception as exc:
            log.exception("result failed")
            await interaction.followup.send(f"❌ Could not save result: {exc}", ephemeral=True)
            return

        group = await self._run(
            db.find_shared_group, interaction.guild_id, player1.id, player2.id
        )
        embed = discord.Embed(
            title="📝 Result recorded",
            description=(
                f"{player1.mention} **{s1}–{s2}** {player2.mention}\n"
                f"**{player1.display_name}** +{s1} pt · "
                f"**{player2.display_name}** +{s2} pt"
            ),
            color=COLOR,
        )
        if group:
            embed.set_footer(text=f"Group {group}")
        await interaction.followup.send(embed=embed)

        if group and interaction.guild:
            rows = await self._run(db.get_ranking, interaction.guild_id, group)
            await interaction.followup.send(
                embed=self._ranking_embed(interaction.guild, group, rows)
            )

    # ── /editresult ───────────────────────────────────────────────────────

    @app_commands.command(
        name="editresult",
        description="🏆 Admin: edit an existing Plato match result.",
    )
    @app_commands.describe(
        player1="First player (score is theirs)",
        player2="Second player",
        score="New score, e.g. 2-0, 1-1, or 0-2",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def editresult(
        self,
        interaction: discord.Interaction,
        player1: discord.Member,
        player2: discord.Member,
        score: str,
    ) -> None:
        await interaction.response.defer()
        try:
            s1, s2 = _parse_score(score)
        except ValueError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return

        try:
            await self._run(
                db.edit_result,
                interaction.guild_id,
                player1.id,
                player2.id,
                s1,
                s2,
            )
        except (LookupError, ValueError) as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        except Exception as exc:
            log.exception("editresult failed")
            await interaction.followup.send(f"❌ Could not edit result: {exc}", ephemeral=True)
            return

        group = await self._run(
            db.find_shared_group, interaction.guild_id, player1.id, player2.id
        )
        await interaction.followup.send(
            embed=discord.Embed(
                title="✏️ Result updated",
                description=f"{player1.mention} **{s1}–{s2}** {player2.mention}",
                color=COLOR,
            )
        )
        if group and interaction.guild:
            rows = await self._run(db.get_ranking, interaction.guild_id, group)
            await interaction.followup.send(
                embed=self._ranking_embed(interaction.guild, group, rows)
            )

    # ── /removeresult ─────────────────────────────────────────────────────

    @app_commands.command(
        name="removeresult",
        description="🏆 Admin: remove a Plato match result.",
    )
    @app_commands.describe(player1="First player", player2="Second player")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def removeresult(
        self,
        interaction: discord.Interaction,
        player1: discord.Member,
        player2: discord.Member,
    ) -> None:
        await interaction.response.defer()
        try:
            ok = await self._run(
                db.remove_result, interaction.guild_id, player1.id, player2.id
            )
        except ValueError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        if not ok:
            await interaction.followup.send("❌ No result found for that pair.", ephemeral=True)
            return

        group = await self._run(
            db.find_shared_group, interaction.guild_id, player1.id, player2.id
        )
        await interaction.followup.send(
            embed=discord.Embed(
                title="🗑️ Result removed",
                description=f"{player1.mention} vs {player2.mention}",
                color=0xCC5566,
            )
        )
        if group and interaction.guild:
            rows = await self._run(db.get_ranking, interaction.guild_id, group)
            await interaction.followup.send(
                embed=self._ranking_embed(interaction.guild, group, rows)
            )

    # ── /plato-reset ──────────────────────────────────────────────────────

    @app_commands.command(
        name="plato-reset",
        description="🏆 Admin: wipe all Plato tournament data for this server.",
    )
    @app_commands.describe(confirm="Type CONFIRM to wipe everything")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def plato_reset(
        self, interaction: discord.Interaction, confirm: str
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if confirm.strip().upper() != "CONFIRM":
            await interaction.followup.send(
                "⚠️ Type `CONFIRM` in the confirm field to wipe all groups and results.",
                ephemeral=True,
            )
            return
        try:
            await self._run(db.reset_tournament, interaction.guild_id)
        except Exception as exc:
            log.exception("plato-reset failed")
            await interaction.followup.send(f"❌ Reset failed: {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            embed=discord.Embed(
                title="♻️ Tournament reset",
                description="All groups, players, and results have been cleared.",
                color=0x666677,
            )
        )

    # ── /matches ──────────────────────────────────────────────────────────

    @app_commands.command(
        name="matches",
        description="🏆 Show a player's finished results and remaining group matches.",
    )
    @app_commands.describe(member="Player to check (defaults to you)")
    @app_commands.guild_only()
    async def matches(
        self,
        interaction: discord.Interaction,
        member: Optional[discord.Member] = None,
    ) -> None:
        await interaction.response.defer()
        target = member or interaction.user
        assert isinstance(target, (discord.Member, discord.User))
        assert interaction.guild is not None

        try:
            sheet = await self._run(
                db.get_player_matches, interaction.guild_id, target.id
            )
        except LookupError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        except Exception as exc:
            log.exception("matches failed")
            await interaction.followup.send(f"❌ Could not load matches: {exc}", ephemeral=True)
            return

        group = sheet["group"]
        embed = discord.Embed(
            title=f"📋 {target.display_name}'s matches — Group {group}",
            color=COLOR,
        )
        embed.add_field(
            name="Progress",
            value=(
                f"**{sheet['played_count']}/{sheet['total']}** matches played\n"
                f"**{sheet['remaining_count']}** left · **{sheet['points']}** pts"
            ),
            inline=False,
        )

        if sheet["played"]:
            lines = []
            for m in sheet["played"]:
                opp = _display_name(interaction.guild, m["opponent_id"])
                lines.append(
                    f"✅ vs **{opp}** — `{m['my_score']}–{m['their_score']}` "
                    f"(+{m['my_score']} pt)"
                )
            # Discord field limit 1024
            chunk: list[str] = []
            size = 0
            field_i = 1
            for line in lines:
                add = len(line) + (1 if chunk else 0)
                if chunk and size + add > 1000:
                    embed.add_field(
                        name="Finished" if field_i == 1 else f"Finished (cont. {field_i})",
                        value="\n".join(chunk),
                        inline=False,
                    )
                    field_i += 1
                    chunk = [line]
                    size = len(line)
                else:
                    chunk.append(line)
                    size += add
            if chunk:
                embed.add_field(
                    name="Finished" if field_i == 1 else f"Finished (cont. {field_i})",
                    value="\n".join(chunk),
                    inline=False,
                )
        else:
            embed.add_field(name="Finished", value="*No results yet*", inline=False)

        if sheet["remaining"]:
            left = "\n".join(
                f"⏳ vs **{_display_name(interaction.guild, oid)}**"
                for oid in sheet["remaining"]
            )
            embed.add_field(name="Remaining", value=left[:1024], inline=False)
        else:
            embed.add_field(
                name="Remaining",
                value="🎉 All group matches completed!",
                inline=False,
            )

        embed.set_footer(text="Results are stored permanently — editing games list won't erase them")
        await interaction.followup.send(embed=embed)

    # ── /ranking ──────────────────────────────────────────────────────────

    @app_commands.command(
        name="ranking",
        description="🏆 Show Plato group rankings (top 4 qualify).",
    )
    @app_commands.describe(group="Which group to show")
    @app_commands.choices(group=GROUP_CHOICES)
    @app_commands.guild_only()
    async def ranking(
        self,
        interaction: discord.Interaction,
        group: app_commands.Choice[str],
    ) -> None:
        await interaction.response.defer()
        code = group.value
        try:
            rows = await self._run(db.get_ranking, interaction.guild_id, code)
        except Exception as exc:
            log.exception("ranking failed")
            await interaction.followup.send(f"❌ Could not load ranking: {exc}", ephemeral=True)
            return
        assert interaction.guild is not None
        await interaction.followup.send(
            embed=self._ranking_embed(interaction.guild, code, rows)
        )

    # ── /platohelp ────────────────────────────────────────────────────────

    @app_commands.command(
        name="platohelp",
        description="🏆 Plato tournament command list.",
    )
    @app_commands.guild_only()
    async def platohelp(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(title="🏆 Plato Tournament Commands", color=COLOR)
        embed.add_field(
            name="Admin",
            value=(
                "`/creategroup` — save a group (A–D, 7 players)\n"
                "`/result` — record match (`2-0` / `1-1` / `0-2`)\n"
                "`/editresult` — fix a result\n"
                "`/removeresult` — delete a result\n"
                "`/plato-reset` — wipe tournament data (`CONFIRM`)"
            ),
            inline=False,
        )
        embed.add_field(
            name="Everyone",
            value=(
                "`/ranking` — Group A / B / C / D standings\n"
                "`/matches` — your (or someone's) results + remaining games"
            ),
            inline=False,
        )
        embed.add_field(
            name="Points",
            value="Each game won = **1 pt**. A match is two games.",
            inline=False,
        )
        embed.add_field(name="Available games", value=" · ".join(GAMES), inline=False)
        embed.set_footer(text="Top 4 in each group qualify (16 total)")
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PlatoTournament(bot))
