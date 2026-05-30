"""
cogs/bank_breakthrough.py
─────────────────────────────────────────────────────────────────────────────
The Great Bank Breakthrough: Champion Edition
─────────────────────────────────────────────────────────────────────────────

A turn-based multiplayer strategy game for Money Heist events.
One Champion per team (4 total) competes on a shared 7×7 grid.

Admin commands:
  /team-add       [team_name] [@member]            — assign a member to a team
  /team-remove    [@member]                        — remove a member from any team
  /team-list      [team_name]                      — list members of a team
  /inventory-give [team_name] [item_name] [qty]    — give items to a team
  /inventory-remove [team_name] [item_name] [qty]  — remove items from a team
  /inventory-set  [team_name] [item_name] [qty]    — hard-set item quantity
  /breakthrough-setup [u1] [u2] [u3] [u4]          — start a match (teams auto-detected)

Player commands:
  /inventory      — privately view YOUR team's inventory (only you can see it)

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
import math
from io import BytesIO
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
C_TEAM     = 0x1ABC9C   # teal

# ──────────────────────────── Constants ──────────────────────────────────────

GRID_SIZE        = 7
VAULT_POS        = (3, 3)
VAULT_POINTS_REQ = 3
LOOT_REWARD      = 1500
ROUND_TIMER      = 90   # seconds

VALID_ITEMS = {
    "smoke":      "Smoke Grenade",
    "c4":         "C4 Charge",
    "adrenaline": "Adrenaline",
    "shield":     "Riot Shield",
    "decrypter":  "Decrypter Card",
}

CHAMPION_STARTS = [(0, 0), (0, 6), (6, 0), (6, 6)]

EMOJI_EMPTY   = "⬛"
EMOJI_LOOT    = "💰"
EMOJI_VAULT   = "🏆"
EMOJI_CHAMP   = ["🔴", "🔵", "🟢", "🟡"]

SEP = "▬" * 22

# ──────────────────────────── Database layer ─────────────────────────────────

_DB_URL = os.environ.get("DATABASE_URL", "")


def _db_connect():
    if not _DB_URL:
        raise RuntimeError("DATABASE_URL not set in environment")
    return psycopg2.connect(_DB_URL, sslmode="require")


def _db_init() -> None:
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
            cur.execute("""
                CREATE TABLE IF NOT EXISTS breakthrough_teams (
                    guild_id   BIGINT  NOT NULL,
                    user_id    BIGINT  NOT NULL,
                    team_name  TEXT    NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
        con.commit()


# ── Inventory DB ──────────────────────────────────────────────────────────────

def _db_give_item(team: str, item_key: str, qty: int) -> int:
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
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT item_key, quantity FROM breakthrough_inventory "
                "WHERE team_name = %s AND quantity > 0",
                (team.lower(),),
            )
            return {row[0]: row[1] for row in cur.fetchall()}


def _db_deduct_item(team: str, item_key: str, qty: int = 1) -> bool:
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT quantity FROM breakthrough_inventory "
                "WHERE team_name = %s AND item_key = %s",
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
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT quantity FROM breakthrough_inventory "
                "WHERE team_name = %s AND item_key = %s",
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
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute("""
                INSERT INTO breakthrough_inventory (team_name, item_key, quantity)
                VALUES (%s, %s, %s)
                ON CONFLICT (team_name, item_key)
                DO UPDATE SET quantity = EXCLUDED.quantity
            """, (team.lower(), item_key, qty))
        con.commit()


# ── Coins DB ──────────────────────────────────────────────────────────────────

