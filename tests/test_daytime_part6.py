"""Part 6: the whole daytime feature, end to end, with honest substitutes.

The model's verdicts are the one part no deterministic test can supply, so a
small substitute stands in for `propose_all` (and the merge arbiter): it reads
the real bundles and returns diffs for lines matching the story. Everything
else — collection, due selection, bundling, apply, evidence, review marks,
spool accounting, brief rendering, and activity reads — is production code.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import activity, archive, brief, db, live
from memcal.config import Config
from memcal.sources import base, catch_up
from memcal.sources.spec import Source


class Scripted(Source):
    """A daytime transport with queued pages, through the real CLI path."""

    name = "chat"
    description = "synthetic daytime transport"
    in_all = True

    def __init__(self, pages, thread="poker group"):
        self.pages = [list(p) for p in pages]
        self.thread = thread
        self.fetched = 0

    def check(self, cfg):
        return True, "ready"

    def fetch(self, conn, cfg, report, limit):
        if not self.pages:
            report.more = False
            return
        for eid, text in self.pages.pop(0):
            self.fetched += 1
            base.deliver(conn, report, stream="chat", external_id=eid,
                         ts=db.now(), text=text, thread=self.thread,
                         handle="friend@example.com")
        report.more = bool(self.pages)


class ModelSub:
    """Deterministic stand-in for propose verdicts. Reads bundles, writes diffs.

    Rules are (needle, make) pairs: the first rule whose needle appears in a
    bundle item's text produces that bundle's diff. Bundles matching nothing
    come back reviewed with an empty diff — read, no change. It never sees the
    test's expectations, only the same bundle items a model would read.
    """

    def __init__(self, rules):
        self.rules = list(rules)
        self.calls = 0

    def propose_all(self, client, conn, cfg, batch, run_id=None, progress=None):
        self.calls += 1
        got = []
        for bundle in batch:
            diff = {"events": []}
            for item in bundle.items:
                text = item["text"] if "text" in item.keys() else ""
                for needle, make in self.rules:
                    if needle in (text or ""):
                        row = make(bundle, item)
                        if row is not None and row not in diff["events"]:
                            diff["events"].append(row)
                        break
            got.append((bundle, diff, "gen-sub"))
        return got, [], []


def _aid(bundle, item):
    return int(item["id"]) if "id" in item.keys() and item["id"] else 0


class _Usage:
    def summary(self):
        return ""


class _Client:
    usage = _Usage()


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)
        self.addCleanup(self.conn.close)
        db.set_today(None)
        self.addCleanup(db.set_today, None)
        self.model_calls = []
        llm_patch = mock.patch("memcal.llm.client_for",
                               side_effect=self._boom)
        llm_patch.start()
        self.addCleanup(llm_patch.stop)

    def _boom(self, *args, **kwargs):
        self.model_calls.append((args, kwargs))
        raise AssertionError("no model on the daytime path")

    def at(self, text):
        db.set_today(text)

    def tick(self, source, *, due=True):
        """One production CLI collection with this transport registered."""
        from memcal import sources as sources_pkg
        args = argparse.Namespace(home=str(self.cfg.home), stream="all",
                                  stale=False, due=due, limit=100, rounds=5)
        with mock.patch.object(sources_pkg, "all_sources", return_value=[source]), \
                mock.patch.object(sources_pkg, "get", return_value=source), \
                mock.patch("memcal.identity.refresh_contacts",
                           return_value=(0, "")), \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            from memcal import cli
            return cli.cmd_ingest(args)

    def night(self, sub):
        """One full production dream pass; only the verdicts are substituted."""
        from memcal.dream import run as dream_run

        with mock.patch.object(dream_run, "llm"), \
                mock.patch("memcal.dream.propose.propose_all", sub.propose_all), \
                mock.patch("memcal.dream.merge.merge_all",
                           side_effect=lambda c, cfg, p, **kw: (p, [])):
            dream_run.llm.client_for.return_value = _Client()
            return dream_run.dream(self.conn, self.cfg, mode="nightly",
                                   skip_sweep=True)

    def handle(self, key):
        row = self.conn.execute("SELECT id FROM events WHERE key = ?",
                                (key,)).fetchone()
        return f"E{row['id']}"

    def spool_pending(self):
        return self.conn.execute(
            "SELECT count(*) n FROM spool WHERE processed_at IS NULL"
            ).fetchone()["n"]


class TestPokerLifecycle(_Base):
    """Saturday established overnight, moved Sunday midday, stable that night."""

    def test_collect_hint_read_correct_redream(self):
        friday = Scripted([[("sat1", "poker Saturday 8pm at Jordan's old place?")]])
        self.at("2026-09-11T22:00:00")
        self.assertEqual(self.tick(friday, due=False), 0)
        sub = ModelSub([("Saturday", lambda b, i: {
            "title": "Poker night", "date": "2026-09-12", "time": "8pm",
            "location": "Jordan's", "status": "confirmed", "subject": "me",
            "cite_ids": [x for x in (_aid(b, j) for j in b.items) if x]})])
        result = self.night(sub)
        self.assertGreater(sub.calls, 0)
        key = "poker-night@2026-09-12"
        row = self.conn.execute("SELECT * FROM events WHERE key = ?",
                                (key,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual((row["status"], row["location"]),
                         ("confirmed", "Jordan's"))
        sat_id = self.conn.execute(
            "SELECT id FROM archive WHERE external_id='sat1'").fetchone()["id"]
        self.assertIn(sat_id, [r["archive_id"] for r in self.conn.execute(
            "SELECT archive_id FROM evidence WHERE kind='event' AND ref=?", (key,))])
        self.assertEqual(self.spool_pending(), 0)

        # Saturday 11:00: the group moves it. A due tick collects, no model.
        calls_before = len(self.model_calls)
        saturday = Scripted([[("sun1", "moved to Sunday at 5 Oak?")]])
        self.at("2026-09-12T11:00:00")
        self.assertEqual(self.tick(saturday), 0)
        self.assertEqual(len(self.model_calls), calls_before)
        text = brief.render(self.conn, self.cfg)
        self.assertIn("New activity: chat/poker group", text)
        self.assertIn("may have changed", text)

        # Noon, old session: open the hint, apply the cited correction.
        page = activity.read(self.conn, "event", key)
        sun_id = self.conn.execute(
            "SELECT id FROM archive WHERE external_id='sun1'").fetchone()["id"]
        self.assertEqual([i["id"] for i in page["items"]], [sun_id])
        self.assertIn("Sunday", page["items"][0]["text"])
        live.update_event(self.conn, self.cfg, key, when="2026-09-13",
                          location="5 Oak",
                          origin=live.Origin.of("test", cited=[sun_id]))
        self.assertEqual(activity.pending(self.conn, "event", key)["strong"], [])

        # That night: reprocessing keeps one row, one date, linked evidence.
        before_evidence = self.conn.execute(
            "SELECT count(*) n FROM evidence WHERE kind='event' AND ref=?",
            (key,)).fetchone()["n"]
        self.at("2026-09-13T03:00:00")
        self.night(ModelSub([]))
        after = self.conn.execute("SELECT * FROM events WHERE key = ?",
                                  (key,)).fetchone()
        self.assertEqual((after["date"], after["location"]),
                         ("2026-09-13", "5 Oak"))
        self.assertEqual(self.conn.execute(
            "SELECT count(*) n FROM events WHERE title LIKE 'Poker%'").fetchone()["n"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) n FROM evidence WHERE kind='event' AND ref=?",
            (key,)).fetchone()["n"], before_evidence)
        # Re-delivery of the same Sunday line duplicates nothing.
        again = Scripted([[("sun1", "moved to Sunday at 5 Oak?")]])
        self.at("2026-09-13T11:00:00")
        self.assertEqual(self.tick(again), 0)
        self.assertEqual(self.conn.execute(
            "SELECT date FROM events WHERE key = ?", (key,)).fetchone()["date"],
            "2026-09-13")

    def test_dream_can_apply_the_move_itself(self):
        friday = Scripted([[("sat1", "poker Saturday 8pm?")]])
        self.at("2026-09-11T22:00:00")
        self.tick(friday, due=False)
        est = ModelSub([("Saturday", lambda b, i: {
            "title": "Poker night", "date": "2026-09-12", "status": "confirmed",
            "subject": "me",
            "cite_ids": [x for x in (_aid(b, j) for j in b.items) if x]})])
        self.night(est)
        key = "poker-night@2026-09-12"
        day = Scripted([[("sun1", "moved to Sunday?")]])
        self.at("2026-09-12T11:00:00")
        self.tick(day)
        sun_id = self.conn.execute(
            "SELECT id FROM archive WHERE external_id='sun1'").fetchone()["id"]
        move = ModelSub([("Sunday", lambda b, i: {
            "key": key, "title": "Poker night", "date": "2026-09-13",
            "cite_ids": [x for x in (_aid(b, j) for j in b.items)
                         if x == sun_id] or [sun_id]})])
        self.at("2026-09-13T03:00:00")
        self.night(move)
        self.assertEqual(self.conn.execute(
            "SELECT date FROM events WHERE key = ?", (key,)).fetchone()["date"],
            "2026-09-13")
        self.assertIn(sun_id, [r["archive_id"] for r in self.conn.execute(
            "SELECT archive_id FROM evidence WHERE kind='event' AND ref=?", (key,))])


class TestCompanions(_Base):
    def _poker_saturday(self):
        friday = Scripted([[("sat1", "poker Saturday 8pm at Jordan's?")]])
        self.at("2026-09-11T22:00:00")
        self.tick(friday, due=False)
        sub = ModelSub([("Saturday", lambda b, i: {
            "title": "Poker night", "date": "2026-09-12", "location": "Jordan's",
            "status": "confirmed", "subject": "me",
            "cite_ids": [x for x in (_aid(b, j) for j in b.items) if x]})])
        self.night(sub)
        return "poker-night@2026-09-12"

    def test_wrong_assistant_answer_leaves_no_trace(self):
        key = self._poker_saturday()
        # Yesterday's assistant said "Saturday" again after the move. That prose
        # was never collected, so the night pass cannot promote it.
        self.assertEqual(self.conn.execute(
            "SELECT count(*) n FROM archive WHERE text LIKE '%assistant%'"
            ).fetchone()["n"], 0)
        day = Scripted([[("sun1", "moved to Sunday?")]])
        self.at("2026-09-12T11:00:00")
        self.tick(day)
        self.at("2026-09-13T03:00:00")
        self.night(ModelSub([]))
        n_evidence = self.conn.execute(
            "SELECT count(*) n FROM evidence WHERE kind='event' AND ref=?",
            (key,)).fetchone()["n"]
        self.assertGreaterEqual(n_evidence, 1)
        rows = self.conn.execute(
            "SELECT text FROM archive JOIN evidence ON evidence.archive_id = archive.id"
            " WHERE evidence.kind='event' AND evidence.ref=?", (key,)).fetchall()
        self.assertFalse(any("assistant" in r["text"] for r in rows))

    def test_year_old_address_survives_as_history(self):
        self.at("2024-03-01T10:00:00")
        old = Scripted([[("old1", "poker at 12 Elm this Friday?")]])
        self.tick(old, due=False)
        self.night(ModelSub([("12 Elm", lambda b, i: {
            "title": "Poker night", "date": "2024-03-08", "status": "confirmed",
            "subject": "me",
            "cite_ids": [x for x in (_aid(b, j) for j in b.items) if x]})]))
        key = self._poker_saturday()
        self.at("2026-09-13T03:00:00")
        self.night(ModelSub([]))
        self.assertEqual(self.conn.execute(
            "SELECT location FROM events WHERE key=?", (key,)).fetchone()
            ["location"], "Jordan's")
        found = archive.search_filtered(self.conn, "12 Elm")
        self.assertTrue(any("12 Elm" in r["text"] for r in found))
        self.assertTrue(any(str(r["ts"]).startswith("2024") for r in found))

    def test_delayed_stale_confirmation_changes_nothing(self):
        key = self._poker_saturday()
        day = Scripted([[("sun1", "moved to Sunday?")]])
        self.at("2026-09-12T11:00:00")
        self.tick(day)
        sun_id = self.conn.execute(
            "SELECT id FROM archive WHERE external_id='sun1'").fetchone()["id"]
        live.update_event(self.conn, self.cfg, key, when="2026-09-13",
                          origin=live.Origin.of("test", cited=[sun_id]))
        late = Scripted([[("late1", "confirming Saturday 8pm, see you there?")]])
        self.at("2026-09-12T15:00:00")
        self.tick(late)
        found = activity.pending(self.conn, "event", key)
        self.assertEqual(len(found["strong"]), 1)  # new arrival, old meaning
        self.at("2026-09-13T03:00:00")
        self.night(ModelSub([]))
        self.assertEqual(self.conn.execute(
            "SELECT date FROM events WHERE key=?", (key,)).fetchone()["date"],
            "2026-09-13")
        self.assertEqual(activity.pending(self.conn, "event", key)["strong"], [])

    def test_different_occurrence_stays_a_second_row(self):
        key = self._poker_saturday()
        other = Scripted([[("fri1", "separate Friday game at Sam's?")]])
        self.at("2026-09-12T12:00:00")
        self.tick(other)
        fri_id = self.conn.execute(
            "SELECT id FROM archive WHERE external_id='fri1'").fetchone()["id"]
        second = ModelSub([("Friday game", lambda b, i: {
            "title": "Poker Friday", "date": "2026-09-18", "status": "mentioned",
            "subject": "me", "cite_ids": [fri_id]})])
        self.at("2026-09-13T03:00:00")
        self.night(second)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) n FROM events WHERE title LIKE 'Poker%'").fetchone()["n"], 2)

    def test_unrelated_chatter_nominates_nothing(self):
        key = self._poker_saturday()
        noise = Scripted([[("n1", "did anyone feed the cat?")]],
                         thread="roommates")
        self.at("2026-09-12T12:00:00")
        self.tick(noise)
        self.assertNotIn("New activity", brief.render(self.conn, self.cfg))
        self.at("2026-09-13T03:00:00")
        self.night(ModelSub([]))
        self.assertEqual(self.conn.execute(
            "SELECT count(*) n FROM events WHERE title LIKE 'Poker%'").fetchone()["n"], 1)

    def test_source_failure_marks_nothing_reviewed(self):
        from memcal.sources.spec import SourceError

        class Down(Source):
            name = "chat"
            in_all = True

            def fetch(self, conn, cfg, report, limit):
                raise SourceError("bridge closed")

        self.at("2026-09-12T11:00:00")
        self.assertEqual(self.tick(Down()), 1)
        row = self.conn.execute(
            "SELECT status FROM collection_sources ORDER BY collection_id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) n FROM reviewed_lines").fetchone()["n"], 0)

    def test_partial_review_leaves_the_rest_pending(self):
        key = self._poker_saturday()
        day = Scripted([[("sun1", "moved to Sunday?")]])
        self.at("2026-09-12T11:00:00")
        self.tick(day)
        # A pass that only reads some bundles: return a diff for none of them.
        # Nothing reviewed, everything still queued.
        self.at("2026-09-13T03:00:00")
        from memcal.dream import run as dream_run
        with mock.patch.object(dream_run, "llm"), \
                mock.patch("memcal.dream.propose.propose_all",
                           return_value=([], [], [])), \
                mock.patch("memcal.dream.merge.merge_all",
                           side_effect=lambda c, cfg, p, **kw: (p, [])):
            dream_run.llm.client_for.return_value = _Client()
            dream_run.dream(self.conn, self.cfg, mode="nightly", skip_sweep=True)
        self.assertGreater(self.spool_pending(), 0)
        self.assertEqual(len(activity.pending(self.conn, "event", key)["strong"]), 1)

    def test_arrival_during_the_pass_survives_it(self):
        key = self._poker_saturday()
        day = Scripted([[("sun1", "moved to Sunday?")]])
        self.at("2026-09-12T11:00:00")
        self.tick(day)
        sun_id = self.conn.execute(
            "SELECT id FROM archive WHERE external_id='sun1'").fetchone()["id"]

        def proposing(client, conn, cfg, batch, run_id=None, progress=None):
            mid = Scripted([[("mid1", "and bring chips?")]])
            cid = archive.open_collection(conn, mode="cli")
            try:
                catch_up(mid, conn, cfg, collection_id=cid)
            finally:
                archive.close_collection(conn, cid)
            # The snapshot bundles are read with no actionable diff; the
            # mid-pass arrival never entered a bundle.
            return ([(b, {"events": []}, "gen-sub") for b in batch], [], [])

        self.at("2026-09-13T03:00:00")
        from memcal.dream import run as dream_run
        with mock.patch.object(dream_run, "llm"), \
                mock.patch("memcal.dream.propose.propose_all",
                           side_effect=proposing), \
                mock.patch("memcal.dream.merge.merge_all",
                           side_effect=lambda c, cfg, p, **kw: (p, [])):
            dream_run.llm.client_for.return_value = _Client()
            dream_run.dream(self.conn, self.cfg, mode="nightly", skip_sweep=True)
        found = activity.pending(self.conn, "event", key)
        texts = [i["text"] for i in found["strong"]]
        self.assertIn("and bring chips?", texts)
        covered = activity.reviewed_ids(self.conn, "event", key)
        self.assertIn(sun_id, covered)
        mid = self.conn.execute(
            "SELECT id FROM archive WHERE external_id='mid1'").fetchone()["id"]
        self.assertNotIn(mid, covered)


class TestWeekendCoverage(_Base):
    def test_prepared_and_unreviewed_sit_side_by_side(self):
        self.at("2026-09-11T22:00:00")
        friday = Scripted([[("sat1", "poker Saturday 8pm?")]])
        self.tick(friday, due=False)
        self.night(ModelSub([("Saturday", lambda b, i: {
            "title": "Poker night", "date": "2026-09-12", "status": "opportunity",
            "subject": "me",
            "cite_ids": [x for x in (_aid(b, j) for j in b.items) if x]})]))
        self.at("2026-09-12T09:00:00")
        stranger = Scripted([[("r1", "rooftop party tonight?")]])
        stranger.name = "chat"
        # A different thread needs a different transport name on the same stream.
        from memcal.sources import base as base_mod
        report = base_mod.IngestReport(stream="chat")
        base_mod.deliver(self.conn, report, stream="chat", external_id="r1",
                         ts=db.now(), text="rooftop party tonight?",
                         thread="rooftop crew", handle="stranger@example.com")
        self.conn.commit()
        text = brief.render(self.conn, self.cfg)
        self.assertIn("Poker night", text)
        self.assertIn("UNREVIEWED: chat/rooftop crew (1 waiting)", text)
        self.assertNotIn("rooftop party tonight", text)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) n FROM events").fetchone()["n"], 1)


class TestNoModelOnTheDaytimePath(_Base):
    def test_ticks_renders_reads_and_hints_are_free(self):
        from memcal import llm
        self.at("2026-09-11T22:00:00")
        friday = Scripted([[("sat1", "poker Saturday 8pm?")]])
        self.tick(friday, due=False)
        self.night(ModelSub([("Saturday", lambda b, i: {
            "title": "Poker night", "date": "2026-09-12", "status": "confirmed",
            "subject": "me",
            "cite_ids": [x for x in (_aid(b, j) for j in b.items) if x]})]))
        key = "poker-night@2026-09-12"
        for minute in (0, 5, 10):
            self.at(f"2026-09-12T11:{minute:02d}:00")
            day = Scripted([[("m%d" % minute, "chatter %d?" % minute)]])
            with mock.patch.object(llm, "client_for",
                                   side_effect=AssertionError("tick spent model")):
                self.tick(day)
                brief.render(self.conn, self.cfg)
                activity.pending(self.conn, "event", key)
                activity.read(self.conn, "event", key)
                activity.unlinked_backlog(self.conn)
        self.assertEqual(self.model_calls, [])
        page = activity.read(self.conn, "event", key, limit=2)
        self.assertEqual(page["total"], 3)
        self.assertEqual(len(page["items"]), 2)
        self.assertEqual(page["omitted"], 1)


if __name__ == "__main__":
    unittest.main()
