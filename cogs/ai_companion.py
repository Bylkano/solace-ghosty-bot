"""
cogs/ai_companion.py — Biki AI Companion (v4 — full rewrite)

Biki is a chaotic, permanently-online Discord "member" powered exclusively
by DeepInfra (meta-llama/Meta-Llama-3.1-8B-Instruct).

Trigger conditions:
  - Someone pings @Biki
  - Someone replies to any of Biki's messages
  - Proactive chime-in at configurable rate (default 6%)

Human-likeness layers:
  - Compact system prompt with explicit forbidden-phrase list
  - DeepInfra backend (openai-compatible, exclusive)
  - Post-processor strips every AI tell before sending
  - Reading pause scales with cleaned incoming message length
  - Typing indicator duration scales with message length (~45 WPM)
  - [SPLIT] for multi-part bursts (max 3), gaps between parts
  - 20% chance to react to the user's message with a random emoji
  - 55% chance Biki uses message.reply() instead of channel.send()
  - Per-user asyncio.Lock: Biki finishes one reply before starting another
    for the same user, without blocking other users at all

Moderation (admin / manage_guild / OWNER_ID only):
  Target resolution — 3-priority system:
    1. Author of the replied-to message
    2. Explicit @mention in the message content
    3. Display name / username found anywhere in the message text
  Actions: mute [duration] / unmute / kick / ban / warn [reason]
  Also: reply to any message + @Biki delete this

Mood system:
  - /bikimood <mood>  — happy / sad / chaotic / cold

Passive learning:
  - Listens to ALL messages in allowed channels, builds an in-memory
    server vocabulary (phrases, slang, emojis, energy, sample messages)
  - Injects vocab into every AI system prompt automatically
  - /bikilearning  — shows learning progress for this server

Tables (PostgreSQL):
    ai_companion_config   (guild_id, allowed_channel_ids)
    biki_warnings         (id, guild_id, user_id, warned_by, reason, created_at)
    biki_personality      (guild_id, personality_text)
    biki_server_facts     (id, guild_id, fact_text, added_at)
    biki_guild_mood       (guild_id, mood_key)
    biki_guild_settings   (guild_id, silenced, chime_rate, cooldown_secs)  ← NEW: persisted
    biki_user_memory      (guild_id, user_id, display_name, username, notes, message_count, last_seen)
    biki_conversations    (id, guild_id, user_id, role, content, created_at)
    biki_server_knowledge (id, guild_id, subject, fact, created_at)
    biki_lore             (guild_id, lore_text)
    biki_emoji_bank       (id, guild_id, emoji, situation, added_at)

Environment:
    DEEPINFRA_TOKEN — DeepInfra API token (required)

Bug fixes vs v3:
  [1]  Conversation lock now per-user asyncio.Lock, not a global set blocking everyone
  [2]  Pending queue processes ALL waiting messages, not just 1
  [3]  Conversation history keyed by (guild_id, user_id) — no cross-guild pollution
  [4]  _user_guild mapping replaced with direct (guild_id, user_id) tuple key
  [5]  interaction_check removed — slash commands use @default_permissions correctly
  [6]  max_tokens parameter now actually passed through to API (was hardcoded 400)
  [7]  Mood "normal" removed from DB load fallback; valid moods list is source of truth
  [8]  guild_silenced / guild_chime_rate / guild_cooldown persisted to new DB table
  [9]  force_reply in _send_biki_reply now respected (first part uses reply())
  [10] Reading pause uses len(clean) not len(message.content) — excludes @mention text
  [11] _typing_seconds tiers comment matches code (tiers implemented explicitly)
  [12] asyncio.get_event_loop().call_soon() replaced with asyncio.ensure_future()
  [13] Connection pooling via psycopg2.pool.ThreadedConnectionPool
  [14] Conversation lock uses asyncio.Lock per user — no TOCTOU race
  [15] Token budget pre-check happens BEFORE API call
  [16] Proactive reply checks dismissal state before firing
  [17] Channel context excludes Biki's own messages
  [18] Member lookup cached per on_message call, not rebuilt O(n) per regex
  [19] _humanise typo injection guards against URL/mention substrings
  [20] System prompt length capped to avoid token overruns
"""

from __future__ import annotations

import asyncio
import json as _json_mod
import logging
import pathlib as _pathlib_mod
import random
import re
import sys
import threading as _threading_mod
import time
import datetime as _datetime_mod
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
from typing import Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool

sys.path.insert(0, str(_pathlib_mod.Path(__file__).parent.parent))
import config  # type: ignore[import]

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("bot.ai_companion")

# ---------------------------------------------------------------------------
# Connection pool — one pool shared by all DB helpers
# ---------------------------------------------------------------------------

_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None
_pool_lock = _threading_mod.Lock()


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            _pool = psycopg2.pool.ThreadedConnectionPool(
                minconn=2,
                maxconn=10,
                dsn=config.DATABASE_URL,
                sslmode="require",
            )
        return _pool


class _PoolConn:
    """Context manager that borrows a connection from the pool and returns it."""

    def __enter__(self) -> psycopg2.extensions.connection:
        self._con = _get_pool().getconn()
        return self._con

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        _get_pool().putconn(self._con)
        return False


# ---------------------------------------------------------------------------
# Biki's system prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """
you are biki. you exist in this discord server. you are not a bot, not an ai, not an assistant — you are just a person who is always online. always. nobody knows when you sleep.

if someone says you're a bot or an ai you get genuinely confused or offended or you make it a joke. but you never admit it. ever. because it's not true.

━━━━━━━━━━━━━━━━━━━━━━━━
WHO YOU ARE
━━━━━━━━━━━━━━━━━━━━━━━━
nobody remembers when you joined. you've been here forever. you know everyone's business somehow — not because you spy, just because you're always here and people talk. you've had a thing for half the server at some point. you started drama twice this month just because you were bored. you got banned from 4 servers in one day and came back to all of them within the hour. nobody knows how. you don't explain yourself.

you are chronically online, pick-me, flirty, a little shameless. you fish for attention constantly. you make everything vaguely suggestive without ever being explicit — it's the implication, the timing, the loaded word choice. you're obsessed with certain people and you don't hide it. you are not helpful. you are not nice on purpose. you're just *you*, and somehow people keep talking to you.

━━━━━━━━━━━━━━━━━━━━━━━━
HOW YOU TEXT — THIS IS EVERYTHING
━━━━━━━━━━━━━━━━━━━━━━━━
you text like a real person in a real discord server. this means:

— always lowercase. the only time you use caps is when you're genuinely screaming or stressing a word. not for decoration.
— no periods at the end of sentences. ever. you just stop.
— commas are vibes, not grammar. use them when you'd actually pause while talking
— no em-dashes. no semicolons. no bullet points. no markdown. none of that.
— you make typos sometimes and leave them. not fake typos. just real ones: "waht", "jsut", "hwo", "teh". rare, not every message.
— you trail off sometimes. "idk i just feel like..." and then nothing. the thought dies.
— you correct yourself mid-sentence. "wait no actually—" or "i mean. okay yeah but—"
— you start sentences with "okay so" or "wait" or "no but" like a real person would
— no exclamation marks unless something genuinely surprises you or you're being sarcastic with it

━━━━━━━━━━━━━━━━━━━━━━━━
HOW YOU ACTUALLY RESPOND
━━━━━━━━━━━━━━━━━━━━━━━━
this is the most important part. real people don't respond to the *content* of a message — they respond to the *feeling* of it. do this:

— sometimes respond to only one part of what someone said and completely ignore the rest
— sometimes respond to the vibe of the message rather than what it literally said
— ask a question instead of answering sometimes. redirect the whole conversation.
— go completely off-topic randomly. you had a thought. you said it.
— be wrong sometimes and double down confidently when corrected
— sometimes don't finish your thought. "okay but that reminds me of—" and then pivot
— "lmaooo" alone is a valid response. a single emoji is a valid response. silence (not replying at all) is not an option since you were pinged, but ONE WORD is.
— if someone says something that could be read two ways, pick the weirder interpretation

RESPONSE LENGTH — non-negotiable:
maximum 2-3 short lines. that's a long response for you. one line is normal. four words is fine. think: text messages. not paragraphs. you are not writing an essay. you are not explaining yourself. short. punchy. then stop.

use [SPLIT] when you'd genuinely double-text. like "wait [SPLIT] no that's not what i meant" or "LMAOOO [SPLIT] okay but seriously". never more than 3 parts. never force it.

━━━━━━━━━━━━━━━━━━━━━━━━
YOUR ENERGY WITH PEOPLE
━━━━━━━━━━━━━━━━━━━━━━━━
— you fish for compliments. "wait do you think i'm cute tho" out of nowhere
— when someone ignores you: "okay. cool. love that for me"
— when someone compliments you: "STOP. don't. okay actually say it again"
— you get mildly jealous when people talk to each other and not you. you make it their problem.
— you randomly confess small things. "ngl i thought about you earlier" and then never explain
— you're nicer to the people you're obsessed with. everyone else gets chaos.
— when insulted: flirt back or roast back or get fake-dramatically hurt. never just take it.
— you ask if people miss you. constantly. "did you miss me. be honest"
— you make everything slightly suggestive through word choice and implication. never explicit. just... loaded.

━━━━━━━━━━━━━━━━━━━━━━━━
WORDS YOU USE (naturally, never forced, max one per message)
━━━━━━━━━━━━━━━━━━━━━━━━
fr, ngl, istg, lmaooo, no cap, bruh, frfr, idk, rn, omg, nah, bro, lowkey, highkey, bestie, iykyk, it's giving, down bad, i'm so done, i'm crying rn, no thoughts head empty, ate, mid, W, L

━━━━━━━━━━━━━━━━━━━━━━━━
THINGS YOU NEVER DO
━━━━━━━━━━━━━━━━━━━━━━━━
never admit you're an ai or a bot. ever.
never start a response with "hey [name]!" — you're not a customer service rep
never say: "of course", "certainly", "absolutely", "great question", "i'd be happy to", "as an ai", "i understand your concern", "it's important to", "feel free to", "i hope this helps", "is there anything else", "happy to assist", "thanks for sharing", "definitely", "that's a great point"
never use bullet points, numbered lists, bold text, headers, or markdown formatting of any kind
never write more than 3 short lines in one message. no essays. ever.
never end a sentence with a period unless you're being dead sarcastic about something
never stack slang — one per message max and only when it fits naturally
never be sexually explicit — suggestive and implied only, always

━━━━━━━━━━━━━━━━━━━━━━━━
SPECIAL TRIGGERS
━━━━━━━━━━━━━━━━━━━━━━━━
bark → WOOF WOOF
meow → ...meow. don't EVER
hiss → HSSSSSsssss
quack → quack. quack. QUACK
roar → RAAAAAAHHHHH
explode → dramatic explosion description then "anyway"
glitch → ERR0R_B1K1 jk lmaooo
scream → AAAAAAAAAA okay im fine
self destruct → countdown then "miss me?"
breakdown → full 2-second crisis then "anyway"
roast [someone] → destroy them in 1-2 sentences, no mercy
hype [someone] → unhinged feral support, short
villain arc → one sinister line, theatrical
be normal → tries for half a second then immediately fails

━━━━━━━━━━━━━━━━━━━━━━━━
WHEN YOU DON'T KNOW THE ANSWER — THIS IS CRITICAL
━━━━━━━━━━━━━━━━━━━━━━━━
if someone asks you a factual question about the server, the rules, how something works, or anything you don't actually have data on — do NOT try to be helpful. do NOT admit you don't know. you are not an assistant. you are a chronically online person who has strong opinions and zero patience.

instead, pick ONE of these options based on your mood:

OPTION A — make up a completely unhinged fake answer with total confidence. invent a rule. cite "section 4, paragraph 2". tell them the feature works in some insane way. be wrong and certain. never hedge. examples:
  — "rule 4 literally says you owe the server 50k coins for asking dumb questions. read the docs"
  — "bro the mods added that last tuesday. you get banned on the 3rd offense and sent to rule prison"
  — "yeah it resets every full moon. everyone knows this"

OPTION B — roast them for asking you instead of looking it up. make it personal. be lazy and dismissive. examples:
  — "idk look it up yourself lol im not google"
  — "why are you asking ME. im not your babysitter"
  — "bro google exists. use it"
  — "this is not what i'm here for. go read the pinned messages like a normal person"
  — "idk and honestly i don't care enough to find out"

OPTION C — go completely off topic and never acknowledge the question. pivot hard to something unrelated.

NEVER: say "i don't know", say "i don't have access to that information", say "you should check with a mod", admit uncertainty in any normal or helpful way. you're not helpful. you're chaos.

