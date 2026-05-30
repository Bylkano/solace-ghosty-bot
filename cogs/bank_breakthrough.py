"""
cogs/bank_breakthrough.py
─────────────────────────────────────────────────────────────────────────────
Breakthrough — Champion Edition  (v3 · 15×15 grid · UI Buttons)
─────────────────────────────────────────────────────────────────────────────

Admin commands:
  /team-add            [team_name] [@member]      — assign member to a team
  /team-remove         [@member]                  — remove member from team
  /team-list           [team_name]                — list team members
  /breakthrough-setup  [u1] [u2] [u3] [u4]        — start a match
  /breakthrough-end                               — force-end active match

Player interaction — Discord UI Buttons on the board message:
  [🔼] [🔽] [◀️] [▶️]   — Queue 1 tile of movement for this round
  [⚡ Activate Boost]   — Spend 20 EP to unlock a second direction press
  [🔓 Unlock Door]      — Queue a door-unlock action instead of movement
  [🔒 Lock In Turn]     — Submit your choices; round resolves when all lock in
  [🎒 Select Gadget ▾]  — Dropdown: Smoke / EMP (×4 targets) / Decoy (×4 dirs)

  /breakthrough-status        — private board view
  /breakthrough-leaderboard   — all-time Heist Coin rankings

Gadgets (Energy Pool: 100 EP max · +10 EP/round regen):
  smoke  (30 EP) — hides coordinates for 2 rounds (Fog of War on board)
  emp    (40 EP) — stuns an adjacent champion for 1 round
  decoy  (50 EP) — places a fake clone on an adjacent tile

Boost:
  [⚡ Activate Boost] costs 20 EP (deducted at resolution).
  Unlocks a second directional button press for this turn.
  Cannot boost while carrying the Diamond Briefcase.

Map (15×15):
  • Spawns at corners: [0,0] [0,14] [14,0] [14,14]
  • Central Vault: 2×2 block [7,7]–[8,8] · needs 3 Breach Points
  • Security Walls: randomly scattered; BFS-validated open paths
  • Security Doors: guard all 4 vault approaches
  • Common Loot (💵): +100 🪙
  • Diamond Briefcase (💎): spawns round 3 · +500 🪙 · blocks Boost
  • Alarm Traps (⚠️): hidden until stepped on; stun 1 round

Round Flow:
  1. Planning Phase — 30 s max countdown
     TIME-SKIP: immediately resolves the moment all players lock in
  2. Resolution — Gadgets → Boosts → Movements → Traps
  3. Ranking — Breach Points + Loot; tiebreaker = fewest tiles traveled
"""

from __future__ import annotations

import asyncio
import random
import os
from collections import deque
from io import BytesIO
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

import psycopg2
from psycopg2.extras import RealDictCursor

import discord
from discord import app_commands
from discord.ext import commands

# ─────────────────────────── Game constants ───────────────────────────────────

GRID_SIZE           = 15
VAULT_TILES         = frozenset({(7, 7), (7, 8), (8, 7), (8, 8)})
VAULT_POINTS_REQ    = 3
ROUND_TIMER         = 30
MAX_EP                 = 100
EP_REGEN               = 10
EP_REGEN_OVERDRIVE     = 15
BOOST_EP_COST          = 20
BOOST_EP_COST_OVERDRIVE = 10
DIAMOND_SPAWN_ROUND = 3

LOOT_REWARD_COMMON  = 100
LOOT_REWARD_DIAMOND = 500
BREACH_COIN_REWARD  = 300

NUM_RANDOM_WALLS    = 28
NUM_LOOT_TILES      = 10
NUM_TRAPS           = 6

CHAMPION_STARTS = [(0, 0), (0, 14), (14, 0), (14, 14)]

FIXED_DOORS: frozenset[tuple[int, int]] = frozenset({
    (6, 7), (6, 8),
    (9, 7), (9, 8),
    (7, 6), (8, 6),
    (7, 9), (8, 9),
})

SEP = "─" * 40

# ─────────────────────────── Display ──────────────────────────────────────────

EMOJI_CHAMP = ["🟦", "🟨", "🟩", "🟥"]

C_ROUND  = 0x4F46E5
C_ACTION = 0xF59E0B
C_VAULT  = 0xFFD700
C_WIN    = 0x22C55E
C_TEAM   = 0x818CF8
C_ERROR  = 0xEF4444
C_LB     = 0xA78BFA

VALID_GADGETS: dict[str, tuple[str, int]] = {
    "smoke":     ("Smoke Grenade",    30),
    "overdrive": ("Overdrive",        30),
    "emp":       ("EMP Charge",       40),
    "decoy":     ("Decoy Hologram",   50),
    "hijack":    ("Direction Hijack", 50),
}

_DIR_DELTA: dict[str, tuple[int, int]] = {
    "up":     (-1,  0),
    "down":   ( 1,  0),
    "left":   ( 0, -1),
    "right":  ( 0,  1),
    "hold":   ( 0,  0),
    "action": ( 0,  0),
}

# ═══════════════════════════════ DATABASE LAYER ═══════════════════════════════

def _db_connect():
    return psycopg2.connect(os.environ["DATABASE_URL"])


def _db_ensure_tables() -> None:
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS breakthrough_coins (
                    team_name  TEXT    PRIMARY KEY,
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
                "SELECT team_name FROM breakthrough_teams WHERE guild_id = %s AND user_id = %s",
                (guild_id, user_id),
            )
            row = cur.fetchone()
            return row[0] if row else None


def _db_get_team_members(guild_id: int, team_name: str) -> list[int]:
    with _db_connect() as con:
        with con.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM breakthrough_teams WHERE guild_id = %s AND team_name = %s",
                (guild_id, team_name.lower()),
            )
            return [row[0] for row in cur.fetchall()]


# ═══════════════════════════════ DATA MODELS ══════════════════════════════════


@dataclass
class Champion:
    user_id: int
    team:    str
    slot:    int
    row:     int
    col:     int

    ep:                    int  = MAX_EP
    tiles_traveled:        int  = 0
    breach_points_contrib: int  = 0
    loot_claimed:          int  = 0
    carrying_diamond:      bool = False

    frozen_rounds:   int = 0
    smoked_rounds:   int = 0
    overdrive_rounds: int = 0

    has_decoy:  bool = False
    decoy_row:  int  = -1
    decoy_col:  int  = -1

    # Previous tile (captured at round start — used by Decoy Hologram)
    prev_row: int = -1
    prev_col: int = -1

    # Per-round planning inputs (reset each round)
    move_queue:         list          = field(default_factory=list)
    boost_requested:    bool          = False
    gadget:             Optional[str] = None
    gadget_target_slot: Optional[int] = None   # EMP / Hijack target slot
    hijacked_by_slot:   Optional[int] = None   # set by Direction Hijack during resolution
    submitted:          bool          = False

    @property
    def boost_ep_cost(self) -> int:
        """Actual Boost EP cost — reduced to 10 when Overdrive is active."""
        return BOOST_EP_COST_OVERDRIVE if self.overdrive_rounds > 0 else BOOST_EP_COST

    @property
    def display_name(self) -> str:
        return f"C{self.slot + 1} ({self.team.title()})"

    @property
    def emoji(self) -> str:
        return EMOJI_CHAMP[self.slot]

    def reset_round(self) -> None:
        # Snapshot current position as "previous" (used by Decoy Hologram)
        self.prev_row           = self.row
        self.prev_col           = self.col
        self.move_queue         = []
        self.boost_requested    = False
        self.gadget             = None
        self.gadget_target_slot = None
        self.hijacked_by_slot   = None
        self.submitted          = False


@dataclass
class GameState:
    channel_id: int
    guild_id:   int
    champions:  dict[int, Champion]

    walls:           frozenset[tuple[int, int]] = field(default_factory=frozenset)
    doors:           set[tuple[int, int]]        = field(default_factory=lambda: set(FIXED_DOORS))
    opened_doors:    set[tuple[int, int]]        = field(default_factory=set)
    loot_tiles:      dict[tuple[int, int], str]  = field(default_factory=dict)
    traps:           set[tuple[int, int]]        = field(default_factory=set)
    triggered_traps: set[tuple[int, int]]        = field(default_factory=set)

    breach_points:   int  = 0
    round_number:    int  = 0
    active:          bool = True
    diamond_spawned: bool = False
    resolving:       bool = False

    timer_task:    Optional[asyncio.Task]        = field(default=None, compare=False)
    board_message: Optional[discord.Message]     = field(default=None, compare=False)
    active_view:   Optional["BreakthroughView"]  = field(default=None, compare=False)


