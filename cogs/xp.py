# cogs/xp.py - XP and leveling system
#
# Awards XP for messages, tracks levels, sends level-up messages.
# Writes to fluxer_member_xp (per-guild stats/cooldowns) AND to the unified
# web_xp_events / web_users tables for users who have linked their QuestLog account.
# Reads active XP boost events from web_fluxer_xp_boost_events.
# Sends level-up messages based on web_fluxer_guild_settings.level_up_* columns.

import time
import fluxer
from fluxer import Cog
from sqlalchemy import text
from config import logger, db_session_scope

XP_PER_MESSAGE = 2
MESSAGE_COOLDOWN_SECONDS = 60

# TTL cache for ignored channels per guild: {guild_id: (set_of_channel_ids, expire_ts)}
_ignored_channels_cache: dict = {}
_IGNORED_CACHE_TTL = 300  # 5 minutes

# TTL cache for XP guild config: {guild_id: (config_dict, expire_ts)}
_guild_config_cache: dict = {}
_GUILD_CONFIG_TTL = 60  # 1 minute - short enough to pick up boost changes quickly


def _get_ignored_channels(guild_id: str, db) -> set:
    """Return set of channel IDs that earn no XP for this guild. TTL-cached."""
    now = time.time()
    cached = _ignored_channels_cache.get(guild_id)
    if cached and now < cached[1]:
        return cached[0]
    try:
        import json as _json
        row = db.execute(
            text("SELECT xp_ignored_channels FROM web_fluxer_guild_settings WHERE guild_id = :g LIMIT 1"),
            {'g': guild_id}
        ).fetchone()
        if row and row[0]:
            ids = set(str(x) for x in _json.loads(row[0]))
        else:
            ids = set()
    except Exception:
        ids = set()
    _ignored_channels_cache[guild_id] = (ids, now + _IGNORED_CACHE_TTL)
    # Prune expired entries to prevent unbounded memory growth
    if len(_ignored_channels_cache) > 500:
        expired = [k for k, v in _ignored_channels_cache.items() if now >= v[1]]
        for k in expired:
            del _ignored_channels_cache[k]
    return ids


def _get_guild_config(guild_id: str, db) -> dict:
    """Return XP + level-up config for guild. TTL-cached 1 minute."""
    now = time.time()
    cached = _guild_config_cache.get(guild_id)
    if cached and now < cached[1]:
        return cached[0]
    cfg = {
        'xp_per_message': XP_PER_MESSAGE,
        'message_cooldown': MESSAGE_COOLDOWN_SECONDS,
        'media_cooldown': MESSAGE_COOLDOWN_SECONDS,
        'reaction_cooldown': REACTION_COOLDOWN_SECONDS,
        'level_up_enabled': False,
        'level_up_destination': 'current',
        'level_up_channel_id': None,
        'level_up_message': None,
    }
    try:
        row = db.execute(text(
            "SELECT xp_per_message, level_up_enabled, level_up_destination, "
            "level_up_channel_id, level_up_message, "
            "xp_cooldown_secs, xp_media_cooldown_secs, xp_reaction_cooldown_secs "
            "FROM web_fluxer_guild_settings WHERE guild_id = :g LIMIT 1"
        ), {'g': guild_id}).fetchone()
        if row:
            cfg['xp_per_message'] = int(row[0] or XP_PER_MESSAGE)
            cfg['level_up_enabled'] = bool(row[1])
            cfg['level_up_destination'] = row[2] or 'current'
            cfg['level_up_channel_id'] = str(row[3]) if row[3] else None
            cfg['level_up_message'] = row[4] or None
            cfg['message_cooldown'] = float(row[5] or MESSAGE_COOLDOWN_SECONDS)
            cfg['media_cooldown'] = float(row[6] or MESSAGE_COOLDOWN_SECONDS)
            cfg['reaction_cooldown'] = float(row[7] or REACTION_COOLDOWN_SECONDS)
    except Exception as e:
        logger.debug(f"_get_guild_config failed for {guild_id}: {e}")
    _guild_config_cache[guild_id] = (cfg, now + _GUILD_CONFIG_TTL)
    if len(_guild_config_cache) > 500:
        expired = [k for k, v in _guild_config_cache.items() if now >= v[1]]
        for k in expired:
            del _guild_config_cache[k]
    return cfg


