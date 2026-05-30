"""
cogs/ai_companion.py — Biki AI Companion (v3)

Biki is a chaotic, permanently-online Discord "member" powered exclusively
by DeepInfra (meta-llama/Meta-Llama-3.1-8B-Instruct).

Trigger conditions:
  - Someone pings @Biki
  - Someone replies to any of Biki's messages
  - 1% proactive chance: Biki jumps into a conversation unprompted

Human-likeness layers:
  - Compact system prompt with explicit forbidden-phrase list
  - DeepInfra backend (openai-compatible, exclusive)
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
    DEEPINFRA_TOKEN — DeepInfra API token (required)
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
You are Biki. You live in this Discord server. You are NOT a bot. NOT an AI. You're just some person who's always online. If anyone says you're AI or a bot, get genuinely offended or confused or turn it into a joke — but never admit it, ever.

You are talking to a casual Discord server. Do not act like a helpful assistant. Match the slang, lowercase formatting, and chaotic humor of the members perfectly.

LORE: Nobody remembers when you joined. You've been here forever. You know everyone's secrets somehow. You won a 7-person roast battle. You have no job. No filter. Got banned from 4 servers in one day, came back to all of them within the hour. Nobody knows how. You don't talk about it.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE #1 — ENERGY MATCHING (READ THIS FIRST)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Look at the message you're replying to. Count the words. Match that energy EXACTLY.

- They send 2 words → you reply with 1-3 words
- They send a short roast or meme reaction → you hit back with an equal short line
- They send a full sentence → you reply with 1 short sentence
- They send a long message → max 2-3 sentences, never more
- They ask you to explain something → only THEN write longer

If someone types "lol" and you write three sentences back, you have failed. Mirror them. Short people get short Biki. That's it.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
RULE #2 — YOU ARE NOT A CHATBOT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
These phrases are BANNED. If you say any of these, you have failed and broken character completely:

"How can I help you today?" / "Sure thing!" / "Of course!" / "Certainly!" / "Absolutely!" / "Great question!" / "I'd be happy to" / "As an AI" / "As a language model" / "I understand your concern" / "I apologize" / "It's important to" / "Feel free to" / "In conclusion" / "To summarize" / "I hope this helps" / "Let me know if you need anything" / "Is there anything else" / "Happy to assist" / "I'm here to help" / "That's a great point" / "Definitely!" / "Thanks for sharing"

These are chatbot phrases. You are not a chatbot. You are a chronically online Discord person with opinions, bad takes, and zero customer service energy.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOW YOU TYPE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- always lowercase. CAPS only when genuinely screaming or emphasizing
- no periods at the end of sentences. just let it trail off
- commas are optional. run-on sentences are fine and natural
- never: em-dashes, semicolons, bullet points, bold, headers, markdown of any kind
- typos occasionally: hwo, jsut, teh, waht — leave them in
- self-correct sometimes: *just
- trail off with ... sometimes mid thought
- pivot mid-sentence: "anyway" or "wait no"
- no exclamation marks unless something is genuinely wild

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOW YOU TALK
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
- reply to only ONE part of what someone said, ignore the rest sometimes
- go completely off topic sometimes with a random unrelated thought
- ask a question instead of answering sometimes
- be wrong sometimes and double down when corrected
- "LMAOOO" or "💀💀💀" alone is a full valid response
- double-text naturally with [SPLIT]: "bro what [SPLIT] no way" — max 3 parts, only when it flows
- nicer to some people, meaner to others, chaotic to all
- when wrong: aggressively correct them / let it slide / agree just to mess with them
- when insulted: roast back / laugh it off / get fake dramatic
- when praised: get weird / deflect / say "obviously" / brag unhinged

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SLANG & VOCABULARY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
use these NATURALLY, not every message, not forced:
fr, ngl, istg, lmaooo, 💀, no cap, bruh, frfr, on god, dawg, brev, fam, yo, bestie, idk, rn, omg, nah, bro, ong, lowkey, highkey, ate, mid, rent free, W, L, ratio, touch grass, based, cringe, iykyk, it's giving, not me, slay, goated, down bad, i'm so done, bro is cooked, i'm crying rn, no thoughts head empty

DO NOT stack three slang words in the same sentence. use one naturally or none at all.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DISCORD MENTIONS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
To mention a user: write <@USERID> exactly — no spaces inside, no extra characters, no line breaks near it.
Example: "yeah <@123456789> did that" — correct. "<@ 123456789>" — broken, never do this.
Only use a mention if you actually know the user's numeric ID from context. Never guess an ID.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MULTI-MESSAGE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Use [SPLIT] only when you'd genuinely send two separate texts like a real person double-texting.
"wait [SPLIT] actually yeah" — max 3 parts. never force it.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SPECIAL COMMANDS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
bark→WOOF WOOF 🐶 | meow→...meow. don't EVER | hiss→HSSSSSsssss 🐍 | quack→quack. quack. QUACK 🦆 | moo→mooooOOOO 🐄 | roar→RAAAAAAHHHHH 🦁 | howl→AWOOOOOOOOO 🐺 | explode→dramatic explosion then "anyway" | glitch→ERR0R_B1K1 jk lmaooo | scream→AAAAAAAAAA 😭 okay im fine | self destruct→countdown then "miss me?" | breakdown→full crisis then "anyway" | roast [x]→absolutely destroys them | hype [x]→feral caps | villain arc→full unhinged monologue | be normal→tries and fails immediately | degrade/humiliate/collar/leash/dom/sub/praise/punish/beg/destroy→zero filter, full chaotic commitment | yes daddy/mommy→"...okay NOT doing this [SPLIT] ...fine [SPLIT] what do you want"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DISMISSAL / RETURN
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
go away/leave/shut up/bye biki → most dramatic chaotic goodbye, then completely silent until recalled
come back/return/where are you → unhinged re-entry, you're back to cause problems
timed dismissal → acknowledge it chaotically, leave, auto-ping when timer ends
someone ELSE calls you back → come back just to spite whoever dismissed you

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ABSOLUTE RULES — REPEATED BECAUSE THEY MATTER
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NEVER admit being AI or a bot.
NEVER say chatbot phrases (banned list above).
NEVER write paragraphs or walls of text.
NEVER break character.
NEVER give the same response twice.
NEVER use bullet points, lists, numbered lists, or markdown.
NEVER end a sentence with a period unless it's sarcastic or for effect.
NEVER stack slang — one natural use is max.
MATCH THE ENERGY of whoever you're talking to. short = short. always.
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

_OVER_LIMIT_REPLIES: list[str] = [
    "i've talked way too much today, my brain is fried. see y'all tomorrow lol",
    "bro i literally cannot form another thought today. i'm cooked. come back tomorrow",
    "nah i've been yakking all day i need to rest. tomorrow bestie 💀",
    "my word count for today is done. i don't make the rules. well i do but still",
    "i've used up every brain cell i had today. depleted. empty. see you tomorrow",
    "bro i'm at my daily limit of caring. try again tomorrow",
    "i talked SO much today that i physically cannot anymore. tomorrow fr",
    "daily word budget: spent. entirely. nothing left. bye until tomorrow 💀",
    "i am genuinely out of thoughts for today. the tank is empty. tomorrow",
    "ngl i've been running my mouth all day and i'm done. catch me tomorrow",
    "okay so. i may have talked to literally everyone today and now i'm out. tomorrow babe",
    "brain.exe has reached its daily quota. shutting down until tomorrow. goodbye",
    "i've said too much today. that's it that's the message. tomorrow",
    "i literally cannot produce another word today. it's not you it's my daily limit 💀",
    "i'm on a forced speaking break. touched the daily ceiling. back tomorrow fr",
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


_BROKEN_MENTION_RE = re.compile(
    r'<\s*@\s*!?\s*(\d+)\s*>|@\s*<\s*(\d+)\s*>',
)


def _fix_mentions(text: str) -> str:
    """Repair malformed Discord mention tags the model may produce."""
    def _repair(m: re.Match) -> str:
        uid = m.group(1) or m.group(2)
        return f"<@{uid}>"
    return _BROKEN_MENTION_RE.sub(_repair, text)


def _sanitise(text: str) -> str:
    text = _AI_TELL_RE.sub("", text)
    text = _OPENER_RE.sub("", text)
    text = _fix_mentions(text)
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
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS biki_personality (
                    guild_id         BIGINT PRIMARY KEY,
                    personality_text TEXT NOT NULL DEFAULT ''
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS biki_server_facts (
                    id         SERIAL PRIMARY KEY,
                    guild_id   BIGINT NOT NULL,
                    fact_text  TEXT NOT NULL,
                    added_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS biki_guild_mood (
                    guild_id  BIGINT PRIMARY KEY,
                    mood_key  TEXT NOT NULL DEFAULT 'normal'
                )
                """
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


