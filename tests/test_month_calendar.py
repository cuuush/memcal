"""The web calendar can browse every saved event beyond the bounded brief."""
import unittest
from memcal import db, events, web_memory
from tests._support import Base


class TestWholeCalendarBrowsing(Base):
    def setUp(self):
        super().setUp()
        db.set_today("2026-10-08")
        for title, start, end in (("Old", "2025-01-01", None),
                                  ("Ongoing", "2026-10-01", "2026-10-10"),
                                  ("Nick visit", "2026-10-16", None),
                                  ("Future", "2028-01-01", None)):
            events.upsert(self.conn, {"title": title, "date": start, "until": end,
                                     "participants": ["Nick"]}, match=False)
        self.conn.commit()

    def test_next_thirty_days_includes_ongoing_visits_and_names(self):
        out = web_memory.event_list(self.conn, self.cfg, scope="upcoming")
        self.assertEqual([e["title"] for e in out["events"]], ["Ongoing", "Nick visit"])
        self.assertEqual(out["events"][1]["participants"], ["Nick"])

    def test_every_event_is_reachable_through_pages_including_past_and_future(self):
        found, offset = [], 0
        while offset is not None:
            out = web_memory.event_list(self.conn, self.cfg, limit=1, offset=offset)
            found.extend(e["title"] for e in out["events"])
            self.assertEqual(out["total"], 4)
            offset = out["next_offset"]
        self.assertEqual(found, ["Future", "Nick visit", "Ongoing", "Old"])


if __name__ == "__main__":
    unittest.main()
