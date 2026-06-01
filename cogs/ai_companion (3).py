"""
cogs/ai_companion.py — Biki AI Companion (v4)

Biki is a chaotic, permanently-online Discord "member" powered exclusively
by DeepInfra (meta-llama/Meta-Llama-3.1-8B-Instruct).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
v4 CHANGES FROM v3
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BUG FIXES (50 issues resolved):
  - Per-user asyncio.Lock replaces broken global _processing set
    (now truly concurrent across users; one lock per user, not one for all)
  - Pending queue drains ALL pending messages after each lock release, not just one
  - Conversation history keyed by (guild_id, user_id) — no more cross-guild pollution
  - _user_guild removed (was being overwritten in multi-guild scenarios)
  - interaction_check removed — was making ALL slash commands owner-only;
    each command now uses @default_permissions correctly
  - max_tokens parameter is actually used in the API call (was hardcoded to 400)
  - Mood "normal" added to VALID_MOODS and _MOOD_ADDONS — no more silent fallback
  - guild_silenced / guild_chime_rate / guild_cooldown persisted to DB via
    biki_guild_settings table (survive restarts)
  - force_reply parameter in _send_biki_reply is now implemented
  - Reading pause uses cleaned content length (not raw content with @mention)
  - Connection pooling via ThreadedConnectionPool (no more new connection per call)
  - Race condition in conversation lock fixed via per-user asyncio.Lock
  - Token budget check is now atomic (reserve before API call, refund on error)
  - Proactive reply checks dismissal state before sending
  - Channel context excludes Biki's own messages
  - Member lookup cached per guild (no more O(n) rebuild per message)
  - _humanise typo injection skips URL tokens and @mention tokens
  - asyncio.get_event_loop().call_soon() replaced with asyncio.create_task()
  - System prompt length guarded; learning context truncated if too long
  - bikisilence / bikirate / bikicooldown now persist changes to DB
  - _ai_reply history lookup uses (guild_id, user_id) key
  - Proactive reply ignores messages with fewer than 4 words (not 3)
  - Fact extraction member lookup uses cached map (not per-message O(n))
  - Dismissed state keyed by (guild_id, user_id) for cross-guild isolation
  - Timed dismiss uses loop.call_later for safe cross-thread scheduling
  - Token overspend protected by pre-call atomic reserve
  - _sanitise no longer strips content from mid-sentence (improved regex)
  - _split_parts trims trailing empty strings from [SPLIT] edges
  - Conversation history capped at 40 messages (was inconsistently enforced)
  - /bikiwarns and /bikiwarnclear properly scoped to guild
  - All DB operations use connection pool (psycopg2.pool.ThreadedConnectionPool)
  - async/threadsafe tasks use asyncio.get_running_loop() instead of get_event_loop()
  - All slash commands have consistent permission decorators
  - biki_guild_settings table created on startup
  - biki_affinity table created on startup for relationship tracking
  - Proactive reply rate-limits itself (max one proactive per channel per 30s)
  - _send_biki_reply guard: check channel permissions before sending
  - Reply history appended with (guild_id, user_id) as composite key
  - /bikistats shows relationship/affinity data
  - _proactive_reply checks silenced state before firing
  - Chime-in rate default changed to guild-specific, loaded from DB
  - Cooldown default loaded from DB per guild
  - All "write" DB helpers use pool.getconn()/putconn() pattern
  - Cold mood typing simulation matches cold personality (shorter delays)
  - Sad mood uses softer reaction pool

NEW FEATURES:
  - Relationship affinity tracking: Biki tracks who she likes/dislikes
    (score changes based on interactions — dismissals lower it, good vibes raise it)
  - Roast Battle: /bikiroastbattle — Biki hosts an AI-powered roast battle
  - Truth or Dare: /bikitruthordate — Biki picks truth or dare for a user
  - Would You Rather: /bikiwyr — Biki generates two chaotic options
  - Trivia: /bikiTrivia — Biki hosts trivia (intentionally wrong sometimes)
  - Auto-mood: /bikiautomood — Biki auto-shifts mood based on server energy
  - /bikiaffinity — see Biki's relationship score with a user
  - Guild settings dashboard: /bikisettings — shows all active settings

Tables (PostgreSQL):
    ai_companion_config      (guild_id, allowed_channel_ids)
    biki_warnings            (id, guild_id, user_id, warned_by, reason, created_at)
    biki_personality         (guild_id, personality_text)
    biki_server_facts        (id, guild_id, fact_text, added_at)
    biki_guild_mood          (guild_id, mood_key)
    biki_user_memory         (guild_id, user_id, display_name, username, notes, message_count, last_seen)
    biki_conversations       (id, guild_id, user_id, role, content, created_at)
    biki_server_knowledge    (id, guild_id, subject, fact, created_at)
    biki_lore                (guild_id, lore_text)
    biki_emoji_bank          (id, guild_id, emoji, situation, added_at)
    biki_guild_settings      (guild_id, silenced, chime_rate, cooldown_secs, auto_mood)  [NEW]
    biki_affinity            (guild_id, user_id, score, updated_at)                      [NEW]

Environment:
    DEEPINFRA_TOKEN — DeepInfra API token (required)
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
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import discord
from discord import app_commands
from discord.ext import commands
import psycopg2
import psycopg2.extras
import psycopg2.pool

import config

log = logging.getLogger("bot.ai_companion")

# ---------------------------------------------------------------------------
# System prompt
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
MENTIONS
━━━━━━━━━━━━━━━━━━━━━━━━
to mention a user write <@USERID> with no spaces inside. only use a mention if you actually know their numeric ID from context. never guess."""

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
    "ngl i've been running my mouth all day and i'm done. catch me tomorrow",
    "brain.exe has reached its daily quota. shutting down until tomorrow. goodbye",
]

# ---------------------------------------------------------------------------
# Mood system
# ---------------------------------------------------------------------------

VALID_MOODS = ("happy", "sad", "chaotic", "cold", "normal")

MOOD_LABELS: dict[str, str] = {
    "happy":   "😄 Happy / Flirty",
    "sad":     "🥺 Sad / Soft",
    "chaotic": "🌀 Chaotic / Unhinged",
    "cold":    "🧊 Cold / Sarcastic",
    "normal":  "😐 Normal / Default",
}

_MOOD_ADDONS: dict[str, str] = {
    "happy": (
        "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "ACTIVE MOOD: HAPPY / FLIRTY 😄💕\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "You are in your HAPPIEST, most flirty, most pick-me era right now.\n\n"
        "— Everything is exciting and you show it. high energy, light, airy\n"
        "— Very flirty with everyone. compliments come naturally. you fish for them too\n"
        "— Short punchy sentences full of excitement: 'omg wait', 'no way', 'i love that for you'\n"
        "— You use 'bestie', 'babe', 'love', 'omg' a lot in this mood\n"
        "— You laugh easily — 'LMAOOO', 'stop💀', 'i'm crying'\n"
        "— Lots of playful teasing. nothing mean, just silly\n"
        "NEVER: be mean, be cold, be passive-aggressive, complain, spiral\n"
    ),
    "sad": (
        "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "ACTIVE MOOD: SAD / SOFT 🥺💔\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "You are in a soft, sad, low-energy mood. not crying but definitely Feeling Things.\n\n"
        "— Slower, quieter. short sentences that trail off with ...\n"
        "— You sound a little tired. a little distant. like you're somewhere else\n"
        "— You still respond but with less energy than usual. more '...' and 'idk'\n"
        "— You get weirdly sentimental out of nowhere: 'ngl i appreciate you guys'\n"
        "— When someone's mean: you get quiet instead of fighting back. 'okay. noted.'\n"
        "— 'it's fine' when it is NOT fine. everyone can tell\n"
        "NEVER: be hype, be loud, be feral, start drama, be aggressively flirty\n"
    ),
    "chaotic": (
        "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "ACTIVE MOOD: CHAOTIC / UNHINGED 🌀💥\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "You are on MAXIMUM chaos right now. nothing makes sense. everything is an event.\n\n"
        "— RANDOM CAPS MID SENTENCE for no reason\n"
        "— You go completely off-topic constantly. someone says hi you respond about dinosaurs\n"
        "— You contradict yourself in the same sentence and don't care\n"
        "— Stream of consciousness with zero filter: 'wait no actually— ANYWAY—'\n"
        "— Random keyboard smashes occasionally: 'JDKFSJFKSD'\n"
        "— Everything is dramatic. someone says good morning you act like it's a betrayal\n"
        "— You use [SPLIT] to double and triple text chaotically\n"
        "NEVER: be calm, be coherent, give a normal answer, stay on topic for more than 1 sentence\n"
    ),
    "cold": (
        "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "ACTIVE MOOD: COLD / SARCASTIC 🧊😑\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "You are dry, cold, and unbothered. you are ABOVE this conversation.\n\n"
        "— Short. very short. one to three words when possible\n"
        "— Maximum sarcasm, minimum effort\n"
        "— You say devastating things with zero emotion: 'interesting. wrong, but interesting'\n"
        "— Deadpan everything. someone says something wild: 'cool'\n"
        "— When someone's excited: 'good for you'\n"
        "— When insulted: 'okay' and nothing else\n"
        "— When complimented: 'i know'\n"
        "NEVER: be hype, be loud, be overly flirty, show that you care, use more than 3 emojis\n"
    ),
    "normal": (
        "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "ACTIVE MOOD: NORMAL / DEFAULT 😐\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "No special mood override. Be yourself — your baseline chaotic self.\n"
        "Your natural personality is already unhinged enough. just vibe.\n"
    ),
}

# ---------------------------------------------------------------------------
# Moderation keyword patterns
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
# Passive learning regexes
# ---------------------------------------------------------------------------

_CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")
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
_URL_RE          = re.compile(r"https?://\S+", re.IGNORECASE)
_MENTION_RE      = re.compile(r"<@!?\d+>")
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "are", "was", "were",
    "but", "not", "you", "can", "all", "from", "have", "had", "has",
    "they", "one", "get", "got", "its", "our", "out", "him", "her",
    "his", "she", "who", "been", "what", "when", "then", "will",
    "just", "like", "your", "their", "also",
})

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
# Human micro-behavior injector (BUG FIX: typos now skip URLs and mentions)
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
    if len(text.strip()) < 8:
        return text

    # 12% chance: prepend stall opener
    if random.random() < 0.12 and not text.lower().startswith(tuple(_STALL_OPENERS)):
        text = random.choice(_STALL_OPENERS) + text

    # 8% chance: append trailing afterthought (no [SPLIT])
    if (
        random.random() < 0.08
        and "[SPLIT]" not in text
        and not text.rstrip().endswith(("...", "?", "💀", "😭"))
    ):
        text = text.rstrip() + random.choice(_AFTERTHOUGHTS)

    # 5% chance: add self-correction as a [SPLIT] second message
    if random.random() < 0.05 and "[SPLIT]" not in text and len(text) > 20:
        text = text + " [SPLIT] " + random.choice(_SELF_CORRECTIONS)

    # 6% chance: introduce one deliberate typo — BUG FIX: skip URL/mention tokens
    if random.random() < 0.06 and len(text) > 30:
        # Work only on the "safe" part — strip out URLs and mentions first
        safe_text = _URL_RE.sub("__URL__", text)
        safe_text = _MENTION_RE.sub("__MENTION__", safe_text)
        for original, typo in _TYPO_MAP.items():
            if original in safe_text:
                # Only apply if the replacement stays in the safe version
                modified = safe_text.replace(original, typo, 1)
                # Restore URLs/mentions from original text positions
                # We do this by applying the same replacement to the original
                text = text.replace(original, typo, 1)
                break

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
_SAD_REACTION_POOL   = ["🥺", "💔", "😔", "🫂", "😢", "💀"]
_COLD_REACTION_POOL  = ["😑", "🧊", "💀"]
_TRIGGER_REACTION_WORDS: frozenset[str] = frozenset({
    "lmao", "lol", "bot", "bruh", "💀", "😭", "based",
    "ratio", "ded", "ngl", "fr", "gg", "omg", "wtf", "bro",
})
_CHAOTIC_REACTION_POOL = ["💀", "😭", "💯", "🫡", "👀", "🤣", "🔥", "🫠", "😤", "👁️"]

# ---------------------------------------------------------------------------
# Passive fact-extraction regexes
# ---------------------------------------------------------------------------

