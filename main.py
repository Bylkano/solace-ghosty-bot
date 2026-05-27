"""
Solace Money Heist — Interactive Team Event Bot
===============================================
A self-contained single-file Discord bot for a live heist event.

Quick setup:
  1. Set DISCORD_TOKEN (or BOT_TOKEN) in environment / Replit secrets.
  2. Run the bot, then use admin setup commands:
       !seteventchannel #channel   — where EMP strikes are announced publicly
       !setleakchannel #channel    — where encrypted data packets are broadcast
       !setteamchannel "Team Name" #channel — set each team's private HQ channel
  3. Deploy the Black Market console with !spawnhub.
  4. Start the leak loop manually with !startleaks (or wait — it fires every 30 min).
"""

import os
import time
import random
import threading

import discord
from discord.ext import commands, tasks
from flask import Flask

# ===========================================================================
# KEEP-ALIVE WEB SERVER (Render / Replit health checks)
# ===========================================================================

_app = Flask(__name__)

@_app.route("/")
def _alive():
    return "I am alive", 200

threading.Thread(
    target=lambda: _app.run(host="0.0.0.0", port=10000), daemon=True
).start()

# ===========================================================================
# DYNAMIC CONFIGURATION (set at runtime via admin commands)
# ===========================================================================

MAIN_EVENT_CHANNEL_ID: int = 0   # !seteventchannel
LEAK_CHANNEL_ID:       int = 0   # !setleakchannel

# ===========================================================================
# IN-MEMORY STATE
# ===========================================================================

TEAM_DATA: dict[str, dict] = {
    "Red Reapers": {
        "balance":        5000,
        "wiretap_active": False,
        "emp_cooldown":   0,      # Unix timestamp — when they can EMP again
        "is_scrambled":   False,  # True when hit by an enemy EMP
        "channel_id":     0,      # Private HQ text channel ID
    },
    "Shadow Vipers": {
        "balance": 5000, "wiretap_active": False,
        "emp_cooldown": 0, "is_scrambled": False, "channel_id": 0,
    },
    "Phantom Thieves": {
        "balance": 5000, "wiretap_active": False,
        "emp_cooldown": 0, "is_scrambled": False, "channel_id": 0,
    },
    "Cyber Ghosts": {
        "balance": 5000, "wiretap_active": False,
        "emp_cooldown": 0, "is_scrambled": False, "channel_id": 0,
    },
}

