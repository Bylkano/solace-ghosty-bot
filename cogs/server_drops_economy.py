"""
cogs/server_drops_economy.py
-----------------------------
A production-ready Discord.py v2.x Cog that implements a dynamic
chat-drop economy system backed by a local SQLite database.

Channel targeting:
  Drops only fire inside the guild's configured automod channel — the
  same channel set via /setchannel in cogs/moderation.py and stored in
  server_config.json via store.py.  If no channel has been configured
  for a guild yet, drops are silently skipped until an admin runs
  /setchannel.

Drop types (equal probability):
  A. Trivia Drop      – first correct typed answer wins 5–15 pts
  B. Word Scramble    – unscramble the word to win 5–15 pts
  C. Lootbox Drop     – click the button to win 15–30 pts

Economy stored in economy.db  →  table: users(user_id PK, points INT)
"""

import asyncio
import random
import sqlite3
import sys
import pathlib

# Make sure the project root is on the path so store.py is importable
# regardless of how Python resolves relative imports in a cogs sub-package.
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import discord
from discord import app_commands
from discord.ext import commands

from store import get_automod_channel  # ← shared channel config from the existing bot

# ─────────────────────────── constants ───────────────────────────

DB_PATH = pathlib.Path(__file__).parent.parent / "economy.db"  # sits next to bot.py
MSG_TRIGGER = 10        # drop fires every N messages in the watched channel
DROP_TIMEOUT = 30       # seconds before an unanswered drop expires

# Trivia bank  →  question : list of accepted answers (all compared lower-stripped)
TRIVIA_BANK: dict[str, list[str]] = {
    "What is 5 + 7?":                            ["12", "twelve"],
    "What color is the sky on a clear day?":     ["blue"],
    "How many sides does a triangle have?":      ["3", "three"],
    "What is the capital of France?":            ["paris"],
    "What is 9 × 9?":                            ["81", "eighty-one", "eighty one"],
    "How many planets are in our solar system?": ["8", "eight"],
    "What gas do plants absorb from the air?":   ["carbon dioxide", "co2"],
    "What is the square root of 144?":           ["12", "twelve"],
    "Which continent is Brazil on?":             ["south america"],
    "What is H2O commonly known as?":            ["water"],
}

WORD_LIST: list[str] = [
    "python", "discord", "server", "economy", "lootbox",
    "scramble", "trivia", "reward", "points", "channel",
    "button", "winner", "random", "treasure", "typing",
]

# Embed colour palette
COLOUR_TRIVIA   = discord.Colour.blue()
COLOUR_SCRAMBLE = discord.Colour.purple()
COLOUR_LOOTBOX  = discord.Colour.green()
COLOUR_POINTS   = discord.Colour.gold()
COLOUR_TIMEOUT  = discord.Colour.red()


# ─────────────────────────── database helpers ────────────────────

def _db_connect() -> sqlite3.Connection:
    """Return a connection with row_factory for convenience."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    """Create the users table if it doesn't already exist."""
    with _db_connect() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                points  INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        con.commit()


