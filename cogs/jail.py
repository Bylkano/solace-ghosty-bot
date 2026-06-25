"""Shared gay-jail channel: auto-created role, channel, timers, and release."""

from __future__ import annotations

import asyncio
import logging
import random
import sys
import time
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import discord
from discord import app_commands
from discord.ext import commands

from cogs.jail_content import JAIL_REQUIREMENTS
from store import (
    clear_jail_channel_id,
    get_jail_channel_id,
    get_jail_role_id,
    get_jail_sentence,
    list_all_active_jail_sentences,
    list_jail_sentences,
    remove_jail_sentence,
    set_jail_channel_id,
    set_jail_role_id,
    upsert_jail_sentence,
)

log = logging.getLogger("bot.jail")

JAILED_ROLE_NAME = "jailed"
JAIL_CHANNEL_NAME = "gay-jail"
MAX_JAIL_MINUTES = 10_080  # 7 days


class Jail(commands.Cog):
    """Temporary shared jail channel with auto-managed role and permissions."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._release_tasks: dict[tuple[int, int], asyncio.Task] = {}

    async def cog_load(self) -> None:
        await self._restore_active_sentences()

    def cog_unload(self) -> None:
        for task in self._release_tasks.values():
            task.cancel()
        self._release_tasks.clear()

    # ------------------------------------------------------------------
    # Permission helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _can_use_jail_commands(interaction: discord.Interaction) -> bool:
        assert interaction.guild is not None
        if interaction.user.id == interaction.guild.owner_id:
            return True
        perms = interaction.user.guild_permissions
        return bool(perms.administrator)

    @staticmethod
    def _can_jail_target(actor: discord.Member, target: discord.Member) -> str | None:
        if target.id == actor.guild.owner_id:
            return "You can't jail the server owner."
        if target.bot:
            return "You can't jail bots."
        if target.id == actor.id:
            return "You can't jail yourself."
        if target.top_role >= actor.top_role and actor.id != actor.guild.owner_id:
            return "You can't jail someone with an equal or higher role."
        me = actor.guild.me
        if me.top_role <= target.top_role:
            return "My role must be above the member you want to jail."
        return None

    @staticmethod
    def _bot_can_manage(guild: discord.Guild) -> str | None:
        me = guild.me
        if not me.guild_permissions.manage_roles:
            return "I need the **Manage Roles** permission."
        if not me.guild_permissions.manage_channels:
            return "I need the **Manage Channels** permission."
        return None

    # ------------------------------------------------------------------
    # Role / channel setup
    # ------------------------------------------------------------------

    async def _ensure_jailed_role(self, guild: discord.Guild) -> discord.Role:
        saved_id = get_jail_role_id(guild.id)
        if saved_id:
            role = guild.get_role(saved_id)
            if role is not None:
                return role

        for role in guild.roles:
            if role.name.lower() == JAILED_ROLE_NAME:
                set_jail_role_id(guild.id, role.id)
                await self._sync_jailed_role_permissions(role)
                return role

        perms = discord.Permissions.none()
        perms.view_channel = False
        perms.send_messages = False
        perms.connect = False
        perms.speak = False
        perms.add_reactions = False

        role = await guild.create_role(
            name=JAILED_ROLE_NAME,
            permissions=perms,
            reason="Gay jail: auto-created jailed role",
        )
        set_jail_role_id(guild.id, role.id)

        me = guild.me
        if me.top_role > role:
            try:
                await role.edit(
                    position=me.top_role.position - 1,
                    reason="Gay jail: position jailed role below bot",
                )
            except discord.HTTPException as exc:
                log.warning("Could not reposition jailed role in guild %s: %s", guild.id, exc)

        return role

    @staticmethod
    async def _sync_jailed_role_permissions(role: discord.Role) -> None:
        perms = discord.Permissions.none()
        perms.view_channel = False
        perms.send_messages = False
        perms.connect = False
        perms.speak = False
        perms.add_reactions = False
        try:
            await role.edit(permissions=perms, reason="Gay jail: sync jailed role permissions")
        except discord.HTTPException as exc:
            log.warning("Could not sync jailed role permissions for %s: %s", role.id, exc)

    async def _ensure_jail_channel(
        self,
        guild: discord.Guild,
        jailed_role: discord.Role,
    ) -> discord.TextChannel:
        saved_id = get_jail_channel_id(guild.id)
        if saved_id:
            channel = guild.get_channel(saved_id)
            if isinstance(channel, discord.TextChannel):
                await self._apply_jail_channel_overwrites(channel, jailed_role)
                return channel
            clear_jail_channel_id(guild.id)

        for channel in guild.text_channels:
            if channel.name == JAIL_CHANNEL_NAME:
                set_jail_channel_id(guild.id, channel.id)
                await self._apply_jail_channel_overwrites(channel, jailed_role)
                return channel

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=False,
            ),
            jailed_role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                add_reactions=False,
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True,
            ),
        }

        channel = await guild.create_text_channel(
            name=JAIL_CHANNEL_NAME,
            overwrites=overwrites,
            reason="Gay jail: auto-created shared jail channel",
        )
        set_jail_channel_id(guild.id, channel.id)
        return channel

    @staticmethod
    async def _apply_jail_channel_overwrites(
        channel: discord.TextChannel,
        jailed_role: discord.Role,
    ) -> None:
        guild = channel.guild
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=False,
            ),
            jailed_role: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                add_reactions=False,
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                manage_messages=True,
            ),
        }
        for target, overwrite in overwrites.items():
            await channel.set_permissions(
                target,
                overwrite=overwrite,
                reason="Gay jail: refresh channel permissions",
            )

    # ------------------------------------------------------------------
    # Sentence lifecycle
    # ------------------------------------------------------------------

    def _cancel_release_task(self, guild_id: int, user_id: int) -> None:
        key = (guild_id, user_id)
        task = self._release_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()

    def _schedule_release(self, guild_id: int, user_id: int, release_at: int) -> None:
        self._cancel_release_task(guild_id, user_id)
        delay = max(0.0, release_at - time.time())
        task = asyncio.create_task(
            self._release_after_delay(guild_id, user_id, delay),
            name=f"jail-release-{guild_id}-{user_id}",
        )
        self._release_tasks[(guild_id, user_id)] = task

    async def _release_after_delay(
        self,
        guild_id: int,
        user_id: int,
        delay: float,
    ) -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await self._release_member(guild_id, user_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error(
                "Jail release failed for guild=%s user=%s: %s",
                guild_id,
                user_id,
                exc,
            )
        finally:
            self._release_tasks.pop((guild_id, user_id), None)

    async def _release_member(self, guild_id: int, user_id: int) -> None:
        remove_jail_sentence(guild_id, user_id)

        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        jailed_role_id = get_jail_role_id(guild_id)
        member = guild.get_member(user_id)
        if member and jailed_role_id:
            role = guild.get_role(jailed_role_id)
            if role and role in member.roles:
                try:
                    await member.remove_roles(role, reason="Gay jail: sentence ended")
                except discord.HTTPException as exc:
                    log.warning("Could not remove jailed role from %s: %s", user_id, exc)

        remaining = list_jail_sentences(guild_id)
        if remaining:
            return

        channel_id = get_jail_channel_id(guild_id)
        if channel_id:
            channel = guild.get_channel(channel_id)
            if isinstance(channel, discord.TextChannel):
                try:
                    await channel.delete(reason="Gay jail: no prisoners remaining")
                except discord.HTTPException as exc:
                    log.warning("Could not delete jail channel %s: %s", channel_id, exc)
            clear_jail_channel_id(guild_id)

    async def _restore_active_sentences(self) -> None:
        now = int(time.time())
        for row in list_all_active_jail_sentences():
            guild_id = row["guild_id"]
            user_id = row["user_id"]
            release_at = row["release_at"]
            if release_at <= now:
                await self._release_member(guild_id, user_id)
            else:
                self._schedule_release(guild_id, user_id, release_at)

        log.info("Restored gay-jail sentence timers from database")

    async def _post_jail_requirement(
        self,
        channel: discord.TextChannel,
        member: discord.Member,
    ) -> None:
        requirement = random.choice(JAIL_REQUIREMENTS)
        embed = discord.Embed(
            title="📜 Requirement to leave jail",
            description=(
                f"{member.mention}, while you serve your sentence, your homework is:\n\n"
                f"**{requirement}**"
            ),
            color=discord.Color.orange(),
        )
        embed.set_footer(text="Your timer still has to run out — this is for your soul.")
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as exc:
            log.warning("Could not post jail requirement in %s: %s", channel.id, exc)

    # ------------------------------------------------------------------
    # Slash commands
    # ------------------------------------------------------------------

    @app_commands.command(
        name="sendtogayjail",
        description="Send a member to gay jail for a set time (admins/owner only).",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        member="The member to jail.",
        duration_minutes="How long they stay jailed (minutes, max 7 days).",
    )
    async def sendtogayjail(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        duration_minutes: app_commands.Range[int, 1, MAX_JAIL_MINUTES],
    ) -> None:
        assert interaction.guild is not None and interaction.guild_id is not None

        if not self._can_use_jail_commands(interaction):
            await interaction.response.send_message(
                "❌ Only the server owner or administrators can use this command.",
                ephemeral=True,
            )
            return

        actor = interaction.user
        if not isinstance(actor, discord.Member):
            actor = interaction.guild.get_member(interaction.user.id)
        if actor is None:
            await interaction.response.send_message(
                "❌ Could not resolve your membership.", ephemeral=True
            )
            return

        err = self._can_jail_target(actor, member)
        if err:
            await interaction.response.send_message(f"❌ {err}", ephemeral=True)
            return

        bot_err = self._bot_can_manage(interaction.guild)
        if bot_err:
            await interaction.response.send_message(f"❌ {bot_err}", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        try:
            jailed_role = await self._ensure_jailed_role(interaction.guild)
            jail_channel = await self._ensure_jail_channel(interaction.guild, jailed_role)

            if jailed_role in member.roles:
                pass
            else:
                await member.add_roles(jailed_role, reason=f"Gay jail: {duration_minutes}m")

            release_at = int(time.time()) + duration_minutes * 60
            upsert_jail_sentence(
                interaction.guild_id,
                member.id,
                release_at,
                actor.id,
            )
            self._schedule_release(interaction.guild_id, member.id, release_at)

            await self._post_jail_requirement(jail_channel, member)

            announcement = (
                f"**{member.display_name}** Has been sent to gay jail to think about their actions. "
                f"{jail_channel.mention}"
            )
            if isinstance(interaction.channel, discord.TextChannel):
                await interaction.channel.send(announcement)
            else:
                await jail_channel.send(announcement)

            await interaction.followup.send(
                f"✅ Jailed {member.mention} for **{duration_minutes}** minute(s). "
                f"Channel: {jail_channel.mention}",
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "❌ Missing permissions. Ensure my role is above **jailed** and I can manage channels.",
                ephemeral=True,
            )
        except discord.HTTPException as exc:
            log.error("sendtogayjail failed: %s", exc)
            await interaction.followup.send(
                f"❌ Failed to jail member: `{exc}`", ephemeral=True
            )

    @app_commands.command(
        name="releasefromgayjail",
        description="Release a member from gay jail early (admins/owner only).",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    async def releasefromgayjail(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        assert interaction.guild_id is not None

        if not self._can_use_jail_commands(interaction):
            await interaction.response.send_message(
                "❌ Only the server owner or administrators can use this command.",
                ephemeral=True,
            )
            return

        if not get_jail_sentence(interaction.guild_id, member.id):
            await interaction.response.send_message(
                f"❌ {member.mention} is not in gay jail.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        self._cancel_release_task(interaction.guild_id, member.id)
        await self._release_member(interaction.guild_id, member.id)

        await interaction.followup.send(
            f"✅ Released {member.mention} from gay jail early.",
            ephemeral=True,
        )

        if isinstance(interaction.channel, discord.TextChannel):
            try:
                await interaction.channel.send(
                    f"🔓 **{member.display_name}** has been released from gay jail early."
                )
            except discord.HTTPException:
                pass

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if not get_jail_sentence(member.guild.id, member.id):
            return
        self._cancel_release_task(member.guild.id, member.id)
        await self._release_member(member.guild.id, member.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Jail(bot))
