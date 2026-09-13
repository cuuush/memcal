"""Scratch-only acceptance checks using the existing Hermes runtime."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
MEMCAL = ROOT
HERMES = Path(os.environ.get("HERMES_TEST_SRC", str(Path.home() / ".hermes/hermes-agent")))
sys.path[:0] = [str(MEMCAL), str(HERMES)]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


from agent.memory_manager import MemoryManager


class TestUndeliveredMemcalContext(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, MEMCAL_HOME=self.tmp.name, MEMCAL_SRC=str(MEMCAL))
        env.start()
        self.addCleanup(env.stop)
        plugin = load("_delivery_memcal_plugin", ROOT / "integrations/hermes/memcal/__init__.py")
        self.provider = plugin.MemcalMemoryProvider()
        self.provider.initialize("first", agent_context="primary")
        from memcal import db, llm
        db.set_today("2026-09-12T12:00:00-04:00")
        self.addCleanup(db.set_today, None)
        self.conn = db.open_db(self.provider._cfg.db_path)
        self.addCleanup(self.conn.close)
        guard = patch.object(llm, "client_for", side_effect=AssertionError("No model calls"))
        self.model_guard = guard.start()
        self.addCleanup(guard.stop)
        self.addCleanup(self.model_guard.assert_not_called)
        self.manager = MemoryManager(external_prefetch_timeout=2)
        self.manager._providers = [self.provider]
        self.addCleanup(self.manager.shutdown_all)

    def ask(self, session="first"):
        return self.manager.prefetch_all("Where is poker?", session_id=session)

    def change_brief(self):
        from memcal import todos
        todos.open_todo(self.conn, "Check the new poker address")
        self.conn.commit()

    def run_timeout(self, *, overlap=False, session="first", before_release=None):
        from memcal import brief
        original = brief.render
        release = threading.Event()
        entered = threading.Event()

        def slow(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test failed to release render")
            return original(*args, **kwargs)

        self.manager._external_prefetch_timeout = .02
        with patch.object(brief, "render", side_effect=slow) as render:
            try:
                warning = self.ask(session)
                self.assertTrue(entered.is_set())
                repeated = self.ask("second") if overlap else self.ask(session)
                calls = render.call_count
                if before_release:
                    before_release()
            finally:
                release.set()
                worker = self.manager._external_prefetch_threads.get("memcal")
                if worker:
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
        self.manager._external_prefetch_timeout = 2
        return warning, repeated, calls

    def complete(self, context, session="first", messages=None):
        from agent.turn_context import compose_user_api_content
        user = "Where is poker?"
        if messages is None:
            messages = [{"role": "user", "content": user,
                         "api_content": compose_user_api_content(user, context, "")},
                        {"role": "assistant", "content": "Synthetic answer"}]
        self.manager.sync_all(user, "Synthetic answer", session_id=session, messages=messages)
        self.assertTrue(self.manager.flush_pending(timeout=3))

    def test_rendering_alone_does_not_acknowledge_delivery(self):
        self.assertIn("MEMCAL SNAPSHOT", self.ask())
        self.assertIn("MEMCAL SNAPSHOT", self.ask())

    def test_completed_api_context_allows_compact_current_confirmation(self):
        first = self.ask()
        self.complete(first)
        second = self.ask()
        self.assertIn("MEMCAL CURRENT", second)
        self.assertNotIn("MEMCAL SNAPSHOT", second)
        self.assertEqual(first.split("[id=", 1)[1].split("]", 1)[0],
                         second.split("[id=", 1)[1].split("]", 1)[0])

    def test_timeout_cannot_make_late_snapshot_disappear_on_retry(self):
        self.complete(self.ask())
        self.change_brief()
        warning, busy, calls = self.run_timeout()
        # Unmodified Hermes drops timed-out results; absence is an explicit
        # missing-confirmation state in the Memcal protocol.
        self.assertEqual(warning, "")
        self.assertEqual(busy, "")
        self.assertEqual(calls, 1)
        recovery = self.ask()
        self.assertIn("Check the new poker address", recovery)
        self.complete(recovery)
        self.assertIn("MEMCAL CURRENT", self.ask())

    def test_absent_confirmation_never_means_current_in_provider_instructions(self):
        block = self.provider.system_prompt_block()
        self.assertIn("MEMCAL CURRENT", block)
        self.assertIn("call memcal_refresh", block)
        self.assertNotIn("always current", block)
        self.assertNotIn("turn without one is normal", block)

    def test_busy_other_session_does_not_lose_either_snapshot(self):
        self.complete(self.ask())
        self.complete(self.ask("second"), "second")
        self.change_brief()
        self.run_timeout(overlap=True)
        self.assertIn("MEMCAL SNAPSHOT", self.ask("second"))
        self.assertIn("MEMCAL SNAPSHOT", self.ask())

    def test_unscoped_session_rotation_cannot_acknowledge_late_result(self):
        self.complete(self.ask(""))
        self.change_brief()
        self.run_timeout(session="", before_release=lambda:
                         self.provider.on_session_switch("second", reset=True))
        self.assertIn("MEMCAL SNAPSHOT", self.ask("first"))
        self.assertIn("MEMCAL SNAPSHOT", self.ask("second"))

    def test_handled_render_failure_invalidates_previous_confirmation(self):
        from memcal import brief
        self.complete(self.ask())
        with patch.object(brief, "render", side_effect=RuntimeError("synthetic")):
            self.assertIn("MEMCAL UNAVAILABLE", self.ask())
        self.assertIn("MEMCAL SNAPSHOT", self.ask())

    def test_provider_exception_preserves_retry_of_changed_context(self):
        self.complete(self.ask())
        self.change_brief()
        with patch.object(self.provider, "prefetch", side_effect=RuntimeError("synthetic")):
            self.assertEqual(self.ask(), "")
        self.assertIn("MEMCAL SNAPSHOT", self.ask())

    def test_user_or_assistant_echo_is_neither_delivery_nor_new_evidence(self):
        first = self.ask()
        messages = [{"role": "user", "content": first, "api_content": first},
                    {"role": "assistant", "content": first}]
        self.complete(first, messages=messages)
        self.assertIn("MEMCAL SNAPSHOT", self.ask())
        rows = self.conn.execute("SELECT text FROM archive").fetchall()
        self.assertTrue(rows)
        self.assertTrue(all(row["text"] == "Where is poker?" for row in rows))

    def test_partial_snapshot_does_not_acknowledge_delivery(self):
        self.complete(self.ask().splitlines()[0])
        self.assertIn("MEMCAL SNAPSHOT", self.ask())

    def test_only_the_latest_user_message_can_acknowledge_pending_context(self):
        from agent.turn_context import compose_user_api_content
        first = self.ask()
        messages = [{"role": "user", "content": "previous",
                     "api_content": compose_user_api_content("previous", first, "")},
                    {"role": "user", "content": "Where is poker?"}]
        self.complete(first, messages=messages)
        self.assertIn("MEMCAL SNAPSHOT", self.ask())

    def test_compression_invalidates_pending_and_acknowledged_context(self):
        first = self.ask()
        self.provider.on_session_switch("first", reason="compression")
        self.complete(first)
        second = self.ask()
        self.assertIn("MEMCAL SNAPSHOT", second)
        self.complete(second)
        self.assertIn("MEMCAL CURRENT", self.ask())
        self.provider.on_session_switch("first", reason="compression")
        self.assertIn("MEMCAL SNAPSHOT", self.ask())

    def test_old_completion_cannot_acknowledge_a_post_compression_render(self):
        first = self.ask()
        self.provider.on_session_switch("first", reason="compression")
        self.ask()
        self.complete(first)
        self.assertIn("MEMCAL SNAPSHOT", self.ask())

    def test_acknowledgement_is_scoped_to_one_session(self):
        self.complete(self.ask())
        self.assertIn("MEMCAL SNAPSHOT", self.ask("second"))
        self.assertIn("MEMCAL CURRENT", self.ask())

    def test_legacy_callback_without_api_context_repeats_safely(self):
        first = self.ask()
        self.provider.sync_turn("Where is poker?", "Synthetic answer")
        self.assertIn("MEMCAL SNAPSHOT", self.ask())

    def test_warning_enters_api_context_without_changing_stored_user_text(self):
        from agent.turn_context import compose_user_api_content
        from memcal import brief
        stored = {"role": "user", "content": "Where is poker?"}
        with patch.object(brief, "render", side_effect=RuntimeError("synthetic")):
            warning = self.ask()
        api_content = compose_user_api_content(stored["content"], warning, "")
        self.assertIn("MEMCAL UNAVAILABLE", api_content)
        self.assertEqual(stored, {"role": "user", "content": "Where is poker?"})

    def test_subagent_receives_no_personal_context(self):
        self.provider._agent_context = "subagent"
        self.assertEqual(self.ask(), "")


if __name__ == "__main__":
    unittest.main()
