"""
cogs/server_drops_economy.py
-----------------------------
Economy drop system — expanded edition.

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
  Blackjack Duel    (5%)  – community vs House; split 50-pt pool on win
  Hot Potato       (10%)  – pass it or get exploded (bomb or golden sack)
"""

from __future__ import annotations

import asyncio
import random
import sqlite3
import sys
import pathlib
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import discord
from discord import app_commands
from discord.ext import commands

from store import get_drops_channel, set_drops_channel
from .descriptions import GAME_CATALOGUE

# ──────────────────────────── constants ───────────────────────────

DB_PATH      = pathlib.Path(__file__).parent.parent / "economy.db"
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

MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}
SEP    = "▬" * 22

# ── Weighted event pool ────────────────────────────────────────────
EVENT_POOL = (
    ["trivia"]     * 20 +
    ["scramble"]   * 15 +
    ["lootbox"]    * 10 +
    ["boss"]       * 10 +
    ["hotcold"]    * 15 +
    ["emoji"]      * 15 +
    ["bomb"]       * 10 +
    ["multi"]      *  5 +
    ["blackjack"]  *  5 +
    ["hotpotato"]  * 10
)

# ──────────────────────── content banks ───────────────────────────

TRIVIA_BANK: dict[str, list[str]] = {
    # Math
    "What is 5 + 7?":                                        ["12", "twelve"],
    "What is 9 × 9?":                                        ["81", "eighty-one", "eighty one"],
    "What is the square root of 144?":                       ["12", "twelve"],
    "How many sides does a hexagon have?":                   ["6", "six"],
    "What is 15% of 200?":                                   ["30", "thirty"],
    "What is 2 to the power of 8?":                         ["256"],
    "What is 7 × 8?":                                        ["56", "fifty-six", "fifty six"],
    "How many degrees are in a right angle?":                ["90", "ninety"],
    "What is 100 divided by 4?":                             ["25", "twenty-five", "twenty five"],
    "What is the value of pi rounded to 2 decimal places?": ["3.14"],
    "What is the next prime number after 7?":               ["11", "eleven"],
    "What is 12 squared?":                                   ["144"],
    "How many minutes are in 3 hours?":                     ["180"],
    "What is 1000 divided by 8?":                            ["125"],
    "What is 17 + 26?":                                      ["43", "forty-three", "forty three"],

    # Science
    "What gas do plants absorb from the air?":              ["carbon dioxide", "co2"],
    "What is H2O commonly known as?":                       ["water"],
    "How many bones are in the adult human body?":          ["206"],
    "What planet is closest to the Sun?":                   ["mercury"],
    "What is the chemical symbol for gold?":                ["au"],
    "What is the chemical symbol for iron?":                ["fe"],
    "What force keeps us on the ground?":                   ["gravity"],
    "What is the speed of light (rounded) in km/s?":       ["300000", "300,000"],
    "What organ pumps blood through the body?":             ["heart"],
    "What is the powerhouse of the cell?":                  ["mitochondria"],
    "What gas do humans breathe out?":                      ["carbon dioxide", "co2"],
    "How many chromosomes do humans have?":                 ["46"],
    "What is the hardest natural substance on Earth?":      ["diamond"],
    "What planet is known as the Red Planet?":              ["mars"],
    "What is the chemical symbol for water?":               ["h2o"],

    # Geography
    "What is the capital of France?":                       ["paris"],
    "What is the capital of Japan?":                        ["tokyo"],
    "Which continent is Brazil on?":                        ["south america"],
    "What is the longest river in the world?":              ["nile"],
    "What is the smallest country in the world?":          ["vatican city", "vatican"],
    "What is the tallest mountain in the world?":          ["mount everest", "everest"],
    "How many continents are there?":                       ["7", "seven"],
    "What is the capital of Australia?":                    ["canberra"],
    "What ocean is the largest?":                           ["pacific", "pacific ocean"],
    "What country has the most natural lakes?":            ["canada"],
    "What is the capital of Canada?":                       ["ottawa"],
    "Which country is the Eiffel Tower in?":               ["france"],
    "What is the capital of Germany?":                      ["berlin"],
    "What is the capital of Spain?":                        ["madrid"],
    "What river flows through Egypt?":                      ["nile", "the nile"],

    # History
    "In what year did World War II end?":                   ["1945"],
    "Who was the first US President?":                      ["george washington", "washington"],
    "What year did the Titanic sink?":                      ["1912"],
    "Which empire built the Colosseum?":                    ["roman", "roman empire"],
    "In what year did the Berlin Wall fall?":               ["1989"],
    "Who invented the telephone?":                         ["alexander graham bell", "bell"],
    "What year did World War I begin?":                     ["1914"],
    "Who was the first person to walk on the Moon?":       ["neil armstrong", "armstrong"],
    "In what year did the French Revolution begin?":        ["1789"],
    "Who wrote the Declaration of Independence?":           ["thomas jefferson", "jefferson"],

    # Pop Culture / Gaming
    "What game features a character named Master Chief?":  ["halo"],
    "In Minecraft, what material is the strongest?":       ["netherite"],
    "What is the name of Link's nemesis in Zelda?":        ["ganon", "ganondorf"],
    "What color is Pikachu?":                               ["yellow"],
    "What game has the Battle Bus?":                        ["fortnite"],
    "How many players start in a standard chess game?":    ["32"],
    "What console did Nintendo release in 2017?":          ["switch", "nintendo switch"],
    "In Among Us, what are the killers called?":           ["impostors", "imposters", "impostor"],
    "What is the max level in Minecraft?":                 ["2147483647"],
    "What game features 'Among Drip'?":                    ["among us"],
    "What is Roblox's virtual currency called?":           ["robux"],
    "In Valorant, what is the objective bomb called?":     ["spike"],
    "What year was Minecraft first released?":             ["2009"],
    "How many players are in a standard League of Legends team?": ["5", "five"],
    "What is the name of the currency in Animal Crossing?": ["bells"],

    # Discord / Internet
    "What does 'DM' stand for on Discord?":               ["direct message"],
    "What does 'AFK' mean?":                               ["away from keyboard"],
    "What does 'GG' stand for in gaming?":                ["good game"],
    "What does 'LFG' stand for?":                          ["looking for group"],
    "What does 'TBH' stand for?":                          ["to be honest"],
    "What color is Discord's logo?":                        ["purple", "blurple"],
    "What does 'NGL' mean?":                               ["not gonna lie", "not going to lie"],
    "What does 'POV' stand for?":                          ["point of view"],
    "What does 'FOMO' stand for?":                         ["fear of missing out"],
    "What does 'IRL' stand for?":                          ["in real life"],

    # General Knowledge
    "How many planets are in our solar system?":           ["8", "eight"],
    "How many sides does a triangle have?":                ["3", "three"],
    "What color is the sky on a clear day?":               ["blue"],
    "How many days are in a leap year?":                   ["366"],
    "What is the largest mammal in the world?":            ["blue whale"],
    "How many strings does a standard guitar have?":       ["6", "six"],
    "What is the fastest land animal?":                    ["cheetah"],
    "How many colors are in a rainbow?":                   ["7", "seven"],
    "What is the largest ocean on Earth?":                 ["pacific", "pacific ocean"],
    "How many hours are in a day?":                        ["24", "twenty-four"],
    "What language has the most native speakers?":         ["mandarin", "chinese", "mandarin chinese"],
    "How many teeth do adult humans have?":                ["32", "thirty-two"],
    "What is the tallest animal on Earth?":                ["giraffe"],
    "What is the currency of Japan?":                      ["yen"],
    "What is the currency of the UK?":                     ["pound", "pounds", "pound sterling"],
    "How many sides does a pentagon have?":                ["5", "five"],
    "What instrument has 88 keys?":                        ["piano"],
    "What is the largest planet in our solar system?":    ["jupiter"],
    "Who painted the Mona Lisa?":                          ["leonardo da vinci", "da vinci", "leonardo"],
    "What is the national animal of Australia?":          ["kangaroo"],
}

