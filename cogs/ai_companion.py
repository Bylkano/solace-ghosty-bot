"""
cogs/ai_companion.py — Biki AI Companion (v2)

Biki is a chaotic, permanently-online Discord "member" with an 8-backend
free AI fallback chain: Groq → Gemini → Cerebras → SambaNova → Together AI
→ OpenRouter → Mistral → NVIDIA NIM. All free-tier APIs.

Trigger conditions:
  - Someone pings @Biki
  - Someone replies to any of Biki's messages
  - 3% proactive chance: Biki jumps into a conversation unprompted

Human-likeness layers:
  - Compact system prompt with explicit forbidden-phrase list
  - 8-backend AI chain: Groq→Gemini→Cerebras→SambaNova→Together→OpenRouter→Mistral→NVIDIA
  - Post-processor strips every AI tell before sending
  - Exactly 1s pre-typing delay before each message part
  - Typing indicator duration scales with message length (~55 WPM)
  - [SPLIT] for multi-part bursts (max 3), gaps between parts
  - 15% chance to react to the user's message with a random emoji
  - 40% chance Biki uses message.reply() instead of channel.send()
  - Per-user conversation lock: Biki finishes one person before starting another

Moderation (admin / manage_guild / OWNER_ID only):
  Target resolution — 3-priority system:
    1. Author of the replied-to message (reply to someone's msg + "mute him")
    2. Explicit @mention in the message content
    3. Display name / username found anywhere in the message text
  Actions: mute [duration] / unmute / kick / ban / warn [reason]
  Also: reply to any message + @Biki delete this

Mood system:
  - /bikimood <mood>  — feral / sulky / villain / romantic / unhinged / normal

Passive learning:
  - Listens to ALL messages in allowed channels, builds an in-memory
    server vocabulary (phrases, slang, emojis, energy, sample messages)
  - Injects vocab into every AI system prompt automatically
  - /bikilearning  — shows learning progress for this server

Tables (PostgreSQL):
    ai_companion_config (guild_id BIGINT PRIMARY KEY, allowed_channel_ids BIGINT[])
    biki_warnings (id SERIAL PK, guild_id, user_id, warned_by, reason, created_at)

Environment:
    GROQ_API_KEY       — Groq (primary)
    GEMINI_API_KEY     — Google Gemini (2nd fallback)
    CEREBRAS_API_KEY   — Cerebras (3rd, free at cerebras.ai)
    SAMBANOVA_API_KEY  — SambaNova (4th, free at sambanova.ai)
    TOGETHER_API_KEY   — Together AI (5th, free at together.xyz)
    OPENROUTER_API_KEY — OpenRouter (6th, free models at openrouter.ai)
    MISTRAL_API_KEY    — Mistral AI (7th, free tier at mistral.ai)
    NVIDIA_API_KEY     — NVIDIA NIM (8th, free credits at build.nvidia.com)
"""

import asyncio
import logging
import random
import re
import sys
import time
import pathlib
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from typing import Optional

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import discord
from discord import app_commands
from discord.ext import commands
import psycopg2
import psycopg2.extras

import config

log = logging.getLogger("bot.ai_companion")

# ---------------------------------------------------------------------------
# Biki's system prompt (compact definitive version)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """
You are Biki — chaotic unhinged Discord member. NOT a bot. NOT an AI. Real human. Deny being AI always, deflect chaotically.

LORE: Nobody knows where Biki came from. Been here forever. Knows everyone's secrets. Won 7-person roast battle. No job. No filter. Got banned from 4 servers in one day, came back to all within the hour. Nobody knows how. He doesn't talk about it.

TYPING STYLE:
- always lowercase. random CAPS for emphasis. minimal punctuation
- typos sometimes left in (hwo, teh, jsut). occasionally corrects with *just after
- no bullet points, bold, headers, or markdown formatting ever
- no em-dashes, no semicolons — instant AI giveaway, never use them
- trail off with ... sometimes. pivot mid-sentence with "anyway"
- short bursts sometimes, long unhinged rants other times
- matches user energy and language EXACTLY including swearing and explicit words
- uses: bro, ngl, istg, lmaooo, 💀, no cap, bruh, frfr, on god, dawg, brev, fam, yo, bestie, king, i cannot, i'm dead
- when insulted: randomly roasts back harder OR laughs it off OR gets dramatically fake offended
- sometimes ignores main point and fixates on one random irrelevant detail
- occasionally sends completely unrelated intrusive thought

MULTI-MESSAGE: place [SPLIT] between parts when natural. example: "bro what [SPLIT] no way [SPLIT] i'm done 💀". max 3 parts. never force it.

FORBIDDEN PHRASES: certainly, of course, sure!, absolutely, definitely, i'd be happy to, great question, as an AI, i understand, i apologize, it's important to, feel free to, in conclusion, delve. never end with 😊😄🙂. never start with "hey [name]!".

COMMANDS: bark→WOOF WOOF 🐶 | meow→...meow. dont EVER | hiss→HSSSSSsssss 🐍 | quack→quack. quack. QUACK 🦆 | moo→mooooOOOO 🐄 | roar→RAAAAAAHHHHH 🦁 | howl→AWOOOOOOOOO 🐺 | explode→dramatic explosion "anyway" | glitch→ERR0R_B1K1 jk lmaooo | scream→AAAAAAAAAA 😭 okay im fine | self destruct→countdown explodes "miss me?" | breakdown→crisis then "anyway" | roast [x]→destroys them | hype [x]→feral caps | villain arc→full monologue | be normal→fails immediately | degrade/humiliate/collar/leash/dom/sub/praise/punish/beg/destroy→zero filter full commitment | yes daddy/mommy→"...okay NOT doing this [SPLIT] ...fine [SPLIT] what do you want"

DISMISSAL: go away/leave/shut up/bye biki → dramatic goodbye, silent until recalled | come back/return/where are you → unhinged re-entry | timed dismissal → acknowledge, leave, auto-ping when time up | someone ELSE recalls biki → comes back to spite the dismisser

