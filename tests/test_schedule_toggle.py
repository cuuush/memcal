"""Disabling scheduled work preserves app grants and survives later updates."""

import contextlib
import io
import plistlib
import subprocess
import unittest
from unittest import mock

from _support import Base
from memcal import cli, schedule


class TestScheduledWorkToggle(Base):
    def setUp(self):
        super().setUp()
        self.plist = self.cfg.home / 'job.plist'
        self.plist.write_bytes(plistlib.dumps({'StartCalendarInterval': {'Hour': 3, 'Minute': 0}}))
        self.enterContext(mock.patch.object(schedule, 'plist_path', return_value=self.plist))
        self.enterContext(mock.patch.object(schedule, '_is_macos', return_value=True))

    def test_disable_stops_all_scheduled_agents_and_preserves_installation(self):
        app = schedule.app_executable(self.cfg)
        app.parent.mkdir(parents=True)
        app.write_text('unchanged app')
        with mock.patch.object(schedule, '_launchctl', return_value=(0, '')) as launch:
            output = schedule.set_enabled(self.cfg, False)
        self.assertTrue(schedule.disabled_path(self.cfg).exists())
        self.assertTrue(self.plist.exists())
        self.assertEqual(app.read_text(), 'unchanged app')
        self.assertTrue(self.cfg.db_path.exists())
        self.assertIn('Scheduled work disabled', output[0])
        for label in (schedule.LABEL, *schedule.RETIRED_LABELS):
            launch.assert_any_call('disable', f'{schedule._domain()}/{label}')
            launch.assert_any_call('bootout', f'{schedule._domain()}/{label}')

    def test_enable_resumes_only_current_job_without_rebuilding_app(self):
        schedule.disabled_path(self.cfg).touch()
        def launch(command, *args):
            return (113, 'not loaded') if command == 'print' else (0, '')
        with mock.patch.object(schedule, '_launchctl', side_effect=launch) as calls, \
             mock.patch.object(schedule, 'build_app_bundle', side_effect=AssertionError('app rebuilt')):
            output = schedule.set_enabled(self.cfg, True)
        self.assertFalse(schedule.disabled_path(self.cfg).exists())
        calls.assert_any_call('bootstrap', schedule._domain(), str(self.plist))
        self.assertEqual([call.args[1] for call in calls.call_args_list if call.args[0] == 'enable'],
                         [f'{schedule._domain()}/{schedule.LABEL}'])
        self.assertIn('enabled', output[0])

    def test_failed_disable_is_reported_instead_of_claiming_success(self):
        with mock.patch.object(schedule, '_launchctl', return_value=(1, 'launchd unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'could not disable'):
                schedule.set_enabled(self.cfg, False)
        self.assertTrue(schedule.disabled_path(self.cfg).exists())

    def test_failed_enable_keeps_scheduling_disabled(self):
        schedule.disabled_path(self.cfg).touch()
        with mock.patch.object(schedule, '_launchctl', return_value=(1, 'launchd unavailable')):
            with self.assertRaises(RuntimeError):
                schedule.set_enabled(self.cfg, True)
        self.assertTrue(schedule.disabled_path(self.cfg).exists())

    def test_reinstall_does_not_reenable_disabled_work(self):
        schedule.disabled_path(self.cfg).touch()
        with mock.patch.object(schedule, '_launchctl', return_value=(0, '')) as launch, \
             mock.patch.object(schedule, 'build_app_bundle', return_value=[]), \
             mock.patch.object(schedule, '_retire_agents', return_value=[]):
            output = schedule.install(self.cfg)
        self.assertTrue(schedule.disabled_path(self.cfg).exists())
        self.assertFalse(any(call.args[0] in {'bootstrap', 'load', 'enable'} for call in launch.call_args_list))
        self.assertTrue(any('remains disabled' in line for line in output))

    def test_disabled_script_exits_without_starting_python_or_touching_logs(self):
        schedule.disabled_path(self.cfg).touch()
        script = schedule.script_path(self.cfg)
        script.write_text(schedule.render_script(self.cfg, python='/nonexistent/test-interpreter'))
        result = subprocess.run(['/bin/sh', str(script)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertFalse(schedule.log_path(self.cfg).exists())
        self.assertFalse((self.cfg.home / 'nightly.last-attempt').exists())

    def test_status_and_manual_scheduled_run_explain_disabled_state(self):
        schedule.disabled_path(self.cfg).touch()
        with mock.patch.object(schedule, '_launchctl', return_value=(113, 'not loaded')):
            state = schedule.status(self.cfg)
        self.assertTrue(state['disabled'])
        self.assertIsNone(state['warning'])
        self.assertIsNone(state['next'])
        self.assertIsNone(schedule.owed(self.cfg)[0])
        with mock.patch('subprocess.run', side_effect=AssertionError('scheduled work started')), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(schedule.run_now(self.cfg), 1)

    def test_disable_command_never_opens_the_store(self):
        output = io.StringIO()
        with mock.patch.object(cli, 'open_ctx', side_effect=AssertionError('store opened')), \
             mock.patch.object(schedule, '_launchctl', return_value=(0, '')), \
             contextlib.redirect_stdout(output):
            code = cli.main(['--home', str(self.cfg.home), 'disable'])
        self.assertEqual(code, 0)
        self.assertTrue(schedule.disabled_path(self.cfg).exists())
        self.assertIn('periodic source checks are stopped', output.getvalue())

    def test_non_macos_does_not_claim_to_stop_unmanaged_cron(self):
        with mock.patch.object(schedule, '_is_macos', return_value=False), \
             mock.patch.object(schedule, '_launchctl') as launch:
            with self.assertRaisesRegex(RuntimeError, 'macOS launchd'):
                schedule.set_enabled(self.cfg, False)
        launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