SCRAMBLE_WORDS: list[str] = [
    # Tech / Discord
    "python", "discord", "server", "economy", "lootbox",
    "scramble", "trivia", "reward", "points", "channel",
    "button", "winner", "random", "treasure", "typing",
    "keyboard", "monitor", "laptop", "internet", "browser",
    "network", "router", "firewall", "bandwidth", "discord",
    "streaming", "database", "command", "message", "reaction",
    "webhook", "sidebar", "mention", "profile", "nickname",

    # Gaming
    "fortnite", "minecraft", "roblox", "valorant", "overwatch",
    "pokemon", "dungeon", "dragon", "warrior", "potion",
    "respawn", "loadout", "headshot", "camping", "airdrop",
    "sniper", "grenade", "shotgun", "crossbow", "pistol",
    "crafting", "building", "farming", "grinding", "raiding",
    "checkpoint", "bossfight", "endgame", "sandbox", "speedrun",

    # General
    "elephant", "giraffe", "dolphin", "penguin", "cheetah",
    "volcano", "glacier", "tornado", "thunder", "rainbow",
    "diamond", "emerald", "sapphire", "crystal", "ancient",
    "mystery", "explore", "journey", "courage", "victory",
    "champion", "legendary", "ultimate", "infinite", "dynamic",
    "creative", "imagine", "inspire", "achieve", "succeed",
    "strategy", "tactics", "mission", "stealth", "bounty",
    "goblin", "wizard", "paladin", "hunter", "archer",
    "kingdom", "empire", "castle", "throne", "crown",
    "festival", "parade", "concert", "trophy", "medal",
    "velocity", "gravity", "quantum", "reactor", "particle",
    "universe", "nebula", "galaxy", "orbital", "asteroid",
    "mountain", "canyon", "waterfall", "savanna", "tundra",
    "tropical", "mirage", "horizon", "twilight", "solstice",
    "blossom", "thunder", "whisper", "silence", "eternal",
]

