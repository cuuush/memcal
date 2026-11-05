"""Consent, diagnostic progress, app ownership and grounded profile lifecycles."""

import argparse
import contextlib
import io
import json
import os
import plistlib
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from _support import Base
from memcal import archive, cli, db, identity, llm, schedule, settings, wiki
from memcal.dream import apply as apply_stage
from memcal.dream.bundle import Bundle
from memcal.sources import ical, imessage, whatsapp


class TestCalendarConsentCompletesWithoutCallback(Base):
    def test_probe_result_does_not_wait_for_a_store_connection(self):
        target = self.cfg.home / 'result.json'
        with mock.patch.object(cli, 'open_ctx', side_effect=AssertionError('store opened')), \
             mock.patch.object(ical, 'request_calendar_access', return_value=(True, 'granted')):
            code = cli.main(['--home', str(self.cfg.home), 'ical', 'probe', '--ek',
                             'request', '--result', str(target)])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(target.read_text())['ok'])

    @unittest.skipUnless(shutil.which('node'), 'JavaScript runtime required')
    def test_existing_grant_and_changed_authorization_do_not_wait_for_a_callback(self):
        function = ical.ACCOUNT_JXA.split('function request(store) {', 1)[1].split(
            '\n//: A repeating event', 1)[0]
        for statuses, expected in (([4], True), ([2], False), ([0, 4], True), ([0, 2], False)):
            script = '''
const FULL_ACCESS = [3,4], EVENT = 0;
const states = %s;
function status() { return states.length > 1 ? states.shift() : states[0]; }
const $ = { NSDate: {dateWithTimeIntervalSinceNow: () => 1,
                     date: {compare: () => -1}},
            NSRunLoop: {currentRunLoop: {runModeBeforeDate: () => { throw Error('waited'); }}} };
function request(store) {%s
console.log(JSON.stringify(request({requestFullAccessToEventsWithCompletion: () => {}})));
''' % (json.dumps(statuses), function)
            done = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=5)
            self.assertEqual(done.returncode, 0, done.stderr)
            result = json.loads(done.stdout)
            self.assertTrue(result['answered'])
            self.assertEqual(result['granted'], expected)


class TestDoctorReportsBeforeSlowChecks(Base):
    def test_progress_precedes_source_probe_and_json_stays_clean(self):
        output = io.StringIO()
        source = mock.Mock()
        source.name, source.in_all = 'imessage', True

        def check(_cfg):
            self.assertIn('Checking Memcal', output.getvalue())
            self.assertIn('Store', output.getvalue())
            return False, imessage.access_help(self.cfg)

        with mock.patch.object(cli.config, 'load', return_value=self.cfg), \
             mock.patch.object(cli.sources, 'all_sources', return_value=[source]), \
             mock.patch.object(schedule, 'source_access_health', return_value=('skip', 'none')), \
             mock.patch.object(source, 'check', side_effect=check), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            cli.main(['--home', str(self.cfg.home), 'doctor'])
        self.assertIn('Full Disk Access', output.getvalue())
        source.check.return_value = (True, 'readable')
        source.check.side_effect = None
        output = io.StringIO()
        with mock.patch.object(cli.sources, 'all_sources', return_value=[]), \
             mock.patch.object(schedule, 'source_access_health', return_value=('skip', 'none')), \
             contextlib.redirect_stdout(output):
            cli.main(['--home', str(self.cfg.home), 'doctor', '--json'])
        self.assertIsInstance(json.loads(output.getvalue()), list)


class TestUnresolvedHandlesFollowRecentSourceActivity(Base):
    def test_old_messages_drop_out_and_new_messages_return_without_losing_history(self):
        db.set_today('2026-11-05')
        handle = '+12025550123'
        identity.note_unresolved(self.conn, handle, 'imessage')
        archive.append(self.conn, channel='imessage', external_id='old', handle=handle,
                       ts='2026-10-01T12:00:00-04:00', text='Old conversation')
        self.conn.commit()
        self.assertFalse(identity.unresolved(self.conn))
        self.assertTrue(identity.unresolved(self.conn, active_days=60))
        archive.append(self.conn, channel='imessage', external_id='new', handle=handle,
                       ts='2026-11-05T12:00:00-05:00', text='New conversation')
        self.conn.commit()
        self.assertEqual(len(identity.unresolved(self.conn)), 1)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM archive').fetchone()[0], 2)