def _db_add_coins(team: str, amount: int) -> int:
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute("""
                INSERT INTO breakthrough_coins (team_name, coins) VALUES (%s, %s)
                ON CONFLICT (team_name)
                DO UPDATE SET coins = breakthrough_coins.coins + EXCLUDED.coins
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


def _db_get_all_coins() -> list[tuple[str, int]]:
    """Return [(team_name, coins), ...] sorted highest first."""
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT team_name, coins FROM breakthrough_coins ORDER BY coins DESC"
            )
            return [(row[0], row[1]) for row in cur.fetchall()]


# ── Team membership DB ────────────────────────────────────────────────────────

def _db_team_add(guild_id: int, user_id: int, team_name: str) -> None:
    """Assign (or reassign) a user to a team in this guild."""
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute("""
                INSERT INTO breakthrough_teams (guild_id, user_id, team_name)
                VALUES (%s, %s, %s)
                ON CONFLICT (guild_id, user_id)
                DO UPDATE SET team_name = EXCLUDED.team_name
            """, (guild_id, user_id, team_name.lower()))
        con.commit()


def _db_team_remove(guild_id: int, user_id: int) -> bool:
    """Remove a user from their team. Returns True if they were in one."""
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "DELETE FROM breakthrough_teams WHERE guild_id = %s AND user_id = %s",
                (guild_id, user_id),
            )
            deleted = cur.rowcount > 0
        con.commit()
        return deleted


def _db_get_user_team(guild_id: int, user_id: int) -> Optional[str]:
    """Return the team name for a user, or None if not assigned."""
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT team_name FROM breakthrough_teams "
                "WHERE guild_id = %s AND user_id = %s",
                (guild_id, user_id),
            )
            row = cur.fetchone()
            return row[0] if row else None


def _db_get_team_members(guild_id: int, team_name: str) -> list[int]:
    """Return list of user_ids belonging to a team in this guild."""
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM breakthrough_teams "
                "WHERE guild_id = %s AND team_name = %s",
                (guild_id, team_name.lower()),
            )
            return [row[0] for row in cur.fetchall()]


def _db_get_all_teams(guild_id: int) -> dict[str, list[int]]:
    """Return {team_name: [user_ids]} for all teams in this guild."""
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT team_name, user_id FROM breakthrough_teams WHERE guild_id = %s",
                (guild_id,),
            )
            result: dict[str, list[int]] = {}
            for team, uid in cur.fetchall():
                result.setdefault(team, []).append(uid)
            return result


# ──────────────────────────── Data models ────────────────────────────────────

@dataclass
class Champion:
    user_id:    int
    team:       str
    slot:       int
    row:        int
    col:        int

    frozen_rounds:    int  = 0
    shield_rounds:    int  = 0
    smoke_this_round: bool = False

    move:            Optional[str] = None
    used_item:       Optional[str] = None
    item_target:     Optional[int] = None
    has_adrenaline:  bool = False

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

    champions:     dict[int, Champion]      = field(default_factory=dict)
    user_to_slot:  dict[int, int]           = field(default_factory=dict)
    loot_tiles:    set[tuple[int, int]]     = field(default_factory=set)

    breach_points: int   = 0
    round_number:  int   = 0
    active:        bool  = True

    board_message: Optional[discord.Message] = None
    pending_c4:    dict[int, int]            = field(default_factory=dict)
    timer_task:    Optional[asyncio.Task]    = None


# ──────────────────────────── Grid helpers ───────────────────────────────────

def _init_grid() -> set[tuple[int, int]]:
    taken = set(CHAMPION_STARTS) | {VAULT_POS}
    candidates = [
        (r, c)
        for r in range(GRID_SIZE)
        for c in range(GRID_SIZE)
        if (r, c) not in taken
    ]
    return set(random.sample(candidates, 4))


def _render_grid(state: GameState) -> str:
    """Fallback emoji grid (used if Pillow unavailable)."""
    grid = [[EMOJI_EMPTY for _ in range(GRID_SIZE)] for _ in range(GRID_SIZE)]
    vr, vc = VAULT_POS
    grid[vr][vc] = EMOJI_VAULT
    for (r, c) in state.loot_tiles:
        grid[r][c] = EMOJI_LOOT
    for slot, champ in state.champions.items():
        grid[champ.row][champ.col] = champ.emoji
    return "\n".join("".join(row) for row in grid)


def _render_grid_image(state: GameState) -> Optional[BytesIO]:
    """
    Render the 7×7 game board as a styled PNG image.
    Returns a BytesIO buffer, or None if Pillow is unavailable.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    # ── Layout constants ──────────────────────────────────────────────────────
    CELL     = 82          # pixels per cell
    PAD      = 48          # outer padding
    LABEL    = 20          # axis-label strip width
    W = PAD + LABEL + GRID_SIZE * CELL + PAD
    H = PAD + LABEL + GRID_SIZE * CELL + PAD

    # ── Palette ───────────────────────────────────────────────────────────────
    BG          = (8,  10,  20)
    BOARD_BG    = (14, 20,  38)
    CELL_A      = (24, 32,  56)      # checkerboard tile A
    CELL_B      = (20, 28,  50)      # checkerboard tile B
    BORDER      = (45, 60, 105)
    GRID_LINE   = (35, 48,  85)

    LOOT_BG     = (85,  58,   5)
    LOOT_RING   = (255, 195,  20)
    LOOT_FG     = (255, 215,  50)

    VAULT_BG    = (70,  50,   0)
    VAULT_RING  = (255, 200,   0)
    VAULT_GLOW  = (255, 235,  80)
    VAULT_INNER = (255, 250, 130)

    CHAMP_COLS = [
        (220,  55,  55),   # slot 0 — red
        ( 55, 120, 230),   # slot 1 — blue
        ( 40, 195,  80),   # slot 2 — green
        (220, 175,   0),   # slot 3 — yellow
    ]
    FROZEN_RING  = (100, 185, 255)
    SHIELD_RING  = (200, 220, 255)
    TEXT_BRIGHT  = (245, 248, 255)
    TEXT_DIM     = ( 90, 110, 160)
    TEXT_DARK    = ( 15,  20,  40)

    # ── Image + draw ──────────────────────────────────────────────────────────
    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Fonts (Pillow 10 default scalable font)
    try:
        f_lg = ImageFont.load_default(size=18)
        f_md = ImageFont.load_default(size=14)
        f_sm = ImageFont.load_default(size=11)
    except Exception:
        f_lg = f_md = f_sm = ImageFont.load_default()

    # ── Board background panel ────────────────────────────────────────────────
    bx0 = PAD + LABEL
    by0 = PAD + LABEL
    bx1 = bx0 + GRID_SIZE * CELL
    by1 = by0 + GRID_SIZE * CELL
    draw.rectangle([bx0 - 5, by0 - 5, bx1 + 5, by1 + 5],
                   fill=BOARD_BG, outline=BORDER, width=4)

    # ── Axis coordinate labels ────────────────────────────────────────────────
    for i in range(GRID_SIZE):
        cx = bx0 + i * CELL + CELL // 2
        cy = by0 + i * CELL + CELL // 2
        draw.text((cx, by0 - 11), str(i), fill=TEXT_DIM, font=f_sm, anchor="mm")
        draw.text((bx0 - 11, cy), str(i), fill=TEXT_DIM, font=f_sm, anchor="mm")

    # ── Champion lookup ───────────────────────────────────────────────────────
    champ_at: dict[tuple[int, int], int] = {
        (c.row, c.col): slot for slot, c in state.champions.items()
    }

    # ── Draw cells ───────────────────────────────────────────────────────────
    for row in range(GRID_SIZE):
        for col in range(GRID_SIZE):
            x0 = bx0 + col * CELL + 2
            y0 = by0 + row * CELL + 2
            x1 = x0 + CELL - 4
            y1 = y0 + CELL - 4
            cx = (x0 + x1) // 2
            cy = (y0 + y1) // 2
            pos = (row, col)

            is_vault = (pos == VAULT_POS)
            is_loot  = (pos in state.loot_tiles)
            slot     = champ_at.get(pos)

            # ── Base tile ────────────────────────────────────────────────────
            base = CELL_A if (row + col) % 2 == 0 else CELL_B
            draw.rounded_rectangle([x0, y0, x1, y1], radius=7,
                                   fill=base, outline=GRID_LINE, width=1)

            # ── Vault tile ───────────────────────────────────────────────────
            if is_vault:
                draw.rounded_rectangle([x0, y0, x1, y1], radius=7,
                                       fill=VAULT_BG, outline=VAULT_RING, width=3)
                # outer glow ring
                r = 27
                draw.ellipse([cx - r - 4, cy - r - 4, cx + r + 4, cy + r + 4],
                             fill=None, outline=(180, 130, 0), width=2)
                # inner filled circle
                draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                             fill=VAULT_GLOW, outline=VAULT_RING, width=2)
                # star / crown symbol
                draw.text((cx, cy - 3), "★", fill=VAULT_INNER, font=f_lg, anchor="mm")
                draw.text((cx, y1 - 9), "VAULT", fill=VAULT_RING, font=f_sm, anchor="mm")

            # ── Loot tile ────────────────────────────────────────────────────
            elif is_loot:
                draw.rounded_rectangle([x0, y0, x1, y1], radius=7,
                                       fill=LOOT_BG, outline=LOOT_RING, width=2)
                r = 22
                draw.ellipse([cx - r, cy - r - 4, cx + r, cy + r - 4],
                             fill=(140, 95, 10), outline=LOOT_RING, width=2)
                draw.text((cx, cy - 4), "$", fill=LOOT_FG, font=f_lg, anchor="mm")
                draw.text((cx, y1 - 9), "LOOT", fill=LOOT_RING, font=f_sm, anchor="mm")

            # ── Champion token ───────────────────────────────────────────────
            if slot is not None:
                champ     = state.champions[slot]
                col_rgb   = CHAMP_COLS[slot]
                r = 27

                # Drop shadow
                draw.ellipse([cx - r + 3, cy - r + 3, cx + r + 3, cy + r + 3],
                             fill=(0, 0, 0))
                # Main circle
                draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                             fill=col_rgb, outline=TEXT_BRIGHT, width=2)
                # Highlight shimmer (small arc at top-left)
                hi = 10
                draw.ellipse([cx - r + 5, cy - r + 5,
                              cx - r + 5 + hi, cy - r + 5 + hi],
                             fill=(255, 255, 255, 80))

                # Champion label  e.g. "C1"
                draw.text((cx, cy - 6), f"C{slot + 1}",
                          fill=TEXT_BRIGHT, font=f_md, anchor="mm")
                # Team abbreviation (up to 5 chars)
                draw.text((cx, cy + 8), champ.team[:5].upper(),
                          fill=TEXT_BRIGHT, font=f_sm, anchor="mm")

                # Frozen ring ❄
                if champ.frozen_rounds > 0:
                    draw.ellipse([cx - r - 5, cy - r - 5, cx + r + 5, cy + r + 5],
                                 fill=None, outline=FROZEN_RING, width=3)
                    draw.text((cx + r - 2, cy - r + 2), "❄",
                              fill=FROZEN_RING, font=f_sm, anchor="mm")

                # Shield ring
                if champ.shield_rounds > 0:
                    draw.ellipse([cx - r - 9, cy - r - 9, cx + r + 9, cy + r + 9],
                                 fill=None, outline=SHIELD_RING, width=2)

    # ── Legend bar at the bottom ──────────────────────────────────────────────
    legend_y = by1 + 12
    items = [
        ((255, 200, 0),  "VAULT"),
        ((255, 195, 20), "LOOT"),
        ((220, 55, 55),  "C1"),
        ((55, 120, 230), "C2"),
        ((40, 195, 80),  "C3"),
        ((220, 175, 0),  "C4"),
    ]
    lx = bx0
    for colour, label in items:
        draw.rectangle([lx, legend_y, lx + 12, legend_y + 12],
                       fill=colour, outline=BORDER, width=1)
        draw.text((lx + 16, legend_y + 6), label,
                  fill=TEXT_DIM, font=f_sm, anchor="lm")
        lx += 16 + draw.textlength(label, font=f_sm) + 14

    buf = BytesIO()
    img.save(buf, "PNG", optimize=True)
    buf.seek(0)
    return buf


