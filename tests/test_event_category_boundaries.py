"""Public listings cannot silently replace personal plans sharing a title."""

import unittest

from tests._support import Base
from tests.test_merge import FakeClient
from memcal import archive, db, events, live
from memcal.dream import apply as apply_stage, merge
from memcal.dream.bundle import Bundle


class TestListingAndCommitmentIdentity(Base):
    def setUp(self):
        super().setUp()
        db.set_today('2026-09-01T09:00:00')

    def plan(self, time="18:00", location="Studio A"):
        return live.add_event(self.conn, self.cfg, title='Pottery',
            when='2026-09-04', time=time, location=location,
            participants=['Aster'], status='confirmed', kind='commitment').event

    def proposals(self, title="Pottery social", time="12:00", location="Hall B"):
        aid = archive.append(self.conn, channel='email', external_id='listing-1',
            ts='2026-09-01T10:00:00', thread='listings', handle='bulletin@example.test',
            person='Arts bulletin', text=f'Public {title} on September 4 at {time} in {location}.')
        item = self.conn.execute("SELECT * FROM archive WHERE external_id='listing-1'").fetchone()
        aid = item['id']
        return [(Bundle(entity='person:Arts bulletin', items=[item]),
            {'events': [{'title': title, 'date': '2026-09-04',
                'time': time, 'location': location, 'kind': 'opportunity',
                'status': 'mentioned', 'cite_ids': [aid]}]}, 'synthetic')]

    def check_listing(self, time, location):
        plan = self.plan(time, location)
        for _ in range(2):
            proposals = self.proposals()
            client = FakeClient(answer={'same_event': False})
            proposals, _ = merge.merge_all(client, self.cfg, proposals, conn=self.conn)
            apply_stage.apply_diffs(self.conn, self.cfg, proposals, written_by='dream:nightly')
        saved = events.get(self.conn, plan.key)
        self.assertEqual((saved.title, saved.time, saved.location, saved.status),
                         ('Pottery', time, location, 'confirmed'))
        rows = events.between(self.conn, '2026-09-04', '2026-09-04')
        self.assertEqual(len(rows), 2)
        listing = next(e for e in rows if e.key != plan.key)
        self.assertEqual((listing.kind, listing.status, listing.location),
                         ('opportunity', 'mentioned', 'Hall B'))

    def test_conflicting_time_and_place_preserve_plan_on_replay(self):
        self.check_listing('18:00', 'Studio A')

    def test_conflicting_place_preserves_plan_on_replay(self):
        self.check_listing('12:00', 'Studio A')

    def test_conflicting_time_preserves_plan_on_replay(self):
        self.check_listing('18:00', 'Hall B')

    def test_venue_token_containment_cannot_bypass_review(self):
        plan = self.plan()
        proposals = self.proposals(title='Pottery', time='18:00', location='Studio B')
        client = FakeClient(answer={'same_event': False})
        proposals, _ = merge.merge_all(client, self.cfg, proposals, conn=self.conn)
        self.assertEqual(len(client.calls), 1)
        apply_stage.apply_diffs(self.conn, self.cfg, proposals, written_by='dream:nightly')
        self.assertEqual(events.get(self.conn, plan.key).location, 'Studio A')
        self.assertEqual(len(events.between(self.conn, '2026-09-04', '2026-09-04')), 2)

    def test_listing_attendance_cannot_bind_to_a_private_plan_at_another_venue(self):
        plan = self.plan()
        for _ in range(2):
            proposals = self.proposals(title='Pottery', time='18:00', location='Studio B')
            row = proposals[0][1]['events'][0]
            row['attendance'] = []
            proposals, _ = merge.merge_all(FakeClient(answer={'same_event': False}),
                                           self.cfg, proposals, conn=self.conn)
            apply_stage.apply_diffs(self.conn, self.cfg, proposals, written_by='dream:nightly')
        stored = events.get(self.conn, plan.key)
        self.assertEqual((stored.location, stored.participants), ('Studio A', ['Aster']))
        rows = events.between(self.conn, '2026-09-04', '2026-09-04')
        self.assertEqual(sorted((e.location, e.kind, e.participants) for e in rows),
                         [('Studio A', 'commitment', ['Aster']), ('Studio B', 'opportunity', [])])

    def test_explicit_identity_still_allows_accepting_a_listing(self):
        listing = events.upsert(self.conn, {'title': 'Workshop', 'date': '2026-09-04',
            'kind': 'opportunity', 'status': 'mentioned'}).event
        result = events.upsert(self.conn, {'key': listing.key, 'date': listing.date,
            'kind': 'commitment', 'status': 'confirmed'}, written_by='cli').event
        self.assertEqual(result.id, listing.id)
        self.assertEqual(result.status, 'confirmed')


if __name__ == '__main__':
    unittest.main()
