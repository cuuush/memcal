"""Source-created precedence and coverage after the final budget cut."""
import unittest

from memcal import brief, db, live, textclean
from memcal.sources import base
from tests.test_daytime_part6 import _Base as LifecycleBase, ModelSub, Scripted, _aid
from tests.test_daytime_freshness_fixes import _Base as ReviewBase


class TestDreamCreationEvidenceOrder(LifecycleBase):
    def test_collected_dream_fact_resists_older_correction_and_replay(self):
        self.at("2026-09-12T11:00:00")
        self.tick(Scripted([[("founding", "Poker Sunday at 5 Oak")]]), due=False)
        self.at("2026-09-12T12:00:00")
        self.night(ModelSub([("Poker Sunday", lambda b, i: {
            "title": "Poker", "date": "2026-09-13", "location": "5 Oak",
            "status": "confirmed", "subject": "me", "cite_ids": [_aid(b, i)]})]))
        event = self.conn.execute("SELECT id, key, location FROM events").fetchone()
        self.assertEqual(event["location"], "5 Oak")

        def collect(eid, ts, text):
            base.deliver(self.conn, base.IngestReport(channel="chat"), channel="chat",
                         external_id=eid, ts=ts, text=text, thread="poker group",
                         handle="friend@example.com")
            self.conn.commit()
            return self.conn.execute("SELECT id FROM archive WHERE external_id=?",
                                     (eid,)).fetchone()["id"]

        older = collect("older", "2026-09-12T10:00:00", "Poker Sunday at 1 Pine")
        before_history = list(self.conn.execute("SELECT * FROM event_history"))
        before_marks = list(self.conn.execute("SELECT * FROM reviewed_lines"))
        for changes in ({"location": "1 Pine"}, {"location": ""}):
            with self.assertRaises(live.LiveError):
                live.update_event(self.conn, self.cfg, event["key"], **changes,
                                  origin=live.Origin.of("test", cited=[older]))
        self.assertEqual(list(self.conn.execute("SELECT * FROM event_history")), before_history)
        self.assertEqual(list(self.conn.execute("SELECT * FROM reviewed_lines")), before_marks)
        newer = collect("newer", "2026-09-12T11:30:00", "Poker Sunday at 7 Elm")
        live.update_event(self.conn, self.cfg, event["key"], location="7 Elm",
                          origin=live.Origin.of("test", cited=[newer]))
        self.night(ModelSub([("1 Pine", lambda b, i: {
            "key": event["key"], "title": "Poker", "date": "2026-09-13",
            "location": "1 Pine", "cite_ids": [_aid(b, i)]})]))
        collect("older", "2026-09-12T10:00:00", "Poker Sunday at 1 Pine")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM events").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT id, location FROM events").fetchone()[:],
                         (event["id"], "7 Elm"))


class TestReconciledCoverageFitsBudget(ReviewBase):
    def test_omitted_later_and_week_events_disclose_backlog_within_cap(self):
        for on in ("2026-10-01", "2026-09-13"):
            with self.subTest(on=on):
                # Separate stores keep event ordering identical in both windows.
                conn = db.open_db(self.cfg.home / (on + ".db"))
                previous = self.conn
                self.conn = conn
                try:
                    for n in range(6):
                        live.add_event(conn, self.cfg, title=f"Outing {n}", when="2026-09-13",
                                       origin=live.Origin.of("test"))
                    event, _ = live.add_event(conn, self.cfg, title="Z poker", when=on,
                                             status="confirmed", origin=live.Origin.of("test"))
                    src = self.collect("chat", "src", "Poker at 5 Oak", "friends")
                    live.update_event(conn, self.cfg, event.key, note="plan",
                                      origin=live.Origin.of("test", cited=[src]))
                    self.collect("chat", "new", "Rooftop party this Sunday?", "friends")
                    removed = False
                    for cap in range(100, 481, 10):
                        self.cfg.brief_token_cap = cap
                        out = brief.render(conn, self.cfg)
                        self.assertLessEqual(textclean.estimate_tokens(out), cap, out)
                        if "New activity: chat/friends" not in out:
                            removed = True
                            self.assertIn("coverage incomplete", out)
                            self.assertNotIn("[UNREVIEWED:", out)
                            self.assertNotIn("[complete for", out)
                    self.assertTrue(removed)
                finally:
                    self.conn = previous
                    conn.close()


if __name__ == "__main__":
    unittest.main()
