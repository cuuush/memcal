"""Clock pins preserve instants independently of the host clock and zone."""
import os
import time
import unittest
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

from memcal import db


class TestPinnedClockTimezone(unittest.TestCase):
    def setUp(self):
        self.addCleanup(db.set_today, None)
        self.zone = mock.patch.dict(os.environ, {"TZ": "UTC"})
        self.zone.start()
        if hasattr(time, "tzset"):
            time.tzset()
        self.addCleanup(self._restore_zone)

    def _restore_zone(self):
        self.zone.stop()
        if hasattr(time, "tzset"):
            time.tzset()

    def test_offset_pin_preserves_eastern_instant_on_utc_host(self):
        for pin in ("2026-09-09T18:20:00-04:00", "2026-12-09T18:20:00-05:00"):
            with self.subTest(pin=pin):
                db.set_today(pin)
                self.assertEqual(db.now(), pin)
                self.assertEqual(db.now_dt().timestamp(), datetime.fromisoformat(pin).timestamp())

    def test_environment_pin_preserves_offset(self):
        db.set_today(None)
        pin = "2026-09-09T18:20:00-04:00"
        with mock.patch.dict(os.environ, {"MEMCAL_TODAY": pin}):
            self.assertEqual(db.now(), pin)

    @unittest.skipUnless(hasattr(time, "tzset"), "requires process timezone configuration")
    def test_naive_eastern_pin_uses_dst_on_pinned_date(self):
        os.environ["TZ"] = "America/New_York"
        time.tzset()
        for pin, offset in (("2026-01-09T18:20:00", -5), ("2026-07-09T18:20:00", -4)):
            with self.subTest(pin=pin):
                db.set_today(pin)
                self.assertEqual(db.now_dt().utcoffset(), timedelta(hours=offset))
                self.assertEqual(db.now_dt().replace(tzinfo=None), datetime.fromisoformat(pin))

    def test_aware_pin_preserves_dst_fold(self):
        for fold, offset in ((0, -4), (1, -5)):
            with self.subTest(fold=fold):
                pin = datetime(2026, 11, 1, 1, 30, tzinfo=ZoneInfo("America/New_York"), fold=fold)
                db.set_today(pin)
                self.assertEqual(db.now_dt().timestamp(), pin.timestamp())
                self.assertEqual(db.now_dt().utcoffset(), timedelta(hours=offset))


if __name__ == "__main__":
    unittest.main()
