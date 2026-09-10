"""What happened to a pass, and how to put its traffic back in the queue.

`archive.spool_mark` stamps `processed_at` and the run id onto every line whose bundle
came back with a diff. That is the whole record of "this has been looked at", so a pass
that failed *after* reading some of its bundles leaves a queue that no longer holds
what it read.

`requeue` is the undo for exactly that, and for nothing else. A run that failed before
claiming anything releases nothing — the common case, since a provider refusing every
request never gets far enough to mark one.
"""

from __future__ import annotations

import sqlite3

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

    Deliberately not "was there anything in the error column". That column is not a
    reliable failure signal: `_finish` writes `result.errors` into it, and builds before
    propose split failures from recoveries filed "re-asked 2 bundle(s)…" there — so
    twenty historical passes that wrote dozens of rows each carry an error string. Read
    that way, most nightly passes are failures and the word stops meaning anything.

    Two structured columns say it properly. `diffs` is what landed, so an error with
    nothing on the board is a pass that spent its window and changed nothing.
    `failed_calls` is completions that raised, so writes *plus* raised calls is a pass
    where some conversations were genuinely not read.
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


def requeue(conn: sqlite3.Connection, run_id: int) -> int:
    """Put back everything this run claimed, so the next pass reads it again.

    Both columns are cleared, not just `processed_at`. Leaving the run id behind would
    have the queue view attribute a waiting line to the pass that failed to read it, and
    the next pass overwrites it anyway the moment it succeeds.
    """
    cur = conn.execute(
        "UPDATE spool SET processed_at = NULL, run_id = NULL WHERE run_id = ?",
        (run_id,))
    conn.commit()
    return cur.rowcount


def failed_runs(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Passes worth offering a retry for, newest first."""
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (max(1, limit) * 4,)).fetchall()
    out = []
    for row in rows:
        if not retryable(row):
            continue
        out.append({"id": row["id"], "at": str(row["started_at"])[:16],
                    "mode": row["mode"], "model": (row["model"] or "").split("/")[-1],
                    "outcome": outcome(row), "bundles": row["bundles"],
                    "items": row["items"], "diffs": row["diffs"],
                    "claimed": claimed(conn, row["id"]),
                    "error": row["error"] or ""})
        if len(out) >= limit:
            break
    return out
