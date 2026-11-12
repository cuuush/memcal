"""Doctor warns about inactive scheduling and offers optional Slack setup."""

import contextlib
import io
import unittest
from unittest import mock

from _support import Base
from memcal import cli, schedule
from memcal.sources.slack import SlackSource


class DoctorWarningBase(Base):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.dict('os.environ', {}, clear=True))
        self.state = {'installed': False, 'loaded': False, 'disabled': False,
                      'at': (3, 0), 'warning': None, 'label': schedule.LABEL,
                      'next': 'next scheduled pass', 'why': 'nothing missed'}
        self.enterContext(mock.patch.object(schedule, 'status', return_value=self.state))
        self.enterContext(mock.patch.object(schedule, 'bundle_health',
                                           return_value=('ok', 'current')))
        self.enterContext(mock.patch.object(schedule, 'plist_identity_health',
                                           return_value=('ok', 'current')))
        self.enterContext(mock.patch.object(schedule, 'source_access_health',
                                           return_value=(cli.SKIP, 'no protected sources')))
        self.enterContext(mock.patch.object(cli.llm, 'provider_status',
                                           return_value=(True, 'available')))
        self.source_list = self.enterContext(mock.patch.object(cli.sources, 'all_sources',
                                                               return_value=[]))

    def finding(self, section, name):
        return next(f for f in cli.doctor_findings(self.conn, self.cfg)
                    if f.section == section and f.name == name)


class TestDoctorWarnsWhenNightlyIsInactive(DoctorWarningBase):
    def test_disabled_nightly_warns_and_suggests_enable(self):
        self.state.update(installed=True, disabled=True)
        finding = self.finding('Schedule', 'nightly')
        self.assertEqual(finding.status, cli.WARN)
        self.assertIn('not running', finding.detail)
        self.assertEqual(finding.fix, 'memcal enable')

    def test_disabled_without_install_needs_enable_and_install(self):
        self.state['disabled'] = True
        finding = self.finding('Schedule', 'nightly')
        self.assertEqual(finding.status, cli.WARN)
        self.assertIn('memcal enable', finding.fix)
        self.assertIn('memcal schedule install', finding.fix)

    def test_missing_or_unloaded_warn_but_loaded_passes(self):
        for installed, loaded, expected in ((False, False, cli.WARN),
                                             (True, False, cli.WARN),
                                             (True, True, cli.OK)):
            with self.subTest(installed=installed, loaded=loaded):
                self.state.update(installed=installed, loaded=loaded)
                finding = self.finding('Schedule', 'nightly')
                self.assertEqual(finding.status, expected)
                if expected == cli.WARN:
                    self.assertIn('memcal schedule install', finding.fix)


class TestDoctorOffersDisablingUnconfiguredSlack(DoctorWarningBase):
    def setUp(self):
        super().setUp()
        self.slack = SlackSource()
        self.source_list.return_value = [self.slack]

    def test_missing_token_offers_login_or_disable(self):
        finding = self.finding('Sources', 'slack')
        self.assertEqual(finding.status, cli.WARN)
        self.assertIn('no Slack token', finding.detail)
        self.assertIn('memcal login slack', finding.fix)
        self.assertIn('disable Slack', finding.fix)
        self.assertIn('Settings', finding.fix)

    def test_disabled_slack_is_skipped_without_setup_warning(self):
        self.cfg.disabled_sources = 'slack'
        finding = self.finding('Sources', 'slack')
        self.assertEqual(finding.status, cli.SKIP)
        self.assertIn('disabled', finding.detail)
        self.assertFalse(finding.fix)

    def test_existing_token_failure_keeps_its_usual_remedy(self):
        self.cfg.env = {'slack': 'synthetic-test-token'}
        with mock.patch.object(self.slack, 'check', return_value=(False, 'connection failed')):
            finding = self.finding('Sources', 'slack')
        self.assertEqual(finding.status, cli.WARN)
        self.assertNotIn('disable Slack', finding.fix)


class TestCompletionHiddenFromMainHelp(unittest.TestCase):
    def test_main_help_hides_completion_but_command_still_generates_scripts(self):
        parser = cli.build_parser()
        self.assertNotIn('completion', parser.format_help())
        for shell, expected in (('zsh', '#compdef memcal'),
                                ('bash', 'complete -F')):
            with self.subTest(shell=shell):
                args = parser.parse_args(['completion', shell])
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(args.func(args), 0)
                self.assertIn(expected, out.getvalue())


if __name__ == '__main__':
    unittest.main()
