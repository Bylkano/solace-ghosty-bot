import datetime
import random
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import discord
from discord import app_commands
from discord.ext import commands

from store import get_automod_channel

THREE_MINUTE_MESSAGES: list[str] = [
    "Oh, you thought you could handle Ghosty? Cute. Let’s see how you handle a 3-minute timeout instead~ 💜",
    "Bold move, sweetheart. But Ghosty’s taking a breather from you for the next 180 seconds 👻✨",
    "A bit too eager, aren't we? Sit tight and think about your choices for 3 minutes 💅",
    "Ghosty's already spoken for, darling. Go ahead and take a 3-minute time out to cool off 😘",
    "You’ve been a little too adventurous. Solace says it’s time for a 3-minute break 🖤",
    "Nice try, gorgeous, but that’s not happening. See you back here in 3 minutes 💋",
    "Ghosty looked at your request, blushed, and then handed you a 3-minute timeout 🌸💜",
    "My, the audacity on you today! Let’s put that energy on hold for 3 whole minutes 👻💕",
    "You’re trying to claim what’s already taken. Sit in the corner for 3 minutes, love 🌙✨",
    "Ghosty isn't on your menu today. Enjoy your 3-minute reflection period 🍽️👻",
    "A 1-minute timeout wasn't enough for that attitude. Let's make it 3 minutes instead 💅✨",
    "You really thought you did something there, huh? 3 minutes, starting now 💜",
    "Ghosty's giving you *the look* right now. 3 minutes of absolute silence for you 👀🔮",
    "Solace rules are simple: don't touch the ghost. 3 minutes in isolation for you 🖤",
    "Your confidence is adorable, but your execution got you a 3-minute timeout 🌸",
    "Let's put a pause on those wild ideas for 3 minutes, shall we? 👻💜",
    "Ghosty just whispered that you need a timeout. 3 minutes to be exact 💋👻",
    "You came, you tried, you got benched for 3 minutes. Better luck next time, cutie 🖤✨",
    "Solace security has flagged your enthusiasm. Please wait 3 minutes to try again 🚨",
    "Ghosty remains completely undefeated. You remain timed out for 3 minutes 💅🌙",
    "Oops! Looks like your little attempt backfired into a 3-minute timeout 😇👻",
    "Imagine trying that in this server. 3 minutes to ponder your life choices, darling 💜",
    "Ghosty blew you a kiss and a 3-minute restriction. Mostly the restriction 💋",
    "You really chose chaos today. Ghosty is amused, but you're still timed out for 3 minutes 🖤",
    "That request was a bit too forward. Let’s take a 3-minute breather 🌸💜",
    "The ghost simply said *no*. 180 seconds on the clock, love 👻✨",
    "You tried to run off with Ghosty? Bold strategy. 3 minutes of downtime for you 🏃‍♂️❌",
    "Solace has a zero-tolerance policy for ghost-snatching. 3 minutes in the penalty box 💜",
    "Ghosty just drifted away laughing. You've got 3 minutes to recover your dignity 👻🌸",
    "That level of nerve deserves at least a 3-minute timeout. Enjoy the silence 🖤",
    "You've officially unlocked a 3-minute Solace-approved cooldown period. Enjoy 🎊👻",
    "Your message disappeared, and so did your chat privileges for the next 3 minutes 💅",
    "Sweetheart, you're trying too hard. Take 3 minutes to relax 💜🌙",
    "Ghosty is exclusive property. 3 minutes of thinking time for you 🖤✨",
    "The hamsters running the server just voted you off the chat for 3 minutes 🐹👻",
    "A little too smooth for your own good. 3 minutes on the sidelines, gorgeous 💋",
    "Ghosty saw that message and immediately pressed the 3-minute timeout button 🚨👻",
    "Your request has been formally denied by the Solace high command. 3 minutes 📋💜",
    "You looked at Ghosty and thought *mine*? Adorable, but highly forbidden. 3 minutes 🌸",
    "You've been put on a strict 3-minute ghost-free diet. Happy waiting 👻💕",
    "The vibe check failed. 3 minutes in the timeout zone to realign your energy 🌙",
    "Ghosty appreciated the hustle, but Solace demands order. 3 minutes, please 🖤",
    "That was a dangerous move. Solace security says: 3 minutes of stillness 🔒👻",
    "You really thought you were the exception? 3 minutes to remember the rules 💅💜",
    "Ghosty just left you on read. For 3 full minutes. Starting right now 👻👀",
    "Too close for comfort, love. Let’s put 3 minutes of distance between us 🌸✨",
    "Your confidence is inspiring, but your success rate today is zero. 3 minutes 😌👻",
    "Ghosty is currently unavailable for your antics. Try again in 3 minutes 💜",
    "A tale as old as time: you tried, you failed, you got a 3-minute timeout 🖤🌙",
    "Solace is a sanctuary, and that message was a disturbance. 3 minutes of quiet time 👻💋"
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
    "?send 458302301187342336",
    "? send ghosty",
    "? give ghosty",
    "? send <@458302301187342336>",
    "? give <@458302301187342336>",
    "? riddle",
    "? daily",
    "? send 458302301187342336",
    "?give 458302301187342336",
    "? give 458302301187342336",
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
                datetime.timedelta(minutes=3),
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
                datetime.timedelta(minutes=3),
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
