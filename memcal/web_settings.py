"""What the settings tab shows, and the two writes it is allowed to make.

`settings.py` owns the schema and the file. This owns everything the page arranges
around it: whether the configured provider is actually there, where the store lives,
what each source still needs, and what the nightly agent is doing. Those last two can
touch the network or a subprocess, so they are a second request the page makes after it
has already drawn — see `probe`.
"""

from __future__ import annotations

import shutil
import sqlite3
import threading
from pathlib import Path

from . import llm, schedule, settings
from .config import Config


#: Serializes source toggles: each one reads the live disabled set, flips one
#: name, and writes it back, so two rapid flips cannot compute from the same
#: set and have the second overwrite the first.
_SOURCE_LOCK = threading.Lock()


def _bytes(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _chosen(cfg: Config) -> str:
    """The provider, named the way every table that answers for it is keyed.

    `llm.provider_status` and `client_for` both casefold before looking anything up, so
    a hand-written `MEMCAL_LLM_PROVIDER=Codex` runs perfectly well. Not doing the same
    here left every model box with nothing to suggest and no hint why.
    """
    return str(getattr(cfg, "llm_provider", "") or llm.DEFAULT_PROVIDER).strip().lower()


def provider(cfg: Config) -> dict:
    """The configured runtime, and whether it is reachable from this process."""
    name = _chosen(cfg)
    try:
        ok, detail = llm.provider_status(cfg)
    except Exception as exc:            # an unknown provider is a setting, not a crash
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    return {"name": name, "ok": bool(ok), "detail": detail,
            "default_model": llm.PROVIDER_DEFAULT_MODELS.get(name, ""),
            "needs_key": name in {"openrouter", "openai-compatible"}}


def store(cfg: Config) -> dict:
    """Where everything this store owns actually is, as paths you can go and look at."""
    return {
        "home": str(cfg.home),
        "db": str(cfg.db_path),
        "db_bytes": _bytes(cfg.db_path),
        "brief": str(cfg.brief_path),
        "brief_bytes": _bytes(cfg.brief_path),
        "wiki": str(cfg.wiki_dir),
        "plugins": str(cfg.plugin_dir),
    }


#: Model settings managed by the shared selector.
MODEL_KEYS = ("MEMCAL_PROPOSE_MODEL", "MEMCAL_SWEEP_MODEL", "MEMCAL_MATCH_MODEL")


def _models_for(name: str, roster: list[str] | None = None) -> list[dict]:
    """List this provider's models with price or subscription notes."""
    priced = dict(llm.catalog(name))
    out = []
    for native in (roster if roster is not None else list(priced)):
        priced_as = priced.get(native, native)
        rate = llm.rates(priced_as)
        note = f"${rate[0]:.2f}/M in · ${rate[1]:.2f}/M out" if rate else ""
        if llm.endpoint(priced_as).service_tier == "flex":
            note += " · flex tier"
        if not note and name in llm.PROVIDER_COMMANDS:
            note = "billed against your subscription"
        if llm.AGY_EFFORT_SUFFIX.search(native):
            note = (note + " · " if note else "") + "effort is in the name"
        out.append({"value": native, "note": note})
    return out


def _all_known_models() -> list[str]:
    """Every model name any provider's roster names, across all of them."""
    seen: list[str] = []
    for name in llm.PROVIDER_DEFAULT_MODELS:
        for native, _priced in llm.catalog(name):
            if native not in seen:
                seen.append(native)
    return seen


def _models_used(conn, name: str) -> list[dict]:
    """Models used by this store that are compatible with the selected provider."""
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """SELECT model, COUNT(*) AS n, MAX(created_at) AS last
                 FROM generations WHERE model IS NOT NULL AND model != ''
                GROUP BY model ORDER BY last DESC LIMIT 12""").fetchall()
    except sqlite3.Error:
        return []
    return [{"value": row["model"],
             "note": f"used here · {row['n']} call{'' if row['n'] == 1 else 's'}"}
            for row in rows if not llm.belongs_elsewhere(name, row["model"])]


