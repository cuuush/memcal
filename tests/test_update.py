"""Installation updates preserve local work and repair the running command."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from memcal import entrypoint, update


class TestCheckoutUpdates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.remote = self.base / "remote.git"
        self.author = self.base / "author"
        self.checkout = self.base / "installed"
        self.git(self.base, "init", "--bare", str(self.remote))
        self.git(self.base, "clone", str(self.remote), str(self.author))
        self.git(self.author, "checkout", "-b", "main")
        self.git(self.author, "config", "user.name", "Fixture")
        self.git(self.author, "config", "user.email", "fixture@example.test")
        (self.author / "memcal").mkdir()
        (self.author / "memcal/__init__.py").write_text("")
        (self.author / "memcal/cli.py").write_text("REVISION = 1\n")
        (self.author / ".gitignore").write_text("install-args.txt\n__pycache__/\n")
        (self.author / "install.sh").write_text(
            '#!/bin/sh\nprintf "%s\\n" "$@" > install-args.txt\n')
        self.git(self.author, "add", ".")
        self.git(self.author, "commit", "-m", "initial")
        self.git(self.author, "push", "-u", "origin", "main")
        self.git(self.remote, "symbolic-ref", "HEAD", "refs/heads/main")
        self.git(self.base, "clone", "--branch", "main", str(self.remote), str(self.checkout))
        self.git(self.checkout, "config", "user.name", "Fixture")
        self.git(self.checkout, "config", "user.email", "fixture@example.test")

    def git(self, root, *args):
        result = subprocess.run(["git", "-C", str(root), *args], text=True,
                                capture_output=True, check=True)
        return result.stdout.strip()

    def publish(self):
        (self.author / "memcal/cli.py").write_text("REVISION = 2\n")
        self.git(self.author, "commit", "-am", "next version")
        self.git(self.author, "push")

    def refresh(self):
        with mock.patch.dict(os.environ, {"MEMCAL_LAUNCHER": str(self.base / "bin/memcal")}):
            update._source_update(self.checkout)

    def test_fast_forward_refreshes_the_same_launcher_and_is_repeatable(self):
        self.publish()
        self.refresh()
        self.assertEqual((self.checkout / "memcal/cli.py").read_text(), "REVISION = 2\n")
        args = (self.checkout / "install-args.txt").read_text().splitlines()
        self.assertEqual(args, ["--no-init", "--upgrade", "--python", sys.executable,
                                "--bin", str(self.base / "bin")])
        self.refresh()
        self.assertEqual(self.git(self.checkout, "status", "--porcelain"), "")

    def test_dirty_checkout_is_preserved(self):
        self.publish()
        local = self.checkout / "memcal/cli.py"
        local.write_text("local work\n")
        with self.assertRaisesRegex(update.UpdateError, "local changes"):
            self.refresh()
        self.assertEqual(local.read_text(), "local work\n")
        self.assertFalse((self.checkout / "install-args.txt").exists())

    def test_component_refresh_executes_the_code_just_pulled(self):
        (self.author / "memcal/update.py").write_text(
            "import os\nfrom pathlib import Path\n"
            "def refresh():\n"
            "    Path(os.environ['MEMCAL_HOME']).write_text('new refresh code')\n")
        self.git(self.author, "add", "memcal/update.py")
        self.git(self.author, "commit", "-m", "new component refresh")
        self.git(self.author, "push")
        self.refresh()
        marker = self.base / "refreshed"
        update._refresh_installed(self.checkout, str(marker))
        self.assertEqual(marker.read_text(), "new refresh code")

    def test_divergent_local_commits_are_preserved(self):
        self.publish()
        (self.checkout / "local.txt").write_text("local work\n")
        self.git(self.checkout, "add", "local.txt")
        self.git(self.checkout, "commit", "-m", "local work")
        old = self.git(self.checkout, "rev-parse", "HEAD")
        with self.assertRaisesRegex(update.UpdateError, "local commit"):
            self.refresh()
        self.assertEqual(self.git(self.checkout, "rev-parse", "HEAD"), old)

    def test_development_worktree_is_not_repointed_by_the_installer(self):
        worktree = self.base / "development"
        self.git(self.checkout, "worktree", "add", "-b", "topic", str(worktree))
        with self.assertRaisesRegex(update.UpdateError, "development worktree"):
            update._source_update(worktree)
        self.assertFalse((worktree / "install-args.txt").exists())

    def test_branch_without_upstream_is_not_switched_implicitly(self):
        self.git(self.checkout, "checkout", "-b", "local-topic")
        with self.assertRaisesRegex(update.UpdateError, "no remote tracking branch"):
            self.refresh()
        self.assertEqual(self.git(self.checkout, "branch", "--show-current"), "local-topic")

    def test_default_branch_without_upstream_reconnects_and_updates(self):
        self.git(self.checkout, "branch", "--unset-upstream")
        self.publish()
        self.refresh()
        self.assertEqual(self.git(self.checkout, "rev-parse", "--abbrev-ref",
                                 "--symbolic-full-name", "@{upstream}"), "origin/main")
        self.assertEqual((self.checkout / "memcal/cli.py").read_text(), "REVISION = 2\n")

    def test_detached_checkout_gets_an_actionable_error(self):
        self.git(self.checkout, "checkout", "--detach")
        with self.assertRaisesRegex(update.UpdateError, "detached commit"):
            self.refresh()

    def test_refresh_failure_reports_the_updated_revision_and_can_be_retried(self):
        self.publish()
        real_run = update._run

        def fail_installer(command, **kwargs):
            if command[0] == "sh":
                raise update.UpdateError("dependency unavailable")
            return real_run(command, **kwargs)

        with mock.patch.object(update, "_run", side_effect=fail_installer):
            with self.assertRaisesRegex(update.UpdateError, "installation refresh failed"):
                self.refresh()
        self.assertEqual((self.checkout / "memcal/cli.py").read_text(), "REVISION = 2\n")
        self.refresh()


class TestPackageUpdates(unittest.TestCase):
    def test_homebrew_retries_in_user_site_only_for_pep668(self):
        with mock.patch.object(update, "_run", side_effect=[
                update.UpdateError("externally-managed-environment"), ""]) as run, \
                mock.patch.object(update, "_verify") as verify, \
                mock.patch.object(sys, "prefix", "system"), \
                mock.patch.object(sys, "base_prefix", "system"):
            update._package_update()
        self.assertEqual(run.call_args_list[0].args[0][:3], [sys.executable, "-m", "pip"])
        self.assertEqual(run.call_args_list[1].args[0][-2:], ["--user", "--break-system-packages"])
        verify.assert_called_once_with()

    def test_network_failure_does_not_retry_with_different_install_scope(self):
        with mock.patch.object(update, "_run", side_effect=update.UpdateError("network down")) as run:
            with self.assertRaisesRegex(update.UpdateError, "network down"):
                update._package_update()
        self.assertEqual(run.call_count, 1)

    def test_regular_cli_help_and_dispatch_include_update(self):
        from memcal import cli
        self.assertIn("update", cli.build_parser().memcal_commands)
        self.assertIn("update", cli._NO_APP_COMMANDS)
        with mock.patch("memcal.update.run", return_value=0) as run:
            self.assertEqual(cli.main(["update"]), 0)
        run.assert_called_once_with()

    def test_dispatch_works_without_importing_the_application(self):
        with mock.patch("memcal.update.run", return_value=0) as run:
            self.assertEqual(entrypoint.main(["--home", "/unused", "update"]), 0)
        run.assert_called_once_with(home="/unused")

    def test_missing_application_dependencies_do_not_prevent_update_dispatch(self):
        script = """\
