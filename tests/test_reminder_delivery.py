"""Reminders wake the chat session as a real turn (issue #56)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

HERMES = Path.home() / ".hermes" / "hermes-agent"
PLUGIN = (Path(__file__).resolve().parent.parent
          / "integrations" / "hermes" / "memcal" / "__init__.py")


def _load_delivery():
    """Load the Hermes plugin by path under a synthetic namespace.

    The plugin package and the app package are both called `memcal`;
    Hermes avoids the collision the same way in production.
    """
    sys.path.insert(0, str(HERMES))
    spec = importlib.util.spec_from_file_location(
        "_memcal_reminder_delivery", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_memcal_reminder_delivery"] = module
    spec.loader.exec_module(module)
    return module


def _run_due_reminders(*argv) -> str:
    """Run tools/due_reminders.py main with patched argv; return stdout."""
    from unittest import mock
    import tools.due_reminders as due
    buf = io.StringIO()
    with mock.patch.object(sys, "argv", ["due_reminders.py", *argv]):
        with contextlib.redirect_stdout(buf):
            assert due.main() == 0
    return buf.getvalue()


class TestDueRemindersJsonFeedsDelivery(unittest.TestCase):
    """--format json carries the structured payload the Hermes side consumes."""

    def setUp(self):
        from memcal import config, db, todos
        self.tmp = tempfile.TemporaryDirectory()
        self.home = str(Path(self.tmp.name))
        cfg = config.load(self.home)
        cfg.ensure_dirs()
        self.conn = db.open_db(cfg.db_path)
        db.set_today("2026-08-12T09:00:01")
        self.todo, _ = todos.open_todo(
            self.conn, "return the EZ-Pass", remind_at="2026-08-12T09:00:00")

    def tearDown(self):
        from memcal import db
        try:
            self.conn.close()
        finally:
            db.set_today(None)
            self.tmp.cleanup()

    def test_json_payload_carries_text_and_keys(self):
        out = _run_due_reminders("--home", self.home, "--format", "json")
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertTrue(payload["wakeAgent"])
        self.assertEqual(payload["count"], 1)
        entry = payload["reminders"][0]
        self.assertEqual(entry["key"], self.todo.key)
        self.assertEqual(entry["kind"], "todo")
        self.assertIn("return the EZ-Pass", entry["text"])
        self.assertIn("return the EZ-Pass", entry["line"])

    def test_text_mode_stays_human_readable(self):
        out = _run_due_reminders("--home", self.home)
        self.assertIn("return the EZ-Pass", out)
        # Text mode with something due wakes via non-JSON output (no gate line).
        last = out.strip().splitlines()[-1].strip()
        try:
            json.loads(last)
            gate_line = True
        except (json.JSONDecodeError, ValueError):
            gate_line = False
        self.assertFalse(gate_line)

    def test_empty_json_stays_silent(self):
        from memcal import db, todos
        todos.close(self.conn, self.todo.key)
        out = _run_due_reminders("--home", self.home, "--format", "json")
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertFalse(payload["wakeAgent"])
        self.assertEqual(payload["reminders"], [])
        db.set_today("2026-08-12T09:00:01")


@unittest.skipUnless(HERMES.is_dir(), "Hermes not installed")
class TestReminderDeliveryAppendsOneSessionTurn(unittest.TestCase):
    """A due reminder lands as one assistant turn the follow-up can refer to."""

    def setUp(self):
        from memcal import config, db, todos
        self.tmp = tempfile.TemporaryDirectory()
        self.home = str(Path(self.tmp.name))
        cfg = config.load(self.home)
        cfg.ensure_dirs()
        self.conn = db.open_db(cfg.db_path)
        db.set_today("2026-08-12T09:00:01")
        self.todo, _ = todos.open_todo(
            self.conn, "return the EZ-Pass", remind_at="2026-08-12T09:00:00")
        self.session = "chat-session-1"

    def tearDown(self):
        from memcal import db
        try:
            self.conn.close()
        finally:
            db.set_today(None)
            self.tmp.cleanup()

    def test_one_turn_with_referent_for_the_followup(self):
        from memcal import todos, trace
        module = _load_delivery()
        payload = json.loads(_run_due_reminders(
            "--home", self.home, "--format", "json").strip().splitlines()[-1])
        thread = f"hermes:{self.session}"
        before = trace.conversation(self.conn, stream="agent", thread=thread)
        self.assertEqual(before, [], "no referent before the wake turn")
        transcript: list = []
        before_n = self.conn.execute(
            "SELECT count(*) AS n FROM archive WHERE thread = ?",
            (thread,)).fetchone()["n"]
        result = module.deliver_due_reminders(
            self.conn, self.session, payload, transcript=transcript)
        self.assertTrue(result["delivered"])
        after_n = self.conn.execute(
            "SELECT count(*) AS n FROM archive WHERE thread = ?",
            (thread,)).fetchone()["n"]
        self.assertEqual(after_n - before_n, 1, "exactly one session turn")
        self.assertEqual(len(transcript), 1)
        turn = transcript[0]
        self.assertEqual(turn["role"], "assistant")
        self.assertIn("return the EZ-Pass", turn["content"])
        self.assertEqual(turn["metadata"]["origin"], module.REMINDER_ORIGIN)
        # Authorship: attributable to the reminder/wake path, never the owner.
        row = self.conn.execute(
            "SELECT * FROM archive WHERE thread = ? ORDER BY id DESC LIMIT 1",
            (thread,)).fetchone()
        self.assertIn("return the EZ-Pass", row["text"])
        self.assertFalse(row["from_me"], "must not be disguised as user words")
        self.assertNotEqual(row["person"], "me")
        import json as _json
        meta = _json.loads(row["meta"])
        self.assertEqual(meta.get("origin"), module.REMINDER_ORIGIN)
        # The simulated follow-up now has a referent in context.
        followup = "yeah I'll do it tomorrow"
        convo = trace.conversation(self.conn, stream="agent", thread=thread)
        texts = [line["text"] for line in convo]
        self.assertTrue(any("return the EZ-Pass" in text for text in texts),
                        "the reminder text must be in the session transcript")
        self.assertTrue(any("return the EZ-Pass" in t for t in
                            [turn["content"]] + texts),
                        f"{followup!r} has 'it' = the reminded todo")
        # Delivery marks the poke, so the next poll stays quiet.
        self.assertEqual(todos.due_reminders(self.conn), [])


@unittest.skipUnless(HERMES.is_dir(), "Hermes not installed")
class TestReminderDeliveryAppendsNothingWhenNothingDue(unittest.TestCase):
    """The wakeAgent-false path appends no turn anywhere."""

    def setUp(self):
        from memcal import config, db
        self.tmp = tempfile.TemporaryDirectory()
        self.home = str(Path(self.tmp.name))
        cfg = config.load(self.home)
        cfg.ensure_dirs()
        self.conn = db.open_db(cfg.db_path)
        db.set_today("2026-08-12T09:00:01")
        self.session = "quiet-session"

    def tearDown(self):
        from memcal import db
        try:
            self.conn.close()
        finally:
            db.set_today(None)
            self.tmp.cleanup()

    def test_nothing_due_appends_nothing(self):
        module = _load_delivery()
        payload = json.loads(_run_due_reminders(
            "--home", self.home, "--format", "json").strip().splitlines()[-1])
        self.assertFalse(payload["wakeAgent"])
        transcript: list = []
        before = self.conn.execute(
            "SELECT count(*) AS n FROM archive").fetchone()["n"]
        result = module.deliver_due_reminders(
            self.conn, self.session, payload, transcript=transcript)
        self.assertFalse(result["delivered"])
        self.assertEqual(result["reason"], "nothing-due")
        after = self.conn.execute(
            "SELECT count(*) AS n FROM archive").fetchone()["n"]
        self.assertEqual(after, before)
        self.assertEqual(transcript, [])


class TestDueRemindersMarkIsPreserved(unittest.TestCase):
    """--mark still records the poke and snoozes it, in both formats."""

    def setUp(self):
        from memcal import config, db, todos
        self.tmp = tempfile.TemporaryDirectory()
        self.home = str(Path(self.tmp.name))
        cfg = config.load(self.home)
        cfg.ensure_dirs()
        self.conn = db.open_db(cfg.db_path)
        db.set_today("2026-08-12T09:00:01")
        self.todo, _ = todos.open_todo(
            self.conn, "book the restaurant", remind_at="2026-08-12T09:00:00")

    def tearDown(self):
        from memcal import db
        try:
            self.conn.close()
        finally:
            db.set_today(None)
            self.tmp.cleanup()

    def test_text_mark_snoozes(self):
        from memcal import todos
        _run_due_reminders("--home", self.home, "--mark")
        self.assertEqual(todos.due_reminders(self.conn), [])

    def test_json_mark_snoozes(self):
        from memcal import todos
        out = _run_due_reminders("--home", self.home, "--format", "json", "--mark")
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertTrue(payload["wakeAgent"])
        self.assertEqual(todos.due_reminders(self.conn), [])


if __name__ == "__main__":
    unittest.main()