def _calendars_seen(conn) -> list[dict]:
    """Calendars memcal has read. Naming one it already knows beats guessing at spelling.

    Deliberately not a live Calendar.app enumeration: a real read can open the macOS
    consent dialog, and drawing a settings page must never do that on its own.
    """
    if conn is None:
        return []
    try:
        rows = conn.execute(
            """SELECT calendar_name AS name, COUNT(*) AS n FROM calendar_items
                GROUP BY calendar_name ORDER BY n DESC LIMIT 20""").fetchall()
    except sqlite3.Error:
        return []
    return [{"value": row["name"], "note": f"{row['n']} items seen"} for row in rows]


def _dedupe(*groups: list[dict]) -> list[dict]:
    """First mention of a value wins, so the most specific note is the one shown."""
    seen, out = set(), []
    for group in groups:
        for row in group:
            if row["value"] and row["value"] not in seen:
                seen.add(row["value"])
                out.append(row)
    return out


def previewed_provider(cfg: Config, provider_name: str = "") -> str:
    """The provider the page is asking about: the one it has picked, or the saved one."""
    previewed = str(provider_name or "").strip().lower()
    return previewed if previewed in llm.PROVIDER_DEFAULT_MODELS else _chosen(cfg)


def models(cfg: Config, conn=None, provider_name: str = "",
           roster: list[str] | None = None) -> dict:
    """Return the shared model-selector state for one provider."""
    name = previewed_provider(cfg, provider_name)
    default = llm.PROVIDER_DEFAULT_MODELS.get(name, "")
    options = _dedupe(
        [{"value": default, "note": f"{name} default"}] if default else [],
        _models_used(conn, name), _models_for(name, roster))
    return {
        "provider": name,
        "default": default,
        "keys": list(MODEL_KEYS),
        "options": options,
        "closed": llm.serves(name, "not-a-real-model") is False,
        "known": [o["value"] for o in options],
        "foreign": {model: owner for model, owner in (
            (m, llm.belongs_elsewhere(name, m))
            for m in _all_known_models()) if owner},
        "effort_in_name": name == "antigravity",
    }


def suggestions(cfg: Config, conn=None, provider_name: str = "",
                roster: list[str] | None = None) -> dict[str, list[dict]]:
    """The usual answers for every field that is free text but rarely arbitrary.

    `provider_name` previews a provider the form has selected but not saved, so the
    model list answers the question being asked rather than the one already settled.
    """
    chosen = models(cfg, conn, provider_name, roster)
    found: dict[str, list[dict]] = {key: chosen["options"] for key in MODEL_KEYS}
    # An absolute path is what the nightly agent needs, and `which` already knows it.
    for key, command in (("MEMCAL_CODEX_COMMAND", cfg.codex_command),
                         ("MEMCAL_CLAUDE_COMMAND", cfg.claude_command),
                         ("MEMCAL_AGY_COMMAND", cfg.agy_command),
                         ("MEMCAL_GROK_COMMAND", cfg.grok_command)):
        bare = Path(command).name or command
        resolved = shutil.which(bare)
        found[key] = _dedupe(
            [{"value": resolved, "note": "found on your PATH"}] if resolved else [],
            [{"value": bare, "note": "whatever PATH resolves at run time"}])
    calendars = _calendars_seen(conn)
    found["MEMCAL_PUBLISH_CALENDAR"] = _dedupe(
        calendars, [{"value": "memcal", "note": "a calendar of its own, as documented"}])
    found["MEMCAL_PUBLISH_REMINDERS"] = _dedupe(
        [{"value": "memcal", "note": "a list of its own, as documented"}])
    found["MEMCAL_OPENAI_BASE_URL"] = _dedupe(
        [{"value": cfg.openai_base_url, "note": "current endpoint"}]
        if cfg.openai_base_url else [],
        [{"value": "https://host/v1", "note": "replace host with your API endpoint"}])
    return found


def live_roster(cfg: Config, provider_name: str = "") -> list[str] | None:
    """Ask the provider itself what it serves. None when it has nothing to ask."""
    roster, _source = llm.list_models(cfg, previewed_provider(cfg, provider_name))
    return roster or None


