"""
cogs/bank_breakthrough.py
─────────────────────────────────────────────────────────────────────────────
The Great Bank Breakthrough: Champion Edition
─────────────────────────────────────────────────────────────────────────────

A turn-based multiplayer strategy game for Money Heist events.
One Champion per team (4 total) competes on a shared 7×7 grid.

Admin commands:
  /inventory-give [team_name] [item_name] [quantity]  — give items to a team
  /inventory                                           — view a team's inventory
  /breakthrough-setup [u1] [u2] [u3] [u4]             — start a match

Champion commands (registered players only):
  /submit-move [direction]  — up / down / left / right / hold / action
  /use                      — use an item (smoke / c4 / adrenaline / shield / decrypter)

Game flow:
  • 4 champions placed at corners of a 7×7 grid
  • 4 random Loot Tiles (💰) and a Central Vault (🏆) at [3,3]
  • 90-second planning phase; moves default to 'hold' if not submitted
  • Simultaneous resolution: items → movement → looting → vault breaching
  • 3 cumulative Breach Points open the vault and end the game
"""

from __future__ import annotations

import asyncio
import random
import os
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Optional

import psycopg2
from psycopg2.extras import RealDictCursor

import discord
from discord import app_commands
from discord.ext import commands

# ──────────────────────────── Colours ────────────────────────────────────────

C_SETUP    = 0xF1C40F   # gold
C_ROUND    = 0x2C2F33   # near-black
C_ACTION   = 0xFF4500   # orange-red
C_LOOT     = 0x2ECC71   # emerald
C_VAULT    = 0xE74C3C   # crimson
C_WIN      = 0xFFD700   # bright gold
C_ITEM     = 0x9B59B6   # purple
C_INV      = 0x5865F2   # blurple
C_ERROR    = 0x992D22   # dark red
C_INFO     = 0x3498DB   # sky blue

# ──────────────────────────── Constants ──────────────────────────────────────

GRID_SIZE        = 7
VAULT_POS        = (3, 3)
VAULT_POINTS_REQ = 3
LOOT_REWARD      = 1500
ROUND_TIMER      = 90   # seconds

# Valid items and their display names
VALID_ITEMS = {
    "smoke":      "Smoke Grenade",
    "c4":         "C4 Charge",
    "adrenaline": "Adrenaline",
    "shield":     "Riot Shield",
    "decrypter":  "Decrypter Card",
}

# Team champion starting corners: (row, col)
CHAMPION_STARTS = [(0, 0), (0, 6), (6, 0), (6, 6)]

# Grid cell emojis
EMOJI_EMPTY   = "⬛"
EMOJI_LOOT    = "💰"
EMOJI_VAULT   = "🏆"
EMOJI_CHAMP   = ["🔴", "🔵", "🟢", "🟡"]   # Champion 1-4 tokens
EMOJI_FROZEN  = "❄️"

SEP = "▬" * 22

# ──────────────────────────── Database layer ─────────────────────────────────

_DB_URL = os.environ.get("DATABASE_URL", "")


def _db_connect():
    if not _DB_URL:
        raise RuntimeError("DATABASE_URL not set in environment")
    return psycopg2.connect(_DB_URL, sslmode="require")


def _db_init() -> None:
    """Create the breakthrough tables if they don't exist."""
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS breakthrough_inventory (
                    team_name  TEXT    NOT NULL,
                    item_key   TEXT    NOT NULL,
                    quantity   INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (team_name, item_key)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS breakthrough_coins (
                    team_name  TEXT    PRIMARY KEY,
                    coins      INTEGER NOT NULL DEFAULT 0
                )
            """)
        con.commit()


def _db_give_item(team: str, item_key: str, qty: int) -> int:
    """Add qty of item to team inventory. Returns new quantity."""
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute("""
                INSERT INTO breakthrough_inventory (team_name, item_key, quantity)
                VALUES (%s, %s, %s)
                ON CONFLICT (team_name, item_key)
                DO UPDATE SET quantity = breakthrough_inventory.quantity + EXCLUDED.quantity
                RETURNING quantity
            """, (team.lower(), item_key, qty))
            row = cur.fetchone()
        con.commit()
        return row[0] if row else qty


def _db_get_inventory(team: str) -> dict[str, int]:
    """Return {item_key: quantity} for a team."""
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT item_key, quantity FROM breakthrough_inventory WHERE team_name = %s AND quantity > 0",
                (team.lower(),),
            )
            return {row[0]: row[1] for row in cur.fetchall()}


def _db_deduct_item(team: str, item_key: str, qty: int = 1) -> bool:
    """Deduct qty from team's item. Returns True if successful (had enough)."""
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT quantity FROM breakthrough_inventory WHERE team_name = %s AND item_key = %s",
                (team.lower(), item_key),
            )
            row = cur.fetchone()
            if not row or row[0] < qty:
                return False
            cur.execute("""
                UPDATE breakthrough_inventory
                SET quantity = quantity - %s
                WHERE team_name = %s AND item_key = %s
            """, (qty, team.lower(), item_key))
        con.commit()
        return True


