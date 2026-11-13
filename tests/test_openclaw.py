"""OpenClaw's native hook and its Python state boundary."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memcal import cli, config, db, harness, mcp_server, todos


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


class TestOpenClawGuidanceTools(unittest.TestCase):
    def test_context_refers_only_to_advertised_mcp_tools(self):
        with tempfile.TemporaryDirectory() as root:
            context = harness.context(config.load(root), "What do I know about a person?")

        self.assertIn("open it with memcal_open (accepts", context)
        guidance = context.split("MEMCAL SNAPSHOT", 1)[0]
        mentioned = set(re.findall(r"\bmemcal_[a-z_]+\b", guidance))
        advertised = {tool["name"] for tool in mcp_server.TOOLS}
        self.assertTrue(mentioned <= advertised, mentioned - advertised)


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
                "SELECT * FROM archive WHERE channel = 'agent'").fetchall()
            conn.close()

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["addressed_to"], "machine")
        self.assertEqual(json.loads(rows[0]["meta"])["origin"], "openclaw-user")


class TestOpenClawPackageShape(unittest.TestCase):
    def test_manifest_declares_the_shipped_entry_point(self):
        root = config.PROJECT_ROOT / "integrations" / "openclaw"
        manifest = json.loads((root / "openclaw.plugin.json").read_text())
        package = json.loads((root / "package.json").read_text())
        self.assertEqual(manifest["id"], "memcal")
        self.assertEqual(package["openclaw"]["extensions"], ["./index.ts"])
        self.assertTrue((root / "index.ts").is_file())


class TestOpenClawHooksReachTheStore(unittest.TestCase):
    def test_registered_hooks_inject_context_and_archive_inbound_turns(self):
        node = shutil.which("node")
        if not node or subprocess.run([node, "--experimental-strip-types", "--version"],
                                      capture_output=True, timeout=5).returncode != 0:
            self.skipTest("Node with native TypeScript support is required")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cfg = config.load(root / "store")
            conn = db.open_db(cfg.db_path)
            try:
                todos.open_todo(conn, "Pack a picnic basket")
            finally:
                conn.close()

            source = config.PROJECT_ROOT / "integrations" / "openclaw"
            shutil.copyfile(source / "index.ts", root / "index.ts")
            sdk = root / "node_modules" / "openclaw"
            sdk.mkdir(parents=True)
            (sdk / "package.json").write_text(json.dumps({
                "type": "module", "exports": {"./plugin-sdk/plugin-entry": "./entry.js"},
            }), encoding="utf-8")
            (sdk / "entry.js").write_text(
                "export const definePluginEntry = entry => entry;\n", encoding="utf-8")
            (root / "exercise.mjs").write_text("""
import assert from "node:assert/strict";
import plugin from "./index.ts";

const [pythonCommand, memcalSrc, memcalHome] = process.argv.slice(2);
const hooks = new Map();
plugin.register({
  pluginConfig: {pythonCommand, memcalSrc, memcalHome},
  on: (name, callback) => hooks.set(name, callback),
  logger: {warn: message => assert.fail(message)},
});
const context = await hooks.get("before_prompt_build")({prompt: "What is on my list?"}, {});
assert.match(context.appendContext, /Pack a picnic basket/);
await hooks.get("message_received")(
  {content: "The picnic is Saturday", messageId: "picnic-1"},
  {sessionKey: "agent:sample:chat"});
""", encoding="utf-8")
            proc = subprocess.run(
                [node, "--experimental-strip-types", "exercise.mjs",
                 sys.executable, str(config.PROJECT_ROOT), str(cfg.home)],
                cwd=root, capture_output=True, text=True, timeout=20)
            self.assertEqual(proc.returncode, 0, proc.stderr)

            conn = db.open_db(cfg.db_path)
            try:
                rows = conn.execute("SELECT text, addressed_to FROM archive WHERE channel = 'agent'")
                self.assertEqual([tuple(row) for row in rows],
                                 [("The picnic is Saturday", "machine")])
            finally:
                conn.close()


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
