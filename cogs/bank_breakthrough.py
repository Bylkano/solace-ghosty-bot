"""
cogs/bank_breakthrough.py
─────────────────────────────────────────────────────────────────────────────
Breakthrough — Champion Edition  (v2 · 14×14 grid)
─────────────────────────────────────────────────────────────────────────────

Admin commands:
  /team-add       [team_name] [@member]        — assign member to a team
  /team-remove    [@member]                    — remove member from any team
  /team-list      [team_name]                  — list team members
  /breakthrough-setup [u1] [u2] [u3] [u4]     — start a match
  /breakthrough-end                            — force-end an active match

Player commands:
  /submit-move [direction] [distance]          — queue move (1–3 tiles)
  /use [gadget] [target_slot] [direction]      — activate gadget
  /breakthrough-status                         — view current board (private)
  /breakthrough-leaderboard                    — all-time heist coin rankings

Gadgets (Energy Pool: 100 EP max · +10 EP/round regen):
  smoke  (30 EP) — hides your coordinates for 2 rounds
  emp    (40 EP) — stuns an adjacent champion for 1 round (target_slot 1–4)
  decoy  (50 EP) — places a fake clone on an adjacent tile (direction required)

Map (14×14):
  • Champions spawn at corners: [0,0] [0,13] [13,0] [13,13]
  • Central Vault: 2×2 block at [6,6]–[7,7] · needs 3 Breach Points to crack
  • Security Walls (🧱): permanent obstacles
  • Security Doors (🚪): 1-turn action to unlock (use 'action' adjacent to door)
  • Common Loot (💵): +100 🪙 when landed on
  • Diamond Briefcase (💎): spawns round 3 · +500 🪙 · carrying it costs −1 max movement
  • Alarm Traps (⚠️): hidden until stepped on; stun for 1 round

Round Flow:
  1. Planning Phase — 30 s (skips instantly when all players submit)
  2. Resolution — Gadgets → Movement → Traps
  3. Ranking — Breach Points + Loot; tiebreaker = least tiles traveled
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

# ─────────────────────────── Game constants ───────────────────────────────────

GRID_SIZE           = 14
VAULT_TILES         = frozenset({(6, 6), (6, 7), (7, 6), (7, 7)})
VAULT_POINTS_REQ    = 3
ROUND_TIMER         = 30          # seconds per planning phase
MAX_EP              = 100
EP_REGEN            = 10
MAX_MOVEMENT        = 3
DIAMOND_SPAWN_ROUND = 3

LOOT_REWARD_COMMON  = 100
LOOT_REWARD_DIAMOND = 500
BREACH_COIN_REWARD  = 300

CHAMPION_STARTS = [(0, 0), (0, 13), (13, 0), (13, 13)]

# Fixed, symmetric bank-layout obstacles
FIXED_WALLS: frozenset[tuple[int, int]] = frozenset({
    # Corner room blockers
    (1, 1),  (1, 2),  (2, 1),
    (1, 11), (1, 12), (2, 12),
    (11, 1), (12, 1), (12, 2),
    (11, 12),(12, 11),(12, 12),
    # Mid-map barriers (symmetric)
    (3, 3),  (3, 4),  (4, 3),
    (3, 9),  (3, 10), (4, 10),
    (9, 3),  (10, 3), (10, 4),
    (9, 10), (10, 9), (10, 10),
    # Vault perimeter guard walls
    (5, 5),  (5, 8),
    (8, 5),  (8, 8),
})

# Locked doors guarding all 4 vault approaches
FIXED_DOORS: frozenset[tuple[int, int]] = frozenset({
    (5, 6), (5, 7),   # North vault approach
    (8, 6), (8, 7),   # South vault approach
    (6, 5), (7, 5),   # West vault approach
    (6, 8), (7, 8),   # East vault approach
})

SEP = "─" * 36

# ─────────────────────────── Display ──────────────────────────────────────────

EMOJI_CHAMP = ["🟦", "🟨", "🟩", "🟥"]

C_ROUND  = 0x4F46E5   # indigo
C_ACTION = 0xF59E0B   # amber
C_VAULT  = 0xFFD700   # gold
C_WIN    = 0x22C55E   # green
C_TEAM   = 0x818CF8   # lavender
C_ERROR  = 0xEF4444   # red
C_LB     = 0xA78BFA   # purple

VALID_GADGETS: dict[str, tuple[str, int]] = {
    "smoke": ("Smoke Grenade",  30),
    "emp":   ("EMP Charge",     40),
    "decoy": ("Decoy Hologram", 50),
}

VALID_DIRECTIONS = {"up", "down", "left", "right", "hold", "action"}

_DIR_DELTA: dict[str, tuple[int, int]] = {
    "up":     (-1,  0),
    "down":   ( 1,  0),
    "left":   ( 0, -1),
    "right":  ( 0,  1),
    "hold":   ( 0,  0),
    "action": ( 0,  0),
}

# ─────────────────────────── DB layer ─────────────────────────────────────────

def _db_connect():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def _db_ensure_tables() -> None:
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS breakthrough_coins (
                    team_name  TEXT PRIMARY KEY,
                    coins      INTEGER NOT NULL DEFAULT 0
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS breakthrough_teams (
                    guild_id   BIGINT,
                    user_id    BIGINT,
                    team_name  TEXT,
                    PRIMARY KEY (guild_id, user_id)
                )
            """)
        con.commit()


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
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT team_name, coins FROM breakthrough_coins ORDER BY coins DESC"
            )
            return [(row[0], row[1]) for row in cur.fetchall()]


def _db_team_add(guild_id: int, user_id: int, team_name: str) -> None:
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute("""
                INSERT INTO breakthrough_teams (guild_id, user_id, team_name)
                VALUES (%s, %s, %s)
                ON CONFLICT (guild_id, user_id) DO UPDATE SET team_name = EXCLUDED.team_name
            """, (guild_id, user_id, team_name.lower()))
        con.commit()


def _db_team_remove(guild_id: int, user_id: int) -> bool:
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "DELETE FROM breakthrough_teams WHERE guild_id = %s AND user_id = %s",
                (guild_id, user_id),
            )
            removed = cur.rowcount > 0
        con.commit()
        return removed