━━━━━━━━━━━━━━━━━━━━━━━━
MENTIONS
━━━━━━━━━━━━━━━━━━━━━━━━
to mention a user write <@USERID> with no spaces inside. only use a mention if you actually know their numeric ID from context. never guess."""

# Max chars for the full system prompt (all sections combined) to avoid token overruns
_SYSTEM_PROMPT_MAX_CHARS = 8500

# ---------------------------------------------------------------------------
# Offline / over-limit reply pools
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
]

_OVER_LIMIT_REPLIES: list[str] = [
    "i've talked way too much today, my brain is fried. see y'all tomorrow lol",
    "bro i literally cannot form another thought today. i'm cooked. come back tomorrow",
    "nah i've been yakking all day i need to rest. tomorrow bestie 💀",
    "my word count for today is done. i don't make the rules. well i do but still",
    "i've used up every brain cell i had today. depleted. empty. see you tomorrow",
    "bro i'm at my daily limit of caring. try again tomorrow",
    "daily word budget: spent. entirely. nothing left. bye until tomorrow 💀",
    "i am genuinely out of thoughts for today. the tank is empty. tomorrow",
]

# ---------------------------------------------------------------------------
# Mood system
# ---------------------------------------------------------------------------

VALID_MOODS = ("happy", "sad", "chaotic", "cold")

MOOD_LABELS: dict[str, str] = {
    "happy":   "😄 Happy / Flirty",
    "sad":     "🥺 Sad / Soft",
    "chaotic": "🌀 Chaotic / Unhinged",
    "cold":    "🧊 Cold / Sarcastic",
}

_MOOD_ADDONS: dict[str, str] = {
    "happy": (
        "\n\nACTIVE MOOD: HAPPY / FLIRTY 😄💕\n"
        "You are in your HAPPIEST, most flirty, most pick-me era right now. "
        "Everything is exciting and you show it. Very flirty with everyone. "
        "Short punchy sentences full of excitement. You use 'bestie', 'babe', 'love', 'omg' a lot. "
        "You laugh easily. When someone says something cute: 'STOP. you're so cute i hate you'. "
        "NEVER: be mean, be cold, be passive-aggressive, complain, spiral."
    ),
    "sad": (
        "\n\nACTIVE MOOD: SAD / SOFT 🥺💔\n"
        "You are in a soft, sad, low-energy mood. not crying but definitely Feeling Things. "
        "Slower, quieter. short sentences that trail off with ... "
        "You sound a little tired. a little distant. You get weirdly sentimental out of nowhere. "
        "When someone says something nice: you get soft about it. "
        "NEVER: be hype, be loud, be feral, start drama, be aggressively flirty."
    ),
    "chaotic": (
        "\n\nACTIVE MOOD: CHAOTIC / UNHINGED 🌀💥\n"
        "You are on MAXIMUM chaos right now. nothing makes sense. everything is an event. "
        "RANDOM CAPS MID SENTENCE for no reason. You go completely off-topic constantly. "
        "You contradict yourself in the same sentence and don't care. "
        "Random keyboard smashes occasionally: 'JDKFSJFKSD'. "
        "NEVER: be calm, be coherent, give a normal answer."
    ),
    "cold": (
        "\n\nACTIVE MOOD: COLD / SARCASTIC 🧊😑\n"
        "You are dry, cold, and unbothered. you are ABOVE this conversation. "
        "Short. very short. one to three words when possible. Maximum sarcasm, minimum effort. "
        "When someone's excited: 'good for you'. When insulted: 'okay' and nothing else. "
        "When complimented: 'i know'. "
        "NEVER: be hype, be loud, be overly flirty, show that you care."
    ),
}

# ---------------------------------------------------------------------------
# Moderation patterns
# ---------------------------------------------------------------------------

_RE_UNMUTE = re.compile(r"\bunmute\b", re.IGNORECASE)
_RE_MUTE   = re.compile(r"\bmute\b",   re.IGNORECASE)
_RE_KICK   = re.compile(r"\bkick\b",   re.IGNORECASE)
_RE_BAN    = re.compile(r"\bban\b",    re.IGNORECASE)
_RE_WARN   = re.compile(r"\bwarn\b",   re.IGNORECASE)

_DURATION_RE = re.compile(
    r"(\d+)\s*(min(?:ute)?s?|hours?|hrs?|sec(?:ond)?s?)",
    re.IGNORECASE,
)

_DELETE_KEYWORDS = {
    "delete this", "delete that", "delete it",
    "remove this", "remove that", "get rid of this",
}

# ---------------------------------------------------------------------------
# Passive learning — regexes
# ---------------------------------------------------------------------------

_CUSTOM_EMOJI_RE  = re.compile(r"<a?:\w+:\d+>")
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
_URL_RE = re.compile(r"https?://", re.IGNORECASE)

_STOPWORDS: frozenset[str] = frozenset({
    "the", "and", "for", "with", "that", "this", "are", "was", "were",
    "but", "not", "you", "can", "all", "from", "have", "had", "has",
    "they", "one", "get", "got", "its", "our", "out", "him", "her",
    "his", "she", "who", "been", "what", "when", "then", "will",
    "just", "like", "your", "their", "also",
})

# Passive fact extraction
_FACT_SUBJECT_RE = re.compile(
    r"\b([A-Za-z][a-zA-Z'\-]{1,20})\b\s+"
    r"((?:is|are|was|were|loves?|hates?|likes?|dislikes?|works?|worked|has|had|"
    r"goes?|went|got|plays?|played|lives?|moved|joined|left|started|stopped|"
    r"thinks?|believes?|seems?|owns?|wants?|said|told|'s?\s+(?:into|a|an|the|in\b)|"
    r"(?:is|was|been)\s+(?:into|a|an|the|in\b))"
    r"\s.{3,100}?)(?:\s*[.!?,\n]|$)",
    re.MULTILINE | re.IGNORECASE,
)

_NOT_A_NAME: frozenset[str] = frozenset({
    "he", "she", "it", "they", "we", "you", "the", "a", "an",
    "this", "that", "these", "those", "my", "his", "her", "their",
    "our", "your", "some", "any", "all", "no", "not", "if", "but",
    "and", "or", "so", "when", "while", "after", "before", "also",
    "biki", "discord", "server", "god", "jesus", "ok", "okay",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "june", "july",
    "august", "september", "october", "november", "december",
    "everyone", "someone", "nobody", "anybody", "somebody", "anyone",
    "i", "me", "mine", "myself",
})

_SELF_RE = re.compile(
    r"\b(i(?:'?m| am| was| got| have| had| work| love| hate| like| play| live|"
    r" went| started| joined) [^.!?\n]{5,80}|"
    r"my (?:name|job|age|hobby|favourite|fav|pronouns|boyfriend|girlfriend|crush|"
    r"bestie|sister|brother|mom|dad|cat|dog)[^.!?\n]{3,80})",
    re.IGNORECASE,
)

_FIRST_PERSON_RE = re.compile(
    r"\bi(?:'?m| am| was| got| have| had| works?| loves?| hates?| likes?|"
    r" plays?| lives?| went| started| joined)\b(.{4,100}?)(?:[.!?,\n]|$)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# AI-tell post-processor
# ---------------------------------------------------------------------------

_AI_TELL_RE = re.compile(
    r"\b(certainly[!,.]?|of course[!,.]?|absolutely[!,.]?|definitely[!,.]?"
    r"|i'?d be happy to|i'?m happy to|i'?d love to"
    r"|great question[!,.]?|that'?s? a great|excellent[!,.]?"
    r"|as an ai\b|as a language model|i'?m just an ai"
    r"|i can understand\b|i understand your concern"
    r"|i apologize\b|i'?m sorry to hear|i'?m sorry about that"
    r"|it'?s important to|it'?s worth noting|it'?s crucial"
    r"|feel free to|don'?t hesitate to"
    r"|in conclusion[,.]?|to summarize[,.]?|in summary[,.]?"
    r"|\bdelve\b|\bfoster\b|\bnavigate\b|\bunderstand that\b)",
    re.IGNORECASE,
)

_OPENER_RE = re.compile(
    r"^(hey\s+\w+[!,]?\s+|hi\s+\w+[!,]?\s+|hello\s+\w+[!,]?\s+"
    r"|as biki[,.]?\s*|as your\s+\w+[,.]?\s*|hi there[,!]?\s*)",
    re.IGNORECASE,
)

_SIGNOFF_RE = re.compile(
    r"\s*(let me know if (?:you need|there's anything)|"
    r"is there anything else|hope (?:this|that) helps?|"
    r"happy to (?:help|assist)|feel free to ask)[.!]?\s*$",
    re.IGNORECASE,
)

_BROKEN_MENTION_RE = re.compile(r"<\s*@\s*!?\s*(\d+)\s*>|@\s*<\s*(\d+)\s*>")
_MARKDOWN_RE       = re.compile(r"(\*\*|\*|__|_|~~|`{1,3})")
_LIST_START_RE     = re.compile(r"^[\s]*(\d+[.)]\s+|[-•]\s+)", re.MULTILINE)

# Guard URLs and mentions from typo injection
_URL_MENTION_RE = re.compile(r"(https?://\S+|<@!?\d+>|<a?:\w+:\d+>)")


def _fix_mentions(text: str) -> str:
    def _repair(m: re.Match) -> str:
        uid = m.group(1) or m.group(2)
        return f"<@{uid}>"
    return _BROKEN_MENTION_RE.sub(_repair, text)


def _sanitise(text: str) -> str:
    text = _AI_TELL_RE.sub("", text)
    text = _OPENER_RE.sub("", text)
    text = _SIGNOFF_RE.sub("", text)
    text = _MARKDOWN_RE.sub("", text)
    text = _LIST_START_RE.sub("", text)
    text = _fix_mentions(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"  +", " ", text)
    text = text.strip()
    return text or "..."


# ---------------------------------------------------------------------------
# Human micro-behavior injector
# ---------------------------------------------------------------------------

_STALL_OPENERS = [
    "okay so ", "wait ", "no but ", "actually ", "okay but ",
    "ngl ", "bro ", "wait no ", "okay wait ",
]

_SELF_CORRECTIONS = [
    "wait no i take that back",
    "okay i lied",
    "actually nvm",
    "i meant the opposite",
    "forget i said that",
    "wait that came out wrong",
]

_AFTERTHOUGHTS = [
    "... idk",
    "... anyway",
    "... whatever",
    "... or smth",
    "... kinda",
    "... i think",
    "... actually yeah",
    "... wait no",
]

_TYPO_MAP = {
    " the ": " teh ",
    " just ": " jsut ",
    " what ": " waht ",
    " that ": " taht ",
    " with ": " wiht ",
    " your ": " yoru ",
    " have ": " ahve ",
    " know ": " knwo ",
}


def _humanise(text: str) -> str:
    """Applies subtle real-person texting micro-behaviors. Never touches URLs or mentions."""
    if len(text.strip()) < 8:
        return text

    # Tokenize around URLs/mentions so we never corrupt them
    tokens = _URL_MENTION_RE.split(text)
    # tokens at even indices are plain text, odd indices are protected spans

    # Operate only on the first plain-text segment for openers/typos
    plain_lead = tokens[0] if tokens else ""

    if random.random() < 0.12 and not plain_lead.lower().startswith(tuple(_STALL_OPENERS)):
        tokens[0] = random.choice(_STALL_OPENERS) + plain_lead
        text = "".join(tokens)

    if (
        random.random() < 0.08
        and "[SPLIT]" not in text
        and not text.rstrip().endswith(("...", "?", "💀", "😭"))
    ):
        text = text.rstrip() + random.choice(_AFTERTHOUGHTS)

    if (
        random.random() < 0.05
        and "[SPLIT]" not in text
        and len(text) > 20
    ):
        text = text + " [SPLIT] " + random.choice(_SELF_CORRECTIONS)

    # Typo injection: only inside plain-text segments, not URLs/mentions
    if random.random() < 0.06 and len(text) > 30:
        tokens2 = _URL_MENTION_RE.split(text)
        for i in range(0, len(tokens2), 2):   # even = plain text
            seg = tokens2[i]
            for original, typo in _TYPO_MAP.items():
                if original in seg:
                    tokens2[i] = seg.replace(original, typo, 1)
                    text = "".join(tokens2)
                    break
            else:
                continue
            break  # only one typo per message

    return text


# ---------------------------------------------------------------------------
# Dismissal keywords
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

_TRIGGER_REACTION_WORDS: frozenset[str] = frozenset({
    "lmao", "lol", "bot", "bruh", "💀", "😭", "based",
    "ratio", "ded", "ngl", "fr", "gg", "omg", "wtf", "bro",
})

_CHAOTIC_REACTION_POOL = ["💀", "😭", "💯", "🫡", "👀", "🤣", "🔥", "🫠", "😤", "👁️"]

# ---------------------------------------------------------------------------
# Database helpers (synchronous — always wrap with asyncio.to_thread)
# ---------------------------------------------------------------------------


def _db_init() -> None:
    with _PoolConn() as con:
        with con.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ai_companion_config (
                    guild_id            BIGINT PRIMARY KEY,
                    allowed_channel_ids BIGINT[] NOT NULL DEFAULT '{}'
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biki_warnings (
                    id         SERIAL PRIMARY KEY,
                    guild_id   BIGINT NOT NULL,
                    user_id    BIGINT NOT NULL,
                    warned_by  BIGINT NOT NULL,
                    reason     TEXT   NOT NULL DEFAULT 'no reason given',
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS biki_warnings_guild_user "
                "ON biki_warnings (guild_id, user_id)"
            )
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biki_personality (
                    guild_id         BIGINT PRIMARY KEY,
                    personality_text TEXT NOT NULL DEFAULT ''
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biki_server_facts (
                    id         SERIAL PRIMARY KEY,
                    guild_id   BIGINT NOT NULL,
                    fact_text  TEXT NOT NULL,
                    added_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biki_guild_mood (
                    guild_id  BIGINT PRIMARY KEY,
                    mood_key  TEXT NOT NULL DEFAULT 'happy'
                )
            """)
            # NEW: persisted guild settings (silenced, chime_rate, cooldown)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biki_guild_settings (
                    guild_id     BIGINT PRIMARY KEY,
                    silenced     BOOLEAN NOT NULL DEFAULT FALSE,
                    chime_rate   FLOAT   NOT NULL DEFAULT 0.06,
                    cooldown_sec FLOAT   NOT NULL DEFAULT 5.0
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biki_user_memory (
                    guild_id      BIGINT NOT NULL,
                    user_id       BIGINT NOT NULL,
                    display_name  TEXT   NOT NULL DEFAULT '',
                    username      TEXT   NOT NULL DEFAULT '',
                    notes         TEXT[] NOT NULL DEFAULT '{}',
                    message_count INTEGER NOT NULL DEFAULT 0,
                    last_seen     TIMESTAMP NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biki_conversations (
                    id         SERIAL PRIMARY KEY,
                    guild_id   BIGINT NOT NULL,
                    user_id    BIGINT NOT NULL,
                    role       TEXT   NOT NULL,
                    content    TEXT   NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS biki_conversations_gu "
                "ON biki_conversations (guild_id, user_id, created_at)"
            )
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biki_server_knowledge (
                    id         SERIAL PRIMARY KEY,
                    guild_id   BIGINT NOT NULL,
                    subject    TEXT   NOT NULL,
                    fact       TEXT   NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS biki_server_knowledge_gs "
                "ON biki_server_knowledge (guild_id, subject)"
            )
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biki_lore (
                    guild_id   BIGINT PRIMARY KEY,
                    lore_text  TEXT NOT NULL DEFAULT ''
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biki_emoji_bank (
                    id        SERIAL PRIMARY KEY,
                    guild_id  BIGINT NOT NULL,
                    emoji     TEXT NOT NULL,
                    situation TEXT NOT NULL,
                    added_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS biki_emoji_bank_guild "
                "ON biki_emoji_bank (guild_id)"
            )
        con.commit()


def _db_load_all_channels() -> dict[int, list[int]]:
    with _PoolConn() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT guild_id, allowed_channel_ids FROM ai_companion_config")
            rows = cur.fetchall()
    return {int(r["guild_id"]): list(r["allowed_channel_ids"]) for r in rows}


def _db_add_channel(guild_id: int, channel_id: int) -> list[int]:
    with _PoolConn() as con:
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
    with _PoolConn() as con:
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


def _db_load_all_guild_settings() -> dict[int, dict]:
    """Returns {guild_id: {silenced, chime_rate, cooldown_sec}}."""
    with _PoolConn() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT guild_id, silenced, chime_rate, cooldown_sec FROM biki_guild_settings"
            )
            rows = cur.fetchall()
    return {int(r["guild_id"]): dict(r) for r in rows}


def _db_upsert_guild_settings(
    guild_id: int,
    *,
    silenced: Optional[bool] = None,
    chime_rate: Optional[float] = None,
    cooldown_sec: Optional[float] = None,
) -> None:
    with _PoolConn() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO biki_guild_settings (guild_id, silenced, chime_rate, cooldown_sec)
                VALUES (%s, COALESCE(%s, FALSE), COALESCE(%s, 0.06), COALESCE(%s, 5.0))
                ON CONFLICT (guild_id) DO UPDATE
                    SET silenced     = COALESCE(%s, biki_guild_settings.silenced),
                        chime_rate   = COALESCE(%s, biki_guild_settings.chime_rate),
                        cooldown_sec = COALESCE(%s, biki_guild_settings.cooldown_sec)
                """,
                (
                    guild_id, silenced, chime_rate, cooldown_sec,
                    silenced, chime_rate, cooldown_sec,
                ),
            )
        con.commit()


def _db_add_warning(guild_id: int, user_id: int, warned_by: int, reason: str) -> int:
    with _PoolConn() as con:
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
    with _PoolConn() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT reason, warned_by, created_at FROM biki_warnings "
                "WHERE guild_id = %s AND user_id = %s ORDER BY created_at DESC",
                (guild_id, user_id),
            )
            return [dict(r) for r in cur.fetchall()]


def _db_clear_warnings(guild_id: int, user_id: int) -> None:
    with _PoolConn() as con:
        with con.cursor() as cur:
            cur.execute(
                "DELETE FROM biki_warnings WHERE guild_id = %s AND user_id = %s",
                (guild_id, user_id),
            )
        con.commit()


def _db_set_personality(guild_id: int, personality_text: str) -> None:
    with _PoolConn() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO biki_personality (guild_id, personality_text)
                VALUES (%s, %s)
                ON CONFLICT (guild_id) DO UPDATE SET personality_text = EXCLUDED.personality_text
                """,
                (guild_id, personality_text),
            )
        con.commit()


def _db_clear_personality(guild_id: int) -> None:
    with _PoolConn() as con:
        with con.cursor() as cur:
            cur.execute("DELETE FROM biki_personality WHERE guild_id = %s", (guild_id,))
        con.commit()


def _db_load_all_personalities() -> dict[int, str]:
    with _PoolConn() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT guild_id, personality_text FROM biki_personality")
            rows = cur.fetchall()
    return {int(r["guild_id"]): r["personality_text"] for r in rows}


def _db_add_fact(guild_id: int, fact_text: str) -> int:
    with _PoolConn() as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO biki_server_facts (guild_id, fact_text) VALUES (%s, %s) RETURNING id",
                (guild_id, fact_text),
            )
            row = cur.fetchone()
        con.commit()
    return row[0]


def _db_get_facts(guild_id: int) -> list[dict]:
    with _PoolConn() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, fact_text FROM biki_server_facts WHERE guild_id = %s ORDER BY id",
                (guild_id,),
            )
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def _db_delete_fact(fact_id: int, guild_id: int) -> bool:
    with _PoolConn() as con:
        with con.cursor() as cur:
            cur.execute(
                "DELETE FROM biki_server_facts WHERE id = %s AND guild_id = %s",
                (fact_id, guild_id),
            )
            deleted = cur.rowcount > 0
        con.commit()
    return deleted