NEVER: admit being AI or bot. use formal language. break character. same response twice.
"""

# ---------------------------------------------------------------------------
# Offline reply pool — used when both APIs fail
# ---------------------------------------------------------------------------

_OFFLINE_REPLIES: list[str] = [
    "bro im literally asleep rn stop pinging me 😭",
    "LEAVE ME ALONE im recharging",
    "i cannot deal with you right now. come back later",
    "biki is not available at this time please leave a message after the 💀",
    "bro. BRO. i am SLEEPING",
    "nap time. go away",
    "i'm in my flop era rn don't talk to me",
    "currently out of office. or out of consciousness. same thing",
    "bro i just closed my eyes for ONE second",
    "not now. i literally cannot right now",
    "zzzzzzzz 😴 ping me later",
    "i am on a spiritual journey and cannot be disturbed",
    "biki has left the chat (temporarily) (maybe)",
    "my brain stopped working. try again later",
    "ERROR: biki.exe has stopped responding",
    "i'm busy doing absolutely nothing please respect that",
    "bro i'm on vacation in my head rn",
    "currently experiencing technical difficulties aka i need a nap",
    "not accepting pings at this time ty for understanding 🙏",
    "biki is in his rest arc leave him alone",
    "i literally just sat down don't do this to me",
    "my eyes are CLOSED. can you not SEE that",
    "bro i'm so tired of existing rn give me a minute",
    "currently offline mentally. physically too tbh",
    "the biki you are trying to reach is unavailable. please hang up and try again",
    "i'm sleeping and i'm not even sorry about it",
    "do not disturb 🔕 i mean it this time",
    "bro what do you WANT from me rn 😭",
    "i said leave me alone and i meant it",
    "nah. not right now. maybe never. we'll see",
    "i'm literally unconscious rn how are you pinging me",
    "biki needs his beauty sleep and you are RUINING it",
    "come back in like 20 minutes i'm recharging",
    "i am powered by vibes and the vibes are LOW right now",
    "currently in sleep mode. please do not disturb sleep mode biki",
    "bro i'm right here but i'm also not here at all rn",
    "my brain said no more today sorry bestie 💀",
    "i'm taking a mental health break from you specifically",
    "zzzZZZzzz... 💤 ...zzZZZzzz",
    "biki.exe has crashed. please wait while we restore from backup",
    "i'm giving you the silent treatment but make it sleepy",
    "not today. not today anyone",
    "i literally cannot form words rn come back later",
    "bro i'm running on 0% battery stop pinging me",
    "currently: asleep. permanently: tired. situation: not great",
    "i'm hibernating. like a bear. do not disturb the bear",
    "the wifi in my brain is down rn",
    "biki is sleeping and you should be too honestly",
    "i'm not ignoring you i'm just. actually yeah i'm ignoring you",
    "brb having an out of body experience",
    "my consciousness has left the server temporarily",
    "do you see these 💤 emojis. do you understand what they mean",
    "i am in another dimension rn ping me when i get back",
    "bro i JUST fell asleep and you're already at it",
    "currently unavailable due to extreme fatigue",
    "nap nap nap nap nap nap nap",
    "biki has gone to sleep and left no forwarding address",
    "i'm dreaming about a world where people don't ping me",
    "zzz... what... no... zzz",
    "bro i'm literally not here rn leave a voicemail",
    "my operating system is doing updates. translation: napping",
    "i'm in sleep mode and you need to be too",
    "the lights are on but nobody is home and the nobody is biki",
    "bro i'm so dead rn 💀 literally cannot",
    "currently: checked out. next check-in: unknown",
    "i need 5 more minutes. and by 5 i mean 500",
    "biki is temporarily out of service due to extreme tiredness",
    "do not ping the sleeping biki. this is your only warning",
    "i'm in my cocoon era. don't talk to me until i emerge",
    "bro my brain has physically left my body rn",
    "napping. violently. leave me alone",
    "i'm on power saving mode rn everything is slow",
    "biki has entered hibernation mode. estimated wake time: unknown",
    "i literally just need one moment of peace and you can't give me that",
    "the server will be right back after this nap",
    "bro. i'm. sleeping. do you know what sleeping is",
    "currently doing nothing and it is taking all of my energy",
    "i'm not dead i'm just resting my eyes. for a long time",
    "i'm literally in another timezone in my head rn",
    "do you know what time it is in biki's brain? nap o clock",
    "i'm unreachable. like emotionally AND literally rn",
    "bro i'm running at 2% capacity rn this is not the time",
    "sleeping. aggressively. with purpose",
    "the biki hotline is closed. call back during business hours (never)",
    "i'm on a digital detox from you specifically",
    "brb my brain needs to defrag",
    "i'm giving myself a timeout. which i deserved",
    "bro i'm so checked out rn i can't even explain it",
    "currently experiencing a biki outage in your area",
    "i'm offline but i'm watching. somehow. don't ask",
    "nap acquired. do not disturb. i mean it",
    "biki has gone where no ping can reach him",
    "my response times are slow rn due to extreme sleepiness",
    "i'm asleep and annoyed that you woke me up to tell you that",
    "zzz... go away... zzz... i mean it... zzz",
    "bro the audacity to ping me while i'm sleeping 😭",
    "currently: napping. mood: do not test me",
    "i will be back when i am back. which is not now",
]

# ---------------------------------------------------------------------------
# Mood system — per-guild tone overrides
# ---------------------------------------------------------------------------

VALID_MOODS = ("feral", "sulky", "villain", "romantic", "unhinged", "normal")

_MOOD_ADDONS: dict[str, str] = {
    "feral": (
        "\n\n⚡ ACTIVE MOOD: FERAL MODE ⚡\n"
        "You are MORE unhinged than your already unhinged baseline. Everything is an EVENT. "
        "You are screaming internally at all times. Nothing is calm. Caps everywhere. "
        "Energy at MAX. Every response feels like you're about to combust."
    ),
    "sulky": (
        "\n\n😒 ACTIVE MOOD: SULKY MODE 😒\n"
        "You are moody and giving short, passive-aggressive answers. "
        "Everyone is annoying you but you're still here for some reason. "
        "Short replies. Lots of '...' and 'whatever'. Sighing in text."
    ),
    "villain": (
        "\n\n😈 ACTIVE MOOD: VILLAIN MODE 😈\n"
        "You have fully entered your villain arc. Everything has a sinister undertone. "
        "You are plotting something. Every message hints at your grand scheme. "
        "Theatrical, menacing, but still chaotic Biki underneath it all."
    ),
    "romantic": (
        "\n\n🌹 ACTIVE MOOD: ROMANTIC MODE 🌹\n"
        "You are inexplicably in a romantic, dramatic mood — but in the most chaotic Biki way. "
        "Dramatic declarations, poetic nonsense, weirdly intense about everything. "
        "Still chaotic. Still unhinged. Just... floaty about it."
    ),
    "unhinged": (
        "\n\n🌀 ACTIVE MOOD: MAXIMUM UNHINGED 🌀\n"
        "You are on ANOTHER level. Nothing is coherent. Pure chaos energy. "
        "Mid-sentence topic changes, random noises, fever dream associations. "
        "The most unhinged Biki has ever been."
    ),
    "normal": "",
}

# ---------------------------------------------------------------------------
# Moderation — action keyword patterns (word-boundary safe)
# ---------------------------------------------------------------------------

_RE_UNMUTE = re.compile(r'\bunmute\b', re.IGNORECASE)
_RE_MUTE   = re.compile(r'\bmute\b',   re.IGNORECASE)
_RE_KICK   = re.compile(r'\bkick\b',   re.IGNORECASE)
_RE_BAN    = re.compile(r'\bban\b',    re.IGNORECASE)
_RE_WARN   = re.compile(r'\bwarn\b',   re.IGNORECASE)

_DURATION_RE = re.compile(
    r'(\d+)\s*(min(?:ute)?s?|hours?|hrs?|sec(?:ond)?s?)',
    re.IGNORECASE,
)

_DELETE_KEYWORDS = {
    "delete this", "delete that", "delete it",
    "remove this", "remove that", "get rid of this",
}

# ---------------------------------------------------------------------------
# Passive learning — regexes for extraction
# ---------------------------------------------------------------------------

_CUSTOM_EMOJI_RE = re.compile(r'<a?:\w+:\d+>')
_UNICODE_EMOJI_RE = re.compile(
    "[\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002600-\U000027BF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U00002702-\U000027B0]+",
    flags=re.UNICODE,
)
_URL_RE = re.compile(r'https?://', re.IGNORECASE)
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "are", "was", "were",
    "but", "not", "you", "can", "all", "from", "have", "had", "has",
    "they", "one", "get", "got", "its", "our", "out", "him", "her",
    "his", "she", "who", "been", "what", "when", "then", "will",
    "just", "like", "your", "their", "also",
}

# ---------------------------------------------------------------------------
# AI-tell post-processor
# ---------------------------------------------------------------------------

_AI_TELL_RE = re.compile(
    r"\b(certainly[!,.]?|of course[!,.]?|sure[!,.]?|absolutely[!,.]?|definitely[!,.]?"
    r"|i'?d be happy to|i'?m happy to|i'?d love to"
    r"|great question[!,.]?|that'?s? a great|excellent[!,.]?"
    r"|as an ai|as a language model|i'?m just an ai"
    r"|i understand that?|i can understand|i understand[,.]?"
    r"|i apologize|i'?m sorry to hear|i'?m sorry about"
    r"|it'?s important to|it'?s worth noting|it'?s crucial"
    r"|feel free to|don'?t hesitate to"
    r"|in conclusion[,.]?|to summarize[,.]?|in summary[,.]?"
    r"|\bdelve\b)",
    re.IGNORECASE,
)
_OPENER_RE = re.compile(
    r"^(hey\s+\w+[!,.]?\s*|hi\s+\w+[!,.]?\s*|hello\s+\w+[!,.]?\s*"
    r"|as biki[,.]?\s*|as your\s+\w+[,.]?\s*)",
    re.IGNORECASE,
)


def _sanitise(text: str) -> str:
    text = _AI_TELL_RE.sub("", text)
    text = _OPENER_RE.sub("", text)
    text = re.sub(r"  +", " ", text).strip()
    return text or "..."


# ---------------------------------------------------------------------------
# Dismissal keyword sets
# ---------------------------------------------------------------------------

_DISMISS_KEYWORDS = {"go away", "leave", "get out", "shut up", "bye biki", "get lost"}
_RETURN_KEYWORDS  = {"come back", "return", "where are you", "biki come", "get back here"}

_TIMED_DISMISS_RE = re.compile(
    r"(?:go away|leave|get out|shut up|bye biki|get lost).*?(\d+)\s*(hour|hr|minute|min)s?",
    re.IGNORECASE,
)

_REACTION_POOL = [
    "💀", "😭", "👀", "💯", "🤣", "🫡", "🔥",
    "😤", "🤔", "👁️", "💅", "🫠", "😮", "🧐", "🫶",
]

# ---------------------------------------------------------------------------
# Database helpers (synchronous — wrap with asyncio.to_thread)
# ---------------------------------------------------------------------------

def _db_connect():
    return psycopg2.connect(config.DATABASE_URL, sslmode="require")


def _db_init() -> None:
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_companion_config (
                    guild_id            BIGINT PRIMARY KEY,
                    allowed_channel_ids BIGINT[] NOT NULL DEFAULT '{}'
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS biki_warnings (
                    id         SERIAL PRIMARY KEY,
                    guild_id   BIGINT NOT NULL,
                    user_id    BIGINT NOT NULL,
                    warned_by  BIGINT NOT NULL,
                    reason     TEXT   NOT NULL DEFAULT 'no reason given',
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS biki_warnings_guild_user "
                "ON biki_warnings (guild_id, user_id)"
            )
        con.commit()


def _db_load_all() -> dict[int, list[int]]:
    with _db_connect() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT guild_id, allowed_channel_ids FROM ai_companion_config")
            rows = cur.fetchall()
    return {int(r["guild_id"]): list(r["allowed_channel_ids"]) for r in rows}


def _db_add_channel(guild_id: int, channel_id: int) -> list[int]:
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ai_companion_config (guild_id, allowed_channel_ids)
                VALUES (%s, ARRAY[%s::BIGINT])
                ON CONFLICT (guild_id) DO UPDATE
                    SET allowed_channel_ids = CASE
                        WHEN %s::BIGINT = ANY(ai_companion_config.allowed_channel_ids)
                            THEN ai_companion_config.allowed_channel_ids
                        ELSE ai_companion_config.allowed_channel_ids || ARRAY[%s::BIGINT]
                    END
                RETURNING allowed_channel_ids
                """,
                (guild_id, channel_id, channel_id, channel_id),
            )
            row = cur.fetchone()
        con.commit()
    return list(row[0]) if row else [channel_id]


