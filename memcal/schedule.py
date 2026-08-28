"""The nightly pass, as a launchd agent."""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from .config import Config, PROJECT_ROOT

LABEL = "com.memcal.nightly"
CATCHUP_LABEL = "com.memcal.catchup"
ICAL_PERMISSION_LABEL = "com.memcal.ical-permission"
DEFAULT_HOUR = 3
DEFAULT_MINUTE = 0

#: Daytime retries cover sources unavailable during the nightly pass. They fetch only
#: stale, reachable streams; on a healthy day this is one query and an early exit.
CATCHUP_HOURS = (12, 19)


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


#: macOS labels launch items by executable. Run the named scripts directly so the two
#: memcal agents are distinguishable in Login Items; extensionless names are deliberate.
SCRIPT_NAMES = {"nightly": "memcal-nightly", "catchup": "memcal-catchup"}

#: What the same files used to be called, so `install` can clear them rather than leave
#: a second copy of the answer lying in the home directory.
LEGACY_SCRIPT_NAMES = {"nightly": "nightly.sh", "catchup": "catchup.sh"}


def script_path(cfg: Config) -> Path:
    return cfg.home / SCRIPT_NAMES["nightly"]


def catchup_script_path(cfg: Config) -> Path:
    return cfg.home / SCRIPT_NAMES["catchup"]


def catchup_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{CATCHUP_LABEL}.plist"


def log_path(cfg: Config) -> Path:
    return cfg.home / "nightly.log"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def pinned_python(cfg: Config) -> str:
    """The interpreter the nightly script will actually execute."""
    script = script_path(cfg)
    if script.exists():
        for line in script.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("PY="):
                candidate = line[3:].strip().strip('"')
                if Path(candidate).is_file():
                    return candidate
                break
    return sys.executable


def calendar_permission_probe(cfg: Config, *, timeout: int = 45) -> tuple[bool, str]:
    """Request Calendar access from a temporary launchd agent.

    Running `osascript` from a terminal can authorize the terminal/Codex responsible
    process while the 03:00 job remains unapproved. This probe uses the same user
    launchd domain, source checkout, home, and pinned Python as the nightly agent.
    """
    cfg.ensure_dirs()
    python = pinned_python(cfg)
    plist = cfg.home / "ical-permission.plist"
    result = cfg.home / "ical-permission-result.json"
    try:
        result.unlink(missing_ok=True)
        with plist.open("wb") as fh:
            plistlib.dump({
                "Label": ICAL_PERMISSION_LABEL,
                "ProgramArguments": [
                    python, "-m", "memcal", "ical", "probe",
                    "--context", "nightly", "--result", str(result),
                ],
                "RunAtLoad": True,
                "ProcessType": "Interactive",
                "EnvironmentVariables": {
                    "MEMCAL_HOME": str(cfg.home),
                    "PYTHONPATH": str(PROJECT_ROOT),
                },
                "WorkingDirectory": str(PROJECT_ROOT),
            }, fh)
        _launchctl("bootout", f"{_domain()}/{ICAL_PERMISSION_LABEL}")
        code, message = _launchctl("bootstrap", _domain(), str(plist))
        if code:
            return False, f"could not start launchd Calendar probe: {message or code}"
        deadline = time.monotonic() + max(1, timeout)
        while time.monotonic() < deadline:
            if result.exists():
                try:
                    payload = json.loads(result.read_text(encoding="utf-8"))
                    ok = bool(payload.get("ok"))
                    detail = str(payload.get("message") or "no detail")
                    return ok, (
                        f"nightly launchd requester ({python}): {detail}"
                    )
                except (OSError, ValueError):
                    pass
            time.sleep(0.25)
        return False, (
            f"nightly launchd Calendar check timed out ({python}); "
            "a permission prompt may still be waiting"
        )
    finally:
        _launchctl("bootout", f"{_domain()}/{ICAL_PERMISSION_LABEL}")
        result.unlink(missing_ok=True)
        plist.unlink(missing_ok=True)


# ------------------------------------------------------------------ writing --

