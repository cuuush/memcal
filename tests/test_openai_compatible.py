"""A generic chat endpoint receives plain requests and records real usage."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memcal import config, llm
from memcal.dream import bundle as bundle_stage, propose as propose_stage


class TestOpenAICompatibleEndpoint(unittest.TestCase):
    def test_chat_request_has_no_openrouter_fields(self):
        client = llm.OpenAICompatible("test-key", "https://example.test/v1")
        response = {
            "id": "response-1",
            "model": "test-model",
            "choices": [{"message": {"content": '{"reviewed":[]}'},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4},
        }
        stream = mock.MagicMock()
        stream.__enter__.return_value.read.return_value = json.dumps(response).encode()
        with mock.patch.object(llm.urllib.request, "urlopen", return_value=stream) as send:
            reply = client.complete(model="test-model", prefix="system text",
                                    suffix="conversation text", schema={"type": "object"},
                                    turns=[{"role": "assistant", "content": "prior"}])
        request = send.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://example.test/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertEqual(payload["messages"][0], {"role": "system", "content": "system text"})
        self.assertEqual(payload["messages"][-1], {"role": "assistant", "content": "prior"})
        self.assertFalse({"provider", "usage", "service_tier", "response_format",
                          "reasoning"} & payload.keys())
        self.assertEqual(reply.data, {"reviewed": []})
        self.assertEqual((reply.usage.prompt_tokens, reply.usage.completion_tokens),
                         (12, 4))

    def test_config_requires_key_url_and_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".env").write_text(
                "MEMCAL_LLM_PROVIDER=openai-compatible\n"
                "MEMCAL_OPENAI_BASE_URL=https://example.test/v1\n"
                "OPENAI_COMPAT_API_KEY=test-key\n"
                "MEMCAL_PROPOSE_MODEL=test-model\n"
                "MEMCAL_SWEEP_MODEL=test-model\n"
                "MEMCAL_MATCH_MODEL=test-model\n")
            cfg = config.load(home)
            self.assertEqual(llm.provider_status(cfg), (True, "API key and endpoint configured"))
            self.assertIsInstance(llm.client_for(cfg), llm.OpenAICompatible)
            cfg.env.pop("OPENAI_COMPAT_API_KEY")
            with mock.patch.dict("os.environ", {"OPENAI_COMPAT_API_KEY": ""}):
                self.assertFalse(llm.provider_status(cfg)[0])

    def test_refuses_plain_http_off_loopback(self):
        with self.assertRaises(llm.LLMError):
            llm.OpenAICompatible("test-key", "http://example.test/v1")

    def test_propose_output_floor_reserves_thinking_headroom(self):
        cfg = config.Config(home=Path("/tmp"))
        cfg.propose_model = "test-model"
        group = [bundle_stage.Bundle(entity="person:Devon")]
        self.assertLess(propose_stage.model_ceiling(cfg, group), 8192)
        cfg.propose_output_floor = 8192
        self.assertEqual(propose_stage.model_ceiling(cfg, group), 8192)


if __name__ == "__main__":
    unittest.main()
