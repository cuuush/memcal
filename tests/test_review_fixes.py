"""Lifecycle regressions for source identity, evidence, replay and ownership."""
from __future__ import annotations
import io
import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from tests._support import Base
from memcal import actions, archive, db, events, identity, live, llm, lock, series, threads, todos, trace, wiki
from memcal.sources import bluebubbles, proton
from memcal.dream import apply, run, sweep

class Pinned(Base):

    def setUp(self):
        super().setUp()
        db.set_today('2026-10-14T12:00:00-04:00')

class TestConfiguredDreamHorizon(Pinned):

    def test_custom_horizon(self):
        self.cfg.spool_horizon_days = 90
        aid = archive.append(self.conn, channel='agent', external_id='old', ts='2026-09-01T10:00:00-04:00', text='Dinner on October 20', gated=True)
        archive.spool_add(self.conn, aid, 'person:me')
        self.conn.commit()
        result = run.dream(self.conn, self.cfg, dry_run=True)
        self.assertEqual(result.bundles, 1)
        self.assertIsNone(self.conn.execute('SELECT processed_at FROM spool').fetchone()[0])

class TestTimestampBoundaryPagination(Pinned):

    def test_same_timestamp_bluebubbles(self):
        stamp = int(db.now_dt().timestamp() * 1000)
        messages = [dict(guid=f'm{i}', dateCreated=stamp, text='Dinner tomorrow at 7', handle={'address': '+15550101234'}, chats=[]) for i in range(501)]

        class Client:

            def __init__(self, cfg):
                pass

            def ping(self):
                return True

            def messages(self, after_ms, limit, offset):
                return [m for m in messages if m['dateCreated'] >= after_ms][offset:offset + limit]
        with patch.object(bluebubbles, 'BlueBubbles', Client):
            a = bluebubbles.ingest(self.conn, self.cfg, limit=500)
            b = bluebubbles.ingest(self.conn, self.cfg, limit=500)
            # A later arrival can be inserted ahead of previously read ties.
            messages.insert(0, dict(guid='late-tie', dateCreated=stamp, text='Lunch tomorrow', chats=[]))
            c = bluebubbles.ingest(self.conn, self.cfg, limit=500)
        self.assertEqual(len(archive.recent(self.conn, 1000)), 502)
        self.assertEqual(c.archived, 1)
        self.assertEqual(b.archived, 1)

class TestBudgetReportsRemainingMessages(Pinned):

    def test_bluebubbles_small_page_more(self):
        stamp = int(db.now_dt().timestamp() * 1000)

        class Client:

            def __init__(self, cfg):
                pass

            def ping(self):
                return True

            def messages(self, after_ms, limit, offset):
                return [dict(guid=f's{i}', dateCreated=stamp + i, text='Dinner tomorrow at 7', chats=[]) for i in range(100)][:limit]
        with patch.object(bluebubbles, 'BlueBubbles', Client):
            a = bluebubbles.ingest(self.conn, self.cfg, limit=20)
        self.assertTrue(a.more)

class TestDeletedOperationReplay(Pinned):

    def test_replay_after_delete(self):
        origin = actions.Origin.of('test', op_id='original-call')
        e = live.add_event(self.conn, self.cfg, title='Lunch', when='2026-10-20', origin=origin).event
        events.delete(self.conn, e.key)
        with self.assertRaises(live.LiveError):
            live.add_event(self.conn, self.cfg, title='Lunch', when='2026-10-20', origin=origin)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM events').fetchone()[0], 0)

class TestEndedScheduleProjections(Pinned):

    def test_ended_projection(self):
        live.set_schedule(self.conn, self.cfg, 'Tutoring', cadence='weekly', weekday=1, time='13:00')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM events').fetchone()[0], 1)
        live.set_schedule(self.conn, self.cfg, 'Tutoring', ended=True)
        series.roll_forward(self.conn)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM events').fetchone()[0], 0)

class TestDuplicateSweepPreservesRelationships(Pinned):

    def test_sweep_duplicate_loses_link(self):
        keep = events.upsert(self.conn, dict(key='keep', title='Lunch', date='2026-10-20'), match=False).event
        drop = events.upsert(self.conn, dict(key='drop', title='Lunch', date='2026-10-20'), match=False).event
        todo, _ = todos.open_todo(self.conn, 'Buy tickets', event_id=drop.id)
        reply = llm.Reply(text='{}', data={'drop_events': [{'key': 'drop', 'reason': 'duplicate', 'keep': 'keep'}]}, model='mock')
        client = SimpleNamespace(complete=lambda **kw: reply)
        sweep.sweep(client, self.conn, self.cfg, [])
        self.assertEqual(todos.get(self.conn, todo.key).event_id, keep.id)
        self.assertIsNone(events.get(self.conn, drop.key))

