import unittest
from unittest import mock
import json
import tempfile
from pathlib import Path

from cogs import gameserver
from cogs.gameserver import _normalize_player_id


class PlayerIdentityNormalizationTests(unittest.TestCase):
    def test_palworld_composite_identity_keeps_stable_steam_id(self):
        self.assertEqual(
            _normalize_player_id(
                'steam_76561198201504147, Player id: '
                '198F4E4D000000000000000000000000'
            ),
            'steam_76561198201504147',
        )

    def test_palworld_ip_suffix_is_not_stored_as_identity(self):
        self.assertEqual(
            _normalize_player_id('steam_76561198201504147, IP=192.0.2.10'),
            'steam_76561198201504147',
        )

    def test_identity_is_bounded_to_database_contract(self):
        self.assertEqual(len(_normalize_player_id('x' * 100)), 64)


class EnshroudedPasswordDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_generic_empty_password_cannot_erase_default_role_value(self):
        with tempfile.TemporaryDirectory() as directory:
            instance = Path(directory) / 'ShroudQuest01'
            game = instance / 'enshrouded' / '2278520'
            game.mkdir(parents=True)
            (instance / 'GenericModule.kvp').write_text('', encoding='utf-8')
            (game / 'enshrouded_server.json').write_text(json.dumps({
                'userGroups': [
                    {'name': 'Admin', 'password': 'admin-value'},
                    {'name': 'Default', 'password': 'player-value'},
                ],
            }), encoding='utf-8')
            with mock.patch.object(gameserver, 'AMP_INSTANCES_PATHS', [Path(directory)]):
                values = gameserver.read_kvp('ShroudQuest01')
                amp = mock.AsyncMock()
                password = await gameserver._get_amp_server_password(amp, values)

        self.assertTrue(values['enshrouded_default_role_password'])
        self.assertNotIn('server_password', values)
        self.assertIsNone(password)
        amp.get_config.assert_not_awaited()


class _FakeResponse:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self, **_kwargs):
        return self._payload


class _FakeSession:
    response = None
    last_request = None

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def get(self, url, **kwargs):
        type(self).last_request = ('GET', url, kwargs)
        return type(self).response

    def post(self, url, **kwargs):
        type(self).last_request = ('POST', url, kwargs)
        return type(self).response


class QuestControlClientTests(unittest.IsolatedAsyncioTestCase):
    @mock.patch.object(gameserver.aiohttp, 'ClientSession', _FakeSession)
    async def test_status_is_loaded_from_tenant_bound_quest_control_endpoint(self):
        _FakeSession.response = _FakeResponse(200, {
            'ok': True,
            'status': {
                'state': 'Running',
                'is_running': True,
                'player_count': 2,
                'server_password': 'public-password',
            },
        })
        with mock.patch.object(gameserver, 'QUEST_CONTROL_BOT_SECRET', 'test-secret'):
            status = await gameserver._get_server_status(
                'CH-HeroesofPalpagos02',
                fluxer_guild_id='1478978400033030757',
            )

        self.assertTrue(status['is_running'])
        self.assertEqual(status['player_count'], 2)
        self.assertEqual(status['server_password'], 'public-password')
        method, url, kwargs = _FakeSession.last_request
        self.assertEqual(method, 'GET')
        self.assertTrue(url.endswith('/api/internal/quest-control/server/'))
        self.assertEqual(kwargs['params']['guild_id'], '1478978400033030757')
        self.assertEqual(kwargs['headers']['X-Bot-Secret'], 'test-secret')
        self.assertFalse(kwargs['allow_redirects'])

    @mock.patch.object(gameserver.aiohttp, 'ClientSession', _FakeSession)
    async def test_lifecycle_action_is_sent_through_quest_control(self):
        _FakeSession.response = _FakeResponse(200, {'ok': True})
        with mock.patch.object(gameserver, 'QUEST_CONTROL_BOT_SECRET', 'test-secret'):
            ok, error = await gameserver._quest_control_server_action(
                'CH-HeroesofPalpagos02',
                '1478978400033030757',
                'restart',
            )

        self.assertTrue(ok)
        self.assertIsNone(error)
        method, _url, kwargs = _FakeSession.last_request
        self.assertEqual(method, 'POST')
        self.assertEqual(kwargs['json']['guild_id'], '1478978400033030757')
        self.assertEqual(kwargs['json']['action'], 'restart')
        self.assertFalse(kwargs['allow_redirects'])

    @mock.patch.object(gameserver.aiohttp, 'ClientSession', _FakeSession)
    async def test_malformed_status_payload_stays_unavailable(self):
        _FakeSession.response = _FakeResponse(200, [])
        with mock.patch.object(gameserver, 'QUEST_CONTROL_BOT_SECRET', 'test-secret'):
            status = await gameserver._get_server_status('fixture-instance', fluxer_guild_id='100')
        self.assertFalse(status['is_running'])
        self.assertEqual(status['state'], 'Unknown')

    @mock.patch.object(gameserver.aiohttp, 'ClientSession', _FakeSession)
    async def test_malformed_action_payload_is_not_success(self):
        _FakeSession.response = _FakeResponse(200, [])
        with mock.patch.object(gameserver, 'QUEST_CONTROL_BOT_SECRET', 'test-secret'):
            ok, error = await gameserver._quest_control_server_action('fixture-instance', '100', 'restart')
        self.assertFalse(ok)
        self.assertIn('invalid response', error)


if __name__ == '__main__':
    unittest.main()