def render_script(cfg: Config, *, python: str | None = None) -> str:
    """The whole nightly job in execution order: ingest, then dream.

    The dream pass renders the brief itself as its last stage, so there is nothing
    to add after it.
    """
    python = python or sys.executable
    return f"""#!/bin/sh
# memcal nightly — written by `memcal schedule install`, then yours to edit.
# launchd runs this file *directly* (not `/bin/sh` on it), which is why it has this
# name and no extension: macOS lists a background item by the filename it executes, and
# `/bin/sh <script>` showed up in Login Items as `sh`. A change here takes effect on the
# next run with no reinstall. `memcal schedule run` executes this exact script.
set -u

export MEMCAL_HOME="{cfg.home}"
PY="{python}"
LOG="{log_path(cfg)}"

# The interpreter is pinned to whatever installed this, which is exact but survives
# only until the next `brew upgrade python` renames it. Falling back keeps the job
# alive; saying so keeps the fallback from being the silent kind.
if [ ! -x "$PY" ]; then
    echo "note: $PY is gone — falling back to python3 on PATH" >&2
    PY="$(command -v python3)" || {{ echo "no python3 at all; giving up" >&2; exit 1; }}
fi

# Keep our own log rather than letting launchd own the handle, so trimming it here
# cannot orphan the file descriptor the job is still writing to.
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 2000000 ]; then
    tail -n 1000 "$LOG" > "$LOG.trim" && mv "$LOG.trim" "$LOG"
fi
exec >> "$LOG" 2>&1

echo "=== $(date '+%Y-%m-%d %H:%M:%S')  nightly ==="
cd "{PROJECT_ROOT}" || exit 1

# Pull everything first: the dream pass reads the spool, so anything not ingested
# by now waits another day.
"$PY" -m memcal ingest all
INGEST=$?

# Frontier model, whole window. Nightly may overwrite what a cheaper pass wrote
# today; anything older than today is frozen unless new traffic references it.
"$PY" -m memcal dream --mode nightly
DREAM=$?

echo "=== $(date '+%Y-%m-%d %H:%M:%S')  done (ingest $INGEST, dream $DREAM) ==="

# Exit with what actually happened. This used to end on the `echo`, so the script's
# status was the echo's and launchd recorded 0 forever — including the run of
# 2026-08-15, which gave up on five bundles after 1,600s of rate limiting and wrote a
# non-null `error` on its `runs` row. `schedule.status()` reads launchd's last exit
# code as its "is this job working" signal and says so in the same breath as
# "installed: yes", so the one job that spends money was the one that could not report
# failing. `render_catchup_script` has always ended `exit "$STATUS"`; this is that.
[ "$DREAM" -ne 0 ] && exit "$DREAM"
exit "$INGEST"
"""


def render_catchup_script(cfg: Config, python: str | None = None) -> str:
    """The retry. Cheap enough to run on a timer, quiet enough to run twice a day.

    Deliberately *not* a second dream pass: this only fetches, so anything it catches up
    is waiting in the spool for the 3am run, and running it cannot cost a model call.
    """
    python = python or sys.executable
    return f"""#!/bin/sh
# memcal catch-up — written by `memcal schedule install`, then yours to edit.
# Named, and run directly, for the same reason as memcal-nightly: two agents both
# listed as `sh` in Login Items is worse than one.
# Pulls only the sources that are behind *and* reachable right now, and does nothing
# at all on a day when everything is up to date.
set -u

export MEMCAL_HOME="{cfg.home}"
PY="{python}"
LOG="{log_path(cfg)}"

if [ ! -x "$PY" ]; then
    PY="$(command -v python3)" || exit 1
fi

exec >> "$LOG" 2>&1
cd "{PROJECT_ROOT}" || exit 1

# Say nothing on a quiet run: this fires twice a day and a log that fills with
# "nothing stale" is a log nobody reads on the day it matters.
OUT="$("$PY" -m memcal ingest --stale 2>&1)"
STATUS=$?
case "$OUT" in
    "nothing stale that is reachable right now") exit 0 ;;
esac
echo "=== $(date '+%Y-%m-%d %H:%M:%S')  catch-up ==="
echo "$OUT"
exit "$STATUS"
"""