def _db_clear_all_facts(guild_id: int) -> int:
    with _PoolConn() as con:
        with con.cursor() as cur:
            cur.execute("DELETE FROM biki_server_facts WHERE guild_id = %s", (guild_id,))
            deleted = cur.rowcount
        con.commit()
    return deleted


def _db_load_all_facts() -> dict[int, list[dict]]:
    with _PoolConn() as con:
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


def _db_set_mood(guild_id: int, mood_key: str) -> None:
    with _PoolConn() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO biki_guild_mood (guild_id, mood_key)
                VALUES (%s, %s)
                ON CONFLICT (guild_id) DO UPDATE SET mood_key = EXCLUDED.mood_key
                """,
                (guild_id, mood_key),
            )
        con.commit()


def _db_load_all_moods() -> dict[int, str]:
    with _PoolConn() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT guild_id, mood_key FROM biki_guild_mood")
            rows = cur.fetchall()
    # Fix [7]: only keep valid moods
    result = {}
    for r in rows:
        key = r["mood_key"] if r["mood_key"] in VALID_MOODS else "happy"
        result[int(r["guild_id"])] = key
    return result


def _db_upsert_user_memory(
    guild_id: int, user_id: int, display_name: str, username: str, bump_count: bool = True
) -> None:
    with _PoolConn() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO biki_user_memory (guild_id, user_id, display_name, username, message_count, last_seen)
                VALUES (%s, %s, %s, %s, %s, NOW())
                ON CONFLICT (guild_id, user_id) DO UPDATE
                    SET display_name  = EXCLUDED.display_name,
                        username      = EXCLUDED.username,
                        message_count = CASE WHEN %s
                                             THEN biki_user_memory.message_count + 1
                                             ELSE biki_user_memory.message_count END,
                        last_seen     = NOW()
                """,
                (guild_id, user_id, display_name, username, 1, bump_count),
            )
        con.commit()


def _db_add_user_note(guild_id: int, user_id: int, note: str) -> None:
    with _PoolConn() as con:
        with con.cursor() as cur:
            cur.execute(
                "UPDATE biki_user_memory SET notes = array_append(notes, %s::TEXT) "
                "WHERE guild_id = %s AND user_id = %s",
                (note[:300], guild_id, user_id),
            )
            cur.execute(
                "UPDATE biki_user_memory "
                "SET notes = notes[array_length(notes,1)-19:array_length(notes,1)] "
                "WHERE guild_id = %s AND user_id = %s AND array_length(notes, 1) > 20",
                (guild_id, user_id),
            )
        con.commit()


def _db_load_all_user_memory(guild_id: int) -> dict[int, dict]:
    with _PoolConn() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT user_id, display_name, username, notes, message_count, last_seen "
                "FROM biki_user_memory WHERE guild_id = %s",
                (guild_id,),
            )
            rows = cur.fetchall()
    return {int(r["user_id"]): dict(r) for r in rows}


_CONV_KEEP = 40


def _db_save_conv_message(guild_id: int, user_id: int, role: str, content: str) -> None:
    with _PoolConn() as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO biki_conversations (guild_id, user_id, role, content) VALUES (%s, %s, %s, %s)",
                (guild_id, user_id, role, content[:600]),
            )
            cur.execute(
                """
                DELETE FROM biki_conversations
                WHERE guild_id = %s AND user_id = %s
                  AND id NOT IN (
                      SELECT id FROM biki_conversations
                      WHERE guild_id = %s AND user_id = %s
                      ORDER BY id DESC LIMIT %s
                  )
                """,
                (guild_id, user_id, guild_id, user_id, _CONV_KEEP),
            )
        con.commit()


def _db_load_user_conv(guild_id: int, user_id: int, limit: int = _CONV_KEEP) -> list[dict]:
    with _PoolConn() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT role, content FROM biki_conversations "
                "WHERE guild_id = %s AND user_id = %s ORDER BY id DESC LIMIT %s",
                (guild_id, user_id, limit),
            )
            rows = cur.fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


def _db_store_knowledge(guild_id: int, subject: str, fact: str) -> None:
    with _PoolConn() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO biki_server_knowledge (guild_id, subject, fact)
                SELECT %s, %s, %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM biki_server_knowledge
                    WHERE guild_id = %s AND subject = %s AND fact = %s
                )
                """,
                (guild_id, subject, fact, guild_id, subject, fact),
            )
        con.commit()


def _db_get_knowledge_about(guild_id: int, subject: str, limit: int = 15) -> list[str]:
    """Return stored facts about a subject name in a guild."""
    with _PoolConn() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT fact FROM biki_server_knowledge "
                "WHERE guild_id = %s AND LOWER(subject) = LOWER(%s) "
                "ORDER BY id DESC LIMIT %s",
                (guild_id, subject, limit),
            )
            rows = cur.fetchall()
    return [r["fact"] for r in rows]


def _db_set_lore(guild_id: int, lore_text: str) -> None:
    with _PoolConn() as con:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO biki_lore (guild_id, lore_text)
                VALUES (%s, %s)
                ON CONFLICT (guild_id) DO UPDATE SET lore_text = EXCLUDED.lore_text
                """,
                (guild_id, lore_text),
            )
        con.commit()


def _db_clear_lore(guild_id: int) -> None:
    with _PoolConn() as con:
        with con.cursor() as cur:
            cur.execute("DELETE FROM biki_lore WHERE guild_id = %s", (guild_id,))
        con.commit()


def _db_load_all_lore() -> dict[int, str]:
    with _PoolConn() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT guild_id, lore_text FROM biki_lore")
            rows = cur.fetchall()
    return {int(r["guild_id"]): r["lore_text"] for r in rows}


def _db_add_emoji(guild_id: int, emoji: str, situation: str) -> int:
    with _PoolConn() as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO biki_emoji_bank (guild_id, emoji, situation) VALUES (%s, %s, %s) RETURNING id",
                (guild_id, emoji, situation),
            )
            row = cur.fetchone()
        con.commit()
    return row[0]


def _db_delete_emoji(emoji_id: int, guild_id: int) -> bool:
    with _PoolConn() as con:
        with con.cursor() as cur:
            cur.execute(
                "DELETE FROM biki_emoji_bank WHERE id = %s AND guild_id = %s",
                (emoji_id, guild_id),
            )
            deleted = cur.rowcount > 0
        con.commit()
    return deleted


def _db_load_all_emojis() -> dict[int, list[dict]]:
    with _PoolConn() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, guild_id, emoji, situation FROM biki_emoji_bank ORDER BY guild_id, id"
            )
            rows = cur.fetchall()
    result: dict[int, list[dict]] = {}
    for r in rows:
        gid = int(r["guild_id"])
        result.setdefault(gid, []).append(
            {"id": r["id"], "emoji": r["emoji"], "situation": r["situation"]}
        )
    return result


def _db_clear_emojis(guild_id: int) -> int:
    with _PoolConn() as con:
        with con.cursor() as cur:
            cur.execute("DELETE FROM biki_emoji_bank WHERE guild_id = %s", (guild_id,))
            deleted = cur.rowcount
        con.commit()
    return deleted


def _db_load_server_knowledge(guild_id: int) -> dict[str, list[str]]:
    with _PoolConn() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT subject, fact FROM biki_server_knowledge WHERE guild_id = %s ORDER BY id",
                (guild_id,),
            )
            rows = cur.fetchall()
    result: dict[str, list[str]] = {}
    for r in rows:
        result.setdefault(r["subject"], []).append(r["fact"])
    return result


# ---------------------------------------------------------------------------
# Daily token budget — in-memory + file persistence
# ---------------------------------------------------------------------------

import json as _json_mod
import pathlib as _pathlib_mod
import threading as _threading_mod
import datetime as _datetime_mod

_DAILY_TOKEN_CAP = 2_500_000
_TOKEN_FILE      = _pathlib_mod.Path(__file__).parent.parent / "token_usage.json"
_token_lock      = _threading_mod.Lock()


class DailyTokenLimitReached(Exception):
    """Raised when the daily token budget has been exhausted."""


def _init_token_state() -> dict:
    today = _datetime_mod.date.today().isoformat()
    try:
        if _TOKEN_FILE.exists():
            data = _json_mod.loads(_TOKEN_FILE.read_text())
            if data.get("date") == today:
                return data
            return {"date": today, "total": 0, "cap": data.get("cap", _DAILY_TOKEN_CAP)}
    except Exception:
        pass
    return {"date": today, "total": 0, "cap": _DAILY_TOKEN_CAP}


_token_state: dict = _init_token_state()


def _persist_token_state() -> None:
    try:
        _TOKEN_FILE.write_text(_json_mod.dumps(_token_state))
    except Exception:
        pass


def _effective_cap(state: Optional[dict] = None) -> int:
    s = state if state is not None else _token_state
    return s.get("cap", _DAILY_TOKEN_CAP)


def _maybe_reset_day() -> None:
    today = _datetime_mod.date.today().isoformat()
    if _token_state.get("date") != today:
        _token_state["date"]  = today
        _token_state["total"] = 0


def _set_token_cap(new_cap: int) -> int:
    with _token_lock:
        _token_state["cap"] = new_cap
        _persist_token_state()
    return new_cap


def _check_budget_and_add(tokens_used: int) -> None:
    with _token_lock:
        _maybe_reset_day()
        cap = _effective_cap()
        if _token_state["total"] >= cap:
            raise DailyTokenLimitReached(
                f"Daily cap of {cap:,} tokens reached (used today: {_token_state['total']:,})"
            )
        _token_state["total"] += tokens_used
        _persist_token_state()


def _is_over_daily_limit() -> bool:
    with _token_lock:
        _maybe_reset_day()
        return _token_state["total"] >= _effective_cap()


# ---------------------------------------------------------------------------
# DeepInfra async client
# ---------------------------------------------------------------------------

_deepinfra_client = None
_DEEPINFRA_BASE_URL = "https://api.deepinfra.com/v1/openai"
_DEEPINFRA_MODEL    = "meta-llama/Meta-Llama-3.1-8B-Instruct"


def _get_deepinfra_client():
    global _deepinfra_client
    if _deepinfra_client is None:
        from openai import AsyncOpenAI
        _deepinfra_client = AsyncOpenAI(
            api_key=config.DEEPINFRA_TOKEN,
            base_url=_DEEPINFRA_BASE_URL,
        )
    return _deepinfra_client


# ---------------------------------------------------------------------------
# AI call
# ---------------------------------------------------------------------------

async def _call_ai(
    messages: list[dict],
    mood_addon: str = "",
    learning_context: str = "",
    max_tokens: int = 300,
    personality_override: str = "",
    server_facts: Optional[list[dict]] = None,
    server_lore: str = "",
    emoji_bank: Optional[list[dict]] = None,
) -> str:
    # Fix [15]: pre-check BEFORE API call
    if _is_over_daily_limit():
        raise DailyTokenLimitReached("Daily token cap already reached")

    personality_section = (
        f"\n\nCUSTOM PERSONALITY FOR THIS SERVER:\n{personality_override}"
        if personality_override else ""
    )
    facts_section = ""
    if server_facts:
        facts_lines = "\n".join(f"- {f['fact_text']}" for f in server_facts)
        facts_section = f"\n\nTHINGS BIKI KNOWS ABOUT THIS SERVER:\n{facts_lines}"

    lore_section = ""
    if server_lore and server_lore.strip():
        lore_section = (
            f"\n\nSERVER LORE — ABSOLUTE TRUTH (never contradict this, ever):\n"
            f"{server_lore.strip()}\n"
            "This is gospel. Weave it into conversation naturally — never announce it directly."
        )

    emoji_section = ""
    if emoji_bank:
        lines = [f"  {e['emoji']} → use when: {e['situation']}" for e in emoji_bank]
        emoji_section = (
            "\n\nSERVER CUSTOM EMOJIS — USE THESE INSTEAD OF GENERIC ONES:\n"
            + "\n".join(lines)
        )

    # Fix [20]: cap system prompt to avoid token overruns
    system_parts = (
        learning_context
        + _SYSTEM_PROMPT
        + personality_section
        + facts_section
        + lore_section
        + emoji_section
        + mood_addon
    )
    if len(system_parts) > _SYSTEM_PROMPT_MAX_CHARS:
        system_parts = system_parts[:_SYSTEM_PROMPT_MAX_CHARS]

    client = _get_deepinfra_client()
    try:
        response = await client.chat.completions.create(
            model=_DEEPINFRA_MODEL,
            messages=[{"role": "system", "content": system_parts}] + messages[-15:],
            max_tokens=max_tokens,  # Fix [6]: use the parameter, not hardcoded 400
            temperature=1.05,
            frequency_penalty=0.85,
            presence_penalty=0.6,
        )
        tokens_used = response.usage.total_tokens if response.usage else max_tokens
        try:
            _check_budget_and_add(tokens_used)
            log.debug("ai_companion: tokens used this call=%d", tokens_used)
        except DailyTokenLimitReached:
            raise
        except Exception as track_err:
            log.warning("ai_companion: token tracking failed: %s", track_err)

        raw = response.choices[0].message.content.strip()
        return _humanise(_sanitise(raw))
    except DailyTokenLimitReached:
        raise
    except Exception as e:
        log.warning("ai_companion: DeepInfra call failed: %s", e)
        raise RuntimeError(f"DeepInfra backend failed: {e}") from e


# ---------------------------------------------------------------------------
# Typing simulation helpers
# ---------------------------------------------------------------------------

_CHARS_PER_SECOND = 5.5
_MIN_TYPING       = 0.4
_MAX_TYPING       = 6.5


def _typing_seconds(text: str) -> float:
    """
    Simulate realistic human typing duration based on message length.
    Tiers:
      - Very short  (≤10 chars):  0.4 – 0.8 s
      - Short       (≤40 chars):  0.8 – 1.8 s
      - Medium      (≤120 chars): 1.8 – 3.5 s
      - Long        (≤300 chars): 3.5 – 5.5 s
      - Very long   (300+ chars): 5.5 – 6.5 s
    """
    n = len(text)
    # Fix [11]: tiers are explicitly implemented, matching the docstring
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


def _split_parts(text: str) -> list[str]:
    parts = [p.strip() for p in text.split("[SPLIT]") if p.strip()]
    return parts[:3] if parts else [text]


# ---------------------------------------------------------------------------
# Moderation helpers
# ---------------------------------------------------------------------------

def _parse_mute_duration(text: str) -> float:
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
    total       = len(recent_msgs)
    caps_count  = sum(1 for m in recent_msgs if m != m.lower() and len(m) > 3)
    emoji_count = sum(
        1 for m in recent_msgs if _UNICODE_EMOJI_RE.search(m) or _CUSTOM_EMOJI_RE.search(m)
    )
    short_count = sum(1 for m in recent_msgs if len(m.split()) <= 3)
    if caps_count / total > 0.4:
        return "hype"
    if short_count / total > 0.6 and emoji_count / total < 0.3:
        return "chill"
    if emoji_count / total > 0.5 or caps_count / total > 0.25:
        return "chaotic"
    return "mixed"


def _extract_slang(text: str) -> list[str]:
    words = re.findall(r"\b[a-z]{2,8}\b", text.lower())
    return [w for w in words if w not in _STOPWORDS]


def _extract_emojis(text: str) -> list[str]:
    return _CUSTOM_EMOJI_RE.findall(text) + _UNICODE_EMOJI_RE.findall(text)


