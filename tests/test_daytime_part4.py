"""Part 4: hints beside the plan, bounded activity reads, honest coverage."""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import activity, archive, brief, db, live, threads
from memcal.config import Config
from memcal.sources import base


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)
        db.set_today("2026-09-12")
        self.addCleanup(db.set_today, None)

    def tearDown(self):
        self.conn.close()

    def collect(self, stream, eid, text, thread, handle="friend@example.com",
                ts=None, **kw):
        from memcal.sources.base import IngestReport
        report = IngestReport(stream=stream)
        base.deliver(self.conn, report, stream=stream, external_id=eid,
                     ts=ts or db.now(), text=text, thread=thread,
                     handle=handle, **kw)
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM archive WHERE stream = ? AND external_id = ?",
            (stream, eid)).fetchone()
        return row["id"] if row else None

    def poker(self, when="2026-09-12"):
        event, _ = live.add_event(self.conn, self.cfg, title="Poker night",
                                  when=when, origin=live.Origin.of("test"))
        return event

    def handle(self, event):
        return brief.source_tag("event", event.id).strip("〔〕")


class TestHintPointsAtTheCorrection(_Base):
    def test_the_row_keeps_its_details_and_names_how_to_read(self):
        m1 = self.collect("chat", "m1", "poker saturday 8pm at Jordan's?",
                          "poker group")
        event = self.poker()
        live.update_event(self.conn, self.cfg, event.key, note="saturday plan",
                          origin=live.Origin.of("test", cited=[m1]))
        self.collect("chat", "m2", "moved to Sunday at 5 Oak?", "poker group")
        text = brief.render(self.conn, self.cfg)
        self.assertIn("Saturday", text)
        self.assertIn("New activity: chat/poker group", text)
        self.assertIn("may have changed", text)
        self.assertNotIn("Sunday", text)
        self.assertNotIn("5 Oak", text)
        # Opening the hint returns the correction with no keyword search.
        page = activity.read(self.conn, "event", event.key)
        self.assertEqual([i["text"] for i in page["items"]],
                         ["moved to Sunday at 5 Oak?"])
        self.assertEqual(page["items"][0]["who"] != "", True)

    def test_quiet_plans_get_no_hint(self):
        m1 = self.collect("chat", "m1", "poker saturday?", "poker group")
        event = self.poker()
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        self.assertNotIn("New activity", brief.render(self.conn, self.cfg))


class TestBoundedReads(_Base):
    def _twenty_five(self, event):
        m1 = self.collect("chat", "m1", "poker saturday?", "poker group")
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        ids = [self.collect("chat", f"n{i}", f"update number {i}?",
                            "poker group") for i in range(25)]
        return m1, ids

    def test_pages_chain_without_dups_or_gaps(self):
        event = self.poker()
        _m1, ids = self._twenty_five(event)
        seen, cursor = [], 0
        for _ in range(3):
            page = activity.read(self.conn, "event", event.key, cursor=cursor,
                                 limit=10)
            seen.extend(i["id"] for i in page["items"])
            cursor = page["next_cursor"]
            if not page["omitted"]:
                break
        self.assertEqual(seen, ids)
        # Late arrivals between page reads join the tail; nothing repeats.
        late = self.collect("chat", "late1", "one more thing?", "poker group")
        page = activity.read(self.conn, "event", event.key, cursor=cursor,
                             limit=10)
        self.assertEqual([i["id"] for i in page["items"]], [late])

    def test_reading_marks_nothing_reviewed(self):
        event = self.poker()
        _m1, ids = self._twenty_five(event)
        covered_before = activity.reviewed_ids(self.conn, "event", event.key)
        activity.read(self.conn, "event", event.key, limit=10)
        self.assertEqual(activity.reviewed_ids(self.conn, "event", event.key),
                         covered_before)
        self.assertEqual(len(activity.pending(self.conn, "event", event.key)
                             ["strong"]), 25)

    def test_noisy_histories_stay_bounded(self):
        event = self.poker()
        m1 = self.collect("chat", "m1", "poker saturday?", "poker group")
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        for i in range(500):
            self.collect("chat", f"z{i}", f"chatter {i}?", "poker group")
        text = brief.render(self.conn, self.cfg)
        hints = [line for line in text.splitlines() if "New activity" in line]
        self.assertLessEqual(len(hints), 3)
        page = activity.read(self.conn, "event", event.key, limit=20)
        self.assertLessEqual(len(page["items"]), 20)
        self.assertEqual(page["total"], 500)
        self.assertGreater(page["omitted"], 0)


