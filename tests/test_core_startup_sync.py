import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs.core import CoreCog


class StartupGuildSyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_gateway_reconciliation_join_is_deferred_until_ready(self):
        bot = SimpleNamespace(guilds=[])
        cog = CoreCog(bot)
        cog._sync_single_guild = AsyncMock()
        cog._sync_guild_minimal = AsyncMock()

        await cog.on_guild_join(SimpleNamespace(id=123, name=None))

        cog._sync_single_guild.assert_not_awaited()
        cog._sync_guild_minimal.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
