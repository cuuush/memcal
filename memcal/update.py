"""Refresh the installation that supplies the running Memcal command."""

from __future__ import annotations

import argparse
from importlib import metadata
import os
import json
import re
import tempfile
import urllib.request
import urllib.error
import shutil
import subprocess
import sys
from pathlib import Path


class UpdateError(RuntimeError):
    pass


GITHUB_SOURCE = "git+https://github.com/cuuush/memcal.git"


def _chat_extras() -> list[str]:
    extras = []
    for extra, distribution in (("slack", "slack_sdk"), ("telegram", "telethon")):
        try:
            metadata.version(distribution)
        except metadata.PackageNotFoundError:
            continue
        extras.append(extra)
    return extras


def _run(command: list[str], *, cwd: Path | None = None,
         env: dict[str, str] | None = None) -> str:
    try:
        result = subprocess.run(command, cwd=cwd, env=env, text=True,
                                capture_output=True, check=False)
    except OSError as exc:
        raise UpdateError(f"cannot run {command[0]}: {exc}") from exc
    if result.returncode:
        raise UpdateError((result.stderr or result.stdout).strip()
                          or f"{command[0]} exited with {result.returncode}")
    return result.stdout.strip()


def _verify(root: Path | None = None) -> None:
    env = dict(os.environ)
    if root:
        env["PYTHONPATH"] = str(root)
    else:
        env.pop("PYTHONPATH", None)
    _run([sys.executable, "-P", "-c", "import memcal.cli; print('CLI ready')"],
         cwd=root or Path.home(), env=env)


def _source_update(root: Path) -> None:
    def git(*args: str) -> str:
        return _run(["git", "-C", str(root), *args])

    if (root / ".git").is_file():
        raise UpdateError("this is a development worktree; run memcal update from "
                          "the installed checkout")
    # Local outputs are not source edits. Let Git preserve unrelated untracked
    # files and reject path collisions during the fast-forward below.
    if git("status", "--porcelain", "--untracked-files=no"):
        raise UpdateError(f"{root} has tracked local changes; commit or stash them, then retry")
    try:
        branch = git("symbolic-ref", "--quiet", "--short", "HEAD")
    except UpdateError as exc:
        raise UpdateError("checkout is on a detached commit; switch to its tracking "
                          "branch before updating") from exc
    tracking = git("for-each-ref", "--format=%(upstream:remotename) %(upstream:remoteref)",
                   f"refs/heads/{branch}").split()
    reconnect = not tracking
    remotes = git("remote").splitlines() if reconnect else []
    if reconnect and ("origin" in remotes or len(remotes) == 1):
        remote = "origin" if "origin" in remotes else remotes[0]
    elif len(tracking) == 2 and tracking[0] != ".":
        remote = tracking[0]
    else:
        raise UpdateError(f"{branch} has no remote tracking branch; configure its "
                          "upstream before updating")
    print(f"Fetching {remote}…", flush=True)
    git("fetch", "--", remote)
    if reconnect:
        git("remote", "set-head", remote, "--auto")
        upstream = git("symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD")
        if upstream != f"{remote}/{branch}":
            raise UpdateError(f"{branch} has no remote tracking branch and is not the "
                              "remote default; configure its upstream before updating")
    else:
        upstream = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    ahead, behind = map(int, git("rev-list", "--left-right", "--count",
                                f"HEAD...{upstream}").split())
    if ahead:
        raise UpdateError(f"{branch} has {ahead} local commit(s) not on {upstream}; "
                          "push or reconcile them before updating")
    if reconnect:
        git("branch", f"--set-upstream-to={upstream}", branch)
        print(f"Tracking {upstream}", flush=True)
    old = git("rev-parse", "--short", "HEAD")
    if behind:
        git("merge", "--ff-only", "--no-overwrite-ignore", upstream)
    new = git("rev-parse", "--short", "HEAD")
    print(f"Source: {old} → {new}" if behind else f"Source already current: {new}",
          flush=True)
    launcher = os.environ.get("MEMCAL_LAUNCHER") or shutil.which("memcal")
    bin_dir = Path(launcher).absolute().parent if launcher else Path.home() / ".local/bin"
    print("Refreshing dependencies and launcher…", flush=True)
    try:
        _run(["sh", str(root / "install.sh"), "--no-init", "--upgrade", "--python", sys.executable,
              "--bin", str(bin_dir)], cwd=root)
        _verify(root)
    except UpdateError as exc:
        raise UpdateError(f"source is at {new}, but installation refresh failed: {exc}. "
                          "Fix the reported problem and run memcal update again") from exc


