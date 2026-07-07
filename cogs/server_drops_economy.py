"""
cogs/server_drops_economy.py
-----------------------------
Solace Event drop system — expanded edition.

  /setdrops    →  configure the economy drops channel
  /getdrops    →  view configured drops channel
  /points      →  check point balance
  /leaderboard →  top 10 players

Drop Types (weighted):
  Trivia           (20%)  – first correct text answer wins
  Scramble         (15%)  – unscramble the word
  Lootbox          (10%)  – click the button first
  Boss Raid        (10%)  – co-op !attack the monster
  Hot or Cold      (15%)  – guess a number 1–100
  Emoji Puzzle     (15%)  – identify what the emojis represent
  Reaction Bomb    (10%)  – click AFTER the signal (not before!)
  Multiplier Drop   (5%)  – double-points or bounty event
  Blackjack Duel    (5%)  – community vs House; split 500-pt pool on win
  Hot Potato       (10%)  – pass it or get exploded (bomb or golden sack)
"""

from __future__ import annotations

import asyncio
import logging
import random
import sys
import pathlib
import os
import time
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import discord
from discord import app_commands
from discord.ext import commands
import psycopg2
from psycopg2.extras import RealDictCursor

from store import (
    get_drops_channel, set_drops_channel,
    get_drop_trigger, set_drop_trigger,
    get_ping_role, set_ping_role, clear_ping_role,
    get_all_channels_mode, set_all_channels_mode,
    add_disabled_channel, remove_disabled_channel, get_disabled_channels,
    get_drops_paused, set_drops_paused,
)

# ──────────────────────────── constants ───────────────────────────

_DB_URL = os.environ.get("DATABASE_URL", "")
MSG_TRIGGER  = 10
DROP_TIMEOUT = 30


# Colour palette — dark/sleek accents
C_TRIVIA   = 0x5865F2   # discord blurple
C_SCRAMBLE = 0x9B59B6   # deep violet
C_LOOTBOX  = 0x2ECC71   # emerald
C_BOSS     = 0xE74C3C   # crimson
C_HOT_COLD = 0x3498DB   # sky blue
C_EMOJI    = 0xF39C12   # amber
C_BOMB     = 0xFF6B35   # orange-red
C_MULTI    = 0x1ABC9C   # teal
C_WIN      = 0xF1C40F   # gold
C_POINTS   = 0xE67E22   # burnt amber
C_BOARD    = 0x2C2F33   # near-black
C_TIMEOUT  = 0x747F8D   # muted grey
C_SET      = 0x43B581   # green confirmation
C_BOUNTY   = 0xFF4500   # deep orange
C_BLACKJACK = 0x1A472A  # casino green
C_POTATO   = 0xFFD700   # golden yellow
C_FILL     = 0xE91E8C   # pink for fill in the blank
C_MATH     = 0x00BCD4   # cyan for fast math
C_TF       = 0x8BC34A   # green for true/false

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
SEP    = "▬" * 22

# ── Weighted event pool ────────────────────────────────────────────
EVENT_POOL = (
    ["trivia"]     * 18 +
    ["scramble"]   * 12 +
    ["boss"]       *  8 +
    ["emoji"]      *  9 +
    ["bomb"]       *  8 +
    ["multi"]      *  5 +
    ["blackjack"]  *  5 +
    ["fastmath"]   * 12 +
    ["worldboss"]  *  4 +
    ["heist"]      *  5 +
    ["losers"]     *  5
)

# ── Catch-up / event mechanics ─────────────────────────────────────
C_HAPPY           = 0xF1C40F       # gold accent for Happy Hour
UNDERDOG_LOCK_N   = 3              # Top 3: no bonus, and barred from the Losers Bracket
UNDERDOG_MID_MAX  = 7              # ranks 4-7 get the mid catch-up bonus
UNDERDOG_MID_MULT = 1.5            # multiplier for ranks 4-7
UNDERDOG_LOW_MULT = 1.7            # multiplier for rank 8 and below (or unranked)
UNDERDOG_MULT     = UNDERDOG_MID_MULT   # back-compat alias
UNDERDOG_TOP_N    = UNDERDOG_LOCK_N     # back-compat alias (now the podium-lock size)
RARE_DROPS        = ("boss", "blackjack", "multi", "bomb")  # doubled during Happy Hour
HAPPY_DEFAULT_MIN = 60             # default Mega-Drop Happy Hour length (minutes)
HAPPY_MAX_MIN     = 360            # safety cap on Happy Hour length
WORLDBOSS_HP      = 750            # default World Boss health pool
WORLDBOSS_MIN     = 10             # default World Boss duration (minutes)
WORLDBOSS_POOL    = 5000           # default World Boss point payout pool
WORLDBOSS_NAMES   = (
    "Vortharion the Devourer",
    "The Obsidian Leviathan",
    "Mor'Gath, World-Ender",
    "The Crimson Titan",
    "Nyxhaal the Eclipse",
    "Garrukthar the Unbroken",
)

from .economy_content import (
    TRIVIA_BANK,
    SCRAMBLE_WORDS,
    EMOJI_PUZZLES,
    FILL_BLANK_BANK,
    MATH_BANK,
    TRUE_FALSE_BANK,
)
from .economy_pool import RotatingPool, TriviaPool

# ─────────────────────── PostgreSQL helpers ──────────────────────

log = logging.getLogger(__name__)


class EconomyError(Exception):
    """Raised when a points read/write fails."""


def _pg_connect():
    if not _DB_URL:
        raise RuntimeError("DATABASE_URL not set in environment")
    return psycopg2.connect(_DB_URL, sslmode="require")


def _clamp_points(value: int) -> int:
    return max(0, int(value))


def _ensure_non_negative_amount(amount: int, *, field: str = "amount") -> int:
    value = int(amount)
    if value < 0:
        raise ValueError(f"{field} must be non-negative, got {value}")
    return value


def _normalize_answer(text: str) -> str:
    return text.lower().strip()


def init_db() -> None:
    try:
        with _pg_connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS economy_users (
                        user_id BIGINT PRIMARY KEY,
                        points  INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
            con.commit()
    except psycopg2.Error as exc:
        log.exception("init_db failed")
        raise EconomyError("Could not initialize economy database") from exc


def add_points(user_id: int, amount: int) -> int:
    """UPSERT points for a user and return their new total."""
    amount = _ensure_non_negative_amount(amount)
    if amount == 0:
        return get_points(user_id)
    try:
        with _pg_connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO economy_users (user_id, points) VALUES (%s, %s)
                    ON CONFLICT (user_id) DO UPDATE
                        SET points = economy_users.points + EXCLUDED.points
                    RETURNING points
                    """,
                    (user_id, amount),
                )
                row = cur.fetchone()
            con.commit()
            return _clamp_points(row[0]) if row else amount
    except psycopg2.Error as exc:
        log.exception("add_points failed for user %s", user_id)
        raise EconomyError("Could not add points") from exc


def deduct_points(user_id: int, amount: int) -> int:
    """Deduct points (floor at 0) and return new total."""
    amount = _ensure_non_negative_amount(amount)
    if amount == 0:
        return get_points(user_id)
    try:
        with _pg_connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO economy_users (user_id, points) VALUES (%s, 0)
                    ON CONFLICT (user_id) DO UPDATE
                        SET points = GREATEST(0, economy_users.points - %s)
                    RETURNING points
                    """,
                    (user_id, amount),
                )
                row = cur.fetchone()
            con.commit()
            return _clamp_points(row[0]) if row else 0
    except psycopg2.Error as exc:
        log.exception("deduct_points failed for user %s", user_id)
        raise EconomyError("Could not deduct points") from exc


def payout_from_target(target_id: int, winner_id: int, deduct: int, award: int) -> tuple[int, int]:
    """Atomically deduct from target (floored at 0) and credit winner."""
    deduct = _ensure_non_negative_amount(deduct, field="deduct")
    award = _ensure_non_negative_amount(award, field="award")
    if deduct == 0 and award == 0:
        return get_points(target_id), get_points(winner_id)
    try:
        with _pg_connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    "INSERT INTO economy_users (user_id, points) VALUES (%s, 0) ON CONFLICT DO NOTHING",
                    (target_id,),
                )
                cur.execute(
                    "INSERT INTO economy_users (user_id, points) VALUES (%s, 0) ON CONFLICT DO NOTHING",
                    (winner_id,),
                )
                cur.execute(
                    """
                    UPDATE economy_users
                    SET points = GREATEST(0, points - %s)
                    WHERE user_id = %s
                    """,
                    (deduct, target_id),
                )
                cur.execute(
                    """
                    INSERT INTO economy_users (user_id, points) VALUES (%s, %s)
                    ON CONFLICT (user_id) DO UPDATE
                        SET points = economy_users.points + EXCLUDED.points
                    RETURNING points
                    """,
                    (winner_id, award),
                )
                winner_row = cur.fetchone()
                cur.execute(
                    "SELECT points FROM economy_users WHERE user_id = %s",
                    (target_id,),
                )
                target_row = cur.fetchone()
            con.commit()
            target_balance = _clamp_points(target_row[0]) if target_row else 0
            winner_balance = _clamp_points(winner_row[0]) if winner_row else award
            return target_balance, winner_balance
    except psycopg2.Error as exc:
        log.exception(
            "payout_from_target failed (target=%s winner=%s)", target_id, winner_id
        )
        raise EconomyError("Could not complete bounty payout") from exc


def get_points(user_id: int) -> int:
    try:
        with _pg_connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT points FROM economy_users WHERE user_id = %s", (user_id,)
                )
                row = cur.fetchone()
                return _clamp_points(row[0]) if row else 0
    except psycopg2.Error as exc:
        log.exception("get_points failed for user %s", user_id)
        raise EconomyError("Could not read point balance") from exc


def get_leaderboard(limit: int = 10) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    try:
        with _pg_connect() as con:
            with con.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT user_id, points FROM economy_users ORDER BY points DESC LIMIT %s",
                    (limit,),
                )
                rows = cur.fetchall()
                for row in rows:
                    row["points"] = _clamp_points(row["points"])
                return rows
    except psycopg2.Error as exc:
        log.exception("get_leaderboard failed")
        raise EconomyError("Could not load leaderboard") from exc


def get_top_user() -> tuple[int, int] | None:
    """Return (user_id, points) of the user with the highest balance, or None."""
    try:
        with _pg_connect() as con:
            with con.cursor() as cur:
                cur.execute(
                    "SELECT user_id, points FROM economy_users ORDER BY points DESC LIMIT 1"
                )
                row = cur.fetchone()
                return (int(row[0]), _clamp_points(row[1])) if row else None
    except psycopg2.Error as exc:
        log.exception("get_top_user failed")
        raise EconomyError("Could not read top user") from exc


class _LockRegistry:
    """Lazy asyncio.Lock pool keyed by int (channel/user id)."""

    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}

    def get(self, key: int) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock


# ──────────────────────── embed builders ──────────────────────────

def _base_embed(colour: int) -> discord.Embed:
    return discord.Embed(colour=colour)


def embed_trivia(question: str, payout: int) -> discord.Embed:
    e = _base_embed(C_TRIVIA)
    e.description = (
        f"```ansi\n\u001b[1;34m  ◈  TRIVIA DROP  ◈\u001b[0m\n```"
        f"{SEP}\n"
        f"**{question}**\n"
        f"{SEP}\n"
        f"⚡ **First correct answer wins `{payout} pts`**\n"
        f"⏳ `{DROP_TIMEOUT}s` to answer — type it below"
    )
    e.set_footer(text="SOLACE EVENT  •  Trivia Event")
    return e


def embed_scramble(scrambled: str, payout: int) -> discord.Embed:
    e = _base_embed(C_SCRAMBLE)
    e.description = (
        f"```ansi\n\u001b[1;35m  ◈  WORD SCRAMBLE  ◈\u001b[0m\n```"
        f"{SEP}\n"
        f"**Unscramble this word:**\n"
        f"```\n{scrambled.upper()}\n```"
        f"{SEP}\n"
        f"⚡ **First correct answer wins `{payout} pts`**\n"
        f"⏳ `{DROP_TIMEOUT}s` remaining — type it below"
    )
    e.set_footer(text="SOLACE EVENT  •  Scramble Event")
    return e


def embed_lootbox(payout_range: str) -> discord.Embed:
    e = _base_embed(C_LOOTBOX)
    e.description = (
        f"```ansi\n\u001b[1;32m  ◈  SUPPLY DROP  ◈\u001b[0m\n```"
        f"{SEP}\n"
        f"**A mystery crate has landed in the server.**\n\n"
        f"💰 Reward: `{payout_range} pts`  *(random)*\n"
        f"⏳ `{DROP_TIMEOUT}s` before it disappears\n"
        f"{SEP}\n"
        f"*Click the button below — first grab wins.*"
    )
    e.set_footer(text="SOLACE EVENT  •  Supply Drop")
    return e


