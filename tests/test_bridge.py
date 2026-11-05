"""App-owned source reads over bounded, private local IPC."""

import json
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _support import Base
from memcal import archive, bridge, db, identity, schedule, sources
from memcal.sources import imessage, whatsapp


class BridgeBase(Base):
    def setUp(self):
        super().setUp()
        db.set_today('2026-11-05T12:00:00-05:00')
        app = schedule.app_executable(self.cfg)
        app.parent.mkdir(parents=True)
        app.write_text('test app')
        app.chmod(0o700)
        self.enterContext(mock.patch.object(schedule, '_is_macos', return_value=True))
        self.enterContext(mock.patch.dict(os.environ, {'MEMCAL_BRIDGE_WORKER': '', 'MEMCAL_HELPER_CONTEXT': ''}))

    def loopback(self, cfg, **kwargs):
        directory = kwargs['work_dir']
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        request = Path(kwargs['memcal_argv'][-1])
        self.assertEqual(stat.S_IMODE(request.stat().st_mode), 0o600)
        result = directory / 'reply.json'
        with mock.patch.dict(os.environ, {'MEMCAL_APP': '1', 'MEMCAL_HELPER_CONTEXT': '1'}):
            bridge.worker(cfg, request, result)
        self.assertEqual(stat.S_IMODE(result.stat().st_mode), 0o600)
        return json.loads(result.read_text()), 'memcal'


class TestAppBridgeCollectionLifecycle(BridgeBase):
    def chat(self):
        from tests.test_collection_jobs import TestASourceThatFilledItsPageSaysSo
        return TestASourceThatFilledItsPageSaysSo._chat_db(self, 5)

    def test_app_collection_keeps_cursor_identity_totals_and_replay_safety(self):
        path = self.chat()
        cid = archive.open_collection(self.conn, mode='test')
        with mock.patch.object(schedule, 'launchd_memcal_call', side_effect=self.loopback), \
             mock.patch.object(imessage, 'CHAT_DB', path):
            report = sources.catch_up(imessage.IMessageSource(), self.conn, self.cfg,
                                      limit=2, rounds=10, collection_id=cid)
            self.assertIsNone(report.error)
            self.assertEqual((report.read, report.archived, report.more), (5, 5, False))
            self.assertEqual(self.conn.execute('SELECT read FROM collection_sources WHERE collection_id=?', (cid,)).fetchone()[0], 5)
            cid2 = archive.open_collection(self.conn, mode='test')
            again = sources.catch_up(imessage.IMessageSource(), self.conn, self.cfg,
                                     limit=2, rounds=10, collection_id=cid2)
        self.assertIsNone(again.error)
        self.assertEqual(again.archived, 0)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM archive').fetchone()[0], 5)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM archive WHERE collection_id=?', (cid,)).fetchone()[0], 5)
        self.assertEqual(db.get_meta(self.conn, 'imessage.rowid'), '5')
        self.assertFalse(list(self.cfg.home.glob('bridge-*')))

    def test_local_source_checks_run_inside_the_app_not_in_the_caller(self):
        def only_in_app():
            self.assertEqual(os.environ.get('MEMCAL_HELPER_CONTEXT'), '1')
            return True
        with mock.patch.object(schedule, 'launchd_memcal_call', side_effect=self.loopback), \
             mock.patch.object(imessage, 'available', side_effect=only_in_app):
            usable, detail = imessage.IMessageSource().check(self.cfg)
        self.assertTrue(usable)
        self.assertIn('memcal.app bridge', detail)

    def test_whatsapp_collection_retains_collection_id_and_owns_one_desktop_session(self):
        from tests.test_core import TestWhatsApp
        self.GROUP_JID, self.MUM_JID = TestWhatsApp.GROUP_JID, TestWhatsApp.MUM_JID
        stamp = (db.now_dt() - whatsapp.APPLE_EPOCH).total_seconds()
        path = TestWhatsApp._store(self, [(n, 'Dinner tomorrow?', stamp, 0, self.MUM_JID, 0, 2, None)
                                          for n in range(1, 4)])
        self.cfg.env['WHATSAPP_DB'] = path
        cid = archive.open_collection(self.conn, mode='test')
        from contextlib import contextmanager
        entered = []
        @contextmanager
        def session(*_args, **_kwargs):
            self.assertEqual(os.environ.get('MEMCAL_HELPER_CONTEXT'), '1')
            entered.append('open')
            try: yield
            finally: entered.append('close')
        with mock.patch.object(schedule, 'launchd_memcal_call', side_effect=self.loopback), \
             mock.patch.object(whatsapp, 'desktop_session', side_effect=session):
            result = sources.catch_up(whatsapp.WhatsAppSource(), self.conn, self.cfg,
                                      limit=2, rounds=10, collection_id=cid)
        self.assertIsNone(result.error)
        self.assertEqual(result.archived, 3)
        self.assertEqual(entered, ['open', 'close'])
        self.assertEqual(self.conn.execute('SELECT count(*) FROM archive WHERE collection_id=?', (cid,)).fetchone()[0], 3)

    def test_contacts_refresh_is_owned_by_the_app(self):
        def import_contacts(conn):
            self.assertEqual(os.environ.get('MEMCAL_HELPER_CONTEXT'), '1')
            identity.link(conn, '+12025550123', 'Avery', source='contacts')
            return 1, 'imported'
        with mock.patch.object(schedule, 'launchd_memcal_call', side_effect=self.loopback), \
             mock.patch.object(identity, 'import_contacts', side_effect=import_contacts):
            count, _ = identity.refresh_contacts(self.conn, cfg=self.cfg)
        self.assertEqual(count, 1)
        self.assertEqual(identity.resolve(self.conn, '+12025550123'), 'Avery')


