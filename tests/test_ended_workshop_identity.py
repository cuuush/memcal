"""A new appointment must not acquire the identity or access details of old workshop."""

import unittest

from _support import Base
from memcal import archive, db, events, live, series, trace, wiki
from memcal.dream import apply as apply_stage
from memcal.dream.bundle import Bundle
from memcal.sources import ical


class TestEndedScheduleCannotContaminateNewAppointment(Base):
    def setUp(self):
        super().setUp()
        db.set_today('2026-10-20')
        series.upsert(self.conn, {'slug': 'workshop', 'title': 'workshop',
            'cadence': 'weekly', 'weekday': 1, 'time': '13:00', 'location': 'Telehealth',
            'join_url': 'https://meet.example/old-workshop', 'status': 'ended',
            'effective_on': '2026-08-01', 'ends_on': '2026-09-11'})
        for day in ('2026-08-11', '2026-08-18'):
            events.upsert(self.conn, {'title': 'workshop', 'date': day, 'series': 'workshop'}, match=False)
        self.item = {'calendar_name': 'Home', 'calendar_uid': 'Home', 'writable': True,
            'uid': 'new-appointment', 'title': 'workshop', 'start': '2026-10-21T20:00:00Z',
            'end': '2026-10-21T21:00:00Z', 'all_day': False, 'location': '',
            'description': '', 'url': ''}

    def ingest(self):
        ical.ingest_snapshot(self.conn, self.cfg, [self.item],
                            scan_start='2026-10-20', scan_end='2026-11-20')
        return events.between(self.conn, '2026-10-21', '2026-10-21')[0]

    def test_calendar_import_and_wiki_grouping_cannot_reuse_ended_workshop(self):
        event = self.ingest()
        wiki.link_series(self.conn, self.cfg.wiki_dir)
        saved = events.get(self.conn, event.key)
        self.assertIsNone(saved.series)
        self.assertIsNone(saved.location)
        self.assertIsNone(saved.join_url)
        self.assertEqual(series.get(self.conn, 'workshop').status, 'ended')

    def test_explicit_model_reference_outside_schedule_lifetime_is_ignored(self):
        event = self.ingest()
        apply_stage._apply_event(self.conn, {'key': event.key, 'title': 'workshop',
            'date': '2026-10-21', 'series': 'workshop'}, source='person:Sam', written_by='dream:nightly')
        self.assertIsNone(events.get(self.conn, event.key).series)

    def test_historical_occurrence_can_still_keep_its_real_series_details(self):
        event, _ = events.upsert(self.conn, {'title': 'workshop', 'date': '2026-08-25',
                                            'series': 'workshop'}, match=False)
        self.assertEqual(event.series, 'workshop')
        self.assertEqual(event.join_url, 'https://meet.example/old-workshop')

    def test_new_plan_just_after_the_end_does_not_move_a_recent_observed_occurrence(self):
        old, _ = events.upsert(self.conn, {'title': 'workshop', 'date': '2026-09-08',
            'series': 'workshop', 'source': 'ical:created:Home'}, written_by='ical', match=False)
        new, verb = events.upsert(self.conn, {'title': 'workshop', 'date': '2026-09-12'})
        self.assertEqual(verb, 'inserted')
        self.assertNotEqual(old.id, new.id)
        self.assertIsNone(new.series)
        self.assertIsNone(new.join_url)
        self.assertEqual(events.get(self.conn, old.key).date, '2026-09-08')

    def test_future_effective_schedule_cannot_lend_to_an_earlier_appointment(self):
        series.upsert(self.conn, {'slug': 'workshop', 'title': 'Workshop',
            'effective_on': '2026-11-01', 'location': 'Future room'})
        event, _ = events.upsert(self.conn, {'title': 'Workshop', 'date': '2026-10-21'})
        self.assertIsNone(event.series)
        self.assertIsNone(event.location)

    def test_user_correction_survives_calendar_reingestion_old_dream_and_wiki_replay(self):
        event = self.ingest()
        # Represent the persisted state produced by the old inheritance implementation.
        self.conn.execute("UPDATE events SET series='workshop', location='Telehealth',"
                          " join_url='https://meet.example/old-workshop', participants='[\"Sam\"]'"
                          " WHERE id=?", (event.id,))
        self.conn.commit()
        correction = archive.append(self.conn, channel='agent', external_id='user-correction',
            ts=db.now(), text='This appointment is with Mom at the new clinic, not Sam or the old workshop.',
            from_me=True, person='me', addressed_to='machine')
        self.conn.commit()
        live.update_event(self.conn, self.cfg, event.key, remove_participants=['Sam'],
            add_participants=['Mom'], location='New clinic', series='', join_url='',
            origin=live.Origin.of('test', [correction]))
        old_id = archive.append(self.conn, channel='imessage', external_id='old-invitation',
            ts='2026-10-08T13:00:00-04:00', text='I might come, depending on my exam.',
            person='Sam', thread='sam', handle='+111')
        self.conn.commit()
        old = self.conn.execute('SELECT * FROM archive WHERE id=?', (old_id,)).fetchone()
        bundle = Bundle(entity='person:Sam', items=[old])
        diff = {'events': [{'key': event.key, 'title': 'workshop', 'date': '2026-10-21',
            'participants': ['Sam'], 'location': 'Telehealth', 'series': 'workshop',
            'cite_ids': [old_id], 'field_cite_ids': {'participants': [old_id],
                                                   'location': [old_id], 'series': [old_id]}}]}
        apply_stage.apply_diffs(self.conn, self.cfg, [(bundle, diff, '')], written_by='dream:nightly')
        wiki.link_series(self.conn, self.cfg.wiki_dir)
        self.ingest()
        saved = events.get(self.conn, event.key)
        self.assertEqual(saved.participants, ['Mom'])
        self.assertEqual(saved.location, 'New clinic')
        self.assertIsNone(saved.series)
        self.assertIsNone(saved.join_url)
        self.assertEqual(len(events.between(self.conn, '2026-10-21', '2026-10-21')), 1)


