"""Dream's terminal summary stays bounded without hiding failures or pricing."""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from memcal import archive, cli, db, llm
from memcal.config import Config
from memcal.dream import diagnostics
from memcal.dream import apply as apply_stage
from memcal.dream import run as dream_run
from memcal.dream.bundle import Bundle
from memcal.sources.base import IngestReport, deliver


class TestCompactDreamTerminal(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)
        db.set_today("2026-10-14")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.conn.close)
        self.addCleanup(db.set_today, None)

    def run_cli(self, *options, errors=()):
        reports = [
            IngestReport(channel="imessage", read=854, archived=801, passed=801,
                         notes=["routine source detail"]),
            IngestReport(channel="signal"),
            IngestReport(channel="email", error="authentication expired"),
            IngestReport(channel="groupme", more=True,
                         notes=["rate limited — more waiting"]),
        ]
        sources = [SimpleNamespace(name=report.channel) for report in reports]
        result = dream_run.DreamResult(
            run_id=148, bundles=114, items=1663, diffs=44,
            log=[f"updated   Item {i} [internal-key-{i}]" for i in range(60)],
            resolved=["kept rows separate (opaque-thread-id)"],
            notes=["asked again about a plan"], errors=list(errors),
            usage_summary="34 calls · 848030 in · 25055 out")

        def fake_dream(_conn, _cfg, *, progress, **_kwargs):
            progress("stage", {"stage": "prepare", "state": "running"})
            progress("stage", {"stage": "prepare", "state": "done",
                               "note": "114 bundles · 1663 lines"})
            for wave in range(4):
                progress("stage", {"stage": "propose", "state": "running",
                                   "note": f"wave {wave + 1} of 4"})
                progress("propose_wave", {"kind": "main", "requests": 10,
                                          "bundles": 29})
                for done in range(wave * 29 + 1, min((wave + 1) * 29, 114) + 1):
                    progress("propose_request", {"done": done, "total": 114,
                                                 "ok": True, "label": "private@sender"})
            progress("propose_wave", {"kind": "second-look", "bundles": 4})
            progress("propose_request", {"done": 118, "total": 118, "ok": True,
                                         "label": "private@sender"})
            for stage in ("merge", "apply", "sweep", "render"):
                progress("stage", {"stage": stage, "state": "running"})
                progress("stage", {"stage": stage, "state": "done", "note": "done"})
            return result

        out, err = io.StringIO(), io.StringIO()
        # The command decorator owns this connection after open_ctx returns it.
        conn = db.open_db(self.cfg.db_path)
        with mock.patch.object(cli, "open_ctx", return_value=(self.cfg, conn)), \
                mock.patch.object(cli.sources, "active_sources", return_value=sources), \
                mock.patch("memcal.sources.bluebubbles.ensure_server", return_value=[]), \
                mock.patch.object(cli.identity, "refresh_contacts", return_value=(279, "linked 279")), \
                mock.patch.object(cli.sources, "catch_up", side_effect=reports), \
                mock.patch.object(cli.brief, "write"), \
                mock.patch.object(cli, "dream", side_effect=fake_dream), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = cli.main(["--home", str(self.cfg.home), "dream", *options])
        return status, out.getvalue(), err.getvalue()

    def test_large_run_is_short_and_keeps_source_problems_visible(self):
        status, out, err = self.run_cli()
        self.assertEqual(status, 0)
        self.assertLess(len(out.splitlines()), 25, out)
        self.assertIn("imessage: 801 new item(s), 801 queued", out)
        self.assertIn("1 source(s) up to date", out)
        self.assertIn("email: authentication expired", out)
        self.assertIn("rate limited — more waiting", out)
        self.assertIn("email", err)
        self.assertIn("Dream #148 complete", out)
        self.assertIn("44 writes", out)
        self.assertIn("memcal trace 148", out)
        for detail in ("private@sender", "opaque-thread-id", "internal-key", "routine source detail",
                       "request(s) in flight", "848030", "100%"):
            self.assertNotIn(detail, out)
        for checkpoint in (25, 50, 75):
            self.assertEqual(out.count(f"about {checkpoint}%"), 1)

    def test_verbose_keeps_source_notes_requests_and_every_change(self):
        status, out, _err = self.run_cli("-v")
        self.assertEqual(status, 0)
        for detail in ("routine source detail", "private@sender", "opaque-thread-id",
                       "internal-key-59", "848030", "propose (second-look)"):
            self.assertIn(detail, out)

    def test_failure_returns_nonzero_and_is_not_hidden_by_summary(self):
        status, out, _err = self.run_cli(errors=["sweep: provider unavailable"])
        self.assertEqual(status, 1)
        self.assertIn("Dream #148 partial", out)
        self.assertIn("error: sweep: provider unavailable", out)