def embed_boss(hp: int, max_hp: int, attackers: dict[int, int]) -> discord.Embed:
    e = _base_embed(C_BOSS)
    bar_filled = int((hp / max_hp) * 20)
    bar = "█" * bar_filled + "░" * (20 - bar_filled)
    e.description = (
        f"```ansi\n\u001b[1;31m  ⚔  CO-OP BOSS RAID  ⚔\u001b[0m\n```"
        f"{SEP}\n"
        f"**☠ Shadow Colossus has appeared!**\n\n"
        f"❤️ HP: `{hp}/{max_hp}`\n"
        f"```\n[{bar}]\n```"
        f"{SEP}\n"
        f"Type **`!attack`** to deal damage (1–5 HP)!\n"
        f"⏳ `{DROP_TIMEOUT}s` to slay the beast\n"
        f"👥 Raiders: `{len(attackers)}`"
    )
    e.set_footer(text="SOLACE EVENT  •  Co-Op Boss Raid")
    return e


def embed_boss_dead(participants: dict[int, int], payout_each: int) -> discord.Embed:
    e = _base_embed(C_WIN)
    e.description = (
        f"```ansi\n\u001b[1;33m  ✔  BOSS DEFEATED  \u001b[0m\n```"
        f"{SEP}\n"
        f"**The Shadow Colossus has fallen!** 🎉\n\n"
        f"⚔️ Raiders: `{len(participants)}`\n"
        f"💰 Reward per raider: **`{payout_each} pts`**\n"
        f"{SEP}"
    )
    e.set_footer(text="SOLACE EVENT  •  Boss Raid Victory")
    return e


def embed_worldboss(name: str, hp: int, max_hp: int, attackers: dict[int, int], mins: int, pool: int = WORLDBOSS_POOL) -> discord.Embed:
    e = _base_embed(C_BOSS)
    bar_filled = int((hp / max_hp) * 20) if max_hp else 0
    bar = "\u2588" * bar_filled + "\u2591" * (20 - bar_filled)
    pct = (hp / max_hp * 100) if max_hp else 0
    e.description = (
        f"```ansi\n\u001b[1;31m  \u2620  WORLD BOSS RAID  \u2620\u001b[0m\n```"
        f"{SEP}\n"
        f"**\U0001f30b {name} threatens the server!**\n\n"
        f"\u2764\ufe0f HP: `{hp:,}/{max_hp:,}`  ({pct:.0f}%)\n"
        f"```\n[{bar}]\n```"
        f"{SEP}\n"
        f"Type **`!attack`** to strike! \U0001f4a5\n"
        f"\u2696\ufe0f *Lower-ranked raiders hit HARDER (up to {UNDERDOG_LOW_MULT}x).*\n"
        f"\U0001f4b0 Pool: **`{pool:,} pts`** \u2014 split by damage dealt\n"
        f"\u23f3 Lasts `{mins}` min  \u2022  \U0001f465 Raiders: `{len(attackers)}`"
    )
    e.set_footer(text="SOLACE EVENT  \u2022  World Boss Raid")
    return e


def embed_worldboss_dead(name: str, participants: dict[int, int], pool: int, defeated: bool) -> discord.Embed:
    e = _base_embed(C_WIN if defeated else C_BOSS)
    total_dmg = sum(participants.values()) or 1
    title = "\u2714  WORLD BOSS DEFEATED" if defeated else "\u231b  WORLD BOSS RETREATED"
    headline = (
        f"**{name} has been slain!** \U0001f389" if defeated
        else f"**{name} retreated \u2014 but you wounded it!** \u2694\ufe0f"
    )
    ranked = sorted(participants.items(), key=lambda kv: kv[1], reverse=True)[:5]
    lines = []
    for i, (uid, dmg) in enumerate(ranked, 1):
        share = int(pool * dmg / total_dmg)
        medal = {1: "\U0001f947", 2: "\U0001f948", 3: "\U0001f949"}.get(i, "\U0001f539")
        lines.append(f"{medal} <@{uid}> \u2014 `{dmg}` dmg \u2192 **`{share:,} pts`**")
    board = "\n".join(lines) if lines else "*No raiders joined the fight.*"
    e.description = (
        f"```ansi\n\u001b[1;33m  {title}  \u001b[0m\n```"
        f"{SEP}\n"
        f"{headline}\n\n"
        f"\u2694\ufe0f Raiders: `{len(participants)}`  \u2022  \U0001f4a5 Total damage: `{total_dmg:,}`\n"
        f"\U0001f4b0 Pool split by damage dealt:\n\n"
        f"{board}\n"
        f"{SEP}"
    )
    e.set_footer(text="SOLACE EVENT  \u2022  World Boss Payout")
    return e


def embed_hotcold(clue: str = "") -> discord.Embed:
    e = _base_embed(C_HOT_COLD)
    e.description = (
        f"```ansi\n\u001b[1;34m  🎯  HOT OR COLD  🎯\u001b[0m\n```"
        f"{SEP}\n"
        f"**I'm thinking of a number between 1 and 100.**\n\n"
        f"Type your guess in chat!\n"
        + (f"*Last hint: {clue}*\n" if clue else "")
        + f"{SEP}\n"
        f"⏳ `{DROP_TIMEOUT}s` to guess the number"
    )
    e.set_footer(text="SOLACE EVENT  •  Number Guessing")
    return e


def embed_emoji_puzzle(emojis: str, payout: int) -> discord.Embed:
    e = _base_embed(C_EMOJI)
    e.description = (
        f"```ansi\n\u001b[1;33m  🧩  EMOJI PUZZLE  🧩\u001b[0m\n```"
        f"{SEP}\n"
        f"**What does this represent?**\n\n"
        f"# {emojis}\n\n"
        f"{SEP}\n"
        f"⚡ First correct text answer wins **`{payout} pts`**\n"
        f"⏳ `{DROP_TIMEOUT}s` to solve it"
    )
    e.set_footer(text="SOLACE EVENT  •  Emoji Puzzle")
    return e


def embed_bomb_waiting() -> discord.Embed:
    e = _base_embed(C_BOMB)
    e.description = (
        f"```ansi\n\u001b[1;31m  💣  REACTION TIME BOMB  💣\u001b[0m\n```"
        f"{SEP}\n"
        f"**The fuse is lit...**\n\n"
        f"🕰️ Wait for my signal to **DEFUSE** it!\n"
        f"⚠️ *Click BEFORE the signal = -50 pts penalty!*\n"
        f"{SEP}"
    )
    e.set_footer(text="SOLACE EVENT  •  Stay patient...")
    return e


def embed_bomb_active(payout: int) -> discord.Embed:
    e = _base_embed(C_BOMB)
    e.description = (
        f"```ansi\n\u001b[1;31m  💥  DEFUSE NOW!  💥\u001b[0m\n```"
        f"{SEP}\n"
        f"**CLICK THE BUTTON — RIGHT NOW!**\n\n"
        f"💰 First to defuse wins **`{payout} pts`**!\n"
        f"{SEP}"
    )
    e.set_footer(text="SOLACE EVENT  •  Time Bomb — DEFUSE!")
    return e


def embed_multiplier(duration_mins: int) -> discord.Embed:
    e = _base_embed(C_MULTI)
    e.description = (
        f"```ansi\n\u001b[1;36m  ✨  DOUBLE POINTS ACTIVE  ✨\u001b[0m\n```"
        f"{SEP}\n"
        f"**🚀 Server-wide Double Points Event!**\n\n"
        f"All drop payouts are doubled for the next **{duration_mins} minutes**!\n"
        f"{SEP}\n"
        f"*Keep chatting to trigger more drops!*"
    )
    e.set_footer(text="SOLACE EVENT  •  Multiplier Event")
    return e


def embed_bounty(target_name: str, bounty_amount: int, puzzle_emojis: str) -> discord.Embed:
    e = _base_embed(C_BOUNTY)
    e.description = (
        f"```ansi\n\u001b[1;31m  🎯  POINT BOUNTY  🎯\u001b[0m\n```"
        f"{SEP}\n"
        f"**A bounty has been placed on `{target_name}`!**\n\n"
        f"💰 Solve this puzzle to claim **`{bounty_amount} pts`** from their vault:\n\n"
        f"# {puzzle_emojis}\n\n"
        f"{SEP}\n"
        f"⏳ `{DROP_TIMEOUT}s` — first solver wins the bounty!"
    )
    e.set_footer(text="SOLACE EVENT  •  Bounty Hunt")
    return e


def embed_heist(target_name: str, steal: int, a: int, b: int) -> discord.Embed:
    e = _base_embed(C_BOUNTY)
    e.description = (
        f"```ansi\n\u001b[1;31m  \U0001f4b0  VAULT HEIST  \U0001f4b0\u001b[0m\n```"
        f"{SEP}\n"
        f"**`{target_name}`'s vault is exposed!**\n\n"
        f"\U0001f513 Crack the lock to steal **`{steal} pts`** from them:\n\n"
        f"# `{a} + {b} = ?`\n\n"
        f"{SEP}\n"
        f"\u23f3 `{DROP_TIMEOUT}s` \u2014 first to crack it robs the vault!"
    )
    e.set_footer(text="SOLACE EVENT  \u2022  Heist")
    return e


def embed_losers(question: str, payout: int) -> discord.Embed:
    e = _base_embed(C_TF)
    e.description = (
        f"```ansi\n\u001b[1;32m  \u267b  LOSERS BRACKET  \u267b\u001b[0m\n```"
        f"{SEP}\n"
        f"\U0001f6ab **Top {UNDERDOG_LOCK_N} are locked out \u2014 underdogs only!**\n\n"
        f"**{question}**\n"
        f"{SEP}\n"
        f"\u26a1 **First eligible correct answer wins `{payout} pts`**\n"
        f"\u23f3 `{DROP_TIMEOUT}s` to answer \u2014 type it below"
    )
    e.set_footer(text="SOLACE EVENT  \u2022  Losers Bracket")
    return e


def embed_win_text(user: discord.Member | discord.User, payout: int, new_total: int, drop_type: str) -> discord.Embed:
    colour = {
        "trivia":    C_TRIVIA,    "scramble": C_SCRAMBLE,
        "emoji":     C_EMOJI,     "hotcold":  C_HOT_COLD,
        "bounty":    C_BOUNTY,    "fillblank": C_FILL,
        "fastmath":  C_MATH,
    }.get(drop_type, C_WIN)
    e = _base_embed(colour)
    e.description = (
        f"```ansi\n\u001b[1;33m  ✔  CORRECT  \u001b[0m\n```"
        f"{SEP}\n"
        f"{user.mention} answered correctly\n\n"
        f"**＋{payout} pts** added  ›  Balance: `{new_total} pts`\n"
        f"{SEP}"
    )
    e.set_thumbnail(url=user.display_avatar.url)
    e.set_footer(text="SOLACE EVENT")
    return e


def embed_win_lootbox(user: discord.Member | discord.User, payout: int, new_total: int) -> discord.Embed:
    e = _base_embed(C_WIN)
    e.description = (
        f"```ansi\n\u001b[1;33m  ✔  LOOT CLAIMED  \u001b[0m\n```"
        f"{SEP}\n"
        f"{user.mention} intercepted the crate\n\n"
        f"**＋{payout} pts** added  ›  Balance: `{new_total} pts`\n"
        f"{SEP}"
    )
    e.set_thumbnail(url=user.display_avatar.url)
    e.set_footer(text="SOLACE EVENT  •  Supply Drop")
    return e


def embed_timeout() -> discord.Embed:
    e = _base_embed(C_TIMEOUT)
    e.description = (
        f"```ansi\n\u001b[1;30m  ✖  TIME EXPIRED  \u001b[0m\n```"
        f"{SEP}\n"
        f"Nobody claimed the drop in time.\n"
        f"*Keep chatting — the next one is coming.*\n"
        f"{SEP}"
    )
    e.set_footer(text="SOLACE EVENT")
    return e


def embed_points(user: discord.Member | discord.User, balance: int) -> discord.Embed:
    e = _base_embed(C_POINTS)
    e.description = (
        f"```ansi\n\u001b[1;33m  ◈  BALANCE  ◈\u001b[0m\n```"
        f"{SEP}\n"
        f"**{user.display_name}**\n"
        f"```\n{balance:,} pts\n```"
        f"{SEP}"
    )
    e.set_thumbnail(url=user.display_avatar.url)
    e.set_footer(text="SOLACE EVENT  •  /leaderboard to see rankings")
    return e