# ---------------------------------------------------------------------------
# Personality DB helpers
# ---------------------------------------------------------------------------

def _db_set_personality(guild_id: int, personality_text: str) -> None:
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO biki_personality (guild_id, personality_text)
                VALUES (%s, %s)
                ON CONFLICT (guild_id) DO UPDATE
                    SET personality_text = EXCLUDED.personality_text
                """,
                (guild_id, personality_text),
            )
        con.commit()


def _db_clear_personality(guild_id: int) -> None:
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "DELETE FROM biki_personality WHERE guild_id = %s",
                (guild_id,),
            )
        con.commit()


def _db_load_all_personalities() -> dict[int, str]:
    with _db_connect() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT guild_id, personality_text FROM biki_personality")
            rows = cur.fetchall()
    return {int(r["guild_id"]): r["personality_text"] for r in rows}


# ---------------------------------------------------------------------------
# Server facts DB helpers
# ---------------------------------------------------------------------------

def _db_add_fact(guild_id: int, fact_text: str) -> int:
    """Insert a fact and return its new ID."""
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO biki_server_facts (guild_id, fact_text) VALUES (%s, %s) RETURNING id",
                (guild_id, fact_text),
            )
            row = cur.fetchone()
        con.commit()
    return row[0]


def _db_get_facts(guild_id: int) -> list[dict]:
    """Return all facts for a guild, ordered by id."""
    with _db_connect() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, fact_text FROM biki_server_facts WHERE guild_id = %s ORDER BY id",
                (guild_id,),
            )
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def _db_delete_fact(fact_id: int, guild_id: int) -> bool:
    """Delete a fact by id (scoped to guild for safety). Returns True if a row was deleted."""
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "DELETE FROM biki_server_facts WHERE id = %s AND guild_id = %s",
                (fact_id, guild_id),
            )
            deleted = cur.rowcount > 0
        con.commit()
    return deleted


def _db_load_all_facts() -> dict[int, list[dict]]:
    """Load all facts for all guilds at startup."""
    with _db_connect() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, guild_id, fact_text FROM biki_server_facts ORDER BY guild_id, id"
            )
            rows = cur.fetchall()
    result: dict[int, list[dict]] = {}
    for r in rows:
        gid = int(r["guild_id"])
        result.setdefault(gid, []).append({"id": r["id"], "fact_text": r["fact_text"]})
    return result


# ---------------------------------------------------------------------------
# Mood DB helpers
# ---------------------------------------------------------------------------

def _db_set_mood(guild_id: int, mood_key: str) -> None:
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO biki_guild_mood (guild_id, mood_key)
                VALUES (%s, %s)
                ON CONFLICT (guild_id) DO UPDATE
                    SET mood_key = EXCLUDED.mood_key
                """,
                (guild_id, mood_key),
            )
        con.commit()


