"""
family_tree_renderer.py – Pillow-based family tree image generator for Solace.
Dark-themed modern design with avatars, glowing connecting lines, and stage badges.
No database imports — all relationship data is passed in by the caller.
"""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Colour palette – dark modern theme
# ---------------------------------------------------------------------------
BG_COLOR       = (13, 15, 23)
GRID_DOT       = (22, 26, 42)
CARD_COLOR     = (24, 28, 44)
CARD_BORDER    = (48, 55, 88)
MARRIAGE_LINE  = (255, 190, 60)    # gold
PARENT_LINE    = (90, 170, 255)    # sky blue
ADOPTED_LINE   = (190, 120, 255)   # purple
DIVORCED_LINE  = (80, 85, 110)     # muted grey
TEXT_PRIMARY   = (235, 238, 255)
TEXT_MUTED     = (130, 138, 175)
BADGE_BG       = (35, 40, 62)
BADGE_BORDER   = (60, 68, 105)
HEADER_COLOR   = (175, 135, 255)
ACCENT_TEAL    = (60, 220, 200)

# Node dimensions
AVATAR_SIZE = 56
CARD_W      = 100
CARD_H      = 100
H_GAP       = 55
V_GAP       = 110
COUPLE_GAP  = 18   # gap between the two spouse cards
MARGIN      = 70


# ---------------------------------------------------------------------------
# Relationship stage  (no DB import – computed here)
# ---------------------------------------------------------------------------

def _stage_from_married_at(married_at) -> tuple[str, str]:
    if isinstance(married_at, str):
        dt = datetime.fromisoformat(married_at)
    else:
        dt = married_at
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - dt).days
    if days < 7:
        return "❤️", "Newly Married"
    if days < 30:
        return "💖", "Loving Couple"
    if days < 180:
        return "💞", "Soulmates"
    return "👑", "Legendary Couple"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class PersonInfo:
    user_id: int
    display_name: str
    is_adopted: bool = False


@dataclass
class FamilyNode:
    """
    A node in the family tree.
    A CoupleNode has left + right persons; a SingleNode only has `person`.
    """
    person: Optional[PersonInfo] = None         # single
    left: Optional[PersonInfo] = None           # couple – left spouse
    right: Optional[PersonInfo] = None          # couple – right spouse
    married_at: Optional[object] = None         # datetime or ISO string
    is_divorced: bool = False
    children: list["FamilyNode"] = field(default_factory=list)

    @property
    def is_couple(self) -> bool:
        return self.left is not None and self.right is not None

    @property
    def all_person_ids(self) -> list[int]:
        ids = []
        if self.person:
            ids.append(self.person.user_id)
        if self.left:
            ids.append(self.left.user_id)
        if self.right:
            ids.append(self.right.user_id)
        return ids


# ---------------------------------------------------------------------------
# Avatar helpers
# ---------------------------------------------------------------------------

async def _fetch_one(session: aiohttp.ClientSession, url: str) -> bytes | None:
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status == 200:
                return await r.read()
    except Exception:
        pass
    return None


async def fetch_avatars(user_avatar_urls: dict[int, str]) -> dict[int, bytes | None]:
    """Fetch all avatar bytes concurrently. Returns {user_id: bytes|None}."""
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *[_fetch_one(session, url) for url in user_avatar_urls.values()],
            return_exceptions=False,
        )
    return dict(zip(user_avatar_urls.keys(), results))


def _make_circle_avatar(raw: bytes | None, size: int = AVATAR_SIZE) -> Image.Image:
    base = Image.new("RGBA", (size, size), (45, 50, 78, 255))
    if raw:
        try:
            src = Image.open(io.BytesIO(raw)).convert("RGBA").resize((size, size), Image.LANCZOS)
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
            base.paste(src, (0, 0), mask)
            return base
        except Exception:
            pass
    # Placeholder: solid circle with user silhouette colouring
    draw = ImageDraw.Draw(base)
    draw.ellipse((0, 0, size - 1, size - 1), fill=(55, 62, 95))
    return base


