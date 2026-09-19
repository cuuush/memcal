"""The Dream tab's live card: a pass started anywhere is visible everywhere.

A CLI (or scheduled) pass never appears in `web_jobs`, which only tracks jobs the
web server started. `dream.live` has every pass report into the store instead,
and `web_dream.dream_live` reads it back. All deterministic — no model, no socket.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import db, web_dream  # noqa: E402
from memcal.config import Config  # noqa: E402
from memcal.dream import live as live_stage  # noqa: E402
from memcal.dream import propose as propose_stage  # noqa: E402


def _bundle(entity: str, label: str, lines: int = 3):
    return SimpleNamespace(entity=entity, label=label, items=[None] * lines)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()
        db.set_today(None)

    def open_run(self, **kw) -> int:
        kw.setdefault("started_at", db.now())
        kw.setdefault("mode", "ondemand")
        kw.setdefault("model", "test/model")
        kw.setdefault("bundles", 2)
        kw.setdefault("items", 6)
        cur = self.conn.execute(
            "INSERT INTO runs(started_at, mode, model, bundles, items)"
            " VALUES(?,?,?,?,?)",
            (kw["started_at"], kw["mode"], kw["model"],
             kw["bundles"], kw["items"]))
        self.conn.commit()
        return int(cur.lastrowid)


class TestLiveFeedPlansBundles(Base):
    def test_attach_records_the_plan_as_queued(self):
        run_id = self.open_run()
        feed = live_stage.LiveFeed(self.cfg.db_path)
        feed.attach(run_id, [_bundle("person:Alice", "Alice"),
                             _bundle("thread:imessage:abc", "Book club", 5)])
        rows = self.conn.execute(
            "SELECT * FROM run_bundles WHERE run_id = ? ORDER BY entity",
            (run_id,)).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["state"] == "queued" for r in rows))
        by_entity = {r["entity"]: r for r in rows}
        self.assertEqual(by_entity["person:Alice"]["bundle_id"],
                         propose_stage.bundle_id("person:Alice"))
        self.assertEqual(by_entity["person:Alice"]["kind"], "person")
        self.assertEqual(by_entity["person:Alice"]["lines"], 3)
        self.assertEqual(by_entity["thread:imessage:abc"]["lines"], 5)

    def test_reattach_replaces_the_plan(self):
        run_id = self.open_run()
        feed = live_stage.LiveFeed(self.cfg.db_path)
        feed.attach(run_id, [_bundle("person:Alice", "Alice")])
        feed.attach(run_id, [_bundle("person:Bob", "Bob")])
        rows = self.conn.execute(
            "SELECT entity FROM run_bundles WHERE run_id = ?",
            (run_id,)).fetchall()
        self.assertEqual([r["entity"] for r in rows], ["person:Bob"])


class TestLiveFeedTracksRequests(Base):
    def test_wave_marks_reading_request_marks_terminal(self):
        run_id = self.open_run()
        feed = live_stage.LiveFeed(self.cfg.db_path)
        feed.attach(run_id, [_bundle("person:Alice", "Alice"),
                             _bundle("person:Bob", "Bob")])
        feed.event("propose_wave", {"kind": "main", "requests": 1, "bundles": 2,
                                    "entities": ["person:Alice", "person:Bob"]})
        feed.event("propose_request", {"label": "Alice", "bundles": 1, "ok": True,
                                       "error": "", "done": 1, "total": 2,
                                       "entities": ["person:Alice"]})
        feed.event("propose_request", {"label": "Bob", "bundles": 1, "ok": False,
                                       "error": "Timeout", "done": 1, "total": 2,
                                       "entities": ["person:Bob"]})
        states = {r["entity"]: r["state"] for r in self.conn.execute(
            "SELECT entity, state FROM run_bundles WHERE run_id = ?",
            (run_id,)).fetchall()}
        self.assertEqual(states, {"person:Alice": "done",
                                  "person:Bob": "failed"})
        n = self.conn.execute(
            "SELECT count(*) n FROM run_events WHERE run_id = ?",
            (run_id,)).fetchone()["n"]
        self.assertEqual(n, 3)

    def test_stage_events_land_with_stage_and_note(self):
        run_id = self.open_run()
        feed = live_stage.LiveFeed(self.cfg.db_path)
        feed.attach(run_id, [])
        feed.event("stage", {"stage": "sweep", "state": "running",
                             "note": "reviewing 4 writes"})
        row = self.conn.execute(
            "SELECT * FROM run_events WHERE run_id = ?",
            (run_id,)).fetchone()
        self.assertEqual(
            (row["event"], row["stage"], row["state"], row["note"]),
            ("stage", "sweep", "running", "reviewing 4 writes"))


class TestLiveFeedNeverBreaksThePass(Base):
    def test_events_before_attach_are_dropped_quietly(self):
        feed = live_stage.LiveFeed(self.cfg.db_path)
        feed.event("stage", {"stage": "prepare", "state": "running"})
        self.assertEqual(
            self.conn.execute("SELECT count(*) n FROM run_events").fetchone()["n"], 0)

    def test_unwritable_store_is_silent(self):
        feed = live_stage.LiveFeed(Path(self.tmp.name) / "no-such-dir" / "m.db")
        feed.attach(1, [_bundle("person:Alice", "Alice")])
        feed.event("stage", {"stage": "prepare", "state": "running"})


class TestDreamLiveSnapshot(Base):
    def _running(self) -> int:
        run_id = self.open_run()
        feed = live_stage.LiveFeed(self.cfg.db_path)
        feed.attach(run_id, [_bundle("person:Alice", "Alice"),
                             _bundle("person:Bob", "Bob")])
        feed.event("stage", {"stage": "prepare", "state": "done",
                             "note": "2 bundles · 6 lines"})
        feed.event("stage", {"stage": "propose", "state": "running",
                             "note": "reading 2 bundles"})
        feed.event("propose_wave", {"kind": "main", "requests": 1, "bundles": 2,
                                    "entities": ["person:Alice", "person:Bob"]})
        feed.event("propose_request", {"label": "Alice, Bob", "bundles": 2,
                                       "ok": True, "error": "",
                                       "done": 2, "total": 2,
                                       "entities": ["person:Alice",
                                                    "person:Bob"]})
        return run_id

    def test_live_run_reports_stages_propose_and_bundles(self):
        run_id = self._running()
        out = web_dream.dream_live(self.conn)
        self.assertIsNotNone(out["live"])
        live = out["live"]
        self.assertEqual(live["run"]["id"], run_id)
        self.assertEqual(live["run"]["mode"], "ondemand")
        self.assertEqual(live["run"]["model"], "model")
        self.assertEqual(live["status"], "live")
        self.assertEqual(
            [(s["stage"], s["state"]) for s in live["stages"]],
            [("prepare", "done"), ("propose", "running")])
        self.assertEqual(live["propose"], {"done": 2, "total": 2})
        self.assertEqual(live["counts"],
                         {"queued": 0, "reading": 0, "done": 2, "failed": 0})
        self.assertEqual(len(live["bundles"]), 2)
        self.assertEqual(len(live["requests"]), 1)
        self.assertTrue(live["requests"][0]["ok"])

    def test_request_feed_keeps_only_the_recent(self):
        run_id = self.open_run()
        feed = live_stage.LiveFeed(self.cfg.db_path)
        feed.attach(run_id, [_bundle("person:Alice", "Alice")])
        for i in range(15):
            feed.event("propose_request", {"label": f"req {i}", "bundles": 1,
                                           "ok": True, "error": "",
                                           "done": i + 1, "total": 15,
                                           "entities": ["person:Alice"]})
        live = web_dream.dream_live(self.conn)["live"]
        self.assertEqual(len(live["requests"]), 12)
        self.assertEqual(live["requests"][0]["label"], "req 3")
        self.assertEqual(live["requests"][-1]["label"], "req 14")

    def test_empty_store_has_no_live_pass(self):
        self.assertEqual(web_dream.dream_live(self.conn), {"live": None})

    def test_finished_run_is_not_live(self):
        run_id = self._running()
        self.conn.execute("UPDATE runs SET finished_at = ? WHERE id = ?",
                          (db.now(), run_id))
        self.conn.commit()
        self.assertEqual(web_dream.dream_live(self.conn), {"live": None})


class TestDreamLiveStaleness(Base):
    def test_quiet_pass_reads_as_stalled_then_goes_silent(self):
        db.set_today("2026-09-10T12:00:00")
        run_id = self.open_run()
        feed = live_stage.LiveFeed(self.cfg.db_path)
        feed.attach(run_id, [_bundle("person:Alice", "Alice")])
        feed.event("stage", {"stage": "propose", "state": "running",
                             "note": "reading 1 bundle"})

        db.set_today("2026-09-10T12:20:00")
        live = web_dream.dream_live(self.conn)["live"]
        self.assertIsNotNone(live)
        self.assertEqual(live["status"], "quiet")
        self.assertGreaterEqual(live["age_s"], 1200)

        db.set_today("2026-09-10T14:30:00")
        self.assertEqual(web_dream.dream_live(self.conn), {"live": None})


if __name__ == "__main__":
    unittest.main()
