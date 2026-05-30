"""
cogs/auction.py
───────────────────────────────────────────────────────────────────────────────
Money Heist Black Market — Auction System  (standalone, separate currency)
───────────────────────────────────────────────────────────────────────────────

Database
  Table  : auction_bank  (user_id BIGINT PK, coins INTEGER ≥ 0)
  Separate from the main economy_users table — Heist Coins (🪙) only here.

Admin slash commands  (administrator permission required)
  /auction-add      item_name description starting_bid — queue an item
  /auction-start    item_name                          — launch in this channel
  /auction-cancel                                      — abort running auction
  /auction-give     member amount                      — deposit coins into member's bank
  /auction-set      member amount                      — hard-set a member's balance
  /auction-timer    idle gavel [initial]               — change countdown timers

Public slash commands
  /bid              amount    — place a bid in the active auction
  /auction-balance  [member]  — check auction bank balance
  /auction-list               — view the item queue
  /auction-leaderboard        — top balances for the event
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor

import discord
from discord import app_commands
from discord.ext import commands

# ─────────────────────────────── colour palette ──────────────────────────────

C_GOLD    = 0xFFD700   # auction open / item listing
C_BID     = 0xFF4500   # new bid — hot red/orange
C_SOLD    = 0x2ECC71   # sold / winner — emerald
C_CANCEL  = 0x747F8D   # cancelled / no bids — grey
C_ERROR   = 0x992D22   # validation failure — dark red
C_LIST    = 0x2C2F33   # queue / leaderboard embed — near-black
C_BANK    = 0x5865F2   # bank / balance — blurple

# ─────────────────────────────── branding GIFs ───────────────────────────────

GIF_AUCTION_OPEN = "https://media.giphy.com/media/26BRv0ThflsHCqDrG/giphy.gif"   # cash-counting stacks
GIF_SOLD         = "https://media.giphy.com/media/g9582DNuQppxC/giphy.gif"          # Leonardo DiCaprio raising a glass — Great Gatsby toast
GIF_OUTBID       = "https://media.giphy.com/media/xT9IgN8QALQjpnkPbq/giphy.gif"  # intense rivalry

# ─────────────────────────────── defaults ────────────────────────────────────
# All three timers are instance variables — /auction-timer overrides them live.

DEFAULT_INITIAL_TIMEOUT = 30   # seconds: opening window before first bid
DEFAULT_IDLE_TIMEOUT    = 15   # seconds: silence after a bid → gavel sequence
DEFAULT_GAVEL_DELAY     =  5   # seconds: between Going-once → twice → SOLD

# ─────────────────────────────── misc ────────────────────────────────────────

SEP  = "▬" * 22
COIN = "🪙"   # Heist Coin symbol used throughout


# ═══════════════════════════════ DATABASE LAYER ═══════════════════════════════
# Standalone — uses its own auction_bank table, no cross-cog imports.

_DB_URL = os.environ.get("DATABASE_URL", "")


def _db_connect():
    if not _DB_URL:
        raise RuntimeError("DATABASE_URL not set in environment")
    return psycopg2.connect(_DB_URL, sslmode="require")


def _db_init() -> None:
    """Create auction_bank table if it doesn't exist yet."""
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS auction_bank (
                    user_id  BIGINT  PRIMARY KEY,
                    coins    INTEGER NOT NULL DEFAULT 0
                )
                """
            )
        con.commit()


def _db_get(user_id: int) -> int:
    """Return the Heist Coin balance for user_id (0 if never banked)."""
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT coins FROM auction_bank WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
            return row[0] if row else 0


def _db_add(user_id: int, amount: int) -> int:
    """Add `amount` coins to user_id. Returns new balance."""
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO auction_bank (user_id, coins) VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE
                    SET coins = auction_bank.coins + EXCLUDED.coins
                RETURNING coins
                """,
                (user_id, amount),
            )
            row = cur.fetchone()
        con.commit()
        return row[0] if row else amount


