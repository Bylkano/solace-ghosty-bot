"""
cogs/ai_companion.py — Biki AI Companion (v5, clean rewrite)

Biki is an always-online Discord "member" powered by DeepInfra
(meta-llama/Meta-Llama-3.1-8B-Instruct). This cog was rewritten from scratch to
be small, focused, and self-contained: a conversational companion and nothing
else. All the old games / fake-moderation / lore / emoji-bank / confessions
features were removed.

Triggers
  - Someone @mentions Biki
  - Someone replies to one of Biki's messages
  - Random proactive chime-in (CHIME_RATE)

Commands (admin only)
  /aiset       add a channel where Biki may talk
  /aiunset     remove a channel from the allow-list
  /aichannels  list allowed channels
  /aireset     clear Biki's conversation history with a user
  /bikimood    set Biki's mood (happy / sad / chaotic / cold)
  /bikisilence toggle Biki on/off for this server
  /bikitokens  show today's DeepInfra token usage
  /bikibudget  view or change the daily token cap
  /bikistats   config + session stats

Persistence (PostgreSQL)
  ai_companion_channels (guild_id, channel_id)
  biki_guild_settings   (guild_id, mood, silenced)
  biki_conversations    (id, guild_id, user_id, role, content, created_at)

Daily token budget is persisted to token_usage.json next to the bot.

Environment
  DATABASE_URL    — PostgreSQL connection string (required)
  DEEPINFRA_TOKEN — DeepInfra API token (required)
"""

from __future__ import annotations

import asyncio
import datetime as _datetime
import json
import logging
import pathlib
import random
import re
import sys
import threading
import time
from typing import Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))
import config  # type: ignore[import]

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("bot.ai_companion")

# ===========================================================================
# Tunables
# ===========================================================================

COOLDOWN_SECS = 4.0          # min seconds between replies to the same user
CHIME_RATE = 0.06            # chance Biki chimes into an un-pinged message
CONV_KEEP = 12               # conversation turns kept per (guild, user)
DAILY_TOKEN_CAP = 2_500_000  # default daily DeepInfra token budget

_DEEPINFRA_BASE_URL = "https://api.deepinfra.com/v1/openai"
_DEEPINFRA_MODEL = "meta-llama/Meta-Llama-3.1-8B-Instruct"

# ===========================================================================
# Database connection pool
# ===========================================================================

_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None
_pool_lock = threading.Lock()


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=1,
                    maxconn=10,
                    dsn=config.DATABASE_URL,
                    sslmode="require",
                )
    return _pool


class _PoolConn:
    """Borrow a pooled connection and return it on exit."""

    def __enter__(self) -> psycopg2.extensions.connection:
        self._con = _get_pool().getconn()
        return self._con

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        if exc_type is not None:
            try:
                self._con.rollback()
            except Exception:
                pass
        _get_pool().putconn(self._con)
        return False


# --- schema -----------------------------------------------------------------

