import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from cogs import welcome


class WelcomeNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_other_guild_join_never_reaches_discord(self):
        with patch.object(welcome, 'DISCORD_BOT_TOKEN', 'fixture-only'), patch.object(
            welcome.aiohttp, 'ClientSession'
        ) as factory:
            await welcome._notify_discord_join('999', 'Other community member')
        factory.assert_not_called()

    async def test_missing_credential_disables_notice(self):
        with patch.object(welcome, 'DISCORD_BOT_TOKEN', ''), patch.object(
            welcome.aiohttp, 'ClientSession'
        ) as factory:
            await welcome._notify_discord_join(welcome._FLUXER_NOTIFY_GUILD_ID, 'Member')
        factory.assert_not_called()

    async def test_own_guild_notice_disables_mentions_and_redirects(self):
        response = MagicMock()
        response.status = 200
        response.__aenter__ = AsyncMock(return_value=response)
        response.__aexit__ = AsyncMock(return_value=False)
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        session.post.return_value = response
        with patch.object(welcome, 'DISCORD_BOT_TOKEN', 'fixture-only'), patch.object(
            welcome.aiohttp, 'ClientSession', return_value=session
        ):
            await welcome._notify_discord_join(welcome._FLUXER_NOTIFY_GUILD_ID, '@everyone')
        args, kwargs = session.post.call_args
        self.assertIn(welcome._DISCORD_GENERAL_CHANNEL_ID, args[0])
        self.assertEqual(kwargs['json']['allowed_mentions'], {'parse': []})
        self.assertFalse(kwargs['allow_redirects'])