def _db_deduct(user_id: int, amount: int) -> int:
    """Deduct `amount` coins (floor at 0). Returns new balance."""
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO auction_bank (user_id, coins) VALUES (%s, 0)
                ON CONFLICT (user_id) DO UPDATE
                    SET coins = GREATEST(0, auction_bank.coins - %s)
                RETURNING coins
                """,
                (user_id, amount),
            )
            row = cur.fetchone()
        con.commit()
        return row[0] if row else 0


def _db_set(user_id: int, amount: int) -> None:
    """Hard-set a user's balance to `amount`."""
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO auction_bank (user_id, coins) VALUES (%s, %s)
                ON CONFLICT (user_id) DO UPDATE SET coins = EXCLUDED.coins
                """,
                (user_id, amount),
            )
        con.commit()


def _db_leaderboard(limit: int = 10) -> list[tuple[int, int]]:
    """Return [(user_id, coins), ...] sorted by coins descending."""
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT user_id, coins FROM auction_bank ORDER BY coins DESC LIMIT %s",
                (limit,),
            )
            return cur.fetchall()


# ═══════════════════════════════ DATA MODELS ══════════════════════════════════


@dataclass
class AuctionItem:
    """An item waiting in the guild's auction queue."""
    name:         str
    description:  str
    starting_bid: int


@dataclass
class ActiveAuction:
    """Runtime state for one in-progress auction."""
    item:           AuctionItem
    channel:        discord.TextChannel
    current_bid:    int                       # initialised to item.starting_bid
    highest_bidder: Optional[discord.Member] = None

    # Set by /bid to wake the background countdown task
    bid_event:  asyncio.Event         = field(default_factory=asyncio.Event)

    # The opening-card message — edited on every new bid
    embed_msg:  Optional[discord.Message] = None

    # The background asyncio.Task driving the countdown
    loop_task:  Optional[asyncio.Task]    = None

    # Flipped to True once closed so any late /bid is instantly rejected
    closed:     bool = False


# ═══════════════════════════════ COG ══════════════════════════════════════════


