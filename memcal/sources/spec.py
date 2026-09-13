"""What a source is."""

from __future__ import annotations

import sqlite3
from typing import Callable

from .. import db
from ..config import Config
from .base import IngestReport, adapt_progress


class Source:
    """Base class for every stream. Subclasses override `name` and `fetch`."""

    #: CLI name — `memcal ingest <name>`. Also the `stream` column in the archive.
    name: str = ""
    #: One line, shown by `memcal sources`.
    description: str = ""
    #: Credential aliases this source looks for; used by `memcal sources` / `doctor`.
    secrets: tuple[str, ...] = ()
    #: Included in `memcal ingest all`. Set False for anything slow or interactive.
    in_all: bool = True
    #: Sources are polled cheapest-first so identity is resolved before it is needed.
    order: int = 50
    #: What proves this source healthy — the data, or the read.
    #:
    #: ``"stream"`` (default): new archive rows. ``"snapshot"``: a successful
    #: read. The mode must match the source shape or staleness is misreported.
    health: str = "stream"

    def fetch(self, conn: sqlite3.Connection, cfg: Config, report: IngestReport,
              limit: int) -> None:
        """Fetch new items and pass each to `deliver()`. Raise SourceError to fail cleanly.

        Use `watermark(conn, key)` / `set_watermark(conn, key, value)` to resume rather
        than re-reading everything; every item is deduplicated on
        (stream, external_id) anyway, so a replay is safe but wasteful.
        """
        raise NotImplementedError

    def setup(self, cfg: Config) -> tuple[bool, str]:
        """One-time interactive sign-in. Reached by `memcal login <source>`.

        Most sources don't need this — a credential in .env is the whole setup. Telegram
        and Signal link a device (phone and code, or a QR scan), which can't happen
        inside the nightly pass.
        """
        return False, f"{self.name} needs no interactive login — see `memcal sources`"

    def check(self, cfg: Config) -> tuple[bool, str]:
        """Is this source usable right now? Reported by `memcal sources` and `doctor`."""
        missing = [s for s in self.secrets if not cfg.secret(s, s.lower())]
        if missing:
            return False, f"missing credential: {', '.join(missing)}"
        return True, "ready"

    # ------------------------------------------------------------------ runner --
    def run(self, conn: sqlite3.Connection, cfg: Config, *, limit: int = 1000,
            progress: Callable[[str], None] | None = None,
            collection_id: int | None = None,
            record: bool = True) -> IngestReport:
        """Wraps fetch so one broken plugin can never take down a whole `ingest all`.

        `collection_id` stamps archived rows for queue grouping. `record` controls
        whether this page writes `collection_sources`: `catch_up` passes
        `record=False` for intermediate pages and finalizes the all-page aggregate
        once itself, so a quiet last page cannot erase earlier pages.
        """
        report = IngestReport(stream=self.name,
                              horizon_days=getattr(cfg, "spool_horizon_days",
                                                   IngestReport.horizon_days),
                              progress=adapt_progress(progress),
                              collection_id=collection_id)
        try:
            self.fetch(conn, cfg, report, limit)
        except SourceError as exc:
            report.error = str(exc)
        except Exception as exc:  # a third-party plugin is not trusted to be tidy
            report.error = f"{type(exc).__name__}: {exc}"
        finally:
            # Record success markers for freshness; record the collection
            # regardless of outcome.
            if not report.error:
                try:
                    db.set_meta(conn, f"source.{self.name}.last_success", db.now())
                except sqlite3.Error:
                    pass
            # Recorded whether it worked or not — especially when it did not, since a
            # source that failed is the case with nothing else to show for itself.
            # Intermediate `catch_up` pages skip this; the orchestrator finalizes once.
            if record:
                try:
                    from .. import archive                          # noqa: PLC0415
                    archive.record_source(conn, collection_id, report)
                except sqlite3.Error:
                    pass
            try:
                conn.commit()
            except sqlite3.Error:
                pass
        return report


class SourceError(RuntimeError):
    """Expected, explainable failure: no credential, service down, endpoint disabled."""