# 6 cryptographic / logic puzzles (answers stored lowercase for case-insensitive matching)
DATA_PACKETS: list[dict] = [
    {
        "id": 1,
        "encrypted": "47 68 6F 73 74 79",
        "decrypted": "ghosty",
        "clue": (
            "🔍 **Wiretap Intel — Packet #1**\n"
            "The payload is ASCII hexadecimal. Each two-character byte maps directly to one letter."
        ),
    },
    {
        "id": 2,
        "encrypted": (
            "01010011 01001111 01001100 01000001 01000011 01000101"
        ),
        "decrypted": "solace",
        "clue": (
            "🔍 **Wiretap Intel — Packet #2**\n"
            "Binary transmission detected. Convert each 8-bit cluster to its ASCII character."
        ),
    },
    {
        "id": 3,
        "encrypted": "VNAGERZ",
        "decrypted": "phantom",
        # ROT13: P→C, H→U, A→N, N→A, T→G, O→B, M→Z → CUNAGBZ
        # Wait, let me verify PHANTOM in ROT13:
        # P(16)+13=29→3=D? No. ROT13 works on A-Z:
        # P→C? No: P is the 16th letter. 16+13=29, 29-26=3 → C. Hmm that gives CUNAGBZ not VNAGERZ
        # Let me recalculate: P=15(0-indexed)+13=28, 28%26=2→C
        # H=7+13=20→U, A=0+13=13→N, N=13+13=26%26=0→A, T=19+13=32%26=6→G, O=14+13=27%26=1→B, M=12+13=25→Z
        # PHANTOM in ROT13 = CUNAGBZ
        # But I wrote VNAGERZ. Let me fix: what decodes to "phantom" via ROT13?
        # ROT13 is its own inverse, so ROT13("phantom") = ROT13 of each letter:
        # p(0-idx=15)+13=28%26=2→c, h(7)+13=20→u, a(0)+13=13→n, n(13)+13=0→a, t(19)+13=6→g, o(14)+13=1→b, m(12)+13=25→z
        # So ROT13("phantom") = "cunagbz"
        # I need to fix this. Let me use a different word.
        # Let's use "viper": v(21)+13=34%26=8→i, i(8)+13=21→v, p(15)+13=2→c, e(4)+13=17→r, r(17)+13=4→e
        # ROT13("viper") = "ivcre" ✓ (this matches what I had in the original economy puzzles)
        # Let me use "cipher" as the answer:
        # c(2)+13=15→p, i(8)+13=21→v, p(15)+13=2→c, h(7)+13=20→u, e(4)+13=17→r, r(17)+13=4→e
        # ROT13("cipher") = "pvcure"
        # Or let me just use "shadow":
        # s(18)+13=5→f, h(7)+13=20→u, a(0)+13=13→n, d(3)+13=16→q, o(14)+13=1→b, w(22)+13=9→j
        # ROT13("shadow") = "funqbj"  
        # Actually VNAGERZ decodes via ROT13 to:
        # V(21)+13=34%26=8→I, N(13)+13=0→A, A(0)+13=13→N, G(6)+13=19→T, E(4)+13=17→R, R(17)+13=4→E, Z(25)+13=12→M
        # VNAGERZ → IANTREM? No: I, A, N, T, R, E, M → "iantrem"? That's not right.
        # Let me just verify: ROT13 of "phantom":
        # Actually I realize I need to fix this. Let me just change the puzzle to use "cunagbz" which decodes to "phantom"
        "clue": (
            "🔍 **Wiretap Intel — Packet #3**\n"
            "ROT13 cipher detected. Each letter has been rotated by exactly 13 positions."
        ),
    },
    {
        "id": 4,
        "encrypted": "thgieH eht ot emocleW",
        "decrypted": "welcome to the heist",
        "clue": (
            "🔍 **Wiretap Intel — Packet #4**\n"
            "Signal arrived mirrored. Reverse the entire string to reveal the message."
        ),
    },
    {
        "id": 5,
        "encrypted": "KHOOR VKDGRZ YLSHUV",
        "decrypted": "hello shadow vipers",
        # Caesar +3: K-3=H, H-3=E, O-3=L, O-3=L, R-3=O → HELLO
        # V-3=S, K-3=H, D-3=A, G-3=D, R-3=O, Z-3=W → SHADOW
        # Y-3=V, L-3=I, S-3=P, H-3=E, U-3=R, V-3=S → VIPERS ✓
        "clue": (
            "🔍 **Wiretap Intel — Packet #5**\n"
            "Caesar cipher shift detected. Each letter has been shifted forward by 3. Shift it back."
        ),
    },
    {
        "id": 6,
        "encrypted": (
            "I have cities, but no houses live there.\n"
            "I have mountains, but no trees grow there.\n"
            "I have water, but no fish swim there.\n"
            "I have roads, but no cars drive there.\n"
            "What am I?"
        ),
        "decrypted": "a map",
        "clue": (
            "🔍 **Wiretap Intel — Packet #6**\n"
            "Logic intercept. Think about what represents geography without being geography itself."
        ),
    },
]

# Fix packet 3: ROT13("phantom") = "cunagbz"
DATA_PACKETS[2]["encrypted"] = "CUNAGBZ"

ACTIVE_LEAK:  dict | None = None   # Currently broadcast puzzle (None = no active leak)
AUDIT_LOG:    list[str]   = []     # Recent action history (max 25)

# ===========================================================================
# HELPERS
# ===========================================================================