async def embed_leaderboard(guild: discord.Guild, rows: list) -> discord.Embed:
    e = _base_embed(C_BOARD)
    e.description = (
        f"```ansi\n\u001b[1;37m  ◈  LEADERBOARD  ◈\u001b[0m\n```"
        f"{SEP}\n"
    )
    lines: list[str] = []
    for rank, row in enumerate(rows, start=1):
        medal  = MEDALS.get(rank, f"`#{rank:>2}`")
        member = guild.get_member(row["user_id"])
        name   = member.display_name if member else f"User {row['user_id']}"
        lines.append(f"{medal}  **{name}** — `{row['points']:,} pts`")
    e.description += "\n".join(lines) if lines else "*No entries yet.*"
    e.description += f"\n{SEP}"
    e.set_footer(text=f"SOLACE EVENT  •  Top {len(rows)} players")
    return e


def embed_set_confirm(label: str, channel: discord.TextChannel) -> discord.Embed:
    e = _base_embed(C_SET)
    e.description = (
        f"```ansi\n\u001b[1;32m  ✔  CHANNEL SET  \u001b[0m\n```"
        f"{SEP}\n"
        f"**{label}** → {channel.mention}\n"
        f"{SEP}"
    )
    e.set_footer(text="SOLACE EVENT")
    return e


def embed_get_channel(label: str, channel_id: int | None) -> discord.Embed:
    if channel_id:
        e = _base_embed(C_SET)
        e.description = f"{SEP}\n**{label}** is set to <#{channel_id}>\n{SEP}"
    else:
        e = _base_embed(C_TIMEOUT)
        e.description = f"{SEP}\n**{label}** has not been configured yet.\n{SEP}"
    e.set_footer(text="SOLACE EVENT")
    return e


def embed_fillblank(prompt: str, payout: int) -> discord.Embed:
    e = _base_embed(C_FILL)
    e.description = (
        f"```ansi\n\u001b[1;35m  ✏️  FILL IN THE BLANK  ✏️\u001b[0m\n```"
        f"{SEP}\n"
        f"**{prompt}**\n\n"
        f"{SEP}\n"
        f"⚡ First correct answer wins **`{payout} pts`**\n"
        f"⏳ `{DROP_TIMEOUT}s` — type your answer below"
    )
    e.set_footer(text="SOLACE EVENT  •  Fill in the Blank")
    return e


def embed_fastmath(prompt: str, payout: int) -> discord.Embed:
    e = _base_embed(C_MATH)
    e.description = (
        f"```ansi\n\u001b[1;36m  🔢  FAST MATH  🔢\u001b[0m\n```"
        f"{SEP}\n"
        f"**{prompt}**\n\n"
        f"{SEP}\n"
        f"⚡ First correct answer wins **`{payout} pts`**\n"
        f"⏳ `{DROP_TIMEOUT}s` — type the number below"
    )
    e.set_footer(text="SOLACE EVENT  •  Fast Math")
    return e


def embed_truefalse(statement: str, payout: int) -> discord.Embed:
    e = _base_embed(C_TF)
    e.description = (
        f"```ansi\n\u001b[1;32m  🟢  TRUE OR FALSE  🟢\u001b[0m\n```"
        f"{SEP}\n"
        f"**{statement}**\n\n"
        f"{SEP}\n"
        f"⚡ First correct click wins **`{payout} pts`**\n"
        f"⏳ `{DROP_TIMEOUT}s` — click a button below"
    )
    e.set_footer(text="SOLACE EVENT  •  True or False")
    return e


def embed_truefalse_result(winner: discord.Member | discord.User | None,
                           statement: str, correct: bool, fact: str,
                           payout: int, new_total: int) -> discord.Embed:
    e = _base_embed(C_WIN if winner else C_TIMEOUT)
    answer_str = "✅ TRUE" if correct else "❌ FALSE"
    if winner:
        e.description = (
            f"```ansi\n\u001b[1;33m  ✔  CORRECT  \u001b[0m\n```"
            f"{SEP}\n"
            f"{winner.mention} got it right!\n\n"
            f"The answer was **{answer_str}**\n"
            f"*{fact}*\n\n"
            f"**＋{payout} pts** added  ›  Balance: `{new_total} pts`\n"
            f"{SEP}"
        )
        if hasattr(winner, 'display_avatar'):
            e.set_thumbnail(url=winner.display_avatar.url)
    else:
        e.description = (
            f"```ansi\n\u001b[1;30m  ✖  TIME EXPIRED  \u001b[0m\n```"
            f"{SEP}\n"
            f"Nobody answered in time!\n\n"
            f"The answer was **{answer_str}**\n"
            f"*{fact}*\n"
            f"{SEP}"
        )
    e.set_footer(text="SOLACE EVENT  •  True or False")
    return e


# ──────────────────────────── utilities ───────────────────────────

def scramble_word(word: str) -> str:
    letters = list(word)
    for _ in range(100):
        random.shuffle(letters)
        if "".join(letters) != word:
            return "".join(letters)
    letters[0], letters[1] = letters[1], letters[0]
    return "".join(letters)


# ─────────── Blackjack helpers ────────────────────────────────────

CARD_SUITS  = ["♠", "♥", "♦", "♣"]
CARD_RANKS  = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]

def _new_deck() -> list[str]:
    """Return a freshly shuffled single-deck list of card strings."""
    deck = [f"{r}{s}" for s in CARD_SUITS for r in CARD_RANKS]
    random.shuffle(deck)
    return deck

def _card_value(rank: str) -> int:
    if rank in ("J", "Q", "K"):
        return 10
    if rank == "A":
        return 11          # Aces start as 11; soft-ace logic applied in hand_value()
    return int(rank)

def _hand_value(cards: list[str]) -> int:
    total = 0
    aces  = 0
    for card in cards:
        rank = card[:-1]   # strip the suit character(s) — handles "10♠" etc.
        if rank == "A":
            aces += 1
        total += _card_value(rank)
    while total > 21 and aces:
        total -= 10
        aces  -= 1
    return total

def _fmt_hand(cards: list[str], hide_second: bool = False) -> str:
    if hide_second and len(cards) > 1:
        return f"`{cards[0]}`  `??`"
    return "  ".join(f"`{c}`" for c in cards)

def _dealer_play(deck: list[str], hand: list[str]) -> list[str]:
    """Dealer draws until 17+."""
    while _hand_value(hand) < 17:
        hand.append(deck.pop())
    return hand

def embed_blackjack(chat_hand: list[str], house_hand: list[str], phase: str, payout: int,
                    clicks: int, needed: int) -> discord.Embed:
    chat_total  = _hand_value(chat_hand)
    house_shown = _hand_value([house_hand[0]])   # only the visible card
    e = _base_embed(C_BLACKJACK)
    hide = phase == "playing"
    status_line = ""
    if phase == "playing":
        status_line = (
            f"\n🗳️ **Community vote:** `{clicks}/{needed}` clicks recorded\n"
            f"*(majority of next {needed} clicks decides — Hit or Stand)*"
        )
    elif phase == "chat_bust":
        status_line = "\n💥 **Chat busted!** House wins."
    elif phase == "house_bust":
        status_line = "\n🎉 **House busted!** Chat wins!"
    elif phase == "chat_win":
        status_line = "\n🏆 **Chat wins!**"
    elif phase == "house_win":
        status_line = "\n🃏 **House wins!**"
    elif phase == "push":
        status_line = "\n🤝 **Push — it's a tie!**"

    e.description = (
        f"```ansi\n\u001b[1;32m  ♠  BLACKJACK DUEL  ♠\u001b[0m\n```"
        f"{SEP}\n"
        f"🏠 **The House** — {_fmt_hand(house_hand, hide_second=hide)}"
        f"  *(showing {house_shown})*\n\n"
        f"💬 **The Chat** — {_fmt_hand(chat_hand)}"
        f"  *(total {chat_total})*\n"
        f"{status_line}\n"
        f"{SEP}\n"
        f"💰 Win pool: **`{payout} pts`** split among participants"
    )
    e.set_footer(text="SOLACE EVENT  •  Blackjack Duel")
    return e


def embed_potato(holder: discord.Member | discord.User, seconds_left: int) -> discord.Embed:
    e = _base_embed(C_POTATO)
    e.description = (
        f"```ansi\n\u001b[1;33m  🥔  HOT POTATO  🥔\u001b[0m\n```"
        f"{SEP}\n"
        f"🔥 **{holder.mention}** is holding the Hot Potato!\n\n"
        f"Type **`!pass @username`** within `{seconds_left}s` to pass it!\n"
        f"⚠️ *When the timer hits 0 — BOOM or GOLD.*\n"
        f"{SEP}"
    )
    e.set_footer(text="SOLACE EVENT  •  Hot Potato")
    return e


def embed_potato_explode_bomb(holder: discord.Member | discord.User) -> discord.Embed:
    e = _base_embed(0xFF0000)
    e.description = (
        f"```ansi\n\u001b[1;31m  💣  BOOM!  💣\u001b[0m\n```"
        f"{SEP}\n"
        f"💥 The potato **EXPLODED** on {holder.mention}!\n\n"
        f"**-200 pts** deducted from their balance.\n"
        f"{SEP}"
    )
    e.set_footer(text="SOLACE EVENT  •  Hot Potato — Bomb!")
    return e


def embed_potato_explode_gold(holder: discord.Member | discord.User) -> discord.Embed:
    e = _base_embed(C_WIN)
    e.description = (
        f"```ansi\n\u001b[1;33m  🎁  GOLDEN LOOT SACK!  🎁\u001b[0m\n```"
        f"{SEP}\n"
        f"🏆 The potato turned into a **Golden Loot Sack** for {holder.mention}!\n\n"
        f"**+500 pts** added to their balance!\n"
        f"{SEP}"
    )
    e.set_footer(text="SOLACE EVENT  •  Hot Potato — Golden Sack!")
    return e


# ─────────────────────────── UI Views ────────────���────────��───────

