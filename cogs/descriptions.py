# cogs/descriptions.py

GAME_CATALOGUE = {
    "trivia": {
        "title": "🧠 Trivia Drop  ·  20% Spawn Rate",
        "description": (
            "A question from **Math**, **Science**, or **Pop Culture** drops into chat.\n"
            "First person to type the correct answer wins.\n\n"
            "⭐ **Reward:** `5 – 15 points`"
        ),
        "color": 0x5865F2
    },
    "scramble": {
        "title": "🔤 Word Scramble  ·  15% Spawn Rate",
        "description": (
            "A word gets jumbled into a random mess of letters.\n"
            "Unscramble it and be the first to type the correct word.\n\n"
            "⭐ **Reward:** `5 – 15 points`"
        ),
        "color": 0x9B59B6
    },
    "hotcold": {
        "title": "🎯 Hot or Cold  ·  15% Spawn Rate",
        "description": (
            "The bot hides a number between **1 and 100**.\n"
            "Guess numbers — it replies `👆` (go higher) or `👇` (go lower).\n"
            "Close in fast!\n\n"
            "⭐ **Reward:** `10 – 20 points`"
        ),
        "color": 0x3498DB
    },
    "emoji": {
        "title": "🧩 Emoji Puzzle  ·  15% Spawn Rate",
        "description": (
            "A sequence of emojis represents a **movie**, **game**, or **brand**.\n"
            "Decode the emoji combo and type the exact title first.\n\n"
            "⭐ **Reward:** `10 – 20 points`"
        ),
        "color": 0xF39C12
    },
    "lootbox": {
        "title": "📦 Supply Lootbox  ·  10% Spawn Rate",
        "description": (
            "A mystery crate crash-lands in the channel.\n"
            "Hit the **`GRAB LOOT`** button before anyone else snatches it.\n\n"
            "⭐ **Reward:** `15 – 30 points`"
        ),
        "color": 0x2ECC71
    },
    "boss": {
        "title": "⚔️ Co-Op Boss Raid  ·  10% Spawn Rate",
        "description": (
            "A **Shadow Colossus** spawns with **50 HP** — it's too strong for one person.\n"
            "Type **`!attack`** (3s cooldown) to deal 1–5 damage. Take it down together!\n\n"
            "⭐ **Reward:** `100-point pool` split equally among all raiders on kill"
        ),
        "color": 0xE74C3C
    },
    "bomb": {
        "title": "💣 Reaction Time Bomb  ·  10% Spawn Rate",
        "description": (
            "A bomb is armed and the countdown begins.\n"
            "Wait for the **GO** signal — then hit **`DEFUSE`** as fast as possible.\n"
            "Click early and you *lose* points.\n\n"
            "⚠️ **Penalty:** Early click = **-5 pts**\n"
            "⭐ **Reward:** First clean defuse = `20 – 30 points`"
        ),
        "color": 0xFF6B35
    },
    "hotpotato": {
        "title": "🥔 Hot Potato  ·  10% Spawn Rate",
        "description": (
            "A random member gets stuck with a **ticking potato**.\n"
            "Type **`!pass @username`** within 10 seconds to offload it.\n"
            "When the timer runs out… boom. 50/50 outcome for whoever's holding it.\n\n"
            "💥 **Boom outcome:** `-20 pts` or `+50 pts` — luck decides"
        ),
        "color": 0xFFD700
    },
    "multi": {
        "title": "✨ Global Multiplier / Vault Bounty  ·  5% Spawn Rate",
        "description": (
            "Two possible events, both are chaotic:\n\n"
            "**Double Points —** All payouts are **2×** for the next 5 minutes.\n"
            "**Vault Bounty —** Solve a puzzle to steal points straight from the richest player."
        ),
        "color": 0x1ABC9C
    },
    "blackjack": {
        "title": "🃏 Blackjack Duel  ·  5% Spawn Rate",
        "description": (
            "Chat squares up against the **House dealer** in a group hand.\n"
            "Vote **`Hit`** or **`Stand`** — 3 matching votes locks the decision in.\n"
            "Beat the dealer and split the pot.\n\n"
            "⭐ **Reward:** `50-point pool` split among all participants on win"
        ),
        "color": 0x1A472A
    }
}