def _stable_version() -> str:
    try:
        request = urllib.request.Request("https://pypi.org/pypi/memcal/json",
                                         headers={"User-Agent": "memcal-update"})
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.load(response)
        versions = [v for v, files in data["releases"].items()
                    if re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", v)
                    and any(not f.get("yanked", False) for f in files)]
        return max(versions, key=lambda v: tuple(map(int, v.split("."))))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise UpdateError(f"cannot resolve a published stable PyPI release: {exc}") from exc


def _requirement(version: str) -> str:
    extras = _chat_extras()
    return "memcal" + (f"[{','.join(extras)}]" if extras else "") + "==" + version


def _stage(version: str) -> None:
    """Verify the released package before replacing the active installation."""
    with tempfile.TemporaryDirectory(prefix="memcal-update-") as directory:
        _run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
              "--target", directory, _requirement(version)])
        env = {**os.environ, "PYTHONPATH": directory}
        _run([sys.executable, "-P", "-c",
              "import memcal.cli; from memcal import __version__; "
              f"assert __version__ == {version!r}"], cwd=Path(directory), env=env)


def _stable_source_update(root: Path, version: str) -> None:
    def git(*args):
        return _run(["git", "-C", str(root), *args])
    if (root / ".git").is_file():
        raise UpdateError("this is a development worktree; update the installed checkout instead")
    if git("status", "--porcelain", "--untracked-files=no"):
        raise UpdateError("checkout has tracked local changes; preserve them before updating")
    old = git("rev-parse", "HEAD")
    remote = "origin"
    if remote not in git("remote").splitlines():
        raise UpdateError("stable checkout updates require an origin remote")
    tag = "v" + version
    # Fetch only the published release tag, never the development branch head.
    git("fetch", "--", remote, f"refs/tags/{tag}:refs/tags/{tag}")
    target = git("rev-parse", f"{tag}^{{commit}}")
    if old != target:
        try:
            git("merge-base", "--is-ancestor", old, target)
        except UpdateError as exc:
            raise UpdateError("checkout contains commits outside the stable release; "
                              "keep this development checkout or explicitly use update --dev") from exc
    print(f"Stable release: {version} ({target[:12]})", flush=True)
    launcher = os.environ.get("MEMCAL_LAUNCHER") or shutil.which("memcal")
    bin_dir = Path(launcher).absolute().parent if launcher else Path.home() / ".local/bin"
    if old != target:
        git("checkout", "--detach", "--no-overwrite-ignore", target)
    try:
        _run(["sh", str(root / "install.sh"), "--no-init", "--python", sys.executable,
              "--bin", str(bin_dir)], cwd=root)
        _verify(root)
    except UpdateError as exc:
        if old != target:
            git("checkout", "--detach", "--no-overwrite-ignore", old)
        raise UpdateError(f"installation failed; source restored to {old[:12]}. "
                          f"Dependencies may need repair; use the saved recovery record. {exc}") from exc


def _package_update(version: str | None = None, *, dev: bool = False) -> None:
    version = version or _stable_version()
    requirement = _requirement(version)
    if dev:
        requirement = requirement.split("==")[0] + " @ " + GITHUB_SOURCE
    print(f"Installing {'development source' if dev else 'stable release ' + version}…", flush=True)
    # Do not bypass an externally managed Python or eagerly upgrade unrelated deps.
    _run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
          "--upgrade", "--upgrade-strategy", "only-if-needed", requirement])
    _verify()


def refresh() -> None:
    """Run in the newly installed code, after dependencies have been repaired."""
    from . import brief, config, db, schedule, integrations

    cfg = config.load()
    if cfg.db_path.exists():
        print(f"Migrating store: {cfg.home}", flush=True)
        conn = db.open_db(cfg.db_path)
        try:
            brief.write(conn, cfg)
        finally:
            conn.close()
        print("Store and saved brief refreshed.", flush=True)
    try:
        for harness in integrations.refresh_existing(cfg):
            print(f"Refreshed managed {harness} integration; restart that host.", flush=True)
    except (RuntimeError, OSError, ValueError) as exc:
        raise UpdateError(f"integration refresh failed (local changes preserved): {exc}") from exc
    if schedule._is_macos() and schedule.plist_path().exists():
        hour, minute = schedule.scheduled_time(cfg)
        print("Refreshing installed scheduler and app wrapper…", flush=True)
        for line in schedule.install(cfg, hour=hour, minute=minute):
            print(line, flush=True)
            if line.startswith("FAILED"):
                raise UpdateError(line)
        if not schedule.status(cfg)["loaded"]:
            raise UpdateError("scheduler was refreshed but launchd did not load it")