EMOJI_PUZZLES: list[tuple[str, str]] = [
    # Movies
    ("🦁👑", "The Lion King"),
    ("🕷️🧑", "Spider-Man"),
    ("🧊❄️👸", "Frozen"),
    ("🤠🐍🌀", "Indiana Jones"),
    ("🦈🌊😱", "Jaws"),
    ("🌌⭐🚀", "Star Wars"),
    ("🤖🚗🔁", "Transformers"),
    ("🧙💍🔥", "Lord of the Rings"),
    ("🦋🦅🤿", "Avatar"),
    ("👻🏚️🕯️", "Ghostbusters"),
    ("🦸‍♂️🦇🌃", "Batman"),
    ("🏎️💨⚡", "Cars"),
    ("🧟🌍💀", "World War Z"),
    ("🐠🌊🐟", "Finding Nemo"),
    ("🐀🍳👨‍🍳", "Ratatouille"),
    ("👶🤖❤️", "A.I. Artificial Intelligence"),
    ("🧸🎠🌈", "Toy Story"),
    ("🐼🥋🐉", "Kung Fu Panda"),
    ("🕰️✈️🧒", "Peter Pan"),
    ("🌕🧑‍🚀🌑", "Apollo 13"),

    # TV Shows
    ("🧪🔵💎", "Breaking Bad"),
    ("🐉🏰🗡️", "Game of Thrones"),
    ("🏝️🌴✈️💥", "Lost"),
    ("🌂☂️💛💙", "How I Met Your Mother"),
    ("🏥💊🩺", "Grey's Anatomy"),
    ("🕵️🔍🧠", "Sherlock"),
    ("👨‍💼📊💰", "The Office"),
    ("🌵🤠🔫", "Breaking Bad"),
    ("🤝😊📋", "Friends"),
    ("🧟🌆🔫", "The Walking Dead"),
    ("🚂⌚🌌", "Dark"),
    ("🤵🍸🎰", "Suits"),
    ("🧹🔮📚", "The Witcher"),
    ("👩‍🔬🧬🔭", "Stranger Things"),
    ("🏫🪄🦉", "Harry Potter"),

    # Games
    ("🍄👷🏰", "Super Mario"),
    ("🧱💎⛏️", "Minecraft"),
    ("🌀🦔💨", "Sonic the Hedgehog"),
    ("⚔️🛡️🧝", "The Legend of Zelda"),
    ("🔴🟡🟢⬆️", "Among Us"),
    ("🦆🎮🏆", "Duck Hunt"),
    ("🃏🎯🔫", "Valorant"),
    ("🤖🌍⚙️", "Horizon Zero Dawn"),
    ("🐉🌐🗡️", "World of Warcraft"),
    ("🔫🏝️🪂", "PUBG"),
    ("👁️🌑👾", "Dead Space"),
    ("🏙️🚗💥", "Grand Theft Auto"),
    ("🦸‍♂️🦹‍♂️🏙️", "Marvel's Spider-Man"),
    ("🌊🤿🐠🏝️", "Subnautica"),
    ("🪐🚀🌌", "Mass Effect"),

    # Brands / Apps
    ("🍎💻📱", "Apple"),
    ("🔍🌐🔎", "Google"),
    ("📘👍👥", "Facebook"),
    ("📸❤️💬", "Instagram"),
    ("🐦💬🌐", "Twitter"),
    ("🎵🎶📱", "TikTok"),
    ("📹🔴▶️", "YouTube"),
    ("💼🤝👔", "LinkedIn"),
    ("📦🚚🛒", "Amazon"),
    ("🚗🗺️📍", "Uber"),
    ("🍔🛵🏠", "DoorDash"),
    ("🎬🍿❤️", "Netflix"),
    ("🎵🎧🟢", "Spotify"),
    ("🎮🕹️🏆", "Steam"),
    ("💬🔒🟢", "WhatsApp"),

    # Food & Misc
    ("🍕🧀🍅", "Pizza"),
    ("🍔🥩🧅", "Burger"),
    ("🌮🌶️🧀", "Taco"),
    ("🍣🐟🍚", "Sushi"),
    ("🍩☕🌅", "Dunkin Donuts"),
    ("🍦🌈🍭", "Ice Cream"),
    ("🍕🎸🎶", "Pizza Party"),
    ("☕📖🌧️", "Coffee Shop"),
    ("🏋️‍♂️💪🥇", "Gym"),
    ("✈️🌍🧳", "Travel"),
    ("🎪🎡🎢", "Amusement Park"),
    ("🎓📚✏️", "School"),
    ("🌙⭐🔭", "Stargazing"),
    ("🎸🥁🎤", "Rock Band"),
    ("🏖️🌊☀️", "Beach Day"),

    # Bonus Pop Culture
    ("💀⚓🦜", "Pirates of the Caribbean"),
    ("🦾🤖❤️‍🔥", "Terminator"),
    ("👽🛸🌽", "Signs"),
    ("🔦👁️🌲", "The Blair Witch Project"),
    ("🧲🦸‍♂️🔴", "Magneto"),
    ("⚡🧒🦉", "Harry Potter"),
    ("🕰️🔮🌀", "Doctor Strange"),
    ("🌻🎩🃏", "The Dark Knight"),
    ("🧬👾🌌", "Interstellar"),
    ("🏔️🧗‍♂️💎", "Everest"),
    # Extra round — games & misc
    ("🧟🪓🌲", "Dead by Daylight"),
    ("🐸🎮🕹️", "Frogger"),
    ("🧩🎯🏅", "Puzzle Game"),
    ("🌍🔫🪖", "Call of Duty"),
    ("🦅🏹🌿", "Far Cry"),
    ("🏰💣🗺️", "Clash of Clans"),
    ("🐉🔥💰", "Dungeons and Dragons"),
    ("🤖🌌⭐", "Star Trek"),
    ("🧜‍♀️🌊🐚", "The Little Mermaid"),
    ("🦸‍♀️⚡🌩️", "Captain Marvel"),
    ("🌋🦖🦕", "Jurassic Park"),
    ("🎩🐇🪄", "Now You See Me"),
    ("🏎️🌍🏆", "Formula One"),
    ("🧊🏒🥅", "Ice Hockey"),
    ("🎭🌹💌", "Romeo and Juliet"),
]


