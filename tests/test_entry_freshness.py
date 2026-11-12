"""Freshness nominations belong to an entry, not an entire conversation."""

import unittest

from memcal import activity, brief, db, live
from tests.test_daytime_part4 import _Base
from tests.test_daytime_part6 import _Base as LifecycleBase, ModelSub, Scripted, _aid


class TestEntrySpecificFreshness(_Base):
    def plan(self, title, when="2026-09-13", eid="source"):
        source = self.collect("imessage", eid, f"{title} on {when}", "friends",
                              ts="2026-09-01T10:00:00-04:00")
        event, _ = live.add_event(self.conn, self.cfg, title=title, when=when,
                                  origin=live.Origin.of("test", cited=[source]))
        return event

    def nominated(self, event):
        return [item["id"] for item in activity.event_pending(
            self.conn, event, limit=-1)["strong"]]

    def test_chatter_does_not_make_an_entry_stale_or_acknowledge_review(self):
        event = self.plan("Juniper dinner")
        before = activity.reviewed_ids(self.conn, "event", event.key)
        ids = [self.collect("imessage", f"c{i}", text, "friends") for i, text in enumerate(
            ["lol", "this new phone case is great", "I emailed you about the bot"])]
        text = brief.render(self.conn, self.cfg)
        self.assertIn("Juniper dinner", text)
        self.assertNotIn("New activity:", text)
        self.assertEqual(self.nominated(event), [])
        self.assertEqual(activity.reviewed_ids(self.conn, "event", event.key), before)
        self.assertEqual([item["id"] for item in activity.read(
            self.conn, "event", event.key)["items"]], ids)
        self.assertIn("coverage incomplete", text)

    def test_unrelated_plan_on_the_same_day_does_not_flag_entry(self):
        event = self.plan("Juniper dinner", when="2026-09-12")
        self.collect("imessage", "work", "I work Saturday", "friends")
        self.assertEqual(self.nominated(event), [])
        self.assertNotIn("New activity:", brief.render(self.conn, self.cfg))

    def test_topic_selects_one_of_two_entries_in_the_same_conversation(self):
        dinner = self.plan("Juniper dinner", eid="dinner")
        hike = self.plan("Cedar hike", eid="hike")
        change = self.collect("imessage", "change", "Cedar hike is cancelled", "friends")
        self.assertEqual(self.nominated(dinner), [])
        self.assertEqual(self.nominated(hike), [change])
        text = brief.render(self.conn, self.cfg)
        lines = text.splitlines()
        dinner_line = next(i for i, line in enumerate(lines) if self.handle(dinner) in line)
        hike_line = next(i for i, line in enumerate(lines) if self.handle(hike) in line)
        self.assertFalse(lines[dinner_line + 1].startswith("  ↳ New activity:"))
        self.assertTrue(lines[hike_line + 1].startswith("  ↳ New activity:"))

    def test_date_distinguishes_occurrences_with_the_same_title(self):
        first = self.plan("Cedar hike", when="2026-09-13", eid="first")
        second = self.plan("Cedar hike", when="2026-09-20", eid="second")
        change = self.collect("imessage", "change", "Cedar hike on September 20 is cancelled",
                              "friends")
        self.assertEqual(self.nominated(first), [])
        self.assertEqual(self.nominated(second), [change])

    def test_relative_date_uses_source_time_across_a_clock_rollover(self):
        self.addCleanup(db.set_today, None)
        first = self.plan("Cedar hike", when="2026-09-13", eid="first")
        second = self.plan("Cedar hike", when="2026-09-20", eid="second")
        change = self.collect("imessage", "change", "Cedar hike next Sunday is cancelled",
                              "friends", ts="2026-09-12T10:00:00-04:00")
        for today in ("2026-09-12", "2026-09-13"):
            db.set_today(today)
            self.assertEqual(self.nominated(first), [])
            self.assertEqual(self.nominated(second), [change])

    def test_date_can_target_a_generic_title_without_matching_another_days_plan(self):
        first = self.plan("Dinner", when="2026-09-13", eid="first")
        second = self.plan("Dinner", when="2026-09-20", eid="second")
        change = self.collect("imessage", "change", "Dinner on September 20 is cancelled",
                              "friends")
        self.assertEqual(self.nominated(first), [])
        self.assertEqual(self.nominated(second), [change])

    def test_ambiguous_cancellation_does_not_flag_every_entry(self):
        dinner = self.plan("Juniper dinner", eid="dinner")
        hike = self.plan("Cedar hike", eid="hike")
        self.collect("imessage", "change", "cancelled", "friends")
        self.assertEqual(self.nominated(dinner), [])
        self.assertEqual(self.nominated(hike), [])
        self.assertIn("coverage incomplete", brief.render(self.conn, self.cfg))

    def test_relevant_message_beyond_noisy_prefix_is_counted_alone_and_replay_is_quiet(self):
        event = self.plan("Juniper dinner")
        for i in range(120):
            self.collect("imessage", f"c{i}", "lol, nice photo", "friends")
        change = self.collect("imessage", "change", "Juniper dinner moved to Monday", "friends")
        self.assertEqual(self.nominated(event), [change])
        text = brief.render(self.conn, self.cfg)
        self.assertIn("1 message(s) since this plan was reviewed", text)
        self.assertNotIn("99+", text)
        page = activity.read(self.conn, "event", event.key, limit=1, relevant_only=True)
        self.assertEqual([item["id"] for item in page["items"]], [change])
        self.assertEqual((page["total"], page["omitted"]), (1, 0))
        from memcal import mcp_server
        server = mcp_server.Server.__new__(mcp_server.Server)
        server.conn, server.cfg = self.conn, self.cfg
        server.harness, server.session = "test", ""
        tool_text = server.call("memcal_activity", {"handle": self.handle(event)})
        self.assertIn("Juniper dinner moved to Monday", tool_text)
        self.assertNotIn("nice photo", tool_text)
        self.collect("imessage", "change", "Juniper dinner moved to Monday", "friends")
        self.assertEqual(self.nominated(event), [change])
        live.update_event(self.conn, self.cfg, event.key, when="2026-09-14",
                          origin=live.Origin.of("test", cited=[change]))
        self.assertEqual(self.nominated(event), [])
        self.assertNotIn("New activity:", brief.render(self.conn, self.cfg))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM events").fetchone()[0], 1)

    def test_short_followup_uses_recent_unambiguous_entry_context(self):
        source = self.collect("imessage", "source", "Cedar hike tomorrow", "friends")
        event, _ = live.add_event(self.conn, self.cfg, title="Cedar hike", when="tomorrow",
                                  origin=live.Origin.of("test", cited=[source]))
        followup = self.collect("imessage", "followup", "is it still at 8?", "friends")
        self.assertEqual(self.nominated(event), [followup])


