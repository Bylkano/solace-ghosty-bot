import datetime
import random
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import discord
from discord import app_commands
from discord.ext import commands

from store import get_automod_channel

FIVE_MINUTE_MESSAGES: list[str] = [
    "Oh, you thought you could handle Ghosty? Cute. Let's see how you handle a 5-minute timeout instead~ 💜",
    "Bold move, sweetheart. But Ghosty's taking a breather from you for the next 300 seconds 👻✨",
    "A bit too eager, aren't we? Sit tight and think about your choices for 5 minutes 💅",
    "Ghosty's already spoken for, darling. Go ahead and take a 5-minute time out to cool off 😘",
    "You've been a little too adventurous. Solace says it's time for a 5-minute break 🖤",
    "Nice try, gorgeous, but that's not happening. See you back here in 5 minutes 💋",
    "Ghosty looked at your request, blushed, and then handed you a 5-minute timeout 🌸💜",
    "My, the audacity on you today! Let's put that energy on hold for 5 whole minutes 👻💕",
    "You're trying to claim what's already taken. Sit in the corner for 5 minutes, love 🌙✨",
    "Ghosty isn't on your menu today. Enjoy your 5-minute reflection period 🍽️👻",
    "That attitude earned you a proper 5-minute cooldown. No shortcuts 💅✨",
    "You really thought you did something there, huh? 5 minutes, starting now 💜",
    "Ghosty's giving you *the look* right now. 5 minutes of absolute silence for you 👀🔮",
    "Solace rules are simple: don't touch the ghost. 5 minutes in isolation for you 🖤",
    "Your confidence is adorable, but your execution got you a 5-minute timeout 🌸",
    "Let's put a pause on those wild ideas for 5 minutes, shall we? 👻💜",
    "Ghosty just whispered that you need a timeout. 5 minutes to be exact 💋👻",
    "You came, you tried, you got benched for 5 minutes. Better luck next time, cutie 🖤✨",
    "Solace security has flagged your enthusiasm. Please wait 5 minutes to try again 🚨",
    "Ghosty remains completely undefeated. You remain timed out for 5 minutes 💅🌙",
    "Oops! Looks like your little attempt backfired into a 5-minute timeout 😇👻",
    "Imagine trying that in this server. 5 minutes to ponder your life choices, darling 💜",
    "Ghosty blew you a kiss and a 5-minute restriction. Mostly the restriction 💋",
    "You really chose chaos today. Ghosty is amused, but you're still timed out for 5 minutes 🖤",
    "That request was a bit too forward. Let's take a 5-minute breather 🌸💜",
    "The ghost simply said *no*. 300 seconds on the clock, love 👻✨",
    "You tried to run off with Ghosty? Bold strategy. 5 minutes of downtime for you 🏃‍♂️❌",
    "Solace has a zero-tolerance policy for ghost-snatching. 5 minutes in the penalty box 💜",
    "Ghosty just drifted away laughing. You've got 5 minutes to recover your dignity 👻🌸",
    "That level of nerve deserves a 5-minute timeout. Enjoy the silence 🖤",
    "You've officially unlocked a 5-minute Solace-approved cooldown period. Enjoy 🎊👻",
    "Your message disappeared, and so did your chat privileges for the next 5 minutes 💅",
    "Sweetheart, you're trying too hard. Take 5 minutes to relax 💜🌙",
    "Ghosty is exclusive property. 5 minutes of thinking time for you 🖤✨",
    "The hamsters running the server just voted you off the chat for 5 minutes 🐹👻",
    "A little too smooth for your own good. 5 minutes on the sidelines, gorgeous 💋",
    "Ghosty just pressed the timeout button and walked away unbothered 👻💜",
    "Your request has been formally denied by the Solace high command. 5 minutes 📋💜",
    "You looked at Ghosty and thought *mine*? Adorable, but highly forbidden. 5 minutes 🌸",
    "You've been put on a strict 5-minute ghost-free diet. Happy waiting 👻💕",
    "The vibe check failed spectacularly. 5 minutes in the timeout zone 🌙",
    "Ghosty appreciated the hustle, but Solace demands order. 5 minutes, please 🖤",
    "That was a dangerous move. Solace security says: 5 minutes of stillness 🔒👻",
    "You really thought you were the exception? 5 minutes to remember the rules 💅💜",
    "Ghosty just left you on read. For 5 full minutes. Starting right now 👻👀",
    "Too close for comfort, love. Let's put 5 minutes of distance between us 🌸✨",
    "Your confidence is inspiring, but your success rate today is zero. 5 minutes 😌👻",
    "Ghosty is currently unavailable for your antics. Try again in 5 minutes 💜",
    "A tale as old as time: you tried, you failed, you got a 5-minute timeout 🖤🌙",
    "Solace is a sanctuary, and that message was a disturbance. 5 minutes of quiet time 👻💋",
    "Caught in 4K doing exactly what you weren't supposed to. 5 minutes, no appeal 📸👻",
    "Ghosty saw everything. Every. Single. Thing. 5 minutes to think about that 😏💜",
    "You really woke up and chose *that*? Respect the audacity, but 5 minutes is the price 🖤",
    "Solace doesn't negotiate with chaos agents. 5 minutes, final answer 🔐✨",
    "The ghost has spoken and the ghost said *sit down*. 5 minutes 👻💅",
    "You had one job. One. And now you have a 5-minute timeout 🌸🖤",
    "Ghosty filed a complaint and it was approved instantly. 5-minute suspension 📝💜",
    "That message? Deleted. You? Timed out. Ghosty? Unbothered 😌👻",
    "You just speedran your way into a 5-minute ban. Impressive, really 💨🖤",
    "Solace protection protocols have been activated. Please enjoy your 5-minute wait 🛡️👻",
]

