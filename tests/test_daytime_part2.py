"""Part 2: daytime ticks collect cheaply and never race each other."""

from __future__ import annotations

import argparse
import contextlib
import io
import multiprocessing
import os
import plistlib
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import archive, brief, cli, db, identity, lock, schedule
from memcal.config import Config
from memcal.sources import base
from memcal.sources.spec import Source, SourceError


def _child_hold(home: str, ready, release) -> None:
    """Multiprocessing target: hold the store lock until told to let go."""
    from memcal import lock as lock_mod
    from memcal.config import Config as Cfg
    with lock_mod.held(Cfg(home=Path(home))):
        ready.set()
        release.wait(10)


class TestCollectLockIsCrossProcess(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()

    def test_a_second_process_gets_busy_not_a_hang(self):
        ctx = multiprocessing.get_context("fork")
        ready, release = ctx.Event(), ctx.Event()
        proc = ctx.Process(target=_child_hold, args=(self.tmp.name, ready, release))
        proc.start()
        try:
            self.assertTrue(ready.wait(10), "child never took the lock")
            with self.assertRaises(lock.Busy):
                with lock.held(self.cfg):
                    pass
        finally:
            release.set()
            proc.join(10)
        # Process exit released ownership: the same store collects again.
        with lock.held(self.cfg):
            pass

    def test_separate_stores_do_not_block_one_another(self):
        other = Config(home=Path(self.tmp.name) / "other")
        other.ensure_dirs()
        with lock.held(self.cfg):
            with lock.held(other):
                pass


class _Quiet(Source):
    name = "quiet"
    in_all = True

    def check(self, cfg):
        return True, "ready"

    def fetch(self, conn, cfg, report, limit):
        report.more = False


class TestBusyCollectionIsExplicit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = str(Path(self.tmp.name) / "store")

    def _args(self, **kw):
        args = argparse.Namespace(home=self.home, stream="all", stale=False,
                                  due=False, limit=50, rounds=3)
        for key, value in kw.items():
            setattr(args, key, value)
        return args

    def test_a_contended_manual_ingest_reports_busy_and_records_nothing(self):
        from memcal import sources as sources_pkg
        cfg = Config(home=Path(self.home))
        cfg.ensure_dirs()
        conn = db.open_db(cfg.db_path)
        before = conn.execute("SELECT count(*) n FROM collections").fetchone()["n"]
        conn.close()
        with lock.held(cfg):
            with mock.patch.object(sources_pkg, "all_sources", return_value=[_Quiet()]), \
                    contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                rc = cli.cmd_ingest(self._args())
        self.assertEqual(rc, 75)
        self.assertIn("already running", err.getvalue())
        conn = db.open_db(cfg.db_path)
        try:
            after = conn.execute("SELECT count(*) n FROM collections").fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(before, after)

    def test_a_contended_web_collection_reports_busy(self):
        from memcal import web_jobs
        cfg = Config(home=Path(self.home))
        cfg.ensure_dirs()
        conn = db.open_db(cfg.db_path)
        try:
            with lock.held(cfg):
                out = web_jobs.collect_work(conn, cfg, web_jobs._Job("gather"))
        finally:
            conn.close()
        self.assertTrue(out.get("busy"))
        self.assertIn("busy", out.get("error", ""))


class TestDaytimeCadenceAndOneNightlyDream(unittest.TestCase):
    """A simulated day: due ticks collect, nothing else spends model quota."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = str(Path(self.tmp.name) / "store")
        db.set_today(None)
        self.addCleanup(db.set_today, None)

    def _args(self, **kw):
        args = argparse.Namespace(home=self.home, stream="all", stale=False,
                                  due=False, limit=50, rounds=3)
        for key, value in kw.items():
            setattr(args, key, value)
        return args

    def _tick(self, **kw):
        from memcal import sources as sources_pkg
        from memcal import llm
        with mock.patch.object(sources_pkg, "all_sources", return_value=[_Quiet()]), \
                mock.patch.object(sources_pkg, "get", return_value=_Quiet()), \
                mock.patch.object(identity, "refresh_contacts", return_value=(0, "")), \
                mock.patch.object(brief, "write", return_value=None), \
                mock.patch.object(llm, "client_for",
                                  side_effect=AssertionError("no model on a tick")), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return cli.cmd_ingest(self._args(**kw))

    def _tick_loud(self, **kw):
        """Like `_tick` but letting stdout through so callers can read it."""
        from memcal import sources as sources_pkg
        from memcal import llm
        with mock.patch.object(sources_pkg, "all_sources", return_value=[_Quiet()]), \
                mock.patch.object(sources_pkg, "get", return_value=_Quiet()), \
                mock.patch.object(identity, "refresh_contacts", return_value=(0, "")), \
                mock.patch.object(brief, "write", return_value=None), \
                mock.patch.object(llm, "client_for",
                                  side_effect=AssertionError("no model on a tick")), \
                contextlib.redirect_stderr(io.StringIO()):
            return cli.cmd_ingest(self._args(**kw))

    def test_due_ticks_collect_at_the_interval_and_never_between(self):
        from memcal import llm
        base_time = datetime(2026, 9, 8, 0, 0).astimezone()
        db.set_today(base_time.replace(hour=0, minute=0))
        self.assertEqual(self._tick(due=True), 0)
        # Four minutes later: quiet. Five after the check: due again.
        db.set_today(base_time.replace(hour=0, minute=4))
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self._tick_loud(due=True), 0)
        self.assertIn("nothing due", out.getvalue())
        db.set_today(base_time.replace(hour=0, minute=5))
        self.assertEqual(self._tick(due=True), 0)
        conn = db.open_db(Config(home=Path(self.home)).db_path)
        try:
            n = conn.execute("SELECT count(*) n FROM collections").fetchone()["n"]
            bad = conn.execute(
                "SELECT count(*) n FROM collection_sources WHERE status NOT IN"
                " ('complete','incomplete','failed','unavailable')").fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(n, 2)
        self.assertEqual(bad, 0)

    def test_a_successful_nightly_collection_postpones_the_next_tick(self):
        db.set_today(datetime(2026, 9, 8, 3, 0).astimezone())
        self.assertEqual(self._tick(), 0)  # the nightly `ingest all`
        db.set_today(datetime(2026, 9, 8, 3, 2).astimezone())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self._tick_loud(due=True), 0)
        self.assertIn("nothing due", out.getvalue())

    def test_frequent_ticks_do_not_dispatch_dream(self):
        """Ticks decide collection only; dream runs once, on purpose, at night."""
        from memcal import llm
        dreams = []
        base_time = datetime(2026, 9, 8, 0, 0).astimezone()
        for minute in range(0, 60, 5):
            db.set_today(base_time.replace(minute=minute))
            self.assertEqual(self._tick(due=True), 0)
        # Thirteen daytime ticks, zero model clients, zero dreams.
        db.set_today(base_time.replace(hour=3, minute=0))
        with mock.patch.object(llm, "client_for",
                               side_effect=AssertionError("ticks never dream")):
            self.assertEqual(self._tick(), 0)  # nightly ingest
        dreams.append("nightly")
        self.assertEqual(dreams, ["nightly"])
        conn = db.open_db(Config(home=Path(self.home)).db_path)
        try:
            n = conn.execute("SELECT count(*) n FROM collections").fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(n, 12 + 1)


class TestMissedNightRecoversOnce(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.home,
                                                            ignore_errors=True))
        self.cfg = Config(home=self.home)
        self.cfg.ensure_dirs()
        self.plist = self.home / "com.memcal.nightly.plist"
        self.plist.write_bytes(plistlib.dumps(
            {"StartCalendarInterval": {"Hour": 3, "Minute": 0}}))
        schedule.script_path(self.cfg).write_text(
            schedule.render_script(self.cfg), encoding="utf-8")
        patch = mock.patch.object(schedule, "plist_path", return_value=self.plist)
        patch.start()
        self.addCleanup(patch.stop)

    def _started_at(self, when: datetime):
        stamp = schedule.stamp_path(self.cfg)
        stamp.touch()
        os.utime(stamp, (when.timestamp(), when.timestamp()))

    def test_one_recovery_pass_clears_the_debt(self):
        now = datetime(2026, 9, 8, 8, 0).astimezone()
        self._started_at(now - timedelta(days=2))
        self.assertIsNotNone(schedule.owed(self.cfg, now=now)[0])
        self._started_at(now)  # what the script itself writes before the work
        later = now + timedelta(minutes=30)
        self.assertIsNone(schedule.owed(self.cfg, now=later)[0])


class TestDownSourcesDoNotSuppressHealthyOnes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = str(Path(self.tmp.name) / "store")
        db.set_today(None)
        self.addCleanup(db.set_today, None)

    def test_unavailable_is_recorded_and_the_healthy_source_finishes(self):
        from memcal import sources as sources_pkg

        class Down(Source):
            name = "down"
            in_all = True

            def check(self, cfg):
                return False, "bridge closed"

            def fetch(self, conn, cfg, report, limit):
                raise AssertionError("login must never start for an unavailable source")

        class Up(Source):
            name = "up"
            in_all = True

            def check(self, cfg):
                return True, "ready"

            def fetch(self, conn, cfg, report, limit):
                base.deliver(conn, report, stream="up", external_id="u1",
                             ts=db.now(), text="up note about dinner?",
                             thread="t", handle="friend@example.com")

        args = argparse.Namespace(home=self.home, stream="all", stale=False,
                                  due=True, limit=50, rounds=3)
        with mock.patch.object(sources_pkg, "all_sources",
                               return_value=[Down(), Up()]), \
                mock.patch.object(identity, "refresh_contacts", return_value=(0, "")), \
                mock.patch.object(brief, "write", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            rc = cli.cmd_ingest(args)
        self.assertEqual(rc, 1)
        self.assertIn("down", err.getvalue())
        conn = db.open_db(Config(home=Path(self.home)).db_path)
        try:
            rows = {r["stream"]: dict(r) for r in conn.execute(
                "SELECT * FROM collection_sources ORDER BY collection_id DESC LIMIT 2")}
        finally:
            conn.close()
        self.assertEqual(rows["down"]["status"], "unavailable")
        self.assertEqual(rows["up"]["status"], "complete")
        self.assertEqual(rows["up"]["archived"], 1)


class TestGeneratedDaytimeScheduling(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()

    def test_the_cheap_branch_collects_due_and_never_dreams(self):
        body = schedule.render_script(self.cfg, python=sys.executable)
        cheap = body[body.index("ok*)"):body.index(': > "$STAMP"')]
        self.assertIn("memcal ingest all --due", cheap)
        self.assertNotIn("dream", cheap)
        self.assertNotIn("--stale", cheap)

    def test_nothing_due_and_busy_are_quiet_not_failures(self):
        body = schedule.render_script(self.cfg, python=sys.executable)
        self.assertIn("nothing due for another check right now", body)
        self.assertIn("already running", body)

    def test_busy_and_idle_ticks_exit_zero(self):
        """Run the cheap branch for real with a stubbed interpreter."""
        body = schedule.render_script(self.cfg, python=sys.executable)
        branch = body[body.index("ok*)"):body.index(': > "$STAMP"')]
        # Strip the `ok*)` selector line; keep the commands.
        lines = branch.splitlines()[1:]
        stub = Path(self.tmp.name) / "stubpy"
        stub.write_text('#!/bin/sh\necho "$STUB_OUT"\nexit "$STUB_STATUS"\n')
        stub.chmod(0o755)
        script = ("VERDICT=ok\nPY=%s\n%s\n" % (stub, "\n".join(lines)))
        env = dict(os.environ, STUB_OUT="nothing due for another check right now",
                   STUB_STATUS="0")
        proc = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                              env=env)
        self.assertEqual(proc.returncode, 0)
        env = dict(os.environ,
                   STUB_OUT="ingest: another collection is already running on this store",
                   STUB_STATUS="75")
        proc = subprocess.run(["sh", "-c", script], capture_output=True, text=True,
                              env=env)
        self.assertEqual(proc.returncode, 0)

    def test_the_owed_branch_still_does_a_full_nightly(self):
        body = schedule.render_script(self.cfg, python=sys.executable)
        nightly = body[body.index(': > "$STAMP"'):]
        self.assertIn('"$PY" -m memcal ingest all', nightly)
        self.assertIn("memcal dream --mode nightly", nightly)
        stamp = body.index(': > "$STAMP"')
        self.assertLess(stamp, body.index("memcal dream --mode nightly"))

    def test_install_replaces_the_old_catch_up_script(self):
        script = schedule.script_path(self.cfg)
        script.write_text(
            schedule.render_script(self.cfg, python=sys.executable).replace(
                "memcal ingest all --due", "memcal ingest --stale"),
            encoding="utf-8")
        fake_plist = Path(self.tmp.name) / "com.memcal.nightly.plist"
        retired = [Path(self.tmp.name) / f"{label}.plist"
                   for label in schedule.RETIRED_LABELS]
        with mock.patch.object(schedule, "_launchctl", return_value=(0, "")), \
                mock.patch.object(schedule, "build_app_bundle", return_value=[]), \
                mock.patch.object(schedule, "plist_path", return_value=fake_plist), \
                mock.patch.object(schedule, "retired_plist_paths", return_value=retired):
            out = schedule.install(self.cfg)
        body = script.read_text(encoding="utf-8")
        self.assertIn("memcal ingest all --due", body)
        self.assertNotIn("memcal ingest --stale", body)
        self.assertTrue(any("overwritten" in line for line in out))
        # Launcher routing survives the reschedule.
        plist = schedule.render_plist(self.cfg)
        self.assertTrue(plist["RunAtLoad"])
        self.assertEqual(schedule.effective_interval(self.cfg),
                         plist["StartInterval"])
        self.assertIn("StartCalendarInterval", plist)


if __name__ == "__main__":
    unittest.main()
