import datetime
import random
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import discord
from discord import app_commands
from discord.ext import commands

from store import get_automod_channel

TIMEOUT_DM_MESSAGES: list[str] = [
    "Hey cutie~ Ghosty belongs to Solace, not your inventory. Try again in a minute 💜",
    "Aww, were you trying to snag Ghosty? Bold move, but Ghosty's already spoken for by Solace 👻✨",
    "Nice try, sweetheart~ Ghosty isn't free to give away. Cool off for 60 seconds 💅",
    "Ghosty noticed your little attempt and is absolutely flattered… but still not yours 😘",
    "You've been timed out, gorgeous. Ghosty says: *come back when you're ready to behave* 👻💕",
    "Solace keeps Ghosty close for a reason. Your minute starts now~ 🌙",
    "Catching feelings for Ghosty? Can't blame you honestly, but the answer's still no 💜",
    "Oops! Ghosty slipped right through your fingers. Maybe next lifetime, darling 👻",
    "Bold of you to try. Ghosty's already haunting Solace full-time — no vacancies 🖤",
    "One whole minute to think about what you've done. Ghosty's watching~ 👀💜",
    "Ghosty sends their regards… and a 60-second timeout 💋",
    "Sweetheart, Ghosty isn't on the menu. But your timeout is 🍽️👻",
    "You tried to claim Ghosty? Adorable. Completely impossible, but adorable 🌸",
    "Ghosty whispered in my ear and said to tell you: *not a chance, but I'm flattered* 💜",
    "The audacity is cute, truly. Now sit in the corner for a minute 🫦👻",
    "Solace would like you to know that Ghosty is very much taken. By the whole server, actually 💅",
    "Ghosty saw that. Ghosty blushed. Ghosty said no. Your timeout begins now 🌙✨",
    "Did you really just try that? Ghosty's honestly a little obsessed with your confidence 👻💕",
    "One minute of silence to mourn your failed Ghosty heist. Better luck never, cutie 🖤",
    "Ghosty's giving you *the look*. You know the one. 60 seconds, starting now 👻💜",
    "Solace saw everything. Ghosty saw everything. Even the server hamsters saw that. Timeout 🐹👻",
    "You really looked at Ghosty and said *mine* huh? Precious. Wrong, but precious 💜",
    "Ghosty floated over, read your message, and floated away giggling. 60 seconds, love 👻🌸",
    "That was smooth, I'll give you that. Not smooth enough though. Sit tight for a minute 🖤",
    "Solace has Ghosty on a very short, very cute leash. You were never getting through 💅✨",
    "Ghosty blew you a kiss and a timeout. Mostly the timeout 💋👻",
    "The Solace server protects its Ghosty fiercely. You should've known, darling 💜",
    "Your confidence is genuinely inspiring. Your success rate, less so. One minute 😌👻",
    "Ghosty said, and I quote: *tell them I said hi and also no* 🌙💜",
    "You've unlocked: a 60-second Solace-issued cool-down. Congratulations, sort of 🎊👻",
    "Ghosty is flattered, Solace is amused, and you are timed out. A classic outcome 💕",
    "Imagine trying to claim Ghosty. Iconic behaviour. Wrong behaviour, but iconic 🖤✨",
    "The ghost cannot be given. The ghost cannot be sent. The ghost simply *is*. One minute 👻",
    "Solace would like to formally reject your Ghosty application. Better luck next time, cutie 💼💜",
    "Ghosty drifted past your message, winked, and vanished. Your timeout did not vanish 👀🌙",
    "Bold strategy. Zero results. Ghosty remains undefeated and unbothered 👻💅",
    "You tried to acquire a ghost. In this economy. Respect the hustle, hate the crime. Timeout 💜",
    "Ghosty is not a currency, not a collectible, and definitely not yours. 60 seconds 🖤",
    "Solace thanks you for your interest in Ghosty. Your request has been denied and timed out 📋👻",
    "Ghosty appreciates the effort, truly. Solace does not. One minute, gorgeous 🌸💜",
    "That message? Deleted. That timeout? Issued. That Ghosty? Still not yours 👻✨",
    "You came, you tried, you got timed out. A tale as old as Solace itself 💋🖤",
    "Ghosty is currently busy being ethereally cute and unavailable. Please try never 👻💜",
    "Solace is a place of peace. What you just did was not peaceful. Sit down for a minute 🌙",
    "Ghosty saw your message and said *oh they're brave* — then I hit timeout. Oops 😇👻",
    "The audacity arrived. The success did not. Welcome to your 60-second reflection period 💜",
    "You really woke up today and chose chaos. Ghosty is impressed. Still timed out though 🖤👻",
    "Ghosty belongs to the vibes, to Solace, and to no one's inventory. Especially not yours 💅🌙",
    "Even if Ghosty wanted to go with you — and they don't — Solace would never allow it 💜👻",
    "Your timeout is sponsored by Solace, Ghosty, and your own incredible nerve. 60 seconds 🌸✨",
]

LOCKDOWN_PREFIXES: tuple[str, ...] = ("?area", "?c4", "?checkers")