def get_team(member: discord.Member) -> str | None:
    """Return the team name for a member based on their server roles."""
    role_names = {role.name for role in member.roles}
    for team in TEAM_DATA:
        if team in role_names:
            return team
    return None


def fmt_cooldown(ts: float) -> str:
    """Return a human-readable EMP cooldown string."""
    remaining = ts - time.time()
    if remaining <= 0:
        return "✅ Ready"
    m, s = divmod(int(remaining), 60)
    return f"⏳ {m}m {s}s"


def log(entry: str) -> None:
    """Append a timestamped line to the audit log (cap at 25)."""
    stamp = discord.utils.utcnow().strftime("%H:%M:%S UTC")
    AUDIT_LOG.append(f"`[{stamp}]` {entry}")
    if len(AUDIT_LOG) > 25:
        AUDIT_LOG.pop(0)


# ===========================================================================
# BOT
# ===========================================================================

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ===========================================================================
# UI — EMP TARGET SELECT
# ===========================================================================

class EMPTargetSelect(discord.ui.Select):
    def __init__(self, buyer_team: str) -> None:
        self.buyer_team = buyer_team
        options = [
            discord.SelectOption(label=name, description=f"Launch EMP pulse at {name}", emoji="🎯")
            for name in TEAM_DATA if name != buyer_team
        ]
        super().__init__(
            placeholder="🎯 Select a target team…",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        buyer = get_team(interaction.user)
        if buyer != self.buyer_team:
            await interaction.response.send_message("❌ Team mismatch — access denied.", ephemeral=True)
            return

        target      = self.values[0]
        buyer_data  = TEAM_DATA[buyer]
        target_data = TEAM_DATA[target]

        # Re-validate (state may have changed since button was pressed)
        if buyer_data["balance"] < 750:
            await interaction.response.send_message("❌ Insufficient funds — need **750 coins**.", ephemeral=True)
            return
        if time.time() < buyer_data["emp_cooldown"]:
            await interaction.response.send_message(
                f"❌ EMP still recharging — **{fmt_cooldown(buyer_data['emp_cooldown'])}** left.", ephemeral=True
            )
            return
        if target_data["is_scrambled"]:
            await interaction.response.send_message(f"❌ **{target}** is already scrambled.", ephemeral=True)
            return

        # Execute strike
        buyer_data["balance"]    -= 750
        buyer_data["emp_cooldown"] = time.time() + 900   # 15-minute cooldown
        target_data["is_scrambled"]   = True
        target_data["wiretap_active"] = False            # Sever wiretap

        log(f"⚡ **{buyer}** deployed EMP on **{target}** (750 coins deducted)")

        await interaction.response.send_message(
            f"⚡ EMP launched at **{target}**! Their network is down.", ephemeral=True
        )

        # Public announcement
        event_ch = bot.get_channel(MAIN_EVENT_CHANNEL_ID)
        if event_ch:
            embed = discord.Embed(
                title="⚡ EMP STRIKE DETECTED ON THE NETWORK",
                description=(
                    f"**{buyer}** has detonated an electromagnetic pulse targeting **{target}**!\n\n"
                    f"🔴 Target systems: **OFFLINE**\n"
                    f"📡 Wiretap feed: **SEVERED**\n"
                    f"⚠️ {target} cannot submit decrypt attempts until the next successful solve."
                ),
                color=discord.Color.orange(),
            )
            await event_ch.send(embed=embed)

        # Warning to target's HQ channel
        target_ch = bot.get_channel(target_data["channel_id"])
        if target_ch:
            warn = discord.Embed(
                title="🚨 INCOMING — SYSTEM COMPROMISED",
                description=(
                    "Your network has been hit by an EMP strike.\n\n"
                    "• 📡 Wiretap severed\n"
                    "• 🔐 Decryption terminal locked\n\n"
                    "Your systems will be restored the moment **any** team cracks the active data leak."
                ),
                color=discord.Color.red(),
            )
            await target_ch.send(embed=warn)

        self.view.stop()


class EMPTargetView(discord.ui.View):
    def __init__(self, buyer_team: str) -> None:
        super().__init__(timeout=60)
        self.add_item(EMPTargetSelect(buyer_team))


# ===========================================================================
# UI — MAIN ESPIONAGE HUB
# ===========================================================================

class EspionageHubView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)   # Persistent across restarts

    # ---- Button 1: Wiretap ------------------------------------------------

    @discord.ui.button(
        label="📡 Buy Team Wiretap (400 Coins)",
        style=discord.ButtonStyle.blurple,
        custom_id="heist_wiretap",
    )
    async def wiretap_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        team = get_team(interaction.user)
        if not team:
            await interaction.response.send_message("❌ No team role detected.", ephemeral=True)
            return

        d = TEAM_DATA[team]

        if d["is_scrambled"]:
            await interaction.response.send_message(
                "❌ Your systems are EMP-scrambled. You cannot buy a wiretap until the scramble is lifted.", ephemeral=True
            )
            return
        if d["wiretap_active"]:
            await interaction.response.send_message("✅ Your team's wiretap is already active.", ephemeral=True)
            return
        if d["balance"] < 400:
            await interaction.response.send_message(
                f"❌ Need **400 coins**, your team has **{d['balance']}**.", ephemeral=True
            )
            return

        d["balance"]       -= 400
        d["wiretap_active"] = True
        log(f"📡 **{team}** bought a wiretap (400 coins deducted)")

        await interaction.response.send_message(
            "📡 Wiretap purchased! You'll receive intelligence clues during the next data leak.", ephemeral=True
        )

        # Confirm in team HQ
        ch = bot.get_channel(d["channel_id"])
        if ch:
            embed = discord.Embed(
                title="📡 WIRETAP HARDWARE — ONLINE",
                description=(
                    "Your team's tap is live and monitoring the network.\n"
                    "When the next data leak is broadcast, your HQ will receive an exclusive intelligence clue."
                ),
                color=discord.Color.blurple(),
            )
            embed.set_footer(text="Stay sharp — your edge is active.")
            await ch.send(embed=embed)

    # ---- Button 2: EMP Sabotage -------------------------------------------

    @discord.ui.button(
        label="⚡ Deploy EMP Sabotage (750 Coins)",
        style=discord.ButtonStyle.danger,
        custom_id="heist_emp",
    )
    async def emp_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        team = get_team(interaction.user)
        if not team:
            await interaction.response.send_message("❌ No team role detected.", ephemeral=True)
            return

        d = TEAM_DATA[team]

        if d["balance"] < 750:
            await interaction.response.send_message(
                f"❌ Need **750 coins**, your team has **{d['balance']}**.", ephemeral=True
            )
            return
        if time.time() < d["emp_cooldown"]:
            await interaction.response.send_message(
                f"❌ EMP recharging — **{fmt_cooldown(d['emp_cooldown'])}** remaining.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            "🎯 Choose your target:", view=EMPTargetView(buyer_team=team), ephemeral=True
        )

    # ---- Button 3: Status Dashboard ---------------------------------------

    @discord.ui.button(
        label="📊 Server Status Dashboard",
        style=discord.ButtonStyle.secondary,
        custom_id="heist_status",
    )
    async def status_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        embed = discord.Embed(
            title="📊 LIVE NETWORK STATUS DASHBOARD",
            description="Real-time intelligence on all operative teams.",
            color=discord.Color.dark_gray(),
        )
        for name, d in TEAM_DATA.items():
            status   = "⚡ SYSTEM COMPROMISED" if d["is_scrambled"]   else "🟢 Operational"
            wiretap  = "🟢 Online"             if d["wiretap_active"] else "⭕ Offline"
            cooldown = fmt_cooldown(d["emp_cooldown"])
            embed.add_field(
                name=f"▸ {name}",
                value=(
                    f"💰 Balance: **{d['balance']} coins**\n"
                    f"📡 Wiretap: {wiretap}\n"
                    f"⚡ EMP Cooldown: {cooldown}\n"
                    f"🔌 Network: {status}"
                ),
                inline=True,
            )
        active_str = f"🔓 Packet #{ACTIVE_LEAK['id']} — ACTIVE" if ACTIVE_LEAK else "✅ No active leak"
        embed.set_footer(text=f"Data Leak: {active_str}")
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ===========================================================================
# BACKGROUND TASK — Data Leak Loop (every 30 minutes)
# ===========================================================================