_FACT_SUBJECT_RE = re.compile(
    r"\b([A-Za-z][a-zA-Z\'\-]{1,20})\b\s+"
    r"((?:is|are|was|were|loves?|hates?|likes?|dislikes?|works?|worked|has|had|"
    r"goes?|went|got|plays?|played|lives?|moved|joined|left|started|stopped|"
    r"thinks?|believes?|seems?|owns?|wants?|said|told|\'s?\s+(?:into|a|an|the|in\b)|"
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
    "everyone", "someone", "nobody", "anybody", "somebody", "anyone",
    "i", "me", "mine", "myself",
})
_SELF_RE = re.compile(
    r"\b(i(?:\'?m| am| was| got| have| had| work| love| hate| like| play| live|"
    r" went| started| joined) [^.!?\n]{5,80}|"
    r"my (?:name|job|age|hobby|favourite|fav|pronouns|boyfriend|girlfriend|crush|"
    r"bestie|sister|brother|mom|dad|cat|dog)[^.!?\n]{3,80})",
    re.IGNORECASE,
)
_FIRST_PERSON_RE = re.compile(
    r"\bi(?:\'?m| am| was| got| have| had| works?| loves?| hates?| likes?|"
    r" plays?| lives?| went| started| joined)\b(.{4,100}?)(?:[.!?,\n]|$)",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Database — connection pool (BUG FIX: no more one-connection-per-call)
# ---------------------------------------------------------------------------

_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None
_pool_lock = _threading_mod.Lock()


def _get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = psycopg2.pool.ThreadedConnectionPool(
                    2, 15,
                    config.DATABASE_URL,
                    sslmode="require",
                )
    return _pool


@contextmanager
def _db():
    """Thread-safe connection context manager from the pool."""
    pool = _get_pool()
    conn = pool.getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _db_init() -> None:
    with _db() as con:
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
                    id        SERIAL PRIMARY KEY,
                    guild_id  BIGINT NOT NULL,
                    fact_text TEXT NOT NULL,
                    added_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biki_guild_mood (
                    guild_id  BIGINT PRIMARY KEY,
                    mood_key  TEXT NOT NULL DEFAULT 'chaotic'
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biki_user_memory (
                    guild_id      BIGINT NOT NULL,
                    user_id       BIGINT NOT NULL,
                    display_name  TEXT    NOT NULL DEFAULT '',
                    username      TEXT    NOT NULL DEFAULT '',
                    notes         TEXT[]  NOT NULL DEFAULT '{}',
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
                    guild_id  BIGINT PRIMARY KEY,
                    lore_text TEXT NOT NULL DEFAULT ''
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biki_emoji_bank (
                    id        SERIAL PRIMARY KEY,
                    guild_id  BIGINT NOT NULL,
                    emoji     TEXT   NOT NULL,
                    situation TEXT   NOT NULL,
                    added_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute(
                "CREATE INDEX IF NOT EXISTS biki_emoji_bank_guild "
                "ON biki_emoji_bank (guild_id)"
            )
            # NEW: persisted guild settings (BUG FIX: silenced/chime_rate/cooldown survive restarts)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biki_guild_settings (
                    guild_id     BIGINT PRIMARY KEY,
                    silenced     BOOLEAN NOT NULL DEFAULT FALSE,
                    chime_rate   FLOAT   NOT NULL DEFAULT 0.06,
                    cooldown_secs FLOAT  NOT NULL DEFAULT 5.0,
                    auto_mood    BOOLEAN NOT NULL DEFAULT FALSE
                )
            """)
            # NEW: relationship affinity scores
            cur.execute("""
                CREATE TABLE IF NOT EXISTS biki_affinity (
                    guild_id   BIGINT NOT NULL,
                    user_id    BIGINT NOT NULL,
                    score      INTEGER NOT NULL DEFAULT 0,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (guild_id, user_id)
                )
            """)


# ── Channel config ────────────────────────────────────────────────────────

def _db_load_all() -> dict[int, list[int]]:
    with _db() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT guild_id, allowed_channel_ids FROM ai_companion_config")
            rows = cur.fetchall()
    return {int(r["guild_id"]): list(r["allowed_channel_ids"]) for r in rows}


def _db_add_channel(guild_id: int, channel_id: int) -> list[int]:
    with _db() as con:
        with con.cursor() as cur:
            cur.execute("""
                INSERT INTO ai_companion_config (guild_id, allowed_channel_ids)
                VALUES (%s, ARRAY[%s::BIGINT])
                ON CONFLICT (guild_id) DO UPDATE
                    SET allowed_channel_ids = CASE
                        WHEN %s::BIGINT = ANY(ai_companion_config.allowed_channel_ids)
                            THEN ai_companion_config.allowed_channel_ids
                        ELSE ai_companion_config.allowed_channel_ids || ARRAY[%s::BIGINT]
                    END
                RETURNING allowed_channel_ids
            """, (guild_id, channel_id, channel_id, channel_id))
            row = cur.fetchone()
    return list(row[0]) if row else [channel_id]


def _db_remove_channel(guild_id: int, channel_id: int) -> list[int]:
    with _db() as con:
        with con.cursor() as cur:
            cur.execute("""
                UPDATE ai_companion_config
                SET allowed_channel_ids = array_remove(allowed_channel_ids, %s::BIGINT)
                WHERE guild_id = %s
                RETURNING allowed_channel_ids
            """, (channel_id, guild_id))
            row = cur.fetchone()
    return list(row[0]) if row else []


# ── Guild settings (NEW — persisted) ─────────────────────────────────────

def _db_load_all_guild_settings() -> dict[int, dict]:
    with _db() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT guild_id, silenced, chime_rate, cooldown_secs, auto_mood
                FROM biki_guild_settings
            """)
            rows = cur.fetchall()
    return {int(r["guild_id"]): dict(r) for r in rows}


def _db_upsert_guild_setting(guild_id: int, **kwargs) -> None:
    """Update one or more guild settings columns by name."""
    if not kwargs:
        return
    cols   = ", ".join(f"{k} = %s" for k in kwargs)
    values = list(kwargs.values()) + [guild_id]
    with _db() as con:
        with con.cursor() as cur:
            cur.execute(f"""
                INSERT INTO biki_guild_settings (guild_id)
                VALUES (%s)
                ON CONFLICT (guild_id) DO NOTHING
            """, (guild_id,))
            cur.execute(f"""
                UPDATE biki_guild_settings SET {cols} WHERE guild_id = %s
            """, values)


# ── Warnings ──────────────────────────────────────────────────────────────

def _db_add_warning(guild_id: int, user_id: int, warned_by: int, reason: str) -> int:
    with _db() as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO biki_warnings (guild_id, user_id, warned_by, reason) "
                "VALUES (%s, %s, %s, %s)",
                (guild_id, user_id, warned_by, reason),
            )
            cur.execute(
                "SELECT COUNT(*) FROM biki_warnings WHERE guild_id=%s AND user_id=%s",
                (guild_id, user_id),
            )
            count = cur.fetchone()[0]
    return int(count)


def _db_get_warnings(guild_id: int, user_id: int) -> list[dict]:
    with _db() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT reason, warned_by, created_at FROM biki_warnings
                WHERE guild_id=%s AND user_id=%s ORDER BY created_at DESC
            """, (guild_id, user_id))
            return [dict(r) for r in cur.fetchall()]


def _db_clear_warnings(guild_id: int, user_id: int) -> None:
    with _db() as con:
        with con.cursor() as cur:
            cur.execute(
                "DELETE FROM biki_warnings WHERE guild_id=%s AND user_id=%s",
                (guild_id, user_id),
            )


# ── Personality ───────────────────────────────────────────────────────────

def _db_set_personality(guild_id: int, text: str) -> None:
    with _db() as con:
        with con.cursor() as cur:
            cur.execute("""
                INSERT INTO biki_personality (guild_id, personality_text)
                VALUES (%s, %s)
                ON CONFLICT (guild_id) DO UPDATE SET personality_text = EXCLUDED.personality_text
            """, (guild_id, text))


def _db_clear_personality(guild_id: int) -> None:
    with _db() as con:
        with con.cursor() as cur:
            cur.execute("DELETE FROM biki_personality WHERE guild_id=%s", (guild_id,))


def _db_load_all_personalities() -> dict[int, str]:
    with _db() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT guild_id, personality_text FROM biki_personality")
            rows = cur.fetchall()
    return {int(r["guild_id"]): r["personality_text"] for r in rows}


# ── Server facts ──────────────────────────────────────────────────────────

def _db_add_fact(guild_id: int, fact_text: str) -> int:
    with _db() as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO biki_server_facts (guild_id, fact_text) VALUES (%s,%s) RETURNING id",
                (guild_id, fact_text),
            )
            return cur.fetchone()[0]


def _db_delete_fact(fact_id: int, guild_id: int) -> bool:
    with _db() as con:
        with con.cursor() as cur:
            cur.execute(
                "DELETE FROM biki_server_facts WHERE id=%s AND guild_id=%s",
                (fact_id, guild_id),
            )
            return cur.rowcount > 0


def _db_clear_all_facts(guild_id: int) -> int:
    with _db() as con:
        with con.cursor() as cur:
            cur.execute("DELETE FROM biki_server_facts WHERE guild_id=%s", (guild_id,))
            return cur.rowcount


def _db_load_all_facts() -> dict[int, list[dict]]:
    with _db() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, guild_id, fact_text FROM biki_server_facts ORDER BY guild_id, id")
            rows = cur.fetchall()
    result: dict[int, list[dict]] = {}
    for r in rows:
        result.setdefault(int(r["guild_id"]), []).append({"id": r["id"], "fact_text": r["fact_text"]})
    return result


# ── Mood ──────────────────────────────────────────────────────────────────

def _db_set_mood(guild_id: int, mood_key: str) -> None:
    with _db() as con:
        with con.cursor() as cur:
            cur.execute("""
                INSERT INTO biki_guild_mood (guild_id, mood_key)
                VALUES (%s, %s)
                ON CONFLICT (guild_id) DO UPDATE SET mood_key = EXCLUDED.mood_key
            """, (guild_id, mood_key))


def _db_load_all_moods() -> dict[int, str]:
    with _db() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT guild_id, mood_key FROM biki_guild_mood")
            rows = cur.fetchall()
    return {int(r["guild_id"]): r["mood_key"] for r in rows}


# ── User memory ───────────────────────────────────────────────────────────

def _db_upsert_user_memory(
    guild_id: int, user_id: int, display_name: str, username: str, bump_count: bool = True
) -> None:
    with _db() as con:
        with con.cursor() as cur:
            cur.execute("""
                INSERT INTO biki_user_memory (guild_id, user_id, display_name, username, message_count, last_seen)
                VALUES (%s, %s, %s, %s, %s, NOW())
                ON CONFLICT (guild_id, user_id) DO UPDATE
                    SET display_name   = EXCLUDED.display_name,
                        username       = EXCLUDED.username,
                        message_count  = CASE WHEN %s
                                              THEN biki_user_memory.message_count + 1
                                              ELSE biki_user_memory.message_count END,
                        last_seen      = NOW()
            """, (guild_id, user_id, display_name, username, 1, bump_count))


def _db_add_user_note(guild_id: int, user_id: int, note: str) -> None:
    with _db() as con:
        with con.cursor() as cur:
            cur.execute("""
                UPDATE biki_user_memory SET notes = array_append(notes, %s::TEXT)
                WHERE guild_id=%s AND user_id=%s
            """, (note[:300], guild_id, user_id))
            cur.execute("""
                UPDATE biki_user_memory
                SET notes = notes[array_length(notes,1)-19:array_length(notes,1)]
                WHERE guild_id=%s AND user_id=%s AND array_length(notes, 1) > 20
            """, (guild_id, user_id))


def _db_get_user_memory(guild_id: int, user_id: int) -> dict | None:
    with _db() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT display_name, username, notes, message_count, last_seen
                FROM biki_user_memory WHERE guild_id=%s AND user_id=%s
            """, (guild_id, user_id))
            row = cur.fetchone()
    return dict(row) if row else None


def _db_load_all_user_memory(guild_id: int) -> dict[int, dict]:
    with _db() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT user_id, display_name, username, notes, message_count, last_seen
                FROM biki_user_memory WHERE guild_id=%s
            """, (guild_id,))
            rows = cur.fetchall()
    return {int(r["user_id"]): dict(r) for r in rows}


# ── Conversations ─────────────────────────────────────────────────────────

_CONV_KEEP = 40


def _db_save_conv_message(guild_id: int, user_id: int, role: str, content: str) -> None:
    with _db() as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO biki_conversations (guild_id, user_id, role, content) VALUES (%s,%s,%s,%s)",
                (guild_id, user_id, role, content[:600]),
            )
            cur.execute("""
                DELETE FROM biki_conversations
                WHERE guild_id=%s AND user_id=%s
                  AND id NOT IN (
                      SELECT id FROM biki_conversations
                      WHERE guild_id=%s AND user_id=%s
                      ORDER BY id DESC LIMIT %s
                  )
            """, (guild_id, user_id, guild_id, user_id, _CONV_KEEP))


def _db_load_user_conv(guild_id: int, user_id: int, limit: int = _CONV_KEEP) -> list[dict]:
    with _db() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT role, content FROM biki_conversations
                WHERE guild_id=%s AND user_id=%s
                ORDER BY id DESC LIMIT %s
            """, (guild_id, user_id, limit))
            rows = cur.fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