def render_plist(cfg: Config, *, hour: int = DEFAULT_HOUR, minute: int = DEFAULT_MINUTE) -> dict:
    return {
        "Label": LABEL,
        # The script itself, not `/bin/sh <script>`. See SCRIPT_NAMES: macOS prints
        # argv[0]'s filename in Login Items, and `/bin/sh` there reads as `sh`.
        "ProgramArguments": [str(script_path(cfg))],
        "StartCalendarInterval": {"Hour": int(hour), "Minute": int(minute)},
        "RunAtLoad": False,
        # The script keeps its own log; this only catches a job that fails to start.
        "StandardOutPath": str(cfg.home / "launchd.err"),
        "StandardErrorPath": str(cfg.home / "launchd.err"),
        "ProcessType": "Background",
        "EnvironmentVariables": {"MEMCAL_HOME": str(cfg.home)},
        "WorkingDirectory": str(PROJECT_ROOT),
    }


def render_catchup_plist(cfg: Config, *, hours=CATCHUP_HOURS) -> dict:
    """`StartCalendarInterval` as a *list*, which launchd accepts and means "each of"."""
    return {
        "Label": CATCHUP_LABEL,
        "ProgramArguments": [str(catchup_script_path(cfg))],
        "StartCalendarInterval": [{"Hour": int(h), "Minute": 0} for h in hours],
        "RunAtLoad": False,
        "StandardOutPath": str(cfg.home / "launchd.err"),
        "StandardErrorPath": str(cfg.home / "launchd.err"),
        "ProcessType": "Background",
        "EnvironmentVariables": {"MEMCAL_HOME": str(cfg.home)},
        "WorkingDirectory": str(PROJECT_ROOT),
    }


def _launchctl(*args: str, runner=subprocess.run) -> tuple[int, str]:
    """One `launchctl` verb, as (exit code, what it said)."""
    try:
        proc = runner(["launchctl", *args], capture_output=True, text=True)
    except OSError as exc:
        return 127, f"launchctl is unavailable on this host: {exc}"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def install(cfg: Config, *, hour: int = DEFAULT_HOUR, minute: int = DEFAULT_MINUTE) -> list[str]:
    cfg.ensure_dirs()
    out = []

    script = script_path(cfg)
    previous = script.exists() and script.read_text(encoding="utf-8")
    script.write_text(render_script(cfg), encoding="utf-8")
    script.chmod(0o755)
    out.append(f"wrote {script}")
    out.extend(_retire_legacy(cfg, "nightly", render_script(cfg)))
    if previous and previous != render_script(cfg):
        out.append(f"note: {script.name} had local edits and they were overwritten")

    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        plistlib.dump(render_plist(cfg, hour=hour, minute=minute), fh)
    out.append(f"wrote {path}")

    # Replacing an existing agent means booting the old one out first; a bootstrap
    # over a loaded label fails with "service already loaded" and changes nothing.
    _launchctl("bootout", f"{_domain()}/{LABEL}")
    code, message = _launchctl("bootstrap", _domain(), str(path))
    if code != 0:
        # Older macOS, or a domain that will not take a bootstrap.
        code, message = _launchctl("load", "-w", str(path))
    if code != 0:
        out.append(f"launchctl refused it: {message or code}")
    else:
        out.append(f"loaded {LABEL} — next run {next_run(hour, minute)}")
    out.extend(_install_catchup(cfg))
    out.append(f"log {log_path(cfg)}")
    return out


def _code_only(body: str) -> str:
    """A script's commands, without its commentary.

    "Did the user edit this?" compared whole files at first, and every one of them came back
    *yes* the moment the template's own header comment changed — so a clean machine was
    told it had local edits and kept a `.superseded` copy of a file identical to the new
    one. The comments in these scripts are ours and the commands are the job; only the
    second can be edited in a way worth preserving.
    """
    return "\n".join(line.rstrip() for line in body.splitlines()
                      if line.strip() and not line.lstrip().startswith("#"))


