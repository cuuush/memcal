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
    #: ``"stream"`` (the default): new archive rows. A message stream that has delivered
    #: nothing for days is behind, whatever its last run reported, and saying so is the
    #: entire job of `archive.stale_streams`.
    #:
    #: ``"snapshot"``: a successful read. A calendar with no changes is healthy and must
    #: not have to manufacture an archive row to prove it.
    #:
    #: Getting this backwards in either direction costs a real failure. Every source
    #: writing `source.<stream>.last_success` is what lets a closed Proton Bridge go
    #: stale within two days; letting that marker *override* the data is what let
    #: iMessage sit eight days behind while `stale_streams()` returned nothing and the
    #: brief reported no gap at all.
    health: str = "stream"

    def fetch(self, conn: sqlite3.Connection, cfg: Config, report: IngestReport,
              limit: int) -> None:
        """Fetch new items and pass each to `deliver()`. Raise SourceError to fail cleanly.

        Use `watermark(conn, key)` / `set_watermark(conn, key, value)` to resume rather
        than re-reading everything; every item is deduplicated on
        (stream, external_id) anyway, so a replay is safe but wasteful.
        """
        raise NotImplementedError

    def check(self, cfg: Config) -> tuple[bool, str]:
        """Is this source usable right now? Reported by `memcal sources` and `doctor`."""
        missing = [s for s in self.secrets if not cfg.secret(s, s.lower())]
        if missing:
            return False, f"missing credential: {', '.join(missing)}"
        return True, "ready"

    # ------------------------------------------------------------------ runner --
    def run(self, conn: sqlite3.Connection, cfg: Config, *, limit: int = 1000,
            progress: Callable[[str], None] | None = None,
            collection_id: int | None = None) -> IngestReport:
        """Wraps fetch so one broken plugin can never take down a whole `ingest all`."""
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
            # A source that ran and found nothing is healthy; a source that has not run
            # for a month is not; and until this was written here, both looked identical
            # from the outside because freshness was measured off the newest *message*.
            # A Proton Bridge that is closed reports an error, writes no marker, and the
            # email stream now goes stale within two days instead of reading as a quiet
            # inbox — which is the whole of what "was the Bridge open?" needs.
            #
            # `freshness()` has honoured `source.<stream>.last_success` since it was
            # added for snapshot sources; only `ical` ever wrote one. Every source
            # writing it is what turns a per-source convention into a health signal.
            if not report.error:
                try:
                    db.set_meta(conn, f"source.{self.name}.last_success", db.now())
                except sqlite3.Error:
                    pass
            # Recorded whether it worked or not — especially when it did not, since a
            # source that failed is the case with nothing else to show for itself.
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