class TestEvidenceUsesInstants(Pinned):

    def test_timezone_precedence(self):
        e = events.upsert(self.conn, dict(title='Lunch', date='2026-10-20', location='New place'), written_by='live', evidence_ts={'location': '2026-10-14T09:00:00-04:00', 'title': '2026-10-14T09:00:00-04:00', 'date': '2026-10-14T09:00:00-04:00'}).event
        report = events.upsert(self.conn, dict(key=e.key, date=e.date, location='Old place'), written_by='dream:nightly', evidence_ts={'location': '2026-10-14T10:00:00+02:00'})
        self.assertEqual(report.event.location, 'New place')

class TestBlockedRecoveryDoesNotFetch(Pinned):

    def test_blocked_body_storage(self):
        from email.message import EmailMessage
        from memcal import web_queue
        aid = archive.append(self.conn, channel='email', external_id='blocked-recovery', ts=db.now(), handle='bad@example.com', text='Old subject', gate_reason='sender', meta={'uid': 1, 'folder': 'INBOX', 'body_error': 'old fetch failed'})
        self.conn.commit()
        web_queue.block(self.conn, self.cfg, {'address': 'bad@example.com'})
        msg = EmailMessage()
        msg['Subject'] = 'Old subject'
        msg.set_content('SHOULD_NOT_FETCH_BLOCKED_BODY')

        class Bridge:

            def __init__(self, cfg):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

            def select(self, folder):
                return (True, 'v1')
        with patch.object(proton, 'Bridge', Bridge), patch.object(proton, '_locate', return_value=(1, msg)) as fetched:
            report = proton.backfill_bodies(self.conn, self.cfg, dry_run=False)
        self.assertFalse(fetched.called)
        self.assertEqual(report['updated'], 0)
        self.assertNotIn('SHOULD_NOT_FETCH_BLOCKED_BODY', self.conn.execute('SELECT text FROM archive WHERE id=?', (aid,)).fetchone()[0])

class TestWikiCorrectionPrecedence(Pinned):

    def test_wiki_direct_correction(self):
        from memcal import wiki, trace
        from memcal.dream import apply
        aid = archive.append(self.conn, channel='imessage', external_id='wiki-old', ts='2026-10-10T09:00:00-04:00', text='Morgan lives at Old Street')
        self.conn.commit()
        wiki.set_slot(self.cfg.wiki_dir, 'morgan', 'address', 'Old Street', source='person:Morgan', conn=self.conn, archive_ids=[aid])
        trace.stamp(self.conn, kind='wiki', ref='morgan.address', archive_ids=[aid])
        self.conn.commit()
        live.note(self.conn, self.cfg, 'morgan', 'address', 'New Street', source='you')
        apply._apply_wiki(self.conn, self.cfg, dict(page='morgan', slot='address', value='Older Street'), source='person:Morgan', evidence_ts='2026-10-11T09:00:00-04:00')
        self.assertEqual(wiki.read(self.cfg.wiki_dir, 'morgan').slots['address']['value'], 'New Street')

class TestExplicitDatesAreValidated(Pinned):

    def test_bad_date(self):
        with self.assertRaises(ValueError):
            live.add_event(self.conn, self.cfg, title='Trip', when='2026-02-30')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM events').fetchone()[0], 0)

class TestJobStreamContentType(Pinned):

    def test_sse_type(self):
        from memcal import web_server, web_jobs
        import io
        job = web_jobs._Job('test')
        job.done = True
        headers = {}
        handler = SimpleNamespace(send_response=lambda code: None, send_header=lambda k, v: headers.update({k: v}), end_headers=lambda: None, wfile=io.BytesIO())
        with patch.object(web_jobs, 'find_job', return_value=('job-1', job)):
            web_server.Handler._stream_job(handler, {'id': 'job-1'})
        self.assertEqual(headers['Content-Type'], 'text/event-stream')

class TestTransportFailuresAreReported(Pinned):

    def test_polled_failure_complete(self):
        from memcal.sources.polled import PolledSource, Conversation

        class Broken(PolledSource):
            name = 'synthetic'

            def connect(self, cfg):
                return object()

            def conversations(self, client):
                return [Conversation(id='1', name='Thread')]

            def history(self, *a):
                raise RuntimeError('network lost')
        report = Broken().run(self.conn, self.cfg)
        self.assertEqual(report.outcome, 'failed')
        self.assertTrue(report.notes)
        self.assertIsNone(db.get_meta(self.conn, 'source.synthetic.last_success'))