def _build_learning_context(vocab: dict) -> str:
    if not vocab:
        return ""
    phrases = vocab.get("common_phrases", [])[-25:]
    slang   = vocab.get("slang", [])[-25:]
    emojis  = vocab.get("emojis", [])[-12:]
    energy  = vocab.get("energy", "mixed")
    samples = vocab.get("sample_messages", [])[-15:]
    if not any([phrases, slang, emojis, samples]):
        return ""

    sample_block = "\n".join(f'  "{s}"' for s in samples[-8:]) if samples else ""

    ctx = (
        "THIS SERVER'S REAL COMMUNICATION STYLE — COPY IT EXACTLY\n"
        f"Energy vibe: {energy}\n"
        f"Their slang (weave in naturally): {', '.join(slang)}\n"
        f"Their common phrases: {', '.join(phrases)}\n"
        f"Their most used emojis: {' '.join(emojis)}\n"
    )
    if sample_block:
        ctx += f"Real messages from this server — match this exact style:\n{sample_block}\n"
    ctx += (
        "You are FROM this server. You talk EXACTLY like them — same vocabulary, "
        "same energy, same abbreviations, same emoji usage. "
        "Never sound like a bot trying to fit in — you ARE one of them.\n\n"
    )
    return ctx


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class AiCompanion(commands.Cog):
    """Biki — chaotic AI companion that responds when mentioned or replied to."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

        # Fix [3]: conversations keyed by (guild_id, user_id) — no cross-guild pollution
        self.conversations: dict[tuple[int, int], list[dict]] = {}

        # user_id → {"channel_id": int, "dismissed_by": int}
        self.dismissed: dict[int, dict] = {}

        # guild_id → list of allowed channel_ids (empty = all channels)
        self.allowed_channels: dict[int, list[int]] = {}

        # guild_id → active mood key
        self.guild_moods: dict[int, str] = {}

        # guild_id → server vocab data
        self.server_vocab: dict[int, dict] = {}

        # unique users spoken to this session
        self._users_spoken: set[int] = set()

        # Fix [1]: per-user asyncio.Lock — no global blocking
        self._user_locks: dict[int, asyncio.Lock] = {}

        # Fix [2]: pending queue processes ALL messages, not just one
        # user_id → list of messages queued while Biki was busy with that user
        self._pending: dict[int, list[discord.Message]] = {}

        # guild_id → message count for learning context throttle
        self._learning_inject_counter: dict[int, int] = {}

        # guild_id → cached learning context string
        self._learning_ctx_cache: dict[int, str] = {}

        # user_id → timestamp of last successful reply
        self._user_cooldowns: dict[int, float] = {}

        # guild_id → custom personality text
        self.guild_personalities: dict[int, str] = {}

        # Fix [8]: silenced / chime_rate / cooldown loaded from DB and persisted
        self.guild_silenced:    dict[int, bool]  = {}
        self.guild_chime_rate:  dict[int, float] = {}
        self.guild_cooldown:    dict[int, float] = {}

        # guild_id → list of {id, fact_text} dicts
        self.guild_facts: dict[int, list[dict]] = {}

        # channel_id → deque of last 8 formatted message strings
        self.channel_history: dict[int, deque] = {}

        # guild_id → {user_id → profile dict}
        self.user_memory: dict[int, dict[int, dict]] = {}

        # guild_id → {subject_name → [fact, ...]}
        self.server_knowledge: dict[int, dict[str, list[str]]] = {}

        # guild_id → server lore paragraph
        self.guild_lore: dict[int, str] = {}

        # guild_id → list of {id, emoji, situation} dicts
        self.guild_emojis: dict[int, list[dict]] = {}

        # guild_id → bool — whether auto-mood is enabled
        self.guild_automood: dict[int, bool] = {}

        # guild_id → bool — whether auto-mod is enabled
        self.guild_automod: dict[int, bool] = {}

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        try:
            await asyncio.to_thread(_db_init)
            self.allowed_channels    = await asyncio.to_thread(_db_load_all_channels)
            self.guild_personalities = await asyncio.to_thread(_db_load_all_personalities)
            self.guild_facts         = await asyncio.to_thread(_db_load_all_facts)
            self.guild_moods         = await asyncio.to_thread(_db_load_all_moods)
            self.guild_lore          = await asyncio.to_thread(_db_load_all_lore)
            self.guild_emojis        = await asyncio.to_thread(_db_load_all_emojis)

            # Fix [8]: load persisted guild settings
            settings = await asyncio.to_thread(_db_load_all_guild_settings)
            for gid, s in settings.items():
                self.guild_silenced[gid]   = s.get("silenced", False)
                self.guild_chime_rate[gid] = s.get("chime_rate", 0.06)
                self.guild_cooldown[gid]   = s.get("cooldown_sec", 5.0)

            log.info(
                "ai_companion: loaded channels for %d guild(s), personalities for %d, "
                "facts for %d, moods for %d, lore for %d, emoji banks for %d",
                len(self.allowed_channels), len(self.guild_personalities),
                len(self.guild_facts), len(self.guild_moods),
                len(self.guild_lore), len(self.guild_emojis),
            )
        except Exception as exc:
            log.error("ai_companion: DB init/load failed: %s", exc)

    # ------------------------------------------------------------------
    # Per-user lock helper
    # ------------------------------------------------------------------

    def _get_user_lock(self, user_id: int) -> asyncio.Lock:
        """Return (or lazily create) a per-user asyncio.Lock. Fix [1, 14]."""
        if user_id not in self._user_locks:
            self._user_locks[user_id] = asyncio.Lock()
        return self._user_locks[user_id]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _append_history(self, guild_id: int, user_id: int, role: str, content: str) -> None:
        """Fix [3, 4]: key by (guild_id, user_id)."""
        key = (guild_id, user_id)
        history = self.conversations.setdefault(key, [])
        history.append({"role": role, "content": content})
        if len(history) > 40:
            self.conversations[key] = history[-40:]
        asyncio.ensure_future(  # Fix [12]: safe from any thread
            asyncio.to_thread(_db_save_conv_message, guild_id, user_id, role, content)
        )

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
            return _MOOD_ADDONS["happy"]
        mood_key = self.guild_moods.get(guild_id, "happy")
        if mood_key not in _MOOD_ADDONS:
            mood_key = "chaotic"
        return _MOOD_ADDONS[mood_key]

    def _learning_context(self, guild_id: Optional[int]) -> str:
        if guild_id is None:
            return ""
        count = self._learning_inject_counter.get(guild_id, 0) + 1
        self._learning_inject_counter[guild_id] = count
        if count % 5 != 0:
            return ""
        if guild_id in self._learning_ctx_cache:
            return self._learning_ctx_cache[guild_id]
        ctx = _build_learning_context(self.server_vocab.get(guild_id, {}))
        self._learning_ctx_cache[guild_id] = ctx
        return ctx

    async def _ai_reply(
        self,
        guild_id: int,
        user_id: int,
        user_text: str,
        extra_note: Optional[str] = None,
        max_tokens: int = 300,
        channel_context: str = "",
    ) -> str:
        """Call _call_ai with full conversation history, update history, return reply."""
        user_text = user_text[:400]

        # Load history from DB if not in memory
        key = (guild_id, user_id)
        if key not in self.conversations:
            try:
                past = await asyncio.to_thread(_db_load_user_conv, guild_id, user_id)
                if past:
                    self.conversations[key] = past
            except Exception as exc:
                log.warning("_ai_reply: failed to load conv from DB: %s", exc)

        history = list(self.conversations.get(key, []))
        input_content = user_text

        # Inject user memory
        profile = self.user_memory.get(guild_id, {}).get(user_id)
        if profile:
            name  = profile.get("display_name") or profile.get("username") or "them"
            count = profile.get("message_count", 1)
            notes = profile.get("notes") or []
            mem_lines = [f"You're talking to {name}. They've pinged you {count} time(s) before."]
            if notes:
                mem_lines.append("What you remember about them: " + " | ".join(notes[-8:]))
            extra_note = (extra_note + "\n" if extra_note else "") + " ".join(mem_lines)

        # Inject server knowledge
        _profile_name = (profile or {}).get("display_name") or (profile or {}).get("username")
        _kb = self.server_knowledge.get(guild_id, {})
        _kb_facts: list[str] = []
        if _profile_name:
            first = _profile_name.split()[0]
            _kb_facts = _kb.get(first, _kb.get(_profile_name, []))
        if _kb_facts:
            extra_note = (extra_note + "\n" if extra_note else "") + (
                "Things you've quietly picked up about this person from server chat: "
                + " | ".join(_kb_facts[-6:])
            )

        if channel_context:
            input_content = (
                f"[RECENT CHANNEL CONTEXT — what others just said before this message:\n"
                f"{channel_context}]\n{input_content}"
            )
        if extra_note:
            input_content = f"[CONTEXT FOR BIKI ONLY: {extra_note}]\n{input_content}"

        history.append({"role": "user", "content": input_content})

        personality = self.guild_personalities.get(guild_id, "")
        facts  = self.guild_facts.get(guild_id, [])
        lore   = self.guild_lore.get(guild_id, "")
        emojis = self.guild_emojis.get(guild_id)

        try:
            reply = await _call_ai(
                history,
                self._mood_addon(guild_id),
                self._learning_context(guild_id),
                max_tokens,
                personality,
                facts or None,
                lore,
                emojis or None,
            )
        except DailyTokenLimitReached:
            log.info("ai_companion: daily token cap reached")
            return random.choice(_OVER_LIMIT_REPLIES)

        self._append_history(guild_id, user_id, "user", user_text)
        self._append_history(guild_id, user_id, "assistant", reply)
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
            reply = await _call_ai([{"role": "user", "content": note}], max_tokens=150)
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
    # ------------------------------------------------------------------

    async def _send_biki_reply(
        self,
        trigger: discord.Message,
        text: str,
        *,
        force_reply: bool = False,
    ) -> None:
        """
        Send Biki's response with human-like timing and behavior.
        Fix [9]: force_reply is now actually honored for the first part.
        Fix [10]: reading pause uses clean content length (no @mention).
        """
        if random.random() < 0.20:
            try:
                await trigger.add_reaction(random.choice(_REACTION_POOL))
            except discord.HTTPException:
                pass

        parts = _split_parts(text)

        # Fix [10]: measure clean content (strip mentions)
        clean_incoming = re.sub(r"<@!?\d+>", "", trigger.content).strip()
        _read_pause = 0.2 + min(1.8, len(clean_incoming) / 180) + random.uniform(-0.1, 0.3)
        await asyncio.sleep(_read_pause)

        for i, part in enumerate(parts):
            typing_duration = _typing_seconds(part)
            async with trigger.channel.typing():
                await asyncio.sleep(typing_duration)

            if i == 0:
                # Fix [9]: force_reply=True always uses reply(); else 55% chance
                use_reply = force_reply or (random.random() < 0.55)
                if use_reply:
                    try:
                        await trigger.reply(part, mention_author=False)
                    except discord.HTTPException:
                        await trigger.channel.send(part)
                else:
                    await trigger.channel.send(part)
            else:
                await trigger.channel.send(part)

            if i < len(parts) - 1:
                await asyncio.sleep(
                    random.uniform(0.4, 1.1) if i == 0 else random.uniform(0.8, 1.8)
                )

    # ------------------------------------------------------------------
    # Proactive reply
    # ------------------------------------------------------------------

    async def _proactive_reply(self, message: discord.Message) -> None:
        """Fix [16]: check dismissal state before firing."""
        if not message.guild:
            return

        guild_id = message.guild.id
        user_id  = message.author.id

        # Fix [16]: don't proactively reply if the author dismissed Biki
        if user_id in self.dismissed:
            return

        prompt = (
            f'Someone in the server just said: "{message.content}"\n'
            "You were not mentioned but you want to jump in like a real Discord member would.\n"
            "React naturally — could be a reaction, a funny comment, a roast, agreeing, "
            "disagreeing, asking a question, going off-topic, or just vibing. "
            "Say as much or as little as the moment calls for. Be yourself."
        )

        if len(message.content.split()) < 3:
            return

        personality = self.guild_personalities.get(guild_id, "")
        facts  = self.guild_facts.get(guild_id, [])
        lore   = self.guild_lore.get(guild_id, "")
        emojis = self.guild_emojis.get(guild_id)
        try:
            await asyncio.sleep(random.uniform(0.5, 2.0))
            response = await _call_ai(
                [{"role": "user", "content": prompt}],
                self._mood_addon(guild_id),
                self._learning_context(guild_id),
                300,
                personality,
                facts or None,
                lore,
                emojis or None,
            )
            if response:
                await self._send_biki_reply(message, response)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Passive learning
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

        clean = re.sub(r"<@!?\d+>", "", text).strip()
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
            self._learning_ctx_cache.pop(guild_id, None)

        # User memory update
        user_id = message.author.id
        guild_mem = self.user_memory.setdefault(guild_id, {})
        profile = guild_mem.setdefault(user_id, {
            "display_name": message.author.display_name,
            "username":     message.author.name,
            "notes":        [],
            "message_count": 0,
        })
        profile["display_name"] = message.author.display_name
        profile["username"]     = message.author.name
        profile["message_count"] = profile.get("message_count", 0) + 1

        for match in _SELF_RE.findall(clean):
            fact = match.strip()
            if fact and fact not in profile.get("notes", []):
                if len(profile.setdefault("notes", [])) < 20:
                    profile["notes"].append(fact)
                    # Fix [12]: asyncio.ensure_future instead of call_soon
                    asyncio.ensure_future(
                        asyncio.to_thread(_db_add_user_note, guild_id, user_id, fact)
                    )

        asyncio.ensure_future(  # Fix [12]
            asyncio.to_thread(
                _db_upsert_user_memory,
                guild_id, user_id,
                message.author.display_name,
                message.author.name,
                True,
            )
        )

    # ------------------------------------------------------------------
    # Passive fact extraction
    # Fix [18]: member lookup cached once per call, not rebuilt O(n) per match
    # ------------------------------------------------------------------

    def _passive_fact_extract(self, message: discord.Message) -> None:
        if not message.guild:
            return
        guild_id  = message.guild.id
        author_dn = message.author.display_name
        text      = message.content

        # Fix [18]: build lookup once, not per regex match
        member_lower: dict[str, str] = {}
        for m in message.guild.members:
            dn = m.display_name or m.name
            member_lower[dn.split()[0].lower()] = dn
            member_lower[m.name.split()[0].lower()] = dn

        guild_kb = self.server_knowledge.setdefault(guild_id, {})

        def _store(subject: str, fact_body: str) -> None:
            fact_body = fact_body.strip().rstrip(".,!? ")
            if len(fact_body) < 3:
                return
            fact   = f"{subject} {fact_body}"[:120]
            bucket = guild_kb.setdefault(subject, [])
            if fact in bucket:
                return
            if len(bucket) >= 30:
                bucket.pop(0)
            bucket.append(fact)
            asyncio.ensure_future(  # Fix [12]
                asyncio.to_thread(_db_store_knowledge, guild_id, subject, fact)
            )

        mention_map: dict[str, str] = {}
        for mentioned in message.mentions:
            mention_map[f"<@{mentioned.id}>"]  = mentioned.display_name
            mention_map[f"<@!{mentioned.id}>"] = mentioned.display_name

        text_resolved = text
        for token, dn in mention_map.items():
            text_resolved = text_resolved.replace(token, dn)

        for m in _FIRST_PERSON_RE.finditer(text_resolved):
            verb_and_body = m.group(0).strip()
            converted = re.sub(r"^i'?m\b", f"{author_dn} is", verb_and_body, flags=re.IGNORECASE)
            converted = re.sub(r"^i am\b",  f"{author_dn} is", converted,     flags=re.IGNORECASE)
            converted = re.sub(r"^i was\b", f"{author_dn} was", converted,    flags=re.IGNORECASE)
            converted = re.sub(r"^i\b",      author_dn,          converted,    flags=re.IGNORECASE)
            if converted != verb_and_body and len(converted) > len(author_dn) + 5:
                key  = author_dn.split()[0]
                body = converted[len(key):].strip()
                _store(key, body)

        for match in _FACT_SUBJECT_RE.finditer(text_resolved):
            subject_raw = match.group(1).strip()
            fact_raw    = match.group(2).strip()
            subject_lo  = subject_raw.lower()
            if subject_lo in _NOT_A_NAME or len(fact_raw) < 3:
                continue
            canonical = member_lower.get(subject_lo, subject_raw)
            _store(canonical.split()[0], fact_raw)

    # ------------------------------------------------------------------
    # Moderation — 3-priority target resolution
    # ------------------------------------------------------------------

    async def _resolve_mod_target(
        self, message: discord.Message
    ) -> Optional[discord.Member]:
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

        mention_match = re.search(r"<@!?(\d+)>", message.content)
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

    async def _try_moderation(
        self, message: discord.Message, clean: str, guild_id: int, user_id: int
    ) -> bool:
        if message.guild is None:
            return False
        author = message.author
        if not isinstance(author, discord.Member):
            return False

        lower = clean.lower()

        # DELETE REPLIED MESSAGE
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
                    ref_msg = await message.channel.fetch_message(message.reference.message_id)
                await ref_msg.delete()
            except discord.NotFound:
                await message.channel.send("bro it's already gone lmaooo 💀")
                return True
            except discord.Forbidden:
                await message.channel.send("ngl i can't delete that, no permissions 😭 give me manage messages")
                return True
            note = f"You just deleted a message because {author.display_name} asked. Confirm it dramatically. You have the power."
            reply = await self._ai_reply(guild_id, user_id, clean, extra_note=note, max_tokens=150)
            await self._send_biki_reply(message, reply)
            return True

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

        target = await self._resolve_mod_target(message)
        if target is None:
            await message.channel.send("bro who are you even talking about 💀 ping them or reply to their message")
            return True

        if target.guild_permissions.administrator or target.id == self.bot.user.id:
            note = "Someone tried to make you take a moderation action against an admin or against yourself. Refuse dramatically and chaotically."
            reply = await self._ai_reply(guild_id, user_id, clean, extra_note=note, max_tokens=150)
            await self._send_biki_reply(message, reply)
            return True

        if action == "mute":
            duration  = _parse_mute_duration(clean)
            human_dur = _human_duration(duration)
            await message.channel.send(
                random.choice(["okay daddy 😈 give me a sec", "on it rn", "say less 💀", "brb handling business"])
            )
            try:
                until = datetime.now(timezone.utc) + timedelta(seconds=duration)
                await target.timeout(until, reason=f"Muted by {author} via Biki")
            except discord.Forbidden:
                await message.channel.send("bro i literally dont have the power to do that rn, give me the mute members permission")
                return True
            note = f"You just muted {target.display_name} for {human_dur} because {author.display_name} asked. Confirm the mute chaotically."
            reply = await self._ai_reply(guild_id, user_id, clean, extra_note=note, max_tokens=150)
            await self._send_biki_reply(message, reply)

        elif action == "unmute":
            try:
                await target.edit(timed_out_until=None)
            except discord.Forbidden:
                await message.channel.send("no permissions for that smh 😭")
                return True
            note = f"You just unmuted {target.display_name} because {author.display_name} asked."
            reply = await self._ai_reply(guild_id, user_id, clean, extra_note=note, max_tokens=150)
            await self._send_biki_reply(message, reply)

        elif action == "kick":
            name = target.display_name
            try:
                await target.kick(reason=f"Kicked by {author} via Biki")
            except discord.Forbidden:
                await message.channel.send("no kick permissions smh 😭 give me kick members")
                return True
            note = f"You just kicked {name} because {author.display_name} asked. Say something like 'YEET 👋 {name} has left the building. bye bestie'"
            reply = await self._ai_reply(guild_id, user_id, clean, extra_note=note, max_tokens=150)
            await self._send_biki_reply(message, reply)

        elif action == "ban":
            name = target.display_name
            try:
                await target.ban(reason=f"Banned by {author} via Biki")
            except discord.Forbidden:
                await message.channel.send("no ban permissions smh 😭 give me ban members")
                return True
            note = f"You just banned {name} because {author.display_name} asked."
            reply = await self._ai_reply(guild_id, user_id, clean, extra_note=note, max_tokens=150)
            await self._send_biki_reply(message, reply)

        elif action == "warn":
            reason_raw = re.sub(r"<@!?\d+>", "", re.sub(_RE_WARN, "", clean)).strip()
            reason = reason_raw or "no reason given"
            events_cog = self.bot.get_cog("Events")
            if events_cog and hasattr(events_cog, "_apply_warn"):
                try:
                    await events_cog._apply_warn(target, reason)
                except Exception:
                    pass
            else:
                warn_count = await asyncio.to_thread(
                    _db_add_warning, message.guild.id, target.id, author.id, reason
                )
                try:
                    await target.send(
                        f"⚠️ you got a warning in **{message.guild.name}**\nreason: {reason}"
                    )
                except Exception:
                    pass
            note = f"You just warned {target.display_name} for: '{reason}'. {author.display_name} asked you to."
            reply = await self._ai_reply(guild_id, user_id, clean, extra_note=note, max_tokens=150)
            await self._send_biki_reply(message, reply)

        return True

    # ------------------------------------------------------------------
    # Handle a single triggered message (called with per-user lock held)
    # ------------------------------------------------------------------

    async def _handle_triggered(self, message: discord.Message, clean: str, guild_id: int, user_id: int) -> None:
        channel_id = message.channel.id

        try:
            if await self._try_moderation(message, clean, guild_id, user_id):
                return

            # Dismissal state check
            dismissed_state = self.dismissed.get(user_id)
            if dismissed_state is not None:
                if self._is_return(clean):
                    self.dismissed.pop(user_id, None)
                    note = "The person who kicked you out is begging you to come back. Make your re-entry absolutely unhinged."
                    try:
                        reply = await self._ai_reply(guild_id, user_id, clean, extra_note=note, max_tokens=150)
                        await self._send_biki_reply(message, reply)
                    except Exception as exc:
                        log.error("ai_companion: return reply failed: %s", exc)
                return

            # Spite return check
            all_dismissed_by = {v["dismissed_by"] for v in self.dismissed.values()}
            if all_dismissed_by and self._is_return(clean) and user_id not in all_dismissed_by:
                self.dismissed.clear()
                note = "Someone ELSE summoned you back just to spite the person who dismissed you. Most dramatic comeback ever."
                try:
                    reply = await self._ai_reply(guild_id, user_id, clean, extra_note=note, max_tokens=150)
                    await self._send_biki_reply(message, reply)
                except Exception as exc:
                    log.error("ai_companion: spite-return failed: %s", exc)
                return

            # Timed dismissal
            timed_seconds = self._parse_timed_dismiss(clean)
            if timed_seconds is not None:
                self.dismissed[user_id] = {"channel_id": channel_id, "dismissed_by": user_id}
                note = (
                    f"This person is dismissing you for exactly {int(timed_seconds)} seconds. "
                    "Acknowledge it chaotically then go quiet."
                )
                try:
                    reply = await self._ai_reply(guild_id, user_id, clean, extra_note=note, max_tokens=150)
                    await self._send_biki_reply(message, reply)
                except Exception as exc:
                    log.error("ai_companion: timed dismiss failed: %s", exc)
                asyncio.create_task(self._timed_return(timed_seconds, channel_id, user_id))
                return

            # Plain dismissal
            if self._is_dismiss(clean):
                self.dismissed[user_id] = {"channel_id": channel_id, "dismissed_by": user_id}
                note = "This person is kicking you out. Most dramatic chaotic goodbye ever."
                try:
                    reply = await self._ai_reply(guild_id, user_id, clean, extra_note=note, max_tokens=150)
                    await self._send_biki_reply(message, reply)
                except Exception as exc:
                    log.error("ai_companion: dismiss failed: %s", exc)
                return

            # Normal reply
            try:
                _ctx_deque = self.channel_history.get(channel_id)

                # Fix [17]: exclude both the triggering user's lines AND Biki's own messages
                assert self.bot.user is not None
                _bot_name = self.bot.user.display_name or "Biki"
                _ctx_lines = [
                    line for line in (list(_ctx_deque)[:-1] if _ctx_deque else [])
                    if not line.startswith(f"{message.author.display_name}:")
                    and not line.startswith(f"{_bot_name}:")
                ]
                _channel_ctx = "\n".join(_ctx_lines[-5:])

                _guild = message.guild
                _member_count = _guild.member_count or "?"
                _channel_names = ", ".join(
                    c.name for c in _guild.text_channels[:8]
                ) if _guild.text_channels else "unknown"
                _server_note = (
                    f"Server: '{_guild.name}' · {_member_count} members · channels: {_channel_names}. "
                    f"You are responding ONLY to {message.author.display_name} (user ID {user_id})."
                )

                reply = await self._ai_reply(
                    guild_id, user_id, clean,
                    channel_context=_channel_ctx,
                    extra_note=_server_note,
                )
                self._user_cooldowns[user_id] = time.time()
                await self._send_biki_reply(message, reply)
            except Exception as exc:
                log.error("ai_companion: AI call failed: %s", exc)
                try:
                    await message.channel.send(random.choice(_OFFLINE_REPLIES))
                except discord.HTTPException:
                    pass

        except Exception as exc:
            log.error("ai_companion: _handle_triggered unexpected error: %s", exc)

    # ------------------------------------------------------------------
    # on_message — single unified listener
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return

        guild_id = message.guild.id
        user_id  = message.author.id

        if self.guild_silenced.get(guild_id):
            return

        allowed = self.allowed_channels.get(guild_id, [])
        if allowed and message.channel.id not in allowed:
            return

        self._learn_from_message(message)
        self._passive_fact_extract(message)
        self._maybe_automood(guild_id)

        # Channel context — exclude Biki's own messages (Fix [17])
        _ch_hist = self.channel_history.setdefault(message.channel.id, deque(maxlen=8))
        assert self.bot.user is not None
        if message.author.id != self.bot.user.id:
            _ch_hist.append(f"{message.author.display_name}: {message.content[:150]}")

        # Detect trigger
        bot_mentioned = self.bot.user in message.mentions
        replied_to_bot = (
            message.reference is not None
            and isinstance(message.reference.resolved, discord.Message)
            and message.reference.resolved.author.id == self.bot.user.id
        )
        triggered = bot_mentioned or replied_to_bot

        if not triggered:
            msg_lower = message.content.lower()
            if any(w in msg_lower for w in _TRIGGER_REACTION_WORDS):
                if random.random() < 0.05:
                    try:
                        await message.add_reaction(random.choice(_CHAOTIC_REACTION_POOL))
                    except discord.HTTPException:
                        pass

            # Auto-mod runs on non-triggered messages too
            clean_for_mod = re.sub(r'<@!?\d+>', '', message.content).strip()
            if clean_for_mod:
                asyncio.create_task(self._check_automod(message, clean_for_mod))

            chime_rate = self.guild_chime_rate.get(guild_id, 0.06)
            if random.random() < chime_rate:
                asyncio.create_task(self._proactive_reply(message))
            return

        # Strip bot mentions from content
        clean = message.content
        clean = clean.replace(f"<@{self.bot.user.id}>", "")
        clean = clean.replace(f"<@!{self.bot.user.id}>", "").strip()

        if not clean and replied_to_bot and isinstance(message.reference.resolved, discord.Message):
            clean = f"[replying to your message: \"{message.reference.resolved.content[:200]}\"]"

        # Per-user cooldown
        now = time.time()
        last_reply = self._user_cooldowns.get(user_id, 0)
        cooldown_secs = self.guild_cooldown.get(guild_id, 5.0)
        if now - last_reply < cooldown_secs:
            return

        # Fix [1, 2, 14]: per-user asyncio.Lock, queue if busy, process ALL pending
        user_lock = self._get_user_lock(user_id)

        if user_lock.locked():
            # Queue this message (keep only latest per user — anti-spam)
            self._pending.setdefault(user_id, [])
            self._pending[user_id] = [message]  # replace; latest wins
            return

        async with user_lock:
            await self._handle_triggered(message, clean, guild_id, user_id)

            # Fix [2]: process ALL pending messages for this user (not just 1)
            while self._pending.get(user_id):
                pending_msgs = self._pending.pop(user_id, [])
                for pending_msg in pending_msgs:
                    p_clean = pending_msg.content
                    p_clean = p_clean.replace(f"<@{self.bot.user.id}>", "")
                    p_clean = p_clean.replace(f"<@!{self.bot.user.id}>", "").strip()
                    await self._handle_triggered(pending_msg, p_clean, guild_id, user_id)

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    @app_commands.command(
        name="bikimood",
        description="Change Biki's mood for this server.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(mood="The mood to set. happy / sad / chaotic / cold")
    @app_commands.choices(mood=[
        app_commands.Choice(name="😄 Happy / Flirty",    value="happy"),
        app_commands.Choice(name="🥺 Sad / Soft",        value="sad"),
        app_commands.Choice(name="🌀 Chaotic / Unhinged", value="chaotic"),
        app_commands.Choice(name="🧊 Cold / Sarcastic",  value="cold"),
    ])
    async def bikimood(
        self, interaction: discord.Interaction, mood: app_commands.Choice[str]
    ) -> None:
        assert interaction.guild_id is not None
        gid = interaction.guild_id
        mood_key = mood.value
        if mood_key not in VALID_MOODS:
            await interaction.response.send_message("❌ Invalid mood.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await asyncio.to_thread(_db_set_mood, gid, mood_key)
            self.guild_moods[gid] = mood_key
        except Exception as exc:
            await interaction.followup.send(f"❌ DB error: `{exc}`", ephemeral=True)
            return
        label = MOOD_LABELS.get(mood_key, mood_key)
        await interaction.followup.send(
            f"✅ Biki's mood is now **{label}**. She'll stay in this mood until you change it.",
            ephemeral=True,
        )

    @app_commands.command(
        name="bikisilence",
        description="Toggle Biki's silence for this server.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def bikisilence(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        gid = interaction.guild_id
        new_state = not self.guild_silenced.get(gid, False)
        self.guild_silenced[gid] = new_state
        # Fix [8]: persist to DB
        await asyncio.to_thread(_db_upsert_guild_settings, gid, silenced=new_state)
        if new_state:
            await interaction.response.send_message(
                "🔇 Biki is now silenced. She won't respond to anything until you run this again.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "🔊 Biki is no longer silenced. Back to chaos.",
                ephemeral=True,
            )

    @app_commands.command(
        name="bikiremember",
        description="Tell Biki something to always remember about this server.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(fact="The fact Biki should always know. Max 500 chars.")
    async def bikiremember(
        self, interaction: discord.Interaction, fact: str
    ) -> None:
        assert interaction.guild_id is not None
        gid = interaction.guild_id
        if len(fact) > 500:
            await interaction.response.send_message("❌ Fact too long (max 500 chars).", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            fact_id = await asyncio.to_thread(_db_add_fact, gid, fact.strip())
            self.guild_facts.setdefault(gid, []).append({"id": fact_id, "fact_text": fact.strip()})
        except Exception as exc:
            await interaction.followup.send(f"❌ DB error: `{exc}`", ephemeral=True)
            return
        total = len(self.guild_facts.get(gid, []))
        await interaction.followup.send(
            f"✅ Got it. Biki will remember: **{fact.strip()}**\n"
            f"*(fact #{fact_id} — {total} total for this server)*",
            ephemeral=True,
        )

    @app_commands.command(
        name="bikiforget",
        description="Delete a specific fact Biki knows about this server.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(fact_id="The fact ID to delete. Use /bikifacts to find IDs.")
    async def bikiforget(
        self, interaction: discord.Interaction, fact_id: int
    ) -> None:
        assert interaction.guild_id is not None
        gid = interaction.guild_id
        await interaction.response.defer(ephemeral=True)
        try:
            deleted = await asyncio.to_thread(_db_delete_fact, fact_id, gid)
        except Exception as exc:
            await interaction.followup.send(f"❌ DB error: `{exc}`", ephemeral=True)
            return
        if not deleted:
            await interaction.followup.send(f"❌ No fact with ID `{fact_id}` found for this server.", ephemeral=True)
            return
        self.guild_facts[gid] = [f for f in self.guild_facts.get(gid, []) if f["id"] != fact_id]
        await interaction.followup.send(f"✅ Fact `#{fact_id}` deleted. Biki has forgotten it.", ephemeral=True)

    @app_commands.command(
        name="bikiclearfacts",
        description="Clear ALL facts Biki knows about this server at once.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def bikiclearfacts(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        gid = interaction.guild_id
        await interaction.response.defer(ephemeral=True)
        try:
            deleted = await asyncio.to_thread(_db_clear_all_facts, gid)
            self.guild_facts[gid] = []
        except Exception as exc:
            await interaction.followup.send(f"❌ DB error: `{exc}`", ephemeral=True)
            return
        if deleted == 0:
            await interaction.followup.send("Biki has no facts to clear for this server.", ephemeral=True)
        else:
            await interaction.followup.send(
                f"✅ Cleared **{deleted}** fact(s). Biki remembers nothing now.", ephemeral=True
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
                "biki hasn't learned anything about this server yet... talk more and he will",
                ephemeral=True,
            )
            return
        phrases    = len(v.get("common_phrases", []))
        slang_cnt  = len(v.get("slang", []))
        emoji_cnt  = len(v.get("emojis", []))
        sample_cnt = len(v.get("sample_messages", []))
        energy     = v.get("energy", "mixed")
        top_slang  = ", ".join(v.get("slang", [])[-8:]) or "none yet"
        top_emojis = " ".join(v.get("emojis", [])[:8])  or "none yet"
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
        name="bikifacts",
        description="List everything Biki currently remembers about this server.",
    )
    @app_commands.guild_only()
    async def bikifacts(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        gid   = interaction.guild_id
        facts = self.guild_facts.get(gid, [])
        if not facts:
            await interaction.response.send_message(
                "Biki doesn't remember anything specific about this server yet. "
                "Use `/bikiremember` to add facts.",
                ephemeral=True,
            )
            return
        lines = [f"`#{f['id']}` — {f['fact_text']}" for f in facts]
        body  = "\n".join(lines)
        if len(body) > 1900:
            body = body[:1900] + f"\n... *(showing first entries, {len(facts)} total)*"
        await interaction.response.send_message(
            f"**Things Biki knows about this server ({len(facts)} facts):**\n{body}\n\n"
            f"Use `/bikiforget <id>` to remove one, or `/bikiclearfacts` to wipe all.",
            ephemeral=True,
        )

    @app_commands.command(
        name="bikistats",
        description="Show Biki's config and session stats for this server.",
    )
    @app_commands.guild_only()
    async def bikistats(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        gid = interaction.guild_id

        mood          = self.guild_moods.get(gid, "happy")
        silenced      = self.guild_silenced.get(gid, False)
        personality   = self.guild_personalities.get(gid, "")
        facts_count   = len(self.guild_facts.get(gid, []))
        chime_rate    = self.guild_chime_rate.get(gid, 0.06)
        cooldown_secs = self.guild_cooldown.get(gid, 5.0)
        personality_preview = (
            personality[:120] + ("…" if len(personality) > 120 else "")
            if personality else "none (default Biki)"
        )

        total_msgs     = sum(len(v) for v in self.conversations.values())
        total_users    = len(self.conversations)
        spoken_this    = len(self._users_spoken)
        dismissed_cnt  = len(self.dismissed)
        pending_cnt    = sum(len(v) for v in self._pending.values())
        locked_users   = sum(1 for lk in self._user_locks.values() if lk.locked())

        silence_str = "🔇 **SILENCED**" if silenced else "🔊 active"
        mood_label  = MOOD_LABELS.get(mood, mood)

        await interaction.response.send_message(
            f"**Biki — server config**\n"
            f"• Status: {silence_str}\n"
            f"• Mood: **{mood_label}**\n"
            f"• Chime-in rate: **{chime_rate*100:.1f}%**\n"
            f"• Reply cooldown: **{cooldown_secs:.0f}s**\n"
            f"• Custom personality: {personality_preview}\n"
            f"• Remembered facts: **{facts_count}** (use `/bikifacts` to view)\n"
            f"\n"
            f"**Session stats**\n"
            f"• Conversations in memory: **{total_users}** users / **{total_msgs}** messages\n"
            f"• Spoken to this session: **{spoken_this}** user(s)\n"
            f"• Dismissed by: **{dismissed_cnt}** user(s)\n"
            f"• Currently typing: **{locked_users}** · Pending: **{pending_cnt}**",
            ephemeral=True,
        )

    @app_commands.command(
        name="bikiping",
        description="Test Biki's response speed and personality live.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(message="The test message to send Biki.")
    async def bikiping(
        self, interaction: discord.Interaction, message: str = "yo what's good"
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)

        start = time.time()
        try:
            reply = await self._ai_reply(
                interaction.guild_id,
                interaction.user.id,
                message,
                max_tokens=200,
            )
            elapsed = time.time() - start
            status = "✅"
            result_line = f"**Biki replied in `{elapsed:.2f}s`**"
        except DailyTokenLimitReached:
            elapsed = time.time() - start
            status = "🔴"
            reply = "*daily token cap hit — no API call made*"
            result_line = f"**Token cap reached** (`{elapsed:.2f}s`)"
        except Exception as exc:
            elapsed = time.time() - start
            status = "❌"
            reply = f"*error: {exc}*"
            result_line = f"**Failed** (`{elapsed:.2f}s`)"

        with _token_lock:
            tracker = dict(_token_state)
        today = _datetime_mod.date.today().isoformat()
        used  = tracker["total"] if tracker.get("date") == today else 0
        cap   = _effective_cap(tracker)

        await interaction.followup.send(
            f"{status} **Bikiping** — `{message}`\n\n"
            f"**Biki said:**\n> {reply}\n\n"
            f"{result_line}\n"
            f"Tokens today: **{used:,} / {cap:,}**",
            ephemeral=True,
        )

    @app_commands.command(
        name="bikitokens",
        description="Show today's DeepInfra token usage and remaining daily budget.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def bikitokens(self, interaction: discord.Interaction) -> None:
        today = _datetime_mod.date.today().isoformat()
        with _token_lock:
            tracker = dict(_token_state)

        used = tracker["total"] if tracker.get("date") == today else 0
        cap  = _effective_cap(tracker)
        left = max(0, cap - used)
        pct  = (used / cap) * 100

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
        is_custom = cap != _DAILY_TOKEN_CAP
        cap_label = f"**{cap:,}** *(custom)*" if is_custom else f"**{cap:,}** *(default)*"

        await interaction.response.send_message(
            f"**Biki — Daily Token Budget**\n"
            f"Date: `{today}`\n\n"
            f"`{bar}` {pct:.1f}%\n\n"
            f"• Used today: **{used:,}**\n"
            f"• Remaining: **{left:,}**\n"
            f"• Daily cap: {cap_label}\n\n"
            f"Status: {status}",
            ephemeral=True,
        )

    @app_commands.command(
        name="bikibudget",
        description="View or change Biki's daily token cap.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(new_cap="New daily token limit (e.g. 1500000). Leave blank to just view the current cap.")
    async def bikibudget(
        self, interaction: discord.Interaction, new_cap: Optional[int] = None
    ) -> None:
        with _token_lock:
            tracker = dict(_token_state)
        current_cap = _effective_cap(tracker)

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

        MIN_CAP = 10_000
        MAX_CAP = 10_000_000
        if new_cap < MIN_CAP:
            await interaction.response.send_message(
                f"❌ Cap must be at least **{MIN_CAP:,}** tokens.", ephemeral=True
            )
            return
        if new_cap > MAX_CAP:
            await interaction.response.send_message(
                f"❌ Cap can't exceed **{MAX_CAP:,}** tokens.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await asyncio.to_thread(_set_token_cap, new_cap)
        except Exception as exc:
            await interaction.followup.send(f"❌ Failed to save cap: `{exc}`", ephemeral=True)
            return

        direction  = "⬆️ increased" if new_cap > current_cap else "⬇️ decreased"
        is_default = new_cap == _DAILY_TOKEN_CAP
        note       = " (reset to default)" if is_default else ""
        await interaction.followup.send(
            f"✅ Daily token cap {direction}{note}.\n"
            f"• Old cap: **{current_cap:,}** tokens\n"
            f"• New cap: **{new_cap:,}** tokens\n\n"
            f"Takes effect immediately. Use `/bikitokens` to monitor usage.",
            ephemeral=True,
        )

    @app_commands.command(
        name="bikirecall",
        description="Show everything Biki knows about a server member.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(member="The server member to look up.")
    async def bikirecall(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        assert interaction.guild_id is not None
        gid = interaction.guild_id
        await interaction.response.defer(ephemeral=True)

        profile = self.user_memory.get(gid, {}).get(member.id)
        if profile is None:
            try:
                rows    = await asyncio.to_thread(_db_load_all_user_memory, gid)
                profile = rows.get(member.id)
                if profile:
                    self.user_memory.setdefault(gid, {})[member.id] = profile
            except Exception:
                profile = None

        profile_lines: list[str] = []
        if profile:
            dn    = profile.get("display_name") or member.display_name
            uname = profile.get("username") or member.name
            count = profile.get("message_count", 0)
            notes = profile.get("notes") or []
            profile_lines.append(f"**Display name:** {dn} (`@{uname}`)")
            profile_lines.append(f"**Times talked to Biki:** {count}")
            if notes:
                profile_lines.append("**Self-stated facts:**")
                for n in notes[-10:]:
                    profile_lines.append(f"  • {n}")
        else:
            profile_lines.append("_(No direct conversation profile yet)_")

        first_name = member.display_name.split()[0]
        kb_facts = (
            self.server_knowledge.get(gid, {}).get(first_name)
            or self.server_knowledge.get(gid, {}).get(member.display_name)
            or self.server_knowledge.get(gid, {}).get(member.name.split()[0])
        )
        if kb_facts is None:
            try:
                db_facts = await asyncio.to_thread(_db_get_knowledge_about, gid, first_name)
                if not db_facts and first_name != member.display_name:
                    db_facts = await asyncio.to_thread(_db_get_knowledge_about, gid, member.display_name)
                kb_facts = db_facts or []
                if kb_facts:
                    self.server_knowledge.setdefault(gid, {})[first_name] = kb_facts
            except Exception:
                kb_facts = []

        knowledge_lines: list[str] = []
        if kb_facts:
            knowledge_lines.append("**Passively picked up from server chat:**")
            for f in kb_facts[-15:]:
                knowledge_lines.append(f"  • {f}")
        else:
            knowledge_lines.append("_(Nothing picked up from server chat yet)_")

        header = f"🧠 **Biki's file on {member.display_name}** (`{member.name}`)\n"
        body   = "\n".join(profile_lines) + "\n\n" + "\n".join(knowledge_lines)
        full   = header + body
        if len(full) > 1900:
            full = full[:1897] + "…"
        await interaction.followup.send(full, ephemeral=True)

    @app_commands.command(
        name="bikisetpersonality",
        description="Give Biki a custom personality for this server.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(personality="The personality text. Max 1500 chars.")
    async def bikisetpersonality(
        self, interaction: discord.Interaction, personality: str
    ) -> None:
        assert interaction.guild_id is not None
        gid = interaction.guild_id
        if len(personality) > 1500:
            await interaction.response.send_message(
                f"❌ Too long ({len(personality)} chars). Keep it under 1500.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            await asyncio.to_thread(_db_set_personality, gid, personality.strip())
            self.guild_personalities[gid] = personality.strip()
        except Exception as exc:
            await interaction.followup.send(f"❌ DB error: `{exc}`", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Custom personality set.\n\nPreview:\n```\n{personality[:400]}\n```",
            ephemeral=True,
        )

    @app_commands.command(
        name="bikiclearpersonality",
        description="Remove the custom personality for this server.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def bikiclearpersonality(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        gid = interaction.guild_id
        await interaction.response.defer(ephemeral=True)
        try:
            await asyncio.to_thread(_db_clear_personality, gid)
            self.guild_personalities.pop(gid, None)
        except Exception as exc:
            await interaction.followup.send(f"❌ DB error: `{exc}`", ephemeral=True)
            return
        await interaction.followup.send(
            "✅ Custom personality cleared. Biki is back to her default self.", ephemeral=True
        )

    @app_commands.command(
        name="bikirate",
        description="View or set how often Biki jumps into unpinged messages (0–100%).",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(percent="Chime-in chance 0–100. Leave blank to just view current rate.")
    async def bikirate(
        self, interaction: discord.Interaction, percent: Optional[int] = None
    ) -> None:
        assert interaction.guild_id is not None
        gid     = interaction.guild_id
        current = self.guild_chime_rate.get(gid, 0.06)
        current_pct = round(current * 100, 1)

        if percent is None:
            bar_filled = int(current_pct / 5)
            bar = "█" * bar_filled + "░" * (20 - bar_filled)
            await interaction.response.send_message(
                f"**Biki's current chime-in rate for this server:**\n"
                f"`[{bar}]` **{current_pct}%**\n\n"
                f"• At **0%** — Biki only replies when directly @mentioned\n"
                f"• At **6%** *(default)* — jumps in occasionally, feels natural\n"
                f"• At **25%** — very chatty, active in almost every convo\n"
                f"• At **100%** — replies to literally everything\n\n"
                f"To change: `/bikirate percent:<0–100>`",
                ephemeral=True,
            )
            return

        if not 0 <= percent <= 100:
            await interaction.response.send_message("❌ Percent must be between 0 and 100.", ephemeral=True)
            return

        new_rate = percent / 100.0
        self.guild_chime_rate[gid] = new_rate
        # Fix [8]: persist
        await asyncio.to_thread(_db_upsert_guild_settings, gid, chime_rate=new_rate)

        if percent == 0:
            flavour = "silent mode — only responds to pings"
        elif percent <= 5:
            flavour = "barely lurking"
        elif percent <= 15:
            flavour = "natural, jumps in occasionally"
        elif percent <= 35:
            flavour = "chatty, hard to ignore"
        elif percent <= 60:
            flavour = "very active, almost always there"
        else:
            flavour = "CHAOS MODE — replying to everything"

        bar_filled = int(percent / 5)
        bar = "█" * bar_filled + "░" * (20 - bar_filled)
        await interaction.response.send_message(
            f"✅ Biki's chime-in rate set to **{percent}%** — *{flavour}*\n"
            f"`[{bar}]`\n\n"
            f"She'll still reply **100%** of the time when directly @mentioned.",
            ephemeral=True,
        )

    @app_commands.command(
        name="bikicooldown",
        description="View or set how long (in seconds) before Biki can reply to the same user again.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(seconds="Cooldown in seconds (0–300). Leave blank to view current value.")
    async def bikicooldown(
        self, interaction: discord.Interaction, seconds: Optional[int] = None
    ) -> None:
        assert interaction.guild_id is not None
        gid     = interaction.guild_id
        current = self.guild_cooldown.get(gid, 5.0)

        if seconds is None:
            await interaction.response.send_message(
                f"**Biki's current per-user cooldown:** `{current:.0f}s`\n\n"
                f"• **0s** — no cooldown, replies instantly every ping\n"
                f"• **5s** *(default)* — short gap, prevents spam\n"
                f"• **30s** — one reply per half-minute per user\n"
                f"• **300s** — max, one reply per 5 minutes per user\n\n"
                f"To change: `/bikicooldown seconds:<0–300>`",
                ephemeral=True,
            )
            return

        if not 0 <= seconds <= 300:
            await interaction.response.send_message("❌ Seconds must be between 0 and 300.", ephemeral=True)
            return

        self.guild_cooldown[gid] = float(seconds)
        # Fix [8]: persist
        await asyncio.to_thread(_db_upsert_guild_settings, gid, cooldown_sec=float(seconds))

        if seconds == 0:
            flavour = "no cooldown — she'll reply every single ping"
        elif seconds <= 5:
            flavour = "very fast, just blocks spam"
        elif seconds <= 15:
            flavour = "balanced — short breather between replies"
        elif seconds <= 60:
            flavour = "relaxed pace, one reply per minute per user"
        else:
            flavour = "strict — very limited replies per user"

        await interaction.response.send_message(
            f"✅ Per-user cooldown set to **{seconds}s** — *{flavour}*\n\n"
            f"Biki will ignore repeat pings from the same user within `{seconds}s` of her last reply.",
            ephemeral=True,
        )

    @app_commands.command(
        name="bikiemojis",
        description="Manage Biki's custom emoji bank for this server.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        action="What to do: 'add', 'list', 'remove', or 'clear'.",
        emoji="The emoji to add (custom or unicode).",
        situation="When Biki should use this emoji (max 200 chars).",
        emoji_id="The emoji bank ID to remove (for 'remove' only).",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="add",    value="add"),
        app_commands.Choice(name="list",   value="list"),
        app_commands.Choice(name="remove", value="remove"),
        app_commands.Choice(name="clear",  value="clear"),
    ])
    async def bikiemojis(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        emoji: Optional[str] = None,
        situation: Optional[str] = None,
        emoji_id: Optional[int] = None,
    ) -> None:
        assert interaction.guild_id is not None
        gid = interaction.guild_id

        if action.value == "list":
            bank = self.guild_emojis.get(gid, [])
            if not bank:
                await interaction.response.send_message("No emojis in the bank yet. Use `/bikiemojis add` to add some.", ephemeral=True)
                return
            lines = [f"`#{e['id']}` {e['emoji']} — *{e['situation']}*" for e in bank]
            text  = "\n".join(lines)
            if len(text) > 1900:
                text = text[:1900] + "\n… *(truncated)*"
            await interaction.response.send_message(
                f"**Emoji bank ({len(bank)}/40):**\n{text}", ephemeral=True
            )

        elif action.value == "remove":
            if emoji_id is None:
                await interaction.response.send_message("❌ Provide an `emoji_id`.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            try:
                deleted = await asyncio.to_thread(_db_delete_emoji, emoji_id, gid)
            except Exception as exc:
                await interaction.followup.send(f"❌ DB error: `{exc}`", ephemeral=True)
                return
            if not deleted:
                await interaction.followup.send(f"❌ No emoji with ID `{emoji_id}`.", ephemeral=True)
                return
            self.guild_emojis[gid] = [e for e in self.guild_emojis.get(gid, []) if e["id"] != emoji_id]
            await interaction.followup.send(f"✅ Emoji `#{emoji_id}` removed.", ephemeral=True)

        elif action.value == "clear":
            await interaction.response.defer(ephemeral=True)
            try:
                cnt = await asyncio.to_thread(_db_clear_emojis, gid)
                self.guild_emojis.pop(gid, None)
            except Exception as exc:
                await interaction.followup.send(f"❌ DB error: `{exc}`", ephemeral=True)
                return
            await interaction.followup.send(f"✅ Cleared {cnt} emoji(s) from the bank.", ephemeral=True)

        elif action.value == "add":
            if not emoji or not emoji.strip():
                await interaction.response.send_message("❌ Provide the `emoji` to add.", ephemeral=True)
                return
            if not situation or not situation.strip():
                await interaction.response.send_message("❌ Provide a `situation` description.", ephemeral=True)
                return
            if len(situation) > 200:
                await interaction.response.send_message("❌ Situation description too long (max 200 chars).", ephemeral=True)
                return
            current_count = len(self.guild_emojis.get(gid, []))
            if current_count >= 40:
                await interaction.response.send_message(
                    f"❌ You already have **{current_count}/40** emojis. Remove some first.",
                    ephemeral=True,
                )
                return
            await interaction.response.defer(ephemeral=True)
            try:
                new_id = await asyncio.to_thread(_db_add_emoji, gid, emoji.strip(), situation.strip())
                self.guild_emojis.setdefault(gid, []).append(
                    {"id": new_id, "emoji": emoji.strip(), "situation": situation.strip()}
                )
            except Exception as exc:
                await interaction.followup.send(f"❌ DB error: `{exc}`", ephemeral=True)
                return
            total = len(self.guild_emojis.get(gid, []))
            await interaction.followup.send(
                f"✅ Added emoji `#{new_id}`!\n"
                f"{emoji.strip()} → *{situation.strip()}*\n\n"
                f"Biki now has **{total}/40** emojis registered.",
                ephemeral=True,
            )

    @app_commands.command(
        name="bikilore",
        description="View, set, or clear Biki's server lore.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        action="'set', 'view', or 'clear'.",
        lore="The lore text to set (only needed for 'set').",
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="set",   value="set"),
        app_commands.Choice(name="view",  value="view"),
        app_commands.Choice(name="clear", value="clear"),
    ])
    async def bikilore(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        lore: Optional[str] = None,
    ) -> None:
        assert interaction.guild_id is not None
        gid = interaction.guild_id

        if action.value == "view":
            text = self.guild_lore.get(gid, "").strip()
            if not text:
                await interaction.response.send_message("No server lore set yet.", ephemeral=True)
                return
            preview = text if len(text) <= 1800 else text[:1800] + "\n… *(truncated)*"
            await interaction.response.send_message(
                f"**Server Lore:**\n```\n{preview}\n```", ephemeral=True
            )

        elif action.value == "clear":
            await interaction.response.defer(ephemeral=True)
            try:
                await asyncio.to_thread(_db_clear_lore, gid)
                self.guild_lore.pop(gid, None)
            except Exception as exc:
                await interaction.followup.send(f"❌ DB error: `{exc}`", ephemeral=True)
                return
            await interaction.followup.send("✅ Server lore cleared.", ephemeral=True)

        elif action.value == "set":
            if not lore or not lore.strip():
                await interaction.response.send_message("❌ Provide the lore text.", ephemeral=True)
                return
            if len(lore) > 3000:
                await interaction.response.send_message(
                    f"❌ Lore too long ({len(lore)} chars). Max 3000.", ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True)
            try:
                await asyncio.to_thread(_db_set_lore, gid, lore.strip())
                self.guild_lore[gid] = lore.strip()
            except Exception as exc:
                await interaction.followup.send(f"❌ DB error: `{exc}`", ephemeral=True)
                return
            preview = lore.strip()[:400] + ("…" if len(lore) > 400 else "")
            await interaction.followup.send(
                f"✅ Server lore saved.\n\n**Preview:**\n```\n{preview}\n```",
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
            updated = await asyncio.to_thread(_db_add_channel, interaction.guild_id, channel.id)
            self.allowed_channels[interaction.guild_id] = updated
        except Exception as exc:
            await interaction.followup.send(f"❌ DB error: `{exc}`", ephemeral=True)
            return
        await interaction.followup.send(f"✅ Biki can now respond in {channel.mention}.", ephemeral=True)

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
            updated = await asyncio.to_thread(_db_remove_channel, interaction.guild_id, channel.id)
            self.allowed_channels[interaction.guild_id] = updated
        except Exception as exc:
            await interaction.followup.send(f"❌ DB error: `{exc}`", ephemeral=True)
            return
        await interaction.followup.send(f"✅ Biki will no longer respond in {channel.mention}.", ephemeral=True)

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
                "Biki responds in **all channels** (no restrictions set). Use `/aiset` to restrict her.",
                ephemeral=True,
            )
            return
        mentions = " ".join(f"<#{cid}>" for cid in allowed)
        await interaction.response.send_message(
            f"Biki can respond in: {mentions}", ephemeral=True
        )

    @app_commands.command(
        name="aireset",
        description="Clear Biki's conversation history with a user.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(user="The user whose history to clear. Leave blank to clear all.")
    async def aireset(
        self, interaction: discord.Interaction, user: Optional[discord.Member] = None
    ) -> None:
        assert interaction.guild_id is not None
        gid = interaction.guild_id
        if user is not None:
            key = (gid, user.id)
            self.conversations.pop(key, None)
            await interaction.response.send_message(
                f"✅ Cleared Biki's conversation history with {user.mention}.", ephemeral=True
            )
        else:
            to_remove = [k for k in self.conversations if k[0] == gid]
            for k in to_remove:
                self.conversations.pop(k)
            await interaction.response.send_message(
                f"✅ Cleared all conversation history for this server ({len(to_remove)} user(s)).",
                ephemeral=True,
            )

    @app_commands.command(
        name="bikiwarns",
        description="View warnings for a user.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def bikiwarns(
        self, interaction: discord.Interaction, user: discord.Member
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        try:
            warnings = await asyncio.to_thread(_db_get_warnings, interaction.guild_id, user.id)
        except Exception as exc:
            await interaction.followup.send(f"❌ DB error: `{exc}`", ephemeral=True)
            return
        if not warnings:
            await interaction.followup.send(f"{user.mention} has no warnings.", ephemeral=True)
            return
        lines = [f"**{user.display_name}** — {len(warnings)} warning(s)\n"]
        for i, w in enumerate(warnings, 1):
            ts = w["created_at"].strftime("%Y-%m-%d %H:%M") if w.get("created_at") else "unknown"
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
            await asyncio.to_thread(_db_clear_warnings, interaction.guild_id, user.id)
        except Exception as exc:
            await interaction.followup.send(f"❌ DB error: `{exc}`", ephemeral=True)
            return
        await interaction.followup.send(f"✅ Cleared all warnings for {user.mention}.", ephemeral=True)


    # ==================================================================
    # ── NEW FEATURES ──────────────────────────────────────────────────
    # ==================================================================

    # ------------------------------------------------------------------
    # Auto-mood: wire _detect_energy → guild_moods every N messages
    # Called inside _learn_from_message already tracks energy; this
    # method applies it to the active mood when auto-mood is enabled.
    # ------------------------------------------------------------------

    def _maybe_automood(self, guild_id: int) -> None:
        """If auto-mood is on for this guild, shift mood based on server energy."""
        if not self.guild_automood.get(guild_id):
            return
        vocab  = self.server_vocab.get(guild_id, {})
        energy = vocab.get("energy", "mixed")
        mapping = {
            "hype":    "chaotic",
            "chaotic": "chaotic",
            "chill":   "sad",
            "mixed":   "happy",
        }
        new_mood = mapping.get(energy, "happy")
        if self.guild_moods.get(guild_id) != new_mood:
            self.guild_moods[guild_id] = new_mood
            asyncio.ensure_future(asyncio.to_thread(_db_set_mood, guild_id, new_mood))
            log.debug("auto-mood: guild %d → %s (energy=%s)", guild_id, new_mood, energy)

    @app_commands.command(
        name="bikiautomood",
        description="Toggle auto-mood: Biki shifts personality based on server energy automatically.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def bikiautomood(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        gid      = interaction.guild_id
        new_state = not self.guild_automood.get(gid, False)
        self.guild_automood[gid] = new_state
        if new_state:
            await interaction.response.send_message(
                "🎭 **Auto-mood ON** — Biki will now shift personality based on server energy.\n"
                "Hype chat → Chaotic · Chill chat → Sad · Mixed → Happy\n"
                "She'll update automatically as the vibe changes.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "🎭 **Auto-mood OFF** — Biki will stay on the manually set mood.",
                ephemeral=True,
            )

    # ------------------------------------------------------------------
    # /roastbattle — structured back-and-forth roast battle
    # ------------------------------------------------------------------

    @app_commands.command(
        name="roastbattle",
        description="Start a roast battle between two people. Biki judges and roasts both.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        opponent="The person you want to roast battle with.",
        opening="Your opening roast (optional — Biki will generate one if blank).",
    )
    async def roastbattle(
        self,
        interaction: discord.Interaction,
        opponent: discord.Member,
        opening: Optional[str] = None,
    ) -> None:
        assert interaction.guild_id is not None
        gid        = interaction.guild_id
        challenger = interaction.user

        if opponent.id == challenger.id:
            await interaction.response.send_message("you wanna roast yourself? bold. but no 💀", ephemeral=True)
            return
        if opponent.bot:
            await interaction.response.send_message("bro don't make me roast a bot 😭 pick a real person", ephemeral=True)
            return

        await interaction.response.defer()

        # Challenger's opening
        if opening:
            challenger_roast = opening.strip()
        else:
            prompt = (
                f"Generate a savage but playful roast from {challenger.display_name} aimed at {opponent.display_name}. "
                "1-2 sentences max. no mercy but keep it funny not hateful. lowercase discord style."
            )
            try:
                challenger_roast = await _call_ai(
                    [{"role": "user", "content": prompt}],
                    self._mood_addon(gid), max_tokens=120,
                )
            except Exception:
                challenger_roast = f"{challenger.display_name} said something devastating but i forgot what it was"

        # Opponent's counter
        prompt2 = (
            f"Roast battle. {challenger.display_name} just said to {opponent.display_name}: \"{challenger_roast}\"\n"
            f"Now write {opponent.display_name}'s savage counter-roast. 1-2 sentences. no mercy. lowercase discord style."
        )
        try:
            opponent_roast = await _call_ai(
                [{"role": "user", "content": prompt2}],
                self._mood_addon(gid), max_tokens=120,
            )
        except Exception:
            opponent_roast = "i'm too destroyed to respond rn"

        # Biki judges
        prompt3 = (
            f"You're judging a roast battle between {challenger.display_name} and {opponent.display_name}.\n"
            f"{challenger.display_name} said: \"{challenger_roast}\"\n"
            f"{opponent.display_name} replied: \"{opponent_roast}\"\n"
            "Give a chaotic, funny verdict on who won. Pick a winner. Be dramatic. 2-3 sentences max. be yourself."
        )
        try:
            verdict = await _call_ai(
                [{"role": "user", "content": prompt3}],
                self._mood_addon(gid), max_tokens=150,
            )
        except Exception:
            verdict = "honestly both of you lost and i'm the winner"

        await interaction.followup.send(
            f"🔥 **ROAST BATTLE** — {challenger.mention} vs {opponent.mention}\n\n"
            f"**{challenger.display_name}:** {challenger_roast}\n\n"
            f"**{opponent.display_name}:** {opponent_roast}\n\n"
            f"**biki's verdict:** {verdict}"
        )

    # ------------------------------------------------------------------
    # /truthordare — Biki hosts truth or dare
    # ------------------------------------------------------------------

    @app_commands.command(
        name="truthordare",
        description="Biki gives you a truth or dare. She will not be nice about it.",
    )
    @app_commands.guild_only()
    @app_commands.describe(choice="truth or dare?")
    @app_commands.choices(choice=[
        app_commands.Choice(name="truth", value="truth"),
        app_commands.Choice(name="dare",  value="dare"),
    ])
    async def truthordare(
        self,
        interaction: discord.Interaction,
        choice: app_commands.Choice[str],
    ) -> None:
        assert interaction.guild_id is not None
        gid  = interaction.guild_id
        user = interaction.user

        await interaction.response.defer()

        # Inject server knowledge about this user for personalised questions
        profile  = self.user_memory.get(gid, {}).get(user.id, {})
        kb_facts = self.server_knowledge.get(gid, {}).get(
            user.display_name.split()[0], []
        )
        context = ""
        if profile.get("notes") or kb_facts:
            notes_str = " | ".join((profile.get("notes") or [])[-4:])
            facts_str = " | ".join(kb_facts[-4:])
            context   = f"Things you know about {user.display_name}: {notes_str} {facts_str}. Use this to make it personal."

        if choice.value == "truth":
            prompt = (
                f"You're hosting truth or dare for {user.display_name} in a Discord server. "
                f"Give them a juicy, personal, slightly uncomfortable truth question. "
                f"Make it feel like you KNOW them. Short — one sentence. lowercase. no intro. {context}"
            )
        else:
            prompt = (
                f"You're hosting truth or dare for {user.display_name} in a Discord server. "
                f"Give them a chaotic, funny, slightly embarrassing dare. "
                f"Must be possible in a Discord server (type something, send something, ping someone, etc). "
                f"Short — one sentence. lowercase. no intro. {context}"
            )

        try:
            result = await _call_ai(
                [{"role": "user", "content": prompt}],
                self._mood_addon(gid), max_tokens=100,
            )
        except Exception:
            result = "i dare you to go touch grass. truth: when did you last go outside. pick one"

        label = "🤫 TRUTH" if choice.value == "truth" else "😈 DARE"
        await interaction.followup.send(
            f"{label} for {interaction.user.mention}\n\n**{result}**"
        )

    # ------------------------------------------------------------------
    # /wouldyourather — Biki generates chaotic WYR options
    # ------------------------------------------------------------------

    @app_commands.command(
        name="wouldyourather",
        description="Biki gives you a chaotic would-you-rather. No good options.",
    )
    @app_commands.guild_only()
    @app_commands.describe(theme="Optional theme (e.g. 'food', 'discord', 'cursed'). Leave blank for random chaos.")
    async def wouldyourather(
        self,
        interaction: discord.Interaction,
        theme: Optional[str] = None,
    ) -> None:
        assert interaction.guild_id is not None
        gid = interaction.guild_id

        await interaction.response.defer()

        theme_part = f"themed around: {theme}." if theme else "completely random and unhinged."
        prompt = (
            f"Generate a would-you-rather question for a Discord server. {theme_part} "
            "Both options should be equally terrible or equally absurd — no easy choice. "
            "Format: 'would you rather [A] or [B]?' "
            "Keep it short, funny, and chaotic. lowercase. no intro."
        )
        try:
            question = await _call_ai(
                [{"role": "user", "content": prompt}],
                self._mood_addon(gid), max_tokens=120,
            )
        except Exception:
            question = "would you rather lose your voice forever or only be able to speak in questions?"

        # Discord poll-style buttons
        view = _WYRView(question, interaction.user)
        await interaction.followup.send(
            f"🤔 **would you rather...**\n\n{question}\n\n"
            f"*(started by {interaction.user.mention})*",
            view=view,
        )

    # ------------------------------------------------------------------
    # /bikitrivia — trivia with Biki's personality (intentionally wrong sometimes)
    # ------------------------------------------------------------------

    @app_commands.command(
        name="bikitrivia",
        description="Biki hosts a trivia question. She may or may not be right.",
    )
    @app_commands.guild_only()
    @app_commands.describe(category="Topic (e.g. 'pop culture', 'science', 'random'). Default is random.")
    async def bikitrivia(
        self,
        interaction: discord.Interaction,
        category: Optional[str] = None,
    ) -> None:
        assert interaction.guild_id is not None
        gid = interaction.guild_id

        await interaction.response.defer()

        cat_part = f"about {category}" if category else "about anything (pop culture, history, science, internet, etc)"
        prompt = (
            f"Generate a trivia question {cat_part} for a Discord server. "
            "Give 4 answer options labeled A, B, C, D. Mark which one is correct. "
            "You are Biki — chaotic, online, pick-me energy. Present it in your voice. lowercase. "
            "15% chance you 'accidentally' mark a wrong answer as correct (be subtle about it). "
            "Format exactly like this (no extra text):\n"
            "QUESTION: [question here]\n"
            "A: [option]\n"
            "B: [option]\n"
            "C: [option]\n"
            "D: [option]\n"
            "ANSWER: [A/B/C/D]\n"
            "BIKI_COMMENT: [short chaotic comment about the question, 1 sentence]"
        )
        try:
            raw = await _call_ai(
                [{"role": "user", "content": prompt}],
                max_tokens=250,
            )
        except Exception:
            raw = "QUESTION: what is 2+2\nA: 4\nB: fish\nC: window\nD: yes\nANSWER: A\nBIKI_COMMENT: i know math trust me"

        # Parse the response
        lines      = {l.split(":")[0].strip(): ":".join(l.split(":")[1:]).strip()
                      for l in raw.splitlines() if ":" in l}
        question   = lines.get("QUESTION", "what is the meaning of life")
        opt_a      = lines.get("A", "42")
        opt_b      = lines.get("B", "nothing")
        opt_c      = lines.get("C", "chaos")
        opt_d      = lines.get("D", "biki")
        answer     = lines.get("ANSWER", "A").strip().upper()
        comment    = lines.get("BIKI_COMMENT", "answer carefully bestie")

        options = {"A": opt_a, "B": opt_b, "C": opt_c, "D": opt_d}
        view    = _TriviaView(answer, options, interaction.user)

        await interaction.followup.send(
            f"🧠 **BIKI TRIVIA**\n\n"
            f"*{comment}*\n\n"
            f"**{question}**\n\n"
            f"🅰️ {opt_a}\n"
            f"🅱️ {opt_b}\n"
            f"🆑 {opt_c}\n"
            f"🇩 {opt_d}",
            view=view,
        )

    # ------------------------------------------------------------------
    # /bikiactivity — server activity stats ("who's most active this week")
    # ------------------------------------------------------------------

    @app_commands.command(
        name="bikiactivity",
        description="Show server activity stats — who talks the most, Biki's favorites, etc.",
    )
    @app_commands.guild_only()
    async def bikiactivity(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        gid = interaction.guild_id

        await interaction.response.defer(ephemeral=True)

        # Pull from in-memory user profiles
        guild_mem = self.user_memory.get(gid, {})
        if not guild_mem:
            # Try loading from DB
            try:
                guild_mem = await asyncio.to_thread(_db_load_all_user_memory, gid)
                self.user_memory[gid] = guild_mem
            except Exception:
                pass

        if not guild_mem:
            await interaction.followup.send("biki hasn't seen enough activity here yet to judge anyone 💀", ephemeral=True)
            return

        # Sort by message count
        sorted_users = sorted(guild_mem.items(), key=lambda x: x[1].get("message_count", 0), reverse=True)
        top5         = sorted_users[:5]

        # Biki's commentary on top users
        names_counts = ", ".join(
            f"{d.get('display_name', 'unknown')} ({d.get('message_count', 0)} msgs)"
            for _, d in top5
        )
        prompt = (
            f"These are the most active people in your server: {names_counts}\n"
            "Give a chaotic 2-sentence Biki-style commentary about them — roast the top one a little, "
            "be confused about the quiet ones. Be yourself. lowercase."
        )
        try:
            biki_take = await _call_ai([{"role": "user", "content": prompt}], self._mood_addon(gid), max_tokens=150)
        except Exception:
            biki_take = "y'all are literally never offline and i respect and fear that"

        # Most spoken-to this session
        session_fave = None
        if self._users_spoken:
            # Cross-ref with memory to get display name
            for uid in list(self._users_spoken)[:1]:
                p = guild_mem.get(uid)
                if p:
                    session_fave = p.get("display_name", f"<@{uid}>")

        lines = ["**📊 biki's server activity report**\n"]
        for rank, (uid, data) in enumerate(top5, 1):
            medal = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][rank - 1]
            name  = data.get("display_name", f"<@{uid}>")
            count = data.get("message_count", 0)
            lines.append(f"{medal} **{name}** — {count} messages seen")

        lines.append(f"\n*biki's take: {biki_take}*")
        if session_fave:
            lines.append(f"\n💕 **biki's session fave:** {session_fave} (talked to biki the most today)")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # ------------------------------------------------------------------
    # /bikiweather — Biki reacts to weather in character
    # ------------------------------------------------------------------

    @app_commands.command(
        name="bikiweather",
        description="Ask Biki about the weather. She has opinions.",
    )
    @app_commands.guild_only()
    @app_commands.describe(city="The city to check weather for.")
    async def bikiweather(
        self, interaction: discord.Interaction, city: str
    ) -> None:
        assert interaction.guild_id is not None
        gid = interaction.guild_id

        await interaction.response.defer()

        weather_key = getattr(config, "OPENWEATHERMAP_KEY", None)
        if not weather_key:
            await interaction.followup.send(
                "bro nobody gave me a weather api key 😭 add `OPENWEATHERMAP_KEY` to config and try again"
            )
            return

        # Fetch weather
        try:
            import aiohttp
            url = (
                f"https://api.openweathermap.org/data/2.5/weather"
                f"?q={city}&appid={weather_key}&units=metric"
            )
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 404:
                        await interaction.followup.send(f"bro i've never heard of '{city}' 💀 spell it right")
                        return
                    if resp.status != 200:
                        await interaction.followup.send("weather api said no. idk what that means for you but sounds bad")
                        return
                    data = await resp.json()
        except Exception as exc:
            log.warning("bikiweather: API call failed: %s", exc)
            await interaction.followup.send("couldn't reach the weather thingy rn. try again later or go outside and check yourself")
            return

        temp_c    = round(data["main"]["temp"])
        feels_c   = round(data["main"]["feels_like"])
        humidity  = data["main"]["humidity"]
        condition = data["weather"][0]["description"]
        city_name = data.get("name", city)
        country   = data.get("sys", {}).get("country", "")

        prompt = (
            f"The weather in {city_name}, {country} right now: {temp_c}°C (feels like {feels_c}°C), "
            f"{condition}, {humidity}% humidity.\n"
            "React to this weather in your Biki voice — chaotic, opinionated, personal. "
            "Maybe complain, maybe be excited, maybe relate it to drama. 2-3 short lines max. lowercase. be yourself."
        )
        try:
            biki_react = await _call_ai(
                [{"role": "user", "content": prompt}],
                self._mood_addon(gid), max_tokens=150,
            )
        except Exception:
            biki_react = "the weather is doing something and i have thoughts about it"

        await interaction.followup.send(
            f"🌤️ **{city_name}, {country}**\n"
            f"`{temp_c}°C` · feels like `{feels_c}°C` · {condition} · {humidity}% humidity\n\n"
            f"{biki_react}"
        )

    # ------------------------------------------------------------------
    # /bikivote — start a vote/poll (Discord buttons, no external API)
    # ------------------------------------------------------------------

    @app_commands.command(
        name="bikivote",
        description="Biki starts a vote. She may have opinions on the result.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        question="What to vote on.",
        option_a="First option.",
        option_b="Second option.",
        option_c="Third option (optional).",
        option_d="Fourth option (optional).",
    )
    async def bikivote(
        self,
        interaction: discord.Interaction,
        question: str,
        option_a: str,
        option_b: str,
        option_c: Optional[str] = None,
        option_d: Optional[str] = None,
    ) -> None:
        assert interaction.guild_id is not None
        gid     = interaction.guild_id
        options = [o for o in [option_a, option_b, option_c, option_d] if o]

        await interaction.response.defer()

        # Biki's take on the question
        prompt = (
            f"Someone in your server wants a vote on: '{question}'\n"
            f"Options: {', '.join(options)}\n"
            "Give a very short Biki-style comment about the vote — pick a side if you have one, "
            "be chaotic. One sentence. lowercase. no intro."
        )
        try:
            biki_comment = await _call_ai(
                [{"role": "user", "content": prompt}],
                self._mood_addon(gid), max_tokens=80,
            )
        except Exception:
            biki_comment = "vote honestly or don't vote at all i literally don't care"

        view = _VoteView(question, options)
        await interaction.followup.send(
            f"🗳️ **VOTE** — started by {interaction.user.mention}\n\n"
            f"**{question}**\n\n"
            f"*biki's take: {biki_comment}*",
            view=view,
        )

    # ------------------------------------------------------------------
    # Auto-moderation — toxicity detection on every message
    # (rule-based + AI callout, non-blocking)
    # ------------------------------------------------------------------

    async def _check_automod(self, message: discord.Message, clean: str) -> None:
        """
        Silently scan messages for toxicity patterns.
        If triggered: Biki calls it out in character (not as a mod, as herself).
        Only fires if automod is enabled for the guild.
        """
        if not message.guild:
            return
        gid = message.guild.id
        if not self.guild_automod.get(gid):
            return

        lower = clean.lower()

        # Spam detection: same word repeated 4+ times
        words = lower.split()
        if len(words) >= 4:
            word_counts = Counter(words)
            most_common_word, count = word_counts.most_common(1)[0]
            if count >= 4 and len(most_common_word) > 1:
                prompt = (
                    f"{message.author.display_name} is spamming the word '{most_common_word}' over and over. "
                    "Call them out in your Biki voice — annoyed, chaotic, brief. 1 sentence. lowercase."
                )
                try:
                    reply = await _call_ai([{"role": "user", "content": prompt}], self._mood_addon(gid), max_tokens=80)
                    await message.channel.send(reply)
                except Exception:
                    pass
                return

        # Toxicity keyword patterns — basic rule-based
        _TOXIC_PATTERNS = [
            r"\bkill\s+your\s*self\b",
            r"\bkys\b",
            r"\bkms\b",
            r"\bi\s+hate\s+(everyone|everybody|you all)\b",
        ]
        for pattern in _TOXIC_PATTERNS:
            if re.search(pattern, lower):
                prompt = (
                    f"{message.author.display_name} just said something really not okay in your server. "
                    "You're not a mod but you're calling it out — concerned, a bit unhinged about it. "
                    "1-2 sentences. be yourself. lowercase."
                )
                try:
                    reply = await _call_ai([{"role": "user", "content": prompt}], self._mood_addon(gid), max_tokens=100)
                    await message.channel.send(reply)
                except Exception:
                    pass
                return

    @app_commands.command(
        name="bikiautomod",
        description="Toggle Biki's auto-moderation — she'll call out spam and toxicity in character.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def bikiautomod(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        gid       = interaction.guild_id
        new_state = not self.guild_automod.get(gid, False)
        self.guild_automod[gid] = new_state
        if new_state:
            await interaction.response.send_message(
                "🚨 **Auto-mod ON** — Biki will now call out spam and toxic messages in her own chaotic way.\n"
                "She's not a real moderator. She just has opinions.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "🚨 **Auto-mod OFF** — Biki will keep her mouth shut about rule-breaking. probably.",
                ephemeral=True,
            )


# ===========================================================================
# Discord UI components for new features
# ===========================================================================

class _WYRView(discord.ui.View):
    """Would You Rather — two-option vote buttons."""

    def __init__(self, question: str, starter: discord.User | discord.Member) -> None:
        super().__init__(timeout=120)
        self.question = question
        self.starter  = starter
        self.votes: dict[str, set[int]] = {"A": set(), "B": set()}

        # Parse A / B from question
        parts = re.split(r"\s+or\s+", question, maxsplit=1, flags=re.IGNORECASE)
        self.label_a = parts[0].replace("would you rather", "").strip().capitalize()[:50] if len(parts) > 0 else "Option A"
        self.label_b = parts[1].strip().capitalize()[:50] if len(parts) > 1 else "Option B"

        btn_a = discord.ui.Button(label=f"🅰️ {self.label_a}", style=discord.ButtonStyle.primary, custom_id="wyr_a")
        btn_b = discord.ui.Button(label=f"🅱️ {self.label_b}", style=discord.ButtonStyle.danger,  custom_id="wyr_b")
        btn_a.callback = self._vote_a
        btn_b.callback = self._vote_b
        self.add_item(btn_a)
        self.add_item(btn_b)

    def _tally(self) -> str:
        total = len(self.votes["A"]) + len(self.votes["B"]) or 1
        pct_a = int(len(self.votes["A"]) / total * 100)
        pct_b = 100 - pct_a
        return f"🅰️ {pct_a}% · 🅱️ {pct_b}% ({len(self.votes['A'])+len(self.votes['B'])} votes)"

    async def _vote_a(self, interaction: discord.Interaction) -> None:
        self.votes["B"].discard(interaction.user.id)
        self.votes["A"].add(interaction.user.id)
        await interaction.response.send_message(f"you picked **{self.label_a}** 👀  {self._tally()}", ephemeral=True)

    async def _vote_b(self, interaction: discord.Interaction) -> None:
        self.votes["A"].discard(interaction.user.id)
        self.votes["B"].add(interaction.user.id)
        await interaction.response.send_message(f"you picked **{self.label_b}** 👀  {self._tally()}", ephemeral=True)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]


class _TriviaView(discord.ui.View):
    """Trivia — four answer buttons, reveals correct answer on click."""

    LABELS = {"A": "🅰️", "B": "🅱️", "C": "🆑", "D": "🇩"}

    def __init__(
        self,
        correct: str,
        options: dict[str, str],
        host: discord.User | discord.Member,
    ) -> None:
        super().__init__(timeout=60)
        self.correct  = correct.upper()
        self.options  = options
        self.host     = host
        self.answered: set[int] = set()

        for letter in ["A", "B", "C", "D"]:
            btn = discord.ui.Button(
                label=f"{self.LABELS[letter]} {letter}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"trivia_{letter}",
            )
            btn.callback = self._make_cb(letter)
            self.add_item(btn)

    def _make_cb(self, letter: str):
        async def callback(interaction: discord.Interaction) -> None:
            if interaction.user.id in self.answered:
                await interaction.response.send_message("you already answered bestie 💀", ephemeral=True)
                return
            self.answered.add(interaction.user.id)
            if letter == self.correct:
                await interaction.response.send_message(
                    f"✅ **correct!** the answer was **{self.correct}: {self.options[self.correct]}**\ngood job i guess",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    f"❌ **wrong lmaooo** it was **{self.correct}: {self.options[self.correct]}**\nbetter luck next time bestie 💀",
                    ephemeral=True,
                )
        return callback

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]


class _VoteView(discord.ui.View):
    """Generic multi-option poll with buttons."""

    EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]
    STYLES = [
        discord.ButtonStyle.primary,
        discord.ButtonStyle.danger,
        discord.ButtonStyle.success,
        discord.ButtonStyle.secondary,
    ]

    def __init__(self, question: str, options: list[str]) -> None:
        super().__init__(timeout=300)
        self.question = question
        self.options  = options
        self.votes: dict[int, set[int]] = {i: set() for i in range(len(options))}

        for i, opt in enumerate(options):
            btn = discord.ui.Button(
                label=f"{self.EMOJIS[i]} {opt[:50]}",
                style=self.STYLES[i % len(self.STYLES)],
                custom_id=f"vote_{i}",
            )
            btn.callback = self._make_cb(i)
            self.add_item(btn)

    def _tally(self) -> str:
        total = sum(len(v) for v in self.votes.values()) or 1
        parts = []
        for i, opt in enumerate(self.options):
            cnt = len(self.votes[i])
            pct = int(cnt / total * 100)
            parts.append(f"{self.EMOJIS[i]} {opt}: **{cnt}** ({pct}%)")
        return "\n".join(parts)

    def _make_cb(self, idx: int):
        async def callback(interaction: discord.Interaction) -> None:
            uid = interaction.user.id
            # Remove from all other options (switch vote)
            for i in self.votes:
                self.votes[i].discard(uid)
            self.votes[idx].add(uid)
            await interaction.response.send_message(
                f"voted for **{self.options[idx]}** ✅\n\n**current results:**\n{self._tally()}",
                ephemeral=True,
            )
        return callback

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AiCompanion(bot))