@tasks.loop(minutes=30)
async def data_leak_loop() -> None:
    global ACTIVE_LEAK
    if ACTIVE_LEAK is not None:
        return   # Current leak still unsolved — skip this cycle

    packet     = random.choice(DATA_PACKETS)
    ACTIVE_LEAK = packet
    log(f"🔓 Data leak initiated — Packet #{packet['id']}")

    # Public broadcast
    leak_ch = bot.get_channel(LEAK_CHANNEL_ID)
    if leak_ch:
        embed = discord.Embed(
            title="🔓 ⚠️  ENCRYPTED DATA PACKET INTERCEPTED  ⚠️",
            description=(
                "An encrypted payload has been detected crossing the network.\n"
                "**First team to decrypt it claims 1,000 coins.**\n"
                "Solve it without a wiretap for an extra **+250 coin bonus**.\n\n"
                f"```\n{packet['encrypted']}\n```"
            ),
            color=discord.Color.dark_red(),
        )
        embed.set_footer(text="Submit your answer with !decrypt [answer] • Wrong guesses cost 150 coins")
        await leak_ch.send(embed=embed)

    # Send clues to teams with an active, unscrambled wiretap
    for team_name, d in TEAM_DATA.items():
        if d["wiretap_active"] and not d["is_scrambled"]:
            ch = bot.get_channel(d["channel_id"])
            if ch:
                clue_embed = discord.Embed(
                    title="📡 WIRETAP — INTELLIGENCE PACKET RECEIVED",
                    description=packet["clue"],
                    color=discord.Color.blurple(),
                )
                clue_embed.set_footer(text="Advantage is yours — use it.")
                await ch.send(embed=clue_embed)


