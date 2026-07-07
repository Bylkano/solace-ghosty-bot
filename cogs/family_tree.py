"""
family_tree.py – Solace Family Tree Bot Cog
============================================
Drop this file (plus family_tree_db.py and family_tree_renderer.py) into your
cogs/ folder, then add "cogs.family_tree" to the COGS list in bot.py.

Commands
--------
  /marry       @user          – Propose marriage
  /divorce                    – Divorce your current spouse
  /adopt       @user          – Adopt someone as a child
  /disown      @user          – Disown one of your children
  /child       [@user]        – List someone's children
  /couple      [@user]        – Show couple stats
  /family      [@user]        – Show a user's family branch image
  /tree        [@user]        – Full server tree, or a single user's branch
  /relationship @user         – Show how you are related to someone
  /runaway                    – (Child) Cut ties with your parents
  /familystats                – Family leaderboards for this server
  /familyconfig incest ...    – (Admin) Allow or disallow incest

Rules
-----
  • Max 10 children per couple
  • Each child may have at most 2 parents
  • Marriage requires both parties to be single in this server
  • Adoption requires the child to accept
  • Incest (marrying a blood/adopted relative) is disallowed by default;
    a server admin can toggle it via /familyconfig incest
"""

from __future__ import annotations

import asyncio
import io
import logging
import pathlib
import sys
import time as _time
from collections import deque
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import discord
from discord import app_commands
from discord.ext import commands

import cogs.family_tree_db as db
from cogs.family_tree_renderer import (
    FamilyNode,
    PersonInfo,
    fetch_avatars,
    render_tree,
)

log = logging.getLogger("bot.family_tree")

MAX_CHILDREN = 10   # per couple
MAX_PARENTS  = 2    # per child
PROPOSAL_TIMEOUT = 60  # seconds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_date(dt) -> str:
    if dt is None:
        return "unknown"
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return discord.utils.format_dt(dt, style="D")


def _avatar_url(member: discord.Member | discord.User) -> str:
    return member.display_avatar.with_format("png").with_size(128).url