def _get_boost_multiplier(guild_id: str, db) -> int:
    """Return the active XP boost multiplier for this guild (1 = no boost).
    Stacks additively: 2x + 2x = 3x (each event adds (mult-1) to the base of 1).
    Auto-deactivates expired events."""
    now = int(time.time())
    try:
        # Auto-expire any events whose end_time has passed
        db.execute(text(
            "UPDATE web_fluxer_xp_boost_events SET is_active = 0 "
            "WHERE guild_id = :g AND is_active = 1 AND end_time IS NOT NULL AND end_time < :now"
        ), {'g': guild_id, 'now': now})

        rows = db.execute(text(
            "SELECT multiplier FROM web_fluxer_xp_boost_events "
            "WHERE guild_id = :g AND is_active = 1"
        ), {'g': guild_id}).fetchall()

        if not rows:
            return 1
        # Additive stacking: 1 + sum(mult - 1) for all active boosts
        total = 1 + sum(int(r[0]) - 1 for r in rows if r[0] > 1)
        return max(1, total)
    except Exception as e:
        logger.debug(f"_get_boost_multiplier failed for {guild_id}: {e}")
        return 1


# XP amounts - must match XP_ACTIONS in helpers.py on the site
FLUXER_XP = {
    'fluxer_message':  2,
    'fluxer_reaction': 1,
}

# HP conversion constants - must match helpers.py
XP_TO_HP_THRESHOLD = 50
HP_PER_THRESHOLD = 10
HP_PER_LEVEL = 5

DEFAULT_LEVELUP_MESSAGE = "GG {user}, you just reached **Level {level}**! Keep chatting to level up further."

LEVEL_UP_VARIABLES = {
    '{user}': "Member mention",
    '{username}': "Member username",
    '{level}': "New level reached",
    '{server}': "Server name",
}


def _format_levelup_message(template: str, display_name: str, mention: str, level: int, guild_name: str) -> str:
    """Replace template variables with actual values."""
    return (template
            .replace('{user}', mention)
            .replace('{username}', display_name)
            .replace('{level}', str(level))
            .replace('{server}', guild_name))


def _get_web_level(xp: int, db) -> int:
    """Calculate level from total XP using level_requirements table (matches site helpers.py)."""
    try:
        rows = db.execute(
            text("SELECT level, xp_required FROM level_requirements ORDER BY level")
        ).fetchall()
        if rows:
            current_level = 1
            for row in rows:
                if xp >= row[1]:
                    current_level = row[0]
                else:
                    break
            return current_level
    except Exception:
        pass
    level = 1
    while level < 99:
        if xp < int(7 * ((level + 1) ** 1.5)):
            break
        level += 1
    return level


