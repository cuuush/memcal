"""Guide explicit macOS Full Disk Access approval and verify each requester.

We test reads of enabled source tables, not TCC's private permission database.
The Settings toggle remains a user action; this module only opens and reveals.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from . import schedule

SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
TARGETS = ("auto", "memcal", "host")


def host_app(cfg, *, pid=None, runner=subprocess.run) -> Path | None:
    """Find an enclosing application from process paths, without reading arguments."""
    current = os.getpid() if pid is None else pid
    seen = set()
    for _ in range(16):
        if current <= 1 or current in seen:
            break
        seen.add(current)
        try:
            result = runner(["ps", "-p", str(current), "-o", "ppid=", "-o", "comm="],
                            capture_output=True, text=True, timeout=2)
            parent, executable = result.stdout.strip().split(None, 1)
            current = int(parent)
        except (OSError, subprocess.TimeoutExpired, ValueError):
            break
        if result.returncode:
            break
        root, separator, _ = executable.partition(".app/")
        if separator:
            app = Path(root + ".app")
            if app != schedule.app_path(cfg) and app.name != "Python.app":
                return app
    return None


def status(cfg) -> dict:
    """Independent interactive and scheduled reads; never opens settings or ingests."""
    if not schedule._is_macos():
        return {"supported": False, "ready": True, "target": None,
                "detail": "Full Disk Access is a macOS setting; nothing to grant on this host."}
    interactive = schedule.read_source_access(cfg)["checks"]
    verdict, detail = schedule.source_access_health(cfg)
    denied = any(check.get("ok") is False for check in interactive.values())
    scheduled_ok = verdict == "ok"
    target = ("host" if scheduled_ok and denied else
              "memcal" if interactive and not scheduled_ok else None)
    app = host_app(cfg) if target == "host" else None
    ready = not interactive or (scheduled_ok and all(
        check.get("ok") is True for check in interactive.values()))
    return {
        "supported": True, "ready": ready, "target": target,
        "interactive": interactive,
        "scheduled": {"status": verdict, "ok": scheduled_ok, "detail": detail},
        "memcal_app": str(schedule.app_path(cfg)),
        "host_app": str(app) if app else None,
        "detail": ("No enabled protected local sources require a grant." if not interactive else
                   "Scheduled Memcal access works; the interactive terminal/agent has a separate grant."
                   if target == "host" else "Enabled protected source reads are verified."
                   if ready else "Scheduled protected source access still needs verification."),
    }


def guide(cfg, state: dict, target="auto") -> dict:
    """Describe the exact bundle and restart/recheck steps before any UI opens."""
    if target not in TARGETS:
        raise ValueError("target must be auto, memcal or host")
    chosen = state.get("target") if target == "auto" else target
    if not state["supported"] or chosen is None:
        return {**state, "selected_target": None, "app": None, "steps": []}
    path = (state.get("memcal_app") if chosen == "memcal" else
            state.get("host_app") or (str(found) if (found := host_app(cfg)) else None))
    steps = [
        "Open System Settings → Privacy & Security → Full Disk Access.",
        (f"Find {Path(path).name}, or click + and select {path}; enable its switch."
         if path else "Find the terminal or agent app running this command and enable its switch."),
        "Approve any additional macOS confirmation yourself. Full Disk Access includes other apps' protected data, beyond enabled Memcal sources.",
        ("The next Memcal probe starts a fresh app process to verify its grant."
         if chosen == "memcal" else "Quit and reopen that terminal/agent app for its new grant to take effect."),
        "Run memcal permissions status to recheck; no messages are ingested by this helper.",
    ]
    return {**state, "selected_target": chosen, "app": path, "steps": steps}


def setup(cfg, *, target="auto", open_ui=True, runner=subprocess.run) -> dict:
    """Open Settings and reveal the chosen bundle; never change a privacy toggle."""
    if target not in TARGETS:
        raise ValueError("target must be auto, memcal or host")
    state = status(cfg)
    plan = guide(cfg, state, target)
    plan["opened"] = False
    plan["notes"] = []
    chosen = plan["selected_target"]
    checks = state.get("interactive", {})
    if chosen == "memcal":
        verified = not checks or state.get("scheduled", {}).get("ok")
    elif chosen == "host":
        verified = all(check.get("ok") is True for check in checks.values())
    else:
        verified = state.get("ready")
    plan["target_ready"] = bool(verified)
    if verified:
        plan["steps"] = []
        if chosen:
            plan["detail"] = ("Scheduled Memcal source reads already work; no additional Memcal grant is needed."
                              if chosen == "memcal" else "Interactive source reads already work; no additional host grant is needed.")
    if not open_ui or not chosen or verified:
        return plan
    if chosen == "memcal" and schedule.bundle_health(cfg)[0] != "ok":
        plan["notes"].extend(schedule.build_app_bundle(cfg))
        if schedule.bundle_health(cfg)[0] != "ok":
            plan["error"] = "Could not prepare memcal.app. Run memcal schedule install, then retry."
            return plan
    try:
        opened = runner(["open", SETTINGS_URL], capture_output=True, text=True, timeout=10)
        if opened.returncode:
            opened = runner(["open", "-a", "System Settings"], capture_output=True,
                            text=True, timeout=10)
        if opened.returncode:
            plan["error"] = "Could not open System Settings; follow the printed steps manually."
            return plan
        plan["opened"] = True
        if plan["app"] and Path(plan["app"]).is_dir():
            revealed = runner(["open", "-R", plan["app"]], capture_output=True,
                              text=True, timeout=10)
            if revealed.returncode:
                plan["notes"].append("Finder could not reveal the app; use its printed path with the + button.")
    except (OSError, subprocess.TimeoutExpired) as exc:
        plan["error"] = f"Could not open the permission helper: {exc}"
    return plan
