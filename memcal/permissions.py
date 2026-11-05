"""Guide explicit macOS Full Disk Access approval and verify each requester.

We test reads of enabled source tables, not TCC's private permission database.
The Settings toggle remains a user action; this module only opens and reveals.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from . import schedule

SETTINGS_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
TARGETS = ("auto", "memcal")


def status(cfg) -> dict:
    """Verify the app bridge, independently of the terminal's privacy grants."""
    from . import bridge
    if not schedule._is_macos():
        return {"supported": False, "ready": True, "target": None,
                "detail": "Full Disk Access is a macOS setting; nothing to grant on this host."}
    names = schedule._protected_sources(cfg)
    checks = {}
    if names and bridge.available(cfg):
        for name in names:
            ok, detail = bridge.check(cfg, name)
            checks[name] = {"ok": ok, "detail": detail}
        ready = all(check["ok"] for check in checks.values())
        verdict = "ok" if ready else "fail"
        detail = "; ".join(f"{name}: {check['detail']}" for name, check in checks.items())
    else:
        verdict, detail = schedule.source_access_health(cfg)
        ready = verdict in {"ok", "skip"}
    return {
        "supported": True, "ready": ready, "target": None if ready else "memcal",
        "checks": checks,
        "scheduled": {"status": verdict, "ok": ready, "detail": detail},
        "memcal_app": str(schedule.app_path(cfg)),
        "detail": ("No enabled protected local sources require a grant." if not names else
                   "Memcal app bridge is ready for protected source reads." if ready else
                   "Memcal app bridge needs setup or permission. Authorize memcal.app, not the terminal or agent."),
    }


def guide(cfg, state: dict, target="auto") -> dict:
    """Describe the exact bundle and restart/recheck steps before any UI opens."""
    if target not in TARGETS:
        raise ValueError("target must be auto or memcal")
    chosen = state.get("target") if target == "auto" else target
    if not state["supported"] or chosen is None:
        return {**state, "selected_target": None, "app": None, "steps": []}
    path = state.get("memcal_app")
    steps = [
        "Open System Settings → Privacy & Security → Full Disk Access.",
        (f"Find {Path(path).name}, or click + and select {path}; enable its switch."
         if path else "Find memcal.app and enable its switch."),
        "Approve any additional macOS confirmation yourself. Full Disk Access includes other apps' protected data, beyond enabled Memcal sources.",
        "The next bridge request starts a fresh Memcal app process to verify its grant.",
        "Run memcal permissions status to recheck; no messages are ingested by this helper.",
    ]
    return {**state, "selected_target": chosen, "app": path, "steps": steps}


def setup(cfg, *, target="auto", open_ui=True, runner=subprocess.run) -> dict:
    """Open Settings and reveal the chosen bundle; never change a privacy toggle."""
    if target not in TARGETS:
        raise ValueError("target must be auto or memcal")
    state = status(cfg)
    plan = guide(cfg, state, target)
    plan["opened"] = False
    plan["notes"] = []
    chosen = plan["selected_target"]
    verified = state.get("ready")
    plan["target_ready"] = bool(verified)
    if verified:
        plan["steps"] = []
        plan["detail"] = "Memcal app bridge reads already work; no additional grant is needed."
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
