# cogs/core.py - Core commands: !help, !ping, !info
#
# All responses use embeds for rich formatting in Fluxer.
# TODO: When Fluxer ships slash commands, add /questlog help, /questlog info

import time
import asyncio
import aiohttp
import fluxer
from fluxer import Cog
from config import logger, QUESTLOG_INTERNAL_API_URL, QUESTLOG_BOT_SECRET

BRAND_COLOR = 0x5865F2
GREEN_COLOR = 0x57F287
GOLD_COLOR = 0xFEE75C


SYNC_COOLDOWN = 5          # seconds between event-triggered syncs per guild
PERIODIC_SYNC_INTERVAL = 1800  # 30 minutes full re-sync of all guilds
ACTION_POLL_INTERVAL = 15  # seconds between pending guild-action checks


class CoreCog(Cog):
    def __init__(self, bot):
        super().__init__(bot)
        # Guard: ignore on_guild_remove events that fire during READY reconciliation.
        # Fluxer SDK fires remove+join for every guild on reconnect - we only care about
        # real removals that happen after the bot is fully ready.
        self._bot_ready = False
        # Event-driven sync: cooldown tracking (guild_id -> last sync timestamp)
        self._sync_cooldowns: dict[str, float] = {}
        self._sync_queued: set[str] = set()
        # Role sync: track guilds where 403 was already logged to avoid spam
        self._role_sync_403_logged: set[str] = set()
        self._periodic_task: asyncio.Task | None = None
        self._action_task: asyncio.Task | None = None

    @Cog.listener()
    async def on_ready(self):
        logger.info(f"CoreCog ready - serving {len(self.bot.guilds)} communities")
        # Wait for GUILD_CREATE events so guild objects are fully populated.
        # Fluxer SDK sometimes delivers guilds without names at READY time - 5s gives it time.
        await asyncio.sleep(5)
        asyncio.ensure_future(self._sync_all_guilds())
        self._bot_ready = True
        # Start periodic re-sync loop
        if self._periodic_task is None or self._periodic_task.done():
            self._periodic_task = asyncio.ensure_future(self._periodic_sync_loop())
        if self._action_task is None or self._action_task.done():
            self._action_task = asyncio.ensure_future(self._action_poll_loop())

    @Cog.listener()
    async def on_guild_join(self, guild):
        """New guild - full sync with joined_at set to now."""
        # Fluxer SDK may pass a dict or a guild object
        guild_id = guild['id'] if isinstance(guild, dict) else guild.id
        guild_name = guild.get('name', 'unknown') if isinstance(guild, dict) else getattr(guild, 'name', 'unknown')
        logger.info(f"Joined guild {guild_id} ({guild_name})")
        await asyncio.sleep(1)  # brief delay so guild data is available
        # Re-fetch from bot.guilds so we have a proper guild object for _sync_single_guild
        guild_obj = None
        for g in self.bot.guilds:
            if str(g.id) == str(guild_id):
                guild_obj = g
                break
        if guild_obj:
            asyncio.ensure_future(self._sync_single_guild(guild_obj, is_join=True))
        else:
            # Fallback: POST minimal data with just the id/name from the event dict
            asyncio.ensure_future(self._sync_guild_minimal(str(guild_id), guild_name, is_join=True))

    @Cog.listener()
    async def on_guild_remove(self, guild):
        """Bot removed from guild - mark inactive, preserve all data."""
        # Ignore READY-time reconciliation events (SDK fires remove+join on reconnect)
        if not self._bot_ready:
            return
        guild_id = guild['id'] if isinstance(guild, dict) else guild.id
        guild_name = guild.get('name', 'unknown') if isinstance(guild, dict) else getattr(guild, 'name', 'unknown')
        logger.info(f"Removed from guild {guild_id} ({guild_name})")
        asyncio.ensure_future(self._report_guild_remove(str(guild_id)))

    # -------------------------------------------------------------------------
    # Event-driven sync: role/channel/member changes
    # -------------------------------------------------------------------------

    @Cog.listener()
    async def on_guild_role_create(self, role):
        await self._queue_guild_sync(role)

    @Cog.listener()
    async def on_guild_role_delete(self, role):
        await self._queue_guild_sync(role)

    @Cog.listener()
    async def on_guild_role_update(self, data):
        # Fluxer fires GUILD_ROLE_UPDATE with raw data (no before/after split)
        await self._queue_guild_sync(data)

    @Cog.listener()
    async def on_channel_create(self, channel):
        await self._queue_guild_sync(channel)

    @Cog.listener()
    async def on_channel_delete(self, channel):
        await self._queue_guild_sync(channel)

    @Cog.listener()
    async def on_channel_update(self, channel):
        # Fluxer SDK fires on_channel_update with a single channel object (no before/after)
        await self._queue_guild_sync(channel)

    @Cog.listener()
    async def on_guild_update(self, data):
        # Fired by GUILD_UPDATE (raw dict) - catches icon/name changes
        await self._queue_guild_sync(data)

    @Cog.listener()
    async def on_member_join(self, member):
        await self._queue_guild_sync(member)

    @Cog.listener()
    async def on_member_remove(self, member):
        await self._queue_guild_sync(member)

    async def _queue_guild_sync(self, obj):
        """Queue a guild sync with 5-second cooldown (port of WardenBot guild_sync.py)."""
        if not self._bot_ready:
            return
        # Extract guild_id from whatever object the SDK passes
        guild_id = None
        if isinstance(obj, dict):
            guild_id = str(obj.get('guild_id') or obj.get('id') or '')
        else:
            gid = getattr(obj, 'guild_id', None) or getattr(obj, 'id', None)
            guild_id = str(gid) if gid else None
        if not guild_id:
            return

        now = time.time()
        last_sync = self._sync_cooldowns.get(guild_id, 0)
        if now - last_sync < SYNC_COOLDOWN:
            # Already synced recently - queue a deferred sync if not already queued
            if guild_id not in self._sync_queued:
                self._sync_queued.add(guild_id)
                wait = SYNC_COOLDOWN - (now - last_sync)
                asyncio.ensure_future(self._deferred_sync(guild_id, wait))
        else:
            self._sync_cooldowns[guild_id] = now
            asyncio.ensure_future(self._sync_guild_by_id(guild_id))

    async def _deferred_sync(self, guild_id: str, delay: float):
        await asyncio.sleep(delay)
        self._sync_queued.discard(guild_id)
        self._sync_cooldowns[guild_id] = time.time()
        await self._sync_guild_by_id(guild_id)

    async def _sync_guild_by_id(self, guild_id: str):
        """Find guild object and sync it."""
        for g in self.bot.guilds:
            if str(g.id) == guild_id:
                try:
                    await self._sync_single_guild(g, is_join=False)
                except Exception as e:
                    logger.warning(f"Event-triggered sync failed for {guild_id}: {e}")
                return
        logger.debug(f"_sync_guild_by_id: guild {guild_id} not in bot.guilds, skipping")

    async def _periodic_sync_loop(self):
        """Full re-sync of all guilds every 30 minutes (port of WardenBot guild_sync_cog.py)."""
        while True:
            await asyncio.sleep(PERIODIC_SYNC_INTERVAL)
            logger.info(f"Periodic sync: re-syncing {len(self.bot.guilds)} guilds")
            await self._sync_all_guilds()

    async def _action_poll_loop(self):
        """Poll for pending dashboard-initiated guild actions every 15 seconds."""
        await asyncio.sleep(10)  # Small startup delay
        _base = QUESTLOG_INTERNAL_API_URL.rstrip('/')
        _headers = {'X-Bot-Secret': QUESTLOG_BOT_SECRET}
        while True:
            for guild in list(self.bot.guilds):
                guild_id = str(guild.id) if guild.id else ''
                if not guild_id:
                    continue
                try:
                    await self._execute_pending_actions(guild, guild_id, _base, _headers)
                except Exception as e:
                    logger.debug(f"Action poll error for guild {guild_id}: {e}")
            await asyncio.sleep(ACTION_POLL_INTERVAL)

    async def _execute_pending_actions(self, guild, guild_id: str, base_url: str, headers: dict):
        """Fetch and execute pending actions for one guild."""
        url = f'{base_url}/api/internal/guild-actions/?guild_id={guild_id}'
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()

        for action in data.get('actions', []):
            action_id = action['id']
            action_type = action.get('action_type', '')
            payload = action.get('payload', {})
            done_url = f'{base_url}/api/internal/guild-actions/{action_id}/done/'

            try:
                if action_type == 'create_role':
                    role = await guild.create_role(
                        name=payload.get('name', 'New Role'),
                        permissions=int(payload.get('permissions', 0) or 0),
                        color=int(payload.get('color', 0) or 0),
                        hoist=bool(payload.get('hoist', False)),
                        mentionable=bool(payload.get('mentionable', False)),
                    )
                    # Re-sync roles so the new role appears in the dashboard
                    await self._push_guild_roles_for(guild_id, guild=guild)
                    async with aiohttp.ClientSession() as session:
                        await session.post(done_url, json={
                            'success': True,
                            'result': {'role_id': str(role.id), 'role_name': role.name},
                        }, headers=headers, timeout=aiohttp.ClientTimeout(total=5))
                    logger.info(f"Created role '{role.name}' in guild {guild_id}")
                elif action_type == 'sync_guild':
                    # Full re-sync of channels, roles, members for this guild
                    await self._sync_single_guild(guild, is_join=False)
                    async with aiohttp.ClientSession() as session:
                        await session.post(done_url, json={
                            'success': True,
                            'result': {'message': 'Guild synced'},
                        }, headers=headers, timeout=aiohttp.ClientTimeout(total=5))
                    logger.info(f"sync_guild action complete for guild {guild_id}")
                elif action_type == 'check_games':
                    discovery_cog = self.bot.cogs.get('DiscoveryCog')
                    if discovery_cog is None:
                        raise ValueError("DiscoveryCog not loaded")
                    count = await discovery_cog.run_for_guild_now(guild_id)
                    async with aiohttp.ClientSession() as session:
                        await session.post(done_url, json={
                            'success': True,
                            'result': {'new_games': count},
                        }, headers=headers, timeout=aiohttp.ClientTimeout(total=5))
                    logger.info(f"check_games action complete for guild {guild_id}: {count} new games")
                elif action_type == 'rss_force_send':
                    rss_cog = self.bot.cogs.get('RssCog')
                    if rss_cog is None:
                        raise ValueError("RssCog not loaded")
                    feed_id = int(payload.get('feed_id', 0) or 0)
                    success, message = await rss_cog.force_send_feed(guild_id, feed_id)
                    async with aiohttp.ClientSession() as session:
                        await session.post(done_url, json={
                            'success': success,
                            'result': {'message': message},
                        }, headers=headers, timeout=aiohttp.ClientTimeout(total=5))
                    logger.info(f"rss_force_send action complete for guild {guild_id} feed {feed_id}: {message}")
                elif action_type == 'send_embed':
                    channel_id = str(payload.get('channel_id', '') or '')
                    if not channel_id:
                        raise ValueError("channel_id is required")
                    color_raw = payload.get('color', '#ea580c') or '#ea580c'
                    color_int = int(color_raw.lstrip('#'), 16) if color_raw.startswith('#') else 0xea580c
                    embed = fluxer.Embed(
                        title=payload.get('title') or None,
                        description=payload.get('description') or None,
                        color=color_int,
                    )
                    if payload.get('footer'):
                        embed.set_footer(text=str(payload['footer'])[:256])
                    await self.bot._http.send_message(channel_id, embed=embed)
                    async with aiohttp.ClientSession() as session:
                        await session.post(done_url, json={
                            'success': True,
                            'result': {'message': 'Embed sent'},
                        }, headers=headers, timeout=aiohttp.ClientTimeout(total=5))
                    logger.info(f"send_embed action complete for guild {guild_id} channel {channel_id}")
                elif action_type == 'apply_role_template':
                    _PERM_BITS = {
                        'create_instant_invite': 1 << 0, 'kick_members': 1 << 1,
                        'ban_members': 1 << 2, 'administrator': 1 << 3,
                        'manage_channels': 1 << 4, 'manage_guild': 1 << 5,
                        'add_reactions': 1 << 6, 'view_audit_log': 1 << 7,
                        'priority_speaker': 1 << 8, 'stream': 1 << 9,
                        'view_channel': 1 << 10, 'send_messages': 1 << 11,
                        'send_tts_messages': 1 << 12, 'manage_messages': 1 << 13,
                        'embed_links': 1 << 14, 'attach_files': 1 << 15,
                        'read_message_history': 1 << 16, 'mention_everyone': 1 << 17,
                        'use_external_emojis': 1 << 18, 'connect': 1 << 20,
                        'speak': 1 << 21, 'mute_members': 1 << 22,
                        'deafen_members': 1 << 23, 'move_members': 1 << 24,
                        'use_voice_activation': 1 << 25, 'change_nickname': 1 << 26,
                        'manage_nicknames': 1 << 27, 'manage_roles': 1 << 28,
                        'manage_webhooks': 1 << 29, 'manage_emojis_and_stickers': 1 << 30,
                        'use_external_stickers': 1 << 33, 'moderate_members': 1 << 40,
                        'pin_messages': 1 << 51, 'bypass_slowmode': 1 << 52,
                    }
                    roles_created = []
                    for role_def in payload.get('template_data', []):
                        perms = 0
                        for p in (role_def.get('permissions') or []):
                            perms |= _PERM_BITS.get(p, 0)
                        color_hex = (role_def.get('color') or '#99aab5').lstrip('#')
                        color_int = int(color_hex, 16) if color_hex else 0
                        role = await guild.create_role(
                            name=role_def.get('name', 'New Role'),
                            permissions=perms,
                            color=color_int,
                            hoist=bool(role_def.get('hoist', False)),
                            mentionable=bool(role_def.get('mentionable', False)),
                        )
                        roles_created.append(role.name)
                    await self._push_guild_roles_for(guild_id, guild=guild)
                    async with aiohttp.ClientSession() as session:
                        await session.post(done_url, json={
                            'success': True,
                            'result': {'roles_created': roles_created},
                        }, headers=headers, timeout=aiohttp.ClientTimeout(total=5))
                    logger.info(f"apply_role_template: created {len(roles_created)} roles in guild {guild_id}")
                elif action_type == 'apply_channel_template':
                    _CHANNEL_TYPES = {'text': 0, 'voice': 2, 'announcement': 5, 'forum': 15, 'stage': 13}
                    channels_created = []
                    for cat_def in payload.get('template_data', []):
                        cat_name = cat_def.get('category_name', 'New Category')
                        cat_resp = await self.bot._http.create_guild_channel(
                            guild_id, name=cat_name, type=4
                        )
                        cat_id = str(cat_resp.get('id', ''))
                        channels_created.append(cat_name)
                        for ch in (cat_def.get('channels') or []):
                            ch_type = _CHANNEL_TYPES.get(ch.get('type', 'text'), 0)
                            ch_resp = await self.bot._http.create_guild_channel(
                                guild_id,
                                name=ch.get('name', 'new-channel'),
                                type=ch_type,
                                topic=ch.get('topic') or None,
                                parent_id=cat_id if cat_id else None,
                            )
                            channels_created.append(ch.get('name', ''))
                    async with aiohttp.ClientSession() as session:
                        await session.post(done_url, json={
                            'success': True,
                            'result': {'channels_created': channels_created},
                        }, headers=headers, timeout=aiohttp.ClientTimeout(total=5))
                    logger.info(f"apply_channel_template: created {len(channels_created)} items in guild {guild_id}")
                else:
                    logger.warning(f"Unknown action_type '{action_type}' for guild {guild_id}")
                    async with aiohttp.ClientSession() as session:
                        await session.post(done_url, json={
                            'success': False, 'error': f'Unknown action_type: {action_type}',
                        }, headers=headers, timeout=aiohttp.ClientTimeout(total=5))

            except Exception as e:
                logger.warning(f"Action {action_id} ({action_type}) failed for guild {guild_id}: {e}")
                try:
                    async with aiohttp.ClientSession() as session:
                        await session.post(done_url, json={
                            'success': False, 'error': str(e),
                        }, headers=headers, timeout=aiohttp.ClientTimeout(total=5))
                except Exception:
                    pass

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    async def _sync_all_guilds(self):
        """On startup / periodic: sync every guild the bot is currently in."""
        if not QUESTLOG_BOT_SECRET:
            return
        for guild in self.bot.guilds:
            # Sync even if guild.name is None - the API can handle empty names
            # and will update them once the SDK populates guild objects
            try:
                await self._sync_single_guild(guild, is_join=False)
                await asyncio.sleep(0.5)  # rate-limit outbound requests
            except Exception as e:
                logger.warning(f"Guild sync failed for {getattr(guild, 'id', '?')}: {e}")

    async def _sync_single_guild(self, guild, is_join: bool = False):
        """Build and POST full guild snapshot to the QuestLog site."""
        if not QUESTLOG_BOT_SECRET:
            return

        guild_id = str(guild.id)
        guild_name = getattr(guild, 'name', None) or ''
        owner_id = str(getattr(guild, 'owner_id', '') or '')
        icon = getattr(guild, 'icon', None)
        guild_icon_hash = str(icon) if icon else None

        # owner_id is not reliably present in GUILD_CREATE payload from Fluxer SDK.
        # GET /v1/guilds/{id} returns 500 on Fluxer's side - skip the HTTP call to avoid
        # 5-retry delay on every startup. owner_id will be populated from DB if already stored.

        # Member counts
        member_count = getattr(guild, 'member_count', 0) or 0
        # online_count is not reliably available without presence intent - send 0
        online_count = 0

        # Channels - start from bot._channels cache (populated via GUILD_CREATE/CHANNEL_CREATE),
        # then supplement with HTTP fetch to catch any channels the cache missed (e.g. created
        # while the bot was offline).
        channels = []
        seen_ids: set[str] = set()
        try:
            for ch in self.bot._channels.values():
                if str(getattr(ch, 'guild_id', None) or '') != guild_id:
                    continue
                ch_id = str(ch.id)
                ch_name = ch.name or str(ch.id)
                channels.append({
                    'id': ch_id,
                    'name': ch_name,
                    'type': int(getattr(ch, 'type', 0) or 0),
                    'category_name': '',
                })
                seen_ids.add(ch_id)
        except Exception as e:
            logger.debug(f"Could not read channels from cache for {guild_id}: {e}")

        # Also try HTTP to catch channels not in the cache
        try:
            ch_data = await self.bot.http.get_guild_channels(guild_id)
            for ch in (ch_data or []):
                ch_id = str(ch.get('id', '') or '')
                ch_name = str(ch.get('name', '') or '')
                if not ch_id or not ch_name or ch_id in seen_ids:
                    continue
                channels.append({
                    'id': ch_id,
                    'name': ch_name,
                    'type': int(ch.get('type', 0) or 0),
                    'category_name': str(ch.get('category_name', '') or ''),
                })
                seen_ids.add(ch_id)
                # Also update the local cache so future event-triggered syncs are accurate
                from fluxer.models.channel import Channel as FluxerChannel
                try:
                    fc = FluxerChannel.from_data(ch, self.bot._http)
                    self.bot._channels[fc.id] = fc
                except Exception:
                    pass
        except Exception as e2:
            logger.debug(f"HTTP channel fetch failed for {guild_id}: {e2}")

        # Emojis
        emojis = []
        try:
            em_data = await self.bot.http.get_guild_emojis(guild_id)
            for em in (em_data or []):
                em_id = str(em.get('id', '') or '')
                em_name = str(em.get('name', '') or '')
                if not em_id or not em_name:
                    continue
                emojis.append({
                    'id': em_id,
                    'name': em_name,
                    'animated': bool(em.get('animated', False)),
                })
        except Exception as e:
            logger.debug(f"Could not fetch emojis for {guild_id}: {e}")

        # Members (non-bots) - try HTTP API first, fall back to guild.fetch_members()
        members = []
        try:
            mem_data = await self.bot.http.get_guild_members(guild_id, limit=1000)
            for m in (mem_data or []):
                if m.get('bot') or (m.get('user', {}) or {}).get('bot'):
                    continue
                user = m.get('user', m)
                m_id = str(user.get('id', '') or '')
                if not m_id:
                    continue
                members.append({
                    'id': m_id,
                    'username': str(user.get('username', '') or ''),
                    'display_name': str(m.get('nick', '') or user.get('username', '') or ''),
                    'avatar': str(user.get('avatar', '') or ''),
                    'roles': [str(r) for r in (m.get('roles', []) or [])],
                })
        except Exception as e:
            logger.debug(f"HTTP get_guild_members failed for {guild_id}: {e}")

        # Fallback: guild.fetch_members() - this endpoint works in Fluxer
        if not members:
            try:
                fetched = await guild.fetch_members(limit=1000)
                for m in (fetched or []):
                    if getattr(m.user, 'bot', False):
                        continue
                    m_id = str(m.user.id)
                    members.append({
                        'id': m_id,
                        'username': str(m.user.username or ''),
                        'display_name': str(m.user.global_name or m.user.username or ''),
                        'avatar': str(m.user.avatar_hash or ''),
                        'roles': [str(r) for r in (m.roles or [])],
                    })
                if members:
                    logger.debug(f"fetch_members() got {len(members)} members for {guild_id}")
            except Exception as e:
                logger.debug(f"fetch_members() also failed for {guild_id}: {e}")

        # Prefer actual fetched member count if it's higher (Fluxer SDK guild.member_count can be stale)
        if members and len(members) > member_count:
            member_count = len(members)

        # Roles (also sync via the existing guild-roles endpoint)
        try:
            await self._push_guild_roles_for(guild_id, guild=guild)
            self._role_sync_403_logged.discard(guild_id)  # Reset on success
        except Exception as e:
            if '403' in str(e) or 'MISSING_PERMISSIONS' in str(e):
                if guild_id not in self._role_sync_403_logged:
                    self._role_sync_403_logged.add(guild_id)
                    logger.warning(
                        f"Role sync for {guild_id}: bot lacks permission to list roles on Fluxer. "
                        f"This warning will not repeat until next restart."
                    )
                else:
                    logger.debug(f"Role sync 403 suppressed for {guild_id} (already logged)")
            else:
                logger.warning(f"Role sync failed for {guild_id}: {e}")

        # Build payload
        payload = {
            'guild_id': guild_id,
            'guild_name': guild_name,
            'owner_id': owner_id,
            'guild_icon_hash': guild_icon_hash,
            'member_count': member_count,
            'online_count': online_count,
            'channels': channels,
            'emojis': emojis,
            'members': members,
        }
        if is_join:
            payload['joined_at'] = int(time.time())

        url = f'{QUESTLOG_INTERNAL_API_URL}/api/internal/guild-sync/'
        headers = {'X-Bot-Secret': QUESTLOG_BOT_SECRET}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        d = await resp.json()
                        verb = 'joined' if is_join else 'synced'
                        logger.info(
                            f"Guild {verb}: {guild_id} ({guild_name}) - "
                            f"{'created' if d.get('created') else 'updated'} | "
                            f"{len(channels)} channels, {len(members)} members"
                        )
                    else:
                        logger.warning(f"Guild sync failed for {guild_id}: HTTP {resp.status}")
        except Exception as e:
            logger.warning(f"Guild sync error for {guild_id}: {e}")

    async def _report_guild_remove(self, guild_id: str):
        """POST to site to mark guild as inactive (bot removed)."""
        if not QUESTLOG_BOT_SECRET:
            return
        url = f'{QUESTLOG_INTERNAL_API_URL}/api/internal/guild-remove/'
        headers = {'X-Bot-Secret': QUESTLOG_BOT_SECRET}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json={'guild_id': guild_id},
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        logger.info(f"Guild {guild_id} marked inactive on site")
                    else:
                        logger.warning(f"Guild remove report failed for {guild_id}: HTTP {resp.status}")
        except Exception as e:
            logger.warning(f"Guild remove report error for {guild_id}: {e}")

    async def _sync_guild_minimal(self, guild_id: str, guild_name: str, is_join: bool = False):
        """Fallback: POST just guild_id and guild_name when full guild object is unavailable."""
        if not QUESTLOG_BOT_SECRET:
            return
        payload = {'guild_id': guild_id, 'guild_name': guild_name}
        if is_join:
            payload['joined_at'] = int(time.time())
        url = f'{QUESTLOG_INTERNAL_API_URL}/api/internal/guild-sync/'
        headers = {'X-Bot-Secret': QUESTLOG_BOT_SECRET}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=payload, headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        logger.info(f"Minimal guild sync: {guild_id} ({guild_name})")
                    else:
                        logger.warning(f"Minimal guild sync failed for {guild_id}: HTTP {resp.status}")
        except Exception as e:
            logger.warning(f"Minimal guild sync error for {guild_id}: {e}")

    async def _push_guild_roles_for(self, guild_id: str, guild=None):
        """Sync roles for one guild to the site (used by full guild sync)."""
        # Resolve guild object - needed for guild.fetch_roles()
        if guild is None:
            guild = next((g for g in self.bot.guilds if str(g.id) == str(guild_id)), None)
        if guild is None:
            logger.debug(f"_push_guild_roles_for: guild {guild_id} not found in bot.guilds")
            return

        role_objects = await guild.fetch_roles()
        roles = [
            {
                'id': str(r.id),
                'name': r.name,
                'color': int(getattr(r, 'color', 0) or 0),
                'position': int(getattr(r, 'position', 0) or 0),
                'managed': bool(getattr(r, 'managed', False)),
            }
            for r in (role_objects or [])
            if r.name and r.name != '@everyone'
        ]
        url = f'{QUESTLOG_INTERNAL_API_URL}/api/internal/guild-roles/'
        headers = {'X-Bot-Secret': QUESTLOG_BOT_SECRET}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json={'guild_id': guild_id, 'roles': roles},
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    logger.info(f"Role sync: {len(roles)} roles synced for guild {guild_id}")
                else:
                    logger.warning(f"Role sync failed for {guild_id}: HTTP {resp.status}")

    @Cog.command()
    async def ping(self, ctx):
        """!ping - Check bot latency."""
        embed = fluxer.Embed(
            title="Pong!",
            description="QuestLog Bot is online and responding.",
            color=GREEN_COLOR,
        )
        embed.set_footer(text="QuestLog Bot | casual-heroes.com/ql/")
        await ctx.reply(embed=embed)

    @Cog.command()
    async def help(self, ctx):
        """!help - Show member commands."""
        embed = fluxer.Embed(
            title="QuestLog Bot - Member Commands",
            description="Your gaming community companion on Fluxer.",
            color=BRAND_COLOR,
            url="https://casual-heroes.com/ql/",
        )

        embed.add_field(
            name="General",
            value=(
                "`!ping` - Check bot status\n"
                "`!help` - This menu\n"
                "`!info` - About QuestLog Bot\n"
                "`!invite` - Get an invite link to this server"
            ),
            inline=False,
        )

        embed.add_field(
            name="XP & Levels",
            value=(
                "`!xp` - Your XP, level, and rank\n"
                "`!leaderboard` - Top 10 members by XP\n"
                "`!heroshop` - Browse and buy flairs with Hero Points"
            ),
            inline=False,
        )

        embed.add_field(
            name="Looking for Group",
            value=(
                "`!lfg` - Post or browse this server's LFG groups\n"
                "`!lfgql` - Browse the full QuestLog Network LFG\n"
                "`!lfglist` - List active LFG groups in this server\n"
                "`!lfgjoin <id>` - Join a LFG group by ID\n"
                "`!lfg delete <id>` - Delete your own LFG group"
            ),
            inline=False,
        )

        embed.add_field(
            name="Creator Features",
            value=(
                "`!raffle` - Enter the active raffle (if running)\n"
                "`!cotw` - See the current Creator of the Week\n"
                "`!cotm` - See the current Creator of the Month"
            ),
            inline=False,
        )

        embed.add_field(
            name="Game Servers",
            value=(
                "`!gs_status` - Status of all game servers\n"
                "`!gs_players` - Who is online across all servers"
            ),
            inline=False,
        )

        embed.set_footer(text="Server staff: use !admin_help for admin commands - casual-heroes.com/ql/")
        await ctx.reply(embed=embed)

    @Cog.command()
    async def admin_help(self, ctx):
        """!admin_help - Show staff commands based on your permission level."""
        from cogs.permissions import is_administrator, is_moderator, is_bot_manager
        try:
            can_admin = await is_administrator(ctx)
            can_mod   = await is_moderator(ctx)
            can_gs    = await is_bot_manager(ctx)
        except Exception:
            can_admin = can_mod = can_gs = False

        if not (can_admin or can_mod or can_gs):
            deny = fluxer.Embed(description='You do not have permission to view admin commands.', color=0xFF4444)
            await ctx.reply(embed=deny)
            return

        embed = fluxer.Embed(
            title="QuestLog Bot - Staff Commands",
            description="Showing commands available to your permission level.",
            color=BRAND_COLOR,
            url="https://casual-heroes.com/ql/",
        )

        # Game server commands - Bot Manager role, Manage Messages, or Administrator
        if can_gs or can_mod or can_admin:
            embed.add_field(
                name="Game Servers",
                value=(
                    "`!gs_serverinfo <instance>` - Post/refresh live pinned server panel\n"
                    "`!gs_start <instance>` - Start a game server\n"
                    "`!gs_stop <instance>` - Stop a game server\n"
                    "`!gs_restart <instance>` - Restart a game server\n"
                    "`!gs_backup <instance>` - Trigger a backup\n"
                    "\nExample: `!gs_start CH-VRising01`"
                ),
                inline=False,
            )

        # Moderation commands - Manage Messages or Administrator
        if can_mod or can_admin:
            embed.add_field(
                name="Moderation",
                value=(
                    "`!ban @user [reason]` - Permanent ban\n"
                    "`!tempban @user <hours> [reason]` - Temporary ban\n"
                    "`!kick @user [reason]` - Kick member\n"
                    "`!timeout @user <minutes> [reason]` - Timeout member"
                ),
                inline=False,
            )

        # Admin-only commands - Administrator only
        if can_admin:
            embed.add_field(
                name="Admin / Owner Only",
                value=(
                    "`!setup` - Configure bot settings for this server\n"
                    "`!checkgames` - Force-run game discovery now\n"
                    "`!refreshtrackers` - Force-refresh game activity trackers\n"
                    "`!checkrss` - Force-run all RSS feeds now"
                ),
                inline=False,
            )

        embed.set_footer(text="Full platform: casual-heroes.com/ql/")
        await ctx.reply(embed=embed)

    @Cog.command()
    async def info(self, ctx):
        """!info - Bot information."""
        guild_count = len(self.bot.guilds)
        embed = fluxer.Embed(
            title="QuestLog Bot",
            description="Free and open source gaming community bot for Fluxer.",
            color=BRAND_COLOR,
            url="https://casual-heroes.com/ql/",
        )
        embed.add_field(name="Communities Served", value=str(guild_count), inline=True)
        embed.add_field(name="Platform", value="Fluxer", inline=True)
        embed.add_field(name="Source", value="[GitHub](https://github.com/Casual-Heroes/QuestLogBot-Fluxer)", inline=True)
        embed.add_field(name="Web Platform", value="[casual-heroes.com/ql/](https://casual-heroes.com/ql/)", inline=True)
        embed.set_footer(text="QuestLog - Gaming Communities, Reimagined")
        await ctx.reply(embed=embed)
