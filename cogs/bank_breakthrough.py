"""
cogs/bank_breakthrough.py
─────────────────────────────────────────────────────────────────────────────
Breakthrough — Champion Edition  (v4 · 15×15 grid · UI Buttons)
─────────────────────────────────────────────────────────────────────────────

Admin commands:
  /team-add            [team_name] [@member]      — assign member to a team
  /team-remove         [@member]                  — remove member from team
  /team-list           [team_name]                — list team members
  /breakthrough-setup  [u1] [u2] [u3] [u4]        — start a match
  /breakthrough-end                               — force-end active match

Player interaction — Discord UI Buttons on the board message:
  [🔼] [🔽] [◀️] [▶️]   — Queue 1 tile of movement for this round
  [🔓 Unlock Door]      — Queue a door-unlock action instead of movement
  [🔒 Lock In Turn]     — Submit your choices; round resolves when all lock in
  [🎒 Select Gadget ▾]  — Dropdown: Boost / Freeze / Shuffle

  /breakthrough-status        — private board view
  /breakthrough-leaderboard   — all-time Heist Coin rankings

Gadgets (Energy Pool: 100 EP max · +10 EP/round regen):
  boost   (20 EP) — unlocks a second directional press this round (blocked if carrying Diamond)
  freeze  (40 EP) — stuns an adjacent opponent for 1 full round; EP refunded on miss
  shuffle (60 EP) — teleport to a random safe empty tile; 1 use per player per match

Map (15×15):
  • Spawns at corners: [0,0] [0,14] [14,0] [14,14]
  • Central Vault: 2×2 block [7,7]–[8,8] · needs 3 Breach Points
  • Security Walls [🧱]: 24-wall permanent skeleton in corridors between spawn blocks + BFS extras
  • Security Doors: guard all 4 vault approaches
  • Cash Bundle  (💵): +100 🪙
  • Gold Bar     (💰): +200 🪙
  • Mystery Box  (📦): revealed on pickup — 60% chance +50…+400 🪙 · 40% chance −50…−200 🪙
  • Diamond Briefcase (💎): spawns round 3 · +500 🪙 · blocks Boost while carried
  • Alarm Traps (⚠️): hidden until stepped on; stun 1 round

Round Flow:
  1. Planning Phase — 30 s max countdown
     TIME-SKIP: immediately resolves the moment all players lock in
  2. Resolution — Boost EP → Gadgets → Movements → Loot → Traps
  3. Ranking — Breach Points + Loot; tiebreaker = fewest tiles traveled
"""

from __future__ import annotations

import asyncio
import random
import os
from collections import deque
from io import BytesIO
from dataclasses import dataclass, field
from typing import Optional

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
MAX_EP              = 100
EP_REGEN            = 10
BOOST_EP_COST       = 20
DIAMOND_SPAWN_ROUND = 3

LOOT_REWARD_COMMON  = 100
LOOT_REWARD_GOLD    = 200
LOOT_REWARD_DIAMOND = 500
BREACH_COIN_REWARD  = 300

MYSTERY_GAIN_MIN   = 50
MYSTERY_GAIN_MAX   = 400
MYSTERY_LOSS_MIN   = 50
MYSTERY_LOSS_MAX   = 200
MYSTERY_GAIN_CHANCE = 0.60   # 60 % chance of gain; 40 % chance of loss

NUM_EXTRA_RANDOM_WALLS = 8   # random extras layered on top of structural skeleton each match
NUM_LOOT_COMMON        = 5
NUM_LOOT_GOLD          = 3
NUM_LOOT_MYSTERY       = 2
NUM_TRAPS              = 5

CHAMPION_STARTS = [(0, 0), (0, 14), (14, 0), (14, 14)]

FIXED_DOORS: frozenset[tuple[int, int]] = frozenset({
    (6, 7), (6, 8),
    (9, 7), (9, 8),
    (7, 6), (8, 6),
    (7, 9), (8, 9),
})

