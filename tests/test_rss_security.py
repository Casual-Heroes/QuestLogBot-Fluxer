import socket
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cogs import rss


class RSSSecurityTests(unittest.TestCase):
    def test_dns_failure_and_any_private_answer_fail_closed(self):
        with patch.object(
            rss.socket, 'getaddrinfo', side_effect=socket.gaierror('missing'),
        ):
            addresses, error = rss._resolve_public_addresses('missing.example', 443)
        self.assertEqual(addresses, [])
        self.assertEqual(error, 'DNS resolution failed')

        answers = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443)),
        ]
        with patch.object(rss.socket, 'getaddrinfo', return_value=answers):
            addresses, error = rss._resolve_public_addresses('mixed.example', 443)
        self.assertEqual(addresses, [])
        self.assertIn('Private/internal IP', error)

    def test_https_connection_uses_validated_ip_and_original_tls_hostname(self):
        response = SimpleNamespace(
            status=200,
            headers={'Content-Length': '4'},
            read=MagicMock(return_value=b'feed'),
            release_conn=MagicMock(),
        )
        pool = MagicMock()
        pool.request.return_value = response
        with (
            patch.object(
                rss, '_resolve_public_addresses',
                return_value=(['8.8.8.8'], None),
            ),
            patch.object(rss.urllib3, 'HTTPSConnectionPool', return_value=pool) as ctor,
        ):
            status, _headers, body, error = rss._fetch_pinned_once(
                'https://rss.example/feed.xml',
            )
        self.assertIsNone(error)
        self.assertEqual((status, body), (200, b'feed'))
        self.assertEqual(ctor.call_args.args[0], '8.8.8.8')
        self.assertEqual(ctor.call_args.kwargs['assert_hostname'], 'rss.example')
        self.assertEqual(ctor.call_args.kwargs['server_hostname'], 'rss.example')

    def test_dns_answers_are_all_validated_then_connection_attempts_are_bounded(self):
        answers = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 443))
            for address in ('8.8.8.8', '8.8.4.4', '1.1.1.1', '1.0.0.1', '9.9.9.9')
        ]
        with patch.object(rss.socket, 'getaddrinfo', return_value=answers):
            addresses, error = rss._resolve_public_addresses(
                'many.example', 443,
            )
        self.assertIsNone(error)
        self.assertEqual(len(addresses), rss.RSS_MAX_ADDRESSES)

    def test_unicode_hostname_is_normalized_for_dns_host_and_tls(self):
        response = SimpleNamespace(
            status=200, headers={'Content-Length': '4'},
            read=MagicMock(return_value=b'feed'),
            release_conn=MagicMock(),
        )
        pool = MagicMock()
        pool.request.return_value = response
        with (
            patch.object(
                rss, '_resolve_public_addresses',
                return_value=(['8.8.8.8'], None),
            ) as resolver,
            patch.object(rss.urllib3, 'HTTPSConnectionPool', return_value=pool) as ctor,
        ):
            _status, _headers, _body, error = rss._fetch_pinned_once(
                'https://bücher.de/feed.xml',
            )
        self.assertIsNone(error)
        resolver.assert_called_once_with('xn--bcher-kva.de', 443)
        self.assertEqual(ctor.call_args.kwargs['assert_hostname'], 'xn--bcher-kva.de')

    def test_each_redirect_is_revalidated_and_relative_redirects_work(self):
        parsed_feed = SimpleNamespace(entries=[{'title': 'ok'}])
        with (
            patch.object(rss, '_fetch_pinned_once', side_effect=[
                (302, {'Location': '/feed.xml'}, b'', None),
                (200, {}, b'<rss/>', None),
            ]) as fetch,
            patch.object(
                rss, '_feedparser',
                SimpleNamespace(parse=lambda _body: parsed_feed),
            ),
        ):
            parsed, error = rss._secure_fetch_sync('https://rss.example/start')
        self.assertIsNone(error)
        self.assertIs(parsed, parsed_feed)
        self.assertEqual(
            fetch.call_args_list[1].args[0],
            'https://rss.example/feed.xml',
        )
        self.assertEqual(
            fetch.call_args_list[0].args[1],
            fetch.call_args_list[1].args[1],
        )


if __name__ == '__main__':
    unittest.main()