import sys
from unittest import mock
from memcal import entrypoint, update
class BlockApplication:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'memcal.cli', 'typedstream'}:
            raise ImportError('fixture missing runtime dependency')
sys.meta_path.insert(0, BlockApplication())
with mock.patch.object(update, 'run', return_value=0):
    raise SystemExit(entrypoint.main(['update']))
"""
        with tempfile.TemporaryDirectory() as directory:
            source = Path(__file__).resolve().parents[1]
            result = subprocess.run([sys.executable, "-c", script], cwd=directory,
                                    env={**os.environ, "PYTHONPATH": str(source)},
                                    text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)


class TestInstallerRefresh(unittest.TestCase):
    def test_pinned_python_creates_a_working_launcher_without_initializing_a_store(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installer = root / "install.sh"
            source = Path(__file__).resolve().parents[1]
            shutil.copyfile(source / "install.sh", installer)
            (root / "pyproject.toml").write_text('[project]\ndependencies = []\n')
            (root / "memcal").mkdir()
            (root / "memcal/__init__.py").write_text("")
            (root / "memcal/__main__.py").write_text("print('fixture command ready')\n")
            store = root / "store"
            env = {**os.environ, "MEMCAL_HOME": str(store)}
            result = subprocess.run(["sh", str(installer), "--no-init", "--python",
                                     sys.executable, "--bin", str(root / "bin")],
                                    text=True, capture_output=True, env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            launcher = root / "bin/memcal"
            ready = subprocess.run([str(launcher), "update", "--help"],
                                   text=True, capture_output=True, env=env)
            self.assertEqual(ready.returncode, 0, ready.stderr)
            self.assertEqual(ready.stdout.strip(), "fixture command ready")
            self.assertFalse(store.exists())
            self.assertIn(f'MEMCAL_LAUNCHER="{launcher}"', launcher.read_text())

    def test_dependency_failure_does_not_install_a_broken_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = Path(__file__).resolve().parents[1]
            shutil.copyfile(source / "install.sh", root / "install.sh")
            (root / "memcal").mkdir()
            interpreter = root / "python"
            interpreter.write_text('#!/bin/sh\ncase "$1" in\n'
                                   '  -) echo fixture-dependency ;;\n'
                                   '  -m) echo "pip failed" >&2; exit 1 ;;\n'
                                   '  *) exit 0 ;;\nesac\n')
            interpreter.chmod(0o755)
            result = subprocess.run(["sh", str(root / "install.sh"), "--no-init",
                                     "--python", str(interpreter), "--bin", str(root / "bin")],
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 1)
            self.assertFalse((root / "bin/memcal").exists())


if __name__ == "__main__":
    unittest.main()
