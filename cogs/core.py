# cogs/core.py - Core commands: !help, !ping, !info, !questlog
#
# Slash command equivalents are stubbed with TODO comments.
# When Fluxer ships slash command support, implement the TODO stubs.

from config import logger


class CoreCog:
    def __init__(self, client):
        self._client = client

    async def on_ready(self, data):
        user = data["user"]
        guild_count = len(data.get("guilds", []))
        logger.info(f"CoreCog ready - serving {guild_count} communities")

    # ====== Commands ======

    async def cmd_ping(self, message: dict, args: list):
        """!ping - Check bot latency."""
        await self._client.send_reply(message, "Pong!")

    async def cmd_help(self, message: dict, args: list):
        """!help - Show available commands."""
        help_text = (
            "**QuestLog Bot Commands**\n"
            "`!ping` - Check if the bot is alive\n"
            "`!help` - Show this message\n"
            "`!info` - About QuestLog Bot\n"
            "`!xp` - Show your XP and level\n"
            "`!leaderboard` - Show top members\n"
            "`!lfg <game>` - Post a Looking for Group request\n"
            "`!lfglist` - Show active LFG posts\n"
            "`!ban @user [reason]` - Ban a member (mod only)\n"
            "`!kick @user [reason]` - Kick a member (mod only)\n"
            "`!timeout @user <minutes> [reason]` - Timeout a member (mod only)\n"
            "\nFull platform: https://casual-heroes.com/ql/"
        )
        await self._client.send_reply(message, help_text)

    async def cmd_info(self, message: dict, args: list):
        """!info - Bot information."""
        guild_count = len(self._client.guilds)
        await self._client.send_reply(
            message,
            f"**QuestLog Bot** - Free & open source gaming community bot\n"
            f"Serving {guild_count} communities on Fluxer\n"
            f"Source: https://github.com/Casual-Heroes/QuestLogBot-Fluxer\n"
            f"Platform: https://casual-heroes.com/ql/"
        )

    # TODO: When Fluxer ships slash commands, add:
    # /questlog help
    # /questlog info
    # /questlog dashboard