def _refresh_installed(root: Path | None, home: str | None, *, maintenance_home: Path | None = None) -> None:
    env = dict(os.environ)
    if home is not None:
        env["MEMCAL_HOME"] = str(Path(home).expanduser().absolute())
    if root:
        env["PYTHONPATH"] = str(root)
    else:
        env.pop("PYTHONPATH", None)
    if maintenance_home is not None:
        from .update_safety import BYPASS
        env[BYPASS] = str(maintenance_home.resolve())
    print("Refreshing installed components…", flush=True)
    output = _run([sys.executable, "-P", "-c",
                   "from memcal.update import refresh; refresh()"],
                  cwd=root or Path.home(), env=env)
    if output:
        print(output, flush=True)


def run(home: str | None = None, *, dev: bool = False) -> int:
    from . import __version__
    from . import update_safety
    from .lock import Busy
    root = Path(__file__).resolve().parent.parent
    store = Path(home or os.environ.get("MEMCAL_HOME", "~/.memcal")).expanduser().absolute()
    backup = None
    try:
        version = _stable_version() if not dev else __version__
        print(f"Installed: {__version__}; target: {'development branch' if dev else version}")
        if not dev:
            _stage(version)
        with update_safety.maintenance(store):
            installation = {'python': sys.executable, 'root': str(root), 'development': dev}
            if (root / '.git').exists():
                installation['commit'] = _run(['git', '-C', str(root), 'rev-parse', 'HEAD'])
            installation['packages'] = _run([sys.executable, '-m', 'pip', 'freeze'])
            backup = update_safety.snapshot(store, version=__version__, installation=installation)
            print(f"Private recovery snapshot: {backup}", flush=True)
            if (root / ".git").exists():
                if dev:
                    _source_update(root)
                else:
                    _stable_source_update(root, version)
            else:
                if (root / "pyproject.toml").is_file() and (root / "install.sh").is_file():
                    raise UpdateError("source copy lacks Git history; use a Git clone or installed package")
                _package_update(version, dev=dev)
                root = None
            _refresh_installed(root, home, maintenance_home=store)
    except (UpdateError, update_safety.MaintenanceBusy, Busy, OSError, ValueError) as exc:
        print(f"Update failed: {exc}", file=sys.stderr)
        if backup:
            print(f"Store snapshot preserved at {backup}. See docs/updates.md for recovery; "
                  "do not run older code against a migrated store.", file=sys.stderr)
        return 1
    print("Memcal updated. Restart UI and agent sessions to load new code and integrations.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memcal")
    parser.add_argument("--home")
    sub = parser.add_subparsers(dest="cmd", required=True)
    command = sub.add_parser("update", help="install a published stable release with a recovery snapshot")
    command.add_argument("--dev", action="store_true", help="explicitly follow development source instead")
    command.add_argument("--restore-backup", type=Path, help="snapshot directory to recover")
    command.add_argument("--restore-to", type=Path, help="new, nonexistent recovery destination")
    args = parser.parse_args(argv)
    if args.restore_backup or args.restore_to:
        if not args.restore_backup or not args.restore_to or args.dev:
            parser.error("recovery requires --restore-backup and --restore-to, without --dev")
        from .update_safety import restore
        try:
            record = restore(args.restore_backup, args.restore_to)
        except (OSError, ValueError) as exc:
            print(f"Recovery failed: {exc}", file=sys.stderr)
            return 1
        print(f"Recovered data to {args.restore_to}. Install MemCal {record['version']} "
              "in an isolated environment before using that store. Original store unchanged.")
        return 0
    kwargs = {}
    if args.home is not None:
        kwargs['home'] = args.home
    if args.dev:
        kwargs['dev'] = True
    return run(**kwargs)
