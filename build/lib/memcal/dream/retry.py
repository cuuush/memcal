"""Dream-run outcomes and selective spool requeueing."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from .. import archive, db

PARTIAL = "partial"
FAILED = "failed"
RUNNING = "running"
OK = "ok"
PRICED = "priced"

OUTCOME_LABELS = {
    OK: "ok",
    PARTIAL: "partial",
    FAILED: "failed",
    RUNNING: "running",
    PRICED: "priced only",
}


def outcome(row) -> str:
    """Classify a run from its writes and failed calls, not diagnostic text alone."""
    if str(row["mode"] or "") == "dry-run":
        return PRICED
    if not row["finished_at"] and not row["error"]:
        return RUNNING
    if (row["diffs"] or 0) <= 0 and (row["bundles"] or 0) > 0 and row["error"]:
        return FAILED
    # NULL predates failed-call tracking and is not evidence of failure.
    if (row["failed_calls"] or 0) > 0:
        return PARTIAL
    return OK


def retryable(row) -> bool:
    """Whether a completed problem run had bundles to re-read."""
    return outcome(row) in (FAILED, PARTIAL) and (row["bundles"] or 0) > 0


def claimed(conn: sqlite3.Connection, run_id: int) -> int:
    """How many spooled lines this run marked as read."""
    return int(conn.execute(
        "SELECT count(*) n FROM spool WHERE run_id = ?", (run_id,)).fetchone()["n"])


def requeue(conn: sqlite3.Connection, run_id: int) -> tuple[int, int]:
    """Release readable lines claimed by a run; return `(released, too_old)`."""
    cutoff = (db.today() - timedelta(days=archive.SPOOL_HORIZON_DAYS)).isoformat()[:10]
    cur = conn.execute(
        """UPDATE spool SET processed_at = NULL, run_id = NULL
            WHERE run_id = ? AND archive_id IN
              (SELECT id FROM archive WHERE substr(ts, 1, 10) >= ?)""",
        (run_id, cutoff))
    released = cur.rowcount
    conn.commit()
    return released, claimed(conn, run_id)