@data_leak_loop.before_loop
async def _before_loop() -> None:
    await bot.wait_until_ready()


# ===========================================================================
# EVENTS
# ===========================================================================

@bot.event
async def on_ready() -> None:
    bot.add_view(EspionageHubView())   # Re-register persistent view after restart
    if not data_leak_loop.is_running():
        data_leak_loop.start()
    print(f"✅ Online as {bot.user} — Solace Money Heist ready.")


# ===========================================================================
# COMMANDS — Admin Setup
# ===========================================================================

@bot.command(name="seteventchannel")
@commands.has_permissions(administrator=True)
async def set_event_channel(ctx: commands.Context, channel: discord.TextChannel) -> None:
    """Set the public channel for EMP announcements."""
    global MAIN_EVENT_CHANNEL_ID
    MAIN_EVENT_CHANNEL_ID = channel.id
    log(f"⚙️ Event channel set to #{channel.name} by {ctx.author.display_name}")
    await ctx.reply(f"✅ Event channel set to {channel.mention}.")


@bot.command(name="setleakchannel")
@commands.has_permissions(administrator=True)
async def set_leak_channel(ctx: commands.Context, channel: discord.TextChannel) -> None:
    """Set the channel where data leaks are broadcast."""
    global LEAK_CHANNEL_ID
    LEAK_CHANNEL_ID = channel.id
    log(f"⚙️ Leak channel set to #{channel.name} by {ctx.author.display_name}")
    await ctx.reply(f"✅ Leak channel set to {channel.mention}.")


