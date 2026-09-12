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


def _item(uid, title, days, *, calendar="Calendar", writable=True):
    start = datetime.combine(db.today() + timedelta(days=days),
                             datetime.min.time(), tzinfo=timezone.utc).replace(hour=19)
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
