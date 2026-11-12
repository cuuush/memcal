"""Synthetic HTTP boundary checks for local-only model configuration."""

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from memcal import config, llm


class TestLocalModelRequestBoundary(unittest.TestCase):
    def make_config(self, home):
        return config.Config(
            home=Path(home), env={"OPENAI_COMPAT_API_KEY": "local-placeholder"},
            llm_provider="openai-compatible",
            openai_base_url="http://127.0.0.1:11434/v1",
            propose_model="synthetic-local-model", sweep_model="synthetic-local-model",
            match_model="synthetic-local-model", fallback_provider="",
        )

    def test_factory_posts_standard_schema_request_only_to_selected_loopback(self):
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
        response = {"choices": [{"message": {"content": '{"ok":true}'}}]}
        with tempfile.TemporaryDirectory() as home:
            cfg = self.make_config(home)
            client = llm.client_for(cfg)
            self.assertIsInstance(client, llm.OpenAICompatible)
            with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(
                    json.dumps(response).encode())) as post:
                reply = client.complete(model=cfg.propose_model, prefix="Synthetic rules",
                                        suffix="Synthetic source and context", schema=schema,
                                        provider=["remote-provider"], service_tier="flex")
            self.assertEqual(reply.data, {"ok": True})
            post.assert_called_once()
            request = post.call_args.args[0]
            self.assertEqual(request.full_url,
                             "http://127.0.0.1:11434/v1/chat/completions")
            payload = json.loads(request.data)
            self.assertEqual(payload["model"], "synthetic-local-model")
            self.assertEqual(payload["messages"][1]["content"], "Synthetic source and context")
            self.assertEqual(payload["response_format"]["json_schema"]["schema"], schema)
            for key in ("provider", "service_tier", "cache_control"):
                self.assertNotIn(key, payload)

    def test_local_failure_retries_local_endpoint_without_implicit_remote_fallback(self):
        with tempfile.TemporaryDirectory() as home:
            cfg = self.make_config(home)
            client = llm.client_for(cfg)
            with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError(
                    "synthetic local server unavailable")) as post, mock.patch("time.sleep"):
                with self.assertRaises(llm.LLMError):
                    client.complete(model=cfg.propose_model, prefix="Synthetic", suffix="Context")
            self.assertEqual(post.call_count, 6)
            self.assertEqual({call.args[0].full_url for call in post.call_args_list},
                             {"http://127.0.0.1:11434/v1/chat/completions"})

    def test_explicit_remote_fallback_receives_the_same_source_and_context(self):
        from dataclasses import replace

        with tempfile.TemporaryDirectory() as home:
            cfg = replace(self.make_config(home), fallback_provider="openrouter",
                          fallback_model="synthetic-remote-model",
                          env={"OPENAI_COMPAT_API_KEY": "local-placeholder",
                               "OPENROUTER_API_KEY": "synthetic-key"})
            client = llm.client_for(cfg)
            self.assertIsInstance(client, llm.FallbackClient)
            kwargs = dict(model=cfg.propose_model, prefix="Synthetic rules",
                          suffix="Synthetic source and expanded context")
            with mock.patch.object(client.primary, "complete", side_effect=llm.LLMError(
                    "synthetic failure")), mock.patch.object(client.secondary, "complete") as remote:
                client.complete(**kwargs)
            remote.assert_called_once_with(**{**kwargs, "model": "synthetic-remote-model"})

    def test_http_is_accepted_only_for_supported_loopback_names(self):
        for host in ("localhost", "127.0.0.1", "[::1]"):
            with self.subTest(host=host):
                llm.OpenAICompatible("local-placeholder", f"http://{host}:11434/v1")
        with self.assertRaises(llm.LLMError):
            llm.OpenAICompatible("local-placeholder", "http://model.example/v1")


if __name__ == "__main__":
    unittest.main()
