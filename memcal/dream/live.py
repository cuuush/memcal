"""Cross-process live progress for a dream pass.

A pass started on the CLI is invisible to the web UI: `web_jobs` only tracks jobs
the web server itself started. Every pass — CLI, web, or scheduled — therefore
also reports here, so the Dream tab can draw a pass it did not start.

Writes go out on a fresh connection per call and commit immediately. That is the
whole point: the pass's main connection sits inside long transactions and behind
minutes of model waits, and a reader must see progress while that is happening,
not when it commits. Each call opens, writes, and closes, which also keeps this
safe to call from propose's worker threads with no shared state.

Nothing here may ever break a pass: every entry point swallows its own errors.
A silent live feed is a missing panel; a loud one is a failed dream.
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

from .. import db


#: Only these happen often enough to matter. Stage transitions are a handful per
#: pass; waves are a handful; requests are bounded by packing (tens, not hundreds).
#: No throttling: one row per event is already the throttled form.
def _conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=db.BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {db.BUSY_TIMEOUT_MS}")
    return conn


class LiveFeed:
    """Progress sink for one pass. Attach once the run row exists, then feed events."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.run_id: int | None = None

    def _write(self, fn) -> None:
        try:
            conn = _conn(self.db_path)
        except Exception:
            return
        try:
            fn(conn)
            conn.commit()
        except Exception:
            with contextlib.suppress(Exception):
                conn.rollback()
        finally:
            with contextlib.suppress(Exception):
                conn.close()

    def attach(self, run_id: int, bundles: list) -> None:
        """Record the run's bundle plan. Idempotent: re-attaching replaces it."""
        self.run_id = run_id

        def _attach(conn: sqlite3.Connection) -> None:
            from . import propose as propose_stage

            conn.execute("DELETE FROM run_bundles WHERE run_id = ?", (run_id,))
            now = db.now()
            conn.executemany(
                """INSERT INTO run_bundles
                     (run_id, entity, bundle_id, label, kind, lines, state, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)""",
                [(run_id, b.entity, propose_stage.bundle_id(b.entity), b.label,
                  b.entity.split(":", 1)[0], len(b.items), now)
                 for b in bundles],
            )
            _prune(conn)

        self._write(_attach)

    def event(self, event: str, data: dict) -> None:
        """One progress callback event, as `dream()` receives it."""
        if self.run_id is None:
            return
        run_id = self.run_id

        def _event(conn: sqlite3.Connection) -> None:
            entities = _entities(data)
            if event == "propose_wave":
                _mark(conn, run_id, entities, "reading")
            elif event == "propose_request":
                _mark(conn, run_id, entities,
                      "done" if data.get("ok") else "failed")
            conn.execute(
                """INSERT INTO run_events
                     (run_id, at, event, stage, state, note, done, total,
                      label, entities, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, db.now(), event, str(data.get("stage") or ""),
                 str(data.get("state") or ""), str(data.get("note") or ""),
                 int(data.get("done") or 0), int(data.get("total") or 0),
                 str(data.get("label") or ""), db.jdump(entities),
                 str(data.get("error") or "")),
            )

        self._write(_event)


def _entities(data: dict) -> list[str]:
    """Bundle entities one event is about, in a stable order."""
    found = data.get("entities") or []
    if isinstance(found, str):
        found = [found]
    return sorted({str(e) for e in found if e})


def _mark(conn: sqlite3.Connection, run_id: int, entities: list[str],
          state: str) -> None:
    """Move bundles between queued / reading / done / failed. Unknown entities
    (a wave re-send naming something the plan did not list) are ignored."""
    if not entities:
        return
    now = db.now()
    for entity in entities:
        conn.execute(
            "UPDATE run_bundles SET state = ?, updated_at = ?"
            " WHERE run_id = ? AND entity = ?",
            (state, now, run_id, entity))


def _prune(conn: sqlite3.Connection) -> None:
    """Keep the feed to the recent past. The finished pass itself lives on
    `runs` and `generations`; this table is only ever read for the live one."""
    with contextlib.suppress(sqlite3.Error):
        conn.execute(
            """DELETE FROM run_events WHERE run_id NOT IN
                 (SELECT id FROM runs ORDER BY id DESC LIMIT 40)""")
        conn.execute(
            """DELETE FROM run_bundles WHERE run_id NOT IN
                 (SELECT id FROM runs ORDER BY id DESC LIMIT 40)""")