def _retire_legacy(cfg: Config, which: str, current: str) -> list[str]:
    """Clear `nightly.sh` / `catchup.sh` once the same job runs from its new name.

    Leaving them would put two plausible copies of the nightly script in the home
    directory with nothing saying which one launchd runs — the exact shape `tools/`
    has a rule against. An edited one is kept, under a name that cannot be mistaken for
    live, because their edits are their.
    """
    stale = cfg.home / LEGACY_SCRIPT_NAMES[which]
    if not stale.exists():
        return []
    body = stale.read_text(encoding="utf-8", errors="replace")
    if _code_only(body) == _code_only(current):
        stale.unlink()
        return [f"removed {stale.name} — the job runs as {SCRIPT_NAMES[which]} now"]
    kept = stale.with_name(stale.name + ".superseded")
    stale.rename(kept)
    return [f"{stale.name} had local edits — kept as {kept.name}; the job now runs "
            f"{SCRIPT_NAMES[which]}, so move anything you want back"]


def _install_catchup(cfg: Config) -> list[str]:
    """The daytime retry, installed alongside the nightly pass and never on its own.

    One `schedule install` should leave the machine in a working state, and "working"
    includes being able to reach a source whose dependency is asleep at 3am. Splitting
    this behind a second opt-in would mean the fix for report 18 shipped switched off.
    """
    out = []
    script = catchup_script_path(cfg)
    script.write_text(render_catchup_script(cfg), encoding="utf-8")
    script.chmod(0o755)
    out.extend(_retire_legacy(cfg, "catchup", render_catchup_script(cfg)))
    path = catchup_plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        plistlib.dump(render_catchup_plist(cfg), fh)
    _launchctl("bootout", f"{_domain()}/{CATCHUP_LABEL}")
    code, message = _launchctl("bootstrap", _domain(), str(path))
    if code != 0:
        code, message = _launchctl("load", "-w", str(path))
    hours = ", ".join(f"{h:02d}:00" for h in CATCHUP_HOURS)
    out.append(f"loaded {CATCHUP_LABEL} — catches up stale sources at {hours}"
               if code == 0 else f"launchctl refused the catch-up job: {message or code}")
    return out


def uninstall(cfg: Config) -> list[str]:
    out = []
    for label, path in ((LABEL, plist_path()), (CATCHUP_LABEL, catchup_plist_path())):
        code, message = _launchctl("bootout", f"{_domain()}/{label}")
        if code != 0:
            _launchctl("unload", "-w", str(path))
        out.append(f"unloaded {label}" if code == 0
                   else f"{label} not loaded ({message or 'already gone'})")
        if path.exists():
            path.unlink()
            out.append(f"removed {path}")
    out.append(f"left {script_path(cfg)}, {catchup_script_path(cfg)} and the log in place")
    return out


def next_run(hour: int = DEFAULT_HOUR, minute: int = DEFAULT_MINUTE) -> str:
    now = datetime.now()
    when = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
    if when <= now:
        when += timedelta(days=1)
    hours = (when - now).total_seconds() / 3600
    return f"{when:%a %H:%M} (in {hours:.1f}h)"


# ------------------------------------------------------------------ reading --

