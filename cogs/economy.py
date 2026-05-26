"""
Economy cog — per-guild wallet system persisted to economy.json.

Supports both standard Unicode emojis and custom Discord server emojis
(e.g. <:solace:123456789>) as the currency symbol.
"""

import json
import pathlib
import random
import re
from datetime import datetime, timezone, timedelta

import discord
from discord import app_commands
from discord.ext import commands

# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

_DATA_PATH = pathlib.Path(__file__).parent.parent / "economy.json"

_DEFAULT: dict = {"config": {}, "balances": {}, "daily": {}}


def _load() -> dict:
    if _DATA_PATH.exists():
        try:
            return json.loads(_DATA_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {k: dict(v) for k, v in _DEFAULT.items()}


def _save(data: dict) -> None:
    _DATA_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Low-level accessors
# ---------------------------------------------------------------------------

def _get_currency(guild_id: int) -> str:
    data = _load()
    return data.get("config", {}).get(str(guild_id), {}).get("currency_emoji", "🪙")


def _set_currency(guild_id: int, emoji: str) -> None:
    data = _load()
    data.setdefault("config", {}).setdefault(str(guild_id), {})["currency_emoji"] = emoji
    _save(data)


def _get_balance(guild_id: int, user_id: int) -> int:
    data = _load()
    return data.get("balances", {}).get(str(guild_id), {}).get(str(user_id), 0)


def _set_balance(guild_id: int, user_id: int, amount: int) -> None:
    data = _load()
    data.setdefault("balances", {}).setdefault(str(guild_id), {})[str(user_id)] = max(0, amount)
    _save(data)


def _get_last_daily(guild_id: int, user_id: int) -> datetime | None:
    data = _load()
    raw = data.get("daily", {}).get(str(guild_id), {}).get(str(user_id))
    if raw:
        return datetime.fromisoformat(raw)
    return None


def _set_last_daily(guild_id: int, user_id: int, when: datetime) -> None:
    data = _load()
    data.setdefault("daily", {}).setdefault(str(guild_id), {})[str(user_id)] = when.isoformat()
    _save(data)


# ---------------------------------------------------------------------------
# Emoji validation helper
# ---------------------------------------------------------------------------

_CUSTOM_EMOJI_RE = re.compile(r"^<a?:[A-Za-z0-9_]+:\d+>$")


def _is_valid_emoji(value: str) -> bool:
    """Accept standard Unicode emoji or a custom Discord emoji mention."""
    return bool(_CUSTOM_EMOJI_RE.match(value.strip())) or len(value.strip()) >= 1


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class Economy(commands.Cog):
    """Wallet, daily rewards, and coin-flip gambling."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------
    # Admin commands
    # ------------------------------------------------------------------

    @app_commands.command(name="setcurrency", description="Set the currency emoji used in all economy commands.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def setcurrency(self, interaction: discord.Interaction, emoji: str) -> None:
        emoji = emoji.strip()
        if not _is_valid_emoji(emoji):
            await interaction.response.send_message("❌ Invalid emoji. Provide a Unicode emoji or a custom server emoji like `<:name:id>`.", ephemeral=True)
            return

        assert interaction.guild_id is not None
        _set_currency(interaction.guild_id, emoji)
        await interaction.response.send_message(
            f"✅ Currency emoji set to **{emoji}**.", ephemeral=True
        )

    @app_commands.command(name="give", description="Add currency to a member's balance.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def give(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1],
    ) -> None:
        assert interaction.guild_id is not None
        cur = _get_currency(interaction.guild_id)
        new_bal = _get_balance(interaction.guild_id, member.id) + amount
        _set_balance(interaction.guild_id, member.id, new_bal)
        await interaction.response.send_message(
            f"✅ Added **{amount} {cur}** to {member.mention}. New balance: **{new_bal} {cur}**."
        )

    @app_commands.command(name="take", description="Remove currency from a member's balance (minimum 0).")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def take(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1],
    ) -> None:
        assert interaction.guild_id is not None
        cur = _get_currency(interaction.guild_id)
        new_bal = max(0, _get_balance(interaction.guild_id, member.id) - amount)
        _set_balance(interaction.guild_id, member.id, new_bal)
        await interaction.response.send_message(
            f"✅ Removed up to **{amount} {cur}** from {member.mention}. New balance: **{new_bal} {cur}**."
        )

    # ------------------------------------------------------------------
    # Wallet commands
    # ------------------------------------------------------------------

    @app_commands.command(name="balance", description="Check your (or another member's) wallet balance.")
    @app_commands.guild_only()
    async def balance(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        assert interaction.guild_id is not None
        target = member or interaction.user
        cur = _get_currency(interaction.guild_id)
        bal = _get_balance(interaction.guild_id, target.id)

        embed = discord.Embed(
            title=f"{target.display_name}'s Wallet",
            description=f"**{bal} {cur}**",
            color=discord.Color.gold(),
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="pay", description="Transfer currency to another member.")
    @app_commands.guild_only()
    async def pay(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: app_commands.Range[int, 1],
    ) -> None:
        assert interaction.guild_id is not None
        if member == interaction.user:
            await interaction.response.send_message("❌ You can't pay yourself.", ephemeral=True)
            return

        cur = _get_currency(interaction.guild_id)
        sender_bal = _get_balance(interaction.guild_id, interaction.user.id)

        if sender_bal < amount:
            await interaction.response.send_message(
                f"❌ Insufficient funds. Your balance: **{sender_bal} {cur}**.", ephemeral=True
            )
            return

        _set_balance(interaction.guild_id, interaction.user.id, sender_bal - amount)
        receiver_bal = _get_balance(interaction.guild_id, member.id) + amount
        _set_balance(interaction.guild_id, member.id, receiver_bal)

        await interaction.response.send_message(
            f"✅ {interaction.user.mention} sent **{amount} {cur}** to {member.mention}."
        )

    # ------------------------------------------------------------------
    # Passive income
    # ------------------------------------------------------------------

    @app_commands.command(name="daily", description="Claim your daily currency reward (once every 24 hours).")
    @app_commands.guild_only()
    async def daily(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        cur = _get_currency(interaction.guild_id)
        now = datetime.now(tz=timezone.utc)
        last = _get_last_daily(interaction.guild_id, interaction.user.id)

        if last is not None:
            # Ensure last is timezone-aware for comparison
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            next_claim = last + timedelta(hours=24)
            if now < next_claim:
                remaining = next_claim - now
                hours, rem = divmod(int(remaining.total_seconds()), 3600)
                minutes = rem // 60
                await interaction.response.send_message(
                    f"⏳ You already claimed your daily reward. Come back in **{hours}h {minutes}m**.",
                    ephemeral=True,
                )
                return

        reward = random.randint(50, 150)
        _set_last_daily(interaction.guild_id, interaction.user.id, now)
        new_bal = _get_balance(interaction.guild_id, interaction.user.id) + reward
        _set_balance(interaction.guild_id, interaction.user.id, new_bal)

        embed = discord.Embed(
            title="🎁 Daily Reward Claimed!",
            description=f"You received **{reward} {cur}**!\nNew balance: **{new_bal} {cur}**",
            color=discord.Color.green(),
        )
        embed.set_footer(text="Come back in 24 hours for your next reward.")
        await interaction.response.send_message(embed=embed)

    # FIX: Changed choice parameter from Choice[str] to plain str,
    # and added @app_commands.choices() decorator instead.
    # Also removed the broken autocomplete method below.
    @app_commands.command(name="coinflip", description="Bet your currency on a coin flip.")
    @app_commands.guild_only()
    @app_commands.choices(choice=[
        app_commands.Choice(name="Heads", value="heads"),
        app_commands.Choice(name="Tails", value="tails"),
    ])
    async def coinflip(
        self,
        interaction: discord.Interaction,
        choice: str,  # FIX: was Choice[str], must be plain str
        bet: app_commands.Range[int, 1],
    ) -> None:
        assert interaction.guild_id is not None
        cur = _get_currency(interaction.guild_id)
        bal = _get_balance(interaction.guild_id, interaction.user.id)

        if bal < bet:
            await interaction.response.send_message(
                f"❌ Insufficient funds. Your balance: **{bal} {cur}**.", ephemeral=True
            )
            return

        result = random.choice(["heads", "tails"])
        won = choice == result  # FIX: was choice.value == result

        if won:
            new_bal = bal + bet
            _set_balance(interaction.guild_id, interaction.user.id, new_bal)
            embed = discord.Embed(
                title="🪙 Coin Flip — You Won!",
                description=(
                    f"The coin landed on **{result}** — you guessed right!\n"
                    f"**+{bet} {cur}** → New balance: **{new_bal} {cur}**"
                ),
                color=discord.Color.green(),
            )
        else:
            new_bal = bal - bet
            _set_balance(interaction.guild_id, interaction.user.id, new_bal)
            embed = discord.Embed(
                title="🪙 Coin Flip — You Lost!",
                description=(
                    f"The coin landed on **{result}** — you guessed wrong.\n"
                    f"**-{bet} {cur}** → New balance: **{new_bal} {cur}**"
                ),
                color=discord.Color.red(),
            )

        embed.set_footer(text=f"Your pick: {choice}")  # FIX: was choice.value
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Economy(bot))
        
