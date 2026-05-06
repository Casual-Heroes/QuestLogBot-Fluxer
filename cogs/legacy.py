# cogs/legacy.py - Legacy points for Fluxer server behavior
#
# Two systems:
#
# 1. Star reactions (⭐)
#    - When a member reacts ⭐ to someone else's message, the message author
#      earns +5 Legacy (comment_helpful). Anti-abuse: one ⭐ per reactor per
#      message (ref_id = "star_{message_id}_{reactor_id}"). Bots ignored.
#      No daily cap here - ref_id dedup in award_legacy handles it.
#
# 2. Clean record milestones (background loop, runs every 6 hours)
#    - Checks web_fluxer_members.joined_at for users linked to a QuestLog account
#    - Awards clean_record_30d / 60d / 90d if:
#        a) They have been a member >= that many days
#        b) They have NO negative legacy events (report_upheld, temp_ban) since joining
#        c) Not already awarded (ref_id dedup)
#
# Source for all awards: 'fluxer'

import asyncio
import time

from fluxer import Cog
from sqlalchemy import text

from config import logger, db_session_scope
from cogs.xp import _award_web_legacy

STAR_EMOJI = '\u2b50'  # ⭐ unicode

CHECK_INTERVAL = 6 * 3600  # 6 hours

CLEAN_RECORD_MILESTONES = [
    (30,  'clean_record_30d'),
    (60,  'clean_record_60d'),
    (90,  'clean_record_90d'),
]


class LegacyCog(Cog):
    """Legacy points for Fluxer server behavior: star reactions + clean record milestones."""

    def __init__(self, bot):
        super().__init__(bot)
        self._task: asyncio.Task | None = None

    @Cog.listener()
    async def on_ready(self):
        logger.info("LegacyCog ready - starting clean record loop")
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._clean_record_loop())

    # ------------------------------------------------------------------
    # Star reaction handler
    # ------------------------------------------------------------------

    @Cog.listener()
    async def on_raw_reaction_add(self, payload):
        """Award Legacy when someone reacts ⭐ to another member's message."""
        # Only star emoji
        emoji_name = str(payload.emoji) if payload.emoji else ''
        if emoji_name != STAR_EMOJI:
            return

        # Must be in a guild
        if not payload.guild_id:
            return

        reactor_id = str(payload.user_id)

        # Ignore bot reactions
        if reactor_id == str(getattr(self.bot, 'user', None) and self.bot.user.id or 0):
            return

        channel_id = str(payload.channel_id)
        message_id = str(payload.message_id)

        # Fetch the message to get the author
        try:
            msg = await self.bot.fetch_message(channel_id, message_id)
        except Exception as e:
            logger.debug(f"LegacyCog: could not fetch message {message_id}: {e}")
            return

        # Don't award to bots
        if not msg or not msg.author or getattr(msg.author, 'bot', False):
            return

        author_id = str(msg.author.id)

        # Don't award self-stars
        if author_id == reactor_id:
            return

        # ref_id: one award per reactor per message
        ref_id = f"star_{message_id}_{reactor_id}"

        pts = _award_web_legacy(author_id, 'comment_helpful', ref_id=ref_id, source='fluxer')
        if pts:
            logger.debug(f"LegacyCog: ⭐ {reactor_id} -> {author_id} +{pts} legacy (msg {message_id})")

    # ------------------------------------------------------------------
    # Clean record background loop
    # ------------------------------------------------------------------

    async def _clean_record_loop(self):
        await asyncio.sleep(60)  # brief startup delay
        while True:
            try:
                await asyncio.get_event_loop().run_in_executor(None, self._check_clean_records)
            except Exception as e:
                logger.error(f"LegacyCog: clean record loop error: {e}")
            await asyncio.sleep(CHECK_INTERVAL)

    def _check_clean_records(self):
        """Sync: find members past 30/60/90 day milestones with no negative legacy events."""
        now = int(time.time())
        day_secs = 86400

        try:
            with db_session_scope() as db:
                # Find all linked members: joined_at + web_user_id via fluxer_id
                rows = db.execute(
                    text(
                        "SELECT fm.user_id, fm.joined_at, wu.id AS web_user_id "
                        "FROM web_fluxer_members fm "
                        "JOIN web_users wu ON wu.fluxer_id = fm.user_id "
                        "WHERE fm.joined_at IS NOT NULL "
                        "  AND fm.left_at IS NULL "
                        "  AND wu.is_banned = 0 "
                    )
                ).fetchall()
        except Exception as e:
            logger.error(f"LegacyCog: clean record DB fetch failed: {e}")
            return

        awarded = 0
        for row in rows:
            fluxer_user_id = str(row[0])
            joined_at = row[1]
            web_user_id = row[2]

            if not joined_at:
                continue

            days_member = (now - int(joined_at)) // day_secs

            for days_required, action_type in CLEAN_RECORD_MILESTONES:
                if days_member < days_required:
                    continue

                ref_id = f"{action_type}_{web_user_id}"

                # Check if already awarded
                try:
                    with db_session_scope() as db:
                        already = db.execute(
                            text(
                                "SELECT id FROM web_legacy_events "
                                "WHERE user_id = :uid AND ref_id = :ref LIMIT 1"
                            ),
                            {"uid": web_user_id, "ref": ref_id},
                        ).fetchone()
                        if already:
                            continue

                        # Check for any negative legacy events since joining
                        neg = db.execute(
                            text(
                                "SELECT id FROM web_legacy_events "
                                "WHERE user_id = :uid "
                                "  AND action_type IN ('report_upheld', 'temp_ban') "
                                "  AND created_at >= :joined "
                                "LIMIT 1"
                            ),
                            {"uid": web_user_id, "joined": int(joined_at)},
                        ).fetchone()
                        if neg:
                            continue
                except Exception as e:
                    logger.error(f"LegacyCog: check failed for user {web_user_id}: {e}")
                    continue

                pts = _award_web_legacy(fluxer_user_id, action_type, ref_id=ref_id, source='fluxer')
                if pts:
                    awarded += 1
                    logger.info(f"LegacyCog: clean record {action_type} -> web_user {web_user_id} +{pts}")

        if awarded:
            logger.info(f"LegacyCog: clean record pass complete - awarded {awarded} milestones")
