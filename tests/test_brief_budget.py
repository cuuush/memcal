"""The monthly brief spends its budget on upcoming plans before ended ones."""
import unittest
from unittest import mock

from memcal import brief, db, events, textclean
from tests._support import Base


class TestUpcomingVisitSurvivesACrowdedMonth(Base):
    def setUp(self):
        super().setUp()
        db.set_today("2026-10-08")

    def add(self, day, title, **fields):
        return events.upsert(self.conn, {"date": day, "title": title, **fields},
                             match=False).event

    def test_recent_past_and_distant_plans_do_not_hide_an_upcoming_visit(self):
        past = [self.add(f"2026-10-{day:02d}", f"Completed meeting {i}")
                for i, day in enumerate([5, 6, 7, 7, 7])]
        for day in [8, 9, 10, 12, 14, 17, 19, 21, 22, 24, 25, 29]:
            self.add(f"2026-10-{day:02d}", f"Shared plan {day}", participants=["Alex"])
        visit = self.add("2026-10-16", "Visit Casey", kind="availability",
                         subject="Rowan Vale", participants=["Rowan Vale"], status="tentative")
        for day in [1, 5, 10, 15, 20]:
            self.add(f"2026-12-{day:02d}", f"Distant plan {day}", status="confirmed")

        def hint(_conn, _cfg, event):
            return brief.FRESHNESS_HINT_FORMAT.format(
                where="chat/friends", count="99+", extra="", handle=f"E{event.id}")

        with mock.patch.object(brief, "_freshness_hint", side_effect=hint), \
             mock.patch.object(brief, "MAX_HINTS_PER_BLOCK", 100):
            full = brief.render(self.conn, self.cfg)
        self.assertLessEqual(textclean.estimate_tokens(full), self.cfg.brief_token_cap)
        self.assertIn(brief.source_tag("event", visit.id), full)
        self.assertIn("Rowan Vale", full)
        self.assertIn(f"memcal_open(ref=E{visit.id})", full)
        self.assertTrue(any(brief.source_tag("event", e.id) not in full for e in past))
        self.assertIn("known events omitted", full)
        self.assertNotIn("[complete for", full)

    def test_an_ongoing_visit_that_started_in_the_past_is_retained(self):
        visit = self.add("2026-10-01", "Ongoing visit", until="2026-10-20")
        for i in range(12):
            self.add("2026-10-07", f"Past meeting {i} " + "long description " * 8)
        text = brief.render(self.conn, self.cfg)
        self.assertIn(brief.source_tag("event", visit.id), text)

    def test_past_parent_with_future_child_is_not_removed_as_a_past_unit(self):
        lines = ['## Upcoming', '〔E1〕 · Tue Oct 6 "Trip"',
                 '  ↳ 〔E2〕 Fri Oct 16 "Dinner"', '〔E3〕 · Wed Oct 7 "Meeting"']
        self.assertEqual(brief._dropped_index(lines, past_ids={1, 3}), (3, 4))


if __name__ == "__main__":
    unittest.main()