@bot.command(name="setteamchannel")
@commands.has_permissions(administrator=True)
async def set_team_channel(ctx: commands.Context, *, args: str) -> None:
    """Set a team's private HQ channel.
    Usage: !setteamchannel Red Reapers #channel
    """
    parts = args.rsplit(" ", 1)
    if len(parts) != 2:
        await ctx.reply("Usage: `!setteamchannel <Team Name> #channel`")
        return

    team_name = parts[0].strip()
    raw_ch    = parts[1].strip()

    if team_name not in TEAM_DATA:
        await ctx.reply(f"❌ Unknown team `{team_name}`. Valid: {', '.join(f'`{t}`' for t in TEAM_DATA)}")
        return

    channel_id = int(raw_ch.strip("<#>"))
    channel    = ctx.guild.get_channel(channel_id)
    if not channel:
        await ctx.reply("❌ Channel not found in this server.")
        return

    TEAM_DATA[team_name]["channel_id"] = channel_id
    log(f"⚙️ **{team_name}** HQ → #{channel.name}")
    await ctx.reply(f"✅ **{team_name}** HQ set to {channel.mention}.")


# ===========================================================================
# COMMANDS — Game
# ===========================================================================

@bot.command(name="spawnhub")
@commands.has_permissions(administrator=True)
async def spawnhub(ctx: commands.Context) -> None:
    """Deploy the Black Market Espionage & Sabotage Console."""
    await ctx.message.delete()
    embed = discord.Embed(
        title="🕶️  BLACK MARKET ESPIONAGE & SABOTAGE CONSOLE",
        description=(
            "Welcome to the underground network.\n\n"
            "**📡 Buy Team Wiretap** — `400 coins`\n"
            "Activate your team's hardware tap. Receive exclusive intelligence clues "
            "delivered straight to your HQ the moment a data leak goes live.\n\n"
            "**⚡ Deploy EMP Sabotage** — `750 coins`\n"
            "Launch an electromagnetic pulse at a rival team. Scrambles their network, "
            "severs their wiretap, and blocks their decryption terminal for the current leak. "
            "15-minute cooldown after use.\n\n"
            "**📊 Server Status Dashboard** — `Free`\n"
            "Live readout of every team's balance, wiretap status, EMP cooldown, and network health."
        ),
        color=discord.Color.from_rgb(20, 20, 40),
    )
    embed.set_footer(text="Solace Money Heist • All operations are logged • Choose wisely.")
    await ctx.send(embed=embed, view=EspionageHubView())


@bot.command(name="decrypt")
async def decrypt_cmd(ctx: commands.Context, *, guess: str = "") -> None:
    """Submit a decryption attempt for the active data leak."""
    global ACTIVE_LEAK

    if not ACTIVE_LEAK:
        await ctx.reply("❌ No data leak is currently active. Wait for the next broadcast.")
        return

    team = get_team(ctx.author)
    if not team:
        await ctx.reply("❌ You must belong to a team to submit a decryption attempt.")
        return

    d = TEAM_DATA[team]

    if d["is_scrambled"]:
        await ctx.reply(
            "❌ **COMMUNICATION LINES DOWN** — Your team's network has been scrambled by an EMP strike. "
            "You cannot submit decrypt attempts until the scramble is lifted."
        )
        return

    if not guess.strip():
        await ctx.reply("Usage: `!decrypt [your answer]`")
        return

    submitted = guess.strip().lower()
    correct   = ACTIVE_LEAK["decrypted"].strip().lower()

    if submitted == correct:
        bonus     = 250 if not d["wiretap_active"] else 0
        prize     = 1000 + bonus
        packet_id = ACTIVE_LEAK["id"]

        d["balance"] += prize
        log(
            f"✅ **{team}** cracked Packet #{packet_id} (+{prize} coins) "
            f"— solver: {ctx.author.display_name}"
        )

        # Clear all wiretaps and EMP scrambles server-wide
        for t in TEAM_DATA.values():
            t["wiretap_active"] = False
            t["is_scrambled"]   = False

        ACTIVE_LEAK = None

        embed = discord.Embed(
            title="✅  DATA PACKET DECRYPTED",
            description=(
                f"**{ctx.author.display_name}** of **{team}** has cracked Packet #{packet_id}!\n\n"
                f"💰 Prize: **{1000:,} coins**"
                + (f"\n🎯 Bonus: **+{bonus} coins** *(no wiretap — pure skill!)*" if bonus else "") +
                f"\n\n🌐 All wiretaps severed — EMP effects cleared across the network."
            ),
            color=discord.Color.green(),
        )
        embed.set_footer(text=f"New balance for {team}: {d['balance']:,} coins")
        await ctx.send(embed=embed)

    else:
        penalty        = min(150, d["balance"])   # Can't go below 0
        d["balance"]   = max(0, d["balance"] - 150)
        log(f"❌ **{team}** wrong guess (−{penalty} coins) — user: {ctx.author.display_name}")
        await ctx.reply(
            f"❌ Incorrect decryption. **{penalty} coins** deducted from **{team}**.\n"
            f"Current balance: **{d['balance']:,} coins**."
        )