def _db_remove_item(team: str, item_key: str, qty: int) -> tuple[bool, int]:
    """
    Remove qty from team's item, flooring at 0.
    Returns (had_enough, new_quantity).
    had_enough is False if they had fewer than qty (still removes what they had).
    """
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT quantity FROM breakthrough_inventory WHERE team_name = %s AND item_key = %s",
                (team.lower(), item_key),
            )
            row = cur.fetchone()
            current = row[0] if row else 0
            had_enough = current >= qty
            new_qty = max(0, current - qty)
            cur.execute("""
                INSERT INTO breakthrough_inventory (team_name, item_key, quantity)
                VALUES (%s, %s, %s)
                ON CONFLICT (team_name, item_key)
                DO UPDATE SET quantity = EXCLUDED.quantity
            """, (team.lower(), item_key, new_qty))
        con.commit()
        return had_enough, new_qty


def _db_set_item(team: str, item_key: str, qty: int) -> None:
    """Hard-set a team's item quantity."""
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute("""
                INSERT INTO breakthrough_inventory (team_name, item_key, quantity)
                VALUES (%s, %s, %s)
                ON CONFLICT (team_name, item_key)
                DO UPDATE SET quantity = EXCLUDED.quantity
            """, (team.lower(), item_key, qty))
        con.commit()


def _db_add_coins(team: str, amount: int) -> int:
    """Add coins to a team. Returns new balance."""
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute("""
                INSERT INTO breakthrough_coins (team_name, coins) VALUES (%s, %s)
                ON CONFLICT (team_name) DO UPDATE SET coins = breakthrough_coins.coins + EXCLUDED.coins
                RETURNING coins
            """, (team.lower(), amount))
            row = cur.fetchone()
        con.commit()
        return row[0] if row else amount


def _db_get_coins(team: str) -> int:
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT coins FROM breakthrough_coins WHERE team_name = %s",
                (team.lower(),),
            )
            row = cur.fetchone()
            return row[0] if row else 0


# ──────────────────────────── Data models ────────────────────────────────────

@dataclass
class Champion:
    user_id:    int
    team:       str          # team name (lowercase)
    slot:       int          # 0-3 (determines starting corner & emoji)
    row:        int
    col:        int

    # Status effects
    frozen_rounds:  int  = 0    # rounds remaining frozen (C4 effect)
    shield_rounds:  int  = 0    # rounds remaining shielded
    smoke_this_round: bool = False  # movement randomised this round

    # Per-round inputs (reset each round)
    move:       Optional[str] = None   # up/down/left/right/hold/action + diagonal
    used_item:  Optional[str] = None   # item key used this round
    item_target: Optional[int] = None  # target user_id (for smoke/c4)
    has_adrenaline: bool = False       # adrenaline active this round

    @property
    def emoji(self) -> str:
        return EMOJI_CHAMP[self.slot]

    @property
    def display_name(self) -> str:
        return f"Champion {self.slot + 1}"

    def reset_round_inputs(self):
        self.move            = None
        self.used_item       = None
        self.item_target     = None
        self.has_adrenaline  = False
        self.smoke_this_round = False


@dataclass
class GameState:
    guild_id:      int
    channel_id:    int

    # champion slot → Champion
    champions:     dict[int, Champion]      = field(default_factory=dict)
    # user_id → champion slot (reverse lookup)
    user_to_slot:  dict[int, int]           = field(default_factory=dict)

    # Grid: set of (row, col) containing loot tiles
    loot_tiles:    set[tuple[int, int]]     = field(default_factory=set)

    breach_points: int   = 0
    round_number:  int   = 0
    active:        bool  = True

    # Message to edit each round
    board_message: Optional[discord.Message] = None

    # Pending C4 blasts: {target_slot: source_slot}
    pending_c4:    dict[int, int]           = field(default_factory=dict)

    # Timer task
    timer_task:    Optional[asyncio.Task]   = None


# ──────────────────────────── Grid helpers ───────────────────────────────────

def _init_grid() -> set[tuple[int, int]]:
    """Pick 4 random non-corner, non-vault loot tile positions."""
    taken = set(CHAMPION_STARTS) | {VAULT_POS}
    candidates = [
        (r, c)
        for r in range(GRID_SIZE)
        for c in range(GRID_SIZE)
        if (r, c) not in taken
    ]
    return set(random.sample(candidates, 4))


