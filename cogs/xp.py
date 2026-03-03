# cogs/xp.py - XP and leveling system
#
# Awards XP for messages, tracks levels, shows leaderboard.
# Syncs with the QuestLog web platform via shared MySQL DB.
#
# TODO slash commands: /xp profile, /xp leaderboard, /flair store

import time
from config import logger, db_session_scope


# XP per action - keep in sync with web platform helpers.py XP_ACTIONS
XP_PER_MESSAGE = 2
XP_PER_REACTION = 1
MESSAGE_COOLDOWN_SECONDS = 60

# Per-user cooldown cache: {user_id: last_xp_timestamp}
_message_cooldowns: dict[str, float] = {}


def _xp_to_level(xp: int) -> int:
    """Simple level formula - matches web platform logic."""
    return int(xp ** 0.5) // 5


class XpCog:
    def __init__(self, client):
        self._client = client

    async def on_message_create(self, message: dict):
        """Award XP for messages, respecting cooldown."""
        author = message.get("author", {})
        if author.get("bot"):
            return

        user_id = author.get("id")
        if not user_id:
            return

        now = time.time()
        last = _message_cooldowns.get(user_id, 0)
        if now - last < MESSAGE_COOLDOWN_SECONDS:
            return

        _message_cooldowns[user_id] = now
        guild_id = message.get("guild_id")
        if not guild_id:
            return

        await self._award_xp(user_id, guild_id, XP_PER_MESSAGE, "message")

    async def _award_xp(self, user_id: str, guild_id: str, amount: int, action: str):
        """Write XP to shared DB. Matches wardenbot XP table schema."""
        try:
            with db_session_scope() as db:
                from sqlalchemy import text
                db.execute(
                    text(
                        "INSERT INTO guild_member_xp (guild_id, user_id, xp, updated_at) "
                        "VALUES (:guild_id, :user_id, :xp, :now) "
                        "ON DUPLICATE KEY UPDATE xp = xp + :xp, updated_at = :now"
                    ),
                    {"guild_id": guild_id, "user_id": user_id, "xp": amount, "now": int(time.time())},
                )
                logger.debug(f"Awarded {amount} XP to {user_id} for {action}")
        except Exception as e:
            logger.error(f"Failed to award XP: {e}", exc_info=True)

    # ====== Commands ======

    async def cmd_xp(self, message: dict, args: list):
        """!xp - Show your XP and level."""
        user_id = message["author"]["id"]
        guild_id = message.get("guild_id")
        if not guild_id:
            return

        try:
            with db_session_scope() as db:
                from sqlalchemy import text
                row = db.execute(
                    text("SELECT xp FROM guild_member_xp WHERE guild_id = :g AND user_id = :u"),
                    {"g": guild_id, "u": user_id},
                ).fetchone()
            xp = row[0] if row else 0
            level = _xp_to_level(xp)
            await self._client.send_reply(
                message,
                f"**{message['author']['username']}** - Level {level} | {xp} XP\n"
                f"Full profile: https://casual-heroes.com/ql/profile/"
            )
        except Exception as e:
            logger.error(f"cmd_xp error: {e}", exc_info=True)
            await self._client.send_reply(message, "Could not fetch XP right now.")

    async def cmd_leaderboard(self, message: dict, args: list):
        """!leaderboard - Top 10 members by XP."""
        guild_id = message.get("guild_id")
        if not guild_id:
            return

        try:
            with db_session_scope() as db:
                from sqlalchemy import text
                rows = db.execute(
                    text(
                        "SELECT user_id, xp FROM guild_member_xp "
                        "WHERE guild_id = :g ORDER BY xp DESC LIMIT 10"
                    ),
                    {"g": guild_id},
                ).fetchall()

            if not rows:
                await self._client.send_reply(message, "No XP data yet - start chatting!")
                return

            lines = ["**Top 10 Members**"]
            for i, (uid, xp) in enumerate(rows, 1):
                level = _xp_to_level(xp)
                lines.append(f"{i}. <@{uid}> - Level {level} ({xp} XP)")
            await self._client.send_reply(message, "\n".join(lines))
        except Exception as e:
            logger.error(f"cmd_leaderboard error: {e}", exc_info=True)
            await self._client.send_reply(message, "Could not fetch leaderboard.")

    # TODO: /xp profile, /flair store, /leaderboard slash commands