# ── Permanent structural wall skeleton ────────────────────────────────────────
# Walls sit BETWEEN the four corner spawn blocks and the centre vault zone —
# never inside a spawn quadrant.  Layout is fully 4-way symmetric so every
# player faces an identical situation.  Each corridor has one guaranteed open
# "needle" column/row that runs straight through to the vault:
#   • Top / bottom corridors  → col 7 is always clear
#   • Left / right corridors  → row 7 is always clear
#
# Corridor zones (where walls are placed):
#   Top   : rows  1–4 , cols 5–9   (between TL/TR spawn blocks)
#   Bottom: rows 10–13, cols 5–9   (between BL/BR spawn blocks)
#   Left  : rows  5–9 , cols 1–4   (between TL/BL spawn blocks)
#   Right : rows  5–9 , cols 10–13 (between TR/BR spawn blocks)
STRUCTURAL_WALLS: frozenset[tuple[int, int]] = frozenset({
    # ═══ TOP CENTER CORRIDOR ════════════════════════════════════════════
    # Gate-post pair at row 1 (col 7 gap = needle)
    (1, 6), (1, 8),
    # Offset posts at row 3 force an S-bend around the needle
    (3, 5), (3, 9),

    # ═══ BOTTOM CENTER CORRIDOR (4-way mirror of top) ════════════════════
    (13, 6), (13, 8),
    (11, 5), (11, 9),

    # ═══ LEFT CENTER CORRIDOR ═══════════════════════════════════════════
    # Gate-post pair at col 1 (row 7 gap = needle)
    (6, 1), (8, 1),
    # Offset posts at col 3 force an S-bend around the needle
    (5, 3), (9, 3),

    # ═══ RIGHT CENTER CORRIDOR (4-way mirror of left) ════════════════════
    (6, 13), (8, 13),
    (5, 11), (9, 11),

    # ═══ INNER CROSS-CORRIDOR SCREENS (4-way symmetric ring) ════════════
    # Transition zone between the outer corridors and the vault approach lanes
    (4, 6),  (4, 8),
    (10, 6), (10, 8),
    (6, 4),  (8, 4),
    (6, 10), (8, 10),
})

# Registered gadgets — ONLY these 3 are allowed in the system
VALID_GADGETS: dict[str, tuple[str, int]] = {
    "boost":   ("Movement Boost",  BOOST_EP_COST),
    "freeze":  ("Freeze Charge",   40),
    "shuffle": ("Board Shuffle",   60),
}

_DIR_DELTA: dict[str, tuple[int, int]] = {
    "up":     (-1,  0),
    "down":   ( 1,  0),
    "left":   ( 0, -1),
    "right":  ( 0,  1),
    "hold":   ( 0,  0),
    "action": ( 0,  0),
}

# ─────────────────────────── Display ──────────────────────────────────────────

EMOJI_CHAMP = ["🟦", "🟨", "🟩", "🟥"]

C_ROUND  = 0x4F46E5
C_ACTION = 0xF59E0B
C_VAULT  = 0xFFD700
C_WIN    = 0x22C55E
C_TEAM   = 0x818CF8
C_ERROR  = 0xEF4444
C_LB     = 0xA78BFA

SEP = "─" * 40

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

    frozen_rounds: int  = 0
    shuffle_used:  bool = False   # Board Shuffle: one use per player per match

    # Per-round planning inputs (reset each round)
    move_queue:         list          = field(default_factory=list)
    boost_requested:    bool          = False
    gadget:             Optional[str] = None
    gadget_target_slot: Optional[int] = None   # Freeze Charge target slot
    submitted:          bool          = False

    @property
    def display_name(self) -> str:
        return f"C{self.slot + 1} ({self.team.title()})"

    @property
    def emoji(self) -> str:
        return EMOJI_CHAMP[self.slot]

    def reset_round(self) -> None:
        self.move_queue         = []
        self.boost_requested    = False
        self.gadget             = None
        self.gadget_target_slot = None
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


def _generate_map(state: GameState) -> None:
    """
    Build the map:
      1. Start with the permanent STRUCTURAL_WALLS skeleton (never removed).
      2. Add up to NUM_EXTRA_RANDOM_WALLS additional BFS-validated random walls
         for per-match variety.
      3. Place loot tiles and traps in remaining open space.
    """
    walls: set[tuple[int, int]] = set(STRUCTURAL_WALLS)

    # Extra random walls layered on top of the skeleton
    forbidden = set(CHAMPION_STARTS) | VAULT_TILES | FIXED_DOORS | STRUCTURAL_WALLS
    candidates = [
        (r, c) for r in range(GRID_SIZE) for c in range(GRID_SIZE)
        if (r, c) not in forbidden
    ]
    random.shuffle(candidates)
    extras_added = 0
    for pos in candidates:
        if extras_added >= NUM_EXTRA_RANDOM_WALLS:
            break
        test = walls | {pos}
        if _all_corners_reach_vault(test, FIXED_DOORS, CHAMPION_STARTS, VAULT_TILES, GRID_SIZE):
            walls.add(pos)
            extras_added += 1

    state.walls = frozenset(walls)

    # Place loot tiles and traps in open space
    taken: set[tuple[int, int]] = (
        set(CHAMPION_STARTS) | VAULT_TILES | state.walls | state.doors
    )
    open_tiles = [
        (r, c) for r in range(GRID_SIZE) for c in range(GRID_SIZE) if (r, c) not in taken
    ]
    random.shuffle(open_tiles)

    total_loot = NUM_LOOT_COMMON + NUM_LOOT_GOLD + NUM_LOOT_MYSTERY
    loot_pool  = open_tiles[:total_loot]
    kinds      = (
        ["common"]  * NUM_LOOT_COMMON  +
        ["gold"]    * NUM_LOOT_GOLD    +
        ["mystery"] * NUM_LOOT_MYSTERY
    )
    random.shuffle(kinds)
    for pos, kind in zip(loot_pool, kinds):
        state.loot_tiles[pos] = kind

    state.traps = set(open_tiles[total_loot : total_loot + NUM_TRAPS])


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
    """True for all 8 surrounding tiles (orthogonal + diagonal)."""
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