def _apply_direction(row: int, col: int, direction: str) -> tuple[int, int]:
    DIRECTIONS = {
        "up":         (-1,  0),
        "down":       ( 1,  0),
        "left":       ( 0, -1),
        "right":      ( 0,  1),
        "hold":       ( 0,  0),
        "action":     ( 0,  0),
        "up-left":    (-1, -1),
        "up-right":   (-1,  1),
        "down-left":  ( 1, -1),
        "down-right": ( 1,  1),
    }
    dr, dc = DIRECTIONS.get(direction, (0, 0))
    return max(0, min(GRID_SIZE - 1, row + dr)), max(0, min(GRID_SIZE - 1, col + dc))


def _is_adjacent(r1: int, c1: int, r2: int, c2: int) -> bool:
    return abs(r1 - r2) <= 1 and abs(c1 - c2) <= 1 and (r1, c1) != (r2, c2)


# ──────────────────────────── Cog ────────────────────────────────────────────

class BankBreakthroughCog(commands.Cog, name="BankBreakthrough"):
    """The Great Bank Breakthrough: Champion Edition"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        _db_init()
        self._games: dict[int, GameState] = {}

    # ═════════════════════════ TEAM MANAGEMENT ════════════════════════════════

    @app_commands.command(
        name="team-add",
        description="[Admin] Assign a member to a team.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        team_name = "Team name (e.g. 'Blue', 'Red').",
        member    = "The member to assign.",
    )
    async def team_add(
        self,
        interaction: discord.Interaction,
        team_name:   str,
        member:      discord.Member,
    ) -> None:
        assert interaction.guild_id is not None
        team = team_name.strip()
        _db_team_add(interaction.guild_id, member.id, team)

        em = discord.Embed(
            title="✅  Team Updated",
            colour=C_TEAM,
            description=f"{member.mention} has been added to **{team.title()}**.",
        )
        em.set_footer(text="Bank Breakthrough  •  Team System")
        await interaction.response.send_message(embed=em, ephemeral=True)

    @app_commands.command(
        name="team-remove",
        description="[Admin] Remove a member from their team.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(member="The member to remove from their team.")
    async def team_remove(
        self,
        interaction: discord.Interaction,
        member:      discord.Member,
    ) -> None:
        assert interaction.guild_id is not None
        removed = _db_team_remove(interaction.guild_id, member.id)

        if removed:
            desc = f"{member.mention} has been removed from their team."
            colour = C_TEAM
        else:
            desc = f"{member.mention} wasn't assigned to any team."
            colour = C_ERROR

        em = discord.Embed(title="🗑️  Team Updated", colour=colour, description=desc)
        em.set_footer(text="Bank Breakthrough  •  Team System")
        await interaction.response.send_message(embed=em, ephemeral=True)

    @app_commands.command(
        name="team-list",
        description="List all members of a team (or all teams if no name given).",
    )
    @app_commands.guild_only()
    @app_commands.describe(team_name="Team name to inspect, or leave blank for all teams.")
    async def team_list(
        self,
        interaction: discord.Interaction,
        team_name:   Optional[str] = None,
    ) -> None:
        assert interaction.guild_id is not None

        em = discord.Embed(title="👥  Team Roster", colour=C_TEAM)

        if team_name:
            members = _db_get_team_members(interaction.guild_id, team_name.strip())
            if members:
                lines = [f"<@{uid}>" for uid in members]
                em.add_field(
                    name=f"**{team_name.strip().title()}**",
                    value="\n".join(lines),
                    inline=False,
                )
            else:
                em.description = f"_No members in **{team_name.strip().title()}**._"
        else:
            all_teams = _db_get_all_teams(interaction.guild_id)
            if not all_teams:
                em.description = "_No teams configured yet. Use `/team-add` to assign members._"
            else:
                for tname, uids in sorted(all_teams.items()):
                    lines = [f"<@{uid}>" for uid in uids]
                    em.add_field(
                        name=f"**{tname.title()}** ({len(uids)} member{'s' if len(uids) != 1 else ''})",
                        value="\n".join(lines),
                        inline=True,
                    )

        em.set_footer(text="Bank Breakthrough  •  Team System")
        await interaction.response.send_message(embed=em)

    # ═════════════════════════ INVENTORY COMMANDS ═════════════════════════════

    @app_commands.command(
        name="inventory-give",
        description="[Admin] Give items to a team's inventory.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        team_name = "Name of the team.",
        item_name = "Item: smoke | c4 | adrenaline | shield | decrypter",
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
                "Valid: `smoke` `c4` `adrenaline` `shield` `decrypter`",
                ephemeral=True,
            )
            return
        new_qty = _db_give_item(team_name.strip(), key, quantity)
        em = discord.Embed(
            title="✅  Inventory Updated",
            colour=C_ITEM,
            description=f"**{team_name.strip().title()}** received **{quantity}× {VALID_ITEMS[key]}**.",
        )
        em.add_field(name="New Stock", value=f"**{new_qty}×**", inline=True)
        em.set_footer(text="Bank Breakthrough  •  Inventory System")
        await interaction.response.send_message(embed=em, ephemeral=True)

    @app_commands.command(
        name="inventory-remove",
        description="[Admin] Remove items from a team's inventory.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        team_name = "Name of the team.",
        item_name = "Item: smoke | c4 | adrenaline | shield | decrypter",
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
                "Valid: `smoke` `c4` `adrenaline` `shield` `decrypter`",
                ephemeral=True,
            )
            return
        had_enough, new_qty = _db_remove_item(team_name.strip(), key, quantity)
        em = discord.Embed(
            title="🗑️  Inventory Updated",
            colour=C_ERROR if not had_enough else C_ITEM,
        )
        if had_enough:
            em.description = f"Removed **{quantity}× {VALID_ITEMS[key]}** from **{team_name.strip().title()}**."
        else:
            em.description = (
                f"⚠️  **{team_name.strip().title()}** didn't have enough — "
                f"removed all they had. Stock is now **0**."
            )
        em.add_field(name="Remaining Stock", value=f"**{new_qty}×**", inline=True)
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
        item_name = "Item: smoke | c4 | adrenaline | shield | decrypter",
        quantity  = "Exact quantity to set (0 to clear).",
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
                "Valid: `smoke` `c4` `adrenaline` `shield` `decrypter`",
                ephemeral=True,
            )
            return
        _db_set_item(team_name.strip(), key, quantity)
        action = "cleared" if quantity == 0 else f"set to **{quantity}×**"
        em = discord.Embed(
            title="✏️  Inventory Set",
            colour=C_ITEM,
            description=f"**{VALID_ITEMS[key]}** for **{team_name.strip().title()}** {action}.",
        )
        em.add_field(name="New Stock", value=f"**{quantity}×**", inline=True)
        em.set_footer(text="Bank Breakthrough  •  Inventory System")
        await interaction.response.send_message(embed=em, ephemeral=True)

    @app_commands.command(
        name="inventory",
        description="View your team's inventory (only visible to you).",
    )
    @app_commands.guild_only()
    async def inventory(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None

        team = _db_get_user_team(interaction.guild_id, interaction.user.id)
        if not team:
            await interaction.response.send_message(
                "❌  You haven't been assigned to a team yet. Ask an admin to use `/team-add`.",
                ephemeral=True,
            )
            return

        inv   = _db_get_inventory(team)
        coins = _db_get_coins(team)

        em = discord.Embed(
            title=f"🎒  {team.title()} — Your Team's Inventory",
            colour=C_INV,
        )
        if not inv:
            em.description = "_No items in stock._"
        else:
            lines = [f"• **{VALID_ITEMS.get(k, k.title())}** × {q}" for k, q in inv.items()]
            em.description = "\n".join(lines)

        em.add_field(name="🪙  Heist Coins", value=f"**{coins:,}**", inline=True)
        em.set_footer(text="Bank Breakthrough  •  Only you can see this")
        # ephemeral=True means ONLY the caller sees this response
        await interaction.response.send_message(embed=em, ephemeral=True)

    # ═════════════════════════ SETUP COMMAND ══════════════════════════════════

    @app_commands.command(
        name="breakthrough-setup",
        description="[Admin] Register 4 Champions and start a match. Teams are auto-detected.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        champion1 = "Champion for slot 1 (Top-Left 🔴)",
        champion2 = "Champion for slot 2 (Top-Right 🔵)",
        champion3 = "Champion for slot 3 (Bottom-Left 🟢)",
        champion4 = "Champion for slot 4 (Bottom-Right 🟡)",
    )
    async def breakthrough_setup(
        self,
        interaction: discord.Interaction,
        champion1:   discord.Member,
        champion2:   discord.Member,
        champion3:   discord.Member,
        champion4:   discord.Member,
    ) -> None:
        assert interaction.guild_id is not None

        if interaction.guild_id in self._games:
            await interaction.response.send_message(
                "❌  A match is already running. End it first with `/breakthrough-end`.",
                ephemeral=True,
            )
            return

        players = [champion1, champion2, champion3, champion4]

        # Auto-detect each champion's team from the membership table
        teams: list[str] = []
        missing: list[discord.Member] = []
        for member in players:
            t = _db_get_user_team(interaction.guild_id, member.id)
            if t is None:
                missing.append(member)
            else:
                teams.append(t)

        if missing:
            names = ", ".join(m.mention for m in missing)
            await interaction.response.send_message(
                f"❌  The following champions don't have a team assigned yet: {names}\n"
                "Use `/team-add [team_name] [@member]` to assign them first.",
                ephemeral=True,
            )
            return

        # Build game state
        champions:    dict[int, Champion] = {}
        user_to_slot: dict[int, int]      = {}
        for i, (member, team) in enumerate(zip(players, teams)):
            r, c = CHAMPION_STARTS[i]
            champ = Champion(user_id=member.id, team=team, slot=i, row=r, col=c)
            champions[i]            = champ
            user_to_slot[member.id] = i

        state = GameState(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            champions=champions,
            user_to_slot=user_to_slot,
            loot_tiles=_init_grid(),
        )
        self._games[interaction.guild_id] = state

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
                name=f"{EMOJI_CHAMP[i]}  Champion {i+1} — {team.title()}",
                value=f"{member.mention} → Start: `[{r},{c}]`",
                inline=False,
            )
        em.add_field(
            name="🏆  Central Vault",
            value=f"Position `[3,3]` — Needs **{VAULT_POINTS_REQ} Breach Points**",
            inline=False,
        )
        em.add_field(name="⏱️  Planning Timer", value=f"**{ROUND_TIMER}s** per round", inline=True)
        em.add_field(name="📋  Commands", value="`/submit-move` · `/use`", inline=True)
        em.set_footer(text=f"Bank Breakthrough  •  Champion Edition  •  {GRID_SIZE}×{GRID_SIZE} Grid")
        await interaction.response.send_message(embed=em)

        await self._post_round(interaction.guild_id)

    # ═════════════════════════ SUBMIT-MOVE COMMAND ════════════════════════════

    @app_commands.command(
        name="submit-move",
        description="Submit your move for this round (champions only).",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        direction=(
            "up | down | left | right | hold | action | "
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
            await interaction.response.send_message("❌  No active match right now.", ephemeral=True)
            return

        slot = state.user_to_slot.get(interaction.user.id)
        if slot is None:
            await interaction.response.send_message(
                "❌  You are not a registered Champion in this match.", ephemeral=True
            )
            return

        champ     = state.champions[slot]
        direction = direction.strip().lower()
        DIAGONALS = {"up-left", "up-right", "down-left", "down-right"}

        if direction in DIAGONALS and not champ.has_adrenaline:
            await interaction.response.send_message(
                "❌  Diagonal moves require an active **Adrenaline** — use `/use item:adrenaline` first.",
                ephemeral=True,
            )
            return

        VALID_DIRS = {"up", "down", "left", "right", "hold", "action"} | DIAGONALS
        if direction not in VALID_DIRS:
            await interaction.response.send_message(
                f"❌  Invalid direction `{direction}`.\n"
                "Valid: `up` `down` `left` `right` `hold` `action` (or diagonal with Adrenaline)",
                ephemeral=True,
            )
            return

        champ.move = direction
        await interaction.response.send_message(
            f"✅  **{champ.display_name}** — move `{direction}` registered!",
            ephemeral=True,
        )

    # ═════════════════════════ USE ITEM COMMAND ═══════════════════════════════

    @app_commands.command(
        name="use",
        description="Use an item from your team's inventory during the planning phase.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        item   = "smoke | c4 | adrenaline | shield | decrypter",
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
            await interaction.response.send_message("❌  No active match right now.", ephemeral=True)
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
                f"❌  Unknown item `{item}`.\nValid: `smoke` `c4` `adrenaline` `shield` `decrypter`",
                ephemeral=True,
            )
            return

        target_slot: Optional[int] = None
        if key in {"smoke", "c4"}:
            if target is None:
                await interaction.response.send_message(
                    f"❌  `{VALID_ITEMS[key]}` requires a `target` Champion.", ephemeral=True
                )
                return
            target_slot = state.user_to_slot.get(target.id)
            if target_slot is None or target_slot == slot:
                await interaction.response.send_message(
                    "❌  Invalid target — must be another registered Champion.", ephemeral=True
                )
                return

        if key == "c4" and target_slot is not None:
            tc = state.champions[target_slot]
            if not _is_adjacent(champ.row, champ.col, tc.row, tc.col):
                await interaction.response.send_message(
                    "❌  **C4** can only be used on an **adjacent** Champion.", ephemeral=True
                )
                return

        if key == "decrypter":
            vr, vc = VAULT_POS
            on_vault   = (champ.row, champ.col) == VAULT_POS
            adj_vault  = _is_adjacent(champ.row, champ.col, vr, vc)
            if not (on_vault or adj_vault):
                await interaction.response.send_message(
                    "❌  **Decrypter Card** must be used while on or adjacent to the Vault 🏆.",
                    ephemeral=True,
                )
                return

        if not _db_deduct_item(champ.team, key):
            await interaction.response.send_message(
                f"❌  **{champ.team.title()}** has no **{VALID_ITEMS[key]}** left.", ephemeral=True
            )
            return

        champ.used_item   = key
        champ.item_target = target_slot

        if key == "adrenaline":
            champ.has_adrenaline = True
            msg = "🟠 **Adrenaline** activated! You may submit a diagonal move this round."
        elif key == "shield":
            champ.shield_rounds = 3
            msg = "🛡️ **Riot Shield** activated! Protected for 3 rounds."
        elif key == "smoke":
            msg = f"💨 **Smoke Grenade** queued on {target.mention}! Their move will be randomised."
        elif key == "c4":
            msg = f"💣 **C4** queued on {target.mention}! They'll be blasted back on resolution."
        else:
            msg = "💾 **Decrypter Card** queued! Counts as 2 Breach Points if you `action` the vault."

        await interaction.response.send_message(msg, ephemeral=True)

    # ═════════════════════════ ROUND ENGINE ════════════════════════════════════

    async def _post_round(self, guild_id: int) -> None:
        state = self._games.get(guild_id)
        if not state:
            return

        state.round_number += 1
        channel = self.bot.get_channel(state.channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            return

        for champ in state.champions.values():
            champ.reset_round_inputs()

        img_buf = _render_grid_image(state)
        em      = self._round_embed(state, has_image=img_buf is not None)

        if img_buf:
            img_buf.seek(0)
            file = discord.File(img_buf, filename="board.png")
            msg  = await channel.send(file=file, embed=em)
        else:
            # Pillow unavailable — fall back to emoji grid in description
            em.description = f"```\n{_render_grid(state)}\n```"
            msg = await channel.send(embed=em)

        state.board_message = msg

        if state.timer_task:
            state.timer_task.cancel()
        state.timer_task = asyncio.create_task(
            self._round_countdown(guild_id, channel)
        )

    async def _round_countdown(self, guild_id: int, channel: discord.TextChannel) -> None:
        await asyncio.sleep(ROUND_TIMER)
        state = self._games.get(guild_id)
        if not state or not state.active:
            return
        await self._resolve_round(guild_id, channel)

    async def _resolve_round(self, guild_id: int, channel: discord.TextChannel) -> None:
        state = self._games.get(guild_id)
        if not state:
            return

        log_lines: list[str] = []

        # 1. Default missing moves
        for champ in state.champions.values():
            if champ.move is None:
                champ.move = "hold"
                log_lines.append(
                    f"⏰ **{champ.display_name}** ({champ.team.title()}) timed out — defaulting to **hold**."
                )

        # 2. Smoke effects
        for champ in state.champions.values():
            if champ.used_item == "smoke" and champ.item_target is not None:
                target = state.champions.get(champ.item_target)
                if target:
                    target.smoke_this_round = True
                    original   = target.move
                    target.move = random.choice(["up", "down", "left", "right", "hold"])
                    log_lines.append(
                        f"💨 **{champ.display_name}** smoked **{target.display_name}**! "
                        f"Move scrambled: `{original}` → `{target.move}`"
                    )

        # 3. C4 blasts
        c4_blasts: dict[int, int] = {}
        for champ in state.champions.values():
            if champ.used_item == "c4" and champ.item_target is not None:
                target = state.champions.get(champ.item_target)
                if target:
                    if target.shield_rounds > 0:
                        log_lines.append(
                            f"💣 **{champ.display_name}** detonated C4 on **{target.display_name}** "
                            f"— Riot Shield blocked it! 🛡️"
                        )
                    else:
                        c4_blasts[target.slot] = champ.slot
                        dr = target.row - champ.row
                        dc = target.col - champ.col
                        if dr != 0: dr = dr // abs(dr)
                        if dc != 0: dc = dc // abs(dc)
                        target.row = max(0, min(GRID_SIZE - 1, target.row + dr * 2))
                        target.col = max(0, min(GRID_SIZE - 1, target.col + dc * 2))
                        target.frozen_rounds = 1
                        target.move = "hold"
                        log_lines.append(
                            f"💣 **{champ.display_name}** C4'd **{target.display_name}**! "
                            f"Blasted to `[{target.row},{target.col}]` — frozen next round ❄️"
                        )

        # 4. Calculate proposed positions
        old_pos: dict[int, tuple[int, int]] = {
            s: (c.row, c.col) for s, c in state.champions.items()
        }
        proposed: dict[int, tuple[int, int]] = {}
        for slot, champ in state.champions.items():
            if slot in c4_blasts:
                proposed[slot] = (champ.row, champ.col)
                continue
            if champ.frozen_rounds > 0:
                champ.frozen_rounds -= 1
                proposed[slot] = (champ.row, champ.col)
                if champ.frozen_rounds == 0:
                    log_lines.append(f"❄️ **{champ.display_name}** has thawed — free next round.")
                continue
            proposed[slot] = _apply_direction(champ.row, champ.col, champ.move)

        # 5. Collision detection
        dest_map: dict[tuple[int, int], list[int]] = {}
        for slot, pos in proposed.items():
            dest_map.setdefault(pos, []).append(slot)

        final: dict[int, tuple[int, int]] = {}
        for pos, slots in dest_map.items():
            if len(slots) == 1:
                final[slots[0]] = pos
            else:
                shielded   = [s for s in slots if state.champions[s].shield_rounds > 0]
                unshielded = [s for s in slots if state.champions[s].shield_rounds == 0]
                if len(shielded) == 1:
                    winner = shielded[0]
                    final[winner] = pos
                    log_lines.append(
                        f"🛡️ **{state.champions[winner].display_name}** won collision at `{pos}` via Riot Shield!"
                    )
                    for loser in unshielded:
                        final[loser] = old_pos[loser]
                        log_lines.append(
                            f"💥 **{state.champions[loser].display_name}** bounced back to `{old_pos[loser]}`!"
                        )
                else:
                    for s in slots:
                        final[s] = old_pos[s]
                        log_lines.append(
                            f"💥 **{state.champions[s].display_name}** collided and bounced to `{old_pos[s]}`!"
                        )

        for slot, (r, c) in final.items():
            state.champions[slot].row = r
            state.champions[slot].col = c

        # 6. Decrement shields
        for champ in state.champions.values():
            if champ.shield_rounds > 0:
                champ.shield_rounds -= 1

        # 7. Looting
        for champ in state.champions.values():
            pos = (champ.row, champ.col)
            if pos in state.loot_tiles:
                state.loot_tiles.discard(pos)
                new_bal = _db_add_coins(champ.team, LOOT_REWARD)
                log_lines.append(
                    f"💰 **{champ.display_name}** ({champ.team.title()}) looted a cache! "
                    f"**+{LOOT_REWARD:,} coins** → total: **{new_bal:,}** 🪙"
                )

        # 8. Vault breaching
        game_over    = False
        winner_team: Optional[str] = None
        for champ in state.champions.values():
            if (champ.row, champ.col) == VAULT_POS and champ.move == "action":
                points = 2 if champ.used_item == "decrypter" else 1
                state.breach_points += points
                tag = " (Decrypter ×2!) 💾" if points == 2 else ""
                log_lines.append(
                    f"🏆 **{champ.display_name}** ({champ.team.title()}) breached the vault{tag}! "
                    f"Points: **{state.breach_points}/{VAULT_POINTS_REQ}**"
                )
                if state.breach_points >= VAULT_POINTS_REQ:
                    game_over   = True
                    winner_team = champ.team

        # 9. Post results
        img_buf = _render_grid_image(state)

        if game_over and winner_team:
            state.active = False
            self._games.pop(guild_id, None)
            await self._post_victory(channel, state, winner_team, log_lines, img_buf)
            return

        # Post action log first
        if log_lines:
            log_em = discord.Embed(
                title=f"📋  Round {state.round_number} — Action Log",
                colour=C_ACTION,
                description="\n".join(log_lines),
            )
            log_em.set_footer(text=f"Vault: {state.breach_points}/{VAULT_POINTS_REQ} Breach Points")
            await channel.send(embed=log_em)

        await self._post_round(guild_id)

    # ─────────────────────────── Embeds ──────────────────────────────────────

    def _round_embed(self, state: GameState, has_image: bool = False) -> discord.Embed:
        em = discord.Embed(
            title=f"🏦  Bank Breakthrough — Round {state.round_number}",
            colour=C_ROUND,
        )
        if has_image:
            em.set_image(url="attachment://board.png")

        lines = []
        for slot, champ in state.champions.items():
            tags = []
            if champ.frozen_rounds > 0: tags.append(f"❄️ Frozen ({champ.frozen_rounds}r)")
            if champ.shield_rounds  > 0: tags.append(f"🛡️ Shielded ({champ.shield_rounds}r)")
            status = " | ".join(tags) if tags else "Active"
            lines.append(
                f"{champ.emoji} **C{slot+1}** ({champ.team.title()}) "
                f"`[{champ.row},{champ.col}]` — {status}"
            )
        em.add_field(name="Champions", value="\n".join(lines), inline=False)
        em.add_field(
            name="🏆 Vault Progress",
            value=f"**{state.breach_points}/{VAULT_POINTS_REQ}** Breach Points",
            inline=True,
        )
        em.add_field(
            name="⏱️ Planning",
            value=f"**{ROUND_TIMER}s** — `/submit-move` & `/use`",
            inline=True,
        )
        em.set_footer(text=f"Bank Breakthrough  •  Loot Tiles remaining: {len(state.loot_tiles)}")
        return em

    async def _post_victory(
        self,
        channel:     discord.TextChannel,
        state:       GameState,
        winner_team: str,
        log_lines:   list[str],
        img_buf:     Optional[BytesIO],
    ) -> None:
        if log_lines:
            log_em = discord.Embed(
                title=f"📋  Round {state.round_number} — Final Action Log",
                colour=C_VAULT,
                description="\n".join(log_lines),
            )
            await channel.send(embed=log_em)

        winner_champ = next(
            (c for c in state.champions.values() if c.team == winner_team), None
        )
        em = discord.Embed(
            title="💥  THE VAULT HAS BEEN CRACKED!  💥",
            colour=C_WIN,
            description=(
                f"```ansi\n\u001b[1;33m  🏆  HEIST COMPLETE!  🏆  \u001b[0m\n```\n"
                f"{SEP}\n**{winner_team.title()}** has breached the Central Vault!\n{SEP}"
            ),
        )
        if img_buf:
            em.set_image(url="attachment://board.png")
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

        if img_buf:
            img_buf.seek(0)
            await channel.send(file=discord.File(img_buf, filename="board.png"), embed=em)
        else:
            await channel.send(embed=em)

    # ─────────────────────── Admin utilities ─────────────────────────────────

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
            await interaction.response.send_message("❌  No active match to end.", ephemeral=True)
            return
        if state.timer_task:
            state.timer_task.cancel()
        state.active = False
        await interaction.response.send_message("🛑  Match forcefully ended by admin.")

    @app_commands.command(
        name="breakthrough-status",
        description="Show the current match status and board.",
    )
    @app_commands.guild_only()
    async def breakthrough_status(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        state = self._games.get(interaction.guild_id)
        if not state or not state.active:
            await interaction.response.send_message("❌  No active match right now.", ephemeral=True)
            return
        img_buf = _render_grid_image(state)
        em      = self._round_embed(state, has_image=img_buf is not None)
        if img_buf:
            img_buf.seek(0)
            await interaction.response.send_message(
                file=discord.File(img_buf, filename="board.png"), embed=em, ephemeral=True
            )
        else:
            em.description = f"```\n{_render_grid(state)}\n```"
            await interaction.response.send_message(embed=em, ephemeral=True)


    @app_commands.command(
        name="breakthrough-leaderboard",
        description="Show the all-time Heist Coin leaderboard for Bank Breakthrough.",
    )
    @app_commands.guild_only()
    async def breakthrough_leaderboard(self, interaction: discord.Interaction) -> None:
        rows = _db_get_all_coins()

        em = discord.Embed(
            title="🪙  Bank Breakthrough — Heist Coin Leaderboard",
            colour=C_ITEM,
        )

        if not rows:
            em.description = "_No coins earned yet. Start a match with `/breakthrough-setup`!_"
        else:
            MEDALS = ["🥇", "🥈", "🥉"]
            lines = []
            for i, (team, coins) in enumerate(rows):
                medal = MEDALS[i] if i < len(MEDALS) else f"`#{i+1}`"
                lines.append(f"{medal}  **{team.title()}** — **{coins:,}** 🪙")
            em.description = "\n".join(lines)

        em.set_footer(text="Bank Breakthrough  •  Coins earned from loot tiles & vault breaches only")
        await interaction.response.send_message(embed=em)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BankBreakthroughCog(bot))
