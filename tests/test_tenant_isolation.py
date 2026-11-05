import unittest
from contextlib import contextmanager
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from cogs.core import _action_resources_belong_to_guild
from cogs.lfg import (
    _broadcast_destination_belongs_to_guild,
    _delete_web_lfg_group,
)


class TenantIsolationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            'sqlite://',
            connect_args={'check_same_thread': False},
            poolclass=StaticPool,
        )
        with self.engine.begin() as connection:
            connection.execute(text(
                'CREATE TABLE web_fluxer_guild_channels '
                '(guild_id TEXT, channel_id TEXT)'
            ))
            connection.execute(text(
                'CREATE TABLE web_fluxer_guild_roles '
                '(guild_id TEXT, role_id TEXT)'
            ))
            connection.execute(text(
                'CREATE TABLE web_fluxer_lfg_groups '
                '(id INTEGER PRIMARY KEY, guild_id TEXT, '
                'creator_web_user_id INTEGER, status TEXT)'
            ))
            connection.execute(text(
                "INSERT INTO web_fluxer_guild_channels VALUES "
                "('100', '101'), ('200', '201')"
            ))
            connection.execute(text(
                "INSERT INTO web_fluxer_guild_roles VALUES "
                "('100', '102'), ('200', '202')"
            ))
            connection.execute(text(
                "INSERT INTO web_fluxer_lfg_groups VALUES "
                "(1, '100', 7, 'open'), (2, '200', 8, 'open')"
            ))

    def tearDown(self):
        self.engine.dispose()

    @contextmanager
    def session(self):
        db = Session(self.engine)
        try:
            yield db
        finally:
            db.close()

    def test_action_resources_must_all_belong_to_polled_guild(self):
        with patch('cogs.core.db_session_scope', self.session):
            self.assertTrue(_action_resources_belong_to_guild(
                '100', channel_id='101', role_ids={'102'},
            ))
            self.assertFalse(_action_resources_belong_to_guild(
                '100', channel_id='201', role_ids={'102'},
            ))
            self.assertFalse(_action_resources_belong_to_guild(
                '100', channel_id='101', role_ids={'202'},
            ))

    def test_broadcast_channel_must_belong_to_queued_guild(self):
        with self.session() as db:
            self.assertTrue(_broadcast_destination_belongs_to_guild(
                db, '100', '101',
            ))
            self.assertFalse(_broadcast_destination_belongs_to_guild(
                db, '100', '201',
            ))

    def test_guild_admin_cannot_cancel_another_guild_group(self):
        with patch('cogs.lfg.db_session_scope', self.session):
            success, error = _delete_web_lfg_group(
                group_id=2, web_user_id=7, guild_id='100', force=True,
            )
        self.assertFalse(success)
        self.assertIn('No group found', error)
        with self.engine.connect() as connection:
            status = connection.execute(text(
                'SELECT status FROM web_fluxer_lfg_groups WHERE id=2'
            )).scalar()
        self.assertEqual(status, 'open')

    def test_guild_admin_can_cancel_group_in_same_guild(self):
        with patch('cogs.lfg.db_session_scope', self.session):
            success, error = _delete_web_lfg_group(
                group_id=1, web_user_id=999, guild_id='100', force=True,
            )
        self.assertTrue(success, error)
        with self.engine.connect() as connection:
            status = connection.execute(text(
                'SELECT status FROM web_fluxer_lfg_groups WHERE id=1'
            )).scalar()
        self.assertEqual(status, 'cancelled')


if __name__ == '__main__':
    unittest.main()