def _random_safe_tile(state: GameState, exclude_slot: int) -> Optional[tuple[int, int]]:
    """Pick a random open floor tile for Board Shuffle — avoids walls, vault, doors, and occupied tiles."""
    occupied = {
        (c.row, c.col) for s, c in state.champions.items() if s != exclude_slot
    }
    forbidden = VAULT_TILES | state.walls | state.doors | occupied | state.triggered_traps
    pool = [
        (r, c) for r in range(GRID_SIZE) for c in range(GRID_SIZE)
        if (r, c) not in forbidden
    ]
    return random.choice(pool) if pool else None


# ═══════════════════════════ GRID RENDERER (PNG) ══════════════════════════════


def _render_grid_image(state: GameState) -> Optional[BytesIO]:
    """Render a styled 15×15 board PNG."""
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
    GOLD_BG     = (55,  38,  0)
    GOLD_FG     = (240, 175, 0)
    MYST_BG     = (38,  8,   55)
    MYST_FG     = (185, 70,  255)
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

    draw.rectangle([bx0 - 3, by0 - 3, bx1 + 3, by1 + 3], fill=BOARD_BG)

    # Neon border glow
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

    # Corner accents
    acc_len = 12
    for (ax, ay), (dx1, dy1), (dx2, dy2) in [
        ((bx0 - 5, by0 - 5), (acc_len, 0), (0, acc_len)),
        ((bx1 + 5, by0 - 5), (-acc_len, 0), (0, acc_len)),
        ((bx0 - 5, by1 + 5), (acc_len, 0), (0, -acc_len)),
        ((bx1 + 5, by1 + 5), (-acc_len, 0), (0, -acc_len)),
    ]:
        draw.line([(ax, ay), (ax + dx1, ay + dy1)], fill=(0, 240, 255), width=2)
        draw.line([(ax, ay), (ax + dx2, ay + dy2)], fill=(0, 240, 255), width=2)

    # Coordinate labels
    for i in range(GRID_SIZE):
        cx = bx0 + i * CELL + CELL // 2
        cy = by0 + i * CELL + CELL // 2
        draw.text((cx, by0 - 13), str(i), fill=TEXT_DIM, font=f_sm, anchor="mm")
        draw.text((bx0 - 13, cy), str(i), fill=TEXT_DIM, font=f_sm, anchor="mm")

    champ_at: dict[tuple[int, int], int] = {
        (c.row, c.col): slot for slot, c in state.champions.items()
    }
    vault_cx = bx0 + 7 * CELL + CELL
    vault_cy = by0 + 7 * CELL + CELL

    # Drop shadow for walls and closed doors
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

    # Draw each cell
    for row in range(GRID_SIZE):
        for col in range(GRID_SIZE):
            x0 = bx0 + col * CELL + 1
            y0 = by0 + row * CELL + 1
            x1 = x0 + CELL - 2
            y1 = y0 + CELL - 2
            cx = (x0 + x1) // 2
            cy = (y0 + y1) // 2
            pos = (row, col)

            is_vault       = pos in VAULT_TILES
            is_wall        = pos in state.walls
            is_door_closed = pos in state.doors and pos not in state.opened_doors
            is_door_open   = pos in state.opened_doors
            loot_kind      = state.loot_tiles.get(pos)
            is_trap_vis    = pos in state.triggered_traps
            slot           = champ_at.get(pos)

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

            # Overlays
            if loot_kind == "common":
                draw.rectangle([x0 + 4, y0 + 4, x1 - 4, y1 - 4], fill=LOOT_BG, outline=LOOT_FG, width=1)
                draw.text((cx, cy), "$", fill=LOOT_FG, font=f_md, anchor="mm")
            elif loot_kind == "gold":
                draw.rectangle([x0 + 4, y0 + 4, x1 - 4, y1 - 4], fill=GOLD_BG, outline=GOLD_FG, width=1)
                draw.text((cx, cy), "Au", fill=GOLD_FG, font=f_md, anchor="mm")
            elif loot_kind == "mystery":
                draw.rectangle([x0 + 4, y0 + 4, x1 - 4, y1 - 4], fill=MYST_BG, outline=MYST_FG, width=1)
                draw.text((cx, cy), "?", fill=MYST_FG, font=f_md, anchor="mm")
            elif loot_kind == "diamond":
                draw.rectangle([x0 + 4, y0 + 4, x1 - 4, y1 - 4], fill=DIAM_BG, outline=DIAM_FG, width=1)
                draw.text((cx, cy), "◆", fill=DIAM_FG, font=f_md, anchor="mm")
            if is_trap_vis:
                draw.rectangle([x0 + 4, y0 + 4, x1 - 4, y1 - 4], fill=TRAP_BG, outline=TRAP_FG, width=1)
                draw.text((cx, cy), "!", fill=TRAP_FG, font=f_md, anchor="mm")

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
                    draw.ellipse(
                        [cx - rr - 3, cy - rr - 3, cx + rr + 3, cy + rr + 3],
                        outline=ICE_RING, width=2,
                    )
                if champ.submitted:
                    draw.text((cx, cy + 5), "✓", fill=(80, 255, 120), font=f_sm, anchor="mm")

    # Vault radial glow
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd   = ImageDraw.Draw(glow)
    for radius, alpha in [(75, 15), (58, 30), (42, 50), (28, 75), (16, 100)]:
        gd.ellipse(
            [vault_cx - radius, vault_cy - radius, vault_cx + radius, vault_cy + radius],
            fill=(255, 200, 0, alpha),
        )
    img = Image.alpha_composite(img, glow)
    draw = ImageDraw.Draw(img)

    # Bottom legend
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
        if champ.shuffle_used:      parts.append("🔀✓")
        if champ.submitted:         parts.append("🔒")
        draw.text((lx + 12, legend_y + 4), " ".join(parts), fill=TEXT_DIM, font=f_sm, anchor="lm")

    buf = BytesIO()
    img.convert("RGB").save(buf, "PNG", optimize=True)
    buf.seek(0)
    return buf