CHAT_REPLY_MESSAGES: list[str] = [
    "🤫 hold on i'm hearing some noise… oh wait, that's just {name} trying to type 💀",
    "👻 did someone speak? no? must've been {name} forgetting they're muted lmaooo",
    "📢 BREAKING NEWS: {name} thought the rules didn't apply to them. they were wrong 💅",
    "🔇 {name} really said 'let me try this' and got immediately humbled 😭",
    "👀 the audacity of {name} never ceases to amaze me honestly",
    "💀 {name} said hold my drink and proceeded to get timed out in record time",
    "🤡 not {name} really thinking that was gonna work lmaooo",
    "😴 ghosty is unbothered. {name} is not. carry on everyone 👻",
    "📵 {name} got a 5 minute vacation compliments of solace 🌴 enjoy the silence bestie",
    "🎭 {name} really came in here with a plan and the plan was absolutely terrible 💜",
    "🎬 director's cut: {name} presses send. ghosty presses timeout. credits roll 👻",
    "🚨 alert: {name} has been placed in the naughty corner for 5 minutes 😇",
    "😂 {name} really said 'today is the day' and today said absolutely not",
    "🧊 {name} thought they ate. they did not eat. they got timed out 💀",
    "👋 {name} wanted attention and now they have all of ours but can't respond 😭💅",
    "🗑️ {name}'s message has been deleted and so has their dignity for the next 5 minutes",
    "🤭 oh no anyway… {name} is timed out, moving on 💜",
    "🎯 ghosty's aim: perfect. {name}'s plan: not so much 👻",
    "💬 {name} said something. ghosty disagreed. timeout issued. democracy wins 🗳️",
    "🌚 {name} really thought this was their moment. it was not their moment 😌",
    "📣 PSA: {name} is currently unavailable due to a self-inflicted timeout 👻💅",
    "🫣 i can't look. {name} is embarrassing themselves and they can't even defend themselves rn 💀",
    "🤦 {name} really looked at the rules and said 'yeah those aren't for me' 💜",
    "🔒 {name} has been locked out of the chat temporarily. skill issue tbh 😂",
    "👑 ghosty: 1. {name}: 0. the scoreboard never lies 👻✨",
    "😬 the way {name} just walked straight into that timeout like they had a death wish",
    "📺 tonight on solace: {name} attempts something bold. ghosty is not impressed 🖤",
    "🎪 ladies and gentlemen {name} has entered the timeout zone. round of applause 👏",
    "😶 {name} is now in their silent era. not by choice though 💀👻",
    "🫡 respect to {name} for the confidence at least. the rest? not so much 💅",
    "🧂 {name} really said let me season this server with chaos. 5 minute cooldown bestie",
    "🎻 playing the world's smallest violin for {name} rn 🎻💀",
    "🤳 {name} said 'watch this' and we watched them get timed out 👁️👁️",
    "🌬️ {name} blew into the server, tried something dumb, and got blown right back out 👻",
    "🫀 the heartbreak of watching {name} think that was going to work 😭💜",
    "📋 log entry: {name} attempted a blocked command. {name} regrets it. probably 👻",
    "🎠 {name} rode the merry-go-round straight into a 5 minute timeout 💅🌙",
    "🤓 fun fact: {name} just discovered what happens when you ignore the rules 👓💀",
    "💌 dear {name}, no. love, ghosty 👻🖤",
    "🚪 {name} tried to open a door that was clearly marked DO NOT ENTER 😂",
    "🦗 …and {name} has nothing to say about it. literally. muted 🦗👻",
    "🍿 everyone grab popcorn {name} is timed out and cannot defend themselves 🍿😭",
    "🎤 {name} dropped the mic but ghosty picked it up and gave them a timeout instead 🎤👻",
    "🌡️ {name}'s temperature check: too hot. ghosty issued a mandatory 5 minute cooldown ❄️",
    "🤷 {name} really said 'what's the worst that could happen' and found out 💀💜",
    "🧲 {name} was attracted to trouble and trouble did not disappoint 👻",
    "🎯 targeted. eliminated. timed out. {name} never saw it coming 💅🖤",
    "😒 {name} really thought ghosty wasn't watching. ghosty is always watching 👁️👻",
    "🪦 rip to {name}'s chat privileges for the next 5 minutes. gone too soon 😔",
    "🔔 ding ding ding! {name} has won a complimentary 5-minute timeout! collect your prize 🎊👻",
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
    "🧨 That little stunt just triggered the full lockdown sequence. 5 minutes in containment 🔒🖤",
    "📻 Ghosty to base: target neutralised and timed out. Zone secured. Over 👻💜",
    "🚁 Solace response team deployed. Area locked. You're grounded for 5 minutes ✨🔴",
    "🛑 Unauthorised zone activity detected. Ghosty has your location. Enjoy your 5-minute detainment 😏🖤",
    "💀 The area didn't explode. Your chat privileges did. 5 minutes 👻💅",
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
        """Delete the message, timeout the author for 5 minutes, DM them, and reply in chat."""
        if message.guild is None:
            return
        automod_channel_id = get_automod_channel(message.guild.id)
        if automod_channel_id is None or message.channel.id != automod_channel_id:
            return

        if not message.content.lower().startswith(BLOCKED_PREFIXES):
            return

        if not isinstance(message.author, discord.Member):
            return

        name = message.author.display_name

        try:
            await message.delete()
        except discord.Forbidden:
            return

        try:
            await message.author.timeout(
                datetime.timedelta(minutes=5),
                reason="Automatic: blocked command used",
            )
        except discord.Forbidden:
            pass

        # DM the user
        try:
            await message.author.send(random.choice(FIVE_MINUTE_MESSAGES))
        except discord.Forbidden:
            pass

        # Reply in the channel mentioning the user
        chat_reply = random.choice(CHAT_REPLY_MESSAGES).format(name=message.author.mention)
        try:
            await message.channel.send(chat_reply)
        except discord.Forbidden:
            pass

    async def _check_lockdown_phrases(self, message: discord.Message) -> None:
        """Delete the message, timeout the author for 5 minutes, DM them, and reply in chat."""
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

        # DM the user
        try:
            await message.author.send(random.choice(LOCKDOWN_DM_MESSAGES))
        except discord.Forbidden:
            pass

        # Reply in the channel mentioning the user
        chat_reply = random.choice(CHAT_REPLY_MESSAGES).format(name=message.author.mention)
        try:
            await message.channel.send(chat_reply)
        except discord.Forbidden:
            pass

    @app_commands.command(name="blocklist", description="Show all currently blocked message prefixes.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    async def blocklist(self, interaction: discord.Interaction) -> None:
        """Display the active blocked prefix list."""
        embed = discord.Embed(
            title="🚫 Blocked Prefixes",
            description="Messages starting with any of the following are automatically deleted and the sender is timed out for 5 minutes.",
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
