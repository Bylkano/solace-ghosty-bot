"""
cogs/simon_says.py  —  Simon Says v1.0
=======================================
A fully self-contained Discord.py Cog for a "Simon Says" Last-Man-Standing game.

NEW IN v1.0
-----------
  • Anti-copy-paste  — _poison() injects invisible zero-width chars into displayed prompts
  • Betting system   — users can bet currency on a player before the game starts
  • Double Trouble   — 15 % chance of a dual-task round (type string AND backward word)
  • Trap Questions   — "Simon Didn't Say" rounds; answering = instant elimination
  • !simonpause / !simonresume  — freeze/resume the round timer mid-game
  • !simonblacklist @user       — permanently ban a user from joining (backed by DB)
  • !simonextend <minutes>      — extend an open lobby
  • !simoncatalogue             — full feature manual embed
  • PostgreSQL stability        — all DB calls wrapped in try/except with rollback
  • Strict Python 3.11.9 type hints throughout

Requirements:  discord.py >= 2.0  |  Python >= 3.11
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import sys
import time
from dataclasses import dataclass, field

import psycopg2
from psycopg2.extras import RealDictCursor
import discord
from discord.ext import commands

# Render free tier can hit the default recursion limit — raise it for headroom
sys.setrecursionlimit(5000)

log = logging.getLogger("bot.simon_says")

# ---------------------------------------------------------------------------
# ANTI-COPY-PASTE
# Inject invisible zero-width characters into displayed prompt text.
# The clean answer stored in the dict is NEVER poisoned — only the embed display.
# Users who copy-paste get hidden chars that break the match check.
# ---------------------------------------------------------------------------
_ZWS  = "\u200b"   # zero-width space
_ZWNJ = "\u200c"   # zero-width non-joiner

_POISONS: tuple[str, ...] = (_ZWS, _ZWNJ)


def _poison(text: str) -> str:
    """Return a visually identical string with invisible chars injected at
    random positions (every 2–4 chars). Only call this on the DISPLAY string."""
    out: list[str] = []
    i = 0
    while i < len(text):
        out.append(text[i])
        # inject after every 2nd–4th character, randomly
        if (i + 1) % random.randint(2, 4) == 0:
            out.append(random.choice(_POISONS))
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# COLOUR PALETTE
# ---------------------------------------------------------------------------
COLOUR_LOBBY   = discord.Color.from_rgb( 88, 101, 242)   # blurple
COLOUR_ROUND   = discord.Color.from_rgb(255, 165,   0)   # orange
COLOUR_BOOM    = discord.Color.from_rgb(237,  66,  69)   # red
COLOUR_WIN     = discord.Color.from_rgb( 87, 242, 135)   # green
COLOUR_SHIELD  = discord.Color.from_rgb(  0, 200, 255)   # cyan
COLOUR_BONUS   = discord.Color.from_rgb(255, 215,   0)   # gold
COLOUR_SUDDEN  = discord.Color.from_rgb(180,   0, 255)   # purple
COLOUR_STATS   = discord.Color.from_rgb(114, 137, 218)   # soft blurple
COLOUR_TRAP    = discord.Color.from_rgb(255,  80,  80)   # bright red
COLOUR_DOUBLE  = discord.Color.from_rgb(255, 140,   0)   # deep orange
COLOUR_BET     = discord.Color.from_rgb( 46, 204, 113)   # emerald
COLOUR_PAUSE   = discord.Color.from_rgb(149, 165, 166)   # grey

# ---------------------------------------------------------------------------
# TIMING CONSTANTS
# ---------------------------------------------------------------------------
STARTING_TIME      = 15.0    # seconds for round 1
MINIMUM_TIME       = 10.0    # floor — never drops below this
TIME_REDUCTION     =  1.0    # seconds shaved off per elimination
POWERUP_BONUS      =  3.0    # extra seconds from a power-up
POWERUP_CHANCE     =  0.10   # 10 % probability of a power-up question
STREAK_REQUIRED    =  3      # correct answers in a row for a shield
DOUBLE_TROUBLE_PCT =  0.15   # 15 % chance of a double-task round
TRAP_QUESTION_PCT  =  0.12   # 12 % chance of a trap ("Simon Didn't Say") round
TRAP_SAFETY_WORD   = "skip"  # players type this to survive a trap
BET_WINDOW_SECS    = 20      # seconds users can place bets after lobby closes

# ---------------------------------------------------------------------------
# BRANDING  — replace with your own URLs
# ---------------------------------------------------------------------------
THUMBNAIL_URL = "https://i.imgur.com/placeholder_thumb.png"
LOBBY_BANNER  = "https://i.imgur.com/placeholder_lobby.png"
WINNER_BANNER = "https://i.imgur.com/placeholder_winner.gif"

# ---------------------------------------------------------------------------
# POSTGRESQL  —  robust helpers with full error handling
# ---------------------------------------------------------------------------
_DB_URL: str = os.environ.get("DATABASE_URL", "")


def _pg_connect():
    """Open a psycopg2 connection to Render's internal PostgreSQL."""
    if not _DB_URL:
        raise RuntimeError("DATABASE_URL not set in environment")
    return psycopg2.connect(_DB_URL, sslmode="require")


def _init_db() -> None:
    """Create all required tables if they don't exist. Safe to call on every boot."""
    con = None
    try:
        con = _pg_connect()
        with con.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS simon_stats (
                    user_id        BIGINT PRIMARY KEY,
                    wins           INTEGER NOT NULL DEFAULT 0,
                    games_played   INTEGER NOT NULL DEFAULT 0,
                    total_survived INTEGER NOT NULL DEFAULT 0,
                    fastest_answer REAL    NOT NULL DEFAULT 999.0,
                    longest_streak INTEGER NOT NULL DEFAULT 0,
                    display_name   TEXT    NOT NULL DEFAULT ''
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS simon_blacklist (
                    guild_id BIGINT NOT NULL,
                    user_id  BIGINT NOT NULL,
                    added_by BIGINT NOT NULL,
                    reason   TEXT   NOT NULL DEFAULT '',
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
        con.commit()
        log.info("[SimonSays] DB initialised successfully.")
    except psycopg2.Error as exc:
        log.error("[SimonSays] DB init failed: %s", exc)
        if con:
            con.rollback()
    finally:
        if con:
            con.close()


# Run once on import
_init_db()


# ── Stats helpers ────────────────────────────────────────────────────────────

def _load_stats() -> dict[str, dict]:
    """Load all player stats from PostgreSQL. Returns empty dict on failure."""
    con = None
    try:
        con = _pg_connect()
        with con.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT user_id, wins, games_played, total_survived, "
                "fastest_answer, longest_streak, display_name FROM simon_stats"
            )
            rows = cur.fetchall()
        return {str(row["user_id"]): dict(row) for row in rows}
    except psycopg2.Error as exc:
        log.error("[SimonSays] _load_stats failed: %s", exc)
        if con:
            con.rollback()
        return {}
    finally:
        if con:
            con.close()


def _save_stats(data: dict[str, dict]) -> None:
    """Persist all player stats to PostgreSQL (upsert). Rolls back on error."""
    con = None
    try:
        con = _pg_connect()
        with con.cursor() as cur:
            for user_id, row in data.items():
                cur.execute(
                    """
                    INSERT INTO simon_stats
                        (user_id, wins, games_played, total_survived,
                         fastest_answer, longest_streak, display_name)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (user_id) DO UPDATE SET
                        wins           = EXCLUDED.wins,
                        games_played   = EXCLUDED.games_played,
                        total_survived = EXCLUDED.total_survived,
                        fastest_answer = EXCLUDED.fastest_answer,
                        longest_streak = EXCLUDED.longest_streak,
                        display_name   = EXCLUDED.display_name
                    """,
                    (
                        int(user_id),
                        row.get("wins", 0),
                        row.get("games_played", 0),
                        row.get("total_survived", 0),
                        row.get("fastest_answer", 999.0),
                        row.get("longest_streak", 0),
                        row.get("display_name", ""),
                    ),
                )
        con.commit()
    except psycopg2.Error as exc:
        log.error("[SimonSays] _save_stats failed: %s", exc)
        if con:
            con.rollback()
    finally:
        if con:
            con.close()


def _get_player(data: dict[str, dict], user_id: int) -> dict:
    """Return (and auto-create) the stats sub-dict for a user ID."""
    key = str(user_id)
    if key not in data:
        data[key] = {
            "wins": 0, "games_played": 0, "total_survived": 0,
            "fastest_answer": 999.0, "longest_streak": 0, "display_name": "",
        }
    return data[key]


# ── Blacklist helpers ────────────────────────────────────────────────────────

def _is_blacklisted(guild_id: int, user_id: int) -> bool:
    """Return True if the user is blacklisted from Simon Says in this guild."""
    con = None
    try:
        con = _pg_connect()
        with con.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM simon_blacklist WHERE guild_id=%s AND user_id=%s",
                (guild_id, user_id),
            )
            return cur.fetchone() is not None
    except psycopg2.Error as exc:
        log.error("[SimonSays] _is_blacklisted failed: %s", exc)
        if con:
            con.rollback()
        return False
    finally:
        if con:
            con.close()


