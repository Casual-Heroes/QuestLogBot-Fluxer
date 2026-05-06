# cogs/lfg.py - Looking for Group
#
# !lfg                  - Browse LFG groups for this server (link to portal)
# !lfg create           - Link to create an LFG group on QuestLog
# !lfg games            - List configured LFG games for this server
# !lfg list             - Alias for !lfglist
# !lfg delete <id>      - Delete your LFG group (or any group if admin)
# !lfg leave <id>       - Leave an LFG group you joined
# !lfg fluxer           - Link to this server's Fluxer LFG portal
# !lfglist              - Show active LFG groups for this server
# !lfgjoin <id>         - Get link to join a group on QuestLog
# !lfgql                - Link to the public QuestLog Network LFG
# !setup lfg [#channel] - Register this channel for LFG announcements
# !setup status         - Show current bot config

import asyncio
import json
import re
import time
import aiohttp
import fluxer
from fluxer import Cog
from sqlalchemy import text
from config import logger, db_session_scope, IGDB_CLIENT_ID, IGDB_CLIENT_SECRET

POLL_INTERVAL = 5      # seconds between broadcast polls

# Per-user command cooldowns: {"cmd:user_id" -> last_use_ts}
_lfg_cmd_cooldowns: dict[str, float] = {}
_LFG_CMD_COOLDOWN = 10.0  # seconds


def _lfg_cooldown(cmd: str, user_id: str) -> bool:
    """Returns True (on cooldown) or False (ok - updates timestamp)."""
    key = f"{cmd}:{user_id}"
    now = time.time()
    if now - _lfg_cmd_cooldowns.get(key, 0) < _LFG_CMD_COOLDOWN:
        return True
    _lfg_cmd_cooldowns[key] = now
    return False

GOLD_COLOR   = 0xFEE75C
GREEN_COLOR  = 0x57F287
RED_COLOR    = 0xED4245
PURPLE_COLOR = 0xA855F7

QUESTLOG_LFG_URL    = "https://casual-heroes.com/ql/lfg/"
QUESTLOG_REGISTER_URL = "https://casual-heroes.com/ql/register/"
QL_BASE             = "https://casual-heroes.com"

# IGDB token cache
_igdb_token: str | None = None
_igdb_token_expires: float = 0.0


# ---------------------------------------------------------------------------
# IGDB helpers
# ---------------------------------------------------------------------------

async def _igdb_ensure_token() -> str | None:
    global _igdb_token, _igdb_token_expires
    if _igdb_token and time.time() < _igdb_token_expires:
        return _igdb_token
    if not IGDB_CLIENT_ID or not IGDB_CLIENT_SECRET:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://id.twitch.tv/oauth2/token",
                params={
                    "client_id": IGDB_CLIENT_ID,
                    "client_secret": IGDB_CLIENT_SECRET,
                    "grant_type": "client_credentials",
                },
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
        _igdb_token = data["access_token"]
        _igdb_token_expires = time.time() + data.get("expires_in", 3600) - 300
        return _igdb_token
    except Exception as e:
        logger.warning(f"IGDB token refresh failed: {e}")
        return None


async def igdb_lookup(game_name: str) -> tuple[str | None, str | None]:
    """Look up a game on IGDB using a case-insensitive exact match.

    Returns (canonical_name, cover_url). Either may be None.
    The canonical_name is IGDB's authoritative casing (e.g. "World of Warcraft").
    """
    token = await _igdb_ensure_token()
    if not token:
        return None, None
    try:
        # Strip IGDB QL special chars - only allow alphanumeric, spaces, and safe punctuation
        safe_name = re.sub(r'[^\w\s\-\':\.!&]', '', game_name)[:100].strip()
        if not safe_name:
            return None, None
        body = (
            f'fields name, cover.image_id; '
            f'where name ~ "{safe_name}" & version_parent = null; '
            f'limit 3; '
        )
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.igdb.com/v4/games",
                headers={
                    "Client-ID": IGDB_CLIENT_ID,
                    "Authorization": f"Bearer {token}",
                },
                data=body,
                timeout=aiohttp.ClientTimeout(total=8),
            ) as resp:
                games = await resp.json()
        if not games:
            return None, None
        canonical_name = games[0].get("name") or None
        cover = games[0].get("cover", {})
        image_id = cover.get("image_id") if isinstance(cover, dict) else None
        cover_url = f"https://images.igdb.com/igdb/image/upload/t_cover_big/{image_id}.jpg" if image_id else None
        return canonical_name, cover_url
    except Exception as e:
        logger.warning(f"IGDB lookup failed for '{game_name}': {e}")
        return None, None


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _get_web_user_by_fluxer_id(fluxer_user_id: str):
    """Return (web_user_id, web_username) for a linked QuestLog account, or (None, None)."""
    try:
        with db_session_scope() as db:
            row = db.execute(
                text(
                    "SELECT id, username FROM web_users "
                    "WHERE fluxer_id = :fid AND is_banned = 0 AND is_disabled = 0 LIMIT 1"
                ),
                {"fid": str(fluxer_user_id)},
            ).fetchone()
            if row:
                return row.id, row.username
    except Exception as e:
        logger.error(f"_get_web_user_by_fluxer_id failed: {e}", exc_info=True)
    return None, None