class TestBacklogAndCoverage(_Base):
    def test_unlinked_invitation_is_visible_but_not_a_plan(self):
        for i in range(3):
            self.collect("chat", f"u{i}", f"rooftop party friday {i}?",
                         "rooftop crew")
        text = brief.render(self.conn, self.cfg)
        self.assertIn("UNREVIEWED: chat/rooftop crew (3 waiting)", text)
        self.assertNotIn("rooftop party", text)
        # Muted chatter never leaks through the backlog surface.
        threads.record(self.conn, "chat", "rooftop crew", is_group=True)
        self.conn.execute("UPDATE threads SET decision='mute'"
                          " WHERE stream='chat' AND thread='rooftop crew'")
        self.conn.commit()
        self.assertNotIn("UNREVIEWED", brief.render(self.conn, self.cfg))

    def test_failed_collection_warns_distinctly_from_activity(self):
        from memcal.sources.spec import Source, SourceError

        class Down(Source):
            name = "chatdown"
            in_all = True

            def fetch(self, conn, cfg, report, limit):
                raise SourceError("bridge closed")

        from memcal.sources import catch_up
        cid = archive.open_collection(self.conn, mode="cli")
        try:
            catch_up(Down(), self.conn, self.cfg, collection_id=cid)
        finally:
            archive.close_collection(self.conn, cid)
        with mock.patch("memcal.sources.all_sources", return_value=[Down()]):
            text = brief.render(self.conn, self.cfg)
        self.assertIn("[COLLECTION: chatdown (bridge closed)", text)

    def test_trim_keeps_hints_with_their_events(self):
        m1 = self.collect("chat", "m1", "poker saturday?", "poker group")
        event = self.poker()
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        self.collect("chat", "m2", "moved to Sunday?", "poker group")
        self.cfg.brief_token_cap = 700
        text = brief.render(self.conn, self.cfg)
        lines = text.splitlines()
        event_at = next(i for i, line in enumerate(lines) if "Poker night" in line)
        hint_at = next(i for i, line in enumerate(lines) if "New activity" in line)
        self.assertEqual(hint_at, event_at + 1)