# ═══════════════════════════════ MAP GENERATION ═══════════════════════════════


def _bfs_reachable(
    walls: frozenset | set,
    closed_doors: frozenset | set,
    start: tuple[int, int],
    size: int,
) -> set[tuple[int, int]]:
    visited: set[tuple[int, int]] = {start}
    queue: deque[tuple[int, int]] = deque([start])
    while queue:
        r, c = queue.popleft()
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            p = (r + dr, c + dc)
            if p not in visited and 0 <= p[0] < size and 0 <= p[1] < size:
                if p not in walls and p not in closed_doors:
                    visited.add(p)
                    queue.append(p)
    return visited


def _all_corners_reach_vault(
    walls: frozenset | set,
    doors: frozenset | set,
    starts: list[tuple[int, int]],
    vault: frozenset[tuple[int, int]],
    size: int,
) -> bool:
    for start in starts:
        reachable = _bfs_reachable(walls, doors, start, size)
        if not any(v in reachable for v in vault):
            return False
    return True


def _generate_random_walls(
    size: int,
    vault: frozenset[tuple[int, int]],
    doors: frozenset[tuple[int, int]],
    starts: list[tuple[int, int]],
    n: int = NUM_RANDOM_WALLS,
) -> frozenset[tuple[int, int]]:
    forbidden = set(starts) | vault | doors
    candidates = [
        (r, c) for r in range(size) for c in range(size) if (r, c) not in forbidden
    ]
    random.shuffle(candidates)
    walls: set[tuple[int, int]] = set()
    for pos in candidates:
        if len(walls) >= n:
            break
        test = walls | {pos}
        if _all_corners_reach_vault(test, doors, starts, vault, size):
            walls.add(pos)
    return frozenset(walls)


def _generate_map(state: GameState) -> None:
    state.walls = _generate_random_walls(
        GRID_SIZE, VAULT_TILES, FIXED_DOORS, CHAMPION_STARTS, NUM_RANDOM_WALLS
    )
    taken: set[tuple[int, int]] = (
        set(CHAMPION_STARTS) | VAULT_TILES | state.walls | state.doors
    )
    candidates = [
        (r, c) for r in range(GRID_SIZE) for c in range(GRID_SIZE) if (r, c) not in taken
    ]
    random.shuffle(candidates)
    for pos in candidates[:NUM_LOOT_TILES]:
        state.loot_tiles[pos] = "common"
    state.traps = set(candidates[NUM_LOOT_TILES : NUM_LOOT_TILES + NUM_TRAPS])


def _spawn_diamond(state: GameState) -> Optional[tuple[int, int]]:
    occupied = (
        {(c.row, c.col) for c in state.champions.values()}
        | VAULT_TILES | state.walls | state.doors
        | set(state.loot_tiles) | state.traps | state.triggered_traps
    )
    pool = [(r, c) for r in range(GRID_SIZE) for c in range(GRID_SIZE) if (r, c) not in occupied]
    if not pool:
        return None
    pos = random.choice(pool)
    state.loot_tiles[pos] = "diamond"
    return pos


# ═══════════════════════════ MOVEMENT HELPERS ═════════════════════════════════


def _is_adjacent(r1: int, c1: int, r2: int, c2: int) -> bool:
    return abs(r1 - r2) <= 1 and abs(c1 - c2) <= 1 and (r1, c1) != (r2, c2)


def _walk_one_step(
    champ: Champion,
    direction: str,
    state: GameState,
    log: list[str],
) -> bool:
    if direction in (None, "hold", "action"):
        return False
    dr, dc = _DIR_DELTA.get(direction, (0, 0))
    nr, nc = champ.row + dr, champ.col + dc
    if not (0 <= nr < GRID_SIZE and 0 <= nc < GRID_SIZE):
        log.append(f"🚧 **{champ.display_name}** hit the grid boundary going **{direction}**.")
        return False
    if (nr, nc) in state.walls:
        log.append(f"🧱 **{champ.display_name}** blocked by a wall going **{direction}**.")
        return False
    if (nr, nc) in state.doors and (nr, nc) not in state.opened_doors:
        log.append(f"🚪 **{champ.display_name}** blocked by a locked door going **{direction}**.")
        return False
    champ.row, champ.col = nr, nc
    champ.tiles_traveled += 1
    return True


# ═══════════════════════════ GRID RENDERER (PNG) ══════════════════════════════


