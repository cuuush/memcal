"""Updates refresh installed components without enabling new ones."""
from pathlib import Path
import plistlib
import unittest
from unittest import mock

from memcal import db, schedule, update
from tests._support import Base


class TestUpdateRefreshesExistingStoreAndSchedule(Base):
    def setUp(self):
        super().setUp()
        db.set_today("2026-10-08")
        self.plist = self.cfg.home / "job.plist"
        for patch in (mock.patch("memcal.config.load", return_value=self.cfg),
                      mock.patch.object(schedule, "plist_path", return_value=self.plist),
                      mock.patch.object(schedule, "_is_macos", return_value=True)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_migrates_an_existing_store_and_refreshes_saved_brief(self):
        self.conn.execute("ALTER TABLE events DROP COLUMN until")
        self.conn.commit()
        update.refresh()
        self.assertIn("until", {row[1] for row in self.conn.execute("PRAGMA table_info(events)")})
        self.assertTrue(self.cfg.brief_path.exists())
        self.assertFalse(self.plist.exists())

    def test_rebuilds_installed_script_preserving_custom_time_and_pending_pass(self):
        self.plist.write_bytes(plistlib.dumps({"StartCalendarInterval": {
            "Hour": 6, "Minute": 20}}))
        schedule.pending_path(self.cfg).touch()
        with mock.patch.object(schedule, "_launchctl", return_value=(0, "")), \
             mock.patch.object(schedule, "build_app_bundle", return_value=[]), \
             mock.patch.object(schedule, "status", return_value={"loaded": True}):
            update.refresh()
        self.assertEqual((6, 20), schedule.scheduled_time(self.cfg))
        self.assertIn('PENDING=', schedule.script_path(self.cfg).read_text())
        self.assertTrue(schedule.pending_path(self.cfg).exists())

    def test_failed_scheduler_reload_is_an_update_failure(self):
        self.plist.write_bytes(plistlib.dumps({}))
        with mock.patch.object(schedule, "install", return_value=[]), \
             mock.patch.object(schedule, "status", return_value={"loaded": False}):
            with self.assertRaisesRegex(update.UpdateError, "did not load"):
                update.refresh()


class TestUpdateUsesFreshCodeAndGitHub(unittest.TestCase):
    def test_refresh_subprocess_uses_updated_checkout_and_requested_store(self):
        with mock.patch.object(update, "_run", return_value="ready") as run:
            update._refresh_installed(Path("/tmp/source"), "/tmp/store")
        args = run.call_args
        self.assertIn("from memcal.update import refresh", args.args[0][-1])
        self.assertEqual(args.kwargs["env"]["PYTHONPATH"], "/tmp/source")
        self.assertEqual(args.kwargs["env"]["MEMCAL_HOME"], "/tmp/store")

    def test_pip_updates_from_github_and_upgrades_existing_chat_extras(self):
        with mock.patch.object(update, "_run") as run, \
             mock.patch.object(update, "_verify"), \
             mock.patch.object(update, "_chat_extras", return_value=["slack", "telegram"]):
            update._package_update()
        command = run.call_args.args[0]
        self.assertEqual(command[-1], "memcal[slack,telegram] @ " + update.GITHUB_SOURCE)
        self.assertIn("eager", command)


if __name__ == "__main__":
    unittest.main()