def status(cfg: Config, *, runner=subprocess.run) -> dict:
    installed = plist_path().exists()
    scheduled = None
    if installed:
        try:
            with plist_path().open("rb") as fh:
                data = plistlib.load(fh)
            interval = data.get("StartCalendarInterval", {})
            scheduled = (int(interval.get("Hour", 0)), int(interval.get("Minute", 0)))
        except Exception:
            scheduled = None

    code, listing = _launchctl("print", f"{_domain()}/{LABEL}", runner=runner)
    loaded = code == 0
    last_exit = None
    for line in listing.splitlines():
        if "last exit code" in line:
            last_exit = line.split("=")[-1].strip()

    log = log_path(cfg)
    tail = ""
    if log.exists():
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = "\n".join(lines[-12:])

    # A job that runs nightly and fails nightly looks exactly like a quiet week from
    # every other angle, so the things most likely to rot get checked by name.
    warning = None
    script = script_path(cfg)
    if installed and not script.exists():
        # The failure this check exists for, and the one it originally missed: the plist
        # was loaded and pointed at a script that was not there, launchd wrote
        # `nightly.sh: No such file or directory` into launchd.err every night, and
        # nothing read that file. The store went five days with one dream run in its
        # entire life while `memcal schedule` reported the job installed and loaded.
        #
        # The old check only looked *inside* the script, so a missing script skipped it
        # entirely — the one thing that actually broke was the one thing not checked.
        warning = (f"{script} does not exist, so the job fails every night the moment "
                   f"launchd starts it — run `memcal schedule install` to regenerate it")
    elif script.exists():
        for line in script.read_text(encoding="utf-8").splitlines():
            if line.startswith("PY="):
                pinned = line[3:].strip().strip('"')
                if not Path(pinned).exists():
                    warning = (f"the pinned interpreter {pinned} is gone — the job falls "
                               f"back to python3 on PATH; reinstall to re-pin")
                break

    # launchd's own stderr, which is where a job that cannot start says so. Nothing read
    # it before, which is why the missing script was invisible for five days.
    err = cfg.home / "launchd.err"
    startup_error = ""
    if err.exists():
        startup_error = err.read_text(encoding="utf-8", errors="replace").strip()[-400:]
        if startup_error and not warning:
            warning = f"launchd could not start the job: {startup_error.splitlines()[-1]}"

    # launchd has been reporting `last exit code = 127` — the shell's "command not
    # found" — every night, and it was parsed out of the listing above and then only
    # ever printed. A nightly job that exits non-zero is failing, whatever the cause,
    # and that is worth saying in the same breath as "installed: yes".
    #
    # But launchd spells "has not run yet" as `last exit code = (never exited)`, in the
    # same field, so a freshly installed job read as a failing one the moment the
    # missing-script warning above stopped taking priority. That is the sentinel shape
    # exactly: the one value meaning *no value* satisfied a test for a bad value. Only
    # an integer is an exit code; anything else is launchd saying it has nothing yet.
    try:
        exited_with = int(str(last_exit).strip())
    except (TypeError, ValueError):
        exited_with = 0
    if not warning and exited_with != 0:
        warning = (f"the last run exited {last_exit} — the job is installed but not "
                   f"working; see {log_path(cfg)}")

    # The catch-up job, reported beside the nightly one rather than in a second command.
    # A retry nobody can see the state of is a retry you have to trust, and the whole
    # reason it exists is that the nightly job was trusted for nine days.
    catchup_loaded = _launchctl(
        "print", f"{_domain()}/{CATCHUP_LABEL}", runner=runner)[0] == 0
    if not warning and installed and not catchup_plist_path().exists():
        warning = ("the catch-up job is not installed, so a source that is unreachable "
                   "at 03:00 waits a full day — run `memcal schedule install`")
    return {
        "warning": warning,
        "catchup": {
            "label": CATCHUP_LABEL,
            "installed": catchup_plist_path().exists(),
            "loaded": catchup_loaded,
            "at": list(CATCHUP_HOURS),
            "script": str(catchup_script_path(cfg)),
        },
        "installed": installed,
        "loaded": loaded,
        "label": LABEL,
        "plist": str(plist_path()),
        "script": str(script_path(cfg)),
        "log": str(log) if log.exists() else None,
        "at": scheduled,
        "next": next_run(*scheduled) if scheduled else None,
        "last_exit": last_exit,
        "startup_error": startup_error,
        "tail": tail,
    }


def run_now(cfg: Config) -> int:
    """Run the script itself, not a reimplementation of it. If the two could drift,
    a green result here would say nothing about what happens at 3am."""
    script = script_path(cfg)
    if not script.exists():
        print("not installed — run `memcal schedule install` first")
        return 1
    print(f"running {script} (output goes to {log_path(cfg)})")
    # Exactly what launchd runs — the file itself, through its shebang. Invoking
    # `/bin/sh <script>` here would still work and would stop this being a test of the
    # thing that actually happens at 3am, which is the whole point of the function.
    return subprocess.run([str(script)]).returncode
