# cogs/member_sync.py - Guild Member Sync
#
# Syncs Fluxer guild members to web_fluxer_members table.
#
# Sources:
#   - Startup:         Try guild.fetch_members(); fall back to refreshing known users via fetch_user()
#   - on_member_join:  Capture new member data immediately
#   - on_member_remove: Mark member as left (left_at timestamp)
#   - Background:      Every 6 hours, refresh profiles for users last synced >1 day ago
#
# NOTE: Fluxer API does not yet implement GET /guilds/{id}/members list.
#       Startup sync will capture nothing new until that endpoint is live.
#       on_member_join and organic capture from messages fills the table over time.

import time
import json
import asyncio

from fluxer import Cog
from sqlalchemy import text
from config import logger, db_session_scope

# How often to run background profile refresh (seconds)
_REFRESH_INTERVAL = 6 * 3600  # 6 hours

# Max members to refresh per run (to avoid rate limiting)
_REFRESH_BATCH = 200

# Re-sync profiles older than this (seconds)
_REFRESH_AGE = 86400  # 1 day


def _upsert_member(db, guild_id: int, user_id: int, username: str,
                   global_name: str | None, avatar_hash: str | None,
                   roles: list | None = None, joined_at: int | None = None):
    """Upsert a single member record into web_fluxer_members."""
    now = int(time.time())
    roles_json = json.dumps(roles) if roles else None
    db.execute(text(
        "INSERT INTO web_fluxer_members "
        "(guild_id, user_id, username, global_name, avatar_hash, roles, joined_at, left_at, last_seen, synced_at) "
        "VALUES (:g, :u, :un, :gn, :av, :ro, :ja, NULL, :now, :now) "
        "ON DUPLICATE KEY UPDATE "
        "username = :un, "
        "global_name = COALESCE(:gn, global_name), "
        "avatar_hash = COALESCE(:av, avatar_hash), "
        "roles = COALESCE(:ro, roles), "
        "joined_at = COALESCE(joined_at, :ja), "
        "left_at = NULL, "
        "last_seen = :now, "
        "synced_at = :now"
    ), {
        'g': guild_id, 'u': user_id, 'un': username, 'gn': global_name,
        'av': avatar_hash, 'ro': roles_json, 'ja': joined_at, 'now': now,
    })