def _is_admin(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    return (interaction.user.guild_permissions.administrator
            or interaction.user.id == interaction.guild.owner_id)


async def _run_in_thread(func, *args):
    """Run a synchronous DB call in a thread pool to avoid blocking the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, func, *args)


# ---------------------------------------------------------------------------
# Tree render cache – invalidated by any mutating family command
# ---------------------------------------------------------------------------

_render_cache: dict[str, tuple[bytes, float]] = {}
_CACHE_TTL = 90  # seconds


def _cache_get(key: str) -> bytes | None:
    entry = _render_cache.get(key)
    if entry and _time.monotonic() - entry[1] < _CACHE_TTL:
        return entry[0]
    return None


def _cache_set(key: str, data: bytes) -> None:
    _render_cache[key] = (data, _time.monotonic())


def _invalidate_guild_cache(guild_id: int) -> None:
    stale = [k for k in _render_cache
             if k == f"tree:{guild_id}" or k.startswith(f"family:{guild_id}:")]
    for k in stale:
        _render_cache.pop(k, None)


async def _rewarm_guild_cache(guild: discord.Guild) -> None:
    """
    Rebuild the full-guild tree PNG in the background immediately after a
    mutation.  This keeps /tree instant — the cache is always pre-populated
    rather than only warm for 90 s after the first manual render.
    """
    guild_id = guild.id
    try:
        marriages = await _run_in_thread(db.get_all_active_marriages, guild_id)
        pc_rows   = await _run_in_thread(db.get_all_active_parent_child, guild_id)

        if not marriages and not pc_rows:
            return  # empty server — nothing to render

        roots = _build_family_tree(marriages, pc_rows)
        if not roots:
            return

        all_ids = _collect_ids_from_roots(roots)
        name_map: dict[int, str] = {}
        av_urls:  dict[int, str] = {}
        for uid in all_ids:
            m = guild.get_member(uid)
            if m:
                name_map[uid] = m.display_name
                av_urls[uid]  = _avatar_url(m)
            else:
                name_map[uid] = f"User {uid}"

        _fill_display_names(roots, name_map)

        img_buf = await render_tree(
            roots=roots,
            avatar_urls=av_urls,
            title="Solace Family Tree",
        )
        img_buf.seek(0)
        _cache_set(f"tree:{guild_id}", img_buf.read())
        log.debug("Background tree rewarm complete for guild %s", guild_id)
    except Exception:
        log.exception("Background tree rewarm failed for guild %s", guild_id)


# ---------------------------------------------------------------------------
# Proposal Views (button-based, in-memory only)
# ---------------------------------------------------------------------------

class MarryView(discord.ui.View):
    """Sent to the proposed-to user. They accept or decline the marriage proposal."""

    def __init__(self, proposer: discord.Member, proposed: discord.Member):
        super().__init__(timeout=PROPOSAL_TIMEOUT)
        self.proposer = proposer
        self.proposed = proposed
        self.accepted: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.proposed.id:
            await interaction.response.send_message(
                "💌 This proposal isn't for you!", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Accept 💍", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.accepted = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Decline 💔", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.accepted = False
        self.stop()
        await interaction.response.defer()

    async def on_timeout(self):
        self.accepted = None
        self.stop()


class AdoptView(discord.ui.View):
    """Sent to the child being adopted. They accept or decline."""

    def __init__(self, parents_mention: str, child: discord.Member):
        super().__init__(timeout=PROPOSAL_TIMEOUT)
        self.parents_mention = parents_mention
        self.child = child
        self.accepted: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.child.id:
            await interaction.response.send_message(
                "🌟 This adoption request is not for you!", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Accept 🌟", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.accepted = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Decline ❌", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.accepted = False
        self.stop()
        await interaction.response.defer()

    async def on_timeout(self):
        self.accepted = None
        self.stop()


class DivorceConfirmView(discord.ui.View):
    """Asks the user to confirm they really want to divorce."""

    def __init__(self, user: discord.Member):
        super().__init__(timeout=30)
        self.user = user
        self.confirmed: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ Not your button.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, divorce 💔", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        self.stop()
        await interaction.response.defer()

    async def on_timeout(self):
        self.confirmed = None
        self.stop()


class RunawayView(discord.ui.View):
    """Confirmation before a user removes themselves from their family."""

    def __init__(self, user: discord.Member):
        super().__init__(timeout=30)
        self.user = user
        self.confirmed: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("❌ Not your button.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, run away 🏃", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Stay ❤️", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = False
        self.stop()
        await interaction.response.defer()

    async def on_timeout(self):
        self.confirmed = None
        self.stop()


# ---------------------------------------------------------------------------
# Tree builder
# ---------------------------------------------------------------------------

def _build_family_tree(
    marriages: list[dict],
    pc_rows: list[dict],
) -> list[FamilyNode]:
    """
    Build a list of root FamilyNodes from raw DB rows.

    Strategy: work with "units" (a couple counts as one unit, keyed by the
    lower of the two user IDs) rather than individual users.  This prevents
    the visited-user approach from silently dropping child links when a child
    has two non-coupled parents.

    marriages – rows with keys: user1_id, user2_id, married_at, divorced_at
    pc_rows   – rows with keys: parent_id, child_id, is_adopted
    """
    # ── Index raw data ────────────────────────────────────────────────────
    # spouse_map[uid] = other_uid   (active marriages only)
    # married_at_map[(min,max)] = married_at timestamp
    spouse_map:    dict[int, int]    = {}
    married_at_map: dict[tuple, object] = {}

    for m in marriages:
        if m.get("divorced_at"):
            continue
        u1, u2 = m["user1_id"], m["user2_id"]
        spouse_map[u1] = u2
        spouse_map[u2] = u1
        married_at_map[(min(u1, u2), max(u1, u2))] = m["married_at"]

    children_of: dict[int, set[int]] = {}   # uid → {child_ids}
    parents_of:  dict[int, list[int]] = {}  # child_uid → [parent_ids]
    adopted_set: set[int] = set()

    for row in pc_rows:
        p, c = row["parent_id"], row["child_id"]
        children_of.setdefault(p, set()).add(c)
        parents_of.setdefault(c, []).append(p)
        if row.get("is_adopted"):
            adopted_set.add(c)

    # ── Collect all user IDs ──────────────────────────────────────────────
    all_users: set[int] = set(spouse_map.keys())
    for p, kids in children_of.items():
        all_users.add(p)
        all_users.update(kids)

    # ── Map every user to their "unit ID" (canonical key for their node) ──
    # A couple's unit ID = min(uid, spouse_uid); a single's = uid itself.
    def _unit_id(uid: int) -> int:
        s = spouse_map.get(uid)
        return min(uid, s) if s is not None else uid

    # Collect unique unit IDs
    all_unit_ids: set[int] = {_unit_id(u) for u in all_users}

    # Children of a unit = union of children from both members (if couple)
    def _unit_children(unit_id: int) -> set[int]:
        kids = set(children_of.get(unit_id, set()))
        spouse = spouse_map.get(unit_id)
        if spouse is not None:
            kids |= children_of.get(spouse, set())
        return kids

    # A unit is a root if none of its members appear as a child with an active parent
    def _is_root_unit(unit_id: int) -> bool:
        members = [unit_id]
        s = spouse_map.get(unit_id)
        if s is not None:
            members.append(s)
        return all(uid not in parents_of for uid in members)

    root_unit_ids = [u for u in all_unit_ids if _is_root_unit(u)]

    # ── Build FamilyNode tree recursively, tracking visited *units* ───────
    visited_units: set[int] = set()

    def _person(uid: int) -> PersonInfo:
        return PersonInfo(
            user_id=uid,
            display_name=str(uid),          # filled in later
            is_adopted=(uid in adopted_set),
        )

    def _build_node(unit_id: int) -> FamilyNode | None:
        if unit_id in visited_units:
            # Already expanded under another root — return a stub (couple/person
            # shown but no children) so the parent→child link is still visible
            # without re-expanding the subtree or causing infinite recursion.
            spouse = spouse_map.get(unit_id)
            if spouse is not None:
                key = (min(unit_id, spouse), max(unit_id, spouse))
                return FamilyNode(
                    left=_person(unit_id), right=_person(spouse),
                    married_at=married_at_map.get(key), is_divorced=False, children=[],
                )
            return FamilyNode(person=_person(unit_id), children=[])
        visited_units.add(unit_id)

        child_unit_ids = {_unit_id(c) for c in _unit_children(unit_id)}
        child_nodes: list[FamilyNode] = []
        for child_unit in child_unit_ids:
            n = _build_node(child_unit)
            if n is not None:
                child_nodes.append(n)

        spouse = spouse_map.get(unit_id)
        if spouse is not None:
            key = (min(unit_id, spouse), max(unit_id, spouse))
            return FamilyNode(
                left=_person(unit_id),
                right=_person(spouse),
                married_at=married_at_map.get(key),
                is_divorced=False,
                children=child_nodes,
            )
        else:
            return FamilyNode(
                person=_person(unit_id),
                children=child_nodes,
            )

    tree_roots: list[FamilyNode] = []
    for uid in root_unit_ids:
        node = _build_node(uid)
        if node is not None:
            tree_roots.append(node)

    # Safety: include any units that weren't reachable from the roots
    for uid in all_unit_ids:
        if uid not in visited_units:
            node = _build_node(uid)
            if node is not None:
                tree_roots.append(node)

    return tree_roots


def _fill_display_names(roots: list[FamilyNode], name_map: dict[int, str]) -> None:
    """Replace numeric placeholders with actual display names (in-place)."""
    def _walk(node: FamilyNode):
        if node.person:
            node.person.display_name = name_map.get(node.person.user_id, f"User {node.person.user_id}")
        if node.left:
            node.left.display_name = name_map.get(node.left.user_id, f"User {node.left.user_id}")
        if node.right:
            node.right.display_name = name_map.get(node.right.user_id, f"User {node.right.user_id}")
        for child in node.children:
            _walk(child)

    for root in roots:
        _walk(root)


def _collect_ids_from_roots(roots: list[FamilyNode]) -> set[int]:
    ids: set[int] = set()

    def _walk(node: FamilyNode):
        if node.person:
            ids.add(node.person.user_id)
        if node.left:
            ids.add(node.left.user_id)
        if node.right:
            ids.add(node.right.user_id)
        for child in node.children:
            _walk(child)

    for root in roots:
        _walk(root)
    return ids


def _find_connected_roots(roots: list[FamilyNode], user_id: int) -> list[FamilyNode]:
    """
    Return every root-level tree that contains *user_id* anywhere in its subtree.

    This correctly handles merged families: if A (adopted by P1/P2) marries B
    (child of C), both the P1/P2 root tree AND the C root tree contain the A-B
    couple node (C's version is a stub), so both are returned and rendered
    together — showing A's full ancestry alongside B's.
    """
    def _contains(node: FamilyNode) -> bool:
        if user_id in node.all_person_ids:
            return True
        return any(_contains(c) for c in node.children)

    return [r for r in roots if _contains(r)]


def _great_prefix(steps_above_parent: int) -> str:
    if steps_above_parent <= 0:
        return ""
    return "great-" * steps_above_parent


def _ancestor_term(distance: int) -> str:
    if distance == 1:
        return "parent"
    if distance == 2:
        return "grandparent"
    return f"{_great_prefix(distance - 2)}grandparent"


def _descendant_term(distance: int) -> str:
    if distance == 1:
        return "child"
    if distance == 2:
        return "grandchild"
    return f"{_great_prefix(distance - 2)}grandchild"


def _steps_up(
    start: int,
    target: int,
    parents_of: dict[int, set[int]],
) -> int | None:
    """Distance from start to target by walking parent links only."""
    q: deque[tuple[int, int]] = deque([(start, 0)])
    seen = {start}
    while q:
        node, dist = q.popleft()
        if node == target:
            return dist
        for p in parents_of.get(node, set()):
            if p in seen:
                continue
            seen.add(p)
            q.append((p, dist + 1))
    return None


def _ancestor_distances(
    start: int,
    parents_of: dict[int, set[int]],
) -> dict[int, int]:
    """Map ancestor_id -> steps up from start (minimum steps in DAG)."""
    dists: dict[int, int] = {start: 0}
    q: deque[int] = deque([start])
    while q:
        node = q.popleft()
        base = dists[node]
        for p in parents_of.get(node, set()):
            new_dist = base + 1
            old = dists.get(p)
            if old is not None and old <= new_dist:
                continue
            dists[p] = new_dist
            q.append(p)
    return dists


def _ordinal(n: int) -> str:
    if 10 <= (n % 100) <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _pibling_term(distance_from_user_to_ancestor: int) -> str:
    if distance_from_user_to_ancestor == 1:
        return "aunt/uncle"
    if distance_from_user_to_ancestor == 2:
        return "great-aunt/uncle"
    return f"{_great_prefix(distance_from_user_to_ancestor - 2)}great-aunt/uncle"


def _nibling_term(distance_from_member_to_ancestor: int) -> str:
    if distance_from_member_to_ancestor == 1:
        return "niece/nephew"
    if distance_from_member_to_ancestor == 2:
        return "grand-niece/nephew"
    return f"{_great_prefix(distance_from_member_to_ancestor - 2)}grand-niece/nephew"


def _cousin_term(m: int, n: int) -> str:
    degree = min(m, n) - 1
    removed = abs(m - n)
    base = f"{_ordinal(degree)} cousin"
    if removed == 0:
        return base
    if removed == 1:
        return f"{base} once removed"
    if removed == 2:
        return f"{base} twice removed"
    return f"{base} {removed} times removed"


def _shortest_family_path(
    start: int,
    target: int,
    adjacency: dict[int, set[int]],
) -> list[int]:
    """Shortest undirected path through family edges (parents + spouses)."""
    if start == target:
        return [start]
    q: deque[int] = deque([start])
    prev: dict[int, int | None] = {start: None}
    while q:
        node = q.popleft()
        for nxt in adjacency.get(node, set()):
            if nxt in prev:
                continue
            prev[nxt] = node
            if nxt == target:
                path: list[int] = [target]
                cur = target
                while prev[cur] is not None:
                    cur = prev[cur]
                    path.append(cur)
                path.reverse()
                return path
            q.append(nxt)
    return []


# ---------------------------------------------------------------------------
# The Cog
# ---------------------------------------------------------------------------

class FamilyTree(commands.Cog):
    """Solace Family Tree – marry, adopt, and explore your server's family history."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /relationship ──────────────────────────────────────────────────────

    @app_commands.command(name="relationship", description="🧬 See how you're related to another member.")
    @app_commands.describe(member="The member you want to check your relationship with.")
    @app_commands.guild_only()
    async def relationship(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer()

        guild_id = interaction.guild_id
        user = interaction.user

        if member.id == user.id:
            return await interaction.followup.send(
                embed=discord.Embed(
                    title="🧬 Relationship Check",
                    description="That's you. You're related to yourself. 😄",
                    color=0x6699CC,
                )
            )

        marriages = await _run_in_thread(db.get_all_active_marriages, guild_id)
        pc_rows = await _run_in_thread(db.get_all_active_parent_child, guild_id)

        spouse_of: dict[int, int] = {}
        parents_of: dict[int, set[int]] = {}
        children_of: dict[int, set[int]] = {}
        adjacency: dict[int, set[int]] = {}

        def _link(a: int, b: int) -> None:
            adjacency.setdefault(a, set()).add(b)
            adjacency.setdefault(b, set()).add(a)

        for m in marriages:
            if m.get("divorced_at"):
                continue
            a = m["user1_id"]
            b = m["user2_id"]
            spouse_of[a] = b
            spouse_of[b] = a
            _link(a, b)

        for row in pc_rows:
            p = row["parent_id"]
            c = row["child_id"]
            parents_of.setdefault(c, set()).add(p)
            children_of.setdefault(p, set()).add(c)
            _link(p, c)

        uid = user.id
        vid = member.id

        relation: str
        path_detail: str | None = None
        if spouse_of.get(uid) == vid:
            relation = "💍 You are married to each other."
        elif vid in parents_of.get(uid, set()):
            relation = "👨‍👩‍👧 They are your parent."
        elif uid in parents_of.get(vid, set()):
            relation = "👶 They are your child."
        elif parents_of.get(uid, set()) & parents_of.get(vid, set()):
            relation = "🧑‍🤝‍🧑 You are siblings (you share at least one parent)."
        else:
            up = _steps_up(uid, vid, parents_of)
            down = _steps_up(vid, uid, parents_of)
            if up is not None and up > 0:
                relation = f"🌳 They are your **{_ancestor_term(up)}**."
            elif down is not None and down > 0:
                relation = f"🌱 They are your **{_descendant_term(down)}**."
            else:
                # Try richer kinship via nearest shared ancestor.
                a_uid = _ancestor_distances(uid, parents_of)
                a_vid = _ancestor_distances(vid, parents_of)
                shared = set(a_uid) & set(a_vid)
                shared.discard(uid)
                shared.discard(vid)
                if shared:
                    best = min(shared, key=lambda a: (max(a_uid[a], a_vid[a]), a_uid[a] + a_vid[a]))
                    m = a_uid[best]
                    n = a_vid[best]
                    if m == 1 and n >= 2:
                        relation = f"🧬 They are your **{_nibling_term(n - 1)}**."
                    elif n == 1 and m >= 2:
                        relation = f"🧬 They are your **{_pibling_term(m - 1)}**."
                    elif m >= 2 and n >= 2:
                        relation = f"🧬 You are **{_cousin_term(m, n)}**."
                    else:
                        relation = "🔗 You are family-connected through a shared ancestor."
                else:
                    relation = "❌ No known family relationship found."

                path = _shortest_family_path(uid, vid, adjacency)
                if path:
                    segments: list[str] = []
                    for i in range(len(path) - 1):
                        a = path[i]
                        b = path[i + 1]
                        if spouse_of.get(a) == b:
                            edge = "spouse"
                        elif b in parents_of.get(a, set()):
                            edge = "parent"
                        elif b in children_of.get(a, set()):
                            edge = "child"
                        else:
                            edge = "family"
                        segments.append(edge)
                    path_detail = " → ".join(segments)

        embed = discord.Embed(
            title="🧬 Relationship",
            description=f"**{user.mention}** ↔ **{member.mention}**\n\n{relation}",
            color=0xAA88FF,
        )
        if path_detail:
            embed.add_field(name="🧭 Path", value=f"`{path_detail}`", inline=False)
        await interaction.followup.send(embed=embed)

    # ── /marry ────────────────────────────────────────────────────────────

    @app_commands.command(name="marry", description="💍 Propose marriage to another member.")
    @app_commands.describe(member="The person you want to propose to.")
    @app_commands.guild_only()
    async def marry(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=False)

        proposer = interaction.user
        guild_id = interaction.guild_id

        # Basic sanity checks
        if member.bot:
            return await interaction.followup.send("❌ You can't marry a bot!", ephemeral=True)
        if member.id == proposer.id:
            return await interaction.followup.send("❌ You can't marry yourself!", ephemeral=True)

        # Both must be single
        proposer_marriage = await _run_in_thread(db.get_marriage, guild_id, proposer.id)
        if proposer_marriage:
            return await interaction.followup.send(
                "❌ You're already married! Use `/divorce` first.", ephemeral=True
            )
        target_marriage = await _run_in_thread(db.get_marriage, guild_id, member.id)
        if target_marriage:
            return await interaction.followup.send(
                f"❌ {member.mention} is already married.", ephemeral=True
            )

        # Incest check
        incest_ok = await _run_in_thread(db.get_incest_allowed, guild_id)
        if not incest_ok:
            related = await _run_in_thread(db.is_related, guild_id, proposer.id, member.id)
            if related:
                return await interaction.followup.send(
                    "❌ You can't marry a blood relative or adopted family member! "
                    "(Incest is disabled on this server.)",
                    ephemeral=True,
                )

        # Send proposal
        view = MarryView(proposer, member)
        proposal_embed = discord.Embed(
            title="💍 Marriage Proposal!",
            description=(
                f"{proposer.mention} has gone down on one knee and proposed to {member.mention}!\n\n"
                f"{member.mention}, will you accept? *(60 seconds to decide)*"
            ),
            color=0xFFD700,
        )
        proposal_embed.set_thumbnail(url=_avatar_url(proposer))
        msg = await interaction.followup.send(embed=proposal_embed, view=view)

        await view.wait()

        if view.accepted is None:
            proposal_embed.color = 0x555566
            proposal_embed.set_footer(text="The proposal expired with no answer. 💨")
            return await msg.edit(embed=proposal_embed, view=None)

        if not view.accepted:
            proposal_embed.color = 0xCC3344
            proposal_embed.description = (
                f"💔 {member.mention} has declined the proposal from {proposer.mention}."
            )
            proposal_embed.title = "💔 Proposal Declined"
            return await msg.edit(embed=proposal_embed, view=None)

        # Re-check both still single (race condition guard)
        proposer_marriage = await _run_in_thread(db.get_marriage, guild_id, proposer.id)
        target_marriage   = await _run_in_thread(db.get_marriage, guild_id, member.id)
        if proposer_marriage or target_marriage:
            return await msg.edit(
                embed=discord.Embed(
                    description="❌ Something changed while you were deciding — please try again.",
                    color=0xCC3344,
                ),
                view=None,
            )

        await _run_in_thread(db.create_marriage, guild_id, proposer.id, member.id)
        _invalidate_guild_cache(guild_id)
        asyncio.create_task(_rewarm_guild_cache(interaction.guild))

        success_embed = discord.Embed(
            title="❤️ Just Married!",
            description=(
                f"🎊 {proposer.mention} and {member.mention} are now married!\n\n"
                f"💍 Status: **Newly Married**\n"
                f"📅 Wedding date: {_fmt_date(_now_utc())}"
            ),
            color=0xFF6B9D,
        )
        success_embed.set_footer(text="May your love grow stronger every day. 💕")
        await msg.edit(embed=success_embed, view=None)

    # ── /divorce ──────────────────────────────────────────────────────────

    @app_commands.command(name="divorce", description="💔 Divorce your current spouse.")
    @app_commands.guild_only()
    async def divorce(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        guild_id = interaction.guild_id
        user = interaction.user

        marriage = await _run_in_thread(db.get_marriage, guild_id, user.id)
        if not marriage:
            return await interaction.followup.send(
                "❌ You're not married.", ephemeral=True
            )

        spouse_id = (marriage["user2_id"]
                     if marriage["user1_id"] == user.id
                     else marriage["user1_id"])
        spouse = interaction.guild.get_member(spouse_id)
        spouse_name = spouse.mention if spouse else f"<@{spouse_id}>"

        view = DivorceConfirmView(user)
        confirm_embed = discord.Embed(
            title="💔 Confirm Divorce",
            description=(
                f"Are you sure you want to divorce {spouse_name}?\n\n"
                "**This cannot be undone.** Your relationship history will be preserved "
                "in the records but marked as divorced."
            ),
            color=0xCC3344,
        )
        msg = await interaction.followup.send(embed=confirm_embed, view=view, ephemeral=True)

        await view.wait()

        if not view.confirmed:
            return await msg.edit(
                embed=discord.Embed(description="✅ Divorce cancelled.", color=0x44BB88),
                view=None,
            )

        await _run_in_thread(db.divorce, guild_id, user.id)
        _invalidate_guild_cache(guild_id)
        asyncio.create_task(_rewarm_guild_cache(interaction.guild))

        await interaction.channel.send(
            embed=discord.Embed(
                title="💔 Divorced",
                description=f"{user.mention} and {spouse_name} have divorced.",
                color=0x666677,
            )
        )
        await msg.edit(
            embed=discord.Embed(description="💔 Divorce finalised.", color=0x666677),
            view=None,
        )

    # ── /adopt ────────────────────────────────────────────────────────────

    @app_commands.command(name="adopt", description="🌟 Adopt a member as your child.")
    @app_commands.describe(member="The person you want to adopt.")
    @app_commands.guild_only()
    async def adopt(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=False)

        guild_id = interaction.guild_id
        parent   = interaction.user

        if member.bot:
            return await interaction.followup.send("❌ You can't adopt a bot!", ephemeral=True)
        if member.id == parent.id:
            return await interaction.followup.send("❌ You can't adopt yourself!", ephemeral=True)

        # Child can't already be one of the adopter's parents
        child_parents = await _run_in_thread(db.get_parents, guild_id, parent.id)
        if member.id in child_parents:
            return await interaction.followup.send(
                f"❌ {member.mention} is already one of your parents!", ephemeral=True
            )

        # Check parent count for the child
        existing_parents = await _run_in_thread(db.get_parents, guild_id, member.id)
        if len(existing_parents) >= MAX_PARENTS:
            return await interaction.followup.send(
                f"❌ {member.mention} already has {MAX_PARENTS} parents and can't have more.",
                ephemeral=True,
            )

        # Already parent?
        already = await _run_in_thread(db.is_already_parent_of, guild_id, parent.id, member.id)
        if already:
            return await interaction.followup.send(
                f"❌ You are already a parent of {member.mention}.", ephemeral=True
            )

        # Figure out who the parents will be (both spouses if married)
        marriage = await _run_in_thread(db.get_marriage, guild_id, parent.id)
        parent_ids: list[int] = [parent.id]
        parent_mentions: list[str] = [parent.mention]

        if marriage:
            spouse_id = (marriage["user2_id"]
                         if marriage["user1_id"] == parent.id
                         else marriage["user1_id"])
            parent_ids.append(spouse_id)
            spouse = interaction.guild.get_member(spouse_id)
            if spouse:
                parent_mentions.append(spouse.mention)
            else:
                parent_mentions.append(f"<@{spouse_id}>")

            # Check shared child cap (children both parents already share)
            shared = await _run_in_thread(db.count_shared_children, guild_id, parent.id, spouse_id)
            if shared >= MAX_CHILDREN:
                return await interaction.followup.send(
                    f"❌ You and your spouse already have {MAX_CHILDREN} children (the maximum).",
                    ephemeral=True,
                )

            # Compute how many NEW parents would actually be added
            # (the child may already have one of the two spouses as a parent)
            new_parent_ids = [pid for pid in parent_ids if pid not in existing_parents]
            if len(existing_parents) + len(new_parent_ids) > MAX_PARENTS:
                return await interaction.followup.send(
                    f"❌ {member.mention} already has {len(existing_parents)} parent(s) "
                    f"and adopting as a couple would exceed the limit of {MAX_PARENTS}.",
                    ephemeral=True,
                )
        else:
            # Single adopter – count their own children
            own_children = await _run_in_thread(db.get_children, guild_id, parent.id)
            if len(own_children) >= MAX_CHILDREN:
                return await interaction.followup.send(
                    f"❌ You already have {MAX_CHILDREN} children (the maximum).",
                    ephemeral=True,
                )

        # Incest check (prevent adopting relatives)
        incest_ok = await _run_in_thread(db.get_incest_allowed, guild_id)
        if not incest_ok:
            for pid in parent_ids:
                related = await _run_in_thread(db.is_related, guild_id, pid, member.id)
                if related:
                    return await interaction.followup.send(
                        "❌ You can't adopt a blood or adopted relative! "
                        "(Incest is disabled on this server.)",
                        ephemeral=True,
                    )

        # Send adoption request to the child
        parents_str = " and ".join(parent_mentions)
        view = AdoptView(parents_str, member)
        request_embed = discord.Embed(
            title="🌟 Adoption Request",
            description=(
                f"{parents_str} would like to **adopt** {member.mention}!\n\n"
                f"{member.mention}, do you accept? *(60 seconds to decide)*"
            ),
            color=0xAA66FF,
        )
        msg = await interaction.followup.send(embed=request_embed, view=view)

        await view.wait()

        if view.accepted is None:
            request_embed.color = 0x555566
            request_embed.set_footer(text="The adoption request expired. 💨")
            return await msg.edit(embed=request_embed, view=None)

        if not view.accepted:
            request_embed.color = 0xCC3344
            request_embed.title = "❌ Adoption Declined"
            request_embed.description = f"{member.mention} has declined the adoption request."
            return await msg.edit(embed=request_embed, view=None)

        # Commit
        for pid in parent_ids:
            already2 = await _run_in_thread(db.is_already_parent_of, guild_id, pid, member.id)
            if not already2:
                await _run_in_thread(db.add_parent_child, guild_id, pid, member.id, True)
        _invalidate_guild_cache(guild_id)
        asyncio.create_task(_rewarm_guild_cache(interaction.guild))

        success_embed = discord.Embed(
            title="🌟 Adopted!",
            description=(
                f"🎉 {member.mention} has been adopted by {parents_str}!\n"
                f"Welcome to the family! 🏠"
            ),
            color=0xAA66FF,
        )
        await msg.edit(embed=success_embed, view=None)

    # ── /disown ───────────────────────────────────────────────────────────

    @app_commands.command(name="disown", description="💢 Disown one of your children.")
    @app_commands.describe(member="The child you want to disown.")
    @app_commands.guild_only()
    async def disown(self, interaction: discord.Interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)

        guild_id = interaction.guild_id
        parent   = interaction.user

        is_parent = await _run_in_thread(db.is_already_parent_of, guild_id, parent.id, member.id)
        if not is_parent:
            return await interaction.followup.send(
                f"❌ {member.mention} is not your child.", ephemeral=True
            )

        await _run_in_thread(db.disown_child, guild_id, parent.id, member.id)
        _invalidate_guild_cache(guild_id)
        asyncio.create_task(_rewarm_guild_cache(interaction.guild))

        await interaction.followup.send(
            embed=discord.Embed(
                title="💢 Disowned",
                description=f"{parent.mention} has disowned {member.mention}.",
                color=0x884422,
            ),
            ephemeral=False,
        )

    # ── /child ────────────────────────────────────────────────────────────

    @app_commands.command(name="child", description="👶 View someone's children.")
    @app_commands.describe(member="The member to check (defaults to you).")
    @app_commands.guild_only()
    async def child(self, interaction: discord.Interaction,
                    member: discord.Member | None = None):
        await interaction.response.defer()

        target = member or interaction.user
        guild_id = interaction.guild_id

        kids = await _run_in_thread(db.get_children_with_details, guild_id, target.id)

        if not kids:
            return await interaction.followup.send(
                embed=discord.Embed(
                    description=f"{target.mention} has no children.",
                    color=0x445566,
                )
            )

        lines = []
        for row in kids:
            kid = interaction.guild.get_member(row["child_id"])
            name = kid.mention if kid else f"<@{row['child_id']}>"
            tag  = " *(adopted)*" if row["is_adopted"] else ""
            lines.append(f"• {name}{tag}")

        embed = discord.Embed(
            title=f"👨‍👩‍👧 {target.display_name}'s Children",
            description="\n".join(lines),
            color=0x6699CC,
        )
        embed.set_thumbnail(url=_avatar_url(target))
        embed.set_footer(text=f"{len(kids)}/{MAX_CHILDREN} children")
        await interaction.followup.send(embed=embed)

    # ── /couple ───────────────────────────────────────────────────────────

    @app_commands.command(name="couple", description="💑 Show couple stats for you or another member.")
    @app_commands.describe(member="The member to check (defaults to you).")
    @app_commands.guild_only()
    async def couple(self, interaction: discord.Interaction,
                     member: discord.Member | None = None):
        await interaction.response.defer()

        target   = member or interaction.user
        guild_id = interaction.guild_id

        marriage = await _run_in_thread(db.get_marriage, guild_id, target.id)
        if not marriage:
            return await interaction.followup.send(
                embed=discord.Embed(
                    description=f"{target.mention} is not currently married.",
                    color=0x445566,
                )
            )

        spouse_id = (marriage["user2_id"]
                     if marriage["user1_id"] == target.id
                     else marriage["user1_id"])
        spouse = interaction.guild.get_member(spouse_id)
        spouse_name = spouse.mention if spouse else f"<@{spouse_id}>"

        married_at = marriage["married_at"]
        emoji, stage = db.get_relationship_stage(married_at)

        # Children count
        ch1 = set(await _run_in_thread(db.get_children, guild_id, target.id))
        ch2 = set(await _run_in_thread(db.get_children, guild_id, spouse_id))
        shared_kids = ch1 | ch2

        if isinstance(married_at, str):
            married_at_dt = datetime.fromisoformat(married_at)
        else:
            married_at_dt = married_at
        if married_at_dt.tzinfo is None:
            married_at_dt = married_at_dt.replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - married_at_dt).days

        embed = discord.Embed(
            title=f"{emoji} Couple Profile",
            color=0xFFB347,
        )
        embed.add_field(name="💑 Partners",
                        value=f"{target.mention}\n{spouse_name}", inline=True)
        embed.add_field(name="📅 Married",
                        value=_fmt_date(married_at_dt), inline=True)
        embed.add_field(name="⏳ Together",
                        value=f"{days} day{'s' if days != 1 else ''}", inline=True)
        embed.add_field(name="💖 Stage",
                        value=f"{emoji} **{stage}**", inline=True)
        embed.add_field(name="👨‍👩‍👧 Children",
                        value=f"{len(shared_kids)} / {MAX_CHILDREN}", inline=True)
        if spouse:
            embed.set_thumbnail(url=_avatar_url(target))
        await interaction.followup.send(embed=embed)

    # ── /family ───────────────────────────────────────────────────────────

    @app_commands.command(name="family", description="🌳 Show a member's family branch.")
    @app_commands.describe(member="The member whose family to display (defaults to you).")
    @app_commands.guild_only()
    async def family(self, interaction: discord.Interaction,
                     member: discord.Member | None = None):
        await interaction.response.defer()

        target   = member or interaction.user
        guild_id = interaction.guild_id

        # Fast path: serve cached render
        cache_key = f"family:{guild_id}:{target.id}"
        cached_png = _cache_get(cache_key)
        if cached_png:
            await interaction.followup.send(
                embed=discord.Embed(title=f"🌳 {target.display_name}'s Family", color=0x6699CC),
                file=discord.File(io.BytesIO(cached_png), filename="family.png"),
            )
            return

        marriages = await _run_in_thread(db.get_all_active_marriages, guild_id)
        pc_rows   = await _run_in_thread(db.get_all_active_parent_child, guild_id)

        roots = _build_family_tree(marriages, pc_rows)

        # Collect every root tree that touches this user (own ancestry + in-laws)
        connected_roots = _find_connected_roots(roots, target.id)
        if not connected_roots:
            return await interaction.followup.send(
                embed=discord.Embed(
                    description=f"{target.mention} has no family connections yet.",
                    color=0x445566,
                )
            )

        all_ids = _collect_ids_from_roots(connected_roots)

        # Resolve display names and avatar URLs
        name_map: dict[int, str] = {}
        av_urls:  dict[int, str] = {}
        for uid in all_ids:
            m = interaction.guild.get_member(uid)
            if m:
                name_map[uid] = m.display_name
                av_urls[uid]  = _avatar_url(m)
            else:
                name_map[uid] = f"User {uid}"

        _fill_display_names(connected_roots, name_map)

        status_msg = await interaction.followup.send(
            embed=discord.Embed(
                description=f"🎨 Generating {target.display_name}'s family tree…",
                color=0x445566,
            )
        )

        img_buf = await render_tree(
            roots=connected_roots,
            avatar_urls=av_urls,
            title=f"{target.display_name}'s Family — Solace",
        )

        img_buf.seek(0)
        png_bytes = img_buf.read()
        _cache_set(cache_key, png_bytes)

        await status_msg.edit(
            embed=discord.Embed(
                title=f"🌳 {target.display_name}'s Family",
                color=0x6699CC,
            ),
            attachments=[discord.File(io.BytesIO(png_bytes), filename="family.png")],
        )

    # ── /tree ─────────────────────────────────────────────────────────────

    @app_commands.command(
        name="tree",
        description="🌲 Show the full family tree, or add @member to see just their branch.",
    )
    @app_commands.describe(member="Show only this member's family branch (optional).")
    @app_commands.guild_only()
    async def tree(self, interaction: discord.Interaction, member: discord.Member | None = None):
        await interaction.response.defer()

        guild_id = interaction.guild_id

        # ── Single-user branch ─────────────────────────────────────────────
        if member is not None:
            cache_key = f"family:{guild_id}:{member.id}"
            cached_png = _cache_get(cache_key)
            if cached_png:
                await interaction.followup.send(
                    embed=discord.Embed(title=f"🌳 {member.display_name}'s Family", color=0x6699CC),
                    file=discord.File(io.BytesIO(cached_png), filename="family.png"),
                )
                return

            marriages = await _run_in_thread(db.get_all_active_marriages, guild_id)
            pc_rows   = await _run_in_thread(db.get_all_active_parent_child, guild_id)
            roots     = _build_family_tree(marriages, pc_rows)
            connected = _find_connected_roots(roots, member.id)

            if not connected:
                return await interaction.followup.send(
                    embed=discord.Embed(
                        description=f"{member.mention} has no family connections yet.",
                        color=0x445566,
                    )
                )

            all_ids = _collect_ids_from_roots(connected)
            name_map: dict[int, str] = {}
            av_urls:  dict[int, str] = {}
            for uid in all_ids:
                m = interaction.guild.get_member(uid)
                if m:
                    name_map[uid] = m.display_name
                    av_urls[uid]  = _avatar_url(m)
                else:
                    name_map[uid] = f"User {uid}"

            _fill_display_names(connected, name_map)

            status_msg = await interaction.followup.send(
                embed=discord.Embed(
                    description=f"🎨 Generating {member.display_name}'s family tree…",
                    color=0x445566,
                )
            )
            img_buf = await render_tree(
                roots=connected,
                avatar_urls=av_urls,
                title=f"{member.display_name}'s Family — Solace",
            )
            img_buf.seek(0)
            png_bytes = img_buf.read()
            _cache_set(cache_key, png_bytes)
            await status_msg.edit(
                embed=discord.Embed(title=f"🌳 {member.display_name}'s Family", color=0x6699CC),
                attachments=[discord.File(io.BytesIO(png_bytes), filename="family.png")],
            )
            return

        # ── Full guild tree ────────────────────────────────────────────────
        cache_key  = f"tree:{guild_id}"
        cached_png = _cache_get(cache_key)
        if cached_png:
            await interaction.followup.send(
                embed=discord.Embed(title="🌲 Solace Family Tree", color=0xAA88FF),
                file=discord.File(io.BytesIO(cached_png), filename="solace_family_tree.png"),
            )
            return

        marriages = await _run_in_thread(db.get_all_active_marriages, guild_id)
        pc_rows   = await _run_in_thread(db.get_all_active_parent_child, guild_id)
        roots     = _build_family_tree(marriages, pc_rows)

        if not roots:
            return await interaction.followup.send(
                embed=discord.Embed(
                    title="🌲 Solace Family Tree",
                    description=(
                        "No family relationships exist yet!\n\n"
                        "Use `/marry` and `/adopt` to start building the family tree."
                    ),
                    color=0x334455,
                )
            )

        all_ids = _collect_ids_from_roots(roots)
        name_map = {}
        av_urls  = {}
        for uid in all_ids:
            m = interaction.guild.get_member(uid)
            if m:
                name_map[uid] = m.display_name
                av_urls[uid]  = _avatar_url(m)
            else:
                name_map[uid] = f"User {uid}"

        _fill_display_names(roots, name_map)

        status_msg = await interaction.followup.send(
            embed=discord.Embed(
                description="🎨 Generating the Solace family tree… This may take a moment.",
                color=0x334455,
            )
        )
        img_buf = await render_tree(roots=roots, avatar_urls=av_urls, title="Solace Family Tree")
        img_buf.seek(0)
        png_bytes = img_buf.read()
        _cache_set(cache_key, png_bytes)

        total_couples = len(marriages)
        total_pc      = len(pc_rows)
        adopted_count = sum(1 for r in pc_rows if r.get("is_adopted"))

        await status_msg.edit(
            embed=discord.Embed(
                title="🌲 Solace Family Tree",
                description=(
                    f"**{total_couples}** couple{'s' if total_couples != 1 else ''} · "
                    f"**{len(all_ids)}** members · "
                    f"**{total_pc}** parent-child links · "
                    f"**{adopted_count}** adopted"
                ),
                color=0xAA88FF,
            ).set_footer(text="💞 Relationships update automatically."),
            attachments=[discord.File(io.BytesIO(png_bytes), filename="solace_family_tree.png")],
        )

    # ── /runaway ──────────────────────────────────────────────────────────

    @app_commands.command(name="runaway", description="🏃 Run away from your family.")
    @app_commands.guild_only()
    async def runaway(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        guild_id = interaction.guild_id
        user     = interaction.user

        parents = await _run_in_thread(db.get_parents, guild_id, user.id)
        if not parents:
            return await interaction.followup.send(
                "🤷 You don't have any family to run away from.", ephemeral=True
            )

        parent_mentions = []
        for pid in parents:
            m = interaction.guild.get_member(pid)
            parent_mentions.append(m.mention if m else f"<@{pid}>")

        view = RunawayView(user)
        confirm_embed = discord.Embed(
            title="🏃 Run Away from Family?",
            description=(
                f"You are about to cut all ties with: {', '.join(parent_mentions)}.\n\n"
                "**This removes your parent connections only.** "
                "Your own children and marriage are not affected."
            ),
            color=0xCC7700,
        )
        msg = await interaction.followup.send(embed=confirm_embed, view=view, ephemeral=True)

        await view.wait()

        if not view.confirmed:
            return await msg.edit(
                embed=discord.Embed(description="✅ Cancelled — you stayed.", color=0x44BB88),
                view=None,
            )

        removed = await _run_in_thread(db.leave_family, guild_id, user.id)
        if removed == 0:
            return await msg.edit(
                embed=discord.Embed(description="🤷 No family links found to remove.", color=0x445566),
                view=None,
            )

        _invalidate_guild_cache(guild_id)
        asyncio.create_task(_rewarm_guild_cache(interaction.guild))
        await msg.edit(
            embed=discord.Embed(description="✅ Done. You ran away.", color=0x666677),
            view=None,
        )
        await interaction.channel.send(
            embed=discord.Embed(
                title="🏃 Someone Ran Away!",
                description=f"{user.mention} has run away from their family! 😱",
                color=0xCC7700,
            )
        )

    # ── /familystats ──────────────────────────────────────────────────────

    @app_commands.command(
        name="familystats",
        description="📊 Family relationship leaderboards for this server.",
    )
    @app_commands.guild_only()
    async def familystats(self, interaction: discord.Interaction):
        await interaction.response.defer()

        guild_id = interaction.guild_id
        longest_couples, top_parents, most_married = await asyncio.gather(
            _run_in_thread(db.get_longest_couples, guild_id),
            _run_in_thread(db.get_top_parents, guild_id),
            _run_in_thread(db.get_most_married, guild_id),
        )

        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]

        embed = discord.Embed(title="📊 Family Leaderboards", color=0xAA88FF)
        embed.set_footer(text="Solace Family Tree")

        # Longest couples
        if longest_couples:
            lines = []
            for i, row in enumerate(longest_couples):
                u1 = interaction.guild.get_member(row["user1_id"])
                u2 = interaction.guild.get_member(row["user2_id"])
                n1 = u1.display_name if u1 else f"User {row['user1_id']}"
                n2 = u2.display_name if u2 else f"User {row['user2_id']}"
                days = row["days"] or 0
                medal = medals[i] if i < len(medals) else "▪️"
                lines.append(f"{medal} **{n1}** × **{n2}** — {days} day{'s' if days != 1 else ''}")
            embed.add_field(name="💑 Longest Couples", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="💑 Longest Couples", value="*No couples yet!*", inline=False)

        # Most children
        if top_parents:
            lines = []
            for i, row in enumerate(top_parents):
                m = interaction.guild.get_member(row["parent_id"])
                name = m.display_name if m else f"User {row['parent_id']}"
                count = row["child_count"]
                medal = medals[i] if i < len(medals) else "▪️"
                lines.append(f"{medal} **{name}** — {count} kid{'s' if count != 1 else ''}")
            embed.add_field(name="👶 Most Children", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="👶 Most Children", value="*No parent-child links yet!*", inline=False)

        # Most married (including past divorces)
        if most_married:
            lines = []
            for i, row in enumerate(most_married):
                m = interaction.guild.get_member(row["user_id"])
                name = m.display_name if m else f"User {row['user_id']}"
                count = row["marriage_count"]
                medal = medals[i] if i < len(medals) else "▪️"
                lines.append(f"{medal} **{name}** — {count} marriage{'s' if count != 1 else ''}")
            embed.add_field(name="💍 Most Married", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="💍 Most Married", value="*No marriages yet!*", inline=False)

        await interaction.followup.send(embed=embed)

    # ── /familyconfig ─────────────────────────────────────────────────────

    familyconfig = app_commands.Group(
        name="familyconfig",
        description="⚙️ Configure family tree settings (Admin only).",
        guild_only=True,
    )

    @familyconfig.command(name="incest", description="Toggle whether relatives can marry or adopt each other.")
    @app_commands.describe(setting="on = allow, off = disallow (default: off)")
    @app_commands.choices(setting=[
        app_commands.Choice(name="on  – allow relatives to marry/adopt", value="on"),
        app_commands.Choice(name="off – disallow (default)",             value="off"),
    ])
    async def familyconfig_incest(self, interaction: discord.Interaction,
                                  setting: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True)

        if not _is_admin(interaction):
            return await interaction.followup.send(
                "❌ You need Administrator permission to change this setting.",
                ephemeral=True,
            )

        allowed = setting.value == "on"
        await _run_in_thread(db.ensure_guild, interaction.guild_id)
        await _run_in_thread(db.set_incest_allowed, interaction.guild_id, allowed)

        if allowed:
            embed = discord.Embed(
                title="⚙️ Incest: Enabled",
                description=(
                    "⚠️ Relatives may now marry and adopt each other on this server.\n"
                    "You can turn this back off with `/familyconfig incest off`."
                ),
                color=0xFF8800,
            )
        else:
            embed = discord.Embed(
                title="⚙️ Incest: Disabled",
                description="✅ Relatives can no longer marry or adopt each other.",
                color=0x44AA66,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# Extension entry point
# ---------------------------------------------------------------------------

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FamilyTree(bot))
    log.info("FamilyTree cog loaded.")