# ─────────────────────── database helpers ─────────────────────────

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
    """UPSERT points for a user and return their new total."""
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


def deduct_points(user_id: int, amount: int) -> int:
    """Deduct points (floor at 0) and return new total."""
    with _db_connect() as con:
        con.execute(
            """
            INSERT INTO users (user_id, points) VALUES (?, 0)
            ON CONFLICT(user_id) DO UPDATE
                SET points = MAX(0, points - ?)
            """,
            (user_id, amount),
        )
        con.commit()
        row = con.execute(
            "SELECT points FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["points"] if row else 0


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


def get_top_user() -> tuple[int, int] | None:
    """Return (user_id, points) of the user with the highest balance, or None."""
    with _db_connect() as con:
        row = con.execute(
            "SELECT user_id, points FROM users ORDER BY points DESC LIMIT 1"
        ).fetchone()
        return (row["user_id"], row["points"]) if row else None


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
    e.set_footer(text="SOLACE ECONOMY  •  Trivia Event")
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
    e.set_footer(text="SOLACE ECONOMY  •  Scramble Event")
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
    e.set_footer(text="SOLACE ECONOMY  •  Supply Drop")
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
    e.set_footer(text="SOLACE ECONOMY  •  Co-Op Boss Raid")
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
    e.set_footer(text="SOLACE ECONOMY  •  Boss Raid Victory")
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
    e.set_footer(text="SOLACE ECONOMY  •  Number Guessing")
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
    e.set_footer(text="SOLACE ECONOMY  •  Emoji Puzzle")
    return e


def embed_bomb_waiting() -> discord.Embed:
    e = _base_embed(C_BOMB)
    e.description = (
        f"```ansi\n\u001b[1;31m  💣  REACTION TIME BOMB  💣\u001b[0m\n```"
        f"{SEP}\n"
        f"**The fuse is lit...**\n\n"
        f"🕰️ Wait for my signal to **DEFUSE** it!\n"
        f"⚠️ *Click BEFORE the signal = -5 pts penalty!*\n"
        f"{SEP}"
    )
    e.set_footer(text="SOLACE ECONOMY  •  Stay patient...")
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
    e.set_footer(text="SOLACE ECONOMY  •  Time Bomb — DEFUSE!")
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
    e.set_footer(text="SOLACE ECONOMY  •  Multiplier Event")
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
    e.set_footer(text="SOLACE ECONOMY  •  Bounty Hunt")
    return e


def embed_win_text(user: discord.Member | discord.User, payout: int, new_total: int, drop_type: str) -> discord.Embed:
    colour = {
        "trivia": C_TRIVIA, "scramble": C_SCRAMBLE,
        "emoji": C_EMOJI, "hotcold": C_HOT_COLD,
        "bounty": C_BOUNTY,
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
    e.set_footer(text="SOLACE ECONOMY")
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
    e.set_footer(text="SOLACE ECONOMY  •  Supply Drop")
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
    e.set_footer(text="SOLACE ECONOMY")
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
    e.set_footer(text="SOLACE ECONOMY  •  /leaderboard to see rankings")
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
    e.set_footer(text=f"SOLACE ECONOMY  •  Top {len(rows)} players")
    return e


def embed_set_confirm(label: str, channel: discord.TextChannel) -> discord.Embed:
    e = _base_embed(C_SET)
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
        e = _base_embed(C_SET)
        e.description = f"{SEP}\n**{label}** is set to <#{channel_id}>\n{SEP}"
    else:
        e = _base_embed(C_TIMEOUT)
        e.description = f"{SEP}\n**{label}** has not been configured yet.\n{SEP}"
    e.set_footer(text="SOLACE ECONOMY")
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
    e.set_footer(text="SOLACE ECONOMY  •  Blackjack Duel")
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
    e.set_footer(text="SOLACE ECONOMY  •  Hot Potato")
    return e


def embed_potato_explode_bomb(holder: discord.Member | discord.User) -> discord.Embed:
    e = _base_embed(0xFF0000)
    e.description = (
        f"```ansi\n\u001b[1;31m  💣  BOOM!  💣\u001b[0m\n```"
        f"{SEP}\n"
        f"💥 The potato **EXPLODED** on {holder.mention}!\n\n"
        f"**-20 pts** deducted from their balance.\n"
        f"{SEP}"
    )
    e.set_footer(text="SOLACE ECONOMY  •  Hot Potato — Bomb!")
    return e


def embed_potato_explode_gold(holder: discord.Member | discord.User) -> discord.Embed:
    e = _base_embed(C_WIN)
    e.description = (
        f"```ansi\n\u001b[1;33m  🎁  GOLDEN LOOT SACK!  🎁\u001b[0m\n```"
        f"{SEP}\n"
        f"🏆 The potato turned into a **Golden Loot Sack** for {holder.mention}!\n\n"
        f"**+50 pts** added to their balance!\n"
        f"{SEP}"
    )
    e.set_footer(text="SOLACE ECONOMY  •  Hot Potato — Golden Sack!")
    return e


# ─────────────────────────── UI Views ─────────────────────────────

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

    def _total_clicks(self) -> int:
        return self._hit_clicks + self._stand_clicks

    async def _record_click(self, interaction: discord.Interaction, action: str):
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
        except discord.HTTPException:
            pass

        channel = self.cog.bot.get_channel(self.channel_id)
        if channel is None:
            return

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
                self.cog.active_drops.pop(self.channel_id, None)
                self.stop()
                return

            # After a hit that didn't bust, automatically stand (simplified flow)
            # This avoids infinite vote loops; a new round of votes could be added here.

        # Stand / post-hit — dealer plays out
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

        self.cog.active_drops.pop(self.channel_id, None)

        if outcome == "win" and self.participants:
            share = max(1, self.payout // len(self.participants))
            lines = []
            for uid in self.participants:
                new_total = add_points(uid, share)
                member = channel.guild.get_member(uid)  # type: ignore[union-attr]
                name   = member.mention if member else f"<@{uid}>"
                lines.append(f"{name} **+{share} pts** → `{new_total} pts`")
            await channel.send(
                f"🏆 **Chat wins the Blackjack Duel!**\n"
                f"The `{self.payout} pt` pool is split `{share} pts` each:\n"
                + "\n".join(lines)
            )
        elif outcome == "push":
            await channel.send("🤝 **Push — nobody wins or loses this round.**")
        else:
            await channel.send("🃏 **House wins!** The chat couldn't beat the dealer.")

        # Cancel the timeout task
        drop = self.cog.active_drops.get(self.channel_id)
        if drop:
            task: asyncio.Task | None = drop.get("task")
            if task and not task.done():
                task.cancel()
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

    @discord.ui.button(label="  GRAB LOOT  ", style=discord.ButtonStyle.success, emoji="📦")
    async def grab_loot(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.claimed:
            await interaction.response.send_message("This crate has already been claimed.", ephemeral=True)
            return

        drop = self.cog.active_drops.get(self.channel_id)
        if drop is None or drop.get("type") != "lootbox":
            await interaction.response.send_message("This drop has expired.", ephemeral=True)
            return

        self.claimed = True
        del self.cog.active_drops[self.channel_id]  # memory before DB

        payout    = drop["payout"]
        new_total = add_points(interaction.user.id, payout)

        button.disabled = True
        button.label    = f"  Claimed by {interaction.user.display_name}  "
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(embed=embed_win_lootbox(interaction.user, payout, new_total))

        task: asyncio.Task | None = drop.get("task")
        if task and not task.done():
            task.cancel()
        self.stop()


class BombView(discord.ui.View):
    """
    Two-phase time bomb.
    Phase 1 (armed=False): clicking deducts 5 pts (too early).
    Phase 2 (armed=True):  first click wins the payout.
    """
    def __init__(self, cog: "ServerDropsEconomy", channel_id: int, payout: int):
        super().__init__(timeout=None)
        self.cog        = cog
        self.channel_id = channel_id
        self.payout     = payout
        self.armed      = False
        self.defused    = False

    @discord.ui.button(label="  💥 DEFUSE  ", style=discord.ButtonStyle.danger)
    async def defuse(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.defused:
            await interaction.response.send_message("Already defused!", ephemeral=True)
            return

        if not self.armed:
            # Too early — penalise
            new_total = deduct_points(interaction.user.id, 5)
            await interaction.response.send_message(
                f"⚠️ Too early! You lost **5 pts**. Balance: `{new_total} pts`",
                ephemeral=True,
            )
            return

        # Armed — first click wins
        drop = self.cog.active_drops.get(self.channel_id)
        if drop is None or drop.get("type") != "bomb":
            await interaction.response.send_message("This drop has expired.", ephemeral=True)
            return

        self.defused = True
        del self.cog.active_drops[self.channel_id]

        new_total = add_points(interaction.user.id, self.payout)

        button.disabled = True
        button.label    = f"  Defused by {interaction.user.display_name}  "
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            f"💥 **{interaction.user.mention}** defused the bomb and earned **`{self.payout} pts`**! "
            f"Balance: `{new_total} pts`"
        )

        task: asyncio.Task | None = drop.get("task")
        if task and not task.done():
            task.cancel()
        self.stop()


# ──────────────────────────── the Cog ─────────────────────────────

class ServerDropsEconomy(commands.Cog, name="ServerDropsEconomy"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_db()
        self.msg_counters:  dict[int, int]  = {}  # channel_id → message count
        self.active_drops:  dict[int, dict] = {}  # channel_id → drop state
        self.double_points: bool            = False  # global multiplier flag
        self.double_until:  float           = 0.0    # epoch time when multiplier expires
        # Boss attack cooldowns: {channel_id: {user_id: last_attack_epoch}}
        self._boss_cooldowns: dict[int, dict[int, float]] = {}

    # ──────────────────── helpers ──────────────────────

    def _effective_payout(self, base: int) -> int:
        import time
        if self.double_points and time.time() < self.double_until:
            return base * 2
        elif self.double_points:
            self.double_points = False  # expired
        return base

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

        drops_id = get_drops_channel(message.guild.id)
        if drops_id is None or message.channel.id != drops_id:
            return

        cid = message.channel.id

        # ── Route to active drop if one is running ──
        if cid in self.active_drops:
            drop = self.active_drops[cid]
            dtype = drop.get("type")
            if dtype in ("trivia", "scramble", "emoji", "bounty"):
                await self._check_text_answer(message, drop)
            elif dtype == "hotcold":
                await self._check_number_guess(message, drop)
            elif dtype == "boss":
                await self._handle_boss_attack(message, drop)
            elif dtype == "hotpotato":
                await self._handle_potato_pass(message, drop)
            return

        # ── Increment counter ──
        self.msg_counters[cid] = self.msg_counters.get(cid, 0) + 1
        if self.msg_counters[cid] >= MSG_TRIGGER:
            self.msg_counters[cid] = 0
            await self._trigger_drop(message.channel)

    # ──────────────────── drop triggering ─────────────

    async def _trigger_drop(self, channel: discord.TextChannel):
        choice = random.choice(EVENT_POOL)
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
        }
        await dispatch[choice](channel)

    # ─────────────── Trivia ───────────────────────────

    async def _start_trivia(self, channel: discord.TextChannel):
        question, answers = random.choice(list(TRIVIA_BANK.items()))
        payout = self._effective_payout(random.randint(5, 15))
        await channel.send(embed=embed_trivia(question, payout))
        state = {"type": "trivia", "answer": [a.lower().strip() for a in answers], "payout": payout, "task": None}
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._drop_timeout(channel))

    # ─────────────── Scramble ─────────────────────────

    async def _start_scramble(self, channel: discord.TextChannel):
        word      = random.choice(SCRAMBLE_WORDS)
        scrambled = scramble_word(word)
        payout    = self._effective_payout(random.randint(5, 15))
        await channel.send(embed=embed_scramble(scrambled, payout))
        state = {"type": "scramble", "answer": [word.lower().strip()], "payout": payout, "task": None}
        self.active_drops[channel.id] = state
        state["task"] = asyncio.create_task(self._drop_timeout(channel))

    # ─────────────── Lootbox ──────────────────────────

    async def _start_lootbox(self, channel: discord.TextChannel):
        payout = self._effective_payout(random.randint(15, 30))
        view   = LootboxView(cog=self, channel_id=channel.id)
        msg    = await channel.send(embed=embed_lootbox("15–30"), view=view)
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
        if message.content.strip().lower() != "!attack":
            return

        import time
        cid     = message.channel.id
        uid     = message.author.id
        now     = time.time()
        cds     = self._boss_cooldowns.get(cid, {})
        last_at = cds.get(uid, 0.0)

        if now - last_at < 3.0:
            remaining = 3.0 - (now - last_at)
            await message.channel.send(
                f"⏱️ {message.author.mention} cooldown! Wait `{remaining:.1f}s`.",
                delete_after=2,
            )
            return

        cds[uid] = now
        damage = random.randint(1, 5)
        drop["hp"] = max(0, drop["hp"] - damage)
        drop["attackers"][uid] = drop["attackers"].get(uid, 0) + damage

        await message.add_reaction("⚔️")

        # Update the boss embed
        try:
            await drop["msg"].edit(
                embed=embed_boss(drop["hp"], drop["max_hp"], drop["attackers"])
            )
        except discord.HTTPException:
            pass

        if drop["hp"] <= 0:
            # Boss defeated
            task: asyncio.Task | None = drop.get("task")
            if task and not task.done():
                task.cancel()

            del self.active_drops[cid]
            self._boss_cooldowns.pop(cid, None)

            attackers = drop["attackers"]
            total_pool = 100
            payout_each = max(1, total_pool // max(len(attackers), 1))
            payout_each = self._effective_payout(payout_each)

            await message.channel.send(embed=embed_boss_dead(attackers, payout_each))

            for raider_id in attackers:
                add_points(raider_id, payout_each)

    # ─────────────── Hot or Cold ──────────────────────

    async def _start_hotcold(self, channel: discord.TextChannel):
        secret = random.randint(1, 100)
        payout = self._effective_payout(random.randint(10, 20))
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
        try:
            guess = int(message.content.strip())
        except ValueError:
            return

        secret = drop["secret"]
        cid    = message.channel.id

        if guess == secret:
            del self.active_drops[cid]
            task: asyncio.Task | None = drop.get("task")
            if task and not task.done():
                task.cancel()

            payout    = drop["payout"]
            new_total = add_points(message.author.id, payout)
            await message.channel.send(
                f"🎯 **{message.author.mention}** guessed it! The number was **{secret}**!\n"
                f"**＋{payout} pts** earned  ›  Balance: `{new_total} pts`"
            )
        elif guess < secret:
            await message.add_reaction("👆")   # Higher
            try:
                await drop["msg"].edit(embed=embed_hotcold("Higher 👆"))
            except discord.HTTPException:
                pass
        else:
            await message.add_reaction("👇")   # Lower
            try:
                await drop["msg"].edit(embed=embed_hotcold("Lower 👇"))
            except discord.HTTPException:
                pass

    # ─────────────── Emoji Puzzle ─────────────────────

    async def _start_emoji(self, channel: discord.TextChannel):
        emojis, answer = random.choice(EMOJI_PUZZLES)
        payout = self._effective_payout(random.randint(10, 20))
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
        payout = self._effective_payout(random.randint(20, 30))
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
        drop = self.active_drops.pop(channel.id, None)
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
        payout     = self._effective_payout(50)   # fixed 50-pt pool

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
        # Disable buttons
        for child in view.children:
            child.disabled = True  # type: ignore[attr-defined]
        try:
            await view.msg.edit(view=view)
        except discord.HTTPException:
            pass
        self.active_drops.pop(channel.id, None)
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

        drop = self.active_drops.pop(channel.id, None)
        if drop is None:
            return   # Already resolved (e.g. another coroutine popped it)

        holder_id = drop["holder_id"]
        member    = channel.guild.get_member(holder_id)
        if member is None:
            await channel.send("🥔 The potato disappeared — nobody around to hold it!")
            return

        # 50/50 coin flip
        if random.random() < 0.5:
            # BOMB
            new_total = deduct_points(holder_id, 20)
            await channel.send(
                embed=embed_potato_explode_bomb(member),
            )
            await channel.send(
                f"💣 {member.mention} **lost 20 pts**!  Balance: `{new_total} pts`"
            )
        else:
            # GOLDEN LOOT SACK
            new_total = add_points(holder_id, 50)
            await channel.send(
                embed=embed_potato_explode_gold(member),
            )
            await channel.send(
                f"🎁 {member.mention} **gained 50 pts**!  Balance: `{new_total} pts`"
            )

    async def _handle_potato_pass(self, message: discord.Message, drop: dict):
        """Called from on_message when a hot potato drop is active."""
        content = message.content.strip()
        if not content.lower().startswith("!pass"):
            return
        if message.author.id != drop["holder_id"]:
            return   # Only the current holder can pass

        # Parse target mention
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

        # Transfer
        drop["holder_id"] = new_holder.id
        # Reset the 10-second personal pass window (the explode timer keeps running)
        loop      = asyncio.get_event_loop()
        remaining = max(0, drop["explode_at"] - loop.time())
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
            # Bounty on the richest user
            top = get_top_user()
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
            bounty_amount = min(25, target_pts)  # Cap at their actual balance
            emojis, answer = random.choice(EMOJI_PUZZLES)

            try:
                target_member = channel.guild.get_member(target_id)
                target_name   = target_member.display_name if target_member else f"User {target_id}"
            except Exception:
                target_name = f"User {target_id}"

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

    # ─────────────── Text answer checker ──────────────

    async def _check_text_answer(self, message: discord.Message, drop: dict):
        if message.content.lower().strip() not in drop["answer"]:
            return

        cid  = message.channel.id
        dtype = drop["type"]
        del self.active_drops[cid]  # memory before DB

        task: asyncio.Task | None = drop.get("task")
        if task and not task.done():
            task.cancel()

        payout = drop["payout"]

        # Bounty: deduct from target before awarding
        if dtype == "bounty":
            target_id = drop.get("target_id")
            if target_id and target_id != message.author.id:
                deduct_points(target_id, payout)

        new_total = add_points(message.author.id, payout)
        await message.channel.send(
            embed=embed_win_text(message.author, payout, new_total, dtype)
        )

    # ─────────────── Timeout handler ──────────────────

    async def _drop_timeout(self, channel: discord.TextChannel):
        await asyncio.sleep(DROP_TIMEOUT)
        drop = self.active_drops.pop(channel.id, None)
        if drop is None:
            return

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
            self._boss_cooldowns.pop(channel.id, None)

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

        # hotpotato has its own internal timer; nothing extra to clean up here

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

    # ─────────────── /points ──────────────────────────

    @commands.hybrid_command(name="points", description="Check your (or another member's) point balance.")
    @app_commands.describe(member="The member whose points you want to check.")
    async def points_command(self, ctx: commands.Context, member: discord.Member | None = None):
        target  = member or ctx.author
        balance = get_points(target.id)
        await ctx.send(embed=embed_points(target, balance))

    # ─────────────── /leaderboard ─────────────────────

    @commands.hybrid_command(name="leaderboard", description="Show the top 10 players by points.")
    async def leaderboard_command(self, ctx: commands.Context):
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        rows = get_leaderboard(10)
        await ctx.send(embed=await embed_leaderboard(ctx.guild, rows))
          # ─────────────── /games ───────────────────────────

    @app_commands.command(name="games", description="View the description and rewards for all drop games.")
    @app_commands.describe(game="Pick a specific game to look up, or leave blank to see all.")
    @app_commands.choices(game=[
        app_commands.Choice(name="🧠 Trivia", value="trivia"),
        app_commands.Choice(name="🔤 Word Scramble", value="scramble"),
        app_commands.Choice(name="🎯 Hot or Cold", value="hotcold"),
        app_commands.Choice(name="🧩 Emoji Puzzle", value="emoji"),
        app_commands.Choice(name="📦 Supply Lootbox", value="lootbox"),
        app_commands.Choice(name="⚔️ Co-Op Boss Raid", value="boss"),
        app_commands.Choice(name="💣 Reaction Time Bomb", value="bomb"),
        app_commands.Choice(name="🥔 Hot Potato", value="hotpotato"),
        app_commands.Choice(name="✨ Multipliers & Bounties", value="multi"),
        app_commands.Choice(name="🃏 Blackjack Duel", value="blackjack"),
    ])
    async def games_command(self, interaction: discord.Interaction, game: str | None = None) -> None:
        if game:
            data = GAME_CATALOGUE[game]
            embed = discord.Embed(
                title=data["title"],
                description=data["description"],
                color=data["color"]
            )
            embed.set_footer(text="SOLACE ECONOMY • Game Catalogue")
            await interaction.response.send_message(embed=embed)
            return

        embed = discord.Embed(
            title="🎮 Solace Economy — Game Catalogue Overview",
            description=f"Here is a breakdown of every event that can trigger while chatting!\n{SEP}",
            color=0x2C2F33
        )
        
        for key, data in GAME_CATALOGUE.items():
            embed.add_field(
                name=data["title"],
                value=data["description"] + f"\n{SEP}",
                inline=False
            )
            
        embed.set_footer(text="SOLACE ECONOMY • Use /games [game] for individual lookups")
        await interaction.response.send_message(embed=embed)
      


# ────────────────────────────── setup ─────────────────────────────

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ServerDropsEconomy(bot))
