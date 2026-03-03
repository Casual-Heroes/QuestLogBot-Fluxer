# cogs/lfg.py - Looking for Group
#
# !lfg <game> [description] - Post an LFG request
# !lfglist - Show active LFG posts
# !lfgjoin <id> - Join an LFG post
#
# LFG posts expire after 2 hours.
# Syncs with QuestLog web platform LFG table.
#
# TODO slash commands: /lfg post, /lfg list, /lfg join

import time
from config import logger, db_session_scope

LFG_EXPIRY_SECONDS = 7200  # 2 hours


class LfgCog:
    def __init__(self, client):
        self._client = client

    async def cmd_lfg(self, message: dict, args: list):
        """!lfg <game> [description] - Post an LFG request."""
        if not args:
            await self._client.send_reply(message, "Usage: `!lfg <game> [description]`\nExample: `!lfg Elden Ring looking for coop partner`")
            return

        game = args[0]
        description = " ".join(args[1:]) if len(args) > 1 else ""
        author = message["author"]
        guild_id = message.get("guild_id", "")
        channel_id = message["channel_id"]

        try:
            with db_session_scope() as db:
                from sqlalchemy import text
                result = db.execute(
                    text(
                        "INSERT INTO lfg_posts "
                        "(guild_id, channel_id, user_id, username, game, description, created_at, expires_at, platform) "
                        "VALUES (:guild_id, :channel_id, :user_id, :username, :game, :description, :now, :expires, 'fluxer')"
                    ),
                    {
                        "guild_id": guild_id,
                        "channel_id": channel_id,
                        "user_id": author["id"],
                        "username": author["username"],
                        "game": game,
                        "description": description,
                        "now": int(time.time()),
                        "expires": int(time.time()) + LFG_EXPIRY_SECONDS,
                    },
                )
                post_id = result.lastrowid

            desc_text = f" - {description}" if description else ""
            await self._client.send_reply(
                message,
                f"LFG posted! **{author['username']}** is looking for **{game}**{desc_text}\n"
                f"Post ID: `{post_id}` | Expires in 2 hours\n"
                f"Others can join with `!lfgjoin {post_id}`\n"
                f"Full LFG board: https://casual-heroes.com/ql/lfg/"
            )
        except Exception as e:
            logger.error(f"LFG post failed: {e}", exc_info=True)
            await self._client.send_reply(message, "Failed to create LFG post.")

    async def cmd_lfglist(self, message: dict, args: list):
        """!lfglist - Show active LFG posts in this server."""
        guild_id = message.get("guild_id", "")

        try:
            with db_session_scope() as db:
                from sqlalchemy import text
                rows = db.execute(
                    text(
                        "SELECT id, username, game, description FROM lfg_posts "
                        "WHERE guild_id = :g AND expires_at > :now AND is_active = 1 "
                        "ORDER BY created_at DESC LIMIT 10"
                    ),
                    {"g": guild_id, "now": int(time.time())},
                ).fetchall()

            if not rows:
                await self._client.send_reply(
                    message,
                    "No active LFG posts right now.\nPost one with `!lfg <game>`\nFull LFG: https://casual-heroes.com/ql/lfg/"
                )
                return

            lines = ["**Active LFG Posts**"]
            for post_id, username, game, description in rows:
                desc_text = f" - {description}" if description else ""
                lines.append(f"`#{post_id}` **{username}** - {game}{desc_text}")
            lines.append(f"\nJoin with `!lfgjoin <id>` | Full board: https://casual-heroes.com/ql/lfg/")
            await self._client.send_reply(message, "\n".join(lines))
        except Exception as e:
            logger.error(f"LFG list failed: {e}", exc_info=True)
            await self._client.send_reply(message, "Could not fetch LFG posts.")

    async def cmd_lfgjoin(self, message: dict, args: list):
        """!lfgjoin <id> - Express interest in an LFG post."""
        if not args or not args[0].isdigit():
            await self._client.send_reply(message, "Usage: `!lfgjoin <post_id>`")
            return

        post_id = int(args[0])
        author = message["author"]

        try:
            with db_session_scope() as db:
                from sqlalchemy import text
                row = db.execute(
                    text(
                        "SELECT username, game, user_id, channel_id FROM lfg_posts "
                        "WHERE id = :id AND expires_at > :now AND is_active = 1"
                    ),
                    {"id": post_id, "now": int(time.time())},
                ).fetchone()

            if not row:
                await self._client.send_reply(message, f"LFG post `#{post_id}` not found or expired.")
                return

            poster_username, game, poster_id, channel_id = row

            if poster_id == author["id"]:
                await self._client.send_reply(message, "You can't join your own LFG post.")
                return

            await self._client.send_reply(
                message,
                f"<@{poster_id}> - **{author['username']}** wants to join your **{game}** LFG!\nCoordinate here or DM them."
            )
        except Exception as e:
            logger.error(f"LFG join failed: {e}", exc_info=True)
            await self._client.send_reply(message, "Could not process LFG join.")

    # TODO: /lfg post, /lfg list, /lfg join slash commands
    # TODO: LFG expiry cleanup task
    # TODO: sync LFG posts to QuestLog web platform
