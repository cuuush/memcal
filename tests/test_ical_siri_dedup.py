"""iCal/Siri shadow dedup: exact title+date corroborates, fuzzy stays separate.

Run: python3 -m unittest tests.test_ical_siri_dedup -v
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

try:
    from tests._support import Base
except ModuleNotFoundError:  # Direct execution: python3 tests/test_ical_siri_dedup.py
    from _support import Base

from memcal import db, events
from memcal.sources import ical


def _item(uid, title, days, *, calendar="Calendar", writable=True, hour=19):
    start = datetime.combine(db.today() + timedelta(days=days),
                             datetime.min.time(), tzinfo=timezone.utc).replace(hour=hour)
    return {
        "calendar_name": calendar,
        "calendar_uid": f"cal-{calendar.lower().replace(' ', '-')}",
        "writable": writable, "uid": uid, "title": title,
        "start": start.isoformat(), "end": (start + timedelta(hours=1)).isoformat(),
        "all_day": False, "location": "", "description": "", "url": "",
    }


def _snapshot(self, items):
    return ical.ingest_snapshot(
        self.conn, self.cfg, items,
        scan_start=(db.today() - timedelta(days=120)).isoformat(),
        scan_end=(db.today() + timedelta(days=365)).isoformat())


class TestSiriShadowCopyJoinsRealCalendarRow(Base):
    """Different Apple UIDs, identical title+date: one real, one Siri shadow."""

    def test_two_uids_same_title_and_date_make_one_row_with_both_linked(self):
        db.set_today("2026-08-01")
        _snapshot(self, [
            _item("real-1", "Dinner with Sam", 3, calendar="Calendar", writable=True),
            _item("siri-1", "Dinner with Sam", 3,
                  calendar="Siri Suggestions", writable=False),
        ])
        rows = [r for r in events.window(self.conn, 0, 10)
                if r.title == "Dinner with Sam"]
        self.assertEqual(len(rows), 1, "shadow copy must corroborate, not duplicate")
        linked = self.conn.execute(
            "SELECT identity, event_key FROM calendar_items WHERE event_key = ?",
            (rows[0].key,)).fetchall()
        self.assertEqual(len(linked), 2, "both calendar identities link to the one row")
        self.assertEqual({r["event_key"] for r in linked}, {rows[0].key})


class TestSameIdentityResyncUpdatesInPlace(Base):
    """True re-syncs of the same uid+occurrence keep the explicit-key fast path."""

    def test_resyncing_the_same_identity_never_duplicates(self):
        db.set_today("2026-08-01")
        item = _item("uid-9", "Dentist", 4)
        _snapshot(self, [item])
        first = [r for r in events.window(self.conn, 0, 10) if r.title == "Dentist"]
        self.assertEqual(len(first), 1)
        _snapshot(self, [dict(item)])
        again = [r for r in events.window(self.conn, 0, 10) if r.title == "Dentist"]
        self.assertEqual(len(again), 1, "re-sync updates in place")
        self.assertEqual(again[0].key, first[0].key)
        self.assertEqual(
            self.conn.execute("SELECT count(*) AS n FROM calendar_items"
                              "  WHERE event_uid = 'uid-9'").fetchone()["n"], 1)


class TestSameTitleDifferentTimesStaySeparate(Base):
    """Two occasions can share a title on one day. A different clock time is
    proof they are distinct, not a Siri shadow to fold together."""

    def test_same_title_and_date_but_different_hours_make_two_rows(self):
        db.set_today("2026-08-01")
        _snapshot(self, [
            _item("morning-1v1", "1:1", 3, hour=10),
            _item("afternoon-1v1", "1:1", 3, hour=15),
        ])
        rows = [r for r in events.window(self.conn, 0, 10) if r.title == "1:1"]
        self.assertEqual(len(rows), 2,
                         "a 10:00 and a 15:00 meeting are two occasions, not one")
        self.assertEqual({r.time for r in rows}, {"10:00", "15:00"})

    def test_a_timeless_entry_still_corroborates_a_timed_one(self):
        # A missing time on either side has nothing to disagree on, so the exact
        # title+date signal still folds the Siri shadow onto the real row.
        db.set_today("2026-08-01")
        real = _item("real-blk", "Focus block", 4, hour=9)
        shadow = _item("siri-blk", "Focus block", 4,
                       calendar="Siri Suggestions", writable=False)
        shadow["all_day"], shadow["start"], shadow["end"] = True, "", ""
        _snapshot(self, [real, shadow])
        rows = [r for r in events.window(self.conn, 0, 10) if r.title == "Focus block"]
        self.assertEqual(len(rows), 1, "a timeless shadow still corroborates")


class TestNearDuplicateTitlesStaySeparate(Base):
    """Fuzzy/inexact pairs must still mint separate rows (no over-merge)."""

    def test_close_but_different_titles_stay_two_rows(self):
        db.set_today("2026-08-01")
        _snapshot(self, [
            _item("a-1", "deep block", 3),
            _item("a-2", "light block", 3),
            _item("b-1", "League gaming", 4),
            _item("b-2", "League or CS2 gaming", 4),
            _item("c-1", "Elements", 5),
            _item("c-2", "Breakfast at Elements", 5),
        ])
        titles = [r.title for r in events.window(self.conn, 0, 10)]
        self.assertEqual(len([t for t in titles if t in ("deep block", "light block")]), 2)
        self.assertEqual(
            len([t for t in titles if t in ("League gaming", "League or CS2 gaming")]), 2)
        self.assertEqual(
            len([t for t in titles if t in ("Elements", "Breakfast at Elements")]), 2,
            '"Elements" must never absorb into "Breakfast at Elements"')


if __name__ == "__main__":
    unittest.main()