def _render_grid_text(state: GameState) -> str:
    champ_at = {(c.row, c.col): slot for slot, c in state.champions.items()}
    rows = []
    for r in range(GRID_SIZE):
        row = []
        for c in range(GRID_SIZE):
            pos  = (r, c)
            slot = champ_at.get(pos)
            if slot is not None:
                row.append(EMOJI_CHAMP[slot])
            elif pos in VAULT_TILES:   row.append("🏦")
            elif pos in state.walls:   row.append("🧱")
            elif pos in state.triggered_traps: row.append("⚠️")
            elif pos in state.doors and pos not in state.opened_doors: row.append("🚪")
            elif pos in state.opened_doors: row.append("🔓")
            elif state.loot_tiles.get(pos) == "diamond": row.append("💎")
            elif state.loot_tiles.get(pos) == "gold":    row.append("💰")
            elif state.loot_tiles.get(pos) == "mystery": row.append("📦")
            elif state.loot_tiles.get(pos) == "common":  row.append("💵")
            else: row.append("⬛")
        rows.append("".join(row))
    return "\n".join(rows)


def _status_table(state: GameState) -> str:
    col_w = [12, 9, 5, 10, 24]
    header = (
        f"{'Champion':<{col_w[0]}} {'Pos':<{col_w[1]}} {'EP':>{col_w[2]}} "
        f"{'Carrying':<{col_w[3]}} Status"
    )
    sep = "─" * (sum(col_w) + 4)
    rows = [header, sep]
    for slot, c in state.champions.items():
        pos      = f"[{c.row},{c.col}]"
        carrying = "💎 Diamond" if c.carrying_diamond else "—"
        tags: list[str] = []
        if c.frozen_rounds > 0: tags.append(f"❄️ Frozen({c.frozen_rounds}r)")
        if c.boost_requested:   tags.append("⚡ Boost")
        if c.shuffle_used:      tags.append("🔀 Used")
        if c.submitted:         tags.append("🔒 Locked")
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
        champ.move_queue      = ["action"]
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
        boost_desc  = f"yes (−{BOOST_EP_COST} EP)" if champ.boost_requested else "no"

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
                label="⚡ Movement Boost  (20 EP) — Unlock a second direction press this round",
                value="boost",
            ),
            discord.SelectOption(
                label="🥶 Freeze Charge  (40 EP) — Stun an adjacent opponent for 1 full round",
                value="freeze",
            ),
            discord.SelectOption(
                label="🔀 Board Shuffle  (60 EP) — Teleport to a random safe tile  [1×/game]",
                value="shuffle",
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
            # If boost was previously queued via this dropdown, undo it
            if champ.gadget == "boost":
                champ.boost_requested = False
                if len(champ.move_queue) > 1:
                    champ.move_queue = champ.move_queue[:1]
            champ.gadget             = None
            champ.gadget_target_slot = None
            await interaction.response.send_message("🚫 Gadget selection cleared.", ephemeral=True)
            return

        if val == "boost":
            if champ.frozen_rounds > 0:
                await interaction.response.send_message("❄️ Frozen — can't boost.", ephemeral=True)
                return
            if champ.carrying_diamond:
                await interaction.response.send_message(
                    "❌ Can't boost while carrying the Diamond Briefcase.", ephemeral=True
                )
                return
            if champ.ep < BOOST_EP_COST:
                await interaction.response.send_message(
                    f"❌ Need **{BOOST_EP_COST} EP** — you have **{champ.ep} EP**.", ephemeral=True
                )
                return
            champ.gadget          = "boost"
            champ.boost_requested = True
            await interaction.response.send_message(
                f"⚡ **Movement Boost** queued! (−{BOOST_EP_COST} EP at resolution)\n"
                "Now press a **second direction arrow** for your bonus move.",
                ephemeral=True,
            )

        elif val == "freeze":
            _, g_cost = VALID_GADGETS["freeze"]
            if champ.ep < g_cost:
                await interaction.response.send_message(
                    f"❌ Need **{g_cost} EP** — you have **{champ.ep} EP**.", ephemeral=True
                )
                return
            # Open target picker
            target_view = FreezeTargetView(view.cog, view.guild_id, champ.slot)
            await interaction.response.send_message(
                "🥶 **Freeze Charge** — select your target champion:",
                view=target_view,
                ephemeral=True,
            )

        elif val == "shuffle":
            _, g_cost = VALID_GADGETS["shuffle"]
            if champ.shuffle_used:
                await interaction.response.send_message(
                    "❌ **Board Shuffle** already used this match — each player gets **1 use** per game.",
                    ephemeral=True,
                )
                return
            if champ.ep < g_cost:
                await interaction.response.send_message(
                    f"❌ Need **{g_cost} EP** — you have **{champ.ep} EP**.", ephemeral=True
                )
                return
            champ.gadget             = "shuffle"
            champ.gadget_target_slot = None
            await interaction.response.send_message(
                f"🔀 **Board Shuffle** queued! (−{g_cost} EP at resolution)\n"
                "You will be teleported to a random safe empty tile at resolution.",
                ephemeral=True,
            )


class FreezeTargetSelect(discord.ui.Select["FreezeTargetView"]):
    """Ephemeral dropdown for picking a Freeze Charge target."""

    def __init__(self, cog: "BankBreakthroughCog", guild_id: int, requester_slot: int) -> None:
        self.cog            = cog
        self.guild_id       = guild_id
        self.requester_slot = requester_slot

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

        super().__init__(placeholder="🎯 Choose target to freeze…", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        state = self.cog._games.get(self.guild_id)
        if not state:
            await interaction.response.send_message("❌ No active match.", ephemeral=True)
            return

        requester = state.champions.get(self.requester_slot)
        if requester is None or interaction.user.id != requester.user_id:
            await interaction.response.send_message("❌ This targeting menu is not yours.", ephemeral=True)
            return

        val = self.values[0]
        if val == "none":
            await interaction.response.send_message("❌ No valid targets available.", ephemeral=True)
            return

        target_slot = int(val)
        target      = state.champions.get(target_slot)
        if target is None:
            await interaction.response.send_message("❌ Target not found.", ephemeral=True)
            return

        _, g_cost = VALID_GADGETS["freeze"]
        if requester.ep < g_cost:
            await interaction.response.send_message(
                f"❌ Not enough EP — need **{g_cost}**, have **{requester.ep}**.", ephemeral=True
            )
            return

        requester.gadget             = "freeze"
        requester.gadget_target_slot = target_slot

        await interaction.response.send_message(
            f"🥶 **Freeze Charge → {target.display_name}** queued (−{g_cost} EP at resolution).\n"
            "Will freeze for 1 full round if adjacent at resolution. EP refunded on miss.",
            ephemeral=True,
        )

        if self.view is not None:
            self.view.stop()


class FreezeTargetView(discord.ui.View):
    def __init__(self, cog: "BankBreakthroughCog", guild_id: int, requester_slot: int) -> None:
        super().__init__(timeout=60.0)
        self.add_item(FreezeTargetSelect(cog, guild_id, requester_slot))


class BreakthroughView(discord.ui.View):
    def __init__(self, cog: "BankBreakthroughCog", guild_id: int) -> None:
        super().__init__(timeout=float(ROUND_TIMER))
        self.cog      = cog
        self.guild_id = guild_id

        self.add_item(DirectionButton("up",    "🔼", row=0))
        self.add_item(DirectionButton("down",  "🔽", row=0))
        self.add_item(DirectionButton("left",  "◀️", row=0))
        self.add_item(DirectionButton("right", "▶️", row=0))
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
            title="🏦  Breakthrough v4 — Match Starting!",
            colour=C_VAULT,
            description=(
                f"{SEP}\n**Champions have entered the building.**\n"
                "Crack the Central Vault (3 Breach Points) to win!\n"
                f"{SEP}\n\n{roster}"
            ),
        )
        em.add_field(
            name="🗺️ Grid",
            value=f"15×15 — {len(state.walls)} permanent walls in corridors between blocks · BFS validated",
            inline=False,
        )
        em.add_field(name="⚡ Energy", value="100 EP · +10/round regen", inline=True)
        em.add_field(
            name="🎒 Gadget Kit  (3 total)",
            value=(
                "⚡ Movement Boost — 20 EP — 2nd move this round\n"
                "🥶 Freeze Charge — 40 EP — stun 1 adj. opponent (EP refunded on miss)\n"
                "🔀 Board Shuffle — 60 EP — random teleport · 1×/match"
            ),
            inline=True,
        )
        em.add_field(
            name="💰 Loot Spawns",
            value=(
                f"💵 Cash Bundle ×{NUM_LOOT_COMMON} (+{LOOT_REWARD_COMMON:,} 🪙)\n"
                f"💰 Gold Bar ×{NUM_LOOT_GOLD} (+{LOOT_REWARD_GOLD:,} 🪙)\n"
                f"📦 Mystery Box ×{NUM_LOOT_MYSTERY} (±random · reveals on pickup)\n"
                f"💎 Diamond Briefcase ×1 at round {DIAMOND_SPAWN_ROUND} (+{LOOT_REWARD_DIAMOND:,} 🪙)"
            ),
            inline=False,
        )
        em.add_field(name="🎮 Controls", value="Use the **buttons on the board message** each round.", inline=False)
        em.set_footer(text="Breakthrough · Champion Edition v4")
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

    @app_commands.command(name="gadgethelp", description="Show the Breakthrough gadget reference card.")
    @app_commands.guild_only()
    async def gadget_help(self, interaction: discord.Interaction) -> None:
        em = discord.Embed(
            title="🎒  Breakthrough — Gadget Reference",
            colour=C_TEAM,
            description=(
                "Select gadgets from the **🎒 Select Gadget…** dropdown during the planning phase.\n"
                "EP is deducted at resolution — not when you queue the gadget.\n"
                "**Only 3 gadgets are registered in the system.** All others are permanently disabled.\n"
            ),
        )

        em.add_field(
            name="⚡  Movement Boost  —  20 EP  |  Self-cast",
            value=(
                "Grants you a **second directional arrow press** this round, letting you cover **2 tiles** instead of 1.\n"
                "Queue the gadget from the dropdown first, then press your **first** direction, then your **second** direction.\n"
                "**Blocked while carrying the Diamond Briefcase** — the extra move is silently stripped.\n"
                "EP is deducted at resolution; if you no longer have 20 EP, the second step is cancelled and you move 1 tile instead.\n"
                "Hit **🚫 Cancel** in the dropdown to undo a queued Boost (also removes the second direction)."
            ),
            inline=False,
        )

        em.add_field(
            name="🥶  Freeze Charge  —  40 EP  |  Sabotage  |  target via dropdown",
            value=(
                "After selecting this gadget a **target picker** appears — choose which opponent to freeze.\n"
                "At resolution, if your target is **within 1 tile** (orthogonal **or** diagonal), they are:\n"
                "  • Immediately rooted — their queued move is cancelled this round\n"
                "  • **Frozen for 1 full round** — they auto-skip their entire next planning phase\n"
                "If the target has moved out of range by resolution time, **the 40 EP is fully refunded** — no penalty.\n"
                "You can only freeze **one** opponent per round."
            ),
            inline=False,
        )

        em.add_field(
            name="🔀  Board Shuffle  —  60 EP  |  Self-cast  |  ⚠️ 1× per player per match",
            value=(
                "Teleports you to a **random safe empty floor tile** anywhere on the 15×15 grid.\n"
                "Safe means: not a wall, vault cell, locked door, triggered trap, or occupied tile.\n"
                "**Each player may only use this once per match**, regardless of how much EP they have.\n"
                "Once spent, a `🔀✓` badge appears next to your name in the board legend.\n"
                "Best used to escape a corner trap, reset your approach vector, or bait opponents."
            ),
            inline=False,
        )

        em.add_field(
            name="📌  General Rules",
            value=(
                "• Only **one gadget** may be queued per round — selecting a new one replaces the previous.\n"
                "• **🚫 Cancel** clears your gadget selection and undoes a queued Boost.\n"
                "• EP costs are deducted **at resolution**, not when you queue — so you can queue freely and cancel.\n"
                "• If you fall below the cost by resolution time, the gadget cancels with **no charge**.\n"
                f"• EP cap: **{MAX_EP}** · Regen: **+{EP_REGEN} EP** at the end of every round."
            ),
            inline=False,
        )

        em.set_footer(text="Breakthrough v4 · Gadget Reference · Costs deducted at resolution · 3 gadgets total")
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
                    f"`[{pos[0]},{pos[1]}]` — +500 🪙 but blocks the Boost gadget while carrying!"
                )

        channel = self.bot.get_channel(state.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        # Reset round inputs; frozen champions are auto-submitted (hold)
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
                resolution_log.append(
                    f"⏰ **{champ.display_name}** timed out — defaulting to **hold**."
                )

        # ════════════════════════════════════════════════════════════════════
        # PHASE 0 — BOOST EP DEDUCTION
        # ════════════════════════════════════════════════════════════════════
        for champ in state.champions.values():
            if champ.boost_requested:
                if champ.ep >= BOOST_EP_COST:
                    champ.ep -= BOOST_EP_COST
                    resolution_log.append(
                        f"⚡ **{champ.display_name}** used Movement Boost "
                        f"(−{BOOST_EP_COST} EP → {champ.ep} EP remaining)."
                    )
                else:
                    champ.boost_requested = False
                    if len(champ.move_queue) > 1:
                        champ.move_queue = champ.move_queue[:1]
                    resolution_log.append(
                        f"⚡ **{champ.display_name}** lacked EP for Boost — second move cancelled."
                    )

        # ════════════════════════════════════════════════════════════════════
        # PHASE 1 — GADGETS
        # ════════════════════════════════════════════════════════════════════

        # Tick down freeze counters first
        for champ in state.champions.values():
            if champ.frozen_rounds > 0:
                champ.frozen_rounds -= 1
                if champ.frozen_rounds == 0:
                    resolution_log.append(f"🌡️ **{champ.display_name}** thawed — free next round.")

        for champ in state.champions.values():
            key = champ.gadget
            if not key or key == "boost":
                continue   # "boost" already handled in PHASE 0

            g_name, g_cost = VALID_GADGETS[key]

            # EP affordability check (re-verify at resolution)
            if champ.ep < g_cost:
                resolution_log.append(
                    f"⚡ **{champ.display_name}** tried **{g_name}** but has insufficient EP "
                    f"({champ.ep}/{g_cost}) — cancelled at no cost."
                )
                continue

            # ── 🥶 Freeze Charge ──────────────────────────────────────────
            if key == "freeze":
                ts     = champ.gadget_target_slot
                target = state.champions.get(ts) if ts is not None else None
                if target and _is_adjacent(champ.row, champ.col, target.row, target.col):
                    champ.ep -= g_cost
                    target.frozen_rounds = max(target.frozen_rounds, 1)
                    target.move_queue    = []
                    target.submitted     = True
                    resolution_log.append(
                        f"🥶 **{champ.display_name}** froze **{target.display_name}** with "
                        f"Freeze Charge! Rooted for **1 full round**. (−{g_cost} EP)"
                    )
                else:
                    t_name = (
                        state.champions[ts].display_name
                        if ts is not None and ts in state.champions else "?"
                    )
                    resolution_log.append(
                        f"🥶 **{champ.display_name}**'s Freeze Charge missed — "
                        f"**{t_name}** was not adjacent. EP fully refunded."
                    )
                    # No EP deducted on miss

            # ── 🔀 Board Shuffle ──────────────────────────────────────────
            elif key == "shuffle":
                if champ.shuffle_used:
                    resolution_log.append(
                        f"🔀 **{champ.display_name}**'s Board Shuffle already spent — cancelled."
                    )
                    continue
                dest = _random_safe_tile(state, champ.slot)
                if dest:
                    champ.ep -= g_cost
                    old_pos = (champ.row, champ.col)
                    champ.row, champ.col = dest
                    champ.shuffle_used   = True
                    resolution_log.append(
                        f"🔀 **{champ.display_name}** activated Board Shuffle! "
                        f"Teleported `{old_pos}` → `[{dest[0]},{dest[1]}]`. "
                        f"(−{g_cost} EP · 1 use permanently consumed)"
                    )
                else:
                    resolution_log.append(
                        f"🔀 **{champ.display_name}**'s Board Shuffle found no safe landing tile — cancelled."
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

        # Collision detection — bounce back on shared tile
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
                    f"💵 **{champ.display_name}** grabbed a Cash Bundle! +{LOOT_REWARD_COMMON:,} 🪙 "
                    f"(team total: {new_bal:,})"
                )
            elif loot_kind == "gold":
                del state.loot_tiles[pos]
                new_bal = _db_add_coins(champ.team, LOOT_REWARD_GOLD)
                champ.loot_claimed += 1
                resolution_log.append(
                    f"💰 **{champ.display_name}** snagged a Gold Bar! +{LOOT_REWARD_GOLD:,} 🪙 "
                    f"(team total: {new_bal:,})"
                )
            elif loot_kind == "mystery":
                del state.loot_tiles[pos]
                champ.loot_claimed += 1
                if random.random() < MYSTERY_GAIN_CHANCE:
                    amount  = random.randint(MYSTERY_GAIN_MIN, MYSTERY_GAIN_MAX)
                    new_bal = _db_add_coins(champ.team, amount)
                    resolution_log.append(
                        f"📦 **{champ.display_name}** opened a Mystery Box — **JACKPOT!** "
                        f"+{amount:,} 🪙 (team total: {new_bal:,})"
                    )
                else:
                    amount  = random.randint(MYSTERY_LOSS_MIN, MYSTERY_LOSS_MAX)
                    new_bal = _db_add_coins(champ.team, -amount)
                    resolution_log.append(
                        f"📦 **{champ.display_name}** opened a Mystery Box — **BOOBY TRAP!** "
                        f"−{amount:,} 🪙 (team total: {new_bal:,})"
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

        # End-of-round EP regen
        for champ in state.champions.values():
            champ.ep = min(MAX_EP, champ.ep + EP_REGEN)

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
            loot_v = list(state.loot_tiles.values())
            loot_summary_parts = []
            if (n := loot_v.count("common")):   loot_summary_parts.append(f"{n} 💵")
            if (n := loot_v.count("gold")):     loot_summary_parts.append(f"{n} 💰")
            if (n := loot_v.count("mystery")):  loot_summary_parts.append(f"{n} 📦")
            if "diamond" in loot_v:             loot_summary_parts.append("1 💎")
            log_em.set_footer(
                text=(
                    f"Vault: {state.breach_points}/{VAULT_POINTS_REQ} BP  ·  "
                    f"Loot on board: {', '.join(loot_summary_parts) or 'none'}"
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
        loot_vals = list(state.loot_tiles.values())
        loot_parts: list[str] = []
        if (n := loot_vals.count("common")):  loot_parts.append(f"**{n}** 💵")
        if (n := loot_vals.count("gold")):    loot_parts.append(f"**{n}** 💰")
        if (n := loot_vals.count("mystery")): loot_parts.append(f"**{n}** 📦")
        if "diamond" in loot_vals:            loot_parts.append("**1** 💎")
        em.add_field(
            name="💰 Loot on Board",
            value=" · ".join(loot_parts) if loot_parts else "—",
            inline=True,
        )
        em.set_footer(
            text=(
                f"Breakthrough v4 · 15×15 · {len(state.walls)} walls · "
                f"Doors: {len(state.opened_doors)}/{len(FIXED_DOORS)} opened"
            )
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
        medals     = ["🥇", "🥈", "🥉", "4️⃣"]
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
        em.set_footer(text="Breakthrough · Champion Edition v4 · Match Over")

        if img_buf:
            img_buf.seek(0)
            await channel.send(file=discord.File(img_buf, filename="board.png"), embed=em)
        else:
            await channel.send(embed=em)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BankBreakthroughCog(bot))