class AuctionCog(commands.Cog, name="Auction"):
    """Money Heist Black Market — auction engine with separate Heist Coin bank."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

        # Ensure DB table exists
        _db_init()

        # ── Tunable timers (changed via /auction-timer) ───────────────────────
        self._initial_timeout: int = DEFAULT_INITIAL_TIMEOUT
        self._idle_timeout:    int = DEFAULT_IDLE_TIMEOUT
        self._gavel_delay:     int = DEFAULT_GAVEL_DELAY

        # ── Runtime state ─────────────────────────────────────────────────────
        # guild_id  → queued items
        self._queue:  dict[int, list[AuctionItem]] = {}
        # channel_id → live auction
        self._active: dict[int, ActiveAuction]     = {}

    # ─────────────────────── embed builders ──────────────────────────────────
    # (regular methods so they can read self._*_timeout for accurate footers)

    def _open_embed(self, item: AuctionItem) -> discord.Embed:
        """Static opening card sent when the auction launches."""
        em = discord.Embed(
            title=f"🔴  BLACK MARKET AUCTION  ·  {item.name}",
            colour=C_GOLD,
            description=(
                f"```ansi\n\u001b[1;33m  🏛️  THE VAULT IS OPEN  🏛️  \u001b[0m\n```\n"
                f"{SEP}"
            ),
        )
        em.add_field(name="📦  Item",          value=item.description,                         inline=False)
        em.add_field(name="💰  Starting Bid",  value=f"**{COIN} {item.starting_bid:,}**",      inline=True)
        em.add_field(name="📣  How to Bid",    value="`/bid [amount]`",                         inline=True)
        em.add_field(
            name="⏳  Timer",
            value=(
                f"First bid window: **{self._initial_timeout}s**\n"
                f"Idle reset after bid: **{self._idle_timeout}s**\n"
                f"Gavel delay: **{self._gavel_delay}s**"
            ),
            inline=False,
        )
        em.set_image(url=GIF_AUCTION_OPEN)
        em.set_footer(text=f"BLACK MARKET  •  Money Heist Edition  •  Currency: Heist Coins {COIN}")
        return em

    def _live_embed(self, auction: ActiveAuction) -> discord.Embed:
        """Replaces the opening card on every new bid."""
        bidder = auction.highest_bidder
        em = discord.Embed(
            title=f"🔴  LIVE AUCTION  ·  {auction.item.name}",
            colour=C_BID,
            description=(
                f"```ansi\n\u001b[1;31m  🔥  BIDDING WAR IN PROGRESS  🔥  \u001b[0m\n```\n"
                f"{SEP}"
            ),
        )
        em.add_field(name="📦  Item",             value=auction.item.description,                     inline=False)
        em.add_field(name=f"{COIN}  High Bid",    value=f"**{auction.current_bid:,} coins**",          inline=True)
        em.add_field(name="👤  Leading Bidder",   value=bidder.mention if bidder else "—",              inline=True)
        em.add_field(
            name="⏳  Status",
            value=(
                f"🔥 **LIVE** — `/bid [amount]` to beat it!\n"
                f"Timer resets to **{self._idle_timeout}s** on each bid."
            ),
            inline=False,
        )
        em.set_image(url=GIF_OUTBID)
        em.set_footer(text=f"BLACK MARKET  •  Money Heist Edition  •  Currency: Heist Coins {COIN}")
        return em

    def _sold_embed(self, auction: ActiveAuction) -> discord.Embed:
        """Full-screen winner celebration card."""
        assert auction.highest_bidder is not None
        em = discord.Embed(
            title="🎉  SOLD!  THE HEIST IS COMPLETE!",
            colour=C_SOLD,
            description=(
                f"```ansi\n\u001b[1;32m  🔨  GAVEL DOWN — IT'S OVER  🔨  \u001b[0m\n```\n"
                f"{SEP}\n"
                f"**{auction.item.name}** has left the vault for good!\n"
                f"{SEP}"
            ),
        )
        em.add_field(name="🏆  Winner",       value=auction.highest_bidder.mention,            inline=True)
        em.add_field(name=f"{COIN}  Final Bid", value=f"**{auction.current_bid:,} coins**",    inline=True)
        em.add_field(name="📦  Item",          value=auction.item.description,                 inline=False)
        em.set_thumbnail(url=auction.highest_bidder.display_avatar.url)
        em.set_image(url=GIF_SOLD)
        em.set_footer(text=f"BLACK MARKET  •  Money Heist Edition  •  Currency: Heist Coins {COIN}")
        return em

    @staticmethod
    def _no_bids_embed(item_name: str) -> discord.Embed:
        """Shown when the auction timer expires with no bids at all."""
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
        Background task — drives the full lifecycle of one auction.

        Phase 1  (initial window = self._initial_timeout)
            Wait for the very first bid.  If the clock runs out with no bids,
            fall into the idle loop immediately (which will also expire and
            produce a "no bids" close).

        Phase 2  (idle loop = self._idle_timeout per cycle)
            After every bid the 15 s window resets.  Silence for the full
            window kicks off the gavel sequence.

        Gavel sequence  (delay = self._gavel_delay between steps)
            "Going once…"   → if new bid → back to Phase 2
            "Going twice…"  → if new bid → back to Phase 2
            "SOLD!"         → _conclude_auction()

        Timer values are read from self._ each iteration so /auction-timer
        takes effect immediately even during a running auction.
        """
        auction = self._active.get(channel_id)
        if not auction:
            return

        # ── Phase 1: initial open window ─────────────────────────────────────
        auction.bid_event.clear()
        try:
            await asyncio.wait_for(
                auction.bid_event.wait(), timeout=float(self._initial_timeout)
            )
        except asyncio.TimeoutError:
            pass  # no bids in the opening window — flow into the idle loop

        # ── Phase 2: idle loop ────────────────────────────────────────────────
        while channel_id in self._active:
            auction = self._active[channel_id]
            auction.bid_event.clear()
            try:
                await asyncio.wait_for(
                    auction.bid_event.wait(), timeout=float(self._idle_timeout)
                )
                continue   # bid arrived → restart the idle window
            except asyncio.TimeoutError:
                pass       # silence for full idle_timeout → call the auction

            # ── No bid for idle_timeout seconds ──────────────────────────────

            if auction.highest_bidder is None:
                # Nobody bid at all
                auction.closed = True
                await auction.channel.send(embed=self._no_bids_embed(auction.item.name))
                self._active.pop(channel_id, None)
                return

            # ── Gavel sequence ────────────────────────────────────────────────
            snap_bid    = auction.current_bid
            snap_bidder = auction.highest_bidder

            # Going once
            await auction.channel.send(
                f"🎙️  **Going once** at **{COIN} {snap_bid:,}** "
                f"to **{snap_bidder.display_name}**! ⏳"
            )
            auction.bid_event.clear()
            try:
                await asyncio.wait_for(
                    auction.bid_event.wait(), timeout=float(self._gavel_delay)
                )
                await auction.channel.send(
                    f"⚡  **New bid just in! The gavel is back up.** "
                    f"Timer reset to **{self._idle_timeout}s**!"
                )
                continue   # back to idle loop
            except asyncio.TimeoutError:
                pass

            # Going twice
            await auction.channel.send(
                f"🎙️  **Going twice** at **{COIN} {snap_bid:,}**... 👀"
            )
            auction.bid_event.clear()
            try:
                await asyncio.wait_for(
                    auction.bid_event.wait(), timeout=float(self._gavel_delay)
                )
                await auction.channel.send(
                    f"⚡  **Saved at the last second!** "
                    f"Timer reset to **{self._idle_timeout}s**!"
                )
                continue
            except asyncio.TimeoutError:
                pass

            # SOLD — no counter-bid during either gavel call
            await self._conclude_auction(channel_id)
            return

    async def _conclude_auction(self, channel_id: int) -> None:
        """
        Called when the gavel falls:
          1. Pop from active map + mark closed
          2. Deduct coins from the winner's auction_bank row
          3. Edit the live embed → SOLD card
          4. Send the winner celebration embed + balance receipt
        """
        auction = self._active.pop(channel_id, None)
        if not auction:
            return
        if not auction.highest_bidder:
            await auction.channel.send(embed=self._no_bids_embed(auction.item.name))
            return

        auction.closed = True

        # Deduct from the winner's Heist Coin balance
        new_total = _db_deduct(auction.highest_bidder.id, auction.current_bid)

        # Edit the opening card to show the SOLD state
        if auction.embed_msg:
            try:
                await auction.embed_msg.edit(embed=self._sold_embed(auction))
            except discord.HTTPException:
                pass  # deleted or too old — not fatal

        # Big winner announcement
        await auction.channel.send(
            content=(
                f"🔨  {auction.highest_bidder.mention} "
                f"— **{auction.item.name}** is yours! 🎉"
            ),
            embed=self._sold_embed(auction),
        )

        # Balance receipt
        await auction.channel.send(
            f"{COIN}  **{auction.current_bid:,} Heist Coins** deducted from your bank. "
            f"Remaining balance: **{new_total:,} coins**."
        )

    # ═══════════════════════ ADMIN COMMANDS ═══════════════════════════════════

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
        starting_bid = "Minimum opening bid in Heist Coins.",
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
                f"❌  Starting bid must be at least **{COIN} 1**.", ephemeral=True
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
        em.add_field(name="📦  Name",          value=item.name,                        inline=True)
        em.add_field(name="💰  Starting Bid",  value=f"{COIN} {starting_bid:,} coins", inline=True)
        em.add_field(name="📝  Description",   value=item.description,                 inline=False)
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

        if channel_id in self._active:
            await interaction.response.send_message(
                "❌  An auction is **already running** in this channel. "
                "Wait for it to finish or use `/auction-cancel`.",
                ephemeral=True,
            )
            return

        queue = self._queue.get(interaction.guild_id, [])
        item  = next(
            (i for i in queue if i.name.lower() == item_name.strip().lower()), None
        )
        if item is None:
            await interaction.response.send_message(
                f"❌  **{item_name}** was not found in the queue.\n"
                "Add it first with `/auction-add`, or check spelling with `/auction-list`.",
                ephemeral=True,
            )
            return

        queue.remove(item)   # pull from queue before launching

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌  Auctions can only run in text channels.", ephemeral=True
            )
            return

        auction = ActiveAuction(
            item=item,
            channel=channel,
            current_bid=item.starting_bid,
        )
        self._active[channel_id] = auction

        # Defer to get a Message object back (needed for later edits)
        await interaction.response.defer()
        msg = await interaction.followup.send(embed=self._open_embed(item), wait=True)
        auction.embed_msg = msg

        task = asyncio.create_task(
            self._run_auction(channel_id), name=f"auction-{channel_id}"
        )
        auction.loop_task = task

    # ─────────────────────── /auction-cancel ─────────────────────────────────

    @app_commands.command(
        name="auction-cancel",
        description="[Admin] Cancel the running auction in this channel (no coins deducted).",
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

        auction.closed = True
        if auction.loop_task and not auction.loop_task.done():
            auction.loop_task.cancel()

        em = discord.Embed(
            title="🛑  Auction Cancelled",
            description=(
                f"**{auction.item.name}** has been pulled from the Black Market.\n"
                "The item is back in the vault. 🔐\n\n"
                + (
                    f"Top bid was **{COIN} {auction.current_bid:,}** "
                    f"by {auction.highest_bidder.mention} — **no coins deducted**."
                    if auction.highest_bidder else
                    "No bids had been placed."
                )
            ),
            colour=C_CANCEL,
        )
        em.set_footer(text=f"Cancelled by {interaction.user.display_name}  •  BLACK MARKET")
        await interaction.response.send_message(embed=em)

    # ─────────────────────── /auction-give ───────────────────────────────────

    @app_commands.command(
        name="auction-give",
        description="[Admin] Deposit Heist Coins into a member's auction bank.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        member = "The member to receive the coins.",
        amount = "Number of Heist Coins to give (must be positive).",
    )
    async def auction_give(
        self,
        interaction: discord.Interaction,
        member:      discord.Member,
        amount:      int,
    ) -> None:
        if amount <= 0:
            await interaction.response.send_message(
                "❌  Amount must be a positive number.", ephemeral=True
            )
            return

        new_total = _db_add(member.id, amount)

        em = discord.Embed(
            title=f"{COIN}  Heist Coins Deposited",
            colour=C_BANK,
            description=(
                f"**+{amount:,} coins** added to {member.mention}'s auction bank.\n\n"
                f"New balance: **{COIN} {new_total:,} coins**"
            ),
        )
        em.set_thumbnail(url=member.display_avatar.url)
        em.set_footer(
            text=f"Issued by {interaction.user.display_name}  •  BLACK MARKET BANK"
        )
        await interaction.response.send_message(embed=em, ephemeral=True)

    # ─────────────────────── /auction-set ────────────────────────────────────

    @app_commands.command(
        name="auction-set",
        description="[Admin] Hard-set a member's Heist Coin balance.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        member = "The member whose balance to set.",
        amount = "The exact new balance (≥ 0).",
    )
    async def auction_set(
        self,
        interaction: discord.Interaction,
        member:      discord.Member,
        amount:      int,
    ) -> None:
        if amount < 0:
            await interaction.response.send_message(
                "❌  Balance cannot be negative.", ephemeral=True
            )
            return

        old = _db_get(member.id)
        _db_set(member.id, amount)

        em = discord.Embed(
            title=f"{COIN}  Balance Updated",
            colour=C_BANK,
            description=(
                f"{member.mention}'s auction balance has been adjusted.\n\n"
                f"**Before:** {COIN} {old:,}\n"
                f"**After:**  {COIN} {amount:,}"
            ),
        )
        em.set_thumbnail(url=member.display_avatar.url)
        em.set_footer(
            text=f"Set by {interaction.user.display_name}  •  BLACK MARKET BANK"
        )
        await interaction.response.send_message(embed=em, ephemeral=True)

    # ─────────────────────── /auction-timer ──────────────────────────────────

    @app_commands.command(
        name="auction-timer",
        description="[Admin] Change the auction countdown timers.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        idle_seconds    = "Silence window after a bid before 'Going once' fires (current default: 15).",
        gavel_seconds   = "Delay between Going-once → twice → SOLD (current default: 5).",
        initial_seconds = "Opening window before the very first bid times out (current default: 30). 0 = no change.",
    )
    async def auction_timer(
        self,
        interaction:     discord.Interaction,
        idle_seconds:    int,
        gavel_seconds:   int,
        initial_seconds: int = 0,
    ) -> None:
        errors: list[str] = []
        if not (5 <= idle_seconds <= 300):
            errors.append("**Idle timeout** must be between **5** and **300** seconds.")
        if not (2 <= gavel_seconds <= 60):
            errors.append("**Gavel delay** must be between **2** and **60** seconds.")
        if initial_seconds != 0 and not (10 <= initial_seconds <= 600):
            errors.append("**Initial timeout** must be between **10** and **600** seconds (or 0 to leave unchanged).")

        if errors:
            await interaction.response.send_message(
                "❌  Invalid timer values:\n" + "\n".join(f"• {e}" for e in errors),
                ephemeral=True,
            )
            return

        old_idle    = self._idle_timeout
        old_gavel   = self._gavel_delay
        old_initial = self._initial_timeout

        self._idle_timeout  = idle_seconds
        self._gavel_delay   = gavel_seconds
        if initial_seconds != 0:
            self._initial_timeout = initial_seconds

        em = discord.Embed(
            title="⏱️  Auction Timers Updated",
            colour=C_GOLD,
            description=(
                "Changes take effect **immediately** — "
                "even for any auction currently running.\n\n"
                f"{SEP}"
            ),
        )
        em.add_field(
            name="⏳  Idle Timeout (after each bid)",
            value=f"`{old_idle}s` → **`{self._idle_timeout}s`**",
            inline=False,
        )
        em.add_field(
            name="🔨  Gavel Delay (going-once → twice → sold)",
            value=f"`{old_gavel}s` → **`{self._gavel_delay}s`**",
            inline=False,
        )
        if initial_seconds != 0:
            em.add_field(
                name="🕐  Initial Opening Window",
                value=f"`{old_initial}s` → **`{self._initial_timeout}s`**",
                inline=False,
            )
        em.set_footer(
            text=f"Updated by {interaction.user.display_name}  •  BLACK MARKET"
        )
        await interaction.response.send_message(embed=em, ephemeral=True)

    # ═══════════════════════ PUBLIC COMMANDS ══════════════════════════════════

    # ─────────────────────── /bid ─────────────────────────────────────────────

    @app_commands.command(
        name="bid",
        description="Place a bid in the active auction in this channel.",
    )
    @app_commands.guild_only()
    @app_commands.describe(amount="Number of Heist Coins you want to bid.")
    async def bid(
        self,
        interaction: discord.Interaction,
        amount:       int,
    ) -> None:
        channel_id = interaction.channel_id
        assert channel_id is not None

        # ── Guard: auction is live ────────────────────────────────────────────
        auction = self._active.get(channel_id)
        if not auction or auction.closed:
            await interaction.response.send_message(
                "❌  There is no active auction in this channel right now.",
                ephemeral=True,
            )
            return

        # ── Guard: must meet the starting bid ────────────────────────────────
        if amount < auction.item.starting_bid:
            await interaction.response.send_message(
                f"❌  Minimum bid is **{COIN} {auction.item.starting_bid:,}**.",
                ephemeral=True,
            )
            return

        # ── Guard: must beat the current high bid ────────────────────────────
        if auction.highest_bidder is not None and amount <= auction.current_bid:
            await interaction.response.send_message(
                f"❌  Your bid of **{COIN} {amount:,}** must beat "
                f"the current high of **{COIN} {auction.current_bid:,}**.\n"
                f"Minimum next bid: **{COIN} {auction.current_bid + 1:,}**.",
                ephemeral=True,
            )
            return

        # ── Guard: must have enough coins ────────────────────────────────────
        balance = _db_get(interaction.user.id)
        if balance < amount:
            await interaction.response.send_message(
                f"❌  **Insufficient Heist Coins!**\n"
                f"You need **{COIN} {amount:,}** but your bank holds **{COIN} {balance:,}**.\n"
                f"Ask an admin to top up your balance with `/auction-give`.",
                ephemeral=True,
            )
            return

        # ── Accept the bid ───────────────────────────────────────────────────
        assert isinstance(interaction.user, discord.Member)
        prev_bidder         = auction.highest_bidder
        auction.current_bid    = amount
        auction.highest_bidder = interaction.user

        # Wake the countdown task — resets its idle window
        auction.bid_event.set()

        # Edit the live embed
        if auction.embed_msg:
            try:
                await auction.embed_msg.edit(embed=self._live_embed(auction))
            except discord.HTTPException:
                pass

        # Public confirmation embed
        em = discord.Embed(
            title="✅  Bid Accepted!",
            colour=C_BID,
            description=(
                f"**{interaction.user.display_name}** is now the leading bidder!\n\n"
                f"{COIN}  **Current Bid:** {amount:,} coins\n"
                f"⏳  **Timer reset to {self._idle_timeout}s** — counter-bid to survive!"
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

    # ─────────────────────── /auction-balance ────────────────────────────────

    @app_commands.command(
        name="auction-balance",
        description="Check your (or another member's) Heist Coin auction bank balance.",
    )
    @app_commands.guild_only()
    @app_commands.describe(member="Leave blank to check your own balance.")
    async def auction_balance(
        self,
        interaction: discord.Interaction,
        member:      Optional[discord.Member] = None,
    ) -> None:
        assert isinstance(interaction.user, discord.Member)
        target  = member or interaction.user
        balance = _db_get(target.id)

        em = discord.Embed(
            title=f"{COIN}  Heist Coin Bank",
            colour=C_BANK,
            description=(
                f"**{target.display_name}**'s auction bank balance:\n\n"
                f"# {COIN} {balance:,} coins"
            ),
        )
        em.set_thumbnail(url=target.display_avatar.url)
        em.set_footer(text="BLACK MARKET BANK  •  Money Heist Edition")

        # Own balance = ephemeral; viewing someone else's = public
        await interaction.response.send_message(
            embed=em, ephemeral=(target == interaction.user)
        )

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
                "📭  The auction queue is empty. Admins can add items with `/auction-add`.",
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
                value=(
                    f"{item.description}\n"
                    f"Starting bid: **{COIN} {item.starting_bid:,}**"
                ),
                inline=False,
            )
        em.set_footer(text="BLACK MARKET  •  Money Heist Edition")
        await interaction.response.send_message(embed=em, ephemeral=True)

    # ─────────────────────── /auction-leaderboard ────────────────────────────

    @app_commands.command(
        name="auction-leaderboard",
        description="Show the top Heist Coin balances for this event.",
    )
    @app_commands.guild_only()
    async def auction_leaderboard(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        rows = _db_leaderboard(10)

        if not rows:
            await interaction.response.send_message(
                f"{COIN}  No balances recorded yet. Admins can fund teams with `/auction-give`.",
                ephemeral=True,
            )
            return

        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        lines: list[str] = []
        for rank, (user_id, coins) in enumerate(rows, start=1):
            medal = medals.get(rank, f"**#{rank}**")
            try:
                member = interaction.guild.get_member(user_id) or await interaction.guild.fetch_member(user_id)
                name   = member.display_name
            except discord.NotFound:
                name = f"User {user_id}"
            lines.append(f"{medal}  {name} — **{COIN} {coins:,}**")

        em = discord.Embed(
            title=f"{COIN}  Heist Coin Leaderboard",
            colour=C_LIST,
            description=f"{SEP}\n" + "\n".join(lines) + f"\n{SEP}",
        )
        em.set_footer(text="BLACK MARKET BANK  •  Money Heist Edition")
        await interaction.response.send_message(embed=em)


# ══════════════════════════════ SETUP ═════════════════════════════════════════


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AuctionCog(bot))