def page(cfg: Config, conn=None, provider_name: str = "",
         roster: list[str] | None = None) -> dict:
    """Everything the tab can draw without touching the network or a subprocess."""
    return {**settings.snapshot(cfg),
            "provider": provider(cfg),
            "credentials": settings.credentials(cfg),
            "suggestions": suggestions(cfg, conn, provider_name, roster),
            "models": models(cfg, conn, provider_name, roster),
            "store": store(cfg)}


def probe(cfg: Config) -> dict:
    """The slow half: a source check may open a socket, and launchctl is a subprocess."""
    from . import sources                                          # noqa: PLC0415
    off = sources.disabled_set(cfg)
    found = []
    for source in sources.all_sources(cfg):
        try:
            ok, detail = source.check(cfg)
        except Exception as exc:      # a third-party plugin is not trusted to be tidy
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        found.append({"name": source.name, "description": source.description,
                      "usable": bool(ok), "detail": detail,
                      "in_all": bool(getattr(source, "in_all", True)),
                      "enabled": source.name.lower() not in off,
                      "secrets": list(getattr(source, "secrets", ()) or ())})
    try:
        nightly = schedule.status(cfg)
    except Exception as exc:          # launchd is macOS; a Linux store is not broken
        nightly = {"error": f"{type(exc).__name__}: {exc}"}
    roster, roster_from = llm.list_models(cfg, _chosen(cfg))
    return {"sources": found, "load_errors": list(sources.load_errors()),
            "plugin_dir": str(cfg.plugin_dir), "schedule": nightly,
            "disabled": sorted(off),
            "roster": {"provider": _chosen(cfg), "models": roster,
                       "source": roster_from}}


def _apply_source_toggle(cfg: Config, spec: dict) -> dict:
    """Flip one source on or off, storing the answer as MEMCAL_DISABLED_SOURCES."""
    from . import sources                                          # noqa: PLC0415
    if not isinstance(spec, dict):
        raise settings.SettingsError("source takes {name, enabled}")
    name = str(spec.get("name", "")).strip().lower()
    if not name:
        raise settings.SettingsError("source takes {name, enabled}")
    known = {s.name.lower(): s.name for s in sources.all_sources(cfg)}
    if name not in known:
        raise settings.SettingsError(
            f"no source named {spec.get('name', '')!r}. "
            f"Known: {', '.join(sorted(known.values())) or 'none'}")
    enabled = spec.get("enabled")
    if not isinstance(enabled, bool):
        raise settings.SettingsError(
            f"source {known[name]} takes enabled true or false, not {enabled!r}")
    with _SOURCE_LOCK:
        off = set(sources.disabled_set(cfg))
        if enabled:
            off.discard(name)
        else:
            off.add(name)
        saved = settings.save(cfg, {"MEMCAL_DISABLED_SOURCES": ",".join(sorted(off))})
    return {"source": {"name": known[name], "enabled": enabled},
            **saved}


def save(cfg: Config, payload: dict, conn=None) -> dict:
    """One POST for the form, a credential, or a source toggle; none is implicit.

    A credential is write-only by construction: it goes in, and only its presence ever
    comes back out. A source toggle rewrites MEMCAL_DISABLED_SOURCES and answers with
    the page it just changed, so the tab can redraw without a second round trip.
    """
    out: dict = {}
    if payload.get("secret") is not None:
        secret = payload["secret"]
        if not isinstance(secret, dict):
            raise settings.SettingsError("secret takes {name, value}")
        out["secret"] = settings.save_credential(
            cfg, secret.get("name", ""), secret.get("value", ""))
    if payload.get("source") is not None:
        out.update(_apply_source_toggle(cfg, payload["source"]))
    if payload.get("changes") is not None:
        # A payload carrying both a toggle and form edits merges the receipts:
        # each save reports its own `saved`/`warnings`, and the later update
        # must not swallow the earlier one.
        saved = settings.save(cfg, payload["changes"])
        out["saved"] = [*out.get("saved", []), *saved.get("saved", [])]
        out["warnings"] = [*out.get("warnings", []), *saved.get("warnings", [])]
    if not out:
        raise settings.SettingsError("nothing to save")
    return {**out, **page(cfg, conn)}
