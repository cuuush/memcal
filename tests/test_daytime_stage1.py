"""Stage 1: reliable daytime collection — trustworthy outcomes and due-only ingest."""

from __future__ import annotations

import argparse
import contextlib
import io
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from memcal import archive, brief, cli, db, identity, threads
from memcal.config import Config
from memcal.sources import base, catch_up
from memcal.sources.spec import Source, SourceError


def _cfg(tmp: str) -> Config:
    cfg = Config(home=Path(tmp))
    cfg.ensure_dirs()
    return cfg


class _Scripted(Source):
    """A source that delivers scripted pages through the real `deliver` path.

    `pages` is a list per round: each entry is ("messages", more) where messages is
    a list of external ids to deliver, or ("error", message) to raise after
    delivering `delivered_before_error` rows, or ("blowup", message) for an
    unexpected exception.
    """

    name = "daytime"
    description = "stage-1 scripted source"

    def __init__(self, pages, *, name="daytime"):
        self.pages = list(pages)
        self.calls = 0
        self.name = name

    def check(self, cfg):
        return True, "ready"

    def fetch(self, conn, cfg, report, limit):
        if self.calls >= len(self.pages):
            report.more = False
            return
        page = self.pages[self.calls]
        self.calls += 1
        kind = page[0]
        if kind == "messages":
            _, ids, more = page
            for eid in ids:
                base.deliver(
                    conn, report, stream=self.name, external_id=eid,
                    ts=db.now(), text=f"daytime note {eid} about dinner?",
                    thread="daytime-thread", handle="friend@example.com",
                )
            report.more = bool(more)
        elif kind == "error":
            _, ids, message = page
            for eid in ids:
                base.deliver(
                    conn, report, stream=self.name, external_id=eid,
                    ts=db.now(), text=f"daytime note {eid} about dinner?",
                    thread="daytime-thread", handle="friend@example.com",
                )
            raise SourceError(message)
        elif kind == "blowup":
            _, message = page
            raise RuntimeError(message)
        else:  # pragma: no cover
            raise AssertionError(f"bad page {page!r}")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = _cfg(self.tmp.name)
        self.conn = db.open_db(self.cfg.db_path)

    def tearDown(self):
        db.set_today(None)
        self.conn.close()
        self.tmp.cleanup()

    def _collect(self, source, **kw):
        cid = archive.open_collection(self.conn, mode="cli")
        try:
            report = catch_up(source, self.conn, self.cfg, collection_id=cid, **kw)
        finally:
            archive.close_collection(self.conn, cid)
        row = self.conn.execute(
            "SELECT * FROM collection_sources WHERE collection_id = ? AND stream = ?",
            (cid, source.name)).fetchone()
        return cid, report, dict(row) if row else None


class TestAllPageTotalsPersistExactlyOnce(_Base):
    def test_populated_pages_then_quiet_final_page_keep_totals(self):
        src = _Scripted([
            ("messages", ["a1", "a2"], True),
            ("messages", ["a3"], True),
            ("messages", [], False),
        ])
        cid, report, row = self._collect(src)
        self.assertEqual(report.archived, 3)
        self.assertFalse(report.error)
        self.assertFalse(report.more)
        self.assertEqual(report.outcome, "complete")
        self.assertEqual(row["archived"], 3)
        self.assertEqual(row["read"], 3)
        self.assertEqual(row["status"], "complete")
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) n FROM collection_sources WHERE collection_id = ?",
                (cid,)).fetchone()["n"], 1)

    def test_quiet_check_with_no_messages_is_complete(self):
        src = _Scripted([("messages", [], False)])
        _cid, report, row = self._collect(src)
        self.assertEqual(report.archived, 0)
        self.assertEqual(report.outcome, "complete")
        self.assertEqual(row["status"], "complete")


