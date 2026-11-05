import asyncio
import socket
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from cogs import bridge


class BridgeSecurityTests(unittest.IsolatedAsyncioTestCase):
    def test_internal_service_origin_must_be_https_without_credentials(self):
        self.assertEqual(
            bridge._validated_service_base('https://questlog.example/'),
            'https://questlog.example',
        )
        for value in (
            'http://questlog.example',
            'https://user:pass@questlog.example',
            'https://questlog.example:8443',
            'https://questlog.example/?redirect=evil',
        ):
            with self.subTest(value=value):
                with self.assertRaises(RuntimeError):
                    bridge._validated_service_base(value)

    def test_secret_bearing_requests_never_follow_redirects(self):
        source = (Path(bridge.__file__)).read_text(encoding='utf-8')
        lines = source.splitlines()
        request_lines = [
            index for index, line in enumerate(lines)
            if 'headers=_HEADERS' in line
        ]
        self.assertGreater(len(request_lines), 0)
        for index in request_lines:
            with self.subTest(line=index + 1):
                self.assertIn(
                    'allow_redirects=False',
                    '\n'.join(lines[index:index + 6]),
                )

    def test_media_requires_https_allowlisted_host(self):
        with patch.object(bridge, '_TRUSTED_MEDIA_HOSTS', {'media.example'}):
            self.assertTrue(bridge._bridge_media_url_allowed(
                'https://media.example/image.png',
            ))
            self.assertFalse(bridge._bridge_media_url_allowed(
                'http://media.example/image.png',
            ))
            self.assertFalse(bridge._bridge_media_url_allowed(
                'https://media.example@evil.example/image.png',
            ))
            self.assertFalse(bridge._bridge_media_url_allowed(
                'https://127.0.0.1/image.png',
            ))

    async def test_media_resolver_rejects_any_private_dns_answer(self):
        loop = asyncio.get_running_loop()
        answers = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443)),
        ]
        with patch.object(loop, 'getaddrinfo', AsyncMock(return_value=answers)):
            with self.assertRaisesRegex(OSError, 'non-public'):
                await bridge._PublicOnlyResolver('media.example').resolve(
                    'media.example', 443,
                )

    async def test_media_resolver_returns_only_validated_public_addresses(self):
        loop = asyncio.get_running_loop()
        answers = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443)),
        ]
        with patch.object(loop, 'getaddrinfo', AsyncMock(return_value=answers)):
            resolved = await bridge._PublicOnlyResolver(
                'media.example',
            ).resolve('media.example', 443)
        self.assertEqual(resolved[0]['host'], '8.8.8.8')

    async def test_allowlisted_public_media_still_downloads_with_size_bound(self):
        class Content:
            async def iter_chunked(self, _size):
                yield b'image'

        class Response:
            status = 200
            headers = {'Content-Length': '5'}
            content = Content()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        class Session:
            request_kwargs = None

            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def get(self, *_args, **kwargs):
                type(self).request_kwargs = kwargs
                return Response()

        with (
            patch.object(bridge, '_TRUSTED_MEDIA_HOSTS', {'media.example'}),
            patch.object(bridge.aiohttp, 'TCPConnector'),
            patch.object(bridge.aiohttp, 'ClientSession', Session),
        ):
            body = await bridge._download_bridge_media(
                'https://media.example/image.png',
            )
        self.assertEqual(body, b'image')
        self.assertFalse(Session.request_kwargs['allow_redirects'])

    def test_relay_preserves_extra_allowed_attachments_as_links(self):
        source = Path(bridge.__file__).read_text(encoding='utf-8')
        self.assertIn('enumerate(attachments[:10])', source)
        self.assertIn('if index >= _MAX_MEDIA_ATTACHMENTS:', source)
        self.assertIn('attach_text_urls.append(url)', source)

    def test_remote_author_and_quote_cannot_break_provenance_header(self):
        formatted = bridge._format_bridged(
            'D', 'Mallory\n**Admin:**', 'hello', 'quote\n**Fake:**',
        )
        self.assertEqual(formatted.count('\n'), 1)
        self.assertTrue(formatted.startswith(
            '> quote **Fake:**\n**[D] Mallory \\*\\*Admin:\\*\\*:**',
        ))

    def test_manual_blockquote_starts_on_new_line_after_relay_header(self):
        self.assertEqual(
            bridge._format_bridged('D', 'Ryven', '> THIS IS A QUOTE.'),
            '**[D] Ryven:**\n> THIS IS A QUOTE.',
        )

    def test_edit_preserves_existing_provenance(self):
        self.assertEqual(
            bridge._safe_bridge_edit(
                '**[D] Alice:** old',
                '**[D] Mallory:** new',
            ),
            '**[D] Alice:** new',
        )
        self.assertIsNone(bridge._safe_bridge_edit(
            '**[D] Alice:** old',
            '**[F] Alice:** new',
        ))

    def test_pending_relay_channel_is_bound_to_bridge(self):
        original = dict(bridge._channel_map_cache)
        try:
            bridge._channel_map_cache.update({
                'bridge_to_fluxer': {'20': '200'},
                'ts': float('inf'),
            })
            self.assertEqual(bridge._bridge_fluxer_channel_id(20), '200')
            self.assertIsNone(bridge._bridge_fluxer_channel_id(21))
        finally:
            bridge._channel_map_cache.clear()
            bridge._channel_map_cache.update(original)

    def test_bridge_map_can_be_force_refreshed_while_cache_is_warm(self):
        original = dict(bridge._channel_map_cache)
        rows = [(39, '1478533129873944850', '1493337726332960998')]
        result = SimpleNamespace(fetchall=lambda: rows)
        db = SimpleNamespace(execute=lambda *_args, **_kwargs: result)
        context = MagicMock()
        context.__enter__.return_value = db
        try:
            bridge._channel_map_cache.update({
                'fluxer_to_discord': {},
                'discord_to_fluxer': {},
                'bridge_to_fluxer': {},
                'ts': float('inf'),
            })
            with patch('cogs.bridge.db_session_scope', return_value=context):
                bridge._get_channel_maps(force=True)
            self.assertEqual(
                bridge._bridge_fluxer_channel_id(39),
                '1478533129873944850',
            )
        finally:
            bridge._channel_map_cache.clear()
            bridge._channel_map_cache.update(original)


if __name__ == '__main__':
    unittest.main()
