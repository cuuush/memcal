"""Manage the nightly launchd job."""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from . import db
from .config import Config, PROJECT_ROOT

LABEL = "com.memcal.nightly"
ICAL_PERMISSION_LABEL = "com.memcal.ical-permission"
ICAL_EVENTKIT_LABEL = "com.memcal.ical-eventkit"
DEFAULT_HOUR = 3
DEFAULT_MINUTE = 0

#: Bundle used to give scheduled work a human-readable macOS identity.
APP_BUNDLE_ID = "com.memcal.agent"
APP_NAME = "memcal"
LAUNCHER_SOURCE = PROJECT_ROOT / "memcal" / "macos" / "launcher.c"
#: Checked-in icon source (the [E]/[T]/[Q] handle grid). The build converts it to
#: AppIcon.icns; when it is absent the bundle still builds, just without an icon.
ICON_SOURCE = PROJECT_ROOT / "memcal" / "macos" / "icon.png"
APP_ICON_NAME = "AppIcon"
APP_ICON_FILENAME = f"{APP_ICON_NAME}.icns"


def _is_macos() -> bool:
    """True on the only platform the app bundle means anything on.

    Patched to True in platform tests so the macOS paths are exercised off a Mac;
    production callers use it to skip bundle work elsewhere entirely.
    """
    return sys.platform == "darwin"

#: Interval used to catch a missed run after wake.
WAKE_INTERVAL = 1800

#: Agents replaced by the combined nightly job.
RETIRED_LABELS = ("com.memcal.catchup", "com.memcal.missed")


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def retired_plist_paths() -> list[Path]:
    return [Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
            for label in RETIRED_LABELS]


#: An extensionless executable gives the background item a useful display name.
SCRIPT_NAMES = {"nightly": "memcal-nightly"}

#: Script names used by earlier schedule layouts.
LEGACY_SCRIPT_NAMES = {"nightly": ("nightly.sh",),
                       "retired": ("catchup.sh", "memcal-catchup", "memcal-missed")}


def script_path(cfg: Config) -> Path:
    return cfg.home / SCRIPT_NAMES["nightly"]


def app_path(cfg: Config) -> Path:
    """Return the local launcher bundle path."""
    return cfg.home / f"{APP_NAME}.app"


def app_executable(cfg: Config) -> Path:
    return app_path(cfg) / "Contents" / "MacOS" / APP_NAME


def app_icon_path(cfg: Config) -> Path:
    """Return where the built bundle icon lives."""
    return app_path(cfg) / "Contents" / "Resources" / APP_ICON_FILENAME


def launch_through(cfg: Config, argv: list[str]) -> list[str]:
    """Run through the app launcher when a usable one is installed."""
    if not _is_macos():
        return argv
    executable = app_executable(cfg)
    if executable.is_file() and os.access(executable, os.X_OK):
        return [str(executable), *argv]
    return argv


def render_info_plist() -> dict:
    """Return the launcher bundle metadata."""
    reason = "memcal reads your calendar and reminders to build its brief."
    return {
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleIdentifier": APP_BUNDLE_ID,
        "CFBundleExecutable": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "CFBundleIconFile": APP_ICON_NAME,
        "CFBundleIconName": APP_ICON_NAME,
        # An agent, not an app with a window or a Dock icon — but an agent that can
        # still present UI. `LSBackgroundOnly` was tried first and broke consent:
        # a background-only identity can never show a TCC dialog, so `ical setup`
        # hung for the whole EventKit wait with no prompt ever appearing.
        # `LSUIElement` keeps it out of the Dock while letting the grant through.
        "LSUIElement": True,
        "LSMinimumSystemVersion": "10.15",
        "NSAppleEventsUsageDescription": reason,
        "NSCalendarsUsageDescription": reason,
    }


def _run(args: list[str], *, runner=subprocess.run) -> tuple[int, str]:
    """One external command, as (exit code, what it said)."""
    try:
        proc = runner(args, capture_output=True, text=True)
    except OSError as exc:
        return 127, f"{args[0]} is unavailable: {exc}"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _compiler_command(*, runner=subprocess.run) -> list[str] | None:
    """Prefer the SDK-aware compiler, then fall back to one on PATH."""
    code, _ = _run(["xcrun", "-f", "clang"], runner=runner)
    if code == 0:
        return ["xcrun", "clang"]
    found = shutil.which("clang") or shutil.which("cc")
    return [found] if found else None