class TestCompactDreamReport(unittest.TestCase):
    def test_calendar_failures_remain_visible_without_verbose(self):
        result = dream_run.DreamResult(run_id=1, log=[
            "calendar  could not publish Lucy daycare: access denied"])
        self.assertIn("warning: calendar  could not publish Lucy daycare: access denied",
                      result.report(verbose=False))

    def test_empty_run_still_shows_a_logging_failure(self):
        result = dream_run.DreamResult(run_id=2, nothing_new=True,
                                      errors=["dream logging failed: disk full"])
        out = result.report(verbose=False)
        self.assertIn("failed", out)
        self.assertIn("disk full", out)

    def test_preview_keeps_token_and_price_estimates_without_request_inventory(self):
        result = dream_run.DreamResult(run_id=3, dry_run=True, bundles=10, log=[
            "10 bundles pack into 8 request(s)", "request 1: 2 bundles (~private)",
            "~40000 input tokens total", "~$0.0400 input; up to $0.5000 output"])
        out = result.report(verbose=False)
        self.assertIn("preview", out)
        self.assertIn("40000 input tokens", out)
        self.assertIn("$0.0400", out)
        self.assertNotIn("~private", out)

    def test_unpriced_preview_and_review_items_remain_visible(self):
        result = dream_run.DreamResult(run_id=4, dry_run=True,
                                      log=["NO PRICE ON FILE — this run is unpriced"],
                                      questions=["where?"], woken=["Book the room"])
        out = result.report(verbose=False)
        self.assertIn("NO PRICE ON FILE", out)
        self.assertIn("Questions for you:", out)
        self.assertIn("where?", out)
        self.assertIn('memcal answer Q<number> "your answer"', out)
        self.assertIn("1 to-do(s) ready to revisit", out)


class TestDreamProgressReflectsReviewedWork(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.cfg.dream_naming = False
        self.conn = db.open_db(self.cfg.db_path)
        db.set_today("2026-10-14")
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.conn.close)
        self.addCleanup(db.set_today, None)
        for i in range(30):
            aid = archive.append(self.conn, channel="imessage", external_id=str(i),
                                 ts=db.now(), text="hello", person=f"Person {i}",
                                 gated=True, gate_reason="subject-event")
            archive.spool_add(self.conn, aid, f"person:Person {i}")
        self.conn.commit()

    def run_dream(self, *, rejected=False, fail=False):
        stages = []

        def propose(_client, _conn, _cfg, batch, **_kwargs):
            if fail:
                return [], ["provider unavailable"], []
            return [(bundle, {}, "g") for bundle in batch], [], []

        def apply(*_args, **_kwargs):
            if rejected:
                return Counter({"event:rejected-stale": 1}), ["rejected-stale rejected a plan"]
            return Counter(), []

        with mock.patch.object(dream_run.llm, "client_for",
                               return_value=SimpleNamespace(usage=llm.Usage())), \
                mock.patch.object(dream_run.propose_stage, "propose_all", side_effect=propose), \
                mock.patch.object(dream_run.merge_stage, "merge_all",
                                  side_effect=lambda _c, _cfg, rows, **_kw: (rows, [])), \
                mock.patch.object(dream_run.apply_stage, "apply_diffs", side_effect=apply), \
                mock.patch.object(dream_run.sweep_stage, "sweep", return_value=({}, [])), \
                contextlib.redirect_stderr(io.StringIO()):
            result = dream_run.dream(self.conn, self.cfg, mode="ondemand",
                                     progress=lambda event, data: stages.append((event, data)))
        return result, [data for event, data in stages if event == "stage"]

    def test_empty_cold_start_is_done_and_journal_keeps_full_report(self):
        result, stages = self.run_dream()
        self.assertFalse(result.errors)
        self.assertEqual(result.diffs, 0)
        done = [s for s in stages if s["stage"] == "propose" and s["state"] == "done"]
        self.assertEqual(len(done), 1)
        self.assertIn("30 of 30 bundles reviewed", done[0]["note"])
        self.assertTrue(all(s["state"] != "failed" for s in stages))
        saved = diagnostics.read(self.cfg.home, result.run_id)
        self.assertEqual(saved[-1]["note"], result.report())
        self.assertEqual(saved[-1]["state"], "done")

    def test_rejections_are_not_counted_as_writes_in_wave_or_sweep_progress(self):
        result, stages = self.run_dream(rejected=True)
        self.assertEqual(result.diffs, 0)
        self.assertTrue(result.log)
        waves = [s["note"] for s in stages if s["stage"] == "propose" and "made" in s["note"]]
        self.assertEqual(len(waves), 4)
        self.assertTrue(all("made 0 write(s)" in note for note in waves))
        sweep = next(s for s in stages if s["stage"] == "sweep" and s["state"] == "running")
        self.assertIn("reviewing 0 write(s)", sweep["note"])
        self.assertIn("rejected-stale", diagnostics.read(self.cfg.home, result.run_id)[-1]["note"])

    def test_unread_bundles_are_not_reported_as_reviewed(self):
        result, stages = self.run_dream(fail=True)
        self.assertTrue(result.errors)
        failed = next(s for s in stages if s["stage"] == "propose" and s["state"] == "failed")
        self.assertIn("0 of 30 bundles reviewed", failed["note"])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM spool WHERE processed_at IS NULL").fetchone()[0], 30)


