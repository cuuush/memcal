"""Provider selection and the two authenticated programmatic CLI contracts."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memcal import cli, config, llm


SCHEMA = {
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": False,
}


class TestProviderNativeDefaults(unittest.TestCase):
    def test_claude_code_selects_sonnet_five_without_stage_overrides(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            (home / ".env").write_text("MEMCAL_LLM_PROVIDER=claude-code\n")
            cfg = config.load(home)
        self.assertEqual(cfg.propose_model, "claude-sonnet-5")
        self.assertEqual(cfg.sweep_model, "claude-sonnet-5")
        self.assertEqual(cfg.match_model, "claude-sonnet-5")

    def test_codex_selects_luna_and_preserves_an_explicit_stage_model(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            (home / ".env").write_text(
                "MEMCAL_LLM_PROVIDER=codex\nMEMCAL_MATCH_MODEL=my-match-model\n")
            cfg = config.load(home)
        self.assertEqual(cfg.propose_model, "gpt-5.6-luna")
        self.assertEqual(cfg.sweep_model, "gpt-5.6-luna")
        self.assertEqual(cfg.match_model, "my-match-model")


class TestClaudeCodeProgrammaticContract(unittest.TestCase):
    def test_print_mode_returns_a_normal_completion_reply(self):
        raw = {
            "result": '{"ok":true}',
            "structured_output": {"ok": True},
            "session_id": "session-1",
            "model": "claude-sonnet-5",
            "stop_reason": "end_turn",
            "total_cost_usd": 0.012,
            "usage": {
                "input_tokens": 11,
                "cache_read_input_tokens": 3,
                "output_tokens": 4,
            },
        }
        completed = subprocess.CompletedProcess([], 0, json.dumps(raw), "")
        with tempfile.TemporaryDirectory() as root, mock.patch(
                "memcal.llm.subprocess.run", return_value=completed) as run:
            client = llm.ClaudeCode("claude", cwd=Path(root))
            reply = client.complete(
                model="anthropic/claude-sonnet-5", prefix="rules", suffix="bundle",
                schema=SCHEMA, turns=[{"role": "assistant", "content": "earlier"}],
                reasoning_effort="high")

        self.assertEqual(reply.data, {"ok": True})
        self.assertEqual(reply.generation_id, "claude-session-1")
        self.assertEqual(reply.usage.prompt_tokens, 14)
        self.assertEqual(reply.usage.completion_tokens, 4)
        args = run.call_args.args[0]
        self.assertEqual(args[:4], ["claude", "-p", "--model", "claude-sonnet-5"])
        self.assertIn("--json-schema", args)
        self.assertIn("--no-session-persistence", args)
        self.assertIn("--safe-mode", args)
        self.assertIn("--tools", args)
        self.assertIn("<system>\nrules\n</system>", run.call_args.kwargs["input"])
        self.assertIn("<assistant>\nearlier\n</assistant>", run.call_args.kwargs["input"])

    def test_cli_error_envelope_is_not_mistaken_for_a_reply(self):
        completed = subprocess.CompletedProcess(
            [], 1, json.dumps({"is_error": True, "result": "Not logged in"}), "")
        with tempfile.TemporaryDirectory() as root, mock.patch(
                "memcal.llm.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(llm.LLMError, "Not logged in"):
                llm.ClaudeCode("claude", cwd=Path(root)).complete(
                    model="claude-sonnet-5", prefix="p", suffix="s")


class TestCodexProgrammaticContract(unittest.TestCase):
    def test_exec_jsonl_returns_a_normal_completion_reply(self):
        events = [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "item.completed", "item": {
                "type": "agent_message", "text": '{"ok":true}'}},
            {"type": "turn.completed", "usage": {
                "input_tokens": 12, "cached_input_tokens": 5,
                "output_tokens": 3}},
        ]
        completed = subprocess.CompletedProcess(
            [], 0, "\n".join(json.dumps(event) for event in events), "")
        observed_schema = {}

        def execute(args, **kwargs):
            path = Path(args[args.index("--output-schema") + 1])
            observed_schema.update(json.loads(path.read_text()))
            return completed

        with tempfile.TemporaryDirectory() as root, mock.patch(
                "memcal.llm.subprocess.run", side_effect=execute) as run:
            client = llm.Codex("codex", cwd=Path(root))
            reply = client.complete(
                model="openai/gpt-5.6-luna", prefix="rules", suffix="bundle",
                schema=SCHEMA, reasoning_effort="medium")

        self.assertEqual(reply.data, {"ok": True})
        self.assertEqual(reply.generation_id, "codex-thread-1")
        self.assertEqual(reply.usage.prompt_tokens, 12)
        self.assertEqual(observed_schema, SCHEMA)
        args = run.call_args.args[0]
        self.assertEqual(args[:4], ["codex", "--ask-for-approval", "never", "exec"])
        self.assertIn("--ephemeral", args)
        self.assertIn("--ignore-user-config", args)
        self.assertEqual(args[-1], "-")
        self.assertEqual(run.call_args.kwargs["cwd"], Path(root))

    def test_failed_turn_is_not_mistaken_for_a_reply(self):
        events = [{"type": "turn.failed", "error": {"message": "bad auth"}}]
        completed = subprocess.CompletedProcess([], 1, json.dumps(events[0]), "")
        with tempfile.TemporaryDirectory() as root, mock.patch(
                "memcal.llm.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(llm.LLMError, "turn.failed"):
                llm.Codex("codex", cwd=Path(root)).complete(
                    model="gpt-5.6-luna", prefix="p", suffix="s")


class TestProviderFactory(unittest.TestCase):
    def test_factory_selects_each_configured_transport(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = config.Config(home=Path(root), llm_provider="claude-code")
            self.assertIsInstance(llm.client_for(cfg), llm.ClaudeCode)
            cfg.llm_provider = "codex"
            self.assertIsInstance(llm.client_for(cfg), llm.Codex)

class TestInteractiveProviderSetup(unittest.TestCase):
    def test_guided_setup_preserves_unowned_env_and_saves_all_stage_models(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            env_path = home / ".env"
            env_path.write_text("# personal\nSOME_SOURCE_TOKEN=keep-me\n")
            args = argparse.Namespace(
                home=str(home), provider=None, model=None, api_key=None)
            with mock.patch("builtins.input", side_effect=["2", ""]), mock.patch(
                    "memcal.llm.provider_status", return_value=(True, "/bin/claude")):
                result = cli.cmd_setup(args)
            saved = env_path.read_text()
            mode = stat.S_IMODE(os.stat(env_path).st_mode)

        self.assertEqual(result, 0)
        self.assertIn("SOME_SOURCE_TOKEN=keep-me", saved)
        self.assertIn("MEMCAL_LLM_PROVIDER=claude-code", saved)
        self.assertEqual(saved.count("claude-sonnet-5"), 3)
        self.assertEqual(mode, 0o600)


if __name__ == "__main__":
    unittest.main()
