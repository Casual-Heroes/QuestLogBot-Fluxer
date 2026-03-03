# cogs/moderation.py - Moderation commands
#
# !ban, !kick, !timeout, !unban
# Uses Fluxer-specific fields: ban_duration_seconds, timeout_reason
#
# TODO slash commands: /ban, /kick, /timeout, /unban

import datetime
from config import logger


def _has_mod_permission(member_roles: list, mod_role_ids: set) -> bool:
    """Check if a member has a mod role. Override in guild config."""
    return bool(set(member_roles) & mod_role_ids)


async def _get_member_roles(client, guild_id: str, user_id: str) -> list:
    try:
        data = await client.get(f"/guilds/{guild_id}/members/{user_id}")
        return data.get("roles", [])
    except Exception:
        return []


class ModerationCog:
    def __init__(self, client):
        self._client = client
        # TODO: load per-guild mod role IDs from DB
        self._mod_role_ids: set = set()

    async def _check_mod(self, message: dict) -> bool:
        """Return True if message author has mod permissions."""
        guild_id = message.get("guild_id")
        if not guild_id:
            return False
        user_id = message["author"]["id"]
        roles = await _get_member_roles(self._client, guild_id, user_id)
        if not roles and not self._mod_role_ids:
            # No mod roles configured - only guild owner can use commands
            try:
                guild = await self._client.get(f"/guilds/{guild_id}")
                return guild.get("owner_id") == user_id
            except Exception:
                return False
        return _has_mod_permission(roles, self._mod_role_ids)

    def _parse_mention(self, text: str) -> str | None:
        """Extract user ID from <@123> or plain ID."""
        text = text.strip()
        if text.startswith("<@") and text.endswith(">"):
            return text[2:-1].lstrip("!")
        if text.isdigit():
            return text
        return None

    # ====== Commands ======

    async def cmd_ban(self, message: dict, args: list):
        """!ban @user [reason] - Permanently ban a member."""
        if not await self._check_mod(message):
            await self._client.send_reply(message, "You don't have permission to use this command.")
            return

        if not args:
            await self._client.send_reply(message, "Usage: `!ban @user [reason]`")
            return

        user_id = self._parse_mention(args[0])
        if not user_id:
            await self._client.send_reply(message, "Please mention a valid user.")
            return

        reason = " ".join(args[1:]) or "No reason provided"
        guild_id = message["guild_id"]

        try:
            await self._client.ban_member(guild_id, user_id, reason=reason, delete_message_days=1)
            await self._client.send_reply(message, f"Banned <@{user_id}>. Reason: {reason}")
            logger.info(f"Banned {user_id} from {guild_id} - {reason}")
        except Exception as e:
            logger.error(f"Ban failed: {e}", exc_info=True)
            await self._client.send_reply(message, "Failed to ban user.")

    async def cmd_tempban(self, message: dict, args: list):
        """!tempban @user <hours> [reason] - Temporarily ban a member."""
        if not await self._check_mod(message):
            await self._client.send_reply(message, "You don't have permission to use this command.")
            return

        if len(args) < 2:
            await self._client.send_reply(message, "Usage: `!tempban @user <hours> [reason]`")
            return

        user_id = self._parse_mention(args[0])
        if not user_id:
            await self._client.send_reply(message, "Please mention a valid user.")
            return

        try:
            hours = int(args[1])
        except ValueError:
            await self._client.send_reply(message, "Hours must be a number.")
            return

        reason = " ".join(args[2:]) or "No reason provided"
        guild_id = message["guild_id"]

        try:
            await self._client.ban_member(
                guild_id, user_id,
                reason=reason,
                delete_message_days=0,
                duration_seconds=hours * 3600,
            )
            await self._client.send_reply(
                message, f"Temp-banned <@{user_id}> for {hours}h. Reason: {reason}"
            )
        except Exception as e:
            logger.error(f"Tempban failed: {e}", exc_info=True)
            await self._client.send_reply(message, "Failed to temp-ban user.")

    async def cmd_kick(self, message: dict, args: list):
        """!kick @user [reason] - Kick a member."""
        if not await self._check_mod(message):
            await self._client.send_reply(message, "You don't have permission to use this command.")
            return

        if not args:
            await self._client.send_reply(message, "Usage: `!kick @user [reason]`")
            return

        user_id = self._parse_mention(args[0])
        if not user_id:
            await self._client.send_reply(message, "Please mention a valid user.")
            return

        guild_id = message["guild_id"]
        reason = " ".join(args[1:]) or "No reason provided"

        try:
            await self._client.kick_member(guild_id, user_id)
            await self._client.send_reply(message, f"Kicked <@{user_id}>. Reason: {reason}")
            logger.info(f"Kicked {user_id} from {guild_id} - {reason}")
        except Exception as e:
            logger.error(f"Kick failed: {e}", exc_info=True)
            await self._client.send_reply(message, "Failed to kick user.")

    async def cmd_timeout(self, message: dict, args: list):
        """!timeout @user <minutes> [reason] - Timeout a member."""
        if not await self._check_mod(message):
            await self._client.send_reply(message, "You don't have permission to use this command.")
            return

        if len(args) < 2:
            await self._client.send_reply(message, "Usage: `!timeout @user <minutes> [reason]`")
            return

        user_id = self._parse_mention(args[0])
        if not user_id:
            await self._client.send_reply(message, "Please mention a valid user.")
            return

        try:
            minutes = int(args[1])
        except ValueError:
            await self._client.send_reply(message, "Minutes must be a number.")
            return

        reason = " ".join(args[2:]) or "No reason provided"
        guild_id = message["guild_id"]
        until = (
            datetime.datetime.utcnow() + datetime.timedelta(minutes=minutes)
        ).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        try:
            await self._client.timeout_member(guild_id, user_id, until=until, reason=reason)
            await self._client.send_reply(
                message, f"Timed out <@{user_id}> for {minutes} minutes. Reason: {reason}"
            )
        except Exception as e:
            logger.error(f"Timeout failed: {e}", exc_info=True)
            await self._client.send_reply(message, "Failed to timeout user.")

    # TODO: /ban, /kick, /timeout, /tempban slash commands
    # TODO: load mod role IDs from DB per guild
    # TODO: audit log integration
