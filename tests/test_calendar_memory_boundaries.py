"""Calendar copies share lifecycle; personal imports stay out of memory views."""

import unittest
from datetime import datetime

from tests._support import Base
from memcal import brief, db, events, web_memory
from memcal.sources import ical


class TestDeletingOneCalendarCopyDoesNotCancelTheOccasion(Base):
    def setUp(self):
        super().setUp()
        db.set_today('2026-11-04T12:00:00-05:00')

    def item(self, uid, *, calendar='Home', title='Lakeside vacation'):
        return {'calendar_name': calendar, 'calendar_uid': calendar, 'writable': True,
                'uid': uid, 'title': title,
                'start': datetime(2026, 11, 5).astimezone().isoformat(),
                'end': datetime(2026, 11, 9).astimezone().isoformat(), 'all_day': True,
                'location': '', 'description': '', 'url': ''}

    def scan(self, items):
        return ical.ingest_snapshot(self.conn, self.cfg, items,
            scan_start='2026-11-01', scan_end='2026-12-01')

    def age(self):
        self.conn.execute("UPDATE calendar_items SET last_seen_at='2026-11-03T12:00:00-05:00'")
        self.conn.commit()

    def test_new_uid_in_another_calendar_preserves_the_same_weekend_and_replay(self):
        old = self.item('old-copy')
        self.scan([old])
        trip = events.window(self.conn, 0, 10)[0]
        self.age()
        new = self.item('new-copy', calendar='Shared')
        for _ in range(2):
            self.scan([new])
            saved = events.get(self.conn, trip.key)
            self.assertEqual(saved.status, 'confirmed')
            self.assertEqual(saved.until, '2026-11-08')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM events').fetchone()[0], 1)
        self.assertEqual([r[0] for r in self.conn.execute(
            'SELECT active FROM calendar_items ORDER BY event_uid')], [1, 0])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM archive WHERE text LIKE '%no longer on their calendar%'").fetchone()[0], 0)

    def test_removing_all_copies_declines_once_and_other_events_stay_active(self):
        self.scan([self.item('copy-a'), self.item('copy-b', calendar='Shared')])
        trip = events.window(self.conn, 0, 10)[0]
        self.age()
        other = self.item('other', title='Unrelated appointment')
        self.scan([other])
        self.assertEqual(events.get(self.conn, trip.key).status, 'declined')
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM archive WHERE text LIKE '%no longer on their calendar%'").fetchone()[0], 1)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM events WHERE title='Unrelated appointment'").fetchone()[0], 'confirmed')


class TestPersonalCalendarImportsAreInternalContext(Base):
    def setUp(self):
        super().setUp()
        db.set_today('2026-11-04T12:00:00-05:00')
        self.personal = self.save('Personal dinner', source='ical:created:Home')
        self.plan = self.save('Dinner with Aster', source='person:Aster')
        self.subscription = self.save('Public concert', source='ical:subscribed:Shows')
        self.conn.commit()

    def save(self, title, *, source, **fields):
        return events.upsert(self.conn, {'title': title, 'date': '2026-11-05',
            'status': 'confirmed', 'participants': ['Aster'], 'location': 'Lantern Cafe',
            'source': source, **fields}, match=False).event

    def test_brief_hides_personal_calendar_import_but_keeps_learned_plans_and_subscriptions(self):
        text = brief.render(self.conn, self.cfg)
        self.assertNotIn(self.personal.title, text)
        self.assertIn(self.plan.title, text)
        self.assertIn(self.subscription.title, text)
        self.assertNotIn(self.personal.key, brief.represented_keys(self.conn, self.cfg))

    def test_web_lists_filter_before_counting_and_paging_including_facets(self):
        for kwargs in ({}, {'scope': 'upcoming'}, {'person': 'Aster'},
                       {'location': 'Lantern Cafe'}):
            with self.subTest(kwargs=kwargs):
                out = web_memory.event_list(self.conn, self.cfg, **kwargs)
                self.assertEqual(out['total'], 2)
                self.assertEqual({r['key'] for r in out['events']},
                                 {self.plan.key, self.subscription.key})
        first = web_memory.event_list(self.conn, self.cfg, limit=1)
        second = web_memory.event_list(self.conn, self.cfg, limit=1, offset=first['next_offset'])
        self.assertIsNone(second['next_offset'])
        self.assertEqual(len(first['events']) + len(second['events']), 2)

    def test_import_is_still_available_for_matching_and_direct_detail_after_enrichment(self):
        events.upsert(self.conn, {'key': self.personal.key, 'title': self.personal.title,
            'date': self.personal.date, 'source': 'person:Aster'}, written_by='cli')
        self.assertNotIn(self.personal.title, brief.render(self.conn, self.cfg))
        self.assertEqual(events.get(self.conn, self.personal.key).origin, 'ical:created:Home')
        self.assertEqual(events.get(self.conn, self.personal.key).status, 'confirmed')
        self.assertEqual(self.conn.execute('SELECT count(*) FROM events').fetchone()[0], 3)

    def test_later_and_nested_personal_imports_are_also_hidden(self):
        later = self.save('Later personal appointment', source='ical:created:Home', date='2026-12-10')
        child = self.save('Nested personal appointment', source='ical:created:Home', part_of=self.plan.id)
        text = brief.render(self.conn, self.cfg)
        self.assertNotIn(later.title, text)
        self.assertNotIn(child.title, text)
        self.assertNotIn(child.key, brief.represented_keys(self.conn, self.cfg))


if __name__ == '__main__':
    unittest.main()
