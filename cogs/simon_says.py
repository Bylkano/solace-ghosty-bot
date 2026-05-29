"""
simon_says.py  —  v2.0
======================
A fully self-contained Discord.py Cog for a "Simon Says" Last-Man-Standing game.

NEW IN v2.0
-----------
  • Leaderboard & Stats  — JSON-backed persistence; !simonleaderboard / !simonstats
  • Admin controls       — !simoncancel (abort mid-game), !simonkick @user
  • Sudden Death         — 1-v-1 final round: same Q sent to both; slowest is out
  • Streak Shield        — 3 correct answers in a row = one free life
  • Power-ups            — 10 % chance of a +3 s bonus on a correct answer
  • UX polish            — lobby countdown warnings, round counter, typing indicator

Drop into your cogs/ folder and load with:
    await bot.load_extension("cogs.simon_says")

Requirements:  discord.py >= 2.0  |  Python >= 3.10
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass
from pathlib import Path

import discord
from discord.ext import commands

# ---------------------------------------------------------------------------
# COLOUR PALETTE
# ---------------------------------------------------------------------------
COLOUR_LOBBY   = discord.Color.from_rgb( 88, 101, 242)   # blurple
COLOUR_ROUND   = discord.Color.from_rgb(255, 165,   0)   # orange
COLOUR_BOOM    = discord.Color.from_rgb(237,  66,  69)   # red
COLOUR_WIN     = discord.Color.from_rgb( 87, 242, 135)   # green
COLOUR_SHIELD  = discord.Color.from_rgb(  0, 200, 255)   # cyan  (streak shield)
COLOUR_BONUS   = discord.Color.from_rgb(255, 215,   0)   # gold  (power-up)
COLOUR_SUDDEN  = discord.Color.from_rgb(180,   0, 255)   # purple (sudden death)
COLOUR_STATS   = discord.Color.from_rgb(114, 137, 218)   # soft blurple

# ---------------------------------------------------------------------------
# PATHS
# ---------------------------------------------------------------------------
# Stats JSON lives next to this file; change the path if you prefer elsewhere.
STATS_FILE = Path(__file__).parent / "simon_says_stats.json"

# ---------------------------------------------------------------------------
# TIMING CONSTANTS
# ---------------------------------------------------------------------------
STARTING_TIME   = 15.0   # seconds for round 1
MINIMUM_TIME    =  5.0   # floor — never drops below this
TIME_REDUCTION  =  1.0   # seconds shaved off per elimination
POWERUP_BONUS   =  3.0   # extra seconds awarded by a power-up
POWERUP_CHANCE  =  0.10  # 10 % probability any given question is a power-up
STREAK_REQUIRED =  3     # correct answers in a row needed to earn a shield

# ---------------------------------------------------------------------------
# QUESTION BANK  (310 entries — Category A: mash, Category B: brain-fire)
# ---------------------------------------------------------------------------
QUESTIONS: list[dict] = [

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
    # CATEGORY B  —  Quick Brain Fire  (150 entries)
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
    {"prompt": "Capital of India? (one word)",               "answer": "New Delhi"},
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
# STATS  —  thin persistence layer (JSON)
# ---------------------------------------------------------------------------

@dataclass
class PlayerStats:
    """Per-player lifetime stats stored in JSON."""
    wins:            int = 0
    games_played:    int = 0
    total_survived:  int = 0   # rounds survived across all games
    fastest_answer:  float = 999.0   # seconds (lower = better)
    longest_streak:  int = 0


def _load_stats() -> dict[str, dict]:
    """Load the JSON stats file; return empty dict if missing/corrupt."""
    if STATS_FILE.exists():
        try:
            with STATS_FILE.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_stats(data: dict[str, dict]) -> None:
    """Persist stats to disk atomically (write to tmp, then rename)."""
    tmp = STATS_FILE.with_suffix(".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        tmp.replace(STATS_FILE)
    except OSError as exc:
        print(f"[SimonSays] WARNING: Could not save stats: {exc}")


def _get_player(data: dict, user_id: int) -> dict:
    """Return (and auto-create) the stats sub-dict for a user ID."""
    key = str(user_id)
    if key not in data:
        data[key] = {
            "wins": 0,
            "games_played": 0,
            "total_survived": 0,
            "fastest_answer": 999.0,
            "longest_streak": 0,
            "display_name": "",
        }
    return data[key]


# ---------------------------------------------------------------------------
# PER-GAME PLAYER STATE
# ---------------------------------------------------------------------------

@dataclass
class _Player:
    """Transient state for a single player during one game."""
    member:         discord.Member
    streak:         int   = 0      # consecutive correct answers
    has_shield:     bool  = False  # True = one free life available
    rounds_survived: int  = 0
    fastest_answer: float = 999.0


# ---------------------------------------------------------------------------
# COG
# ---------------------------------------------------------------------------

class SimonSaysGame(commands.Cog, name="Simon Says"):
    """
    Simon Says — Last Man Standing  (v2.0)

    Commands
    --------
    !simonstart [minutes]   — Admin: open lobby
    !simoncancel            — Admin: abort the running game
    !simonkick @user        — Admin: eliminate a player mid-game
    !simonleaderboard       — Show the all-time top-10 winners
    !simonstats [@user]     — Show stats for yourself or another user
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

        # channel_id → True when a game (lobby or match) is in progress
        self._active_games: dict[int, bool] = {}

        # channel_id → asyncio.Event — set to True to signal cancellation
        self._cancel_events: dict[int, asyncio.Event] = {}

        # channel_id → set of user IDs mid-game (for !simonkick)
        self._active_players: dict[int, list[_Player]] = {}

    # ───────────────────────────────────────────────────────────────────────
    # UTILITY: embed factory
    # ───────────────────────────────────────────────────────────────────────

    @staticmethod
    def _embed(
        title: str,
        description: str,
        colour: discord.Color,
        *,
        footer: str | None = None,
    ) -> discord.Embed:
        em = discord.Embed(title=title, description=description, colour=colour)
        if footer:
            em.set_footer(text=footer)
        return em

    # ───────────────────────────────────────────────────────────────────────
    # COMMAND: !simonstart
    # ───────────────────────────────────────────────────────────────────────

    @commands.command(name="simonstart")
    @commands.has_permissions(administrator=True)
    async def simon_start(self, ctx: commands.Context, minutes: int = 1) -> None:
        """Open a Simon Says lobby. Usage: !simonstart [minutes]"""

        if self._active_games.get(ctx.channel.id):
            await ctx.send(embed=self._embed(
                "⚠️  Game Already Running",
                "A Simon Says game is already active in this channel!",
                COLOUR_BOOM,
            ))
            return

        minutes = max(1, min(minutes, 30))
        self._active_games[ctx.channel.id]   = True
        self._cancel_events[ctx.channel.id]  = asyncio.Event()

        try:
            await self._run_lobby(ctx, minutes)
        finally:
            self._active_games.pop(ctx.channel.id,  None)
            self._cancel_events.pop(ctx.channel.id, None)
            self._active_players.pop(ctx.channel.id, None)

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

        event.set()   # signal the game loop to stop
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
            await ctx.send(
                f"⚠️  **{member.display_name}** is not in the current game."
            )
            return

        players.remove(target)
        await ctx.send(embed=self._embed(
            "👢  Player Kicked",
            (
                f"**{member.display_name}** has been forcibly removed from the game "
                f"by **{ctx.author.display_name}**. 💀\n\n"
                f"**{len(players)}** player{'s' if len(players) != 1 else ''} remain."
            ),
            COLOUR_BOOM,
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

        # Sort by wins desc, then games_played asc as tiebreaker
        ranked = sorted(
            data.items(),
            key=lambda kv: (-kv[1].get("wins", 0), kv[1].get("games_played", 0)),
        )[:10]

        medals = ["🥇", "🥈", "🥉"] + ["🔹"] * 7
        lines  = []
        for i, (uid, stats) in enumerate(ranked):
            name    = stats.get("display_name", f"User#{uid}")
            wins    = stats.get("wins", 0)
            played  = stats.get("games_played", 0)
            wr      = f"{wins/played*100:.0f}%" if played else "—"
            lines.append(
                f"{medals[i]}  **{name}** — "
                f"{wins} win{'s' if wins != 1 else ''} "
                f"({wr} win rate, {played} games)"
            )

        await ctx.send(embed=self._embed(
            "🏆  Simon Says — All-Time Leaderboard",
            "\n".join(lines),
            COLOUR_STATS,
            footer="Use !simonstats @user for detailed breakdown",
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
            f"📈  Stats — {target.display_name}",
            desc,
            COLOUR_STATS,
        ))

    # ───────────────────────────────────────────────────────────────────────
    # PHASE 1 — LOBBY
    # ───────────────────────────────────────────────────────────────────────

    async def _run_lobby(self, ctx: commands.Context, minutes: int) -> None:
        """
        Open the lobby for `minutes`, send countdown warnings,
        collect players, then hand off to _run_game().
        """

        lobby_seconds  = minutes * 60
        cancel_event   = self._cancel_events[ctx.channel.id]
        joined_members: list[discord.Member] = []

        # ── Opening announcement ──────────────────────────────────────────
        await ctx.send(embed=self._embed(
            title="🎮  Simon Says — Last Man Standing!",
            description=(
                f"A new game has been opened by **{ctx.author.display_name}**!\n\n"
                f"⏳ **Lobby closes in {minutes} minute{'s' if minutes != 1 else ''}.**\n\n"
                "Type **`join`** in this channel to enter.\n\n"
                "🛡️  Earn a **Streak Shield** by answering **3 in a row** correctly!\n"
                "⚡  Watch for **Power-Up** questions — get +3 bonus seconds!\n\n"
                "The last player standing wins! 💥"
            ),
            colour=COLOUR_LOBBY,
            footer="Admins: !simoncancel to abort | !simonkick @user to remove a player",
        ))

        # ── Warning timestamps (send a reminder at 50 % and 10 % remaining) ──
        warning_times: list[int] = []
        if lobby_seconds >= 60:
            warning_times.append(lobby_seconds // 2)   # halfway
        if lobby_seconds >= 30:
            warning_times.append(max(10, lobby_seconds // 10))  # near the end

        elapsed       = 0
        warned_at     = set()

        # ── Collect joins with non-blocking polling ───────────────────────
        # We use a short-timeout wait_for inside a manual countdown so we
        # can also check cancel_event and fire lobby warnings.

        async def collect_joins() -> None:
            nonlocal elapsed

            while elapsed < lobby_seconds:
                if cancel_event.is_set():
                    return   # admin cancelled during lobby

                # Fire countdown warnings
                remaining = lobby_seconds - elapsed
                for w in warning_times:
                    if remaining <= w and w not in warned_at:
                        warned_at.add(w)
                        await ctx.send(
                            f"⏰  **{remaining} seconds** left to join the lobby! "
                            f"({len(joined_members)} player{'s' if len(joined_members) != 1 else ''} so far)"
                        )

                # Wait up to 2 s for a join message, then re-loop
                try:
                    def check(m: discord.Message) -> bool:
                        return (
                            m.channel == ctx.channel
                            and m.content.strip().lower() == "join"
                            and not m.author.bot
                            and m.author not in joined_members
                        )
                    msg: discord.Message = await asyncio.wait_for(
                        self.bot.wait_for("message", check=check),
                        timeout=2.0,
                    )
                    joined_members.append(msg.author)
                    await ctx.send(
                        f"✅  **{msg.author.display_name}** joined! "
                        f"({len(joined_members)} player{'s' if len(joined_members) != 1 else ''} in lobby)"
                    )
                except asyncio.TimeoutError:
                    elapsed += 2   # 2 s tick

        await collect_joins()

        # ── Cancelled during lobby? ───────────────────────────────────────
        if cancel_event.is_set():
            return

        # ── Not enough players? ───────────────────────────────────────────
        if len(joined_members) < 2:
            await ctx.send(embed=self._embed(
                "❌  Not Enough Players",
                (
                    f"Only **{len(joined_members)}** player joined. "
                    "Need at least **2** to start. Game cancelled! 😢"
                ),
                COLOUR_BOOM,
            ))
            return

        player_list = "\n".join(
            f"{i+1}. {m.display_name}" for i, m in enumerate(joined_members)
        )
        await ctx.send(embed=self._embed(
            f"✅  Lobby Closed — {len(joined_members)} Players",
            f"{player_list}\n\n🎮  Starting the game...",
            COLOUR_WIN,
        ))

        await self._run_game(ctx, joined_members)

    # ───────────────────────────────────────────────────────────────────────
    # PHASE 2 — MAIN GAME LOOP
    # ───────────────────────────────────────────────────────────────────────

    async def _run_game(
        self,
        ctx: commands.Context,
        members: list[discord.Member],
    ) -> None:
        """
        Core Last-Man-Standing loop with:
          • Round counter
          • Streak shield (3-in-a-row = free life)
          • Power-up questions (+3 s bonus)
          • Typing indicator before each challenge
          • Sudden Death when 2 players remain
          • Stats recording
        """

        cancel_event = self._cancel_events[ctx.channel.id]

        # Build transient player objects
        players: list[_Player] = [_Player(member=m) for m in members]
        self._active_players[ctx.channel.id] = players

        # Update games_played for everyone who entered
        stats_data = _load_stats()
        for p in players:
            ps = _get_player(stats_data, p.member.id)
            ps["games_played"] += 1
            ps["display_name"]  = p.member.display_name
        _save_stats(stats_data)

        current_max_time: float = STARTING_TIME
        round_number:     int   = 0

        # Pre-shuffle the question pool
        question_pool = list(QUESTIONS)
        random.shuffle(question_pool)
        q_index = 0

        # ── Opening announcement ──────────────────────────────────────────
        player_list = "\n".join(f"• {p.member.display_name}" for p in players)
        await ctx.send(embed=self._embed(
            title="🚀  Game Starting!",
            description=(
                f"**{len(players)} players** have entered the arena:\n\n"
                f"{player_list}\n\n"
                "Fingers on keyboards… **GO!** 💀"
            ),
            colour=COLOUR_LOBBY,
        ))
        await asyncio.sleep(3)

        # ── Main loop: run until 2 players left, then Sudden Death ────────
        while len(players) > 2:
            if cancel_event.is_set():
                await ctx.send(embed=self._embed(
                    "🛑  Game Aborted",
                    "The game was cancelled by an administrator.",
                    COLOUR_BOOM,
                ))
                return

            round_number += 1

            # Pick a random active player
            target_state: _Player = random.choice(players)

            # Pull next question (re-shuffle on exhaustion)
            if q_index >= len(question_pool):
                random.shuffle(question_pool)
                q_index = 0
            question  = question_pool[q_index]
            q_index  += 1

            # Decide if this is a Power-Up question
            is_powerup = random.random() < POWERUP_CHANCE
            time_limit = current_max_time

            # ── Typing indicator ──────────────────────────────────────────
            async with ctx.channel.typing():
                await asyncio.sleep(1.2)   # brief dramatic pause

            # ── Build and send the challenge embed ────────────────────────
            shield_note = (
                "  🛡️ **SHIELD ACTIVE** — you have one free life!"
                if target_state.has_shield else ""
            )
            powerup_note = (
                f"\n\n⚡ **POWER-UP QUESTION!** Correct answer gives **+{POWERUP_BONUS:.0f}s** bonus!"
                if is_powerup else ""
            )

            await ctx.send(embed=self._embed(
                title=f"⚡  CHALLENGE! — Round {round_number}",
                description=(
                    f"{target_state.member.mention}, it's your turn!{shield_note}\n\n"
                    f"**{question['prompt']}**{powerup_note}\n\n"
                    f"⏱  You have **{time_limit:.0f} seconds**!"
                ),
                colour=COLOUR_BONUS if is_powerup else COLOUR_ROUND,
                footer=(
                    f"{len(players)} players remaining  |  "
                    f"Round {round_number}  |  "
                    f"🔥 Streak: {target_state.streak}  |  "
                    f"Time limit: {time_limit:.0f}s"
                ),
            ))

            # ── Wait for a correct answer ─────────────────────────────────
            correct_answer = question["answer"]

            def answer_check(m: discord.Message) -> bool:
                return (
                    m.channel   == ctx.channel
                    and m.author == target_state.member
                    and m.content.strip() == correct_answer
                )

            t_start = time.monotonic()
            try:
                await self.bot.wait_for(
                    "message",
                    check=answer_check,
                    timeout=time_limit,
                )
                answer_time = time.monotonic() - t_start

                # ── Correct! ──────────────────────────────────────────────
                target_state.streak         += 1
                target_state.rounds_survived += 1
                target_state.fastest_answer  = min(
                    target_state.fastest_answer, answer_time
                )

                # Power-up bonus
                if is_powerup:
                    current_max_time = min(STARTING_TIME, current_max_time + POWERUP_BONUS)
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

                # Streak milestone — award shield
                if target_state.streak >= STREAK_REQUIRED and not target_state.has_shield:
                    target_state.has_shield = True
                    await ctx.send(embed=self._embed(
                        "🛡️  STREAK SHIELD EARNED!",
                        (
                            f"**{target_state.member.display_name}** has answered "
                            f"**{target_state.streak} in a row** and earned a "
                            f"**Streak Shield** — one free life! 🔰"
                        ),
                        COLOUR_SHIELD,
                    ))

            except asyncio.TimeoutError:
                # ── Time's up ─────────────────────────────────────────────
                target_state.streak = 0   # streak broken on timeout

                if target_state.has_shield:
                    # Shield absorbs the elimination
                    target_state.has_shield = False
                    await ctx.send(embed=self._embed(
                        "🛡️  SHIELD USED!",
                        (
                            f"**{target_state.member.display_name}** ran out of time, "
                            f"but their **Streak Shield** saved them! 💨\n\n"
                            f"Correct answer was: **`{correct_answer}`**\n\n"
                            f"Shield is now gone — no more free passes! ⚠️"
                        ),
                        COLOUR_SHIELD,
                    ))
                else:
                    # No shield — eliminate the player
                    players.remove(target_state)
                    current_max_time = max(MINIMUM_TIME, current_max_time - TIME_REDUCTION)

                    await ctx.send(embed=self._embed(
                        "💥  K A B O O M!",
                        (
                            f"**{target_state.member.display_name}** ran out of time "
                            f"and has been **OBLITERATED**! 🔥\n\n"
                            f"Correct answer was: **`{correct_answer}`**\n\n"
                            f"⏱  Time limit is now **{current_max_time:.0f}s** — "
                            f"getting spicy! 🌶️\n\n"
                            f"**{len(players)}** player{'s' if len(players) != 1 else ''} remain…"
                        ),
                        COLOUR_BOOM,
                    ))
                    await asyncio.sleep(2)

        # ── Check for cancel / game ending before sudden death ────────────
        if cancel_event.is_set():
            await ctx.send(embed=self._embed(
                "🛑  Game Aborted", "Cancelled by an administrator.", COLOUR_BOOM
            ))
            return

        if len(players) == 1:
            # Edge case: only 1 left (e.g., after a kick)
            await self._conclude_game(ctx, players[0], stats_data)
            return

        # ── SUDDEN DEATH — exactly 2 players remain ───────────────────────
        await self._sudden_death(ctx, players, stats_data, round_number, cancel_event)

    # ───────────────────────────────────────────────────────────────────────
    # PHASE 2b — SUDDEN DEATH (1 v 1)
    # ───────────────────────────────────────────────────────────────────────

    async def _sudden_death(
        self,
        ctx: commands.Context,
        players: list[_Player],
        stats_data: dict,
        round_number: int,
        cancel_event: asyncio.Event,
    ) -> None:
        """
        1-v-1 finale: BOTH players receive the SAME question simultaneously.
        The one who answers SLOWER (or times out) is eliminated.
        Repeat until one player loses.
        """

        p1, p2 = players[0], players[1]

        await ctx.send(embed=self._embed(
            title="☠️  SUDDEN DEATH!",
            description=(
                f"We're down to **{p1.member.mention}** vs **{p2.member.mention}**!\n\n"
                "Same question. Same time. Whoever answers **SLOWER** explodes. 💀\n\n"
                "May the fastest fingers win… 🏁"
            ),
            colour=COLOUR_SUDDEN,
        ))
        await asyncio.sleep(3)

        question_pool = list(QUESTIONS)
        random.shuffle(question_pool)
        q_index = 0

        while len(players) == 2:
            if cancel_event.is_set():
                await ctx.send(embed=self._embed(
                    "🛑  Game Aborted", "Cancelled by an administrator.", COLOUR_BOOM
                ))
                return

            round_number += 1

            if q_index >= len(question_pool):
                random.shuffle(question_pool)
                q_index = 0
            question      = question_pool[q_index]
            q_index      += 1
            correct_answer = question["answer"]

            async with ctx.channel.typing():
                await asyncio.sleep(1.0)

            await ctx.send(embed=self._embed(
                title=f"☠️  SUDDEN DEATH — Round {round_number}",
                description=(
                    f"{p1.member.mention} vs {p2.member.mention}\n\n"
                    f"**{question['prompt']}**\n\n"
                    f"⏱  You have **{MINIMUM_TIME:.0f} seconds** — GO!"
                ),
                colour=COLOUR_SUDDEN,
                footer="Slowest correct answer loses. Wrong answer = wait and retype.",
            ))

            # Collect first and second correct answers with timestamps
            results: list[tuple[_Player, float]] = []   # (player, elapsed)
            t_start = time.monotonic()

            def sd_check(m: discord.Message) -> bool:
                return (
                    m.channel == ctx.channel
                    and m.author in (p1.member, p2.member)
                    and m.content.strip() == correct_answer
                )

            # We wait twice (for both players) or until time is up
            deadline = time.monotonic() + MINIMUM_TIME

            while len(results) < 2 and time.monotonic() < deadline:
                remaining = max(0.0, deadline - time.monotonic())
                try:
                    msg: discord.Message = await asyncio.wait_for(
                        self.bot.wait_for("message", check=sd_check),
                        timeout=remaining,
                    )
                    elapsed  = time.monotonic() - t_start
                    responder = p1 if msg.author == p1.member else p2

                    # Only record the FIRST correct message per player
                    if not any(r[0] == responder for r in results):
                        results.append((responder, elapsed))

                except asyncio.TimeoutError:
                    break

            # ── Determine who loses ───────────────────────────────────────
            answered_ids = {r[0] for r in results}

            if len(results) == 0:
                # Both timed out — re-run this round
                await ctx.send("💨  Both players timed out! Reshuffling the same round…")
                continue

            if len(results) == 1:
                # Only one answered — the other is out
                winner_state = results[0][0]
                loser_state  = p2 if winner_state == p1 else p1
            else:
                # Both answered — slowest is out
                results.sort(key=lambda x: x[1])   # fastest first
                winner_state = results[0][0]
                loser_state  = results[1][0]

                await ctx.send(
                    f"⚡  **{winner_state.member.display_name}** answered in "
                    f"**{results[0][1]:.2f}s** vs "
                    f"**{results[1][1]:.2f}s** — "
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
    # PHASE 3 — CONCLUSION  (save stats + announce winner)
    # ───────────────────────────────────────────────────────────────────────

    async def _conclude_game(
        self,
        ctx: commands.Context,
        winner_state: _Player,
        stats_data: dict,
    ) -> None:
        """Persist all per-player stats for this game, then announce the winner."""

        # Grab the full active list (may have been modified by kicks)
        all_players = self._active_players.get(ctx.channel.id, [winner_state])

        for ps_obj in all_players:
            row = _get_player(stats_data, ps_obj.member.id)
            row["display_name"]    = ps_obj.member.display_name
            row["total_survived"] += ps_obj.rounds_survived
            row["longest_streak"]  = max(row["longest_streak"], ps_obj.streak)
            if ps_obj.fastest_answer < row["fastest_answer"]:
                row["fastest_answer"] = ps_obj.fastest_answer

        # Record the win
        winner_row = _get_player(stats_data, winner_state.member.id)
        winner_row["wins"] += 1
        _save_stats(stats_data)

        # ── Winner embed ──────────────────────────────────────────────────
        await ctx.send(embed=self._embed(
            title="🏆  WE HAVE A WINNER!",
            description=(
                f"# 🎉  {winner_state.member.mention}  🎉\n\n"
                "Against all odds, through explosions, sudden death, and pure chaos…\n\n"
                f"**{winner_state.member.display_name}** is the **LAST ONE STANDING!**\n\n"
                f"🔥  Streak this game: **{winner_state.streak}** in a row\n"
                f"⚡  Fastest answer: **{winner_state.fastest_answer:.2f}s**\n\n"
                "👑  **CHAMPION OF SIMON SAYS** 👑\n\n"
                "All others have been reduced to smouldering craters. Bow! 🫡💥"
            ),
            colour=COLOUR_WIN,
            footer="!simonleaderboard to see the all-time rankings | !simonstart to play again",
        ))

    # ───────────────────────────────────────────────────────────────────────
    # ERROR HANDLERS
    # ───────────────────────────────────────────────────────────────────────

    @simon_start.error
    async def simon_start_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
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
    async def admin_cmd_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
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