def _bundle_is_current(cfg: Config) -> bool:
    """True when the built launcher is newer than its sources.

    The icon is a source too: a newer icon.png must trigger one rebuild (and
    re-sign), but a missing icon toolset must not trap the bundle in a rebuild
    loop — so freshness is measured exe-vs-sources, never icns-vs-source.
    """
    try:
        exe_mtime = app_executable(cfg).stat().st_mtime if app_executable(cfg).is_file() else None
        if exe_mtime is None:
            return False
        if exe_mtime < LAUNCHER_SOURCE.stat().st_mtime:
            return False
        if ICON_SOURCE.is_file() and exe_mtime < ICON_SOURCE.stat().st_mtime:
            return False
        return True
    except OSError:
        return False


def bundle_health(cfg: Config) -> tuple[str, str]:
    """`(verdict, detail)` for the app-bundle wrapper: ok, stale, or fallback.

    Verdicts are `ok` (built, executable, current), `stale` (built but behind the
    source, not executable, or half-written), or `absent` (no usable bundle — the
    nightly job and probes run under the interpreter's name, including a metadata-
    only `.app` left by a compiler-less install). Doctor renders stale as a
    warning with a `--rebuild` fix: every stale state still falls back to the
    interpreter, so none of them is fatal.
    """
    if not _is_macos():
        return ("ok", "app bundle is macOS-only — nothing to check here")
    exe = app_executable(cfg)
    tmp = exe.with_name(exe.name + ".new")
    try:
        if tmp.exists():
            return ("stale", f"{tmp.name} left behind by a failed build — the live "
                             f"{exe.name} was kept")
        if not exe.is_file():
            return ("absent", "no memcal.app launcher — Calendar access reads as "
                              "the interpreter")
        if not os.access(exe, os.X_OK):
            return ("stale", f"{exe.name} is not executable, so the job falls back "
                             "to the interpreter")
        try:
            info = plistlib.loads((app_path(cfg) / "Contents" / "Info.plist").read_bytes())
        except Exception:
            info = {}
        if info.get("CFBundleIdentifier") != APP_BUNDLE_ID:
            return ("stale", "memcal.app Info.plist does not carry the memcal bundle id")
        try:
            if exe.stat().st_mtime < LAUNCHER_SOURCE.stat().st_mtime:
                return ("stale", "memcal.app is older than launcher.c — rebuild it")
        except OSError:
            pass
        return ("ok", f"memcal.app current — Calendar access reads as {APP_NAME}")
    except OSError as exc:
        return ("stale", f"could not inspect memcal.app ({exc})")


def plist_identity_health(cfg: Config) -> tuple[str, str]:
    """`(verdict, detail)` for the installed plist's bundle association."""
    if not _is_macos():
        return ("ok", "plist identity is macOS-only — nothing to check here")
    try:
        with plist_path().open("rb") as fh:
            installed = plistlib.load(fh)
    except Exception:
        return ("absent", "no installed plist")
    exe = app_executable(cfg)
    routed = exe.is_file() and os.access(exe, os.X_OK)
    claimed = list(installed.get("AssociatedBundleIdentifiers") or [])
    if routed and APP_BUNDLE_ID in claimed:
        return ("ok", "nightly plist claims the memcal bundle it runs through")
    if not routed and APP_BUNDLE_ID not in claimed:
        return ("ok", "nightly plist runs the script directly, with no bundle to claim")
    if routed:
        return ("stale", "nightly plist runs through memcal.app but claims no bundle")
    return ("stale", "nightly plist claims memcal.app that is not built")


#: (base size, scale) pairs needed for a complete macOS iconset.
ICONSET_SIZES = ((16, 1), (16, 2), (32, 1), (32, 2), (128, 1),
                 (128, 2), (256, 1), (256, 2), (512, 1), (512, 2))