def _ring_avatar(av: Image.Image, color: tuple, width: int = 3) -> Image.Image:
    """Draw a coloured ring around the avatar."""
    size = av.size[0]
    result = av.copy()
    draw = ImageDraw.Draw(result)
    draw.ellipse((width // 2, width // 2, size - width // 2 - 1, size - width // 2 - 1),
                 outline=color, width=width)
    return result


# ---------------------------------------------------------------------------
# Font helpers
# ---------------------------------------------------------------------------

def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = (
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
         "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
         "C:/Windows/Fonts/arialbd.ttf",
         "/System/Library/Fonts/Helvetica.ttc"]
        if bold else
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
         "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
         "C:/Windows/Fonts/arial.ttf",
         "/System/Library/Fonts/Helvetica.ttc"]
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Layout engine
# ---------------------------------------------------------------------------

@dataclass
class _LayoutNode:
    node: FamilyNode
    cx: int        # horizontal centre of this unit
    cy: int        # top-y of this unit
    subtree_w: int
    children: list["_LayoutNode"] = field(default_factory=list)


def _subtree_width(node: FamilyNode) -> int:
    """Minimum pixel width required by a node's full subtree."""
    if node.is_couple:
        self_w = CARD_W * 2 + COUPLE_GAP + H_GAP * 2
    else:
        self_w = CARD_W + H_GAP * 2

    if not node.children:
        return self_w

    children_total = sum(_subtree_width(c) for c in node.children)
    return max(self_w, children_total)


def _build_layout(node: FamilyNode, cx: int, cy: int) -> _LayoutNode:
    sw = _subtree_width(node)
    ln = _LayoutNode(node=node, cx=cx, cy=cy, subtree_w=sw)

    if node.children:
        child_cy = cy + CARD_H + V_GAP
        widths = [_subtree_width(c) for c in node.children]
        total = sum(widths)
        start_x = cx - total // 2
        for child, w in zip(node.children, widths):
            child_cx = start_x + w // 2
            ln.children.append(_build_layout(child, child_cx, child_cy))
            start_x += w

    return ln


def _canvas_size(roots: list[_LayoutNode]) -> tuple[int, int]:
    def _depth(n: _LayoutNode) -> int:
        if not n.children:
            return 1
        return 1 + max(_depth(c) for c in n.children)

    total_w = sum(r.subtree_w for r in roots) + MARGIN * 2 + H_GAP * max(0, len(roots) - 1)
    max_d   = max((_depth(r) for r in roots), default=1)
    total_h = 90 + max_d * (CARD_H + V_GAP) + MARGIN

    return max(total_w, 900), max(total_h, 450)


# ---------------------------------------------------------------------------
# Drawing primitives
# ---------------------------------------------------------------------------

def _glow_line(canvas: Image.Image,
               x0: int, y0: int, x1: int, y1: int,
               color: tuple, width: int = 3,
               glow: bool = True) -> None:
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    if glow:
        for extra in range(8, 0, -2):
            alpha = max(15, 70 - extra * 8)
            d.line([(x0, y0), (x1, y1)],
                   fill=(color[0], color[1], color[2], alpha),
                   width=width + extra)
    d.line([(x0, y0), (x1, y1)], fill=(*color, 220), width=width)
    canvas.alpha_composite(overlay)


def _rounded_rect(draw: ImageDraw.ImageDraw, xy, radius, fill, outline=None, outline_width=2):
    draw.rounded_rectangle(xy, radius=radius, fill=fill, outline=outline, width=outline_width)


def _truncate(text: str, font: ImageFont.FreeTypeFont, max_px: int) -> str:
    while len(text) > 1:
        bb = font.getbbox(text)
        if (bb[2] - bb[0]) <= max_px:
            break
        text = text[:-2] + "…"
    return text


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------

