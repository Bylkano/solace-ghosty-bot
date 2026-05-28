"""
cogs/server_drops_economy.py
-----------------------------
Economy drop system with dark & sleek styled embeds + leaderboard.

  /setchannel  →  moderation channel  (cogs/moderation.py)
  /setdrops    →  economy drops channel (this cog)
  /points      →  check point balance
  /leaderboard →  top 10 players
"""

import asyncio
import random
import sqlite3
import sys
import pathlib
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import discord
from discord import app_commands
from discord.ext import commands

from store import get_drops_channel, set_drops_channel

# ─────────────────────────── constants ───────────────────────────

DB_PATH      = pathlib.Path(__file__).parent.parent / "economy.db"
MSG_TRIGGER  = 10
DROP_TIMEOUT = 30

# ── Dark & sleek colour palette ───────────────────────────────────
# All slightly desaturated so they read as "dark UI accent" colours
C_TRIVIA   = 0x5865F2   # discord blurple
C_SCRAMBLE = 0x9B59B6   # deep violet
C_LOOTBOX  = 0x2ECC71   # emerald (dim)
C_WIN      = 0xF1C40F   # gold
C_POINTS   = 0xE67E22   # burnt amber
C_BOARD    = 0x2C2F33   # near-black (discord dark bg)
C_TIMEOUT  = 0x747F8D   # muted grey
C_SET      = 0x43B581   # green confirmation

# ── Rank medals for leaderboard ───────────────────────────────────
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}

# ── Thin separator line used across embeds ────────────────────────
SEP = "▬" * 22

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


# ─────────────────────────── database helpers ────────────────────

