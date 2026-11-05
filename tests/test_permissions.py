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
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(schedule, '_is_macos', return_value=True))

    def inspect(self, interactive, scheduled):
        with mock.patch.object(schedule, 'read_source_access', return_value={'checks': interactive}), \
             mock.patch.object(schedule, 'source_access_health', return_value=scheduled), \
             mock.patch.object(permissions, 'host_app', return_value=Path('/Applications/ExampleHost.app')):
            return permissions.status(self.cfg)

    def test_working_scheduled_grant_directs_only_interactive_denial_to_the_host(self):
        state = self.inspect({'imessage': {'ok': False, 'detail': 'denied'}}, ('ok', 'verified'))
        self.assertEqual(state['target'], 'host')
        self.assertFalse(state['ready'])
        plan = permissions.guide(self.cfg, state)
        self.assertEqual(plan['app'], '/Applications/ExampleHost.app')
        self.assertTrue(any('Quit and reopen' in step for step in plan['steps']))

    def test_unverified_scheduled_grant_selects_memcal_even_when_host_can_read(self):
        state = self.inspect({'imessage': {'ok': True, 'detail': 'readable'}}, ('fail', 'denied'))
        self.assertEqual(state['target'], 'memcal')
        self.assertEqual(permissions.guide(self.cfg, state)['app'], str(schedule.app_path(self.cfg)))

    def test_readable_sources_are_verified_and_unknown_sources_are_not(self):
        self.assertTrue(self.inspect({'whatsapp': {'ok': True}}, ('ok', 'verified'))['ready'])
        self.assertFalse(self.inspect({'whatsapp': {'ok': None}}, ('warn', 'missing database'))['ready'])

    def test_other_platforms_never_probe_mac_sources_or_launch_apps(self):
        with mock.patch.object(schedule, '_is_macos', return_value=False), \
             mock.patch.object(schedule, 'read_source_access') as read, \
             mock.patch.object(schedule, 'source_access_health') as probe, \
             mock.patch.object(permissions, 'host_app') as host:
            plan = permissions.setup(self.cfg, runner=mock.Mock(side_effect=AssertionError('opened UI')))
        self.assertFalse(plan['supported'])
        read.assert_not_called()
        probe.assert_not_called()
        host.assert_not_called()


class TestHostApplicationDiscovery(Base):
    def test_walks_past_python_and_memcal_without_reading_command_arguments(self):
        runner = mock.Mock(side_effect=[
            subprocess.CompletedProcess([], 0, '30 /Library/Python.app/Contents/MacOS/Python\n'),
            subprocess.CompletedProcess([], 0, '20 ' + str(schedule.app_executable(self.cfg)) + '\n'),
            subprocess.CompletedProcess([], 0, '1 /Applications/Example Host.app/Contents/MacOS/Example\n'),
        ])
        self.assertEqual(permissions.host_app(self.cfg, pid=40, runner=runner),
                         Path('/Applications/Example Host.app'))
        for call in runner.call_args_list:
            self.assertEqual(call.args[0][-4:], ['-o', 'ppid=', '-o', 'comm='])

    def test_cycles_and_failed_process_lookups_return_unknown(self):
        for result in (subprocess.CompletedProcess([], 0, '40 /bin/sh'),
                       subprocess.CompletedProcess([], 1, ''),
                       subprocess.TimeoutExpired('ps', 2)):
            runner = mock.Mock(side_effect=result if isinstance(result, Exception) else None,
                               return_value=result)
            self.assertIsNone(permissions.host_app(self.cfg, pid=40, runner=runner))


class TestPermissionHelperOpensOnlyReviewableSettings(Base):
    def setUp(self):
        super().setUp()
        self.app = self.cfg.home / 'Example Host.app'
        self.app.mkdir()
        self.state = {'supported': True, 'ready': False, 'target': 'host',
                      'memcal_app': str(schedule.app_path(self.cfg)), 'host_app': str(self.app),
                      'interactive': {'imessage': {'ok': False}},
                      'scheduled': {'ok': True, 'status': 'ok', 'detail': 'verified'},
                      'detail': 'Scheduled grant works; host is denied.'}
        self.enterContext(mock.patch.object(permissions, 'status', return_value=self.state))

    def test_opens_settings_and_reveals_exact_app_without_mutating_a_grant(self):
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
        runner = mock.Mock(side_effect=AssertionError('already authorized'))
        result = permissions.setup(self.cfg, target='memcal', runner=runner)
        self.assertFalse(result['opened'])
        self.assertTrue(result['target_ready'])
        self.assertFalse(result['steps'])

    def test_a_readable_host_is_not_reprompted_when_only_scheduled_access_is_denied(self):
        self.state.update(ready=False, target='memcal', scheduled={'ok': False},
                          interactive={'imessage': {'ok': True}})
        result = permissions.setup(self.cfg, target='host', runner=mock.Mock(side_effect=AssertionError('opened UI')))
        self.assertTrue(result['target_ready'])
        self.assertFalse(result['opened'])

    def test_failed_deep_link_falls_back_to_settings_and_keeps_manual_steps(self):
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
