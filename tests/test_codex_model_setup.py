"""Setup reads the signed-in Codex picker without spending an inference turn."""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from memcal import cli, config, llm, settings, setup_config


class TestCodexCatalogProtocol(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.requests = self.home / "requests.jsonl"
        self.command = self.home / "codex-fixture"

    def executable(self, *, account="chatgpt", mode="normal"):
        code = '''#!/usr/bin/env python3
import json,sys,time
requests = %s
account = %s
mode = %s
for line in sys.stdin:
    message=json.loads(line)
    with open(requests,'a') as out: out.write(json.dumps(message)+'\\n')
    method=message['method']
    if method=='initialized': continue
    if mode=='timeout': time.sleep(10);continue
    if mode=='exit': sys.exit(1)
    if method=='initialize': result={}
    elif method=='account/read': result={'account': {'type': account} if account else None}
    elif method=='model/list':
        if mode=='malformed':
            print('not JSON',flush=True);continue
        if mode=='error':
            print(json.dumps({'id':message['id'],'error':{'code':-1,'message':'private-token-fixture'}}),flush=True);continue
        if mode=='empty':result={'data':[],'nextCursor':None}
        elif message['params'].get('cursor'):
            result={'data':[{'model':'new-oauth-model'},{'model':'gpt-6-luna'}], 'nextCursor':None}
        else:
            result={'data':[{'model':'gpt-6-luna'}, {'model':'hidden-model','hidden':True},
                            {'model':'gpt-6-astra'}, {'id':'not-an-inference-id'}], 'nextCursor':'page-2'}
        if mode=='loop': result['nextCursor']='page-2'
    else:sys.exit('unexpected method')
    payload=json.dumps({'method':'catalog/updated','params':{}})+'\\n'+json.dumps({'id':message['id'],'result':result})+'\\n'
    # Notifications and split writes exercise the actual byte-stream reader.
    halfway=len(payload)//2
    sys.stdout.write(payload[:halfway]);sys.stdout.flush()
    sys.stdout.write(payload[halfway:]);sys.stdout.flush()
''' % (repr(str(self.requests)), repr(account), repr(mode))
        self.command.write_text(code)
        self.command.chmod(0o700)
        return str(self.command)

    def sent(self):
        return [json.loads(line) for line in self.requests.read_text().splitlines()]

    def test_handshake_oauth_visible_pagination_and_process_cleanup(self):
        command = self.executable()
        children = []
        real_popen = subprocess.Popen

        def tracked(*args, **kwargs):
            proc = real_popen(*args, **kwargs)
            children.append(proc)
            return proc

        with mock.patch.object(llm.subprocess, "Popen", side_effect=tracked):
            models = llm._codex_models(command)
        self.assertEqual(models, ["gpt-6-luna", "gpt-6-astra", "new-oauth-model"])
        sent = self.sent()
        self.assertEqual([row["method"] for row in sent],
                         ["initialize", "initialized", "account/read", "model/list", "model/list"])
        self.assertEqual(sent[0]["params"]["clientInfo"]["name"], "memcal")
        self.assertFalse(sent[2]["params"]["refreshToken"])
        self.assertFalse(sent[3]["params"]["includeHidden"])
        self.assertEqual(sent[4]["params"]["cursor"], "page-2")
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(children[0].stdin.closed)
        self.assertTrue(children[0].stdout.closed)

    def test_missing_login_and_api_key_sessions_do_not_claim_oauth_models(self):
        for account in (None, "apiKey"):
            with self.subTest(account=account):
                self.requests.unlink(missing_ok=True)
                cfg = config.Config(home=self.home, codex_command=self.executable(account=account))
                models, source = llm.list_models(cfg, "codex")
                self.assertEqual(models, [])
                self.assertIn("run `codex login`", source)
                self.assertNotIn("model/list", [r["method"] for r in self.sent()])

    def test_failed_or_invalid_catalogs_are_not_replaced_with_a_static_available_list(self):
        for mode in ("malformed", "error", "empty", "loop", "exit"):
            with self.subTest(mode=mode):
                cfg = config.Config(home=self.home, codex_command=self.executable(mode=mode))
                models, source = llm.list_models(cfg, "codex")
                self.assertEqual(models, [])
                self.assertIn("Codex", source)
                self.assertNotIn("private-token-fixture", source)

    def test_timeout_is_bounded_and_terminates_the_reader(self):
        with self.assertRaisesRegex(llm.LLMError, "timed out"):
            llm._codex_models(self.executable(mode="timeout"), timeout=0.1)

    def test_missing_executable_is_actionable(self):
        cfg = config.Config(home=self.home, codex_command=str(self.home / "absent-codex"))
        models, source = llm.list_models(cfg, "codex")
        self.assertEqual(models, [])
        self.assertIn("install Codex", source)


class TestCodexSetupModelPicker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.path = self.home / ".env"
        self.cfg = config.Config(home=self.home)

    def wizard(self, answer, *, catalog=("gpt-6-luna", "new-oauth-model"), advanced=False):
        args = argparse.Namespace(home=str(self.home), provider=None, model=None,
                                  api_key=None, base_url=None, section="provider", advanced=advanced)
        output = io.StringIO()
        real_load = config.load
        with mock.patch.object(cli.config, "load", side_effect=lambda *_: real_load(self.home)), \
                mock.patch("builtins.input", side_effect=answer), \
                mock.patch.object(llm, "list_models", return_value=(list(catalog), "fixture catalog")) as lookup, \
                mock.patch.object(llm, "provider_status", return_value=(True, "ready")), \
                redirect_stdout(output):
            result = cli.cmd_setup(args)
        return result, output.getvalue(), lookup.call_count

    def test_number_selection_saves_a_live_model_unknown_to_the_price_catalog(self):
        answers = lambda prompt: "2" if prompt.startswith("Propose model") else ""
        result, text, calls = self.wizard(answers)
        self.assertEqual(result, 0)
        self.assertIn("1. Codex via OAuth", text)
        self.assertNotIn("Codex programmatic mode", text)
        self.assertIn("2. new-oauth-model", text)
        cfg = config.load(self.home)
        self.assertEqual((cfg.propose_model, cfg.sweep_model, cfg.match_model),
                         ("new-oauth-model",) * 3)
        self.assertEqual(calls, 1)

    def test_enter_keeps_saved_models_and_does_not_rewrite_the_file(self):
        original = ("MEMCAL_LLM_PROVIDER=codex\nMEMCAL_PROPOSE_MODEL=my-saved-model\n"
                    "MEMCAL_SWEEP_MODEL=my-sweep-model\nMEMCAL_MATCH_MODEL=my-match-model\n")
        self.path.write_text(original)
        result, text, _calls = self.wizard(lambda _: "")
        self.assertEqual(result, 0)
        self.assertEqual(self.path.read_text(), original)
        self.assertIn("No settings changed", text)

    def test_failed_discovery_allows_manual_entry(self):
        result, text, _calls = self.wizard(
            lambda prompt: "manual-model" if prompt.startswith("Propose model") else "", catalog=())
        self.assertEqual(result, 0)
        self.assertIn("enter a model ID manually", text)
        self.assertEqual(config.load(self.home).propose_model, "manual-model")

    def test_invalid_number_can_be_corrected_and_declining_save_preserves_config(self):
        original = "# keep my file\nMEMCAL_LLM_PROVIDER=codex\n"
        self.path.write_text(original)
        picks = iter(["999", "2"])
        result, text, _calls = self.wizard(lambda prompt: next(picks)
                                         if prompt.startswith("Propose model") else
                                         "n" if prompt.startswith("Save?") else "")
        self.assertEqual(result, 0)
        self.assertIn("Invalid value", text)
        self.assertEqual(self.path.read_text(), original)

    def test_advanced_setup_reuses_catalog_for_all_stage_models(self):
        _result, text, calls = self.wizard(lambda _: "", advanced=True)
        self.assertEqual(calls, 1)
        self.assertIn("Sweep model:", text)
        self.assertIn("Merge model:", text)

    def test_provider_label_is_shared_with_settings(self):
        self.assertIn(("codex", "Codex via OAuth"), settings.BY_KEY["MEMCAL_LLM_PROVIDER"].choices)


if __name__ == "__main__":
    unittest.main()