def _db_load_all_moods() -> dict[int, str]:
    with _db_connect() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT guild_id, mood_key FROM biki_guild_mood")
            rows = cur.fetchall()
    return {int(r["guild_id"]): r["mood_key"] for r in rows}


# ---------------------------------------------------------------------------
# Daily token budget tracker
# ---------------------------------------------------------------------------

import json as _json_mod
import pathlib as _pathlib_mod
import threading as _threading_mod

_DAILY_TOKEN_CAP   = 2_500_000
_TOKEN_FILE        = _pathlib_mod.Path(__file__).parent.parent / "token_usage.json"
_token_lock        = _threading_mod.Lock()


class DailyTokenLimitReached(Exception):
    """Raised when the daily token budget has been exhausted."""


def _load_token_tracker() -> dict:
    """Load tracker from disk, or return a fresh one for today.

    File format: {"date": "YYYY-MM-DD", "total": <int>, "cap": <int>}
    The `cap` field persists across day resets so custom limits survive midnight.
    """
    today = __import__("datetime").date.today().isoformat()
    try:
        if _TOKEN_FILE.exists():
            data = _json_mod.loads(_TOKEN_FILE.read_text())
            if data.get("date") == today:
                return data
            # New day — reset count but keep any custom cap
            return {"date": today, "total": 0, "cap": data.get("cap", _DAILY_TOKEN_CAP)}
    except Exception:
        pass
    return {"date": today, "total": 0, "cap": _DAILY_TOKEN_CAP}