class BlackjackView(discord.ui.View):
    """
    Community Blackjack Duel.
    Collects the first VOTE_THRESHOLD total button clicks from any users
    in the channel to decide Hit vs Stand, then resolves dealer logic.
    """
    VOTE_THRESHOLD = 3   # clicks needed to lock in a decision

    def __init__(self, cog: "ServerDropsEconomy", channel_id: int,
                 deck: list[str], chat_hand: list[str], house_hand: list[str],
                 payout: int, msg: discord.Message):
        super().__init__(timeout=None)
        self.cog         = cog
        self.channel_id  = channel_id
        self.deck        = deck
        self.chat_hand   = chat_hand
        self.house_hand  = house_hand
        self.payout      = payout
        self.msg         = msg
        self.resolved    = False
        self.participants: set[int] = set()   # user_ids who clicked anything
        self._hit_clicks  = 0
        self._stand_clicks = 0
        self._resolve_lock = asyncio.Lock()

    def _total_clicks(self) -> int:
        return self._hit_clicks + self._stand_clicks

    async def _record_click(self, interaction: discord.Interaction, action: str):
        async with self._resolve_lock:
            if self.resolved:
                await interaction.response.send_message("This round has already ended.", ephemeral=True)
                return

            self.participants.add(interaction.user.id)
            if action == "hit":
                self._hit_clicks += 1
            else:
                self._stand_clicks += 1

            total = self._total_clicks()
            needed = self.VOTE_THRESHOLD

            await interaction.response.send_message(
                f"🗳️ Your **{'Hit' if action == 'hit' else 'Stand'}** vote registered! "
                f"(`{total}/{needed}` clicks so far)",
                ephemeral=True,
            )

            if total >= needed:
                await self._resolve()

    async def _resolve(self):
        if self.resolved:
            return
        self.resolved = True

        # Disable buttons immediately
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        try:
            await self.msg.edit(view=self)
        except discord.HTTPException as exc:
            log.debug("Blackjack view edit failed: %s", exc)

        channel = self.cog.bot.get_channel(self.channel_id)
        if channel is None:
            return

        drop = self.cog.active_drops.get(self.channel_id)
        if drop is not None:
            await self.cog._cancel_drop_task(drop)
            async with self.cog._channel_locks.get(self.channel_id):
                if self.cog.active_drops.get(self.channel_id) is drop:
                    del self.cog.active_drops[self.channel_id]

        # Majority rules
        action = "hit" if self._hit_clicks >= self._stand_clicks else "stand"

        if action == "hit":
            self.chat_hand.append(self.deck.pop())
            chat_val = _hand_value(self.chat_hand)

            if chat_val > 21:
                # Chat busted
                phase = "chat_bust"
                await self.msg.edit(embed=embed_blackjack(
                    self.chat_hand, self.house_hand, phase, self.payout,
                    self._total_clicks(), self.VOTE_THRESHOLD))
                await channel.send("💥 The Chat **busted**! Better luck next time.")
                self.stop()
                return

            # After a hit that didn't bust, automatically stand (simplified flow)
            # This avoids infinite vote loops; a new round of votes could be added here.

        # Stand / post-hit �� dealer plays out
        self.house_hand = _dealer_play(self.deck, self.house_hand)
        chat_val  = _hand_value(self.chat_hand)
        house_val = _hand_value(self.house_hand)

        if house_val > 21:
            phase = "house_bust"
            outcome = "win"
        elif chat_val > house_val:
            phase = "chat_win"
            outcome = "win"
        elif chat_val < house_val:
            phase = "house_win"
            outcome = "lose"
        else:
            phase = "push"
            outcome = "push"

        await self.msg.edit(embed=embed_blackjack(
            self.chat_hand, self.house_hand, phase, self.payout,
            self._total_clicks(), self.VOTE_THRESHOLD))

        async with self.cog._channel_locks.get(self.channel_id):
            self.cog.active_drops.pop(self.channel_id, None)

        if outcome == "win" and self.participants:
            share = max(1, int(self.payout) // len(self.participants))
            lines = []
            for uid in self.participants:
                try:
                    new_total = self.cog._award_win(uid, share)
                except EconomyError:
                    log.exception("Blackjack payout failed for user %s", uid)
                    continue
                member = channel.guild.get_member(uid)  # type: ignore[union-attr]
                name   = member.mention if member else f"<@{uid}>"
                lines.append(f"{name} **+{share} pts** → `{new_total} pts`")
            if lines:
                await channel.send(
                    f"🏆 **Chat wins the Blackjack Duel!**\n"
                    f"The `{self.payout} pt` pool is split `{share} pts` each:\n"
                    + "\n".join(lines)
                )
        elif outcome == "push":
            await channel.send("🤝 **Push — nobody wins or loses this round.**")
        else:
            await channel.send("🃏 **House wins!** The chat couldn't beat the dealer.")

        self.stop()

    @discord.ui.button(label="  🟢 Hit  ", style=discord.ButtonStyle.success)
    async def btn_hit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._record_click(interaction, "hit")

    @discord.ui.button(label="  🔴 Stand  ", style=discord.ButtonStyle.danger)
    async def btn_stand(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._record_click(interaction, "stand")


class LootboxView(discord.ui.View):
    def __init__(self, cog: "ServerDropsEconomy", channel_id: int):
        super().__init__(timeout=None)
        self.cog        = cog
        self.channel_id = channel_id
        self.claimed    = False
        self._claim_lock = asyncio.Lock()

    @discord.ui.button(label="  GRAB LOOT  ", style=discord.ButtonStyle.success, emoji="📦")
    async def grab_loot(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self._claim_lock:
            if self.claimed:
                await interaction.response.send_message("This crate has already been claimed.", ephemeral=True)
                return

            drop = self.cog.active_drops.get(self.channel_id)
            if drop is None or drop.get("type") != "lootbox":
                await interaction.response.send_message("This drop has expired.", ephemeral=True)
                return

            if not await self.cog._mark_drop_resolved(self.channel_id, drop):
                await interaction.response.send_message("This crate has already been claimed.", ephemeral=True)
                return

            self.claimed = True
            await self.cog._cancel_drop_task(drop)

            payout = max(0, int(drop["payout"]))
            try:
                new_total = self.cog._award_win(interaction.user.id, payout)
            except EconomyError:
                self.claimed = False
                await self.cog._rollback_drop_resolution(interaction.channel, drop)  # type: ignore[arg-type]
                await interaction.response.send_message(
                    "⚠️ Could not save your points — the crate is still up for grabs.",
                    ephemeral=True,
                )
                return

            if not await self.cog._finish_drop(self.channel_id, drop):
                await interaction.response.send_message("This crate has already been claimed.", ephemeral=True)
                return

            button.disabled = True
            button.label    = f"  Claimed by {interaction.user.display_name}  "
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(embed=embed_win_lootbox(interaction.user, payout, new_total))
            self.stop()


class BombView(discord.ui.View):
    """
    Two-phase time bomb.
    Phase 1 (armed=False): clicking deducts 50 pts (too early).
    Phase 2 (armed=True):  first click wins the payout.
    """
    def __init__(self, cog: "ServerDropsEconomy", channel_id: int, payout: int):
        super().__init__(timeout=None)
        self.cog        = cog
        self.channel_id = channel_id
        self.payout     = max(0, int(payout))
        self.armed      = False
        self.defused    = False
        self._claim_lock = asyncio.Lock()

    @discord.ui.button(label="  💥 DEFUSE  ", style=discord.ButtonStyle.danger)
    async def defuse(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with self._claim_lock:
            if self.defused:
                await interaction.response.send_message("Already defused!", ephemeral=True)
                return

            if not self.armed:
                async with self.cog._user_locks.get(interaction.user.id):
                    new_total = await self.cog._deduct_safe(
                        interaction.channel, interaction.user.id, 50
                    )
                if new_total is None:
                    if not interaction.response.is_done():
                        await interaction.response.send_message(
                            "⚠️ Could not update your balance.", ephemeral=True
                        )
                    return
                await interaction.response.send_message(
                    f"⚠️ Too early! You lost **50 pts**. Balance: `{new_total} pts`",
                    ephemeral=True,
                )
                return

            drop = self.cog.active_drops.get(self.channel_id)
            if drop is None or drop.get("type") != "bomb":
                await interaction.response.send_message("This drop has expired.", ephemeral=True)
                return

            if not await self.cog._mark_drop_resolved(self.channel_id, drop):
                await interaction.response.send_message("Already defused!", ephemeral=True)
                return

            self.defused = True
            await self.cog._cancel_drop_task(drop)

            try:
                new_total = self.cog._award_win(interaction.user.id, self.payout)
            except EconomyError:
                self.defused = False
                await self.cog._rollback_drop_resolution(interaction.channel, drop)  # type: ignore[arg-type]
                await interaction.response.send_message(
                    "⚠️ Could not save your points — the bomb is still live.",
                    ephemeral=True,
                )
                return

            if not await self.cog._finish_drop(self.channel_id, drop):
                await interaction.response.send_message("Already defused!", ephemeral=True)
                return

            button.disabled = True
            button.label    = f"  Defused by {interaction.user.display_name}  "
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(
                f"💥 **{interaction.user.mention}** defused the bomb and earned **`{self.payout} pts`**! "
                f"Balance: `{new_total} pts`"
            )
            self.stop()


class TrueFalseView(discord.ui.View):
    """Button-based True/False drop. First correct click wins."""

    def __init__(self, cog: "ServerDropsEconomy", channel_id: int,
                 correct: bool, fact: str, statement: str, payout: int):
        super().__init__(timeout=None)
        self.cog        = cog
        self.channel_id = channel_id
        self.correct    = correct
        self.fact       = fact
        self.statement  = statement
        self.payout     = max(0, int(payout))
        self.resolved   = False
        self._claim_lock = asyncio.Lock()

    async def _attempt(self, interaction: discord.Interaction, chosen: bool):
        async with self._claim_lock:
            if self.resolved:
                await interaction.response.send_message("This round has already ended.", ephemeral=True)
                return

            drop = self.cog.active_drops.get(self.channel_id)
            if drop is None or drop.get("type") != "truefalse":
                await interaction.response.send_message("This drop has expired.", ephemeral=True)
                return

            if chosen != self.correct:
                await interaction.response.send_message(
                    "❌ Wrong! Keep trying — someone else might still win.", ephemeral=True
                )
                return

            if not await self.cog._mark_drop_resolved(self.channel_id, drop):
                await interaction.response.send_message("This round has already ended.", ephemeral=True)
                return

            self.resolved = True
            await self.cog._cancel_drop_task(drop)

            try:
                new_total = self.cog._award_win(interaction.user.id, self.payout)
            except EconomyError:
                self.resolved = False
                await self.cog._rollback_drop_resolution(interaction.channel, drop)  # type: ignore[arg-type]
                await interaction.response.send_message(
                    "⚠️ Could not save your points — keep trying!",
                    ephemeral=True,
                )
                return

            if not await self.cog._finish_drop(self.channel_id, drop):
                await interaction.response.send_message("This round has already ended.", ephemeral=True)
                return

            for child in self.children:
                child.disabled = True  # type: ignore[attr-defined]
            try:
                await interaction.response.edit_message(view=self)
            except discord.HTTPException as exc:
                log.debug("True/False view edit failed: %s", exc)
                if not interaction.response.is_done():
                    await interaction.response.defer()

            await interaction.followup.send(
                embed=embed_truefalse_result(
                    interaction.user, self.statement, self.correct, self.fact,
                    self.payout, new_total,
                )
            )
            self.stop()

    @discord.ui.button(label="  ✅  TRUE  ", style=discord.ButtonStyle.success)
    async def btn_true(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._attempt(interaction, True)

    @discord.ui.button(label="  ❌  FALSE  ", style=discord.ButtonStyle.danger)
    async def btn_false(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._attempt(interaction, False)


# ──────────────────────────── the Cog ─────────────────────────────

class ServerDropsEconomy(commands.Cog, name="ServerDropsEconomy"):
    BOSS_ATTACK_COOLDOWN = 3.0

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_db()
        self.msg_counters:  dict[int, int]  = {}  # channel_id → message count
        self.active_drops:  dict[int, dict] = {}  # channel_id → drop state
        self.double_points: bool            = False  # global multiplier flag
        self.double_until:  float           = 0.0    # epoch time when multiplier expires
        self.underdog_enabled: bool         = True   # Underdog catch-up multiplier (on by default)
        self.happy_until:   float           = 0.0    # epoch time when Happy Hour ends
        # Boss attack cooldowns: {channel_id: {user_id: last_attack_epoch}}
        self._boss_cooldowns: dict[int, dict[int, float]] = {}
        self._channel_locks = _LockRegistry()
        self._user_locks = _LockRegistry()
        # Shuffled content pools — recently drawn items are held back
        self._pool_trivia = TriviaPool(TRIVIA_BANK, recent_size=20)
        self._pool_scramble = RotatingPool(SCRAMBLE_WORDS, recent_size=10)
        self._pool_emoji = RotatingPool(EMOJI_PUZZLES, recent_size=10)
        self._pool_fillblank = RotatingPool(FILL_BLANK_BANK, recent_size=10)
        self._pool_math = RotatingPool(MATH_BANK, recent_size=10)
        self._pool_truefalse = RotatingPool(TRUE_FALSE_BANK, recent_size=10)

    # ──────────────────── helpers ──────────────────────

    def _effective_payout(self, base: int) -> int:
        base = max(0, int(base))
        if self.double_points and time.time() < self.double_until:
            return base * 2
        if self.double_points:
            self.double_points = False  # expired
        return base

    def _underdog_mult(self, user_id: int) -> float:
        """Tiered catch-up multiplier by leaderboard rank.

        Top 3 -> 1.0x (no bonus), ranks 4-7 -> 1.5x, rank 8+ / unranked -> 1.7x.
        """
        if not self.underdog_enabled:
            return 1.0
        try:
            top = get_leaderboard(UNDERDOG_MID_MAX)  # fetch top 7
        except EconomyError:
            log.warning("Leaderboard unavailable for underdog calc; using 1.0x")
            return 1.0
        if len(top) < 4:
            return 1.0  # too few ranked players for tiers to matter yet
        ids = [r["user_id"] for r in top]
        if user_id in ids[:UNDERDOG_LOCK_N]:        # ranks 1-3 (podium)
            return 1.0
        if user_id in ids[UNDERDOG_LOCK_N:]:        # ranks 4-7
            return UNDERDOG_MID_MULT
        return UNDERDOG_LOW_MULT                     # rank 8 and below / unranked

    def _is_underdog(self, user_id: int) -> bool:
        """True if the user qualifies for any catch-up bonus (outside the Top 3)."""
        return self._underdog_mult(user_id) > 1.0

    def _compute_award_amount(self, user_id: int, base: int) -> int:
        """Integer payout after underdog multiplier."""
        base = max(0, int(base))
        mult = self._underdog_mult(user_id)
        if mult > 1.0:
            return max(0, int(round(base * mult)))
        return base

    def _award_win(self, user_id: int, base: int) -> int:
        """Award drop winnings, applying the tiered Underdog catch-up bonus."""
        amount = self._compute_award_amount(user_id, base)
        return add_points(user_id, amount)

    async def _award_win_safe(
        self,
        channel: discord.abc.Messageable,
        user_id: int,
        base: int,
    ) -> int | None:
        """Award points; notify channel if the database write fails."""
        try:
            return self._award_win(user_id, base)
        except EconomyError:
            await channel.send(
                "⚠️ Could not save your points right now — try again in a moment."
            )
            return None

    async def _deduct_safe(
        self,
        channel: discord.abc.Messageable,
        user_id: int,
        amount: int,
    ) -> int | None:
        try:
            return deduct_points(user_id, amount)
        except EconomyError:
            await channel.send(
                "⚠️ Could not update your balance right now — try again in a moment."
            )
            return None

    async def _cancel_drop_task(self, drop: dict) -> None:
        task: asyncio.Task | None = drop.get("task")
        if task and not task.done():
            task.cancel()

    async def _rollback_drop_resolution(
        self,
        channel: discord.TextChannel,
        drop: dict,
        *,
        restart_timeout: bool = True,
    ) -> None:
        """Undo a failed payout so the drop can continue or time out cleanly."""
        cid = channel.id
        async with self._channel_locks.get(cid):
            current = self.active_drops.get(cid)
            if current is not drop:
                return
            drop["resolved"] = False
            if restart_timeout and drop.get("task") is None:
                drop["task"] = asyncio.create_task(self._drop_timeout(channel))

    async def _mark_drop_resolved(self, channel_id: int, drop: dict) -> bool:
        """Atomically mark a drop resolved. Returns False if already claimed."""
        async with self._channel_locks.get(channel_id):
            current = self.active_drops.get(channel_id)
            if current is not drop or current.get("resolved"):
                return False
            current["resolved"] = True
            return True

    async def _finish_drop(self, channel_id: int, drop: dict) -> bool:
        """Remove an active drop after it has been resolved. Returns False if already gone."""
        async with self._channel_locks.get(channel_id):
            current = self.active_drops.get(channel_id)
            if current is not drop:
                return False
            del self.active_drops[channel_id]
            return True

    def _happy_active(self) -> bool:
        """True while a Mega-Drop Happy Hour window is running."""
        return time.time() < self.happy_until

    def _pick_drop(self) -> str:
        """Pick a drop type, doubling rare drops during Happy Hour."""
        if self._happy_active():
            return random.choice(list(EVENT_POOL) + [d for d in EVENT_POOL if d in RARE_DROPS])
        return random.choice(EVENT_POOL)

    # ──────────────────── message listener ────────────

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

        # Master switch: if drops are paused server-wide, fire nothing.
        if get_drops_paused(message.guild.id):
            return

        # Channel gating: all-channels mode (minus blacklist) or the single configured channel.
        if get_all_channels_mode(message.guild.id):
            if message.channel.id in get_disabled_channels(message.guild.id):
                return
        else:
            drops_id = get_drops_channel(message.guild.id)
            if drops_id is None or message.channel.id != drops_id:
                return

        cid = message.channel.id

        # ── Route to active drop if one is running ──
        if cid in self.active_drops:
            drop = self.active_drops[cid]
            if drop.get("type") == "_booting":
                return
            dtype = drop.get("type")
            if dtype in ("trivia", "scramble", "emoji", "bounty", "fillblank", "fastmath", "heist"):
                await self._check_text_answer(message, drop)
            elif dtype == "hotcold":
                await self._check_number_guess(message, drop)
            elif dtype == "boss":
                await self._handle_boss_attack(message, drop)
            elif dtype == "worldboss":
                await self._handle_worldboss_attack(message, drop)
            elif dtype == "hotpotato":
                await self._handle_potato_pass(message, drop)
            elif dtype == "losers":
                await self._handle_losers_answer(message, drop)
            # truefalse is button-only; no text routing needed
            return

        # ── Increment counter (serialized per channel) ──
        should_fire = False
        async with self._channel_locks.get(cid):
            if cid in self.active_drops:
                return
            self.msg_counters[cid] = self.msg_counters.get(cid, 0) + 1
            trigger = get_drop_trigger(message.guild.id)
            if self._happy_active():
                trigger = max(3, trigger // 2)  # Mega-Drop Happy Hour: drops fire twice as fast
            if self.msg_counters[cid] >= trigger:
                self.msg_counters[cid] = 0
                should_fire = True

        if should_fire:
            await self._trigger_drop(message.channel)

    # ──────────────────── drop triggering ─────────────

    async def _trigger_drop(self, channel: discord.TextChannel):
        cid = channel.id
        async with self._channel_locks.get(cid):
            if cid in self.active_drops:
                return

        # Ping the configured drops role (if set) right before a drop fires.
        if channel.guild is not None:
            ping_role_id = get_ping_role(channel.guild.id)
            if ping_role_id:
                role = channel.guild.get_role(ping_role_id)
                if role is not None:
                    try:
                        await channel.send(
                            f"{role.mention} 🎉 **A drop is landing — get ready!**",
                            allowed_mentions=discord.AllowedMentions(roles=True),
                        )
                    except discord.HTTPException as exc:
                        log.debug("Drop ping failed in channel %s: %s", cid, exc)

        choice = self._pick_drop()
        dispatch = {
            "trivia":     self._start_trivia,
            "scramble":   self._start_scramble,
            "lootbox":    self._start_lootbox,
            "boss":       self._start_boss,
            "hotcold":    self._start_hotcold,
            "emoji":      self._start_emoji,
            "bomb":       self._start_bomb,
            "multi":      self._start_multiplier,
            "blackjack":  self._start_blackjack,
            "hotpotato":  self._start_hotpotato,
            "fillblank":  self._start_fillblank,
            "fastmath":   self._start_fastmath,
            "truefalse":  self._start_truefalse,
            "worldboss":  self._start_worldboss,
            "heist":      self._start_heist,
            "losers":     self._start_losers,
        }
        starter = dispatch.get(choice)
        if starter is None:
            log.error("Unknown drop type selected: %s", choice)
            return

        async with self._channel_locks.get(cid):
            if cid in self.active_drops:
                return
            self.active_drops[cid] = {"type": "_booting", "resolved": False}

        try:
            await starter(channel)
        except Exception:
            log.exception("Failed to start %s drop in channel %s", choice, cid)
            async with self._channel_locks.get(cid):
                drop = self.active_drops.get(cid)
                if drop and drop.get("type") == "_booting":
                    self.active_drops.pop(cid, None)

    # ─────────────── Trivia ───────────────────────────

    async def _start_trivia(self, channel: discord.TextChannel):
        question, answers = self._pool_trivia.draw()
        payout = self._effective_payout(random.randint(50, 150))
        await channel.send(embed=embed_trivia(question, payout))
        state = {"type": "trivia", "answer": [a.lower().strip() for a in answers], "payout": payout, "task": None}
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._drop_timeout(channel))

    # ─────────────── Scramble ─────────────────────────

    async def _start_scramble(self, channel: discord.TextChannel):
        word      = self._pool_scramble.draw()
        scrambled = scramble_word(word)
        payout    = self._effective_payout(random.randint(50, 150))
        await channel.send(embed=embed_scramble(scrambled, payout))
        state = {"type": "scramble", "answer": [word.lower().strip()], "payout": payout, "task": None}
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._drop_timeout(channel))

    # ─────────���───── Lootbox ──────────────────────────

    async def _start_lootbox(self, channel: discord.TextChannel):
        payout = self._effective_payout(random.randint(150, 300))
        view   = LootboxView(cog=self, channel_id=channel.id)
        msg    = await channel.send(embed=embed_lootbox("150–300"), view=view)
        state  = {"type": "lootbox", "payout": payout, "view": view, "msg": msg, "task": None}
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._drop_timeout(channel))

    # ─────────────── Co-Op Boss Raid ──────────────────

    async def _start_boss(self, channel: discord.TextChannel):
        max_hp = 50
        state = {
            "type":         "boss",
            "hp":           max_hp,
            "max_hp":       max_hp,
            "attackers":    {},     # user_id → total damage dealt
            "task":         None,
        }
        self.active_drops[channel.id] = state
        self._boss_cooldowns[channel.id] = {}
        msg = await channel.send(embed=embed_boss(max_hp, max_hp, {}))
        state["msg"] = msg
        state["task"] = asyncio.create_task(self._drop_timeout(channel))

    async def _handle_boss_attack(self, message: discord.Message, drop: dict):
        if _normalize_answer(message.content) != "!attack":
            return

        cid = message.channel.id
        uid = message.author.id

        async with self._channel_locks.get(cid):
            if cid not in self.active_drops or self.active_drops[cid] is not drop:
                return
            if drop.get("resolved"):
                return

            now = time.time()
            cds = self._boss_cooldowns.setdefault(cid, {})
            last_at = cds.get(uid, 0.0)

            if now - last_at < self.BOSS_ATTACK_COOLDOWN:
                remaining = self.BOSS_ATTACK_COOLDOWN - (now - last_at)
                await message.channel.send(
                    f"⏱️ {message.author.mention} cooldown! Wait `{remaining:.1f}s`.",
                    delete_after=2,
                )
                return

            cds[uid] = now
            damage = random.randint(1, 5)
            drop["hp"] = max(0, int(drop["hp"]) - damage)
            drop["attackers"][uid] = drop["attackers"].get(uid, 0) + damage
            defeated = drop["hp"] <= 0
            if defeated:
                drop["resolved"] = True

        try:
            await message.add_reaction("⚔️")
        except discord.HTTPException:
            pass

        try:
            await drop["msg"].edit(
                embed=embed_boss(drop["hp"], drop["max_hp"], drop["attackers"])
            )
        except discord.HTTPException:
            pass

        if not defeated:
            return

        await self._cancel_drop_task(drop)
        async with self._channel_locks.get(cid):
            if self.active_drops.get(cid) is drop:
                del self.active_drops[cid]
            self._boss_cooldowns.pop(cid, None)

        attackers = drop["attackers"]
        total_pool = 1000
        payout_each = max(1, total_pool // max(len(attackers), 1))
        payout_each = self._effective_payout(payout_each)

        await message.channel.send(embed=embed_boss_dead(attackers, payout_each))

        for raider_id in attackers:
            try:
                self._award_win(raider_id, payout_each)
            except EconomyError:
                log.exception("Boss payout failed for user %s", raider_id)

    # \u2500\u2500\u2500\u2500\u2500 World Boss Raid (catch-up event) \u2500\u2500
    async def _handle_worldboss_attack(self, message: discord.Message, drop: dict):
        if _normalize_answer(message.content) != "!attack":
            return
        cid = message.channel.id
        uid = message.author.id
        defeated = False

        async with self._channel_locks.get(cid):
            if cid not in self.active_drops or self.active_drops[cid] is not drop:
                return
            if drop.get("resolved"):
                return

            now = time.time()
            cds = self._boss_cooldowns.setdefault(cid, {})
            last_at = cds.get(uid, 0.0)
            if now - last_at < self.BOSS_ATTACK_COOLDOWN:
                remaining = self.BOSS_ATTACK_COOLDOWN - (now - last_at)
                await message.channel.send(
                    f"\u23f1\ufe0f {message.author.mention} cooldown! Wait `{remaining:.1f}s`.",
                    delete_after=2,
                )
                return
            cds[uid] = now
            base_dmg = random.randint(8, 16)
            boss_mult = self._underdog_mult(uid)
            if boss_mult > 1.0:
                base_dmg = max(1, int(round(base_dmg * boss_mult)))
            drop["hp"] = max(0, int(drop["hp"]) - base_dmg)
            drop["attackers"][uid] = drop["attackers"].get(uid, 0) + base_dmg
            drop["_hits"] = drop.get("_hits", 0) + 1
            defeated = drop["hp"] <= 0
            if defeated:
                drop["resolved"] = True
            should_edit = defeated or drop["_hits"] % 3 == 0

        try:
            await message.add_reaction("\U0001f4a5")
        except discord.HTTPException:
            pass
        if should_edit:
            try:
                await drop["msg"].edit(
                    embed=embed_worldboss(
                        drop["name"], drop["hp"], drop["max_hp"], drop["attackers"],
                        drop["mins"], drop.get("pool", WORLDBOSS_POOL),
                    )
                )
            except discord.HTTPException:
                pass
        if defeated:
            await self._finish_worldboss(message.channel, defeated=True)

    async def _finish_worldboss(self, channel: discord.TextChannel, defeated: bool):
        cid = channel.id
        async with self._channel_locks.get(cid):
            drop = self.active_drops.get(cid)
            if drop is None or drop.get("type") != "worldboss":
                return
            if drop.get("resolved") and drop.get("_paid"):
                return
            drop["resolved"] = True
            drop["_paid"] = True
            await self._cancel_drop_task(drop)
            self.active_drops.pop(cid, None)
            self._boss_cooldowns.pop(cid, None)
            attackers = dict(drop["attackers"])
            max_hp = int(drop["max_hp"])
            pool_total = int(drop.get("pool", WORLDBOSS_POOL))
            name = drop["name"]

        dmg_done = sum(attackers.values())
        if defeated:
            pool = pool_total
        else:
            pool = int(pool_total * min(1.0, dmg_done / max_hp)) if max_hp else 0
        pool = max(0, pool)
        total_dmg = dmg_done or 1
        for uid, dmg in attackers.items():
            share = max(0, int(pool * dmg / total_dmg))
            if share > 0:
                try:
                    add_points(uid, share)
                except EconomyError:
                    log.exception("World boss payout failed for user %s", uid)
        await channel.send(embed=embed_worldboss_dead(name, attackers, pool, defeated))

    async def _worldboss_timeout(self, channel: discord.TextChannel, seconds: int):
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            return
        await self._finish_worldboss(channel, defeated=False)

    # ─────────────── Hot or Cold ──────────────────────

    async def _start_hotcold(self, channel: discord.TextChannel):
        secret = random.randint(1, 100)
        payout = self._effective_payout(random.randint(100, 200))
        msg    = await channel.send(embed=embed_hotcold())
        state  = {
            "type":   "hotcold",
            "secret": secret,
            "payout": payout,
            "msg":    msg,
            "task":   None,
        }
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._drop_timeout(channel))

    async def _check_number_guess(self, message: discord.Message, drop: dict):
        raw = message.content.strip()
        if not raw.isdigit():
            return
        guess = int(raw)
        if guess < 1 or guess > 100:
            return

        secret = drop["secret"]
        cid    = message.channel.id

        if guess == secret:
            if not await self._mark_drop_resolved(cid, drop):
                return
            await self._cancel_drop_task(drop)

            payout = max(0, int(drop["payout"]))
            try:
                new_total = self._award_win(message.author.id, payout)
            except EconomyError:
                await self._rollback_drop_resolution(message.channel, drop)
                await message.channel.send(
                    "⚠️ Could not save your points right now — keep guessing!"
                )
                return
            if not await self._finish_drop(cid, drop):
                return
            await message.channel.send(
                f"🎯 **{message.author.mention}** guessed it! The number was **{secret}**!\n"
                f"**＋{payout} pts** earned  →  Balance: `{new_total} pts`"
            )
        elif guess < secret:
            try:
                await message.add_reaction("👆")   # Higher
            except discord.HTTPException:
                pass
            try:
                await drop["msg"].edit(embed=embed_hotcold("Higher 👆"))
            except discord.HTTPException:
                pass
        else:
            try:
                await message.add_reaction("👇")   # Lower
            except discord.HTTPException:
                pass
            try:
                await drop["msg"].edit(embed=embed_hotcold("Lower 👇"))
            except discord.HTTPException:
                pass

    # ─────────────── Emoji Puzzle ─────────────────────

    async def _start_emoji(self, channel: discord.TextChannel):
        emojis, answer = self._pool_emoji.draw()
        payout = self._effective_payout(random.randint(100, 200))
        await channel.send(embed=embed_emoji_puzzle(emojis, payout))
        state = {
            "type":   "emoji",
            "answer": [answer.lower().strip()],
            "payout": payout,
            "task":   None,
        }
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._drop_timeout(channel))

    # ─────────────── Reaction Time Bomb ───────────────

    async def _start_bomb(self, channel: discord.TextChannel):
        payout = self._effective_payout(random.randint(200, 300))
        view   = BombView(cog=self, channel_id=channel.id, payout=payout)
        msg    = await channel.send(embed=embed_bomb_waiting(), view=view)
        state  = {"type": "bomb", "payout": payout, "view": view, "msg": msg, "task": None}
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._bomb_sequence(channel, view, msg, state))

    async def _bomb_sequence(self, channel: discord.TextChannel, view: BombView, msg: discord.Message, state: dict):
        """Wait a random delay, then arm the bomb, then impose a timeout."""
        fuse_delay = random.uniform(3, 10)
        await asyncio.sleep(fuse_delay)

        if channel.id not in self.active_drops:
            return  # Already resolved

        view.armed = True
        try:
            await msg.edit(embed=embed_bomb_active(state["payout"]), view=view)
        except discord.HTTPException:
            pass

        # Now run the 30-second timeout from when the bomb went live
        await asyncio.sleep(DROP_TIMEOUT)
        cid = channel.id
        async with self._channel_locks.get(cid):
            drop = self.active_drops.pop(cid, None)
        if drop is None:
            return
        view.stop()
        await channel.send(embed=embed_timeout())

    # ─────────────── Blackjack Duel ───────────────────

    async def _start_blackjack(self, channel: discord.TextChannel):
        deck      = _new_deck()
        # Deal: chat gets 2 open cards; house gets 2 (one hidden)
        chat_hand  = [deck.pop(), deck.pop()]
        house_hand = [deck.pop(), deck.pop()]
        payout     = self._effective_payout(500)   # fixed 500-pt pool

        # Send a placeholder message first so we have the object for BlackjackView
        placeholder = await channel.send("🃏 *Dealing cards…*")
        view = BlackjackView(
            cog=self, channel_id=channel.id,
            deck=deck, chat_hand=chat_hand, house_hand=house_hand,
            payout=payout, msg=placeholder,
        )
        embed = embed_blackjack(chat_hand, house_hand, "playing", payout,
                                0, BlackjackView.VOTE_THRESHOLD)
        await placeholder.edit(content=None, embed=embed, view=view)

        state = {"type": "blackjack", "view": view, "msg": placeholder, "task": None}
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._blackjack_timeout(channel, view))

    async def _blackjack_timeout(self, channel: discord.TextChannel, view: BlackjackView):
        await asyncio.sleep(DROP_TIMEOUT)
        if view.resolved:
            return
        view.resolved = True
        for child in view.children:
            child.disabled = True  # type: ignore[attr-defined]
        try:
            await view.msg.edit(view=view)
        except discord.HTTPException as exc:
            log.debug("Blackjack timeout view edit failed: %s", exc)
        cid = channel.id
        drop = self.active_drops.get(cid)
        if drop is not None:
            await self._cancel_drop_task(drop)
        async with self._channel_locks.get(cid):
            self.active_drops.pop(cid, None)
        await channel.send(embed=embed_timeout())
        view.stop()

    # ─────────────── Hot Potato / Loot Sack ───────────

    async def _start_hotpotato(self, channel: discord.TextChannel):
        # Pick a random recent chatter as the first holder
        # We pull from the guild's member cache filtered to online/recently active
        members = [m for m in channel.members if not m.bot and m.status != discord.Status.offline]
        if not members:
            members = [m for m in channel.members if not m.bot]
        if not members:
            async with self._channel_locks.get(channel.id):
                drop = self.active_drops.get(channel.id)
                if drop and drop.get("type") == "_booting":
                    self.active_drops.pop(channel.id, None)
            return   # No valid users — skip this drop

        holder = random.choice(members)
        explode_at = asyncio.get_event_loop().time() + random.uniform(20, 30)

        state = {
            "type":       "hotpotato",
            "holder_id":  holder.id,
            "explode_at": explode_at,
            "task":       None,
        }
        self.active_drops[channel.id] = state

        await channel.send(
            f"🥔 **HOT POTATO!** {holder.mention} just caught the potato!\n"
            f"Type **`!pass @username`** within **10 seconds** to toss it!",
            embed=embed_potato(holder, 10),
        )
        state["task"] = asyncio.create_task(self._potato_timer(channel, state))

    async def _potato_timer(self, channel: discord.TextChannel, state: dict):
        """Runs until the hidden explode_at deadline, then resolves."""
        loop      = asyncio.get_event_loop()
        remaining = state["explode_at"] - loop.time()
        if remaining > 0:
            await asyncio.sleep(remaining)

        cid = channel.id
        async with self._channel_locks.get(cid):
            drop = self.active_drops.get(cid)
            if drop is not state or drop.get("resolved"):
                return
            drop["resolved"] = True
            del self.active_drops[cid]
            holder_id = drop["holder_id"]

        member = channel.guild.get_member(holder_id)
        if member is None:
            await channel.send("🥔 The potato disappeared — nobody around to hold it!")
            return

        if random.random() < 0.5:
            new_total = await self._deduct_safe(channel, holder_id, 200)
            if new_total is None:
                return
            await channel.send(embed=embed_potato_explode_bomb(member))
            await channel.send(
                f"💣 {member.mention} **lost 200 pts**!  Balance: `{new_total} pts`"
            )
        else:
            new_total = await self._award_win_safe(channel, holder_id, 500)
            if new_total is None:
                return
            await channel.send(embed=embed_potato_explode_gold(member))
            await channel.send(
                f"🎁 {member.mention} **gained 500 pts**!  Balance: `{new_total} pts`"
            )

    async def _handle_potato_pass(self, message: discord.Message, drop: dict):
        """Called from on_message when a hot potato drop is active."""
        content = message.content.strip()
        if not content.lower().startswith("!pass"):
            return
        if message.author.id != drop.get("holder_id"):
            return   # Only the current holder can pass

        if not message.mentions:
            await message.channel.send(
                f"⚠️ {message.author.mention}, use `!pass @username` to pass the potato.",
                delete_after=5,
            )
            return

        new_holder = message.mentions[0]
        if new_holder.bot:
            await message.channel.send("🤖 You can't pass the potato to a bot!", delete_after=5)
            return
        if new_holder.id == message.author.id:
            await message.channel.send("🙃 You can't pass it to yourself!", delete_after=5)
            return

        async with self._channel_locks.get(message.channel.id):
            current = self.active_drops.get(message.channel.id)
            if current is not drop or drop.get("resolved"):
                return
            drop["holder_id"] = new_holder.id
            remaining = max(0.0, drop["explode_at"] - asyncio.get_event_loop().time())
            secs_show = min(10, int(remaining))

        await message.channel.send(
            f"🥔 {message.author.mention} **passed** the potato to {new_holder.mention}!\n"
            f"Type **`!pass @username`** within **{secs_show}s** to toss it!",
            embed=embed_potato(new_holder, secs_show),
        )

    # ─────────────── Multiplier / Bounty ──────────────

    async def _start_multiplier(self, channel: discord.TextChannel):
        import time

        # 60% chance double points, 40% chance bounty
        if random.random() < 0.6:
            # Double points for 5 minutes
            self.double_points = True
            self.double_until  = time.time() + (5 * 60)
            await channel.send(embed=embed_multiplier(5))
            # No active drop state needed — just a passive buff
            # Still register a dummy drop so the counter is blocked, then clear it
            state = {"type": "multi_buff", "task": None}
            self.active_drops[channel.id] = state
            state["task"] = asyncio.create_task(self._multi_buff_clear(channel))
        else:
            try:
                top = get_top_user()
            except EconomyError:
                log.warning("Bounty drop fallback: could not read top user")
                top = None
            if top is None:
                # Fallback to double points if no users exist
                self.double_points = True
                self.double_until  = time.time() + (5 * 60)
                await channel.send(embed=embed_multiplier(5))
                state = {"type": "multi_buff", "task": None}
                self.active_drops[channel.id] = state
                state["task"] = asyncio.create_task(self._multi_buff_clear(channel))
                return

            target_id, target_pts = top
            bounty_amount = max(0, min(250, int(target_pts)))
            emojis, answer = self._pool_emoji.draw()

            target_member = channel.guild.get_member(target_id) if channel.guild else None
            target_name = target_member.display_name if target_member else f"User {target_id}"

            await channel.send(embed=embed_bounty(target_name, bounty_amount, emojis))
            state = {
                "type":      "bounty",
                "answer":    [answer.lower().strip()],
                "payout":    bounty_amount,
                "target_id": target_id,
                "task":      None,
            }
            self.active_drops[channel.id] = state
            state["task"] = asyncio.create_task(self._drop_timeout(channel))

    async def _multi_buff_clear(self, channel: discord.TextChannel):
        """Remove the dummy drop state after a brief delay so messages resume."""
        await asyncio.sleep(3)
        self.active_drops.pop(channel.id, None)

    async def _start_heist(self, channel: discord.TextChannel):
        try:
            top = get_top_user()
        except EconomyError:
            log.warning("Heist fallback to trivia: could not read top user")
            await self._start_trivia(channel)
            return
        if top is None:
            await self._start_trivia(channel)
            return
        target_id, target_pts = top
        if target_pts <= 0:
            await self._start_trivia(channel)
            return
        steal = max(50, min(500, int(target_pts * 0.15)))
        a = random.randint(20, 99)
        b = random.randint(20, 99)
        member = channel.guild.get_member(target_id) if channel.guild else None
        target_name = member.display_name if member else f"User {target_id}"
        await channel.send(embed=embed_heist(target_name, steal, a, b))
        state = {
            "type":      "heist",
            "answer":    [str(a + b)],
            "payout":    steal,
            "target_id": target_id,
            "task":      None,
        }
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._drop_timeout(channel))

    async def _start_losers(self, channel: discord.TextChannel):
        question, answers = self._pool_trivia.draw()
        payout = self._effective_payout(random.randint(120, 220))
        try:
            blocked = {r["user_id"] for r in get_leaderboard(UNDERDOG_LOCK_N)}
        except EconomyError:
            log.warning("Losers bracket: leaderboard unavailable; no podium lock")
            blocked = set()
        await channel.send(embed=embed_losers(question, payout))
        state = {
            "type":    "losers",
            "answer":  [ans.lower().strip() for ans in answers],
            "payout":  payout,
            "blocked": blocked,
            "task":    None,
        }
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._drop_timeout(channel))

    async def _handle_losers_answer(self, message: discord.Message, drop: dict):
        content = _normalize_answer(message.content)
        if not content or content not in drop.get("answer", []):
            return
        uid = message.author.id
        if uid in drop.get("blocked", set()):
            await message.channel.send(
                f"\U0001f512 {message.author.mention} the **Losers Bracket** is underdogs only \u2014 Top {UNDERDOG_LOCK_N} can't claim this one!",
                delete_after=4,
            )
            return
        cid = message.channel.id
        if not await self._mark_drop_resolved(cid, drop):
            return
        await self._cancel_drop_task(drop)
        payout = max(0, int(drop["payout"]))
        try:
            new_total = self._award_win(uid, payout)
        except EconomyError:
            await self._rollback_drop_resolution(message.channel, drop)
            await message.channel.send(
                "⚠️ Could not save your points right now — the bracket is still open."
            )
            return
        if not await self._finish_drop(cid, drop):
            return
        await message.channel.send(
            embed=embed_win_text(message.author, payout, new_total, "losers")
        )

    async def _start_worldboss(self, channel: discord.TextChannel):
        max_hp = random.randint(120, 220)
        mins   = 2
        pool   = random.randint(800, 1500)
        name   = random.choice(WORLDBOSS_NAMES)
        state = {
            "type": "worldboss", "name": name,
            "hp": max_hp, "max_hp": max_hp, "mins": mins,
            "pool": pool, "attackers": {}, "task": None, "_hits": 0,
        }
        self.active_drops[channel.id] = state
        self._boss_cooldowns[channel.id] = {}
        msg = await channel.send(embed=embed_worldboss(name, max_hp, max_hp, {}, mins, pool))
        state["msg"] = msg
        state["task"] = asyncio.create_task(self._worldboss_timeout(channel, mins * 60))

    # ─────────────── Fill in the Blank ────────────────

    async def _start_fillblank(self, channel: discord.TextChannel):
        entry  = self._pool_fillblank.draw()
        payout = self._effective_payout(random.randint(80, 180))
        await channel.send(embed=embed_fillblank(entry["prompt"], payout))
        state = {
            "type":   "fillblank",
            "answer": [a.lower().strip() for a in entry["answer"]],
            "payout": payout,
            "task":   None,
        }
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._drop_timeout(channel))

    # ─────────────── Fast Math ────────────────────────

    async def _start_fastmath(self, channel: discord.TextChannel):
        entry  = self._pool_math.draw()
        payout = self._effective_payout(random.randint(100, 200))
        await channel.send(embed=embed_fastmath(entry["prompt"], payout))
        state = {
            "type":   "fastmath",
            "answer": [a.lower().strip() for a in entry["answer"]],
            "payout": payout,
            "task":   None,
        }
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._drop_timeout(channel))

    # ─────────────── True or False ──────────���─────────

    async def _start_truefalse(self, channel: discord.TextChannel):
        entry   = self._pool_truefalse.draw()
        payout  = self._effective_payout(random.randint(80, 180))
        view    = TrueFalseView(
            cog=self,
            channel_id=channel.id,
            correct=entry["answer"],
            fact=entry["fact"],
            statement=entry["statement"],
            payout=payout,
        )
        msg = await channel.send(embed=embed_truefalse(entry["statement"], payout), view=view)
        state = {
            "type":      "truefalse",
            "payout":    payout,
            "view":      view,
            "msg":       msg,
            "task":      None,
            "statement": entry["statement"],
            "correct":   entry["answer"],
            "fact":      entry["fact"],
        }
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._drop_timeout(channel))

    # ─────────────── Text answer checker ──────────────

    async def _check_text_answer(self, message: discord.Message, drop: dict):
        content = _normalize_answer(message.content)
        if not content or content not in drop.get("answer", []):
            return

        cid = message.channel.id
        if not await self._mark_drop_resolved(cid, drop):
            return

        await self._cancel_drop_task(drop)

        dtype = drop["type"]
        payout = max(0, int(drop["payout"]))
        winner_id = message.author.id

        if dtype in ("bounty", "heist"):
            target_id = drop.get("target_id")
            if target_id and target_id != winner_id:
                award = self._compute_award_amount(winner_id, payout)
                try:
                    _, new_total = payout_from_target(target_id, winner_id, payout, award)
                except EconomyError:
                    await self._rollback_drop_resolution(message.channel, drop)
                    await message.channel.send(
                        "⚠️ Could not complete the payout — no points were changed. The drop is still live."
                    )
                    return
                if not await self._finish_drop(cid, drop):
                    return
                await message.channel.send(
                    embed=embed_win_text(message.author, payout, new_total, dtype)
                )
                return

        try:
            new_total = self._award_win(winner_id, payout)
        except EconomyError:
            await self._rollback_drop_resolution(message.channel, drop)
            await message.channel.send(
                "⚠️ Could not save your points right now — the drop is still live."
            )
            return

        if not await self._finish_drop(cid, drop):
            return
        await message.channel.send(
            embed=embed_win_text(message.author, payout, new_total, dtype)
        )

    # ─────────────── Timeout handler ──────────────────

    async def _drop_timeout(self, channel: discord.TextChannel):
        await asyncio.sleep(DROP_TIMEOUT)
        cid = channel.id
        async with self._channel_locks.get(cid):
            drop = self.active_drops.pop(cid, None)
            if drop is None or drop.get("resolved") or drop.get("type") == "_booting":
                return
            drop["resolved"] = True

        dtype = drop.get("type")

        if dtype == "lootbox":
            view: LootboxView = drop["view"]
            view.claimed = True
            view.stop()
            try:
                for child in view.children:
                    child.disabled = True  # type: ignore[attr-defined]
                await drop["msg"].edit(view=view)
            except discord.HTTPException:
                pass

        elif dtype == "boss":
            self._boss_cooldowns.pop(cid, None)

        elif dtype == "bomb":
            view_b: BombView = drop["view"]
            view_b.stop()

        elif dtype == "blackjack":
            view_bj: BlackjackView = drop["view"]
            if not view_bj.resolved:
                view_bj.resolved = True
                for child in view_bj.children:
                    child.disabled = True  # type: ignore[attr-defined]
                try:
                    await drop["msg"].edit(view=view_bj)
                except discord.HTTPException:
                    pass
                view_bj.stop()

        elif dtype == "truefalse":
            view_tf: TrueFalseView = drop["view"]
            if not view_tf.resolved:
                view_tf.resolved = True
                for child in view_tf.children:
                    child.disabled = True  # type: ignore[attr-defined]
                try:
                    await drop["msg"].edit(view=view_tf)
                except discord.HTTPException:
                    pass
                view_tf.stop()
            await channel.send(
                embed=embed_truefalse_result(
                    None,
                    drop.get("statement", ""),
                    drop.get("correct", False),
                    drop.get("fact", ""),
                    drop.get("payout", 0),
                    0,
                )
            )
            return  # skip generic embed_timeout() below

        # hotpotato has its own internal timer; nothing extra to clean up here

        await channel.send(embed=embed_timeout())

    # ─────────────── /setdrops ────────────────────────

    @app_commands.command(name="setdrops", description="Set the channel where event drops will fire (single-channel mode).")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel="The text channel to send drops in.")
    async def setdrops(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        assert interaction.guild_id is not None
        set_drops_channel(interaction.guild_id, channel.id)
        await interaction.response.send_message(
            embed=embed_set_confirm("Drops Channel", channel), ephemeral=True
        )

    @app_commands.command(name="getdrops", description="Show which channel is set for event drops.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def getdrops(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        channel_id = get_drops_channel(interaction.guild_id)
        await interaction.response.send_message(
            embed=embed_get_channel("Drops Channel", channel_id), ephemeral=True
        )

    # ─────────────── /setpingrole ─────────────────────

    @app_commands.command(name="setpingrole", description="Set a role to ping before every event drop.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(role="The role to ping before each drop.")
    async def setpingrole(self, interaction: discord.Interaction, role: discord.Role) -> None:
        assert interaction.guild_id is not None
        set_ping_role(interaction.guild_id, role.id)
        e = _base_embed(C_SET)
        e.description = (
            f"```ansi\n\u001b[1;32m  ✔  DROP PING ROLE SET  \u001b[0m\n```"
            f"{SEP}\n"
            f"{role.mention} will be pinged right before every drop.\n"
            f"{SEP}"
        )
        e.set_footer(text="SOLACE EVENT  •  Drop Settings")
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ─────────────── /clearpingrole ───────────��───────

    @app_commands.command(name="clearpingrole", description="Stop pinging a role before drops.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def clearpingrole(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        clear_ping_role(interaction.guild_id)
        e = _base_embed(C_TIMEOUT)
        e.description = (
            f"{SEP}\n"
            f"**Drop pings disabled.** No role will be pinged before drops.\n"
            f"{SEP}"
        )
        e.set_footer(text="SOLACE EVENT  •  Drop Settings")
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ─────────���────�� /points ──────────────────────────

    @commands.hybrid_command(name="points", description="Check your (or another member's) point balance.")
    @app_commands.describe(member="The member whose points you want to check.")
    async def points_command(self, ctx: commands.Context, member: discord.Member | None = None):
        target = member or ctx.author
        try:
            balance = get_points(target.id)
        except EconomyError:
            await ctx.send("⚠️ Could not load point balance right now — try again shortly.")
            return
        await ctx.send(embed=embed_points(target, balance))

    # ─────────────── /leaderboard ─────────────────────

    @commands.hybrid_command(name="leaderboard", description="Show the top 10 players by points.")
    async def leaderboard_command(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        try:
            rows = get_leaderboard(10)
        except EconomyError:
            await ctx.send("⚠️ Could not load the leaderboard right now — try again shortly.")
            return
        await ctx.send(embed=await embed_leaderboard(ctx.guild, rows))

    # ─────────────── /setdroptrigger ──────────────────

    @app_commands.command(name="setdroptrigger", description="Set how many messages trigger a drop (default: 10).")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(count="Number of messages before a drop fires (min 3, max 1000).")
    async def setdroptrigger(self, interaction: discord.Interaction, count: int) -> None:
        assert interaction.guild_id is not None
        if count < 3 or count > 1000:
            await interaction.response.send_message(
                "Count must be between **3** and **1000**.", ephemeral=True
            )
            return
        set_drop_trigger(interaction.guild_id, count)
        e = _base_embed(C_SET)
        e.description = (
            f"```ansi\n\u001b[1;32m  ✔  DROP TRIGGER SET  \u001b[0m\n```"
            f"{SEP}\n"
            f"A drop will now fire every **{count} messages**.\n"
            f"{SEP}"
        )
        e.set_footer(text="SOLACE EVENT  •  Drop Settings")
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ─────────────── /dropinfo ────────────────────────

    @app_commands.command(name="worldboss", description="Summon a massive co-op World Boss raid (catch-up event).")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(hp="Boss health pool (default 750).", minutes="Raid duration in minutes (default 10).")
    async def worldboss(self, interaction: discord.Interaction, hp: int | None = None, minutes: int | None = None) -> None:
        assert interaction.guild_id is not None
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message("Run this in a text channel.", ephemeral=True)
            return
        if channel.id in self.active_drops:
            await interaction.response.send_message(
                "A drop or boss is already active in this channel \u2014 wait for it to finish.", ephemeral=True
            )
            return
        max_hp = max(50, min(hp or WORLDBOSS_HP, 100000))
        mins   = max(1, min(minutes or WORLDBOSS_MIN, 120))
        name   = random.choice(WORLDBOSS_NAMES)
        state = {
            "type": "worldboss", "name": name,
            "hp": max_hp, "max_hp": max_hp, "mins": mins,
            "pool": WORLDBOSS_POOL, "attackers": {}, "task": None, "_hits": 0,
        }
        self.active_drops[channel.id] = state
        self._boss_cooldowns[channel.id] = {}
        await interaction.response.send_message(
            f"\U0001f30b **{name}** has been summoned in {channel.mention}!", ephemeral=True
        )
        msg = await channel.send(embed=embed_worldboss(name, max_hp, max_hp, {}, mins, WORLDBOSS_POOL))
        state["msg"] = msg
        state["task"] = asyncio.create_task(self._worldboss_timeout(channel, mins * 60))

    @app_commands.command(name="happyhour", description="Start a Mega-Drop Happy Hour (faster drops + double rare odds).")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(minutes="Length in minutes (default 60). Use 0 to end it early.")
    async def happyhour(self, interaction: discord.Interaction, minutes: int | None = None) -> None:
        assert interaction.guild_id is not None
        mins = HAPPY_DEFAULT_MIN if minutes is None else minutes
        e = _base_embed(C_HAPPY)
        if mins <= 0:
            self.happy_until = 0.0
            e.description = (
                f"```ansi\n\u001b[1;33m  \u25c8  HAPPY HOUR ENDED  \u25c8\u001b[0m\n```"
                f"{SEP}\n"
                f"Mega-Drop Happy Hour has been switched **off**.\n"
                f"{SEP}"
            )
            e.set_footer(text="SOLACE EVENT  \u2022  Happy Hour")
            await interaction.response.send_message(embed=e, ephemeral=True)
            return
        mins = min(mins, HAPPY_MAX_MIN)
        self.happy_until = time.time() + mins * 60
        e.description = (
            f"```ansi\n\u001b[1;33m  \u26a1  MEGA-DROP HAPPY HOUR  \u26a1\u001b[0m\n```"
            f"{SEP}\n"
            f"**Happy Hour is LIVE for `{mins}` minutes!**\n\n"
            f"\U0001f4ac Drop trigger **halved** \u2014 drops fire twice as fast\n"
            f"\U0001f3b2 Rare drops (boss, blackjack, multiplier, bomb) **2\u00d7 more likely**\n"
            f"{SEP}"
        )
        e.set_footer(text="SOLACE EVENT  \u2022  Happy Hour")
        await interaction.response.send_message(embed=e, ephemeral=True)
        drops_id = get_drops_channel(interaction.guild_id)
        if drops_id and interaction.guild is not None:
            ch = interaction.guild.get_channel(drops_id)
            if isinstance(ch, discord.TextChannel):
                ann = _base_embed(C_HAPPY)
                ann.description = (
                    f"```ansi\n\u001b[1;33m  \u26a1  HAPPY HOUR STARTED  \u26a1\u001b[0m\n```"
                    f"{SEP}\n"
                    f"**Drops are now twice as fast for `{mins}` minutes!**\n"
                    f"Rare events are 2\u00d7 more common \u2014 get grinding! \U0001f525\n"
                    f"{SEP}"
                )
                try:
                    await ch.send(embed=ann)
                except discord.HTTPException:
                    pass

    @app_commands.command(name="underdog", description="Toggle the tiered Underdog catch-up bonus (Top 3 none, 4-7 1.5x, 8+ 1.7x).")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(enabled="Turn the Underdog multiplier on or off.")
    async def underdog(self, interaction: discord.Interaction, enabled: bool) -> None:
        self.underdog_enabled = enabled
        e = _base_embed(C_SET)
        e.description = (
            f"```ansi\n\u001b[1;32m  \u25c8  UNDERDOG MULTIPLIER  \u25c8\u001b[0m\n```"
            f"{SEP}\n"
            f"Underdog catch-up bonus is now **{'`ON`' if enabled else '`OFF`'}**.\n"
            f"Tiers: Top {UNDERDOG_LOCK_N} none  \u2022  ranks 4-{UNDERDOG_MID_MAX} {UNDERDOG_MID_MULT}x  \u2022  rank {UNDERDOG_MID_MAX + 1}+ {UNDERDOG_LOW_MULT}x.\n"
            f"{SEP}"
        )
        e.set_footer(text="SOLACE EVENT  \u2022  Catch-Up Mechanics")
        await interaction.response.send_message(embed=e, ephemeral=True)

    @app_commands.command(name="dropinfo", description="Show current drop settings for this server.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def dropinfo(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        channel_id = get_drops_channel(interaction.guild_id)
        trigger    = get_drop_trigger(interaction.guild_id)
        ping_role_id = get_ping_role(interaction.guild_id)
        all_mode   = get_all_channels_mode(interaction.guild_id)
        disabled   = get_disabled_channels(interaction.guild_id)
        channel_mention = f"<#{channel_id}>" if channel_id else "*not set*"
        ping_mention    = f"<@&{ping_role_id}>" if ping_role_id else "*not set*"
        mode_str   = "`ALL channels`" if all_mode else "`single channel`"
        disabled_str = ", ".join(f"<#{c}>" for c in disabled) if disabled else "*none*"
        paused     = get_drops_paused(interaction.guild_id)
        active = len(self.active_drops)
        happy_str = (
            f"`{int((self.happy_until - time.time()) // 60) + 1}m left`"
            if self._happy_active() else "`off`"
        )
        e = _base_embed(C_POINTS)
        e.description = (
            f"```ansi\n\u001b[1;33m  ◈  DROP SETTINGS  ◈\u001b[0m\n```"
            f"{SEP}\n"
            f"⏸️ **Drops paused:** {'`YES — all off`' if paused else '`no`'}\n"
            f"🌐 **Drop mode:** {mode_str}\n"
            f"📢 **Drop Channel:** {channel_mention}\n"
            f"🚫 **Disabled channels:** {disabled_str}\n"
            f"🔔 **Drop ping role:** {ping_mention}\n"
            f"💬 **Messages to trigger:** `{trigger}`\n"
            f"🎯 **Active drops right now:** `{active}`\n"
            f"✨ **Double points:** {'`ACTIVE`' if self.double_points else '`off`'}\n"
            f"\u2696\ufe0f **Underdog (4-{UNDERDOG_MID_MAX}: {UNDERDOG_MID_MULT}x \u2022 {UNDERDOG_MID_MAX + 1}+: {UNDERDOG_LOW_MULT}x):** {'`ON`' if self.underdog_enabled else '`off`'}\n"
            f"\u26a1 **Happy Hour:** {happy_str}\n"
            f"{SEP}"
        )
        e.set_footer(text="SOLACE EVENT  •  /setdrops | /setdroptrigger")
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ─────────────── /mystats ─────────────────────────

    @commands.hybrid_command(name="mystats", description="Show your event rank and point balance.")
    async def mystats(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        user = ctx.author
        try:
            balance = get_points(user.id)
            rows = get_leaderboard(200)
        except EconomyError:
            await ctx.send("⚠️ Could not load your stats right now — try again shortly.")
            return
        rank = next((i + 1 for i, r in enumerate(rows) if r["user_id"] == user.id), None)
        total_players = len(rows)

        e = _base_embed(C_POINTS)
        e.set_thumbnail(url=user.display_avatar.url)
        rank_str = f"**#{rank}** of {total_players}" if rank else "*unranked*"
        e.description = (
            f"```ansi\n\u001b[1;33m  ◈  MY STATS  ◈\u001b[0m\n```"
            f"{SEP}\n"
            f"**{user.display_name}**\n\n"
            f"💰 Balance:  `{balance:,} pts`\n"
            f"🏆 Rank:     {rank_str}\n"
            f"{SEP}"
        )
        e.set_footer(text="SOLACE EVENT  •  /leaderboard for full board")
        await ctx.send(embed=e)

    # ─────────────── /givepts ─────────────────────────

    @app_commands.command(name="givepts", description="Give or deduct points from a member. Use negative values to deduct.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        member="The member to give or deduct points from.",
        amount="Points to add (positive) or remove (negative).",
    )
    async def givepts(self, interaction: discord.Interaction, member: discord.Member, amount: int) -> None:
        if amount == 0:
            await interaction.response.send_message("Amount can't be zero.", ephemeral=True)
            return

        try:
            if amount > 0:
                new_total = add_points(member.id, amount)
                sign      = f"＋{amount}"
                colour    = C_SET
                action    = "awarded"
            else:
                new_total = deduct_points(member.id, abs(amount))
                sign      = f"−{abs(amount)}"
                colour    = 0xE74C3C
                action    = "deducted"
        except EconomyError:
            await interaction.response.send_message(
                "⚠️ Could not update points — database unavailable. Try again shortly.",
                ephemeral=True,
            )
            return

        e = discord.Embed(colour=colour)
        e.description = (
            f"```ansi\n\u001b[1;32m  ◈  POINTS {action.upper()}  ◈\u001b[0m\n```"
            f"{SEP}\n"
            f"**{member.display_name}**\n"
            f"{sign} pts  ›  New balance: `{new_total:,} pts`\n"
            f"{SEP}"
        )
        e.set_thumbnail(url=member.display_avatar.url)
        e.set_footer(text=f"SOLACE EVENT  •  Adjusted by {interaction.user.display_name}")
        await interaction.response.send_message(embed=e)

    # ─────────────── /allchannels ─────────────────────

    @app_commands.command(name="allchannels", description="Let event drops fire in ALL channels (exclude some with /disablechannel).")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(enabled="Turn all-channels drop mode on or off.")
    async def allchannels(self, interaction: discord.Interaction, enabled: bool) -> None:
        assert interaction.guild_id is not None
        set_all_channels_mode(interaction.guild_id, enabled)
        disabled = get_disabled_channels(interaction.guild_id)
        if enabled:
            excl = ", ".join(f"<#{c}>" for c in disabled) if disabled else "*none*"
            body = (
                "✅ **Drops now fire in EVERY channel.**\n"
                f"🚫 Excluded channels: {excl}\n"
                "Use `/disablechannel` to exclude more, `/enablechannel` to re-allow."
            )
            colour = C_SET
        else:
            body = (
                "🛑 **All-channels mode is OFF.**\n"
                "Drops only fire in the channel set with `/setdrops`."
            )
            colour = C_TIMEOUT
        e = _base_embed(colour)
        e.description = f"{SEP}\n{body}\n{SEP}"
        e.set_footer(text="SOLACE EVENT  •  Drop Settings")
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ─────────────── /disablechannel ──────────────────

    @app_commands.command(name="disablechannel", description="Block event drops in a specific channel (all-channels mode).")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel="The channel where drops should NOT fire.")
    async def disablechannel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        assert interaction.guild_id is not None
        add_disabled_channel(interaction.guild_id, channel.id)
        e = _base_embed(C_TIMEOUT)
        e.description = f"{SEP}\n🚫 Event drops are now **disabled** in {channel.mention}.\n{SEP}"
        e.set_footer(text="SOLACE EVENT  •  Drop Settings")
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ─────────────── /enablechannel ───────────────────

    @app_commands.command(name="enablechannel", description="Re-allow event drops in a previously disabled channel.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel="The channel to re-enable drops in.")
    async def enablechannel(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        assert interaction.guild_id is not None
        remove_disabled_channel(interaction.guild_id, channel.id)
        e = _base_embed(C_SET)
        e.description = f"{SEP}\n✅ Event drops are now **allowed** in {channel.mention}.\n{SEP}"
        e.set_footer(text="SOLACE EVENT  •  Drop Settings")
        await interaction.response.send_message(embed=e, ephemeral=True)

    # ─────────────── /disableall ──────────────────────

    @app_commands.command(name="disableall", description="Disable (or re-enable) event drops in ALL channels server-wide.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(disabled="True = stop all drops everywhere, False = resume.")
    async def disableall(self, interaction: discord.Interaction, disabled: bool) -> None:
        assert interaction.guild_id is not None
        set_drops_paused(interaction.guild_id, disabled)
        if disabled:
            body = (
                "🛑 **All event drops are now DISABLED server-wide.**\n"
                "No drops will fire in any channel until you re-enable them."
            )
            colour = C_TIMEOUT
        else:
            body = (
                "✅ **Event drops re-enabled.**\n"
                "Drops will resume based on your current mode (`/dropinfo`)."
            )
            colour = C_SET
        e = _base_embed(colour)
        e.description = f"{SEP}\n{body}\n{SEP}"
        e.set_footer(text="SOLACE EVENT  •  Drop Settings")
        await interaction.response.send_message(embed=e, ephemeral=True)


# ────────────────────────────── setup ─────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerDropsEconomy(bot))