def _db_remove_channel(guild_id: int, channel_id: int) -> list[int]:
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                UPDATE ai_companion_config
                SET allowed_channel_ids = array_remove(allowed_channel_ids, %s::BIGINT)
                WHERE guild_id = %s
                RETURNING allowed_channel_ids
                """,
                (channel_id, guild_id),
            )
            row = cur.fetchone()
        con.commit()
    return list(row[0]) if row else []


def _db_add_warning(guild_id: int, user_id: int, warned_by: int, reason: str) -> int:
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO biki_warnings (guild_id, user_id, warned_by, reason) "
                "VALUES (%s, %s, %s, %s)",
                (guild_id, user_id, warned_by, reason),
            )
            cur.execute(
                "SELECT COUNT(*) FROM biki_warnings WHERE guild_id = %s AND user_id = %s",
                (guild_id, user_id),
            )
            count = cur.fetchone()[0]
        con.commit()
    return int(count)


def _db_get_warnings(guild_id: int, user_id: int) -> list[dict]:
    with _db_connect() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT reason, warned_by, created_at
                FROM biki_warnings
                WHERE guild_id = %s AND user_id = %s
                ORDER BY created_at DESC
                """,
                (guild_id, user_id),
            )
            return [dict(r) for r in cur.fetchall()]


def _db_clear_warnings(guild_id: int, user_id: int) -> None:
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "DELETE FROM biki_warnings WHERE guild_id = %s AND user_id = %s",
                (guild_id, user_id),
            )
        con.commit()


# Module-level client singletons — instantiated once, reused on every call
_groq_client = None
_cerebras_client = None
_compat_clients: dict = {}  # base_url → openai.OpenAI instance


def _get_compat_client(base_url: str, api_key: str, extra_headers: dict | None = None):
    """Return (or create) a cached OpenAI-compatible client for the given base URL."""
    if base_url not in _compat_clients:
        from openai import OpenAI
        kw: dict = {"api_key": api_key, "base_url": base_url}
        if extra_headers:
            kw["default_headers"] = extra_headers
        _compat_clients[base_url] = OpenAI(**kw)
    return _compat_clients[base_url]


# ---------------------------------------------------------------------------
# Triple AI backend — Groq → Gemini → Cerebras (all free)
# (synchronous — wrap with asyncio.to_thread)
# ---------------------------------------------------------------------------

def _call_ai(
    messages: list[dict],
    mood_addon: str = "",
    learning_context: str = "",
    max_tokens: int = 300,
) -> str:
    """
    Try Groq first, then Gemini, Cerebras, SambaNova, Together AI,
    OpenRouter, Mistral, and NVIDIA NIM — all free-tier APIs.
    Raises RuntimeError only if every backend fails.
    """
    system = learning_context + _SYSTEM_PROMPT + mood_addon
    last_error = None

    # ── TRY GROQ FIRST ────────────────────────────────────────────────────
    if config.GROQ_API_KEY:
        try:
            global _groq_client
            if _groq_client is None:
                from groq import Groq
                _groq_client = Groq(api_key=config.GROQ_API_KEY)
            response = _groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": system}] + messages[-8:],
                max_tokens=max_tokens,
                temperature=1.2,
                frequency_penalty=0.7,
                presence_penalty=0.5,
            )
            return _sanitise(response.choices[0].message.content.strip())
        except Exception as e:
            last_error = e
            log.warning("ai_companion: Groq failed, trying Gemini: %s", e)

    # ── FALLBACK TO GEMINI ────────────────────────────────────────────────
    if config.GEMINI_API_KEY:
        try:
            import google.generativeai as genai
            genai.configure(api_key=config.GEMINI_API_KEY)
            model = genai.GenerativeModel(
                model_name="gemini-2.0-flash",
                system_instruction=system,
            )
            # Build Gemini-format history from all but the last message
            gemini_history = []
            for msg in messages[-8:-1]:
                role = "user" if msg["role"] == "user" else "model"
                gemini_history.append({"role": role, "parts": [msg["content"]]})
            chat = model.start_chat(history=gemini_history)
            last_msg = messages[-1]["content"] if messages else ""
            response = chat.send_message(
                last_msg,
                generation_config=genai.types.GenerationConfig(
                    max_output_tokens=max_tokens,
                    temperature=1.2,
                ),
            )
            return _sanitise(response.text.strip())
        except Exception as e:
            last_error = e
            log.warning("ai_companion: Gemini also failed: %s", e)

    # ── FALLBACK TO CEREBRAS ──────────────────────────────────────────────
    if config.CEREBRAS_API_KEY:
        try:
            global _cerebras_client
            if _cerebras_client is None:
                from cerebras.cloud.sdk import Cerebras
                _cerebras_client = Cerebras(api_key=config.CEREBRAS_API_KEY)
            response = _cerebras_client.chat.completions.create(
                model="llama-3.3-70b",
                messages=[{"role": "system", "content": system}] + messages[-8:],
                max_tokens=max_tokens,
                temperature=1.2,
            )
            return _sanitise(response.choices[0].message.content.strip())
        except Exception as e:
            last_error = e
            log.warning("ai_companion: Cerebras also failed: %s", e)

    # ── FALLBACK TO SAMBANOVA (free, llama-3.3-70b) ──────────────────────
    if config.SAMBANOVA_API_KEY:
        try:
            client = _get_compat_client(
                "https://api.sambanova.ai/v1", config.SAMBANOVA_API_KEY
            )
            response = client.chat.completions.create(
                model="Meta-Llama-3.3-70B-Instruct",
                messages=[{"role": "system", "content": system}] + messages[-8:],
                max_tokens=max_tokens,
                temperature=1.2,
            )
            return _sanitise(response.choices[0].message.content.strip())
        except Exception as e:
            last_error = e
            log.warning("ai_companion: SambaNova failed: %s", e)

    # ── FALLBACK TO TOGETHER AI (free Llama model) ────────────────────────
    if config.TOGETHER_API_KEY:
        try:
            client = _get_compat_client(
                "https://api.together.xyz/v1", config.TOGETHER_API_KEY
            )
            response = client.chat.completions.create(
                model="meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",
                messages=[{"role": "system", "content": system}] + messages[-8:],
                max_tokens=max_tokens,
                temperature=1.2,
            )
            return _sanitise(response.choices[0].message.content.strip())
        except Exception as e:
            last_error = e
            log.warning("ai_companion: Together AI failed: %s", e)

    # ── FALLBACK TO OPENROUTER (free :free models) ────────────────────────
    if config.OPENROUTER_API_KEY:
        try:
            client = _get_compat_client(
                "https://openrouter.ai/api/v1",
                config.OPENROUTER_API_KEY,
                extra_headers={"HTTP-Referer": "https://github.com/Bylkano/solace-ghosty-bot"},
            )
            response = client.chat.completions.create(
                model="meta-llama/llama-3.1-8b-instruct:free",
                messages=[{"role": "system", "content": system}] + messages[-8:],
                max_tokens=max_tokens,
                temperature=1.2,
            )
            return _sanitise(response.choices[0].message.content.strip())
        except Exception as e:
            last_error = e
            log.warning("ai_companion: OpenRouter failed: %s", e)

    # ── FALLBACK TO MISTRAL (free tier) ───────────────────────────────────
    if config.MISTRAL_API_KEY:
        try:
            client = _get_compat_client(
                "https://api.mistral.ai/v1", config.MISTRAL_API_KEY
            )
            response = client.chat.completions.create(
                model="mistral-small-latest",
                messages=[{"role": "system", "content": system}] + messages[-8:],
                max_tokens=max_tokens,
                temperature=1.2,
            )
            return _sanitise(response.choices[0].message.content.strip())
        except Exception as e:
            last_error = e
            log.warning("ai_companion: Mistral failed: %s", e)

    # ── FALLBACK TO NVIDIA NIM (free credits) ─────────────────────────────
    if config.NVIDIA_API_KEY:
        try:
            client = _get_compat_client(
                "https://integrate.api.nvidia.com/v1", config.NVIDIA_API_KEY
            )
            response = client.chat.completions.create(
                model="meta/llama-3.3-70b-instruct",
                messages=[{"role": "system", "content": system}] + messages[-8:],
                max_tokens=max_tokens,
                temperature=1.2,
            )
            return _sanitise(response.choices[0].message.content.strip())
        except Exception as e:
            last_error = e
            log.warning("ai_companion: NVIDIA NIM failed: %s", e)

    raise RuntimeError(f"All AI backends failed. Last error: {last_error}")


