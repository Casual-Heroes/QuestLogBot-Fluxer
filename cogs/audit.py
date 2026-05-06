# cogs/audit.py - Audit Logging System for QuestLogFluxer
#
# Event handler signatures based on fluxer/client.py _dispatch():
#   on_message_delete(data)       - raw dict: {id, channel_id, guild_id}
#   on_member_join(data)          - raw dict: {guild_id, user: {id, username, ...}, roles, ...}
#   on_member_remove(data)        - raw dict: {guild_id, user: {id, username, ...}}
#   on_channel_create(channel)    - Channel object: .id, .name, .guild_id
#   on_channel_update(channel)    - Channel object
#   on_channel_delete(channel|data) - Channel object OR raw dict if not cached
#   on_guild_ban_add(data)        - raw dict (generic handler): {guild_id, user: {...}}
#   on_guild_ban_remove(data)     - raw dict
#   on_guild_role_create(data)    - raw dict: {guild_id, role: {id, name, ...}}
#   on_guild_role_delete(data)    - raw dict: {guild_id, role_id: '...'}
#   on_message_delete_bulk(data)  - raw dict: {ids, channel_id, guild_id}

import time
from datetime import datetime
from collections import OrderedDict

import fluxer
from fluxer import Cog
from sqlalchemy import text

from config import logger, db_session_scope

# Simple LRU message cache: {message_id: {author_id, author_name, content, channel_id, guild_id}}
# Capped at 5000 entries to limit memory usage
_MSG_CACHE = OrderedDict()
_MSG_CACHE_MAX = 5000


ACTION_CATEGORIES = {
    'member_join':         'members',
    'member_leave':        'members',
    'member_ban':          'moderation',
    'member_unban':        'moderation',
    'role_create':         'roles',
    'role_delete':         'roles',
    'channel_create':      'channels',
    'channel_delete':      'channels',
    'channel_update':      'channels',
    'message_delete':      'messages',
    'message_bulk_delete': 'messages',
}

ACTION_EMOJIS = {
    'member_join': '📥', 'member_leave': '📤',
    'member_ban': '🔨', 'member_unban': '🔓',
    'role_create': '🏷️', 'role_delete': '🗑️',
    'channel_create': '📁', 'channel_delete': '🗑️', 'channel_update': '✏️',
    'message_delete': '🗑️', 'message_bulk_delete': '🗑️',
}

ACTION_LABELS = {
    'member_join': 'Member Joined', 'member_leave': 'Member Left',
    'member_ban': 'Member Banned', 'member_unban': 'Member Unbanned',
    'role_create': 'Role Created', 'role_delete': 'Role Deleted',
    'channel_create': 'Channel Created', 'channel_delete': 'Channel Deleted',
    'channel_update': 'Channel Updated',
    'message_delete': 'Message Deleted', 'message_bulk_delete': 'Bulk Messages Deleted',
}

EMBED_COLORS = {
    'moderation': 0xEF4444,
    'members':    0x3B82F6,
    'roles':      0xEAB308,
    'channels':   0xA855F7,
    'messages':   0x6B7280,
}

# Severity-based color overrides (matches Discord bot 1:1)
_SEVERITY_RED    = {'member_ban', 'member_kick', 'raid_detected', 'lockdown_activated'}
_SEVERITY_GREEN  = {'member_join', 'verification_passed', 'member_unban', 'lockdown_deactivated'}
_SEVERITY_ORANGE = {'member_leave', 'message_delete'}


def _user_tag(user_data):
    name = user_data.get('global_name') or user_data.get('username', 'Unknown')
    disc = user_data.get('discriminator', '0')
    if disc and disc != '0':
        return f"{name}#{disc}"
    return name


def _get_settings(db, guild_id):
    row = db.execute(text(
        "SELECT audit_logging_enabled, audit_log_channel_id "
        "FROM web_fluxer_guild_settings WHERE guild_id = :g LIMIT 1"
    ), {'g': str(guild_id)}).fetchone()
    if not row:
        return False, None, {}
    enabled = bool(row.audit_logging_enabled)
    channel_id = row.audit_log_channel_id or None
    return enabled, channel_id, {}


