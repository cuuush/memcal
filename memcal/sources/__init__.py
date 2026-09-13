"""Source registry and plugin discovery.

Sources register via three mechanisms:
  1. Built-in modules in this package.
  2. Python files in `~/.memcal/plugins/*.py`.
  3. Installed distributions advertising a `memcal.sources` entry point.

All sources populate the same registry and present the same CLI interface.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import sys
import traceback
from pathlib import Path
from typing import Callable

from .base import (HttpError, IngestReport, deliver, get_json, post_json,  # noqa: F401
                   set_watermark, watermark)
from .spec import Source, SourceError  # noqa: F401

BUILTINS = ("bluebubbles", "imessage", "whatsapp", "groupme", "slack", "telegram",
            "signal", "proton", "ical")
ENTRY_POINT_GROUP = "memcal.sources"

_registry: dict[str, Source] = {}
_load_errors: list[str] = []
_discovered = False


def register(source_class):
    """Register a source class. Also usable directly: register(MySource)."""
    instance = source_class() if isinstance(source_class, type) else source_class
    if not getattr(instance, "name", ""):
        raise ValueError(f"{source_class!r} needs a name")
    _registry[instance.name] = instance
    return source_class


def discover(cfg=None, *, force: bool = False) -> dict[str, Source]:
    """Load built-ins, installed plugins, and local plugin files. Idempotent."""
    global _discovered
    if _discovered and not force:
        return _registry
    _discovered = True

    for name in BUILTINS:
        try:
            importlib.import_module(f"{__name__}.{name}")
        except Exception as exc:
            _load_errors.append(f"builtin {name}: {type(exc).__name__}: {exc}")

    _load_entry_points()
    if cfg is not None:
        load_plugin_dir(cfg.plugin_dir)
    return _registry


def _load_entry_points() -> None:
    try:
        from importlib.metadata import entry_points
    except ImportError:  # pragma: no cover
        return
    try:
        found = entry_points(group=ENTRY_POINT_GROUP)
    except TypeError:  # pragma: no cover
        found = entry_points().get(ENTRY_POINT_GROUP, [])
    for entry in found:
        try:
            loaded = entry.load()
            if isinstance(loaded, type) and issubclass(loaded, Source):
                register(loaded)
        except Exception as exc:
            _load_errors.append(f"entry point {entry.name}: {type(exc).__name__}: {exc}")


def load_plugin_dir(directory: Path) -> None:
    """Import all Python files in the plugin directory. Load failures are recorded and non-fatal."""
    if not directory or not directory.is_dir():
        return
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        module_name = f"memcal_plugin_{path.stem}"
        if module_name in sys.modules:
            continue
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except Exception as exc:
            _load_errors.append(f"plugin {path.name}: {type(exc).__name__}: {exc}")
            if "MEMCAL_DEBUG" in __import__("os").environ:
                traceback.print_exc()


def get(name: str, cfg=None) -> Source | None:
    discover(cfg)
    return _registry.get(name)


def names(cfg=None, *, in_all_only: bool = False) -> list[str]:
    discover(cfg)
    items = sorted(_registry.values(), key=lambda s: (s.order, s.name))
    return [s.name for s in items if not in_all_only or s.in_all]


def all_sources(cfg=None) -> list[Source]:
    discover(cfg)
    return sorted(_registry.values(), key=lambda s: (s.order, s.name))


def load_errors() -> list[str]:
    return list(_load_errors)


# Maximum rounds an ingest run executes to bring a stale source current.
DEFAULT_ROUNDS = 25


def catch_up(source: Source, conn, cfg, *, limit: int = 1000,
             rounds: int = DEFAULT_ROUNDS,
             progress: Callable[[str], None] | None = None,
             collection_id: int | None = None) -> IngestReport:
    """Ingest repeatedly until the source reports no further records or rounds exhaust.

    `limit` sets page size per request. Ingest continues while the source indicates
    additional records remain, stopping when exhausted or when the round cap is reached.

    This layer owns the final aggregate outcome: intermediate pages stamp rows with
    `collection_id` but never finalize `collection_sources` (new plugins via
    `record=False`); the all-page totals are persisted exactly once at the end, so a
    quiet final page cannot erase earlier pages and a failure preserves its partial
    counts. Legacy plugins without the `record` flag still write per page; the final
    overwrite corrects them.
    """
    from .. import archive as _archive

    try:
        accepted = inspect.signature(source.run).parameters
    except (TypeError, ValueError):
        accepted = {}
    supports_record = "record" in accepted

    def run_once() -> IngestReport:
        # Inspect source signature to pass only supported keyword arguments for backward compatibility.
        extra = {}
        if progress is not None and "progress" in accepted:
            extra["progress"] = progress
        if collection_id is not None and "collection_id" in accepted:
            extra["collection_id"] = collection_id
        if supports_record:
            # Suppress per-page finalization; `catch_up` persists the aggregate once.
            extra["record"] = False
        return source.run(conn, cfg, limit=limit, **extra)

    try:
        total = run_once()
    except Exception as exc:  # a custom `run` that raises instead of reporting
        total = IngestReport(stream=getattr(source, "name", ""))
        total.error = f"{type(exc).__name__}: {exc}"
        total.notes.append(f"collecting {total.stream} failed before it started: {exc}")
        used, stalled = 0, False
    else:
        used, stalled = 1, False
        while total.more and not total.error and used < rounds:
            total.more = False
            try:
                this_round = run_once()
            except Exception as exc:
                total.error = total.error or f"{type(exc).__name__}: {exc}"
                total.notes.append(f"collecting {total.stream} failed mid-pass: {exc}")
                break
            total.absorb(this_round)
            used += 1
            if total.error:
                break
            if not this_round.archived and this_round.more:
                # No new rows while more work remains: incomplete, not exhausted.
                # Keep `more` set; a quiet exhausted page (no rows, no more) stays
                # complete and exits the loop naturally below.
                total.more, stalled = True, True
                break

    if total.error:
        pass  # failure preserves partial counts; status derives from the error
    elif stalled:
        total.notes.append(f"stopped after {used} rounds — the last one added nothing "
                           f"(rate limited, or genuinely done)")
    elif total.more:
        total.notes.append(f"stopped at {rounds} rounds with more waiting — "
                           f"run again, or raise --rounds")
    elif used > 1:
        total.notes.append(f"caught up over {used} rounds")
    if collection_id:
        try:
            _archive.record_source(conn, collection_id, total)
        except Exception:
            pass
    return total
