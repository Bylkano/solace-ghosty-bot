"""
cogs/auction.py
───────────────────────────────────────────────────────────────────────────────
Money Heist Black Market — Auction System
───────────────────────────────────────────────────────────────────────────────

Admin-only slash commands:
  /auction-add    item_name description starting_bid  — queue an item
  /auction-start  item_name                           — launch in this channel
  /auction-cancel                                     — abort running auction

Public slash commands:
  /bid    amount   — place a bid in the active auction
  /auction-list    — view queued items

Integrates with the existing economy:
  · Uses get_points / deduct_points from cogs.server_drops_economy
  · Points are deducted automatically when an item is sold
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Optional
import sys
import pathlib

import discord
from discord import app_commands
from discord.ext import commands

# Pull the economy helpers from the sibling cog (module-level functions)
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
from cogs.server_drops_economy import get_points, deduct_points

# ─────────────────────────────── colour palette ──────────────────────────────

C_GOLD    = 0xFFD700   # auction open / item listing
C_BID     = 0xFF4500   # new bid — hot red/orange
C_SOLD    = 0x2ECC71   # sold / winner — emerald
C_CANCEL  = 0x747F8D   # cancelled / no bids — grey
C_ERROR   = 0x992D22   # validation failure — dark red
C_LIST    = 0x2C2F33   # queue embed — near-black

# ─────────────────────────────── branding GIFs ───────────────────────────────
# Money Heist / high-stakes themed — all Giphy CDN

GIF_AUCTION_OPEN = "https://media.giphy.com/media/26BRv0ThflsHCqDrG/giphy.gif"   # counting cash stacks
GIF_SOLD         = "https://media.giphy.com/media/l3V0xbo2qJK6Yxnra/giphy.gif"   # confetti explosion
GIF_OUTBID       = "https://media.giphy.com/media/xT9IgN8QALQjpnkPbq/giphy.gif"  # intense rivalry

# ─────────────────────────────── timing ──────────────────────────────────────

INITIAL_TIMEOUT = 30    # seconds after auction opens before "going once" if no bids
IDLE_TIMEOUT    = 15    # seconds of silence after a bid triggers the gavel sequence
GAVEL_DELAY     =  3    # seconds between "going once" → "going twice" → "SOLD"

# ─────────────────────────────── misc ────────────────────────────────────────

SEP = "▬" * 22


# ─────────────────────────────── data models ─────────────────────────────────

@dataclass
class AuctionItem:
    """An item sitting in the queue, not yet active."""
    name:        str
    description: str
    starting_bid: int


@dataclass
class ActiveAuction:
    """Runtime state of an in-progress auction."""
    item:           AuctionItem
    channel:        discord.TextChannel
    current_bid:    int                    # starts at item.starting_bid
    highest_bidder: Optional[discord.Member] = None

    # Signalled by /bid to wake the countdown task
    bid_event:  asyncio.Event        = field(default_factory=asyncio.Event)

    # The opening embed message — we edit it on every bid
    embed_msg:  Optional[discord.Message] = None

    # The background asyncio.Task running the countdown
    loop_task:  Optional[asyncio.Task]    = None

    # True once the auction has closed (prevents late /bid acceptance)
    closed:     bool = False


# ─────────────────────────────── cog ─────────────────────────────────────────

class AuctionCog(commands.Cog, name="Auction"):
    """Money Heist Black Market auction engine."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # guild_id  → ordered list of queued AuctionItems (the waiting room)
        self._queue:  dict[int, list[AuctionItem]] = {}
        # channel_id → ActiveAuction (one auction per channel at a time)
        self._active: dict[int, ActiveAuction]     = {}

    # ─────────────────────── embed builders ──────────────────────────────────

    @staticmethod
    def _open_embed(item: AuctionItem) -> discord.Embed:
        """Sent once when the auction opens — static starting card."""
        em = discord.Embed(
            title=f"🔴  BLACK MARKET AUCTION  ·  {item.name}",
            colour=C_GOLD,
            description=(
                f"```ansi\n\u001b[1;33m  🏛️  THE VAULT IS OPEN  🏛️  \u001b[0m\n```\n"
                f"{SEP}"
            ),
        )
        em.add_field(name="📦  Item",         value=item.description,            inline=False)
        em.add_field(name="💰  Starting Bid", value=f"**{item.starting_bid:,} pts**", inline=True)
        em.add_field(name="📣  How to Bid",   value="`/bid [amount]`",             inline=True)
        em.add_field(
            name="⏳  Status",
            value=f"Waiting for first bid… (**{INITIAL_TIMEOUT}s** window)",
            inline=False,
        )
        em.set_image(url=GIF_AUCTION_OPEN)
        em.set_footer(text="BLACK MARKET  •  Money Heist Edition")
        return em

    @staticmethod
    def _live_embed(auction: ActiveAuction) -> discord.Embed:
        """Replaces the opening embed whenever a new bid is placed."""
        bidder = auction.highest_bidder
        em = discord.Embed(
            title=f"🔴  LIVE AUCTION  ·  {auction.item.name}",
            colour=C_BID,
            description=(
                f"```ansi\n\u001b[1;31m  🔥  BIDDING WAR IN PROGRESS  🔥  \u001b[0m\n```\n"
                f"{SEP}"
            ),
        )
        em.add_field(name="📦  Item",           value=auction.item.description,                  inline=False)
        em.add_field(name="🏆  Current High Bid",value=f"**{auction.current_bid:,} pts**",       inline=True)
        em.add_field(name="👤  Leading Bidder",  value=bidder.mention if bidder else "—",         inline=True)
        em.add_field(
            name="⏳  Status",
            value=f"🔥 **LIVE** — `/bid [amount]` to beat it! Timer resets to **{IDLE_TIMEOUT}s** on each bid.",
            inline=False,
        )
        em.set_image(url=GIF_OUTBID)
        em.set_footer(text="BLACK MARKET  •  Money Heist Edition")
        return em

    @staticmethod
    def _sold_embed(auction: ActiveAuction) -> discord.Embed:
        """Full-screen winner announcement embed."""
        assert auction.highest_bidder is not None
        em = discord.Embed(
            title="🎉  SOLD!  THE HEIST IS COMPLETE!",
            colour=C_SOLD,
            description=(
                f"```ansi\n\u001b[1;32m  🔨  GAVEL DOWN — IT'S OVER  🔨  \u001b[0m\n```\n"
                f"{SEP}\n"
                f"**{auction.item.name}** has left the vault!\n"
                f"{SEP}"
            ),
        )
        em.add_field(name="🏆  Winner",      value=auction.highest_bidder.mention,           inline=True)
        em.add_field(name="💸  Final Price", value=f"**{auction.current_bid:,} pts**",        inline=True)
        em.add_field(name="📦  Item",        value=auction.item.description,                  inline=False)
        em.set_thumbnail(url=auction.highest_bidder.display_avatar.url)
        em.set_image(url=GIF_SOLD)
        em.set_footer(text="BLACK MARKET  •  Money Heist Edition")
        return em

    @staticmethod
    def _no_bids_embed(item_name: str) -> discord.Embed:
        """Shown when the auction timer expires with zero bids."""
        em = discord.Embed(
            title="🏦  No Bids Received",
            description=(
                f"**{item_name}** went completely unsold.\n"
                "The item has been returned to the vault. 🔐\n\n"
                "Better luck next time, heisters."
            ),
            colour=C_CANCEL,
        )
        em.set_footer(text="BLACK MARKET  •  Money Heist Edition")
        return em

    # ─────────────────────── background auction loop ─────────────────────────

    async def _run_auction(self, channel_id: int) -> None:
        """
        Background task that drives the full auction lifecycle:

        Phase 1 — Initial window (INITIAL_TIMEOUT seconds)
            Wait for the first bid. If none arrives, go straight to no-bids close.

        Phase 2 — Idle loop (IDLE_TIMEOUT seconds per cycle)
            After each bid the timer resets. If IDLE_TIMEOUT passes silently,
            kick off the gavel sequence.

        Gavel sequence
            "Going once"  → wait GAVEL_DELAY → if new bid, back to Phase 2
            "Going twice" → wait GAVEL_DELAY → if new bid, back to Phase 2
            "SOLD!"       → _conclude_auction()
        """
        auction = self._active.get(channel_id)
        if not auction:
            return

        # ── Phase 1: initial open window ─────────────────────────────────────
        auction.bid_event.clear()
        try:
            await asyncio.wait_for(auction.bid_event.wait(), timeout=float(INITIAL_TIMEOUT))
        except asyncio.TimeoutError:
            pass  # no bids yet — fall straight through to idle loop (which will also expire)

        # ── Phase 2: 15-second idle loop ─────────────────────────────────────
        while channel_id in self._active:
            auction = self._active[channel_id]   # re-fetch in case of edits
            auction.bid_event.clear()
            try:
                await asyncio.wait_for(auction.bid_event.wait(), timeout=float(IDLE_TIMEOUT))
                # A bid arrived — restart the 15s window
                continue
            except asyncio.TimeoutError:
                pass  # silence for IDLE_TIMEOUT → call the auction

            # ── No bid for IDLE_TIMEOUT seconds ──────────────────────────────

            # Edge case: nobody bid at all
            if auction.highest_bidder is None:
                auction.closed = True
                await auction.channel.send(embed=self._no_bids_embed(auction.item.name))
                self._active.pop(channel_id, None)
                return

            # ── Gavel sequence ────────────────────────────────────────────────
            # Snapshot current leader so the messages stay consistent even if
            # a bid sneaks in between the two "going" calls.
            snap_bid    = auction.current_bid
            snap_bidder = auction.highest_bidder

            # — Going once —
            await auction.channel.send(
                f"🎙️  **Going once** at **{snap_bid:,} pts** "
                f"to **{snap_bidder.display_name}**! ⏳"
            )
            auction.bid_event.clear()
            try:
                await asyncio.wait_for(auction.bid_event.wait(), timeout=float(GAVEL_DELAY))
                # New bid during "going once" — back to 15s idle loop
                await auction.channel.send(
                    "⚡  **New bid just in! The gavel is back up.** "
                    f"Timer reset to **{IDLE_TIMEOUT}s**!"
                )
                continue
            except asyncio.TimeoutError:
                pass

            # — Going twice —
            await auction.channel.send(
                f"🎙️  **Going twice** at **{snap_bid:,} pts**... 👀"
            )
            auction.bid_event.clear()
            try:
                await asyncio.wait_for(auction.bid_event.wait(), timeout=float(GAVEL_DELAY))
                await auction.channel.send(
                    "⚡  **Saved at the last second!** "
                    f"Timer reset to **{IDLE_TIMEOUT}s**!"
                )
                continue
            except asyncio.TimeoutError:
                pass

            # — SOLD — (no bid during either gavel call)
            await self._conclude_auction(channel_id)
            return

    async def _conclude_auction(self, channel_id: int) -> None:
        """
        Called when the gavel falls:
          1. Remove from active map
          2. Deduct points from winner
          3. Edit the live embed to show "SOLD"
          4. Send the winner celebration embed + balance update
        """
        auction = self._active.pop(channel_id, None)
        if not auction:
            return
        if not auction.highest_bidder:
            # Shouldn't happen, but guard anyway
            await auction.channel.send(embed=self._no_bids_embed(auction.item.name))
            return

        auction.closed = True

        # Deduct from winner's economy balance
        new_total = deduct_points(auction.highest_bidder.id, auction.current_bid)

        # Edit the live embed to "SOLD" state
        if auction.embed_msg:
            try:
                await auction.embed_msg.edit(embed=self._sold_embed(auction))
            except discord.HTTPException:
                pass  # message may have been deleted — not fatal

        # Big winner announcement
        await auction.channel.send(
            content=(
                f"🔨  {auction.highest_bidder.mention} "
                f"— **{auction.item.name}** is yours!"
            ),
            embed=self._sold_embed(auction),
        )

        # Balance receipt
        await auction.channel.send(
            f"💸  **{auction.current_bid:,} pts** deducted from your balance. "
            f"Remaining: **{new_total:,} pts**."
        )

    # ─────────────────────── /auction-add ────────────────────────────────────

    @app_commands.command(
        name="auction-add",
        description="[Admin] Add an item to the Black Market auction queue.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        item_name    = "Name of the item being auctioned.",
        description  = "What the item is / what it does / its lore.",
        starting_bid = "Minimum opening bid in points.",
    )
    async def auction_add(
        self,
        interaction:  discord.Interaction,
        item_name:    str,
        description:  str,
        starting_bid: int,
    ) -> None:
        assert interaction.guild_id is not None

        if starting_bid < 1:
            await interaction.response.send_message(
                "❌  Starting bid must be at least **1 pt**.", ephemeral=True
            )
            return

        item = AuctionItem(
            name=item_name.strip(),
            description=description.strip(),
            starting_bid=starting_bid,
        )
        self._queue.setdefault(interaction.guild_id, []).append(item)
        position = len(self._queue[interaction.guild_id])

        em = discord.Embed(title="✅  Item Added to the Vault", colour=C_GOLD)
        em.add_field(name="📦  Name",         value=item.name,               inline=True)
        em.add_field(name="💰  Starting Bid", value=f"{starting_bid:,} pts", inline=True)
        em.add_field(name="📝  Description",  value=item.description,        inline=False)
        em.set_footer(text=f"Queue position #{position}  •  BLACK MARKET")
        await interaction.response.send_message(embed=em, ephemeral=True)

    # ─────────────────────── /auction-start ──────────────────────────────────

    @app_commands.command(
        name="auction-start",
        description="[Admin] Start the auction for a queued item in this channel.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        item_name="Exact name of the item to start (as added with /auction-add)."
    )
    async def auction_start(
        self,
        interaction: discord.Interaction,
        item_name:   str,
    ) -> None:
        assert interaction.guild_id is not None
        channel_id = interaction.channel_id
        assert channel_id is not None

        # Block if an auction is already live in this channel
        if channel_id in self._active:
            await interaction.response.send_message(
                "❌  An auction is **already running** in this channel. "
                "Wait for it to finish or use `/auction-cancel`.",
                ephemeral=True,
            )
            return

        # Find the item in the guild's queue (case-insensitive)
        queue = self._queue.get(interaction.guild_id, [])
        item  = next(
            (i for i in queue if i.name.lower() == item_name.strip().lower()), None
        )
        if item is None:
            await interaction.response.send_message(
                f"❌  **{item_name}** was not found in the queue.\n"
                "Add it first with `/auction-add`, or check the spelling with `/auction-list`.",
                ephemeral=True,
            )
            return

        # Remove from queue now (so it can't be started twice)
        queue.remove(item)

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌  Auctions can only run in text channels.", ephemeral=True
            )
            return

        # Build the auction state
        auction = ActiveAuction(
            item=item,
            channel=channel,
            current_bid=item.starting_bid,
        )
        self._active[channel_id] = auction

        # Defer so we can retrieve the sent Message object for later editing
        await interaction.response.defer()
        msg = await interaction.followup.send(embed=self._open_embed(item), wait=True)
        auction.embed_msg = msg

        # Fire off the background countdown task
        task = asyncio.create_task(
            self._run_auction(channel_id),
            name=f"auction-{channel_id}",
        )
        auction.loop_task = task

    # ─────────────────────── /bid ─────────────────────────────────────────────

    @app_commands.command(
        name="bid",
        description="Place a bid in the active auction in this channel.",
    )
    @app_commands.guild_only()
    @app_commands.describe(amount="Number of points you want to bid.")
    async def bid(
        self,
        interaction: discord.Interaction,
        amount:       int,
    ) -> None:
        channel_id = interaction.channel_id
        assert channel_id is not None

        # ── Guard: auction exists and is still open ──────────────────────────
        auction = self._active.get(channel_id)
        if not auction or auction.closed:
            await interaction.response.send_message(
                "❌  There is no active auction in this channel right now.",
                ephemeral=True,
            )
            return

        # ── Guard: amount meets starting bid ────────────────────────────────
        if amount < auction.item.starting_bid:
            await interaction.response.send_message(
                f"❌  Minimum bid is **{auction.item.starting_bid:,} pts**.",
                ephemeral=True,
            )
            return

        # ── Guard: amount beats the current high bid (if one exists) ─────────
        if auction.highest_bidder is not None and amount <= auction.current_bid:
            await interaction.response.send_message(
                f"❌  Your bid of **{amount:,} pts** must be higher than "
                f"the current bid of **{auction.current_bid:,} pts**.\n"
                f"Minimum next bid: **{auction.current_bid + 1:,} pts**.",
                ephemeral=True,
            )
            return

        # ── Guard: bidder has sufficient funds ───────────────────────────────
        balance = get_points(interaction.user.id)
        if balance < amount:
            await interaction.response.send_message(
                f"❌  **Insufficient funds!**\n"
                f"You need **{amount:,} pts** but only have **{balance:,} pts**. 💸\n"
                f"Maximum you can bid: **{balance:,} pts**.",
                ephemeral=True,
            )
            return

        # ── Accept the bid ───────────────────────────────────────────────────
        assert isinstance(interaction.user, discord.Member)
        prev_bidder    = auction.highest_bidder
        auction.current_bid    = amount
        auction.highest_bidder = interaction.user

        # Wake the countdown task — resets its 15s window
        auction.bid_event.set()

        # Edit the main live embed to show the new leader
        if auction.embed_msg:
            try:
                await auction.embed_msg.edit(embed=self._live_embed(auction))
            except discord.HTTPException:
                pass

        # Build the public confirmation embed
        em = discord.Embed(
            title="✅  Bid Accepted!",
            colour=C_BID,
            description=(
                f"**{interaction.user.display_name}** is now the highest bidder!\n\n"
                f"💰  **Current Bid:** {amount:,} pts\n"
                f"⏳  **Timer reset to {IDLE_TIMEOUT}s** — counter-bid to survive!"
            ),
        )
        if prev_bidder and prev_bidder != interaction.user:
            em.add_field(
                name="😤  Outbid",
                value=f"{prev_bidder.mention} has been knocked off the throne!",
                inline=False,
            )
        em.set_footer(text="BLACK MARKET  •  Money Heist Edition")
        await interaction.response.send_message(embed=em)

    # ─────────────────────── /auction-cancel ─────────────────────────────────

    @app_commands.command(
        name="auction-cancel",
        description="[Admin] Cancel the running auction in this channel.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def auction_cancel(self, interaction: discord.Interaction) -> None:
        channel_id = interaction.channel_id
        assert channel_id is not None

        auction = self._active.pop(channel_id, None)
        if not auction:
            await interaction.response.send_message(
                "❌  No auction is running in this channel.", ephemeral=True
            )
            return

        # Mark closed first so any in-flight /bid is rejected
        auction.closed = True

        # Cancel the background task gracefully
        if auction.loop_task and not auction.loop_task.done():
            auction.loop_task.cancel()

        em = discord.Embed(
            title="🛑  Auction Cancelled",
            description=(
                f"**{auction.item.name}** has been pulled from the Black Market.\n"
                "The item has been returned to the vault. 🔐\n\n"
                f"Current highest bid was **{auction.current_bid:,} pts**"
                + (f" by {auction.highest_bidder.mention}" if auction.highest_bidder else "")
                + " — **no points have been deducted**."
            ),
            colour=C_CANCEL,
        )
        em.set_footer(text=f"Cancelled by {interaction.user.display_name}  •  BLACK MARKET")
        await interaction.response.send_message(embed=em)

    # ─────────────────────── /auction-list ───────────────────────────────────

    @app_commands.command(
        name="auction-list",
        description="View all items waiting in the Black Market auction queue.",
    )
    @app_commands.guild_only()
    async def auction_list(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        queue = self._queue.get(interaction.guild_id, [])

        if not queue:
            await interaction.response.send_message(
                "📭  The auction queue is empty. "
                "Admins can add items with `/auction-add`.",
                ephemeral=True,
            )
            return

        em = discord.Embed(
            title="🏷️  Black Market — Auction Queue",
            colour=C_LIST,
            description=f"{SEP}\n{len(queue)} item(s) waiting in the vault\n{SEP}",
        )
        for idx, item in enumerate(queue, start=1):
            em.add_field(
                name=f"#{idx}  {item.name}",
                value=f"{item.description}\nStarting bid: **{item.starting_bid:,} pts**",
                inline=False,
            )
        em.set_footer(text="BLACK MARKET  •  Money Heist Edition")
        await interaction.response.send_message(embed=em, ephemeral=True)


# ─────────────────────────────── setup ───────────────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AuctionCog(bot))