class TestBookingStatusIsSeparateFromSenderLabel(unittest.TestCase):
    def test_incoming_only_notifications_preserve_confirmation_on_reread(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(home=Path(tmp))
            cfg.ensure_dirs()
            conn = db.open_db(cfg.db_path)
            self.addCleanup(conn.close)
            db.set_today("2026-10-14")
            self.addCleanup(db.set_today, None)
            handle = "+18005551212"
            first = deliver(conn, IngestReport(channel="imessage"), channel="imessage",
                            external_id="confirmed", ts="2026-10-12T09:45:28-04:00",
                            thread=handle, handle=handle,
                            text="The owner has confirmed a booking request (daycare) "
                                 "with Lucy on 10/14 - View on Rover r.rover.com/example")
            bundle = Bundle(entity=f"thread:imessage:{handle}", items=[conn.execute(
                "SELECT * FROM archive WHERE id=?", (first,)).fetchone()])
            row = {"title": "Lucy doggy daycare", "date": "2026-10-14",
                   "kind": "commitment", "status": "confirmed", "cite_ids": [first]}
            diff = {"events": [row], "threads": [
                {"channel": "imessage", "thread": handle, "name": "Lucy doggy daycare"}]}
            _counts, log = apply_stage.apply_diffs(conn, cfg, [(bundle, diff)],
                                                 written_by="dream:ondemand")
            self.assertIn(f'label     {handle} → "Lucy doggy daycare" (inferred from messages)', log)
            self.assertFalse(any("maybe Lucy" in line for line in log))
            event = conn.execute("SELECT * FROM events").fetchone()
            self.assertEqual(event["status"], "confirmed")
            # A later incoming scheduling message cannot undo a confirmed booking
            # just because the transport contains none of the sitter's replies.
            second = deliver(conn, IngestReport(channel="imessage"), channel="imessage",
                             external_id="scheduling", ts="2026-10-13T09:45:28-04:00",
                             thread=handle, handle=handle,
                             text="Would tomorrow's dropoff at 7:30 work?")
            later = Bundle(entity=bundle.entity, items=[conn.execute(
                "SELECT * FROM archive WHERE id=?", (second,)).fetchone()])
            uncertain = {**row, "key": event["key"], "status": "mentioned",
                         "cite_ids": [second]}
            for _ in range(2):
                apply_stage.apply_diffs(conn, cfg, [(later, {"events": [uncertain]})],
                                        written_by="dream:ondemand")
            self.assertEqual(conn.execute("SELECT count(*) FROM events").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT status FROM events").fetchone()[0], "confirmed")


if __name__ == "__main__":
    unittest.main()