class TestFailurePreservesPartialCounts(_Base):
    def test_error_after_good_pages_keeps_counts_and_stays_failed(self):
        src = _Scripted([
            ("messages", ["b1", "b2"], True),
            ("error", ["b3"], "bridge down"),
        ])
        _cid, report, row = self._collect(src)
        self.assertEqual(report.archived, 3)
        self.assertIn("bridge", report.error or "")
        self.assertEqual(report.outcome, "failed")
        self.assertEqual(row["archived"], 3)
        self.assertEqual(row["status"], "failed")
        self.assertIsNone(archive.last_complete_check(self.conn, src.name))

    def test_later_failure_does_not_erase_earlier_complete(self):
        good = _Scripted([("messages", ["c1"], False)])
        cid1, _r1, row1 = self._collect(good)
        self.assertEqual(row1["status"], "complete")
        first_complete = archive.last_complete_check(self.conn, good.name)
        self.assertEqual(first_complete["collection_id"], cid1)
        bad = _Scripted([("error", ["c2"], "bridge down")], name=good.name)
        _cid2, _r2, row2 = self._collect(bad)
        self.assertEqual(row2["status"], "failed")
        still = archive.last_complete_check(self.conn, good.name)
        self.assertEqual(still["collection_id"], cid1)


class TestIncompleteVersusExhausted(_Base):
    def test_round_cap_with_more_remaining_is_incomplete(self):
        src = _Scripted([
            ("messages", ["d1"], True),
            ("messages", ["d2"], True),
            ("messages", ["d3"], True),
        ])
        _cid, report, row = self._collect(src, rounds=2)
        self.assertTrue(report.more)
        self.assertEqual(report.outcome, "incomplete")
        self.assertEqual(row["status"], "incomplete")
        self.assertTrue(any("more waiting" in n for n in report.notes))

    def test_stalled_dry_round_claiming_more_is_incomplete(self):
        src = _Scripted([
            ("messages", ["e1"], True),
            ("messages", [], True),
        ])
        _cid, report, row = self._collect(src)
        self.assertTrue(report.more)
        self.assertEqual(row["status"], "incomplete")

    def test_zero_row_exhausted_page_stays_complete(self):
        src = _Scripted([
            ("messages", ["f1"], True),
            ("messages", [], False),
        ])
        _cid, report, row = self._collect(src)
        self.assertFalse(report.more)
        self.assertEqual(row["status"], "complete")
        self.assertEqual(row["archived"], 1)


class TestPreflightAndUnexpectedFailures(_Base):
    def test_preflight_unavailable_is_durable_and_others_finish(self):
        class Down(Source):
            name = "down"

            def check(self, cfg):
                return False, "missing credential: DOWN_TOKEN"

            def fetch(self, conn, cfg, report, limit):
                raise AssertionError("fetch must not start when preflight fails")

        class Up(Source):
            name = "up"

            def check(self, cfg):
                return True, "ready"

            def fetch(self, conn, cfg, report, limit):
                base.deliver(conn, report, stream="up", external_id="u1",
                             ts=db.now(), text="up note about dinner?",
                             thread="t", handle="friend@example.com")

        cid = archive.open_collection(self.conn, mode="cli")
        try:
            down, up = Down(), Up()
            for src in (down, up):
                ok, why = src.check(self.cfg)
                if not ok:
                    archive.record_unavailable(self.conn, cid, src.name, why)
                    continue
                catch_up(src, self.conn, self.cfg, collection_id=cid)
        finally:
            archive.close_collection(self.conn, cid)
        rows = {r["stream"]: dict(r) for r in self.conn.execute(
            "SELECT * FROM collection_sources WHERE collection_id = ?", (cid,))}
        self.assertEqual(rows["down"]["status"], "unavailable")
        self.assertIn("credential", rows["down"]["error"] or "")
        self.assertEqual(rows["up"]["status"], "complete")
        self.assertEqual(rows["up"]["archived"], 1)

    def test_unexpected_plugin_exception_is_a_durable_failure(self):
        src = _Scripted([("blowup", "kaboom")])
        _cid, report, row = self._collect(src)
        self.assertIn("kaboom", report.error or "")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["archived"], 0)

    def test_interrupted_attempt_never_reads_as_complete(self):
        cid = archive.open_collection(self.conn, mode="cli")
        # Simulate a process dying mid-pass: a collections row with no finish and
        # no per-source outcome row.
        self.assertIsNone(archive.last_source_attempt(self.conn, "ghost"))
        self.assertIsNone(archive.last_complete_check(self.conn, "ghost"))
        due, _reason = archive.source_due(self.conn, "ghost", self.cfg)
        self.assertTrue(due)


