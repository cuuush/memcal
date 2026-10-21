"""Regressions at the scheduled-run, retained-context, and detail boundaries."""

import os
import sqlite3
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from _support import Base
from memcal import brief, db, detail, events, identity, schedule, threads, todos, trace
from memcal.dream import bundle as bundles, propose, run, sweep
from memcal.sources import base, imessage, proton, whatsapp


class TestScheduledRuntimeVisibility(Base):
    def test_generated_script_preserves_node_path_without_a_login_shell(self):
        bindir = Path(self.tmp.name) / 'node runtime'
        bindir.mkdir()
        node = bindir / 'node'
        node.write_text('#!/bin/sh\necho node-visible\n')
        node.chmod(0o755)
        with mock.patch.dict(os.environ, {'PATH': str(bindir) + ':/usr/bin:/bin'}):
            script = schedule.render_script(self.cfg)
            path_line = next(line for line in script.splitlines() if line.startswith('export PATH='))
            proc = subprocess.run(['/bin/sh', '-c', path_line + '\nnode'],
                                  env={'PATH': '/usr/bin:/bin'}, capture_output=True, text=True)
            plist = schedule.render_plist(self.cfg)
        self.assertEqual(proc.stdout.strip(), 'node-visible')
        self.assertEqual(proc.returncode, 0)
        self.assertIn(str(bindir), plist['EnvironmentVariables']['PATH'])


class TestScheduledReadPermissionGuidance(Base):
    def test_failed_message_reads_name_the_responsible_bundle(self):
        path = Path(self.tmp.name) / 'protected.sqlite'
        path.touch()
        with mock.patch.dict(os.environ, {'MEMCAL_APP': '1', 'MEMCAL_HOME': str(self.cfg.home)}), \
             mock.patch.object(imessage.sqlite3, 'connect', side_effect=sqlite3.OperationalError('unable to open database file')):
            reports = [imessage.ingest(self.conn, db_path=path),
                       whatsapp.ingest(self.conn, self.cfg, db_path=str(path))]
        for report in reports:
            self.assertIn(str(self.cfg.home / 'memcal.app'), report.error)
            self.assertIn('Full Disk Access', report.error)
            self.assertNotIn('terminal', report.error)


class TestBridgeAutomaticStartup(Base):
    def test_refused_local_connection_launches_once_then_logs_in(self):
        self.cfg.env = {'PROTON_BRIDGE_USER': 'user', 'PROTON_BRIDGE_PASSWORD': 'pass'}
        imap = mock.Mock()
        with mock.patch.object(proton.imaplib, 'IMAP4_SSL',
                               side_effect=[ConnectionRefusedError(), imap]), \
             mock.patch.object(proton, '_launch_local_bridge', return_value=True) as launch:
            with proton.Bridge(self.cfg) as bridge:
                self.assertEqual(bridge.security, 'SSL')
        launch.assert_called_once_with('127.0.0.1', 1143)
        imap.login.assert_called_once_with('user', 'pass')

    def test_remote_endpoint_never_launches_a_local_app(self):
        with mock.patch.object(proton, 'subprocess') as process:
            self.assertFalse(proton._launch_local_bridge('mail.example.org', 1143))
        process.run.assert_not_called()

    def test_launch_waits_for_listener_and_checks_open_failure(self):
        with mock.patch.object(proton.sys, 'platform', 'darwin'), \
             mock.patch.object(proton.subprocess, 'run', return_value=mock.Mock(returncode=0)) as opened, \
             mock.patch.object(proton.socket, 'create_connection',
                               side_effect=[ConnectionRefusedError(), mock.MagicMock()]) as connect, \
             mock.patch.object(proton.time, 'sleep'):
            self.assertTrue(proton._launch_local_bridge('localhost', 1143))
        self.assertEqual(connect.call_count, 2)
        self.assertIn('Proton Mail Bridge', opened.call_args.args[0])
        with mock.patch.object(proton.sys, 'platform', 'darwin'), \
             mock.patch.object(proton.subprocess, 'run', return_value=mock.Mock(returncode=1)), \
             mock.patch.object(proton.socket, 'create_connection') as connect:
            self.assertFalse(proton._launch_local_bridge('localhost', 1143))
        connect.assert_not_called()


