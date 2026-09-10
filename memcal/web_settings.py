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
from pathlib import Path

from . import llm, schedule, settings
from .config import Config


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
            "needs_key": name == "openrouter"}


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


#: The three settings that name a model. One list serves all of them, and one control
#: on the page can set all of them at once — they are nearly always the same choice.
MODEL_KEYS = ("MEMCAL_PROPOSE_MODEL", "MEMCAL_SWEEP_MODEL", "MEMCAL_MATCH_MODEL")


def _models_for(name: str, roster: list[str] | None = None) -> list[dict]:
    """The models this provider serves, priced where memcal has a price on file.

    `roster` is a live listing when one could be had — see `llm.list_models` — and the
    static catalog otherwise. Either way every entry here is something the chosen
    provider will actually accept, which is the point: the previous version merged
    memcal's whole price table into every provider's list, so Codex was offered
    `gemini-3.8-flash-high`, and a pass configured that way had all 74 of its bundles
    refused with "not supported when using Codex with a ChatGPT account".
    """
    priced = dict(llm.catalog(name))
    out = []
    for native in (roster if roster is not None else list(priced)):
        priced_as = priced.get(native, native)
        rate = llm.rates(priced_as)
        note = f"${rate[0]:.2f}/M in · ${rate[1]:.2f}/M out" if rate else ""
        if llm.endpoint(priced_as).service_tier == "flex":
            note += " · flex tier"
        if not note and name in llm.PROVIDER_COMMANDS:
            # A CLI backend bills against a subscription, so there is no per-token price
            # to be missing. Saying nothing here reads as "we forgot"; saying this is the
            # actual answer.
            note = "billed against your subscription"
        # Antigravity states its reasoning budget in the model name, so the effort
        # setting has nothing to add for one of these. Worth saying beside the name
        # rather than only in the effort row three fields down.
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
    """Models this store has run, minus any that belong to a different provider.

    "Used here" is a strong recommendation and it was being made across providers: a
    model this store ran happily under Antigravity was offered, carrying that
    endorsement, to someone configuring Codex — which refuses it.
    """
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
    """Everything the page needs to offer a model choice for one provider.

    Kept together rather than spread across `suggestions`, because the three model
    fields, the "set all three" control and the warning about a model this provider
    cannot serve are all answers to the same question and they have to agree.
    """
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
        # Whether this provider has a roster at all. OpenRouter routes far more than
        # this repo prices, so nothing there is ever shaded as unrecognised.
        "closed": llm.serves(name, "not-a-real-model") is False,
        "known": [o["value"] for o in options],
        # Models memcal recognises as *another* provider's. Not merely absent from this
        # one's list — these rosters lag every release, and a model nobody recognises is
        # a new one far more often than it is a mistake. This is the set a save actually
        # refuses, and the set the form moves off a field when the provider changes.
        "foreign": {model: owner for model, owner in (
            (m, llm.belongs_elsewhere(name, m))
            for m in _all_known_models()) if owner},
        # Antigravity says its budget in the model name, and `--effort` is not passed
        # when it does. The effort row has to say so or it reads as being ignored.
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
                         ("MEMCAL_AGY_COMMAND", cfg.agy_command)):
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
    found = []
    for source in sources.all_sources(cfg):
        try:
            ok, detail = source.check(cfg)
        except Exception as exc:      # a third-party plugin is not trusted to be tidy
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        found.append({"name": source.name, "description": source.description,
                      "usable": bool(ok), "detail": detail,
                      "in_all": bool(getattr(source, "in_all", True)),
                      "secrets": list(getattr(source, "secrets", ()) or ())})
    try:
        nightly = schedule.status(cfg)
    except Exception as exc:          # launchd is macOS; a Linux store is not broken
        nightly = {"error": f"{type(exc).__name__}: {exc}"}
    # Asking the provider what it serves runs `agy models`, which is a subprocess and a
    # network round trip — exactly the kind of thing this half exists for. The page has
    # already drawn with the static roster by the time this lands.
    roster, roster_from = llm.list_models(cfg, _chosen(cfg))
    return {"sources": found, "load_errors": list(sources.load_errors()),
            "plugin_dir": str(cfg.plugin_dir), "schedule": nightly,
            "roster": {"provider": _chosen(cfg), "models": roster,
                       "source": roster_from}}


def save(cfg: Config, payload: dict, conn=None) -> dict:
    """One POST for the form and for a credential; neither is implicit.

    A credential is write-only by construction: it goes in, and only its presence ever
    comes back out.
    """
    out: dict = {}
    if payload.get("secret") is not None:
        secret = payload["secret"]
        if not isinstance(secret, dict):
            raise settings.SettingsError("secret takes {name, value}")
        out["secret"] = settings.save_credential(
            cfg, secret.get("name", ""), secret.get("value", ""))
    if payload.get("changes") is not None:
        out.update(settings.save(cfg, payload["changes"]))
    if not out:
        raise settings.SettingsError("nothing to save")
    return {**out, **page(cfg, conn)}
