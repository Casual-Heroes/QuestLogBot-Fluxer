# bot.py - QuestLog Fluxer Bot Entry Point
#
# Run with: python bot.py

import asyncio
import signal
import sys

from config import get_bot_token, COMMAND_PREFIX, logger
from client import FluxerClient
from cogs.core import CoreCog
from cogs.xp import XpCog
from cogs.moderation import ModerationCog
from cogs.lfg import LfgCog


async def main():
    token = get_bot_token()
    client = FluxerClient(token=token, command_prefix=COMMAND_PREFIX)

    # Load cogs
    client.add_cog(CoreCog(client))
    client.add_cog(XpCog(client))
    client.add_cog(ModerationCog(client))
    client.add_cog(LfgCog(client))

    # Graceful shutdown on SIGTERM/SIGINT
    loop = asyncio.get_running_loop()

    def _shutdown():
        logger.info("Shutdown signal received")
        asyncio.create_task(client.close())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown)

    logger.info("Starting QuestLog Fluxer Bot...")
    await client.start()


if __name__ == "__main__":
    asyncio.run(main())
