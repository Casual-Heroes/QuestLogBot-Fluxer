# bot.py - QuestLog Fluxer Bot Entry Point
#
# Run with: python bot.py

import asyncio
import fluxer
from config import get_bot_token, COMMAND_PREFIX, logger
from cogs.core import CoreCog
from cogs.xp import XpCog
from cogs.moderation import ModerationCog
from cogs.lfg import LfgCog
from cogs.flair_sync import FlairSyncCog
from cogs.invite import InviteCog
from cogs.bridge import BridgeCog
from cogs.member_sync import MemberSyncCog
from cogs.welcome import WelcomeCog
from cogs.discovery import DiscoveryCog
from cogs.rss import RssCog
from cogs.trackers import TrackersCog
from cogs.live_alerts import LiveAlertsCog
from cogs.creators import CreatorsCog
from cogs.audit import AuditCog

intents = fluxer.Intents.default()

bot = fluxer.Bot(command_prefix=COMMAND_PREFIX, intents=intents)


@bot.event
async def on_ready():
    # Wait briefly for GUILD_CREATE events to arrive after READY
    await asyncio.sleep(2)
    logger.info(f"Logged in as {bot.user} - serving {len(bot.guilds)} communities")


async def main():
    token = get_bot_token()
    await bot.add_cog(CoreCog(bot))
    await bot.add_cog(XpCog(bot))
    await bot.add_cog(ModerationCog(bot))
    await bot.add_cog(LfgCog(bot))
    await bot.add_cog(FlairSyncCog(bot))
    await bot.add_cog(InviteCog(bot))
    await bot.add_cog(BridgeCog(bot))
    await bot.add_cog(MemberSyncCog(bot))
    await bot.add_cog(WelcomeCog(bot))
    await bot.add_cog(DiscoveryCog(bot))
    await bot.add_cog(RssCog(bot))
    await bot.add_cog(TrackersCog(bot))
    await bot.add_cog(LiveAlertsCog(bot))
    await bot.add_cog(CreatorsCog(bot))
    await bot.add_cog(AuditCog(bot))
    logger.info("Starting QuestLog Fluxer Bot...")
    await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