class TestRetainedEmailNamesAndContext(Base):
    def add_email(self, handle='receipts@example.org', name='Example', reason='bulk-headers'):
        meta = db.jdump({'from': f'\\"{name}\\" <{handle}>', 'subject': 'Our October concerts'})
        cursor = self.conn.execute(
            "INSERT INTO archive(channel, external_id, ts, thread, handle, text, meta, gate_reason, created_at)"
            " VALUES('email', ?, ?, 'mail-thread', ?, 'Concert announcement', ?, ?, ?)",
            (handle, db.now(), handle, meta, reason, db.now()))
        self.conn.commit()
        return cursor.lastrowid

    def test_existing_header_recovers_name_and_removes_unresolved_without_domain_guess(self):
        self.add_email()
        identity.note_unresolved(self.conn, 'receipts@example.org', 'email')
        identity.note_unresolved(self.conn, 'unknown@other.org', 'email')
        self.assertEqual(proton.recover_sender_names(self.conn), 1)
        self.assertEqual(identity.resolve(self.conn, 'receipts@example.org'), 'Example')
        self.assertIsNone(identity.resolve(self.conn, 'unknown@other.org'))
        self.assertEqual(proton.recover_sender_names(self.conn), 0)
        self.assertEqual([r['handle'] for r in identity.unresolved(self.conn)], ['unknown@other.org'])

    def test_promotional_provenance_survives_compact_brief_and_open(self):
        db.set_today('2026-10-20')
        rid = self.add_email()
        event, _ = events.upsert(self.conn, {'title': 'The band', 'date': '2026-10-22',
                                           'kind': 'opportunity', 'status': 'mentioned'})
        trace.stamp(self.conn, kind='event', ref=event.key, verb='created', archive_ids=[rid])
        opened = detail.open_handle(self.conn, self.cfg, f'E{event.id}')
        self.assertIn('Promotional email from Example', opened)
        self.assertIn('Our October concerts', opened)
        self.assertIn('Promotional email from Example', brief.render(self.conn, self.cfg))

    def test_address_is_not_an_already_named_conversation(self):
        item = {'channel': 'email', 'thread': 'mail', 'handle': 'sender@example.org',
                'from_me': 0, 'person': None}
        bundle = bundles.Bundle(entity='thread:email:mail', title='sender@example.org', items=[item])
        self.assertEqual(propose._lone_nameless_thread(bundle), ('email', 'mail'))


class TestQuestionReviewReportsActionableRows(Base):
    def test_rejected_and_dropped_questions_are_not_reported_as_pending(self):
        db.set_today('2026-10-20')
        event, _ = events.upsert(self.conn, {'title': 'Lunch', 'date': '2026-10-19'})
        with mock.patch.object(todos, 'ask', return_value=''):
            self.assertEqual(sweep.reconcile_backward_window(self.conn, self.cfg), [])
        key = todos.ask(self.conn, 'Did Lunch happen on Monday? who with?',
                        key=f'q:resolve:{event.key}', about_event=event.id, written_by='reconcile')
        self.conn.execute("UPDATE questions SET status='dropped' WHERE key=?", (key,))
        self.conn.commit()
        self.assertEqual(sweep.reconcile_backward_window(self.conn, self.cfg), [])

    def test_report_names_the_question_and_reply_command(self):
        report = run.DreamResult(run_id=1, questions=['Q7  What time is lunch with Sam?']).report(verbose=False)
        self.assertIn('Q7  What time is lunch with Sam?', report)
        self.assertIn('memcal answer', report)


class TestSingleRecordThreadLookup(Base):
    def test_narrow_title_lookup_matches_global_names_and_empty_scope_is_empty(self):
        self.conn.execute("INSERT INTO archive(channel,external_id,ts,thread,handle,person,text,created_at)"
                          " VALUES('imessage','one',?,'one','+111','Sam','hello',?)", (db.now(), db.now()))
        self.conn.execute("INSERT INTO archive(channel,external_id,ts,thread,handle,person,text,created_at)"
                          " VALUES('imessage','two',?,'two','+222','Riley','hello',?)", (db.now(), db.now()))
        expected = threads.titles(self.conn)[('imessage', 'one')]
        self.assertEqual(threads.title(self.conn, 'imessage', 'one'), expected)
        self.assertEqual(threads.titles(self.conn, keys=[]), {})
        self.assertEqual(threads.titles(self.conn, keys=[('imessage','one')]),
                         {('imessage', 'one'): expected})


class TestGroupSchedulingKeepsConversationContext(Base):
    def test_short_date_reply_gets_prior_topic_and_silent_members(self):
        db.set_today('2026-10-20')
        identity.set_me(self.conn, 'Casey Owner')
        identity.link(self.conn, '+111', 'Sam', source='contacts')
        identity.link(self.conn, '+222', 'Riley', source='contacts')
        threads.record_members(self.conn, 'imessage', 'group', [('+111', None), ('+222', None)])
        self.conn.execute("INSERT INTO threads(channel,thread,is_group,updated_at)"
                          " VALUES('imessage','group',1,?)", (db.now(),))
        for external_id, ts, text in [('topic', '2026-10-14T12:00:00-04:00', 'Our next tabletop campaign session'),
                                      ('date', '2026-10-19T12:00:00-04:00', 'I can do the 1st!')]:
            self.conn.execute(
                "INSERT INTO archive(channel,external_id,ts,thread,handle,person,text,created_at)"
                " VALUES('imessage',?,?,'group','+111','Sam',?,?)", (external_id, ts, text, db.now()))
        self.conn.commit()
        newest = self.conn.execute("SELECT * FROM archive WHERE external_id='date'").fetchone()
        bundle = bundles.Bundle(entity='thread:imessage:group', items=[newest])
        bundles.add_thread_context(self.conn, bundle)
        block = propose.build_bundle_block(self.cfg, bundle, self.conn)
        self.assertIn('Our next tabletop campaign session', block)
        self.assertIn('Sam', block)
        self.assertIn('Riley', block)  # A roster member who has not spoken in this bundle.
        self.assertIn('membership is not attendance', block)
        self.assertEqual(bundle.cite(['L1']), [bundle.items[0]['id']])
        self.assertIn(bundle.items[0]['id'], bundle.context_ids)


if __name__ == '__main__':
    unittest.main()
