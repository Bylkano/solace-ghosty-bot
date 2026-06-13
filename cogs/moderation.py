import logging
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import discord
from discord import app_commands
from discord.ext import commands

from store import (
    get_automod_channel,
    set_automod_channel,
    add_self_role,
    remove_self_role,
    get_self_roles,
)

log = logging.getLogger("bot.moderation")


class SelfRoleSelect(discord.ui.Select):
    """Dropdown that toggles one of the guild's self-assignable roles."""

    def __init__(self, roles: list[discord.Role]) -> None:
        options = [
            discord.SelectOption(label=role.name[:100], value=str(role.id))
            for role in roles[:25]
        ]
        super().__init__(
            placeholder="Choose a role…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None
        role = guild.get_role(int(self.values[0]))
        if role is None:
            await interaction.response.send_message(
                "❌ That role no longer exists.", ephemeral=True
            )
            return

        me = guild.me
        if not me.guild_permissions.manage_roles or me.top_role <= role:
            await interaction.response.send_message(
                f"❌ I can't manage {role.mention} right now. I need the **Manage Roles** "
                "permission and my own role must sit above it.",
                ephemeral=True,
            )
            return

        member = interaction.user
        if not isinstance(member, discord.Member):
            member = guild.get_member(interaction.user.id)
        if member is None:
            await interaction.response.send_message(
                "❌ Couldn't find your membership in this server.", ephemeral=True
            )
            return

        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Self-role toggle via /role")
                msg = f"➖ Removed {role.mention} from you."
            else:
                await member.add_roles(role, reason="Self-role toggle via /role")
                msg = f"➕ Gave you {role.mention}."
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have permission to change that role for you.", ephemeral=True
            )
            return
        await interaction.response.send_message(msg, ephemeral=True)


class SelfRoleView(discord.ui.View):
    """Ephemeral, single-user view wrapping the self-role dropdown."""

    def __init__(self, roles: list[discord.Role], owner_id: int) -> None:
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.add_item(SelfRoleSelect(roles))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "This menu isn't yours — run `/role` to get your own.", ephemeral=True
            )
            return False
        return True


