import os
from dotenv import load_dotenv

load_dotenv()

# Accepts DISCORD_TOKEN (standard for most hosting platforms) with a
# fallback to BOT_TOKEN so existing Replit secret configs keep working.
BOT_TOKEN: str = os.getenv("DISCORD_TOKEN") or os.getenv("BOT_TOKEN", "")
BOT_PREFIX: str = os.getenv("BOT_PREFIX", "!")
OWNER_ID: int = int(os.getenv("OWNER_ID", "0"))
DEV_GUILD_ID: int | None = int(guild_id) if (guild_id := os.getenv("DEV_GUILD_ID")) else None

# Database
DATABASE_URL: str = os.getenv("DATABASE_URL", "")

# AI backends — 8-backend fallback chain (all free)
# Priority: Groq → Gemini → Cerebras → SambaNova → Together → OpenRouter → Mistral → NVIDIA
GROQ_API_KEY: str      = os.getenv("GROQ_API_KEY", "")
GEMINI_API_KEY: str    = os.getenv("GEMINI_API_KEY", "")
CEREBRAS_API_KEY: str  = os.getenv("CEREBRAS_API_KEY", "")
SAMBANOVA_API_KEY: str = os.getenv("SAMBANOVA_API_KEY", "")
TOGETHER_API_KEY: str  = os.getenv("TOGETHER_API_KEY", "")
OPENROUTER_API_KEY: str= os.getenv("OPENROUTER_API_KEY", "")
MISTRAL_API_KEY: str   = os.getenv("MISTRAL_API_KEY", "")
NVIDIA_API_KEY: str    = os.getenv("NVIDIA_API_KEY", "")

if not BOT_TOKEN:
    raise ValueError(
        "No token found. Set DISCORD_TOKEN (or BOT_TOKEN) in your environment or .env file."
    )

if not DATABASE_URL:
    raise ValueError(
        "DATABASE_URL is required. Set it in your environment or .env file."
    )