def _insert_log(db, guild_id, action, actor_id=None, actor_name=None,
                target_id=None, target_name=None, target_type=None,
                reason=None, details=None):
    category = ACTION_CATEGORIES.get(action, 'other')
    db.execute(text(
        "INSERT INTO web_fluxer_audit_log "
        "(guild_id, action, action_category, actor_id, actor_name, target_id, target_name, "
        "target_type, reason, details, created_at) "
        "VALUES (:gid, :act, :cat, :aid, :an, :tid, :tn, :tt, :reason, :details, :ts)"
    ), {
        'gid': str(guild_id), 'act': action, 'cat': category,
        'aid': str(actor_id) if actor_id else None,
        'an':  str(actor_name)[:128] if actor_name else None,
        'tid': str(target_id) if target_id else None,
        'tn':  str(target_name)[:128] if target_name else None,
        'tt':  str(target_type)[:32] if target_type else None,
        'reason':  str(reason)[:1000] if reason else None,
        'details': str(details)[:2000] if details else None,
        'ts': int(time.time()),
    })
    db.commit()


class AuditCog(Cog):
    def __init__(self, bot):
        super().__init__(bot)

    async def _record(self, guild_id, action, actor_id=None, actor_name=None,
                      target_id=None, target_name=None, target_type=None,
                      reason=None, details=None):
        """Persist event to DB and post embed to audit channel if configured."""
        channel_id = None
        try:
            with db_session_scope() as db:
                enabled, channel_id, cfg = _get_settings(db, guild_id)
                if not enabled:
                    return
                if not cfg.get(action, True):
                    return
                _insert_log(db, guild_id, action,
                            actor_id=actor_id, actor_name=actor_name,
                            target_id=target_id, target_name=target_name,
                            target_type=target_type, reason=reason, details=details)
        except Exception as e:
            logger.error(f"AuditCog._record DB error ({action}): {e}")
            return

        if channel_id:
            await self._send_embed(
                channel_id, action,
                actor_id=actor_id, actor_name=actor_name,
                target_id=target_id, target_name=target_name,
                target_type=target_type, reason=reason, details=details,
            )

    async def _send_embed(self, channel_id, action, actor_id=None, actor_name=None,
                          target_id=None, target_name=None, target_type=None,
                          reason=None, details=None):
        # Severity-based color (matches Discord bot)
        if action in _SEVERITY_RED:
            color = 0xEF4444
        elif action in _SEVERITY_GREEN:
            color = 0x22C55E
        elif action in _SEVERITY_ORANGE:
            color = 0xF97316
        else:
            category = ACTION_CATEGORIES.get(action, 'other')
            color = EMBED_COLORS.get(category, 0x6B7280)

        emoji = ACTION_EMOJIS.get(action, '📋')
        label = ACTION_LABELS.get(action, action.replace('_', ' ').title())

        embed = fluxer.Embed(title=f"{emoji} {label}", color=color)

        # Actor field - name + mention
        if actor_name:
            actor_val = actor_name
            if actor_id:
                actor_val += f"\n(<@{actor_id}>)"
            embed.add_field(name="Actor", value=actor_val, inline=True)

        # Target field - type-aware formatting
        if target_name:
            if target_type == 'user' and target_id:
                target_val = f"{target_name}\n(<@{target_id}>)"
            elif target_type == 'role' and target_id:
                target_val = f"{target_name}\n(<@&{target_id}>)"
            elif target_type == 'channel' and target_id:
                target_val = f"{target_name}\n(<#{target_id}>)"
            else:
                target_val = target_name
            embed.add_field(name="Target", value=target_val, inline=True)

        if reason:
            embed.add_field(name="Reason", value=str(reason)[:1024], inline=False)
        if details:
            embed.add_field(name="Details", value=str(details)[:1024], inline=False)

        embed.set_footer(text="QuestLog Audit")
        embed.timestamp = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')

        try:
            await self.bot._http.send_message(str(channel_id), embeds=[embed])
        except Exception as e:
            logger.warning(f"AuditCog: send_message failed for channel {channel_id}: {e}")

    # -------------------------------------------------------------------------
    # Member events - raw dicts
    # -------------------------------------------------------------------------

    @Cog.listener()
    async def on_member_join(self, data):
        try:
            guild_id = str(data.get('guild_id', ''))
            user = data.get('user', data)
            if not guild_id or user.get('bot'):
                return
            user_id = str(user.get('id', ''))
            # Estimate account creation from Discord snowflake ID
            details = None
            try:
                if user_id:
                    created_ts = (int(user_id) >> 22) // 1000 + 1420070400
                    details = f"Account created: <t:{created_ts}:R>"
            except Exception:
                pass
            await self._record(
                guild_id=guild_id, action='member_join',
                target_id=user_id,
                target_name=_user_tag(user), target_type='user',
                details=details,
            )
        except Exception as e:
            logger.error(f"AuditCog on_member_join: {e}")

    @Cog.listener()
    async def on_member_remove(self, data):
        try:
            guild_id = str(data.get('guild_id', ''))
            user = data.get('user', data)
            if not guild_id:
                return
            user_id = str(user.get('id', ''))
            target_name = _user_tag(user)
            # If Fluxer didn't include name fields in the payload, fall back to DB
            if target_name == 'Unknown' and user_id:
                try:
                    with db_session_scope() as db:
                        row = db.execute(
                            text("SELECT username FROM web_fluxer_members WHERE guild_id=:g AND user_id=:u LIMIT 1"),
                            {'g': guild_id, 'u': user_id}
                        ).fetchone()
                        if row and row[0]:
                            target_name = row[0]
                except Exception:
                    pass
            await self._record(
                guild_id=guild_id, action='member_leave',
                target_id=user_id,
                target_name=target_name, target_type='user',
            )
        except Exception as e:
            logger.error(f"AuditCog on_member_remove: {e}")

    # Ban events go through the generic handler as on_guild_ban_add / on_guild_ban_remove
    @Cog.listener()
    async def on_guild_ban_add(self, data):
        try:
            guild_id = str(data.get('guild_id', ''))
            user = data.get('user', {})
            if not guild_id:
                return
            await self._record(
                guild_id=guild_id, action='member_ban',
                target_id=str(user.get('id', '')),
                target_name=_user_tag(user), target_type='user',
            )
        except Exception as e:
            logger.error(f"AuditCog on_guild_ban_add: {e}")

    @Cog.listener()
    async def on_guild_ban_remove(self, data):
        try:
            guild_id = str(data.get('guild_id', ''))
            user = data.get('user', {})
            if not guild_id:
                return
            await self._record(
                guild_id=guild_id, action='member_unban',
                target_id=str(user.get('id', '')),
                target_name=_user_tag(user), target_type='user',
            )
        except Exception as e:
            logger.error(f"AuditCog on_guild_ban_remove: {e}")

    # -------------------------------------------------------------------------
    # Role events - generic handler, raw dicts
    # data = {guild_id, role: {id, name, ...}}
    # -------------------------------------------------------------------------

    @Cog.listener()
    async def on_guild_role_create(self, data):
        try:
            guild_id = str(data.get('guild_id', ''))
            role = data.get('role', {})
            if not guild_id:
                return
            await self._record(
                guild_id=guild_id, action='role_create',
                target_id=str(role.get('id', '')),
                target_name=role.get('name', ''), target_type='role',
            )
        except Exception as e:
            logger.error(f"AuditCog on_guild_role_create: {e}")

    @Cog.listener()
    async def on_guild_role_delete(self, data):
        try:
            guild_id = str(data.get('guild_id', ''))
            if not guild_id:
                return
            # GUILD_ROLE_DELETE only provides role_id, not the role object
            role_id = str(data.get('role_id', ''))
            await self._record(
                guild_id=guild_id, action='role_delete',
                target_id=role_id, target_type='role',
            )
        except Exception as e:
            logger.error(f"AuditCog on_guild_role_delete: {e}")

    # -------------------------------------------------------------------------
    # Channel events - Channel objects (or raw dict if delete and not cached)
    # Channel has: .id (int), .name (str), .guild_id (int|None)
    # -------------------------------------------------------------------------

    @Cog.listener()
    async def on_channel_create(self, channel):
        try:
            guild_id = getattr(channel, 'guild_id', None) or channel.get('guild_id') if isinstance(channel, dict) else None
            if not guild_id:
                return
            name = getattr(channel, 'name', None) or (channel.get('name', '') if isinstance(channel, dict) else '')
            cid = getattr(channel, 'id', None) or (channel.get('id', '') if isinstance(channel, dict) else '')
            await self._record(
                guild_id=str(guild_id), action='channel_create',
                target_id=str(cid), target_name=name, target_type='channel',
            )
        except Exception as e:
            logger.error(f"AuditCog on_channel_create: {e}")

    @Cog.listener()
    async def on_channel_delete(self, channel):
        try:
            guild_id = getattr(channel, 'guild_id', None) or (channel.get('guild_id') if isinstance(channel, dict) else None)
            if not guild_id:
                return
            name = getattr(channel, 'name', None) or (channel.get('name', '') if isinstance(channel, dict) else '')
            cid = getattr(channel, 'id', None) or (channel.get('id', '') if isinstance(channel, dict) else '')
            await self._record(
                guild_id=str(guild_id), action='channel_delete',
                target_id=str(cid), target_name=name, target_type='channel',
            )
        except Exception as e:
            logger.error(f"AuditCog on_channel_delete: {e}")

    @Cog.listener()
    async def on_channel_update(self, channel):
        try:
            guild_id = getattr(channel, 'guild_id', None) or (channel.get('guild_id') if isinstance(channel, dict) else None)
            if not guild_id:
                return
            name = getattr(channel, 'name', None) or (channel.get('name', '') if isinstance(channel, dict) else '')
            cid = getattr(channel, 'id', None) or (channel.get('id', '') if isinstance(channel, dict) else '')
            await self._record(
                guild_id=str(guild_id), action='channel_update',
                target_id=str(cid), target_name=name, target_type='channel',
            )
        except Exception as e:
            logger.error(f"AuditCog on_channel_update: {e}")

    # -------------------------------------------------------------------------
    # Message cache - populate on every message so we have author+content on delete
    # -------------------------------------------------------------------------

    @Cog.listener()
    async def on_message(self, message):
        try:
            if not message.guild_id:
                return
            msg_id = str(message.id)
            _MSG_CACHE[msg_id] = {
                'author_id':   str(message.author.id) if message.author else None,
                'author_name': str(message.author) if message.author else None,
                'content':     (message.content or '')[:500],
                'channel_id':  str(message.channel_id),
                'guild_id':    str(message.guild_id),
            }
            # Evict oldest if over cap
            while len(_MSG_CACHE) > _MSG_CACHE_MAX:
                _MSG_CACHE.popitem(last=False)
        except Exception:
            pass

    # -------------------------------------------------------------------------
    # Message events - raw dicts
    # delete:      {id, channel_id, guild_id}
    # bulk delete: {ids, channel_id, guild_id}
    # -------------------------------------------------------------------------

    def _resolve_channel_name(self, db, guild_id, channel_id):
        """Look up channel name from web_fluxer_guild_channels."""
        try:
            row = db.execute(text(
                "SELECT channel_name FROM web_fluxer_guild_channels "
                "WHERE guild_id = :g AND channel_id = :c LIMIT 1"
            ), {'g': str(guild_id), 'c': str(channel_id)}).fetchone()
            if row and row.channel_name:
                return f"#{row.channel_name}"
        except Exception:
            pass
        return f"#{channel_id}"

    @Cog.listener()
    async def on_message_delete(self, data):
        try:
            guild_id = str(data.get('guild_id', ''))
            channel_id = str(data.get('channel_id', ''))
            msg_id = str(data.get('id', ''))
            if not guild_id:
                return

            cached = _MSG_CACHE.pop(msg_id, None)
            actor_id = None
            actor_name = None
            content_snippet = None

            if cached:
                actor_id = cached.get('author_id')
                actor_name = cached.get('author_name')
                content = cached.get('content', '')
                if content:
                    content_snippet = content[:200] + ('...' if len(content) > 200 else '')

            with db_session_scope() as db:
                enabled, log_channel, _ = _get_settings(db, guild_id)
                if not enabled:
                    return
                channel_name = self._resolve_channel_name(db, guild_id, channel_id)
                # actor = message author (who we know performed or whose msg was deleted)
                # target = the message content
                # details = channel
                target_name = f'"{content_snippet}"' if content_snippet else 'message'
                _insert_log(db, guild_id, 'message_delete',
                            actor_id=actor_id, actor_name=actor_name,
                            target_name=target_name,
                            target_type='message', details=channel_name)

            if log_channel:
                await self._send_embed(
                    log_channel, 'message_delete',
                    actor_id=actor_id, actor_name=actor_name,
                    target_name=target_name, target_type='message',
                    details=f"Channel: <#{channel_id}>\nContent: {content_snippet or '(unknown)'}",
                )
        except Exception as e:
            logger.error(f"AuditCog on_message_delete: {e}")

    @Cog.listener()
    async def on_message_delete_bulk(self, data):
        try:
            guild_id = str(data.get('guild_id', ''))
            channel_id = str(data.get('channel_id', ''))
            ids = data.get('ids', [])
            if not guild_id:
                return
            # Remove cached messages
            for mid in ids:
                _MSG_CACHE.pop(str(mid), None)

            with db_session_scope() as db:
                enabled, log_channel, _ = _get_settings(db, guild_id)
                if not enabled:
                    return
                channel_name = self._resolve_channel_name(db, guild_id, channel_id)
                details = f"{len(ids)} messages deleted in {channel_name}"
                _insert_log(db, guild_id, 'message_bulk_delete',
                            target_type='message', details=details)

            if log_channel:
                await self._send_embed(log_channel, 'message_bulk_delete', details=details)
        except Exception as e:
            logger.error(f"AuditCog on_message_delete_bulk: {e}")
