"""The setup wizard edits sources and settings before one confirmed save."""

from __future__ import annotations

import argparse
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from memcal import cli, config, settings


class TestSetupConfiguresMessagesAndOtherSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.path = self.home / ".env"

    def wizard(self, answers=None, *, section="all", password="fixture-password"):
        answers = answers or {}
        args = argparse.Namespace(home=str(self.home), section=section, provider=None,
                                  model=None, api_key=None, base_url=None)
        prompts = []

        def answer(prompt):
            prompts.append(prompt)
            for prefix, value in answers.items():
                if prompt.startswith(prefix):
                    return value(prompt) if callable(value) else value
            return ""

        output = io.StringIO()
        with mock.patch("builtins.input", side_effect=answer), \
                mock.patch("getpass.getpass", **({"side_effect": password} if callable(password)
                                                else {"return_value": password})), \
                mock.patch("memcal.llm.provider_status", return_value=(True, "ready")), \
                redirect_stdout(output):
            result = cli.cmd_setup(args)
        return result, output.getvalue(), prompts

    def test_plain_setup_includes_imessage_and_other_configuration(self):
        self.path.write_text("# personal\nSOME_SOURCE_TOKEN=keep-me\n")
        result, output, prompts = self.wizard({
            "Brief token cap [": "2500", "Disabled sources [": "slack,telegram",
            "Bundles per request [": "8"})
        cfg = config.load(self.home)
        self.assertEqual(result, 0)
        self.assertTrue(any(p.startswith("iMessage transport [") for p in prompts))
        self.assertEqual(cfg.brief_token_cap, 2500)
        self.assertEqual(cfg.disabled_sources, "slack,telegram")
        self.assertEqual(cfg.pack_bundles, 8)
        self.assertIn("SOME_SOURCE_TOKEN=keep-me", self.path.read_text())
        self.assertIn("Brief token cap: 1500 → 2500", output)
        self.assertEqual(sum(p.startswith("Save?") for p in prompts), 1)

    def test_bluebubbles_selection_replaces_legacy_url_and_password(self):
        self.path.write_text("bluebubbles=old-fixture-password\n"
                            "BLUEBUBBLESURL=http://old.example.test:1234\n")
        result, output, _ = self.wizard({
            "iMessage transport [": "2",
            "BlueBubbles server URL [": "https://messages.example.test",
            "Fall back to the local database [": "off",
            "Where BlueBubbles runs [": "remote"}, section="imessage")
        cfg = config.load(self.home)
        from memcal.sources.bluebubbles import BlueBubbles
        client = BlueBubbles(cfg)
        self.assertEqual(result, 0)
        self.assertEqual(cfg.imessage_backend, "bluebubbles")
        self.assertEqual(client.url, "https://messages.example.test")
        self.assertEqual(client.password, "fixture-password")
        self.assertFalse(cfg.imessage_fallback)
        self.assertEqual(cfg.bluebubbles_location, "remote")
        self.assertNotIn("fixture-password", output)
        self.assertNotIn("old-fixture-password", output)

    def test_switching_to_local_database_keeps_saved_bluebubbles_connection(self):
        self.path.write_text("MEMCAL_IMESSAGE_BACKEND=bluebubbles\n"
                            "BLUEBUBBLES_PASSWORD=fixture-password\n"
                            "BLUEBUBBLES_URL=https://messages.example.test\n")
        result, _, prompts = self.wizard({"iMessage transport [": "chatdb"}, section="imessage")
        cfg = config.load(self.home)
        self.assertEqual(result, 0)
        self.assertEqual(cfg.imessage_backend, "chatdb")
        self.assertEqual(cfg.secret("BLUEBUBBLES_PASSWORD"), "fixture-password")
        self.assertFalse(any(p.startswith("BlueBubbles server URL") for p in prompts))

    def test_enter_keeps_saved_source_and_other_settings(self):
        original = ("MEMCAL_IMESSAGE_BACKEND=bluebubbles\n"
                    "bluebubbles=fixture-password\n"
                    "MEMCAL_IMESSAGE_FALLBACK=0\n")
        self.path.write_text(original)
        result, output, _ = self.wizard(section="imessage", password="")
        self.assertEqual(result, 0)
        self.assertEqual(self.path.read_text(), original)
        self.assertIn("None", output)

    def test_declining_all_changes_preserves_the_file_byte_for_byte(self):
        original = "# personal\nMEMCAL_BRIEF_TOKEN_CAP=1800\n"
        self.path.write_text(original)
        result, _, _ = self.wizard({"Brief token cap [": "2500", "Save?": "n"})
        self.assertEqual(result, 0)
        self.assertEqual(self.path.read_text(), original)

    def test_invalid_value_can_be_corrected_without_partial_saves(self):
        attempts = iter(["-10", "2000"])
        result, output, _ = self.wizard({"Brief token cap [": lambda _: next(attempts)}, section="brief")
        self.assertEqual(result, 0)
        self.assertIn("Invalid value:", output)
        self.assertEqual(config.load(self.home).brief_token_cap, 2000)

    def test_eof_after_a_change_does_not_save(self):
        original = "MEMCAL_BRIEF_TOKEN_CAP=1800\n"
        self.path.write_text(original)
        args = argparse.Namespace(home=str(self.home), section="brief", provider=None,
                                  model=None, api_key=None, base_url=None)
        with mock.patch("builtins.input", side_effect=["4", EOFError()]):
            self.assertEqual(cli.cmd_setup(args), 0)
        self.assertEqual(self.path.read_text(), original)

    def test_reset_can_disable_calendar_publishing(self):
        self.path.write_text("MEMCAL_PUBLISH_CALENDAR=Work\n")
        result, _, _ = self.wizard({"Publish to calendar [": "-"}, section="publish")
        self.assertEqual(result, 0)
        self.assertEqual(config.load(self.home).publish_calendar, "")

    def test_source_credentials_can_replace_a_legacy_token(self):
        self.path.write_text("slack=old-fixture-token\n")
        result, output, _ = self.wizard(section="credentials", password=lambda prompt:
                                       "new-fixture-token" if "SLACK_TOKEN" in prompt else "")
        self.assertEqual(result, 0)
        self.assertEqual(config.load(self.home).secret("SLACK_TOKEN", "slack"), "new-fixture-token")
        self.assertNotIn("new-fixture-token", output)
        self.assertNotIn("old-fixture-token", output)

    def test_validation_prepares_values_without_writing(self):
        cfg = config.load(self.home)
        plan = settings.prepare(cfg, {"MEMCAL_BRIEF_TOKEN_CAP": "2500"})
        self.assertEqual(plan["MEMCAL_BRIEF_TOKEN_CAP"], ("2500", 2500))
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