class TestCalendarWithdrawalsReachPublication(Pinned):

    def test_calendar_withdrawal(self):
        from memcal.sources import ical
        event = live.add_event(self.conn, self.cfg, title='Appointment', when='2026-10-20', status='confirmed').event
        self.cfg.publish_calendar = 'Synthetic Calendar'
        self.conn.execute("INSERT INTO calendar_items(identity,calendar_uid,calendar_name,event_uid,event_key,starts_at,provider,active,published,last_seen_at,updated_at) VALUES('cal-1','cal','Synthetic Calendar','uid-1',?,'2026-10-20T13:00:00Z','ical',1,1,?,?)", (event.key, db.now(), db.now()))
        self.conn.commit()
        with patch.object(ical, '_write_event', side_effect=AssertionError('should not publish declined')), patch.object(ical, 'retract_unpublishable') as retract:
            live.update_event(self.conn, self.cfg, event.key, status='declined')
            ical.publish_pending(self.conn, self.cfg)
        self.assertTrue(retract.called)
        self.assertEqual(self.conn.execute("SELECT published FROM calendar_items WHERE identity='cal-1'").fetchone()[0], 1)

class TestExplicitAttendanceCorrections(Pinned):

    def test_user_status_downgrade(self):
        event = live.add_event(self.conn, self.cfg, title='Dinner', when='2026-10-20', status='confirmed').event
        out = live.update_event(self.conn, self.cfg, event.key, status='tentative')
        self.assertEqual(out.event.status, 'tentative')