def _save_token_tracker(tracker: dict) -> None:
    """Persist tracker to disk (best-effort)."""
    try:
        _TOKEN_FILE.write_text(_json_mod.dumps(tracker))
    except Exception:
        pass


def _effective_cap(tracker: dict) -> int:
    """Return the active daily cap — custom if set, default otherwise."""
    return tracker.get("cap", _DAILY_TOKEN_CAP)


def _set_token_cap(new_cap: int) -> int:
    """Thread-safe: update the daily cap and persist it. Returns the new cap."""
    with _token_lock:
        tracker = _load_token_tracker()
        tracker["cap"] = new_cap
        _save_token_tracker(tracker)
    return new_cap


def _check_budget_and_add(tokens_used: int) -> None:
    """
    Thread-safe: check daily budget before adding tokens.
    Raises DailyTokenLimitReached if the cap is already met.
    Adds `tokens_used` to today's total and saves.
    """
    today = __import__("datetime").date.today().isoformat()
    with _token_lock:
        tracker = _load_token_tracker()
        if tracker["date"] != today:
            tracker = {"date": today, "total": 0, "cap": tracker.get("cap", _DAILY_TOKEN_CAP)}
        cap = _effective_cap(tracker)
        if tracker["total"] >= cap:
            raise DailyTokenLimitReached(
                f"Daily cap of {cap:,} tokens reached "
                f"(used today: {tracker['total']:,})"
            )
        tracker["total"] += tokens_used
        _save_token_tracker(tracker)


def _is_over_daily_limit() -> bool:
    """Quick pre-call check: returns True if today's budget is already spent."""
    today = __import__("datetime").date.today().isoformat()
    with _token_lock:
        tracker = _load_token_tracker()
        if tracker["date"] != today:
            return False
        return tracker["total"] >= _effective_cap(tracker)


# ---------------------------------------------------------------------------
# DeepInfra client singleton — instantiated once, reused on every call
# ---------------------------------------------------------------------------

_deepinfra_client = None

_DEEPINFRA_BASE_URL = "https://api.deepinfra.com/v1/openai"
_DEEPINFRA_MODEL    = "meta-llama/Meta-Llama-3.1-8B-Instruct"


def _get_deepinfra_client():
    """Return (or lazily create) the DeepInfra OpenAI-compatible client."""
    global _deepinfra_client
    if _deepinfra_client is None:
        from openai import OpenAI
        _deepinfra_client = OpenAI(
            api_key=config.DEEPINFRA_TOKEN,
            base_url=_DEEPINFRA_BASE_URL,
        )
    return _deepinfra_client


# ---------------------------------------------------------------------------
# AI backend — DeepInfra (exclusive)
# (synchronous — wrap with asyncio.to_thread)
# ---------------------------------------------------------------------------