def _install_app_icon(cfg: Config, *, runner=subprocess.run) -> list[str]:
    """Convert ICON_SOURCE to AppIcon.icns inside the bundle, best-effort.

    Missing source, missing sips/iconutil, or a mocked runner that answers 0
    without writing anything all degrade to "no icon" rather than a failed
    build: the bundle identity (and the Calendar grant) matters more than art.
    """
    if not ICON_SOURCE.is_file():
        return []
    dest = app_icon_path(cfg)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return [f"note: could not install the app icon ({exc})"]
    try:
        with tempfile.TemporaryDirectory(prefix="memcal-iconset-") as tmpdir:
            iconset = Path(tmpdir) / f"{APP_ICON_NAME}.iconset"
            iconset.mkdir(parents=True, exist_ok=True)
            for base, scale in ICONSET_SIZES:
                pixels = base * scale
                suffix = "@2x" if scale == 2 else ""
                cell = iconset / f"icon_{base}x{base}{suffix}.png"
                code, message = _run(
                    ["sips", "-z", str(pixels), str(pixels),
                     str(ICON_SOURCE), "--out", str(cell)], runner=runner)
                if code:
                    return [f"note: could not resize the app icon ({message or code})"]
                if not cell.is_file():
                    # A test double (or a tool that answered 0 without writing)
                    # — stay silent and iconless rather than crying failure.
                    return []
            tmp_icns = Path(tmpdir) / APP_ICON_FILENAME
            code, message = _run(
                ["iconutil", "-c", "icns", str(iconset), "-o", str(tmp_icns)],
                runner=runner)
            if code:
                return [f"note: could not build the app icon ({message or code})"]
            if not tmp_icns.is_file():
                return []
            try:
                tmp_icns.replace(dest)
            except OSError as exc:
                return [f"note: could not install the app icon ({exc})"]
    except OSError as exc:
        return [f"note: could not build the app icon ({exc})"]
    return [f"installed {APP_ICON_FILENAME}"]


def build_app_bundle(cfg: Config, *, runner=subprocess.run, force: bool = False) -> list[str]:
    """Build and ad-hoc-sign the optional launchd app wrapper."""
    if not _is_macos():
        return ["memcal.app is macOS-only — skipping the bundle build"]
    out: list[str] = []
    app = app_path(cfg)
    contents = app / "Contents"
    try:
        tmp = app_executable(cfg).with_name(app_executable(cfg).name + ".new")
        if tmp.exists() and not force:
            # A kill between compile and rename leaves the temp output behind
            # while the live binary is untouched. Clear it so the skip-current
            # fast path below does not keep reporting stale forever.
            try:
                tmp.unlink()
            except OSError:
                pass
        if not force and _bundle_is_current(cfg):
            # The grant is keyed to the bundle signature: rebuilding when nothing
            # changed only risks invalidating the TCC grant and forcing a re-prompt.
            return [f"{app.name} is current — Calendar access already reads as {APP_NAME}"]
        cc = _compiler_command(runner=runner)
        if not cc:
            out.append("note: no C compiler; memcal.app not built — the nightly "
                       "job will run under the interpreter's name")
            return out
        (contents / "MacOS").mkdir(parents=True, exist_ok=True)
        with (contents / "Info.plist").open("wb") as fh:
            plistlib.dump(render_info_plist(), fh)
        (contents / "PkgInfo").write_text("APPL????", encoding="ascii")
        # Icon before codesign: Resources are part of the signature.
        out.extend(_install_app_icon(cfg, runner=runner))

        # Compile beside the live executable and rename into place, so a failed
        # rebuild never leaves a stale or missing binary where a working one was.
        code, message = _run([*cc, "-O2", "-o", str(tmp),
                              str(LAUNCHER_SOURCE)], runner=runner)
        if code:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            out.append(f"note: could not compile the launcher ({message or code})")
            return out
        try:
            tmp.replace(app_executable(cfg))
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            out.append(f"note: could not install the launcher ({exc})")
            return out
        code, message = _run(["codesign", "--force", "--sign", "-",
                              "--identifier", APP_BUNDLE_ID, str(app)], runner=runner)
        if code:
            out.append(f"note: memcal.app is unsigned ({message or code}); the "
                       "Calendar grant may not survive a rebuild")
        out.append(f"built {app.name} — Calendar access reads as {APP_NAME}")
    except OSError as exc:
        out.append(f"note: could not build memcal.app ({exc})")
    return out


def stamp_path(cfg: Config) -> Path:
    """Path recording when the whole scheduled job last started."""
    return cfg.home / "nightly.last-start"


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