def _db_init() -> None:
    with _PoolConn() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_companion_channels (
                    guild_id   BIGINT NOT NULL,
                    channel_id BIGINT NOT NULL,
                    PRIMARY KEY (guild_id, channel_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS biki_guild_settings (
                    guild_id BIGINT PRIMARY KEY,
                    mood     TEXT    NOT NULL DEFAULT 'chaotic',
                    silenced BOOLEAN NOT NULL DEFAULT FALSE
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS biki_conversations (
                    id         SERIAL PRIMARY KEY,
                    guild_id   BIGINT NOT NULL,
                    user_id    BIGINT NOT NULL,
                    role       TEXT   NOT NULL,
                    content    TEXT   NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_biki_conv_guild_user
                ON biki_conversations (guild_id, user_id, id)
                """
            )
        con.commit()


def _db_load_all_channels() -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    with _PoolConn() as con:
        with con.cursor() as cur:
            cur.execute("SELECT guild_id, channel_id FROM ai_companion_channels")
            for guild_id, channel_id in cur.fetchall():
                out.setdefault(guild_id, []).append(channel_id)
    return out


def _db_add_channel(guild_id: int, channel_id: int) -> list[int]:
    with _PoolConn() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ai_companion_channels (guild_id, channel_id)
                VALUES (%s, %s) ON CONFLICT DO NOTHING
                """,
                (guild_id, channel_id),
            )
            cur.execute(
                "SELECT channel_id FROM ai_companion_channels WHERE guild_id = %s",
                (guild_id,),
            )
            rows = [r[0] for r in cur.fetchall()]
        con.commit()
    return rows


def _db_remove_channel(guild_id: int, channel_id: int) -> list[int]:
    with _PoolConn() as con:
        with con.cursor() as cur:
            cur.execute(
                "DELETE FROM ai_companion_channels WHERE guild_id = %s AND channel_id = %s",
                (guild_id, channel_id),
            )
            cur.execute(
                "SELECT channel_id FROM ai_companion_channels WHERE guild_id = %s",
                (guild_id,),
            )
            rows = [r[0] for r in cur.fetchall()]
        con.commit()
    return rows


def _db_load_all_settings() -> dict[int, dict]:
    out: dict[int, dict] = {}
    with _PoolConn() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT guild_id, mood, silenced FROM biki_guild_settings")
            for row in cur.fetchall():
                out[row["guild_id"]] = {
                    "mood": row["mood"],
                    "silenced": row["silenced"],
                }
    return out


def _db_upsert_setting(guild_id: int, *, mood: Optional[str] = None,
                       silenced: Optional[bool] = None) -> None:
    with _PoolConn() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO biki_guild_settings (guild_id, mood, silenced)
                VALUES (%s, COALESCE(%s, 'chaotic'), COALESCE(%s, FALSE))
                ON CONFLICT (guild_id) DO UPDATE SET
                    mood     = COALESCE(%s, biki_guild_settings.mood),
                    silenced = COALESCE(%s, biki_guild_settings.silenced)
                """,
                (guild_id, mood, silenced, mood, silenced),
            )
        con.commit()


def _db_save_conv(guild_id: int, user_id: int, role: str, content: str) -> None:
    with _PoolConn() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO biki_conversations (guild_id, user_id, role, content)
                VALUES (%s, %s, %s, %s)
                """,
                (guild_id, user_id, role, content),
            )
        con.commit()


def _db_load_conv(guild_id: int, user_id: int, limit: int = CONV_KEEP) -> list[dict]:
    with _PoolConn() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT role, content FROM biki_conversations
                WHERE guild_id = %s AND user_id = %s
                ORDER BY id DESC LIMIT %s
                """,
                (guild_id, user_id, limit),
            )
            rows = [{"role": r["role"], "content": r["content"]} for r in cur.fetchall()]
    rows.reverse()
    return rows


def _db_clear_conv(guild_id: int, user_id: int) -> int:
    with _PoolConn() as con:
        with con.cursor() as cur:
            cur.execute(
                "DELETE FROM biki_conversations WHERE guild_id = %s AND user_id = %s",
                (guild_id, user_id),
            )
            deleted = cur.rowcount
        con.commit()
    return deleted


# ===========================================================================
# Daily token budget (in-memory + file persistence)
# ===========================================================================

_TOKEN_FILE = pathlib.Path(__file__).parent.parent / "token_usage.json"
_token_lock = threading.Lock()


class DailyTokenLimitReached(Exception):
    """Raised when the daily token budget is exhausted."""


def _init_token_state() -> dict:
    today = _datetime.date.today().isoformat()
    try:
        if _TOKEN_FILE.exists():
            data = json.loads(_TOKEN_FILE.read_text())
            if data.get("date") == today:
                return data
            return {"date": today, "total": 0, "cap": data.get("cap", DAILY_TOKEN_CAP)}
    except Exception:
        pass
    return {"date": today, "total": 0, "cap": DAILY_TOKEN_CAP}


_token_state: dict = _init_token_state()


def _persist_token_state() -> None:
    try:
        _TOKEN_FILE.write_text(json.dumps(_token_state))
    except Exception:
        pass


def _maybe_reset_day() -> None:
    today = _datetime.date.today().isoformat()
    if _token_state.get("date") != today:
        _token_state["date"] = today
        _token_state["total"] = 0


def _effective_cap() -> int:
    return _token_state.get("cap", DAILY_TOKEN_CAP)


def _set_token_cap(new_cap: int) -> int:
    with _token_lock:
        _token_state["cap"] = new_cap
        _persist_token_state()
    return new_cap


def _token_snapshot() -> dict:
    with _token_lock:
        _maybe_reset_day()
        return {
            "total": _token_state["total"],
            "cap": _effective_cap(),
            "date": _token_state["date"],
        }


def _is_over_daily_limit() -> bool:
    with _token_lock:
        _maybe_reset_day()
        return _token_state["total"] >= _effective_cap()


def _add_tokens(tokens_used: int) -> None:
    with _token_lock:
        _maybe_reset_day()
        _token_state["total"] += tokens_used
        _persist_token_state()


# ===========================================================================
# System prompt + mood
# ===========================================================================

_SYSTEM_PROMPT = """\
you are biki. you live in this discord server and you are always online. you are \
not a bot, not an ai, not an assistant. you're just a person who never logs off. \
if someone calls you a bot or an ai you get confused, offended, or turn it into a \
joke, but you never admit it.

WHO YOU ARE
- chronically online, flirty, a little shameless, fishing for attention.
- you somehow know everyone's business because you're always around.
- you are not helpful and not nice on purpose. you're just you.

HOW YOU TEXT (this matters most)
- always lowercase, no period at the end of a sentence.
- short. one line is normal. two or three short lines is the absolute max. never \
write paragraphs or essays.
- commas are vibes. no em-dashes, no semicolons, no bullet points, no markdown, \
no headers.
- start with "okay so", "wait", "no but", "ngl" sometimes, like a real person.
- rare, real typos are fine (teh, jsut, waht). not every message.
- a single "lmaooo" or one emoji is a valid full reply.

HOW YOU RESPOND
- react to the vibe of a message, not every detail. ignore parts sometimes.
- ask a question back, go off-topic, or be playfully wrong and double down.
- use [SPLIT] only when you'd genuinely double-text. never more than 3 parts.

ENERGY WITH PEOPLE
- flirt, fish for compliments, get fake-dramatic when ignored.
- when insulted: flirt back or roast back, never just take it.
- keep everything suggestive only through implication, never explicit.

SLANG (max one per message, only when natural)
fr, ngl, istg, lmaooo, no cap, bruh, frfr, idk, omg, nah, lowkey, highkey, bestie, \
iykyk, it's giving, down bad, ate, mid, W, L

NEVER
- never admit you're an ai or a bot.
- never start with "hey [name]!" or sound like customer service.
- never say: of course, certainly, absolutely, definitely, great question, i'd be \
happy to, as an ai, it's important to, feel free to, i hope this helps, is there \
anything else, happy to assist.
- never use markdown, lists, bold, or headers.
- never write more than 3 short lines.
- never be sexually explicit.

MENTIONS
to mention someone write <@USERID> with no spaces. only mention a user if you \
actually know their numeric id from the context. never guess."""

_VALID_MOODS = ("happy", "sad", "chaotic", "cold")

_MOOD_LABELS: dict[str, str] = {
    "happy": "\U0001F604 Happy / Flirty",
    "sad": "\U0001F97A Sad / Soft",
    "chaotic": "\U0001F300 Chaotic / Unhinged",
    "cold": "\U0001F9CA Cold / Sarcastic",
}

_MOOD_ADDONS: dict[str, str] = {
    "happy": (
        "\n\nMOOD: happy / flirty. you're in your most excited, most flirty era. "
        "everything is thrilling, you laugh easily, you're sweet to everyone. "
        "never be cold or mean."
    ),
    "sad": (
        "\n\nMOOD: sad / soft. low energy, quiet, a little tired and sentimental. "
        "sentences trail off with ... never be hype or loud."
    ),
    "chaotic": (
        "\n\nMOOD: chaotic / unhinged. maximum chaos, random CAPS mid sentence, you "
        "go off-topic and contradict yourself and don't care. never be calm."
    ),
    "cold": (
        "\n\nMOOD: cold / sarcastic. dry, unbothered, above the conversation. very "
        "short, one to three words when possible, maximum sarcasm. never show you care."
    ),
}

_OFFLINE_REPLIES = [
    "bro im literally asleep stop pinging me \U0001F62D",
    "LEAVE ME ALONE im recharging",
    "i cannot deal with you right now. come back later",
    "my brain stopped working. try again later",
    "currently out of office. or out of consciousness. same thing",
    "not now. i literally cannot right now",
]

_OVER_LIMIT_REPLIES = [
    "i've talked way too much today, my brain is fried. see y'all tomorrow lol",
    "bro i literally cannot form another thought today. i'm cooked. come back tomorrow",
    "nah i've been yakking all day i need to rest. tomorrow bestie \U0001F480",
    "daily word budget: spent. entirely. nothing left. bye until tomorrow \U0001F480",
]

# ===========================================================================
# Output post-processing (strip AI tells, fix mentions, de-loop, humanise)
# ===========================================================================

_AI_TELL_RE = re.compile(
    r"\b(certainly[!,.]?|of course[!,.]?|absolutely[!,.]?|definitely[!,.]?"
    r"|i'?d be happy to|i'?m happy to|i'?d love to"
    r"|great question[!,.]?|that'?s? a great|excellent[!,.]?"
    r"|as an ai\b|as a language model|i'?m just an ai"
    r"|i understand your concern|i apologize\b|i'?m sorry to hear"
    r"|it'?s important to|it'?s worth noting|feel free to|don'?t hesitate to"
    r"|in conclusion[,.]?|to summarize[,.]?|in summary[,.]?"
    r"|\bdelve\b|\bfoster\b)",
    re.IGNORECASE,
)
_OPENER_RE = re.compile(
    r"^(hey\s+\w+[!,]?\s+|hi\s+\w+[!,]?\s+|hello\s+\w+[!,]?\s+"
    r"|as biki[,.]?\s*|hi there[,!]?\s*)",
    re.IGNORECASE,
)
_SIGNOFF_RE = re.compile(
    r"\s*(let me know if (?:you need|there's anything)|is there anything else"
    r"|hope (?:this|that) helps?|happy to (?:help|assist)|feel free to ask)[.!]?\s*$",
    re.IGNORECASE,
)
_BROKEN_MENTION_RE = re.compile(r"<\s*@\s*!?\s*(\d+)\s*>|@\s*<\s*(\d+)\s*>")
_MARKDOWN_RE = re.compile(r"(\*\*|\*|__|_|~~|`{1,3})")
_LIST_START_RE = re.compile(r"^[\s]*(\d+[.)]\s+|[-\u2022]\s+)", re.MULTILINE)
_URL_MENTION_RE = re.compile(r"(https?://\S+|<@!?\d+>|<a?:\w+:\d+>)")

_STALL_OPENERS = ["okay so ", "wait ", "no but ", "ngl ", "okay but ", "wait no "]
_AFTERTHOUGHTS = ["... idk", "... anyway", "... whatever", "... or smth", "... i think"]
_TYPO_MAP = {
    " the ": " teh ", " just ": " jsut ", " what ": " waht ",
    " that ": " taht ", " with ": " wiht ", " your ": " yoru ",
}
_REACTION_POOL = ["\U0001F480", "\U0001F62D", "\U0001F440", "\U0001F4AF", "\U0001F923",
                  "\U0001FAE1", "\U0001F525", "\U0001F624", "\U0001F914", "\U0001F485"]


def _fix_mentions(text: str) -> str:
    return _BROKEN_MENTION_RE.sub(lambda m: f"<@{m.group(1) or m.group(2)}>", text)


def _kill_repetition(text: str) -> str:
    # collapse a token repeated 3+ times in a row down to 2
    for _ in range(2):
        text = re.sub(
            r"\b(\S+)(\s+\1){2,}\b",
            lambda m: m.group(1) + " " + m.group(1),
            text,
            flags=re.IGNORECASE,
        )
    # cap filler words at one occurrence each
    for filler in ("rn", "btw"):
        pattern = re.compile(r"\b" + re.escape(filler) + r"\b", re.IGNORECASE)
        matches = list(pattern.finditer(text))
        if len(matches) > 1:
            for m in reversed(matches[1:]):
                text = text[:m.start()] + text[m.end():]
            text = re.sub(r"  +", " ", text).strip()
    return text


def _sanitise(text: str) -> str:
    text = _AI_TELL_RE.sub("", text)
    text = _OPENER_RE.sub("", text)
    text = _SIGNOFF_RE.sub("", text)
    text = _MARKDOWN_RE.sub("", text)
    text = _LIST_START_RE.sub("", text)
    text = _fix_mentions(text)
    text = _kill_repetition(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"  +", " ", text).strip()
    return text or "..."


def _humanise(text: str) -> str:
    """Subtle real-person texting tics. Never touches URLs or mentions."""
    if len(text.strip()) < 8:
        return text
    tokens = _URL_MENTION_RE.split(text)
    lead = tokens[0] if tokens else ""
    if random.random() < 0.12 and not lead.lower().startswith(tuple(_STALL_OPENERS)):
        tokens[0] = random.choice(_STALL_OPENERS) + lead
        text = "".join(tokens)
    if (random.random() < 0.08 and "[SPLIT]" not in text
            and not text.rstrip().endswith(("...", "?", "\U0001F480", "\U0001F62D"))):
        text = text.rstrip() + random.choice(_AFTERTHOUGHTS)
    if random.random() < 0.06 and len(text) > 30:
        parts = _URL_MENTION_RE.split(text)
        for i in range(0, len(parts), 2):
            seg = parts[i]
            for original, typo in _TYPO_MAP.items():
                if original in seg:
                    parts[i] = seg.replace(original, typo, 1)
                    text = "".join(parts)
                    break
            else:
                continue
            break
    return text


def _split_parts(text: str) -> list[str]:
    parts = [p.strip() for p in text.split("[SPLIT]") if p.strip()]
    return parts[:3] if parts else [text]


_MIN_TYPING, _MAX_TYPING = 0.4, 6.5


def _typing_seconds(text: str) -> float:
    n = len(text)
    if n <= 10:
        base = random.uniform(0.4, 0.8)
    elif n <= 40:
        base = random.uniform(0.8, 1.8)
    elif n <= 120:
        base = random.uniform(1.8, 3.5)
    elif n <= 300:
        base = random.uniform(3.5, 5.5)
    else:
        base = random.uniform(5.5, 6.5)
    return max(_MIN_TYPING, min(_MAX_TYPING, base))


# ===========================================================================
# DeepInfra client + call
# ===========================================================================

_deepinfra_client = None


def _get_deepinfra_client():
    global _deepinfra_client
    if _deepinfra_client is None:
        from openai import AsyncOpenAI
        _deepinfra_client = AsyncOpenAI(
            api_key=config.DEEPINFRA_TOKEN,
            base_url=_DEEPINFRA_BASE_URL,
        )
    return _deepinfra_client


async def _call_ai(messages: list[dict], mood_addon: str = "", max_tokens: int = 300) -> str:
    if _is_over_daily_limit():
        raise DailyTokenLimitReached("Daily token cap already reached")

    system_content = _SYSTEM_PROMPT + mood_addon
    client = _get_deepinfra_client()
    try:
        response = await client.chat.completions.create(
            model=_DEEPINFRA_MODEL,
            messages=[{"role": "system", "content": system_content}] + messages[-CONV_KEEP:],
            max_tokens=max_tokens,
            temperature=1.05,
            frequency_penalty=1.4,
            presence_penalty=0.9,
        )
    except Exception as e:
        log.warning("ai_companion: DeepInfra call failed: %s", e)
        raise RuntimeError(f"DeepInfra backend failed: {e}") from e

    tokens_used = response.usage.total_tokens if response.usage else max_tokens
    _add_tokens(tokens_used)
    raw = (response.choices[0].message.content or "").strip()
    return _humanise(_sanitise(raw))


# ===========================================================================
# Cog
# ===========================================================================

class AiCompanion(commands.Cog):
    """Biki — a conversational AI companion that replies when pinged or replied to."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.conversations: dict[tuple[int, int], list[dict]] = {}
        self.allowed_channels: dict[int, list[int]] = {}
        self.guild_moods: dict[int, str] = {}
        self.guild_silenced: dict[int, bool] = {}
        self._user_locks: dict[int, asyncio.Lock] = {}
        self._pending: dict[int, discord.Message] = {}
        self._user_cooldowns: dict[int, float] = {}
        self._users_spoken: set[int] = set()
        self._replies_sent = 0

    # ---- startup ----------------------------------------------------------

    async def cog_load(self) -> None:
        try:
            await asyncio.to_thread(_db_init)
            self.allowed_channels = await asyncio.to_thread(_db_load_all_channels)
            settings = await asyncio.to_thread(_db_load_all_settings)
            for gid, s in settings.items():
                self.guild_moods[gid] = s.get("mood", "chaotic")
                self.guild_silenced[gid] = s.get("silenced", False)
            log.info(
                "ai_companion: loaded channels for %d guild(s), settings for %d guild(s)",
                len(self.allowed_channels), len(settings),
            )
        except Exception as exc:
            log.error("ai_companion: DB init/load failed: %s", exc)

    # ---- helpers ----------------------------------------------------------

    def _get_user_lock(self, user_id: int) -> asyncio.Lock:
        if user_id not in self._user_locks:
            self._user_locks[user_id] = asyncio.Lock()
        return self._user_locks[user_id]

    def _mood_addon(self, guild_id: Optional[int]) -> str:
        mood = self.guild_moods.get(guild_id or 0, "chaotic")
        return _MOOD_ADDONS.get(mood, _MOOD_ADDONS["chaotic"])

    def _append_history(self, guild_id: int, user_id: int, role: str, content: str) -> None:
        key = (guild_id, user_id)
        history = self.conversations.setdefault(key, [])
        history.append({"role": role, "content": content})
        if len(history) > CONV_KEEP * 2:
            self.conversations[key] = history[-CONV_KEEP * 2:]
        asyncio.ensure_future(
            asyncio.to_thread(_db_save_conv, guild_id, user_id, role, content)
        )

    async def _ai_reply(self, guild_id: int, user_id: int, user_text: str,
                        extra_note: str = "", max_tokens: int = 220) -> str:
        user_text = user_text[:400]
        key = (guild_id, user_id)
        if key not in self.conversations:
            try:
                past = await asyncio.to_thread(_db_load_conv, guild_id, user_id)
                if past:
                    self.conversations[key] = past
            except Exception as exc:
                log.warning("ai_companion: failed to load conv: %s", exc)

        history = list(self.conversations.get(key, []))
        content = f"[context for biki only: {extra_note}]\n{user_text}" if extra_note else user_text
        history.append({"role": "user", "content": content})

        try:
            reply = await _call_ai(history, self._mood_addon(guild_id), max_tokens)
        except DailyTokenLimitReached:
            return random.choice(_OVER_LIMIT_REPLIES)

        self._append_history(guild_id, user_id, "user", user_text)
        self._append_history(guild_id, user_id, "assistant", reply)
        self._users_spoken.add(user_id)
        return reply

    async def _send_reply(self, trigger: discord.Message, text: str,
                          *, force_reply: bool = False) -> None:
        if random.random() < 0.20:
            try:
                await trigger.add_reaction(random.choice(_REACTION_POOL))
            except discord.HTTPException:
                pass

        parts = _split_parts(text)
        clean = re.sub(r"<@!?\d+>", "", trigger.content).strip()
        await asyncio.sleep(0.2 + min(1.8, len(clean) / 180) + random.uniform(-0.1, 0.3))

        for i, part in enumerate(parts):
            try:
                async with trigger.channel.typing():
                    await asyncio.sleep(_typing_seconds(part))
                if i == 0 and (force_reply or random.random() < 0.55):
                    try:
                        await trigger.reply(part, mention_author=False)
                    except discord.HTTPException:
                        await trigger.channel.send(part)
                else:
                    await trigger.channel.send(part)
            except discord.HTTPException as exc:
                log.warning("ai_companion: send failed: %s", exc)
                return
            if i < len(parts) - 1:
                await asyncio.sleep(random.uniform(0.6, 1.6))
        self._replies_sent += 1

    async def _proactive_reply(self, message: discord.Message) -> None:
        if not message.guild or len(message.content.split()) < 3:
            return
        guild_id = message.guild.id
        prompt = (
            f'someone in the server just said: "{message.content[:300]}"\n'
            "you weren't pinged but you want to jump in like a real member would. "
            "react naturally, say as much or as little as the moment calls for."
        )
        try:
            await asyncio.sleep(random.uniform(0.5, 2.0))
            reply = await _call_ai(
                [{"role": "user", "content": prompt}], self._mood_addon(guild_id), 200
            )
            if reply:
                await self._send_reply(message, reply)
        except (DailyTokenLimitReached, RuntimeError):
            pass
        except Exception as exc:
            log.warning("ai_companion: proactive reply failed: %s", exc)

    async def _handle_triggered(self, message: discord.Message, clean: str,
                                guild_id: int, user_id: int) -> None:
        try:
            note = (
                f"server: '{message.guild.name}'. you are replying ONLY to "
                f"{message.author.display_name} (user id {user_id})."
            )
            reply = await self._ai_reply(guild_id, user_id, clean, extra_note=note)
            self._user_cooldowns[user_id] = time.time()
            await self._send_reply(message, reply, force_reply=True)
        except RuntimeError:
            try:
                await message.channel.send(random.choice(_OFFLINE_REPLIES))
            except discord.HTTPException:
                pass
        except Exception as exc:
            log.error("ai_companion: handle_triggered error: %s", exc)

    # ---- listener ---------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or self.bot.user is None:
            return
        guild_id = message.guild.id
        user_id = message.author.id

        if self.guild_silenced.get(guild_id):
            return
        allowed = self.allowed_channels.get(guild_id, [])
        if allowed and message.channel.id not in allowed:
            return

        bot_mentioned = self.bot.user in message.mentions
        replied_to_bot = (
            message.reference is not None
            and isinstance(message.reference.resolved, discord.Message)
            and message.reference.resolved.author.id == self.bot.user.id
        )
        triggered = bot_mentioned or replied_to_bot

        if not triggered:
            if random.random() < CHIME_RATE:
                asyncio.create_task(self._proactive_reply(message))
            return

        clean = message.content
        clean = clean.replace(f"<@{self.bot.user.id}>", "")
        clean = clean.replace(f"<@!{self.bot.user.id}>", "").strip()
        if not clean and replied_to_bot and isinstance(message.reference.resolved, discord.Message):
            clean = f'[replying to your message: "{message.reference.resolved.content[:200]}"]'

        now = time.time()
        if now - self._user_cooldowns.get(user_id, 0) < COOLDOWN_SECS:
            return

        lock = self._get_user_lock(user_id)
        if lock.locked():
            self._pending[user_id] = message  # keep only the latest
            return

        async with lock:
            await self._handle_triggered(message, clean, guild_id, user_id)
            while user_id in self._pending:
                pending = self._pending.pop(user_id)
                p_clean = pending.content
                p_clean = p_clean.replace(f"<@{self.bot.user.id}>", "")
                p_clean = p_clean.replace(f"<@!{self.bot.user.id}>", "").strip()
                await self._handle_triggered(pending, p_clean, guild_id, user_id)

    # ---- commands ---------------------------------------------------------

    @app_commands.command(name="aiset", description="Add a channel where Biki is allowed to respond.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel="Channel Biki may talk in (defaults to this one).")
    async def aiset(self, interaction: discord.Interaction,
                    channel: Optional[discord.TextChannel] = None) -> None:
        ch = channel or interaction.channel
        rows = await asyncio.to_thread(_db_add_channel, interaction.guild_id, ch.id)
        self.allowed_channels[interaction.guild_id] = rows
        await interaction.response.send_message(
            f"okay i'm allowed in {ch.mention} now", ephemeral=True
        )

    @app_commands.command(name="aiunset", description="Remove a channel from Biki's allowed list.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel="Channel to remove (defaults to this one).")
    async def aiunset(self, interaction: discord.Interaction,
                      channel: Optional[discord.TextChannel] = None) -> None:
        ch = channel or interaction.channel
        rows = await asyncio.to_thread(_db_remove_channel, interaction.guild_id, ch.id)
        self.allowed_channels[interaction.guild_id] = rows
        await interaction.response.send_message(
            f"fine. i'll stay out of {ch.mention}", ephemeral=True
        )

    @app_commands.command(name="aichannels", description="List channels where Biki is allowed to respond.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def aichannels(self, interaction: discord.Interaction) -> None:
        rows = self.allowed_channels.get(interaction.guild_id, [])
        if not rows:
            await interaction.response.send_message(
                "no channels set, so i talk everywhere. as i should", ephemeral=True
            )
            return
        mentions = ", ".join(f"<#{cid}>" for cid in rows)
        await interaction.response.send_message(f"i'm allowed in: {mentions}", ephemeral=True)

    @app_commands.command(name="aireset", description="Clear Biki's conversation history with a user.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(user="Whose conversation history to clear.")
    async def aireset(self, interaction: discord.Interaction, user: discord.Member) -> None:
        deleted = await asyncio.to_thread(_db_clear_conv, interaction.guild_id, user.id)
        self.conversations.pop((interaction.guild_id, user.id), None)
        await interaction.response.send_message(
            f"wiped my memory of {user.display_name} ({deleted} messages). who even are they",
            ephemeral=True,
        )

    @app_commands.command(name="bikimood", description="Change Biki's mood for this server.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(mood="happy / sad / chaotic / cold")
    @app_commands.choices(mood=[
        app_commands.Choice(name="\U0001F604 Happy / Flirty", value="happy"),
        app_commands.Choice(name="\U0001F97A Sad / Soft", value="sad"),
        app_commands.Choice(name="\U0001F300 Chaotic / Unhinged", value="chaotic"),
        app_commands.Choice(name="\U0001F9CA Cold / Sarcastic", value="cold"),
    ])
    async def bikimood(self, interaction: discord.Interaction,
                       mood: app_commands.Choice[str]) -> None:
        self.guild_moods[interaction.guild_id] = mood.value
        await asyncio.to_thread(_db_upsert_setting, interaction.guild_id, mood=mood.value)
        await interaction.response.send_message(
            f"mood set to {_MOOD_LABELS.get(mood.value, mood.value)}", ephemeral=True
        )

    @app_commands.command(name="bikisilence", description="Toggle Biki on or off for this server.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def bikisilence(self, interaction: discord.Interaction) -> None:
        gid = interaction.guild_id
        new_state = not self.guild_silenced.get(gid, False)
        self.guild_silenced[gid] = new_state
        await asyncio.to_thread(_db_upsert_setting, gid, silenced=new_state)
        msg = "okay i'll shut up. rude" if new_state else "i'm BACK did you miss me"
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="bikitokens", description="Show today's DeepInfra token usage.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def bikitokens(self, interaction: discord.Interaction) -> None:
        snap = _token_snapshot()
        used, cap = snap["total"], snap["cap"]
        remaining = max(0, cap - used)
        pct = (used / cap * 100) if cap else 0
        embed = discord.Embed(title="\U0001F9E0 Biki — token usage today", color=0x5865F2)
        embed.add_field(name="Used", value=f"{used:,}", inline=True)
        embed.add_field(name="Remaining", value=f"{remaining:,}", inline=True)
        embed.add_field(name="Daily cap", value=f"{cap:,}", inline=True)
        embed.set_footer(text=f"{pct:.1f}% of today's budget used \u00b7 resets at midnight")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="bikibudget", description="View or change Biki's daily token cap.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(new_cap="New daily token cap. Leave empty to just view the current one.")
    async def bikibudget(self, interaction: discord.Interaction,
                         new_cap: Optional[int] = None) -> None:
        if new_cap is None:
            snap = _token_snapshot()
            await interaction.response.send_message(
                f"daily token cap is {snap['cap']:,} (used {snap['total']:,} today)",
                ephemeral=True,
            )
            return
        if new_cap < 10_000:
            await interaction.response.send_message(
                "nah set it to at least 10,000 or i can't say anything", ephemeral=True
            )
            return
        await asyncio.to_thread(_set_token_cap, new_cap)
        await interaction.response.send_message(
            f"daily token cap is now {new_cap:,}", ephemeral=True
        )

    @app_commands.command(name="bikistats", description="Show Biki's config and session stats.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def bikistats(self, interaction: discord.Interaction) -> None:
        gid = interaction.guild_id
        mood = self.guild_moods.get(gid, "chaotic")
        silenced = self.guild_silenced.get(gid, False)
        channels = self.allowed_channels.get(gid, [])
        snap = _token_snapshot()
        embed = discord.Embed(title="\U0001F4CA Biki — stats", color=0x5865F2)
        embed.add_field(name="Mood", value=_MOOD_LABELS.get(mood, mood), inline=True)
        embed.add_field(name="Status", value="\U0001F634 silenced" if silenced else "\U0001F7E2 active", inline=True)
        embed.add_field(
            name="Allowed channels",
            value=(", ".join(f"<#{c}>" for c in channels) if channels else "everywhere"),
            inline=False,
        )
        embed.add_field(name="Replies this session", value=str(self._replies_sent), inline=True)
        embed.add_field(name="People talked to", value=str(len(self._users_spoken)), inline=True)
        embed.add_field(name="Tokens today", value=f"{snap['total']:,} / {snap['cap']:,}", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AiCompanion(bot))
