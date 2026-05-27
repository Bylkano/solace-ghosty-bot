import datetime
import random
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import discord
from discord import app_commands
from discord.ext import commands

from store import get_automod_channel

timeout_messages = [
    # --- BYLKA & GHOSTY MESSAGES ---
    "Sweetheart, Ghosty isn't on the menu, and Bylka says your 3-minute timeout is served 🍽️👻",
    "You tried to claim Ghosty? Adorable. Bylka thinks it's completely impossible, but incredibly cute 🌸",
    "Ghosty whispered in Bylka's ear and said to tell you: *not a chance, but I'm flattered* 💜",
    "The audacity is cute, truly. Bylka says go sit in the corner for 3 whole minutes 🫦👻",
    "Bylka wants you to know that Ghosty is very much taken. By the whole server, actually 💅",
    "Ghosty saw that, looked at Bylka, and blushed. Your 3-minute timeout begins now 🌙✨",
    "Did you really just try that? Bylka and Ghosty are honestly a little obsessed with your confidence 👻💕",
    "3 minutes of silence to mourn your failed Ghosty heist. Bylka says better luck never, cutie 🖤",
    "Bylka's giving you *the look*. You know the one. 3 minutes, starting now 👻💜",
    "Bylka saw everything. Ghosty saw everything. Even the server hamsters saw that. Timeout 🐹👻",
    "You really looked at Ghosty and said *mine* huh? Bylka thinks you're precious. Wrong, but precious 💜",
    "Ghosty floated over to Bylka, read your message, and giggled. 3 minutes, love 👻🌸",
    "That was smooth, Bylka will give you that. Not smooth enough though. Sit tight for 3 minutes 🖤",
    "Bylka has Ghosty on a very short, very cute leash. You were never getting through 💅✨",
    "Bylka and Ghosty send you a kiss and a timeout. Mostly the 3-minute timeout 💋👻",
    "You're gorgeous when you're being troublesome. Too bad Bylka still says no. See you in 3 minutes 😉💜",
    "Ghosty likes a rebel, but Bylka demands order. Take 3 minutes to cool down, handsome 👻🔥",
    "Are you trying to steal Ghosty, or just trying to get Bylka's attention? Either way, you got 3 minutes 💅✨",
    "I love the confidence, darling, but Ghosty only haunts Bylka's server. Sit pretty for 3 minutes 🖤",
    "Ghosty literally bit their lip looking Rogers your message, but Bylka's rules are rules. 3 minutes in the box 🫦👻",
    "Trying to take Ghosty home on the first try? Slow down, cutie. Bylka says take 3 minutes to think about it 🌸",
    "You've got nerve, sweetheart. Bylka's a little bit obsessed. Cool off for 3 minutes 💜🌙",
    "Oh, a thief? How thrilling. Sadly for you, Bylka keeps Ghosty right here. 3 minutes for your crimes 💋🖤",
    "Ghosty just materialised to tell Bylka you're cute. Then we decided to time you out for 3 minutes 😇👻",
    "You look like you need a break from being so bold. Bylka prescribes 3 minutes of quiet time 🩺💜",

    # --- ULTRA-FLIRTY 3-MINUTE MESSAGES ---
    "Did it hurt? When you fell for Ghosty and got immediately timed out for 3 minutes? 👻💔",
    "You're a distraction, gorgeous. Ghosty needs their space. Catch your breath for 3 minutes 🌬️🖤",
    "Solace security just called. They said you're entirely too dangerous to be left untimed-out for 3 minutes 🚨✨",
    "Ghosty loves the view from over here, but you need a 3-minute timeout. Don't ruin the tension 😉👻",
    "I'd say 'nice try,' but it wasn't. It was cute though. 3 minutes on the bench, darling 💜",
    "Ghosty is currently hiding behind me blushing. Look what you did! 3 minutes for making the ghost shy 👻👉👈",
    "Is it hot in here, or did you just trigger the Solace auto-mod? Enjoy your 3-minute cooldown 🔥💅",
    "You can't just buy Ghosty's affection with a blocked command, sweetheart. Try waiting 3 minutes instead 💋",
    "Ghosty said you have lovely energy. Terrible timing, but lovely energy. 3 minutes 🖤✨",
    "A whole 3 minutes without you? Ghosty might actually miss you. Make it count, love 👻👋",
    "You're lucky you're cute, because that command was a total disaster. 3 minutes in the corner 🌸💜",
    "Ghosty's heart skipped a beat! Oh wait, ghosts don't have hearts. Back to reality, 3 minutes 👻💔",
    "You think you can just come into Solace and steal our ghost? Bold. Sassy. Timed out for 3 minutes 🌙",
    "Ghosty just winked at me and whispered, 'Give them 3 minutes to think of a better pickup line' 😉👻",
    "I appreciate the hustle, gorgeous, but Ghosty isn't yours to take. 3 minutes of reflection starts now 💜",
    "You're playing with fire, sweetheart. Luckily, Ghosty brought a 3-minute bucket of ice water 🧊👻",
    "Ghosty is flattered by the attempt, but Solace requires you to behave for the next 3 minutes 💅🖤",
    "A master thief? Not quite. But you definitely stole a 3-minute timeout from me 🕵️‍♂️💜",
    "Ghosty is strictly VIP access only, darling. You'll need 3 minutes to get on the guest list 🎟️👻",
    "You really thought you did something there, didn't you? Cute. 3 minutes of silence for your ego 🤫✨",
    "Ghosty just floated through my screen to tell me you're a menace. 3 minutes of timeout for the trouble 💋",
    "Solace isn't ready for your level of main character energy. Take 3 minutes to dial it back, superstar 🌟👻",
    "Ghosty's already spoken for, love. But I can offer you a premium 3-minute timeout package 🎁💜",
    "You've been caught red-handed. Ghosty thinks the handcuffs look good on you. 3 minutes 🔒🔥",
    "Such a beautiful attempt, such a tragic 3-minute consequence. Ghosty sends their condolences 💐👻",
    "Ghosty told me they'd give you a second chance... in exactly 3 minutes. Don't be late 🖤⏱️",
    "Are you trying to break the server, or just trying to break Ghosty's heart? 3 minutes of timeout either way 💔💅",
    "Ghosty is casting a spell on you. It's called 'Sit Still and Be Quiet for 3 Minutes' 🪄🔮👻",
    "You can't bypass Solace security just by being charming. Nice try, 3 minutes 🚫😘",
    "Ghosty is watching you from the shadows... mostly just laughing at your 3-minute timeout though 👻😂",
    "Don't look at me with those eyes, sweetheart. The 3-minute timeout stays 🥺💜",
    "Ghosty says you're giving major trouble-maker vibes today. Let's fix that with a 3-minute break 🌊🖤",
    "You wanted Ghosty, but you got me instead. Honestly, a 3-minute timeout is a pretty good deal 👻✨",
    "Solace has a zero-tolerance policy for ghost-napping, no matter how cute the suspect is. 3 minutes 🚨🌸",
    "Ghosty just made a dramatic sigh. You've exhausted the spirit. Go rest for 3 minutes 👻💤",
    "You're a little too fast for this server, darling. Ghosty's slowing you down for 3 minutes 🏎️💜",
    "Did you think Ghosty wouldn't notice? Ghosty notices everything. Especially you. 3 minutes 👁️💋",
    "That was a high-risk move for zero reward. Ghosty admires the drama. 3 minutes 🎭👻",
    "You can try again later, sweetheart. For now, Ghosty demands a 3-minute intermission 🍿🖤",
    "Solace belongs to Ghosty, Ghosty belongs to Solace, and you belong in timeout for 3 minutes. Simple math 👻📐",
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
                datetime.timedelta(minutes=2),
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