def launchd_memcal_call(cfg: Config, *, label: str, stem: str,
                          memcal_argv: list[str], timeout: int = 45,
                          ) -> tuple[dict | None, str]:
    """Run one memcal invocation as a temporary launchd agent; return its payload.

    The agent runs through the app bundle with the bundle id claimed: the nightly
    job's TCC identity. A terminal `osascript` resolves to the *terminal's* identity
    instead, so consent requests and account checks go through here. Returns the
    parsed payload, or None on timeout/startup failure, plus the identity label.
    """
    cfg.ensure_dirs()
    python = pinned_python(cfg)
    plist = cfg.home / f"{stem}.plist"
    result = cfg.home / f"{stem}-result.json"
    argv = launch_through(cfg, [python, "-m", "memcal", *memcal_argv,
                                "--result", str(result)])
    identity = APP_NAME if argv[0] == str(app_executable(cfg)) else python
    try:
        result.unlink(missing_ok=True)
        payload_plist: dict = {
            "Label": label,
            "ProgramArguments": argv,
            "RunAtLoad": True,
            "ProcessType": "Interactive",
            "EnvironmentVariables": {
                "MEMCAL_HOME": str(cfg.home),
                "PYTHONPATH": str(PROJECT_ROOT),
            },
            "WorkingDirectory": str(PROJECT_ROOT),
        }
        if argv[0] == str(app_executable(cfg)):
            payload_plist["AssociatedBundleIdentifiers"] = [APP_BUNDLE_ID]
        with plist.open("wb") as fh:
            plistlib.dump(payload_plist, fh)
        _launchctl("bootout", f"{_domain()}/{label}")
        code, message = _launchctl("bootstrap", _domain(), str(plist))
        if code:
            return None, f"could not start launchd job {label}: {message or code}"
        deadline = time.monotonic() + max(1, timeout)
        while time.monotonic() < deadline:
            if result.exists():
                try:
                    return json.loads(result.read_text(encoding="utf-8")), identity
                except (OSError, ValueError):
                    pass
            time.sleep(0.25)
        return None, identity
    finally:
        _launchctl("bootout", f"{_domain()}/{label}")
        result.unlink(missing_ok=True)
        plist.unlink(missing_ok=True)


def calendar_permission_probe(cfg: Config, *, timeout: int = 45) -> tuple[bool, str]:
    """Request Calendar access from a temporary launchd agent.

    Running `osascript` from a terminal can authorize the terminal/Codex responsible
    process while the 03:00 job remains unapproved. This probe uses the same user
    launchd domain, source checkout, home, pinned Python, and — through the app
    bundle — the same TCC identity as the nightly agent, so the grant it obtains is
    the one the 03:00 job will use.
    """
    payload, identity = launchd_memcal_call(
        cfg, label=ICAL_PERMISSION_LABEL, stem="ical-permission",
        memcal_argv=["ical", "probe", "--context", "nightly"], timeout=timeout)
    if payload is None:
        if identity.startswith("could not start"):
            return False, identity
        return False, (
            f"nightly launchd Calendar check timed out ({identity}); "
            "a permission prompt may still be waiting"
        )
    ok = bool(payload.get("ok"))
    detail = str(payload.get("message") or "no detail")
    return ok, f"nightly launchd requester ({identity}): {detail}"


def eventkit_call(cfg: Config, verb: str, *, timeout: int = 90) -> tuple[bool, str]:
    """Run one EventKit verb as memcal and report it.

    The request and the account check both run *inside* the temporary agent, where
    plain in-process calls already carry the nightly identity. Called with "request"
    to open the consent dialog, with "where" for the passive account check.
    """
    payload, identity = launchd_memcal_call(
        cfg, label=ICAL_EVENTKIT_LABEL, stem="ical-eventkit",
        memcal_argv=["ical", "probe", "--ek", verb, "--context", "nightly"],
        timeout=timeout)
    if payload is None:
        if identity.startswith("could not start"):
            return False, identity
        return False, (f"launchd EventKit {verb} timed out ({identity}); "
                       "a permission prompt may still be waiting")
    return bool(payload.get("ok")), str(payload.get("message") or "no detail")


# ------------------------------------------------------------------ writing --