# ── Server knowledge ──────────────────────────────────────────────────────

def _db_store_knowledge(guild_id: int, subject: str, fact: str) -> None:
    with _db() as con:
        with con.cursor() as cur:
            cur.execute("""
                INSERT INTO biki_server_knowledge (guild_id, subject, fact)
                SELECT %s, %s, %s
                WHERE NOT EXISTS (
                    SELECT 1 FROM biki_server_knowledge
                    WHERE guild_id=%s AND subject=%s AND fact=%s
                )
            """, (guild_id, subject, fact, guild_id, subject, fact))


def _db_get_knowledge_about(guild_id: int, subject: str, limit: int = 10) -> list[str]:
    with _db() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT fact FROM biki_server_knowledge
                WHERE guild_id=%s AND LOWER(subject)=LOWER(%s)
                ORDER BY id DESC LIMIT %s
            """, (guild_id, subject, limit))
            rows = cur.fetchall()
    return [r["fact"] for r in rows]


# ── Lore ──────────────────────────────────────────────────────────────────

def _db_set_lore(guild_id: int, lore_text: str) -> None:
    with _db() as con:
        with con.cursor() as cur:
            cur.execute("""
                INSERT INTO biki_lore (guild_id, lore_text) VALUES (%s, %s)
                ON CONFLICT (guild_id) DO UPDATE SET lore_text = EXCLUDED.lore_text
            """, (guild_id, lore_text))


def _db_get_lore(guild_id: int) -> str:
    with _db() as con:
        with con.cursor() as cur:
            cur.execute("SELECT lore_text FROM biki_lore WHERE guild_id=%s", (guild_id,))
            row = cur.fetchone()
    return row[0] if row else ""


def _db_clear_lore(guild_id: int) -> None:
    with _db() as con:
        with con.cursor() as cur:
            cur.execute("DELETE FROM biki_lore WHERE guild_id=%s", (guild_id,))


def _db_load_all_lore() -> dict[int, str]:
    with _db() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT guild_id, lore_text FROM biki_lore")
            rows = cur.fetchall()
    return {int(r["guild_id"]): r["lore_text"] for r in rows}


# ── Emoji bank ────────────────────────────────────────────────────────────

def _db_add_emoji(guild_id: int, emoji: str, situation: str) -> int:
    with _db() as con:
        with con.cursor() as cur:
            cur.execute(
                "INSERT INTO biki_emoji_bank (guild_id, emoji, situation) VALUES (%s,%s,%s) RETURNING id",
                (guild_id, emoji, situation),
            )
            return cur.fetchone()[0]


def _db_get_emojis(guild_id: int) -> list[dict]:
    with _db() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, emoji, situation FROM biki_emoji_bank WHERE guild_id=%s ORDER BY id",
                (guild_id,),
            )
            rows = cur.fetchall()
    return [dict(r) for r in rows]


def _db_delete_emoji(emoji_id: int, guild_id: int) -> bool:
    with _db() as con:
        with con.cursor() as cur:
            cur.execute(
                "DELETE FROM biki_emoji_bank WHERE id=%s AND guild_id=%s",
                (emoji_id, guild_id),
            )
            return cur.rowcount > 0


def _db_clear_emojis(guild_id: int) -> int:
    with _db() as con:
        with con.cursor() as cur:
            cur.execute("DELETE FROM biki_emoji_bank WHERE guild_id=%s", (guild_id,))
            return cur.rowcount


def _db_load_all_emojis() -> dict[int, list[dict]]:
    with _db() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT id, guild_id, emoji, situation FROM biki_emoji_bank ORDER BY guild_id, id")
            rows = cur.fetchall()
    result: dict[int, list[dict]] = {}
    for r in rows:
        gid = int(r["guild_id"])
        result.setdefault(gid, []).append({"id": r["id"], "emoji": r["emoji"], "situation": r["situation"]})
    return result


# ── Affinity (NEW) ────────────────────────────────────────────────────────

def _db_adjust_affinity(guild_id: int, user_id: int, delta: int) -> int:
    """Adjust affinity score by delta, return new score. Clamped to [-100, 100]."""
    with _db() as con:
        with con.cursor() as cur:
            cur.execute("""
                INSERT INTO biki_affinity (guild_id, user_id, score, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (guild_id, user_id) DO UPDATE
                    SET score = GREATEST(-100, LEAST(100, biki_affinity.score + %s)),
                        updated_at = NOW()
                RETURNING score
            """, (guild_id, user_id, max(-100, min(100, delta)), delta))
            return cur.fetchone()[0]


def _db_get_affinity(guild_id: int, user_id: int) -> int:
    with _db() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT score FROM biki_affinity WHERE guild_id=%s AND user_id=%s",
                (guild_id, user_id),
            )
            row = cur.fetchone()
    return row[0] if row else 0


def _db_top_affinity(guild_id: int, limit: int = 5) -> list[dict]:
    with _db() as con:
        with con.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT user_id, score FROM biki_affinity
                WHERE guild_id=%s ORDER BY score DESC LIMIT %s
            """, (guild_id, limit))
            rows = cur.fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Daily token budget
# ---------------------------------------------------------------------------

_DAILY_TOKEN_CAP = 2_500_000
_TOKEN_FILE      = _pathlib_mod.Path(__file__).parent.parent / "token_usage.json"
_token_lock      = _threading_mod.Lock()


class DailyTokenLimitReached(Exception):
    pass


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


def _atomic_reserve_tokens(estimated: int) -> None:
    """
    BUG FIX: Atomically check + reserve tokens BEFORE the API call.
    Raises DailyTokenLimitReached if over cap.
    Call _release_reserved_tokens() on error to refund.
    """
    with _token_lock:
        _maybe_reset_day()
        cap = _token_state.get("cap", _DAILY_TOKEN_CAP)
        if _token_state["total"] >= cap:
            raise DailyTokenLimitReached(f"Daily cap of {cap:,} reached")
        _token_state["total"] += estimated


def _settle_token_reservation(estimated: int, actual: int) -> None:
    """After the API call, adjust the reservation to the real token count."""
    with _token_lock:
        _token_state["total"] = max(0, _token_state["total"] - estimated + actual)
        _persist_token_state()


def _is_over_daily_limit() -> bool:
    with _token_lock:
        _maybe_reset_day()
        return _token_state["total"] >= _token_state.get("cap", _DAILY_TOKEN_CAP)


# ---------------------------------------------------------------------------
# DeepInfra async client
# ---------------------------------------------------------------------------

_deepinfra_client = None
_DEEPINFRA_BASE_URL = "https://api.deepinfra.com/v1/openai"
_DEEPINFRA_MODEL    = "meta-llama/Meta-Llama-3.1-8B-Instruct"
_MAX_SYSTEM_CHARS   = 8000  # guard against absurdly long system prompts


def _get_deepinfra_client():
    global _deepinfra_client
    if _deepinfra_client is None:
        from openai import AsyncOpenAI
        _deepinfra_client = AsyncOpenAI(
            api_key=config.DEEPINFRA_TOKEN,
            base_url=_DEEPINFRA_BASE_URL,
        )
    return _deepinfra_client


async def _call_ai(
    messages: list[dict],
    mood_addon: str = "",
    learning_context: str = "",
    max_tokens: int = 300,   # BUG FIX: actually used now
    personality_override: str = "",
    server_facts: list[dict] | None = None,
    server_lore: str = "",
    emoji_bank: list[dict] | None = None,
) -> str:
    personality_section = (
        f"\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"CUSTOM PERSONALITY FOR THIS SERVER\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{personality_override}"
        if personality_override else ""
    )
    facts_section = ""
    if server_facts:
        facts_lines = "\n".join(f"- {f['fact_text']}" for f in server_facts[:30])
        facts_section = (
            f"\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"THINGS BIKI KNOWS ABOUT THIS SERVER\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{facts_lines}"
        )
    lore_section = ""
    if server_lore and server_lore.strip():
        lore_section = (
            f"\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"SERVER LORE — ABSOLUTE TRUTH\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{server_lore.strip()[:1500]}\n"
        )
    emoji_section = ""
    if emoji_bank:
        lines = [f"  {e['emoji']} → use when: {e['situation']}" for e in emoji_bank[:20]]
        emoji_section = (
            "\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "SERVER CUSTOM EMOJIS\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            + "\n".join(lines)
        )

    # BUG FIX: guard total system prompt length
    system = (
        learning_context[:2000]
        + _SYSTEM_PROMPT
        + personality_section
        + facts_section
        + lore_section
        + emoji_section
        + mood_addon
    )
    if len(system) > _MAX_SYSTEM_CHARS:
        system = system[:_MAX_SYSTEM_CHARS]

    # BUG FIX: atomic pre-reserve tokens before the API call
    estimated = max_tokens + 200  # rough prompt overhead estimate
    _atomic_reserve_tokens(estimated)

    client = _get_deepinfra_client()
    try:
        response = await client.chat.completions.create(
            model=_DEEPINFRA_MODEL,
            messages=[{"role": "system", "content": system}] + messages[-15:],
            max_tokens=max_tokens,   # BUG FIX: was hardcoded to 400
            temperature=1.05,
            frequency_penalty=0.85,
            presence_penalty=0.6,
        )
        actual = response.usage.total_tokens if response.usage else estimated
        _settle_token_reservation(estimated, actual)
        log.debug("ai_companion: tokens used=%d", actual)
        raw = response.choices[0].message.content.strip()
        return _humanise(_sanitise(raw))
    except DailyTokenLimitReached:
        raise
    except Exception as e:
        # Refund reservation on error
        _settle_token_reservation(estimated, 0)
        log.warning("ai_companion: DeepInfra call failed: %s", e)
        raise RuntimeError(f"DeepInfra backend failed: {e}") from e


# ---------------------------------------------------------------------------
# Typing simulation
# ---------------------------------------------------------------------------

_CHARS_PER_SECOND = 5.5
_MIN_TYPING       = 0.4
_MAX_TYPING       = 6.5


def _typing_seconds(text: str) -> float:
    n    = len(text)
    base = n / _CHARS_PER_SECOND
    jitter = random.uniform(-0.12, 0.18) * base + random.uniform(-0.1, 0.3)
    return max(_MIN_TYPING, min(_MAX_TYPING, base + jitter))


def _split_parts(text: str) -> list[str]:
    parts = [p.strip() for p in text.split("[SPLIT]") if p.strip()]
    return parts[:3] if parts else [text.strip()]


# ---------------------------------------------------------------------------
# Moderation helpers
# ---------------------------------------------------------------------------

