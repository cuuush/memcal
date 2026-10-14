"""An arrival observation cannot discard a booking before Calendar publishing."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from memcal import db, events, identity, live, llm, presentation, threads, whois
from memcal.config import Config
from memcal.dream import apply as apply_stage
from memcal.dream import bundle as bundle_stage
from memcal.dream import sweep as sweep_stage
from memcal.sources import ical
from memcal.sources.base import IngestReport, deliver


class CalendarBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.conn.close)
        db.set_today("2026-10-14")
        self.addCleanup(db.set_today, None)
        self.specs = []

    def message(self, key, text, stamp="2026-10-14T08:00:00-04:00"):
        return deliver(self.conn, IngestReport(channel="imessage"), channel="imessage",
                       external_id=key, ts=stamp, thread="+18005551212",
                       handle="+18005551212", text=text)

    def runner(self, cmd, **_kwargs):
        spec = json.loads(cmd[-1])
        self.specs.append(spec)
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
            "calendar": "memcal", "calendar_uid": "C1", "uid": "LUCY-UID"}))

    def cleanup_reply(self, rows):
        class Client:
            def complete(_self, **kwargs):
                return llm.Reply(text="{}", model=kwargs["model"], data={
                    "drop_events": rows, "drop_todos": [], "questions": []})

        return sweep_stage.sweep(Client(), self.conn, self.cfg, [])[1]


class TestBookingSurvivesDuplicateArrival(CalendarBase):
    def test_ingest_typed_confirmation_dream_cleanup_publish_and_replay(self):
        confirmation = self.message("booking", "Lucy daycare is confirmed for October 14 at 7:30 AM.",
                                    "2026-10-12T09:45:00-04:00")
        booking = live.add_event(
            self.conn, self.cfg, title="Lucy daycare", when="2026-10-14", time="07:30",
            kind="commitment", status="confirmed",
            origin=live.Origin.of("test", cited=[confirmation])).event
        arrival = self.message("arrival", "Lucy arrived for her daycare. Pickup at 9:30 PM.")
        rows = [self.conn.execute("SELECT * FROM archive WHERE id=?", (arrival,)).fetchone()]
        bundle = bundle_stage.Bundle(entity="person:Rover notices", items=rows)
        proposal = {"events": [{"title": "Lucy doggy day care", "date": "2026-10-14",
                                  "kind": "observed", "status": "happened", "time": "07:30",
                                  "participants": ["Yiqing"], "cite_ids": [arrival]}]}
        self.cfg.publish_calendar = "memcal"
        for index in range(2):
            apply_stage.apply_diffs(self.conn, self.cfg, [(bundle, proposal)], written_by="dream:ondemand")
            observation = next((e for e in events.between(self.conn, "2026-10-14", "2026-10-14")
                                if e.key != booking.key), None)
            if index == 0:
                self.assertIsNotNone(observation, "the regression must reproduce the duplicate pair")
            # Reproduce the old reply: the model chose the booking as the redundant
            # row. Structured duplicate cleanup must retain its calendar eligibility.
            if observation:
                actions = self.cleanup_reply([{"key": booking.key, "keep_key": observation.key,
                                               "reason": "duplicate"}])
                self.assertTrue(any("merged duplicate" in action for action in actions))
            remaining = events.get(self.conn, booking.key)
            self.assertIsNotNone(remaining)
            self.assertEqual(remaining.kind, "commitment")
            self.assertIn(remaining.status, ("confirmed", "happened"))
            self.assertEqual(self.conn.execute("SELECT count(*) FROM events").fetchone()[0], 1)
            evidence = {r[0] for r in self.conn.execute(
                "SELECT archive_id FROM evidence WHERE kind='event' AND ref=?", (booking.key,))}
            self.assertEqual(evidence, {confirmation, arrival})
            ical.publish_pending(self.conn, self.cfg, runner=self.runner)
        self.assertTrue(self.specs)
        self.assertTrue(all(spec["uid"] == "LUCY-UID" for spec in self.specs[1:]),
                        "reread must update the same calendar identity")
        self.assertEqual(self.specs[0]["title"], "Lucy daycare")
        self.assertEqual(self.conn.execute(
            "SELECT event_key FROM calendar_items WHERE published=1").fetchone()[0], booking.key)
        sent = len(self.specs)
        apply_stage.apply_diffs(self.conn, self.cfg, [(bundle, proposal)], written_by="dream:ondemand")
        ical.publish_pending(self.conn, self.cfg, runner=self.runner)
        self.assertEqual(len(self.specs), sent, "an unchanged source replay must not republish")

    def test_a_duplicate_reply_without_a_survivor_cannot_delete_a_booking(self):
        booking = events.upsert(self.conn, {"title": "Lucy daycare", "date": "2026-10-14",
                                           "kind": "commitment", "status": "confirmed"}).event
        for keep in (None, "missing", booking.key):
            self.cleanup_reply([{"key": booking.key, "keep_key": keep, "reason": "duplicate"}])
            self.assertIsNotNone(events.get(self.conn, booking.key))

    def test_distinct_same_day_appointments_are_not_merged_over_conflicting_times(self):
        one = events.upsert(self.conn, {"title": "Visit", "date": "2026-10-14", "time": "07:30",
                                       "kind": "commitment", "status": "confirmed"}, match=False).event
        two = events.upsert(self.conn, {"key": "second-visit", "title": "Visit", "date": "2026-10-14",
                                       "time": "17:30", "kind": "commitment", "status": "confirmed"},
                            match=False).event
        self.cleanup_reply([{"key": one.key, "keep_key": two.key, "reason": "duplicate"}])
        self.assertIsNotNone(events.get(self.conn, one.key))
        self.assertIsNotNone(events.get(self.conn, two.key))

    def test_cancelled_booking_and_replacement_both_survive(self):
        old = events.upsert(self.conn, {"title": "Lucy daycare", "date": "2026-10-14",
                                       "status": "declined"}, match=False).event
        new = events.upsert(self.conn, {"title": "Lucy daycare", "date": "2026-10-15",
                                       "status": "confirmed"}, match=False).event
        self.cleanup_reply([{"key": old.key, "keep_key": new.key, "reason": "duplicate"}])
        self.assertIsNotNone(events.get(self.conn, old.key))
        self.assertIsNotNone(events.get(self.conn, new.key))

    def test_an_existing_calendar_identity_survives_a_duplicate_merge(self):
        self.cfg.publish_calendar = "memcal"
        original = events.upsert(self.conn, {"title": "Lucy daycare", "date": "2026-10-14",
                                            "status": "confirmed"}, match=False).event
        ical.publish_pending(self.conn, self.cfg, runner=self.runner)
        copy = events.upsert(self.conn, {"title": "Lucy doggy daycare", "date": "2026-10-14",
                                        "status": "confirmed"}, match=False).event
        self.cleanup_reply([{"key": original.key, "keep_key": copy.key, "reason": "duplicate"}])
        ical.publish_pending(self.conn, self.cfg, runner=self.runner)
        self.assertEqual(self.conn.execute("SELECT event_key FROM calendar_items").fetchone()[0], original.key)
        self.assertEqual(len(self.specs), 1)

    def test_finishing_a_published_booking_keeps_calendar_history(self):
        self.cfg.publish_calendar = "memcal"
        booking = events.upsert(self.conn, {"title": "Lucy daycare", "date": "2026-10-14",
                                           "status": "confirmed"}, match=False).event
        ical.publish_pending(self.conn, self.cfg, runner=self.runner)
        events.upsert(self.conn, {"key": booking.key, "date": booking.date,
                                 "kind": "observed", "status": "happened"},
                      written_by="live", match=False)
        self.assertEqual(ical.retract_unpublishable(
            self.conn, self.cfg, dry_run=False,
            runner=lambda *_a, **_kw: self.fail("completed booking must not be retracted")), [])

    def test_a_booking_completed_before_the_first_publish_still_reaches_calendar(self):
        booking = events.upsert(self.conn, {"title": "Lucy daycare", "date": "2026-10-14",
                                           "status": "confirmed"}, written_by="dream:ondemand",
                                evidence_ts="2026-10-12T09:45:00-04:00").event
        events.upsert(self.conn, {"key": booking.key, "date": booking.date,
                                 "kind": "observed", "status": "happened"},
                      written_by="dream:ondemand", evidence_ts="2026-10-14T08:00:00-04:00")
        current = events.get(self.conn, booking.key)
        self.assertEqual((current.kind, current.status), ("commitment", "happened"))
        self.cfg.publish_calendar = "memcal"
        ical.publish_pending(self.conn, self.cfg, runner=self.runner)
        self.assertEqual(len(self.specs), 1)


class TestInferenceBelongsToTheName(CalendarBase):
    def test_agent_and_calendar_formats_are_separate_and_reversible(self):
        name = 'Lucy "daycare"'
        agent = presentation.as_guess(name)
        calendar = presentation.as_guess(name, audience="calendar")
        self.assertEqual(agent, '"Lucy \\"daycare\\"" (inferred from messages)')
        self.assertEqual(calendar, 'Lucy "daycare" (name inferred)')
        for value in (agent, calendar, "maybe: " + name):
            self.assertEqual(presentation.raw_name(value), name)
            self.assertEqual(presentation.as_guess(value), agent)

    def test_bundle_marks_inference_without_changing_its_routing_identity(self):
        self.message("incoming", "Your booking is confirmed.")
        identity.guess_name(self.conn, "+18005551212", "Lucy", channel="imessage")
        bundles = bundle_stage.build(self.conn)
        self.assertEqual(len(bundles), 1)
        rendered = bundles[0].render()
        self.assertIn('"Lucy" (inferred from messages)', rendered)
        self.assertIn("BUNDLE " + bundles[0].entity, rendered)
        self.assertNotIn("inferred", bundles[0].entity)
        self.assertEqual(identity.resolve(self.conn, "+18005551212"), "Lucy")

    def test_confirmation_accepts_the_agent_displayed_name(self):
        identity.guess_name(self.conn, "+18005551212", "Lucy", channel="imessage")
        self.assertEqual(whois.settle_guess(self.conn, presentation.as_guess("Lucy")), "confirmed Lucy")
        self.assertFalse(identity.is_guessed(self.conn, "+18005551212"))

    def test_calendar_title_is_plain_and_notes_refresh_when_a_name_is_confirmed(self):
        identity.guess_name(self.conn, "+18005551212", "Lucy", channel="imessage")
        event = events.upsert(self.conn, {"title": "Lucy daycare", "date": "2026-10-14",
                                         "status": "confirmed", "participants": ["Lucy"]}).event
        self.cfg.publish_calendar = "memcal"
        ical.publish_pending(self.conn, self.cfg, runner=self.runner)
        self.assertEqual(self.specs[-1]["title"], "Lucy daycare")
        self.assertIn("With Lucy (name inferred).", self.specs[-1]["description"])
        self.assertNotIn("inferred from messages", self.specs[-1]["description"])
        whois.settle_guess(self.conn, "Lucy")
        ical.publish_pending(self.conn, self.cfg, runner=self.runner)
        self.assertEqual(len(self.specs), 2)
        self.assertEqual(self.specs[-1]["uid"], "LUCY-UID")
        self.assertIn("With Lucy.", self.specs[-1]["description"])
        self.assertNotIn("inferred", self.specs[-1]["description"])
        ical.publish_pending(self.conn, self.cfg, runner=self.runner)
        self.assertEqual(len(self.specs), 2)


if __name__ == "__main__":
    unittest.main()