def render_script(cfg: Config, *, python: str | None = None) -> str:
    """Render the scheduled job."""
    python = python or sys.executable
    return f"""#!/bin/sh
# Generated by `memcal schedule install`; launchd executes this file directly.
# `memcal schedule run` executes the same file.
set -u

export MEMCAL_HOME="{cfg.home}"
PY="{python}"
LOG="{log_path(cfg)}"
STAMP="{stamp_path(cfg)}"

# Fall back when the interpreter recorded at installation has moved.
if [ ! -x "$PY" ]; then
    echo "note: $PY is gone — falling back to python3 on PATH" >&2
    PY="$(command -v python3)" || {{ echo "no python3 at all; giving up" >&2; exit 1; }}
fi

# Trim the log before opening it for this run.
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 2000000 ]; then
    tail -n 1000 "$LOG" > "$LOG.trim" && mv "$LOG.trim" "$LOG"
fi
exec >> "$LOG" 2>&1

cd "{PROJECT_ROOT}" || exit 1

# `memcal schedule run` forces the owed path.
VERDICT="${{MEMCAL_RUN_NOW:-}}"
if [ "$VERDICT" != "owed" ]; then
    VERDICT="$("$PY" -m memcal schedule due 2>&1)"
fi

case "$VERDICT" in
    owed*) ;;
    ok*)
        # A catch-up fetch never invokes the model.
        OUT="$("$PY" -m memcal ingest --stale 2>&1)"
        STATUS=$?
        case "$OUT" in
            "nothing stale that is reachable right now") exit 0 ;;
        esac
        echo "=== $(date '+%Y-%m-%d %H:%M:%S')  catch-up ==="
        echo "$OUT"
        exit "$STATUS"
        ;;
    *)
        # Neither answer. `schedule status` reads launchd's last exit code, so a check
        # that cannot answer must exit non-zero rather than read as "nothing to do".
        echo "=== $(date '+%Y-%m-%d %H:%M:%S')  cannot tell whether a pass is owed ==="
        echo "$VERDICT"
        exit 1
        ;;
esac

# Record the start before ingest so later triggers do not duplicate this pass.
: > "$STAMP"

echo "=== $(date '+%Y-%m-%d %H:%M:%S')  nightly ==="

# Pull everything first: the dream pass reads the spool, so anything not ingested
# by now waits another day.
"$PY" -m memcal ingest all
INGEST=$?

# Frontier model, whole window. Nightly may overwrite what a cheaper pass wrote
# today; anything older than today is frozen unless new traffic references it.
"$PY" -m memcal dream --mode nightly
DREAM=$?

echo "=== $(date '+%Y-%m-%d %H:%M:%S')  done (ingest $INGEST, dream $DREAM) ==="

# Report dream failure first, otherwise the ingest result.
[ "$DREAM" -ne 0 ] && exit "$DREAM"
exit "$INGEST"
"""


def render_plist(cfg: Config, *, hour: int = DEFAULT_HOUR, minute: int = DEFAULT_MINUTE,
                 interval: int = WAKE_INTERVAL) -> dict:
    """Render the calendar, interval, and login triggers for one job."""
    argv = launch_through(cfg, [str(script_path(cfg))])
    plist: dict = {
        "Label": LABEL,
        "ProgramArguments": argv,
        "StartCalendarInterval": {"Hour": int(hour), "Minute": int(minute)},
        "StartInterval": int(interval),
        "RunAtLoad": True,
        # The script keeps its own log; this only catches a job that fails to start.
        "StandardOutPath": str(cfg.home / "launchd.err"),
        "StandardErrorPath": str(cfg.home / "launchd.err"),
        "ProcessType": "Background",
        "EnvironmentVariables": {"MEMCAL_HOME": str(cfg.home)},
        "WorkingDirectory": str(PROJECT_ROOT),
    }
    if argv[0] == str(app_executable(cfg)):
        plist["AssociatedBundleIdentifiers"] = [APP_BUNDLE_ID]
    return plist


