"""
oos_loans.py – Shared notebook for oos debts (not an economy).

Only stores reminders of who owes whom. The real oos currency lives in
another bot — this never moves balances.

Commands
--------
  /lend @user amount [note]   – Note that you lent them oos
  /owe  @user amount [note]   – Note that you owe them oos
  /pay  id [amount]           – Mark paid / reduce the noted amount
  /loan-delete id             – Remove a note
  /debts [@user]              – List open debt notes
  /debt id                    – Show one note
  /settle @user               – Notes between you and someone
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
import sys
from typing import Optional

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import discord
from discord import app_commands
from discord.ext import commands

import cogs.oos_loans_db as ldb

log = logging.getLogger("bot.oos_loans")

COLOR = 0xE8B84A
MAX_AMOUNT = 1_000_000_000_000


def _fmt(amount: int) -> str:
    return f"{amount:,} oos"


def _mention(guild: discord.Guild | None, user_id: int) -> str:
    if guild is not None:
        member = guild.get_member(user_id)
        if member is not None:
            return member.mention
    return f"<@{user_id}>"


def _can_manage(loan: dict, user: discord.abc.User, member: discord.Member | None) -> bool:
    if user.id in (loan["lender_id"], loan["borrower_id"], loan["created_by"]):
        return True
    if member is not None and member.guild_permissions.manage_guild:
        return True
    return False


class OosLoans(commands.Cog):
    """📒 Debt notes for oos (ledger only — no balances)."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        ldb.init_tables()

    async def _run(self, func, *args):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, func, *args)

    def _loan_line(self, guild: discord.Guild | None, loan: dict, viewer_id: int) -> str:
        note = f" — {loan['note']}" if loan.get("note") else ""
        if loan["borrower_id"] == viewer_id:
            other = _mention(guild, loan["lender_id"])
            return f"`#{loan['id']}` You owe {other} **{_fmt(loan['amount'])}**{note}"
        if loan["lender_id"] == viewer_id:
            other = _mention(guild, loan["borrower_id"])
            return f"`#{loan['id']}` {_mention(guild, loan['borrower_id'])} owes you **{_fmt(loan['amount'])}**{note}"
        # Third-party view
        return (
            f"`#{loan['id']}` {_mention(guild, loan['borrower_id'])} owes "
            f"{_mention(guild, loan['lender_id'])} **{_fmt(loan['amount'])}**{note}"
        )

    async def _add(
        self,
        interaction: discord.Interaction,
        *,
        lender: discord.Member,
        borrower: discord.Member,
        amount: int,
        note: Optional[str],
        title: str,
    ) -> None:
        if amount <= 0:
            await interaction.followup.send("Amount must be greater than 0.", ephemeral=True)
            return
        if amount > MAX_AMOUNT:
            await interaction.followup.send("That amount is too large.", ephemeral=True)
            return
        if lender.id == borrower.id:
            await interaction.followup.send("You can't loan oos to yourself.", ephemeral=True)
            return
        if lender.bot or borrower.bot:
            await interaction.followup.send("Bots can't be part of oos loans.", ephemeral=True)
            return

        try:
            row = await self._run(
                ldb.add_loan,
                interaction.guild_id,
                lender.id,
                borrower.id,
                amount,
                interaction.user.id,
                note,
            )
        except Exception as exc:
            log.exception("Failed to add loan")
            await interaction.followup.send(f"Could not save loan: {exc}", ephemeral=True)
            return

        embed = discord.Embed(
            title=title,
            description=(
                f"{_mention(interaction.guild, borrower.id)} owes "
                f"{_mention(interaction.guild, lender.id)} **{_fmt(amount)}**"
            ),
            color=COLOR,
        )
        embed.add_field(name="Note ID", value=f"`#{row['id']}`", inline=True)
        if note:
            embed.add_field(name="Reminder", value=note[:200], inline=True)
        embed.set_footer(text="Notebook only — mark paid with /pay · list with /debts")
        await interaction.followup.send(embed=embed)

    # ── /lend ──────────────────────────────────────────────────────────────

    @app_commands.command(
        name="lend",
        description="💸 Note that you lent oos to someone (they owe you).",
    )
    @app_commands.describe(
        member="Who you lent oos to",
        amount="How much oos (note only — does not move currency)",
        note="Optional reminder (why / when)",
    )
    @app_commands.guild_only()
    async def lend(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1, MAX_AMOUNT],
        note: Optional[str] = None,
    ) -> None:
        await interaction.response.defer()
        await self._add(
            interaction,
            lender=interaction.user,  # type: ignore[arg-type]
            borrower=member,
            amount=int(amount),
            note=note,
            title="💸 Debt note saved",
        )

    # ── /owe ───────────────────────────────────────────────────────────────

    @app_commands.command(
        name="owe",
        description="🧾 Note that you owe someone oos.",
    )
    @app_commands.describe(
        member="Who you owe",
        amount="How much oos (note only — does not move currency)",
        note="Optional reminder (why / when)",
    )
    @app_commands.guild_only()
    async def owe(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1, MAX_AMOUNT],
        note: Optional[str] = None,
    ) -> None:
        await interaction.response.defer()
        await self._add(
            interaction,
            lender=member,
            borrower=interaction.user,  # type: ignore[arg-type]
            amount=int(amount),
            note=note,
            title="🧾 Debt note saved",
        )

    # ── /pay ───────────────────────────────────────────────────────────────

    @app_commands.command(
        name="pay",
        description="✅ Mark an oos debt note as paid (or reduce the amount).",
    )
    @app_commands.describe(
        loan_id="Note ID from /debts (number only, no #)",
        amount="Partial amount paid; leave empty to mark fully paid",
    )
    @app_commands.guild_only()
    async def pay(
        self,
        interaction: discord.Interaction,
        loan_id: int,
        amount: Optional[app_commands.Range[int, 1, MAX_AMOUNT]] = None,
    ) -> None:
        await interaction.response.defer()
        loan = await self._run(ldb.get_loan, interaction.guild_id, loan_id)
        if not loan:
            await interaction.followup.send(f"No loan `#{loan_id}` in this server.", ephemeral=True)
            return
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not _can_manage(loan, interaction.user, member):
            await interaction.followup.send(
                "Only the lender, borrower, or a server manager can update this loan.",
                ephemeral=True,
            )
            return

        try:
            updated = await self._run(
                ldb.pay_loan,
                interaction.guild_id,
                loan_id,
                int(amount) if amount is not None else None,
            )
        except LookupError:
            await interaction.followup.send(f"No loan `#{loan_id}` found.", ephemeral=True)
            return
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        except Exception as exc:
            log.exception("pay failed")
            await interaction.followup.send(f"Could not update loan: {exc}", ephemeral=True)
            return

        paid = int(updated["paid_amount"])
        if updated["paid_fully"]:
            embed = discord.Embed(
                title="✅ Note marked paid",
                description=(
                    f"Debt note `#{loan_id}` is cleared.\n"
                    f"Marked **{_fmt(paid)}** paid — "
                    f"{_mention(interaction.guild, loan['borrower_id'])} ↔ "
                    f"{_mention(interaction.guild, loan['lender_id'])}"
                ),
                color=0x44BB88,
            )
        else:
            embed = discord.Embed(
                title="💵 Note updated",
                description=(
                    f"Marked **{_fmt(paid)}** paid on note `#{loan_id}`.\n"
                    f"Still noted: **{_fmt(int(updated['amount']))}**"
                ),
                color=COLOR,
            )
        await interaction.followup.send(embed=embed)

    # ── /loan-delete ───────────────────────────────────────────────────────

    @app_commands.command(
        name="loan-delete",
        description="🗑️ Delete an oos debt note.",
    )
    @app_commands.describe(loan_id="Note ID from /debts")
    @app_commands.guild_only()
    async def loan_delete(self, interaction: discord.Interaction, loan_id: int) -> None:
        await interaction.response.defer(ephemeral=True)
        loan = await self._run(ldb.get_loan, interaction.guild_id, loan_id)
        if not loan:
            await interaction.followup.send(f"No loan `#{loan_id}` in this server.", ephemeral=True)
            return
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if not _can_manage(loan, interaction.user, member):
            await interaction.followup.send(
                "Only the lender, borrower, or a server manager can delete this loan.",
                ephemeral=True,
            )
            return

        ok = await self._run(ldb.delete_loan, interaction.guild_id, loan_id)
        if ok:
            await interaction.followup.send(f"Deleted loan `#{loan_id}`.", ephemeral=True)
        else:
            await interaction.followup.send(f"Could not delete loan `#{loan_id}`.", ephemeral=True)

    # ── /debts ─────────────────────────────────────────────────────────────

    @app_commands.command(
        name="debts",
        description="📒 Show open oos debt notes for you or another member.",
    )
    @app_commands.describe(member="Member to check (defaults to you)")
    @app_commands.guild_only()
    async def debts(
        self,
        interaction: discord.Interaction,
        member: Optional[discord.Member] = None,
    ) -> None:
        await interaction.response.defer()
        target = member or interaction.user  # type: ignore[assignment]
        assert isinstance(target, discord.Member)

        rows = await self._run(ldb.list_active_for_user, interaction.guild_id, target.id)
        summary = await self._run(ldb.summarize_user, interaction.guild_id, target.id)

        embed = discord.Embed(
            title=f"📒 {target.display_name}'s oos notes",
            color=COLOR,
        )
        embed.add_field(name="They owe", value=_fmt(summary["owed_by_me"]), inline=True)
        embed.add_field(name="Owed to them", value=_fmt(summary["owed_to_me"]), inline=True)
        net = summary["net"]
        if net > 0:
            net_txt = f"+{_fmt(net)} (net creditor)"
        elif net < 0:
            net_txt = f"-{_fmt(abs(net))} (net debtor)"
        else:
            net_txt = "0 oos (even)"
        embed.add_field(name="Net", value=net_txt, inline=True)

        if not rows:
            embed.description = "No open debt notes."
        else:
            # Discord field value limit 1024; split across fields
            lines = [self._loan_line(interaction.guild, r, target.id) for r in rows]
            chunk: list[str] = []
            size = 0
            field_i = 1
            for line in lines:
                add = len(line) + (1 if chunk else 0)
                if chunk and size + add > 1000:
                    embed.add_field(
                        name="Open notes" if field_i == 1 else f"Open notes (cont. {field_i})",
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
                    name="Open notes" if field_i == 1 else f"Open notes (cont. {field_i})",
                    value="\n".join(chunk),
                    inline=False,
                )

        embed.set_footer(text="Notes only — oos stays in your other bot · /lend · /owe · /pay")
        await interaction.followup.send(embed=embed)

    # ── /debt ──────────────────────────────────────────────────────────────

    @app_commands.command(
        name="debt",
        description="🔍 Show one oos debt note by ID.",
    )
    @app_commands.describe(loan_id="Note ID from /debts")
    @app_commands.guild_only()
    async def debt(self, interaction: discord.Interaction, loan_id: int) -> None:
        await interaction.response.defer()
        loan = await self._run(ldb.get_loan, interaction.guild_id, loan_id)
        if not loan:
            await interaction.followup.send(f"No loan `#{loan_id}` in this server.", ephemeral=True)
            return

        status = "Paid ✅" if loan["paid_at"] else "Open 🟡"
        embed = discord.Embed(title=f"Loan #{loan['id']}", color=COLOR)
        embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(
            name="Amount",
            value=_fmt(int(loan["amount"])) if not loan["paid_at"] else "0 oos (settled)",
            inline=True,
        )
        embed.add_field(
            name="Borrower",
            value=_mention(interaction.guild, loan["borrower_id"]),
            inline=True,
        )
        embed.add_field(
            name="Lender",
            value=_mention(interaction.guild, loan["lender_id"]),
            inline=True,
        )
        embed.add_field(
            name="Recorded by",
            value=_mention(interaction.guild, loan["created_by"]),
            inline=True,
        )
        if loan.get("note"):
            embed.add_field(name="Note", value=loan["note"][:500], inline=False)
        created = loan["created_at"]
        embed.set_footer(text=f"Created {created} · note only, no currency moved")
        await interaction.followup.send(embed=embed)

    # ── /settle ────────────────────────────────────────────────────────────

    @app_commands.command(
        name="settle",
        description="🤝 Show open oos debt notes between you and another member.",
    )
    @app_commands.describe(member="The other person")
    @app_commands.guild_only()
    async def settle(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        await interaction.response.defer()
        if member.id == interaction.user.id:
            await interaction.followup.send("Pick someone else.", ephemeral=True)
            return

        rows = await self._run(
            ldb.list_active_involving,
            interaction.guild_id,
            interaction.user.id,
            member.id,
        )
        embed = discord.Embed(
            title=f"🤝 Settling with {member.display_name}",
            color=COLOR,
        )
        if not rows:
            embed.description = "No open loans between you two."
        else:
            you = interaction.user.id
            lines = [self._loan_line(interaction.guild, r, you) for r in rows]
            embed.description = "\n".join(lines)[:4000]
            you_owe = sum(int(r["amount"]) for r in rows if r["borrower_id"] == you)
            they_owe = sum(int(r["amount"]) for r in rows if r["lender_id"] == you)
            embed.add_field(name="You owe them", value=_fmt(you_owe), inline=True)
            embed.add_field(name="They owe you", value=_fmt(they_owe), inline=True)
            net = they_owe - you_owe
            if net > 0:
                embed.add_field(name="Net", value=f"They owe you {_fmt(net)}", inline=True)
            elif net < 0:
                embed.add_field(name="Net", value=f"You owe them {_fmt(abs(net))}", inline=True)
            else:
                embed.add_field(name="Net", value="Even", inline=True)
        embed.set_footer(text="Pay with /pay <id> · delete with /loan-delete <id>")
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(OosLoans(bot))