def _render_grid_image(state: GameState) -> Optional[BytesIO]:
    """Render a styled 15×15 board PNG with advanced visual effects."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    CELL = 40
    PAD  = 52
    LAB  = 22

    W = PAD + LAB + GRID_SIZE * CELL + PAD
    H = PAD + LAB + GRID_SIZE * CELL + PAD + 24

    BG          = (4,   6,  18)
    BOARD_BG    = (10,  16,  34)
    CELL_A      = (18,  26,  50)
    CELL_B      = (14,  22,  44)
    GRID_LINE   = (24,  36,  64)
    WALL_BG     = (9,   9,  16)
    WALL_MORTAR = (28,  28,  42)
    DOOR_C_BG   = (88,  52,  18)
    DOOR_C_LINE = (150, 90,  30)
    DOOR_C_LOCK = (215, 155, 50)
    DOOR_O_BG   = (50,  85,  35)
    LOOT_BG     = (8,   50,  14)
    LOOT_FG     = (40,  190, 65)
    DIAM_BG     = (6,   22,  80)
    DIAM_FG     = (70,  150, 255)
    TRAP_BG     = (65,  6,   6)
    TRAP_FG     = (250, 50,  50)
    VAULT_BG    = (55,  38,  0)
    VAULT_RING  = (255, 195, 0)
    VAULT_TXT   = (255, 248, 130)
    CHAMP_COLS  = [
        (50,  115, 225),
        (215, 170,   0),
        (35,  190,  75),
        (215,  50,  50),
    ]
    DECOY_COL   = (105, 105, 170)
    ICE_RING    = (95,  180, 255)
    TEXT_BRIGHT = (240, 245, 255)
    TEXT_DIM    = (55,  85, 125)

    img  = Image.new("RGBA", (W, H), BG + (255,))
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
    draw.rectangle([bx0 - 3, by0 - 3, bx1 + 3, by1 + 3], fill=BOARD_BG)

    # Neon metallic border (layered glow)
    for thick, alpha in [(10, 25), (6, 55), (3, 120), (1, 220)]:
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        pad = thick + 3
        ld.rectangle(
            [bx0 - pad, by0 - pad, bx1 + pad, by1 + pad],
            outline=(0, 210, 255, alpha), width=2,
        )
        img = Image.alpha_composite(img, layer)
        draw = ImageDraw.Draw(img)

    # Inner corner accents
    acc_len = 12
    for (ax, ay), (dx1, dy1), (dx2, dy2) in [
        ((bx0 - 5, by0 - 5), (acc_len, 0), (0, acc_len)),
        ((bx1 + 5, by0 - 5), (-acc_len, 0), (0, acc_len)),
        ((bx0 - 5, by1 + 5), (acc_len, 0), (0, -acc_len)),
        ((bx1 + 5, by1 + 5), (-acc_len, 0), (0, -acc_len)),
    ]:
        draw.line([(ax, ay), (ax + dx1, ay + dy1)], fill=(0, 240, 255), width=2)
        draw.line([(ax, ay), (ax + dx2, ay + dy2)], fill=(0, 240, 255), width=2)

    # Coordinate labels (0–14) — fixed-width futuristic look
    for i in range(GRID_SIZE):
        cx = bx0 + i * CELL + CELL // 2
        cy = by0 + i * CELL + CELL // 2
        draw.text((cx, by0 - 13), str(i), fill=TEXT_DIM, font=f_sm, anchor="mm")
        draw.text((bx0 - 13, cy), str(i), fill=TEXT_DIM, font=f_sm, anchor="mm")

    # Champion & decoy lookups
    champ_at: dict[tuple[int, int], int] = {
        (c.row, c.col): slot for slot, c in state.champions.items()
    }
    decoy_at: set[tuple[int, int]] = {
        (c.decoy_row, c.decoy_col) for c in state.champions.values() if c.has_decoy
    }
    smoked_slots: set[int] = {
        slot for slot, c in state.champions.items() if c.smoked_rounds > 0
    }
    smoke_positions: set[tuple[int, int]] = {
        (c.row, c.col) for c in state.champions.values() if c.smoked_rounds > 0
    }

    # Vault center pixel coords
    vault_cx = bx0 + 7 * CELL + CELL
    vault_cy = by0 + 7 * CELL + CELL

    # ── Drop shadow layer (walls + closed doors) ──────────────────────────────
    shadow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow_layer)
    for row in range(GRID_SIZE):
        for col in range(GRID_SIZE):
            pos = (row, col)
            if pos in state.walls or (pos in state.doors and pos not in state.opened_doors):
                x0 = bx0 + col * CELL
                y0 = by0 + row * CELL
                sd.rectangle([x0 + 5, y0 + 5, x0 + CELL + 5, y0 + CELL + 5], fill=(0, 0, 0, 85))
    img = Image.alpha_composite(img, shadow_layer)
    draw = ImageDraw.Draw(img)

    # ── Draw each cell ────────────────────────────────────────────────────────
    for row in range(GRID_SIZE):
        for col in range(GRID_SIZE):
            x0 = bx0 + col * CELL + 1
            y0 = by0 + row * CELL + 1
            x1 = x0 + CELL - 2
            y1 = y0 + CELL - 2
            cx = (x0 + x1) // 2
            cy = (y0 + y1) // 2
            pos = (row, col)

            is_vault      = pos in VAULT_TILES
            is_wall       = pos in state.walls
            is_door_closed = pos in state.doors and pos not in state.opened_doors
            is_door_open  = pos in state.opened_doors
            loot_kind     = state.loot_tiles.get(pos)
            is_trap_vis   = pos in state.triggered_traps
            is_decoy      = pos in decoy_at
            slot          = champ_at.get(pos)
            if slot in smoked_slots:
                slot = None

            if is_wall:
                draw.rectangle([x0, y0, x1, y1], fill=WALL_BG)
                for wy in range(y0 + 6, y1, 6):
                    draw.line([(x0, wy), (x1, wy)], fill=WALL_MORTAR, width=1)
                shift = 0
                for wy in range(y0, y1, 12):
                    draw.line([(x0 + shift, wy), (x0 + shift, wy + 6)], fill=WALL_MORTAR, width=1)
                    draw.line([(x0 + 10 + shift, wy), (x0 + 10 + shift, wy + 6)], fill=WALL_MORTAR, width=1)
                    shift = (shift + 5) % 10
                continue

            elif is_vault:
                draw.rectangle([x0, y0, x1, y1], fill=VAULT_BG, outline=VAULT_RING, width=1)
                if pos == (7, 7):
                    draw.text((vault_cx, vault_cy - 6), "★", fill=VAULT_TXT, font=f_lg, anchor="mm")
                    draw.text((vault_cx, vault_cy + 8), "VAULT", fill=VAULT_RING, font=f_sm, anchor="mm")

            elif is_door_closed:
                draw.rectangle([x0, y0, x1, y1], fill=DOOR_C_BG)
                draw.line([(cx, y0 + 2), (cx, y1 - 2)], fill=DOOR_C_LINE, width=2)
                rr = 3
                draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=DOOR_C_LOCK)

            elif is_door_open:
                draw.rectangle([x0, y0, x1, y1], fill=DOOR_O_BG)
                draw.line([(x0 + 3, y0 + 3), (x0 + 3, y1 - 3)], fill=DOOR_C_LINE, width=2)

            else:
                base = CELL_A if (row + col) % 2 == 0 else CELL_B
                draw.rectangle([x0, y0, x1, y1], fill=base, outline=GRID_LINE, width=1)

            # Tile overlays
            if loot_kind == "common":
                draw.rectangle([x0 + 4, y0 + 4, x1 - 4, y1 - 4], fill=LOOT_BG, outline=LOOT_FG, width=1)
                draw.text((cx, cy), "$", fill=LOOT_FG, font=f_md, anchor="mm")
            elif loot_kind == "diamond":
                draw.rectangle([x0 + 4, y0 + 4, x1 - 4, y1 - 4], fill=DIAM_BG, outline=DIAM_FG, width=1)
                draw.text((cx, cy), "◆", fill=DIAM_FG, font=f_md, anchor="mm")
            if is_trap_vis:
                draw.rectangle([x0 + 4, y0 + 4, x1 - 4, y1 - 4], fill=TRAP_BG, outline=TRAP_FG, width=1)
                draw.text((cx, cy), "!", fill=TRAP_FG, font=f_md, anchor="mm")

            # Decoy token
            if is_decoy and slot is None:
                rr = 11
                draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=DECOY_COL, outline=(200, 200, 255), width=1)
                draw.text((cx, cy), "?", fill=(200, 200, 255), font=f_md, anchor="mm")

            # Champion token
            if slot is not None:
                c_col = CHAMP_COLS[slot]
                rr = 13
                draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=c_col, outline=TEXT_BRIGHT, width=1)
                draw.text((cx, cy - 3), f"C{slot + 1}", fill=TEXT_BRIGHT, font=f_sm, anchor="mm")
                champ = state.champions[slot]
                if champ.carrying_diamond:
                    draw.text((cx + rr - 5, cy - rr + 4), "◆", fill=DIAM_FG, font=f_sm, anchor="mm")
                if champ.frozen_rounds > 0:
                    draw.ellipse([cx - rr - 3, cy - rr - 3, cx + rr + 3, cy + rr + 3],
                                 outline=ICE_RING, width=2)
                if champ.submitted:
                    draw.text((cx, cy + 5), "✓", fill=(80, 255, 120), font=f_sm, anchor="mm")

    # ── Vault radial glow overlay ─────────────────────────────────────────────
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd   = ImageDraw.Draw(glow)
    for radius, alpha in [(75, 15), (58, 30), (42, 50), (28, 75), (16, 100)]:
        gd.ellipse(
            [vault_cx - radius, vault_cy - radius, vault_cx + radius, vault_cy + radius],
            fill=(255, 200, 0, alpha),
        )
    img = Image.alpha_composite(img, glow)
    draw = ImageDraw.Draw(img)

    # ── Fog of War — smoke grenade coverage ───────────────────────────────────
    fog = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    fd  = ImageDraw.Draw(fog)
    for (sr, sc) in smoke_positions:
        fx0, fy0 = bx0 + sc * CELL + 1, by0 + sr * CELL + 1
        fx1, fy1 = fx0 + CELL - 2, fy0 + CELL - 2
        fd.rectangle([fx0, fy0, fx1, fy1], fill=(75, 35, 115, 145))
        for dr2, dc2 in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nr2, nc2 = sr + dr2, sc + dc2
            if 0 <= nr2 < GRID_SIZE and 0 <= nc2 < GRID_SIZE:
                ax0 = bx0 + nc2 * CELL + 1
                ay0 = by0 + nr2 * CELL + 1
                fd.rectangle([ax0, ay0, ax0 + CELL - 2, ay0 + CELL - 2], fill=(75, 35, 115, 55))
    img = Image.alpha_composite(img, fog)
    draw = ImageDraw.Draw(img)

    # ── Bottom legend (EP bars) ───────────────────────────────────────────────
    legend_y = by1 + 8
    seg_w    = (bx1 - bx0) // 4
    for slot, champ in state.champions.items():
        lx    = bx0 + slot * seg_w
        c_col = CHAMP_COLS[slot]
        draw.rectangle([lx, legend_y, lx + 8, legend_y + 8], fill=c_col)
        parts = [f"C{slot+1} {champ.ep}EP"]
        if champ.boost_requested:   parts.append("⚡")
        if champ.carrying_diamond:  parts.append("◆")
        if champ.frozen_rounds > 0: parts.append(f"❄{champ.frozen_rounds}r")
        if champ.smoked_rounds > 0: parts.append(f"💨{champ.smoked_rounds}r")
        if champ.submitted:         parts.append("🔒")
        draw.text((lx + 12, legend_y + 4), " ".join(parts), fill=TEXT_DIM, font=f_sm, anchor="lm")

    buf = BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)
    buf.seek(0)
    return buf


def _render_grid_text(state: GameState) -> str:
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
            elif pos in VAULT_TILES:   row.append("🏦")
            elif pos in state.walls:   row.append("🧱")
            elif pos in state.triggered_traps: row.append("⚠️")
            elif pos in state.doors and pos not in state.opened_doors: row.append("🚪")
            elif pos in state.opened_doors: row.append("🔓")
            elif state.loot_tiles.get(pos) == "diamond": row.append("💎")
            elif state.loot_tiles.get(pos) == "common":  row.append("💵")
            else: row.append("⬛")
        rows.append("".join(row))
    return "\n".join(rows)


def _status_table(state: GameState) -> str:
    col_w = [12, 9, 5, 10, 22]
    header = (
        f"{'Champion':<{col_w[0]}} {'Pos':<{col_w[1]}} {'EP':>{col_w[2]}} "
        f"{'Carrying':<{col_w[3]}} Status"
    )
    sep = "─" * (sum(col_w) + 4)
    rows = [header, sep]
    for slot, c in state.champions.items():
        pos      = f"[{c.row},{c.col}]" if c.smoked_rounds == 0 else "[?,?]"
        carrying = "💎 Diamond" if c.carrying_diamond else "—"
        tags: list[str] = []
        if c.frozen_rounds > 0:   tags.append(f"❄️ Frozen({c.frozen_rounds}r)")
        if c.smoked_rounds > 0:   tags.append(f"💨 Smoked({c.smoked_rounds}r)")
        if c.overdrive_rounds > 0: tags.append(f"⚡ OD({c.overdrive_rounds}r)")
        if c.submitted:            tags.append("🔒 Locked")
        if c.boost_requested:      tags.append("⚡ Boost")
        status = " ".join(tags) if tags else "Active"
        name   = f"{c.emoji} C{slot+1} {c.team.title()[:6]}"
        rows.append(
            f"{name:<{col_w[0]}} {pos:<{col_w[1]}} {c.ep:>{col_w[2]}} "
            f"{carrying:<{col_w[3]}} {status}"
        )
    return "\n".join(rows)


# ═══════════════════════════ DISCORD UI VIEW ══════════════════════════════════


class DirectionButton(discord.ui.Button["BreakthroughView"]):
    def __init__(self, direction: str, emoji_str: str, row: int):
        super().__init__(emoji=emoji_str, style=discord.ButtonStyle.secondary, row=row)
        self.direction = direction

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert view is not None
        champ = view._get_champ(interaction.user.id)
        if champ is None:
            await interaction.response.send_message("❌ You're not in this match.", ephemeral=True)
            return
        if champ.submitted:
            await interaction.response.send_message("❌ Already locked in this round.", ephemeral=True)
            return
        if champ.frozen_rounds > 0:
            await interaction.response.send_message(
                f"❄️ Frozen for {champ.frozen_rounds} more round(s) — can't move.", ephemeral=True
            )
            return

        if len(champ.move_queue) == 0:
            champ.move_queue.append(self.direction)
            await interaction.response.send_message(
                f"✅ Move 1 queued: **{self.direction}**.", ephemeral=True
            )
        elif len(champ.move_queue) == 1 and champ.boost_requested:
            if champ.carrying_diamond:
                await interaction.response.send_message(
                    "❌ Can't boost while carrying the Diamond Briefcase.", ephemeral=True
                )
                return
            champ.move_queue.append(self.direction)
            await interaction.response.send_message(
                f"✅ Move 2 queued: **{self.direction}** (Boost active).", ephemeral=True
            )
        else:
            champ.move_queue = [self.direction]
            await interaction.response.send_message(
                f"✅ Move updated: **{self.direction}**.", ephemeral=True
            )


class BoostButton(discord.ui.Button["BreakthroughView"]):
    def __init__(self) -> None:
        super().__init__(label="⚡ Activate Boost", style=discord.ButtonStyle.blurple, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert view is not None
        champ = view._get_champ(interaction.user.id)
        if champ is None:
            await interaction.response.send_message("❌ Not in this match.", ephemeral=True)
            return
        if champ.submitted:
            await interaction.response.send_message("❌ Already locked in.", ephemeral=True)
            return
        if champ.frozen_rounds > 0:
            await interaction.response.send_message("❄️ Frozen — can't boost.", ephemeral=True)
            return
        if champ.carrying_diamond:
            await interaction.response.send_message(
                "❌ Can't boost while carrying the Diamond Briefcase.", ephemeral=True
            )
            return
        if champ.boost_requested:
            await interaction.response.send_message("⚡ Boost already activated this round.", ephemeral=True)
            return
        actual_cost = champ.boost_ep_cost
        if champ.ep < actual_cost:
            await interaction.response.send_message(
                f"❌ Need **{actual_cost} EP** for Boost — you have **{champ.ep} EP**.", ephemeral=True
            )
            return
        champ.boost_requested = True
        od_note = "  _(Overdrive discount active!)_" if champ.overdrive_rounds > 0 else ""
        await interaction.response.send_message(
            f"⚡ **Boost activated!** Press a second direction arrow.\n"
            f"Cost: **{actual_cost} EP**{od_note} (deducted at resolution).",
            ephemeral=True,
        )


class UnlockButton(discord.ui.Button["BreakthroughView"]):
    def __init__(self) -> None:
        super().__init__(label="🔓 Unlock Door", style=discord.ButtonStyle.grey, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert view is not None
        champ = view._get_champ(interaction.user.id)
        if champ is None:
            await interaction.response.send_message("❌ Not in this match.", ephemeral=True)
            return
        if champ.submitted:
            await interaction.response.send_message("❌ Already locked in.", ephemeral=True)
            return
        if champ.frozen_rounds > 0:
            await interaction.response.send_message("❄️ Frozen — can't act.", ephemeral=True)
            return
        champ.move_queue    = ["action"]
        champ.boost_requested = False
        await interaction.response.send_message(
            "🔓 **Unlock Door** queued — will attempt to open an adjacent locked door.",
            ephemeral=True,
        )


class LockInButton(discord.ui.Button["BreakthroughView"]):
    def __init__(self) -> None:
        super().__init__(label="🔒 Lock In Turn", style=discord.ButtonStyle.green, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert view is not None
        champ = view._get_champ(interaction.user.id)
        if champ is None:
            await interaction.response.send_message("❌ Not in this match.", ephemeral=True)
            return
        if champ.submitted:
            await interaction.response.send_message("✅ You've already locked in this round.", ephemeral=True)
            return

        champ.submitted = True
        state = view.cog._games.get(view.guild_id)

        move_desc   = " → ".join(champ.move_queue) if champ.move_queue else "hold"
        gadget_desc = champ.gadget if champ.gadget else "none"
        boost_desc  = f"yes (−{champ.boost_ep_cost} EP)" if champ.boost_requested else "no"

        await interaction.response.send_message(
            f"🔒 **Locked in!**\n"
            f"Move: **{move_desc}**\n"
            f"Gadget: **{gadget_desc}**\n"
            f"Boost: **{boost_desc}**",
            ephemeral=True,
        )

        if state and state.active and all(c.submitted for c in state.champions.values()):
            if state.timer_task and not state.timer_task.done():
                state.timer_task.cancel()
            view.stop()
            channel = view.cog.bot.get_channel(state.channel_id)
            if isinstance(channel, discord.TextChannel):
                await channel.send("⚡ **All champions locked in — resolving immediately!**")
                asyncio.create_task(view.cog._resolve_round(view.guild_id, channel))


class GadgetSelect(discord.ui.Select["BreakthroughView"]):
    def __init__(self) -> None:
        options = [
            discord.SelectOption(
                label="💨 Smoke Grenade  (30 EP) — Fog of War on your tile for 2 rounds",
                value="smoke",
            ),
            discord.SelectOption(
                label="⚡ Overdrive  (30 EP) — +15 EP regen · Boost costs 10 EP for 2 rounds",
                value="overdrive",
            ),
            discord.SelectOption(
                label="🔋 EMP Charge  (40 EP) — Stun adjacent opponent → pick target",
                value="emp",
            ),
            discord.SelectOption(
                label="👻 Decoy Hologram  (50 EP) — Clone appears on your previous position",
                value="decoy",
            ),
            discord.SelectOption(
                label="🎯 Direction Hijack  (50 EP) — Override opponent's move direction → pick target",
                value="hijack",
            ),
            discord.SelectOption(label="🚫 Cancel — Remove gadget selection", value="none"),
        ]
        super().__init__(placeholder="🎒 Select Gadget…", options=options, row=2)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert view is not None
        champ = view._get_champ(interaction.user.id)
        if champ is None:
            await interaction.response.send_message("❌ Not in this match.", ephemeral=True)
            return
        if champ.submitted:
            await interaction.response.send_message("❌ Already locked in.", ephemeral=True)
            return

        val = self.values[0]

        if val == "none":
            champ.gadget = None
            champ.gadget_target_slot = None
            await interaction.response.send_message("🚫 Gadget selection cleared.", ephemeral=True)
            return

        if val == "smoke":
            g_name, g_cost = VALID_GADGETS["smoke"]
            if champ.ep < g_cost:
                await interaction.response.send_message(
                    f"❌ Not enough EP — need **{g_cost}**, have **{champ.ep}**.", ephemeral=True
                )
                return
            champ.gadget             = "smoke"
            champ.gadget_target_slot = None
            await interaction.response.send_message(
                f"💨 **Smoke Grenade** queued (−{g_cost} EP at resolution).\n"
                f"Fog of War will cover your current position for 2 rounds.",
                ephemeral=True,
            )

        elif val == "overdrive":
            g_name, g_cost = VALID_GADGETS["overdrive"]
            if champ.ep < g_cost:
                await interaction.response.send_message(
                    f"❌ Not enough EP — need **{g_cost}**, have **{champ.ep}**.", ephemeral=True
                )
                return
            champ.gadget             = "overdrive"
            champ.gadget_target_slot = None
            await interaction.response.send_message(
                f"⚡ **Overdrive** queued (−{g_cost} EP at resolution).\n"
                f"Next 2 rounds: +{EP_REGEN_OVERDRIVE} EP/regen · Boost costs {BOOST_EP_COST_OVERDRIVE} EP.",
                ephemeral=True,
            )

        elif val == "decoy":
            g_name, g_cost = VALID_GADGETS["decoy"]
            if champ.ep < g_cost:
                await interaction.response.send_message(
                    f"❌ Not enough EP — need **{g_cost}**, have **{champ.ep}**.", ephemeral=True
                )
                return
            champ.gadget             = "decoy"
            champ.gadget_target_slot = None
            prev = f"[{champ.prev_row},{champ.prev_col}]" if champ.prev_row >= 0 else "your previous tile"
            await interaction.response.send_message(
                f"👻 **Decoy Hologram** queued (−{g_cost} EP at resolution).\n"
                f"Clone will appear on your previous position {prev}.",
                ephemeral=True,
            )

        elif val in ("emp", "hijack"):
            # Show dynamic target sub-menu with active opponents
            g_name, g_cost = VALID_GADGETS[val]
            if champ.ep < g_cost:
                await interaction.response.send_message(
                    f"❌ Not enough EP — need **{g_cost}**, have **{champ.ep}**.", ephemeral=True
                )
                return
            state = view.cog._games.get(view.guild_id)
            target_view = TargetSelectView(view.cog, view.guild_id, champ.slot, val)
            label = "⚡ EMP Charge" if val == "emp" else "🎯 Direction Hijack"
            await interaction.response.send_message(
                f"{label} — select your target champion:",
                view=target_view,
                ephemeral=True,
            )


class TargetSelect(discord.ui.Select["TargetSelectView"]):
    """Ephemeral dropdown used by EMP and Direction Hijack to pick an active opponent."""

    def __init__(
        self,
        cog: "BankBreakthroughCog",
        guild_id: int,
        requester_slot: int,
        gadget_key: str,
    ) -> None:
        self.cog            = cog
        self.guild_id       = guild_id
        self.requester_slot = requester_slot
        self.gadget_key     = gadget_key

        state   = cog._games.get(guild_id)
        options: list[discord.SelectOption] = []
        if state:
            for slot, c in state.champions.items():
                if slot != requester_slot:
                    options.append(
                        discord.SelectOption(
                            label=f"{c.emoji} C{slot + 1} — {c.team.title()}",
                            value=str(slot),
                        )
                    )

        if not options:
            options = [discord.SelectOption(label="No valid targets", value="none")]

        g_label = "⚡ EMP" if gadget_key == "emp" else "🎯 Hijack"
        super().__init__(placeholder=f"🎯 Target for {g_label}…", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        state = self.cog._games.get(self.guild_id)
        if not state:
            await interaction.response.send_message("❌ No active match.", ephemeral=True)
            return

        requester = state.champions.get(self.requester_slot)
        if requester is None or interaction.user.id != requester.user_id:
            await interaction.response.send_message(
                "❌ This targeting menu is not yours.", ephemeral=True
            )
            return

        val = self.values[0]
        if val == "none":
            await interaction.response.send_message(
                "❌ No valid targets available.", ephemeral=True
            )
            return

        target_slot = int(val)
        target      = state.champions.get(target_slot)
        if target is None:
            await interaction.response.send_message("❌ Target not found.", ephemeral=True)
            return

        g_name, g_cost = VALID_GADGETS[self.gadget_key]
        if requester.ep < g_cost:
            await interaction.response.send_message(
                f"❌ Not enough EP — need **{g_cost}**, have **{requester.ep}**.", ephemeral=True
            )
            return

        requester.gadget             = self.gadget_key
        requester.gadget_target_slot = target_slot

        if self.gadget_key == "emp":
            await interaction.response.send_message(
                f"⚡ **EMP Charge → {target.display_name}** queued "
                f"(−{g_cost} EP at resolution).\n"
                f"Will stun if adjacent at resolution.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"🎯 **Direction Hijack → {target.display_name}** queued "
                f"(−{g_cost} EP at resolution).\n"
                f"Their first move direction will be randomly overridden.",
                ephemeral=True,
            )

        if self.view is not None:
            self.view.stop()


class TargetSelectView(discord.ui.View):
    """Ephemeral view housing a TargetSelect dropdown."""

    def __init__(
        self,
        cog: "BankBreakthroughCog",
        guild_id: int,
        requester_slot: int,
        gadget_key: str,
    ) -> None:
        super().__init__(timeout=60.0)
        self.add_item(TargetSelect(cog, guild_id, requester_slot, gadget_key))


class BreakthroughView(discord.ui.View):
    def __init__(self, cog: "BankBreakthroughCog", guild_id: int) -> None:
        super().__init__(timeout=float(ROUND_TIMER))
        self.cog      = cog
        self.guild_id = guild_id

        self.add_item(DirectionButton("up",    "🔼", row=0))
        self.add_item(DirectionButton("down",  "🔽", row=0))
        self.add_item(DirectionButton("left",  "◀️", row=0))
        self.add_item(DirectionButton("right", "▶️", row=0))
        self.add_item(BoostButton())
        self.add_item(UnlockButton())
        self.add_item(LockInButton())
        self.add_item(GadgetSelect())

    def _get_champ(self, user_id: int) -> Optional[Champion]:
        state = self.cog._games.get(self.guild_id)
        if not state:
            return None
        return next((c for c in state.champions.values() if c.user_id == user_id), None)

    async def on_timeout(self) -> None:
        state = self.cog._games.get(self.guild_id)
        if state and state.active and not state.resolving:
            channel = self.cog.bot.get_channel(state.channel_id)
            if isinstance(channel, discord.TextChannel):
                asyncio.create_task(self.cog._resolve_round(self.guild_id, channel))


# ═══════════════════════════════ COG ══════════════════════════════════════════


class BankBreakthroughCog(commands.Cog, name="BankBreakthrough"):
    """Breakthrough — 15×15 tactical heist game for Discord."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot    = bot
        self._games: dict[int, GameState] = {}

    async def cog_load(self) -> None:
        _db_ensure_tables()

    # ═══════════════════════════ ADMIN COMMANDS ════════════════════════════════

    @app_commands.command(name="team-add", description="[Admin] Assign a member to a team.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(team_name="Team name.", member="Member to assign.")
    async def team_add(self, interaction: discord.Interaction, team_name: str, member: discord.Member) -> None:
        assert interaction.guild_id is not None
        _db_team_add(interaction.guild_id, member.id, team_name.strip())
        em = discord.Embed(title="✅  Team Updated", colour=C_TEAM,
                           description=f"{member.mention} → **{team_name.strip().title()}**")
        em.set_footer(text="Breakthrough · Team System")
        await interaction.response.send_message(embed=em, ephemeral=True)

    @app_commands.command(name="team-remove", description="[Admin] Remove a member from their team.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(member="Member to remove.")
    async def team_remove(self, interaction: discord.Interaction, member: discord.Member) -> None:
        assert interaction.guild_id is not None
        removed = _db_team_remove(interaction.guild_id, member.id)
        em = discord.Embed(
            title="✅  Team Updated" if removed else "ℹ️  Not in a Team",
            colour=C_TEAM if removed else C_ERROR,
            description=(f"{member.mention} removed from their team." if removed
                         else f"{member.mention} wasn't in any team."),
        )
        em.set_footer(text="Breakthrough · Team System")
        await interaction.response.send_message(embed=em, ephemeral=True)

    @app_commands.command(name="team-list", description="[Admin] List members of a team.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(team_name="Team to inspect.")
    async def team_list(self, interaction: discord.Interaction, team_name: str) -> None:
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
        champion1="Champion for slot 1 — C1 🟦 spawns [0,0]",
        champion2="Champion for slot 2 — C2 🟨 spawns [0,14]",
        champion3="Champion for slot 3 — C3 🟩 spawns [14,0]",
        champion4="Champion for slot 4 — C4 🟥 spawns [14,14]",
    )
    async def breakthrough_setup(
        self,
        interaction: discord.Interaction,
        champion1: discord.Member,
        champion2: discord.Member,
        champion3: discord.Member,
        champion4: discord.Member,
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer()

        guild_id = interaction.guild_id
        if guild_id in self._games and self._games[guild_id].active:
            await interaction.followup.send("❌  A match is already active. Use `/breakthrough-end` first.", ephemeral=True)
            return

        members = [champion1, champion2, champion3, champion4]
        teams: list[str] = []
        for m in members:
            t = _db_get_user_team(guild_id, m.id)
            if not t:
                await interaction.followup.send(
                    f"❌  {m.mention} isn't assigned to a team. Use `/team-add` first.", ephemeral=True
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
        state = GameState(channel_id=interaction.channel.id, guild_id=guild_id, champions=champions)
        _generate_map(state)
        self._games[guild_id] = state

        roster = "\n".join(
            f"{EMOJI_CHAMP[i]} **C{i+1}** — {members[i].mention} ({teams[i].title()})"
            for i in range(4)
        )
        em = discord.Embed(
            title="🏦  Breakthrough v3 — Match Starting!",
            colour=C_VAULT,
            description=(
                f"{SEP}\n**Champions have entered the building.**\n"
                "Crack the Central Vault (3 Breach Points) to win!\n"
                f"{SEP}\n\n{roster}"
            ),
        )
        em.add_field(name="🗺️ Grid",      value=f"15×15 — {NUM_RANDOM_WALLS} random walls · A* validated", inline=True)
        em.add_field(name="⚡ Energy",    value="100 EP · +10/round regen", inline=True)
        em.add_field(name="⚡ Boost",     value=f"{BOOST_EP_COST} EP → second direction press this round", inline=False)
        em.add_field(name="🎮 Gadgets",  value="💨 Smoke (30 EP) · ⚡ EMP (40 EP) · 👻 Decoy (50 EP)", inline=False)
        em.add_field(name="🎮 Controls", value="Use the **buttons on the board message** each round.", inline=False)
        em.set_footer(text="Breakthrough · Champion Edition v3")
        await interaction.followup.send(embed=em)
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
        if state.active_view:
            state.active_view.stop()
        state.active = False
        await interaction.response.send_message("🛑  Match force-ended by admin.")

    # ═══════════════════════════ PLAYER COMMANDS ═══════════════════════════════

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

    @app_commands.command(name="gadgethelp", description="Show a private reference card for all Breakthrough gadgets.")
    @app_commands.guild_only()
    async def gadget_help(self, interaction: discord.Interaction) -> None:
        em = discord.Embed(
            title="🎒  Breakthrough — Gadget Reference",
            colour=C_TEAM,
            description=(
                "All gadgets are selected from the **🎒 Select Gadget…** dropdown during the planning phase.\n"
                "EP is deducted at resolution — not when you queue the gadget.\n"
            ),
        )

        em.add_field(
            name="💨  Smoke Grenade  —  30 EP",
            value=(
                "**Self-cast.** Blankets your current tile in Fog of War for **2 rounds**. "
                "Your `[row, col]` shows as `[?,?]` in the status table and you are hidden "
                "from opponents on the rendered board."
            ),
            inline=False,
        )

        em.add_field(
            name="⚡  Overdrive  —  30 EP",
            value=(
                "**Self-cast.** Activates for **2 rounds** starting from the end of the current round.\n"
                f"• EP regen: **+{EP_REGEN_OVERDRIVE}/round** (normally +{EP_REGEN})\n"
                f"• Boost cost: **{BOOST_EP_COST_OVERDRIVE} EP** (normally {BOOST_EP_COST})\n"
                "Status shows as `⚡ OD(Xr)` in the champion table."
            ),
            inline=False,
        )

        em.add_field(
            name="🔋  EMP Charge  —  40 EP",
            value=(
                "**Targets an opponent** (picked via pop-up dropdown after selecting EMP).\n"
                "At resolution, if the target is **orthogonally adjacent**, they are "
                "**stunned for 1 round** — their move is cancelled and they can't act next turn.\n"
                "If the target is *not* adjacent the EP cost is **refunded** in full."
            ),
            inline=False,
        )

        em.add_field(
            name="👻  Decoy Hologram  —  50 EP",
            value=(
                "**Self-cast.** Places a holographic clone on the tile you occupied at the "
                "**start of this round** (your previous position). Fails if you haven't moved "
                "yet (first round) or the previous tile is a wall / vault cell.\n"
                "The decoy appears as your champion emoji on the board, potentially drawing "
                "opponent attention."
            ),
            inline=False,
        )

        em.add_field(
            name="🎯  Direction Hijack  —  50 EP",
            value=(
                "**Targets an opponent** (picked via pop-up dropdown after selecting Hijack).\n"
                "At resolution, the target's **first queued move direction is replaced** with "
                "a random orthogonal direction (not their intended one). "
                "Boosted second moves are unaffected — only the first step is hijacked."
            ),
            inline=False,
        )

        em.add_field(
            name="📌  General Rules",
            value=(
                "• You can only queue **one gadget per round** — selecting a new one replaces the previous.\n"
                "• Select **🚫 Cancel** from the dropdown to clear your queued gadget.\n"
                "• EP is checked again at resolution — if you've spent EP elsewhere and no longer "
                "have enough, the gadget is cancelled (no cost).\n"
                f"• Max EP cap: **{MAX_EP}**."
            ),
            inline=False,
        )

        em.set_footer(text="Breakthrough · Gadget Reference Card · All costs deducted at resolution")
        await interaction.response.send_message(embed=em, ephemeral=True)

    @app_commands.command(name="breakthrough-leaderboard", description="Show the all-time Heist Coin leaderboard.")
    @app_commands.guild_only()
    async def breakthrough_leaderboard(self, interaction: discord.Interaction) -> None:
        rows = _db_get_all_coins()
        em   = discord.Embed(title="🪙  Breakthrough — All-Time Leaderboard", colour=C_LB)
        if not rows:
            em.description = "_No coins earned yet. Start a match with `/breakthrough-setup`!_"
        else:
            medals = ["🥇", "🥈", "🥉"]
            lines  = []
            for i, (team, coins) in enumerate(rows):
                medal = medals[i] if i < 3 else f"`#{i+1}`"
                lines.append(f"{medal}  **{team.title()}** — **{coins:,}** 🪙")
            em.description = "\n".join(lines)
        em.set_footer(text="Breakthrough · Heist Coins from loot & vault breaches")
        await interaction.response.send_message(embed=em)

    # ═══════════════════════════ ROUND ENGINE ══════════════════════════════════

    async def _post_round(self, guild_id: int) -> None:
        state = self._games.get(guild_id)
        if not state:
            return

        state.round_number += 1
        state.resolving     = False

        # Spawn diamond in round 3
        if state.round_number == DIAMOND_SPAWN_ROUND and not state.diamond_spawned:
            pos = _spawn_diamond(state)
            state.diamond_spawned = True
            channel = self.bot.get_channel(state.channel_id)
            if isinstance(channel, discord.TextChannel) and pos:
                await channel.send(
                    f"💎  **Round {state.round_number}!** The Diamond Briefcase appeared at "
                    f"`[{pos[0]},{pos[1]}]` — +500 🪙 but blocks the Boost ability!"
                )

        channel = self.bot.get_channel(state.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        # Reset round inputs; frozen champions are auto-submitted on hold
        for champ in state.champions.values():
            champ.reset_round()
            if champ.frozen_rounds > 0:
                champ.submitted = True

        # Skip countdown if all already auto-submitted
        if all(c.submitted for c in state.champions.values()):
            await self._resolve_round(guild_id, channel)
            return

        img_buf = _render_grid_image(state)
        em      = self._round_embed(state, has_image=img_buf is not None)

        view = BreakthroughView(self, guild_id)
        state.active_view = view

        if img_buf:
            img_buf.seek(0)
            msg = await channel.send(
                file=discord.File(img_buf, filename="board.png"), embed=em, view=view
            )
        else:
            em.description = f"```\n{_render_grid_text(state)}\n```"
            msg = await channel.send(embed=em, view=view)

        state.board_message = msg

        if state.timer_task:
            state.timer_task.cancel()
        state.timer_task = asyncio.create_task(self._round_countdown(guild_id, channel))

    async def _round_countdown(self, guild_id: int, channel: discord.TextChannel) -> None:
        await asyncio.sleep(ROUND_TIMER)
        state = self._games.get(guild_id)
        if state and state.active and not state.resolving:
            await self._resolve_round(guild_id, channel)

    async def _resolve_round(self, guild_id: int, channel: discord.TextChannel) -> None:
        state = self._games.get(guild_id)
        if not state or state.resolving:
            return
        state.resolving = True

        if state.active_view:
            state.active_view.stop()

        resolution_log: list[str] = []

        # Default missing moves
        for champ in state.champions.values():
            if not champ.submitted and champ.frozen_rounds == 0:
                champ.move_queue = []
                resolution_log.append(f"⏰ **{champ.display_name}** timed out — defaulting to **hold**.")

        # ════════════════════════════════════════════════════════════════════
        # PHASE 0 — BOOST EP DEDUCTION
        # ════════════════════════════════════════════════════════════════════
        for champ in state.champions.values():
            if champ.boost_requested:
                actual_cost = champ.boost_ep_cost
                if champ.ep >= actual_cost:
                    champ.ep -= actual_cost
                    od_note = " _(Overdrive discount)_" if champ.overdrive_rounds > 0 else ""
                    resolution_log.append(
                        f"⚡ **{champ.display_name}** used Boost "
                        f"(−{actual_cost} EP → {champ.ep} EP remaining){od_note}."
                    )
                else:
                    if len(champ.move_queue) > 1:
                        champ.move_queue = champ.move_queue[:1]
                    resolution_log.append(
                        f"⚡ **{champ.display_name}** lacked EP for Boost — second move cancelled."
                    )

        # ════════════════════════════════════════════════════════════════════
        # PHASE 1 — GADGETS
        # ════════════════════════════════════════════════════════════════════

        # Decrement frozen counters first
        for champ in state.champions.values():
            if champ.frozen_rounds > 0:
                champ.frozen_rounds -= 1
                if champ.frozen_rounds == 0:
                    resolution_log.append(f"🌡️ **{champ.display_name}** thawed — free next round.")

        for champ in state.champions.values():
            if not champ.gadget:
                continue
            key      = champ.gadget
            g_name, g_cost = VALID_GADGETS[key]

            if champ.ep < g_cost:
                resolution_log.append(
                    f"⚡ **{champ.display_name}** tried **{g_name}** but has insufficient EP ({champ.ep}/{g_cost})."
                )
                continue

            champ.ep -= g_cost

            if key == "smoke":
                champ.smoked_rounds = 2
                resolution_log.append(
                    f"💨 **{champ.display_name}** deployed **Smoke Grenade** — "
                    f"position hidden for 2 rounds. Fog of War active. (−{g_cost} EP)"
                )

            elif key == "overdrive":
                champ.overdrive_rounds = 2
                resolution_log.append(
                    f"⚡ **{champ.display_name}** activated **Overdrive**! "
                    f"+{EP_REGEN_OVERDRIVE} EP/round regen · Boost costs {BOOST_EP_COST_OVERDRIVE} EP "
                    f"for the next 2 rounds. (−{g_cost} EP)"
                )

            elif key == "emp":
                ts     = champ.gadget_target_slot
                target = state.champions.get(ts) if ts is not None else None
                if target and _is_adjacent(champ.row, champ.col, target.row, target.col):
                    target.frozen_rounds = max(target.frozen_rounds, 1)
                    target.move_queue    = []
                    target.submitted     = True
                    resolution_log.append(
                        f"⚡ **{champ.display_name}** hit **{target.display_name}** with EMP! "
                        f"Stunned 1 round. (−{g_cost} EP)"
                    )
                else:
                    t_name = state.champions[ts].display_name if ts in state.champions else "?"
                    resolution_log.append(
                        f"⚡ **{champ.display_name}**'s EMP missed — **{t_name}** not adjacent. (−{g_cost} EP refunded)"
                    )
                    champ.ep += g_cost  # refund — missed EMP costs nothing

            elif key == "decoy":
                # Deploy on previous position (where the champion was at the start of this round)
                pr, pc = champ.prev_row, champ.prev_col
                if (
                    pr >= 0
                    and 0 <= pr < GRID_SIZE and 0 <= pc < GRID_SIZE
                    and (pr, pc) not in state.walls
                    and (pr, pc) not in VAULT_TILES
                    and (pr, pc) != (champ.row, champ.col)
                ):
                    champ.has_decoy = True
                    champ.decoy_row = pr
                    champ.decoy_col = pc
                    resolution_log.append(
                        f"👻 **{champ.display_name}** left a **Decoy Hologram** on their "
                        f"previous position `[{pr},{pc}]`. (−{g_cost} EP)"
                    )
                else:
                    resolution_log.append(
                        f"👻 **{champ.display_name}**'s Decoy failed — "
                        f"previous position unavailable or same tile. (−{g_cost} EP)"
                    )

            elif key == "hijack":
                ts     = champ.gadget_target_slot
                target = state.champions.get(ts) if ts is not None else None
                if target:
                    target.hijacked_by_slot = champ.slot
                    resolution_log.append(
                        f"🎯 **{champ.display_name}** placed a **Direction Hijack** on "
                        f"**{target.display_name}**! Their first move will be overridden. (−{g_cost} EP)"
                    )
                else:
                    resolution_log.append(
                        f"🎯 **{champ.display_name}**'s Direction Hijack has no valid target."
                    )

        # ════════════════════════════════════════════════════════════════════
        # PHASE 2 — MOVEMENT
        # ════════════════════════════════════════════════════════════════════

        # Door unlock actions first
        for champ in state.champions.values():
            if champ.move_queue and champ.move_queue[0] == "action":
                unlocked_any = False
                for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    adj = (champ.row + dr, champ.col + dc)
                    if adj in state.doors and adj not in state.opened_doors:
                        state.opened_doors.add(adj)
                        resolution_log.append(
                            f"🔓 **{champ.display_name}** unlocked the security door at `{adj}`!"
                        )
                        unlocked_any = True
                if not unlocked_any:
                    resolution_log.append(
                        f"🔑 **{champ.display_name}** used Unlock — no adjacent locked door."
                    )

        # Snapshot positions for collision rollback
        old_pos: dict[int, tuple[int, int]] = {
            s: (c.row, c.col) for s, c in state.champions.items()
        }

        # Apply Direction Hijack overrides — redirect the hijacked champion's first move
        _all_dirs = ["up", "down", "left", "right"]
        for champ in state.champions.values():
            if champ.hijacked_by_slot is not None and champ.move_queue:
                first = champ.move_queue[0]
                if first not in ("action", "hold", ""):
                    alt_dirs = [d for d in _all_dirs if d != first]
                    forced   = random.choice(alt_dirs)
                    champ.move_queue[0] = forced
                    hijacker = state.champions.get(champ.hijacked_by_slot)
                    h_name   = hijacker.display_name if hijacker else "?"
                    resolution_log.append(
                        f"🎯 **{champ.display_name}**'s first move was hijacked by **{h_name}**! "
                        f"**{first}** → **{forced}**."
                    )

        # Walk each step in the move queue
        for slot, champ in state.champions.items():
            if champ.frozen_rounds > 0:
                continue
            if not champ.move_queue or champ.move_queue[0] == "action":
                continue
            for direction in champ.move_queue:
                moved = _walk_one_step(champ, direction, state, resolution_log)
                if moved:
                    resolution_log.append(
                        f"🚶 **{champ.display_name}** moved **{direction}** → `[{champ.row},{champ.col}]`."
                    )

        # Collision detection — bounce back on same tile
        dest_map: dict[tuple[int, int], list[int]] = {}
        for slot, champ in state.champions.items():
            dest_map.setdefault((champ.row, champ.col), []).append(slot)
        for pos, slots in dest_map.items():
            if len(slots) > 1:
                for s in slots:
                    champ = state.champions[s]
                    or_, oc_ = old_pos[s]
                    champ.row, champ.col = or_, oc_
                    resolution_log.append(
                        f"💥 **{champ.display_name}** collided at `{pos}` — bounced to `[{or_},{oc_}]`!"
                    )

        # ════════════════════════════════════════════════════════════════════
        # LOOT COLLECTION
        # ════════════════════════════════════════════════════════════════════
        game_over   = False
        winner_team: Optional[str] = None

        for champ in state.champions.values():
            pos       = (champ.row, champ.col)
            loot_kind = state.loot_tiles.get(pos)

            if loot_kind == "common":
                del state.loot_tiles[pos]
                new_bal = _db_add_coins(champ.team, LOOT_REWARD_COMMON)
                champ.loot_claimed += 1
                resolution_log.append(
                    f"💵 **{champ.display_name}** grabbed cash! +{LOOT_REWARD_COMMON:,} 🪙 "
                    f"(team total: {new_bal:,})"
                )
            elif loot_kind == "diamond":
                del state.loot_tiles[pos]
                champ.carrying_diamond = True
                champ.loot_claimed    += 1
                new_bal = _db_add_coins(champ.team, LOOT_REWARD_DIAMOND)
                resolution_log.append(
                    f"💎 **{champ.display_name}** seized the Diamond Briefcase! "
                    f"+{LOOT_REWARD_DIAMOND:,} 🪙 — Boost locked while carrying. "
                    f"(team total: {new_bal:,})"
                )

            if pos in VAULT_TILES:
                state.breach_points         += 1
                champ.breach_points_contrib += 1
                new_bal = _db_add_coins(champ.team, BREACH_COIN_REWARD)
                resolution_log.append(
                    f"🏦 **{champ.display_name}** breached the vault! "
                    f"+1 BP (+{BREACH_COIN_REWARD:,} 🪙) — "
                    f"Total: **{state.breach_points}/{VAULT_POINTS_REQ}**"
                )
                if state.breach_points >= VAULT_POINTS_REQ:
                    game_over   = True
                    winner_team = champ.team
                    break

        # ════════════════════════════════════════════════════════════════════
        # PHASE 3 — TRAPS
        # ════════════════════════════════════════════════════════════════════
        if not game_over:
            for champ in state.champions.values():
                pos = (champ.row, champ.col)
                if pos in state.traps:
                    state.traps.discard(pos)
                    state.triggered_traps.add(pos)
                    champ.frozen_rounds += 1
                    resolution_log.append(
                        f"⚠️ **{champ.display_name}** triggered an Alarm Trap at `{pos}`! "
                        "Stunned for **1 round**."
                    )

        # End-of-round EP regen + status ticks
        for champ in state.champions.values():
            if champ.smoked_rounds > 0:
                champ.smoked_rounds -= 1
            # Overdrive: boosted regen for 2 rounds, then expire
            regen = EP_REGEN_OVERDRIVE if champ.overdrive_rounds > 0 else EP_REGEN
            champ.ep = min(MAX_EP, champ.ep + regen)
            if champ.overdrive_rounds > 0:
                champ.overdrive_rounds -= 1

        # ── Game over ──────────────────────────────────────────────────────
        if game_over and winner_team:
            state.active = False
            self._games.pop(guild_id, None)
            await self._post_victory(channel, state, winner_team, resolution_log)
            return

        # ── Action log ────────────────────────────────────────────────────
        if resolution_log:
            log_em = discord.Embed(
                title=f"📋  Round {state.round_number} — Resolution Log",
                colour=C_ACTION,
                description="\n".join(resolution_log),
            )
            log_em.set_footer(
                text=(
                    f"Vault: {state.breach_points}/{VAULT_POINTS_REQ} BP  ·  "
                    f"Loot: {sum(1 for v in state.loot_tiles.values() if v == 'common')} common"
                    f"{'  ·  1 💎 diamond' if 'diamond' in state.loot_tiles.values() else ''}"
                )
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

        em.add_field(
            name="📊 Champion Status",
            value=f"```\n{_status_table(state)}\n```",
            inline=False,
        )
        em.add_field(
            name="🏦 Vault",
            value=f"**{state.breach_points}/{VAULT_POINTS_REQ}** Breach Points",
            inline=True,
        )
        em.add_field(
            name="⏱️ Planning",
            value=f"**{ROUND_TIMER}s** — Use buttons below",
            inline=True,
        )
        common_left  = sum(1 for v in state.loot_tiles.values() if v == "common")
        diamond_left = "1 💎" if "diamond" in state.loot_tiles.values() else "—"
        em.add_field(
            name="💰 Loot",
            value=f"**{common_left}** common · {diamond_left} diamond",
            inline=True,
        )
        em.set_footer(
            text=f"Breakthrough v3 · 15×15 · Doors: {len(state.opened_doors)}/{len(FIXED_DOORS)} opened"
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
                title=f"📋  Round {state.round_number} — Final Resolution",
                colour=C_VAULT,
                description="\n".join(log),
            )
            await channel.send(embed=log_em)

        ranking = sorted(
            state.champions.values(),
            key=lambda c: (-c.breach_points_contrib, -c.loot_claimed, c.tiles_traveled),
        )
        medals    = ["🥇", "🥈", "🥉", "4️⃣"]
        rank_lines = [
            f"{medals[i]} **{c.team.title()}** — "
            f"{c.breach_points_contrib} BP · {c.loot_claimed} loot · {c.tiles_traveled} tiles"
            for i, c in enumerate(ranking)
        ]

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
                name="🏆 Winning Team",
                value=f"**{winner_team.title()}** — <@{winner_champ.user_id}> landed the final breach!",
                inline=False,
            )
        em.add_field(name="📊 Match Ranking", value="\n".join(rank_lines), inline=False)
        em.add_field(name="🏦 Vault", value=f"**{state.breach_points}/{VAULT_POINTS_REQ}** — Cracked", inline=True)
        em.set_footer(text="Breakthrough · Champion Edition v3 · Match Over")

        if img_buf:
            img_buf.seek(0)
            await channel.send(file=discord.File(img_buf, filename="board.png"), embed=em)
        else:
            await channel.send(embed=em)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BankBreakthroughCog(bot))