def _db_connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
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
    with _db_connect() as con:
        con.execute(
            """
            INSERT INTO users (user_id, points) VALUES (?, ?)
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
    with _db_connect() as con:
        row = con.execute(
            "SELECT points FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["points"] if row else 0


def get_leaderboard(limit: int = 10) -> list[sqlite3.Row]:
    with _db_connect() as con:
        return con.execute(
            "SELECT user_id, points FROM users ORDER BY points DESC LIMIT ?",
            (limit,),
        ).fetchall()


# ─────────────────────────── embed builders ──────────────────────

def _base_embed(title: str, colour: int) -> discord.Embed:
    """Shared dark-style base: no timestamp clutter, consistent footer line."""
    embed = discord.Embed(title=title, colour=colour)
    return embed


def embed_trivia(question: str, payout: int) -> discord.Embed:
    e = _base_embed("", C_TRIVIA)
    e.description = (
        f"```ansi\n\u001b[1;34m  ◈  TRIVIA DROP  ◈\u001b[0m\n```"
        f"{SEP}\n"
        f"**{question}**\n"
        f"{SEP}\n"
        f"⚡ **First correct answer wins `{payout} pts`**\n"
        f"⏳ `{DROP_TIMEOUT}s` to answer — type it below"
    )
    e.set_footer(text="SOLACE ECONOMY  •  Trivia Event")
    return e


def embed_scramble(scrambled: str, payout: int) -> discord.Embed:
    e = _base_embed("", C_SCRAMBLE)
    e.description = (
        f"```ansi\n\u001b[1;35m  ◈  WORD SCRAMBLE  ◈\u001b[0m\n```"
        f"{SEP}\n"
        f"**Unscramble this word:**\n"
        f"```\n{scrambled.upper()}\n```"
        f"{SEP}\n"
        f"⚡ **First correct answer wins `{payout} pts`**\n"
        f"⏳ `{DROP_TIMEOUT}s` remaining — type it below"
    )
    e.set_footer(text="SOLACE ECONOMY  •  Scramble Event")
    return e


def embed_lootbox(payout_range: str) -> discord.Embed:
    e = _base_embed("", C_LOOTBOX)
    e.description = (
        f"```ansi\n\u001b[1;32m  ◈  SUPPLY DROP  ◈\u001b[0m\n```"
        f"{SEP}\n"
        f"**A mystery crate has landed in the server.**\n\n"
        f"💰 Reward: `{payout_range} pts`  *(random)*\n"
        f"⏳ `{DROP_TIMEOUT}s` before it disappears\n"
        f"{SEP}\n"
        f"*Click the button below — first grab wins.*"
    )
    e.set_footer(text="SOLACE ECONOMY  •  Supply Drop")
    return e


def embed_win_text(user: discord.Member | discord.User, payout: int, new_total: int, drop_type: str) -> discord.Embed:
    colour = C_TRIVIA if drop_type == "trivia" else C_SCRAMBLE
    e = _base_embed("", colour)
    e.description = (
        f"```ansi\n\u001b[1;33m  ✔  CORRECT  \u001b[0m\n```"
        f"{SEP}\n"
        f"{user.mention} answered correctly\n\n"
        f"**＋{payout} pts** added  ›  Balance: `{new_total} pts`\n"
        f"{SEP}"
    )
    e.set_thumbnail(url=user.display_avatar.url)
    e.set_footer(text="SOLACE ECONOMY")
    return e


def embed_win_lootbox(user: discord.Member | discord.User, payout: int, new_total: int) -> discord.Embed:
    e = _base_embed("", C_WIN)
    e.description = (
        f"```ansi\n\u001b[1;33m  ✔  LOOT CLAIMED  \u001b[0m\n```"
        f"{SEP}\n"
        f"{user.mention} intercepted the crate\n\n"
        f"**＋{payout} pts** added  ›  Balance: `{new_total} pts`\n"
        f"{SEP}"
    )
    e.set_thumbnail(url=user.display_avatar.url)
    e.set_footer(text="SOLACE ECONOMY  •  Supply Drop")
    return e


def embed_timeout() -> discord.Embed:
    e = _base_embed("", C_TIMEOUT)
    e.description = (
        f"```ansi\n\u001b[1;30m  ✖  TIME EXPIRED  \u001b[0m\n```"
        f"{SEP}\n"
        f"Nobody claimed the drop in time.\n"
        f"*Keep chatting — the next one is coming.*\n"
        f"{SEP}"
    )
    e.set_footer(text="SOLACE ECONOMY")
    return e


def embed_points(user: discord.Member | discord.User, balance: int) -> discord.Embed:
    e = _base_embed("", C_POINTS)
    e.description = (
        f"```ansi\n\u001b[1;33m  ◈  BALANCE  ◈\u001b[0m\n```"
        f"{SEP}\n"
        f"**{user.display_name}**\n"
        f"```\n{balance:,} pts\n```"
        f"{SEP}"
    )
    e.set_thumbnail(url=user.display_avatar.url)
    e.set_footer(text="SOLACE ECONOMY  •  /leaderboard to see rankings")
    return e


async def embed_leaderboard(guild: discord.Guild, rows: list) -> discord.Embed:
    e = _base_embed("", C_BOARD)
    e.description = (
        f"```ansi\n\u001b[1;37m  ◈  LEADERBOARD  ◈\u001b[0m\n```"
        f"{SEP}\n"
    )

    lines: list[str] = []
    for rank, row in enumerate(rows, start=1):
        medal = MEDALS.get(rank, f"`#{rank:>2}`")
        # Try to resolve the member name from the guild cache
        member = guild.get_member(row["user_id"])
        name = member.display_name if member else f"User {row['user_id']}"
        lines.append(f"{medal}  **{name}** — `{row['points']:,} pts`")

    e.description += "\n".join(lines) if lines else "*No entries yet.*"
    e.description += f"\n{SEP}"
    e.set_footer(text=f"SOLACE ECONOMY  •  Top {len(rows)} players")
    return e


def embed_set_confirm(label: str, channel: discord.TextChannel) -> discord.Embed:
    e = _base_embed("", C_SET)
    e.description = (
        f"```ansi\n\u001b[1;32m  ✔  CHANNEL SET  \u001b[0m\n```"
        f"{SEP}\n"
        f"**{label}** → {channel.mention}\n"
        f"{SEP}"
    )
    e.set_footer(text="SOLACE ECONOMY")
    return e


def embed_get_channel(label: str, channel_id: int | None) -> discord.Embed:
    if channel_id:
        e = _base_embed("", C_SET)
        e.description = (
            f"{SEP}\n"
            f"**{label}** is set to <#{channel_id}>\n"
            f"{SEP}"
        )
    else:
        e = _base_embed("", C_TIMEOUT)
        e.description = (
            f"{SEP}\n"
            f"**{label}** has not been configured yet.\n"
            f"{SEP}"
        )
    e.set_footer(text="SOLACE ECONOMY")
    return e


# ─────────────────────────── utility ─────────────────────────────

def scramble_word(word: str) -> str:
    letters = list(word)
    for _ in range(100):
        random.shuffle(letters)
        if "".join(letters) != word:
            return "".join(letters)
    letters[0], letters[1] = letters[1], letters[0]
    return "".join(letters)


# ─────────────────────────── UI component ────────────────────────

class LootboxView(discord.ui.View):
    def __init__(self, cog: "ServerDropsEconomy", channel_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.channel_id = channel_id
        self.claimed = False

    @discord.ui.button(label="  GRAB LOOT  ", style=discord.ButtonStyle.success, emoji="📦")
    async def grab_loot(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.claimed:
            await interaction.response.send_message(
                "This crate has already been claimed.", ephemeral=True
            )
            return

        drop = self.cog.active_drops.get(self.channel_id)
        if drop is None or drop.get("type") != "lootbox":
            await interaction.response.send_message(
                "This drop has expired.", ephemeral=True
            )
            return

        self.claimed = True
        del self.cog.active_drops[self.channel_id]   # memory before DB

        payout    = drop["payout"]
        new_total = add_points(interaction.user.id, payout)

        button.disabled = True
        button.label = f"  Claimed by {interaction.user.display_name}  "
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            embed=embed_win_lootbox(interaction.user, payout, new_total)
        )

        task: asyncio.Task | None = drop.get("task")
        if task and not task.done():
            task.cancel()
        self.stop()


# ─────────────────────────── the Cog ─────────────────────────────

class ServerDropsEconomy(commands.Cog, name="ServerDropsEconomy"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_db()
        self.msg_counters: dict[int, int] = {}
        self.active_drops: dict[int, dict] = {}

    # ─────────────── message listener ────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        if not isinstance(message.channel, discord.TextChannel):
            return
        if message.guild is None:
            return

        drops_id = get_drops_channel(message.guild.id)
        if drops_id is None or message.channel.id != drops_id:
            return

        cid = message.channel.id

        if cid in self.active_drops:
            drop = self.active_drops[cid]
            if drop["type"] in ("trivia", "scramble"):
                await self._check_text_answer(message, drop)
            return

        self.msg_counters[cid] = self.msg_counters.get(cid, 0) + 1
        if self.msg_counters[cid] >= MSG_TRIGGER:
            self.msg_counters[cid] = 0
            await self._trigger_drop(message.channel)

    # ─────────────── drop triggering ─────────────────

    async def _trigger_drop(self, channel: discord.TextChannel):
        choice = random.choice(["trivia", "scramble", "lootbox"])
        if choice == "trivia":
            await self._start_trivia(channel)
        elif choice == "scramble":
            await self._start_scramble(channel)
        else:
            await self._start_lootbox(channel)

    async def _start_trivia(self, channel: discord.TextChannel):
        question, answers = random.choice(list(TRIVIA_BANK.items()))
        payout = random.randint(5, 15)
        await channel.send(embed=embed_trivia(question, payout))
        state = {"type": "trivia", "answer": [a.lower().strip() for a in answers], "payout": payout, "task": None}
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._drop_timeout(channel))

    async def _start_scramble(self, channel: discord.TextChannel):
        word      = random.choice(WORD_LIST)
        scrambled = scramble_word(word)
        payout    = random.randint(5, 15)
        await channel.send(embed=embed_scramble(scrambled, payout))
        state = {"type": "scramble", "answer": [word.lower().strip()], "payout": payout, "task": None}
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._drop_timeout(channel))

    async def _start_lootbox(self, channel: discord.TextChannel):
        payout = random.randint(15, 30)
        view   = LootboxView(cog=self, channel_id=channel.id)
        msg    = await channel.send(embed=embed_lootbox("15–30"), view=view)
        state  = {"type": "lootbox", "payout": payout, "view": view, "msg": msg, "task": None}
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._drop_timeout(channel))

    # ─────────────── answer checker ──────────────────

    async def _check_text_answer(self, message: discord.Message, drop: dict):
        if message.content.lower().strip() not in drop["answer"]:
            return

        cid = message.channel.id
        del self.active_drops[cid]

        task: asyncio.Task | None = drop.get("task")
        if task and not task.done():
            task.cancel()

        payout    = drop["payout"]
        new_total = add_points(message.author.id, payout)
        await message.channel.send(
            embed=embed_win_text(message.author, payout, new_total, drop["type"])
        )

    # ─────────────── timeout handler ─────────────────

    async def _drop_timeout(self, channel: discord.TextChannel):
        await asyncio.sleep(DROP_TIMEOUT)
        drop = self.active_drops.pop(channel.id, None)
        if drop is None:
            return

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

        await channel.send(embed=embed_timeout())

    # ─────────────── /setdrops ────────────────────────

    @app_commands.command(name="setdrops", description="Set the channel where economy drops will fire.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel="The text channel to send drops in.")
    async def setdrops(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        assert interaction.guild_id is not None
        set_drops_channel(interaction.guild_id, channel.id)
        await interaction.response.send_message(
            embed=embed_set_confirm("Drops Channel", channel), ephemeral=True
        )

    @app_commands.command(name="getdrops", description="Show which channel is set for economy drops.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def getdrops(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        channel_id = get_drops_channel(interaction.guild_id)
        await interaction.response.send_message(
            embed=embed_get_channel("Drops Channel", channel_id), ephemeral=True
        )

    # ─────────────── /points ─────────────────────────

    @commands.hybrid_command(name="points", description="Check your (or another member's) point balance.")
    @app_commands.describe(member="The member whose points you want to check.")
    async def points_command(self, ctx: commands.Context, member: discord.Member | None = None):
        target  = member or ctx.author
        balance = get_points(target.id)
        await ctx.send(embed=embed_points(target, balance))

    # ─────────────── /leaderboard ────────────────────

    @commands.hybrid_command(name="leaderboard", description="Show the top 10 players by points.")
    async def leaderboard_command(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        rows = get_leaderboard(10)
        await ctx.send(embed=await embed_leaderboard(ctx.guild, rows))


# ─────────────────────────── setup ───────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerDropsEconomy(bot))