class Moderation(commands.Cog):
    """Channel configuration for Ghosty auto-moderation."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="getchannel", description="Show which channel Ghosty protection is currently watching.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def getchannel(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None
        channel_id = get_automod_channel(interaction.guild_id)
        if channel_id is None:
            await interaction.response.send_message(
                "⚠️ No channel set yet. Use `/setchannel` to configure one.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"👻 Ghosty protection is currently watching <#{channel_id}>.", ephemeral=True
            )

    @app_commands.command(name="setchannel", description="Set the channel where Ghosty protection runs.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def setchannel(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
    ) -> None:
        assert interaction.guild_id is not None
        try:
            set_automod_channel(interaction.guild_id, channel.id)
        except Exception as exc:
            log.error("setchannel: failed for guild %s: %s", interaction.guild_id, exc)
            await interaction.response.send_message(
                f"❌ Failed to save channel: `{exc}`", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"✅ Ghosty protection is now watching {channel.mention}.", ephemeral=True
        )

    # ------------------------------------------------------------------ #
    #  Channel lock / unlock                                             #
    # ------------------------------------------------------------------ #
    @app_commands.command(name="lock", description="Lock a channel so members can no longer send messages.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.describe(
        channel="Channel to lock (defaults to the current channel).",
        reason="Optional reason shown to members and in the audit log.",
    )
    async def lock(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        reason: str | None = None,
    ) -> None:
        assert interaction.guild is not None
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message(
                "❌ I can only lock standard text channels.", ephemeral=True
            )
            return

        everyone = interaction.guild.default_role
        overwrite = target.overwrites_for(everyone)
        if overwrite.send_messages is False:
            await interaction.response.send_message(
                f"🔒 {target.mention} is already locked.", ephemeral=True
            )
            return

        audit = f"Locked by {interaction.user} ({interaction.user.id})"
        if reason:
            audit += f" — {reason}"

        overwrite.send_messages = False
        overwrite.send_messages_in_threads = False
        overwrite.create_public_threads = False
        overwrite.create_private_threads = False
        try:
            await target.set_permissions(everyone, overwrite=overwrite, reason=audit)
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I can't edit that channel. I need the **Manage Channels** permission "
                "(and a role positioned above the members you want to lock out).",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            log.error("lock: failed for channel %s: %s", target.id, exc)
            await interaction.response.send_message(
                f"❌ Failed to lock channel: `{exc}`", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"🔒 Locked {target.mention}.", ephemeral=True
        )
        notice = discord.Embed(
            title="🔒 Channel Locked",
            description=reason or "This channel has been locked. Only staff can send messages right now.",
            color=discord.Color.red(),
        )
        notice.set_footer(text=f"Locked by {interaction.user.display_name}")
        try:
            await target.send(embed=notice)
        except discord.HTTPException:
            pass

    @app_commands.command(name="unlock", description="Unlock a channel so members can send messages again.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.describe(
        channel="Channel to unlock (defaults to the current channel).",
        reason="Optional reason shown in the audit log.",
    )
    async def unlock(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
        reason: str | None = None,
    ) -> None:
        assert interaction.guild is not None
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.response.send_message(
                "❌ I can only unlock standard text channels.", ephemeral=True
            )
            return

        everyone = interaction.guild.default_role
        overwrite = target.overwrites_for(everyone)
        if overwrite.send_messages is not False:
            await interaction.response.send_message(
                f"🔓 {target.mention} isn't locked.", ephemeral=True
            )
            return

        audit = f"Unlocked by {interaction.user} ({interaction.user.id})"
        if reason:
            audit += f" — {reason}"

        # Reset to inherit from category/role defaults rather than forcing allow.
        overwrite.send_messages = None
        overwrite.send_messages_in_threads = None
        overwrite.create_public_threads = None
        overwrite.create_private_threads = None
        try:
            await target.set_permissions(everyone, overwrite=overwrite, reason=audit)
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I can't edit that channel. I need the **Manage Channels** permission.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            log.error("unlock: failed for channel %s: %s", target.id, exc)
            await interaction.response.send_message(
                f"❌ Failed to unlock channel: `{exc}`", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"🔓 Unlocked {target.mention}.", ephemeral=True
        )
        notice = discord.Embed(
            title="🔓 Channel Unlocked",
            description="This channel is open again — members can send messages.",
            color=discord.Color.green(),
        )
        notice.set_footer(text=f"Unlocked by {interaction.user.display_name}")
        try:
            await target.send(embed=notice)
        except discord.HTTPException:
            pass

    # ------------------------------------------------------------------ #
    #  Self-assignable roles                                             #
    # ------------------------------------------------------------------ #
    @app_commands.command(name="addselfrole", description="Make a role members can pick with /role.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.describe(role="The role to make self-assignable.")
    async def addselfrole(self, interaction: discord.Interaction, role: discord.Role) -> None:
        assert interaction.guild is not None and interaction.guild_id is not None
        if role.is_default() or role.managed:
            await interaction.response.send_message(
                "❌ That role can't be self-assigned (it's @everyone or managed by an integration).",
                ephemeral=True,
            )
            return

        above_warning = interaction.guild.me.top_role <= role
        try:
            add_self_role(interaction.guild_id, role.id)
        except Exception as exc:
            log.error("addselfrole: failed for guild %s: %s", interaction.guild_id, exc)
            await interaction.response.send_message(
                f"❌ Failed to save role: `{exc}`", ephemeral=True
            )
            return

        if above_warning:
            await interaction.response.send_message(
                f"⚠️ {role.mention} added to the `/role` menu — but it sits **above my highest "
                "role**, so I won't be able to hand it out. Move my bot role above it in "
                "**Server Settings → Roles**.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"✅ {role.mention} is now self-assignable via `/role`.", ephemeral=True
            )

    @app_commands.command(name="removeselfrole", description="Stop a role from being self-assignable.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.describe(role="The role to remove from the /role menu.")
    async def removeselfrole(self, interaction: discord.Interaction, role: discord.Role) -> None:
        assert interaction.guild_id is not None
        remove_self_role(interaction.guild_id, role.id)
        await interaction.response.send_message(
            f"✅ {role.mention} is no longer self-assignable.", ephemeral=True
        )

    @app_commands.command(name="selfroles", description="List the roles members can pick with /role.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_roles=True)
    async def selfroles(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None and interaction.guild_id is not None
        role_ids = get_self_roles(interaction.guild_id)
        roles = [r for r in (interaction.guild.get_role(rid) for rid in role_ids) if r is not None]
        if not roles:
            await interaction.response.send_message(
                "⚠️ No self-assignable roles yet. Add one with `/addselfrole`.", ephemeral=True
            )
            return
        listing = "\n".join(f"• {r.mention}" for r in roles)
        await interaction.response.send_message(
            f"🎭 **Self-assignable roles ({len(roles)})**\n{listing}", ephemeral=True
        )

    @app_commands.command(name="role", description="Pick a role to get it — pick it again to remove it.")
    @app_commands.guild_only()
    async def role(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None and interaction.guild_id is not None
        role_ids = get_self_roles(interaction.guild_id)
        roles = [r for r in (interaction.guild.get_role(rid) for rid in role_ids) if r is not None]
        if not roles:
            await interaction.response.send_message(
                "⚠️ No self-assignable roles are set up yet. An admin can add some with `/addselfrole`.",
                ephemeral=True,
            )
            return
        view = SelfRoleView(roles, interaction.user.id)
        await interaction.response.send_message(
            "🎭 **Pick a role to toggle** — choosing one you already have removes it.",
            view=view,
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
