import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from cogs.audit import AuditCog, _MSG_CACHE, _message_cache_key


@contextmanager
def _fake_db_scope():
    yield MagicMock()


class _Author:
    def __init__(self, user_id, name='Test Member'):
        self.id = user_id
        self.name = name

    def __str__(self):
        return self.name


class AuditDeleteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _MSG_CACHE.clear()
        self.cog = AuditCog(SimpleNamespace())
        self.cog._send_embed = AsyncMock()

    async def asyncTearDown(self):
        _MSG_CACHE.clear()

    async def _cache_message(self, message_id, guild_id='10', channel_id='20',
                             content='deleted words', author_id='30'):
        message = SimpleNamespace(
            id=message_id, guild_id=guild_id, channel_id=channel_id,
            content=content, author=_Author(author_id), attachments=[], embeds=[],
        )
        await self.cog.on_message(message)

    @patch('cogs.audit._insert_log')
    @patch('cogs.audit._get_settings', return_value=(True, '99', {}))
    @patch('cogs.audit.db_session_scope', side_effect=_fake_db_scope)
    async def test_one_message_bulk_event_preserves_content_and_author(
        self, _scope, _settings, insert_log,
    ):
        await self._cache_message('40')

        await self.cog.on_message_delete_bulk({
            'guild_id': '10', 'channel_id': '20', 'ids': ['40'],
        })

        self.assertNotIn(_message_cache_key('10', '20', '40'), _MSG_CACHE)
        insert_kwargs = insert_log.call_args.kwargs
        self.assertEqual(insert_log.call_args.args[2], 'message_delete')
        self.assertEqual(insert_kwargs['target_id'], '30')
        self.assertIn('deleted words', insert_kwargs['details'])
        send_kwargs = self.cog._send_embed.await_args.kwargs
        self.assertEqual(self.cog._send_embed.await_args.args[1], 'message_delete')
        self.assertIn('deleted words', send_kwargs['details'])

    @patch('cogs.audit._insert_log')
    @patch('cogs.audit._get_settings', return_value=(True, '99', {}))
    @patch('cogs.audit.db_session_scope', side_effect=_fake_db_scope)
    async def test_bulk_event_lists_each_cached_message(
        self, _scope, _settings, insert_log,
    ):
        await self._cache_message('40', content='first deleted message')
        await self._cache_message('41', content='second deleted message')

        await self.cog.on_message_delete_bulk({
            'guild_id': '10', 'channel_id': '20', 'ids': ['40', '41'],
        })

        details = insert_log.call_args.kwargs['details']
        self.assertIn('first deleted message', details)
        self.assertIn('second deleted message', details)
        self.assertEqual(insert_log.call_args.args[2], 'message_bulk_delete')

    @patch('cogs.audit._insert_log')
    @patch('cogs.audit._get_settings', return_value=(True, '99', {}))
    @patch('cogs.audit.db_session_scope', side_effect=_fake_db_scope)
    async def test_delete_cannot_read_another_guild_cache_entry(
        self, _scope, _settings, insert_log,
    ):
        await self._cache_message('40', guild_id='other', content='private text')

        await self.cog.on_message_delete({
            'guild_id': '10', 'channel_id': '20', 'id': '40',
        })

        details = insert_log.call_args.kwargs['details']
        self.assertNotIn('private text', details)
        self.assertIn('not cached', details)

    @patch('cogs.audit._insert_log')
    @patch('cogs.audit._get_settings', return_value=(True, '99', {}))
    @patch('cogs.audit.db_session_scope', side_effect=_fake_db_scope)
    async def test_deleted_content_cannot_create_live_mentions(
        self, _scope, _settings, insert_log,
    ):
        await self._cache_message('40', content='hello @everyone')

        await self.cog.on_message_delete_bulk({
            'guild_id': '10', 'channel_id': '20', 'ids': ['40'],
        })

        details = insert_log.call_args.kwargs['details']
        self.assertNotIn('@everyone', details)
        self.assertIn('@\u200beveryone', details)

    @patch('cogs.audit._insert_log')
    @patch('cogs.audit._get_settings', return_value=(True, '99', {}))
    @patch('cogs.audit.db_session_scope', side_effect=_fake_db_scope)
    async def test_edit_refreshes_the_deleted_message_snapshot(
        self, _scope, _settings, insert_log,
    ):
        await self._cache_message('40', content='original text')
        edited = SimpleNamespace(
            id='40', guild_id='10', channel_id='20', content='edited text',
            author=_Author('30'), attachments=[], embeds=[],
        )
        await self.cog.on_message_edit(edited)

        await self.cog.on_message_delete({
            'guild_id': '10', 'channel_id': '20', 'id': '40',
        })

        details = insert_log.call_args.kwargs['details']
        self.assertIn('edited text', details)
        self.assertNotIn('original text', details)


if __name__ == '__main__':
    unittest.main()
