"""
dna_test.py – DNA Test System for Solace Bot
=============================================
Drop into cogs/ and add "cogs.dna_test" to COGS in bot.py.

Command
-------
  /dna [@user]  – Generate a humorous fictional DNA breakdown.
"""

from __future__ import annotations

import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import discord
from discord import app_commands
from discord.ext import commands

# ── Trait definitions ─────────────────────────────────────────────────────────

TRAITS: list[tuple[str, str]] = [
    ("Dragon",      "🐉"),
    ("Pizza",       "🍕"),
    ("Wi-Fi Signal","📶"),
    ("Cat",         "🐱"),
    ("Potato",      "🥔"),
    ("Goblin",      "👺"),
    ("Programmer",  "💻"),
    ("Meme Lord",   "😂"),
    ("Human",       "🧍"),
    ("Coffee",      "☕"),
]

# Conclusions keyed by dominant trait name
CONCLUSIONS: dict[str, list[str]] = {
    "Dragon": [
        "Highly dangerous near keyboards and treasure vaults. Do not approach without snacks.",
        "Fire breath confirmed. Please do not provoke before morning coffee.",
        "Scales detected on elbows. Hoard of snacks found under the desk.",
    ],
    "Pizza": [
        "Warning: causes severe hunger in a 10-metre radius.",
        "Best served hot. Terrible in any other context.",
        "90% cheese composition confirmed. Scientifically impressive.",
    ],
    "Wi-Fi Signal": [
        "Drops connection mid-sentence. Works perfectly at the neighbour's house.",
        "5G DNA strands detected. Conspiracy theorists are alarmed.",
        "Signal strength: two bars inside their own home. Classic.",
    ],
    "Cat": [
        "Knocks things off tables for no reason. Judges you silently. Offers no apology.",
        "Ignores all commands but demands attention at 3 AM. Standard.",
        "Technically domesticated. Spiritually feral.",
    ],
    "Potato": [
        "Starchy core confirmed. Thrives underground. Rarely appreciated.",
        "Vitamin-rich and surprisingly versatile. More depth than expected.",
        "May be baked, fried, or mashed. Please handle with care.",
    ],
    "Goblin": [
        "Hoards shiny objects. Speaks in riddles when cornered. Nocturnal.",
        "Do not look directly at their Steam library. You will lose hours.",
        "Chaotic good alignment confirmed. Or chaotic neutral. Hard to say.",
    ],
    "Programmer": [
        "Bleeds coffee. Communicates exclusively through Stack Overflow links.",
        "404: social skills not found. 200: snacks fully loaded.",
        "Talks to computers more than people. The computers respond better.",
    ],
    "Meme Lord": [
        "References memes from 2014. Laughs at their own jokes. Irony levels critical.",
        "Every situation has a relevant GIF ready. This is both a gift and a curse.",
        "Archaeologist of the internet. Preserving culture nobody asked to keep.",
    ],
    "Human": [
        "Surprisingly functional for a basic model. Standard issue.",
        "Human DNA detected. Minimal upgrades applied. Warranty expired.",
        "Classified as Homo sapiens. Scientists remain skeptical.",
    ],
    "Coffee": [
        "Unable to function before caffeine intake. Decaf is a personal insult.",
        "Espresso detected in the bloodstream. Highly efficient energy source.",
        "Runs on bean juice and stubbornness. A tale as old as time.",
    ],
}

# Fallback conclusions if dominant trait not found
FALLBACK_CONCLUSIONS: list[str] = [
    "A truly unique genetic cocktail. Scientists are baffled.",
    "Results inconclusive. Please consume more pizza and retry.",
    "Nature vs nurture? In this case: chaos vs chaos.",
    "The universe created this being on purpose. Apparently.",
    "Science has no words. We're just going to nod and move on.",
]


def _generate_dna() -> list[tuple[str, str, int]]:
    """
    Pick 3–5 random traits and assign integer percentages summing to 100.
    Returns list of (name, emoji, percentage) sorted by percentage descending.
    """
    count = random.randint(3, 5)
    selected = random.sample(TRAITS, count)

    # Generate random integer percentages that sum to exactly 100
    # using the "random cuts" method
    cuts = sorted(random.sample(range(1, 100), count - 1))
    boundaries = [0] + cuts + [100]
    percentages = [boundaries[i + 1] - boundaries[i] for i in range(count)]

    # Ensure no 0% traits (rare edge case with few cuts)
    for i, p in enumerate(percentages):
        if p == 0:
            percentages[i] = 1
            percentages[percentages.index(max(percentages))] -= 1

    result = [(name, emoji, pct) for (name, emoji), pct in zip(selected, percentages)]
    result.sort(key=lambda x: x[2], reverse=True)
    return result


def _dna_bar(pct: int) -> str:
    """Render a small visual bar for a percentage."""
    filled = round(pct / 10)
    return "█" * filled + "░" * (10 - filled)


# ── Cog ───────────────────────────────────────────────────────────────────────

class DNATest(commands.Cog):
    """🧬 DNA Test – humorous fictional genetic analysis."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /dna ──────────────────────────────────────────────────────────────────

    @app_commands.command(name="dna", description="🧬 Run a DNA analysis on yourself or someone else.")
    @app_commands.describe(member="Who to analyse (leave blank for yourself).")
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 60.0, key=lambda i: (i.guild_id, i.user.id))
    async def dna(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        await interaction.response.defer()

        target = member or interaction.user
        traits = _generate_dna()
        dominant_name = traits[0][0]

        # Build trait lines
        trait_lines = "\n".join(
            f"`{_dna_bar(pct)}` **{pct}%** {emoji} {name}"
            for name, emoji, pct in traits
        )

        conclusion = random.choice(
            CONCLUSIONS.get(dominant_name, FALLBACK_CONCLUSIONS)
        )

        embed = discord.Embed(
            title=f"🧬 DNA Analysis for {target.display_name}",
            description=trait_lines,
            color=discord.Color.from_rgb(114, 137, 218),
        )
        embed.add_field(
            name="🔬 Scientific Conclusion",
            value=f"*{conclusion}*",
            inline=False,
        )
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text="Certified by the Solace Institute of Made-Up Genetics™")

        await interaction.followup.send(embed=embed)

    # ── Error handler ─────────────────────────────────────────────────────────

    @dna.error
    async def dna_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"⏳ The lab is still processing your last sample. Try again in "
                f"**{error.retry_after:.0f}s**.",
                ephemeral=True,
            )
        else:
            raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DNATest(bot))
