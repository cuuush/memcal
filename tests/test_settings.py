"""The settings surface: what it lists, what it refuses, and what a save actually does.

Everything here is deterministic — no model, no server socket, no launchd. The HTTP
layer is a thin wrapper over these functions, so testing the functions tests the tab.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import config, llm, settings, web, web_server, web_settings  # noqa: E402
from memcal.config import Config  # noqa: E402
from memcal.dream import bundle as bundle_stage  # noqa: E402
from memcal.dream import instructions  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "store"
        self.cfg = Config(home=self.home)
        self.cfg.ensure_dirs()

    def tearDown(self):
        self.tmp.cleanup()

    def env_text(self) -> str:
        path = self.home / ".env"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def find(self, key: str) -> dict:
        for group in settings.snapshot(self.cfg)["groups"]:
            for row in group["settings"]:
                if row["key"] == key:
                    return row
        raise AssertionError(f"{key} is not in the snapshot")


class TestTheSchemaCoversEveryVariableMemcalReads(Base):
    """A knob missing from the schema is a knob the page silently denies exists.

    The settings tab is only worth having if it is the whole list, so the list is
    checked against the loader rather than maintained beside it and hoped about.
    """

    #: Selects the store rather than configuring one, so it is not a setting: changing
    #: it from inside a running store would mean writing the answer into the store it
    #: was about to stop being.
    NOT_A_SETTING = {"MEMCAL_HOME"}

    def test_every_variable_config_load_reads_has_a_setting(self):
        source = Path(config.__file__).read_text(encoding="utf-8")
        read = set(re.findall(r'"(MEMCAL_[A-Z_]+)"', source)) - self.NOT_A_SETTING
        self.assertEqual(read - set(settings.BY_KEY), set())

    def test_every_setting_is_a_variable_config_load_reads(self):
        source = Path(config.__file__).read_text(encoding="utf-8")
        read = set(re.findall(r'"(MEMCAL_[A-Z_]+)"', source))
        self.assertEqual(set(settings.BY_KEY) - read, set())

    def test_every_setting_names_a_config_field_and_a_declared_group(self):
        groups = {group.id for group in settings.GROUPS}
        self.assertGreater(len(settings.SETTINGS), len(groups))
        for setting in settings.SETTINGS:
            with self.subTest(setting=setting.key):
                self.assertTrue(hasattr(self.cfg, setting.attr))
                self.assertIn(setting.group, groups)
                self.assertTrue(setting.help.strip())

    def test_choices_agree_with_the_tables_behind_them(self):
        """An option the code cannot honour is worse than no option at all."""
        def offered(key):
            return [value for value, _label in settings.BY_KEY[key].choices]

        self.assertEqual(sorted(offered("MEMCAL_LLM_PROVIDER")),
                         sorted(llm.PROVIDER_DEFAULT_MODELS))
        self.assertEqual(sorted(offered("MEMCAL_BUNDLE_FORMAT")),
                         sorted(bundle_stage.FORMATS))
        self.assertEqual(sorted(offered("MEMCAL_PROMPT_VERSION")),
                         sorted(instructions.VERSIONS))


class TestSavingWritesTheFileAndTheRunningProcess(Base):
    def test_a_save_lands_in_the_store_env_and_in_this_config(self):
        out = settings.save(self.cfg, {"MEMCAL_DAYS_FORWARD": "12",
                                       "MEMCAL_PACK_STRATEGY": "affinity"})
        self.assertEqual(out["saved"],
                         ["MEMCAL_DAYS_FORWARD", "MEMCAL_PACK_STRATEGY"])
        self.assertEqual(self.cfg.days_forward, 12)
        self.assertEqual(self.cfg.pack_strategy, "affinity")
        self.assertIn("MEMCAL_DAYS_FORWARD=12", self.env_text())
        # And it survives the restart it claims to: a fresh load reads the same values.
        reloaded = config.load(self.home)
        self.assertEqual(reloaded.days_forward, 12)
        self.assertEqual(reloaded.pack_strategy, "affinity")

    def test_hand_written_lines_in_the_env_file_are_left_alone(self):
        (self.home / ".env").write_text("# mine\nGROUPME_ACCESS_TOKEN=t\n", encoding="utf-8")
        settings.save(self.cfg, {"MEMCAL_DAYS_BACK": "5"})
        text = self.env_text()
        self.assertIn("# mine", text)
        self.assertIn("GROUPME_ACCESS_TOKEN=t", text)
        self.assertIn("MEMCAL_DAYS_BACK=5", text)

    def test_the_snapshot_reports_which_file_a_value_came_from(self):
        self.assertEqual(self.find("MEMCAL_DAYS_BACK")["origin"], "default")
        settings.save(self.cfg, {"MEMCAL_DAYS_BACK": "5"})
        row = self.find("MEMCAL_DAYS_BACK")
        self.assertEqual(row["origin"], "store")
        self.assertEqual(row["origin_path"], str(self.home / ".env"))
        self.assertTrue(row["custom"])


class TestARefusedValueChangesNothing(Base):
    def refuse(self, changes) -> str:
        with self.assertRaises(settings.SettingsError) as caught:
            settings.save(self.cfg, changes)
        return str(caught.exception)

    def test_a_line_break_cannot_smuggle_a_second_key_into_the_file(self):
        # The whole file format is `key=value` per line, so a value that can end a line
        # can write any variable it likes — including a credential.
        self.refuse({"MEMCAL_PROPOSE_MODEL": "m\nOPENROUTER_API_KEY=stolen"})
        self.assertEqual(self.env_text(), "")
        self.assertNotIn("OPENROUTER_API_KEY", self.cfg.env)

    def test_a_variable_memcal_does_not_own_is_refused(self):
        self.assertIn("not a memcal setting", self.refuse({"PATH": "/tmp"}))
        self.assertEqual(self.env_text(), "")

    def test_a_number_outside_its_range_is_refused_with_the_bound(self):
        self.assertIn("365", self.refuse({"MEMCAL_DAYS_FORWARD": "9000"}))
        self.assertIn("whole number", self.refuse({"MEMCAL_DAYS_FORWARD": "soon"}))

    def test_an_option_the_code_cannot_honour_is_refused(self):
        self.assertIn("size", self.refuse({"MEMCAL_PACK_STRATEGY": "whatever"}))

    def test_a_stage_name_that_does_not_exist_is_refused_before_the_pass_runs(self):
        # Saved, this fails at propose — after the collect it was meant to read.
        self.assertIn("nope", self.refuse({"MEMCAL_PROPOSE_STAGES": "calendar,nope"}))

    def test_one_bad_field_does_not_let_the_good_ones_land(self):
        self.refuse({"MEMCAL_DAYS_BACK": "4", "MEMCAL_DAYS_FORWARD": "-3"})
        self.assertEqual(self.env_text(), "")
        self.assertEqual(self.cfg.days_back, Config(home=self.home).days_back)


class TestClearingAFieldRestoresTheDefault(Base):
    def test_an_empty_value_blanks_the_key_and_restores_the_built_in(self):
        settings.save(self.cfg, {"MEMCAL_DAYS_BACK": "9"})
        settings.save(self.cfg, {"MEMCAL_DAYS_BACK": ""})
        self.assertEqual(self.cfg.days_back, Config(home=self.home).days_back)
        self.assertIn("MEMCAL_DAYS_BACK=", self.env_text())
        self.assertEqual(config.load(self.home).days_back,
                         Config(home=self.home).days_back)

    def test_clearing_a_model_falls_back_to_what_the_provider_actually_uses(self):
        """`config.load` fills an unset model from the provider, not from the dataclass.

        Reported the other way, three model rows read as changed on a store that had
        changed nothing, and clearing one would have set the live process to a model
        the next restart would not have chosen.
        """
        settings.save(self.cfg, {"MEMCAL_LLM_PROVIDER": "claude-code",
                                 "MEMCAL_PROPOSE_MODEL": ""})
        native = llm.PROVIDER_DEFAULT_MODELS["claude-code"]
        self.assertEqual(self.cfg.propose_model, native)
        self.assertEqual(config.load(self.home).propose_model, native)
        self.assertFalse(self.find("MEMCAL_PROPOSE_MODEL")["custom"])


class TestAFileThatOutranksTheStoreIsSaidSo(Base):
    """`load_env` merges left to right, so the working directory beats the store.

    Saving into the store's `.env` while another file already sets the same key looks
    exactly like saving nothing, one restart later.
    """

    def test_a_working_directory_env_is_reported_and_warned_about(self):
        elsewhere = Path(self.tmp.name) / "checkout"
        elsewhere.mkdir()
        (elsewhere / ".env").write_text("MEMCAL_DAYS_BACK=99\n", encoding="utf-8")
        here = os.getcwd()
        os.chdir(elsewhere)
        try:
            self.assertEqual(self.find("MEMCAL_DAYS_BACK")["shadowed_by"],
                             str(elsewhere / ".env"))
            out = settings.save(self.cfg, {"MEMCAL_DAYS_BACK": "4"})
            self.assertTrue(any("wins the next time" in w for w in out["warnings"]))
            self.assertEqual(self.cfg.days_back, 4)          # this process, now
            self.assertEqual(config.load(self.home).days_back, 99)   # a restart, honestly
        finally:
            os.chdir(here)

    def test_a_store_scoped_setting_is_never_shadowed_by_a_checkout(self):
        elsewhere = Path(self.tmp.name) / "checkout2"
        elsewhere.mkdir()
        (elsewhere / ".env").write_text("MEMCAL_PUBLISH_CALENDAR=theirs\n", encoding="utf-8")
        here = os.getcwd()
        os.chdir(elsewhere)
        try:
            self.assertEqual(self.find("MEMCAL_PUBLISH_CALENDAR")["shadowed_by"], "")
            settings.save(self.cfg, {"MEMCAL_PUBLISH_CALENDAR": "mine"})
            self.assertEqual(config.load(self.home).publish_calendar, "mine")
        finally:
            os.chdir(here)


class TestCredentialsAreWriteOnly(Base):
    def test_the_page_reports_presence_and_never_the_value(self):
        settings.save_credential(self.cfg, "groupme_access_token", "tok-secret")
        payload = repr(web_settings.page(self.cfg))
        self.assertNotIn("tok-secret", payload)
        present = {c["name"]: c["present"] for c in settings.credentials(self.cfg)}
        self.assertTrue(present["GROUPME_ACCESS_TOKEN"])
        self.assertFalse(present["BLUEBUBBLES_PASSWORD"])

    def test_a_credential_can_be_replaced_and_cleared(self):
        settings.save_credential(self.cfg, "GROUPME_ACCESS_TOKEN", "one")
        self.assertEqual(self.cfg.secret("GROUPME_ACCESS_TOKEN"), "one")
        settings.save_credential(self.cfg, "GROUPME_ACCESS_TOKEN", "")
        self.assertIsNone(self.cfg.secret("GROUPME_ACCESS_TOKEN"))

    def test_only_a_credential_a_source_asks_for_can_be_written(self):
        with self.assertRaises(settings.SettingsError):
            settings.save_credential(self.cfg, "AWS_SECRET_ACCESS_KEY", "x")
        with self.assertRaises(settings.SettingsError):
            settings.save_credential(self.cfg, "GROUPME_ACCESS_TOKEN", "a\nPATH=/tmp")
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", self.env_text())
        self.assertNotIn("PATH=/tmp", self.env_text())


class TestThePagePayload(Base):
    def test_the_page_says_whether_the_configured_provider_is_there(self):
        page = web_settings.page(self.cfg)
        self.assertIn("ok", page["provider"])
        self.assertEqual(page["provider"]["name"], self.cfg.llm_provider)
        self.assertEqual(page["store"]["db"], str(self.cfg.db_path))
        self.assertEqual(page["env_file"], str(self.home / ".env"))

    def test_an_unknown_provider_is_a_setting_to_fix_not_a_five_hundred(self):
        self.cfg.llm_provider = "nonesuch"
        provider = web_settings.provider(self.cfg)
        self.assertFalse(provider["ok"])
        self.assertIn("nonesuch", provider["detail"])

    def test_saving_nothing_at_all_is_refused_rather_than_reported_as_done(self):
        with self.assertRaises(settings.SettingsError):
            web_settings.save(self.cfg, {})

    def test_a_save_answers_with_the_page_it_just_changed(self):
        out = web_settings.save(self.cfg, {"changes": {"MEMCAL_DAYS_BACK": "6"}})
        self.assertEqual(out["saved"], ["MEMCAL_DAYS_BACK"])
        self.assertIn("groups", out)
        self.assertEqual(
            [s["value"] for g in out["groups"] for s in g["settings"]
             if s["key"] == "MEMCAL_DAYS_BACK"], ["6"])


class TestTheTabIsWiredIntoThePage(unittest.TestCase):
    """The schema is only reachable if the shell, the router and the module agree."""

    def test_the_shell_has_the_tab_and_the_module_that_draws_it(self):
        source = web_server.frontend_source()
        for needle in ('data-view="settings"', 'id="view-settings"', "loadSettings",
                       "/api/settings", "/api/settings_probe", 'id="setbar"'):
            with self.subTest(needle=needle):
                self.assertIn(needle, source)

    def test_the_facade_re_exports_the_settings_surface(self):
        self.assertIs(web.settings_page, web_settings.page)
        self.assertIs(web.save_settings, web_settings.save)

    def test_the_cli_and_the_tab_write_env_files_the_same_way(self):
        from memcal import cli
        self.assertIs(cli._write_env, settings.write_env)


if __name__ == "__main__":
    unittest.main()
