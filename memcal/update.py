"""Refresh the installation that supplies the running Memcal command."""

from __future__ import annotations

import argparse
from importlib import metadata
import os
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
    if git("status", "--porcelain", "--untracked-files=normal"):
        raise UpdateError(f"{root} has local changes; commit or stash them, then retry")
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
        git("merge", "--ff-only", upstream)
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


def _package_update() -> None:
    print(f"Updating from GitHub with {sys.executable}…", flush=True)
    extras = _chat_extras()
    requirement = "memcal" + (f"[{','.join(extras)}]" if extras else "")
    command = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
               "--upgrade", "--force-reinstall", "--upgrade-strategy", "eager",
               f"{requirement} @ {GITHUB_SOURCE}"]
    try:
        _run(command)
    except UpdateError as exc:
        if "externally-managed-environment" not in str(exc) or sys.prefix != sys.base_prefix:
            raise
        _run([*command, "--user", "--break-system-packages"])
    _verify()


def refresh() -> None:
    """Run in the newly installed code, after dependencies have been repaired."""
    from . import brief, config, db, schedule

    cfg = config.load()
    if cfg.db_path.exists():
        print(f"Migrating store: {cfg.home}", flush=True)
        conn = db.open_db(cfg.db_path)
        try:
            brief.write(conn, cfg)
        finally:
            conn.close()
        print("Store and saved brief refreshed.", flush=True)
    if schedule._is_macos() and schedule.plist_path().exists():
        hour, minute = schedule.scheduled_time(cfg)
        print("Refreshing installed scheduler and app wrapper…", flush=True)
        for line in schedule.install(cfg, hour=hour, minute=minute):
            print(line, flush=True)
            if line.startswith("FAILED"):
                raise UpdateError(line)
        if not schedule.status(cfg)["loaded"]:
            raise UpdateError("scheduler was refreshed but launchd did not load it")


def _refresh_installed(root: Path | None, home: str | None) -> None:
    env = dict(os.environ)
    if home is not None:
        env["MEMCAL_HOME"] = str(Path(home).expanduser().absolute())
    if root:
        env["PYTHONPATH"] = str(root)
    else:
        env.pop("PYTHONPATH", None)
    print("Refreshing installed components…", flush=True)
    output = _run([sys.executable, "-P", "-c",
                   "from memcal.update import refresh; refresh()"],
                  cwd=root or Path.home(), env=env)
    if output:
        print(output, flush=True)


def run(home: str | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    try:
        if (root / ".git").exists():
            print(f"Updating checkout: {root}", flush=True)
            _source_update(root)
        else:
            if (root / "pyproject.toml").is_file() and (root / "install.sh").is_file():
                raise UpdateError(f"{root} is a source copy without Git history; "
                                  "use a Git clone to receive checkout updates")
            _package_update()
            root = None
        _refresh_installed(root, home)
    except UpdateError as exc:
        print(f"Update failed: {exc}", file=sys.stderr)
        return 1
    print("Memcal updated. New commands use the updated code.")
    print("Restart running Memcal UI or agent sessions to load it there.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memcal")
    parser.add_argument("--home")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("update", help="update this installation and refresh its launcher")
    args = parser.parse_args(argv)
    return run(home=args.home) if args.home is not None else run()
