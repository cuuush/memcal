"""OpenClaw's native hook and its Python state boundary."""

from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memcal import cli, config, db, harness, todos


class TestOpenClawContextIsFresh(unittest.TestCase):
    def test_each_turn_reads_current_typed_state(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = config.load(root)
            cfg.ensure_dirs()
            conn = db.open_db(cfg.db_path)
            first_todo, _ = todos.open_todo(conn, "First typed snapshot")
            conn.commit()
            conn.close()
            first = harness.context(cfg, "what is coming up?")

            conn = db.open_db(cfg.db_path)
            todos.close(conn, first_todo.key)
            todos.open_todo(conn, "Second typed snapshot")
            conn.commit()
            conn.close()
            second = harness.context(cfg, "what is coming up?")

        self.assertIn("First typed snapshot", first)
        self.assertIn("Second typed snapshot", second)
        self.assertNotIn("First typed snapshot", second)
        self.assertNotEqual(first, second)


class TestOpenClawArchivesOnlyInboundTurn(unittest.TestCase):
    def test_message_identity_deduplicates_replayed_hooks(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = config.load(root)
            first = harness.archive_user_turn(
                cfg, "Poker is Saturday and I am going", harness="openclaw",
                session_id="session-1", message_id="message-1")
            second = harness.archive_user_turn(
                cfg, "Poker is Saturday and I am going", harness="openclaw",
                session_id="session-1", message_id="message-1")
            conn = db.open_db(cfg.db_path)
            rows = conn.execute(
                "SELECT * FROM archive WHERE stream = 'agent'").fetchall()
            conn.close()

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["addressed_to"], "machine")
        self.assertEqual(json.loads(rows[0]["meta"])["origin"], "openclaw-user")


class TestOpenClawPackageShape(unittest.TestCase):
    def test_plugin_has_prompt_and_inbound_hooks(self):
        root = config.PROJECT_ROOT / "integrations" / "openclaw"
        manifest = json.loads((root / "openclaw.plugin.json").read_text())
        package = json.loads((root / "package.json").read_text())
        source = (root / "index.ts").read_text()

        self.assertEqual(manifest["id"], "memcal")
        self.assertEqual(package["openclaw"]["extensions"], ["./index.ts"])
        self.assertIn('"before_prompt_build"', source)
        self.assertIn('"message_received"', source)


class TestOpenClawSetupRegistersBothBoundaries(unittest.TestCase):
    def test_setup_links_context_plugin_and_existing_mcp_server(self):
        with tempfile.TemporaryDirectory() as root:
            commands = []

            def run(command):
                commands.append(command)
                return True, "ok"

            args = argparse.Namespace(home=root, action="setup", yes=True)
            with mock.patch("memcal.cli._run_openclaw", side_effect=run), \
                    mock.patch("memcal.cli._openclaw_config_path",
                               return_value=Path(root) / "absent.json"):
                result = cli.cmd_openclaw(args)

        self.assertEqual(result, 0)
        self.assertEqual(commands[0][:4], ["openclaw", "plugins", "install", "--link"])
        self.assertEqual(commands[1], ["openclaw", "plugins", "enable", "memcal"])
        self.assertEqual(commands[2][:4], ["openclaw", "mcp", "set", "memcal"])
        mcp = json.loads(commands[2][4])
        self.assertEqual(mcp["args"], ["-m", "memcal.mcp_server"])
        self.assertEqual(mcp["env"]["MEMCAL_HOME"], root)


class TestOpenClawPrunesStalePluginLinks(unittest.TestCase):
    def test_prune_removes_only_gone_memcal_links(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            live = root / "checkout" / "integrations" / "openclaw"
            live.mkdir(parents=True)
            other = root / ".openclaw" / "memory-palace"
            other.mkdir(parents=True)
            dead = root / "gone" / "integrations" / "openclaw"  # never created
            cfg_path = root / "openclaw.json"
            cfg_path.write_text(json.dumps({
                "plugins": {"load": {"paths": [str(other), str(dead), str(live)]}},
                "keep": True,
            }))

            removed = cli._prune_stale_openclaw_plugins(cfg_path)

            self.assertEqual(removed, [str(dead)])
            data = json.loads(cfg_path.read_text())
            self.assertEqual(data["plugins"]["load"]["paths"], [str(other), str(live)])
            self.assertTrue(data["keep"])  # unrelated config is untouched

    def test_prune_is_a_safe_noop_when_absent_or_unparseable(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            self.assertEqual(cli._prune_stale_openclaw_plugins(root / "missing.json"), [])
            junk = root / "junk.json"
            junk.write_text("{ not json")
            self.assertEqual(cli._prune_stale_openclaw_plugins(junk), [])

    def test_setup_prunes_before_installing(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            cfg_path = root / "openclaw.json"
            dead = root / "gone" / "integrations" / "openclaw"
            cfg_path.write_text(json.dumps(
                {"plugins": {"load": {"paths": [str(dead)]}}}))
            args = argparse.Namespace(home=str(root), action="setup", yes=True)
            with mock.patch("memcal.cli._run_openclaw", return_value=(True, "ok")), \
                    mock.patch("memcal.cli._openclaw_config_path", return_value=cfg_path):
                result = cli.cmd_openclaw(args)
            self.assertEqual(result, 0)
            self.assertEqual(
                json.loads(cfg_path.read_text())["plugins"]["load"]["paths"], [])


if __name__ == "__main__":
    unittest.main()