class TestScheduleReinstallAcknowledgesOnlyOldFailures(Base):
    def test_failure_returns_as_soon_as_a_new_attempt_fails(self):
        plist = self.cfg.home / 'job.plist'
        plist.write_bytes(plistlib.dumps({'StartCalendarInterval': {'Hour': 3, 'Minute': 0}}))
        schedule.script_path(self.cfg).write_text('PY="' + os.sys.executable + '"\n')
        (self.cfg.home / 'nightly.installed').touch()
        with mock.patch.object(schedule, 'plist_path', return_value=plist), \
             mock.patch.object(schedule, '_launchctl', return_value=(0, 'runs = 0\nlast exit code = 1')):
            state = schedule.status(self.cfg)
        self.assertIsNone(state['warning'])
        self.assertTrue(state['awaiting_run'])
        with mock.patch.object(schedule, 'plist_path', return_value=plist), \
             mock.patch.object(schedule, '_launchctl', return_value=(0, 'runs = 1\nlast exit code = 1')):
            state = schedule.status(self.cfg)
        self.assertIn('exited 1', state['warning'])


class TestWhatsAppCollectionOwnsOnlyItsLaunch(Base):
    def setUp(self):
        super().setUp()
        self.cfg.whatsapp_autostart = True
        self.enterContext(mock.patch.object(whatsapp.sys, 'platform', 'darwin'))
        self.enterContext(mock.patch.object(whatsapp.WhatsAppSource, 'check', return_value=(True, 'ready')))

    def test_new_app_closes_even_when_collection_raises(self):
        runner = mock.Mock(side_effect=[subprocess.CompletedProcess([], 1),
                                       subprocess.CompletedProcess([], 0),
                                       subprocess.CompletedProcess([], 0)])
        with self.assertRaisesRegex(RuntimeError, 'collection failed'):
            with whatsapp.desktop_session(self.cfg, runner=runner, report=lambda _: None):
                raise RuntimeError('collection failed')
        self.assertEqual(runner.call_args_list[1].args[0][:3], ['open', '-g', '-j'])
        self.assertEqual(runner.call_args.args[0][0], 'osascript')

    def test_already_open_app_and_disabled_launch_are_never_closed(self):
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0))
        with whatsapp.desktop_session(self.cfg, runner=runner, report=lambda _: None):
            pass
        self.assertEqual(runner.call_count, 1)
        with whatsapp.desktop_session(self.cfg, enabled=False, runner=runner):
            pass
        self.assertEqual(runner.call_count, 1)

    def test_wait_is_bounded_and_leave_open_is_configurable(self):
        self.cfg.whatsapp_close_after = False
        runner = mock.Mock(side_effect=[subprocess.CompletedProcess([], 1),
                                       subprocess.CompletedProcess([], 0)])
        clock = mock.Mock(side_effect=[0, 0, 0, 30])
        with mock.patch.object(whatsapp.WhatsAppSource, 'check', return_value=(False, 'unreadable')):
            with whatsapp.desktop_session(self.cfg, runner=runner, monotonic=clock,
                                          sleep=lambda _: None, report=lambda _: None):
                pass
        self.assertEqual(runner.call_count, 2)


class TestProviderFallbackIsExplicitAndPreservesRequests(Base):
    def test_primary_success_skips_fallback_and_error_retries_the_same_request(self):
        primary, secondary = mock.Mock(), mock.Mock()
        primary.usage, secondary.usage = llm.Usage(failed=1), llm.Usage(cost=0.25, calls=1)
        reply = llm.Reply('ok', {'ok': True}, model='fallback-model')
        primary.complete.return_value = reply
        secondary.complete.return_value = reply
        client = llm.FallbackClient(primary, secondary, 'fallback-model')
        request = {'model': 'primary-model', 'prefix': 'rules', 'suffix': 'messages',
                   'schema': {'type': 'object'}, 'turns': [{'role': 'user', 'content': 'follow up'}]}
        self.assertIs(client.complete(**request), reply)
        secondary.complete.assert_not_called()
        primary.complete.side_effect = llm.LLMError('rate limited')
        self.assertIs(client.complete(**request), reply)
        secondary.complete.assert_called_once_with(**{**request, 'model': 'fallback-model'})
        self.assertEqual(client.usage.cost, 0.25)
        self.assertEqual(client.usage.failed, 1)
        secondary.complete.side_effect = llm.LLMError('fallback failed')
        with self.assertRaisesRegex(llm.LLMError, 'fallback failed'):
            client.complete(**request)

    def test_disabled_fallback_does_not_construct_another_provider(self):
        with mock.patch.object(llm, '_client_for_provider') as factory:
            llm.client_for(self.cfg)
        factory.assert_called_once()
        with self.assertRaises(settings.SettingsError):
            settings.prepare(self.cfg, {'MEMCAL_FALLBACK_PROVIDER': 'codex'})


