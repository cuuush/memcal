"""What happened to a pass, and how to put its traffic back in the queue.

`archive.spool_mark` stamps `processed_at` and the run id onto every line whose bundle
came back with a diff. That is the whole record of "this has been looked at", so a pass
that failed *after* reading some of its bundles leaves a queue missing what it read.

`requeue` is the undo for exactly that, and for nothing else.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from .. import archive, db

#: Writes on the board, and calls that raised. Something landed and something did not,
#: and which half matters is a question only the run detail can answer — so it is named
#: rather than being flattened into either "ok" or "failed".
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
    """How one `runs` row ended, in one word.

    Deliberately not "was there anything in the error column". `_finish` writes
    `result.errors` there, and builds before propose split failures from recoveries
    filed "re-asked 2 bundle(s)…" in it — read that way, most nightly passes are
    failures and the word stops meaning anything. Two structured columns say it
    properly: `diffs` is what landed, and `failed_calls` is completions that raised.
    """
    if str(row["mode"] or "") == "dry-run":
        return PRICED
    if not row["finished_at"] and not row["error"]:
        return RUNNING
    if (row["diffs"] or 0) <= 0 and (row["bundles"] or 0) > 0 and row["error"]:
        return FAILED
    # NULL is "not recorded" — a run older than the column — and is not evidence of a
    # failure. Counting it as one would retro-flag every pass from before it existed.
    if (row["failed_calls"] or 0) > 0:
        return PARTIAL
    return OK


def retryable(row) -> bool:
    """Whether re-running this pass would read anything.

    A priced run called nothing, and a running one still might. Everything else is
    retryable when it had bundles at all — a pass that found nothing to read has nothing
    to re-read, and offering to retry it is offering to do nothing twice.
    """
    return outcome(row) in (FAILED, PARTIAL) and (row["bundles"] or 0) > 0


def claimed(conn: sqlite3.Connection, run_id: int) -> int:
    """How many spooled lines this run marked as read."""
    return int(conn.execute(
        "SELECT count(*) n FROM spool WHERE run_id = ?", (run_id,)).fetchone()["n"])


def requeue(conn: sqlite3.Connection, run_id: int) -> tuple[int, int]:
    """Put back what this run claimed *and a pass could still read*. `(released, kept)`.

    Only lines inside the model horizon. `_dream` retires everything older than
    `archive.SPOOL_HORIZON_DAYS` as its first step, so releasing an old line does not
    get it re-read — it gets it re-filed as retired-*unread*, erasing the one record
    that a pass read it. Those stay claimed, and `kept` counts them.

    Both columns are cleared for the rest: leaving the run id behind would have the
    queue view blame a waiting line on the pass that failed to read it.
    """
    cutoff = (db.today() - timedelta(days=archive.SPOOL_HORIZON_DAYS)).isoformat()[:10]
    cur = conn.execute(
        """UPDATE spool SET processed_at = NULL, run_id = NULL
            WHERE run_id = ? AND archive_id IN
              (SELECT id FROM archive WHERE substr(ts, 1, 10) >= ?)""",
        (run_id, cutoff))
    released = cur.rowcount
    conn.commit()
    return released, claimed(conn, run_id)