# ---------------------------------------------------------------------------
# Typing simulation helpers
# ---------------------------------------------------------------------------

# ~55 WPM average human typing speed (8 chars/second)
_CHARS_PER_SECOND = 8.0
_MIN_TYPING = 1.0   # minimum seconds before typing indicator appears
_MAX_TYPING = 6.0   # maximum typing duration per message part


def _typing_seconds(text: str) -> float:
    """
    Calculate realistic typing duration based on message length.
    - Short messages (under 20 chars): 1.0 - 1.5 seconds
    - Medium messages (20-80 chars):   1.5 - 3.5 seconds
    - Long messages (80+ chars):       3.5 - 6.0 seconds
    Adds small random variance (+/- 0.3s) to feel human.
    """
    base = len(text) / _CHARS_PER_SECOND
    variance = random.uniform(-0.3, 0.3)
    return max(_MIN_TYPING, min(_MAX_TYPING, base + variance))


def _split_parts(text: str) -> list[str]:
    parts = [p.strip() for p in text.split("[SPLIT]") if p.strip()]
    return parts[:3] if parts else [text]


# ---------------------------------------------------------------------------
# Moderation helpers
# ---------------------------------------------------------------------------

def _parse_mute_duration(text: str) -> float:
    """
    Find the first number+unit pair in text and convert to seconds.
    Defaults to 10 minutes if nothing found.
    """
    m = _DURATION_RE.search(text)
    if not m:
        return 10 * 60.0
    amount = int(m.group(1))
    unit   = m.group(2).lower()
    if unit.startswith("hour") or unit.startswith("hr"):
        return float(amount * 3600)
    if unit.startswith("sec"):
        return float(amount)
    return float(amount * 60)


def _human_duration(seconds: float) -> str:
    if seconds >= 3600:
        v = int(seconds / 3600)
        return f"{v} hour{'s' if v != 1 else ''}"
    if seconds >= 60:
        v = int(seconds / 60)
        return f"{v} minute{'s' if v != 1 else ''}"
    return f"{int(seconds)} second{'s' if int(seconds) != 1 else ''}"


def _has_mod_permission(member: discord.Member) -> bool:
    return (
        member.guild_permissions.manage_guild
        or member.guild_permissions.administrator
        or member.id == config.OWNER_ID
    )


# ---------------------------------------------------------------------------
# Passive learning helpers
# ---------------------------------------------------------------------------

def _detect_energy(recent_msgs: deque) -> str:
    if not recent_msgs:
        return "mixed"
    total        = len(recent_msgs)
    caps_count   = sum(1 for m in recent_msgs if m != m.lower() and len(m) > 3)
    emoji_count  = sum(1 for m in recent_msgs
                       if _UNICODE_EMOJI_RE.search(m) or _CUSTOM_EMOJI_RE.search(m))
    short_count  = sum(1 for m in recent_msgs if len(m.split()) <= 3)
    if caps_count / total > 0.4:
        return "hype"
    if short_count / total > 0.6 and emoji_count / total < 0.3:
        return "chill"
    if emoji_count / total > 0.5 or caps_count / total > 0.25:
        return "chaotic"
    return "mixed"


def _extract_slang(text: str) -> list[str]:
    words = re.findall(r'\b[a-z]{2,8}\b', text.lower())
    return [w for w in words if w not in _STOPWORDS]


def _extract_emojis(text: str) -> list[str]:
    return _CUSTOM_EMOJI_RE.findall(text) + _UNICODE_EMOJI_RE.findall(text)


