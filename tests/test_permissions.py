"""Full Disk Access guidance chooses the right requester without granting access."""

import contextlib
import io
import json
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from _support import Base
from memcal import cli, db, permissions, schedule, web


class TestPermissionRequesterSelection(Base):
    def test_app_access_is_sufficient_without_host_full_disk_access(self):
        with mock.patch.object(schedule, '_is_macos', return_value=True), \
             mock.patch('memcal.bridge.available', return_value=True), \
             mock.patch('memcal.bridge.check', return_value=(True, 'app read verified')), \
             mock.patch.object(schedule, 'read_source_access', side_effect=AssertionError('host read')):
            state = permissions.status(self.cfg)
        self.assertTrue(state['ready'])
        self.assertIsNone(state['target'])
        self.assertNotIn('host_app', state)

    def test_denied_bridge_directs_approval_to_memcal_only(self):
        with mock.patch.object(schedule, '_is_macos', return_value=True), \
             mock.patch('memcal.bridge.available', return_value=True), \
             mock.patch('memcal.bridge.check', return_value=(False, 'app denied')):
            state = permissions.status(self.cfg)
        self.assertFalse(state['ready'])
        self.assertEqual(permissions.guide(self.cfg, state)['app'], str(schedule.app_path(self.cfg)))

    def test_other_platforms_do_not_probe_or_open_mac_settings(self):
        with mock.patch.object(schedule, '_is_macos', return_value=False), \
             mock.patch.object(schedule, 'source_access_health') as probe:
            plan = permissions.setup(self.cfg, runner=mock.Mock(side_effect=AssertionError('opened UI')))
        self.assertFalse(plan['supported'])
        probe.assert_not_called()


class TestPermissionHelperOpensOnlyReviewableSettings(Base):
    def setUp(self):
        super().setUp()
        self.app = schedule.app_path(self.cfg)
        self.app.mkdir()
        self.state = {'supported': True, 'ready': False, 'target': 'memcal',
                      'memcal_app': str(schedule.app_path(self.cfg)), 'checks': {},
                      'interactive': {'imessage': {'ok': False}},
                      'scheduled': {'ok': False, 'status': 'fail', 'detail': 'denied'},
                      'detail': 'App bridge needs permission.'}
        self.enterContext(mock.patch.object(permissions, 'status', return_value=self.state))

    def test_opens_settings_and_reveals_exact_app_without_mutating_a_grant(self):
        self.enterContext(mock.patch.object(schedule, 'bundle_health', return_value=('ok', 'current')))
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
        result = permissions.setup(self.cfg, runner=runner)
        self.assertTrue(result['opened'])
        self.assertFalse(result['ready'])
        self.assertEqual([call.args[0] for call in runner.call_args_list],
                         [['open', permissions.SETTINGS_URL], ['open', '-R', str(self.app)]])
        self.assertTrue(any('Approve' in step for step in result['steps']))

    def test_no_open_is_a_read_only_plan_and_never_builds_a_bundle(self):
        self.state.update(target='memcal', scheduled={'ok': False})
        runner = mock.Mock(side_effect=AssertionError('opened UI'))
        with mock.patch.object(schedule, 'build_app_bundle') as build:
            result = permissions.setup(self.cfg, open_ui=False, runner=runner)
        self.assertFalse(result['opened'])
        self.assertEqual(result['app'], str(schedule.app_path(self.cfg)))
        build.assert_not_called()

    def test_working_target_is_not_reprompted(self):
        self.state.update(ready=True)
        runner = mock.Mock(side_effect=AssertionError('already authorized'))
        result = permissions.setup(self.cfg, target='memcal', runner=runner)
        self.assertFalse(result['opened'])
        self.assertTrue(result['target_ready'])
        self.assertFalse(result['steps'])

    def test_failed_deep_link_falls_back_to_settings_and_keeps_manual_steps(self):
        self.enterContext(mock.patch.object(schedule, 'bundle_health', return_value=('ok', 'current')))
        runner = mock.Mock(side_effect=[subprocess.CompletedProcess([], 1, '', ''),
                                       subprocess.CompletedProcess([], 0, '', ''),
                                       subprocess.CompletedProcess([], 1, '', '')])
        result = permissions.setup(self.cfg, runner=runner)
        self.assertTrue(result['opened'])
        self.assertEqual(runner.call_args_list[1].args[0], ['open', '-a', 'System Settings'])
        self.assertTrue(result['notes'])

    def test_invalid_target_cannot_open_an_arbitrary_path(self):
        with self.assertRaises(ValueError):
            permissions.setup(self.cfg, target='/Applications/Other.app')

    def test_unavailable_bundle_is_prepared_only_during_explicit_setup(self):
        self.state.update(target='memcal', scheduled={'ok': False})
        with mock.patch.object(schedule, 'bundle_health', side_effect=[('absent', ''), ('absent', '')]), \
             mock.patch.object(schedule, 'build_app_bundle', return_value=['no compiler']) as build:
            result = permissions.setup(self.cfg, runner=mock.Mock(side_effect=AssertionError('opened UI')))
        build.assert_called_once_with(self.cfg)
        self.assertIn('error', result)