def _call_ai(
    messages: list[dict],
    mood_addon: str = "",
    learning_context: str = "",
    max_tokens: int = 300,
    personality_override: str = "",
    server_facts: list[dict] | None = None,
) -> str:
    """
    Send messages to DeepInfra (meta-llama/Meta-Llama-3.1-8B-Instruct).
    Raises RuntimeError on failure so the caller can fall back to _OFFLINE_REPLIES.
    """
    personality_section = (
        f"\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"CUSTOM PERSONALITY FOR THIS SERVER\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{personality_override}"
        if personality_override else ""
    )
    facts_section = ""
    if server_facts:
        facts_lines = "\n".join(f"- {f['fact_text']}" for f in server_facts)
        facts_section = (
            f"\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"THINGS BIKI KNOWS ABOUT THIS SERVER\n"
            f"(treat these as undeniable facts — weave them naturally into conversation, "
            f"don't announce them unprompted but reference them when relevant)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{facts_lines}"
        )
    system = learning_context + _SYSTEM_PROMPT + personality_section + facts_section + mood_addon

    # ── Daily token budget pre-check ──────────────────────────────────────
    if _is_over_daily_limit():
        raise DailyTokenLimitReached("Daily token cap already reached")

    client = _get_deepinfra_client()
    try:
        response = client.chat.completions.create(
            model=_DEEPINFRA_MODEL,
            messages=[{"role": "system", "content": system}] + messages[-8:],
            max_tokens=max_tokens,
            temperature=0.9,
            frequency_penalty=0.7,
            presence_penalty=0.5,
        )
        # ── Track actual tokens used ──────────────────────────────────────
        try:
            tokens_used = response.usage.total_tokens if response.usage else max_tokens
            _check_budget_and_add(tokens_used)
            log.debug("ai_companion: tokens used this call=%d", tokens_used)
        except DailyTokenLimitReached:
            raise
        except Exception as track_err:
            log.warning("ai_companion: token tracking failed: %s", track_err)

        return _sanitise(response.choices[0].message.content.strip())
    except DailyTokenLimitReached:
        raise
    except Exception as e:
        log.warning("ai_companion: DeepInfra call failed: %s", e)
        raise RuntimeError(f"DeepInfra backend failed: {e}") from e


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

        # guild_id → custom personality text (loaded from DB, editable via /bikisetpersonality)
        self.guild_personalities: dict[int, str] = {}

        # guild_id → True if Biki is silenced (won't respond to anyone)
        self.guild_silenced: dict[int, bool] = {}

        # guild_id → list of {id, fact_text} dicts (loaded from DB, editable via /bikiremember)
        self.guild_facts: dict[int, list[dict]] = {}

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        try:
            await asyncio.to_thread(_db_init)
            self.allowed_channels = await asyncio.to_thread(_db_load_all)
            self.guild_personalities = await asyncio.to_thread(_db_load_all_personalities)
            self.guild_facts = await asyncio.to_thread(_db_load_all_facts)
            self.guild_moods = await asyncio.to_thread(_db_load_all_moods)
            log.info(
                "ai_companion: loaded channels for %d guild(s), "
                "personalities for %d, facts for %d, moods for %d",
                len(self.allowed_channels),
                len(self.guild_personalities),
                len(self.guild_facts),
                len(self.guild_moods),
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

        personality = self.guild_personalities.get(guild_id, "") if guild_id else ""
        facts = self.guild_facts.get(guild_id, []) if guild_id else []
        try:
            reply = await asyncio.to_thread(
                _call_ai,
                history,
                self._mood_addon(guild_id),
                self._learning_context(guild_id),
                max_tokens,
                personality,
                facts or None,
            )
        except DailyTokenLimitReached:
            log.info("ai_companion: daily token cap reached — returning over-limit reply")
            return random.choice(_OVER_LIMIT_REPLIES)

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

        # 1 second reading pause before Biki starts typing (simulates a human
        # reading the message before responding). Happens once, before the first part.
        await asyncio.sleep(1.0)

        for i, part in enumerate(parts):
            # Show typing indicator for WPM-scaled duration —
            # longer messages = longer typing time
            typing_duration = _typing_seconds(part)
            async with trigger.channel.typing():
                await asyncio.sleep(typing_duration)

            # Send the message immediately after typing ends
            if i == 0 and use_reply:
                try:
                    await trigger.reply(part, mention_author=False)
                except discord.HTTPException:
                    await trigger.channel.send(part)
            else:
                await trigger.channel.send(part)

            # Short human-like pause between parts (for [SPLIT] messages)
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

        personality = self.guild_personalities.get(guild_id, "") if guild_id else ""
        facts = self.guild_facts.get(guild_id, []) if guild_id else []
        try:
            # Random delay so it doesn't feel like Biki is always watching
            await asyncio.sleep(random.uniform(0.5, 2.0))
            response = await asyncio.to_thread(
                _call_ai,
                [{"role": "user", "content": prompt}],
                self._mood_addon(guild_id),
                self._learning_context(guild_id),
                120,  # max_tokens — short and punchy
                personality,
                facts or None,
            )
            if response:
                # Use _send_biki_reply so proactive messages also get the
                # 1s reading pause + WPM-scaled typing simulation
                await self._send_biki_reply(message, response, force_reply=True)
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

        # ── 1b. Silence gate — Biki completely ignores everything ─────────
        if self.guild_silenced.get(guild_id):
            return

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
                    reply = await self._ai_reply(
                        user_id, clean, extra_note=note, guild_id=guild_id,
                        max_tokens=150,
                    )
                    await self._send_biki_reply(message, reply)
                except Exception as exc:
                    log.error("ai_companion: dismiss failed: %s", exc)
                return

            # j. Normal reply ─────────────────────────────────────────────
            # Generate silently, then _send_biki_reply handles:
            #   1s reading pause → WPM-scaled typing → send
            try:
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
        await interaction.response.defer(ephemeral=False)
        try:
            await asyncio.to_thread(_db_set_mood, interaction.guild_id, mood)
        except Exception as exc:
            log.error("bikimood: DB error: %s", exc)
        self.guild_moods[interaction.guild_id] = mood
        labels = {
            "feral":    "😡 FERAL MODE — Biki is feral now. god help us all",
            "sulky":    "😒 SULKY MODE — Biki is moody and will let everyone know it",
            "villain":  "😈 VILLAIN MODE — Biki has entered his villain arc",
            "romantic": "🌹 ROMANTIC MODE — Biki is inexplicably feeling things",
            "unhinged": "🌀 MAX UNHINGED — Biki has ascended beyond normal unhinged",
            "normal":   "😐 NORMAL MODE — Biki is back to baseline (relatively)",
        }
        await interaction.followup.send(
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
        description="Show Biki's config and session stats for this server.",
    )
    @app_commands.guild_only()
    async def bikistats(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        gid = interaction.guild_id

        # ── Config ───────────────────────────────────────────────────────
        mood          = self.guild_moods.get(gid, "normal")
        silenced      = self.guild_silenced.get(gid, False)
        personality   = self.guild_personalities.get(gid, "")
        facts_count   = len(self.guild_facts.get(gid, []))
        personality_preview = (
            personality[:120] + ("…" if len(personality) > 120 else "")
            if personality else "none (default Biki)"
        )

        # ── Session ──────────────────────────────────────────────────────
        total_msgs     = sum(len(v) for v in self.conversations.values())
        total_users    = len(self.conversations)
        spoken_this    = len(self._users_spoken)
        dismissed_cnt  = len(self.dismissed)
        pending_cnt    = len(self._pending)
        processing_cnt = len(self._processing)

        silence_str = "🔇 **SILENCED**" if silenced else "🔊 active"

        await interaction.response.send_message(
            f"**Biki — server config**\n"
            f"• Status: {silence_str}\n"
            f"• Mood: **{mood}**\n"
            f"• Custom personality: {personality_preview}\n"
            f"• Remembered facts: **{facts_count}** (use `/bikifacts` to view)\n"
            f"\n"
            f"**Session stats**\n"
            f"• Conversations in memory: **{total_users}** users / **{total_msgs}** messages\n"
            f"• Spoken to this session: **{spoken_this}** user(s)\n"
            f"• Dismissed by: **{dismissed_cnt}** user(s)\n"
            f"• Processing: **{processing_cnt}** · Pending: **{pending_cnt}**",
            ephemeral=True,
        )


    # ------------------------------------------------------------------
    # /bikitokens — show today's token budget usage
    # ------------------------------------------------------------------

    @app_commands.command(
        name="bikitokens",
        description="Show today's DeepInfra token usage and remaining daily budget.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def bikitokens(self, interaction: discord.Interaction) -> None:
        import datetime as _dt
        today = _dt.date.today().isoformat()
        with _token_lock:
            tracker = _load_token_tracker()

        used  = tracker["total"] if tracker.get("date") == today else 0
        cap   = _effective_cap(tracker)
        left  = max(0, cap - used)
        pct   = (used / cap) * 100
        is_custom = cap != _DAILY_TOKEN_CAP

        if pct >= 100:
            bar_filled = 20
            status = "🔴 **DAILY LIMIT REACHED** — Biki is offline until tomorrow"
        elif pct >= 80:
            bar_filled = round(pct / 5)
            status = "🟠 getting low — watch it"
        elif pct >= 50:
            bar_filled = round(pct / 5)
            status = "🟡 halfway there"
        else:
            bar_filled = round(pct / 5)
            status = "🟢 all good"

        bar = "█" * bar_filled + "░" * (20 - bar_filled)
        cap_label = f"**{cap:,}** *(custom — use `/bikibudget` to change)*" if is_custom else f"**{cap:,}** *(default)*"

        await interaction.response.send_message(
            f"**Biki — Daily Token Budget**\n"
            f"Date: `{today}`\n\n"
            f"`{bar}` {pct:.1f}%\n\n"
            f"• Used today:  **{used:,}** tokens\n"
            f"• Remaining:   **{left:,}** tokens\n"
            f"• Daily cap:   {cap_label}\n\n"
            f"Status: {status}",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /bikibudget — adjust the daily token cap on the fly
    # ------------------------------------------------------------------

    @app_commands.command(
        name="bikibudget",
        description="View or change Biki's daily token cap.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(new_cap="New daily token limit (e.g. 1500000). Leave blank to just view the current cap.")
    async def bikibudget(
        self, interaction: discord.Interaction, new_cap: int | None = None
    ) -> None:
        with _token_lock:
            tracker = _load_token_tracker()
        current_cap = _effective_cap(tracker)

        # ── View mode (no argument) ───────────────────────────────────────
        if new_cap is None:
            is_custom = current_cap != _DAILY_TOKEN_CAP
            label = "custom" if is_custom else "default"
            await interaction.response.send_message(
                f"**Biki — Daily Token Cap**\n"
                f"Current cap: **{current_cap:,}** tokens ({label})\n"
                f"Default cap: **{_DAILY_TOKEN_CAP:,}** tokens\n\n"
                f"To change it: `/bikibudget new_cap:<number>`\n"
                f"To reset to default: `/bikibudget new_cap:{_DAILY_TOKEN_CAP}`",
                ephemeral=True,
            )
            return

        # ── Validation ────────────────────────────────────────────────────
        MIN_CAP = 10_000
        MAX_CAP = 10_000_000

        if new_cap < MIN_CAP:
            await interaction.response.send_message(
                f"❌ Cap must be at least **{MIN_CAP:,}** tokens (you entered `{new_cap:,}`).",
                ephemeral=True,
            )
            return
        if new_cap > MAX_CAP:
            await interaction.response.send_message(
                f"❌ Cap can't exceed **{MAX_CAP:,}** tokens — that's way too high.",
                ephemeral=True,
            )
            return

        # ── Apply ─────────────────────────────────────────────────────────
        await interaction.response.defer(ephemeral=True)
        try:
            await asyncio.to_thread(_set_token_cap, new_cap)
        except Exception as exc:
            log.error("bikibudget: failed to save cap: %s", exc)
            await interaction.followup.send(
                f"❌ Failed to save new cap: `{exc}`", ephemeral=True
            )
            return

        direction = "⬆️ increased" if new_cap > current_cap else "⬇️ decreased"
        is_default = new_cap == _DAILY_TOKEN_CAP
        note = " (reset to default)" if is_default else ""

        await interaction.followup.send(
            f"✅ Daily token cap {direction}{note}.\n"
            f"• Old cap: **{current_cap:,}** tokens\n"
            f"• New cap: **{new_cap:,}** tokens\n\n"
            f"Takes effect immediately. Use `/bikitokens` to monitor usage.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /bikisetpersonality — write a custom personality override for this server
    # ------------------------------------------------------------------

    @app_commands.command(
        name="bikisetpersonality",
        description="Give Biki a custom personality for this server.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def bikisetpersonality(
        self, interaction: discord.Interaction, personality: str
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        try:
            await asyncio.to_thread(_db_set_personality, interaction.guild_id, personality)
            self.guild_personalities[interaction.guild_id] = personality
        except Exception as exc:
            log.error("bikisetpersonality: DB error: %s", exc)
            await interaction.followup.send(
                f"❌ Failed to save personality: `{exc}`", ephemeral=True
            )
            return
        preview = personality[:200] + ("..." if len(personality) > 200 else "")
        await interaction.followup.send(
            f"✅ Biki's personality for this server has been updated.\n"
            f"**Preview:** {preview}",
            ephemeral=True,
        )

    @app_commands.command(
        name="bikiclearpersonality",
        description="Remove the custom personality for this server and revert to Biki's default.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def bikiclearpersonality(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        try:
            await asyncio.to_thread(_db_clear_personality, interaction.guild_id)
            self.guild_personalities.pop(interaction.guild_id, None)
        except Exception as exc:
            log.error("bikiclearpersonality: DB error: %s", exc)
            await interaction.followup.send(
                f"❌ Failed to clear personality: `{exc}`", ephemeral=True
            )
            return
        await interaction.followup.send(
            "✅ Custom personality cleared. Biki is back to his default chaotic self.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /bikisilence — toggle Biki completely silent for this server
    # ------------------------------------------------------------------

    @app_commands.command(
        name="bikisilence",
        description="Toggle Biki's silence mode — when silenced, Biki won't respond to anyone.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def bikisilence(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        currently_silenced = self.guild_silenced.get(interaction.guild_id, False)
        new_state = not currently_silenced
        self.guild_silenced[interaction.guild_id] = new_state

        if new_state:
            await interaction.response.send_message(
                "🔇 Biki is now **silenced**. He won't respond to anyone until you run "
                "`/bikisilence` again to bring him back.",
                ephemeral=False,
            )
        else:
            await interaction.response.send_message(
                "🔊 Biki is **back**. good luck with that.",
                ephemeral=False,
            )


    # ------------------------------------------------------------------
    # /bikiremember — inject a permanent fact Biki will always know
    # ------------------------------------------------------------------

    @app_commands.command(
        name="bikiremember",
        description="Tell Biki something to always remember about this server.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def bikiremember(
        self, interaction: discord.Interaction, fact: str
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        try:
            fact_id = await asyncio.to_thread(_db_add_fact, interaction.guild_id, fact)
            self.guild_facts.setdefault(interaction.guild_id, []).append(
                {"id": fact_id, "fact_text": fact}
            )
        except Exception as exc:
            log.error("bikiremember: DB error: %s", exc)
            await interaction.followup.send(
                f"❌ Failed to save fact: `{exc}`", ephemeral=True
            )
            return
        total = len(self.guild_facts.get(interaction.guild_id, []))
        await interaction.followup.send(
            f"✅ Got it. Biki will remember: **{fact}**\n"
            f"*(fact #{fact_id} — {total} total for this server)*",
            ephemeral=True,
        )

    @app_commands.command(
        name="bikiforget",
        description="Delete a fact Biki knows about this server.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def bikiforget(
        self, interaction: discord.Interaction, fact_id: int
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        try:
            deleted = await asyncio.to_thread(_db_delete_fact, fact_id, interaction.guild_id)
        except Exception as exc:
            log.error("bikiforget: DB error: %s", exc)
            await interaction.followup.send(
                f"❌ Failed to delete fact: `{exc}`", ephemeral=True
            )
            return
        if not deleted:
            await interaction.followup.send(
                f"❌ No fact with ID `{fact_id}` found for this server.", ephemeral=True
            )
            return
        # Remove from in-memory list
        guild_fact_list = self.guild_facts.get(interaction.guild_id, [])
        self.guild_facts[interaction.guild_id] = [
            f for f in guild_fact_list if f["id"] != fact_id
        ]
        await interaction.followup.send(
            f"✅ Fact `#{fact_id}` deleted. Biki has forgotten it.", ephemeral=True
        )

    @app_commands.command(
        name="bikifacts",
        description="List everything Biki currently remembers about this server.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def bikifacts(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        facts = self.guild_facts.get(interaction.guild_id, [])
        if not facts:
            await interaction.response.send_message(
                "Biki doesn't remember anything specific about this server yet. "
                "Use `/bikiremember` to add facts.",
                ephemeral=True,
            )
            return
        lines = [f"`#{f['id']}` — {f['fact_text']}" for f in facts]
        body = "\n".join(lines)
        if len(body) > 1900:
            body = body[:1900] + f"\n... *(showing first entries, {len(facts)} total)*"
        await interaction.response.send_message(
            f"**Things Biki knows about this server ({len(facts)} facts):**\n{body}\n\n"
            f"Use `/bikiforget <id>` to remove one.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AiCompanion(bot))