class TestEntryFreshnessLifecycle(LifecycleBase):
    def test_collect_correct_dream_and_replay_preserve_entry_and_unrelated_plan(self):
        self.at("2026-09-12T08:00:00")
        self.tick(Scripted([[("founding", "Poker Sunday at 5 Oak")]]), due=False)
        self.night(ModelSub([("Poker Sunday", lambda b, i: {
            "title": "Poker", "date": "2026-09-13", "location": "5 Oak",
            "status": "confirmed", "cite_ids": [_aid(b, i)]})]))
        event = self.conn.execute("SELECT id, key FROM events").fetchone()
        other, _ = live.add_event(self.conn, self.cfg, title="Cedar hike", when="2026-09-13",
                                  origin=live.Origin.of("test"))
        self.at("2026-09-12T11:00:00")
        self.tick(Scripted([[("chatter", "I emailed you about the bot")]]), due=False)
        self.assertNotIn("New activity:", brief.render(self.conn, self.cfg))
        self.at("2026-09-12T12:00:00")
        self.tick(Scripted([[("correction", "Poker moved to Monday at 7 Elm")]]), due=False)
        self.assertIn("New activity:", brief.render(self.conn, self.cfg))
        correction = self.conn.execute(
            "SELECT id FROM archive WHERE external_id='correction'").fetchone()[0]
        live.update_event(self.conn, self.cfg, event["key"], when="2026-09-14", location="7 Elm",
                          origin=live.Origin.of("test", cited=[correction]))
        self.at("2026-09-13T03:00:00")
        self.night(ModelSub([]))
        self.tick(Scripted([[("correction", "Poker moved to Monday at 7 Elm")]]), due=False)
        self.assertNotIn("New activity:", brief.render(self.conn, self.cfg))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM events").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT id, date, location FROM events WHERE key=?",
                                          (event["key"],)).fetchone()[:],
                         (event["id"], "2026-09-14", "7 Elm"))
        self.assertEqual(self.conn.execute("SELECT title, date FROM events WHERE key=?",
                                          (other.key,)).fetchone()[:],
                         ("Cedar hike", "2026-09-13"))


if __name__ == "__main__":
    unittest.main()