def _launchctl(*args: str, runner=subprocess.run) -> tuple[int, str]:
    """One `launchctl` verb, as (exit code, what it said)."""
    try:
        proc = runner(["launchctl", *args], capture_output=True, text=True)
    except OSError as exc:
        return 127, f"launchctl is unavailable on this host: {exc}"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def install(cfg: Config, *, hour: int = DEFAULT_HOUR, minute: int = DEFAULT_MINUTE,
            force_rebuild: bool = False) -> list[str]:
    cfg.ensure_dirs()
    out = []

    out.extend(build_app_bundle(cfg, force=force_rebuild))

    script = script_path(cfg)
    previous = script.exists() and script.read_text(encoding="utf-8")
    script.write_text(render_script(cfg), encoding="utf-8")
    script.chmod(0o755)
    out.append(f"wrote {script}")
    out.extend(_retire_legacy(cfg, "nightly", render_script(cfg)))
    # Template comment changes are not user edits.
    if previous and _code_only(previous) != _code_only(render_script(cfg)):
        out.append(f"note: {script.name} had local edits and they were overwritten")

    # RunAtLoad must not turn installation into an immediate full pass.
    stamp = stamp_path(cfg)
    if not stamp.exists():
        stamp.touch()
        out.append(f"stamped {stamp.name} — the first pass is the next scheduled one")

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
        out.append(f"loaded {LABEL} — next run {next_run(hour, minute)}, "
                   f"and on wake if that one is missed")
        out.extend(_retire_agents(cfg))
    out.append(f"log {log_path(cfg)}")
    return out


def _retire_agents(cfg: Config) -> list[str]:
    """Unload retired agents, then preserve their scripts."""
    out = []
    all_unloaded = True
    for label, path in zip(RETIRED_LABELS, retired_plist_paths()):
        loaded = _launchctl("print", f"{_domain()}/{label}")[0] == 0
        had_plist = path.exists()
        if loaded:
            code, message = _launchctl("bootout", f"{_domain()}/{label}")
            if code != 0:
                code, message = _launchctl("unload", "-w", str(path))
            if code != 0:
                all_unloaded = False
                out.append(f"could not retire {label}: {message or code}")
                continue
        if had_plist:
            path.unlink()
        if loaded or had_plist:
            out.append(f"retired {label} — one agent does its job now")
    if not all_unloaded:
        return out
    for name in LEGACY_SCRIPT_NAMES["retired"]:
        stale = cfg.home / name
        if stale.exists():
            kept = stale.with_name(stale.name + ".superseded")
            suffix = 1
            while kept.exists():
                kept = stale.with_name(f"{stale.name}.superseded.{suffix}")
                suffix += 1
            stale.rename(kept)
            out.append(f"retired {name} — kept as {kept.name}")
    return out


def _code_only(body: str) -> str:
    """Return executable script lines for local-edit detection."""
    return "\n".join(line.rstrip() for line in body.splitlines()
                      if line.strip() and not line.lstrip().startswith("#"))


def _retire_legacy(cfg: Config, which: str, current: str) -> list[str]:
    """Remove an unchanged legacy script and preserve an edited one."""
    out = []
    for name in LEGACY_SCRIPT_NAMES[which]:
        stale = cfg.home / name
        if not stale.exists():
            continue
        body = stale.read_text(encoding="utf-8", errors="replace")
        if _code_only(body) == _code_only(current):
            stale.unlink()
            out.append(f"removed {stale.name} — the job runs as {SCRIPT_NAMES[which]} now")
            continue
        kept = stale.with_name(stale.name + ".superseded")
        stale.rename(kept)
        out.append(f"{stale.name} had local edits — kept as {kept.name}; the job now runs "
                   f"{SCRIPT_NAMES[which]}, so move anything you want back")
    return out


def uninstall(cfg: Config) -> list[str]:
    out = []
    for label, path in [(LABEL, plist_path()), *zip(RETIRED_LABELS, retired_plist_paths())]:
        code, message = _launchctl("bootout", f"{_domain()}/{label}")
        if code != 0:
            _launchctl("unload", "-w", str(path))
        if code == 0:
            out.append(f"unloaded {label}")
        elif label not in RETIRED_LABELS:
            out.append(f"{label} not loaded ({message or 'already gone'})")
        if path.exists():
            path.unlink()
            out.append(f"removed {path}")
    app = app_path(cfg)
    if app.exists():
        try:
            shutil.rmtree(app)
            out.append(f"removed {app.name}")
        except OSError as exc:
            out.append(f"could not remove {app.name}: {exc}")
    out.append(f"left {script_path(cfg)} and the log in place")
    return out


def next_run(hour: int = DEFAULT_HOUR, minute: int = DEFAULT_MINUTE) -> str:
    now = datetime.now()
    when = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
    if when <= now:
        when += timedelta(days=1)
    hours = (when - now).total_seconds() / 3600
    return f"{when:%a %H:%M} (in {hours:.1f}h)"


