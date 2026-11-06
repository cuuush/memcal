"""One app-owned Calendar grant serves interactive and scheduled commands."""

import contextlib
import io
import unittest
from unittest import mock

from memcal import cli, db, schedule
from memcal.sources import ical
from tests.test_bridge import BridgeBase


class TestSharedCalendarPermissionStatus(BridgeBase):
    def command(self, *arguments):
        output = io.StringIO()
        with mock.patch.object(cli, 'open_ctx', return_value=(self.cfg, self.conn)), \
             contextlib.redirect_stdout(output):
            code = cli.main(['--home', str(self.cfg.home), 'ical', *arguments])
        self.conn = db.open_db(self.cfg.db_path)
        return code, output.getvalue()

    def test_passive_status_uses_one_app_check_without_reading_calendar_or_schedule(self):
        ical.record_automation_check(self.conn, True)
        with mock.patch.object(ical, 'permission_status', side_effect=AssertionError('Calendar read')), \
             mock.patch.object(schedule, 'status', side_effect=AssertionError('schedule queried')):
            code, output = self.command('status')
        self.assertEqual(code, 0)
        self.assertIn('memcal.app   Automation last verified', output)
        self.assertIn('shared by interactive commands and scheduled work', output)
        self.assertNotIn('interactive  not verified', output)

    def test_legacy_nightly_check_remains_usable_but_terminal_grant_does_not_verify_app(self):
        db.set_meta(self.conn, 'ical.permission.interactive', db.now())
        self.assertIsNone(ical.automation_verified(self.conn))
        db.set_meta(self.conn, 'ical.permission.nightly', db.now())
        self.assertIsNotNone(ical.automation_verified(self.conn))

    def test_setup_uses_app_helper_even_when_scheduling_is_disabled(self):
        schedule.disabled_path(self.cfg).touch()
        with mock.patch.object(schedule, 'calendar_permission_probe', return_value=(True, 'app granted')) as probe, \
             mock.patch.object(ical, 'permission_status', side_effect=AssertionError('caller read Calendar')), \
             mock.patch.object(schedule, 'set_enabled', side_effect=AssertionError('schedule changed')):
            code, _ = self.command('setup', '--yes')
        self.assertEqual(code, 0)
        probe.assert_called_once_with(self.cfg)
        self.assertIsNotNone(db.get_meta(self.conn, 'ical.permission.app'))
        self.assertTrue(schedule.disabled_path(self.cfg).exists())

    def test_failed_setup_clears_shared_and_legacy_automation_success(self):
        for key in ('app', 'nightly', 'interactive'):
            db.set_meta(self.conn, f'ical.permission.{key}', db.now())
        with mock.patch.object(schedule, 'calendar_permission_probe', return_value=(False, 'app denied')):
            code, _ = self.command('setup', '--yes')
        self.assertEqual(code, 1)
        self.assertIsNone(ical.automation_verified(self.conn))
        self.assertIsNone(db.get_meta(self.conn, 'ical.permission.interactive'))

    def test_eventkit_probe_cannot_claim_automation_was_checked(self):
        with mock.patch.object(ical, 'request_calendar_access', return_value=(True, 'EventKit granted')):
            code, _ = self.command('probe', '--ek', 'request')
        self.assertEqual(code, 0)
        self.assertIsNotNone(db.get_meta(self.conn, 'ical.eventkit.verified'))
        self.assertIsNone(ical.automation_verified(self.conn))

    def test_failed_automation_probe_preserves_independent_eventkit_grant(self):
        ical.record_automation_check(self.conn, True)
        db.set_meta(self.conn, 'ical.eventkit.verified', db.now())
        with mock.patch.object(ical, 'permission_status', return_value=(False, 'app denied')):
            code, _ = self.command('probe')
        self.assertEqual(code, 1)
        self.assertIsNone(ical.automation_verified(self.conn))
        self.assertIsNotNone(db.get_meta(self.conn, 'ical.eventkit.verified'))

    def test_successful_automation_probe_records_shared_app_check(self):
        with mock.patch.object(ical, 'permission_status', return_value=(True, 'app granted')):
            code, _ = self.command('probe')
        self.assertEqual(code, 0)
        self.assertIsNotNone(db.get_meta(self.conn, 'ical.permission.app'))


if __name__ == '__main__':
    unittest.main()