class TestPermissionCliAndHttpKeepTrustBoundaries(Base):
    def test_cli_status_does_not_create_or_migrate_the_store_and_emits_clean_json(self):
        home = self.cfg.home / 'not-created'
        state = {'supported': False, 'ready': True, 'detail': 'macOS only', 'target': None}
        output = io.StringIO()
        with mock.patch.object(permissions, 'status', return_value=state), \
             mock.patch.object(cli, 'open_ctx', side_effect=AssertionError('opened store')), \
             mock.patch.object(db, 'open_db', side_effect=AssertionError('migrated')), \
             contextlib.redirect_stdout(output):
            rc = cli.main(['--home', str(home), 'permissions', 'status', '--json'])
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(output.getvalue()), state)
        self.assertFalse(home.exists())

    def test_web_status_is_passive_and_same_origin_is_required_to_open_settings(self):
        handler = object.__new__(web.Handler)
        handler.path = '/api/permissions'
        handler.headers = {'Host': '127.0.0.1:8765', 'Origin': 'https://attacker.example'}
        handler.origin = 'http://127.0.0.1:8765'
        handler.csrf_token = 'server-token'
        handler.rfile = io.BytesIO(b'{}')
        handler._send = mock.Mock()
        handler._conn = mock.Mock(side_effect=AssertionError('store opened'))
        with mock.patch.object(permissions, 'setup') as setup:
            handler.do_POST()
        setup.assert_not_called()
        handler._send.assert_called_once_with({'error': 'cross-site requests are not allowed'}, 403)

    def test_permission_routes_work_even_when_the_memcal_database_cannot_open(self):
        handler = object.__new__(web.Handler)
        handler.path = '/api/permissions'
        handler.cfg = self.cfg
        handler.origin = 'http://127.0.0.1:8765'
        handler.csrf_token = 'server-token'
        handler.headers = {'Host': '127.0.0.1:8765', 'Origin': handler.origin,
                           'Cookie': 'memcal_csrf=server-token',
                           'Content-Type': 'application/json', 'Content-Length': '2'}
        handler.rfile = io.BytesIO(b'{}')
        handler._send = mock.Mock()
        handler._conn = mock.Mock(side_effect=AssertionError('store opened'))
        state = {'supported': False, 'ready': True, 'target': None, 'detail': 'macOS only'}
        with mock.patch.object(permissions, 'status', return_value=state), \
             mock.patch.object(permissions, 'setup', return_value=state) as setup:
            handler.do_GET()
            handler.do_POST()
        setup.assert_called_once_with(self.cfg, target='auto')
        handler._conn.assert_not_called()



if __name__ == '__main__':
    unittest.main()
