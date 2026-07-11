"""
family_tree_renderer.py – Names-only family tree formatter for Solace.

No avatars, no image generation, no CDN downloads — only display names.
Keeps bandwidth near zero for /family and /tree on Railway/Render.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


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
# Text formatting
# ---------------------------------------------------------------------------

def _person_label(info: PersonInfo) -> str:
    name = (info.display_name or f"User {info.user_id}").replace("`", "'")
    if info.is_adopted:
        return f"{name} ✦"
    return name


def _node_label(node: FamilyNode) -> str:
    if node.is_couple:
        left = _person_label(node.left)
        right = _person_label(node.right)
        if node.is_divorced:
            return f"{left} ✕ {right} (divorced)"
        label = f"{left} ∞ {right}"
        if node.married_at:
            _, stage = _stage_from_married_at(node.married_at)
            label = f"{label}  [{stage}]"
        return label
    return _person_label(node.person)


def _walk_lines(node: FamilyNode, prefix: str = "", is_last: bool = True) -> list[str]:
    connector = "└── " if is_last else "├── "
    lines = [f"{prefix}{connector}{_node_label(node)}"]

    child_prefix = prefix + ("    " if is_last else "│   ")
    for i, child in enumerate(node.children):
        lines.extend(_walk_lines(child, child_prefix, i == len(node.children) - 1))
    return lines


def format_tree_text(roots: list[FamilyNode]) -> str:
    """
    Build a monospace names-only tree.

    Example:
        Alice ∞ Bob  [Soulmates]
        ├── Charlie
        └── Dana ∞ Eve  [Newly Married]
            └── Frank ✦
    """
    if not roots:
        return "No family relationships exist yet."

    lines: list[str] = []
    for i, root in enumerate(roots):
        if i > 0:
            lines.append("")
        lines.append(_node_label(root))
        for j, child in enumerate(root.children):
            lines.extend(_walk_lines(child, "", j == len(root.children) - 1))
    return "\n".join(lines)


def format_tree_chunks(roots: list[FamilyNode], max_chars: int = 3800) -> list[str]:
    """
    Split a formatted tree into Discord-safe chunks (under embed description limits).
    Each chunk is plain text; callers wrap in code fences if desired.
    """
    full = format_tree_text(roots)
    if len(full) <= max_chars:
        return [full]

    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in full.splitlines():
        # +1 for newline when joining
        add = len(line) + (1 if current else 0)
        if current and size + add > max_chars:
            chunks.append("\n".join(current))
            current = [line]
            size = len(line)
        else:
            current.append(line)
            size += add
    if current:
        chunks.append("\n".join(current))
    return chunks or ["No family relationships exist yet."]


def count_people(roots: list[FamilyNode]) -> int:
    seen: set[int] = set()

    def _walk(node: FamilyNode) -> None:
        for uid in node.all_person_ids:
            seen.add(uid)
        for child in node.children:
            _walk(child)

    for root in roots:
        _walk(root)
    return len(seen)