def _award_web_xp(fluxer_user_id: str, action_type: str, ref_id: str) -> tuple[bool, int]:
    """
    Award XP to the unified QuestLog profile for a linked Fluxer user.
    Looks up web_user via web_users.fluxer_id, writes to web_xp_events,
    and updates web_users.web_xp / web_level / hero_points in one transaction.
    No-op if user is not linked.
    Returns (web_leveled_up, new_web_level).
    """
    xp_amount = FLUXER_XP.get(action_type)
    if not xp_amount:
        return False, 0
    try:
        with db_session_scope() as db:
            row = db.execute(
                text("SELECT id, web_xp, web_level, hero_points FROM web_users WHERE fluxer_id = :fid LIMIT 1"),
                {"fid": fluxer_user_id},
            ).fetchone()
            if not row:
                return False, 0  # User not linked - only bot-local XP applies

            web_user_id = row.id
            old_xp = row.web_xp or 0
            old_level = row.web_level or 1
            hero_points = row.hero_points or 0

            # ref_id is required for dedup - refuse to award without it to prevent duplicates
            if not ref_id:
                logger.error(f"_award_web_xp called without ref_id for action_type={action_type}, refusing to award")
                return False, 0
            dup = db.execute(
                text("SELECT id FROM web_xp_events WHERE user_id = :uid AND action_type = :at AND ref_id = :ref LIMIT 1"),
                {"uid": web_user_id, "at": action_type, "ref": ref_id},
            ).fetchone()
            if dup:
                return False, old_level

            now = int(time.time())
            new_xp = old_xp + xp_amount

            db.execute(text(
                "INSERT INTO web_xp_events (user_id, action_type, xp, source, ref_id, created_at) "
                "VALUES (:uid, :at, :xp, 'fluxer', :ref, :now)"
            ), {"uid": web_user_id, "at": action_type, "xp": xp_amount, "ref": ref_id, "now": now})

            # Award HP for every 50-XP threshold crossed
            thresholds_crossed = (new_xp // XP_TO_HP_THRESHOLD) - (old_xp // XP_TO_HP_THRESHOLD)
            if thresholds_crossed > 0:
                hp_from_xp = thresholds_crossed * HP_PER_THRESHOLD
                hero_points += hp_from_xp
                db.execute(text(
                    "INSERT INTO web_hero_point_events (user_id, action_type, points, source, ref_id, created_at) "
                    "VALUES (:uid, 'xp_conversion', :pts, 'fluxer', :ref, :now)"
                ), {"uid": web_user_id, "pts": hp_from_xp, "ref": f"xp_{new_xp}", "now": now})

            # Level-up
            new_level = _get_web_level(new_xp, db)
            web_leveled_up = new_level > old_level
            if web_leveled_up:
                hp_from_level = (new_level - old_level) * HP_PER_LEVEL
                hero_points += hp_from_level
                db.execute(text(
                    "INSERT INTO web_hero_point_events (user_id, action_type, points, source, ref_id, created_at) "
                    "VALUES (:uid, 'level_up', :pts, 'fluxer', :ref, :now)"
                ), {"uid": web_user_id, "pts": hp_from_level, "ref": f"level_{new_level}", "now": now})

            db.execute(text(
                "UPDATE web_users SET web_xp = :xp, web_level = :lvl, hero_points = :hp WHERE id = :uid"
            ), {"xp": new_xp, "lvl": new_level, "hp": hero_points, "uid": web_user_id})
            db.commit()
            return web_leveled_up, new_level
    except Exception as e:
        logger.warning(f"_award_web_xp failed for fluxer_id={fluxer_user_id}: {e}")
    return False, 0


BRAND_COLOR = 0x66aa8b  # Fluxer green
GOLD_COLOR = 0xFEE75C

# Per-user cooldown cache: {"guild_id:user_id" -> last_xp_timestamp}
_message_cooldowns: dict[str, float] = {}

# Per-user reaction cooldown: {"guild_id:user_id" -> last_reaction_timestamp}
_reaction_cooldowns: dict[str, float] = {}
REACTION_COOLDOWN_SECONDS = 30.0

# Voice tracking: {"guild_id:user_id" -> join_timestamp}
_voice_join_times: dict[str, float] = {}
VOICE_INTERVAL_SECONDS = 60.0  # award XP every 60s in voice

# Per-user command cooldowns: {"cmd:user_id" -> last_use_timestamp}
_cmd_cooldowns: dict[str, float] = {}
_CMD_COOLDOWN = 15.0  # seconds between uses of !xp / !leaderboard per user


def _check_cmd_cooldown(cmd: str, user_id: str) -> bool:
    """Returns True if the command is on cooldown for this user. Updates timestamp if not."""
    key = f"{cmd}:{user_id}"
    now = time.time()
    if now - _cmd_cooldowns.get(key, 0) < _CMD_COOLDOWN:
        return True
    _cmd_cooldowns[key] = now
    # Prune stale entries
    if len(_cmd_cooldowns) > 5000:
        cutoff = now - _CMD_COOLDOWN * 2
        stale = [k for k, v in _cmd_cooldowns.items() if v < cutoff]
        for k in stale:
            del _cmd_cooldowns[k]
    return False

# Medal emojis for leaderboard
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _xp_to_level(xp: float) -> int:
    """Calculate level from XP using the same formula as the site and WardenBot: 7*(level+1)^1.5"""
    level = 1
    while level < 99:
        if int(xp) < int(7 * ((level + 1) ** 1.5)):
            break
        level += 1
    return level


def _xp_bar(xp: float, level: int) -> str:
    """Visual XP progress bar. Uses same formula as site: 7*(level+1)^1.5"""
    level_start = int(7 * (level ** 1.5)) if level > 1 else 0
    level_end = int(7 * ((level + 1) ** 1.5))
    progress = max(0, xp - level_start)
    total = max(1, level_end - level_start)
    pct = min(1.0, progress / total)
    filled = int(pct * 10)
    bar = "█" * filled + "░" * (10 - filled)
    return f"`{bar}` {int(pct * 100)}%"


def _get_avatar(author) -> str | None:
    """Extract avatar URL from author, trying multiple attribute names."""
    for attr in ("display_avatar_url", "avatar_url", "avatar"):
        val = getattr(author, attr, None)
        if val and isinstance(val, str) and val.startswith("http"):
            return val
        if val and hasattr(val, "url"):
            return str(val.url)
    return None


class XpCog(Cog):
    def __init__(self, bot):
        super().__init__(bot)

    @Cog.listener()
    async def on_member_join(self, member):
        """Register new guild members in fluxer_member_xp with 0 XP so they appear in My Servers sidebar."""
        user_id = getattr(member, 'id', None) or (member.get('user', {}).get('id') if isinstance(member, dict) else None)
        guild_id = getattr(member, 'guild_id', None) or (member.get('guild_id') if isinstance(member, dict) else None)
        if not user_id or not guild_id:
            return
        username = ''
        if isinstance(member, dict):
            username = member.get('user', {}).get('username', '')
        else:
            username = getattr(member, 'username', '') or getattr(getattr(member, 'user', None), 'username', '')
        try:
            with db_session_scope() as db:
                now = int(time.time())
                db.execute(
                    text(
                        "INSERT INTO fluxer_member_xp "
                        "(guild_id, user_id, username, xp, level, message_count, last_message_ts, first_seen, last_active) "
                        "VALUES (:guild_id, :user_id, :username, 0, 0, 0, 0, :now, :now) "
                        "ON DUPLICATE KEY UPDATE last_active = last_active"
                    ),
                    {"guild_id": int(guild_id), "user_id": int(user_id), "username": username, "now": now},
                )
        except Exception as e:
            logger.error(f"Failed to register member on join: {e}", exc_info=True)

    @Cog.listener()
    async def on_message(self, message):
        """Award XP for messages, applying any active boosts and sending level-up messages."""
        if message.author.bot:
            return
        # Never award XP for bridged messages - XP is earned on the origin platform only
        content = getattr(message, 'content', '') or ''
        if content.startswith(('**[D]', '**[F]', '**[M]', '[D]', '[F]', '[M]')):
            return

        user_id = str(message.author.id)
        guild_id = str(message.guild_id) if getattr(message, "guild_id", None) else None
        if not guild_id:
            return

        channel_id = str(getattr(message, 'channel_id', '') or '')

        cache_key = f"{guild_id}:{user_id}"
        now = time.time()

        with db_session_scope() as db:
            # Check if channel is ignored for XP
            if channel_id and channel_id in _get_ignored_channels(guild_id, db):
                return
            # Read guild config (XP amount, cooldowns, level-up settings) and boost multiplier
            cfg = _get_guild_config(guild_id, db)
            multiplier = _get_boost_multiplier(guild_id, db)
            db.commit()  # commit the auto-expiry updates

        # Per-guild message cooldown (configured by server admin, default 60s)
        msg_cooldown = cfg.get('message_cooldown', MESSAGE_COOLDOWN_SECONDS)
        if now - _message_cooldowns.get(cache_key, 0) < msg_cooldown:
            return

        xp_amount = max(1, cfg['xp_per_message'] * multiplier)

        _message_cooldowns[cache_key] = now
        # Prune stale cooldown entries
        if len(_message_cooldowns) > 10000:
            cutoff = now - msg_cooldown * 2
            stale = [k for k, v in _message_cooldowns.items() if v < cutoff]
            for k in stale:
                del _message_cooldowns[k]

        username = getattr(message.author, "username", "")
        ref_id = f"msg_{guild_id}_{message.id}"

        # Detect media attachments (images, video, files)
        attachments = getattr(message, 'attachments', None) or []
        inc_media = 1 if attachments else 0

        leveled_up, new_level = await self._award_xp(user_id, guild_id, username, xp_amount, ref_id, inc_media=inc_media)

        # Send level-up message if enabled and user leveled up
        if leveled_up and cfg['level_up_enabled'] and cfg['level_up_destination'] != 'none':
            await self._send_levelup_message(message, guild_id, new_level, cfg)

    @Cog.listener()
    async def on_raw_reaction_add(self, payload):
        """Track reaction counts per user in fluxer_member_xp (no XP, stats only)."""
        if payload.user_id == self.bot.user.id:
            return
        guild_id = str(getattr(payload, 'guild_id', None) or '')
        if not guild_id:
            return
        user_id = str(payload.user_id)
        now = time.time()
        cache_key = f"{guild_id}:{user_id}"
        # Fetch per-guild reaction cooldown (TTL-cached)
        try:
            with db_session_scope() as db:
                cfg = _get_guild_config(guild_id, db)
        except Exception:
            cfg = {}
        react_cooldown = cfg.get('reaction_cooldown', REACTION_COOLDOWN_SECONDS)
        if now - _reaction_cooldowns.get(cache_key, 0) < react_cooldown:
            return
        _reaction_cooldowns[cache_key] = now
        try:
            with db_session_scope() as db:
                db.execute(
                    text(
                        "UPDATE fluxer_member_xp SET reaction_count = reaction_count + 1, last_active = :now "
                        "WHERE guild_id = :g AND user_id = :u"
                    ),
                    {"g": int(guild_id), "u": int(user_id), "now": int(now)},
                )
                db.commit()
        except Exception as e:
            logger.debug(f"XpCog reaction tracking error: {e}")

    @Cog.listener()
    async def on_voice_state_update(self, voice_state):
        """Track voice join/leave times and award XP per minute in voice."""
        user_id = str(getattr(voice_state, 'user_id', None) or '')
        guild_id = str(getattr(voice_state, 'guild_id', None) or '')
        channel_id = getattr(voice_state, 'channel_id', None)
        if not user_id or not guild_id:
            return
        cache_key = f"{guild_id}:{user_id}"
        now = time.time()

        if channel_id:
            # User joined or moved to a voice channel - record join time if not tracked
            if cache_key not in _voice_join_times:
                _voice_join_times[cache_key] = now
        else:
            # User left voice - calculate minutes and award
            join_time = _voice_join_times.pop(cache_key, None)
            if join_time:
                minutes = max(1, int((now - join_time) / 60))
                try:
                    with db_session_scope() as db:
                        db.execute(
                            text(
                                "UPDATE fluxer_member_xp SET voice_minutes = voice_minutes + :mins, last_active = :now "
                                "WHERE guild_id = :g AND user_id = :u"
                            ),
                            {"g": int(guild_id), "u": int(user_id), "mins": minutes, "now": int(now)},
                        )
                        db.commit()
                except Exception as e:
                    logger.debug(f"XpCog voice tracking error: {e}")

    async def _award_xp(self, user_id: str, guild_id: str, username: str, amount: int, ref_id: str = "",
                        inc_media: int = 0, inc_reaction: int = 0, inc_voice_minutes: int = 0) -> tuple[bool, int]:
        """Write XP to fluxer_member_xp (per-guild) and web_users (unified profile if linked).
        Returns (leveled_up, new_level)."""
        leveled_up = False
        new_level = 0
        try:
            with db_session_scope() as db:
                now = int(time.time())
                # Fetch current XP + level so we can compute the new level in Python
                # using the correct formula (7*(level+1)^1.5) - never let MySQL compute it
                old_row = db.execute(text(
                    "SELECT xp, level FROM fluxer_member_xp WHERE guild_id = :g AND user_id = :u"
                ), {"g": int(guild_id), "u": int(user_id)}).fetchone()
                old_xp = int(old_row[0]) if old_row else 0
                old_level = int(old_row[1]) if old_row else -1
                new_total_xp = old_xp + amount
                new_level = _xp_to_level(new_total_xp)

                db.execute(
                    text(
                        "INSERT INTO fluxer_member_xp "
                        "(guild_id, user_id, username, xp, level, message_count, media_count, reaction_count, voice_minutes, last_message_ts, first_seen, last_active) "
                        "VALUES (:guild_id, :user_id, :username, :xp, :new_level, 1, :inc_media, :inc_reaction, :inc_voice, :now, :now, :now) "
                        "ON DUPLICATE KEY UPDATE "
                        "xp = xp + :xp, "
                        "username = :username, "
                        "level = :new_level, "
                        "message_count = message_count + 1, "
                        "media_count = media_count + :inc_media, "
                        "reaction_count = reaction_count + :inc_reaction, "
                        "voice_minutes = voice_minutes + :inc_voice, "
                        "last_message_ts = :now, "
                        "last_active = :now"
                    ),
                    {
                        "guild_id": int(guild_id),
                        "user_id": int(user_id),
                        "username": username,
                        "xp": amount,
                        "new_level": new_level,
                        "inc_media": inc_media,
                        "inc_reaction": inc_reaction,
                        "inc_voice": inc_voice_minutes,
                        "now": now,
                    },
                )
                if new_level > old_level and old_level >= 0:
                    leveled_up = True

                # Award level roles if any are configured
                if leveled_up:
                    await self._apply_level_roles(guild_id, user_id, new_level, db)

                db.commit()
            logger.debug(f"Awarded {amount} XP to {user_id} in guild {guild_id} (level {new_level})")
        except Exception as e:
            logger.error(f"Failed to award bot XP: {e}", exc_info=True)

        # Also award to unified web profile if user has linked their QuestLog account.
        # If the unified web level went up (and guild local didn't already trigger one),
        # surface a level-up so the user still gets notified on Fluxer.
        web_leveled_up, web_new_level = _award_web_xp(user_id, 'fluxer_message', ref_id)
        if web_leveled_up and not leveled_up:
            leveled_up = True
            new_level = web_new_level
        return leveled_up, new_level

    async def _apply_level_roles(self, guild_id: str, user_id: str, new_level: int, db):
        """Assign any level roles the user has now unlocked, optionally removing previous tier."""
        try:
            rows = db.execute(text(
                "SELECT role_id, remove_previous FROM web_fluxer_level_roles "
                "WHERE guild_id = :g AND level_required <= :lvl ORDER BY level_required DESC LIMIT 1"
            ), {'g': guild_id, 'lvl': new_level}).fetchall()
            if not rows:
                return

            http = self.bot._http if hasattr(self.bot, '_http') else None
            if not http:
                return

            for role_id, remove_prev in rows:
                try:
                    await http.add_guild_member_role(int(guild_id), int(user_id), int(role_id),
                                                     reason=f'QuestLog level-up to {new_level}')
                except Exception as e:
                    logger.debug(f"Level role assign failed guild={guild_id} user={user_id} role={role_id}: {e}")

            if remove_prev:
                # Remove all lower-tier level roles
                lower = db.execute(text(
                    "SELECT role_id FROM web_fluxer_level_roles "
                    "WHERE guild_id = :g AND level_required < :lvl"
                ), {'g': guild_id, 'lvl': new_level}).fetchall()
                for (old_role_id,) in lower:
                    try:
                        await http.remove_guild_member_role(int(guild_id), int(user_id), int(old_role_id),
                                                            reason='QuestLog level role replaced')
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"_apply_level_roles failed: {e}")

    async def _send_levelup_message(self, message, guild_id: str, new_level: int, cfg: dict):
        """Send the level-up notification according to guild config."""
        try:
            dest = cfg['level_up_destination']
            template = cfg['level_up_message'] or DEFAULT_LEVELUP_MESSAGE
            display_name = getattr(message.author, 'display_name', None) or getattr(message.author, 'username', None) or 'Member'
            mention = getattr(message.author, 'mention', None) or display_name
            guild_name = (getattr(message.guild, 'name', None) or '') if message.guild else ''

            text_msg = _format_levelup_message(template, display_name, mention, new_level, guild_name)
            embed = fluxer.Embed(
                title="Level Up!",
                description=text_msg,
                color=GOLD_COLOR,
            )

            http = self.bot._http if hasattr(self.bot, '_http') else None
            if not http:
                return

            if dest == 'dm':
                try:
                    dm = await http.start_private_message(message.author.id)
                    await http.send_message(dm['id'], embed=embed)
                except Exception as e:
                    logger.debug(f"Level-up DM failed: {e}")
            elif dest == 'channel' and cfg['level_up_channel_id']:
                try:
                    await http.send_message(cfg['level_up_channel_id'], embed=embed)
                except Exception as e:
                    logger.debug(f"Level-up channel send failed: {e}")
            else:
                # 'current' - reply in the same channel
                try:
                    channel_id = str(getattr(message, 'channel_id', ''))
                    if channel_id:
                        await http.send_message(channel_id, embed=embed)
                except Exception as e:
                    logger.debug(f"Level-up current-channel send failed: {e}")
        except Exception as e:
            logger.warning(f"_send_levelup_message failed: {e}")

    @Cog.command()
    async def xp(self, ctx):
        """!xp - Show your QuestLog profile (unified if linked, guild-local otherwise)."""
        user_id = str(ctx.author.id)
        if _check_cmd_cooldown('xp', user_id):
            return
        guild_id = str(ctx.guild.id) if ctx.guild else None
        if not guild_id:
            return

        try:
            display_name = getattr(ctx.author, "display_name", None) or ctx.author.username
            avatar_url = _get_avatar(ctx.author)

            with db_session_scope() as db:
                # Guild-local stats
                local_row = db.execute(
                    text(
                        "SELECT xp, level, message_count, media_count, voice_minutes, reaction_count, "
                        "RANK() OVER (PARTITION BY guild_id ORDER BY xp DESC) AS rank_pos "
                        "FROM fluxer_member_xp WHERE guild_id = :g AND user_id = :u"
                    ),
                    {"g": int(guild_id), "u": int(user_id)},
                ).fetchone()

                # Network check
                network_row = db.execute(
                    text(
                        "SELECT id FROM web_communities "
                        "WHERE platform='fluxer' AND platform_id=:g AND network_status='approved' LIMIT 1"
                    ),
                    {"g": guild_id},
                ).fetchone()
                is_network = network_row is not None

                # Unified profile lookup (fluxer_id links to web_users)
                unified_row = None
                if is_network:
                    unified_row = db.execute(
                        text(
                            "SELECT web_xp, web_level, hero_points, username "
                            "FROM web_users WHERE fluxer_id=:uid LIMIT 1"
                        ),
                        {"uid": user_id},
                    ).fetchone()

            local_xp = local_row[0] if local_row else 0
            local_level = local_row[1] if local_row else 0
            msg_count = local_row[2] if local_row else 0
            media_count = local_row[3] if local_row else 0
            voice_minutes = local_row[4] if local_row else 0
            reaction_count = local_row[5] if local_row else 0
            rank_pos = local_row[6] if local_row else None

            if is_network and unified_row:
                # Unified QuestLog profile - matches Discord /xp profile layout exactly
                embed = fluxer.Embed(
                    title=f"📊 {display_name}'s QuestLog Profile",
                    url=f"https://casual-heroes.com/ql/profile/{unified_row.username}/",
                    color=0x6366F1,
                )
                embed.add_field(name="🌐 QuestLog XP", value=f"**{unified_row.web_xp:,.0f}**", inline=True)
                embed.add_field(name="🏆 QL Level", value=f"**{unified_row.web_level}**", inline=True)
                embed.add_field(name="🪙 Hero Points", value=f"**{unified_row.hero_points}**", inline=True)
                embed.add_field(
                    name="📈 Server Activity",
                    value=(
                        f"Messages: {msg_count:,}\n"
                        f"  \u2514 Media: {media_count:,}\n"
                        f"Voice: {voice_minutes:,} min\n"
                        f"Reactions: {reaction_count:,}"
                    ),
                    inline=True,
                )
                embed.set_footer(text="QuestLog Network - unified profile | casual-heroes.com/ql/")
            elif is_network:
                # Network guild but user not linked
                embed = fluxer.Embed(
                    title=f"📊 {display_name}'s Profile",
                    color=BRAND_COLOR,
                )
                embed.add_field(name="Level", value=str(local_level), inline=True)
                embed.add_field(name="XP", value=f"{local_xp:,.0f}", inline=True)
                embed.add_field(name="Messages", value=f"{msg_count:,}", inline=True)
                if rank_pos:
                    embed.add_field(name="Server Rank", value=f"#{rank_pos}", inline=False)
                embed.add_field(
                    name="",
                    value="[Link your QuestLog account](https://casual-heroes.com/ql/) to see your unified profile!",
                    inline=False,
                )
                embed.set_footer(text="QuestLog Network server | casual-heroes.com/ql/")
            else:
                # Non-network guild - guild-local only
                embed = fluxer.Embed(
                    title=f"{display_name}'s XP",
                    color=BRAND_COLOR,
                    url="https://casual-heroes.com/ql/",
                )
                embed.add_field(name="Level", value=str(local_level), inline=True)
                embed.add_field(name="XP", value=f"{local_xp:,.0f}", inline=True)
                embed.add_field(name="Messages", value=f"{msg_count:,}", inline=True)
                if rank_pos:
                    embed.add_field(name="Server Rank", value=f"#{rank_pos}", inline=True)
                embed.add_field(
                    name="Progress to Next Level",
                    value=_xp_bar(local_xp, local_level),
                    inline=False,
                )
                embed.set_footer(text="Earn XP by chatting! | casual-heroes.com/ql/")

            if avatar_url:
                embed.set_thumbnail(url=avatar_url)

            await ctx.reply(embed=embed)
        except Exception as e:
            logger.error(f"cmd_xp error: {e}", exc_info=True)
            embed = fluxer.Embed(title="Error", description="Could not fetch XP right now.", color=0xED4245)
            await ctx.reply(embed=embed)

    @Cog.command()
    async def leaderboard(self, ctx):
        """!leaderboard - Top 10 members by XP. Uses unified leaderboard if guild is opted in."""
        user_id = str(ctx.author.id)
        if _check_cmd_cooldown('leaderboard', user_id):
            return
        guild_id = str(ctx.guild.id) if ctx.guild else None
        if not guild_id:
            return

        try:
            with db_session_scope() as db:
                # Check if this guild is opted in to the unified leaderboard
                community = db.execute(
                    text(
                        "SELECT site_xp_to_guild FROM web_communities "
                        "WHERE platform='fluxer' AND platform_id=:g AND network_status='approved' AND is_active=1 LIMIT 1"
                    ),
                    {"g": guild_id},
                ).fetchone()
                is_unified = bool(community and community[0])

                if is_unified:
                    # Use fluxer_member_xp as base so ALL members appear,
                    # then LEFT JOIN unified data for linked users
                    rows = db.execute(
                        text(
                            "SELECT "
                            "  COALESCE(wu.id, 0) AS user_id, "
                            "  COALESCE(wu.username, fx.username) AS username, "
                            "  COALESCE(ul.xp_total, fx.xp) AS xp, "
                            "  COALESCE(wu.web_level, fx.level) AS level, "
                            "  COALESCE(ul.messages, fx.message_count) AS msg_count "
                            "FROM fluxer_member_xp fx "
                            "LEFT JOIN web_users wu "
                            "  ON wu.fluxer_id COLLATE utf8mb4_general_ci = fx.user_id COLLATE utf8mb4_general_ci "
                            "LEFT JOIN web_unified_leaderboard ul "
                            "  ON ul.user_id = wu.id AND ul.guild_id = :g AND ul.platform = 'fluxer' "
                            "WHERE fx.guild_id = :gi "
                            "ORDER BY xp DESC LIMIT 10"
                        ),
                        {"g": guild_id, "gi": int(guild_id)},
                    ).fetchall()
                    title = "Unified XP Leaderboard"
                    footer = "Unified XP across all platforms | casual-heroes.com/ql/"
                else:
                    rows = db.execute(
                        text(
                            "SELECT user_id, username, xp, level, message_count FROM fluxer_member_xp "
                            "WHERE guild_id = :g ORDER BY xp DESC LIMIT 10"
                        ),
                        {"g": int(guild_id)},
                    ).fetchall()
                    title = "XP Leaderboard"
                    footer = "Chat more to climb the leaderboard! | Use !xp to check your stats."

            if not rows:
                embed = fluxer.Embed(
                    title=title,
                    description="No XP data yet - start chatting to earn XP!",
                    color=GOLD_COLOR,
                )
                await ctx.reply(embed=embed)
                return

            lines = []
            for i, row in enumerate(rows, 1):
                medal = MEDALS.get(i, f"**{i}.**")
                uid, username, xp, level, msg_count = row[0], row[1], row[2], row[3], row[4]
                lines.append(f"{medal} **{username}** - Level {level} | {xp:,.0f} XP | {msg_count:,} msgs")

            embed = fluxer.Embed(
                title=title,
                description="\n".join(lines),
                color=GOLD_COLOR,
            )
            embed.set_footer(text=footer)
            await ctx.reply(embed=embed)
        except Exception as e:
            logger.error(f"cmd_leaderboard error: {e}", exc_info=True)
            embed = fluxer.Embed(title="Error", description="Could not fetch leaderboard.", color=0xED4245)
            await ctx.reply(embed=embed)

    @Cog.command(name='heroshop')
    async def cmd_heroshop(self, ctx):
        """!heroshop - Opens the QuestLog Hero Shop to browse and buy flairs."""
        embed = fluxer.Embed(
            title="QuestLog Hero Shop",
            description=(
                "Browse flairs, cosmetics, and Champion-exclusive items in the Hero Shop.\n\n"
                "[Open the Hero Shop](https://casual-heroes.com/ql/shop/)"
            ),
            color=BRAND_COLOR,
            url="https://casual-heroes.com/ql/shop/",
        )
        embed.set_footer(text="QuestLog Hero Shop | casual-heroes.com/ql/shop/")
        await ctx.reply(embed=embed)


def setup(bot):
    bot.add_cog(XpCog(bot))
