import asyncio
import discord
from discord import app_commands
from discord.ext import commands
import g4f
# Import a highly reliable free provider backend
from g4f.Provider import ChatGptEs 

_SYSTEM_PROMPT = (
    "You are a razor-sharp roast comedian who specialises in gaming burns. "
    "Your style: open with one savage, clever dig at the target's gaming skills or habits, "
    "then immediately pivot into one over-the-top, smooth, genuinely funny flirty line aimed at them. "
    "Rules: exactly 2-3 sentences total, SFW playful banter only, no hate speech, "
    "no slurs, no toxic harassment. Always mention the target's name naturally inside the text."
)

class Fun(commands.Cog):
    """Fun commands — roasts and more."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="roast",
        description="Hit someone with a savage gaming burn that flips into a smooth flirty line.",
    )
    @app_commands.guild_only()
    async def roast(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        await interaction.response.defer(thinking=True)

        name = member.display_name
        user_prompt = (
            f"Roast the Discord gamer named '{name}'. "
            "Start with a clever, savage burn about their gaming skills or typical gamer behaviour, "
            "then pivot into an over-the-top smooth and funny flirty line — all in 2-3 sentences. "
            f"Make sure '{name}' is mentioned naturally in the roast itself."
        )

        def _call_g4f() -> str:
            # Switched to gpt_4o and forced the ChatGptEs provider for stability
            response = g4f.ChatCompletion.create(
                model=g4f.models.gpt_4o,
                provider=ChatGptEs,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user",   "content": user_prompt},
                ],
            )
            return response if isinstance(response, str) else str(response)

        try:
            text = await asyncio.to_thread(_call_g4f)
            text = text.strip() or "The roast generator blanked — must be too busy admiring you. 😏"
            await interaction.followup.send(f"🔥 {text}")
        except Exception:
            await interaction.followup.send(
                "❌ Roast machine jammed. Try again in a moment.", ephemeral=True
            )

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Fun(bot))
    