class TestDueSelectionOnRecordedOutcomes(_Base):
    def _check_at(self, pin: str):
        db.set_today(pin)
        cid = archive.open_collection(self.conn, mode="cli")
        try:
            src = _Scripted([("messages", [], False)])
            catch_up(src, self.conn, self.cfg, collection_id=cid)
        finally:
            archive.close_collection(self.conn, cid)

    def test_eleven_oclock_check_drives_eleven_oh_four_and_five(self):
        self._check_at("2026-09-12T11:00:00")
        db.set_today("2026-09-12T11:04:00")
        due, _ = archive.source_due(self.conn, "daytime", self.cfg)
        self.assertFalse(due)
        db.set_today("2026-09-12T11:05:00")
        due, _ = archive.source_due(self.conn, "daytime", self.cfg)
        self.assertTrue(due)

    def test_message_timestamps_do_not_drive_due(self):
        self._check_at("2026-09-12T11:00:00")
        self.conn.execute(
            "INSERT INTO archive(stream, external_id, ts, thread, text, created_at)"
            " VALUES('daytime','old1','2020-01-01T00:00:00','t','old note',?)",
            (db.now(),))
        self.conn.commit()
        db.set_today("2026-09-12T11:04:00")
        due, _ = archive.source_due(self.conn, "daytime", self.cfg)
        self.assertFalse(due)

    def test_failed_and_incomplete_wait_the_interval(self):
        db.set_today("2026-09-12T11:00:00")
        cid = archive.open_collection(self.conn, mode="cli")
        try:
            bad = _Scripted([("error", [], "down")], name="flaky")
            catch_up(bad, self.conn, self.cfg, collection_id=cid)
        finally:
            archive.close_collection(self.conn, cid)
        db.set_today("2026-09-12T11:01:00")
        due, _ = archive.source_due(self.conn, "flaky", self.cfg)
        self.assertFalse(due)
        db.set_today("2026-09-12T11:06:00")
        due, _ = archive.source_due(self.conn, "flaky", self.cfg)
        self.assertTrue(due)

    def test_never_checked_and_legacy_unknown_are_due(self):
        due, reason = archive.source_due(self.conn, "never-seen", self.cfg)
        self.assertTrue(due)
        self.assertIn("never", reason)
        cid = archive.open_collection(self.conn, mode="cli")
        self.conn.execute(
            "INSERT INTO collection_sources(collection_id, stream, read, archived,"
            " passed, finished_at, status) VALUES(?,?,?,?,?,?,?)",
            (cid, "legacy", 0, 0, 0, db.now(), "unknown"))
        self.conn.commit()
        archive.close_collection(self.conn, cid)
        due, reason = archive.source_due(self.conn, "legacy", self.cfg)
        self.assertTrue(due)
        self.assertIn("trustworthy", reason)

    def test_future_stamp_collects_rather_than_suppressing(self):
        self._check_at("2026-09-12T11:00:00")
        future = (db.now_dt() + timedelta(hours=2)).isoformat()
        self.conn.execute(
            "UPDATE collection_sources SET finished_at = ? WHERE stream = 'daytime'",
            (future,))
        self.conn.commit()
        due, reason = archive.source_due(self.conn, "daytime", self.cfg)
        self.assertTrue(due)
        self.assertIn("future", reason)

    def test_timezone_offsets_compare_as_instants(self):
        cid = archive.open_collection(self.conn, mode="cli")
        self.conn.execute(
            "INSERT INTO collection_sources(collection_id, stream, read, archived,"
            " passed, finished_at, status) VALUES(?,?,?,?,?,?,?)",
            (cid, "tz", 0, 0, 0, "2026-09-12T11:00:00+00:00", "complete"))
        self.conn.commit()
        archive.close_collection(self.conn, cid)
        # Same instant in +02:00 four minutes later: not due.
        moment = db.parse_ts("2026-09-12T11:04:00+00:00")
        due, _ = archive.source_due(
            self.conn, "tz", self.cfg,
            now=moment.astimezone(timezone(timedelta(hours=2))))
        self.assertFalse(due)
        moment = db.parse_ts("2026-09-12T11:06:00+00:00")
        due, _ = archive.source_due(
            self.conn, "tz", self.cfg,
            now=moment.astimezone(timezone(timedelta(hours=-5))))
        self.assertTrue(due)


