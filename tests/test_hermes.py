"""The Hermes memory provider — §8's plugin, against the real ABC.

Skipped when Hermes isn't installed, so the suite stays green anywhere.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

HERMES = Path.home() / ".hermes" / "hermes-agent"
PLUGIN = Path(__file__).resolve().parent.parent / "integrations" / "hermes"

# `_load_memcal` intentionally evicts this package from the module cache.  Keep that
# integration behaviour inside this test module: other test modules may have imported
# `db`, `series`, and `ical` before Hermes reloads them, and mixing those module objects
# gives one process two clocks.
_MEMCAL_BEFORE_HERMES = {
    name: module for name, module in sys.modules.items()
    if name == "memcal" or name.startswith("memcal.")
}
_SYS_PATH_BEFORE_HERMES = list(sys.path)
_ENVIRONMENT_BEFORE_HERMES = {name: os.environ.get(name)
                              for name in ("MEMCAL_HOME", "MEMCAL_SRC")}


def _provider():
    """Load the plugin by path.

    The plugin package and the app package are both called `memcal`, so a plain
    import resolves to the wrong one. Hermes avoids this in production by loading
    user plugins under a synthetic namespace; do the same here.
    """
    import importlib.util
    sys.path.insert(0, str(HERMES))
    spec = importlib.util.spec_from_file_location(
        "_memcal_hermes_plugin", PLUGIN / "memcal" / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_memcal_hermes_plugin"] = module
    spec.loader.exec_module(module)
    return module.MemcalMemoryProvider


@unittest.skipUnless(HERMES.is_dir(), "Hermes not installed")
class TestHermesProvider(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        # The provider deliberately drops `memcal.*` from sys.modules to reload a
        # changed checkout. Preserve the runner's imports around that integration
        # exercise: downstream modules otherwise retain an old `db` while a dynamic
        # import inside `ical` receives the replacement `series`, splitting the
        # process-global test clock in two.
        cls._memcal_modules = {
            name: module for name, module in sys.modules.items()
            if name == "memcal" or name.startswith("memcal.")
        }
        cls._sys_path = list(sys.path)
        cls._environment = {name: os.environ.get(name)
                            for name in ("MEMCAL_HOME", "MEMCAL_SRC")}
        os.environ["MEMCAL_HOME"] = cls.tmp.name
        os.environ["MEMCAL_SRC"] = str(Path(__file__).resolve().parent.parent)
        from memcal import config, db          # the app
        cfg = config.load(cls.tmp.name)
        cfg.ensure_dirs()
        db.open_db(cfg.db_path).close()
        cls.cls = _provider()

    @classmethod
    def tearDownClass(cls):
        try:
            for name in [name for name in sys.modules
                         if name == "memcal" or name.startswith("memcal.")]:
                sys.modules.pop(name, None)
            sys.modules.update(cls._memcal_modules)
            sys.path[:] = cls._sys_path
            for name, value in cls._environment.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        finally:
            cls.tmp.cleanup()

    def _ready(self, context: str = "primary"):
        provider = self.cls()
        provider.initialize("test", hermes_home="x", platform="cli", agent_context=context)
        return provider

    def test_it_satisfies_the_abc(self):
        sys.path.insert(0, str(HERMES))
        from agent.memory_provider import MemoryProvider
        self.assertTrue(issubclass(self.cls, MemoryProvider))
        self.assertEqual(self.cls().name, "memcal")

    def test_the_spec_tools_are_exposed(self):
        names = {t["name"] for t in self._ready().get_tool_schemas()}
        # §8 names the read tools explicitly.
        self.assertTrue({"memcal_open_page", "memcal_list_month",
                         "memcal_search_archive", "memcal_open_source"} <= names)
        # §8's fourth is `remember`, taking a sentence. What that shipped as sent the
        # sentence to a model to be turned back into fields the caller already had, so
        # it is these instead: one verb each, no extraction step.
        self.assertTrue({"memcal_add", "memcal_update", "memcal_merge", "memcal_drop",
                         "memcal_todo", "memcal_note", "memcal_answer"} <= names)
        self.assertNotIn("memcal_remember", names)

    def test_anything_memcal_add_can_set_memcal_update_can_correct(self):
        """`until` was on `memcal_add` and not on `memcal_update`, so the end of a trip
        could be set once and never moved. Told "Montana is 15 to 23 btw", the model
        did the only thing the schema left it — wrote the range into `note` — and the
        row went on ending seven days early, on the brief and in Calendar.app.

        The asymmetry is invisible from inside either schema, so compare them. `when`
        is `date` under the name the update tool uses for it; the rest must match.
        """
        schemas = {t["name"]: set(t["parameters"]["properties"])
                   for t in self._ready().get_tool_schemas()
                   if t["name"] in ("memcal_add", "memcal_update")}
        settable = schemas["memcal_add"] - {"title", "when", "participants"}
        correctable = schemas["memcal_update"] | {"which", "when"}
        self.assertEqual(settable - correctable, set(),
                         "memcal_add can set a field memcal_update cannot correct")

    def test_a_span_is_corrected_in_the_columns_not_in_a_note(self):
        provider = self._ready()
        provider.handle_tool_call("memcal_add", {
            "title": "Montana trip", "when": "2026-08-15", "until": "2026-08-16",
            "status": "confirmed"})
        out = json.loads(provider.handle_tool_call(
            "memcal_update", {"which": "Montana", "until": "2026-08-23"}))
        self.assertIn("until Sun Aug 23", out["row"])
        self.assertIn("until: 2026-08-16 → 2026-08-23", out["changed"])

    def test_a_read_handle_targets_that_exact_duplicate_title_row(self):
        """The provider must let a write consume the exact selector its read returned."""
        provider = self._ready()
        from memcal import config, db, events

        cfg = config.load(self.tmp.name)
        conn = db.open_db(cfg.db_path)
        first, _ = events.upsert(conn, {
            "title": "Studio appointment", "date": "2032-03-03",
            "status": "mentioned"}, written_by="live", match=False)
        second, _ = events.upsert(conn, {
            "title": "Studio appointment", "date": "2032-03-07",
            "status": "mentioned"}, written_by="live", match=False)
        conn.commit()
        conn.close()

        listed = json.loads(provider.handle_tool_call(
            "memcal_list_days", {"when": second.date}))
        matches = [row for row in listed["rows"] if "Studio appointment" in row["row"]]
        self.assertTrue(matches, listed)
        handle = matches[0]["source"]
        out = json.loads(provider.handle_tool_call(
            "memcal_update", {"which": handle, "status": "declined"}))
        self.assertIn("not going", out["row"])

        conn = db.open_db(cfg.db_path)
        self.assertEqual(events.get(conn, first.key).status, "mentioned")
        self.assertEqual(events.get(conn, second.key).status, "declined")
        conn.close()

    def test_no_write_tool_can_reach_a_model(self):
        """The whole point. A live write that needs the network is a live write that
        can time out, truncate, or quietly do something else — all three happened."""
        provider = self._ready()
        writes = {"memcal_add", "memcal_update", "memcal_merge", "memcal_drop",
                  "memcal_todo", "memcal_note", "memcal_answer", "memcal_alias"}
        self.assertTrue(writes <= {t["name"] for t in provider.get_tool_schemas()})
        from memcal import llm

        def explode(*_a, **_k):
            raise AssertionError("a live write reached for a model")

        original = llm.OpenRouter.complete
        llm.OpenRouter.complete = explode
        try:
            out = json.loads(provider.handle_tool_call(
                "memcal_add", {"title": "Poker model-free probe", "when": "saturday",
                               "participants": ["Jordan Lee"], "status": "confirmed"}))
            self.assertTrue(any("Poker model-free probe" in str(value)
                                for value in out.values()))
            out = json.loads(provider.handle_tool_call(
                "memcal_update", {"which": "model-free probe", "status": "declined"}))
            self.assertIn("not going", out["row"])
        finally:
            llm.OpenRouter.complete = original

    def test_answering_closes_a_question(self):
        provider = self._ready()
        from memcal import config, db, todos
        cfg = config.load(self.tmp.name)
        conn = db.open_db(cfg.db_path)
        todos.ask(conn, "Did Tuesday dinner happen? who with?")
        conn.close()
        import json as _json
        result = _json.loads(provider.handle_tool_call(
            "memcal_answer", {"question": "Tuesday dinner", "answer": "yes, with Alex"}))
        self.assertTrue(result.get("recorded"))
        conn = db.open_db(cfg.db_path)
        still_open = [q["text"] for q in todos.open_questions(conn)]
        conn.close()
        self.assertFalse(any("Tuesday dinner" in q for q in still_open))

    def test_a_question_about_one_day_has_a_tool_that_answers_it(self):
        """"am I doing anything this sat" reached for the only date tool there was —
        a whole month — and reported a stacked Saturday from rows that were mostly
        other days. A day question needs a day answer."""
        from datetime import timedelta
        from memcal import db
        provider = self._ready()
        # A Saturday far enough out that nothing else in this class can have written to
        # it. The store is one `setUpClass` shared by every test here, and the day was
        # written down as `2026-09-19` — which is "this Saturday" when the suite runs on
        # 2026-09-13, so a sibling test's row landed on it and "free" stopped being true.
        far = db.today() + timedelta(days=120)
        saturday = far + timedelta(days=(5 - far.weekday()) % 7)
        out = json.loads(provider.handle_tool_call(
            "memcal_list_days", {"when": saturday.isoformat()}))
        self.assertEqual(out["weekday"], "Saturday")
        self.assertEqual(out["from"], out["to"])          # one day, not a month
        self.assertTrue(out["free"])                       # empty calendar in this fixture
        weekend = json.loads(
            provider.handle_tool_call("memcal_list_days", {"when": "this weekend"}))
        self.assertEqual(weekend["weekday"], "Saturday")
        self.assertNotEqual(weekend["from"], weekend["to"])   # Saturday and Sunday

    def test_tools_always_return_json(self):
        provider = self._ready()
        for tool, args in (("memcal_list_month", {}),
                           ("memcal_list_days", {"when": "saturday"}),
                           ("memcal_list_days", {}),
                           ("memcal_search_archive", {"query": "poker"}),
                           ("memcal_open_page", {"slug": "nobody"}),
                           ("memcal_answer", {"question": "nothing", "answer": "x"}),
                           ("memcal_bogus", {})):
            json.loads(provider.handle_tool_call(tool, args))   # must not raise

    def test_the_brief_is_refreshed_on_every_turn(self):
        provider = self._ready()
        from memcal import brief, config, db, todos
        cfg = config.load(self.tmp.name)
        conn = db.open_db(cfg.db_path)
        first_todo, _ = todos.open_todo(conn, "First typed snapshot")
        brief.write(conn, cfg)
        conn.close()
        block = provider.system_prompt_block()
        first = provider.prefetch("what is coming up?")
        self.assertNotIn("First typed snapshot", block,
                         "changing memory would make the cached system prompt stale")
        self.assertIn("First typed snapshot", first)

        conn = db.open_db(cfg.db_path)
        todos.close(conn, first_todo.key)
        todos.open_todo(conn, "Second typed snapshot")
        conn.close()
        second = provider.prefetch("and now?")
        self.assertIn("Second typed snapshot", second)
        self.assertNotIn("First typed snapshot", second)
        self.assertNotEqual(first, second)

    def test_prompt_routes_real_calendar_writes_to_ical(self):
        block = self._ready().system_prompt_block()
        self.assertIn("imported into this snapshot directly", block)
        self.assertIn("Hermes's built-in iCal capability", block)
        self.assertIn("do not change Calendar.app", block)

    def test_only_the_clean_user_turn_crosses_the_ingest_boundary(self):
        provider = self._ready()
        injected = "MEMCAL SNAPSHOT\n2026-08-01 Poker at Robbie's house"
        provider.on_turn_start(91, "what am I doing Saturday?")
        provider.sync_turn("what am I doing Saturday?",
                           f"{injected}\nYou have poker at Robbie's.")
        from memcal import config, db
        conn = db.open_db(config.load(self.tmp.name).db_path)
        rows = conn.execute(
            "SELECT text, meta FROM archive WHERE external_id LIKE '%turn:91'"
        ).fetchall()
        conn.close()
        self.assertEqual([r["text"] for r in rows], ["what am I doing Saturday?"])
        self.assertTrue(all("hermes-user" in (r["meta"] or "") for r in rows))
        self.assertFalse(any("Robbie" in r["text"] for r in rows),
                         "assistant output and injected memory must never loop back")

    def test_a_live_write_links_back_to_the_current_user_turn(self):
        provider = self._ready()
        provider.on_turn_start(92, "Poker at Robbie's Saturday; yes, I'm going.")
        json.loads(provider.handle_tool_call(
            "memcal_add", {"title": "Poker at Robbie's", "when": "saturday",
                           "status": "confirmed", "participants": ["Robbie"]}))
        from memcal import config, db
        conn = db.open_db(config.load(self.tmp.name).db_path)
        event_id = conn.execute(
            "SELECT id FROM events WHERE title = ? ORDER BY id DESC",
            ("Poker at Robbie's",)).fetchone()["id"]
        conn.close()
        opened = json.loads(provider.handle_tool_call(
            "memcal_open_source", {"source": f"E{event_id}"}))
        self.assertEqual(
            [row["text"] for row in opened["evidence"] if row["evidence"]],
            ["Poker at Robbie's Saturday; yes, I'm going."])

    def test_mentioning_a_nickname_prefetches_its_material_page(self):
        provider = self._ready()
        from memcal import config, db, wiki
        cfg = config.load(self.tmp.name)
        conn = db.open_db(cfg.db_path)
        wiki.set_slot(cfg.wiki_dir, "quinn-brooks", "favorite movie theater",
                      "Alamo Drafthouse", source="groupme", conn=conn)
        wiki.add_alias(cfg.wiki_dir, "quinn-brooks", "Q")
        conn.close()
        snapshot = provider.prefetch("what theater does Quinn like?")
        self.assertIn("Alamo Drafthouse", snapshot)
        self.assertIn("WIKI PAGES MENTIONED THIS TURN", snapshot)

    def test_the_gate_applies_to_agent_turns_too(self):
        provider = self._ready()
        provider.sync_turn("dinner with harper thursday at 7", "ok")
        provider.sync_turn("hey", "hi")
        time.sleep(1.5)
        from memcal import config, db
        conn = db.open_db(config.load(self.tmp.name).db_path)
        archived = conn.execute("SELECT count(*) AS n FROM archive WHERE stream='agent'").fetchone()["n"]
        spooled = conn.execute("SELECT count(*) AS n FROM spool").fetchone()["n"]
        conn.close()
        self.assertGreaterEqual(archived, 2, "everything is archived")
        self.assertLess(spooled, archived, "only what the gate passes is spooled")

    def test_turns_in_the_same_second_are_not_deduplicated(self):
        provider = self._ready()
        for text in ("first thing tomorrow", "second thing tomorrow"):
            provider.sync_turn(text, "ok")
        time.sleep(1.5)
        from memcal import config, db
        conn = db.open_db(config.load(self.tmp.name).db_path)
        rows = conn.execute(
            "SELECT count(*) AS n FROM archive WHERE text LIKE '%thing tomorrow%'").fetchone()["n"]
        conn.close()
        self.assertEqual(rows, 2)

    def test_identical_in_flight_turns_do_not_confuse_sync_deduplication(self):
        provider = self._ready()
        provider.on_turn_start(101, "same plan tomorrow")
        provider.on_turn_start(102, "same plan tomorrow")
        provider.sync_turn("same plan tomorrow", "ok")
        provider.sync_turn("same plan tomorrow", "ok again")
        from memcal import config, db
        conn = db.open_db(config.load(self.tmp.name).db_path)
        rows = conn.execute(
            "SELECT count(*) n FROM archive WHERE external_id LIKE '%turn:10_'"
        ).fetchone()["n"]
        conn.close()
        self.assertEqual(rows, 2)

    def test_subagents_never_write(self):
        from memcal import config, db
        conn = db.open_db(config.load(self.tmp.name).db_path)
        before = conn.execute("SELECT count(*) AS n FROM archive").fetchone()["n"]
        conn.close()
        self._ready(context="subagent").sync_turn("a cron prompt", "ok")
        time.sleep(1.0)
        conn = db.open_db(config.load(self.tmp.name).db_path)
        after = conn.execute("SELECT count(*) AS n FROM archive").fetchone()["n"]
        conn.close()
        self.assertEqual(before, after, "a subagent must not corrupt the user's memory")


@unittest.skipUnless(HERMES.is_dir(), "Hermes not installed")
class TestSourceReload(unittest.TestCase):
    """A long-running Hermes must not serve a six-hour-old memcal.

    Real session: memcal was edited at 23:55, Hermes had been running since 17:53, and
    the conversation at 00:21 hit bugs that were already fixed on disk. Python serves
    every later `import memcal` from sys.modules, so a fix reaches the user only on the
    next restart — and from the outside that is indistinguishable from a broken tool.
    """

    def setUp(self):
        os.environ["MEMCAL_SRC"] = str(Path(__file__).resolve().parent.parent)
        _provider()                       # loads the plugin under its own namespace
        self.module = sys.modules["_memcal_hermes_plugin"]

    def test_an_edit_drops_the_cached_modules(self):
        self.module._load_memcal()
        first = self.module._loaded_mtime
        self.assertGreater(first, 0, "should have stamped the source it loaded")
        sys.modules.setdefault("memcal.todos", object())

        # Pretend the working tree moved on.
        self.module._loaded_mtime = first - 10
        self.module._load_memcal()
        self.assertGreaterEqual(self.module._loaded_mtime, first)

    def test_an_unchanged_tree_does_not_reload(self):
        self.module._load_memcal()
        stamp = self.module._loaded_mtime
        marker = object()
        sys.modules["memcal.__reload_marker__"] = marker
        self.module._load_memcal()
        self.assertEqual(self.module._loaded_mtime, stamp)
        self.assertIs(sys.modules.get("memcal.__reload_marker__"), marker,
                      "an unchanged tree must not pay the cost of a purge")
        sys.modules.pop("memcal.__reload_marker__", None)

    def test_source_mtime_survives_a_missing_checkout(self):
        self.assertEqual(self.module._source_mtime("/nope/not/here"), 0.0)


@unittest.skipUnless(HERMES.is_dir(), "Hermes not installed")
class TestAToolShippedOnOneSurfaceOnly(unittest.TestCase):

    def setUp(self):
        _provider()
        self.module = sys.modules["_memcal_hermes_plugin"]

    def _hermes_names(self) -> set[str]:
        provider = self.module.MemcalMemoryProvider
        for attr in ("tools", "get_tools", "_tools"):
            found = getattr(provider, attr, None)
            if callable(found):
                try:
                    return {t["name"] for t in found(provider.__new__(provider))}
                except Exception:
                    continue
        source = (PLUGIN / "memcal" / "__init__.py").read_text(encoding="utf-8")
        listed = source.split("return [OPEN,", 1)[1].split("]", 1)[0]
        constants = {name.strip().rstrip(",") for name in ("OPEN," + listed).split(",")}
        return {getattr(self.module, name)["name"]
                for name in constants if name and hasattr(self.module, name)}

    def test_both_surfaces_can_open_a_row(self):
        from memcal import mcp_server
        mcp = {tool["name"] for tool in mcp_server.TOOLS}
        self.assertIn("memcal_open", mcp)
        self.assertIn("memcal_open", self._hermes_names())

    def test_neither_surface_has_a_read_tool_the_other_lacks(self):
        from memcal import mcp_server
        # The one known-tolerated difference, named rather than silently allowed.
        aliases = {"memcal_open_source": "memcal_source"}
        hermes = {aliases.get(name, name) for name in self._hermes_names()}
        mcp = {tool["name"] for tool in mcp_server.TOOLS}
        # MCP carries writes Hermes routes differently; compare the read half only.
        reads = {"memcal_open", "memcal_open_page", "memcal_source",
                 "memcal_conversation", "memcal_search_archive"}
        self.assertEqual(reads & mcp, reads & hermes,
                         "a read tool exists on one surface and not the other")


def tearDownModule():
    for name in [name for name in sys.modules
                 if name == "memcal" or name.startswith("memcal.")]:
        sys.modules.pop(name, None)
    sys.modules.update(_MEMCAL_BEFORE_HERMES)
    sys.path[:] = _SYS_PATH_BEFORE_HERMES
    for name, value in _ENVIRONMENT_BEFORE_HERMES.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


if __name__ == "__main__":
    unittest.main()