class TestDreamHasOneOwner(Pinned):

    def test_a_second_pass_cannot_abandon_or_read_the_active_pass(self):
        from memcal.dream import propose, merge
        aid = archive.append(self.conn, channel='agent', external_id='one-owner', ts=db.now(), text='Dinner tomorrow at seven', gated=True)
        archive.spool_add(self.conn, aid, 'person:me')
        self.conn.commit()
        ready, release = (threading.Event(), threading.Event())
        errors, seen = ([], [])

        def propose_once(client, conn, cfg, bundles, **kwargs):
            seen.append(len(bundles))
            ready.set()
            if not release.wait(10):
                raise RuntimeError('test did not release the owner')
            return ([(bundle, {}, None) for bundle in bundles], [], [])

        def worker():
            conn = db.connect(self.cfg.db_path)
            try:
                run.dream(conn, self.cfg, skip_sweep=True)
            except Exception as exc:
                errors.append(exc)
            finally:
                conn.close()
        with patch.object(llm, 'client_for', return_value=llm.CompletionClient()), patch.object(propose, 'propose_all', side_effect=propose_once), patch.object(merge, 'merge_all', side_effect=lambda client, cfg, proposals, **kw: (proposals, [])):
            owner = threading.Thread(target=worker)
            owner.start()
            try:
                self.assertTrue(ready.wait(10))
                with self.assertRaises(lock.Busy):
                    run.dream(self.conn, self.cfg, skip_sweep=True)
                active = self.conn.execute('SELECT * FROM runs').fetchall()
                self.assertEqual(len(active), 1)
                self.assertIsNone(active[0]['error'])
                self.assertIsNone(active[0]['finished_at'])
            finally:
                release.set()
                owner.join(10)
        self.assertFalse(owner.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual(seen, [1])

class TestWikiPublicationSerializesStaging(Pinned):

    def test_a_write_during_publication_remains_pending_and_is_published_next(self):
        self.conn.execute('BEGIN')
        wiki.set_slot(self.cfg.wiki_dir, 'morgan', 'address', 'First Street', conn=self.conn, commit=False)
        self.conn.commit()
        publishing, done = (threading.Event(), threading.Event())
        errors = []
        original = wiki._write_rendered

        def stage_second():
            conn = db.connect(self.cfg.db_path)
            try:
                if not publishing.wait(10):
                    raise RuntimeError('publication did not start')
                conn.execute('BEGIN IMMEDIATE')
                wiki.set_slot(self.cfg.wiki_dir, 'morgan', 'address', 'Second Street', conn=conn, commit=False)
                conn.commit()
            except Exception as exc:
                errors.append(exc)
            finally:
                conn.close()
                done.set()

        def paused_publish(path, content):
            publishing.set()
            self.assertFalse(done.wait(0.05))
            original(path, content)
        writer = threading.Thread(target=stage_second)
        writer.start()
        try:
            with patch.object(wiki, '_write_rendered', side_effect=paused_publish):
                wiki.recover(self.conn, self.cfg.wiki_dir)
        finally:
            publishing.set()
            writer.join(10)
        self.assertFalse(writer.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM wiki_pending_writes').fetchone()[0], 1)
        wiki.recover(self.conn, self.cfg.wiki_dir)
        self.assertEqual(wiki.read(self.cfg.wiki_dir, 'morgan').slots['address']['value'], 'Second Street')

class TestEvidenceLifecycleAcrossWritesAndReplay(Pinned):

    def test_old_source_rules_and_wiki_cannot_undo_typed_writes_but_newer_ones_can(self):
        live.set_schedule(self.conn, self.cfg, 'Tutoring', cadence='weekly', weekday=1, time='13:00')
        live.note(self.conn, self.cfg, 'morgan', 'address', 'New Street', source='you')
        for external_id, stamp, weekday, time, address in (('old', '2026-10-10T09:00:00-04:00', 2, '15:00', 'Old Street'), ('new', '2026-10-15T09:00:00-04:00', 4, '17:00', 'Newest Street')):
            aid = archive.append(self.conn, channel='agent', external_id=external_id, ts=stamp, text=f'Tutoring is now at {time}; Morgan lives at {address}')
            self.conn.commit()
            for replay in range(2):
                apply._apply_series(self.conn, self.cfg, {'slug': 'tutoring', 'cadence': 'weekly', 'weekday': weekday, 'time': time}, source='person:Morgan', written_by='dream:nightly', evidence_ts=stamp)
                apply._apply_wiki(self.conn, self.cfg, {'page': 'morgan', 'slot': 'address', 'value': address}, source='person:Morgan', evidence_ts=stamp, archive_ids=[aid])
            expected = (1, '13:00', 'New Street') if external_id == 'old' else (4, '17:00', 'Newest Street')
            rule = series.get(self.conn, 'tutoring')
            page = wiki.read(self.cfg.wiki_dir, 'morgan')
            self.assertEqual((rule.weekday, rule.time, page.slots['address']['value']), expected)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM series').fetchone()[0], 1)

class TestStableConversationIdentity(Pinned):

    def test_equal_names_and_rosters_stay_separate_and_rename_keeps_identity(self):
        from tests.test_polled import FakeChat, Conversation
        chats = [Conversation(id=str(i), name='Friends', is_group=True) for i in (1, 2)]
        source = FakeChat(chats, {'1': [{'id': '1'}], '2': [{'id': '1'}]})
        source.run(self.conn, self.cfg)
        self.assertEqual({row[0] for row in self.conn.execute('SELECT thread FROM archive')}, {'id:1', 'id:2'})
        self.assertEqual(threads.aliases(self.conn), {})
        source._conversations = [Conversation(id='1', name='Renamed Friends', is_group=True)]
        source._history['1'].append({'id': '2'})
        source.run(self.conn, self.cfg)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM threads').fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT label FROM threads WHERE thread = 'id:1'").fetchone()[0], 'Renamed Friends')

    def test_unambiguous_legacy_history_and_mute_follow_the_id(self):
        threads.record(self.conn, 'synthetic', 'Friends', label='Friends', participants=['synthetic:42'])
        threads.record_members(self.conn, 'synthetic', 'Friends', [('synthetic:42', 'Morgan')])
        self.conn.execute("UPDATE threads SET decision = 'mute', decided_by = 'you'")
        archive.append(self.conn, channel='synthetic', external_id='legacy', thread='Friends', ts=db.now(), text='Dinner tomorrow')
        threads.record(self.conn, 'synthetic', 'id:123', label='Friends', participants=['synthetic:42'])
        threads.record_members(self.conn, 'synthetic', 'id:123', [('synthetic:42', 'Morgan')])
        threads.adopt_key(self.conn, 'synthetic', 'id:123', 'Friends')
        row = self.conn.execute('SELECT * FROM threads').fetchone()
        self.assertEqual((row['thread'], row['decision'], row['decided_by']), ('id:123', 'mute', 'you'))
        self.assertEqual(self.conn.execute('SELECT thread FROM archive').fetchone()[0], 'id:123')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM thread_members').fetchone()[0], 1)
        self.assertEqual(self.conn.execute('SELECT seen_count FROM thread_member_names').fetchone()[0], 2)

class TestSweepUsesTheReviewedVersion(Pinned):

    def test_a_correction_during_the_model_call_is_not_deleted(self):
        event = events.upsert(self.conn, {'key': 'corrected', 'title': 'Lunch', 'date': '2026-10-20'}, match=False).event

        def complete(**kwargs):
            live.update_event(self.conn, self.cfg, event.key, location='New venue')
            return llm.Reply(text='{}', model='mock', data={'drop_events': [{'key': event.key, 'reason': 'junk', 'keep': ''}]})
        _, changes = sweep.sweep(SimpleNamespace(complete=complete), self.conn, self.cfg, [])
        self.assertEqual(changes, [])
        self.assertEqual(events.get(self.conn, event.key).location, 'New venue')
if __name__ == '__main__':
    unittest.main()