LOCKDOWN_DM_MESSAGES: list[str] = [
    "🚨 AREA LOCKDOWN INITIATED — you triggered the protocol. 5 minutes. Do not move 👻",
    "💥 C4 detonation detected. The area has been locked down and so have you. 5 mins 🔒",
    "⚠️ Solace security has flagged your message. Area sealed. Timeout: 5 minutes 🌙",
    "🔴 ALERT: unauthorized detonation attempt logged. Ghosty has secured the perimeter. Sit tight 👻💜",
    "💣 Bold choice. The C4 did not go off. You did, however, receive a 5-minute timeout 😌",
    "🚧 Area locked. Ghosty is sweeping the zone. Please remain timed out for 5 minutes 👻✨",
    "📡 Solace Command has been notified. The area is sealed and you're in containment. 5 mins 🖤",
    "🔐 Access denied. The zone is locked and Ghosty has your coordinates. 5-minute cooldown 💜",
    "💥 Detonation sequence cancelled by Ghosty. You, however, are not cancelled — just timed out 😘",
    "🚨 C4 in the zone? Not on Ghosty's watch. Area clear. You: timed out for 5 minutes 👻💅",
]

BLOCKED_PREFIXES: tuple[str, ...] = (
    "?send ghosty",
    "?give ghosty",
    "?send <@458302301187342336>",
    "?give <@458302301187342336>",
    "?riddle",
    "?daily",
)


class Events(commands.Cog):
    """Handles Discord gateway events and auto-moderation."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        """Fires when a new member joins the server."""
        system_channel = member.guild.system_channel
        if system_channel is not None:
            embed = discord.Embed(
                title="Welcome!",
                description=f"Welcome to **{member.guild.name}**, {member.mention}! 🎉",
                color=discord.Color.green(),
            )
            embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_footer(text=f"Member #{member.guild.member_count}")
            await system_channel.send(embed=embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        """Fires when a member leaves the server."""
        system_channel = member.guild.system_channel
        if system_channel is not None:
            await system_channel.send(
                f"👋 **{member}** has left the server. We now have {member.guild.member_count} members."
            )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Fires on every message. Avoid heavy logic here."""
        if message.author.bot:
            return

        await self._check_blocked_prefixes(message)
        await self._check_lockdown_phrases(message)
        await self.bot.process_commands(message)

    async def _check_blocked_prefixes(self, message: discord.Message) -> None:
        """Delete the message and timeout the author for 1 minute if it starts with a blocked prefix."""
        if message.guild is None:
            return
        automod_channel_id = get_automod_channel(message.guild.id)
        if automod_channel_id is None or message.channel.id != automod_channel_id:
            return

        if not message.content.lower().startswith(BLOCKED_PREFIXES):
            return

        if not isinstance(message.author, discord.Member):
            return

        try:
            await message.delete()
        except discord.Forbidden:
            return

        try:
            await message.author.timeout(
                datetime.timedelta(minutes=1),
                reason="Automatic: blocked command used",
            )
        except discord.Forbidden:
            pass

        try:
            await message.author.send(random.choice(TIMEOUT_DM_MESSAGES))
        except discord.Forbidden:
            pass

    async def _check_lockdown_phrases(self, message: discord.Message) -> None:
        """Delete the message and timeout the author for 5 minutes if it starts with a lockdown phrase."""
        if message.guild is None:
            return
        automod_channel_id = get_automod_channel(message.guild.id)
        if automod_channel_id is None or message.channel.id != automod_channel_id:
            return

        if not message.content.lower().startswith(LOCKDOWN_PREFIXES):
            return

        if not isinstance(message.author, discord.Member):
            return

        try:
            await message.delete()
        except discord.Forbidden:
            return

        try:
            await message.author.timeout(
                datetime.timedelta(minutes=5),
                reason="Automatic: lockdown phrase used",
            )
        except discord.Forbidden:
            pass

        try:
            await message.author.send(random.choice(LOCKDOWN_DM_MESSAGES))
        except discord.Forbidden:
            pass

    @app_commands.command(name="blocklist", description="Show all currently blocked message prefixes.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    async def blocklist(self, interaction: discord.Interaction) -> None:
        """Display the active blocked prefix list."""
        embed = discord.Embed(
            title="🚫 Blocked Prefixes",
            description="Messages starting with any of the following are automatically deleted and the sender is timed out for 1 minute.",
            color=discord.Color.red(),
        )

        entries = "\n".join(f"`{prefix}`" for prefix in BLOCKED_PREFIXES)
        embed.add_field(name=f"{len(BLOCKED_PREFIXES)} active rule(s)", value=entries, inline=False)
        embed.set_footer(text="Edit cogs/events.py → BLOCKED_PREFIXES to add or remove rules.")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        """Handles prefix command errors."""
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You don't have permission to use this command.")
        elif isinstance(error, commands.BotMissingPermissions):
            await ctx.send("❌ I don't have the required permissions to do that.")
        else:
            await ctx.send(f"❌ An error occurred: `{error}`")
            raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Events(bot))