def _draw_person_card(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    info: PersonInfo,
    cx: int, cy: int,
    av_img: Image.Image,
    font_name: ImageFont.FreeTypeFont,
    font_small: ImageFont.FreeTypeFont,
    ring_color: tuple = CARD_BORDER,
) -> None:
    x0, y0 = cx - CARD_W // 2, cy
    x1, y1 = x0 + CARD_W, y0 + CARD_H

    # Card shadow (subtle)
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.rounded_rectangle((x0 + 3, y0 + 3, x1 + 3, y1 + 3), radius=14,
                          fill=(0, 0, 0, 60))
    canvas.alpha_composite(shadow)

    # Card background
    _rounded_rect(draw, (x0, y0, x1, y1), radius=14,
                  fill=CARD_COLOR, outline=CARD_BORDER, outline_width=2)

    # Adopted star badge
    if info.is_adopted:
        _rounded_rect(draw, (x0 + 3, y0 + 3, x0 + 22, y0 + 18), radius=4,
                      fill=(80, 50, 130), outline=ADOPTED_LINE, outline_width=1)
        draw.text((x0 + 5, y0 + 3), "✦", font=font_small, fill=ADOPTED_LINE)

    # Avatar with ring
    ringed = _ring_avatar(av_img, ring_color, width=3)
    av_x = cx - AVATAR_SIZE // 2
    av_y = y0 + 8
    canvas.paste(ringed, (av_x, av_y), ringed)

    # Name
    name = _truncate(info.display_name, font_name, CARD_W - 10)
    bb = font_name.getbbox(name)
    nw = bb[2] - bb[0]
    draw.text((cx - nw // 2, av_y + AVATAR_SIZE + 5), name, font=font_name, fill=TEXT_PRIMARY)


def _draw_badge(canvas: Image.Image, draw: ImageDraw.ImageDraw,
                cx: int, line_y: int, text: str, font: ImageFont.FreeTypeFont) -> None:
    bb = font.getbbox(text)
    bw = (bb[2] - bb[0]) + 14
    bh = (bb[3] - bb[1]) + 8
    bx, by = cx - bw // 2, line_y - bh // 2
    _rounded_rect(draw, (bx, by, bx + bw, by + bh), radius=8,
                  fill=BADGE_BG, outline=BADGE_BORDER, outline_width=1)
    draw.text((bx + 7, by + 4), text, font=font, fill=TEXT_MUTED)


def _render_node(
    canvas: Image.Image,
    ln: _LayoutNode,
    av_cache: dict[int, Image.Image],
    fonts: dict,
    parent_connector_xy: tuple[int, int] | None = None,
) -> None:
    draw = ImageDraw.Draw(canvas)
    node = ln.node

    # ── Determine x positions ──────────────────────────────────────────────
    if node.is_couple:
        half_gap = CARD_W // 2 + COUPLE_GAP // 2 + 2
        left_cx  = ln.cx - half_gap
        right_cx = ln.cx + half_gap
        couple_top_y = ln.cy
    else:
        left_cx = right_cx = ln.cx
        couple_top_y = ln.cy

    # ── Draw parent connector (line from parent spine to this node) ────────
    if parent_connector_xy:
        px, py = parent_connector_xy
        entry_y = ln.cy
        mid_y = (py + entry_y) // 2
        entry_x = ln.cx
        _glow_line(canvas, px, py, px, mid_y, PARENT_LINE, width=2)
        _glow_line(canvas, px, mid_y, entry_x, mid_y, PARENT_LINE, width=2)
        _glow_line(canvas, entry_x, mid_y, entry_x, entry_y, PARENT_LINE, width=2)

    # ── Marriage line between spouses ──────────────────────────────────────
    if node.is_couple:
        lx = left_cx + CARD_W // 2
        rx = right_cx - CARD_W // 2
        line_y = couple_top_y + CARD_H // 2
        line_col = DIVORCED_LINE if node.is_divorced else MARRIAGE_LINE
        _glow_line(canvas, lx, line_y, rx, line_y, line_col, width=3,
                   glow=not node.is_divorced)

        # Stage badge on the marriage line
        if not node.is_divorced and node.married_at:
            emoji, label = _stage_from_married_at(node.married_at)
            _draw_badge(canvas, draw, ln.cx, line_y,
                        f"{emoji} {label}", fonts["badge"])

    # ── Draw person card(s) ────────────────────────────────────────────────
    if node.is_couple:
        ring_col = DIVORCED_LINE if node.is_divorced else MARRIAGE_LINE
        av_l = av_cache.get(node.left.user_id, _make_circle_avatar(None))
        av_r = av_cache.get(node.right.user_id, _make_circle_avatar(None))
        _draw_person_card(canvas, draw, node.left,  left_cx,  ln.cy, av_l,
                          fonts["name"], fonts["small"], ring_col)
        _draw_person_card(canvas, draw, node.right, right_cx, ln.cy, av_r,
                          fonts["name"], fonts["small"], ring_col)
    else:
        av = av_cache.get(node.person.user_id, _make_circle_avatar(None))
        _draw_person_card(canvas, draw, node.person, ln.cx, ln.cy, av,
                          fonts["name"], fonts["small"], ACCENT_TEAL)

    # ── Draw children ──────────────────────────────────────────────────────
    if ln.children:
        stem_start_y = ln.cy + CARD_H + 6
        spine_y      = stem_start_y + V_GAP // 2

        # Vertical stem down from this node
        _glow_line(canvas, ln.cx, stem_start_y, ln.cx, spine_y, PARENT_LINE, width=2)

        # Horizontal spine across children
        if len(ln.children) > 1:
            xs = [c.cx for c in ln.children]
            _glow_line(canvas, min(xs), spine_y, max(xs), spine_y, PARENT_LINE, width=2)

        for child_ln in ln.children:
            # Choose line colour based on whether child is adopted
            child_node = child_ln.node
            adopted = (child_node.person and child_node.person.is_adopted) or \
                      (child_node.is_couple and (
                          (child_node.left and child_node.left.is_adopted) or
                          (child_node.right and child_node.right.is_adopted)))
            child_line_col = ADOPTED_LINE if adopted else PARENT_LINE

            # Drop from spine to child top
            _glow_line(canvas, child_ln.cx, spine_y, child_ln.cx, child_ln.cy,
                       child_line_col, width=2, glow=adopted)

            _render_node(canvas, child_ln, av_cache, fonts, parent_connector_xy=None)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def render_tree(
    roots: list[FamilyNode],
    avatar_urls: dict[int, str],
    title: str = "Solace Family Tree",
) -> io.BytesIO:
    """
    Render the full family tree to a WEBP BytesIO.

    roots        – list of top-level FamilyNode objects (no parents above them)
    avatar_urls  – {user_id: avatar_url}  (any user appearing in the tree)
    title        – header text
    """
    if not roots:
        return _empty_canvas(title)

    # Fetch all avatars concurrently
    raw_avatars = await fetch_avatars(avatar_urls)
    av_cache: dict[int, Image.Image] = {
        uid: _make_circle_avatar(raw) for uid, raw in raw_avatars.items()
    }

    # Build layout
    layout_roots: list[_LayoutNode] = []
    cur_x = MARGIN
    for node in roots:
        sw = _subtree_width(node)
        ln = _build_layout(node, cur_x + sw // 2, 90)
        layout_roots.append(ln)
        cur_x += sw + H_GAP

    canvas_w, canvas_h = _canvas_size(layout_roots)

    # Create canvas
    canvas = Image.new("RGBA", (canvas_w, canvas_h), (*BG_COLOR, 255))
    draw = ImageDraw.Draw(canvas)

    # Grid dots background
    for gx in range(0, canvas_w, 38):
        for gy in range(0, canvas_h, 38):
            draw.ellipse((gx - 1, gy - 1, gx + 1, gy + 1), fill=(*GRID_DOT, 255))

    # Fonts
    fonts = {
        "title": _load_font(26, bold=True),
        "sub":   _load_font(13),
        "name":  _load_font(11, bold=True),
        "small": _load_font(9),
        "badge": _load_font(10),
    }

    # Header
    draw.text((canvas_w // 2, 14), title, font=fonts["title"],
              fill=HEADER_COLOR, anchor="mt")

    # Legend
    legend = [
        ("━━ Marriage",    MARRIAGE_LINE),
        ("━━ Parent-Child", PARENT_LINE),
        ("━━ Adopted",     ADOPTED_LINE),
        ("━━ Divorced",    DIVORCED_LINE),
    ]
    lx = 18
    for txt, col in legend:
        draw.text((lx, 52), txt, font=fonts["sub"], fill=col)
        lx += fonts["sub"].getbbox(txt)[2] + 28

    # Render tree
    for ln in layout_roots:
        _render_node(canvas, ln, av_cache, fonts)

    # Output
    out = io.BytesIO()
    canvas.convert("RGB").save(out, format="WEBP", quality=75, method=6)
    out.seek(0)
    return out


def _empty_canvas(title: str) -> io.BytesIO:
    canvas = Image.new("RGB", (700, 300), BG_COLOR)
    draw = ImageDraw.Draw(canvas)
    font = _load_font(22, bold=True)
    draw.text((350, 120), title, font=font, fill=HEADER_COLOR, anchor="mm")
    draw.text((350, 165), "No family relationships exist yet.", font=_load_font(14),
              fill=TEXT_MUTED, anchor="mm")
    out = io.BytesIO()
    canvas.save(out, format="WEBP", quality=75, method=6)
    out.seek(0)
    return out
