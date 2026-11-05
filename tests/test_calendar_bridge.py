"""Interactive Calendar work must use the launchd app's consent identity."""

import json
import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest import mock

from memcal import bridge, db, events, schedule
from memcal.sources import ical
from tests.test_bridge import BridgeBase


class TestCalendarAppBridge(BridgeBase):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(ical, '_have_osascript', return_value=True))
        self.enterContext(mock.patch.object(schedule, 'launchd_memcal_call', side_effect=self.loopback))
        self.commands = []

    def app_transport(self, argv, **kwargs):
        self.assertEqual(os.environ.get('MEMCAL_HELPER_CONTEXT'), '1')
        self.assertEqual(os.environ.get('MEMCAL_BRIDGE_WORKER'), '1')
        self.commands.append(argv)
        payload = {'granted': True, 'answered': True, 'status': 4,
                   'uid': 'synthetic-event', 'calendar': 'Test calendar',
                   'calendar_uid': 'synthetic-calendar', 'removed': True}
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), '')

    def test_shell_bundle_marker_does_not_skip_calendar_bridge(self):
        # Direct app re-exec still has the terminal as its responsible process.
        with mock.patch.dict(os.environ, {'MEMCAL_APP': '1'}), \
             mock.patch('subprocess.run', side_effect=self.app_transport):
            ok, _ = ical.permission_status(cfg=self.cfg)
        self.assertTrue(ok)
        self.assertEqual(self.commands[0][4], ical.PROBE_JXA)
        self.assertFalse(list(self.cfg.home.glob('bridge-*')))

    def test_worker_rejects_a_bundle_started_directly_from_the_terminal(self):
        with mock.patch.dict(os.environ, {'MEMCAL_APP': '1', 'MEMCAL_HELPER_CONTEXT': ''}):
            with self.assertRaisesRegex(bridge.BridgeError, 'launchd'):
                bridge.worker(self.cfg, 'unused', 'unused')

    def test_calendar_and_reminders_setup_and_writes_share_app_identity(self):
        todo = SimpleNamespace(text='Bring notes', remind_at='2026-11-06T10:00:00',
                               reminder_uid='synthetic-reminder', event_title='')
        self.cfg.publish_reminders = 'Test reminders'
        with mock.patch('subprocess.run', side_effect=self.app_transport):
            self.assertTrue(ical.request_calendar_access(cfg=self.cfg)[0])
            self.assertTrue(ical.request_reminders_access(cfg=self.cfg)[0])
            ical.publish_reminder(self.cfg, todo)
            ical.retract_reminder(self.cfg, todo)
        self.assertEqual([cmd[5] for cmd in self.commands], ['request', 'request', 'put', 'drop'])

    def test_dream_publishing_and_retraction_keep_database_bookkeeping_in_caller(self):
        self.cfg.publish_calendar = 'Test calendar'
        event, _ = events.upsert(self.conn, {'title': 'Library visit', 'date': self.d(1),
                                           'status': 'confirmed', 'kind': 'commitment'})
        with mock.patch('subprocess.run', side_effect=self.app_transport), \
             mock.patch.object(ical, '_have_osascript', return_value=True):
            ical.publish_pending(self.conn, self.cfg)
            self.assertEqual(self.commands[-1][4], ical.PUBLISH_JXA)
            self.assertEqual(self.conn.execute('SELECT published FROM calendar_items').fetchone()[0], 1)
            # Replay does not publish the same version a second time.
            before = len(self.commands)
            ical.publish_pending(self.conn, self.cfg)
            self.assertEqual(len(self.commands), before)
            self.conn.execute("UPDATE events SET status='cancelled' WHERE key=?", (event.key,))
            ical.retract_unpublishable(self.conn, self.cfg, dry_run=False)
        self.assertEqual(self.commands[-1][4], ical.RETRACT_JXA)
        self.assertEqual(self.conn.execute('SELECT published FROM calendar_items').fetchone()[0], 0)

    def test_account_verbs_and_recurring_event_transports_use_bridge(self):
        with mock.patch('subprocess.run', side_effect=self.app_transport):
            for verb, args in [('where', ['Test calendar']), ('ensure', ['Test calendar']),
                               ('migrate', ['Test calendar', 'plan']),
                               ('repeat', ['Test calendar', '{}']),
                               ('unrepeat', ['Test calendar', '{"uid":"synthetic-event"}'])]:
                ical._account_call(verb, *args, cfg=self.cfg)
        self.assertEqual(len(self.commands), 5)
        self.assertTrue(all(cmd[4] == ical.ACCOUNT_JXA for cmd in self.commands))

    def test_snapshot_keeps_partial_read_metadata_and_reports_app_progress(self):
        real_snapshot = ical._calendar_snapshot
        progress = []
        captured = []

        def inside_app(start, end, **kwargs):
            self.assertTrue(bridge.in_helper())
            captured.append((start, end))
            kwargs['progress']('5/10 calendar events', done=5, total=10, phase='reading')
            return ical.Snapshot([{'uid': 'synthetic-event'}], ('Unavailable subscription',))

        def handoff(cfg, **kwargs):
            payload = self.loopback(cfg, **kwargs)
            kwargs['on_poll']()
            return payload

        with mock.patch.object(ical, '_calendar_snapshot', side_effect=inside_app), \
             mock.patch.object(schedule, 'launchd_memcal_call', side_effect=handoff):
            result = real_snapshot('2026-11-01', '2026-12-01', cfg=self.cfg,
                                   progress=lambda note, **fields: progress.append((note, fields)))
        self.assertEqual(captured, [('2026-11-01', '2026-12-01')])
        self.assertEqual(result.items, [{'uid': 'synthetic-event'}])
        self.assertFalse(result.complete)
        self.assertEqual(result.unreadable, ('Unavailable subscription',))
        self.assertEqual(progress[0][1]['done'], 5)

    def test_bridge_failure_never_runs_osascript_from_the_host(self):
        self.cfg.publish_calendar = 'Test calendar'
        with mock.patch.object(schedule, 'launchd_memcal_call', return_value=(None, 'memcal')), \
             mock.patch('subprocess.run', side_effect=AssertionError('host osascript called')):
            self.assertFalse(ical.permission_status(cfg=self.cfg)[0])
            with self.assertRaises(ical.PublishError):
                ical._write_event('Test calendar', {}, cfg=self.cfg)
            with self.assertRaises(ical.CalendarAccountError):
                ical._account_call('where', 'Test calendar', cfg=self.cfg)
            with self.assertRaises(ical.ReminderError):
                ical._reminder_call('status', '', cfg=self.cfg)
            with self.assertRaises(ical.SourceError):
                ical._calendar_snapshot('2026-11-01', '2026-12-01', cfg=self.cfg)

    def test_missing_app_returns_setup_guidance_without_host_prompt(self):
        schedule.app_executable(self.cfg).unlink()
        with mock.patch('subprocess.run', side_effect=AssertionError('host osascript called')):
            ok, detail = ical.permission_status(cfg=self.cfg)
        self.assertFalse(ok)
        self.assertIn('memcal permissions setup', detail)

    def test_transport_does_not_commit_an_active_caller_transaction(self):
        self.conn.execute("INSERT INTO meta(key,value) VALUES('pending','work')")
        with mock.patch('subprocess.run', side_effect=self.app_transport):
            ical._write_event('Test calendar', {}, cfg=self.cfg)
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()

    def test_arbitrary_scripts_and_invalid_calendar_arguments_are_rejected(self):
        with mock.patch.object(schedule, 'launchd_memcal_call') as send:
            for kind, args in [('shell', ['id']), ('account', ['eval', 'bad']),
                               ('account', ['migrate', 'Test calendar', 'unknown']),
                               ('snapshot', ['bad', 'dates']), ('publish', ['Test calendar', '[]']),
                               ([], [])]:
                with self.assertRaises(OSError):
                    bridge.calendar_call(self.cfg, kind, args)
            with self.assertRaises(OSError):
                bridge.calendar_runner(self.cfg, ical._REAL_TRANSPORT[0])(
                    ['osascript', '-l', 'JavaScript', '-e', 'arbitrary script'])
        send.assert_not_called()

    def test_injected_transports_remain_local_and_do_not_need_an_app(self):
        schedule.app_executable(self.cfg).unlink()
        fake = mock.Mock(return_value=subprocess.CompletedProcess([], 0, '{}', ''))
        self.assertTrue(ical.permission_status(cfg=self.cfg, runner=fake)[0])
        self.assertEqual(fake.call_count, 1)

    def test_calendar_bridge_does_not_require_or_open_a_memcal_store(self):
        self.conn.close()
        self.cfg.db_path.unlink()
        # Base teardown can safely close an already-closed sqlite connection.
        with mock.patch('subprocess.run', side_effect=self.app_transport), \
             mock.patch.object(db, 'connect', side_effect=AssertionError('store opened')):
            self.assertTrue(ical.permission_status(cfg=self.cfg)[0])


if __name__ == '__main__':
    unittest.main()