def _db_get_user_team(guild_id: int, user_id: int) -> Optional[str]:
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
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM breakthrough_teams "
                "WHERE guild_id = %s AND team_name = %s",
                (guild_id, team_name.lower()),
            )
            return [row[0] for row in cur.fetchall()]


def _db_get_all_teams(guild_id: int) -> dict[str, list[int]]:
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

# ─────────────────────────── Data models ──────────────────────────────────────

@dataclass
class Champion:
    user_id: int
    team:    str
    slot:    int
    row:     int
    col:     int

    ep:                      int  = MAX_EP
    tiles_traveled:          int  = 0
    breach_points_contrib:   int  = 0
    loot_claimed:            int  = 0
    carrying_diamond:        bool = False

    frozen_rounds: int  = 0   # cannot move; decrements start of resolution
    smoked_rounds: int  = 0   # position hidden on board; decrements end of round

    # Active decoy
    has_decoy:  bool = False
    decoy_row:  int  = -1
    decoy_col:  int  = -1

    # Round inputs (reset each planning phase)
    move:              Optional[str] = None
    move_distance:     int           = MAX_MOVEMENT
    gadget:            Optional[str] = None
    gadget_target_slot:Optional[int] = None
    gadget_direction:  Optional[str] = None
    submitted:         bool          = False

    @property
    def max_movement(self) -> int:
        return max(1, MAX_MOVEMENT - (1 if self.carrying_diamond else 0))

    @property
    def display_name(self) -> str:
        return f"C{self.slot + 1} ({self.team.title()})"

    @property
    def emoji(self) -> str:
        return EMOJI_CHAMP[self.slot]

    def reset_round(self) -> None:
        self.move              = None
        self.move_distance     = self.max_movement
        self.gadget            = None
        self.gadget_target_slot= None
        self.gadget_direction  = None
        self.submitted         = False


@dataclass
class GameState:
    channel_id: int
    guild_id:   int
    champions:  dict[int, Champion]

    walls:         frozenset[tuple[int, int]] = field(default_factory=lambda: frozenset(FIXED_WALLS))
    doors:         set[tuple[int, int]]       = field(default_factory=lambda: set(FIXED_DOORS))
    opened_doors:  set[tuple[int, int]]       = field(default_factory=set)
    loot_tiles:    dict[tuple[int, int], str] = field(default_factory=dict)   # pos → "common"|"diamond"
    traps:         set[tuple[int, int]]       = field(default_factory=set)    # hidden traps
    triggered_traps: set[tuple[int, int]]     = field(default_factory=set)    # revealed

    breach_points:   int  = 0
    round_number:    int  = 0
    active:          bool = True
    diamond_spawned: bool = False

    timer_task:    Optional[asyncio.Task]       = field(default=None, compare=False)
    board_message: Optional[discord.Message]    = field(default=None, compare=False)

# ─────────────────────────── Map generation ───────────────────────────────────

def _generate_map(state: GameState) -> None:
    """Place 8 common loot tiles and 5 hidden alarm traps on the fresh board."""
    taken: set[tuple[int, int]] = (
        set(CHAMPION_STARTS)
        | VAULT_TILES
        | set(state.walls)
        | set(state.doors)
    )
    candidates = [
        (r, c)
        for r in range(GRID_SIZE)
        for c in range(GRID_SIZE)
        if (r, c) not in taken
    ]
    random.shuffle(candidates)
    for pos in candidates[:8]:
        state.loot_tiles[pos] = "common"
    state.traps = set(candidates[8:13])


def _spawn_diamond(state: GameState) -> Optional[tuple[int, int]]:
    """Spawn the Diamond Briefcase at a random unoccupied tile."""
    occupied: set[tuple[int, int]] = (
        {(c.row, c.col) for c in state.champions.values()}
        | {(c.decoy_row, c.decoy_col) for c in state.champions.values() if c.has_decoy}
        | VAULT_TILES
        | set(state.walls)
        | set(state.doors)
        | set(state.loot_tiles)
        | state.traps
        | state.triggered_traps
    )
    pool = [
        (r, c)
        for r in range(GRID_SIZE)
        for c in range(GRID_SIZE)
        if (r, c) not in occupied
    ]
    if not pool:
        return None
    pos = random.choice(pool)
    state.loot_tiles[pos] = "diamond"
    return pos

# ─────────────────────────── Movement helpers ─────────────────────────────────

def _apply_direction(row: int, col: int, direction: str) -> tuple[int, int]:
    dr, dc = _DIR_DELTA.get(direction, (0, 0))
    return row + dr, col + dc


def _is_adjacent(r1: int, c1: int, r2: int, c2: int) -> bool:
    return abs(r1 - r2) <= 1 and abs(c1 - c2) <= 1 and (r1, c1) != (r2, c2)


def _walk_champion(
    champ: Champion,
    state: GameState,
    log:   list[str],
) -> tuple[int, int]:
    """
    Step-by-step movement. Returns (old_row, old_col).
    Stops at walls, closed doors, and grid boundary.
    Loot / vault / trap effects are resolved AFTER all champions move.
    """
    direction = champ.move
    if direction in (None, "hold", "action"):
        return champ.row, champ.col

    old_r, old_c = champ.row, champ.col
    steps = min(champ.move_distance, champ.max_movement)
    moved = 0

    for _ in range(steps):
        nr, nc = _apply_direction(champ.row, champ.col, direction)
        if not (0 <= nr < GRID_SIZE and 0 <= nc < GRID_SIZE):
            break
        if (nr, nc) in state.walls:
            log.append(f"🧱 **{champ.display_name}** blocked by wall at `[{nr},{nc}]`.")
            break
        if (nr, nc) in state.doors and (nr, nc) not in state.opened_doors:
            log.append(
                f"🚪 **{champ.display_name}** blocked by a locked security door at `[{nr},{nc}]`. "
                f"Use `action` while adjacent to unlock it."
            )
            break
        champ.row, champ.col = nr, nc
        moved += 1

    champ.tiles_traveled += moved
    return old_r, old_c

# ─────────────────────────── Grid image renderer ──────────────────────────────