class TestDueCommandIsANoopWhenNothingIsDue(unittest.TestCase):
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

    def test_no_due_writes_no_collection_and_touches_nothing(self):
        from memcal import sources as sources_pkg

        class Quiet(Source):
            name = "quiet"
            in_all = True

            def check(self, cfg):
                raise AssertionError("preflight must not run when nothing is due")

            def fetch(self, conn, cfg, report, limit):
                raise AssertionError("fetch must not run when nothing is due")

        db.set_today("2026-09-12T11:00:00")
        self.addCleanup(db.set_today, None)
        cfg = Config(home=Path(self.home))
        cfg.ensure_dirs()
        conn = db.open_db(cfg.db_path)
        cid = archive.open_collection(conn, mode="cli")
        try:
            catch_up(_Scripted([("messages", [], False)], name="quiet"),
                     conn, cfg, collection_id=cid)
        finally:
            archive.close_collection(conn, cid)
        before = conn.execute("SELECT count(*) n FROM collections").fetchone()["n"]
        conn.close()
        db.set_today("2026-09-12T11:01:00")

        with mock.patch.object(sources_pkg, "all_sources", return_value=[Quiet()]), \
                mock.patch.object(identity, "refresh_contacts",
                                  side_effect=AssertionError("no contact refresh")), \
                mock.patch.object(brief, "write",
                                  side_effect=AssertionError("no brief rewrite")), \
                mock.patch.object(archive, "open_collection",
                                  side_effect=AssertionError("no collection")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli.cmd_ingest(self._args(due=True))
        self.assertEqual(rc, 0)
        self.assertIn("nothing due", out.getvalue())
        conn = db.open_db(Config(home=Path(self.home)).db_path)
        try:
            after = conn.execute("SELECT count(*) n FROM collections").fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(before, after)

    def test_forced_ingest_bypasses_due_selection(self):
        from memcal import sources as sources_pkg

        calls = []

        class Quiet(Source):
            name = "quiet"
            in_all = True

            def check(self, cfg):
                return True, "ready"

            def fetch(self, conn, cfg, report, limit):
                calls.append(1)

        db.set_today("2026-09-12T11:00:00")
        self.addCleanup(db.set_today, None)
        with mock.patch.object(sources_pkg, "all_sources", return_value=[Quiet()]), \
                mock.patch.object(sources_pkg, "get", return_value=Quiet()), \
                mock.patch.object(identity, "refresh_contacts", return_value=(0, "")), \
                mock.patch.object(brief, "write", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()):
            first = cli.cmd_ingest(self._args(due=True))
        self.assertEqual(first, 0)
        self.assertEqual(calls, [1])
        # Four minutes later --due is a no-op, but a forced ingest still runs.
        db.set_today("2026-09-12T11:04:00")
        with mock.patch.object(sources_pkg, "all_sources", return_value=[Quiet()]), \
                mock.patch.object(sources_pkg, "get", return_value=Quiet()), \
                mock.patch.object(identity, "refresh_contacts", return_value=(0, "")), \
                mock.patch.object(brief, "write", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()):
            noop = cli.cmd_ingest(self._args(due=True))
            forced = cli.cmd_ingest(self._args(due=False))
        self.assertEqual(noop, 0)
        self.assertEqual(forced, 0)
        self.assertEqual(calls, [1, 1])

    def test_due_and_stale_conflict(self):
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = cli.cmd_ingest(self._args(due=True, stale=True))
        self.assertEqual(rc, 2)

    def test_due_with_bad_outcome_is_nonzero_with_reason(self):
        from memcal import sources as sources_pkg

        class Bad(Source):
            name = "bad"
            in_all = True

            def check(self, cfg):
                return True, "ready"

            def fetch(self, conn, cfg, report, limit):
                raise SourceError("bridge down")

        db.set_today(None)
        with mock.patch.object(sources_pkg, "all_sources", return_value=[Bad()]), \
                mock.patch.object(identity, "refresh_contacts", return_value=(0, "")), \
                mock.patch.object(brief, "write", return_value=None), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            rc = cli.cmd_ingest(self._args(due=True))
        self.assertEqual(rc, 1)
        self.assertIn("bad", err.getvalue())


class TestSettingValidation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(home=Path(self.tmp.name))

    def test_interval_accepts_five_and_rejects_out_of_range(self):
        from memcal import settings
        settings.save(self.cfg, {"MEMCAL_COLLECT_INTERVAL_MINUTES": "5"})
        self.assertEqual(self.cfg.collect_interval_minutes, 5)
        for bad in ("0", "-3", "9999", "soon"):
            with self.assertRaises(settings.SettingsError, msg=bad):
                settings.save(self.cfg, {"MEMCAL_COLLECT_INTERVAL_MINUTES": bad})


class TestCliAndWebAgree(_Base):
    def test_same_source_same_counts_same_status(self):
        from memcal import web_jobs

        cli_src = _Scripted([("messages", ["w1", "w2"], True),
                             ("messages", [], False)], name="agree")
        cid = archive.open_collection(self.conn, mode="cli")
        try:
            cli_report = catch_up(cli_src, self.conn, self.cfg, collection_id=cid)
        finally:
            archive.close_collection(self.conn, cid)
        cli_row = self.conn.execute(
            "SELECT * FROM collection_sources WHERE collection_id = ?",
            (cid,)).fetchone()

        web_src = _Scripted([("messages", ["x1", "x2"], True),
                             ("messages", [], False)], name="agree")
        cid2 = archive.open_collection(self.conn, mode="web")
        try:
            web_report = catch_up(web_src, self.conn, self.cfg, collection_id=cid2)
        finally:
            archive.close_collection(self.conn, cid2)
        web_row = self.conn.execute(
            "SELECT * FROM collection_sources WHERE collection_id = ?",
            (cid2,)).fetchone()
        self.assertEqual((cli_report.read, cli_report.archived, cli_report.outcome),
                         (web_report.read, web_report.archived, web_report.outcome))
        self.assertEqual((cli_row["read"], cli_row["archived"], cli_row["status"]),
                         (web_row["read"], web_row["archived"], web_row["status"]))
        job = web_jobs._Job("gather")
        self.assertIn(cli_row["status"], ("complete", "incomplete", "failed",
                                          "unavailable", "unknown"))

    def test_legacy_plugin_signature_still_collects(self):
        class Old(Source):
            name = "old"

            def run(self, conn, cfg, *, limit=1000):  # no collection_id/progress/record
                report = base.IngestReport(stream=self.name)
                base.deliver(conn, report, stream=self.name, external_id="o1",
                             ts=db.now(), text="old plugin note about dinner?",
                             thread="t", handle="friend@example.com")
                return report

        cid = archive.open_collection(self.conn, mode="cli")
        try:
            report = catch_up(Old(), self.conn, self.cfg, collection_id=cid)
        finally:
            archive.close_collection(self.conn, cid)
        self.assertEqual(report.archived, 1)
        row = self.conn.execute(
            "SELECT * FROM collection_sources WHERE collection_id = ?",
            (cid,)).fetchone()
        self.assertEqual(row["archived"], 1)
        self.assertEqual(row["status"], "complete")

    def test_old_rows_migrate_and_remigrate_harmlessly(self):
        path = Path(self.tmp.name) / "legacy.db"
        conn = db.connect(path)
        try:
            conn.executescript(
                "CREATE TABLE collections(id INTEGER PRIMARY KEY, started_at TEXT,"
                " finished_at TEXT, mode TEXT, read INTEGER DEFAULT 0,"
                " archived INTEGER DEFAULT 0, passed INTEGER DEFAULT 0, error TEXT);"
                "CREATE TABLE collection_sources(collection_id INTEGER, stream TEXT,"
                " read INTEGER DEFAULT 0, archived INTEGER DEFAULT 0,"
                " passed INTEGER DEFAULT 0, muted INTEGER DEFAULT 0,"
                " too_old INTEGER DEFAULT 0, error TEXT, note TEXT,"
                " finished_at TEXT, PRIMARY KEY(collection_id, stream));"
                "INSERT INTO collections(id, started_at, mode) VALUES(1, '2026-01-01', 'cli');"
                "INSERT INTO collection_sources(collection_id, stream, finished_at)"
                " VALUES(1, 'email', '2026-01-01T00:00:00');")
            conn.commit()
            db.migrate(conn)
            row = conn.execute(
                "SELECT * FROM collection_sources WHERE collection_id = 1").fetchone()
            self.assertEqual(row["status"], "unknown")
            db.migrate(conn)
            row = conn.execute(
                "SELECT * FROM collection_sources WHERE collection_id = 1").fetchone()
            self.assertEqual(row["status"], "unknown")
            due, _ = archive.source_due(conn, "email", self.cfg)
            self.assertTrue(due)
        finally:
            conn.close()


class TestSyntheticDaytimeDelivery(_Base):
    def test_message_reaches_spool_without_model_work(self):
        from memcal import llm

        src = _Scripted([("messages", ["day1"], False)], name="daytime")
        with mock.patch.object(llm, "client_for",
                               side_effect=AssertionError("no model on collection")):
            cid, report, row = self._collect(src)
        self.assertEqual(report.archived, 1)
        archived = self.conn.execute(
            "SELECT * FROM archive WHERE stream = 'daytime' AND external_id = 'day1'"
        ).fetchone()
        self.assertIsNotNone(archived)
        spooled = self.conn.execute(
            "SELECT * FROM spool WHERE archive_id = ?", (archived["id"],)).fetchone()
        self.assertIsNotNone(spooled)
        # Re-delivery keeps identity and does not duplicate.
        before = self.conn.execute(
            "SELECT count(*) n FROM archive WHERE stream='daytime'").fetchone()["n"]
        _cid2, report2, _row2 = self._collect(
            _Scripted([("messages", ["day1"], False)], name="daytime"))
        after = self.conn.execute(
            "SELECT count(*) n FROM archive WHERE stream='daytime'").fetchone()["n"]
        self.assertEqual(before, after)
        self.assertEqual(report2.archived, 0)

    def test_blocked_sender_and_mute_still_hold(self):
        from memcal import gate
        identity.set_sender(self.conn, "spam@example.com", "ignore", "no",
                            source="you")
        verdict = gate.gate_email(self.conn, address="spam@example.com",
                                  subject="dinner?")
        self.assertFalse(verdict)
        self.assertTrue(verdict.excluded)
        threads.record(self.conn, "daytime", "hushed", is_group=True)
        self.conn.execute(
            "UPDATE threads SET decision='mute' WHERE stream='daytime' AND thread='hushed'")
        self.conn.commit()
        report2 = base.IngestReport(stream="daytime")
        base.deliver(self.conn, report2, stream="daytime", external_id="m1",
                     ts=db.now(), text="hushed note about dinner?",
                     thread="hushed", handle="friend@example.com", is_group=True)
        self.assertEqual(report2.muted, 1)
        self.assertEqual(report2.passed, 0)


if __name__ == "__main__":
    unittest.main()