def _delete_web_lfg_group(group_id: int, web_user_id: int, force: bool = False) -> tuple[bool, str]:
    """Mark an LFG group as cancelled. Returns (success, error_msg)."""
    try:
        with db_session_scope() as db:
            row = db.execute(
                text("SELECT creator_web_user_id, status FROM web_fluxer_lfg_groups WHERE id = :id"),
                {"id": group_id},
            ).fetchone()
            if not row:
                return False, "No group found with that ID."
            if row.status == 'cancelled':
                return False, "That group has already been deleted."
            if not force and row.creator_web_user_id != web_user_id:
                return False, "You can only delete your own LFG groups."
            db.execute(
                text("UPDATE web_fluxer_lfg_groups SET status = 'cancelled' WHERE id = :id"),
                {"id": group_id},
            )
            db.commit()
            return True, ""
    except Exception as e:
        logger.error(f"_delete_web_lfg_group failed: {e}", exc_info=True)
        return False, "Database error. Please try again."


async def _is_guild_admin(ctx) -> bool:
    """Returns True if the author is guild owner or has Administrator permission.
    Delegates to permissions.py is_administrator.
    """
    from cogs.permissions import is_administrator
    return await is_administrator(ctx)


# ---------------------------------------------------------------------------
# BROADCAST (commented out - enable once network broadcast is fully set up)
# ---------------------------------------------------------------------------
# def _queue_network_broadcast(group_id: int, game_name: str, title: str,
#                               cover_url, group_size: int, description,
#                               group_url: str) -> int:
#     """Queue LFG embed to all opted-in Fluxer communities. Returns count queued."""
#     try:
#         desc_parts = []
#         if description:
#             desc_parts.append(description)
#         desc_parts.append(f"[View & Join on QuestLog]({group_url})")
#         embed_data = {
#             "title": f"LFG - {game_name}: {title}",
#             "description": "\n\n".join(desc_parts),
#             "url": group_url,
#             "color": GOLD_COLOR,
#             "fields": [
#                 {"name": "Game", "value": game_name, "inline": True},
#                 {"name": "Group Size", "value": f"1/{group_size}", "inline": True},
#             ],
#             "footer": "QuestLog Network - casual-heroes.com/ql/lfg/",
#         }
#         if cover_url:
#             embed_data["thumbnail"] = cover_url
#         now = int(time.time())
#         with db_session_scope() as db:
#             configs = db.execute(
#                 text(
#                     "SELECT guild_id, channel_id FROM web_community_bot_configs "
#                     "WHERE platform = 'fluxer' AND event_type = 'lfg_announce' "
#                     "AND is_enabled = 1 AND channel_id IS NOT NULL"
#                 )
#             ).fetchall()
#             for cfg in configs:
#                 db.execute(
#                     text(
#                         "INSERT INTO fluxer_pending_broadcasts "
#                         "(guild_id, channel_id, payload, created_at) "
#                         "VALUES (:guild_id, :channel_id, :payload, :now)"
#                     ),
#                     {
#                         "guild_id": cfg.guild_id,
#                         "channel_id": cfg.channel_id,
#                         "payload": json.dumps(embed_data),
#                         "now": now,
#                     },
#                 )
#             db.commit()
#             return len(configs)
#     except Exception as e:
#         logger.error(f"_queue_network_broadcast failed: {e}", exc_info=True)
#         return 0


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class LfgCog(Cog):
    def __init__(self, bot):
        super().__init__(bot)
        self._broadcast_task: asyncio.Task | None = None

    # -----------------------------------------------------------------------
    # Background: poll fluxer_pending_broadcasts
    # -----------------------------------------------------------------------

    @Cog.listener()
    async def on_ready(self):
        if self._broadcast_task is None or self._broadcast_task.done():
            self._broadcast_task = asyncio.ensure_future(self._broadcast_poll_loop())
            logger.info("LFG broadcast poll loop started")
        # Delay so GUILD_CREATE events have time to arrive before we read bot.guilds
        await asyncio.sleep(2)
        await self._sync_guild_names()
        await self._sync_guild_channels()

    @Cog.listener()
    async def on_guild_join(self, guild):
        """Sync channels when the bot joins a new guild."""
        await self._sync_guild_channels(guild_ids=[guild.id])

    @Cog.listener()
    async def on_channel_create(self, channel):
        gid = getattr(channel, 'guild_id', None)
        if gid:
            await self._sync_guild_channels(guild_ids=[gid])

    @Cog.listener()
    async def on_channel_delete(self, channel):
        gid = getattr(channel, 'guild_id', None) or (channel.get('guild_id') if isinstance(channel, dict) else None)
        if gid:
            await self._sync_guild_channels(guild_ids=[gid])

    @Cog.listener()
    async def on_channel_update(self, channel):
        gid = getattr(channel, 'guild_id', None)
        if gid:
            await self._sync_guild_channels(guild_ids=[gid])

    async def _sync_guild_names(self):
        """Back-fill guild_name for any configs where it is NULL or empty."""
        try:
            with db_session_scope() as db:
                rows = db.execute(
                    text(
                        "SELECT id, guild_id FROM web_community_bot_configs "
                        "WHERE platform = 'fluxer' AND (guild_name IS NULL OR guild_name = '')"
                    )
                ).fetchall()
                # Build lookup from the bot's cached guild list (no get_guild in fluxer)
                guild_map = {str(g.id): g.name for g in self.bot.guilds}
                for row in rows:
                    name = guild_map.get(str(row.guild_id))
                    if name:
                        db.execute(
                            text(
                                "UPDATE web_community_bot_configs SET guild_name = :name "
                                "WHERE id = :id"
                            ),
                            {"name": name, "id": row.id},
                        )
                        logger.info(f"Synced guild_name '{name}' for config id={row.id}")
        except Exception as e:
            logger.error(f"_sync_guild_names error: {e}", exc_info=True)

    async def _broadcast_poll_loop(self):
        """Dispatch pending web-originated LFG broadcasts to configured channels."""
        await asyncio.sleep(3)  # give bot a moment to finish connecting
        while True:
            try:
                await self._dispatch_pending_broadcasts()
            except Exception as e:
                logger.error(f"Broadcast poll loop error: {e}", exc_info=True)
            await asyncio.sleep(POLL_INTERVAL)

    def _build_embed(self, embed_data: dict) -> fluxer.Embed:
        """Build a fluxer.Embed from a payload dict."""
        embed = fluxer.Embed(
            title=embed_data.get("title", ""),
            description=embed_data.get("description", ""),
            color=embed_data.get("color", GOLD_COLOR),
            url=embed_data.get("url") or None,
        )
        for field in embed_data.get("fields", []):
            embed.add_field(
                name=field["name"],
                value=field["value"],
                inline=field.get("inline", True),
            )
        if embed_data.get("thumbnail"):
            embed.set_thumbnail(url=embed_data["thumbnail"])
        if embed_data.get("footer"):
            embed.set_footer(text=embed_data["footer"])
        return embed

    async def _pin_message(self, channel_id: str, message_id: str):
        """Pin a message via raw API call."""
        try:
            route = self.bot._http._route(
                "PUT", "/channels/{channel_id}/pins/{message_id}",
                channel_id=channel_id, message_id=message_id,
            )
            await self.bot._http.request(route)
        except Exception as e:
            logger.warning(f"Failed to pin message {message_id} in channel {channel_id}: {e}")

    async def _unpin_message(self, channel_id: str, message_id: str):
        """Unpin a message via raw API call."""
        try:
            route = self.bot._http._route(
                "DELETE", "/channels/{channel_id}/pins/{message_id}",
                channel_id=channel_id, message_id=message_id,
            )
            await self.bot._http.request(route)
        except Exception as e:
            logger.warning(f"Failed to unpin message {message_id} in channel {channel_id}: {e}")

    async def _dispatch_pending_broadcasts(self):
        try:
            with db_session_scope() as db:
                rows = db.execute(
                    text(
                        "SELECT id, guild_id, channel_id, payload FROM fluxer_pending_broadcasts "
                        "WHERE dispatched_at IS NULL "
                        "ORDER BY created_at ASC LIMIT 10"
                    )
                ).fetchall()

                if not rows:
                    return

                now = int(time.time())
                dispatched_ids = []

                for row_id, guild_id, channel_id, payload_json in rows:
                    try:
                        embed_data = json.loads(payload_json)
                        action = embed_data.get("action", "post")
                        track_group_id = embed_data.get("track_group_id")
                        track_group_platform = embed_data.get("track_group_platform", "web")

                        if action == "edit" and track_group_id:
                            # Edit the existing pinned message in-place
                            existing = db.execute(
                                text(
                                    "SELECT message_id, channel_id FROM web_lfg_channel_messages "
                                    "WHERE group_id=:gid AND group_platform=:gp AND platform='fluxer' AND guild_id=:guild "
                                    "LIMIT 1"
                                ),
                                {"gid": int(track_group_id), "gp": track_group_platform, "guild": str(guild_id)},
                            ).fetchone()
                            if existing:
                                stored_msg_id, stored_ch_id = existing
                                embed = self._build_embed(embed_data)
                                try:
                                    await self.bot._http.edit_message(
                                        str(stored_ch_id), str(stored_msg_id),
                                        embeds=[embed.to_dict()],
                                    )
                                    # Re-pin if group reopened (action carries pin_state)
                                    pin_state = embed_data.get("pin_state")  # "pin" or "unpin"
                                    if pin_state == "unpin":
                                        await self._unpin_message(str(stored_ch_id), str(stored_msg_id))
                                    elif pin_state == "pin":
                                        await self._pin_message(str(stored_ch_id), str(stored_msg_id))
                                except Exception as edit_err:
                                    logger.warning(f"Failed to edit message {stored_msg_id}: {edit_err}")
                            dispatched_ids.append(row_id)
                            continue

                        # action == "post" - send new message, pin it, store ID
                        embed = self._build_embed(embed_data)
                        logger.info(f"_dispatch_pending_broadcasts: sending to guild={guild_id} channel={channel_id}")
                        resp = await self.bot._http.send_message(
                            str(channel_id),
                            embed=embed,
                        )
                        new_message_id = str(resp.get("id")) if resp and resp.get("id") else None
                        logger.info(f"_dispatch_pending_broadcasts: sent message_id={new_message_id} to channel={channel_id}")

                        if new_message_id:
                            # Pin only LFG group posts (not new_member / new_post / other notifications)
                            if track_group_id:
                                await self._pin_message(str(channel_id), new_message_id)

                            # Store message ID for future edits
                            if track_group_id and guild_id:
                                db.execute(
                                    text(
                                        "DELETE FROM web_lfg_channel_messages "
                                        "WHERE group_id=:gid AND group_platform=:gp AND platform='fluxer' AND guild_id=:guild"
                                    ),
                                    {"gid": int(track_group_id), "gp": track_group_platform, "guild": str(guild_id)},
                                )
                                db.execute(
                                    text(
                                        "INSERT INTO web_lfg_channel_messages "
                                        "(group_id, group_platform, platform, guild_id, channel_id, message_id, created_at) "
                                        "VALUES (:gid, :gp, 'fluxer', :guild, :ch, :mid, :ts)"
                                    ),
                                    {
                                        "gid": int(track_group_id),
                                        "gp": track_group_platform,
                                        "guild": str(guild_id),
                                        "ch": str(channel_id),
                                        "mid": new_message_id,
                                        "ts": now,
                                    },
                                )

                        dispatched_ids.append(row_id)
                    except Exception as e:
                        logger.warning(f"Failed to dispatch broadcast {row_id} to channel {channel_id}: {e}")
                        dispatched_ids.append(row_id)  # mark done anyway to avoid infinite retry

                if dispatched_ids:
                    params = {"now": now}
                    for _i, _rid in enumerate(dispatched_ids):
                        params[f"id_{_i}"] = int(_rid)
                    id_keys = ", ".join(f":id_{_i}" for _i in range(len(dispatched_ids)))
                    db.execute(
                        text(
                            f"UPDATE fluxer_pending_broadcasts "
                            f"SET dispatched_at = :now "
                            f"WHERE id IN ({id_keys})"
                        ),
                        params,
                    )
                    db.commit()
        except Exception as e:
            logger.error(f"_dispatch_pending_broadcasts failed: {e}", exc_info=True)

    async def _sync_guild_channels(self, guild_ids: list | None = None):
        """Sync text channels from all (or specified) guilds to web_fluxer_guild_channels."""
        try:
            now = int(time.time())

            # Collect guild_id -> guild_name mapping from cache
            guild_names: dict[str, str] = {}
            for g in self.bot.guilds:
                gid = str(g.id)
                if guild_ids and g.id not in guild_ids:
                    continue
                guild_names[gid] = (getattr(g, 'name', '') or '')

            target_guild_ids = list(guild_names.keys()) if not guild_ids else [str(g) for g in guild_ids]

            # Build entries from bot._channels cache first
            entries: dict[str, tuple] = {}  # channel_id -> (guild_id, guild_name, channel_id, channel_name, channel_type)
            for ch in self.bot._channels.values():
                if ch.guild_id is None:
                    continue
                gid = str(ch.guild_id)
                if guild_ids and ch.guild_id not in guild_ids:
                    continue
                if ch.type not in (0, 5):  # 0=text, 5=news
                    continue
                gname = guild_names.get(gid) or (ch._guild.name if (ch._guild and ch._guild.name) else '') or ''
                entries[str(ch.id)] = (gid, gname, str(ch.id), ch.name or str(ch.id), ch.type)

            # Supplement with HTTP fetch to catch channels created while bot was offline
            for gid in target_guild_ids:
                try:
                    ch_data = await self.bot.http.get_guild_channels(gid)
                    for ch in (ch_data or []):
                        ch_id = str(ch.get('id', '') or '')
                        ch_name = str(ch.get('name', '') or '')
                        ch_type = int(ch.get('type', 0) or 0)
                        if not ch_id or not ch_name or ch_type not in (0, 5):
                            continue
                        if ch_id not in entries:
                            entries[ch_id] = (gid, guild_names.get(gid, ''), ch_id, ch_name, ch_type)
                except Exception:
                    pass  # HTTP fetch failed - rely on cache only for this guild

            if not entries:
                return

            with db_session_scope() as db:
                for guild_id, guild_name, channel_id, channel_name, channel_type in entries.values():
                    db.execute(
                        text(
                            "INSERT INTO web_fluxer_guild_channels "
                            "  (guild_id, guild_name, channel_id, channel_name, channel_type, synced_at) "
                            "VALUES (:guild_id, :guild_name, :channel_id, :channel_name, :channel_type, :now) "
                            "ON DUPLICATE KEY UPDATE "
                            "  guild_name   = IF(VALUES(guild_name) != '', VALUES(guild_name), guild_name), "
                            "  channel_name = VALUES(channel_name), "
                            "  channel_type = VALUES(channel_type), "
                            "  synced_at    = VALUES(synced_at)"
                        ),
                        {
                            "guild_id": guild_id,
                            "guild_name": guild_name,
                            "channel_id": channel_id,
                            "channel_name": channel_name,
                            "channel_type": channel_type,
                            "now": now,
                        },
                    )
                db.commit()

            # Remove stale channels - only delete if we got a full list from HTTP
            # (skip deletion if HTTP failed and we only have the cache)
            live_channel_ids = list(entries.keys())
            guild_ids_in_sync = list({str(e[0]) for e in entries.values()})
            if live_channel_ids and guild_ids_in_sync:
                with db_session_scope() as db:
                    placeholders = ','.join([f':cid{i}' for i in range(len(live_channel_ids))])
                    gplaceholders = ','.join([f':gid{i}' for i in range(len(guild_ids_in_sync))])
                    params = {f'cid{i}': cid for i, cid in enumerate(live_channel_ids)}
                    params.update({f'gid{i}': gid for i, gid in enumerate(guild_ids_in_sync)})
                    result = db.execute(text(
                        f"DELETE FROM web_fluxer_guild_channels "
                        f"WHERE guild_id IN ({gplaceholders}) "
                        f"AND channel_id NOT IN ({placeholders})"
                    ), params)
                    if result.rowcount:
                        logger.info(f"Removed {result.rowcount} stale channel(s) from DB")
                    db.commit()

            logger.info(f"Synced {len(entries)} Fluxer channels to DB")
        except Exception as e:
            logger.error(f"_sync_guild_channels error: {e}", exc_info=True)

    # -----------------------------------------------------------------------
    # !lfg delete <id>
    # -----------------------------------------------------------------------

    async def _lfg_delete(self, ctx, id_str: str):
        """Handle !lfg delete <id>."""
        if not id_str or not id_str.isdigit():
            await ctx.reply(embed=fluxer.Embed(
                description="**Usage:** `!lfg delete <id>`\n\nUse `!lfglist` to see group IDs.",
                color=RED_COLOR,
            ))
            return

        group_id = int(id_str)
        fluxer_user_id = str(ctx.author.id)
        web_user_id, web_username = _get_web_user_by_fluxer_id(fluxer_user_id)

        # Unlinked users can't own any groups - but admins can still delete by force
        try:
            is_admin = await _is_guild_admin(ctx)
        except Exception as e:
            logger.warning(f"_is_guild_admin failed for {ctx.author.id}: {e}")
            await ctx.reply(embed=fluxer.Embed(
                description="Could not verify your permissions right now. Please try again in a moment.",
                color=RED_COLOR,
            ))
            return
        if not web_user_id and not is_admin:
            await ctx.reply(embed=fluxer.Embed(
                description=(
                    "You need a QuestLog account to manage LFG groups.\n\n"
                    f"[Create a free account]({QUESTLOG_REGISTER_URL}) or "
                    f"[link your existing account](https://casual-heroes.com/ql/settings/) "
                    f"via Discord in your settings."
                ),
                color=PURPLE_COLOR,
            ))
            return

        success, err = _delete_web_lfg_group(
            group_id=group_id,
            web_user_id=web_user_id or 0,
            force=is_admin,
        )

        if success:
            await ctx.reply(embed=fluxer.Embed(
                title="LFG Deleted",
                description=f"Group **#{group_id}** has been removed.",
                color=GREEN_COLOR,
            ))
            logger.info(f"LFG #{group_id} deleted by {web_username or ctx.author.id} (admin={is_admin})")
        else:
            await ctx.reply(embed=fluxer.Embed(
                description=f"Could not delete: {err}",
                color=RED_COLOR,
            ))

    # -----------------------------------------------------------------------
    # !lfg
    # -----------------------------------------------------------------------

    @Cog.command()
    async def lfg(self, ctx, *, args: str = ""):
        """!lfg  -  Browse LFG groups for this server on QuestLog."""
        if _lfg_cooldown('lfg', str(ctx.author.id)):
            return
        if not ctx.guild:
            await ctx.reply(embed=fluxer.Embed(
                title="Server Only",
                description="LFG can only be used inside a server.",
                color=RED_COLOR,
            ))
            return

        # Subcommand routing
        parts = args.strip().split()
        first_word = parts[0].lower() if parts else ""
        if first_word == "setup":
            await self._setup_lfg(ctx)
            return
        if first_word == "list":
            await self.lfglist(ctx)
            return
        if first_word == "delete":
            await self._lfg_delete(ctx, args.strip()[6:].strip().lstrip('#'))
            return
        if first_word == "games":
            await self._lfg_games(ctx)
            return
        if first_word == "leave":
            await self._lfg_leave(ctx, parts[1].lstrip('#') if len(parts) > 1 else "")
            return
        if first_word == "create":
            await self._lfg_create(ctx)
            return
        if first_word == "fluxer":
            await self._lfg_fluxer(ctx)
            return

        guild_id = str(ctx.guild.id if hasattr(ctx.guild, 'id') else ctx.guild_id)
        guild_lfg_url = f"{QL_BASE}/ql/fluxer/{guild_id}/lfg/browse/"

        fluxer_user_id = str(ctx.author.id)
        web_user_id, web_username = _get_web_user_by_fluxer_id(fluxer_user_id)

        if web_user_id:
            account_note = f"Logged in as **{web_username}** on QuestLog."
        else:
            account_note = (
                f"No QuestLog account linked - [create a free account]({QUESTLOG_REGISTER_URL}) "
                f"to join groups, earn XP, and track attendance."
            )

        embed = fluxer.Embed(
            title="Looking for Group",
            description=(
                f"Browse and join LFG groups for this server on QuestLog.\n\n"
                f"[Open LFG for this server]({guild_lfg_url})\n\n"
                f"{account_note}"
            ),
            color=GOLD_COLOR,
            url=guild_lfg_url,
        )
        embed.add_field(
            name="Commands",
            value=(
                "`!lfg create` - Create a new group\n"
                "`!lfg games` - See configured games\n"
                "`!lfglist` - Show active groups\n"
                "`!lfgjoin <id>` - Join a group\n"
                "`!lfg leave <id>` - Leave a group\n"
                "`!lfg delete <id>` - Delete your group\n"
                "`!lfg fluxer` - Fluxer LFG portal\n"
                "`!lfgql` - QuestLog Network LFG"
            ),
            inline=False,
        )
        embed.set_footer(text="casual-heroes.com/ql/")
        await ctx.reply(embed=embed)

    # -----------------------------------------------------------------------
    # !lfgql
    # -----------------------------------------------------------------------

    @Cog.command()
    async def lfgql(self, ctx):
        """!lfgql  -  Browse or create LFG groups on the public QuestLog Network."""
        if _lfg_cooldown('lfgql', str(ctx.author.id)):
            return
        embed = fluxer.Embed(
            title="QuestLog LFG Network",
            description=(
                "Find players from across the QuestLog Network and create public LFG groups.\n\n"
                f"[Browse & Create Groups]({QUESTLOG_LFG_URL})"
            ),
            color=PURPLE_COLOR,
            url=QUESTLOG_LFG_URL,
        )
        embed.set_footer(text="casual-heroes.com/ql/lfg/")
        await ctx.reply(embed=embed)

    # -----------------------------------------------------------------------
    # !lfglist
    # -----------------------------------------------------------------------

    @Cog.command()
    async def lfglist(self, ctx):
        """!lfglist  -  Show active LFG groups for this server."""
        if _lfg_cooldown('lfglist', str(ctx.author.id)):
            return
        guild_id = str(ctx.guild.id if ctx.guild and hasattr(ctx.guild, 'id') else (ctx.guild_id or ''))
        guild_lfg_url = f"{QL_BASE}/ql/fluxer/{guild_id}/lfg/browse/" if guild_id else QUESTLOG_LFG_URL

        try:
            with db_session_scope() as db:
                rows = db.execute(
                    text(
                        "SELECT g.id, COALESCE(u.username, g.creator_name, 'Unknown') AS uname, "
                        "       g.game_name, g.title, "
                        "       g.max_size, g.current_size, g.scheduled_time "
                        "FROM web_fluxer_lfg_groups g "
                        "LEFT JOIN web_users u ON g.creator_web_user_id = u.id "
                        "WHERE g.guild_id = :gid AND g.status IN ('open', 'full') "
                        "ORDER BY g.created_at DESC LIMIT 10"
                    ),
                    {"gid": guild_id},
                ).fetchall()

            if not rows:
                await ctx.reply(embed=fluxer.Embed(
                    title="Looking for Group",
                    description=(
                        "No active LFG groups right now.\n\n"
                        f"Create one on QuestLog: [LFG for this server]({guild_lfg_url})"
                    ),
                    color=GOLD_COLOR,
                    url=guild_lfg_url,
                ))
                return

            embed = fluxer.Embed(
                title=f"Active LFG Groups ({len(rows)})",
                description=f"[View all on QuestLog]({guild_lfg_url})",
                color=GOLD_COLOR,
                url=guild_lfg_url,
            )
            for group_id, uname, game, title, max_size, current_size, scheduled_time in rows:
                group_url = guild_lfg_url
                when = ""
                if scheduled_time:
                    import datetime
                    when = f" | {datetime.datetime.utcfromtimestamp(scheduled_time).strftime('%b %d %H:%M UTC')}"
                embed.add_field(
                    name=f"#{group_id} - {game}: {title}",
                    value=f"by **{uname}** | {current_size}/{max_size}{when}\n[View & Join]({group_url})",
                    inline=False,
                )

            embed.set_footer(text="casual-heroes.com/ql/")
            await ctx.reply(embed=embed)
        except Exception as e:
            logger.error(f"LFG list failed: {e}", exc_info=True)
            await ctx.reply(embed=fluxer.Embed(
                title="Error", description="Could not fetch LFG groups.", color=RED_COLOR
            ))

    # -----------------------------------------------------------------------
    # !lfgjoin
    # -----------------------------------------------------------------------

    @Cog.command()
    async def lfgjoin(self, ctx, *, args: str = ""):
        """!lfgjoin <id>  -  Go to QuestLog to join an LFG group and pick your class/spec."""
        arg = args.strip().lstrip('#')
        if not arg or not arg.isdigit():
            await ctx.reply(embed=fluxer.Embed(
                title="Join LFG",
                description=f"**Usage:** `!lfgjoin <group_id>`\n\nUse `!lfglist` to see active groups.",
                color=GOLD_COLOR,
            ))
            return

        group_id = int(arg)
        guild_id = str(ctx.guild.id if ctx.guild and hasattr(ctx.guild, 'id') else (ctx.guild_id or ''))
        group_url = f"{QL_BASE}/ql/fluxer/{guild_id}/lfg/browse/" if guild_id else QUESTLOG_LFG_URL

        fluxer_user_id = str(ctx.author.id)
        web_user_id, web_username = _get_web_user_by_fluxer_id(fluxer_user_id)

        if web_user_id:
            desc = (
                f"You're signed in as **{web_username}** on QuestLog.\n\n"
                f"[Open LFG and join Group #{group_id}]({group_url})"
            )
        else:
            desc = (
                f"[Open LFG and join Group #{group_id}]({group_url})\n\n"
                f"**No QuestLog account?** [Create a free account]({QUESTLOG_REGISTER_URL}) "
                f"first so your attendance and XP are tracked."
            )

        await ctx.reply(embed=fluxer.Embed(
            title=f"Join Group #{group_id}",
            description=desc,
            color=GREEN_COLOR,
            url=group_url,
        ))

    # -----------------------------------------------------------------------
    # !setup
    # -----------------------------------------------------------------------

    @Cog.command()
    async def setup(self, ctx, *, args: str = ""):
        """!setup <lfg|status>  -  Configure QuestLog Network for this server."""
        subcommand = args.strip().lower().split()[0] if args.strip() else ""
        setup_guild_id = str(ctx.guild.id if ctx.guild and hasattr(ctx.guild, 'id') else (ctx.guild_id or ''))
        dashboard_url = f"{QL_BASE}/ql/dashboard/fluxer/{setup_guild_id}/" if setup_guild_id else f"{QL_BASE}/ql/dashboard/fluxer/"

        if not subcommand:
            embed = fluxer.Embed(
                title="QuestLog Network Setup",
                description=(
                    "**Commands:**\n"
                    "`!setup lfg` - Register this channel for LFG\n"
                    "`!setup status` - Show current configuration\n\n"
                    f"Manage via web: [Fluxer Dashboard]({dashboard_url})"
                ),
                color=PURPLE_COLOR,
            )
            await ctx.reply(embed=embed)
            return

        if subcommand == "lfg":
            await self._setup_lfg(ctx)
        elif subcommand == "status":
            await self._setup_status(ctx)
        else:
            await ctx.reply(embed=fluxer.Embed(
                description=f"Unknown: `{subcommand}`. Try `!setup lfg` or `!setup status`.",
                color=RED_COLOR,
            ))

    # -----------------------------------------------------------------------
    # !lfg games
    # -----------------------------------------------------------------------

    async def _lfg_games(self, ctx):
        """Show configured LFG games for this server."""
        guild_id = str(ctx.guild.id if ctx.guild and hasattr(ctx.guild, 'id') else (ctx.guild_id or ''))
        guild_lfg_url = f"{QL_BASE}/ql/fluxer/{guild_id}/lfg/browse/" if guild_id else QUESTLOG_LFG_URL
        try:
            with db_session_scope() as db:
                rows = db.execute(
                    text(
                        "SELECT name, emoji, max_group_size "
                        "FROM web_fluxer_lfg_games "
                        "WHERE guild_id = :gid AND enabled = 1 "
                        "ORDER BY name ASC LIMIT 20"
                    ),
                    {"gid": guild_id},
                ).fetchall()

            if not rows:
                await ctx.reply(embed=fluxer.Embed(
                    title="LFG Games",
                    description=(
                        "No LFG games configured for this server yet.\n\n"
                        f"Ask an admin to add games via the [Fluxer Dashboard]({QL_BASE}/ql/dashboard/fluxer/{guild_id}/)."
                    ),
                    color=GOLD_COLOR,
                ))
                return

            lines = []
            for game_name, game_emoji, max_size in rows:
                emoji = (game_emoji + " ") if game_emoji else ""
                lines.append(f"{emoji}**{game_name}** - up to {max_size} players")

            embed = fluxer.Embed(
                title=f"LFG Games ({len(rows)})",
                description="\n".join(lines) + f"\n\n[Browse Groups]({guild_lfg_url})",
                color=GOLD_COLOR,
                url=guild_lfg_url,
            )
            embed.set_footer(text="casual-heroes.com/ql/")
            await ctx.reply(embed=embed)
        except Exception as e:
            logger.error(f"LFG games list failed: {e}", exc_info=True)
            await ctx.reply(embed=fluxer.Embed(
                title="Error", description="Could not fetch LFG games.", color=RED_COLOR
            ))

    # -----------------------------------------------------------------------
    # !lfg leave <id>
    # -----------------------------------------------------------------------

    async def _lfg_leave(self, ctx, id_str: str):
        """Leave an LFG group by ID."""
        if not id_str or not id_str.isdigit():
            await ctx.reply(embed=fluxer.Embed(
                description="**Usage:** `!lfg leave <id>`\n\nUse `!lfglist` to see group IDs.",
                color=RED_COLOR,
            ))
            return

        group_id = int(id_str)
        fluxer_user_id = str(ctx.author.id)
        web_user_id, web_username = _get_web_user_by_fluxer_id(fluxer_user_id)

        if not web_user_id:
            await ctx.reply(embed=fluxer.Embed(
                description=(
                    "You need a linked QuestLog account to leave groups.\n\n"
                    f"[Create a free account]({QUESTLOG_REGISTER_URL}) or "
                    f"[link your existing account](https://casual-heroes.com/ql/settings/)."
                ),
                color=PURPLE_COLOR,
            ))
            return

        try:
            with db_session_scope() as db:
                member_row = db.execute(
                    text(
                        "SELECT id FROM web_fluxer_lfg_members "
                        "WHERE group_id = :gid AND web_user_id = :uid"
                    ),
                    {"gid": group_id, "uid": web_user_id},
                ).fetchone()

                if not member_row:
                    await ctx.reply(embed=fluxer.Embed(
                        description=f"You are not in Group **#{group_id}**.",
                        color=RED_COLOR,
                    ))
                    return

                db.execute(
                    text("DELETE FROM web_fluxer_lfg_members WHERE id = :id"),
                    {"id": member_row[0]},
                )
                # Update current_size
                db.execute(
                    text(
                        "UPDATE web_fluxer_lfg_groups "
                        "SET current_size = GREATEST(0, current_size - 1), "
                        "    status = IF(status = 'full', 'open', status) "
                        "WHERE id = :gid"
                    ),
                    {"gid": group_id},
                )
                db.commit()

            await ctx.reply(embed=fluxer.Embed(
                title="Left Group",
                description=f"You've left Group **#{group_id}**.",
                color=GREEN_COLOR,
            ))
            logger.info(f"User {web_username} (web_id={web_user_id}) left Fluxer LFG group #{group_id}")
        except Exception as e:
            logger.error(f"LFG leave failed: {e}", exc_info=True)
            await ctx.reply(embed=fluxer.Embed(
                title="Error", description="Could not process your leave request.", color=RED_COLOR
            ))

    # -----------------------------------------------------------------------
    # !lfg create
    # -----------------------------------------------------------------------

    async def _lfg_create(self, ctx):
        """Direct link to create an LFG group on QuestLog for this server."""
        if not ctx.guild:
            return
        guild_id = str(ctx.guild.id if hasattr(ctx.guild, 'id') else ctx.guild_id)
        create_url = f"{QL_BASE}/ql/fluxer/{guild_id}/lfg/browse/"
        fluxer_user_id = str(ctx.author.id)
        web_user_id, web_username = _get_web_user_by_fluxer_id(fluxer_user_id)

        if web_user_id:
            desc = (
                f"You're signed in as **{web_username}** on QuestLog.\n\n"
                f"[Create an LFG Group]({create_url})\n\n"
                "Use the **Create Group** button on the LFG page."
            )
        else:
            desc = (
                f"[Create an LFG Group for this server]({create_url})\n\n"
                f"**No QuestLog account?** [Register free]({QUESTLOG_REGISTER_URL}) "
                "to post groups, earn XP, and track your gaming sessions."
            )

        embed = fluxer.Embed(
            title="Create LFG Group",
            description=desc,
            color=GOLD_COLOR,
            url=create_url,
        )
        embed.set_footer(text="casual-heroes.com/ql/")
        await ctx.reply(embed=embed)

    # -----------------------------------------------------------------------
    # !lfg fluxer
    # -----------------------------------------------------------------------

    async def _lfg_fluxer(self, ctx):
        """Link to this server's Fluxer LFG member portal."""
        if not ctx.guild:
            return
        guild_id = str(ctx.guild.id if hasattr(ctx.guild, 'id') else ctx.guild_id)
        portal_url = f"{QL_BASE}/ql/fluxer/{guild_id}/lfg/browse/"
        embed = fluxer.Embed(
            title="Fluxer LFG Portal",
            description=(
                f"Browse, join, and create LFG groups for **{ctx.guild.name}** on QuestLog.\n\n"
                f"[Open Fluxer LFG Portal]({portal_url})"
            ),
            color=PURPLE_COLOR,
            url=portal_url,
        )
        embed.set_footer(text="casual-heroes.com/ql/")
        await ctx.reply(embed=embed)

    async def _setup_lfg(self, ctx):
        """Register the current channel for LFG. No webhook URL needed."""
        if not ctx.guild:
            await ctx.reply(embed=fluxer.Embed(
                description="This command can only be used inside a server.",
                color=RED_COLOR,
            ))
            return

        guild_id = str(ctx.guild.id)
        guild_name = ctx.guild.name or "Unknown Server"
        channel_id = str(ctx.channel.id)
        channel_name = getattr(ctx.channel, "name", None) or channel_id
        dashboard_url = f"{QL_BASE}/ql/dashboard/fluxer/{guild_id}/"

        try:
            with db_session_scope() as db:
                existing = db.execute(
                    text(
                        "SELECT id FROM web_community_bot_configs "
                        "WHERE platform = 'fluxer' AND guild_id = :g AND event_type = 'lfg_announce'"
                    ),
                    {"g": guild_id},
                ).fetchone()

                now_ts = int(time.time())
                if existing:
                    db.execute(
                        text(
                            "UPDATE web_community_bot_configs "
                            "SET channel_id = :ch, channel_name = :cname, "
                            "    guild_name = :gname, is_enabled = 1, updated_at = :now "
                            "WHERE platform = 'fluxer' AND guild_id = :g AND event_type = 'lfg_announce'"
                        ),
                        {
                            "ch": channel_id, "cname": channel_name,
                            "gname": guild_name, "now": now_ts, "g": guild_id,
                        },
                    )
                    action = "updated"
                else:
                    db.execute(
                        text(
                            "INSERT INTO web_community_bot_configs "
                            "(platform, guild_id, guild_name, channel_id, channel_name, "
                            " event_type, is_enabled, created_at, updated_at) "
                            "VALUES ('fluxer', :g, :gname, :ch, :cname, "
                            "        'lfg_announce', 1, :now, :now)"
                        ),
                        {
                            "g": guild_id, "gname": guild_name,
                            "ch": channel_id, "cname": channel_name, "now": now_ts,
                        },
                    )
                    action = "registered"
                db.commit()

            await ctx.reply(embed=fluxer.Embed(
                title="LFG Channel Set!",
                description=(
                    f"**#{channel_name}** is now {action} as the LFG channel for this server.\n\n"
                    "LFG posts from `!lfg` and the QuestLog Network will appear here.\n"
                    f"Manage at [Fluxer Dashboard]({dashboard_url})"
                ),
                color=GREEN_COLOR,
            ))
            logger.info(f"LFG setup {action} for guild {guild_id} channel {channel_id}")
        except Exception as e:
            logger.error(f"Setup LFG failed: {e}", exc_info=True)
            await ctx.reply(embed=fluxer.Embed(
                title="Setup Failed",
                description="Could not save LFG configuration. Please try again.",
                color=RED_COLOR,
            ))

    async def _setup_status(self, ctx):
        guild_id = str(ctx.guild.id) if ctx.guild else None
        if not guild_id:
            return

        try:
            with db_session_scope() as db:
                rows = db.execute(
                    text(
                        "SELECT event_type, channel_id, channel_name, is_enabled "
                        "FROM web_community_bot_configs "
                        "WHERE platform = 'fluxer' AND guild_id = :g "
                        "ORDER BY event_type"
                    ),
                    {"g": guild_id},
                ).fetchall()

            embed = fluxer.Embed(
                title="QuestLog Network - Server Status",
                description=f"Configuration for **{ctx.guild.name}**",
                color=PURPLE_COLOR,
            )
            if not rows:
                embed.description += "\n\nNo LFG channel configured yet.\nRun `!setup lfg` in the channel you want to use."
            else:
                for event_type, channel_id, channel_name, is_enabled in rows:
                    label = "LFG Announcements" if event_type == "lfg_announce" else event_type
                    status = "Enabled" if is_enabled else "Disabled"
                    channel = f"<#{channel_id}>" if channel_id else "Not set"
                    embed.add_field(name=label, value=f"{status} | {channel}", inline=False)

            embed.set_footer(text=f"Manage at casual-heroes.com/ql/dashboard/fluxer/{guild_id}/")
            await ctx.reply(embed=embed)
        except Exception as e:
            logger.error(f"Setup status failed: {e}", exc_info=True)
            await ctx.reply(embed=fluxer.Embed(
                title="Error", description="Could not fetch configuration.", color=RED_COLOR
            ))
