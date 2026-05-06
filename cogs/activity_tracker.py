# cogs/activity_tracker.py
# Tracks Fluxer member activity by role and writes to fluxer_activity_data.json
# Runs every 5 minutes. Django reads this file to populate game page member counts.

import asyncio
import json
import logging
from collections import defaultdict
from pathlib import Path
from sqlalchemy import text
from fluxer import Cog

from config import db_session_scope, logger

ACTIVITY_FILE = Path("/mnt/gamestoreage2/DiscordBots/questlogfluxer/data/fluxer_activity_data.json")
UPDATE_INTERVAL = 300  # 5 minutes


class ActivityTrackerCog(Cog):
    def __init__(self, bot):
        super().__init__(bot)
        self._running = False

    @Cog.listener()
    async def on_ready(self):
        if not self._running:
            self._running = True
            asyncio.ensure_future(self._activity_loop())

    async def _activity_loop(self):
        await asyncio.sleep(15)  # brief startup delay
        while True:
            try:
                await self._update_activity()
            except Exception as e:
                logger.error(f"ActivityTracker update failed: {e}", exc_info=True)
            await asyncio.sleep(UPDATE_INTERVAL)

    async def _update_activity(self):
        # Load role mappings from DB: game_key -> [(guild_id, role_id)]
        mappings = defaultdict(list)
        with db_session_scope() as db:
            rows = db.execute(text("""
                SELECT sag.game_key, fr.guild_id, fr.role_id
                FROM site_activity_fluxer_roles fr
                JOIN site_activity_games sag ON sag.id = fr.game_id
                WHERE fr.is_active = 1 AND sag.is_active = 1
            """)).fetchall()
            for row in rows:
                mappings[row[0]].append((str(row[1]), str(row[2])))

        if not mappings:
            logger.debug("ActivityTracker: no Fluxer role mappings configured")
            return

        activity = {}
        for game_key, role_pairs in mappings.items():
            total_members = set()
            for guild_id, role_id in role_pairs:
                try:
                    members = await self._get_role_members(guild_id, role_id)
                    total_members.update(members)
                except Exception as e:
                    logger.warning(f"ActivityTracker: failed to get members for guild={guild_id} role={role_id}: {e}")

            count = len(total_members)
            activity[game_key] = {
                "active": 0,   # no rich presence on Fluxer yet - will auto-populate when added
                "online": 0,
                "total": count,
            }
            logger.debug(f"ActivityTracker: {game_key} = {count} members")

        ACTIVITY_FILE.parent.mkdir(parents=True, exist_ok=True)
        ACTIVITY_FILE.write_text(json.dumps(activity, indent=2))
        logger.info(f"ActivityTracker: wrote {len(activity)} games to {ACTIVITY_FILE}")

    async def _get_role_members(self, guild_id: str, role_id: str) -> list:
        """Fetch member IDs that have a specific role in a Fluxer guild."""
        # Use get_guild_members convenience method (same pattern as trackers.py)
        try:
            members_data = await self.bot._http.get_guild_members(guild_id, limit=1000)
        except Exception as e:
            logger.warning(f"ActivityTracker: get_guild_members failed for guild={guild_id}: {e}")
            return []

        if not members_data:
            return []

        result = []
        for m in members_data:
            user = m.get("user", m)
            if user.get("bot"):
                continue
            m_roles = [str(r) for r in (m.get("roles") or [])]
            if role_id in m_roles:
                uid = user.get("id") or m.get("id")
                if uid:
                    result.append(str(uid))

        return result