def add_points(user_id: int, amount: int) -> int:
    """
    Atomically upsert a user row and add `amount` points.
    Returns the user's new total.
    """
    with _db_connect() as con:
        con.execute(
            """
            INSERT INTO users (user_id, points)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET points = points + excluded.points
            """,
            (user_id, amount),
        )
        con.commit()
        row = con.execute(
            "SELECT points FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["points"] if row else amount


def get_points(user_id: int) -> int:
    """Fetch a user's current point balance (0 if not found)."""
    with _db_connect() as con:
        row = con.execute(
            "SELECT points FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["points"] if row else 0


# ─────────────────────────── utility ─────────────────────────────

def scramble_word(word: str) -> str:
    """Shuffle a word's letters, guaranteeing the result differs from the original."""
    letters = list(word)
    for _ in range(100):
        random.shuffle(letters)
        if "".join(letters) != word:
            return "".join(letters)
    # Fallback: swap first two chars (works for any word len ≥ 2)
    letters[0], letters[1] = letters[1], letters[0]
    return "".join(letters)


# ─────────────────────────── UI component ────────────────────────

class LootboxView(discord.ui.View):
    """A single-use button view for the Lootbox Supply Drop."""

    def __init__(self, cog: "ServerDropsEconomy", channel_id: int):
        super().__init__(timeout=None)  # we manage expiry ourselves via asyncio
        self.cog = cog
        self.channel_id = channel_id
        self.claimed = False  # guard against race-condition double-claims

    @discord.ui.button(label="🎁  Grab Loot!", style=discord.ButtonStyle.success)
    async def grab_loot(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        # ── concurrency guard ──
        if self.claimed:
            await interaction.response.send_message(
                "Someone already grabbed this loot!", ephemeral=True
            )
            return

        drop = self.cog.active_drops.get(self.channel_id)
        if drop is None or drop.get("type") != "lootbox":
            await interaction.response.send_message(
                "This lootbox has already expired.", ephemeral=True
            )
            return

        self.claimed = True

        # ── remove from memory BEFORE touching the database ──
        del self.cog.active_drops[self.channel_id]

        payout = drop["payout"]
        new_total = add_points(interaction.user.id, payout)

        # Disable the button so no one else can click
        button.disabled = True
        button.label = f"🎁  Claimed by {interaction.user.display_name}!"
        await interaction.response.edit_message(view=self)

        embed = discord.Embed(
            title="🎉  Loot Secured!",
            description=(
                f"{interaction.user.mention} grabbed the lootbox and won "
                f"**{payout} points**!\n"
                f"New balance: **{new_total} points** 💰"
            ),
            colour=COLOUR_LOOTBOX,
        )
        await interaction.followup.send(embed=embed)

        # Cancel the timeout task
        task: asyncio.Task | None = drop.get("task")
        if task and not task.done():
            task.cancel()

        self.stop()


# ─────────────────────────── the Cog ─────────────────────────────

class ServerDropsEconomy(commands.Cog, name="ServerDropsEconomy"):
    """
    Dynamic chat-drop economy system with SQLite persistence.

    Only listens inside the guild's configured automod channel
    (shared with the existing Ghosty moderation cog via store.py).
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_db()

        # channel_id → message counter (only the watched channel increments)
        self.msg_counters: dict[int, int] = {}

        # channel_id → active drop state dict
        # shape: { "type": str, "answer": list[str], "payout": int, "task": Task, ... }
        self.active_drops: dict[int, dict] = {}

    # ─────────────── helpers ─────────────────────────

    def _get_watched_channel_id(self, guild_id: int) -> int | None:
        """
        Returns the automod channel ID configured for this guild via /setchannel,
        or None if not yet set.  Delegates entirely to store.get_automod_channel()
        so both cogs always reference the same channel.
        """
        return get_automod_channel(guild_id)

    # ─────────────── message listener ────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore bots, system messages, and DMs
        if message.author.bot:
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        if not isinstance(message.channel, discord.TextChannel):
            return
        if message.guild is None:
            return

        # ── Only act on the guild's configured channel ──
        watched_id = self._get_watched_channel_id(message.guild.id)
        if watched_id is None or message.channel.id != watched_id:
            return

        channel_id = message.channel.id

        # ── Route to answer-checker if a text drop is waiting ──
        if channel_id in self.active_drops:
            drop = self.active_drops[channel_id]
            if drop["type"] in ("trivia", "scramble"):
                await self._check_text_answer(message, drop)
            # Lootbox drops are button-only; ignore chat messages
            return

        # ── Increment counter; trigger when threshold is hit ──
        self.msg_counters[channel_id] = self.msg_counters.get(channel_id, 0) + 1

        if self.msg_counters[channel_id] >= MSG_TRIGGER:
            self.msg_counters[channel_id] = 0   # reset immediately
            await self._trigger_drop(message.channel)

    # ─────────────── drop triggering ─────────────────

    async def _trigger_drop(self, channel: discord.TextChannel):
        """Randomly choose and send one of the three drop types."""
        choice = random.choice(["trivia", "scramble", "lootbox"])
        if choice == "trivia":
            await self._start_trivia(channel)
        elif choice == "scramble":
            await self._start_scramble(channel)
        else:
            await self._start_lootbox(channel)

    # ── A. Trivia Drop ────────────────────────────────

    async def _start_trivia(self, channel: discord.TextChannel):
        question, answers = random.choice(list(TRIVIA_BANK.items()))
        payout = random.randint(5, 15)

        embed = discord.Embed(
            title="🧠  Trivia Drop!",
            description=(
                f"**{question}**\n\n"
                f"First correct answer wins **{payout} points**!\n"
                f"⏱️ You have **{DROP_TIMEOUT} seconds**."
            ),
            colour=COLOUR_TRIVIA,
        )
        embed.set_footer(text="Type your answer in chat!")
        await channel.send(embed=embed)

        drop_state = {
            "type":   "trivia",
            "answer": [a.lower().strip() for a in answers],
            "payout": payout,
            "task":   None,
        }
        self.active_drops[channel.id] = drop_state
        drop_state["task"] = asyncio.create_task(self._drop_timeout(channel))

    # ── B. Word Scramble Drop ─────────────────────────

    async def _start_scramble(self, channel: discord.TextChannel):
        word = random.choice(WORD_LIST)
        scrambled = scramble_word(word)
        payout = random.randint(5, 15)

        embed = discord.Embed(
            title="🔀  Word Scramble Drop!",
            description=(
                f"Unscramble this word:\n"
                f"# `{scrambled.upper()}`\n\n"
                f"First correct answer wins **{payout} points**!\n"
                f"⏱️ You have **{DROP_TIMEOUT} seconds**."
            ),
            colour=COLOUR_SCRAMBLE,
        )
        embed.set_footer(text="Type the unscrambled word in chat!")
        await channel.send(embed=embed)

        drop_state = {
            "type":   "scramble",
            "answer": [word.lower().strip()],
            "payout": payout,
            "task":   None,
        }
        self.active_drops[channel.id] = drop_state
        drop_state["task"] = asyncio.create_task(self._drop_timeout(channel))

    # ── C. Lootbox Supply Drop ────────────────────────

    async def _start_lootbox(self, channel: discord.TextChannel):
        payout = random.randint(15, 30)

        embed = discord.Embed(
            title="📦  Lootbox Supply Drop!",
            description=(
                "A mystery crate just landed!\n\n"
                "Press the button below to claim a random reward between "
                f"**15 and 30 points**!\n"
                f"⏱️ You have **{DROP_TIMEOUT} seconds**."
            ),
            colour=COLOUR_LOOTBOX,
        )
        embed.set_footer(text="First click wins!")

        view = LootboxView(cog=self, channel_id=channel.id)
        msg = await channel.send(embed=embed, view=view)

        drop_state = {
            "type":   "lootbox",
            "payout": payout,
            "view":   view,
            "msg":    msg,
            "task":   None,
        }
        self.active_drops[channel.id] = drop_state
        drop_state["task"] = asyncio.create_task(self._drop_timeout(channel))

    # ─────────────── answer checker ──────────────────

    async def _check_text_answer(self, message: discord.Message, drop: dict):
        """Compare the incoming message against the active drop's accepted answers."""
        user_answer = message.content.lower().strip()

        if user_answer not in drop["answer"]:
            return  # Wrong — keep waiting

        channel_id = message.channel.id

        # ── remove from memory BEFORE database write ──
        del self.active_drops[channel_id]

        task: asyncio.Task | None = drop.get("task")
        if task and not task.done():
            task.cancel()

        payout = drop["payout"]
        new_total = add_points(message.author.id, payout)

        embed = discord.Embed(
            title="🎉  Correct!",
            description=(
                f"{message.author.mention} got it right and won "
                f"**{payout} points**!\n"
                f"New balance: **{new_total} points** 💰"
            ),
            colour=COLOUR_TRIVIA if drop["type"] == "trivia" else COLOUR_SCRAMBLE,
        )
        await message.channel.send(embed=embed)

    # ─────────────── timeout handler ─────────────────

    async def _drop_timeout(self, channel: discord.TextChannel):
        """Wait DROP_TIMEOUT seconds then gracefully expire the active drop."""
        await asyncio.sleep(DROP_TIMEOUT)

        drop = self.active_drops.pop(channel.id, None)
        if drop is None:
            return  # Already claimed

        # Disable button for lootbox drops
        if drop["type"] == "lootbox":
            view: LootboxView = drop["view"]
            view.claimed = True
            view.stop()
            try:
                for child in view.children:
                    child.disabled = True  # type: ignore[attr-defined]
                await drop["msg"].edit(view=view)
            except discord.HTTPException:
                pass

        embed = discord.Embed(
            title="⏰  Time's Up!",
            description="Nobody claimed the drop in time. Better luck next time!",
            colour=COLOUR_TIMEOUT,
        )
        await channel.send(embed=embed)

    # ─────────────── /points command ─────────────────

    @commands.hybrid_command(
        name="points",
        description="Check your (or another member's) current point balance.",
    )
    @app_commands.describe(member="The member whose points you want to check.")
    async def points_command(
        self,
        ctx: commands.Context,
        member: discord.Member | None = None,
    ):
        target = member or ctx.author
        balance = get_points(target.id)

        embed = discord.Embed(
            title="💰  Point Balance",
            description=f"{target.mention} currently has **{balance} points**.",
            colour=COLOUR_POINTS,
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text="Keep chatting to earn more drops!")
        await ctx.send(embed=embed)


# ─────────────────────────── setup ───────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerDropsEconomy(bot))