class MemberSyncCog(Cog):
    """Syncs guild member profiles to the database."""

    def __init__(self, bot):
        super().__init__(bot)
        self._refresh_task = None

    # -------------------------------------------------------------------------
    # Startup sync
    # -------------------------------------------------------------------------

    @Cog.listener()
    async def on_ready(self):
        """On startup, sync members for all guilds."""
        await asyncio.sleep(6)  # Let GUILD_CREATE events settle
        logger.info(f"MemberSyncCog: syncing members for {len(self.bot.guilds)} guilds")
        for guild in self.bot.guilds:
            try:
                await self._sync_guild_members(guild)
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"MemberSyncCog: startup sync failed for guild {getattr(guild, 'id', '?')}: {e}")

        # Start background refresh loop
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.ensure_future(self._background_refresh_loop())
        logger.info("MemberSyncCog: startup sync complete")

    async def _sync_guild_members(self, guild):
        """Try to fetch and store all members for a guild."""
        guild_id = int(guild.id)

        # Attempt guild member list (may return empty if Fluxer doesn't support it yet)
        try:
            members = await guild.fetch_members(limit=1000)
            if members:
                logger.info(f"MemberSyncCog: got {len(members)} members from API for guild {guild_id}")
                with db_session_scope() as db:
                    for m in members:
                        if getattr(m.user, 'bot', False):
                            continue
                        _upsert_member(
                            db,
                            guild_id=guild_id,
                            user_id=int(m.user.id),
                            username=m.user.username or '',
                            global_name=m.user.global_name,
                            avatar_hash=m.user.avatar_hash,
                            roles=[str(r) for r in (m.roles or [])],
                            joined_at=None,
                        )
                    db.commit()
                return
            else:
                logger.debug(f"MemberSyncCog: guild.fetch_members() returned empty for {guild_id} (API limitation)")
        except Exception as e:
            logger.debug(f"MemberSyncCog: guild.fetch_members() failed for {guild_id}: {e}")

        # Fall back: refresh profiles of users we already know in this guild
        await self._refresh_known_users(guild_id, limit=_REFRESH_BATCH)

    async def _refresh_known_users(self, guild_id: int, limit: int = _REFRESH_BATCH):
        """Refresh Fluxer user profiles for members already in web_fluxer_members
        or in fluxer_member_xp (fallback source).
        Calls bot.fetch_user() for each and updates the DB.
        """
        # Gather user IDs to refresh (stale records first)
        cutoff = int(time.time()) - _REFRESH_AGE
        try:
            with db_session_scope() as db:
                rows = db.execute(text(
                    "SELECT user_id FROM web_fluxer_members "
                    "WHERE guild_id = :g AND synced_at < :cutoff "
                    "ORDER BY synced_at ASC LIMIT :lim"
                ), {'g': guild_id, 'cutoff': cutoff, 'lim': limit}).fetchall()
                known_ids = [r[0] for r in rows]

                if not known_ids:
                    # First time: seed from fluxer_member_xp (users who have chatted)
                    rows2 = db.execute(text(
                        "SELECT user_id, username FROM fluxer_member_xp "
                        "WHERE guild_id = :g ORDER BY last_active DESC LIMIT :lim"
                    ), {'g': guild_id, 'lim': limit}).fetchall()
                    known_ids = [r[0] for r in rows2]
                    if not known_ids:
                        return
        except Exception as e:
            logger.debug(f"MemberSyncCog: DB read failed for guild {guild_id}: {e}")
            return

        logger.debug(f"MemberSyncCog: refreshing {len(known_ids)} user profiles for guild {guild_id}")
        refreshed = 0
        for user_id in known_ids:
            try:
                user = await self.bot.fetch_user(str(user_id))
                with db_session_scope() as db:
                    _upsert_member(
                        db,
                        guild_id=guild_id,
                        user_id=user_id,
                        username=user.username or '',
                        global_name=user.global_name,
                        avatar_hash=user.avatar_hash,
                    )
                    db.commit()
                refreshed += 1
                await asyncio.sleep(0.3)  # Rate limit: ~3 req/sec
            except Exception as e:
                logger.debug(f"MemberSyncCog: fetch_user({user_id}) failed: {e}")

        if refreshed:
            logger.info(f"MemberSyncCog: refreshed {refreshed} profiles for guild {guild_id}")

    # -------------------------------------------------------------------------
    # Event listeners
    # -------------------------------------------------------------------------

    @Cog.listener()
    async def on_member_join(self, data):
        """Capture new member data when someone joins."""
        try:
            guild_id = int(data.get('guild_id', 0))
            user_data = data.get('user', data)
            user_id = int(user_data.get('id', 0))
            if not guild_id or not user_id:
                return
            if user_data.get('bot'):
                return

            username = user_data.get('username', '')
            global_name = user_data.get('global_name')
            avatar_hash = user_data.get('avatar')
            roles = data.get('roles', [])

            with db_session_scope() as db:
                _upsert_member(
                    db,
                    guild_id=guild_id,
                    user_id=user_id,
                    username=username,
                    global_name=global_name,
                    avatar_hash=avatar_hash,
                    roles=[str(r) for r in roles],
                    joined_at=int(time.time()),
                )
                db.commit()
            logger.debug(f"MemberSyncCog: captured join - {username} ({user_id}) in guild {guild_id}")
        except Exception as e:
            logger.error(f"MemberSyncCog: on_member_join error: {e}")

    @Cog.listener()
    async def on_member_remove(self, data):
        """Mark member as left when they leave the guild."""
        try:
            guild_id = int(data.get('guild_id', 0))
            user_data = data.get('user', data)
            user_id = int(user_data.get('id', 0))
            if not guild_id or not user_id:
                return

            now = int(time.time())
            with db_session_scope() as db:
                db.execute(text(
                    "UPDATE web_fluxer_members SET left_at = :now WHERE guild_id = :g AND user_id = :u"
                ), {'now': now, 'g': guild_id, 'u': user_id})
                db.commit()
            logger.debug(f"MemberSyncCog: marked left - user {user_id} from guild {guild_id}")
        except Exception as e:
            logger.error(f"MemberSyncCog: on_member_remove error: {e}")

    @Cog.listener()
    async def on_message(self, message):
        """Capture user profile from message author (keeps data fresh organically)."""
        try:
            if message.author.bot:
                return
            guild_id = int(getattr(message, 'guild_id', 0) or 0)
            if not guild_id:
                return

            user = message.author
            user_id = int(user.id)
            username = getattr(user, 'username', '') or ''
            global_name = getattr(user, 'global_name', None)
            avatar_hash = getattr(user, 'avatar_hash', None)

            # Only update if we haven't seen this user recently (last 1 hour) to avoid DB spam
            now = int(time.time())
            cache_key = f"{guild_id}:{user_id}"
            if now - _seen_cache.get(cache_key, 0) < 3600:
                return
            _seen_cache[cache_key] = now

            with db_session_scope() as db:
                _upsert_member(
                    db,
                    guild_id=guild_id,
                    user_id=user_id,
                    username=username,
                    global_name=global_name,
                    avatar_hash=avatar_hash,
                )
                db.commit()
        except Exception:
            pass  # Silent - don't interrupt message flow

    # -------------------------------------------------------------------------
    # Background refresh loop
    # -------------------------------------------------------------------------

    async def _background_refresh_loop(self):
        """Every 6 hours, refresh stale user profiles via Fluxer API."""
        while True:
            await asyncio.sleep(_REFRESH_INTERVAL)
            logger.info("MemberSyncCog: background profile refresh started")
            for guild in self.bot.guilds:
                try:
                    guild_id = int(guild.id)
                    await self._refresh_known_users(guild_id, limit=_REFRESH_BATCH)
                    await asyncio.sleep(2)
                except Exception as e:
                    logger.error(f"MemberSyncCog: background refresh failed for {getattr(guild, 'id', '?')}: {e}")
            logger.info("MemberSyncCog: background profile refresh complete")


# Module-level cache to avoid flooding the DB from on_message
# {guild_id:user_id -> last_upsert_ts}
_seen_cache: dict[str, float] = {}


def setup(bot):
    bot.add_cog(MemberSyncCog(bot))