def _render_grid_image(state: GameState) -> Optional[BytesIO]:
    """Render a styled 14×14 board PNG. Returns BytesIO or None if Pillow missing."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    CELL = 48
    PAD  = 44
    LAB  = 18

    W = PAD + LAB + GRID_SIZE * CELL + PAD
    H = PAD + LAB + GRID_SIZE * CELL + PAD

    # ── Palette ──────────────────────────────────────────────────────────────
    BG          = (6,   8,  18)
    BOARD_BG    = (12,  18,  36)
    CELL_A      = (22,  30,  52)
    CELL_B      = (18,  26,  46)
    GRID_LINE   = (28,  40,  72)
    PANEL_BORDER= (40,  55,  95)

    WALL_BG     = (12,  12,  18)
    WALL_MORTAR = (32,  32,  45)

    DOOR_C_BG   = (90,  55,  20)
    DOOR_C_LINE = (155, 95,  35)
    DOOR_C_LOCK = (220, 160, 55)
    DOOR_O_BG   = (105, 80,  40)
    DOOR_O_LINE = (165, 125, 65)

    LOOT_BG     = (12,  55,  18)
    LOOT_FG     = (45,  195, 75)

    DIAM_BG     = (8,   25,  85)
    DIAM_FG     = (75,  155, 255)

    TRAP_BG     = (70,  8,   8)
    TRAP_FG     = (255, 55,  55)

    VAULT_BG    = (75,  50,  0)
    VAULT_RING  = (255, 195, 0)
    VAULT_GLOW  = (255, 230, 75)
    VAULT_TXT   = (255, 248, 130)

    CHAMP_COLS  = [
        (55,  120, 230),   # C1 — blue
        (220, 175,   0),   # C2 — yellow
        (40,  195,  80),   # C3 — green
        (220,  55,  55),   # C4 — red
    ]
    DECOY_COL   = (110, 110, 175)
    ICE_RING    = (100, 185, 255)
    TEXT_BRIGHT = (245, 248, 255)
    TEXT_DIM    = (75,  100, 150)

    img  = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    try:
        f_lg = ImageFont.load_default(size=13)
        f_md = ImageFont.load_default(size=10)
        f_sm = ImageFont.load_default(size=8)
    except Exception:
        f_lg = f_md = f_sm = ImageFont.load_default()

    bx0 = PAD + LAB
    by0 = PAD + LAB
    bx1 = bx0 + GRID_SIZE * CELL
    by1 = by0 + GRID_SIZE * CELL

    # Board panel background
    draw.rectangle([bx0 - 4, by0 - 4, bx1 + 4, by1 + 4],
                   fill=BOARD_BG, outline=PANEL_BORDER, width=3)

    # Axis labels
    for i in range(GRID_SIZE):
        cx = bx0 + i * CELL + CELL // 2
        cy = by0 + i * CELL + CELL // 2
        draw.text((cx, by0 - 9), str(i), fill=TEXT_DIM, font=f_sm, anchor="mm")
        draw.text((bx0 - 9, cy), str(i), fill=TEXT_DIM, font=f_sm, anchor="mm")

    # Champion & decoy position lookups
    champ_at: dict[tuple[int, int], int] = {
        (c.row, c.col): slot for slot, c in state.champions.items()
    }
    decoy_at: set[tuple[int, int]] = {
        (c.decoy_row, c.decoy_col) for c in state.champions.values() if c.has_decoy
    }
    smoked: set[int] = {
        slot for slot, c in state.champions.items() if c.smoked_rounds > 0
    }

    # Pre-calculate vault center for the 2×2 glow
    vault_cx = bx0 + 6 * CELL + CELL   # center of [6,6]–[7,7] block
    vault_cy = by0 + 6 * CELL + CELL

    for row in range(GRID_SIZE):
        for col in range(GRID_SIZE):
            x0 = bx0 + col * CELL + 1
            y0 = by0 + row * CELL + 1
            x1 = x0 + CELL - 2
            y1 = y0 + CELL - 2
            cx = (x0 + x1) // 2
            cy = (y0 + y1) // 2
            pos = (row, col)

            is_vault        = pos in VAULT_TILES
            is_wall         = pos in state.walls
            is_door_closed  = pos in state.doors and pos not in state.opened_doors
            is_door_open    = pos in state.opened_doors
            loot_kind       = state.loot_tiles.get(pos)
            is_trap_vis     = pos in state.triggered_traps
            is_decoy        = pos in decoy_at
            slot            = champ_at.get(pos)
            if slot in smoked:
                slot = None   # hide smoked champion's real position

            # ── Cell base ────────────────────────────────────────────────────
            if is_wall:
                draw.rectangle([x0, y0, x1, y1], fill=WALL_BG)
                # Brick lines
                for wy in range(y0 + 8, y1, 8):
                    draw.line([(x0, wy), (x1, wy)], fill=WALL_MORTAR, width=1)
                shift = 0
                for wy in range(y0, y1, 16):
                    draw.line([(x0 + shift, wy), (x0 + shift, wy + 8)],
                              fill=WALL_MORTAR, width=1)
                    draw.line([(x0 + 12 + shift, wy), (x0 + 12 + shift, wy + 8)],
                              fill=WALL_MORTAR, width=1)
                    shift = (shift + 6) % 12
                continue   # skip overlays for walls

            elif is_vault:
                draw.rectangle([x0, y0, x1, y1],
                               fill=VAULT_BG, outline=VAULT_RING, width=2)
                # Full 2×2 glow drawn once at top-left vault tile
                if pos == (6, 6):
                    r = 30
                    draw.ellipse(
                        [vault_cx - r, vault_cy - r, vault_cx + r, vault_cy + r],
                        fill=VAULT_GLOW, outline=VAULT_RING, width=2,
                    )
                    draw.text((vault_cx, vault_cy - 4), "★",
                              fill=VAULT_TXT, font=f_lg, anchor="mm")
                    draw.text((vault_cx, vault_cy + 11), "VAULT",
                              fill=VAULT_RING, font=f_sm, anchor="mm")

            elif is_door_closed:
                draw.rectangle([x0, y0, x1, y1], fill=DOOR_C_BG)
                draw.line([(cx, y0 + 3), (cx, y1 - 3)], fill=DOOR_C_LINE, width=2)
                draw.line([(cx - 5, y0 + 3), (cx - 5, y1 - 3)], fill=DOOR_C_LINE, width=1)
                draw.line([(cx + 5, y0 + 3), (cx + 5, y1 - 3)], fill=DOOR_C_LINE, width=1)
                rr = 3
                draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=DOOR_C_LOCK)

            elif is_door_open:
                draw.rectangle([x0, y0, x1, y1], fill=DOOR_O_BG)
                draw.line([(x0 + 3, y0 + 3), (x0 + 3, y1 - 3)],
                          fill=DOOR_O_LINE, width=2)

            else:
                base = CELL_A if (row + col) % 2 == 0 else CELL_B
                draw.rectangle([x0, y0, x1, y1], fill=base, outline=GRID_LINE, width=1)

            # ── Tile overlays ────────────────────────────────────────────────
            if loot_kind == "common":
                draw.rectangle([x0 + 3, y0 + 3, x1 - 3, y1 - 3],
                               fill=LOOT_BG, outline=LOOT_FG, width=1)
                draw.text((cx, cy), "$", fill=LOOT_FG, font=f_md, anchor="mm")

            elif loot_kind == "diamond":
                draw.rectangle([x0 + 3, y0 + 3, x1 - 3, y1 - 3],
                               fill=DIAM_BG, outline=DIAM_FG, width=1)
                draw.text((cx, cy), "◆", fill=DIAM_FG, font=f_md, anchor="mm")

            if is_trap_vis:
                draw.rectangle([x0 + 3, y0 + 3, x1 - 3, y1 - 3],
                               fill=TRAP_BG, outline=TRAP_FG, width=1)
                draw.text((cx, cy), "!", fill=TRAP_FG, font=f_md, anchor="mm")

            # ── Decoy token ──────────────────────────────────────────────────
            if is_decoy and slot is None:
                r = 14
                draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                             fill=None, outline=DECOY_COL, width=2)
                draw.text((cx, cy), "?", fill=DECOY_COL, font=f_md, anchor="mm")

            # ── Champion token ───────────────────────────────────────────────
            if slot is not None:
                champ   = state.champions[slot]
                c_rgb   = CHAMP_COLS[slot]
                r       = 16

                # Drop shadow
                draw.ellipse([cx - r + 2, cy - r + 2, cx + r + 2, cy + r + 2],
                             fill=(0, 0, 0))
                # Main circle
                draw.ellipse([cx - r, cy - r, cx + r, cy + r],
                             fill=c_rgb, outline=TEXT_BRIGHT, width=1)
                draw.text((cx, cy - 4), f"C{slot + 1}",
                          fill=TEXT_BRIGHT, font=f_sm, anchor="mm")
                draw.text((cx, cy + 5), champ.team[:4].upper(),
                          fill=TEXT_BRIGHT, font=f_sm, anchor="mm")

                # Diamond indicator (small ◆ on token)
                if champ.carrying_diamond:
                    draw.text((cx + r - 4, cy - r + 4), "◆",
                              fill=DIAM_FG, font=f_sm, anchor="mm")

                # Frozen ring
                if champ.frozen_rounds > 0:
                    draw.ellipse([cx - r - 4, cy - r - 4, cx + r + 4, cy + r + 4],
                                 fill=None, outline=ICE_RING, width=2)

    # ── Champion EP mini-legend (bottom strip) ────────────────────────────────
    legend_y = by1 + 9
    seg_w    = (bx1 - bx0) // 4
    for slot, champ in state.champions.items():
        lx      = bx0 + slot * seg_w
        c_rgb   = CHAMP_COLS[slot]
        draw.rectangle([lx, legend_y, lx + 8, legend_y + 8], fill=c_rgb)
        ep_bar  = f"C{slot+1} · {champ.ep}EP"
        if champ.carrying_diamond:
            ep_bar += " · ◆"
        if champ.frozen_rounds > 0:
            ep_bar += f" · ❄{champ.frozen_rounds}"
        if champ.smoked_rounds > 0:
            ep_bar += f" · 💨{champ.smoked_rounds}"
        draw.text((lx + 12, legend_y + 4), ep_bar, fill=TEXT_DIM, font=f_sm, anchor="lm")

    buf = BytesIO()
    img.save(buf, "PNG", optimize=True)
    buf.seek(0)
    return buf


def _render_grid_text(state: GameState) -> str:
    """Fallback emoji grid if Pillow is unavailable."""
    TILE_WALL    = "🧱"
    TILE_VAULT   = "🏦"
    TILE_LOOT    = "💵"
    TILE_DIAMOND = "💎"
    TILE_TRAP    = "⚠️"
    TILE_DOOR_C  = "🚪"
    TILE_DOOR_O  = "🔓"
    TILE_EMPTY   = "⬛"

    champ_at = {(c.row, c.col): slot for slot, c in state.champions.items()}
    smoked   = {slot for slot, c in state.champions.items() if c.smoked_rounds > 0}

    rows = []
    for r in range(GRID_SIZE):
        row = []
        for c in range(GRID_SIZE):
            pos  = (r, c)
            slot = champ_at.get(pos)
            if slot is not None and slot not in smoked:
                row.append(EMOJI_CHAMP[slot])
            elif pos in VAULT_TILES:
                row.append(TILE_VAULT)
            elif pos in state.walls:
                row.append(TILE_WALL)
            elif pos in state.triggered_traps:
                row.append(TILE_TRAP)
            elif pos in state.doors and pos not in state.opened_doors:
                row.append(TILE_DOOR_C)
            elif pos in state.opened_doors:
                row.append(TILE_DOOR_O)
            elif state.loot_tiles.get(pos) == "diamond":
                row.append(TILE_DIAMOND)
            elif state.loot_tiles.get(pos) == "common":
                row.append(TILE_LOOT)
            else:
                row.append(TILE_EMPTY)
        rows.append("".join(row))
    return "\n".join(rows)

# ─────────────────────────── Cog ──────────────────────────────────────────────

class BankBreakthroughCog(commands.Cog, name="BankBreakthrough"):
    """Breakthrough — 14×14 tactical heist game for Discord."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot   = bot
        self._games: dict[int, GameState] = {}

    async def cog_load(self) -> None:
        _db_ensure_tables()

    # ═══════════════════════════ ADMIN COMMANDS ════════════════════════════════

    @app_commands.command(name="team-add", description="[Admin] Assign a member to a team.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(team_name="Team name.", member="Member to assign.")
    async def team_add(
        self,
        interaction: discord.Interaction,
        team_name:   str,
        member:      discord.Member,
    ) -> None:
        assert interaction.guild_id is not None
        _db_team_add(interaction.guild_id, member.id, team_name.strip())
        em = discord.Embed(
            title="✅  Team Updated", colour=C_TEAM,
            description=f"{member.mention} → **{team_name.strip().title()}**",
        )
        em.set_footer(text="Breakthrough · Team System")
        await interaction.response.send_message(embed=em, ephemeral=True)

    @app_commands.command(name="team-remove", description="[Admin] Remove a member from their team.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(member="Member to remove.")
    async def team_remove(
        self,
        interaction: discord.Interaction,
        member:      discord.Member,
    ) -> None:
        assert interaction.guild_id is not None
        removed = _db_team_remove(interaction.guild_id, member.id)
        em = discord.Embed(
            title="✅  Team Updated" if removed else "ℹ️  Not in a Team",
            colour=C_TEAM if removed else C_ERROR,
            description=(
                f"{member.mention} has been removed from their team."
                if removed else
                f"{member.mention} wasn't in any team."
            ),
        )
        em.set_footer(text="Breakthrough · Team System")
        await interaction.response.send_message(embed=em, ephemeral=True)

    @app_commands.command(name="team-list", description="[Admin] List members of a team.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(team_name="Team to inspect.")
    async def team_list(
        self,
        interaction: discord.Interaction,
        team_name:   str,
    ) -> None:
        assert interaction.guild_id is not None
        uids = _db_get_team_members(interaction.guild_id, team_name.strip())
        em   = discord.Embed(title=f"👥  {team_name.strip().title()} — Roster", colour=C_TEAM)
        em.description = "\n".join(f"• <@{uid}>" for uid in uids) if uids else "_Empty team._"
        em.set_footer(text="Breakthrough · Team System")
        await interaction.response.send_message(embed=em, ephemeral=True)

    @app_commands.command(
        name="breakthrough-setup",
        description="[Admin] Register 4 Champions and start a Breakthrough match.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        champion1="Champion for slot 1 — C1 (Blue) · spawns [0,0]",
        champion2="Champion for slot 2 — C2 (Yellow) · spawns [0,13]",
        champion3="Champion for slot 3 — C3 (Green) · spawns [13,0]",
        champion4="Champion for slot 4 — C4 (Red) · spawns [13,13]",
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
        await interaction.response.defer()

        guild_id = interaction.guild_id
        if guild_id in self._games and self._games[guild_id].active:
            await interaction.followup.send(
                "❌  A match is already active. Use `/breakthrough-end` first.", ephemeral=True
            )
            return

        members = [champion1, champion2, champion3, champion4]
        teams: list[str] = []
        for m in members:
            t = _db_get_user_team(guild_id, m.id)
            if not t:
                await interaction.followup.send(
                    f"❌  {m.mention} isn't assigned to a team. Use `/team-add` first.",
                    ephemeral=True,
                )
                return
            teams.append(t)

        champions = {
            i: Champion(
                user_id=members[i].id,
                team=teams[i],
                slot=i,
                row=CHAMPION_STARTS[i][0],
                col=CHAMPION_STARTS[i][1],
            )
            for i in range(4)
        }

        assert isinstance(interaction.channel, discord.TextChannel)
        state = GameState(
            channel_id=interaction.channel.id,
            guild_id=guild_id,
            champions=champions,
        )
        _generate_map(state)
        self._games[guild_id] = state

        roster = "\n".join(
            f"{EMOJI_CHAMP[i]} **C{i+1}** — {members[i].mention} ({teams[i].title()})"
            for i in range(4)
        )
        setup_em = discord.Embed(
            title="🏦  Breakthrough — Match Starting!",
            colour=C_VAULT,
            description=(
                f"{SEP}\n"
                "**Champions have entered the building.**\n"
                "Crack the Central Vault — 3 Breach Points wins.\n"
                f"{SEP}\n\n{roster}"
            ),
        )
        setup_em.add_field(name="🗺️ Grid", value="14×14 with walls, doors & hidden traps", inline=True)
        setup_em.add_field(name="⚡ Energy", value="100 EP · +10/round regen", inline=True)
        setup_em.add_field(
            name="🎮 Gadgets",
            value="💨 Smoke (30 EP) · ⚡ EMP (40 EP) · 👻 Decoy (50 EP)",
            inline=False,
        )
        setup_em.set_footer(text="Breakthrough · Champion Edition v2")
        await interaction.followup.send(embed=setup_em)
        await self._post_round(guild_id)

    @app_commands.command(name="breakthrough-end", description="[Admin] Force-end an active match.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def breakthrough_end(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        state = self._games.pop(interaction.guild_id, None)
        if not state:
            await interaction.response.send_message("❌  No active match.", ephemeral=True)
            return
        if state.timer_task:
            state.timer_task.cancel()
        state.active = False
        await interaction.response.send_message("🛑  Match force-ended by admin.")

    # ═══════════════════════════ PLAYER COMMANDS ═══════════════════════════════

    @app_commands.command(
        name="submit-move",
        description="Queue your movement for this round.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        direction="up · down · left · right · hold · action (unlock adjacent door / breach vault)",
        distance="Tiles to move (1–3). Defaults to your current max movement.",
    )
    async def submit_move(
        self,
        interaction: discord.Interaction,
        direction:   str,
        distance:    app_commands.Range[int, 1, 3] = 3,
    ) -> None:
        assert interaction.guild_id is not None
        state = self._games.get(interaction.guild_id)
        if not state or not state.active:
            await interaction.response.send_message("❌  No active match.", ephemeral=True)
            return

        d = direction.strip().lower()
        if d not in VALID_DIRECTIONS:
            await interaction.response.send_message(
                f"❌  Invalid direction **{direction}**.\n"
                "Valid: `up` `down` `left` `right` `hold` `action`",
                ephemeral=True,
            )
            return

        champ = next(
            (c for c in state.champions.values() if c.user_id == interaction.user.id),
            None,
        )
        if not champ:
            await interaction.response.send_message(
                "❌  You're not a champion in this match.", ephemeral=True
            )
            return
        if champ.frozen_rounds > 0:
            await interaction.response.send_message(
                f"❄️  You're **frozen** for {champ.frozen_rounds} more round(s) — move locked.",
                ephemeral=True,
            )
            return

        champ.move          = d
        champ.move_distance = min(distance, champ.max_movement)
        champ.submitted     = True

        # Check time-skip
        all_in = all(c.submitted for c in state.champions.values())
        if all_in and state.timer_task and not state.timer_task.done():
            state.timer_task.cancel()
            state.timer_task = None
            channel = self.bot.get_channel(state.channel_id)
            if isinstance(channel, discord.TextChannel):
                asyncio.create_task(self._resolve_round(interaction.guild_id, channel))
            await interaction.response.send_message(
                f"✅  Move locked: **{d}** × {champ.move_distance} tile(s).\n"
                "⚡ **All champions submitted — resolving immediately!**",
                ephemeral=True,
            )
        else:
            remaining = sum(1 for c in state.champions.values() if not c.submitted)
            await interaction.response.send_message(
                f"✅  Move locked: **{d}** × {champ.move_distance} tile(s).\n"
                f"⏳  Waiting on **{remaining}** more champion(s).",
                ephemeral=True,
            )

    @app_commands.command(
        name="use",
        description="Activate a Breakthrough gadget using your Energy Pool.",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        gadget      ="Gadget: smoke (30 EP) · emp (40 EP) · decoy (50 EP)",
        target_slot ="(EMP only) Champion slot to stun: 1, 2, 3 or 4",
        direction   ="(Decoy only) Direction to place clone: up · down · left · right",
    )
    async def use_gadget(
        self,
        interaction: discord.Interaction,
        gadget:      str,
        target_slot: Optional[app_commands.Range[int, 1, 4]] = None,
        direction:   Optional[str] = None,
    ) -> None:
        assert interaction.guild_id is not None
        state = self._games.get(interaction.guild_id)
        if not state or not state.active:
            await interaction.response.send_message("❌  No active match.", ephemeral=True)
            return

        key = gadget.strip().lower()
        if key not in VALID_GADGETS:
            await interaction.response.send_message(
                "❌  Unknown gadget. Use: `smoke` · `emp` · `decoy`", ephemeral=True
            )
            return

        champ = next(
            (c for c in state.champions.values() if c.user_id == interaction.user.id),
            None,
        )
        if not champ:
            await interaction.response.send_message(
                "❌  You're not a champion in this match.", ephemeral=True
            )
            return

        g_name, g_cost = VALID_GADGETS[key]
        if champ.ep < g_cost:
            await interaction.response.send_message(
                f"❌  Not enough EP for **{g_name}** (costs **{g_cost} EP**, you have **{champ.ep} EP**).",
                ephemeral=True,
            )
            return

        if key == "emp":
            if target_slot is None:
                await interaction.response.send_message(
                    "❌  **EMP Charge** requires `target_slot` (1–4).", ephemeral=True
                )
                return
            ts = target_slot - 1
            if ts == champ.slot:
                await interaction.response.send_message(
                    "❌  You can't EMP yourself.", ephemeral=True
                )
                return
            if ts not in state.champions:
                await interaction.response.send_message(
                    "❌  Invalid target slot.", ephemeral=True
                )
                return
            champ.gadget_target_slot = ts

        elif key == "decoy":
            if direction is None or direction.strip().lower() not in {"up","down","left","right"}:
                await interaction.response.send_message(
                    "❌  **Decoy Hologram** requires a `direction` (up/down/left/right).",
                    ephemeral=True,
                )
                return
            champ.gadget_direction = direction.strip().lower()

        champ.gadget = key
        hint = ""
        if key == "emp":
            hint = f" → targeting **C{target_slot}**"
        elif key == "decoy":
            hint = f" → placing clone **{direction}**"
        await interaction.response.send_message(
            f"✅  **{g_name}** ({g_cost} EP) queued{hint}.\n"
            f"Your EP this round: **{champ.ep}** → **{champ.ep - g_cost}** after resolution.",
            ephemeral=True,
        )

    @app_commands.command(name="breakthrough-status", description="View the current board (private).")
    @app_commands.guild_only()
    async def breakthrough_status(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        state = self._games.get(interaction.guild_id)
        if not state or not state.active:
            await interaction.response.send_message("❌  No active match.", ephemeral=True)
            return
        img_buf = _render_grid_image(state)
        em      = self._round_embed(state, has_image=img_buf is not None)
        if img_buf:
            img_buf.seek(0)
            await interaction.response.send_message(
                file=discord.File(img_buf, filename="board.png"), embed=em, ephemeral=True
            )
        else:
            em.description = f"```\n{_render_grid_text(state)}\n```"
            await interaction.response.send_message(embed=em, ephemeral=True)

    @app_commands.command(
        name="breakthrough-leaderboard",
        description="Show the all-time Heist Coin leaderboard.",
    )
    @app_commands.guild_only()
    async def breakthrough_leaderboard(self, interaction: discord.Interaction) -> None:
        rows = _db_get_all_coins()
        em   = discord.Embed(title="🪙  Breakthrough — Heist Coin Leaderboard", colour=C_LB)
        if not rows:
            em.description = "_No coins earned yet. Start a match with `/breakthrough-setup`!_"
        else:
            medals = ["🥇", "🥈", "🥉"]
            lines  = []
            for i, (team, coins) in enumerate(rows):
                medal = medals[i] if i < 3 else f"`#{i+1}`"
                lines.append(f"{medal}  **{team.title()}** — **{coins:,}** 🪙")
            em.description = "\n".join(lines)
        em.set_footer(text="Breakthrough · Coins from loot tiles & vault breaches only")
        await interaction.response.send_message(embed=em)

    # ═══════════════════════════ ROUND ENGINE ══════════════════════════════════

    async def _post_round(self, guild_id: int) -> None:
        state = self._games.get(guild_id)
        if not state:
            return

        state.round_number += 1

        # Spawn diamond briefcase in round 3
        if state.round_number == DIAMOND_SPAWN_ROUND and not state.diamond_spawned:
            pos = _spawn_diamond(state)
            state.diamond_spawned = True
            channel = self.bot.get_channel(state.channel_id)
            if isinstance(channel, discord.TextChannel) and pos:
                await channel.send(
                    f"💎  **Round {state.round_number}!** The Diamond Briefcase has appeared at "
                    f"`[{pos[0]},{pos[1]}]` — +500 🪙 but it slows you by 1 tile/turn!"
                )

        channel = self.bot.get_channel(state.channel_id)
        if not channel or not isinstance(channel, discord.TextChannel):
            return

        # Reset round inputs; auto-submit frozen champions
        for champ in state.champions.values():
            champ.reset_round()
            if champ.frozen_rounds > 0:
                champ.submitted = True   # frozen = auto-hold

        # Skip countdown if everyone is already submitted (all frozen edge-case)
        if all(c.submitted for c in state.champions.values()):
            await self._resolve_round(guild_id, channel)
            return

        img_buf = _render_grid_image(state)
        em      = self._round_embed(state, has_image=img_buf is not None)

        if img_buf:
            img_buf.seek(0)
            msg = await channel.send(file=discord.File(img_buf, filename="board.png"), embed=em)
        else:
            em.description = f"```\n{_render_grid_text(state)}\n```"
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
        if state and state.active:
            await self._resolve_round(guild_id, channel)

    async def _resolve_round(self, guild_id: int, channel: discord.TextChannel) -> None:
        state = self._games.get(guild_id)
        if not state:
            return

        log: list[str] = []

        # ── Default missing moves ─────────────────────────────────────────────
        for champ in state.champions.values():
            if champ.move is None and champ.frozen_rounds == 0:
                champ.move = "hold"
                log.append(
                    f"⏰ **{champ.display_name}** didn't submit — defaulting to **hold**."
                )

        # ════════════════════════════════════════════════════════════════════
        # PHASE 1 — GADGETS
        # ════════════════════════════════════════════════════════════════════

        # Decrement frozen rounds NOW so the current round's stun still counts
        for champ in state.champions.values():
            if champ.frozen_rounds > 0:
                champ.frozen_rounds -= 1
                if champ.frozen_rounds == 0:
                    log.append(f"🌡️ **{champ.display_name}** is no longer frozen — free next round.")

        for champ in state.champions.values():
            if not champ.gadget:
                continue
            key         = champ.gadget
            g_name, g_cost = VALID_GADGETS[key]

            if champ.ep < g_cost:
                log.append(f"⚡ **{champ.display_name}** tried **{g_name}** but has insufficient EP.")
                continue

            champ.ep -= g_cost

            if key == "smoke":
                champ.smoked_rounds = 2
                log.append(
                    f"💨 **{champ.display_name}** deployed a **Smoke Grenade**! "
                    f"Position hidden for 2 rounds. (−{g_cost} EP)"
                )

            elif key == "emp":
                ts     = champ.gadget_target_slot
                target = state.champions.get(ts) if ts is not None else None
                if target and _is_adjacent(champ.row, champ.col, target.row, target.col):
                    target.frozen_rounds = max(target.frozen_rounds, 1)
                    target.move          = "hold"
                    target.submitted     = True
                    log.append(
                        f"⚡ **{champ.display_name}** hit **{target.display_name}** with EMP! "
                        f"Stunned for 1 round. (−{g_cost} EP)"
                    )
                else:
                    target_name = state.champions[ts].display_name if ts in state.champions else "?"
                    log.append(
                        f"⚡ **{champ.display_name}**'s EMP missed — "
                        f"**{target_name}** is not adjacent."
                    )

            elif key == "decoy":
                dr, dc = _DIR_DELTA.get(champ.gadget_direction or "hold", (0, 0))
                nr, nc = champ.row + dr, champ.col + dc
                if (0 <= nr < GRID_SIZE and 0 <= nc < GRID_SIZE
                        and (nr, nc) not in state.walls
                        and (nr, nc) not in VAULT_TILES):
                    # Remove old decoy if any
                    champ.has_decoy  = True
                    champ.decoy_row  = nr
                    champ.decoy_col  = nc
                    log.append(
                        f"👻 **{champ.display_name}** placed a **Decoy Hologram** at "
                        f"`[{nr},{nc}]`. (−{g_cost} EP)"
                    )
                else:
                    log.append(
                        f"👻 **{champ.display_name}**'s Decoy failed — "
                        f"target tile `[{nr},{nc}]` is blocked."
                    )

        # ════════════════════════════════════════════════════════════════════
        # PHASE 2 — MOVEMENT
        # ════════════════════════════════════════════════════════════════════

        # Handle door-unlock actions first
        for champ in state.champions.values():
            if champ.move == "action":
                unlocked_any = False
                for dr, dc in [(-1,0),(1,0),(0,-1),(0,1)]:
                    adj = (champ.row + dr, champ.col + dc)
                    if adj in state.doors and adj not in state.opened_doors:
                        state.opened_doors.add(adj)
                        log.append(
                            f"🔓 **{champ.display_name}** unlocked the security door at `{adj}`!"
                        )
                        unlocked_any = True
                if not unlocked_any:
                    log.append(
                        f"🔑 **{champ.display_name}** used action — no adjacent locked door."
                    )

        # Capture starting positions for collision rollback
        old_pos: dict[int, tuple[int, int]] = {
            s: (c.row, c.col) for s, c in state.champions.items()
        }

        # Walk champions
        for slot, champ in state.champions.items():
            if champ.frozen_rounds > 0 or champ.move in (None, "hold", "action"):
                continue
            _walk_champion(champ, state, log)

        # Collision detection — two champions on the same tile bounce back
        dest_map: dict[tuple[int, int], list[int]] = {}
        for slot, champ in state.champions.items():
            dest_map.setdefault((champ.row, champ.col), []).append(slot)

        for pos, slots in dest_map.items():
            if len(slots) > 1:
                for s in slots:
                    champ = state.champions[s]
                    or_, oc_ = old_pos[s]
                    champ.row, champ.col = or_, oc_
                    log.append(
                        f"💥 **{champ.display_name}** collided at `{pos}` — bounced back to `[{or_},{oc_}]`!"
                    )

        # ════════════════════════════════════════════════════════════════════
        # LOOT COLLECTION (at final positions)
        # ════════════════════════════════════════════════════════════════════
        game_over   = False
        winner_team: Optional[str] = None

        for champ in state.champions.values():
            pos = (champ.row, champ.col)

            # Loot
            loot_kind = state.loot_tiles.get(pos)
            if loot_kind == "common":
                del state.loot_tiles[pos]
                coins = LOOT_REWARD_COMMON
                champ.loot_claimed += 1
                new_bal = _db_add_coins(champ.team, coins)
                log.append(
                    f"💵 **{champ.display_name}** grabbed cash! +{coins:,} 🪙 "
                    f"(team total: {new_bal:,})"
                )
            elif loot_kind == "diamond":
                del state.loot_tiles[pos]
                coins = LOOT_REWARD_DIAMOND
                champ.carrying_diamond = True
                champ.loot_claimed += 1
                new_bal = _db_add_coins(champ.team, coins)
                log.append(
                    f"💎 **{champ.display_name}** seized the Diamond Briefcase! "
                    f"+{coins:,} 🪙 — movement reduced to **{champ.max_movement}** tile(s)/round. "
                    f"(team total: {new_bal:,})"
                )

            # Vault breach
            if pos in VAULT_TILES:
                state.breach_points += 1
                champ.breach_points_contrib += 1
                new_bal = _db_add_coins(champ.team, BREACH_COIN_REWARD)
                log.append(
                    f"🏦 **{champ.display_name}** breached the vault! "
                    f"+1 Breach Point (+{BREACH_COIN_REWARD:,} 🪙). "
                    f"Total: **{state.breach_points}/{VAULT_POINTS_REQ}**"
                )
                if state.breach_points >= VAULT_POINTS_REQ:
                    game_over   = True
                    winner_team = champ.team
                    break

        # ════════════════════════════════════════════════════════════════════
        # PHASE 3 — TRAPS (final positions, if not already game over)
        # ════════════════════════════════════════════════════════════════════
        if not game_over:
            for champ in state.champions.values():
                pos = (champ.row, champ.col)
                if pos in state.traps:
                    state.traps.discard(pos)
                    state.triggered_traps.add(pos)
                    champ.frozen_rounds += 1
                    log.append(
                        f"⚠️ **{champ.display_name}** triggered a hidden Alarm Trap at `{pos}`! "
                        f"Stunned for **1 round**."
                    )

        # ────── End-of-round bookkeeping ──────────────────────────────────
        for champ in state.champions.values():
            # Smoke tick
            if champ.smoked_rounds > 0:
                champ.smoked_rounds -= 1
            # EP regen
            champ.ep = min(MAX_EP, champ.ep + EP_REGEN)

        # ── Game over ─────────────────────────────────────────────────────
        if game_over and winner_team:
            state.active = False
            self._games.pop(guild_id, None)
            await self._post_victory(channel, state, winner_team, log)
            return

        # ── Action log ────────────────────────────────────────────────────
        if log:
            log_em = discord.Embed(
                title=f"📋  Round {state.round_number} — Action Log",
                colour=C_ACTION,
                description="\n".join(log),
            )
            log_em.set_footer(
                text=f"Vault: {state.breach_points}/{VAULT_POINTS_REQ} BP  ·  "
                     f"Loot tiles left: {sum(1 for v in state.loot_tiles.values() if v == 'common')}"
            )
            await channel.send(embed=log_em)

        await self._post_round(guild_id)

    # ═══════════════════════════ EMBEDS ════════════════════════════════════════

    def _round_embed(self, state: GameState, *, has_image: bool = False) -> discord.Embed:
        em = discord.Embed(
            title=f"🏦  Breakthrough — Round {state.round_number}",
            colour=C_ROUND,
        )
        if has_image:
            em.set_image(url="attachment://board.png")

        # Champion status table
        lines: list[str] = []
        for slot, champ in state.champions.items():
            tags: list[str] = []
            if champ.frozen_rounds > 0: tags.append(f"❄️ Frozen ({champ.frozen_rounds}r)")
            if champ.smoked_rounds > 0: tags.append(f"💨 Smoked ({champ.smoked_rounds}r)")
            if champ.carrying_diamond:  tags.append("💎 Carrying")
            status = " · ".join(tags) if tags else "Active"
            pos    = f"`[{champ.row},{champ.col}]`" if champ.smoked_rounds == 0 else "`[?,?]`"
            lines.append(
                f"{champ.emoji} **C{slot+1}** ({champ.team.title()}) "
                f"{pos} · **{champ.ep} EP** · {status}"
            )
        em.add_field(name="Champions", value="\n".join(lines), inline=False)
        em.add_field(
            name="🏦 Vault", value=f"**{state.breach_points}/{VAULT_POINTS_REQ}** Breach Points",
            inline=True,
        )
        em.add_field(
            name="⏱️ Planning",
            value=f"**{ROUND_TIMER}s** — `/submit-move` & `/use`",
            inline=True,
        )
        em.add_field(
            name="💵 Loot Remaining",
            value=f"**{sum(1 for v in state.loot_tiles.values() if v == 'common')}** common "
                  f"· {'**1** 💎 diamond' if 'diamond' in state.loot_tiles.values() else '_none_'}",
            inline=True,
        )
        em.set_footer(
            text=f"Breakthrough  ·  Doors unlocked: {len(state.opened_doors)}/{len(FIXED_DOORS)}"
        )
        return em

    async def _post_victory(
        self,
        channel:     discord.TextChannel,
        state:       GameState,
        winner_team: str,
        log:         list[str],
    ) -> None:
        if log:
            log_em = discord.Embed(
                title=f"📋  Round {state.round_number} — Final Action Log",
                colour=C_VAULT,
                description="\n".join(log),
            )
            await channel.send(embed=log_em)

        # Build leaderboard from this match
        ranking = sorted(
            state.champions.values(),
            key=lambda c: (-c.breach_points_contrib, -c.loot_claimed, c.tiles_traveled),
        )
        rank_lines = []
        medals = ["🥇", "🥈", "🥉", "4️⃣"]
        for i, c in enumerate(ranking):
            rank_lines.append(
                f"{medals[i]} **{c.team.title()}** — "
                f"{c.breach_points_contrib} BP · {c.loot_claimed} loot · "
                f"{c.tiles_traveled} tiles"
            )

        winner_champ = next(
            (c for c in state.champions.values() if c.team == winner_team), None
        )

        img_buf = _render_grid_image(state)
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
            name="📊  Match Ranking",
            value="\n".join(rank_lines),
            inline=False,
        )
        em.add_field(
            name="🏦 Breach Points",
            value=f"**{state.breach_points}/{VAULT_POINTS_REQ}** — Vault Cracked",
            inline=True,
        )
        em.set_footer(text="Breakthrough · Champion Edition v2 · Match Over")

        if img_buf:
            img_buf.seek(0)
            await channel.send(file=discord.File(img_buf, filename="board.png"), embed=em)
        else:
            await channel.send(embed=em)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BankBreakthroughCog(bot))
