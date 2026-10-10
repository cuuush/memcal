"""Source, dream and typed writes must agree on separate occasion identities."""
from datetime import date
import unittest

from tests._support import Base
from memcal import archive, db, events
from memcal.dream import apply, bundle


class TestDistinctGuestsAndDetailsDoNotCollapse(Base):
    def setUp(self):
        super().setUp()
        db.set_today(date(2026, 8, 5))

    def _source(self, person, clock, venue, external_id):
        aid = archive.append(self.conn, channel="imessage", external_id=external_id,
            ts="2026-08-05T18:00:00-04:00", text=f"Dinner Friday at {clock} at {venue}",
            thread=person, person=person, from_me=False, gated=True, gate_reason="temporal")
        self.conn.commit()
        item = self.conn.execute("SELECT * FROM archive WHERE id=?", (aid,)).fetchone()
        return (bundle.Bundle(entity=f"person:{person}", items=[item]), {"events": [{
            "title": "Dinner", "date": "2026-08-07", "time": clock,
            "location": venue, "participants": [person], "status": "confirmed"}]})

    def test_source_apply_replay_and_typed_correction_keep_two_occasions(self):
        proposals = [self._source("Avery", "18:00", "North Cafe", "north"),
                     self._source("Jordan", "19:00", "South Cafe", "south")]
        for _ in range(2):
            apply.apply_diffs(self.conn, self.cfg, proposals, written_by="dream:nightly")
        rows = events.search(self.conn, "Dinner")
        self.assertEqual(len(rows), 2)
        north = next(ev for ev in rows if ev.participants == ["Avery"])
        south = next(ev for ev in rows if ev.participants == ["Jordan"])
        for _ in range(2):
            changed, _ = events.upsert(self.conn, {"key": north.key, "date": north.date,
                "time": "17:00", "location": "West Cafe"}, written_by="live")
            self.assertEqual(changed.id, north.id)
        self.assertEqual(events.get(self.conn, south.key).time, "19:00")
        self.assertEqual(events.get(self.conn, south.key).location, "South Cafe")
        self.assertEqual(len(events.search(self.conn, "Dinner")), 2)

    def test_partial_group_roster_and_changed_details_update_same_occasion(self):
        first, _ = events.upsert(self.conn, {"title": "Dinner", "date": "2026-08-07",
            "time": "19:00", "location": "South Cafe", "participants": ["Jordan"]})
        group, verb = events.upsert(self.conn, {"title": "Dinner", "date": first.date,
            "time": "17:00", "location": "West Cafe", "participants": ["Jordan", "Robin"]})
        self.assertEqual((group.id, verb), (first.id, "updated"))
        self.assertEqual(len(events.search(self.conn, "Dinner")), 1)

    def test_explicit_key_can_replace_entire_roster_and_details(self):
        first, _ = events.upsert(self.conn, {"title": "Dinner", "date": "2026-08-07",
            "time": "19:00", "location": "South Cafe", "participants": ["Jordan"]})
        replacement, verb = events.upsert(self.conn, {"key": first.key, "date": first.date,
            "time": "17:00", "location": "West Cafe", "participants": ["Avery"]},
            replace_participants=True)
        self.assertEqual((replacement.id, verb), (first.id, "updated"))
        self.assertEqual(replacement.participants, ["Avery"])

    def test_one_changed_detail_from_another_guest_is_not_a_new_occasion(self):
        first, _ = events.upsert(self.conn, {"title": "Dinner", "date": "2026-08-07",
            "time": "19:00", "location": "South Cafe", "participants": ["Jordan"]})
        correction, verb = events.upsert(self.conn, {"title": "Dinner", "date": first.date,
            "time": "20:00", "location": "South Cafe", "participants": ["Robin"]})
        self.assertEqual((correction.id, verb), (first.id, "updated"))


if __name__ == "__main__":
    unittest.main()
