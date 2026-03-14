# cogs/flair_sync.py - Flair role sync
#
# Polls fluxer_pending_role_updates every 10 seconds.
# When a QuestLog user equips or unequips a flair on the site, this cog:
#   1. Looks up their fluxer_id from web_users
#   2. For every guild the bot shares with that user:
#      - Removes all roles whose name starts with "Flair: "
#      - If action='set_flair': finds or creates "Flair: {emoji} {name}" role and assigns it
# Role color: 0 (no color - default transparent)
# Role position: just below the bot's highest role so the bot can manage it

import asyncio
import time
import fluxer
from fluxer import Cog
from sqlalchemy import text
from config import logger, db_session_scope

POLL_INTERVAL = 10    # seconds between polls
FLAIR_ROLE_PREFIX = 'Flair: '
FLAIR_ROLE_COLOR  = 0   # no color - display as default grey


class FlairSyncCog(Cog):
    def __init__(self, bot):
        super().__init__(bot)
        self._sync_task = None

    async def cog_load(self):
        self._sync_task = asyncio.ensure_future(self._poll_loop())
        logger.info('FlairSyncCog: poll loop started')

    async def cog_unload(self):
        if self._sync_task:
            self._sync_task.cancel()

    @Cog.listener()
    async def on_ready(self):
        if not self._sync_task or self._sync_task.done():
            self._sync_task = asyncio.ensure_future(self._poll_loop())
            logger.info('FlairSyncCog: poll loop restarted on reconnect')

    async def _poll_loop(self):
        await asyncio.sleep(5)  # brief startup delay
        while True:
            try:
                await self._process_pending_updates()
            except Exception as e:
                logger.error(f'FlairSync poll loop error: {e}', exc_info=True)
            await asyncio.sleep(POLL_INTERVAL)

    async def _process_pending_updates(self):
        with db_session_scope() as db:
            rows = db.execute(text(
                "SELECT id, web_user_id, action, flair_emoji, flair_name "
                "FROM fluxer_pending_role_updates "
                "WHERE processed_at IS NULL "
                "ORDER BY created_at ASC LIMIT 20"
            )).fetchall()

            if not rows:
                return

            for row in rows:
                row_id, web_user_id, action, flair_emoji, flair_name = row
                try:
                    await self._apply_flair_update(web_user_id, action, flair_emoji or '', flair_name or '')
                except Exception as e:
                    logger.warning(f'FlairSync: error processing row {row_id} for user {web_user_id}: {e}')

                # Mark as processed regardless - avoid infinite retry on hard failures
                db.execute(text(
                    "UPDATE fluxer_pending_role_updates SET processed_at = :now WHERE id = :id"
                ), {'now': int(time.time()), 'id': row_id})

            db.commit()

    async def _apply_flair_update(self, web_user_id: int, action: str, flair_emoji: str, flair_name: str):
        """Apply flair role change across all guilds the bot shares with this user."""
        # Look up fluxer_id from linked QuestLog account
        with db_session_scope() as db:
            result = db.execute(text(
                "SELECT fluxer_id FROM web_users WHERE id = :uid AND fluxer_id IS NOT NULL"
            ), {'uid': web_user_id}).fetchone()

        if not result or not result[0]:
            return  # User hasn't linked their Fluxer account

        fluxer_user_id = int(result[0])
        http = self.bot._http if hasattr(self.bot, '_http') else None
        if not http:
            logger.warning('FlairSync: bot has no _http client')
            return

        for guild in self.bot.guilds:
            try:
                await self._sync_guild_flair(http, guild.id, fluxer_user_id, action, flair_emoji, flair_name)
            except Exception as e:
                # Member not in this guild or other transient error - skip
                logger.debug(f'FlairSync: skipped guild {guild.id} for user {fluxer_user_id}: {e}')

    async def _sync_guild_flair(self, http, guild_id: int, user_id: int,
                                 action: str, flair_emoji: str, flair_name: str):
        """Update flair role for user in a single guild."""
        # Fetch member and all guild roles
        try:
            member_data = await http.get_guild_member(guild_id, user_id)
        except Exception:
            return  # User not in this guild

        roles_data = await http.get_guild_roles(guild_id)
        member_role_ids = {int(r) for r in member_data.get('roles', [])}

        # Build a map of role name -> role id for existing flair roles
        flair_roles = {r['name']: int(r['id']) for r in roles_data if r['name'].startswith(FLAIR_ROLE_PREFIX)}

        # Remove all current flair roles from member
        for role_id in list(member_role_ids):
            role = next((r for r in roles_data if int(r['id']) == role_id), None)
            if role and role['name'].startswith(FLAIR_ROLE_PREFIX):
                await http.remove_guild_member_role(guild_id, user_id, role_id,
                                                     reason='QuestLog flair update')

        if action == 'set_flair' and (flair_emoji or flair_name):
            target_name = f'{FLAIR_ROLE_PREFIX}{flair_emoji} {flair_name}'.strip()

            # Find or create the flair role
            role_id = flair_roles.get(target_name)
            if role_id:
                # Fix color on existing flair roles if they still have the old purple color
                existing = next((r for r in roles_data if int(r['id']) == role_id), None)
                if existing and existing.get('color', 0) != 0:
                    try:
                        await http.modify_guild_role(guild_id, role_id, color=0)
                    except Exception:
                        pass
            if not role_id:
                new_role = await http.create_guild_role(
                    guild_id,
                    name=target_name,
                    color=FLAIR_ROLE_COLOR,
                    reason='QuestLog flair role auto-created',
                )
                role_id = int(new_role['id'])
                logger.info(f'FlairSync: created role "{target_name}" in guild {guild_id}')

                # Position just below the bot's highest role so it can be managed.
                # Bot's managed roles have 'managed': True in roles_data.
                # Fall back to finding the highest position role the bot holds via bot.user.id.
                try:
                    bot_member = await http.get_guild_member(guild_id, self.bot.user.id)
                    bot_role_ids = {int(r) for r in bot_member.get('roles', [])}
                    # Re-fetch roles now that we've created the new one
                    updated_roles = await http.get_guild_roles(guild_id)
                    # Find the highest position among the bot's roles (managed = bot role)
                    bot_top_pos = 0
                    for r in updated_roles:
                        if int(r['id']) in bot_role_ids or r.get('managed'):
                            pos = r.get('position', 0)
                            if pos > bot_top_pos:
                                bot_top_pos = pos
                    # Place flair role one below bot's top role (bot_top_pos - 1, min 1)
                    target_pos = max(1, bot_top_pos - 1)
                    await http.modify_guild_role(
                        guild_id, role_id,
                        position=target_pos,
                    )
                    logger.info(f'FlairSync: positioned "{target_name}" at {target_pos} in guild {guild_id}')
                except Exception as e:
                    logger.debug(f'FlairSync: could not reposition flair role: {e}')

            await http.add_guild_member_role(guild_id, user_id, role_id,
                                              reason='QuestLog flair update')


def setup(bot):
    bot.add_cog(FlairSyncCog(bot))
