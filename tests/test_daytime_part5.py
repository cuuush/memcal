"""Part 5: yesterday's session gets today's context; assistant prose is not evidence."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PLUGIN = ROOT / "integrations" / "hermes" / "memcal" / "__init__.py"


def _load_plugin():
    """The Hermes adapter with a stubbed provider ABC: no checkout required."""
    import types
    saved = {name: sys.modules.get(name)
             for name in ("agent", "agent.memory_provider", "_memcal_hermes_p5")}
    agent_mod = types.ModuleType("agent")
    mp_mod = types.ModuleType("agent.memory_provider")

    class MemoryProvider:
        pass

    mp_mod.MemoryProvider = MemoryProvider
    agent_mod.memory_provider = mp_mod
    sys.modules["agent"] = agent_mod
    sys.modules["agent.memory_provider"] = mp_mod
    spec = importlib.util.spec_from_file_location(
        "_memcal_hermes_p5", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_memcal_hermes_p5"] = module
    spec.loader.exec_module(module)
    return module, saved


def _restore_plugin(saved):
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


class _HermesBase(unittest.TestCase):
    """A live provider against a scratch store, without touching the real profile."""

    @classmethod
    def setUpClass(cls):
        cls._memcal_modules = {
            name: module for name, module in sys.modules.items()
            if name == "memcal" or name.startswith("memcal.")}
        cls._sys_path = list(sys.path)
        cls._env = {name: __import__("os").environ.get(name)
                    for name in ("MEMCAL_HOME", "MEMCAL_SRC")}
        cls.tmp = tempfile.TemporaryDirectory()
        import os
        os.environ["MEMCAL_HOME"] = cls.tmp.name
        os.environ["MEMCAL_SRC"] = str(ROOT)
        cls.plugin, cls._saved_plugin = _load_plugin()
        cls.addClassCleanup(cls._cleanup_class)
        # The adapter reloads memcal.* from MEMCAL_SRC on first use: talk to the
        # reloaded copies from here on, or there are two clocks in one process.
        # Submodules import on demand inside the adapter, so import them here to
        # register the reloaded copies.
        provider = cls.plugin.MemcalMemoryProvider()
        provider.initialize("sess", agent_context="primary")
        import memcal.db
        import memcal.live
        import memcal.brief
        import memcal.config
        import memcal.sources.base
        mods = sys.modules
        cls.mdb = mods["memcal.db"]
        cls.mlive = mods["memcal.live"]
        cls.mbrief = mods["memcal.brief"]
        cls.mconfig = mods["memcal.config"]
        cls.mbase = mods["memcal.sources.base"]
        cls.cfg = cls.mconfig.load(cls.tmp.name)
        cls.cfg.ensure_dirs()
        cls.mdb.open_db(cls.cfg.db_path).close()

    @classmethod
    def _cleanup_class(cls):
        import os
        try:
            for name in [n for n in sys.modules
                         if n == "memcal" or n.startswith("memcal.")]:
                sys.modules.pop(name, None)
            sys.modules.update(cls._memcal_modules)
            sys.path[:] = cls._sys_path
            for name, value in cls._env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        finally:
            _restore_plugin(cls._saved_plugin)
            cls.tmp.cleanup()

    def setUp(self):
        import os
        # Fresh scratch store per test: event keys repeat across tests, and the
        # provider suppresses unchanged snapshots per session.
        self._test_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._test_tmp.cleanup)
        self._prev_home = os.environ.get("MEMCAL_HOME")
        os.environ["MEMCAL_HOME"] = self._test_tmp.name
        self.addCleanup(self._restore_home)
        self.cfg = self.mconfig.load(self._test_tmp.name)
        self.cfg.ensure_dirs()
        self.mdb.open_db(self.cfg.db_path).close()
        self.provider = self.plugin.MemcalMemoryProvider()
        self.provider.initialize("sess", agent_context="primary")
        self.mdb.set_today("2026-09-12")
        self.addCleanup(self.mdb.set_today, None)

    def _restore_home(self):
        import os
        if self._prev_home is None:
            os.environ.pop("MEMCAL_HOME", None)
        else:
            os.environ["MEMCAL_HOME"] = self._prev_home

    def conn(self):
        return self.mdb.open_db(self.cfg.db_path)

    def collect(self, conn, eid, text, thread="poker group",
                handle="friend@example.com"):
        report = self.mbase.IngestReport(stream="chat")
        self.mbase.deliver(conn, report, stream="chat", external_id=eid,
                           ts=self.mdb.now(), text=text, thread=thread,
                           handle=handle)
        conn.commit()
        row = conn.execute(
            "SELECT id FROM archive WHERE external_id = ?", (eid,)).fetchone()
        return row["id"] if row else None


class TestOldSessionsGetToday(_HermesBase):
    def test_next_day_turn_supersedes_with_hints(self):
        conn = self.conn()
        try:
            self.provider.on_turn_start(1, "poker saturday?")
            first = self.provider.prefetch("where is poker?")
            self.assertIn("MEMCAL SNAPSHOT", first)
            self.assertNotIn("New activity", first)
            m1 = self.collect(conn, "m1", "poker saturday 8pm?", "poker group")
            event, _ = self.mlive.add_event(
                conn, self.cfg, title="Poker night", when="2026-09-12",
                origin=self.mlive.Origin.of("test"))
            self.mlive.update_event(
                conn, self.cfg, event.key, note="saturday plan",
                origin=self.mlive.Origin.of("test", cited=[m1]))
            self.collect(conn, "m2", "moved to Sunday?", "poker group")
        finally:
            conn.close()
        self.mdb.set_today("2026-09-13")
        second = self.provider.prefetch("where is poker?")
        self.assertIn("MEMCAL SNAPSHOT", second)
        self.assertIn("New activity", second)
        self.assertNotEqual(first, second)
        # A short follow-up with nothing new reuses the standing snapshot.
        self.assertEqual(self.provider.prefetch("and where?"), "")

    def test_resume_reemits_and_failure_warns(self):
        self.provider.on_turn_start(1, "hi")
        first = self.provider.prefetch("hi")
        self.assertIn("MEMCAL SNAPSHOT", first)
        self.provider.on_session_switch("sess2", reset=True)
        again = self.provider.prefetch("hi", session_id="sess2")
        self.assertIn("MEMCAL SNAPSHOT", again)
        with mock.patch.object(self.mbrief, "render",
                               side_effect=RuntimeError("disk gone")):
            failed = self.provider.prefetch("hi")
        self.assertIn("MEMCAL UNAVAILABLE", failed)
        self.assertNotIn("MEMCAL SNAPSHOT", failed)

    def test_user_turn_archived_once_assistant_never(self):
        self.provider.on_turn_start(1, "is poker still saturday?")
        self.provider.on_turn_start(1, "is poker still saturday?")
        self.provider.sync_turn("is poker still saturday?",
                                "Poker is Saturday at 8 at Jordan's")
        conn = self.conn()
        try:
            rows = conn.execute(
                "SELECT text FROM archive WHERE stream = 'agent'").fetchall()
            texts = [r["text"] for r in rows]
        finally:
            conn.close()
        self.assertEqual(len(texts), 1)
        self.assertIn("is poker still saturday?", texts[0].lower())
        self.assertFalse(any("Jordan" in t for t in texts))
        conn = self.conn()
        try:
            n = conn.execute("SELECT count(*) n FROM events").fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(n, 0)

    def test_refresh_tool_renders_despite_the_hash_gate(self):
        self.provider.on_turn_start(1, "hi")
        self.provider.prefetch("hi")
        out = json.loads(self.provider.handle_tool_call("memcal_refresh", {}))
        self.assertIn("## This week", out["snapshot"])


class TestCitedWritesThroughHermes(_HermesBase):
    def _poker_with_activity(self, conn):
        m1 = self.collect(conn, "m1", "poker saturday 8pm?", "poker group")
        event, _ = self.mlive.add_event(
            conn, self.cfg, title="Poker night", when="2026-09-12",
            origin=self.mlive.Origin.of("test"))
        self.mlive.update_event(conn, self.cfg, event.key, note="saturday plan",
                                origin=self.mlive.Origin.of("test", cited=[m1]))
        m2 = self.collect(conn, "m2", "moved to Sunday?", "poker group")
        m3 = self.collect(conn, "m3", "at 5 Oak?", "poker group")
        from memcal import brief as _brief  # only for the handle shape
        _ = _brief
        row = conn.execute("SELECT id FROM events WHERE key = ?",
                           (event.key,)).fetchone()
        handle = f"E{row['id']}"
        return event, handle, m1, m2, m3

    def test_activity_update_reviewed_round_trip(self):
        conn = self.conn()
        try:
            event, handle, _m1, m2, m3 = self._poker_with_activity(conn)
        finally:
            conn.close()
        seen = json.loads(self.provider.handle_tool_call(
            "memcal_activity", {"handle": handle}))
        self.assertEqual([i["id"] for i in seen["items"]], [m2, m3])
        done = json.loads(self.provider.handle_tool_call(
            "memcal_update", {"which": handle, "when": "sunday",
                              "source_ids": [m2]}))
        self.assertIn("Sep 13", done["row"])
        conn = self.conn()
        try:
            cited = [r["archive_id"] for r in conn.execute(
                "SELECT archive_id FROM evidence WHERE kind='event' AND ref=?",
                (event.key,))]
            self.assertIn(m2, cited)
            seen2 = json.loads(self.provider.handle_tool_call(
                "memcal_activity", {"handle": handle}))
            self.assertEqual([i["id"] for i in seen2["items"]], [m3])
            acked = json.loads(self.provider.handle_tool_call(
                "memcal_reviewed", {"handle": handle, "source_ids": [m3]}))
            self.assertIn("no change", acked["reviewed"])
            seen3 = json.loads(self.provider.handle_tool_call(
                "memcal_activity", {"handle": handle}))
            self.assertEqual(seen3["items"], [])
        finally:
            conn.close()

    def test_bogus_citations_fail_cleanly(self):
        conn = self.conn()
        try:
            event, handle, _m1, _m2, _m3 = self._poker_with_activity(conn)
        finally:
            conn.close()
        out = json.loads(self.provider.handle_tool_call(
            "memcal_reviewed", {"handle": handle, "source_ids": [999999]}))
        self.assertIn("error", out)
        conn = self.conn()
        try:
            total = self.provider.handle_tool_call(
                "memcal_activity", {"handle": handle})
            self.assertEqual(len(json.loads(total)["items"]), 2)
        finally:
            conn.close()


class TestAQuestionAcknowledgesNothing(_HermesBase):
    """The turn behind the call is authorship, never reviewed evidence."""

    def _question_turn(self, conn):
        m1 = self.collect(conn, "m1", "poker saturday 8pm?", "poker group")
        event, _ = self.mlive.add_event(
            conn, self.cfg, title="Poker night", when="2026-09-12",
            origin=self.mlive.Origin.of("test"))
        self.mlive.update_event(conn, self.cfg, event.key, note="saturday plan",
                                origin=self.mlive.Origin.of("test", cited=[m1]))
        m2 = self.collect(conn, "m2", "moved to Sunday?", "poker group")
        m3 = self.collect(conn, "m3", "at 5 Oak?", "poker group")
        row = conn.execute("SELECT id FROM events WHERE key = ?",
                           (event.key,)).fetchone()
        return event, f"E{row['id']}", m1, m2, m3

    def test_bounded_read_then_cited_update_keeps_the_rest(self):
        conn = self.conn()
        try:
            event, handle, m1, m2, m3 = self._question_turn(conn)
        finally:
            conn.close()
        self.provider.on_turn_start(1, "When is poker?")
        turn = self._turn_id()
        self.provider.prefetch("When is poker?")
        seen = json.loads(self.provider.handle_tool_call(
            "memcal_activity", {"handle": handle, "limit": 1}))
        self.assertEqual([i["id"] for i in seen["items"]], [m2])
        self.assertEqual(seen["omitted"], 1)
        done = json.loads(self.provider.handle_tool_call(
            "memcal_update", {"which": handle, "when": "sunday",
                              "source_ids": [m2]}))
        self.assertIn("Sep 13", done["row"])
        conn = self.conn()
        try:
            import sys as _sys
            activity_mod = _sys.modules["memcal.activity"]
            self.assertEqual(activity_mod.reviewed_ids(conn, "event", event.key),
                             {m1, m2})
            self.assertNotIn(turn, activity_mod.reviewed_ids(
                conn, "event", event.key))
            cited = [r["archive_id"] for r in conn.execute(
                "SELECT archive_id FROM evidence WHERE kind='event' AND ref=?",
                (event.key,))]
            # Authorship is still linked; it just never counts as reviewed.
            self.assertIn(m2, cited)
            self.assertIn(turn, cited)
            rest = json.loads(self.provider.handle_tool_call(
                "memcal_activity", {"handle": handle}))
            self.assertEqual([i["id"] for i in rest["items"]], [m3])
        finally:
            conn.close()

    def test_empty_review_fails_with_a_turn_available(self):
        conn = self.conn()
        try:
            _event, handle, _m1, _m2, _m3 = self._question_turn(conn)
        finally:
            conn.close()
        self.provider.on_turn_start(1, "When is poker?")
        out = json.loads(self.provider.handle_tool_call(
            "memcal_reviewed", {"handle": handle, "source_ids": []}))
        self.assertIn("error", out)
        self.assertIn("cite", out["error"])
        conn = self.conn()
        try:
            rest = json.loads(self.provider.handle_tool_call(
                "memcal_activity", {"handle": handle}))
            self.assertEqual(len(rest["items"]), 2)
        finally:
            conn.close()

    def _turn_id(self):
        conn = self.conn()
        try:
            return conn.execute(
                "SELECT id FROM archive WHERE stream = 'agent'"
                " ORDER BY id DESC LIMIT 1").fetchone()["id"]
        finally:
            conn.close()


class TestAuthorshipBoundaries(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from memcal.config import Config
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        from memcal import db
        self.conn = db.open_db(self.cfg.db_path)
        self.addCleanup(self.conn.close)
        db.set_today("2026-09-12")
        self.addCleanup(db.set_today, None)

    def test_harness_archives_user_turns_only(self):
        from memcal import archive, db, harness
        row_id = harness.archive_user_turn(
            self.cfg, "is poker still saturday?", harness="openclaw",
            session_id="s1", message_id="t1")
        self.assertIsNotNone(row_id)
        row = self.conn.execute(
            "SELECT * FROM archive WHERE id = ?", (row_id,)).fetchone()
        self.assertEqual(row["person"], "me")
        self.assertEqual(row["addressed_to"], "machine")
        # The adapter never hands assistant prose to this boundary: there is no
        # parameter for it, and identical redelivery deduplicates on the id.
        again = harness.archive_user_turn(
            self.cfg, "is poker still saturday?", harness="openclaw",
            session_id="s1", message_id="t1")
        self.assertIsNone(again)
        ctx = harness.context(self.cfg, "poker?")
        self.assertIn("MEMCAL SNAPSHOT", ctx)

    def test_a_failed_write_records_no_effect(self):
        from memcal import live
        with self.assertRaises(live.LiveError):
            live.update_event(self.conn, self.cfg, "no such row E999",
                              status="declined",
                              origin=live.Origin.of("test", cited=[1]))
        self.assertEqual(self.conn.execute(
            "SELECT count(*) n FROM provenance").fetchone()["n"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) n FROM actions").fetchone()["n"], 0)

    def test_saturday_reply_cannot_override_sunday_evidence(self):
        from memcal import archive, db, live
        from memcal.sources import base
        from memcal.sources.base import IngestReport
        # Saturday's plan, established from Saturday's message.
        sat = IngestReport(stream="chat")
        base.deliver(self.conn, sat, stream="chat", external_id="sat1",
                     ts="2026-09-05T19:00:00", text="poker saturday 8pm?",
                     thread="poker group", handle="friend@example.com")
        self.conn.commit()
        sat_id = self.conn.execute(
            "SELECT id FROM archive WHERE external_id='sat1'").fetchone()["id"]
        event, _ = live.add_event(self.conn, self.cfg, title="Poker night",
                                  when="2026-09-12",
                                  origin=live.Origin.of("test"))
        live.update_event(self.conn, self.cfg, event.key, status="confirmed",
                          origin=live.Origin.of("test", cited=[sat_id]))
        # Sunday's correction lands in the same thread the next morning.
        sun = IngestReport(stream="chat")
        base.deliver(self.conn, sun, stream="chat", external_id="sun1",
                     ts="2026-09-12T11:00:00", text="moved to Sunday?",
                     thread="poker group", handle="friend@example.com")
        self.conn.commit()
        sun_id = self.conn.execute(
            "SELECT id FROM archive WHERE external_id='sun1'").fetchone()["id"]
        live.update_event(self.conn, self.cfg, event.key, when="2026-09-13",
                          origin=live.Origin.of("test", cited=[sun_id]))
        # The assistant's stale "Saturday" reply was never collected: it is not
        # in the archive, cites nothing, and overrides nothing.
        self.assertEqual(self.conn.execute(
            "SELECT count(*) n FROM archive WHERE text LIKE '%assistant%'"
            ).fetchone()["n"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT date FROM events WHERE key = ?", (event.key,)).fetchone()
            ["date"], "2026-09-13")
        # Re-delivery of the Sunday line changes neither the date nor the evidence.
        before = self.conn.execute(
            "SELECT count(*) n FROM evidence WHERE kind='event' AND ref=?",
            (event.key,)).fetchone()["n"]
        dup = IngestReport(stream="chat")
        base.deliver(self.conn, dup, stream="chat", external_id="sun1",
                     ts="2026-09-12T11:00:00", text="moved to Sunday?",
                     thread="poker group", handle="friend@example.com")
        self.conn.commit()
        self.assertEqual(dup.archived, 0)
        self.assertEqual(self.conn.execute(
            "SELECT date FROM events WHERE key = ?", (event.key,)).fetchone()
            ["date"], "2026-09-13")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) n FROM evidence WHERE kind='event' AND ref=?",
            (event.key,)).fetchone()["n"], before)
        # A genuine later user correction still applies.
        live.update_event(self.conn, self.cfg, event.key, when="2026-09-14",
                          origin=live.Origin.of("test"))
        self.assertEqual(self.conn.execute(
            "SELECT date FROM events WHERE key = ?", (event.key,)).fetchone()
            ["date"], "2026-09-14")

    def test_questions_and_quotations_assert_no_fact(self):
        from memcal import archive, db, harness
        harness.archive_user_turn(self.cfg, "Is poker still Saturday?",
                                  harness="openclaw", session_id="s1",
                                  message_id="q1")
        harness.archive_user_turn(self.cfg, "You told me Saturday",
                                  harness="openclaw", session_id="s1",
                                  message_id="q2")
        rows = self.conn.execute(
            "SELECT text, addressed_to FROM archive WHERE stream='agent'"
            " ORDER BY id").fetchall()
        self.assertEqual([r["text"] for r in rows],
                         ["Is poker still Saturday?", "You told me Saturday"])
        self.assertTrue(all(r["addressed_to"] == "machine" for r in rows))
        self.assertEqual(self.conn.execute(
            "SELECT count(*) n FROM events").fetchone()["n"], 0)

    def test_mcp_citations_land_on_original_lines(self):
        from memcal import db, live, mcp_server
        from memcal.sources import base
        from memcal.sources.base import IngestReport
        rep = IngestReport(stream="chat")
        base.deliver(self.conn, rep, stream="chat", external_id="m1",
                     ts=db.now(), text="poker saturday?", thread="poker group",
                     handle="friend@example.com")
        self.conn.commit()
        m1 = self.conn.execute(
            "SELECT id FROM archive WHERE external_id='m1'").fetchone()["id"]
        event, _ = live.add_event(self.conn, self.cfg, title="Poker night",
                                  when="2026-09-12",
                                  origin=live.Origin.of("test"))
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        m2 = IngestReport(stream="chat")
        base.deliver(self.conn, m2, stream="chat", external_id="m2",
                     ts=db.now(), text="moved to Sunday?", thread="poker group",
                     handle="friend@example.com")
        self.conn.commit()
        m2id = self.conn.execute(
            "SELECT id FROM archive WHERE external_id='m2'").fetchone()["id"]
        server = mcp_server.Server.__new__(mcp_server.Server)
        server.conn, server.cfg = self.conn, self.cfg
        server.harness, server.session = "openclaw", ""
        row = self.conn.execute("SELECT id FROM events WHERE key = ?",
                                (event.key,)).fetchone()
        out = server.call("memcal_update",
                          {"which": f"E{row['id']}", "when": "sunday",
                           "source_ids": [m2id, 424242]})
        self.assertIn("2026-09-13", out)
        cited = [r["archive_id"] for r in self.conn.execute(
            "SELECT archive_id FROM evidence WHERE kind='event' AND ref=?",
            (event.key,))]
        self.assertIn(m2id, cited)
        self.assertNotIn(424242, cited)

    def test_mcp_session_turn_acknowledges_nothing(self):
        from memcal import activity, db, harness, live, mcp_server
        from memcal.sources import base
        from memcal.sources.base import IngestReport
        rep = IngestReport(stream="chat")
        base.deliver(self.conn, rep, stream="chat", external_id="m1",
                     ts=db.now(), text="poker saturday?", thread="poker group",
                     handle="friend@example.com")
        self.conn.commit()
        m1 = self.conn.execute(
            "SELECT id FROM archive WHERE external_id='m1'").fetchone()["id"]
        event, _ = live.add_event(self.conn, self.cfg, title="Poker night",
                                  when="2026-09-12",
                                  origin=live.Origin.of("test"))
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        for eid, text in (("m2", "moved to Sunday?"), ("m3", "at 5 Oak?")):
            page = IngestReport(stream="chat")
            base.deliver(self.conn, page, stream="chat", external_id=eid,
                         ts=db.now(), text=text, thread="poker group",
                         handle="friend@example.com")
        self.conn.commit()
        m2 = self.conn.execute(
            "SELECT id FROM archive WHERE external_id='m2'").fetchone()["id"]
        m3 = self.conn.execute(
            "SELECT id FROM archive WHERE external_id='m3'").fetchone()["id"]
        harness.archive_user_turn(self.cfg, "When is poker?", harness="openclaw",
                                  session_id="s1", message_id="t1")
        server = mcp_server.Server.__new__(mcp_server.Server)
        server.conn, server.cfg = self.conn, self.cfg
        server.harness, server.session = "openclaw", "s1"
        row = self.conn.execute("SELECT id FROM events WHERE key = ?",
                                (event.key,)).fetchone()
        handle = f"E{row['id']}"
        refused = server.call("memcal_reviewed",
                              {"handle": handle, "source_ids": []})
        self.assertIn("cite", refused)
        self.assertEqual([i["id"] for i in activity.pending(
            self.conn, "event", event.key)["strong"]], [m2, m3])
        out = server.call("memcal_update",
                          {"which": handle, "when": "sunday",
                           "source_ids": [m2]})
        self.assertIn("2026-09-13", out)
        self.assertEqual(activity.reviewed_ids(self.conn, "event", event.key),
                         {m1, m2})


if __name__ == "__main__":
    unittest.main()
