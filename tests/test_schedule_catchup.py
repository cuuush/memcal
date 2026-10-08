"""Scheduled catch-up retries unfinished work without repeating a successful night."""

import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from memcal import db, schedule
from memcal.config import Config


class TestUnfinishedNightRetriesAtStartup(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.cfg = Config(home=Path(temporary.name))
        self.cfg.ensure_dirs()
        self.now = datetime(2026, 10, 8, 8, tzinfo=timezone.utc)
        db.set_today(self.now)
        self.addCleanup(db.set_today, None)
        plist = self.cfg.home / "job.plist"
        plist.write_bytes(plistlib.dumps({"StartCalendarInterval": {
            "Hour": 3, "Minute": 0}}))
        patch = mock.patch.object(schedule, "plist_path", return_value=plist)
        patch.start()
        self.addCleanup(patch.stop)
        self.calls = self.cfg.home / "commands"
        interpreter = self.cfg.home / "fake-python"
        interpreter.write_text('''#!/bin/sh
case "$3" in
    schedule) printf '%s\\n' "$TEST_VERDICT" ;;
    ingest|dream)
        printf '%s\\n' "$3" >> "$MEMCAL_HOME/commands"
        test -f "$MEMCAL_HOME/nightly.pending" || exit 99
        test -f "$MEMCAL_HOME/nightly.last-start" || exit 99
        if [ "$3" = ingest ]; then exit "$TEST_INGEST_EXIT"; fi
        exit "$TEST_DREAM_EXIT"
        ;;
    *) exit 98 ;;
esac
''')
        interpreter.chmod(0o755)
        self.script = schedule.script_path(self.cfg)
        self.script.write_text(schedule.render_script(self.cfg, python=str(interpreter)))

    def _stamp(self, when):
        stamp = schedule.stamp_path(self.cfg)
        stamp.touch()
        os.utime(stamp, (when.timestamp(), when.timestamp()))

    def _run(self, *, ingest=0, dream=0):
        due, why = schedule.owed(self.cfg)
        env = dict(os.environ, TEST_VERDICT=f"{'owed' if due else 'ok'}: {why}",
                   TEST_INGEST_EXIT=str(ingest), TEST_DREAM_EXIT=str(dream))
        env.pop("MEMCAL_RUN_NOW", None)
        result = subprocess.run(["/bin/sh", str(self.script)], env=env,
                                capture_output=True, text=True, timeout=10)
        if schedule.stamp_path(self.cfg).exists():
            self._stamp(self.now)
        return result.returncode

    def test_power_on_after_several_missed_nights_runs_one_catchup(self):
        self._stamp(self.now - timedelta(days=7))
        self.assertTrue(schedule.render_plist(self.cfg)["RunAtLoad"])
        self.assertEqual(0, self._run())
        self.assertEqual(["ingest", "dream"], self.calls.read_text().splitlines())
        self.assertIsNone(schedule.owed(self.cfg)[0])
        self.assertFalse(schedule.pending_path(self.cfg).exists())

    def test_failed_dream_retries_then_success_satisfies_the_night(self):
        self.assertEqual(7, self._run(dream=7))
        self.assertTrue(schedule.pending_path(self.cfg).exists())
        self.assertIsNotNone(schedule.owed(self.cfg)[0])
        self.assertEqual(0, self._run())
        self.assertEqual(["ingest", "dream", "ingest", "dream"],
                         self.calls.read_text().splitlines())
        self.assertIsNone(schedule.owed(self.cfg)[0])

    def test_failed_collection_keeps_its_uncollected_traffic_owed(self):
        self.assertEqual(4, self._run(ingest=4))
        self.assertTrue(schedule.pending_path(self.cfg).exists())
        self.assertIsNotNone(schedule.owed(self.cfg)[0])

    def test_shutdown_after_start_does_not_satisfy_the_night(self):
        self._stamp(self.now.replace(hour=3, minute=1))
        schedule.pending_path(self.cfg).touch()
        due, why = schedule.owed(self.cfg)
        self.assertEqual(self.now.replace(hour=3), due)
        self.assertIn("retry owed", why)
        self.assertEqual(0, self._run())
        self.assertIsNone(schedule.owed(self.cfg)[0])

    def test_pending_marker_survives_reinstall(self):
        schedule.pending_path(self.cfg).touch()
        with mock.patch.object(schedule, "_is_macos", return_value=True), \
             mock.patch.object(schedule, "_launchctl", return_value=(0, "")), \
             mock.patch.object(schedule, "build_app_bundle", return_value=[]):
            schedule.install(self.cfg)
        self.assertIsNotNone(schedule.owed(self.cfg)[0])


if __name__ == "__main__":
    unittest.main()
