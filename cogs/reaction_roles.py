# cogs/reaction_roles.py - Reaction Roles
#
# When a member reacts to a configured message with a configured emoji,
# they are assigned the mapped role. Removing the reaction removes the role
# (if remove_on_unreact=1, which is the default).
#
# Commands (admin/mod only):
#   !reactionrole add <message_id> <emoji> <role_id>   - configure a reaction role
#   !reactionrole remove <message_id> <emoji>          - remove a reaction role mapping
#   !reactionrole list                                  - list all configured reaction roles
#
# DB table: fluxer_react_roles
#   guild_id, channel_id, message_id, emoji, role_id, role_name, remove_on_unreact, created_at

import time
import asyncio

from fluxer import Cog
from sqlalchemy import text
from config import logger, db_session_scope


class ReactionRolesCog(Cog):
    def __init__(self, bot):
        super().__init__(bot)

    @Cog.listener()
    async def on_ready(self):
        logger.info("ReactionRolesCog: ready")

    # ------------------------------------------------------------------
    # Listeners
    # ------------------------------------------------------------------

    @Cog.listener()
    async def on_raw_reaction_add(self, payload):
        """Assign role when member reacts with a configured emoji."""
        if payload.user_id == self.bot.user.id:
            return

        guild_id  = str(getattr(payload, 'guild_id',  None) or '')
        message_id = str(getattr(payload, 'message_id', None) or '')
        emoji_str  = str(payload.emoji) if payload.emoji else ''

        if not guild_id or not message_id or not emoji_str:
            return

        try:
            with db_session_scope() as db:
                row = db.execute(text(
                    "SELECT role_id FROM fluxer_react_roles "
                    "WHERE guild_id = :g AND message_id = :m AND emoji = :e LIMIT 1"
                ), {'g': guild_id, 'm': message_id, 'e': emoji_str}).fetchone()

            if not row:
                return

            role_id = int(row[0])
            await self.bot._http.add_guild_member_role(
                int(guild_id), int(payload.user_id), role_id,
                reason='Reaction role'
            )
            logger.debug(f"ReactionRoles: assigned role {role_id} to user {payload.user_id} in guild {guild_id}")
        except Exception as e:
            logger.warning(f"ReactionRoles: add failed guild={guild_id} user={payload.user_id}: {e}")

    @Cog.listener()
    async def on_raw_reaction_remove(self, payload):
        """Remove role when member removes a configured reaction."""
        if payload.user_id == self.bot.user.id:
            return

        guild_id   = str(getattr(payload, 'guild_id',  None) or '')
        message_id = str(getattr(payload, 'message_id', None) or '')
        emoji_str  = str(payload.emoji) if payload.emoji else ''

        if not guild_id or not message_id or not emoji_str:
            return

        try:
            with db_session_scope() as db:
                row = db.execute(text(
                    "SELECT role_id, remove_on_unreact FROM fluxer_react_roles "
                    "WHERE guild_id = :g AND message_id = :m AND emoji = :e LIMIT 1"
                ), {'g': guild_id, 'm': message_id, 'e': emoji_str}).fetchone()

            if not row or not row[1]:
                return

            role_id = int(row[0])
            await self.bot._http.remove_guild_member_role(
                int(guild_id), int(payload.user_id), role_id,
                reason='Reaction role removed'
            )
            logger.debug(f"ReactionRoles: removed role {role_id} from user {payload.user_id} in guild {guild_id}")
        except Exception as e:
            logger.warning(f"ReactionRoles: remove failed guild={guild_id} user={payload.user_id}: {e}")

    # ------------------------------------------------------------------
    # Commands
    # ------------------------------------------------------------------

    @Cog.command(name='reactionrole')
    async def reaction_role_cmd(self, ctx, action: str = '', *args):
        """Manage reaction roles. Usage: !reactionrole add/remove/list"""
        from cogs.permissions import is_admin_or_mod
        if not await is_admin_or_mod(ctx):
            await ctx.send("You need admin or mod permissions to manage reaction roles.")
            return

        action = action.lower()

        if action == 'add':
            await self._cmd_add(ctx, args)
        elif action == 'remove':
            await self._cmd_remove(ctx, args)
        elif action == 'list':
            await self._cmd_list(ctx)
        else:
            await ctx.send(
                "**Reaction Role Commands:**\n"
                "`!reactionrole add <message_id> <emoji> <role_id>` - Add a reaction role\n"
                "`!reactionrole remove <message_id> <emoji>` - Remove a reaction role\n"
                "`!reactionrole list` - List all reaction roles"
            )

    async def _cmd_add(self, ctx, args):
        if len(args) < 3:
            await ctx.send("Usage: `!reactionrole add <message_id> <emoji> <role_id>`")
            return

        message_id, emoji, role_id = str(args[0]), str(args[1]), str(args[2])
        guild_id   = str(ctx.guild.id)
        channel_id = str(ctx.channel.id)

        try:
            with db_session_scope() as db:
                existing = db.execute(text(
                    "SELECT id FROM fluxer_react_roles "
                    "WHERE guild_id = :g AND message_id = :m AND emoji = :e LIMIT 1"
                ), {'g': guild_id, 'm': message_id, 'e': emoji}).fetchone()

                if existing:
                    await ctx.send(f"A reaction role for `{emoji}` on that message already exists. Remove it first.")
                    return

                # Try to get role name from guild roles
                role_name = role_id
                try:
                    roles = await self.bot._http.get_guild_roles(int(guild_id))
                    for r in (roles or []):
                        if str(r.get('id')) == role_id:
                            role_name = r.get('name', role_id)
                            break
                except Exception:
                    pass

                db.execute(text(
                    "INSERT INTO fluxer_react_roles "
                    "(guild_id, channel_id, message_id, emoji, role_id, role_name, remove_on_unreact, created_at) "
                    "VALUES (:g, :ch, :m, :e, :r, :rn, 1, :ts)"
                ), {
                    'g': guild_id, 'ch': channel_id, 'm': message_id,
                    'e': emoji, 'r': role_id, 'rn': role_name, 'ts': int(time.time())
                })

            await ctx.send(f"Reaction role set: {emoji} on message `{message_id}` -> **{role_name}**")
        except Exception as e:
            logger.error(f"ReactionRoles: add cmd failed: {e}")
            await ctx.send("Failed to save reaction role.")

    async def _cmd_remove(self, ctx, args):
        if len(args) < 2:
            await ctx.send("Usage: `!reactionrole remove <message_id> <emoji>`")
            return

        message_id, emoji = str(args[0]), str(args[1])
        guild_id = str(ctx.guild.id)

        try:
            with db_session_scope() as db:
                result = db.execute(text(
                    "DELETE FROM fluxer_react_roles "
                    "WHERE guild_id = :g AND message_id = :m AND emoji = :e"
                ), {'g': guild_id, 'm': message_id, 'e': emoji})

            if result.rowcount:
                await ctx.send(f"Removed reaction role for {emoji} on message `{message_id}`.")
            else:
                await ctx.send("No matching reaction role found.")
        except Exception as e:
            logger.error(f"ReactionRoles: remove cmd failed: {e}")
            await ctx.send("Failed to remove reaction role.")

    async def _cmd_list(self, ctx):
        guild_id = str(ctx.guild.id)
        try:
            with db_session_scope() as db:
                rows = db.execute(text(
                    "SELECT message_id, emoji, role_name, role_id FROM fluxer_react_roles "
                    "WHERE guild_id = :g ORDER BY created_at DESC"
                ), {'g': guild_id}).fetchall()

            if not rows:
                await ctx.send("No reaction roles configured for this server.")
                return

            lines = ["**Reaction Roles:**"]
            for message_id, emoji, role_name, role_id in rows:
                lines.append(f"- Message `{message_id}` | {emoji} -> **{role_name}** (`{role_id}`)")
            await ctx.send('\n'.join(lines))
        except Exception as e:
            logger.error(f"ReactionRoles: list cmd failed: {e}")
            await ctx.send("Failed to fetch reaction roles.")


def setup(bot):
    bot.add_cog(ReactionRolesCog(bot))