class TestSurfacesAgree(_Base):
    def _state(self):
        m1 = self.collect("chat", "m1", "poker saturday?", "poker group")
        event = self.poker()
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        m2 = self.collect("chat", "m2", "moved to Sunday?", "poker group")
        return event, m1, m2

    def test_cli_mcp_and_core_report_the_same_activity(self):
        import argparse
        from memcal import cli, mcp_server
        event, _m1, m2 = self._state()
        handle = self.handle(event)
        args = argparse.Namespace(home=str(self.cfg.home), ref=handle, cursor=0,
                                  limit=20)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.cmd_activity(args), 0)
        self.assertIn("moved to Sunday?", out.getvalue())
        server = mcp_server.Server.__new__(mcp_server.Server)
        server.conn, server.cfg = self.conn, self.cfg
        server.harness, server.session = "openclaw", ""
        text = server.call("memcal_activity", {"handle": handle})
        self.assertIn("moved to Sunday?", text)
        self.assertIn(str(m2), text)
        core = activity.read(self.conn, "event", event.key)
        self.assertEqual([i["id"] for i in core["items"]], [m2])

    def test_cli_backlog_and_reviewed_round_trip(self):
        import argparse
        from memcal import cli
        event, _m1, m2 = self._state()
        handle = self.handle(event)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.cmd_activity(argparse.Namespace(
                home=str(self.cfg.home), ref=None, cursor=0, limit=20)), 0)
        self.assertIn("no unlinked traffic", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli.cmd_reviewed(argparse.Namespace(
                home=str(self.cfg.home), ref=handle, source_ids=str(m2)))
        self.assertEqual(rc, 0)
        self.assertIn("no change", out.getvalue())
        self.assertNotIn("New activity", brief.render(self.conn, self.cfg))

    def test_render_and_readers_dispatch_no_model(self):
        from memcal import llm, mcp_server
        event, _m1, _m2 = self._state()
        handle = self.handle(event)
        server = mcp_server.Server.__new__(mcp_server.Server)
        server.conn, server.cfg = self.conn, self.cfg
        server.harness, server.session = "openclaw", ""
        with mock.patch.object(llm, "client_for",
                               side_effect=AssertionError("rendering is free")):
            brief.render(self.conn, self.cfg)
            activity.read(self.conn, "event", event.key)
            activity.pending(self.conn, "event", event.key)
            activity.unlinked_backlog(self.conn)
            server.call("memcal_activity", {"handle": handle})
            server.call("memcal_brief", {})
            server.call("memcal_refresh", {})

    def test_ordinary_archive_tools_still_work(self):
        event, _m1, _m2 = self._state()
        self.assertTrue(archive.search(self.conn, "Sunday"))
        self.assertTrue(archive.search_filtered(self.conn, "", thread="poker group"))


def _pairs_hold(text):
    """Every warning line sits directly under the event row it qualifies."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("  ↳ New activity:"):
            assert i > 0, "hint with no row above it"
            above = lines[i - 1]
            assert above.startswith("〔") or above.startswith("↳ 〔"), \
                f"orphaned hint after {above!r}"


class TestTrimKeepsUnits(_Base):
    def _eleven_affected(self):
        events = []
        for n in range(11):
            m = self.collect("chat", f"e{n}m", f"plan {n} saturday?", f"group {n}")
            event, _ = live.add_event(self.conn, self.cfg, title=f"Plan {n}",
                                      when="2026-09-12",
                                      origin=live.Origin.of("test"))
            live.update_event(self.conn, self.cfg, event.key, note="plan",
                              origin=live.Origin.of("test", cited=[m]))
            self.collect("chat", f"e{n}n", f"plan {n} moved?", f"group {n}")
            events.append(event)
        return events

    def test_units_drop_whole_or_not_at_all(self):
        self._eleven_affected()
        self.cfg.brief_token_cap = 1500
        _pairs_hold(brief.render(self.conn, self.cfg))
        self.cfg.brief_token_cap = 500
        text = brief.render(self.conn, self.cfg)
        _pairs_hold(text)

    def test_overflow_names_the_rows_it_stands_in_for(self):
        events = self._eleven_affected()
        text = brief.render(self.conn, self.cfg)
        inline = [line for line in text.splitlines()
                  if line.startswith("  ↳ New activity:")]
        self.assertEqual(len(inline), 8)
        overflow = [line for line in text.splitlines()
                    if "more row(s) with new activity" in line]
        self.assertEqual(len(overflow), 1)
        named = overflow[0].split(":", 1)[1]
        missing = [e for e in events[8:]
                   if brief.source_tag("event", e.id).strip("〔〕") not in named]
        self.assertEqual(missing, [])

    def test_hard_cut_discloses_and_orphans_nothing(self):
        from memcal import textclean
        events = self._eleven_affected()
        full = brief.render(self.conn, self.cfg)
        # A budget that fits the header plus one event row but no warning.
        lines = full.splitlines()
        first_event = next(i for i, line in enumerate(lines)
                           if line.startswith("〔"))
        cap = textclean.estimate_tokens(
            "\n".join(lines[:first_event + 1]).rstrip() + "\n… (trimmed)\n")
        self.cfg.brief_token_cap = cap
        text = brief.render(self.conn, self.cfg)
        self.assertIn("… (trimmed)", text)
        _pairs_hold(text)
        self.assertNotIn("New activity", text)

    def test_coverage_warnings_survive_a_tight_cap(self):
        from memcal.sources.spec import Source, SourceError
        from memcal.sources import catch_up

        class Down(Source):
            name = "chatdown"
            in_all = True

            def fetch(self, conn, cfg, report, limit):
                raise SourceError("bridge closed")

        cid = archive.open_collection(self.conn, mode="cli")
        try:
            catch_up(Down(), self.conn, self.cfg, collection_id=cid)
        finally:
            archive.close_collection(self.conn, cid)
        for i in range(2):
            self.collect("chat", f"u{i}", f"party {i}?", f"crew {i}")
        self.cfg.brief_token_cap = 600
        with mock.patch("memcal.sources.all_sources",
                        return_value=[Down()]):
            text = brief.render(self.conn, self.cfg)
        self.assertIn("[COLLECTION: chatdown (bridge closed)", text)
        self.assertIn("[UNREVIEWED: chat/crew", text)


class TestIncompleteCoverage(_Base):
    def _fakes(self, *names):
        class Fake:
            def __init__(self, name):
                self.name = name
                self.in_all = True
        return [Fake(n) for n in names]

    def test_partial_page_warns_and_a_full_check_clears_it(self):
        from memcal.sources import catch_up
        from memcal.sources.spec import Source

        class Gappy(Source):
            name = "chat"
            in_all = True

            def fetch(self, conn, cfg, report, limit):
                base.deliver(conn, report, stream="chat", external_id="g1",
                             ts=db.now(), text="half the story?",
                             thread="t", handle="friend@example.com")
                report.more = True

        cid = archive.open_collection(self.conn, mode="cli")
        try:
            catch_up(Gappy(), self.conn, self.cfg, limit=10, rounds=1,
                     collection_id=cid)
        finally:
            archive.close_collection(self.conn, cid)
        with mock.patch("memcal.sources.all_sources",
                                 return_value=self._fakes("chat")):
            text = brief.render(self.conn, self.cfg)
        self.assertIn("[COLLECTION: chat (incomplete — more waiting)", text)
        self.assertNotIn("failed", text)
        cid = archive.open_collection(self.conn, mode="cli")
        try:
            catch_up(Gappy(), self.conn, self.cfg, limit=10, rounds=1,
                     collection_id=cid)
        finally:
            archive.close_collection(self.conn, cid)
        row = self.conn.execute(
            "SELECT status FROM collection_sources ORDER BY collection_id DESC"
            " LIMIT 1").fetchone()
        self.assertEqual(row["status"], "incomplete")
        # An exhausted check clears the warning.
        class Full(Source):
            name = "chat"
            in_all = True

            def fetch(self, conn, cfg, report, limit):
                report.more = False

        cid = archive.open_collection(self.conn, mode="cli")
        try:
            catch_up(Full(), self.conn, self.cfg, collection_id=cid)
        finally:
            archive.close_collection(self.conn, cid)
        with mock.patch("memcal.sources.all_sources",
                                 return_value=self._fakes("chat")):
            self.assertNotIn("[COLLECTION", brief.render(self.conn, self.cfg))

    def test_never_checked_source_named_on_a_live_store(self):
        cid = archive.open_collection(self.conn, mode="cli")
        archive.close_collection(self.conn, cid)
        with mock.patch("memcal.sources.all_sources",
                                 return_value=self._fakes("chat", "mail")):
            text = brief.render(self.conn, self.cfg)
        self.assertIn("chat (not yet checked)", text)
        self.assertIn("mail (not yet checked)", text)

    def test_virgin_store_names_nothing(self):
        with mock.patch("memcal.sources.all_sources",
                                 return_value=self._fakes("chat")):
            self.assertNotIn("[COLLECTION", brief.render(self.conn, self.cfg))


if __name__ == "__main__":
    unittest.main()