def _render_grid(state: GameState) -> str:
    """Build the 7×7 emoji grid string."""
    # Build a 2D array
    grid = [[EMOJI_EMPTY for _ in range(GRID_SIZE)] for _ in range(GRID_SIZE)]

    # Place vault
    vr, vc = VAULT_POS
    grid[vr][vc] = EMOJI_VAULT

    # Place loot
    for (r, c) in state.loot_tiles:
        grid[r][c] = EMOJI_LOOT

    # Place champions (later slots overwrite if colliding — visual only)
    for slot, champ in state.champions.items():
        grid[champ.row][champ.col] = champ.emoji

    lines = []
    for row in grid:
        lines.append("".join(row))
    return "\n".join(lines)


def _apply_direction(row: int, col: int, direction: str) -> tuple[int, int]:
    """Return new (row, col) after moving in `direction`. Clamps to grid."""
    DIRECTIONS = {
        "up":         (-1,  0),
        "down":       ( 1,  0),
        "left":       ( 0, -1),
        "right":      ( 0,  1),
        "hold":       ( 0,  0),
        "action":     ( 0,  0),
        # Diagonal (adrenaline)
        "up-left":    (-1, -1),
        "up-right":   (-1,  1),
        "down-left":  ( 1, -1),
        "down-right": ( 1,  1),
    }
    dr, dc = DIRECTIONS.get(direction, (0, 0))
    new_r = max(0, min(GRID_SIZE - 1, row + dr))
    new_c = max(0, min(GRID_SIZE - 1, col + dc))
    return new_r, new_c


def _is_adjacent(r1: int, c1: int, r2: int, c2: int) -> bool:
    return abs(r1 - r2) <= 1 and abs(c1 - c2) <= 1 and (r1, c1) != (r2, c2)


# ──────────────────────────── Cog ────────────────────────────────────────────

