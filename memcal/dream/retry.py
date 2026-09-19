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


def resume_source(conn: sqlite3.Connection, home) -> dict | None:
    """The failed run whose spent propose calls are still queued, if any.

    A pass that dies before marking the spool read loses its propose outputs
    from memory, but the call files survive on disk. Returns the run, its
    progress, and the replay index — or None when there is nothing to resume:
    last run clean, queue already drained, or no reusable single-turn calls.
    """
    from . import propose as propose_stage                    # noqa: PLC0415
    row = conn.execute(
        "SELECT * FROM runs WHERE mode <> 'dry-run' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None or not row["error"]:
        return None
    run_id = row["id"]
    index = propose_stage.load_replay(conn, home, run_id)
    if not index:
        return None
    queued = {r[0] for r in conn.execute(
        "SELECT DISTINCT entity FROM spool WHERE processed_at IS NULL")}
    index = {entities: turns for entities, turns in index.items()
             if any(e in queued for e in entities)}
    if not index:
        return None
    covered = ({e for entities in index for e in entities}
               | {r[0] for r in conn.execute(
                   "SELECT DISTINCT entity FROM provenance WHERE run_id = ?",
                   (run_id,))})
    total = max(1, row["bundles"] or 0)
    wrote = conn.execute("SELECT COUNT(*) n FROM provenance WHERE run_id = ?",
                         (run_id,)).fetchone()["n"]
    stages = [r[0] for r in conn.execute(
        "SELECT stage FROM generations WHERE run_id = ? ORDER BY id", (run_id,))]
    last = stages[-1] if stages else ""
    if wrote:
        stage = "sweep or later"
    elif last == "sweep" or last == "wakes":
        stage = last
    elif last == "merge":
        stage = "apply"
    elif last == "propose" or last.startswith("propose:"):
        stage = "propose"
    elif stages:
        stage = last
    else:
        stage = "prepare"
    error = str(row["error"] or "").split("; ")[0][:120]
    return {
        "run_id": run_id,
        "mode": row["mode"],
        "started": str(row["started_at"] or "")[:16],
        "error": error,
        "stage": stage,
        "read": len(covered),
        "bundles": row["bundles"] or 0,
        "read_pct": round(100 * len(covered) / total),
        "wrote": wrote,
        "calls": sum(len(turns) for turns in index.values()),
        "index": index,
    }