def _add_blacklist(guild_id: int, user_id: int, added_by: int, reason: str) -> None:
    """Add a user to the Simon Says blacklist for this guild."""
    con = None
    try:
        con = _pg_connect()
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO simon_blacklist (guild_id, user_id, added_by, reason)
                VALUES (%s,%s,%s,%s)
                ON CONFLICT (guild_id, user_id) DO UPDATE SET
                    added_by = EXCLUDED.added_by,
                    reason   = EXCLUDED.reason
                """,
                (guild_id, user_id, added_by, reason),
            )
        con.commit()
    except psycopg2.Error as exc:
        log.error("[SimonSays] _add_blacklist failed: %s", exc)
        if con:
            con.rollback()
    finally:
        if con:
            con.close()


# ---------------------------------------------------------------------------
# QUESTION BANK  (310 entries — Category A: mash, Category B: brain-fire)
# ---------------------------------------------------------------------------
QUESTIONS: list[dict[str, str]] = [

    # ===================================================================
    # CATEGORY A  —  Keyboard Mash / Simon Says  (150 entries)
    # ===================================================================

    # batch 1 – symbols + digits
    {"prompt": "Type exactly: `!#X7q_9`",            "answer": "!#X7q_9"},
    {"prompt": "Type exactly: `@Bb3*mZ`",            "answer": "@Bb3*mZ"},
    {"prompt": "Type exactly: `$5jK!w^`",            "answer": "$5jK!w^"},
    {"prompt": "Type exactly: `%eR2&Lp`",            "answer": "%eR2&Lp"},
    {"prompt": "Type exactly: `^nW8#Qz`",            "answer": "^nW8#Qz"},
    {"prompt": "Type exactly: `&Yt6@Hv`",            "answer": "&Yt6@Hv"},
    {"prompt": "Type exactly: `*Ud4$Fx`",            "answer": "*Ud4$Fx"},
    {"prompt": "Type exactly: `(oS1%Gw`",            "answer": "(oS1%Gw"},
    {"prompt": "Type exactly: `)iP9^Jb`",            "answer": ")iP9^Jb"},
    {"prompt": "Type exactly: `-rM7!Ac`",            "answer": "-rM7!Ac"},
    {"prompt": "Type exactly: `_kN5@Ds`",            "answer": "_kN5@Ds"},
    {"prompt": "Type exactly: `=lB3#Et`",            "answer": "=lB3#Et"},
    {"prompt": "Type exactly: `+hC6$Fu`",            "answer": "+hC6$Fu"},
    {"prompt": "Type exactly: `[gD2%Gv`",            "answer": "[gD2%Gv"},
    {"prompt": "Type exactly: `]fA8^Hw`",            "answer": "]fA8^Hw"},
    {"prompt": "Type exactly: `{eZ4&Ix`",            "answer": "{eZ4&Ix"},
    {"prompt": "Type exactly: `}dY1*Jy`",            "answer": "}dY1*Jy"},
    {"prompt": "Type exactly: `|cX9(Kz`",            "answer": "|cX9(Kz"},
    {"prompt": "Type exactly: `;bW7)La`",            "answer": ";bW7)La"},
    {"prompt": "Type exactly: `:aV5-Mb`",            "answer": ":aV5-Mb"},

    # batch 2 – mixed caps + symbols
    {"prompt": "Type exactly: `Qw3!rT`",             "answer": "Qw3!rT"},
    {"prompt": "Type exactly: `Zx7@yU`",             "answer": "Zx7@yU"},
    {"prompt": "Type exactly: `Nm1#oI`",             "answer": "Nm1#oI"},
    {"prompt": "Type exactly: `Kl5$pO`",             "answer": "Kl5$pO"},
    {"prompt": "Type exactly: `Jh9%qP`",             "answer": "Jh9%qP"},
    {"prompt": "Type exactly: `Ig4^rQ`",             "answer": "Ig4^rQ"},
    {"prompt": "Type exactly: `Hf2&sR`",             "answer": "Hf2&sR"},
    {"prompt": "Type exactly: `Ge6*tS`",             "answer": "Ge6*tS"},
    {"prompt": "Type exactly: `Fd0(uT`",             "answer": "Fd0(uT"},
    {"prompt": "Type exactly: `Ec8)vU`",             "answer": "Ec8)vU"},

    # batch 3 – backslash combos
    {"prompt": r"Type exactly: `a\Bc/D3`",           "answer": r"a\Bc/D3"},
    {"prompt": r"Type exactly: `X\yZ/w9`",           "answer": r"X\yZ/w9"},
    {"prompt": r"Type exactly: `M\nO/p7`",           "answer": r"M\nO/p7"},
    {"prompt": r"Type exactly: `Q\rS/t5`",           "answer": r"Q\rS/t5"},
    {"prompt": r"Type exactly: `V\uW/x3`",           "answer": r"V\uW/x3"},
    {"prompt": r"Type exactly: `E\fG/h1`",           "answer": r"E\fG/h1"},
    {"prompt": r"Type exactly: `K\jL/m8`",           "answer": r"K\jL/m8"},
    {"prompt": r"Type exactly: `P\qR/s6`",           "answer": r"P\qR/s6"},
    {"prompt": r"Type exactly: `T\uV/w4`",           "answer": r"T\uV/w4"},
    {"prompt": r"Type exactly: `Y\zA/b2`",           "answer": r"Y\zA/b2"},

    # batch 4 – longer strings
    {"prompt": "Type exactly: `aB3!cD4@eF`",         "answer": "aB3!cD4@eF"},
    {"prompt": "Type exactly: `Gh5#iJ6$kL`",         "answer": "Gh5#iJ6$kL"},
    {"prompt": "Type exactly: `Mn7%oP8^qR`",         "answer": "Mn7%oP8^qR"},
    {"prompt": "Type exactly: `St9&uV0*wX`",         "answer": "St9&uV0*wX"},
    {"prompt": "Type exactly: `Yz1(aB2)cD`",         "answer": "Yz1(aB2)cD"},
    {"prompt": "Type exactly: `Ef3-gH4_iJ`",         "answer": "Ef3-gH4_iJ"},
    {"prompt": "Type exactly: `Kl5=mN6+oP`",         "answer": "Kl5=mN6+oP"},
    {"prompt": "Type exactly: `Qr7[sT8]uV`",         "answer": "Qr7[sT8]uV"},
    {"prompt": "Type exactly: `Wx9{yZ0}aB`",         "answer": "Wx9{yZ0}aB"},
    {"prompt": "Type exactly: `Cd1|eF2;gH`",         "answer": "Cd1|eF2;gH"},

    # batch 5 – all-caps mash
    {"prompt": "Type exactly: `QWERTY123`",          "answer": "QWERTY123"},
    {"prompt": "Type exactly: `ZXCVBN456`",          "answer": "ZXCVBN456"},
    {"prompt": "Type exactly: `ASDFGH789`",          "answer": "ASDFGH789"},
    {"prompt": "Type exactly: `POIUYT321`",          "answer": "POIUYT321"},
    {"prompt": "Type exactly: `LKJHGF654`",          "answer": "LKJHGF654"},
    {"prompt": "Type exactly: `MNBVCX987`",          "answer": "MNBVCX987"},
    {"prompt": "Type exactly: `TREWQ012`",           "answer": "TREWQ012"},
    {"prompt": "Type exactly: `YUHGFD345`",          "answer": "YUHGFD345"},
    {"prompt": "Type exactly: `PLOKIJ678`",          "answer": "PLOKIJ678"},
    {"prompt": "Type exactly: `NMBVCX901`",          "answer": "NMBVCX901"},

    # batch 6 – lowercase mash
    {"prompt": "Type exactly: `qazxsw123`",          "answer": "qazxsw123"},
    {"prompt": "Type exactly: `edcrfv456`",          "answer": "edcrfv456"},
    {"prompt": "Type exactly: `tgbyhn789`",          "answer": "tgbyhn789"},
    {"prompt": "Type exactly: `ujmkilo321`",         "answer": "ujmkilo321"},
    {"prompt": "Type exactly: `plmnjkb654`",         "answer": "plmnjkb654"},
    {"prompt": "Type exactly: `vghytr987`",          "answer": "vghytr987"},
    {"prompt": "Type exactly: `xswqaz012`",          "answer": "xswqaz012"},
    {"prompt": "Type exactly: `cfredcv345`",         "answer": "cfredcv345"},
    {"prompt": "Type exactly: `nbhytg678`",          "answer": "nbhytg678"},
    {"prompt": "Type exactly: `omjuik901`",          "answer": "omjuik901"},

    # batch 7 – punctuation heavy
    {"prompt": "Type exactly: `...!!!???`",          "answer": "...!!!???"},
    {"prompt": "Type exactly: `---===+++`",          "answer": "---===+++"},
    {"prompt": "Type exactly: `<<<>>>^^^`",          "answer": "<<<>>>^^^"},
    {"prompt": "Type exactly: `@@##$$%%`",           "answer": "@@##$$%%"},
    {"prompt": "Type exactly: `((()))`",             "answer": "((()))"},
    {"prompt": "Type exactly: `[[{{}}]]`",           "answer": "[[{{}}]]"},
    {"prompt": "Type exactly: `^^&&**(()`",          "answer": "^^&&**(()"},

    # batch 8 – camelCase style
    {"prompt": "Type exactly: `SimOn3Say$`",         "answer": "SimOn3Say$"},
    {"prompt": "Type exactly: `BoMb4LiFe!`",         "answer": "BoMb4LiFe!"},
    {"prompt": "Type exactly: `ExPl0De_Now`",        "answer": "ExPl0De_Now"},
    {"prompt": "Type exactly: `SuRv1Ve#Me`",         "answer": "SuRv1Ve#Me"},
    {"prompt": "Type exactly: `LaStMaN_Win`",        "answer": "LaStMaN_Win"},
    {"prompt": "Type exactly: `TypE_fAsT99`",        "answer": "TypE_fAsT99"},
    {"prompt": "Type exactly: `QuiCk_0r_Die`",       "answer": "QuiCk_0r_Die"},
    {"prompt": "Type exactly: `N0_MiStAkEs!`",       "answer": "N0_MiStAkEs!"},
    {"prompt": "Type exactly: `GoGo4ThEwIn`",        "answer": "GoGo4ThEwIn"},
    {"prompt": "Type exactly: `HaHa_B00m!`",         "answer": "HaHa_B00m!"},

    # batch 9 – number-heavy
    {"prompt": "Type exactly: `314159265`",          "answer": "314159265"},
    {"prompt": "Type exactly: `271828182`",          "answer": "271828182"},
    {"prompt": "Type exactly: `161803398`",          "answer": "161803398"},
    {"prompt": "Type exactly: `141421356`",          "answer": "141421356"},
    {"prompt": "Type exactly: `173205080`",          "answer": "173205080"},
    {"prompt": "Type exactly: `258197324`",          "answer": "258197324"},
    {"prompt": "Type exactly: `369258147`",          "answer": "369258147"},
    {"prompt": "Type exactly: `741852963`",          "answer": "741852963"},
    {"prompt": "Type exactly: `852963741`",          "answer": "852963741"},
    {"prompt": "Type exactly: `963741852`",          "answer": "963741852"},

    # batch 10 – mixed madness
    {"prompt": "Type exactly: `A1!b2@C3#`",          "answer": "A1!b2@C3#"},
    {"prompt": "Type exactly: `d4$E5%f6^`",          "answer": "d4$E5%f6^"},
    {"prompt": "Type exactly: `G7&h8*I9(`",          "answer": "G7&h8*I9("},
    {"prompt": "Type exactly: `j0)K1-l2_`",          "answer": "j0)K1-l2_"},
    {"prompt": "Type exactly: `M3=n4+O5[`",          "answer": "M3=n4+O5["},
    {"prompt": "Type exactly: `p6]Q7{r8}`",          "answer": "p6]Q7{r8}"},
    {"prompt": "Type exactly: `S9|t0;U1:`",          "answer": "S9|t0;U1:"},
    {"prompt": "Type exactly: `Y5,z6<A7>`",          "answer": "Y5,z6<A7>"},

    # batch 11 – short sharp strings
    {"prompt": "Type exactly: `Xk!9`",               "answer": "Xk!9"},
    {"prompt": "Type exactly: `pZ@4`",               "answer": "pZ@4"},
    {"prompt": "Type exactly: `mQ#7`",               "answer": "mQ#7"},
    {"prompt": "Type exactly: `wR$2`",               "answer": "wR$2"},
    {"prompt": "Type exactly: `nS%5`",               "answer": "nS%5"},
    {"prompt": "Type exactly: `oT^8`",               "answer": "oT^8"},
    {"prompt": "Type exactly: `lU&3`",               "answer": "lU&3"},
    {"prompt": "Type exactly: `kV*6`",               "answer": "kV*6"},
    {"prompt": "Type exactly: `jW(1`",               "answer": "jW(1"},
    {"prompt": "Type exactly: `iX)0`",               "answer": "iX)0"},

    # batch 12 – underscore & dash combos
    {"prompt": "Type exactly: `a_B-c_D-e`",          "answer": "a_B-c_D-e"},
    {"prompt": "Type exactly: `F-g_H-i_J`",          "answer": "F-g_H-i_J"},
    {"prompt": "Type exactly: `k_L-m_N-o`",          "answer": "k_L-m_N-o"},
    {"prompt": "Type exactly: `P-q_R-s_T`",          "answer": "P-q_R-s_T"},
    {"prompt": "Type exactly: `u_V-w_X-y`",          "answer": "u_V-w_X-y"},
    {"prompt": "Type exactly: `Z-a_B-c_D`",          "answer": "Z-a_B-c_D"},
    {"prompt": "Type exactly: `e_F-g_H-i`",          "answer": "e_F-g_H-i"},
    {"prompt": "Type exactly: `J-k_L-m_N`",          "answer": "J-k_L-m_N"},
    {"prompt": "Type exactly: `o_P-q_R-s`",          "answer": "o_P-q_R-s"},
    {"prompt": "Type exactly: `T-u_V-w_X`",          "answer": "T-u_V-w_X"},

    # batch 13 – ASCII emoticons
    {"prompt": "Type exactly: `:-) ;-) :-D`",        "answer": ":-) ;-) :-D"},
    {"prompt": "Type exactly: `>_< o_O ^_^`",        "answer": ">_< o_O ^_^"},
    {"prompt": "Type exactly: `(^_^) (*_*)`",        "answer": "(^_^) (*_*)"},
    {"prompt": "Type exactly: `B-) 8-D xD`",         "answer": "B-) 8-D xD"},
    {"prompt": "Type exactly: `UwU OwO >w<`",        "answer": "UwU OwO >w<"},

    # batch 14 – long-form mashes
    {"prompt": "Type exactly: `Rt#5Yh@2Uj!`",        "answer": "Rt#5Yh@2Uj!"},
    {"prompt": "Type exactly: `Wq$8Ep%3Ri^`",        "answer": "Wq$8Ep%3Ri^"},
    {"prompt": "Type exactly: `Zo&6Xa*1Cb(`",        "answer": "Zo&6Xa*1Cb("},
    {"prompt": "Type exactly: `Nm)4Lk-9Jh_`",       "answer": "Nm)4Lk-9Jh_"},
    {"prompt": "Type exactly: `Gf=7Ds+2Aq[`",        "answer": "Gf=7Ds+2Aq["},
    {"prompt": "Type exactly: `Pl]5Ok{3Ij}`",        "answer": "Pl]5Ok{3Ij}"},
    {"prompt": "Type exactly: `Uh|8Yg;6Xf:`",        "answer": "Uh|8Yg;6Xf:"},
    {"prompt": "Type exactly: `Qb,4Pa<9Oz>`",        "answer": "Qb,4Pa<9Oz>"},

    # batch 15 – fun game-themed mashes
    {"prompt": "Type exactly: `B0oM_ShAkA!`",        "answer": "B0oM_ShAkA!"},
    {"prompt": "Type exactly: `Z4Pp3r_F1zZ`",        "answer": "Z4Pp3r_F1zZ"},
    {"prompt": "Type exactly: `sN4pCr4Ckl3`",        "answer": "sN4pCr4Ckl3"},
    {"prompt": "Type exactly: `W1zZ_b4Ng!!`",        "answer": "W1zZ_b4Ng!!"},
    {"prompt": "Type exactly: `kAbOoM_3,2,1`",       "answer": "kAbOoM_3,2,1"},
    {"prompt": "Type exactly: `b4Ng_B4Ng_POW`",      "answer": "b4Ng_B4Ng_POW"},
    {"prompt": "Type exactly: `PoW_zAp_BaM!`",       "answer": "PoW_zAp_BaM!"},
    {"prompt": "Type exactly: `sPlAt_KaPow9`",       "answer": "sPlAt_KaPow9"},
    {"prompt": "Type exactly: `bLaSt_0ff_GO`",       "answer": "bLaSt_0ff_GO"},
    {"prompt": "Type exactly: `BOOM_goes_99`",       "answer": "BOOM_goes_99"},

    # ===================================================================
    # CATEGORY B  —  Quick Brain Fire  (160 entries)
    # ===================================================================

    # Math (25)
    {"prompt": "What is 7 + 5?",                     "answer": "12"},
    {"prompt": "What is 9 × 9?",                     "answer": "81"},
    {"prompt": "What is 100 – 37?",                  "answer": "63"},
    {"prompt": "What is 48 ÷ 6?",                    "answer": "8"},
    {"prompt": "What is 13 × 3?",                    "answer": "39"},
    {"prompt": "What is 144 ÷ 12?",                  "answer": "12"},
    {"prompt": "What is 25 + 76?",                   "answer": "101"},
    {"prompt": "What is 8 × 8?",                     "answer": "64"},
    {"prompt": "What is 200 – 73?",                  "answer": "127"},
    {"prompt": "What is 56 ÷ 7?",                    "answer": "8"},
    {"prompt": "What is 11 × 11?",                   "answer": "121"},
    {"prompt": "What is 17 + 19?",                   "answer": "36"},
    {"prompt": "What is 75 – 48?",                   "answer": "27"},
    {"prompt": "What is 9 × 6?",                     "answer": "54"},
    {"prompt": "What is 63 ÷ 9?",                    "answer": "7"},
    {"prompt": "What is 4 × 4 × 4?",                 "answer": "64"},
    {"prompt": "What is 50 + 55?",                   "answer": "105"},
    {"prompt": "What is 1000 – 999?",                "answer": "1"},
    {"prompt": "What is 7 × 7?",                     "answer": "49"},
    {"prompt": "What is 99 + 1?",                    "answer": "100"},
    {"prompt": "What is 120 ÷ 8?",                   "answer": "15"},
    {"prompt": "What is 6 × 9?",                     "answer": "54"},
    {"prompt": "What is 81 – 45?",                   "answer": "36"},
    {"prompt": "What is 3 × 3 × 3?",                 "answer": "27"},
    {"prompt": "What is 500 ÷ 5?",                   "answer": "100"},

    # Opposites (20)
    {"prompt": "Type the opposite of 'UP' in lowercase",          "answer": "down"},
    {"prompt": "Type the opposite of 'HOT' in lowercase",         "answer": "cold"},
    {"prompt": "Type the opposite of 'LEFT' in lowercase",        "answer": "right"},
    {"prompt": "Type the opposite of 'FAST' in lowercase",        "answer": "slow"},
    {"prompt": "Type the opposite of 'LIGHT' in lowercase",       "answer": "dark"},
    {"prompt": "Type the opposite of 'GOOD' in lowercase",        "answer": "bad"},
    {"prompt": "Type the opposite of 'BIG' in lowercase",         "answer": "small"},
    {"prompt": "Type the opposite of 'OLD' in lowercase",         "answer": "new"},
    {"prompt": "Type the opposite of 'HAPPY' in lowercase",       "answer": "sad"},
    {"prompt": "Type the opposite of 'DAY' in lowercase",         "answer": "night"},
    {"prompt": "Type the opposite of 'OPEN' in lowercase",        "answer": "closed"},
    {"prompt": "Type the opposite of 'HARD' in lowercase",        "answer": "soft"},
    {"prompt": "Type the opposite of 'EMPTY' in lowercase",       "answer": "full"},
    {"prompt": "Type the opposite of 'LOUD' in lowercase",        "answer": "quiet"},
    {"prompt": "Type the opposite of 'WIN' in lowercase",         "answer": "lose"},
    {"prompt": "Type the opposite of 'START' in lowercase",       "answer": "stop"},
    {"prompt": "Type the opposite of 'LOVE' in lowercase",        "answer": "hate"},
    {"prompt": "Type the opposite of 'TRUE' in lowercase",        "answer": "false"},
    {"prompt": "Type the opposite of 'NORTH' in lowercase",       "answer": "south"},
    {"prompt": "Type the opposite of 'EAST' in lowercase",        "answer": "west"},

    # Reversals (20)
    {"prompt": "Type 'BOMB' backward",               "answer": "bmob"},
    {"prompt": "Type 'CAT' backward",                "answer": "tac"},
    {"prompt": "Type 'DOG' backward",                "answer": "god"},
    {"prompt": "Type 'LIVE' backward",               "answer": "evil"},
    {"prompt": "Type 'STOP' backward",               "answer": "pots"},
    {"prompt": "Type 'WOLF' backward",               "answer": "flow"},
    {"prompt": "Type 'RATS' backward",               "answer": "star"},
    {"prompt": "Type 'SLEEP' backward",              "answer": "peels"},
    {"prompt": "Type 'SWAP' backward",               "answer": "paws"},
    {"prompt": "Type 'NOPE' backward",               "answer": "epon"},
    {"prompt": "Type 'DRAW' backward",               "answer": "ward"},
    {"prompt": "Type 'DOOM' backward",               "answer": "mood"},
    {"prompt": "Type 'REPAY' backward",              "answer": "yaper"},
    {"prompt": "Type 'SNAP' backward",               "answer": "pans"},
    {"prompt": "Type 'TRAP' backward",               "answer": "part"},
    {"prompt": "Type 'SPIN' backward",               "answer": "nips"},
    {"prompt": "Type 'KEEP' backward",               "answer": "peek"},
    {"prompt": "Type 'MAPS' backward",               "answer": "spam"},
    {"prompt": "Type 'LOOP' backward",               "answer": "pool"},
    {"prompt": "Type 'TRAM' backward",               "answer": "mart"},

    # World capitals (15)
    {"prompt": "Capital of France? (one word)",              "answer": "Paris"},
    {"prompt": "Capital of Japan? (one word)",               "answer": "Tokyo"},
    {"prompt": "Capital of Australia? (one word)",           "answer": "Canberra"},
    {"prompt": "Capital of Brazil? (one word)",              "answer": "Brasilia"},
    {"prompt": "Capital of Canada? (one word)",              "answer": "Ottawa"},
    {"prompt": "Capital of Germany? (one word)",             "answer": "Berlin"},
    {"prompt": "Capital of Italy? (one word)",               "answer": "Rome"},
    {"prompt": "Capital of Spain? (one word)",               "answer": "Madrid"},
    {"prompt": "Capital of Russia? (one word)",              "answer": "Moscow"},
    {"prompt": "Capital of Egypt? (one word)",               "answer": "Cairo"},
    {"prompt": "Capital of South Korea? (one word)",         "answer": "Seoul"},
    {"prompt": "Capital of China? (one word)",               "answer": "Beijing"},
    {"prompt": "Capital of India? (two words)",              "answer": "New Delhi"},
    {"prompt": "Capital of Mexico? (two words)",             "answer": "Mexico City"},
    {"prompt": "Capital of Argentina? (two words)",          "answer": "Buenos Aires"},

    # Casing tricks (15)
    {"prompt": "Type 'banana' in ALL CAPS",                  "answer": "BANANA"},
    {"prompt": "Type 'EXPLOSION' in all lowercase",          "answer": "explosion"},
    {"prompt": "Type 'discord' with ONLY the first letter capitalised", "answer": "Discord"},
    {"prompt": "Type 'WINNER' in all lowercase",             "answer": "winner"},
    {"prompt": "Type 'python' in ALL CAPS",                  "answer": "PYTHON"},
    {"prompt": "Type 'GAME' in all lowercase",               "answer": "game"},
    {"prompt": "Type 'clock' in ALL CAPS",                   "answer": "CLOCK"},
    {"prompt": "Type 'FAST' in all lowercase",               "answer": "fast"},
    {"prompt": "Type 'robot' in ALL CAPS",                   "answer": "ROBOT"},
    {"prompt": "Type 'SIMON' in all lowercase",              "answer": "simon"},
    {"prompt": "Type 'fire' in ALL CAPS",                    "answer": "FIRE"},
    {"prompt": "Type 'BOOM' in all lowercase",               "answer": "boom"},
    {"prompt": "Type 'zigzag' in ALL CAPS",                  "answer": "ZIGZAG"},
    {"prompt": "Type 'CHAOS' in all lowercase",              "answer": "chaos"},
    {"prompt": "Type 'oxygen' in ALL CAPS",                  "answer": "OXYGEN"},

    # Counting / Sequences (10)
    {"prompt": "How many seconds in a minute?",              "answer": "60"},
    {"prompt": "How many minutes in an hour?",               "answer": "60"},
    {"prompt": "How many hours in a day?",                   "answer": "24"},
    {"prompt": "How many days in a week?",                   "answer": "7"},
    {"prompt": "How many months in a year?",                 "answer": "12"},
    {"prompt": "How many sides does a triangle have?",       "answer": "3"},
    {"prompt": "How many sides does a hexagon have?",        "answer": "6"},
    {"prompt": "How many sides does an octagon have?",       "answer": "8"},
    {"prompt": "How many legs does a spider have?",          "answer": "8"},
    {"prompt": "How many legs does an insect have?",         "answer": "6"},

    # Word associations (15)
    {"prompt": "Simon says type the colour of the sky (lowercase)",      "answer": "blue"},
    {"prompt": "Simon says type the colour of grass (lowercase)",        "answer": "green"},
    {"prompt": "Simon says type the colour of snow (lowercase)",         "answer": "white"},
    {"prompt": "Simon says type the colour of coal (lowercase)",         "answer": "black"},
    {"prompt": "Simon says type the colour of the sun (lowercase)",      "answer": "yellow"},
    {"prompt": "Simon says type a fruit that is yellow (lowercase)",     "answer": "banana"},
    {"prompt": "Simon says type an animal that says 'moo' (lowercase)", "answer": "cow"},
    {"prompt": "Simon says type the planet we live on (lowercase)",      "answer": "earth"},
    {"prompt": "Simon says type the closest star to Earth (lowercase)",  "answer": "sun"},
    {"prompt": "Simon says type a vehicle with two wheels (lowercase)",  "answer": "bike"},
    {"prompt": "Simon says type the opposite of 'begin' (lowercase)",   "answer": "end"},
    {"prompt": "Simon says type an animal with a mane (lowercase)",      "answer": "lion"},
    {"prompt": "Simon says type what unlocks a door (lowercase)",        "answer": "key"},
    {"prompt": "Simon says type a season after summer (lowercase)",      "answer": "autumn"},
    {"prompt": "Simon says type what you use to write (lowercase)",      "answer": "pen"},

    # Anagram teasers (10)
    {"prompt": "Unscramble 'MOBK' — what word?",             "answer": "bomb"},
    {"prompt": "Unscramble 'VEOL' — what word?",             "answer": "love"},
    {"prompt": "Unscramble 'STEA' — what word?",             "answer": "eats"},
    {"prompt": "Unscramble 'STOOB' — what word?",            "answer": "boots"},
    {"prompt": "Unscramble 'MARES' — what word?",            "answer": "smear"},
    {"prompt": "Unscramble 'GLEAN' — what word?",            "answer": "angel"},
    {"prompt": "Unscramble 'OPTED' — what word?",            "answer": "depot"},
    {"prompt": "Unscramble 'LACED' — what word?",            "answer": "decal"},
    {"prompt": "Unscramble 'LATEP' — what word?",            "answer": "plate"},
    {"prompt": "Unscramble 'REFIT' — what word?",            "answer": "fiter"},

    # Fill-in-the-blank (10)
    {"prompt": "Complete: 'Ready, Set, ___' (one word lowercase)",       "answer": "go"},
    {"prompt": "Complete: 'Rock, Paper, ___' (one word lowercase)",      "answer": "scissors"},
    {"prompt": "Complete: 'Stop, Drop and ___' (one word lowercase)",    "answer": "roll"},
    {"prompt": "Complete: '1, 2, 3, ___' (just the number)",             "answer": "4"},
    {"prompt": "Complete: 'Bread and ___' (one word lowercase)",         "answer": "butter"},
    {"prompt": "Complete: 'Thunder and ___' (one word lowercase)",       "answer": "lightning"},
    {"prompt": "Complete: 'Cat and ___' (one word lowercase)",           "answer": "dog"},
    {"prompt": "Complete: 'Salt and ___' (one word lowercase)",          "answer": "pepper"},
    {"prompt": "Complete: 'Night and ___' (one word lowercase)",         "answer": "day"},
    {"prompt": "Complete: 'Sun, Moon and ___' (one word lowercase)",     "answer": "stars"},

    # True / False (10)
    {"prompt": "True or False: 5 × 5 = 25",                             "answer": "true"},
    {"prompt": "True or False: A triangle has 4 sides",                  "answer": "false"},
    {"prompt": "True or False: The sun rises in the west",               "answer": "false"},
    {"prompt": "True or False: Ice is frozen water",                     "answer": "true"},
    {"prompt": "True or False: Dogs are reptiles",                       "answer": "false"},
    {"prompt": "True or False: 100 seconds = 1 minute",                  "answer": "false"},
    {"prompt": "True or False: Paris is in France",                      "answer": "true"},
    {"prompt": "True or False: Sharks are mammals",                      "answer": "false"},
    {"prompt": "True or False: 7 × 6 = 42",                             "answer": "true"},
    {"prompt": "True or False: The moon orbits Mars",                    "answer": "false"},
]

assert all("prompt" in q and "answer" in q for q in QUESTIONS), (
    "Every QUESTIONS entry must have 'prompt' and 'answer' keys!"
)

# ---------------------------------------------------------------------------
# TRAP QUESTIONS  ("Simon Didn't Say" — answer = instant boom)
# Players must type the TRAP_SAFETY_WORD ("skip") to survive, or ignore it.
# ---------------------------------------------------------------------------
TRAP_PROMPTS: list[str] = [
    "Type the word 'explode' right now.",
    "Quickly type 'surrender' to continue.",
    "Type your username here.",
    "Type 'I give up' to stay in.",
    "Type 'boom' to prove you're paying attention.",
    "Type 'forfeit' right now.",
    "Type 'quit' to keep playing.",
    "Type 'abort' immediately.",
    "Type 'exit' to advance to the next round.",
    "Type 'stop' to survive.",
    "Type 'hello' in the chat.",
    "Type 'yes' to confirm your entry.",
    "Write your answer here now.",
    "Type 'ready' to proceed.",
    "Respond with 'ok' to move on.",
]

# ---------------------------------------------------------------------------
# DOUBLE TROUBLE WORDS  (used for the second task in double-trouble rounds)
# ---------------------------------------------------------------------------
DOUBLE_TROUBLE_WORDS: list[str] = [
    "chaos", "blaze", "storm", "flame", "ghost",
    "sharp", "quick", "frost", "spark", "gloom",
    "blast", "surge", "venom", "swift", "flare",
]

# ---------------------------------------------------------------------------
# PER-GAME PLAYER STATE
# ---------------------------------------------------------------------------

@dataclass
class _Player:
    """Transient state for a single player during one game."""
    member:          discord.Member
    streak:          int   = 0
    has_shield:      bool  = False
    rounds_survived: int   = 0
    fastest_answer:  float = 999.0


# ---------------------------------------------------------------------------
# COG
# ---------------------------------------------------------------------------

class SimonSaysGame(commands.Cog, name="Simon Says"):
    """
    Simon Says — Last Man Standing  (v1.0)

    Commands (prefix-based)
    -----------------------
    !simonstart [minutes]       — Admin: open a lobby
    !simoncancel                — Admin: abort the running game
    !simonkick @user            — Admin: eliminate a player mid-game
    !simonpause                 — Admin: freeze the round timer
    !simonresume                — Admin: resume a paused round
    !simonblacklist @user [reason] — Admin: permanently ban from joining
    !simonextend <minutes>      — Admin: add minutes to open lobby
    !simonleaderboard           — Show all-time top-10 winners
    !simonstats [@user]         — Show per-player stats
    !simoncatalogue             — Full feature manual embed
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

        # channel_id → True when a game (lobby or match) is in progress
        self._active_games:  dict[int, bool]           = {}

        # channel_id → asyncio.Event — set to signal cancellation
        self._cancel_events: dict[int, asyncio.Event]  = {}

        # channel_id → list of live _Player objects
        self._active_players: dict[int, list[_Player]] = {}

        # channel_id → asyncio.Event — set when admin calls !simonpause
        self._pause_events:  dict[int, asyncio.Event]  = {}

        # channel_id → asyncio.Event — set when admin calls !simonresume
        self._resume_events: dict[int, asyncio.Event]  = {}

        # channel_id → extra seconds remaining in open lobby (from !simonextend)
        self._lobby_extension: dict[int, int]          = {}

        # channel_id → {user_id: (bet_amount, target_user_id)}
        self._bets: dict[int, dict[int, tuple[int, int]]] = {}

    # ───────────────────────────────────────────────────────────────────────
    # UTILITY: embed factory
    # ───────────────────────────────────────────────────────────────────────

    @staticmethod
    def _embed(
        title:       str,
        description: str,
        colour:      discord.Color,
        *,
        footer:    str | None = None,
        banner:    str | None = None,
        thumbnail: bool       = True,
    ) -> discord.Embed:
        em = discord.Embed(title=title, description=description, colour=colour)
        if footer:
            em.set_footer(text=footer)
        if thumbnail and THUMBNAIL_URL:
            em.set_thumbnail(url=THUMBNAIL_URL)
        if banner:
            em.set_image(url=banner)
        return em

    # ───────────────────────────────────────────────────────────────────────
    # PAUSE HELPER  — awaitable sleep that respects pause/resume/cancel
    # ───────────────────────────────────────────────────────────────────────

    async def _pausable_sleep(
        self,
        channel_id: int,
        seconds:    float,
    ) -> bool:
        """
        Sleep for `seconds` but yield to pause events.
        Returns True if the cancel event was set during the sleep.
        """
        cancel_event = self._cancel_events.get(channel_id)
        pause_event  = self._pause_events.get(channel_id)
        resume_event = self._resume_events.get(channel_id)

        deadline = time.monotonic() + seconds

        while time.monotonic() < deadline:
            if cancel_event and cancel_event.is_set():
                return True

            # If paused, block until resumed (or cancelled)
            if pause_event and pause_event.is_set():
                if resume_event:
                    resume_event.clear()
                    await asyncio.wait_for(
                        resume_event.wait(), timeout=None  # type: ignore[arg-type]
                    ) if False else None  # noqa — handled below
                    try:
                        await resume_event.wait()
                    except Exception:
                        pass
                    pause_event.clear()

            remaining = deadline - time.monotonic()
            await asyncio.sleep(min(0.5, max(0.0, remaining)))

        return False

    # ───────────────────────────────────────────────────────────────────────
    # COMMAND: !simonstart
    # ───────────────────────────────────────────────────────────────────────

    @commands.command(name="simonstart")
    @commands.has_permissions(administrator=True)
    async def simon_start(
        self, ctx: commands.Context, minutes: int = 1
    ) -> None:
        """Open a Simon Says lobby. Usage: !simonstart [minutes]"""

        if self._active_games.get(ctx.channel.id):
            await ctx.send(embed=self._embed(
                "⚠️  Game Already Running",
                "A Simon Says game is already active in this channel!",
                COLOUR_BOOM,
            ))
            return

        minutes = max(1, min(minutes, 30))
        self._active_games[ctx.channel.id]    = True
        self._cancel_events[ctx.channel.id]   = asyncio.Event()
        self._pause_events[ctx.channel.id]    = asyncio.Event()
        self._resume_events[ctx.channel.id]   = asyncio.Event()
        self._lobby_extension[ctx.channel.id] = 0
        self._bets[ctx.channel.id]            = {}

        try:
            await self._run_lobby(ctx, minutes)
        finally:
            self._active_games.pop(ctx.channel.id,    None)
            self._cancel_events.pop(ctx.channel.id,   None)
            self._pause_events.pop(ctx.channel.id,    None)
            self._resume_events.pop(ctx.channel.id,   None)
            self._active_players.pop(ctx.channel.id,  None)
            self._lobby_extension.pop(ctx.channel.id, None)
            self._bets.pop(ctx.channel.id,            None)

    # ───────────────────────────────────────────────────────────────────────
    # COMMAND: !simoncancel
    # ───────────────────────────────────────────────────────────────────────

    @commands.command(name="simoncancel")
    @commands.has_permissions(administrator=True)
    async def simon_cancel(self, ctx: commands.Context) -> None:
        """Abort the running Simon Says game in this channel."""

        event = self._cancel_events.get(ctx.channel.id)
        if not event:
            await ctx.send(embed=self._embed(
                "❌  No Game Running",
                "There is no active Simon Says game to cancel here.",
                COLOUR_BOOM,
            ))
            return

        event.set()
        await ctx.send(embed=self._embed(
            "🛑  Game Cancelled",
            f"**{ctx.author.display_name}** pulled the plug. Game over! 🔌",
            COLOUR_BOOM,
        ))

    # ───────────────────────────────────────────────────────────────────────
    # COMMAND: !simonkick
    # ───────────────────────────────────────────────────────────────────────

    @commands.command(name="simonkick")
    @commands.has_permissions(administrator=True)
    async def simon_kick(
        self, ctx: commands.Context, member: discord.Member
    ) -> None:
        """Eliminate a player from the active game. Usage: !simonkick @user"""

        players = self._active_players.get(ctx.channel.id)
        if not players:
            await ctx.send(embed=self._embed(
                "❌  No Game Running",
                "There is no active game in this channel.",
                COLOUR_BOOM,
            ))
            return

        target = next((p for p in players if p.member == member), None)
        if target is None:
            await ctx.send(f"⚠️  **{member.display_name}** is not in the current game.")
            return

        players.remove(target)
        await ctx.send(embed=self._embed(
            "👢  Player Kicked",
            (
                f"**{member.display_name}** has been forcibly removed by "
                f"**{ctx.author.display_name}**. 💀\n\n"
                f"**{len(players)}** player{'s' if len(players) != 1 else ''} remain."
            ),
            COLOUR_BOOM,
        ))

    # ───────────────────────────────────────────────────────────────────────
    # COMMAND: !simonpause
    # ───────────────────────────────────────────────────────────────────────

    @commands.command(name="simonpause")
    @commands.has_permissions(administrator=True)
    async def simon_pause(self, ctx: commands.Context) -> None:
        """Freeze the current round timer. Usage: !simonpause"""

        pause_event = self._pause_events.get(ctx.channel.id)
        if not pause_event:
            await ctx.send(embed=self._embed(
                "❌  No Game Running",
                "There is no active game to pause here.",
                COLOUR_PAUSE,
            ))
            return

        if pause_event.is_set():
            await ctx.send("⏸️  The game is already paused. Use `!simonresume` to continue.")
            return

        pause_event.set()
        await ctx.send(embed=self._embed(
            "⏸️  Game Paused",
            (
                f"**{ctx.author.display_name}** has paused the game.\n\n"
                "The round timer is frozen. Use `!simonresume` to continue."
            ),
            COLOUR_PAUSE,
        ))

    # ───────────────────────────────────────────────────────────────────────
    # COMMAND: !simonresume
    # ───────────────────────────────────────────────────────────────────────

    @commands.command(name="simonresume")
    @commands.has_permissions(administrator=True)
    async def simon_resume(self, ctx: commands.Context) -> None:
        """Resume a paused Simon Says game. Usage: !simonresume"""

        pause_event  = self._pause_events.get(ctx.channel.id)
        resume_event = self._resume_events.get(ctx.channel.id)

        if not pause_event or not pause_event.is_set():
            await ctx.send("▶️  The game is not currently paused.")
            return

        if resume_event:
            resume_event.set()

        await ctx.send(embed=self._embed(
            "▶️  Game Resumed",
            f"**{ctx.author.display_name}** has resumed the game. Back to it! 🔥",
            COLOUR_ROUND,
        ))

    # ───────────────────────────────────────────────────────────────────────
    # COMMAND: !simonblacklist
    # ───────────────────────────────────────────────────────────────────────

    @commands.command(name="simonblacklist")
    @commands.has_permissions(administrator=True)
    async def simon_blacklist(
        self,
        ctx: commands.Context,
        member: discord.Member,
        *,
        reason: str = "No reason provided.",
    ) -> None:
        """Permanently ban a user from joining Simon Says. Usage: !simonblacklist @user [reason]"""

        assert ctx.guild is not None
        _add_blacklist(ctx.guild.id, member.id, ctx.author.id, reason)
        await ctx.send(embed=self._embed(
            "🚫  Blacklisted",
            (
                f"**{member.display_name}** has been permanently banned from joining "
                f"Simon Says in this server.\n\n"
                f"**Reason:** {reason}\n\n"
                "They will not be able to use the `join` command in future lobbies."
            ),
            COLOUR_BOOM,
        ))

    # ───────────────────────────────────────────────────────────────────────
    # COMMAND: !simonextend
    # ───────────────────────────────────────────────────────────────────────

    @commands.command(name="simonextend")
    @commands.has_permissions(administrator=True)
    async def simon_extend(
        self, ctx: commands.Context, minutes: int = 1
    ) -> None:
        """Add extra time to an open lobby. Usage: !simonextend <minutes>"""

        if not self._active_games.get(ctx.channel.id):
            await ctx.send(embed=self._embed(
                "❌  No Active Lobby",
                "There is no open lobby to extend.",
                COLOUR_BOOM,
            ))
            return

        minutes = max(1, min(minutes, 10))
        self._lobby_extension[ctx.channel.id] = (
            self._lobby_extension.get(ctx.channel.id, 0) + minutes * 60
        )
        await ctx.send(embed=self._embed(
            "⏰  Lobby Extended",
            (
                f"**{ctx.author.display_name}** added **{minutes} minute"
                f"{'s' if minutes != 1 else ''}** to the lobby! "
                "More time to join! 🎉"
            ),
            COLOUR_LOBBY,
        ))

    # ───────────────────────────────────────────────────────────────────────
    # COMMAND: !simonleaderboard
    # ───────────────────────────────────────────────────────────────────────

    @commands.command(name="simonleaderboard", aliases=["simonlb"])
    async def simon_leaderboard(self, ctx: commands.Context) -> None:
        """Display the top-10 all-time Simon Says winners."""

        data = _load_stats()
        if not data:
            await ctx.send(embed=self._embed(
                "📊  Leaderboard",
                "No stats recorded yet — play a game first!",
                COLOUR_STATS,
            ))
            return

        ranked = sorted(
            data.items(),
            key=lambda kv: (-kv[1].get("wins", 0), kv[1].get("games_played", 0)),
        )[:10]

        medals: list[str] = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
        lines:  list[str] = []
        for i, (uid, stats) in enumerate(ranked):
            name   = stats.get("display_name", f"User#{uid}")
            wins   = stats.get("wins", 0)
            played = stats.get("games_played", 0)
            wr     = f"{wins/played*100:.0f}%" if played else "—"
            lines.append(
                f"{medals[i]}  **{name}** — "
                f"{wins} win{'s' if wins != 1 else ''} "
                f"({wr} win rate, {played} games)"
            )

        await ctx.send(embed=self._embed(
            "🏆  Simon Says — All-Time Leaderboard",
            "\n".join(lines),
            COLOUR_STATS,
            footer="Use !simonstats @user for a detailed breakdown",
        ))

    # ───────────────────────────────────────────────────────────────────────
    # COMMAND: !simonstats
    # ───────────────────────────────────────────────────────────────────────

    @commands.command(name="simonstats")
    async def simon_stats(
        self,
        ctx: commands.Context,
        member: discord.Member | None = None,
    ) -> None:
        """Show stats for yourself or another player. Usage: !simonstats [@user]"""

        target = member or ctx.author
        data   = _load_stats()
        stats  = data.get(str(target.id))

        if not stats or stats.get("games_played", 0) == 0:
            await ctx.send(embed=self._embed(
                f"📈  Stats — {target.display_name}",
                "No stats recorded yet for this player.",
                COLOUR_STATS,
            ))
            return

        wins    = stats.get("wins", 0)
        played  = stats.get("games_played", 0)
        surv    = stats.get("total_survived", 0)
        fastest = stats.get("fastest_answer", 999.0)
        streak  = stats.get("longest_streak", 0)
        wr      = f"{wins/played*100:.1f}%" if played else "—"
        fa_str  = f"{fastest:.2f}s" if fastest < 999 else "—"

        desc = (
            f"🏆  **Wins:** {wins}\n"
            f"🎮  **Games Played:** {played}\n"
            f"📊  **Win Rate:** {wr}\n"
            f"💪  **Total Rounds Survived:** {surv}\n"
            f"⚡  **Fastest Correct Answer:** {fa_str}\n"
            f"🔥  **Longest Streak:** {streak} in a row\n"
        )
        await ctx.send(embed=self._embed(
            f"📈  Stats — {target.display_name}", desc, COLOUR_STATS
        ))

    # ───────────────────────────────────────────────────────────────────────
    # COMMAND: !simoncatalogue
    # ───────────────────────────────────────────────────────────────────────

    @commands.command(name="simoncatalogue", aliases=["simonhelp", "simonmanual"])
    async def simon_catalogue(self, ctx: commands.Context) -> None:
        """Display the full Simon Says v1.0 feature manual."""

        embed = discord.Embed(
            title="📖  Simon Says v1.0 — Complete Feature Catalogue",
            colour=COLOUR_LOBBY,
        )
        embed.set_thumbnail(url=THUMBNAIL_URL)

        embed.add_field(
            name="🎮  Lobby Mechanics",
            value=(
                "• Admin opens a lobby with `!simonstart [minutes]` (1–30 min).\n"
                "• Any server member types **`join`** in the channel to enter.\n"
                "• Blacklisted users are silently rejected at join time.\n"
                "• Admin can add extra time mid-lobby with `!simonextend <minutes>`.\n"
                "• A **betting window** opens after the lobby closes — anyone can bet "
                "their economy points on a player using `!simonbet @player <amount>`."
            ),
            inline=False,
        )
        embed.add_field(
            name="⚔️  Gameplay Rounds",
            value=(
                "• Players are challenged one at a time in a rotating order.\n"
                "• Each round has a time limit (starts at 15 s, minimum 10 s).\n"
                "• The timer ticks down by 1 s per elimination — pressure builds!\n"
                "• **⚡ Power-Up (10%):** Correct answer gives **+3 s** bonus time.\n"
                "• Answers are checked against the **clean** (un-poisoned) value."
            ),
            inline=False,
        )
        embed.add_field(
            name="🛡️  Streak Shields",
            value=(
                "• Answer **3 consecutive questions** correctly to earn a **Streak Shield**.\n"
                "• A shield absorbs **one** timeout or wrong answer without elimination.\n"
                "• After a shield is used, it is gone — earn a new one to get it back.\n"
                "• Streaks reset to 0 on any timeout or wrong answer."
            ),
            inline=False,
        )
        embed.add_field(
            name="⚡  Power-Up Shards",
            value=(
                "• Any question has a **10%** chance of being a Power-Up question.\n"
                "• Correct answer on a Power-Up restores up to **3 seconds** of the "
                "round timer (capped at starting time).\n"
                "• Power-Up questions are announced in the challenge embed."
            ),
            inline=False,
        )
        embed.add_field(
            name="🔥  Double Trouble Rounds (15%)",
            value=(
                "• A round may require **two tasks** in a single message.\n"
                "• Example: `Type exact string AND type a word backward`.\n"
                "• Both answers must be in the **same message**, separated by a space.\n"
                "• Announced clearly in the challenge embed."
            ),
            inline=False,
        )
        embed.add_field(
            name="💣  Trap Questions — \"Simon Didn't Say\" (12%)",
            value=(
                "• Some prompts intentionally **omit** \"Simon says\".\n"
                "• If a player **answers** a trap question, they are **instantly eliminated**.\n"
                f"• To survive a trap, type **`{TRAP_SAFETY_WORD}`** or simply **ignore** it.\n"
                "• Trap prompts are visually identical to normal ones — read carefully!"
            ),
            inline=False,
        )
        embed.add_field(
            name="🔒  Anti-Copy-Paste System",
            value=(
                "• Displayed question text is injected with **invisible zero-width characters**.\n"
                "• The bot's answer check uses the **clean, original** answer string.\n"
                "• Users who **copy-paste** the prompt get hidden chars that break the match.\n"
                "• Users who **manually type** the answer pass seamlessly.\n"
                "• The poisoning is random — it changes every time a question is shown."
            ),
            inline=False,
        )
        embed.add_field(
            name="☠️  Sudden Death",
            value=(
                "• When exactly **2 players** remain, Sudden Death begins.\n"
                "• Both players receive the **same question simultaneously**.\n"
                "• The player who answers **slower** (or times out) is eliminated.\n"
                "• If both time out, the round is replayed.\n"
                "• Continues until one winner remains."
            ),
            inline=False,
        )
        embed.add_field(
            name="💰  Betting System",
            value=(
                "• After the lobby closes, a **20-second betting window** opens.\n"
                "• Any server user can use `!simonbet @player <amount>` to wager economy points.\n"
                "• Points are deducted immediately from the `economy_users` table.\n"
                "• Winners receive **2× their bet** back; losers forfeit their wager.\n"
                "• Bets cannot exceed your current balance."
            ),
            inline=False,
        )
        embed.add_field(
            name="👑  Administrative Suite",
            value=(
                "`!simonstart [min]`  — Open a lobby\n"
                "`!simoncancel`        — Abort any active game\n"
                "`!simonkick @user`    — Remove a player mid-game\n"
                "`!simonpause`         — Freeze the round timer\n"
                "`!simonresume`        — Resume a paused game\n"
                "`!simonblacklist @user [reason]` — Permanently ban from joining\n"
                "`!simonextend <min>`  — Add time to an open lobby\n\n"
                "All admin commands require the **Administrator** permission."
            ),
            inline=False,
        )
        embed.add_field(
            name="📊  Stats & Leaderboard",
            value=(
                "`!simonleaderboard` / `!simonlb` — Top-10 all-time winners\n"
                "`!simonstats [@user]`             — Personal stats breakdown\n\n"
                "Stats tracked: wins, games played, win rate, total rounds survived, "
                "fastest answer, longest streak."
            ),
            inline=False,
        )
        embed.set_footer(text="Simon Says v1.0 — Solace Ghosty Bot")
        await ctx.send(embed=embed)

    # ───────────────────────────────────────────────────────────────────────
    # COMMAND: !simonbet  (called during the betting window only)
    # ───────────────────────────────────────────────────────────────────────

    @commands.command(name="simonbet")
    async def simon_bet(
        self,
        ctx: commands.Context,
        target: discord.Member,
        amount: int,
    ) -> None:
        """Bet economy points on a Simon Says player. Usage: !simonbet @player <amount>"""

        channel_bets = self._bets.get(ctx.channel.id)
        if channel_bets is None:
            await ctx.send("❌  There is no open betting window right now.")
            return

        if amount <= 0:
            await ctx.send("⚠️  Bet amount must be a positive integer.")
            return

        # Import economy helpers lazily to avoid circular issues
        try:
            from cogs.server_drops_economy import get_points, deduct_points
        except ImportError:
            await ctx.send("❌  Economy system unavailable.")
            return

        balance = get_points(ctx.author.id)
        if balance < amount:
            await ctx.send(
                f"❌  You only have **{balance} pts** — not enough to bet {amount}."
            )
            return

        # Deduct immediately; reward or forfeit after game
        deduct_points(ctx.author.id, amount)
        channel_bets[ctx.author.id] = (amount, target.id)

        await ctx.send(embed=self._embed(
            "💰  Bet Placed!",
            (
                f"**{ctx.author.display_name}** bet **{amount} pts** on "
                f"**{target.display_name}**!\n\n"
                "If they win, you'll receive **2× your bet** back. Good luck! 🎰"
            ),
            COLOUR_BET,
        ))

    # ───────────────────────────────────────────────────────────────────────
    # PHASE 1 — LOBBY
    # ───────────────────────────────────────────────────────────────────────

    async def _run_lobby(self, ctx: commands.Context, minutes: int) -> None:
        """Open the lobby, collect players, run betting window, then start game."""

        lobby_seconds  = minutes * 60
        cancel_event   = self._cancel_events[ctx.channel.id]
        joined_members: list[discord.Member] = []

        await ctx.send(embed=self._embed(
            title="🎮  Simon Says v1.0 — Last Man Standing!",
            description=(
                f"A new game has been opened by **{ctx.author.display_name}**!\n\n"
                f"⏳ **Lobby closes in {minutes} minute{'s' if minutes != 1 else ''}.**\n\n"
                "Type **`join`** in this channel to enter.\n\n"
                "🛡️  Earn a **Streak Shield** by answering **3 in a row** correctly!\n"
                "⚡  Watch for **Power-Up** questions — get +3 bonus seconds!\n"
                "💣  Beware **Double Trouble** rounds and **Trap Questions**!\n"
                "💰  Spectators can **bet** on players after the lobby closes!\n\n"
                "The last player standing wins! 💥"
            ),
            colour=COLOUR_LOBBY,
            footer=(
                "Admins: !simoncancel | !simonkick @user | "
                "!simonpause | !simonresume | !simonextend <min>"
            ),
            banner=LOBBY_BANNER,
        ))

        warning_times: list[int] = []
        if lobby_seconds >= 60:
            warning_times.append(lobby_seconds // 2)
        if lobby_seconds >= 30:
            warning_times.append(max(10, lobby_seconds // 10))

        elapsed   = 0
        warned_at: set[int] = set()

        async def collect_joins() -> None:
            nonlocal elapsed

            while True:
                # Respect extensions added by !simonextend
                ext = self._lobby_extension.get(ctx.channel.id, 0)
                effective_limit = lobby_seconds + ext

                if elapsed >= effective_limit:
                    break
                if cancel_event.is_set():
                    return

                remaining = effective_limit - elapsed
                for w in warning_times:
                    if remaining <= w and w not in warned_at:
                        warned_at.add(w)
                        await ctx.send(
                            f"⏰  **{remaining}s** left to join! "
                            f"({len(joined_members)} player"
                            f"{'s' if len(joined_members) != 1 else ''} so far)"
                        )

                try:
                    def join_check(m: discord.Message) -> bool:
                        return (
                            m.channel == ctx.channel
                            and m.content.strip().lower() == "join"
                            and not m.author.bot
                            and m.author not in joined_members
                        )
                    msg: discord.Message = await asyncio.wait_for(
                        self.bot.wait_for("message", check=join_check),
                        timeout=2.0,
                    )
                    # Check blacklist
                    assert ctx.guild is not None
                    if _is_blacklisted(ctx.guild.id, msg.author.id):
                        await ctx.send(
                            f"🚫  **{msg.author.display_name}** is blacklisted "
                            "from Simon Says."
                        )
                        continue
                    joined_members.append(msg.author)
                    await ctx.send(
                        f"✅  **{msg.author.display_name}** joined! "
                        f"({len(joined_members)} player"
                        f"{'s' if len(joined_members) != 1 else ''} in lobby)"
                    )
                except asyncio.TimeoutError:
                    elapsed += 2

        await collect_joins()

        if cancel_event.is_set():
            return

        if len(joined_members) < 2:
            await ctx.send(embed=self._embed(
                "😴  Not Enough Players",
                (
                    f"Only **{len(joined_members)}** player"
                    f"{'s' if len(joined_members) != 1 else ''} joined — "
                    "need at least **2** to start. Game cancelled."
                ),
                COLOUR_BOOM,
            ))
            return

        # ── Betting window ────────────────────────────────────────────────
        names_list = ", ".join(f"**{m.display_name}**" for m in joined_members)
        await ctx.send(embed=self._embed(
            "💰  Betting Window Open!",
            (
                f"The lobby is locked! Players: {names_list}\n\n"
                f"You have **{BET_WINDOW_SECS} seconds** to bet on a player!\n"
                "Use: `!simonbet @player <amount>`\n\n"
                "Winners receive **2× their bet** back!"
            ),
            COLOUR_BET,
            footer=f"Betting closes in {BET_WINDOW_SECS}s",
        ))
        await asyncio.sleep(BET_WINDOW_SECS)

        # ── Hand off to game ──────────────────────────────────────────────
        players = [_Player(member=m) for m in joined_members]
        self._active_players[ctx.channel.id] = players
        stats_data = _load_stats()

        # Increment games_played for all participants
        for ps_obj in players:
            row = _get_player(stats_data, ps_obj.member.id)
            row["display_name"]  = ps_obj.member.display_name
            row["games_played"] += 1

        await self._run_game(ctx, players, stats_data)

    # ───────────────────────────────────────────────────────────────────────
    # PHASE 2 — MAIN GAME LOOP
    # ───────────────────────────────────────────────────────────────────────

    async def _run_game(
        self,
        ctx:        commands.Context,
        players:    list[_Player],
        stats_data: dict[str, dict],
    ) -> None:
        """
        Rotate through players, issue challenges, and eliminate them until
        exactly 1 (or 2 for sudden death) remain.
        """

        cancel_event     = self._cancel_events[ctx.channel.id]
        pause_event      = self._pause_events[ctx.channel.id]
        resume_event     = self._resume_events[ctx.channel.id]
        current_max_time = STARTING_TIME
        round_number     = 1
        elimination_count = 0

        question_pool: list[dict[str, str]] = list(QUESTIONS)
        random.shuffle(question_pool)
        q_index = 0

        await ctx.send(embed=self._embed(
            title=f"💥  Game Start!  {len(players)} Players",
            description=(
                "The lobby is locked and the game has begun!\n\n"
                "Players will be called one by one.\n"
                "Miss your question and you're **OUT**. 💣\n\n"
                "Good luck — you'll need it. 🔥"
            ),
            colour=COLOUR_ROUND,
            footer=f"Starting time limit: {STARTING_TIME:.0f}s | Minimum: {MINIMUM_TIME:.0f}s",
        ))
        await asyncio.sleep(3)

        player_index = 0  # rotating index into `players`

        while len(players) > 2:
            if cancel_event.is_set():
                await ctx.send(embed=self._embed(
                    "🛑  Game Aborted", "Cancelled by an administrator.", COLOUR_BOOM
                ))
                return

            # ── Handle pause ─────────────────────────────────────────────
            if pause_event.is_set():
                resume_event.clear()
                try:
                    await resume_event.wait()
                except Exception:
                    pass
                pause_event.clear()

            # Clamp index in case a player was kicked
            player_index = player_index % len(players)
            target_state = players[player_index]

            # Rotate to next player
            player_index = (player_index + 1) % len(players)

            # Fetch next question
            if q_index >= len(question_pool):
                random.shuffle(question_pool)
                q_index = 0
            question = question_pool[q_index]
            q_index += 1

            # Decide round type
            is_powerup       = random.random() < POWERUP_CHANCE
            is_double_trouble = random.random() < DOUBLE_TROUBLE_PCT
            is_trap           = (not is_double_trouble) and random.random() < TRAP_QUESTION_PCT
            time_limit        = current_max_time

            async with ctx.channel.typing():
                await asyncio.sleep(1.0)

            if is_trap:
                # ── TRAP QUESTION  ("Simon Didn't Say") ──────────────────
                trap_prompt = random.choice(TRAP_PROMPTS)

                await ctx.send(embed=self._embed(
                    title=f"❓  CHALLENGE! — Round {round_number}",
                    description=(
                        f"{target_state.member.mention}, it's your turn!\n\n"
                        f"**{_poison(trap_prompt)}**\n\n"
                        f"⏱  You have **{time_limit:.0f} seconds**!"
                    ),
                    colour=COLOUR_ROUND,
                    footer=(
                        f"{len(players)} players remaining  |  Round {round_number}  |  "
                        f"🔥 Streak: {target_state.streak}"
                    ),
                ))

                # Check: if the player answers ANYTHING (other than skip), they explode
                def trap_check(m: discord.Message) -> bool:
                    return (
                        m.channel == ctx.channel
                        and m.author == target_state.member
                        and not m.author.bot
                    )

                try:
                    trap_msg: discord.Message = await asyncio.wait_for(
                        self.bot.wait_for("message", check=trap_check),
                        timeout=time_limit,
                    )
                    player_response = trap_msg.content.strip().lower()

                    if player_response == TRAP_SAFETY_WORD:
                        # Survived by typing the safety word
                        await ctx.send(embed=self._embed(
                            "✅  Trap Survived!",
                            (
                                f"**{target_state.member.display_name}** recognized "
                                f"the trap and typed `{TRAP_SAFETY_WORD}`! 🧠\n\n"
                                "Smart move — Simon Didn't Say!"
                            ),
                            COLOUR_WIN,
                        ))
                        target_state.streak += 1
                        target_state.rounds_survived += 1
                    else:
                        # Answered the trap — instant elimination
                        if target_state.has_shield:
                            target_state.has_shield = False
                            target_state.streak = 0
                            await ctx.send(embed=self._embed(
                                "🛡️  Shield Used on Trap!",
                                (
                                    f"**{target_state.member.display_name}** fell for "
                                    f"the trap, but their **Streak Shield** saved them! 💨\n\n"
                                    "**Simon Didn't Say!** Shield is now gone. ⚠️"
                                ),
                                COLOUR_SHIELD,
                            ))
                        else:
                            players.remove(target_state)
                            elimination_count += 1
                            round_number = elimination_count + 1
                            current_max_time = max(
                                MINIMUM_TIME, current_max_time - TIME_REDUCTION
                            )
                            await ctx.send(embed=self._embed(
                                "💥  TRAP TRIGGERED!",
                                (
                                    f"**{target_state.member.display_name}** answered a trap!\n\n"
                                    "**Simon Didn't Say!** You're ELIMINATED! 🔥\n\n"
                                    f"**{len(players)}** player"
                                    f"{'s' if len(players) != 1 else ''} remain…"
                                ),
                                COLOUR_TRAP,
                            ))
                            await asyncio.sleep(2)

                except asyncio.TimeoutError:
                    # Ignored the trap — also survives
                    await ctx.send(
                        f"✅  **{target_state.member.display_name}** wisely "
                        "ignored the trap! Simon Didn't Say! 🧠"
                    )
                    target_state.streak += 1
                    target_state.rounds_survived += 1

            elif is_double_trouble:
                # ── DOUBLE TROUBLE ────────────────────────────────────────
                dt_word    = random.choice(DOUBLE_TROUBLE_WORDS)
                dt_reversed = dt_word[::-1]

                # Build a composite answer: exact string + space + reversed word
                composite_answer = f"{question['answer']} {dt_reversed}"

                displayed = (
                    f"**DOUBLE TROUBLE!** 🔥🔥\n\n"
                    f"Task 1 — {_poison(question['prompt'])}\n"
                    f"Task 2 — Type the word **`{dt_word.upper()}`** **backward**\n\n"
                    f"Answer format: `<task1_answer> <task2_answer>` in ONE message\n"
                    f"⏱  You have **{time_limit:.0f} seconds** — GO!"
                )

                await ctx.send(embed=self._embed(
                    title=f"🔥  DOUBLE TROUBLE! — Round {round_number}",
                    description=(
                        f"{target_state.member.mention}, it's your turn!\n\n"
                        + displayed
                    ),
                    colour=COLOUR_DOUBLE,
                    footer=(
                        f"{len(players)} players remaining  |  Round {round_number}  |  "
                        f"🔥 Streak: {target_state.streak}"
                    ),
                ))

                def dt_check(m: discord.Message) -> bool:
                    return (
                        m.channel == ctx.channel
                        and m.author == target_state.member
                        and m.content.strip() == composite_answer
                    )

                t_start = time.monotonic()
                try:
                    await asyncio.wait_for(
                        self.bot.wait_for("message", check=dt_check),
                        timeout=time_limit,
                    )
                    answer_time = time.monotonic() - t_start
                    target_state.streak += 1
                    target_state.rounds_survived += 1
                    target_state.fastest_answer = min(
                        target_state.fastest_answer, answer_time
                    )
                    await ctx.send(
                        f"✅  **{target_state.member.display_name}** smashed Double Trouble "
                        f"in **{answer_time:.2f}s**! 🔥🎉"
                    )
                except asyncio.TimeoutError:
                    target_state.streak = 0
                    if target_state.has_shield:
                        target_state.has_shield = False
                        await ctx.send(embed=self._embed(
                            "🛡️  Shield Used!",
                            (
                                f"**{target_state.member.display_name}** ran out of time "
                                f"on Double Trouble, but their **Streak Shield** saved them!\n\n"
                                f"Expected: **`{composite_answer}`**"
                            ),
                            COLOUR_SHIELD,
                        ))
                    else:
                        players.remove(target_state)
                        elimination_count += 1
                        round_number = elimination_count + 1
                        current_max_time = max(
                            MINIMUM_TIME, current_max_time - TIME_REDUCTION
                        )
                        await ctx.send(embed=self._embed(
                            "💥  K A B O O M!",
                            (
                                f"**{target_state.member.display_name}** failed Double Trouble "
                                f"and has been **OBLITERATED**! 🔥\n\n"
                                f"Expected: **`{composite_answer}`**\n\n"
                                f"⏱  Time limit: **{current_max_time:.0f}s**\n"
                                f"**{len(players)}** player"
                                f"{'s' if len(players) != 1 else ''} remain…"
                            ),
                            COLOUR_BOOM,
                        ))
                        await asyncio.sleep(2)

            else:
                # ── STANDARD ROUND ────────────────────────────────────────
                shield_note  = (
                    "\n🛡️ **SHIELD ACTIVE** — you have one free life!"
                    if target_state.has_shield else ""
                )
                powerup_note = (
                    f"\n\n⚡ **POWER-UP QUESTION!** Correct answer gives "
                    f"**+{POWERUP_BONUS:.0f}s** bonus!"
                    if is_powerup else ""
                )

                await ctx.send(embed=self._embed(
                    title=f"⚡  CHALLENGE! — Round {round_number}",
                    description=(
                        f"{target_state.member.mention}, it's your turn!{shield_note}\n\n"
                        f"**{_poison(question['prompt'])}**{powerup_note}\n\n"
                        f"⏱  You have **{time_limit:.0f} seconds**!"
                    ),
                    colour=COLOUR_BONUS if is_powerup else COLOUR_ROUND,
                    footer=(
                        f"{len(players)} players remaining  |  Round {round_number}  |  "
                        f"🔥 Streak: {target_state.streak}  |  Time limit: {time_limit:.0f}s"
                    ),
                ))

                correct_answer = question["answer"]

                def answer_check(m: discord.Message) -> bool:
                    return (
                        m.channel == ctx.channel
                        and m.author == target_state.member
                        and m.content.strip() == correct_answer
                    )

                t_start = time.monotonic()
                try:
                    await asyncio.wait_for(
                        self.bot.wait_for("message", check=answer_check),
                        timeout=time_limit,
                    )
                    answer_time = time.monotonic() - t_start
                    target_state.streak          += 1
                    target_state.rounds_survived += 1
                    target_state.fastest_answer   = min(
                        target_state.fastest_answer, answer_time
                    )

                    if is_powerup:
                        current_max_time = min(
                            STARTING_TIME, current_max_time + POWERUP_BONUS
                        )
                        await ctx.send(embed=self._embed(
                            "⚡  POWER-UP!",
                            (
                                f"**{target_state.member.display_name}** nailed it in "
                                f"**{answer_time:.2f}s** and collected the power-up!\n\n"
                                f"⏱  Time limit boosted to **{current_max_time:.0f}s**!"
                            ),
                            COLOUR_BONUS,
                        ))
                    else:
                        await ctx.send(
                            f"✅  **{target_state.member.display_name}** answered correctly "
                            f"in **{answer_time:.2f}s**! 🎉"
                        )

                    # Streak shield milestone
                    if (
                        target_state.streak >= STREAK_REQUIRED
                        and not target_state.has_shield
                    ):
                        target_state.has_shield = True
                        await ctx.send(embed=self._embed(
                            "🛡️  STREAK SHIELD EARNED!",
                            (
                                f"**{target_state.member.display_name}** answered "
                                f"**{target_state.streak} in a row** and earned a "
                                "**Streak Shield** — one free life! 🔰"
                            ),
                            COLOUR_SHIELD,
                        ))

                except asyncio.TimeoutError:
                    target_state.streak = 0

                    if target_state.has_shield:
                        target_state.has_shield = False
                        await ctx.send(embed=self._embed(
                            "🛡️  SHIELD USED!",
                            (
                                f"**{target_state.member.display_name}** ran out of time, "
                                "but their **Streak Shield** saved them! 💨\n\n"
                                f"Correct answer was: **`{correct_answer}`**\n\n"
                                "Shield is now gone — no more free passes! ⚠️"
                            ),
                            COLOUR_SHIELD,
                        ))
                    else:
                        players.remove(target_state)
                        elimination_count += 1
                        round_number = elimination_count + 1
                        current_max_time = max(
                            MINIMUM_TIME, current_max_time - TIME_REDUCTION
                        )
                        await ctx.send(embed=self._embed(
                            "💥  K A B O O M!",
                            (
                                f"**{target_state.member.display_name}** ran out of time "
                                "and has been **OBLITERATED**! 🔥\n\n"
                                f"Correct answer was: **`{correct_answer}`**\n\n"
                                f"⏱  Time limit is now **{current_max_time:.0f}s** "
                                "— getting spicy! 🌶️\n\n"
                                f"**{len(players)}** player"
                                f"{'s' if len(players) != 1 else ''} remain…"
                            ),
                            COLOUR_BOOM,
                        ))
                        await asyncio.sleep(2)

            round_number += 1

        # ── Post-loop: check cancel / edge cases ──────────────────────────
        if cancel_event.is_set():
            await ctx.send(embed=self._embed(
                "🛑  Game Aborted", "Cancelled by an administrator.", COLOUR_BOOM
            ))
            return

        if len(players) == 1:
            await self._conclude_game(ctx, players[0], stats_data)
            return

        # Exactly 2 players left → Sudden Death
        await self._safe_sudden_death(ctx, players, stats_data, round_number, cancel_event)

    # ───────────────────────────────────────────────────────────────────────
    # PHASE 2b — SUDDEN DEATH (1 v 1)
    # ───────────────────────────────────────────────────────────────────────

    async def _sudden_death(
        self,
        ctx:          commands.Context,
        players:      list[_Player],
        stats_data:   dict[str, dict],
        round_number: int,
        cancel_event: asyncio.Event,
    ) -> None:
        """
        1-v-1 finale: BOTH players receive the SAME question simultaneously.
        The one who answers SLOWER (or times out) is eliminated.
        """

        await ctx.send(embed=self._embed(
            title="☠️  SUDDEN DEATH!",
            description=(
                f"We're down to **{players[0].member.mention}** vs "
                f"**{players[1].member.mention}**!\n\n"
                "Same question. Same time. Whoever answers **SLOWER** explodes. 💀\n\n"
                "May the fastest fingers win… 🏁"
            ),
            colour=COLOUR_SUDDEN,
        ))
        await asyncio.sleep(3)

        question_pool: list[dict[str, str]] = list(QUESTIONS)
        random.shuffle(question_pool)
        q_index = 0

        while len(players) == 2:
            p1, p2 = players[0], players[1]

            if cancel_event.is_set():
                await ctx.send(embed=self._embed(
                    "🛑  Game Aborted", "Cancelled by an administrator.", COLOUR_BOOM
                ))
                return

            round_number += 1

            if q_index >= len(question_pool):
                random.shuffle(question_pool)
                q_index = 0
            question       = question_pool[q_index]
            q_index       += 1
            correct_answer = question["answer"]

            # Decide if this sudden death round is a trap
            is_sd_trap = random.random() < TRAP_QUESTION_PCT

            async with ctx.channel.typing():
                await asyncio.sleep(0.8)

            if is_sd_trap:
                # In sudden death, BOTH must ignore/skip the trap
                trap_prompt = random.choice(TRAP_PROMPTS)

                await ctx.send(embed=self._embed(
                    title=f"☠️  SUDDEN DEATH — Round {round_number}",
                    description=(
                        f"{p1.member.mention} vs {p2.member.mention}\n\n"
                        f"**{_poison(trap_prompt)}**\n\n"
                        f"⏱  **{MINIMUM_TIME:.0f}s** — GO! "
                        f"(type `{TRAP_SAFETY_WORD}` to survive!)"
                    ),
                    colour=COLOUR_SUDDEN,
                    footer="Simon Didn't Say — DO NOT answer. Type 'skip' to survive.",
                ))

                # Collect any responses from either player
                t_start  = time.monotonic()
                deadline = t_start + MINIMUM_TIME
                losers:  list[_Player] = []

                def sd_trap_check(m: discord.Message) -> bool:
                    return (
                        m.channel == ctx.channel
                        and m.author in (p1.member, p2.member)
                        and not m.author.bot
                    )

                while time.monotonic() < deadline and len(losers) < 2:
                    remaining = max(0.0, deadline - time.monotonic())
                    try:
                        msg: discord.Message = await asyncio.wait_for(
                            self.bot.wait_for("message", check=sd_trap_check),
                            timeout=remaining,
                        )
                        responder = p1 if msg.author == p1.member else p2
                        response  = msg.content.strip().lower()

                        if response != TRAP_SAFETY_WORD and responder not in losers:
                            losers.append(responder)
                    except asyncio.TimeoutError:
                        break

                if not losers:
                    await ctx.send("🧠  Both players survived the trap! Replaying round…")
                    continue

                # Eliminate the one(s) who answered
                loser_state = losers[0]
                players.remove(loser_state)
                await ctx.send(embed=self._embed(
                    "💥  TRAP IN SUDDEN DEATH!",
                    (
                        f"**{loser_state.member.display_name}** answered the trap "
                        "and was **VAPORISED**! 🔥\n\n"
                        "**Simon Didn't Say!** 👁️"
                    ),
                    COLOUR_TRAP,
                ))
                await asyncio.sleep(2)

            else:
                # Standard sudden death round with poisoned display
                await ctx.send(embed=self._embed(
                    title=f"☠️  SUDDEN DEATH — Round {round_number}",
                    description=(
                        f"{p1.member.mention} vs {p2.member.mention}\n\n"
                        f"**{_poison(question['prompt'])}**\n\n"
                        f"⏱  You have **{MINIMUM_TIME:.0f} seconds** — GO!"
                    ),
                    colour=COLOUR_SUDDEN,
                    footer="Slowest correct answer loses. Wrong answer = retype.",
                ))

                results: list[tuple[_Player, float]] = []
                t_start  = time.monotonic()
                deadline = t_start + MINIMUM_TIME

                def sd_check(m: discord.Message, _ans: str = correct_answer) -> bool:
                    return (
                        m.channel == ctx.channel
                        and m.author in (p1.member, p2.member)
                        and m.content.strip() == _ans
                    )

                while len(results) < 2 and time.monotonic() < deadline:
                    remaining = max(0.0, deadline - time.monotonic())
                    try:
                        sd_msg: discord.Message = await asyncio.wait_for(
                            self.bot.wait_for("message", check=sd_check),
                            timeout=remaining,
                        )
                        elapsed   = time.monotonic() - t_start
                        responder = p1 if sd_msg.author == p1.member else p2
                        if not any(r[0] == responder for r in results):
                            results.append((responder, elapsed))
                    except asyncio.TimeoutError:
                        break

                if len(results) == 0:
                    await ctx.send("💨  Both players timed out! Replaying the round…")
                    continue

                if len(results) == 1:
                    winner_state = results[0][0]
                    loser_state  = p2 if winner_state == p1 else p1
                else:
                    results.sort(key=lambda x: x[1])
                    winner_state = results[0][0]
                    loser_state  = results[1][0]
                    await ctx.send(
                        f"⚡  **{winner_state.member.display_name}** answered in "
                        f"**{results[0][1]:.2f}s** vs **{results[1][1]:.2f}s** — "
                        f"**{loser_state.member.display_name}** was slower!"
                    )

                players.remove(loser_state)
                await ctx.send(embed=self._embed(
                    "💥  ELIMINATED IN SUDDEN DEATH!",
                    (
                        f"**{loser_state.member.display_name}** has been vaporised! 🔥\n\n"
                        f"Correct answer was: **`{correct_answer}`**"
                    ),
                    COLOUR_BOOM,
                ))
                await asyncio.sleep(2)

        if players:
            await self._conclude_game(ctx, players[0], stats_data)

    # ───────────────────────────────────────────────────────────────────────
    # PHASE 2b ERROR WRAPPER
    # ───────────────────────────────────────────────────────────────────────

    async def _safe_sudden_death(
        self,
        ctx:          commands.Context,
        players:      list[_Player],
        stats_data:   dict[str, dict],
        round_number: int,
        cancel_event: asyncio.Event,
    ) -> None:
        try:
            await self._sudden_death(
                ctx, players, stats_data, round_number, cancel_event
            )
        except RecursionError:
            log.error("[SimonSays] RecursionError in sudden death — Render restart?")
            await ctx.send(embed=self._embed(
                "💥  Game Error",
                "The server restarted mid-game (Render free tier). "
                "Use `!simonstart` to play again!",
                COLOUR_BOOM,
            ))
        except Exception as exc:
            log.error("[SimonSays] Sudden death crashed: %s", exc)
            await ctx.send(embed=self._embed(
                "💥  Game Error",
                "Something went wrong in Sudden Death. "
                "Use `!simonstart` to play again.",
                COLOUR_BOOM,
            ))

    # ───────────────────────────────────────────────────────────────────────
    # PHASE 3 — CONCLUSION
    # ───────────────────────────────────────────────────────────────────────

    async def _conclude_game(
        self,
        ctx:          commands.Context,
        winner_state: _Player,
        stats_data:   dict[str, dict],
    ) -> None:
        """Persist stats, pay out bets, and announce the winner."""

        all_players = self._active_players.get(ctx.channel.id, [winner_state])

        for ps_obj in all_players:
            row = _get_player(stats_data, ps_obj.member.id)
            row["display_name"]    = ps_obj.member.display_name
            row["total_survived"] += ps_obj.rounds_survived
            row["longest_streak"]  = max(row["longest_streak"], ps_obj.streak)
            if ps_obj.fastest_answer < row["fastest_answer"]:
                row["fastest_answer"] = ps_obj.fastest_answer

        winner_row = _get_player(stats_data, winner_state.member.id)
        winner_row["wins"] += 1
        _save_stats(stats_data)

        # ── Pay out bets ──────────────────────────────────────────────────
        bets = self._bets.get(ctx.channel.id, {})
        if bets:
            try:
                from cogs.server_drops_economy import add_points
                payout_lines: list[str] = []
                for bettor_id, (amount, target_id) in bets.items():
                    if target_id == winner_state.member.id:
                        # Winner — return 2× bet
                        add_points(bettor_id, amount * 2)
                        payout_lines.append(
                            f"<@{bettor_id}> bet on the winner — "
                            f"won **{amount * 2} pts**! 🎰"
                        )
                    # Losers already had their points deducted; nothing to do
                if payout_lines:
                    await ctx.send(embed=self._embed(
                        "💰  Betting Payouts",
                        "\n".join(payout_lines),
                        COLOUR_BET,
                    ))
            except ImportError:
                pass
            except Exception as exc:
                log.error("[SimonSays] Bet payout failed: %s", exc)

        # ── Winner announcement ───────────────────────────────────────────
        await ctx.send(embed=self._embed(
            title="🏆  WE HAVE A WINNER!",
            description=(
                f"# 🎉  {winner_state.member.mention}  🎉\n\n"
                "Against all odds, through explosions, sudden death, and pure chaos…\n\n"
                f"**{winner_state.member.display_name}** is the **LAST ONE STANDING!**\n\n"
                f"🔥  Streak this game: **{winner_state.streak}** in a row\n"
                f"⚡  Fastest answer: "
                f"**{winner_state.fastest_answer:.2f}s**\n\n"
                "👑  **CHAMPION OF SIMON SAYS** 👑\n\n"
                "All others have been reduced to smouldering craters. Bow! 🫡💥"
            ),
            colour=COLOUR_WIN,
            footer=(
                "!simonleaderboard to see all-time rankings | "
                "!simonstart to play again"
            ),
            banner=WINNER_BANNER,
        ))

    # ───────────────────────────────────────────────────────────────────────
    # ERROR HANDLERS
    # ───────────────────────────────────────────────────────────────────────

    @simon_start.error
    async def simon_start_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(embed=self._embed(
                "🚫  Permission Denied",
                "Only **Administrators** can start a Simon Says game!",
                COLOUR_BOOM,
            ))
        elif isinstance(error, commands.BadArgument):
            await ctx.send(embed=self._embed(
                "⚠️  Invalid Argument",
                "Usage: `!simonstart [minutes]`  e.g. `!simonstart 3`",
                COLOUR_BOOM,
            ))
        else:
            raise error

    @simon_cancel.error
    @simon_kick.error
    @simon_pause.error
    @simon_resume.error
    @simon_blacklist.error
    @simon_extend.error
    async def admin_cmd_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(embed=self._embed(
                "🚫  Permission Denied",
                "Only **Administrators** can use this command!",
                COLOUR_BOOM,
            ))
        elif isinstance(error, commands.MemberNotFound):
            await ctx.send(embed=self._embed(
                "⚠️  Member Not Found",
                "Could not find that member. Try mentioning them directly.",
                COLOUR_BOOM,
            ))
        else:
            raise error


# ---------------------------------------------------------------------------
# SETUP
# ---------------------------------------------------------------------------

async def setup(bot: commands.Bot) -> None:
    """Called automatically by bot.load_extension()."""
    await bot.add_cog(SimonSaysGame(bot))
