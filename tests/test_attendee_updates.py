"""Cited roster corrections survive source, dream, typed edits and replay."""
from datetime import date
import unittest

from tests._support import Base
from memcal import archive, db, events, live, actions
from memcal.dream import apply, bundle, merge, propose


class TestCitedRosterCorrections(Base):
    def setUp(self):
        super().setUp()
        db.set_today(date(2026, 8, 6))
        self.event, _ = events.upsert(self.conn, {
            "title": "Dinner", "date": "2026-08-07", "time": "19:00",
            "participants": ["Jordan", "Robin"], "status": "confirmed"},
            written_by="dream:nightly", evidence_ts="2026-08-05T18:00:00-04:00")

    def source(self, external, stamp, people, *, cited=True, keyed=True):
        aid = archive.append(self.conn, channel="imessage", external_id=external,
            ts=stamp, text="Updated dinner guest list: " + ", ".join(people),
            thread="dinner", person="Jordan", from_me=False,
            gated=True, gate_reason="temporal")
        self.conn.commit()
        item = self.conn.execute("SELECT * FROM archive WHERE id=?", (aid,)).fetchone()
        packet = bundle.Bundle(entity="thread:imessage:dinner", items=[item])
        row = {"title": "Dinner", "date": self.event.date, "participants": people,
               "cites": ["L1"], "field_cites": {"participants": ["L1"]} if cited else {}}
        if keyed:
            row["key"] = self.event.key
        diff = {"events": [row]}
        propose._resolve_cites(packet, diff)
        return packet, diff

    def apply(self, source):
        apply.apply_diffs(self.conn, self.cfg, [source], written_by="dream:nightly")
        return events.get(self.conn, self.event.key)

    def test_withdrawal_replaces_cited_roster_without_cancelling_other_plans(self):
        other, _ = events.upsert(self.conn, {"title": "Call", "date": self.event.date,
            "time": "10:00", "participants": ["Robin"], "status": "confirmed"})
        source = self.source("withdraw", "2026-08-06T18:00:00-04:00", ["Jordan"])
        for _ in range(2):
            current = self.apply(source)
            self.assertEqual(current.participants, ["Jordan"])
            self.assertEqual(current.status, "confirmed")
            self.assertEqual(events.get(self.conn, other.key).participants, ["Robin"])
        history = self.conn.execute("SELECT * FROM event_history WHERE event_id=? "
            "AND field='participants' AND written_by NOT LIKE '%:born'", (self.event.id,)).fetchall()
        self.assertEqual(len(history), 1)

    def test_older_dream_cannot_undo_typed_rejoin_and_newer_source_still_applies(self):
        withdrawal = self.source("withdraw", "2026-08-06T18:00:00-04:00", ["Jordan"])
        self.apply(withdrawal)
        aid = archive.append(self.conn, channel="agent", external_id="rejoin",
            ts="2026-08-06T19:00:00-04:00", text="Robin is joining dinner again.",
            thread="assistant", person="me", from_me=True)
        self.conn.commit()
        live.update_event(self.conn, self.cfg, self.event.key,
            origin=actions.Origin.of("hermes", cited=(aid,)), add_participants=["Robin"])
        self.assertEqual(self.apply(withdrawal).participants, ["Jordan", "Robin"])
        newer = self.source("later", "2026-08-06T20:00:00-04:00", ["Robin"])
        self.assertEqual(self.apply(newer).participants, ["Robin"])
        self.assertEqual(self.apply(withdrawal).participants, ["Robin"])

    def test_partial_or_uncited_roster_remains_additive(self):
        for cited, keyed in ((False, True), (True, False)):
            with self.subTest(cited=cited, keyed=keyed):
                source = self.source(str((cited, keyed)), "2026-08-06T18:00:00-04:00",
                                     ["Jordan"], cited=cited, keyed=keyed)
                self.assertEqual(self.apply(source).participants, ["Jordan", "Robin"])

    def test_cited_empty_roster_can_remove_the_last_guest(self):
        source = self.source("alone", "2026-08-06T18:00:00-04:00", [])
        self.assertEqual(self.apply(source).participants, [])

    def test_merge_does_not_union_a_withdrawn_guest_back_into_the_roster(self):
        source = self.source("withdraw", "2026-08-06T18:00:00-04:00", ["Jordan"])
        partial = bundle.Bundle(entity="person:Jordan"), {"events": [{
            "key": self.event.key, "title": "Dinner", "date": self.event.date,
            "participants": ["Jordan", "Robin"]}]}
        # A second unkeyed fragment is old partial context, not a roster correction.
        existing = merge.Mention(partial[1]["events"][0], partial[0], partial[1], existing=True)
        fresh = merge.Mention(source[1]["events"][0], source[0], source[1])
        row = merge._merge_locally([existing, fresh])
        row["field_cite_ids"] = merge._field_cites([existing, fresh], row)
        merged = source[0], {"events": [row]}
        self.assertEqual(self.apply(merged).participants, ["Jordan"])

    def test_conflicting_fresh_rosters_are_not_blindly_unioned(self):
        sources = [self.source("one", "2026-08-06T18:00:00-04:00", ["Jordan"]),
                   self.source("two", "2026-08-06T19:00:00-04:00", ["Robin"])]
        mentions = [merge.Mention(d["events"][0], b, d) for b, d in sources]
        self.assertTrue(merge._conflicted(mentions))

    def test_partial_fragment_cannot_lose_the_roster_corrections_target(self):
        source = self.source("withdraw", "2026-08-06T18:00:00-04:00", ["Jordan"])
        partial = bundle.Bundle(entity="person:Jordan"), {"events": [{
            "key": None, "title": "Dinner", "date": self.event.date, "participants": ["Jordan"]}]}
        client = type("NoModel", (), {"complete": lambda *a, **k: self.fail("unnecessary model call")})()
        proposals, _ = merge.merge_all(client, self.cfg, [(*partial, "partial"), (*source, "roster")])
        apply.apply_diffs(self.conn, self.cfg, proposals, written_by="dream:nightly")
        self.assertEqual(events.get(self.conn, self.event.key).participants, ["Jordan"])
        self.assertEqual(len(events.search(self.conn, "Dinner")), 1)


if __name__ == "__main__":
    unittest.main()
