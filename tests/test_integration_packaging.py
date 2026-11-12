"""Native harness distribution and managed refresh contracts."""
import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from memcal import cli, config, integrations


class TestUpdateFullCliRouting(unittest.TestCase):
    def test_regular_cli_delegates_update_options(self):
        from memcal import update
        with mock.patch.object(update, "main", return_value=0) as dispatch:
            args = cli.build_parser().parse_args([
                "--home", "/tmp/synthetic-store", "update", "--dev"])
            self.assertEqual(args.func(args), 0)
            dispatch.assert_called_once_with(["--home", "/tmp/synthetic-store", "update", "--dev"])

    def test_regular_cli_no_options_preserves_run_contract(self):
        from memcal import update
        with mock.patch.object(update, "run", return_value=0) as run:
            args = cli.build_parser().parse_args(["update"])
            self.assertEqual(args.func(args), 0)
            run.assert_called_once_with()


class TestVersionedNativeAssets(unittest.TestCase):
    def test_assets_include_both_hosts(self):
        self.assertTrue((integrations.asset_path("hermes") / "memcal" / "__init__.py").is_file())
        self.assertTrue((integrations.asset_path("openclaw") / "index.ts").is_file())

    def test_refresh_only_managed_and_preserves_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = config.load(directory)
            self.assertEqual(integrations.refresh_existing(cfg), [])
            plugin = integrations.prepare("openclaw", cfg.home)
            runtime = json.loads((plugin / "memcal-runtime.json").read_text())
            self.assertEqual(runtime["home"], directory)
            self.assertEqual(integrations.refresh_existing(cfg), ["openclaw"])
            edited = plugin / "index.ts"
            edited.write_text("// custom\n")
            with self.assertRaisesRegex(RuntimeError, "modified managed"):
                integrations.refresh_existing(cfg)
            self.assertEqual(edited.read_text(), "// custom\n")

    def test_refresh_refuses_symlink_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            home = root / "store"
            home.mkdir()
            (home / "integrations").symlink_to(outside)
            with self.assertRaisesRegex(RuntimeError, "symlinks"):
                integrations.prepare("openclaw", home)
            self.assertEqual(list(outside.iterdir()), [])

    def test_hermes_setup_links_packaged_provider_without_touching_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            host = root / "host"
            with mock.patch.dict("os.environ", {"HERMES_HOME": str(host)}):
                args = argparse.Namespace(home=str(root / "store"), action="setup", yes=True)
                self.assertEqual(cli.cmd_hermes(args), 0)
                target = host / "plugins" / "memcal"
                self.assertTrue(target.is_symlink())
                self.assertTrue((target / "__init__.py").is_file())
                self.assertEqual(cli.cmd_hermes(args), 0)
                self.assertFalse((host / "config.yaml").exists())

    def test_installed_openclaw_stages_assets_and_registers_own_interpreter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = argparse.Namespace(home=str(root / "store"), action="refresh", yes=True)
            commands = []
            with mock.patch.object(config, "PROJECT_ROOT", root / "site-packages"), \
                    mock.patch.object(cli, "_openclaw_config_path", return_value=root / "absent"), \
                    mock.patch.object(cli, "_run_openclaw", side_effect=lambda c: (commands.append(c) or True, "ok")):
                self.assertEqual(cli.cmd_openclaw(args), 0)
            plugin = Path(commands[0][-1])
            self.assertTrue((plugin / "index.ts").is_file())
            self.assertTrue((plugin / "memcal-managed.json").is_file())


if __name__ == "__main__": unittest.main()