# ------------------------------------------------------------------ reading --

def scheduled_time(cfg: Config | None = None) -> tuple[int, int]:
    """The hour and minute the installed agent holds. `--hour` is a real option, so
    03:00 is a default, not a fact."""
    try:
        with plist_path().open("rb") as fh:
            interval = plistlib.load(fh).get("StartCalendarInterval", {})
        if isinstance(interval, list):
            interval = interval[0] if interval else {}
        return (int(interval.get("Hour", DEFAULT_HOUR)),
                int(interval.get("Minute", DEFAULT_MINUTE)))
    except Exception:
        return DEFAULT_HOUR, DEFAULT_MINUTE


def last_due(now: datetime, hour: int, minute: int) -> datetime:
    """The most recent moment the pass was supposed to start."""
    due = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
    return due - timedelta(days=1) if due > now else due


def last_started(cfg: Config) -> datetime | None:
    """When the job last began, or None if it never has on this store."""
    try:
        return datetime.fromtimestamp(stamp_path(cfg).stat().st_mtime).astimezone()
    except OSError:
        return None


def owed(cfg: Config, *, now: datetime | None = None) -> tuple[datetime | None, str]:
    """`(the due time that went unserved, why)`; the first is None when none did.

    The window is one night wide: a machine off for a week runs one pass when it comes
    back, not seven, and a pass at 08:00 does not leave tonight's 03:00 looking served.
    """
    now = now or db.now_dt()
    if not plist_path().exists():
        return None, "the job is not installed, so nothing is due"
    script = script_path(cfg)
    if not script.exists():
        return None, (f"{script} does not exist, so there is nothing to run — "
                      f"`memcal schedule install` regenerates it")
    hour, minute = scheduled_time(cfg)
    due = last_due(now, hour, minute)
    started = last_started(cfg)
    if started is None:
        # Only reachable if the stamp was deleted, since `install` writes one. Owed is
        # the safer wrong answer: one extra pass beats never running.
        return due, "never — no pass has been recorded on this store"
    if started >= due:
        # Worded about the job, not about 03:00: `install` stamps a fresh store at
        # whatever time it runs, and "the 03:00 pass ran at 12:59" would be a lie.
        return None, f"{started:%a %d %b %H:%M} — nothing missed"
    return due, (f"{started:%a %d %b %H:%M} — the {due:%a %H:%M} pass is owed and "
                 f"runs at the next wake-up")


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

    # Reported, not warned about: an owed pass is picked up by the next wake-up.
    due, why = owed(cfg)
    started = last_started(cfg)
    return {
        "warning": warning,
        "installed": installed,
        "loaded": loaded,
        "label": LABEL,
        "plist": str(plist_path()),
        "script": str(script_path(cfg)),
        "log": str(log) if log.exists() else None,
        "at": scheduled,
        "every": WAKE_INTERVAL,
        "next": next_run(*scheduled) if scheduled else None,
        "last_start": started.isoformat(timespec="seconds") if started else None,
        "owed": due.isoformat(timespec="seconds") if due else None,
        "why": why,
        "last_exit": last_exit,
        "startup_error": startup_error,
        "tail": tail,
    }


def run_now(cfg: Config, *, force: bool = True) -> int:
    """Run the script itself, not a reimplementation of it. If the two could drift,
    a green result here would say nothing about what happens at 03:00.

    `force` overrides the script's own owed check; typing `memcal schedule run` has
    already answered that question.
    """
    script = script_path(cfg)
    if not script.exists():
        print("not installed — run `memcal schedule install` first")
        return 1
    argv = launch_through(cfg, [str(script)])
    if argv[0] != str(script):
        print(f"running {script} through {app_path(cfg).name} "
              f"(output goes to {log_path(cfg)})")
    else:
        print(f"running {script} (output goes to {log_path(cfg)})")
    env = dict(os.environ)
    if force:
        env["MEMCAL_RUN_NOW"] = "owed"
    # Exactly what launchd runs — the file itself, through its shebang, routed
    # through the app bundle when one is built so the manual run uses the same
    # TCC identity as the 03:00 job. Invoking `/bin/sh <script>` here would still
    # work and would stop this being a test of the thing that actually happens
    # at 03:00, which is the whole point of the function.
    return subprocess.run(argv, env=env).returncode
