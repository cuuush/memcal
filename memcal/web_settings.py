"""What the settings tab shows, and the two writes it is allowed to make.

`settings.py` owns the schema and the file. This owns everything the page arranges
around it: whether the configured provider is actually there, where the store lives,
what each source still needs, and what the nightly agent is doing. Those last two can
touch the network or a subprocess, so they are a second request the page makes after it
has already drawn — see `probe`.
"""

from __future__ import annotations

from pathlib import Path

from . import llm, schedule, settings
from .config import Config


def _bytes(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def provider(cfg: Config) -> dict:
    """The configured runtime, and whether it is reachable from this process."""
    name = str(getattr(cfg, "llm_provider", "") or llm.DEFAULT_PROVIDER)
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


def page(cfg: Config) -> dict:
    """Everything the tab can draw without touching the network or a subprocess."""
    return {**settings.snapshot(cfg),
            "provider": provider(cfg),
            "credentials": settings.credentials(cfg),
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
    return {"sources": found, "load_errors": list(sources.load_errors()),
            "plugin_dir": str(cfg.plugin_dir), "schedule": nightly}


def save(cfg: Config, payload: dict) -> dict:
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
    return {**out, **page(cfg)}