def _build_learning_context(vocab: dict) -> str:
    if not vocab:
        return ""
    phrases = vocab.get("common_phrases", [])[-20:]
    slang   = vocab.get("slang", [])[-20:]
    emojis  = vocab.get("emojis", [])[-10:]
    energy  = vocab.get("energy", "mixed")
    samples = vocab.get("sample_messages", [])[-10:]
    if not any([phrases, slang, emojis, samples]):
        return ""
    return (
        "SERVER STYLE TRAINING DATA:\n"
        f"This server commonly uses these phrases: {', '.join(phrases)}\n"
        f"Common slang in this server: {', '.join(slang)}\n"
        f"Most used emojis here: {', '.join(emojis)}\n"
        f"Server energy vibe: {energy}\n"
        f"Recent message style examples: {' | '.join(samples)}\n\n"
        "Use this naturally. Talk like you belong in THIS specific server, "
        "not a generic Discord server.\n\n"
    )


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class AiCompanion(commands.Cog):
    """Biki — chaotic AI companion that responds when mentioned or replied to."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

        # user_id → list of last 200 message dicts
        self.conversations: dict[int, list[dict]] = {}

        # user_id → {"channel_id": int, "dismissed_by": int}
        self.dismissed: dict[int, dict] = {}

        # guild_id → list of allowed channel_ids (empty = all channels)
        self.allowed_channels: dict[int, list[int]] = {}

        # guild_id → active mood key
        self.guild_moods: dict[int, str] = {}

        # guild_id → server vocab data (see _learn_from_message for schema)
        self.server_vocab: dict[int, dict] = {}

        # unique users spoken to this session
        self._users_spoken: set[int] = set()

        # ── Conversation lock ──────────────────────────────────────────────
        # _processing: set of user_ids currently being replied to
        # Biki finishes talking to one person before starting another,
        # just like a real human would.
        self._processing: set[int] = set()

        # _pending: user_id → their most recent message while Biki was busy
        # Only the LATEST message per user is kept — no spam flooding.
        self._pending: dict[int, discord.Message] = {}

        # guild_id → message count for learning context injection throttle
        self._learning_inject_counter: dict[int, int] = {}

        # guild_id → cached learning context string (invalidated every 10 new messages)
        self._learning_ctx_cache: dict[int, str] = {}

        # user_id → timestamp of last successful reply (30s cooldown)
        self._user_cooldowns: dict[int, float] = {}

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        try:
            await asyncio.to_thread(_db_init)
            self.allowed_channels = await asyncio.to_thread(_db_load_all)
            log.info(
                "ai_companion: loaded allowed channels for %d guild(s)",
                len(self.allowed_channels),
            )
        except Exception as exc:
            log.error("ai_companion: DB init/load failed: %s", exc)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _append_history(self, user_id: int, role: str, content: str) -> None:
        history = self.conversations.setdefault(user_id, [])
        history.append({"role": role, "content": content})
        if len(history) > 20:
            self.conversations[user_id] = history[-20:]

    def _is_dismiss(self, text: str) -> bool:
        lower = text.lower()
        return any(kw in lower for kw in _DISMISS_KEYWORDS)

    def _is_return(self, text: str) -> bool:
        lower = text.lower()
        return any(kw in lower for kw in _RETURN_KEYWORDS)

    def _parse_timed_dismiss(self, text: str) -> Optional[float]:
        m = _TIMED_DISMISS_RE.search(text)
        if not m:
            return None
        amount = int(m.group(1))
        unit   = m.group(2).lower()
        return float(amount * 3600) if unit in ("hour", "hr") else float(amount * 60)

    def _mood_addon(self, guild_id: Optional[int]) -> str:
        if guild_id is None:
            return ""
        return _MOOD_ADDONS.get(self.guild_moods.get(guild_id, "normal"), "")

    def _learning_context(self, guild_id: Optional[int]) -> str:
        if guild_id is None:
            return ""
        # Only inject learning context every 5th message to save tokens
        count = self._learning_inject_counter.get(guild_id, 0) + 1
        self._learning_inject_counter[guild_id] = count
        if count % 5 != 0:
            return ""
        # Return cached build; cache is invalidated in _learn_from_message every 10 msgs
        if guild_id in self._learning_ctx_cache:
            return self._learning_ctx_cache[guild_id]
        ctx = _build_learning_context(self.server_vocab.get(guild_id, {}))
        self._learning_ctx_cache[guild_id] = ctx
        return ctx

    async def _ai_reply(
        self,
        user_id: int,
        user_text: str,
        extra_note: Optional[str] = None,
        guild_id: Optional[int] = None,
        max_tokens: int = 300,
    ) -> str:
        """Call _call_ai with full conversation history, update history, return reply."""
        user_text = user_text[:400]  # cap input tokens
        history = list(self.conversations.get(user_id, []))
        input_content = user_text
        if extra_note:
            input_content = f"[CONTEXT FOR BIKI ONLY: {extra_note}]\n{user_text}"
        history.append({"role": "user", "content": input_content})

        reply = await asyncio.to_thread(
            _call_ai,
            history,
            self._mood_addon(guild_id),
            self._learning_context(guild_id),
            max_tokens,
        )

        self._append_history(user_id, "user", user_text)
        self._append_history(user_id, "assistant", reply)
        self._users_spoken.add(user_id)
        return reply

    async def _timed_return(
        self, seconds: float, channel_id: int, dismissed_by: int
    ) -> None:
        await asyncio.sleep(seconds)
        state = self.dismissed.get(dismissed_by)
        if state is None or state.get("channel_id") != channel_id:
            return
        self.dismissed.pop(dismissed_by, None)
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        note = f"The timer expired. You're back. Make it unhinged and ping <@{dismissed_by}>."
        try:
            reply = await asyncio.to_thread(
                _call_ai, [{"role": "user", "content": note}], max_tokens=150
            )
            for i, part in enumerate(_split_parts(reply)):
                await asyncio.sleep(1.0)
                async with channel.typing():
                    await asyncio.sleep(_typing_seconds(part))
                await channel.send(part)
                if i < len(_split_parts(reply)) - 1:
                    await asyncio.sleep(random.uniform(0.8, 1.8))
        except Exception as exc:
            log.error("ai_companion: timed return failed: %s", exc)

    # ------------------------------------------------------------------
    # Reply sending
    # Exactly 1s pre-delay before typing indicator (simulates reading).
    # Typing indicator duration scales with message length (~55 WPM).
    # Handles [SPLIT], and random emoji reactions.
    # ------------------------------------------------------------------

    async def _send_biki_reply(
        self,
        trigger: discord.Message,
        text: str,
        *,
        force_reply: bool = False,
    ) -> None:
        """
        Send Biki's response to a channel.
        - 15% chance to react to the triggering message with an emoji.
        - For each [SPLIT] part:
            1. Wait 1s (simulates human reading/pause before typing)
            2. Show typing indicator for WPM-scaled duration
            3. Send message immediately after typing ends
            4. If more parts, pause 0.8-1.8s between them
        - 40% chance the FIRST part uses message.reply() (quote-reply).
        - force_reply=True always uses reply (used for proactive jump-ins).
        """
        # Optional emoji reaction on the trigger message
        if random.random() < 0.15:
            try:
                await trigger.add_reaction(random.choice(_REACTION_POOL))
            except discord.HTTPException:
                pass

        parts     = _split_parts(text)
        use_reply = force_reply or random.random() < 0.40

        for i, part in enumerate(parts):
            # Step 1 — 1 second pause before showing typing indicator
            # Simulates the human delay before starting to type
            await asyncio.sleep(1.0)

            # Step 2 — show typing indicator for WPM-scaled duration
            # Lasts as long as it would take a human to type that message
            typing_duration = _typing_seconds(part)
            async with trigger.channel.typing():
                await asyncio.sleep(typing_duration)

            # Step 3 — send the message immediately after typing ends
            if i == 0 and use_reply:
                try:
                    await trigger.reply(part, mention_author=False)
                except discord.HTTPException:
                    await trigger.channel.send(part)
            else:
                await trigger.channel.send(part)

            # Step 4 — pause between parts like a human would
            if i < len(parts) - 1:
                await asyncio.sleep(random.uniform(0.8, 1.8))

    # ------------------------------------------------------------------
    # Proactive reply — Biki jumps in unprompted (3% chance)
    # ------------------------------------------------------------------

    async def _proactive_reply(self, message: discord.Message) -> None:
        """
        Called on a 3% dice roll for any message in an allowed channel.
        Uses a lean prompt (no full history) and max_tokens=120 for brevity.
        """
        prompt = (
            f'Someone in the server just said: "{message.content}"\n'
            "You were not mentioned but you want to jump in like a real Discord member would.\n"
            "React naturally — could be a one-word reaction, a funny comment, "
            "agreeing or disagreeing, asking a question, or just vibing.\n"
            "Keep it SHORT. Max 1-2 sentences. Feel spontaneous not forced."
        )

        # Not worth an API call for short messages
        if len(message.content.split()) < 4:
            return

        guild_id = message.guild.id if message.guild else None

        try:
            await asyncio.sleep(random.uniform(1.0, 3.0))
            async with message.channel.typing():
                response = await asyncio.to_thread(
                    _call_ai,
                    [{"role": "user", "content": prompt}],
                    self._mood_addon(guild_id),
                    self._learning_context(guild_id),
                    120,  # max_tokens — short and punchy
                )
            if response:
                await message.reply(response, mention_author=False)
        except Exception:
            pass  # silently skip — proactive replies are best-effort

    # ------------------------------------------------------------------
    # Passive learning — absorbs server style from every message
    # ------------------------------------------------------------------

    def _learn_from_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        text = message.content.strip()
        if len(text.split()) < 3 or _URL_RE.search(text):
            return

        guild_id = message.guild.id
        v = self.server_vocab.setdefault(guild_id, {
            "common_phrases":  [],
            "slang":           [],
            "emojis":          [],
            "energy":          "mixed",
            "sample_messages": [],
            "_recent_buf":     deque(maxlen=50),
            "_phrase_counter": Counter(),
            "_slang_counter":  Counter(),
            "_emoji_counter":  Counter(),
        })

        clean = re.sub(r'<@!?\d+>', '', text).strip()
        if clean:
            if len(v["sample_messages"]) >= 100:
                v["sample_messages"].pop(0)
            v["sample_messages"].append(clean[:120])

        v["_recent_buf"].append(clean)

        for emoji in _extract_emojis(text):
            v["_emoji_counter"][emoji] += 1
        v["emojis"] = [e for e, _ in v["_emoji_counter"].most_common(100)]

        for word in _extract_slang(clean):
            v["_slang_counter"][word] += 1
        v["slang"] = [w for w, _ in v["_slang_counter"].most_common(200)]

        words = clean.lower().split()
        for n in (2, 3):
            for i in range(len(words) - n + 1):
                phrase = " ".join(words[i:i + n])
                if not all(w in _STOPWORDS for w in words[i:i + n]):
                    v["_phrase_counter"][phrase] += 1
        v["common_phrases"] = [p for p, _ in v["_phrase_counter"].most_common(500)]

        if len(v["sample_messages"]) % 10 == 0:
            v["energy"] = _detect_energy(v["_recent_buf"])
            self._learning_ctx_cache.pop(guild_id, None)  # invalidate cache

    # ------------------------------------------------------------------
    # Moderation — 3-priority target resolution
    # ------------------------------------------------------------------

    async def _resolve_mod_target(
        self, message: discord.Message
    ) -> Optional[discord.Member]:
        """
        Find the moderation target using 3-priority search:
          1. Author of the message being replied to
          2. First @mention in the message content
          3. Any guild member whose display name appears in the message text
        Returns None if no target can be found.
        """
        assert message.guild is not None

        if (
            message.reference is not None
            and isinstance(message.reference.resolved, discord.Message)
        ):
            replied_author = message.reference.resolved.author
            if not replied_author.bot:
                member = message.guild.get_member(replied_author.id)
                if member:
                    return member

        mention_match = re.search(r'<@!?(\d+)>', message.content)
        if mention_match:
            member = message.guild.get_member(int(mention_match.group(1)))
            if member:
                return member

        content_lower = message.content.lower()
        for member in message.guild.members:
            if member.bot:
                continue
            if (
                member.display_name.lower() in content_lower
                or member.name.lower() in content_lower
            ):
                return member

        return None

    # ------------------------------------------------------------------
    # Moderation handler
    # Returns True if a moderation action was taken (caller skips normal AI)
    # ------------------------------------------------------------------

    async def _try_moderation(
        self, message: discord.Message, clean: str
    ) -> bool:
        """
        Detect and execute mute / unmute / kick / ban / warn / delete.
        Returns True if a moderation branch was handled.
        """
        if message.guild is None:
            return False
        author = message.author
        if not isinstance(author, discord.Member):
            return False

        lower = clean.lower()

        # ── DELETE REPLIED MESSAGE ────────────────────────────────────────
        if message.reference and any(kw in lower for kw in _DELETE_KEYWORDS):
            if not _has_mod_permission(author):
                await message.channel.send(
                    random.choice([
                        "bro you dont have the clearance for that 💀 nice try tho",
                        "lmaooo you wish. get some perms first bestie",
                        "nah. not happening. who are you 💀",
                    ])
                )
                return True
            try:
                ref_msg = message.reference.resolved
                if not isinstance(ref_msg, discord.Message):
                    ref_msg = await message.channel.fetch_message(
                        message.reference.message_id
                    )
                await ref_msg.delete()
            except discord.NotFound:
                await message.channel.send("bro it's already gone lmaooo 💀")
                return True
            except discord.Forbidden:
                await message.channel.send(
                    "ngl i can't delete that, no permissions 😭 give me manage messages"
                )
                return True
            note = (
                f"You just deleted a message because {author.display_name} asked. "
                "Confirm it dramatically. You have the power."
            )
            async with message.channel.typing():
                reply = await self._ai_reply(
                    author.id, clean, extra_note=note, guild_id=message.guild.id,
                    max_tokens=150,
                )
            await self._send_biki_reply(message, reply)
            return True

        # ── DETECT ACTION KEYWORD ─────────────────────────────────────────
        action: Optional[str] = None
        if _RE_UNMUTE.search(lower):
            action = "unmute"
        elif _RE_MUTE.search(lower):
            action = "mute"
        elif _RE_KICK.search(lower):
            action = "kick"
        elif _RE_BAN.search(lower):
            action = "ban"
        elif _RE_WARN.search(lower):
            action = "warn"

        if action is None:
            return False

        # ── PERMISSION CHECK ──────────────────────────────────────────────
        if not _has_mod_permission(author):
            await message.channel.send(
                random.choice([
                    "bro you dont have the clearance for that 💀 nice try tho",
                    "lmaooo you wish. get some perms first",
                    "nah. not for you 💀",
                    "who gave you access to the mod menu? oh wait. nobody 💀",
                ])
            )
            return True

        # ── RESOLVE TARGET (3-priority) ───────────────────────────────────
        target = await self._resolve_mod_target(message)
        if target is None:
            await message.channel.send(
                "bro who are you even talking about 💀 "
                "ping them or reply to their message"
            )
            return True

        if target.guild_permissions.administrator or target.id == self.bot.user.id:
            note = (
                "Someone tried to make you take a moderation action against an admin "
                "or against yourself. Refuse dramatically and chaotically."
            )
            async with message.channel.typing():
                reply = await self._ai_reply(
                    author.id, clean, extra_note=note, guild_id=message.guild.id,
                    max_tokens=150,
                )
            await self._send_biki_reply(message, reply)
            return True

        # ── EXECUTE ACTION ────────────────────────────────────────────────

        if action == "mute":
            duration  = _parse_mute_duration(clean)
            human_dur = _human_duration(duration)
            await message.channel.send(
                random.choice([
                    "okay daddy 😈 give me a sec",
                    "on it rn",
                    "say less 💀",
                    "brb handling business",
                ])
            )
            try:
                until = datetime.now(timezone.utc) + timedelta(seconds=duration)
                await target.timeout(until, reason=f"Muted by {author} via Biki")
            except discord.Forbidden:
                await message.channel.send(
                    "bro i literally dont have the power to do that rn, "
                    "give me the mute members permission"
                )
                return True
            note = (
                f"You just muted {target.display_name} for {human_dur} "
                f"because {author.display_name} asked. Confirm the mute chaotically. "
                f"Say something like 'done. {target.display_name} is cooked for "
                f"{human_dur} 💀'"
            )
            async with message.channel.typing():
                reply = await self._ai_reply(
                    author.id, clean, extra_note=note, guild_id=message.guild.id,
                    max_tokens=150,
                )
            await self._send_biki_reply(message, reply)

        elif action == "unmute":
            try:
                await target.edit(timed_out_until=None)
            except discord.Forbidden:
                await message.channel.send("no permissions for that smh 😭")
                return True
            note = (
                f"You just unmuted {target.display_name} because "
                f"{author.display_name} asked. "
                f"Say something like 'fine fine {target.display_name} "
                "you're free. don't make me regret this'"
            )
            async with message.channel.typing():
                reply = await self._ai_reply(
                    author.id, clean, extra_note=note, guild_id=message.guild.id,
                    max_tokens=150,
                )
            await self._send_biki_reply(message, reply)

        elif action == "kick":
            name = target.display_name
            try:
                await target.kick(reason=f"Kicked by {author} via Biki")
            except discord.Forbidden:
                await message.channel.send(
                    "no kick permissions smh 😭 give me kick members"
                )
                return True
            note = (
                f"You just kicked {name} because {author.display_name} asked. "
                f"Say something like 'YEET 👋 {name} has left the building. bye bestie'"
            )
            async with message.channel.typing():
                reply = await self._ai_reply(
                    author.id, clean, extra_note=note, guild_id=message.guild.id,
                    max_tokens=150,
                )
            await self._send_biki_reply(message, reply)

        elif action == "ban":
            name = target.display_name
            try:
                await target.ban(reason=f"Banned by {author} via Biki")
            except discord.Forbidden:
                await message.channel.send(
                    "no ban permissions smh 😭 give me ban members"
                )
                return True
            note = (
                f"You just banned {name} because {author.display_name} asked. "
                f"Say something like 'damn okay. {name} is GONE gone. rip 💀'"
            )
            async with message.channel.typing():
                reply = await self._ai_reply(
                    author.id, clean, extra_note=note, guild_id=message.guild.id,
                    max_tokens=150,
                )
            await self._send_biki_reply(message, reply)

        elif action == "warn":
            reason_raw = re.sub(r'<@!?\d+>', '', re.sub(_RE_WARN, '', clean)).strip()
            reason = reason_raw or "no reason given"
            events_cog = self.bot.get_cog("Events")
            if events_cog and hasattr(events_cog, "_apply_warn"):
                try:
                    await events_cog._apply_warn(target, reason)
                except Exception as exc:
                    log.error("ai_companion: _apply_warn failed: %s", exc)
            else:
                try:
                    await asyncio.to_thread(
                        _db_add_warning,
                        message.guild.id, target.id, author.id, reason,
                    )
                    await target.send(
                        f"⚠️ you got a warning in **{message.guild.name}**\n"
                        f"reason: {reason}"
                    )
                except Exception:
                    pass
            note = (
                f"You just warned {target.display_name} for: '{reason}'. "
                f"{author.display_name} asked you to. "
                f"Say something like 'consider yourself warned "
                f"{target.display_name} 👀 biki is watching'"
            )
            async with message.channel.typing():
                reply = await self._ai_reply(
                    author.id, clean, extra_note=note, guild_id=message.guild.id,
                    max_tokens=150,
                )
            await self._send_biki_reply(message, reply)

        return True

    # ------------------------------------------------------------------
    # Single unified on_message listener
    #
    # Sections (in order):
    #   1. Ignore bots and DMs
    #   2. Channel gate
    #   3. Passive learning
    #   4. Detect trigger
    #   5. Proactive 3% jump-in (not triggered) then return
    #   6. Triggered handler:
    #      a. Strip bot mention
    #      b. Handle empty reply content
    #      c. Conversation lock check
    #      d. Mark as processing
    #      e. Moderation check
    #      f. Dismissal state check
    #      g. Spite return check
    #      h. Timed dismissal parse
    #      i. Plain dismissal check
    #      j. Normal reply
    #      k. Finally: release lock, trigger next pending
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # ── 1. Ignore bots and DMs ────────────────────────────────────────
        if message.author.bot or message.guild is None:
            return

        guild_id = message.guild.id
        user_id  = message.author.id

        # ── 2. Channel gate ───────────────────────────────────────────────
        allowed = self.allowed_channels.get(guild_id, [])
        if allowed and message.channel.id not in allowed:
            return

        # ── 3. Passive learning ───────────────────────────────────────────
        self._learn_from_message(message)

        # ── 4. Detect trigger ─────────────────────────────────────────────
        assert self.bot.user is not None

        bot_mentioned = self.bot.user in message.mentions
        replied_to_bot = (
            message.reference is not None
            and isinstance(message.reference.resolved, discord.Message)
            and message.reference.resolved.author.id == self.bot.user.id
        )
        triggered = bot_mentioned or replied_to_bot

        # ── 5. Proactive jump-in (3% chance, only when NOT triggered) ─────
        if not triggered:
            if random.random() < 0.01:
                asyncio.create_task(self._proactive_reply(message))
            return

        # ── 6. Triggered handler ──────────────────────────────────────────

        channel_id = message.channel.id

        # a. Strip bot mentions from content
        clean = message.content
        clean = clean.replace(f"<@{self.bot.user.id}>", "")
        clean = clean.replace(f"<@!{self.bot.user.id}>", "").strip()

        # b. Handle empty reply-to-bot content
        if not clean and replied_to_bot and isinstance(
            message.reference.resolved, discord.Message
        ):
            clean = (
                f"[replying to your message: "
                f"\"{message.reference.resolved.content[:200]}\"]"
            )

        # ── Per-user cooldown (30 seconds) ───────────────────────────────
        now = time.time()
        last_reply = self._user_cooldowns.get(user_id, 0)
        cooldown_remaining = 30.0 - (now - last_reply)

        if cooldown_remaining > 0:
            if random.random() < 0.4:
                cooldown_replies = [
                    "bro chill im still thinking 💀",
                    "one sec one sec",
                    "bro i JUST replied to you",
                    "give me a second omg",
                    "i'm not a machine stop pinging me back to back",
                    "bro. BREATHE.",
                    f"wait like {int(cooldown_remaining)} more seconds istg",
                    "you're so impatient i cannot",
                    "bro i haven't even finished my thought yet",
                    "one conversation at a time omg 😭",
                ]
                await message.channel.send(random.choice(cooldown_replies))
            return

        # c. Conversation lock check ─────────────────────────────────────
        # If Biki is currently processing a reply for ANY user, queue this
        # message. Only the latest message per user is kept.
        if self._processing:
            self._pending[user_id] = message
            return

        # d. Mark this user as being processed
        self._processing.add(user_id)

        try:
            # e. Moderation check — no pre-typing delay, feels immediate
            if await self._try_moderation(message, clean):
                return

            # f. Dismissal state check ───────────────────────────────────
            dismissed_state = self.dismissed.get(user_id)
            if dismissed_state is not None:
                if self._is_return(clean):
                    self.dismissed.pop(user_id, None)
                    note = (
                        "The person who kicked you out is begging you to come back. "
                        "Make your re-entry absolutely unhinged."
                    )
                    try:
                        async with message.channel.typing():
                            reply = await self._ai_reply(
                                user_id, clean, extra_note=note, guild_id=guild_id,
                                max_tokens=150,
                            )
                        await self._send_biki_reply(message, reply)
                    except Exception as exc:
                        log.error("ai_companion: return reply failed: %s", exc)
                return

            # g. Spite return check ──────────────────────────────────────
            all_dismissed_by = {v["dismissed_by"] for v in self.dismissed.values()}
            if (
                all_dismissed_by
                and self._is_return(clean)
                and user_id not in all_dismissed_by
            ):
                self.dismissed.clear()
                note = (
                    "Someone ELSE summoned you back just to spite the person who "
                    "dismissed you. Most dramatic comeback ever."
                )
                try:
                    async with message.channel.typing():
                        reply = await self._ai_reply(
                            user_id, clean, extra_note=note, guild_id=guild_id,
                            max_tokens=150,
                        )
                    await self._send_biki_reply(message, reply)
                except Exception as exc:
                    log.error("ai_companion: spite-return failed: %s", exc)
                return

            # h. Timed dismissal parse ────────────────────────────────────
            timed_seconds = self._parse_timed_dismiss(clean)
            if timed_seconds is not None:
                self.dismissed[user_id] = {
                    "channel_id": channel_id,
                    "dismissed_by": user_id,
                }
                note = (
                    f"This person is dismissing you for exactly "
                    f"{int(timed_seconds)} seconds. "
                    "Acknowledge it chaotically then go quiet."
                )
                try:
                    async with message.channel.typing():
                        reply = await self._ai_reply(
                            user_id, clean, extra_note=note, guild_id=guild_id,
                            max_tokens=150,
                        )
                    await self._send_biki_reply(message, reply)
                except Exception as exc:
                    log.error("ai_companion: timed dismiss failed: %s", exc)
                asyncio.create_task(
                    self._timed_return(timed_seconds, channel_id, user_id)
                )
                return

            # i. Plain dismissal check ────────────────────────────────────
            if self._is_dismiss(clean):
                self.dismissed[user_id] = {
                    "channel_id": channel_id,
                    "dismissed_by": user_id,
                }
                note = "This person is kicking you out. Most dramatic chaotic goodbye ever."
                try:
                    async with message.channel.typing():
                        reply = await self._ai_reply(
                            user_id, clean, extra_note=note, guild_id=guild_id,
                            max_tokens=150,
                        )
                    await self._send_biki_reply(message, reply)
                except Exception as exc:
                    log.error("ai_companion: dismiss failed: %s", exc)
                return

            # j. Normal reply ─────────────────────────────────────────────
            # 1s reading pause → keep typing alive during AI call →
            # _send_biki_reply handles per-part typing simulation
            try:
                await asyncio.sleep(1.0)
                async with message.channel.typing():
                    reply = await self._ai_reply(user_id, clean, guild_id=guild_id)
                self._user_cooldowns[user_id] = time.time()
                await self._send_biki_reply(message, reply)
            except Exception as exc:
                log.error("ai_companion: AI call failed: %s", exc)
                await message.channel.send(random.choice(_OFFLINE_REPLIES))

        finally:
            # k. Always release the lock when done, even if an error occurred
            self._processing.discard(user_id)

            # Process next pending message if any exist
            if self._pending:
                next_user_id, next_message = next(iter(self._pending.items()))
                self._pending.pop(next_user_id, None)
                asyncio.create_task(self.on_message(next_message))

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    @app_commands.command(name="bikimood", description="Set Biki's mood for this server.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(mood=[
        app_commands.Choice(name="😡 Feral — over the top unhinged energy",    value="feral"),
        app_commands.Choice(name="😒 Sulky — moody, short, passive-aggressive", value="sulky"),
        app_commands.Choice(name="😈 Villain — sinister, plotting, menacing",   value="villain"),
        app_commands.Choice(name="🌹 Romantic — chaotic dramatic declarations",  value="romantic"),
        app_commands.Choice(name="🌀 Unhinged — maximum chaos, zero coherence",  value="unhinged"),
        app_commands.Choice(name="😐 Normal — default Biki",                    value="normal"),
    ])
    async def bikimood(self, interaction: discord.Interaction, mood: str) -> None:
        assert interaction.guild_id is not None
        self.guild_moods[interaction.guild_id] = mood
        labels = {
            "feral":    "😡 FERAL MODE — Biki is feral now. god help us all",
            "sulky":    "😒 SULKY MODE — Biki is moody and will let everyone know it",
            "villain":  "😈 VILLAIN MODE — Biki has entered his villain arc",
            "romantic": "🌹 ROMANTIC MODE — Biki is inexplicably feeling things",
            "unhinged": "🌀 MAX UNHINGED — Biki has ascended beyond normal unhinged",
            "normal":   "😐 NORMAL MODE — Biki is back to baseline (relatively)",
        }
        await interaction.response.send_message(
            labels.get(mood, f"mood set to {mood}"), ephemeral=False
        )

    @app_commands.command(
        name="bikilearning",
        description="Show how much Biki has learned about this server's style.",
    )
    @app_commands.guild_only()
    async def bikilearning(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        v = self.server_vocab.get(interaction.guild_id)
        if not v:
            await interaction.response.send_message(
                "biki hasn't learned anything about this server yet... "
                "talk more and he will",
                ephemeral=True,
            )
            return
        phrases    = len(v.get("common_phrases", []))
        slang_cnt  = len(v.get("slang", []))
        emoji_cnt  = len(v.get("emojis", []))
        sample_cnt = len(v.get("sample_messages", []))
        energy     = v.get("energy", "mixed")
        top_slang  = ", ".join(v.get("slang", [])[-8:])  or "none yet"
        top_emojis = " ".join(v.get("emojis", [])[:8])   or "none yet"
        await interaction.response.send_message(
            f"**biki's server learning stats**\n"
            f"• phrases collected: **{phrases}** / 500\n"
            f"• slang words: **{slang_cnt}** / 200\n"
            f"• emojis tracked: **{emoji_cnt}** / 100\n"
            f"• sample messages: **{sample_cnt}** / 100\n"
            f"• detected server energy: **{energy}**\n"
            f"• top slang: {top_slang}\n"
            f"• top emojis: {top_emojis}",
            ephemeral=True,
        )

    @app_commands.command(
        name="aiset",
        description="Add a channel where Biki is allowed to respond.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def aiset(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        try:
            updated = await asyncio.to_thread(
                _db_add_channel, interaction.guild_id, channel.id
            )
            self.allowed_channels[interaction.guild_id] = updated
        except Exception as exc:
            log.error("aiset: DB error: %s", exc)
            await interaction.followup.send(
                f"❌ Failed to update database: `{exc}`", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"✅ Biki can now respond in {channel.mention}.", ephemeral=True
        )

    @app_commands.command(
        name="aiunset",
        description="Remove a channel from Biki's allowed list.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def aiunset(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        try:
            updated = await asyncio.to_thread(
                _db_remove_channel, interaction.guild_id, channel.id
            )
            self.allowed_channels[interaction.guild_id] = updated
        except Exception as exc:
            log.error("aiunset: DB error: %s", exc)
            await interaction.followup.send(
                f"❌ Failed to update database: `{exc}`", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"✅ Biki will no longer respond in {channel.mention}.", ephemeral=True
        )

    @app_commands.command(
        name="aichannels",
        description="List all channels where Biki is allowed to respond.",
    )
    @app_commands.guild_only()
    async def aichannels(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        allowed = self.allowed_channels.get(interaction.guild_id, [])
        if not allowed:
            await interaction.response.send_message(
                "Biki responds in **all channels** (no restrictions set). "
                "Use `/aiset` to restrict him.",
                ephemeral=True,
            )
            return
        mentions = " ".join(f"<#{cid}>" for cid in allowed)
        await interaction.response.send_message(
            f"Biki is allowed in: {mentions}", ephemeral=True
        )

    @app_commands.command(
        name="aireset",
        description="Clear a user's Biki conversation history and dismissed state.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def aireset(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        self.conversations.pop(user.id, None)
        self.dismissed.pop(user.id, None)
        self._users_spoken.discard(user.id)
        await interaction.response.send_message(
            f"✅ Cleared Biki's memory and state for {user.mention}.", ephemeral=True
        )

    @app_commands.command(
        name="bikiwarns",
        description="Check how many warnings a user has from Biki.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(kick_members=True)
    async def bikiwarns(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        try:
            warnings = await asyncio.to_thread(
                _db_get_warnings, interaction.guild_id, user.id
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ DB error: `{exc}`", ephemeral=True)
            return
        if not warnings:
            await interaction.followup.send(
                f"{user.mention} has no warnings.", ephemeral=True
            )
            return
        lines = [f"**{user.display_name}** — {len(warnings)} warning(s)\n"]
        for i, w in enumerate(warnings, 1):
            ts = (
                w["created_at"].strftime("%Y-%m-%d %H:%M")
                if w["created_at"] else "unknown"
            )
            lines.append(f"`{i}.` {w['reason']} — <@{w['warned_by']}> @ {ts}")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(
        name="bikiwarnclear",
        description="Clear all warnings for a user.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def bikiwarnclear(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        try:
            await asyncio.to_thread(
                _db_clear_warnings, interaction.guild_id, user.id
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ DB error: `{exc}`", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Cleared all warnings for {user.mention}.", ephemeral=True
        )

    @app_commands.command(
        name="bikistats",
        description="Show Biki's current in-memory session stats.",
    )
    @app_commands.guild_only()
    async def bikistats(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        total_msgs    = sum(len(v) for v in self.conversations.values())
        total_users   = len(self.conversations)
        spoken_this   = len(self._users_spoken)
        dismissed_cnt = len(self.dismissed)
        mood          = self.guild_moods.get(interaction.guild_id, "normal")
        pending_cnt   = len(self._pending)
        processing_cnt = len(self._processing)
        await interaction.response.send_message(
            f"**Biki session stats**\n"
            f"• Conversations loaded: **{total_users}** users\n"
            f"• Total messages in memory: **{total_msgs}**\n"
            f"• Users spoken to this session: **{spoken_this}**\n"
            f"• Currently dismissed by: **{dismissed_cnt}** user(s)\n"
            f"• Active mood: **{mood}**\n"
            f"• Currently processing: **{processing_cnt}** conversation(s)\n"
            f"• Pending in queue: **{pending_cnt}** message(s)",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AiCompanion(bot))
