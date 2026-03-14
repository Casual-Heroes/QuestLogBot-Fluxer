# cogs/trackers.py - Channel Stat Trackers for QuestLogFluxer
#
# Mirrors WardenBot's activity_tracker.py:
# - Runs every 60 seconds, reads fluxer_channel_stat_trackers table
# - Counts members with the configured role
# - Optionally counts members with a matching game activity
# - Updates the channel topic if it changed
# - !refreshtrackers - force immediate update (owner/admin only)

import time
import asyncio
import logging

import fluxer
from fluxer import Cog
from sqlalchemy import text

from config import logger, db_session_scope

TRACKER_INTERVAL = 60  # seconds


class TrackersCog(Cog):
    """Channel stat trackers for QuestLogFluxer."""

    def __init__(self, bot):
        super().__init__(bot)
        self._task: asyncio.Task | None = None

    @Cog.listener()
    async def on_ready(self):
        logger.info("TrackersCog ready - starting tracker loop")
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._tracker_loop())

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    async def _tracker_loop(self):
        await asyncio.sleep(10)  # brief startup delay
        while True:
            try:
                await self._update_all_trackers()
            except Exception as e:
                logger.error(f"TrackersCog: loop error: {e}")
            await asyncio.sleep(TRACKER_INTERVAL)

    async def _update_all_trackers(self):
        try:
            with db_session_scope() as db:
                rows = db.execute(
                    text(
                        "SELECT id, guild_id, channel_id, role_id, label, emoji, "
                        "game_name, show_playing_count, last_topic "
                        "FROM fluxer_channel_stat_trackers WHERE enabled = 1"
                    )
                ).fetchall()
        except Exception as e:
            logger.error(f"TrackersCog: DB fetch failed: {e}")
            return

        for row in rows:
            try:
                await self._update_tracker(row)
            except Exception as e:
                logger.warning(f"TrackersCog: failed to update tracker {row.id}: {e}")

    async def _update_tracker(self, row):
        guild_id = str(row.guild_id)
        channel_id = str(row.channel_id)
        role_id = str(row.role_id)

        # Find guild in bot
        guild = None
        for g in self.bot.guilds:
            if str(g.id) == guild_id:
                guild = g
                break
        if not guild:
            return

        # Count role members via HTTP API
        try:
            members_data = await self.bot._http.get_guild_members(guild_id, limit=1000)
        except Exception:
            members_data = []

        # Fallback to guild.fetch_members
        if not members_data:
            try:
                fetched = await guild.fetch_members(limit=1000)
                members_data = [
                    {'roles': [str(r) for r in (m.roles or [])],
                     'user': {'bot': getattr(m.user, 'bot', False)}}
                    for m in (fetched or [])
                ]
            except Exception as e:
                logger.debug(f"TrackersCog: fetch_members failed for {guild_id}: {e}")
                return

        # Count members with the role
        role_members = 0
        for m in (members_data or []):
            user = m.get('user', m)
            if user.get('bot'):
                continue
            m_roles = [str(r) for r in (m.get('roles', []) or [])]
            if role_id in m_roles:
                role_members += 1

        # Build topic
        emoji = row.emoji or ''
        prefix = (emoji + ' ') if emoji else ''
        topic = f"{prefix}{row.label}: {role_members} members"

        if row.show_playing_count and row.game_name:
            # Game tracking is not yet available in Fluxer (no presence/activity data)
            # Omit the playing count for now - topic just shows member count
            pass

        # Only update if topic changed
        if topic == (row.last_topic or ''):
            return

        try:
            await self.bot._http.modify_channel(channel_id, topic=topic)
            with db_session_scope() as db:
                db.execute(
                    text(
                        "UPDATE fluxer_channel_stat_trackers "
                        "SET last_topic = :t, last_updated = :ts "
                        "WHERE id = :id"
                    ),
                    {'t': topic[:500], 'ts': int(time.time()), 'id': row.id},
                )
                db.commit()
            logger.debug(f"TrackersCog: updated tracker {row.id} -> {topic!r}")
        except Exception as e:
            logger.warning(f"TrackersCog: modify_channel failed for {channel_id}: {e}")

    # ------------------------------------------------------------------
    # !refreshtrackers command
    # ------------------------------------------------------------------

    @Cog.command(name="refreshtrackers")
    async def refreshtrackers(self, ctx):
        """Force-refresh all channel stat trackers for this guild. Owner only."""
        guild_id = str(ctx.guild.id) if hasattr(ctx, 'guild') and ctx.guild else None
        if not guild_id:
            return

        # Owner check
        is_owner = False
        try:
            with db_session_scope() as db:
                row = db.execute(
                    text("SELECT owner_id FROM web_fluxer_guild_settings WHERE guild_id = :g"),
                    {'g': guild_id},
                ).fetchone()
                if row and str(row.owner_id) == str(ctx.author.id):
                    is_owner = True
        except Exception:
            pass

        if not is_owner:
            await self.bot._http.send_message(str(ctx.channel.id), content="Only the server owner can force-refresh trackers.")
            return

        await self.bot._http.send_message(str(ctx.channel.id), content="Refreshing channel stat trackers...")
        try:
            with db_session_scope() as db:
                rows = db.execute(
                    text(
                        "SELECT id, guild_id, channel_id, role_id, label, emoji, "
                        "game_name, show_playing_count, last_topic "
                        "FROM fluxer_channel_stat_trackers WHERE enabled = 1 AND guild_id = :g"
                    ),
                    {'g': guild_id},
                ).fetchall()
            count = 0
            for row in rows:
                # Force update by clearing last_topic
                with db_session_scope() as db:
                    db.execute(
                        text("UPDATE fluxer_channel_stat_trackers SET last_topic = NULL WHERE id = :id"),
                        {'id': row.id},
                    )
                    db.commit()
                await self._update_tracker(row)
                count += 1
            await self.bot._http.send_message(str(ctx.channel.id), content=f"Refreshed {count} tracker(s).")
        except Exception as e:
            logger.error(f"TrackersCog: refreshtrackers failed: {e}")
            await self.bot._http.send_message(str(ctx.channel.id), content="Failed to refresh trackers. Check logs.")