class TestWikiSubjectAndContextLifecycles(Base):
    def setUp(self):
        super().setUp()
        db.set_today('2026-11-05')

    def apply_claim(self, row, *, text='I live in Eastwood', from_me=True, eid='e1', ts=None):
        archive.append(self.conn, channel='imessage', external_id=eid,
                       ts=ts or '2026-11-04T12:00:00-05:00', text=text,
                       person='me' if from_me else 'Avery', from_me=from_me)
        self.conn.commit()
        items = list(self.conn.execute('SELECT * FROM archive WHERE external_id = ?', (eid,)))
        row = {**row, 'cite_ids': [items[0]['id']]}
        bundle = Bundle(entity='person:me', title='me', items=items)
        return apply_stage.apply_diffs(self.conn, self.cfg,
                                       [(bundle, {'wiki': [row]}, 'test-gen')], written_by='dream:test')

    def claim(self, **changes):
        return {'page': 'me', 'subject': 'me', 'subject_role': 'speaker',
                'durability': 'lasting', 'claim_kind': 'residence',
                'slot': 'where they live', 'value': 'Eastwood', **changes}

    def test_outgoing_recipient_fact_never_becomes_a_self_fact(self):
        self.apply_claim(self.claim(subject_role='recipient', claim_kind='contact',
                                    slot='account email', value='avery@example.invalid'),
                         text='This is your account email: avery@example.invalid')
        self.assertIsNone(wiki.read(self.cfg.wiki_dir, 'me'))

    def test_temporary_visits_administrative_notices_and_tautologies_are_rejected(self):
        for n, changes in enumerate(({'durability': 'temporary'},
                                     {'claim_kind': 'administrative'},
                                     {'slot': 'trustee', 'value': 'trustee'})):
            self.apply_claim(self.claim(**changes), eid='noise-' + str(n))
        self.assertIsNone(wiki.read(self.cfg.wiki_dir, 'me'))
        self.apply_claim(self.claim(), eid='home')
        self.assertEqual(wiki.read(self.cfg.wiki_dir, 'me').slots['where they live']['value'], 'Eastwood')

    def test_context_is_cited_searchable_replay_safe_and_preserves_user_prose(self):
        page = wiki.ensure(self.cfg.wiki_dir, 'me')
        page.body = 'A hand-written introduction.'
        wiki.write(self.cfg.wiki_dir, page)
        row = self.claim(slot=None, value=None, claim_kind='context', note_key='employer',
                         note_text='Prism is a design studio where I am interviewing.', note_previous=None)
        self.apply_claim(row, text=row['note_text'])
        page = wiki.read(self.cfg.wiki_dir, 'me')
        self.assertEqual(page.body, 'A hand-written introduction.')
        self.assertEqual(page.notes['employer']['value'], row['note_text'])
        self.assertTrue(page.notes['employer']['lines'])
        self.assertEqual(wiki.search_facts(self.cfg.wiki_dir, 'Prism')[0]['matched'], ['context'])
        self.assertTrue(wiki.profile(self.conn, self.cfg.wiki_dir, 'me')['context'][0]['sources'])
        count = len(wiki.slot_history(self.conn, 'me'))
        self.apply_claim(row)
        self.assertEqual(len(wiki.slot_history(self.conn, 'me')), count)
        corrected = page.render().replace(row['note_text'], 'Prism was a prospective employer.')
        wiki.record_raw_edit(self.conn, page, corrected)
        self.conn.commit()
        wiki.write_raw(self.cfg.wiki_dir, page, corrected)
        self.apply_claim({**row, 'note_previous': row['note_text'], 'note_text': 'Wrong replacement.'})
        self.assertEqual(wiki.read(self.cfg.wiki_dir, 'me').notes['employer']['value'],
                         'Prism was a prospective employer.')

    def test_deleted_slot_is_not_resurrected_by_old_evidence(self):
        self.apply_claim(self.claim())
        page = wiki.read(self.cfg.wiki_dir, 'me')
        page.slots.clear()
        wiki.record_raw_edit(self.conn, page, page.render())
        self.conn.commit()
        wiki.write(self.cfg.wiki_dir, page)
        self.apply_claim(self.claim())
        self.assertFalse(wiki.read(self.cfg.wiki_dir, 'me').slots)


if __name__ == '__main__':
    unittest.main()
