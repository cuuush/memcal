"""Doctor checks the scheduled app's read permission, not its caller's identity."""

import json
import sqlite3
import unittest
from pathlib import Path
from unittest import mock

from _support import Base
from memcal import cli, schedule
from memcal.sources import imessage


class TestProtectedSourceProbeReadsNoBodies(Base):
    def setUp(self):
        super().setUp()
        self.cfg.disabled_sources = 'whatsapp'

    def test_checks_source_table_read_only_and_does_not_return_contents(self):
        path = Path(self.tmp.name) / 'chat.db'
        src = sqlite3.connect(path)
        src.execute('CREATE TABLE message(text TEXT)')
        src.execute("INSERT INTO message VALUES('private message body')")
        src.commit()
        src.close()
        with mock.patch.object(imessage, 'CHAT_DB', path):
            result = schedule.read_source_access(self.cfg)
        self.assertTrue(result['checks']['imessage']['ok'])
        self.assertNotIn('private message body', json.dumps(result))
        with sqlite3.connect(path) as src:
            self.assertEqual(src.execute('SELECT count(*) FROM message').fetchone()[0], 1)

    def test_missing_source_is_not_created_and_reports_failure(self):
        path = Path(self.tmp.name) / 'missing.db'
        with mock.patch.object(imessage, 'CHAT_DB', path):
            result = schedule.read_source_access(self.cfg)
        self.assertFalse(result['checks']['imessage']['ok'])
        self.assertFalse(path.exists())

    def test_respects_disabled_sources_and_remote_imessage(self):
        self.cfg.imessage_backend = 'bluebubbles'
        with mock.patch.object(schedule, 'sqlite3') as database:
            self.assertEqual(schedule.read_source_access(self.cfg), {'checks': {}})
        database.connect.assert_not_called()

    def test_whatsapp_uses_configured_database(self):
        self.cfg.disabled_sources = 'imessage'
        path = Path(self.tmp.name) / 'whatsapp.db'
        self.cfg.env = {'WHATSAPP_DB': str(path)}
        with sqlite3.connect(path) as src:
            src.execute('CREATE TABLE ZWAMESSAGE(ZTEXT TEXT)')
        result = schedule.read_source_access(self.cfg)
        self.assertEqual(set(result['checks']), {'whatsapp'})
        self.assertTrue(result['checks']['whatsapp']['ok'])

    def test_internal_cli_probe_never_opens_or_migrates_memcal_store(self):
        home = Path(self.tmp.name) / 'untouched-store'
        result = Path(self.tmp.name) / 'probe.json'
        with mock.patch.object(cli, 'open_ctx', side_effect=AssertionError('opened store')), \
             mock.patch.object(schedule, 'read_source_access', return_value={'checks': {}}):
            self.assertEqual(cli.main(['--home', str(home), 'doctor', '--probe-source-access',
                                       '--result', str(result)]), 0)
        self.assertEqual(json.loads(result.read_text()), {'checks': {}})
        self.assertFalse(home.exists())


class TestDoctorUsesScheduledPermissionIdentity(Base):
    def setUp(self):
        super().setUp()
        self.cfg.disabled_sources = 'whatsapp'
        self.enterContext(mock.patch.object(schedule, '_is_macos', return_value=True))
        self.enterContext(mock.patch.object(schedule, 'bundle_health', return_value=('ok', 'current')))

    def test_rechecks_access_and_detects_revocation_under_scheduled_identity(self):
        payloads = [({'checks': {'imessage': {'ok': True}}}, 'memcal'),
                    ({'checks': {'imessage': {'ok': False, 'detail': 'permission denied'}}}, 'memcal')]
        with mock.patch.object(schedule, 'launchd_memcal_call', side_effect=payloads) as launch:
            self.assertEqual(schedule.source_access_health(self.cfg)[0], 'ok')
            status, detail = schedule.source_access_health(self.cfg)
        self.assertEqual(status, 'fail')
        self.assertIn('imessage', detail)
        self.assertIn('permission denied', detail)
        self.assertEqual(launch.call_args.kwargs['memcal_argv'], ['doctor', '--probe-source-access'])
        self.assertNotEqual(launch.call_args_list[0].kwargs['label'], launch.call_args_list[1].kwargs['label'])

    def test_timeout_or_incomplete_result_is_unverified_not_healthy(self):
        for payload in (None, {}, {'checks': {}}, {'checks': {'imessage': {'ok': 'yes'}}},
                        {'checks': {'imessage': {'ok': 1}}}):
            with self.subTest(payload=payload), \
                 mock.patch.object(schedule, 'launchd_memcal_call', return_value=(payload, 'memcal')):
                self.assertEqual(schedule.source_access_health(self.cfg)[0], 'warn')

    def test_disabled_sources_do_not_launch_a_probe(self):
        self.cfg.disabled_sources = 'imessage,whatsapp'
        with mock.patch.object(schedule, 'launchd_memcal_call') as launch:
            self.assertEqual(schedule.source_access_health(self.cfg)[0], 'skip')
        launch.assert_not_called()

    def test_doctor_reports_denial_even_when_interactive_source_is_reachable(self):
        state = {'installed': True, 'loaded': True, 'at': (3,0), 'warning': None,
                 'label': schedule.LABEL, 'next': 'tomorrow', 'why': 'up to date'}
        source = mock.Mock(name='imessage-source')
        source.name, source.in_all = 'imessage', True
        source.check.return_value = (True, 'terminal can read it')
        with mock.patch.object(schedule, 'status', return_value=state), \
             mock.patch.object(schedule, 'source_access_health', return_value=('fail', 'memcal.app cannot read imessage')), \
             mock.patch.object(cli.sources, 'all_sources', return_value=[source]):
            findings = cli.doctor_findings(self.conn, self.cfg)
        finding = next(f for f in findings if f.name == 'protected source access')
        self.assertEqual(finding.status, cli.FAIL)
        self.assertIn('Full Disk Access', finding.fix)
        self.assertIn(str(schedule.app_path(self.cfg)), finding.fix)
        self.assertIn("terminal's permission is separate", finding.fix)


if __name__ == '__main__':
    unittest.main()