class BankBreakthroughCog(commands.Cog, name="BankBreakthrough"):
    """The Great Bank Breakthrough: Champion Edition"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        _db_init()
        # guild_id → GameState
        self._games: dict[int, GameState] = {}

    # ═════════════════════════ INVENTORY COMMANDS ═════════════════════════════

    @app_commands.command(
        name="inventory-give",
        description="[Admin] Give items to a team's inventory.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        team_name = "Name of the team (e.g. 'Team Alpha').",
        item_name = "Item to give: smoke | c4 | adrenaline | shield | decrypter",
        quantity  = "How many to give.",
    )
    async def inventory_give(
        self,
        interaction: discord.Interaction,
        team_name:   str,
        item_name:   str,
        quantity:    app_commands.Range[int, 1, 99],
    ) -> None:
        key = item_name.strip().lower()
        if key not in VALID_ITEMS:
            await interaction.response.send_message(
                f"❌  Unknown item **{item_name}**.\n"
                f"Valid items: `smoke`, `c4`, `adrenaline`, `shield`, `decrypter`",
                ephemeral=True,
            )
            return

        new_qty = _db_give_item(team_name.strip(), key, quantity)
        full_name = VALID_ITEMS[key]

        em = discord.Embed(
            title="✅  Inventory Updated",
            colour=C_ITEM,
            description=f"**{team_name.strip()}** received **{quantity}× {full_name}**.",
        )
        em.add_field(name="New Stock", value=f"`{full_name}` → **{new_qty}×**", inline=True)
        em.set_footer(text="Bank Breakthrough  •  Inventory System")
        await interaction.response.send_message(embed=em, ephemeral=True)

    @app_commands.command(
        name="inventory",
        description="View a team's current item inventory.",
    )
    @app_commands.guild_only()
    @app_commands.describe(team_name="Name of the team to inspect.")
    async def inventory(
        self,
        interaction: discord.Interaction,
        team_name:   str,
    ) -> None:
        inv = _db_get_inventory(team_name.strip())
        coins = _db_get_coins(team_name.strip())

        em = discord.Embed(
            title=f"🎒  {team_name.strip()} — Inventory",
            colour=C_INV,
        )

        if not inv:
            em.description = "_No items in stock._"
        else:
            lines = []
            for key, qty in inv.items():
                lines.append(f"• **{VALID_ITEMS.get(key, key.title())}** × {qty}")
            em.description = "\n".join(lines)

        em.add_field(name="🪙  Heist Coins", value=f"**{coins:,}**", inline=True)
        em.set_footer(text="Bank Breakthrough  •  Inventory System")
        await interaction.response.send_message(embed=em)

    @app_commands.command(
        name="inventory-remove",
        description="[Admin] Remove items from a team's inventory.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        team_name = "Name of the team.",
        item_name = "Item to remove: smoke | c4 | adrenaline | shield | decrypter",
        quantity  = "How many to remove.",
    )
    async def inventory_remove(
        self,
        interaction: discord.Interaction,
        team_name:   str,
        item_name:   str,
        quantity:    app_commands.Range[int, 1, 99],
    ) -> None:
        key = item_name.strip().lower()
        if key not in VALID_ITEMS:
            await interaction.response.send_message(
                f"❌  Unknown item **{item_name}**.\n"
                f"Valid items: `smoke`, `c4`, `adrenaline`, `shield`, `decrypter`",
                ephemeral=True,
            )
            return

        full_name = VALID_ITEMS[key]
        had_enough, new_qty = _db_remove_item(team_name.strip(), key, quantity)

        em = discord.Embed(
            title="🗑️  Inventory Updated",
            colour=C_ERROR if not had_enough else C_ITEM,
        )
        if had_enough:
            em.description = f"Removed **{quantity}× {full_name}** from **{team_name.strip()}**."
        else:
            em.description = (
                f"⚠️  **{team_name.strip()}** didn't have enough **{full_name}** — "
                f"removed all they had. Stock is now **0**."
            )
        em.add_field(name="Remaining Stock", value=f"`{full_name}` → **{new_qty}×**", inline=True)
        em.set_footer(text="Bank Breakthrough  •  Inventory System")
        await interaction.response.send_message(embed=em, ephemeral=True)

    @app_commands.command(
        name="inventory-set",
        description="[Admin] Set a team's item quantity to an exact number.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        team_name = "Name of the team.",
        item_name = "Item to set: smoke | c4 | adrenaline | shield | decrypter",
        quantity  = "Exact quantity to set (0 clears the item).",
    )
    async def inventory_set(
        self,
        interaction: discord.Interaction,
        team_name:   str,
        item_name:   str,
        quantity:    app_commands.Range[int, 0, 99],
    ) -> None:
        key = item_name.strip().lower()
        if key not in VALID_ITEMS:
            await interaction.response.send_message(
                f"❌  Unknown item **{item_name}**.\n"
                f"Valid items: `smoke`, `c4`, `adrenaline`, `shield`, `decrypter`",
                ephemeral=True,
            )
            return

        full_name = VALID_ITEMS[key]
        _db_set_item(team_name.strip(), key, quantity)

        action = "cleared" if quantity == 0 else f"set to **{quantity}×**"
        em = discord.Embed(
            title="✏️  Inventory Set",
            colour=C_ITEM,
            description=f"**{full_name}** for **{team_name.strip()}** has been {action}.",
        )
        em.add_field(name="New Stock", value=f"`{full_name}` → **{quantity}×**", inline=True)
        em.set_footer(text="Bank Breakthrough  •  Inventory System")
        await interaction.response.send_message(embed=em, ephemeral=True)

    # ═════════════════════════ SETUP COMMAND ══════════════════════════════════

    @app_commands.command(
        name="breakthrough-setup",
        description="[Admin] Register 4 Champions and start a Bank Breakthrough match.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        champion1 = "Champion for Team 1 (Top-Left 🔴)",
        champion2 = "Champion for Team 2 (Top-Right 🔵)",
        champion3 = "Champion for Team 3 (Bottom-Left 🟢)",
        champion4 = "Champion for Team 4 (Bottom-Right 🟡)",
        team1     = "Team name for Champion 1",
        team2     = "Team name for Champion 2",
        team3     = "Team name for Champion 3",
        team4     = "Team name for Champion 4",
    )
    async def breakthrough_setup(
        self,
        interaction: discord.Interaction,
        champion1:   discord.Member,
        champion2:   discord.Member,
        champion3:   discord.Member,
        champion4:   discord.Member,
        team1:       str = "Team 1",
        team2:       str = "Team 2",
        team3:       str = "Team 3",
        team4:       str = "Team 4",
    ) -> None:
        assert interaction.guild_id is not None

        if interaction.guild_id in self._games:
            await interaction.response.send_message(
                "❌  A match is already running in this server. "
                "It must end before starting a new one.",
                ephemeral=True,
            )
            return

        players = [champion1, champion2, champion3, champion4]
        teams   = [team1.strip(), team2.strip(), team3.strip(), team4.strip()]

        # Build game state
        champions: dict[int, Champion] = {}
        user_to_slot: dict[int, int]   = {}
        for i, (member, team) in enumerate(zip(players, teams)):
            r, c = CHAMPION_STARTS[i]
            champ = Champion(
                user_id=member.id,
                team=team.lower(),
                slot=i,
                row=r,
                col=c,
            )
            champions[i]         = champ
            user_to_slot[member.id] = i

        loot = _init_grid()
        state = GameState(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            champions=champions,
            user_to_slot=user_to_slot,
            loot_tiles=loot,
        )
        self._games[interaction.guild_id] = state

        # Build setup embed
        em = discord.Embed(
            title="🏦  THE GREAT BANK BREAKTHROUGH",
            colour=C_SETUP,
            description=(
                "```ansi\n\u001b[1;33m  ⚡  CHAMPION EDITION — MATCH BEGINS!  ⚡  \u001b[0m\n```\n"
                f"{SEP}"
            ),
        )
        for i, (member, team) in enumerate(zip(players, teams)):
            r, c = CHAMPION_STARTS[i]
            em.add_field(
                name=f"{EMOJI_CHAMP[i]}  Champion {i+1} — {team}",
                value=f"{member.mention} → Start: `[{r},{c}]`",
                inline=False,
            )
        em.add_field(
            name="🏆  Central Vault",
            value=f"Position `[3,3]` — Needs **{VAULT_POINTS_REQ} Breach Points**",
            inline=False,
        )
        em.add_field(
            name="⏱️  Planning Timer",
            value=f"**{ROUND_TIMER} seconds** per round",
            inline=True,
        )
        em.add_field(
            name="📋  Commands",
            value="`/submit-move` · `/use`",
            inline=True,
        )
        em.set_footer(text=f"Bank Breakthrough  •  Champion Edition  •  {GRID_SIZE}×{GRID_SIZE} Grid")
        await interaction.response.send_message(embed=em)

        # Post the initial board
        await self._post_round(interaction.guild_id)

    # ═════════════════════════ SUBMIT-MOVE COMMAND ════════════════════════════

    @app_commands.command(
        name="submit-move",
        description="Submit your move for this round (champions only).",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        direction=(
            "Direction: up | down | left | right | hold | action | "
            "up-left | up-right | down-left | down-right (diagonal needs Adrenaline)"
        ),
    )
    async def submit_move(
        self,
        interaction: discord.Interaction,
        direction:   str,
    ) -> None:
        assert interaction.guild_id is not None
        state = self._games.get(interaction.guild_id)
        if not state or not state.active:
            await interaction.response.send_message(
                "❌  No active match right now.", ephemeral=True
            )
            return

        slot = state.user_to_slot.get(interaction.user.id)
        if slot is None:
            await interaction.response.send_message(
                "❌  You are not a registered Champion in this match.", ephemeral=True
            )
            return

        champ = state.champions[slot]
        direction = direction.strip().lower()

        # Diagonal requires adrenaline
        DIAGONALS = {"up-left", "up-right", "down-left", "down-right"}
        if direction in DIAGONALS and not champ.has_adrenaline:
            await interaction.response.send_message(
                "❌  Diagonal moves require an active **Adrenaline** (use `/use item:adrenaline` first).",
                ephemeral=True,
            )
            return

        VALID_DIRECTIONS = {"up", "down", "left", "right", "hold", "action"} | DIAGONALS
        if direction not in VALID_DIRECTIONS:
            await interaction.response.send_message(
                f"❌  Invalid direction `{direction}`.\n"
                "Valid: `up` `down` `left` `right` `hold` `action` (or diagonal with Adrenaline)",
                ephemeral=True,
            )
            return

        champ.move = direction
        await interaction.response.send_message(
            f"✅  **{champ.display_name}** — move `{direction}` registered! "
            f"Waiting for the round to resolve...",
            ephemeral=True,
        )

    # ═════════════════════════ USE ITEM COMMAND ═══════════════════════════════

    @app_commands.command(
        name="use",
        description="Use an item from your team's inventory during the planning phase.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        item   = "Item to use: smoke | c4 | adrenaline | shield | decrypter",
        target = "Target champion (required for smoke / c4)",
    )
    async def use_item(
        self,
        interaction: discord.Interaction,
        item:        str,
        target:      Optional[discord.Member] = None,
    ) -> None:
        assert interaction.guild_id is not None
        state = self._games.get(interaction.guild_id)
        if not state or not state.active:
            await interaction.response.send_message(
                "❌  No active match right now.", ephemeral=True
            )
            return

        slot = state.user_to_slot.get(interaction.user.id)
        if slot is None:
            await interaction.response.send_message(
                "❌  You are not a registered Champion in this match.", ephemeral=True
            )
            return

        champ = state.champions[slot]
        key   = item.strip().lower()

        if key not in VALID_ITEMS:
            await interaction.response.send_message(
                f"❌  Unknown item `{item}`.\n"
                f"Valid: `smoke`, `c4`, `adrenaline`, `shield`, `decrypter`",
                ephemeral=True,
            )
            return

        # Target validation for smoke & c4
        target_slot: Optional[int] = None
        if key in {"smoke", "c4"}:
            if target is None:
                await interaction.response.send_message(
                    f"❌  `{VALID_ITEMS[key]}` requires a `target` Champion.",
                    ephemeral=True,
                )
                return
            target_slot = state.user_to_slot.get(target.id)
            if target_slot is None or target_slot == slot:
                await interaction.response.send_message(
                    "❌  Invalid target — must be another registered Champion.",
                    ephemeral=True,
                )
                return

        # C4 adjacency check
        if key == "c4" and target_slot is not None:
            tc = state.champions[target_slot]
            if not _is_adjacent(champ.row, champ.col, tc.row, tc.col):
                await interaction.response.send_message(
                    "❌  **C4** can only be used on an **adjacent** Champion.",
                    ephemeral=True,
                )
                return

        # Decrypter — must be on/adjacent to vault
        if key == "decrypter":
            vr, vc = VAULT_POS
            if not (_is_adjacent(champ.row, champ.col, vr, vc) or (champ.row, champ.col) == VAULT_POS):
                await interaction.response.send_message(
                    "❌  **Decrypter Card** must be used while standing on or adjacent to the Vault 🏆.",
                    ephemeral=True,
                )
                return

        # Check team inventory and deduct
        if not _db_deduct_item(champ.team, key):
            await interaction.response.send_message(
                f"❌  **{champ.team.title()}** has no **{VALID_ITEMS[key]}** left in inventory.",
                ephemeral=True,
            )
            return

        # Register the item usage
        champ.used_item   = key
        champ.item_target = target_slot

        # Instant effects
        if key == "adrenaline":
            champ.has_adrenaline = True
            msg = "🟠 **Adrenaline** activated! You may submit a diagonal move this round."
        elif key == "shield":
            champ.shield_rounds = 3
            msg = "🛡️ **Riot Shield** activated! Protected from C4 and collisions for 3 rounds."
        elif key == "smoke":
            msg = f"💨 **Smoke Grenade** queued against {target.mention}! Their move will be randomised on resolution."
        elif key == "c4":
            msg = f"💣 **C4 Charge** queued on {target.mention}! They'll be blasted back when the round resolves."
        elif key == "decrypter":
            msg = "💾 **Decrypter Card** queued! It will count as 2 Breach Points if you `action` the vault this round."
        else:
            msg = f"✅ **{VALID_ITEMS[key]}** queued."

        await interaction.response.send_message(msg, ephemeral=True)

    # ═════════════════════════ ROUND ENGINE ════════════════════════════════════

    async def _post_round(self, guild_id: int) -> None:
        """Post a new round embed and start the 90-second countdown."""
        state = self._games.get(guild_id)
        if not state:
            return

        state.round_number += 1
        channel = self.bot.get_channel(state.channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            return

        # Reset all champion inputs
        for champ in state.champions.values():
            champ.reset_round_inputs()

        grid_str = _render_grid(state)
        em = self._round_embed(state, grid_str)

        # Post or edit board message
        msg = await channel.send(embed=em)
        state.board_message = msg

        # Start the countdown task
        if state.timer_task:
            state.timer_task.cancel()
        state.timer_task = asyncio.create_task(
            self._round_countdown(guild_id, channel)
        )

    async def _round_countdown(
        self, guild_id: int, channel: discord.TextChannel
    ) -> None:
        """Wait ROUND_TIMER seconds, then resolve the round."""
        await asyncio.sleep(ROUND_TIMER)
        state = self._games.get(guild_id)
        if not state or not state.active:
            return
        await self._resolve_round(guild_id, channel)

    async def _resolve_round(
        self, guild_id: int, channel: discord.TextChannel
    ) -> None:
        """Simultaneous resolution: items → movement → looting → vault."""
        state = self._games.get(guild_id)
        if not state:
            return

        log_lines: list[str] = []

        # ── 1. Default missing moves to 'hold' ────────────────────────────────
        for champ in state.champions.values():
            if champ.move is None:
                champ.move = "hold"
                log_lines.append(
                    f"⏰ **{champ.display_name}** ({champ.team.title()}) failed to move — defaulting to **hold**."
                )

        # ── 2. Apply Smoke effects ────────────────────────────────────────────
        for champ in state.champions.values():
            if champ.used_item == "smoke" and champ.item_target is not None:
                target = state.champions.get(champ.item_target)
                if target:
                    target.smoke_this_round = True
                    original = target.move
                    dirs = ["up", "down", "left", "right", "hold"]
                    target.move = random.choice(dirs)
                    log_lines.append(
                        f"💨 **{champ.display_name}** lobbed a Smoke Grenade at **{target.display_name}**! "
                        f"Their move was scrambled: `{original}` → `{target.move}`"
                    )

        # ── 3. Apply C4 blasts ────────────────────────────────────────────────
        c4_blasts: dict[int, int] = {}  # target_slot → source_slot
        for champ in state.champions.values():
            if champ.used_item == "c4" and champ.item_target is not None:
                target = state.champions.get(champ.item_target)
                if target:
                    if target.shield_rounds > 0:
                        log_lines.append(
                            f"💣 **{champ.display_name}** detonated C4 on **{target.display_name}** "
                            f"— but the **Riot Shield** absorbed it! 🛡️"
                        )
                    else:
                        c4_blasts[target.slot] = champ.slot
                        # Blast target 2 tiles backward (away from source) + freeze
                        dr = target.row - champ.row
                        dc = target.col - champ.col
                        # Normalise direction
                        if dr != 0: dr = dr // abs(dr)
                        if dc != 0: dc = dc // abs(dc)
                        target.row = max(0, min(GRID_SIZE - 1, target.row + dr * 2))
                        target.col = max(0, min(GRID_SIZE - 1, target.col + dc * 2))
                        target.frozen_rounds = 1   # frozen for next round
                        target.move = "hold"        # override move
                        log_lines.append(
                            f"💣 **{champ.display_name}** detonated C4 on **{target.display_name}**! "
                            f"Blasted to `[{target.row},{target.col}]` and **frozen** next round! ❄️"
                        )

        # ── 4. Calculate new positions ────────────────────────────────────────
        proposed: dict[int, tuple[int, int]] = {}
        old_positions: dict[int, tuple[int, int]] = {
            slot: (champ.row, champ.col)
            for slot, champ in state.champions.items()
        }

        for slot, champ in state.champions.items():
            if slot in c4_blasts:
                # Already moved by C4
                proposed[slot] = (champ.row, champ.col)
                continue
            if champ.frozen_rounds > 0:
                champ.frozen_rounds -= 1
                proposed[slot] = (champ.row, champ.col)
                if champ.frozen_rounds == 0:
                    log_lines.append(f"❄️ **{champ.display_name}** thawed — free to move next round.")
                continue

            nr, nc = _apply_direction(champ.row, champ.col, champ.move)
            proposed[slot] = (nr, nc)

        # ── 5. Collision detection ────────────────────────────────────────────
        # Group champions heading to the same tile
        dest_map: dict[tuple[int, int], list[int]] = {}
        for slot, pos in proposed.items():
            dest_map.setdefault(pos, []).append(slot)

        final: dict[int, tuple[int, int]] = {}
        for pos, slots in dest_map.items():
            if len(slots) == 1:
                final[slots[0]] = pos
            else:
                # Collision — check for shields
                shielded   = [s for s in slots if state.champions[s].shield_rounds > 0]
                unshielded = [s for s in slots if state.champions[s].shield_rounds == 0]

                if len(shielded) == 1:
                    # Shield wins the tile; others bounce back
                    winner = shielded[0]
                    final[winner] = pos
                    log_lines.append(
                        f"🛡️ **{state.champions[winner].display_name}** won a collision at `{pos}` with a Riot Shield!"
                    )
                    for loser in unshielded:
                        final[loser] = old_positions[loser]
                        log_lines.append(
                            f"💥 **{state.champions[loser].display_name}** bounced back to "
                            f"`{old_positions[loser]}` after colliding!"
                        )
                else:
                    # All bounce back
                    for s in slots:
                        final[s] = old_positions[s]
                        log_lines.append(
                            f"💥 **{state.champions[s].display_name}** collided and bounced back to "
                            f"`{old_positions[s]}`!"
                        )

        # Apply final positions
        for slot, (r, c) in final.items():
            state.champions[slot].row = r
            state.champions[slot].col = c

        # ── 6. Decrement shield rounds ────────────────────────────────────────
        for champ in state.champions.values():
            if champ.shield_rounds > 0:
                champ.shield_rounds -= 1

        # ── 7. Looting ────────────────────────────────────────────────────────
        for slot, champ in state.champions.items():
            pos = (champ.row, champ.col)
            if pos in state.loot_tiles:
                state.loot_tiles.discard(pos)
                new_bal = _db_add_coins(champ.team, LOOT_REWARD)
                log_lines.append(
                    f"💰 **{champ.display_name}** ({champ.team.title()}) looted a cache! "
                    f"**+{LOOT_REWARD:,} Heist Coins** → total: **{new_bal:,}** 🪙"
                )

        # ── 8. Vault breaching ────────────────────────────────────────────────
        game_over = False
        winner_team: Optional[str] = None
        for slot, champ in state.champions.items():
            if (champ.row, champ.col) == VAULT_POS and champ.move == "action":
                points = 2 if champ.used_item == "decrypter" else 1
                state.breach_points += points
                tag = " (Decrypter ×2!) 💾" if points == 2 else ""
                log_lines.append(
                    f"🏆 **{champ.display_name}** ({champ.team.title()}) breached the vault{tag}! "
                    f"Breach Points: **{state.breach_points}/{VAULT_POINTS_REQ}**"
                )
                if state.breach_points >= VAULT_POINTS_REQ:
                    game_over = True
                    winner_team = champ.team

        # ── 9. Post action log and updated board ─────────────────────────────
        grid_str = _render_grid(state)

        if game_over and winner_team:
            state.active = False
            self._games.pop(guild_id, None)
            await self._post_victory(channel, state, winner_team, log_lines, grid_str)
            return

        # Build round summary embed
        em = self._round_embed(state, grid_str)
        if state.board_message:
            try:
                await state.board_message.edit(embed=em)
            except discord.HTTPException:
                pass

        if log_lines:
            log_em = discord.Embed(
                title=f"📋  Round {state.round_number} — Action Log",
                colour=C_ACTION,
                description="\n".join(log_lines),
            )
            log_em.set_footer(text=f"Vault: {state.breach_points}/{VAULT_POINTS_REQ} Breach Points")
            await channel.send(embed=log_em)

        # Start next round
        await self._post_round(guild_id)

    # ─────────────────────────── Embeds ──────────────────────────────────────

    def _round_embed(self, state: GameState, grid_str: str) -> discord.Embed:
        em = discord.Embed(
            title=f"🏦  Bank Breakthrough — Round {state.round_number}",
            colour=C_ROUND,
            description=f"```\n{grid_str}\n```",
        )
        # Status per champion
        champ_lines = []
        for slot, champ in state.champions.items():
            status_tags = []
            if champ.frozen_rounds > 0:
                status_tags.append(f"❄️ Frozen ({champ.frozen_rounds}r)")
            if champ.shield_rounds > 0:
                status_tags.append(f"🛡️ Shielded ({champ.shield_rounds}r)")
            status = " | ".join(status_tags) if status_tags else "Active"
            champ_lines.append(
                f"{champ.emoji} **C{slot+1}** ({champ.team.title()}) → `[{champ.row},{champ.col}]` — {status}"
            )
        em.add_field(name="Champions", value="\n".join(champ_lines), inline=False)
        em.add_field(name="🏆 Vault Progress", value=f"**{state.breach_points}/{VAULT_POINTS_REQ}** Breach Points", inline=True)
        em.add_field(name="⏱️ Planning", value=f"**{ROUND_TIMER}s** — Use `/submit-move` & `/use`", inline=True)
        em.set_footer(text=f"Bank Breakthrough  •  Champion Edition  •  Loot Tiles: {len(state.loot_tiles)}")
        return em

    async def _post_victory(
        self,
        channel: discord.TextChannel,
        state: GameState,
        winner_team: str,
        log_lines: list[str],
        grid_str: str,
    ) -> None:
        # Post final log
        if log_lines:
            log_em = discord.Embed(
                title=f"📋  Round {state.round_number} — Final Action Log",
                colour=C_VAULT,
                description="\n".join(log_lines),
            )
            await channel.send(embed=log_em)

        # Find the champion on the winning team who triggered the final breach
        winner_champ = next(
            (c for c in state.champions.values() if c.team == winner_team), None
        )

        em = discord.Embed(
            title="💥  THE VAULT HAS BEEN CRACKED!  💥",
            colour=C_WIN,
            description=(
                f"```ansi\n\u001b[1;33m  🏆  HEIST COMPLETE!  🏆  \u001b[0m\n```\n"
                f"{SEP}\n"
                f"**{winner_team.title()}** has breached the Central Vault!\n"
                f"{SEP}"
            ),
        )
        em.add_field(
            name="🗺️  Final Board",
            value=f"```\n{grid_str}\n```",
            inline=False,
        )
        if winner_champ:
            em.add_field(
                name="🏆  Winning Team",
                value=f"**{winner_team.title()}** — <@{winner_champ.user_id}> led the final breach!",
                inline=False,
            )
        em.add_field(
            name="📊  Breach Points",
            value=f"**{state.breach_points}/{VAULT_POINTS_REQ}** — Vault Unlocked",
            inline=True,
        )
        em.set_footer(text="Bank Breakthrough  •  Champion Edition  •  Match Over")
        await channel.send(embed=em)

    # ─────────────────────── Admin: force-end ────────────────────────────────

    @app_commands.command(
        name="breakthrough-end",
        description="[Admin] Force-end an active Bank Breakthrough match.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def breakthrough_end(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        state = self._games.pop(interaction.guild_id, None)
        if not state:
            await interaction.response.send_message(
                "❌  No active match to end.", ephemeral=True
            )
            return
        if state.timer_task:
            state.timer_task.cancel()
        state.active = False
        await interaction.response.send_message(
            "🛑  Match forcefully ended by admin.", ephemeral=False
        )

    # ─────────────────────── Admin: check status ────────────────────────────

    @app_commands.command(
        name="breakthrough-status",
        description="Show the current match status and board.",
    )
    @app_commands.guild_only()
    async def breakthrough_status(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        state = self._games.get(interaction.guild_id)
        if not state or not state.active:
            await interaction.response.send_message(
                "❌  No active match right now.", ephemeral=True
            )
            return
        grid_str = _render_grid(state)
        em = self._round_embed(state, grid_str)
        await interaction.response.send_message(embed=em, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BankBreakthroughCog(bot))
