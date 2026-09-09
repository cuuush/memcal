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

    def test_antigravity_selects_a_flash_model_without_stage_overrides(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            (home / ".env").write_text("MEMCAL_LLM_PROVIDER=antigravity\n")
            cfg = config.load(home)
        self.assertEqual(cfg.propose_model, llm.PROVIDER_DEFAULT_MODELS["antigravity"])
        self.assertEqual(cfg.sweep_model, cfg.propose_model)
        self.assertEqual(cfg.match_model, cfg.propose_model)

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

    def test_reasoning_summary_is_captured_and_the_effort_comes_from_the_spec(self):
        """The backend read no reasoning items and requested no summary, so `reasoning`
        was always empty; the bare native model name also missed its `ENDPOINTS` row,
        so the tuned effort never applied."""
        events = [
            {"type": "thread.started", "thread_id": "thread-2"},
            {"type": "item.completed", "item": {
                "type": "reasoning", "summary": [{"text": "Two dates conflict."}]}},
            {"type": "item.completed", "item": {
                "type": "agent_message", "text": '{"ok":true}'}},
            {"type": "turn.completed", "usage": {
                "input_tokens": 9, "output_tokens": 4, "reasoning_output_tokens": 40}},
        ]
        completed = subprocess.CompletedProcess(
            [], 0, "\n".join(json.dumps(event) for event in events), "")
        with tempfile.TemporaryDirectory() as root, mock.patch(
                "memcal.llm.subprocess.run", return_value=completed) as run:
            reply = llm.Codex("codex", cwd=Path(root)).complete(
                model="gpt-5.6-luna", prefix="rules", suffix="bundle")

        self.assertEqual(reply.reasoning, "Two dates conflict.")
        self.assertEqual(reply.usage.reasoning_tokens, 40)
        args = run.call_args.args[0]
        self.assertIn('model_reasoning_summary="detailed"', args)
        # The spec lives under the OpenRouter id; the CLI is given the native name.
        self.assertIn('model_reasoning_effort="medium"', args)

    def test_a_backend_that_shows_no_reasoning_stores_none_rather_than_inventing_it(self):
        events = [
            {"type": "thread.started", "thread_id": "thread-3"},
            {"type": "item.completed", "item": {
                "type": "agent_message", "text": '{"ok":true}'}},
            {"type": "turn.completed", "usage": {"reasoning_output_tokens": 41}},
        ]
        completed = subprocess.CompletedProcess(
            [], 0, "\n".join(json.dumps(event) for event in events), "")
        with tempfile.TemporaryDirectory() as root, mock.patch(
                "memcal.llm.subprocess.run", return_value=completed):
            reply = llm.Codex("codex", cwd=Path(root)).complete(
                model="gpt-5.6-luna", prefix="p", suffix="s")
        # Billed for thinking it will not show. Both halves are recorded, because
        # "it did not think" and "it will not show us" are different facts.
        self.assertEqual(reply.reasoning, "")
        self.assertEqual(reply.usage.reasoning_tokens, 41)

    def test_reasoning_is_read_from_the_older_event_name_too(self):
        events = [
            {"type": "thread.started", "thread_id": "thread-4"},
            {"type": "agent_reasoning", "text": "Checking the guest list."},
            {"type": "item.completed", "item": {
                "type": "agent_message", "text": "{}"}},
        ]
        completed = subprocess.CompletedProcess(
            [], 0, "\n".join(json.dumps(event) for event in events), "")
        with tempfile.TemporaryDirectory() as root, mock.patch(
                "memcal.llm.subprocess.run", return_value=completed):
            reply = llm.Codex("codex", cwd=Path(root)).complete(
                model="gpt-5.6-luna", prefix="p", suffix="s")
        self.assertEqual(reply.reasoning, "Checking the guest list.")

    def test_failed_turn_is_not_mistaken_for_a_reply(self):
        events = [{"type": "turn.failed", "error": {"message": "bad auth"}}]
        completed = subprocess.CompletedProcess([], 1, json.dumps(events[0]), "")
        with tempfile.TemporaryDirectory() as root, mock.patch(
                "memcal.llm.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(llm.LLMError, "turn.failed"):
                llm.Codex("codex", cwd=Path(root)).complete(
                    model="gpt-5.6-luna", prefix="p", suffix="s")


class TestAntigravityProgrammaticContract(unittest.TestCase):
    def test_print_mode_returns_a_normal_completion_reply(self):
        raw = {
            "conversation_id": "conv-1",
            "status": "SUCCESS",
            "response": '{"ok":true}\n',
            "structured_output": {"ok": True},
            "usage": {
                "input_tokens": 20,
                "output_tokens": 6,
                "thinking_tokens": 4,
                "cache_read_tokens": 7,
                "total_tokens": 26,
            },
        }
        completed = subprocess.CompletedProcess([], 0, json.dumps(raw), "")
        observed_schema = {}

        def execute(args, **kwargs):
            path = Path(args[args.index("--json-schema") + 1])
            observed_schema.update(json.loads(path.read_text()))
            return completed

        with tempfile.TemporaryDirectory() as root, mock.patch(
                "memcal.llm.subprocess.run", side_effect=execute) as run:
            reply = llm.Antigravity("agy", cwd=Path(root)).complete(
                model="gemini-3.8-flash-high", prefix="rules", suffix="bundle",
                schema=SCHEMA, turns=[{"role": "assistant", "content": "earlier"}])

        self.assertEqual(reply.data, {"ok": True})
        self.assertEqual(reply.generation_id, "agy-conv-1")
        self.assertEqual(observed_schema, SCHEMA)
        # A cache hit is reported beside the fresh input, not inside it.
        self.assertEqual(reply.usage.prompt_tokens, 27)
        self.assertEqual(reply.usage.cached_tokens, 7)
        self.assertEqual(reply.usage.reasoning_tokens, 4)
        args = run.call_args.args[0]
        self.assertEqual(args[:5],
                         ["agy", "--output-format", "json", "--model",
                          "gemini-3.8-flash-high"])
        self.assertIn("--sandbox", args)
        # The prompt is attached to the flag and last, so no later argument can be
        # swallowed as the thing to ask.
        self.assertTrue(args[-1].startswith("--print="))
        self.assertIn("<system>\nrules\n</system>", args[-1])
        self.assertIn("<assistant>\nearlier\n</assistant>", args[-1])

    def test_a_model_naming_its_own_budget_is_not_also_given_an_effort_flag(self):
        raw = {"conversation_id": "conv-2", "status": "SUCCESS", "response": "{}"}
        completed = subprocess.CompletedProcess([], 0, json.dumps(raw), "")
        with tempfile.TemporaryDirectory() as root, mock.patch(
                "memcal.llm.subprocess.run", return_value=completed) as run:
            llm.Antigravity("agy", cwd=Path(root)).complete(
                model="gemini-3.8-flash-high", prefix="p", suffix="s",
                reasoning_effort="low")
            suffixless = llm.Antigravity("agy", cwd=Path(root))
            suffixless.complete(model="gpt-oss-120b", prefix="p", suffix="s",
                                reasoning_effort="low")
        first, second = [call.args[0] for call in run.call_args_list]
        self.assertNotIn("--effort", first)
        self.assertEqual(second[second.index("--effort") + 1], "low")

    def test_success_with_an_empty_response_is_a_failed_call_not_an_empty_diff(self):
        raw = {"conversation_id": "conv-3", "status": "SUCCESS", "response": "",
               "usage": {"input_tokens": 900, "output_tokens": 40}}
        completed = subprocess.CompletedProcess([], 0, json.dumps(raw), "")
        with tempfile.TemporaryDirectory() as root, mock.patch(
                "memcal.llm.subprocess.run", return_value=completed):
            client = llm.Antigravity("agy", cwd=Path(root))
            with self.assertRaisesRegex(llm.LLMError, "no response"):
                client.complete(model="gemini-3.8-flash-high", prefix="p", suffix="s")
        # Nothing was answered, so nothing is charged as an answered call.
        self.assertEqual(client.usage.calls, 0)

    def test_error_status_is_not_mistaken_for_a_reply(self):
        raw = {"conversation_id": "", "status": "ERROR", "response": "",
               "error": "invalid model selection"}
        completed = subprocess.CompletedProcess([], 1, json.dumps(raw), "")
        with tempfile.TemporaryDirectory() as root, mock.patch(
                "memcal.llm.subprocess.run", return_value=completed):
            with self.assertRaisesRegex(llm.LLMError, "invalid model selection"):
                llm.Antigravity("agy", cwd=Path(root)).complete(
                    model="gemini-3.8-flash-high", prefix="p", suffix="s")


class TestProviderFactory(unittest.TestCase):
    def test_factory_selects_each_configured_transport(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = config.Config(home=Path(root), llm_provider="claude-code")
            self.assertIsInstance(llm.client_for(cfg), llm.ClaudeCode)
            cfg.llm_provider = "codex"
            self.assertIsInstance(llm.client_for(cfg), llm.Codex)
            cfg.llm_provider = "antigravity"
            self.assertIsInstance(llm.client_for(cfg), llm.Antigravity)

    def test_every_cli_provider_has_a_command_field_a_default_model_and_a_status(self):
        """Three tables have to agree about a provider, and a new backend that is
        missing from one of them fails only at the moment someone selects it."""
        self.assertEqual(set(llm.PROVIDER_COMMANDS),
                         {"claude-code", "codex", "antigravity"})
        for provider, backend in llm.PROVIDER_COMMANDS.items():
            with tempfile.TemporaryDirectory() as root:
                cfg = config.Config(home=Path(root), llm_provider=provider)
                self.assertTrue(backend.command(cfg))
                self.assertTrue(backend.env.startswith("MEMCAL_"))
                self.assertIn(provider, llm.PROVIDER_DEFAULT_MODELS)
                self.assertIsInstance(llm.client_for(cfg), llm.ProgrammaticClient)
                ok, detail = llm.provider_status(cfg)
                self.assertIsInstance(ok, bool)
                self.assertTrue(detail)

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


class TestACliBackendIsGivenTheSameDeadlineMemcalIsWaitingOut(unittest.TestCase):
    """`agy` runs its own five-minute clock and memcal was waiting fifteen.

    When that clock expires the CLI returns *partial output with returncode 0* — an
    `ERROR` status, or a `SUCCESS` carrying an empty response and a zeroed token count.
    Neither is a refusal, so a real propose request with several bundles and a thinking
    budget failed every single time while a one-line probe succeeded, and the reported
    reason named the symptom rather than the deadline.
    """

    def _argv(self, timeout=900.0):
        raw = {"conversation_id": "c", "status": "SUCCESS", "response": '{"ok":true}',
               "structured_output": {"ok": True}, "usage": {}}
        completed = subprocess.CompletedProcess([], 0, json.dumps(raw), "")
        seen = {}

        def execute(args, **kwargs):
            seen["args"] = list(args)
            return completed

        with tempfile.TemporaryDirectory() as root, mock.patch(
                "memcal.llm.subprocess.run", side_effect=execute):
            llm.Antigravity("agy", cwd=Path(root), timeout=timeout).complete(
                model="gemini-3.8-flash-high", prefix="rules", suffix="bundle",
                schema=SCHEMA)
        return seen["args"]

    def test_the_cli_is_told_how_long_it_has(self):
        args = self._argv()
        self.assertIn("--print-timeout", args)

    def test_it_is_the_configured_timeout_and_slightly_under_it(self):
        """Slightly under, so the CLI reports its own timeout instead of being killed —
        a killed process leaves no status to explain itself with."""
        args = self._argv(timeout=600.0)
        given = args[args.index("--print-timeout") + 1]
        self.assertEqual(given, "570s")

    def test_an_empty_success_names_the_deadline_as_the_likely_cause(self):
        raw = {"conversation_id": "c", "status": "SUCCESS", "response": "", "usage": {}}
        completed = subprocess.CompletedProcess([], 0, json.dumps(raw), "")
        with tempfile.TemporaryDirectory() as root, mock.patch(
                "memcal.llm.subprocess.run", return_value=completed):
            with self.assertRaises(llm.LLMError) as caught:
                llm.Antigravity("agy", cwd=Path(root)).complete(
                    model="gemini-3.8-flash-high", prefix="p", suffix="s")
        self.assertIn("print timeout", str(caught.exception))


class TestASpentSubscriptionIsNotWorthRetrying(unittest.TestCase):
    """A failed request is worth splitting and re-sending; a spent account is not.

    Retrying into a quota wall spends the pass's whole wall-clock budget producing the
    same sentence eight times in parallel, then reports a low score for a run in which
    no model read anything.
    """

    def _fail(self, message):
        completed = subprocess.CompletedProcess([], 1, "", message)
        return llm.ProgrammaticClient._failure(completed)

    def test_a_quota_wall_is_its_own_kind_of_failure(self):
        for message in (
                "Individual quota reached. Please upgrade your subscription.",
                "usage limit reached, resets in 4h",
                "You are out of credits",
        ):
            self.assertIsInstance(self._fail(message), llm.QuotaExhausted, message)

    def test_an_ordinary_failure_stays_ordinary(self):
        for message in ("invalid model name", "connection reset", "no output"):
            failure = self._fail(message)
            self.assertIsInstance(failure, llm.LLMError, message)
            self.assertNotIsInstance(failure, llm.QuotaExhausted, message)

    def test_the_reset_window_survives_into_the_message(self):
        failure = self._fail("Individual quota reached. Resets in 166h33m20s.")
        self.assertIn("166h33m20s", str(failure))


if __name__ == "__main__":
    unittest.main()