@bot.command(name="startleaks")
@commands.has_permissions(administrator=True)
async def start_leaks(ctx: commands.Context) -> None:
    """Manually trigger the first data leak immediately and start the loop."""
    global ACTIVE_LEAK
    if ACTIVE_LEAK:
        await ctx.reply("⚠️ A leak is already active. Solve it first or use `!admin_clear`.")
        return
    if not LEAK_CHANNEL_ID:
        await ctx.reply("❌ Set the leak channel first with `!setleakchannel #channel`.")
        return
    # Fire immediately
    await data_leak_loop()
    await ctx.reply("🔓 Data leak initiated manually.")


@bot.command(name="leaderboard")
async def leaderboard(ctx: commands.Context) -> None:
    """Display team balances ranked from highest to lowest."""
    ranked = sorted(TEAM_DATA.items(), key=lambda x: x[1]["balance"], reverse=True)
    medals = ["🥇", "🥈", "🥉", "4️⃣"]
    embed  = discord.Embed(
        title="🏆  SOLACE MONEY HEIST — LEADERBOARD",
        color=discord.Color.gold(),
    )
    for i, (name, d) in enumerate(ranked):
        embed.add_field(
            name=f"{medals[i]} {name}",
            value=f"**{d['balance']:,} coins**",
            inline=False,
        )
    await ctx.send(embed=embed)


@bot.command(name="audit")
async def audit_cmd(ctx: commands.Context) -> None:
    """Display the recent operations audit log."""
    if not AUDIT_LOG:
        await ctx.send("📋 Audit log is empty.")
        return
    embed = discord.Embed(
        title="📋  OPERATIONS AUDIT LOG",
        description="\n".join(reversed(AUDIT_LOG)),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text=f"Showing last {len(AUDIT_LOG)} entries (most recent first)")
    await ctx.send(embed=embed)


@bot.command(name="admin_clear")
@commands.has_permissions(administrator=True)
async def admin_clear(ctx: commands.Context) -> None:
    """Emergency reset — wipes the active leak, all EMP cooldowns, and all debuffs."""
    global ACTIVE_LEAK
    ACTIVE_LEAK = None
    for d in TEAM_DATA.values():
        d["emp_cooldown"]   = 0
        d["is_scrambled"]   = False
        d["wiretap_active"] = False
    log(f"🛡️ Emergency reset by **{ctx.author.display_name}**")
    await ctx.reply(
        "🛡️ **Emergency reset complete.**\n"
        "• Active leak cleared\n"
        "• All EMP cooldowns reset\n"
        "• All scrambles and wiretaps removed"
    )


# ===========================================================================
# ERROR HANDLER
# ===========================================================================

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply("❌ You don't have permission to use this command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(f"❌ Missing argument: `{error.param.name}`. Check the command usage.")
    elif isinstance(error, commands.CommandNotFound):
        pass   # Silently ignore unknown commands
    else:
        await ctx.reply(f"❌ Unexpected error: `{error}`")
        raise error


# ===========================================================================
# RUN
# ===========================================================================

_token = os.environ.get("DISCORD_TOKEN") or os.environ.get("BOT_TOKEN", "")
if not _token:
    raise ValueError("Set DISCORD_TOKEN (or BOT_TOKEN) in your environment.")

bot.run(_token)