class TestOpenUsesOnlyRecordedCitations(Base):
    def test_no_bundle_fallback_or_uncited_neighbours(self):
        event, _ = events.upsert(self.conn, {'title': 'Appointment', 'date': self.d(1)})
        cited = archive.append(self.conn, channel='imessage', external_id='cited', ts=db.now(),
            text='The appointment is with Mom.', thread='thread', person='Mom')
        uncited = archive.append(self.conn, channel='imessage', external_id='uncited', ts=db.now(),
            text='Unrelated chatter.', thread='thread', person='Sam')
        trace.stamp(self.conn, kind='event', ref=event.key, verb='created', archive_ids=[cited])
        rows = trace.source_rows(self.conn, 'event', event.key, cited_only=True)
        self.assertEqual([r['id'] for r in rows], [cited])
        self.assertNotIn(uncited, [r['id'] for r in rows])

    def test_legacy_bundle_is_not_presented_as_citations(self):
        from memcal import detail
        event, _ = events.upsert(self.conn, {'title': 'Appointment', 'date': self.d(1)})
        line = archive.append(self.conn, channel='imessage', external_id='legacy', ts=db.now(),
            text='Uncited legacy bundle contents.', thread='legacy', person='Sam')
        archive.spool_add(self.conn, line, 'person:Sam')
        self.conn.execute("UPDATE spool SET run_id=1 WHERE entity='person:Sam'")
        self.conn.execute("INSERT INTO provenance(kind,ref,verb,entity,stage,run_id,at)"
                          " VALUES('event',?,'inserted','person:Sam','propose',1,?)", (event.key, db.now()))
        self.conn.commit()
        self.assertTrue(trace.source_rows(self.conn, 'event', event.key))
        opened = detail.open_handle(self.conn, self.cfg, f'E{event.id}')
        self.assertIn('no citations recorded', opened)
        self.assertNotIn('Uncited legacy bundle contents', opened)


if __name__ == '__main__':
    unittest.main()