class TestBridgeBoundaryFailures(BridgeBase):
    def test_busy_transactions_are_not_committed_or_sent_to_another_process(self):
        self.conn.execute("INSERT INTO meta(key,value) VALUES('pending','work')")
        with mock.patch.object(schedule, 'launchd_memcal_call') as send:
            with self.assertRaisesRegex(bridge.BridgeError, 'transaction'):
                bridge.call(self.cfg, 'contacts', conn=self.conn)
        send.assert_not_called()
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()

    def test_worker_never_recursively_delegates(self):
        with mock.patch.dict(os.environ, {'MEMCAL_HELPER_CONTEXT': '1'}):
            self.assertFalse(bridge.available(self.cfg))

    def test_timeout_and_wrong_request_id_fail_without_falling_back_to_host_reads(self):
        for response in (None, {'id': 'wrong', 'result': {}}):
            with mock.patch.object(schedule, 'launchd_memcal_call', return_value=(response, 'memcal')):
                with self.assertRaises(bridge.BridgeError):
                    bridge.call(self.cfg, 'check', source='imessage')
            self.assertFalse(list(self.cfg.home.glob('bridge-*')))

    def test_commands_sql_and_source_paths_are_not_bridge_operations(self):
        with mock.patch.object(schedule, 'launchd_memcal_call') as send:
            for args in ({'operation': 'shell', 'source': 'imessage'},
                         {'operation': 'check', 'source': '/etc/passwd'},
                         {'operation': 'ingest', 'source': 'whatsapp', 'limit': -1}):
                with self.assertRaises(bridge.BridgeError):
                    bridge.call(self.cfg, **args)
        send.assert_not_called()

    def test_worker_rejects_nonprivate_requests_before_opening_the_store(self):
        with tempfile.TemporaryDirectory(prefix='bridge-', dir=self.cfg.home) as temporary:
            path = Path(temporary) / 'request.json'
            path.write_text('{}')
            path.chmod(0o644)
            with mock.patch.dict(os.environ, {'MEMCAL_APP': '1'}), \
                 mock.patch.object(db, 'connect', side_effect=AssertionError('store opened')):
                with self.assertRaisesRegex(bridge.BridgeError, 'private'):
                    bridge.worker(self.cfg, path, Path(temporary) / 'reply.json')

    def test_result_files_cannot_escape_the_private_request_directory(self):
        with tempfile.TemporaryDirectory(prefix='bridge-', dir=self.cfg.home) as temporary:
            request = Path(temporary) / 'request.json'
            request.write_text('{}')
            request.chmod(0o600)
            outside = self.cfg.home / 'outside.json'
            with mock.patch.dict(os.environ, {'MEMCAL_APP': '1'}):
                with self.assertRaisesRegex(bridge.BridgeError, 'private store request directory'):
                    bridge.worker(self.cfg, request, outside)
            self.assertFalse(outside.exists())

    def test_a_connection_to_another_store_cannot_be_used_for_app_writes(self):
        other = sqlite3.connect(self.cfg.home / 'other.db')
        try:
            with self.assertRaisesRegex(bridge.BridgeError, 'same Memcal store'):
                bridge.call(self.cfg, 'contacts', conn=other)
        finally:
            other.close()

    def test_worker_requires_the_memcal_app_context(self):
        with mock.patch.dict(os.environ, {'MEMCAL_APP': ''}):
            with self.assertRaisesRegex(bridge.BridgeError, 'memcal.app'):
                bridge.worker(self.cfg, 'unused', 'unused')


if __name__ == '__main__':
    unittest.main()
