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
    "Sweetheart, Ghosty isn't on the menu, but this 3-minute timeout is served hot 🍽️👅",
    "You tried to claim Ghosty? Adorable. Completely impossible, but that confidence is so hot 🌸🫦",
    "Ghosty whispered in my ear and said to tell you: *not a chance, but you look damn good trying* 💜😏",
    "The audacity is incredibly sexy, truly. Now shake that cute ass over to the corner for 3 whole minutes 🍑👻",
    "Solace would like you to know that Ghosty is very much taken. But hey, we love watching you try 💅🔥",
    "Ghosty saw that, bit their lip, and blushed. Your 3-minute cooldown begins now, gorgeous 🫦✨",
    "Did you really just try that? Honestly, I'm a little obsessed with how bold you are 👻💕",
    "3 minutes of silence to mourn your failed heist. Let's see if you look as good sitting tight as you do talking smack 🖤🍑",
    "Ghosty's giving you *the look*. The one that means you've been a very bad rebel. 3 minutes, starting now 👻🫦",
    "I saw everything. Ghosty saw everything. We're both just staring at you now. Timeout, trouble 🐹👻",
    "You really looked at Ghosty and said *mine* huh? Precious. I love a thief with a fine ass 💜🍑",
    "Ghosty floated over, checked you out, and floated away giggling. 3 minutes, love 👻🌸",
    "That was smooth, I'll give you that. Got me leaning in closer. Sit tight for 3 minutes 🖤💋",
    "Solace has Ghosty on a very short, very cute leash. But your distraction is definitely working 💅✨",
    "Ghosty blew you a kiss right on your lips, followed by a 3-minute timeout 💋👻",
    "You're gorgeous when you're being troublesome. Let's see how well you behave for 3 minutes 😉💜",
    "Ghosty likes a rebel, and I demand order. Take 3 minutes to cool down, handsome 🔥😏",
    "Are you trying to steal Ghosty, or just trying to get my eyes on your ass? Either way, you got 3 minutes 🍑✨",
    "I love the confidence, darling, but Ghosty stays here. Go sit pretty for 3 minutes 🖤🫦",
    "Ghosty literally bit their lip looking at your message. Rules are rules, but damn. 3 minutes in the box 🫦👻",
    "Trying to take Ghosty home on the first try? Slow down, cutie. Take 3 minutes to think about what you'd do next 🌸",
    "You've got nerve, sweetheart. I'm getting a little bit obsessed. Cool off for 3 minutes 💜✨",
    "Oh, a thief? How thrilling. Sadly for you, Ghosty stays here. 3 minutes of punishment for your crimes 💋🖤",
    "Ghosty just materialised to tell me you're gorgeous. Then they told me to lock you away for 3 minutes 😇👻",
    "You look like you need a break from being so bold. I prescribe 3 minutes of quiet time with me 🩺💜",
    "Did it hurt? When you fell for Ghosty and got immediately timed out for 3 minutes? 👻💔",
    "You're a major distraction, gorgeous. Catch your breath and relax that pretty mind for 3 minutes 🌬️🖤",
    "Solace security just called. They said that ass is entirely too dangerous to be left untimed-out for 3 minutes 🚨🍑",
    "Ghosty loves the view of you from over here. Don't ruin the tension, just enjoy the 3-minute wait 😉👻",
    "I'd say 'nice try,' but it wasn't. It was incredibly hot though. 3 minutes on the bench, darling 💜💋",
    "Ghosty is currently hiding behind me blushing. Look what you did! 3 minutes for making us all hot and bothered 👻👉👈",
    "Is it hot in here, or did you just trigger a 3-minute cooldown? Let's turn the heat down 🔥💅",
    "You can't just buy Ghosty's affection with a blocked command, sweetheart. Try biting your lip and waiting 3 minutes 💋",
    "Ghosty said you have lovely energy. Terrible timing, but amazing vibes. 3 minutes 🖤✨",
    "A whole 3 minutes without you? I might actually miss you. Make it count, love 👻👋",
    "You're lucky you're cute, because that command was a total disaster. 3 minutes in the corner, trouble 🌸💜",
    "Ghosty's heart skipped a beat! Oh wait, ghosts don't have hearts. Back to reality, 3 minutes 👻💔",
    "You think you can just come into Solace and steal our ghost? Bold. Sassy. Timed out for 3 minutes 🌙🫦",
    "Ghosty just winked at me and whispered, 'Give them 3 minutes to think of an even dirtier line' 😉👻",
    "I appreciate the hustle, gorgeous, but Ghosty isn't yours to take. 3 minutes of reflection starts now 💜",
    "You're playing with fire, sweetheart. Luckily, I brought a 3-minute bucket of ice water 🧊👻",
    "Ghosty is flattered by the attempt, but I require you to behave your cute ass for the next 3 minutes 💅🖤",
    "A master thief? Not quite. But you definitely stole my attention for 3 minutes 🕵️‍♂️💜",
    "Ghosty is strictly VIP access only, darling. You'll need 3 minutes to get on my exclusive list 🎟️👻",
    "You really thought you did something there, didn't you? Cute. 3 minutes of silence for that big ego 🤫✨",
    "Ghosty just floated through my screen to tell me you're a menace. 3 minutes of timeout for the trouble 💋🍑",
    "Solace isn't ready for your level of main character energy. Take 3 minutes to dial it back, superstar 🌟👻",
    "Ghosty's already spoken for, love. But I can offer you a premium 3-minute one-on-one package 🎁💜",
    "You've been caught red-handed. I think the handcuffs would look incredible on you. 3 minutes 🔒🔥",
    "Such a beautiful attempt, such a tragic 3-minute consequence. Sending you a little kiss to make it better 💐💋",
    "Ghosty told me they'd give you a second chance... in exactly 3 minutes. Press your lips together and wait 🖤⏱️",
    "Are you trying to break the server, or just trying to break my heart? 3 minutes of timeout either way 💔💅",
    "Ghosty is casting a spell on you. It's called 'Sit Still, Look Sexy, and Be Quiet for 3 Minutes' 🪄🔮👻",
    "You can't bypass Solace security just by being charming. Nice try, 3 minutes 🚫😘",
    "Ghosty is watching you from the shadows... mostly just admiring how good you look in a 3-minute timeout 👻🔥",
    "Don't look at me with those eyes, sweetheart. The 3-minute timeout stays 🥺💜",
    "Ghosty says you're giving major trouble-maker vibes today. Let's fix that with a 3-minute break 🌊🖤",
    "You wanted Ghosty, but you got me instead. Honestly, a 3-minute timeout with me is a much better deal 👻✨",
    "Solace has a zero-tolerance policy for ghost-napping, no matter how fine the suspect is. 3 minutes 🚨🌸",
    "Ghosty just made a dramatic sigh. You've exhausted the spirit. Go rest your pretty ass for 3 minutes 👻💤",
    "You're a little too fast for this server, darling. Let's slow things down for 3 minutes 🏎️💜",
    "Did you think I wouldn't notice? I notice everything. Especially you. 3 minutes 👁️💋",
    "That was a high-risk move for zero reward. I admire the drama. 3 minutes 🎭👻",
    "You can try again later, sweetheart. For now, we demand a 3-minute intermission 🍿🖤",
    "Solace belongs to Ghosty, Ghosty belongs to Solace, and your cute ass belongs in timeout for 3 minutes 👻🍑",
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