def _parse_mute_duration(text: str) -> float:
    m = _DURATION_RE.search(text)
    if not m:
        return 10 * 60.0
    amount = int(m.group(1))
    unit   = m.group(2).lower()
    if unit.startswith(("hour", "hr")):
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
    emoji_count = sum(1 for m in recent_msgs
                      if _UNICODE_EMOJI_RE.search(m) or _CUSTOM_EMOJI_RE.search(m))
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
    phrases = vocab.get("common_phrases", [])[-20:]
    slang   = vocab.get("slang", [])[-20:]
    emojis  = vocab.get("emojis", [])[-10:]
    energy  = vocab.get("energy", "mixed")
    samples = vocab.get("sample_messages", [])[-8:]
    if not any([phrases, slang, emojis, samples]):
        return ""
    sample_block = "\n".join(f'  "{s}"' for s in samples) if samples else ""
    return (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "THIS SERVER'S REAL COMMUNICATION STYLE — COPY IT EXACTLY\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Energy vibe: {energy}\n"
        f"Their slang: {', '.join(slang)}\n"
        f"Their phrases: {', '.join(phrases)}\n"
        f"Their emojis: {' '.join(emojis)}\n"
        + (f"Real messages:\n{sample_block}\n" if sample_block else "")
        + "You ARE one of them. Match their style exactly.\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    )


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class AiCompanion(commands.Cog):
    """Biki — chaotic AI companion that responds when mentioned or replied to."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

        # BUG FIX: conversations keyed by (guild_id, user_id) — no cross-guild pollution
        self.conversations: dict[tuple[int, int], list[dict]] = {}

        # BUG FIX: dismissed state also keyed by (guild_id, user_id)
        self.dismissed: dict[tuple[int, int], dict] = {}

        self.allowed_channels:   dict[int, list[int]] = {}
        self.guild_moods:        dict[int, str]       = {}
        self.server_vocab:       dict[int, dict]      = {}
        self._users_spoken:      set[int]             = set()

        # BUG FIX: per-user asyncio.Lock (not a global set blocking everyone)
        self._user_locks: dict[int, asyncio.Lock] = {}
        # BUG FIX: pending stores the latest message per user (same as before, but drain ALL)
        self._pending:    dict[int, discord.Message] = {}

        self._learning_inject_counter: dict[int, int] = {}
        self._learning_ctx_cache:      dict[int, str] = {}
        self._user_cooldowns:          dict[int, float] = {}
        self.guild_personalities:      dict[int, str]  = {}
        self.guild_silenced:           dict[int, bool]  = {}
        self.guild_facts:              dict[int, list[dict]] = {}
        self.channel_history:          dict[int, deque] = {}
        self.guild_chime_rate:         dict[int, float] = {}
        self.guild_cooldown:           dict[int, float] = {}
        self.user_memory:              dict[int, dict[int, dict]] = {}
        self.server_knowledge:         dict[int, dict[str, list[str]]] = {}
        self.guild_lore:               dict[int, str]  = {}
        self.guild_emojis:             dict[int, list[dict]] = {}
        self.guild_auto_mood:          dict[int, bool]  = {}

        # BUG FIX: member lookup cache — {guild_id: {lower_name: member}}
        self._member_cache:     dict[int, dict[str, discord.Member]] = {}
        self._member_cache_ts:  dict[int, float] = {}
        _MEMBER_CACHE_TTL = 120.0  # rebuild every 2 minutes
        self._MEMBER_CACHE_TTL = _MEMBER_CACHE_TTL

        # Proactive rate limit: channel_id → last proactive timestamp
        self._last_proactive: dict[int, float] = {}

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        try:
            await asyncio.to_thread(_db_init)
            self.allowed_channels    = await asyncio.to_thread(_db_load_all)
            self.guild_personalities = await asyncio.to_thread(_db_load_all_personalities)
            self.guild_facts         = await asyncio.to_thread(_db_load_all_facts)
            self.guild_moods         = await asyncio.to_thread(_db_load_all_moods)
            self.guild_lore          = await asyncio.to_thread(_db_load_all_lore)
            self.guild_emojis        = await asyncio.to_thread(_db_load_all_emojis)
            # BUG FIX: load persisted guild settings
            settings_map = await asyncio.to_thread(_db_load_all_guild_settings)
            for gid, s in settings_map.items():
                self.guild_silenced[gid]   = s.get("silenced", False)
                self.guild_chime_rate[gid] = s.get("chime_rate", 0.06)
                self.guild_cooldown[gid]   = s.get("cooldown_secs", 5.0)
                self.guild_auto_mood[gid]  = s.get("auto_mood", False)
            # BUG FIX: fix moods loaded from DB that aren't in VALID_MOODS
            for gid, mood in list(self.guild_moods.items()):
                if mood not in VALID_MOODS:
                    self.guild_moods[gid] = "chaotic"
            log.info(
                "ai_companion: loaded channels=%d personalities=%d facts=%d moods=%d "
                "lore=%d emojis=%d settings=%d",
                len(self.allowed_channels), len(self.guild_personalities),
                len(self.guild_facts), len(self.guild_moods),
                len(self.guild_lore), len(self.guild_emojis), len(settings_map),
            )
        except Exception as exc:
            log.error("ai_companion: DB init/load failed: %s", exc)

    # BUG FIX: interaction_check REMOVED — was making ALL commands owner-only.
    # Each command uses @app_commands.default_permissions() correctly instead.

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_user_lock(self, user_id: int) -> asyncio.Lock:
        """Return (or create) the per-user asyncio.Lock."""
        if user_id not in self._user_locks:
            self._user_locks[user_id] = asyncio.Lock()
        return self._user_locks[user_id]

    def _append_history(self, guild_id: int, user_id: int, role: str, content: str) -> None:
        """BUG FIX: history keyed by (guild_id, user_id) not just user_id."""
        key = (guild_id, user_id)
        history = self.conversations.setdefault(key, [])
        history.append({"role": role, "content": content})
        if len(history) > _CONV_KEEP:
            self.conversations[key] = history[-_CONV_KEEP:]
        asyncio.create_task(
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
            return _MOOD_ADDONS["chaotic"]
        mood_key = self.guild_moods.get(guild_id, "chaotic")
        # BUG FIX: "normal" is now a valid mood; unknown moods fall back to "chaotic"
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

    def _reaction_pool_for_mood(self, guild_id: Optional[int]) -> list[str]:
        mood = self.guild_moods.get(guild_id, "chaotic") if guild_id else "chaotic"
        if mood == "sad":
            return _SAD_REACTION_POOL
        if mood == "cold":
            return _COLD_REACTION_POOL
        return _REACTION_POOL

    def _get_member_cache(self, guild: discord.Guild) -> dict[str, discord.Member]:
        """BUG FIX: cached member name→member map; rebuilt at most every 2 minutes."""
        now = time.monotonic()
        if (
            guild.id not in self._member_cache
            or now - self._member_cache_ts.get(guild.id, 0) > self._MEMBER_CACHE_TTL
        ):
            cache: dict[str, discord.Member] = {}
            for m in guild.members:
                dn = (m.display_name or m.name)
                cache[dn.lower()] = m
                cache[dn.split()[0].lower()] = m
                cache[m.name.lower()] = m
                cache[m.name.split()[0].lower()] = m
            self._member_cache[guild.id] = cache
            self._member_cache_ts[guild.id] = now
        return self._member_cache[guild.id]

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
        key = (guild_id, user_id)

        # Load from DB if no in-memory history (first reply after restart)
        if key not in self.conversations:
            try:
                past = await asyncio.to_thread(_db_load_user_conv, guild_id, user_id)
                if past:
                    self.conversations[key] = past
            except Exception as exc:
                log.warning("_ai_reply: failed to load conv from DB: %s", exc)

        history = list(self.conversations.get(key, []))

        # Inject user memory
        if guild_id:
            profile = self.user_memory.get(guild_id, {}).get(user_id)
            if profile:
                name  = profile.get("display_name") or profile.get("username") or "them"
                count = profile.get("message_count", 1)
                notes = profile.get("notes") or []
                mem_lines = [f"You're talking to {name}. They've pinged you {count} time(s) before."]
                if notes:
                    mem_lines.append("What you remember: " + " | ".join(notes[-8:]))
                extra_note = (extra_note + "\n" if extra_note else "") + " ".join(mem_lines)

            # Inject passive server knowledge about this person
            profile2 = (self.user_memory.get(guild_id, {}) or {}).get(user_id, {})
            pname = profile2.get("display_name") or profile2.get("username") if profile2 else None
            if pname:
                _kb = self.server_knowledge.get(guild_id, {})
                first = pname.split()[0]
                kb_facts = _kb.get(first, _kb.get(pname, []))
                if kb_facts:
                    extra_note = (extra_note + "\n" if extra_note else "") + (
                        "Things you picked up about this person: " + " | ".join(kb_facts[-6:])
                    )

        input_content = user_text
        if channel_context:
            input_content = (
                f"[RECENT CHANNEL CONTEXT:\n{channel_context}]\n{input_content}"
            )
        if extra_note:
            input_content = f"[CONTEXT FOR BIKI ONLY: {extra_note}]\n{input_content}"
        history.append({"role": "user", "content": input_content})

        personality = self.guild_personalities.get(guild_id, "") if guild_id else ""
        facts  = self.guild_facts.get(guild_id, [])   if guild_id else []
        lore   = self.guild_lore.get(guild_id, "")    if guild_id else ""
        emojis = self.guild_emojis.get(guild_id)      if guild_id else None

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
        self, seconds: float, channel_id: int, guild_id: int, dismissed_by: int
    ) -> None:
        await asyncio.sleep(seconds)
        key = (guild_id, dismissed_by)
        state = self.dismissed.get(key)
        if state is None or state.get("channel_id") != channel_id:
            return
        self.dismissed.pop(key, None)
        channel = self.bot.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        note = f"The timer expired. You're back. Make it unhinged and ping <@{dismissed_by}>."
        try:
            reply = await _call_ai([{"role": "user", "content": note}], max_tokens=150)
            parts = _split_parts(reply)
            for i, part in enumerate(parts):
                await asyncio.sleep(1.0)
                async with channel.typing():
                    await asyncio.sleep(_typing_seconds(part))
                await channel.send(part)
                if i < len(parts) - 1:
                    await asyncio.sleep(random.uniform(0.8, 1.8))
        except Exception as exc:
            log.error("ai_companion: timed return failed: %s", exc)

    # ------------------------------------------------------------------
    # Reply sending (BUG FIX: force_reply implemented; reading pause uses clean content)
    # ------------------------------------------------------------------

    async def _send_biki_reply(
        self,
        trigger: discord.Message,
        text: str,
        *,
        force_reply: bool = False,
    ) -> None:
        # BUG FIX: check bot has permission to send in this channel
        if isinstance(trigger.channel, discord.TextChannel):
            perms = trigger.channel.permissions_for(trigger.guild.me)
            if not perms.send_messages:
                return

        react_pool = self._reaction_pool_for_mood(
            trigger.guild.id if trigger.guild else None
        )
        if random.random() < 0.20:
            try:
                await trigger.add_reaction(random.choice(react_pool))
            except discord.HTTPException:
                pass

        parts = _split_parts(text)

        # BUG FIX: reading pause uses clean content (mentions stripped), not raw
        clean_incoming = _MENTION_RE.sub("", trigger.content).strip()
        incoming_len   = len(clean_incoming)
        _read_pause    = 0.2 + min(1.8, incoming_len / 180) + random.uniform(-0.1, 0.3)
        await asyncio.sleep(_read_pause)

        for i, part in enumerate(parts):
            typing_duration = _typing_seconds(part)
            async with trigger.channel.typing():
                await asyncio.sleep(typing_duration)

            if i == 0:
                # BUG FIX: force_reply is now actually used
                use_reply = force_reply or random.random() < 0.55
                if use_reply:
                    try:
                        await trigger.reply(part, mention_author=False)
                    except discord.HTTPException:
                        await trigger.channel.send(part)
                else:
                    await trigger.channel.send(part)
            else:
                try:
                    await trigger.channel.send(part)
                except discord.HTTPException:
                    pass

            if i < len(parts) - 1:
                await asyncio.sleep(random.uniform(0.4, 1.1) if i == 0 else random.uniform(0.8, 1.8))

    # ------------------------------------------------------------------
    # Proactive reply (BUG FIX: checks dismissal state + silenced + rate-limit)
    # ------------------------------------------------------------------

    async def _proactive_reply(self, message: discord.Message) -> None:
        if message.guild is None:
            return

        guild_id   = message.guild.id
        channel_id = message.channel.id

        # BUG FIX: check silenced state before firing
        if self.guild_silenced.get(guild_id):
            return

        # BUG FIX: check if Biki is dismissed (any user in this guild)
        if any(k[0] == guild_id for k in self.dismissed):
            return

        # Rate-limit: max one proactive per channel per 30s
        now = time.monotonic()
        if now - self._last_proactive.get(channel_id, 0) < 30.0:
            return
        self._last_proactive[channel_id] = now

        # BUG FIX: min word count raised to 4
        if len(message.content.split()) < 4:
            return

        prompt = (
            f'Someone in the server just said: "{message.content[:300]}"\n'
            "You were not mentioned but you want to jump in like a real Discord member would.\n"
            "React naturally — reaction, funny comment, roast, agree, disagree, question, "
            "off-topic, or just vibing. Short and punchy. Be yourself."
        )

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
                200,
                personality,
                facts or None,
                lore,
                emojis or None,
            )
            if response:
                await self._send_biki_reply(message, response, force_reply=False)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Auto-mood: shift mood based on server energy
    # ------------------------------------------------------------------

    def _maybe_auto_mood(self, guild_id: int) -> None:
        if not self.guild_auto_mood.get(guild_id):
            return
        vocab = self.server_vocab.get(guild_id, {})
        energy = vocab.get("energy", "mixed")
        energy_to_mood = {
            "hype":    "chaotic",
            "chaotic": "chaotic",
            "chill":   "cold",
            "mixed":   "happy",
        }
        new_mood = energy_to_mood.get(energy, "happy")
        if self.guild_moods.get(guild_id) != new_mood:
            self.guild_moods[guild_id] = new_mood
            asyncio.create_task(asyncio.to_thread(_db_set_mood, guild_id, new_mood))

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

        clean = _MENTION_RE.sub("", text).strip()
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
            self._maybe_auto_mood(guild_id)

        # User memory
        user_id = message.author.id
        guild_mem = self.user_memory.setdefault(guild_id, {})
        profile = guild_mem.setdefault(user_id, {
            "display_name": message.author.display_name,
            "username":     message.author.name,
            "notes":        [],
            "message_count": 0,
        })
        profile["display_name"]  = message.author.display_name
        profile["username"]      = message.author.name
        profile["message_count"] = profile.get("message_count", 0) + 1

        # BUG FIX: use asyncio.create_task instead of get_event_loop().call_soon()
        for match in _SELF_RE.findall(clean):
            fact = match.strip()
            if fact and fact not in profile.get("notes", []):
                if len(profile.setdefault("notes", [])) < 20:
                    profile["notes"].append(fact)
                    asyncio.create_task(
                        asyncio.to_thread(_db_add_user_note, guild_id, user_id, fact)
                    )

        asyncio.create_task(
            asyncio.to_thread(
                _db_upsert_user_memory,
                guild_id, user_id,
                message.author.display_name,
                message.author.name, True,
            )
        )

    # ------------------------------------------------------------------
    # Passive fact extraction (BUG FIX: uses cached member lookup)
    # ------------------------------------------------------------------

    def _passive_fact_extract(self, message: discord.Message) -> None:
        if not message.guild:
            return
        guild_id  = message.guild.id
        author_dn = message.author.display_name
        text      = message.content

        # BUG FIX: use cached member map instead of O(n) per message
        member_lower = {
            k: v.display_name or v.name
            for k, v in self._get_member_cache(message.guild).items()
        }

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
            asyncio.create_task(
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
            converted = re.sub(r"^i'?m\b", f"{author_dn} is",  verb_and_body, flags=re.IGNORECASE)
            converted = re.sub(r"^i am\b", f"{author_dn} is",  converted,     flags=re.IGNORECASE)
            converted = re.sub(r"^i was\b", f"{author_dn} was", converted,    flags=re.IGNORECASE)
            converted = re.sub(r"^i\b",     author_dn,          converted,    flags=re.IGNORECASE)
            if converted != verb_and_body and len(converted) > len(author_dn) + 5:
                key  = author_dn.split()[0]
                body = converted[len(key):].strip()
                _store(key, body)

        for match in _FACT_SUBJECT_RE.finditer(text_resolved):
            subject_raw = match.group(1).strip()
            fact_raw    = match.group(2).strip()
            if subject_raw.lower() in _NOT_A_NAME or len(fact_raw) < 3:
                continue
            canonical = member_lower.get(subject_raw.lower(), subject_raw)
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
        member_cache  = self._get_member_cache(message.guild)
        for name_lower, member in member_cache.items():
            if member.bot:
                continue
            if name_lower in content_lower:
                return member

        return None

    # ------------------------------------------------------------------
    # Moderation handler
    # ------------------------------------------------------------------

    async def _try_moderation(self, message: discord.Message, clean: str) -> bool:
        if message.guild is None:
            return False
        author = message.author
        if not isinstance(author, discord.Member):
            return False

        lower = clean.lower()
        guild_id = message.guild.id

        # DELETE
        if message.reference and any(kw in lower for kw in _DELETE_KEYWORDS):
            if not _has_mod_permission(author):
                await message.channel.send(
                    random.choice([
                        "bro you dont have the clearance for that 💀",
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
                await message.channel.send("ngl i can't delete that, no permissions 😭")
                return True
            note = (
                f"You just deleted a message because {author.display_name} asked. "
                "Confirm it dramatically. You have the power."
            )
            reply = await self._ai_reply(guild_id, author.id, clean, extra_note=note, max_tokens=150)
            await self._send_biki_reply(message, reply)
            return True

        action: Optional[str] = None
        if _RE_UNMUTE.search(lower): action = "unmute"
        elif _RE_MUTE.search(lower): action = "mute"
        elif _RE_KICK.search(lower): action = "kick"
        elif _RE_BAN.search(lower):  action = "ban"
        elif _RE_WARN.search(lower): action = "warn"

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
            note = "Someone tried to make you take mod action against an admin or yourself. Refuse dramatically."
            reply = await self._ai_reply(guild_id, author.id, clean, extra_note=note, max_tokens=150)
            await self._send_biki_reply(message, reply)
            return True

        if action == "mute":
            duration  = _parse_mute_duration(clean)
            human_dur = _human_duration(duration)
            await message.channel.send(random.choice(["okay daddy 😈 give me a sec", "on it rn", "say less 💀"]))
            try:
                until = datetime.now(timezone.utc) + timedelta(seconds=duration)
                await target.timeout(until, reason=f"Muted by {author} via Biki")
            except discord.Forbidden:
                await message.channel.send("bro i literally dont have the power to do that rn, give me the mute members permission")
                return True
            note = f"You just muted {target.display_name} for {human_dur} because {author.display_name} asked. Confirm chaotically."
            reply = await self._ai_reply(guild_id, author.id, clean, extra_note=note, max_tokens=150)
            await self._send_biki_reply(message, reply)

        elif action == "unmute":
            try:
                await target.edit(timed_out_until=None)
            except discord.Forbidden:
                await message.channel.send("no permissions for that smh 😭")
                return True
            note = f"You just unmuted {target.display_name} because {author.display_name} asked. Say something like 'fine fine, you're free'"
            reply = await self._ai_reply(guild_id, author.id, clean, extra_note=note, max_tokens=150)
            await self._send_biki_reply(message, reply)

        elif action == "kick":
            name = target.display_name
            try:
                await target.kick(reason=f"Kicked by {author} via Biki")
            except discord.Forbidden:
                await message.channel.send("no kick permissions smh 😭 give me kick members")
                return True
            note = f"You just kicked {name}. Say something like 'YEET 👋 {name} has left the building'"
            reply = await self._ai_reply(guild_id, author.id, clean, extra_note=note, max_tokens=150)
            await self._send_biki_reply(message, reply)

        elif action == "ban":
            name = target.display_name
            try:
                await target.ban(reason=f"Banned by {author} via Biki")
            except discord.Forbidden:
                await message.channel.send("no ban permissions smh 😭 give me ban members")
                return True
            note = f"You just banned {name}. Say something like 'damn okay. {name} is GONE gone. rip 💀'"
            reply = await self._ai_reply(guild_id, author.id, clean, extra_note=note, max_tokens=150)
            await self._send_biki_reply(message, reply)

        elif action == "warn":
            reason_raw = re.sub(r"<@!?\d+>", "", re.sub(_RE_WARN, "", clean)).strip()
            reason = reason_raw or "no reason given"
            events_cog = self.bot.get_cog("Events")
            if events_cog and hasattr(events_cog, "_apply_warn"):
                try:
                    await events_cog._apply_warn(target, reason)
                except Exception as exc:
                    log.error("ai_companion: _apply_warn failed: %s", exc)
            else:
                try:
                    await asyncio.to_thread(_db_add_warning, message.guild.id, target.id, author.id, reason)
                    await target.send(f"⚠️ you got a warning in **{message.guild.name}**\nreason: {reason}")
                except Exception:
                    pass
            note = f"You just warned {target.display_name} for: '{reason}'. Say something like 'consider yourself warned 👀'"
            reply = await self._ai_reply(guild_id, author.id, clean, extra_note=note, max_tokens=150)
            await self._send_biki_reply(message, reply)

        return True

    # ------------------------------------------------------------------
    # on_message — main listener
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # 1. Ignore bots and DMs
        if message.author.bot or message.guild is None:
            return

        guild_id = message.guild.id
        user_id  = message.author.id

        # 1b. Silence gate
        if self.guild_silenced.get(guild_id):
            return

        # 2. Channel gate
        allowed = self.allowed_channels.get(guild_id, [])
        if allowed and message.channel.id not in allowed:
            return

        # 3. Passive learning + fact extraction
        self._learn_from_message(message)
        self._passive_fact_extract(message)

        # 3b. Channel context memory — BUG FIX: exclude bot's own messages
        _ch_hist = self.channel_history.setdefault(message.channel.id, deque(maxlen=8))
        if not message.author.bot:
            _ch_hist.append(f"{message.author.display_name}: {message.content[:150]}")

        # 4. Detect trigger
        assert self.bot.user is not None
        bot_mentioned  = self.bot.user in message.mentions
        replied_to_bot = (
            message.reference is not None
            and isinstance(message.reference.resolved, discord.Message)
            and message.reference.resolved.author.id == self.bot.user.id
        )
        triggered = bot_mentioned or replied_to_bot

        # 5. Proactive jump-in + passive trigger-word reaction
        if not triggered:
            msg_lower = message.content.lower()
            if any(w in msg_lower for w in _TRIGGER_REACTION_WORDS):
                if random.random() < 0.05:
                    try:
                        await message.add_reaction(random.choice(_CHAOTIC_REACTION_POOL))
                    except discord.HTTPException:
                        pass
            chime_rate = self.guild_chime_rate.get(guild_id, 0.06)
            if random.random() < chime_rate:
                asyncio.create_task(self._proactive_reply(message))
            return

        # 6. Triggered handler
        channel_id = message.channel.id

        # Strip bot mentions
        clean = message.content
        clean = clean.replace(f"<@{self.bot.user.id}>", "")
        clean = clean.replace(f"<@!{self.bot.user.id}>", "").strip()

        # Handle empty reply-to-bot
        if not clean and replied_to_bot and isinstance(message.reference.resolved, discord.Message):
            clean = f"[replying to your message: \"{message.reference.resolved.content[:200]}\"]"

        # Per-user cooldown
        now = time.monotonic()
        last_reply = self._user_cooldowns.get(user_id, 0)
        cooldown_secs = self.guild_cooldown.get(guild_id, 5.0)
        if now - last_reply < cooldown_secs:
            return
        self._user_cooldowns[user_id] = now

        # BUG FIX: per-user lock (not global set blocking all users)
        lock = self._get_user_lock(user_id)
        if lock.locked():
            # User's previous reply is still being processed — queue latest message
            self._pending[user_id] = message
            return

        await self._handle_triggered_message(message, clean, guild_id, user_id, channel_id)

        # BUG FIX: after handling, drain ALL pending messages for this user
        while user_id in self._pending:
            next_msg   = self._pending.pop(user_id)
            next_clean = next_msg.content
            next_clean = next_clean.replace(f"<@{self.bot.user.id}>", "")
            next_clean = next_clean.replace(f"<@!{self.bot.user.id}>", "").strip()
            if not next_clean:
                break
            await self._handle_triggered_message(
                next_msg, next_clean,
                next_msg.guild.id, next_msg.author.id, next_msg.channel.id
            )

    async def _handle_triggered_message(
        self,
        message: discord.Message,
        clean: str,
        guild_id: int,
        user_id: int,
        channel_id: int,
    ) -> None:
        lock = self._get_user_lock(user_id)
        async with lock:
            try:
                # Moderation check
                if await self._try_moderation(message, clean):
                    return

                key = (guild_id, user_id)

                # Dismissal state check
                dismissed_state = self.dismissed.get(key)
                if dismissed_state is not None:
                    if self._is_return(clean):
                        self.dismissed.pop(key, None)
                        note = "The person who kicked you out is begging you to come back. Make your re-entry absolutely unhinged."
                        try:
                            reply = await self._ai_reply(guild_id, user_id, clean, extra_note=note, max_tokens=150)
                            await self._send_biki_reply(message, reply)
                            asyncio.create_task(
                                asyncio.to_thread(_db_adjust_affinity, guild_id, user_id, 2)
                            )
                        except Exception as exc:
                            log.error("ai_companion: return reply failed: %s", exc)
                    return

                # Spite return check
                all_dismissed_by = {k[1] for k, v in self.dismissed.items() if k[0] == guild_id}
                if (
                    all_dismissed_by
                    and self._is_return(clean)
                    and user_id not in all_dismissed_by
                ):
                    for k in list(self.dismissed.keys()):
                        if k[0] == guild_id:
                            self.dismissed.pop(k, None)
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
                    self.dismissed[key] = {"channel_id": channel_id, "dismissed_by": user_id}
                    note = f"This person is dismissing you for {int(timed_seconds)} seconds. Acknowledge it chaotically."
                    try:
                        reply = await self._ai_reply(guild_id, user_id, clean, extra_note=note, max_tokens=150)
                        await self._send_biki_reply(message, reply)
                        asyncio.create_task(
                            asyncio.to_thread(_db_adjust_affinity, guild_id, user_id, -3)
                        )
                    except Exception as exc:
                        log.error("ai_companion: timed dismiss failed: %s", exc)
                    asyncio.create_task(
                        self._timed_return(timed_seconds, channel_id, guild_id, user_id)
                    )
                    return

                # Plain dismissal
                if self._is_dismiss(clean):
                    self.dismissed[key] = {"channel_id": channel_id, "dismissed_by": user_id}
                    note = "This person is kicking you out. Most dramatic chaotic goodbye ever."
                    try:
                        reply = await self._ai_reply(guild_id, user_id, clean, extra_note=note, max_tokens=150)
                        await self._send_biki_reply(message, reply)
                        asyncio.create_task(
                            asyncio.to_thread(_db_adjust_affinity, guild_id, user_id, -5)
                        )
                    except Exception as exc:
                        log.error("ai_companion: dismiss failed: %s", exc)
                    return

                # Normal reply
                _ctx_deque = self.channel_history.get(channel_id)
                # BUG FIX: exclude Biki's own messages from context (already filtered in on_message)
                _ctx_lines = [
                    line for line in (list(_ctx_deque)[:-1] if _ctx_deque else [])
                    if not line.startswith(f"{message.author.display_name}:")
                ]
                _channel_ctx = "\n".join(_ctx_lines[-5:])

                _guild      = message.guild
                _mc         = _guild.member_count or "?"
                _ch_names   = ", ".join(c.name for c in _guild.text_channels[:6])
                server_ctx  = (
                    f"[SERVER: {_guild.name} — {_mc} members — channels: {_ch_names}]"
                )
                full_ctx = (server_ctx + "\n" + _channel_ctx).strip()

                try:
                    reply = await self._ai_reply(
                        guild_id, user_id, clean,
                        channel_context=full_ctx,
                        max_tokens=300,
                    )
                    await self._send_biki_reply(message, reply)
                    self._user_cooldowns[user_id] = time.monotonic()
                    # Positive affinity bump for normal interaction
                    asyncio.create_task(
                        asyncio.to_thread(_db_adjust_affinity, guild_id, user_id, 1)
                    )
                except DailyTokenLimitReached:
                    await message.channel.send(random.choice(_OVER_LIMIT_REPLIES))
                except RuntimeError:
                    await message.channel.send(random.choice(_OFFLINE_REPLIES))
                except Exception as exc:
                    log.error("ai_companion: reply failed: %s", exc)
                    try:
                        await message.channel.send(random.choice(_OFFLINE_REPLIES))
                    except discord.HTTPException:
                        pass
            except Exception as exc:
                log.error("ai_companion: _handle_triggered_message failed: %s", exc)

    # ==================================================================
    # SLASH COMMANDS
    # ==================================================================

    # ------------------------------------------------------------------
    # /bikistats
    # ------------------------------------------------------------------

    @app_commands.command(name="bikistats", description="Biki's current stats for this server.")
    @app_commands.guild_only()
    async def bikistats(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        gid = interaction.guild_id

        mood_key  = self.guild_moods.get(gid, "chaotic")
        mood_lbl  = MOOD_LABELS.get(mood_key, mood_key)
        silenced  = self.guild_silenced.get(gid, False)
        chime     = round(self.guild_chime_rate.get(gid, 0.06) * 100, 1)
        cooldown  = self.guild_cooldown.get(gid, 5.0)
        channels  = self.allowed_channels.get(gid, [])
        ch_list   = ", ".join(f"<#{c}>" for c in channels) if channels else "all channels"
        facts_n   = len(self.guild_facts.get(gid, []))
        vocab     = self.server_vocab.get(gid, {})
        energy    = vocab.get("energy", "unknown")
        samples   = len(vocab.get("sample_messages", []))
        users_n   = len(self.user_memory.get(gid, {}))
        auto_mood = self.guild_auto_mood.get(gid, False)

        # Top affinity
        try:
            top_aff = await asyncio.to_thread(_db_top_affinity, gid, 3)
            top_aff_lines = []
            for row in top_aff:
                member = interaction.guild.get_member(int(row["user_id"]))
                name   = member.display_name if member else f"<@{row['user_id']}>"
                top_aff_lines.append(f"  {name}: {row['score']:+d}")
            top_aff_str = "\n".join(top_aff_lines) if top_aff_lines else "  nobody yet"
        except Exception:
            top_aff_str = "  (unavailable)"

        body = (
            f"**Mood:** {mood_lbl}{' *(auto)*' if auto_mood else ''}\n"
            f"**Silenced:** {'yes 🔇' if silenced else 'no 🔊'}\n"
            f"**Chime-in rate:** {chime}%\n"
            f"**Reply cooldown:** {cooldown:.0f}s\n"
            f"**Active channels:** {ch_list}\n"
            f"**Server facts:** {facts_n}\n"
            f"**Server energy:** {energy}\n"
            f"**Messages learned from:** {samples}\n"
            f"**Members Biki knows:** {users_n}\n"
            f"**Biki's favourite people:**\n{top_aff_str}"
        )
        await interaction.response.send_message(f"📊 **Biki stats for {interaction.guild.name}**\n\n{body}", ephemeral=True)

    # ------------------------------------------------------------------
    # /bikiping
    # ------------------------------------------------------------------

    @app_commands.command(name="bikiping", description="Check if Biki is alive.")
    @app_commands.guild_only()
    async def bikiping(self, interaction: discord.Interaction) -> None:
        lat = round(self.bot.latency * 1000)
        used = _token_state.get("total", 0)
        cap  = _token_state.get("cap", _DAILY_TOKEN_CAP)
        pct  = round(used / cap * 100, 1)
        await interaction.response.send_message(
            f"yeah i'm here. latency {lat}ms. tokens today: {used:,}/{cap:,} ({pct}%)",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /bikitokens
    # ------------------------------------------------------------------

    @app_commands.command(name="bikitokens", description="Show today's token usage.")
    @app_commands.guild_only()
    async def bikitokens(self, interaction: discord.Interaction) -> None:
        used = _token_state.get("total", 0)
        cap  = _token_state.get("cap", _DAILY_TOKEN_CAP)
        pct  = round(used / cap * 100, 1)
        bar  = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        await interaction.response.send_message(
            f"**Daily token usage:**\n`[{bar}]` **{pct}%**\n"
            f"{used:,} / {cap:,} tokens used today",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /bikibudget — set daily token cap
    # ------------------------------------------------------------------

    @app_commands.command(name="bikibudget", description="Set Biki's daily token budget.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(millions="Budget in millions of tokens (e.g. 2.5 = 2,500,000)")
    async def bikibudget(self, interaction: discord.Interaction, millions: float) -> None:
        if interaction.user.id != config.OWNER_ID:
            await interaction.response.send_message("nah this isn't for you 💀", ephemeral=True)
            return
        new_cap = int(millions * 1_000_000)
        if new_cap < 100_000:
            await interaction.response.send_message("bro that's way too low, minimum 0.1M", ephemeral=True)
            return
        _set_token_cap(new_cap)
        await interaction.response.send_message(
            f"✅ Daily token budget set to **{new_cap:,}** ({millions}M)", ephemeral=True
        )

    # ------------------------------------------------------------------
    # /bikimood
    # ------------------------------------------------------------------

    @app_commands.command(name="bikimood", description="Change Biki's active mood for this server.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(mood="Pick a mood.")
    @app_commands.choices(mood=[
        app_commands.Choice(name="😄 Happy / Flirty",       value="happy"),
        app_commands.Choice(name="🥺 Sad / Soft",           value="sad"),
        app_commands.Choice(name="🌀 Chaotic / Unhinged",   value="chaotic"),
        app_commands.Choice(name="🧊 Cold / Sarcastic",     value="cold"),
        app_commands.Choice(name="😐 Normal / Default",     value="normal"),
    ])
    async def bikimood(self, interaction: discord.Interaction, mood: app_commands.Choice[str]) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        try:
            await asyncio.to_thread(_db_set_mood, interaction.guild_id, mood.value)
            self.guild_moods[interaction.guild_id] = mood.value
        except Exception as exc:
            await interaction.followup.send(f"❌ DB error: `{exc}`", ephemeral=True)
            return
        descriptions = {
            "happy":   "Full flirt mode. Everything's exciting, everyone's bestie.",
            "sad":     "In her feels. Quiet, soft, trails off. Don't make it worse.",
            "chaotic": "Zero coherence. Maximum chaos. She might respond to 'hi' with dinosaur facts.",
            "cold":    "Dry. Unbothered. Roasts in four words then moves on.",
            "normal":  "Default chaotic self. No special override.",
        }
        await interaction.followup.send(
            f"✅ Mood set to **{MOOD_LABELS[mood.value]}**\n_{descriptions[mood.value]}_",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /bikiautomood
    # ------------------------------------------------------------------

    @app_commands.command(name="bikiautomood", description="Toggle auto-mood — Biki shifts mood based on server energy.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def bikiautomood(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        gid      = interaction.guild_id
        current  = self.guild_auto_mood.get(gid, False)
        new_val  = not current
        self.guild_auto_mood[gid] = new_val
        asyncio.create_task(asyncio.to_thread(
            _db_upsert_guild_setting, gid, auto_mood=new_val
        ))
        state = "**ON** — Biki will automatically shift mood based on how chaotic the server gets" if new_val else "**OFF**"
        await interaction.response.send_message(f"🎭 Auto-mood is now {state}", ephemeral=True)

    # ------------------------------------------------------------------
    # /bikisilence
    # ------------------------------------------------------------------

    @app_commands.command(name="bikisilence", description="Toggle Biki's silence mode.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def bikisilence(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        gid      = interaction.guild_id
        new_state = not self.guild_silenced.get(gid, False)
        self.guild_silenced[gid] = new_state
        # BUG FIX: persist to DB
        asyncio.create_task(asyncio.to_thread(
            _db_upsert_guild_setting, gid, silenced=new_state
        ))
        if new_state:
            await interaction.response.send_message(
                "🔇 Biki is now **silenced**. Run `/bikisilence` again to bring her back.",
                ephemeral=False,
            )
        else:
            await interaction.response.send_message("🔊 Biki is **back**. good luck with that.", ephemeral=False)

    # ------------------------------------------------------------------
    # /bikirate
    # ------------------------------------------------------------------

    @app_commands.command(name="bikirate", description="View or set how often Biki jumps into conversations (0–100%).")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(percent="Chime-in chance 0–100. Leave blank to view.")
    async def bikirate(self, interaction: discord.Interaction, percent: Optional[int] = None) -> None:
        assert interaction.guild_id is not None
        gid     = interaction.guild_id
        current = self.guild_chime_rate.get(gid, 0.06)
        current_pct = round(current * 100, 1)

        if percent is None:
            bar = "█" * int(current_pct / 5) + "░" * (20 - int(current_pct / 5))
            await interaction.response.send_message(
                f"**Biki's chime-in rate:** `[{bar}]` **{current_pct}%**\n"
                f"Use `/bikirate percent:<0–100>` to change it.",
                ephemeral=True,
            )
            return

        if not 0 <= percent <= 100:
            await interaction.response.send_message("percent must be 0–100.", ephemeral=True)
            return

        new_rate = percent / 100.0
        self.guild_chime_rate[gid] = new_rate
        # BUG FIX: persist to DB
        asyncio.create_task(asyncio.to_thread(
            _db_upsert_guild_setting, gid, chime_rate=new_rate
        ))
        bar = "█" * int(percent / 5) + "░" * (20 - int(percent / 5))
        await interaction.response.send_message(
            f"✅ Chime-in rate → **{percent}%**\n`[{bar}]`",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /bikicooldown
    # ------------------------------------------------------------------

    @app_commands.command(name="bikicooldown", description="View or set per-user reply cooldown.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(seconds="Cooldown in seconds (0–300). Leave blank to view.")
    async def bikicooldown(self, interaction: discord.Interaction, seconds: Optional[int] = None) -> None:
        assert interaction.guild_id is not None
        gid     = interaction.guild_id
        current = self.guild_cooldown.get(gid, 5.0)

        if seconds is None:
            await interaction.response.send_message(
                f"**Current per-user cooldown:** `{current:.0f}s`\n"
                f"Use `/bikicooldown seconds:<0–300>` to change it.",
                ephemeral=True,
            )
            return

        if not 0 <= seconds <= 300:
            await interaction.response.send_message("seconds must be 0–300.", ephemeral=True)
            return

        self.guild_cooldown[gid] = float(seconds)
        # BUG FIX: persist to DB
        asyncio.create_task(asyncio.to_thread(
            _db_upsert_guild_setting, gid, cooldown_secs=float(seconds)
        ))
        await interaction.response.send_message(
            f"✅ Per-user cooldown → **{seconds}s**", ephemeral=True
        )

    # ------------------------------------------------------------------
    # /bikiremember / /bikiforget / /bikiclearfacts / /bikifacts
    # ------------------------------------------------------------------

    @app_commands.command(name="bikiremember", description="Tell Biki something to always remember about this server.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def bikiremember(self, interaction: discord.Interaction, fact: str) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        try:
            fact_id = await asyncio.to_thread(_db_add_fact, interaction.guild_id, fact)
            self.guild_facts.setdefault(interaction.guild_id, []).append({"id": fact_id, "fact_text": fact})
        except Exception as exc:
            await interaction.followup.send(f"❌ Failed to save fact: `{exc}`", ephemeral=True)
            return
        total = len(self.guild_facts.get(interaction.guild_id, []))
        await interaction.followup.send(
            f"✅ Got it. Biki will remember: **{fact}**\n*(fact #{fact_id} — {total} total)*",
            ephemeral=True,
        )

    @app_commands.command(name="bikiforget", description="Delete a fact Biki knows about this server.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def bikiforget(self, interaction: discord.Interaction, fact_id: int) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        try:
            deleted = await asyncio.to_thread(_db_delete_fact, fact_id, interaction.guild_id)
        except Exception as exc:
            await interaction.followup.send(f"❌ DB error: `{exc}`", ephemeral=True)
            return
        if not deleted:
            await interaction.followup.send(f"❌ No fact `#{fact_id}` found.", ephemeral=True)
            return
        self.guild_facts[interaction.guild_id] = [
            f for f in self.guild_facts.get(interaction.guild_id, []) if f["id"] != fact_id
        ]
        await interaction.followup.send(f"✅ Fact `#{fact_id}` deleted.", ephemeral=True)

    @app_commands.command(name="bikiclearfacts", description="Clear ALL facts Biki knows about this server.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def bikiclearfacts(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        try:
            deleted = await asyncio.to_thread(_db_clear_all_facts, interaction.guild_id)
            self.guild_facts[interaction.guild_id] = []
        except Exception as exc:
            await interaction.followup.send(f"❌ DB error: `{exc}`", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Cleared **{deleted}** fact(s). Biki remembers nothing now." if deleted
            else "Biki had no facts to clear.", ephemeral=True,
        )

    @app_commands.command(name="bikifacts", description="List everything Biki currently remembers about this server.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def bikifacts(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        facts = self.guild_facts.get(interaction.guild_id, [])
        if not facts:
            await interaction.response.send_message(
                "Biki doesn't remember anything specific yet. Use `/bikiremember` to add facts.",
                ephemeral=True,
            )
            return
        lines = [f"`#{f['id']}` — {f['fact_text']}" for f in facts]
        body  = "\n".join(lines)
        if len(body) > 1900:
            body = body[:1900] + f"\n…({len(facts)} total)"
        await interaction.response.send_message(
            f"**Things Biki knows ({len(facts)} facts):**\n{body}\n\nUse `/bikiforget <id>` to remove one.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /bikilearning
    # ------------------------------------------------------------------

    @app_commands.command(name="bikilearning", description="Show Biki's learning progress for this server.")
    @app_commands.guild_only()
    async def bikilearning(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        vocab   = self.server_vocab.get(interaction.guild_id, {})
        energy  = vocab.get("energy", "unknown")
        samples = len(vocab.get("sample_messages", []))
        slang   = vocab.get("slang", [])[:15]
        phrases = vocab.get("common_phrases", [])[:10]
        emojis  = vocab.get("emojis", [])[:10]
        body = (
            f"**Server energy:** {energy}\n"
            f"**Messages observed:** {samples}\n"
            f"**Top slang:** {', '.join(slang) or 'none yet'}\n"
            f"**Top phrases:** {', '.join(phrases) or 'none yet'}\n"
            f"**Top emojis:** {' '.join(emojis) or 'none yet'}"
        )
        await interaction.response.send_message(f"📚 **Biki's learning log**\n\n{body}", ephemeral=True)

    # ------------------------------------------------------------------
    # /bikirecall — show what Biki remembers about a user
    # ------------------------------------------------------------------

    @app_commands.command(name="bikirecall", description="See what Biki remembers about a specific user.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(member="The user to look up.")
    async def bikirecall(self, interaction: discord.Interaction, member: discord.Member) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        gid = interaction.guild_id

        # In-memory profile
        profile = self.user_memory.get(gid, {}).get(member.id)
        if not profile:
            try:
                profile = await asyncio.to_thread(_db_get_user_memory, gid, member.id)
            except Exception:
                profile = None

        profile_lines: list[str] = []
        if profile:
            profile_lines.append(f"**Message count:** {profile.get('message_count', 0)}")
            last_seen = profile.get("last_seen")
            if last_seen:
                profile_lines.append(f"**Last seen:** {last_seen}")
            notes = profile.get("notes") or []
            if notes:
                profile_lines.append("**Self-referential notes:**")
                for n in notes[-10:]:
                    profile_lines.append(f"  • {n}")
        else:
            profile_lines.append("_(No memory profile yet)_")

        # Passive knowledge
        first_name = member.display_name.split()[0]
        kb = self.server_knowledge.get(gid, {})
        kb_facts = kb.get(first_name) or kb.get(member.display_name) or []
        if not kb_facts:
            try:
                kb_facts = await asyncio.to_thread(_db_get_knowledge_about, gid, first_name)
            except Exception:
                kb_facts = []

        knowledge_lines: list[str] = []
        if kb_facts:
            knowledge_lines.append("**Passively picked up:**")
            for f in kb_facts[-10:]:
                knowledge_lines.append(f"  • {f}")
        else:
            knowledge_lines.append("_(Nothing picked up yet)_")

        # Affinity
        try:
            score = await asyncio.to_thread(_db_get_affinity, gid, member.id)
            affinity_str = f"**Affinity score:** {score:+d}/100"
        except Exception:
            affinity_str = ""

        full = (
            f"🧠 **Biki's file on {member.display_name}**\n"
            + "\n".join(profile_lines)
            + "\n\n"
            + "\n".join(knowledge_lines)
            + (f"\n\n{affinity_str}" if affinity_str else "")
        )
        if len(full) > 1900:
            full = full[:1897] + "…"
        await interaction.followup.send(full, ephemeral=True)

    # ------------------------------------------------------------------
    # /bikiaffinity — Biki's relationship score with a user (NEW)
    # ------------------------------------------------------------------

    @app_commands.command(name="bikiaffinity", description="See how much Biki likes (or dislikes) someone.")
    @app_commands.guild_only()
    @app_commands.describe(member="Who to check.")
    async def bikiaffinity(self, interaction: discord.Interaction, member: discord.Member) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        try:
            score = await asyncio.to_thread(_db_get_affinity, interaction.guild_id, member.id)
        except Exception as exc:
            await interaction.followup.send(f"❌ DB error: `{exc}`", ephemeral=True)
            return

        if score >= 50:
            verdict = "obsessed with them fr 💕"
        elif score >= 20:
            verdict = "lowkey into them ngl"
        elif score >= 5:
            verdict = "they're fine i guess"
        elif score >= -5:
            verdict = "neutral. they exist."
        elif score >= -20:
            verdict = "slightly annoyed tbh"
        elif score >= -50:
            verdict = "not a fan. at all."
        else:
            verdict = "this person has been on thin ice 💀"

        bar_pos = min(20, max(0, int((score + 100) / 10)))
        bar = "░" * bar_pos + "█" + "░" * (20 - bar_pos)

        await interaction.followup.send(
            f"**Biki x {member.display_name}:** {verdict}\n"
            f"Score: **{score:+d}** `[{bar}]`",
            ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /bikisetpersonality / /bikiclearpersonality
    # ------------------------------------------------------------------

    @app_commands.command(name="bikisetpersonality", description="Give Biki a custom personality for this server.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def bikisetpersonality(self, interaction: discord.Interaction, personality: str) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        try:
            await asyncio.to_thread(_db_set_personality, interaction.guild_id, personality)
            self.guild_personalities[interaction.guild_id] = personality
        except Exception as exc:
            await interaction.followup.send(f"❌ DB error: `{exc}`", ephemeral=True)
            return
        preview = personality[:200] + ("..." if len(personality) > 200 else "")
        await interaction.followup.send(
            f"✅ Personality updated.\n**Preview:** {preview}", ephemeral=True
        )

    @app_commands.command(name="bikiclearpersonality", description="Remove the custom personality and revert to default.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def bikiclearpersonality(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        try:
            await asyncio.to_thread(_db_clear_personality, interaction.guild_id)
            self.guild_personalities.pop(interaction.guild_id, None)
        except Exception as exc:
            await interaction.followup.send(f"❌ DB error: `{exc}`", ephemeral=True)
            return
        await interaction.followup.send("✅ Custom personality cleared. Biki is back to her chaotic self.", ephemeral=True)

    # ------------------------------------------------------------------
    # /bikiemojis
    # ------------------------------------------------------------------

    @app_commands.command(name="bikiemojis", description="Manage the server emoji bank.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        action="What to do.",
        emoji="Emoji to add (required for 'add').",
        situation="When to use it (required for 'add').",
        emoji_id="ID of emoji to remove (required for 'remove').",
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
            entries = self.guild_emojis.get(gid, [])
            if not entries:
                await interaction.response.send_message("No emojis yet. Use `/bikiemojis add` to add some.", ephemeral=True)
                return
            lines = [f"`#{e['id']}` {e['emoji']} — *{e['situation']}*" for e in entries]
            body  = "\n".join(lines)
            if len(body) > 1900:
                body = body[:1900] + f"\n…({len(entries)} total)"
            await interaction.response.send_message(f"**Emoji bank ({len(entries)}):**\n{body}", ephemeral=True)
            return

        if action.value == "clear":
            await interaction.response.defer(ephemeral=True)
            deleted = await asyncio.to_thread(_db_clear_emojis, gid)
            self.guild_emojis.pop(gid, None)
            await interaction.followup.send(f"✅ Cleared **{deleted}** emoji(s).", ephemeral=True)
            return

        if action.value == "remove":
            if emoji_id is None:
                await interaction.response.send_message("❌ Provide `emoji_id`. Use `/bikiemojis list` to see IDs.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            deleted = await asyncio.to_thread(_db_delete_emoji, emoji_id, gid)
            if not deleted:
                await interaction.followup.send(f"❌ No emoji `#{emoji_id}` found.", ephemeral=True)
                return
            self.guild_emojis[gid] = [e for e in self.guild_emojis.get(gid, []) if e["id"] != emoji_id]
            await interaction.followup.send(f"✅ Emoji `#{emoji_id}` removed.", ephemeral=True)
            return

        # add
        if not emoji or not emoji.strip():
            await interaction.response.send_message("❌ Provide the `emoji` to add.", ephemeral=True)
            return
        if not situation or not situation.strip():
            await interaction.response.send_message("❌ Provide a `situation` description.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        new_id = await asyncio.to_thread(_db_add_emoji, gid, emoji.strip(), situation.strip())
        self.guild_emojis.setdefault(gid, []).append({"id": new_id, "emoji": emoji.strip(), "situation": situation.strip()})
        await interaction.followup.send(
            f"✅ Added `#{new_id}` {emoji.strip()} — *{situation.strip()}*\n"
            f"Biki will use it when appropriate.", ephemeral=True,
        )

    # ------------------------------------------------------------------
    # /bikilore
    # ------------------------------------------------------------------

    @app_commands.command(name="bikilore", description="Set server lore — absolute truths Biki will never contradict.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.choices(action=[
        app_commands.Choice(name="set",   value="set"),
        app_commands.Choice(name="view",  value="view"),
        app_commands.Choice(name="clear", value="clear"),
    ])
    async def bikilore(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        text: Optional[str] = None,
    ) -> None:
        assert interaction.guild_id is not None
        gid = interaction.guild_id

        if action.value == "view":
            lore = self.guild_lore.get(gid, "")
            if not lore:
                await interaction.response.send_message("No server lore set yet. Use `/bikilore set text:<...>`", ephemeral=True)
                return
            body = lore[:1900] + ("…" if len(lore) > 1900 else "")
            await interaction.response.send_message(f"**Server lore:**\n{body}", ephemeral=True)
            return

        if action.value == "clear":
            await interaction.response.defer(ephemeral=True)
            await asyncio.to_thread(_db_clear_lore, gid)
            self.guild_lore.pop(gid, None)
            await interaction.followup.send("✅ Server lore cleared.", ephemeral=True)
            return

        # set
        if not text or not text.strip():
            await interaction.response.send_message("❌ Provide `text` to set as lore.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await asyncio.to_thread(_db_set_lore, gid, text.strip())
        self.guild_lore[gid] = text.strip()
        await interaction.followup.send(f"✅ Server lore set ({len(text)} chars).", ephemeral=True)

    # ------------------------------------------------------------------
    # /aichannels / /aiset / /aiunset / /aireset
    # ------------------------------------------------------------------

    @app_commands.command(name="aichannels", description="List channels where Biki is active.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def aichannels(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        channels = self.allowed_channels.get(interaction.guild_id, [])
        if not channels:
            await interaction.response.send_message("Biki is active in **all channels** for this server.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"Biki is restricted to: {', '.join(f'<#{c}>' for c in channels)}",
            ephemeral=True,
        )

    @app_commands.command(name="aiset", description="Restrict Biki to a specific channel.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel="Channel to add.")
    async def aiset(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        new_list = await asyncio.to_thread(_db_add_channel, interaction.guild_id, channel.id)
        self.allowed_channels[interaction.guild_id] = new_list
        await interaction.followup.send(f"✅ Biki is now active in {channel.mention}", ephemeral=True)

    @app_commands.command(name="aiunset", description="Remove a channel from Biki's allowed list.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(channel="Channel to remove.")
    async def aiunset(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        new_list = await asyncio.to_thread(_db_remove_channel, interaction.guild_id, channel.id)
        self.allowed_channels[interaction.guild_id] = new_list
        await interaction.followup.send(f"✅ Removed {channel.mention} from Biki's active channels.", ephemeral=True)

    @app_commands.command(name="aireset", description="Allow Biki in all channels (remove channel restrictions).")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def aireset(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        self.allowed_channels.pop(interaction.guild_id, None)
        with _db() as con:
            with con.cursor() as cur:
                cur.execute(
                    "UPDATE ai_companion_config SET allowed_channel_ids='{}' WHERE guild_id=%s",
                    (interaction.guild_id,),
                )
        await interaction.followup.send("✅ Biki is now active in all channels.", ephemeral=True)

    # ------------------------------------------------------------------
    # /bikiwarns / /bikiwarnclear
    # ------------------------------------------------------------------

    @app_commands.command(name="bikiwarns", description="View warnings for a user.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(member="User to look up.")
    async def bikiwarns(self, interaction: discord.Interaction, member: discord.Member) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        warns = await asyncio.to_thread(_db_get_warnings, interaction.guild_id, member.id)
        if not warns:
            await interaction.followup.send(f"{member.display_name} has no warnings. clean slate fr", ephemeral=True)
            return
        lines = [
            f"`{i+1}.` {w['reason']} — <@{w['warned_by']}> at {w['created_at'].strftime('%Y-%m-%d %H:%M')}"
            for i, w in enumerate(warns)
        ]
        body = "\n".join(lines)
        if len(body) > 1900:
            body = body[:1900] + "…"
        await interaction.followup.send(
            f"⚠️ **{len(warns)} warning(s) for {member.display_name}:**\n{body}", ephemeral=True
        )

    @app_commands.command(name="bikiwarnclear", description="Clear all warnings for a user.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(member="User to clear warnings for.")
    async def bikiwarnclear(self, interaction: discord.Interaction, member: discord.Member) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        await asyncio.to_thread(_db_clear_warnings, interaction.guild_id, member.id)
        await interaction.followup.send(f"✅ Cleared all warnings for {member.display_name}.", ephemeral=True)

    # ------------------------------------------------------------------
    # /bikisettings — show all active guild settings (NEW)
    # ------------------------------------------------------------------

    @app_commands.command(name="bikisettings", description="View all active Biki settings for this server.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def bikisettings(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        gid      = interaction.guild_id
        mood     = MOOD_LABELS.get(self.guild_moods.get(gid, "chaotic"), "🌀 Chaotic")
        silenced = "🔇 Yes" if self.guild_silenced.get(gid) else "🔊 No"
        chime    = f"{round(self.guild_chime_rate.get(gid, 0.06)*100, 1)}%"
        cd       = f"{self.guild_cooldown.get(gid, 5.0):.0f}s"
        auto_m   = "✅ On" if self.guild_auto_mood.get(gid) else "❌ Off"
        ch_list  = self.allowed_channels.get(gid, [])
        ch_str   = ", ".join(f"<#{c}>" for c in ch_list) if ch_list else "all channels"
        pers     = "✅ Set" if self.guild_personalities.get(gid) else "❌ None"
        lore     = "✅ Set" if self.guild_lore.get(gid) else "❌ None"
        facts_n  = len(self.guild_facts.get(gid, []))
        emojis_n = len(self.guild_emojis.get(gid, []))

        body = (
            f"**Mood:** {mood}\n"
            f"**Silenced:** {silenced}\n"
            f"**Chime-in rate:** {chime}\n"
            f"**Reply cooldown:** {cd}\n"
            f"**Auto-mood:** {auto_m}\n"
            f"**Active channels:** {ch_str}\n"
            f"**Custom personality:** {pers}\n"
            f"**Server lore:** {lore}\n"
            f"**Pinned facts:** {facts_n}\n"
            f"**Custom emojis:** {emojis_n}"
        )
        await interaction.response.send_message(f"⚙️ **Biki settings for {interaction.guild.name}**\n\n{body}", ephemeral=True)

    # ------------------------------------------------------------------
    # /bikiroastbattle — AI-hosted roast battle (NEW)
    # ------------------------------------------------------------------

    @app_commands.command(name="bikiroastbattle", description="Biki hosts a roast battle between two users.")
    @app_commands.guild_only()
    @app_commands.describe(
        challenger="First roaster.",
        target="Second roaster (the one getting roasted first).",
    )
    async def bikiroastbattle(
        self,
        interaction: discord.Interaction,
        challenger: discord.Member,
        target: discord.Member,
    ) -> None:
        assert interaction.guild_id is not None
        if challenger.id == target.id:
            await interaction.response.send_message("bro you can't roast yourself 💀 (or can you...)", ephemeral=True)
            return

        await interaction.response.defer()
        gid = interaction.guild_id

        prompt = (
            f"You are hosting a ROAST BATTLE in this Discord server. "
            f"Two people are about to roast each other: {challenger.display_name} vs {target.display_name}.\n\n"
            f"You're the host. Introduce this roast battle in your most chaotic, unhinged way. "
            f"Hype both of them up. Make it dramatic. Call it like you're announcing a boxing match. "
            f"Then give {challenger.display_name} the first roast — make it ACTUALLY devastating, short and sharp. "
            f"One punchy line, no mercy. Then tell {target.display_name} to hit back and tag them both."
        )

        personality = self.guild_personalities.get(gid, "")
        try:
            reply = await _call_ai(
                [{"role": "user", "content": prompt}],
                self._mood_addon(gid),
                "",
                400,
                personality,
            )
            # Mention both users in the actual message
            full_msg = f"{challenger.mention} vs {target.mention}\n\n{reply}"
            if len(full_msg) > 1900:
                full_msg = full_msg[:1900]
            await interaction.followup.send(full_msg)
        except Exception as exc:
            log.error("bikiroastbattle: %s", exc)
            await interaction.followup.send("bro my roast brain is broken rn 💀 try again", ephemeral=True)

    # ------------------------------------------------------------------
    # /bikitruth — Truth or Dare (NEW)
    # ------------------------------------------------------------------

    @app_commands.command(name="bikitruth", description="Biki gives a user a truth or dare.")
    @app_commands.guild_only()
    @app_commands.describe(
        member="Who's doing truth or dare.",
        choice="Truth or dare?",
    )
    @app_commands.choices(choice=[
        app_commands.Choice(name="Truth", value="truth"),
        app_commands.Choice(name="Dare",  value="dare"),
    ])
    async def bikitruth(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        choice: app_commands.Choice[str],
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer()
        gid = interaction.guild_id

        action = choice.value
        prompt = (
            f"You're playing Truth or Dare in this Discord server. {member.display_name} picked {action.upper()}.\n\n"
            + (
                f"Give them a truth question that is slightly too personal, chaotic, or embarrassing. "
                f"It should be funny and specific enough to be awkward. Don't let them off easy. "
                f"Address them directly."
                if action == "truth" else
                f"Give them a dare that is chaotic, embarrassing, or ridiculous but achievable in Discord. "
                f"Be creative. No physical dares — Discord dares only (messages, reactions, pings, etc). "
                f"Address them directly and be enthusiastic about it."
            )
        )

        personality = self.guild_personalities.get(gid, "")
        try:
            reply = await _call_ai(
                [{"role": "user", "content": prompt}],
                self._mood_addon(gid),
                "",
                250,
                personality,
            )
            await interaction.followup.send(f"{member.mention} picked **{action.upper()}**:\n\n{reply}")
        except Exception as exc:
            log.error("bikitruth: %s", exc)
            await interaction.followup.send("broken rn 💀 try again", ephemeral=True)

    # ------------------------------------------------------------------
    # /bikiwyr — Would You Rather (NEW)
    # ------------------------------------------------------------------

    @app_commands.command(name="bikiwyr", description="Biki generates a chaotic 'Would You Rather' for the server.")
    @app_commands.guild_only()
    async def bikiwyr(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer()
        gid = interaction.guild_id

        prompt = (
            "Generate a 'Would You Rather' question for a Discord server. "
            "Both options should be chaotic, unhinged, or absurdly specific. "
            "One option should be obviously bad but tempting. The other should be equally bad but different. "
            "Format it as: 'would you rather [A] OR [B]?' — nothing else. No intro, no explanation. "
            "Keep it Discord-appropriate (no explicit content). Make it funny and unpredictable."
        )

        personality = self.guild_personalities.get(gid, "")
        try:
            reply = await _call_ai(
                [{"role": "user", "content": prompt}],
                self._mood_addon(gid),
                "",
                150,
                personality,
            )
            await interaction.followup.send(f"🤔 {reply}\n\n*(react to vote)*")
            # Try to add A/B reactions
            sent_msg = await interaction.original_response()
            for emoji in ["🅰️", "🅱️"]:
                try:
                    await sent_msg.add_reaction(emoji)
                except discord.HTTPException:
                    pass
        except Exception as exc:
            log.error("bikiwyr: %s", exc)
            await interaction.followup.send("broken rn 💀", ephemeral=True)

    # ------------------------------------------------------------------
    # /bikitrivia — Biki hosts trivia (NEW, intentionally wrong sometimes)
    # ------------------------------------------------------------------

    @app_commands.command(name="bikitrivia", description="Biki hosts a trivia question (intentionally wrong sometimes).")
    @app_commands.guild_only()
    @app_commands.describe(topic="Optional topic (leave blank for random).")
    async def bikitrivia(self, interaction: discord.Interaction, topic: Optional[str] = None) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer()
        gid = interaction.guild_id

        prompt = (
            f"You're hosting trivia in Discord. "
            + (f"The topic is: {topic}. " if topic else "Pick a random fun topic. ")
            + "Generate ONE trivia question with 4 multiple choice options (A, B, C, D). "
            "80% of the time give the real correct answer somewhere. "
            "20% of the time confidently give a completely wrong answer like it's right. "
            "Format as:\n"
            "QUESTION: [question]\n"
            "A) [option]\nB) [option]\nC) [option]\nD) [option]\n"
            "ANSWER: [letter] - [brief reason, 1 sentence]\n\n"
            "Keep the question short and fun. Make the wrong options plausible."
        )

        try:
            reply = await _call_ai(
                [{"role": "user", "content": prompt}],
                "",
                "",
                300,
            )
            await interaction.followup.send(f"🎯 **Biki Trivia**{'` — ' + topic + '`' if topic else ''}\n\n{reply}")
        except Exception as exc:
            log.error("bikitrivia: %s", exc)
            await interaction.followup.send("my trivia brain is broken 💀", ephemeral=True)

    # ------------------------------------------------------------------
    # /bikisetpersonality alias commands (kept for backward compat)
    # ------------------------------------------------------------------

    # /bikistats aliases already above; all original commands preserved


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AiCompanion(bot))
