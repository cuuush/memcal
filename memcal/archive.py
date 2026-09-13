"""Archive and spool storage.

Raw items are appended and full-text indexed. Items do not exist exclusively in
derived stores, allowing recovery from gating errors via search.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from . import db


def append(
    conn: sqlite3.Connection,
    *,
    stream: str,
    external_id: str,
    ts: str,
    text: str,
    thread: str | None = None,
    handle: str | None = None,
    person: str | None = None,
    from_me: bool = False,
    addressed_to: str = "person",
    meta: dict | None = None,
    gated: bool = False,
    gate_reason: str | None = None,
    collection_id: int | None = None,
) -> int | None:
    """Append one item. Returns its archive id, or None if already present.

    `collection_id` is the ingest pass that first recorded the row; skipped items
    never enter the spool, so it preserves which pass filtered them.
    """
    cur = conn.execute(
        """INSERT INTO archive(stream, external_id, ts, thread, handle, person, from_me,
                               addressed_to, text, meta, gated, gate_reason, created_at,
                               collection_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(stream, external_id) DO NOTHING""",
        (
            stream, str(external_id), ts, thread, handle, person, int(bool(from_me)),
            addressed_to or "person",
            text, db.jdump(meta or {}), int(bool(gated)), gate_reason, db.now(),
            collection_id,
        ),
    )
    if cur.rowcount == 0:
        return None
    return int(cur.lastrowid)


def search(conn: sqlite3.Connection, query: str, limit: int = 20) -> list[sqlite3.Row]:
    try:
        return conn.execute(
            """SELECT a.* FROM archive_fts f JOIN archive a ON a.id = f.rowid
               WHERE archive_fts MATCH ? ORDER BY rank LIMIT ?""",
            (query, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        # Unparseable FTS query (bare punctuation, unbalanced quotes) — fall back to LIKE.
        return conn.execute(
            "SELECT * FROM archive WHERE lower(text) LIKE ? ORDER BY ts DESC LIMIT ?",
            (f"%{query.lower()}%", limit),
        ).fetchall()


def search_filtered(conn: sqlite3.Connection, query: str, *, limit: int = 20,
                    person: str = "", stream: str = "", thread: str = "",
                    since: str = "", until: str = "") -> list[sqlite3.Row]:
    """Full-text search filtered by person, stream, thread, or date range.

    An empty `query` with filters returns all matching items in that scope.
    """
    where, args = [], []
    if person:
        # Matches person name across message sender, handle, and thread identifiers.
        where.append("(lower(coalesce(a.person,'')) LIKE ? OR lower(coalesce(a.handle,'')) LIKE ?"
                     " OR lower(coalesce(a.thread,'')) LIKE ?)")
        args += [f"%{person.lower()}%"] * 3
    if stream:
        where.append("a.stream = ?")
        args.append(stream)
    if thread:
        where.append("a.thread = ?")
        args.append(thread)
    if since:
        where.append("a.ts >= ?")
        args.append(since)
    if until:
        where.append("a.ts <= ?")
        args.append(until + "T23:59:59" if len(until) == 10 else until)
    clause = (" AND " + " AND ".join(where)) if where else ""

    if (query or "").strip():
        try:
            return conn.execute(
                "SELECT a.* FROM archive_fts f JOIN archive a ON a.id = f.rowid"
                " WHERE archive_fts MATCH ?" + clause + " ORDER BY rank LIMIT ?",
                [query, *args, limit]).fetchall()
        except sqlite3.OperationalError:
            where.append("lower(a.text) LIKE ?")
            args.append(f"%{query.lower()}%")
            clause = " AND " + " AND ".join(where)
    return conn.execute(
        "SELECT a.* FROM archive a WHERE 1=1" + clause + " ORDER BY a.ts DESC LIMIT ?",
        [*args, limit]).fetchall()


def recent(conn: sqlite3.Connection, limit: int = 20, stream: str | None = None) -> list[sqlite3.Row]:
    if stream:
        return conn.execute(
            "SELECT * FROM archive WHERE stream = ? ORDER BY ts DESC LIMIT ?", (stream, limit)
        ).fetchall()
    return conn.execute("SELECT * FROM archive ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()


def counts_by_stream(conn: sqlite3.Connection, since: str | None = None) -> list[sqlite3.Row]:
    sql = ("SELECT stream, count(*) AS n, sum(gated) AS gated, sum(length(text)) AS chars"
           " FROM archive")
    args: list[object] = []
    if since:
        sql += " WHERE ts >= ?"
        args.append(since)
    sql += " GROUP BY stream ORDER BY n DESC"
    return conn.execute(sql, args).fetchall()


# Threshold in days for inactive-stream reporting.
STALE_AFTER_DAYS = 2

# Internally generated streams excluded from stale-source reporting.
INTERNAL_STREAMS = frozenset({"agent", "cli"})


def freshness(conn: sqlite3.Connection, cfg=None) -> list[dict]:
    """Newest successful observation per stream.

    Evaluates health according to `Source.health`:
    - A snapshot source is healthy on successful execution regardless of row changes;
      `source.<stream>.last_success` is used as the timestamp.
    - A stream source is healthy only when new rows arrive in `archive`.

    A stream with no archived rows still returns an entry derived from `last_success`
    to surface empty executions.
    """
    snapshots = snapshot_streams(cfg)
    rows = [dict(row) for row in conn.execute(
        "SELECT stream, max(ts) AS newest, count(*) AS n FROM archive"
        " GROUP BY stream ORDER BY newest DESC"
    ).fetchall()]
    by_stream = {row["stream"]: row for row in rows}
    for row in conn.execute(
        "SELECT key, value FROM meta WHERE key LIKE 'source.%.last_success'"
    ):
        stream = row["key"][len("source."):-len(".last_success")]
        stamp = row["value"]
        if not stream or not stamp:
            continue
        if stream not in by_stream:
            item = {"stream": stream, "newest": stamp, "n": 0}
            rows.append(item)
            by_stream[stream] = item
        elif stream in snapshots:
            by_stream[stream]["newest"] = max(str(by_stream[stream]["newest"] or ""),
                                              str(stamp))
    return sorted(rows, key=lambda row: str(row["newest"] or ""), reverse=True)


def snapshot_streams(cfg=None) -> set[str]:
    """Return stream names where health is determined by read success rather than new rows."""
    from . import sources
    return {source.name for source in sources.all_sources(cfg)
            if getattr(source, "health", "stream") == "snapshot"}


def registered_streams(cfg=None) -> set[str]:
    """Return registered stream names currently available in the source registry.

    Scans plugin sources when `cfg` is provided.
    """
    from . import sources
    return set(sources.names(cfg))


def stale_streams(conn: sqlite3.Connection, days: int = STALE_AFTER_DAYS,
                  cfg=None) -> list[tuple[str, str]]:
    """Return active streams whose newest item is older than `days`, as (stream, age phrase).

    Only active registered streams are checked. Removed sources persist historical metadata
    in `meta` and `archive` but are excluded from active staleness reporting.
    """
    cutoff = (db.today() - timedelta(days=days)).isoformat()
    known = registered_streams(cfg)
    return [(row["stream"], db.age_phrase(row["newest"]))
            for row in freshness(conn, cfg)
            if row["stream"] not in INTERNAL_STREAMS
            and row["stream"] in known
            and row["newest"] and row["newest"][:10] < cutoff]


# ---------------------------------------------------------------------- spool --

# Default horizon in days when Config is unavailable; standard paths pass Config.spool_horizon_days.
SPOOL_HORIZON_DAYS = 30


def within_horizon(ts: str, days: int = SPOOL_HORIZON_DAYS) -> bool:
    cutoff = (db.today() - timedelta(days=days)).isoformat()
    return bool(ts) and str(ts)[:10] >= cutoff


def spool_add(conn: sqlite3.Connection, archive_id: int, entity: str, *,
              priority: str = "normal") -> None:
    """Queue one gated line. `priority` orders the queue; it never removes anything.

    Low-priority lines are read after the rest within a bounded share of each pass;
    what does not fit stays pending.
    """
    conn.execute(
        "INSERT INTO spool(archive_id, entity, added_at, priority) VALUES(?,?,?,?)"
        " ON CONFLICT(archive_id) DO NOTHING",
        (archive_id, entity, db.now(), priority if priority in ("normal", "low")
         else "normal"),
    )


def spool_retire(conn: sqlite3.Connection, before: str) -> int:
    """Mark pending spool items older than `before` as processed without processing them.

    Archived items remain in `archive` and FTS indices.
    """
    cur = conn.execute(
        """UPDATE spool SET processed_at = ?
           WHERE processed_at IS NULL AND archive_id IN
             (SELECT id FROM archive WHERE substr(ts, 1, 10) < ?)""",
        (db.now(), before[:10]),
    )
    conn.commit()
    return cur.rowcount


def spool_rekey_groups(conn: sqlite3.Connection) -> int:
    """Reassign pending group messages previously keyed under individual speakers."""
    from . import gate                      # gate imports identity, not archive
    rows = conn.execute(
        """SELECT s.id, s.entity, a.stream, a.thread, a.person, a.meta
             FROM spool s JOIN archive a ON a.id = s.archive_id
            WHERE s.processed_at IS NULL AND s.entity LIKE 'person:%'"""
    ).fetchall()
    fixed = 0
    for row in rows:
        if not (db.jload(row["meta"], {}) or {}).get("group"):
            continue
        want = gate.entity_for(person=row["person"], thread=row["thread"],
                               stream=row["stream"], is_group=True)
        if want != row["entity"]:
            conn.execute("UPDATE spool SET entity = ? WHERE id = ?", (want, row["id"]))
            fixed += 1
    if fixed:
        conn.commit()
    return fixed


# Maximum lines one entity may contribute to a single pass.
ITEMS_PER_ENTITY = 60


#: Bounded share of one pass's budget reserved for low-priority traffic, so quiet
#: backlog is always read even when ordinary traffic would fill the pass alone.
LOW_PRIORITY_SHARE = 0.25


def spool_pending(conn: sqlite3.Connection, limit: int = 1500,
                  per_entity: int = ITEMS_PER_ENTITY,
                  low_share: float = LOW_PRIORITY_SHARE) -> list[sqlite3.Row]:
    """Pending items, partitioned by entity, normal priority first.

    Low-priority items get a reserved slice of the budget; whatever does not fit
    stays pending.
    """
    def page(where: str, budget: int) -> list[sqlite3.Row]:
        if budget <= 0:
            return []
        return conn.execute(
            f"""SELECT * FROM (
                 SELECT s.id AS spool_id, s.entity,
                        coalesce(s.priority, 'normal') AS priority, a.*,
                        row_number() OVER (PARTITION BY s.entity ORDER BY a.ts DESC) AS rank
                   FROM spool s JOIN archive a ON a.id = s.archive_id
                  WHERE s.processed_at IS NULL AND {where}
               ) WHERE rank <= ? ORDER BY rank, ts DESC LIMIT ?""",
            (per_entity, budget)).fetchall()

    reserved = int(max(0, limit) * max(0.0, min(1.0, low_share)))
    normal = page("coalesce(s.priority, 'normal') <> 'low'", limit - reserved)
    low = page("coalesce(s.priority, 'normal') = 'low'", reserved)
    # Unclaimed quiet share goes back to ordinary traffic.
    spare = limit - len(normal) - len(low)
    if spare > 0 and len(normal) == limit - reserved:
        normal += [row for row in page("coalesce(s.priority, 'normal') <> 'low'",
                                       limit)[len(normal):]][:spare]
    rows = normal + low
    return sorted(rows, key=lambda row: str(row["ts"]))


def backlog(conn: sqlite3.Connection) -> dict:
    """What is queued and not yet read, by priority."""
    rows = conn.execute(
        """SELECT coalesce(s.priority, 'normal') AS priority, count(*) AS n,
                  min(a.ts) AS oldest
             FROM spool s JOIN archive a ON a.id = s.archive_id
            WHERE s.processed_at IS NULL GROUP BY 1""").fetchall()
    out = {row["priority"]: {"waiting": row["n"], "oldest": str(row["oldest"] or "")[:16]}
           for row in rows}
    out["total"] = sum(item["waiting"] for item in out.values())
    return out


def spool_shape(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return per-entity counts and timestamp spans for unprocessed spool items."""
    return conn.execute(
        """SELECT s.entity, count(*) AS n, min(a.ts) AS first_ts, max(a.ts) AS last_ts
             FROM spool s JOIN archive a ON a.id = s.archive_id
            WHERE s.processed_at IS NULL GROUP BY 1 ORDER BY n DESC"""
    ).fetchall()


def spool_mark(conn: sqlite3.Connection, spool_ids: list[int], run_id: int) -> None:
    if not spool_ids:
        return
    conn.executemany(
        "UPDATE spool SET processed_at = ?, run_id = ? WHERE id = ?",
        [(db.now(), run_id, sid) for sid in spool_ids],
    )
    conn.commit()


def spool_reset(conn: sqlite3.Connection, since: str | None = None) -> int:
    """Reset `processed_at` to NULL on spool rows, optionally filtered by `processed_at >= since`."""
    if since:
        cur = conn.execute(
            "UPDATE spool SET processed_at = NULL WHERE processed_at >= ?", (since,)
        )
    else:
        cur = conn.execute("UPDATE spool SET processed_at = NULL")
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------- collection --
#
# Persistent tracking for ingest passes in `collections` and `collection_sources`.


def open_collection(conn: sqlite3.Connection, mode: str = "cli") -> int:
    cur = conn.execute(
        "INSERT INTO collections(started_at, mode) VALUES(?,?)", (db.now(), mode))
    conn.commit()
    return int(cur.lastrowid)


def _outcome_for(report, status: str | None = None) -> str:
    """Terminal outcome for one source attempt. Explicit status wins; otherwise the
    report decides: an error is a failure, remaining work is incomplete, anything
    else exhausted the source and is complete."""
    if status:
        return status
    if getattr(report, "error", None):
        return "failed"
    if getattr(report, "more", False):
        return "incomplete"
    return "complete"


def record_source(conn: sqlite3.Connection, collection_id: int, report,
                   *, status: str | None = None) -> None:
    """Record the final aggregate outcome for one source attempt.

    `catch_up` owns this write: it persists all-page totals once, so a quiet final
    page cannot erase earlier pages. Per-page writes must not call this with the
    live collection id (see `Source.run(record=False)`).
    """
    if not collection_id:
        return
    outcome = _outcome_for(report, status)
    conn.execute(
        """INSERT INTO collection_sources(collection_id, stream, read, archived, passed,
                                          muted, too_old, error, note, finished_at,
                                          status)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(collection_id, stream) DO UPDATE SET
               read=excluded.read, archived=excluded.archived, passed=excluded.passed,
               muted=excluded.muted, too_old=excluded.too_old, error=excluded.error,
               note=excluded.note, finished_at=excluded.finished_at,
               status=excluded.status""",
        (collection_id, report.stream, report.read, report.archived, report.passed,
         report.muted, report.too_old, report.error or None,
         "; ".join(report.notes)[:400] or None, db.now(), outcome),
    )
    conn.commit()


def record_unavailable(conn: sqlite3.Connection, collection_id: int, stream: str,
                       reason: str) -> None:
    """Record a preflight skip without starting login. Same durable contract as a
    fetch outcome, so due selection and reporting see it as an attempt."""
    if not collection_id:
        return
    conn.execute(
        """INSERT INTO collection_sources(collection_id, stream, read, archived, passed,
                                          muted, too_old, error, note, finished_at,
                                          status)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(collection_id, stream) DO UPDATE SET
               read=excluded.read, archived=excluded.archived, passed=excluded.passed,
               muted=excluded.muted, too_old=excluded.too_old, error=excluded.error,
               note=excluded.note, finished_at=excluded.finished_at,
               status=excluded.status""",
        (collection_id, stream, 0, 0, 0, 0, 0, (reason or "unavailable")[:400],
         f"preflight: {(reason or 'unavailable')[:300]}", db.now(), "unavailable"),
    )
    conn.commit()


#: Outcomes that count as a trustworthy previous attempt for due selection.
#: 'unknown' covers legacy rows written before the outcome contract.
TRUSTWORTHY_OUTCOMES = frozenset({"complete", "incomplete", "failed", "unavailable"})


def last_source_attempt(conn: sqlite3.Connection, stream: str) -> dict | None:
    """Most recent attempt for one source, by write order. None when never checked."""
    row = conn.execute(
        "SELECT * FROM collection_sources WHERE stream = ?"
        " ORDER BY collection_id DESC LIMIT 1", (stream,)).fetchone()
    return dict(row) if row else None


def last_complete_check(conn: sqlite3.Connection, stream: str) -> dict | None:
    """Most recent complete check for one source. A later failure never erases this;
    an interrupted attempt writes no row and so never appears here."""
    row = conn.execute(
        "SELECT * FROM collection_sources WHERE stream = ? AND status = 'complete'"
        " ORDER BY collection_id DESC LIMIT 1", (stream,)).fetchone()
    return dict(row) if row else None


def _due_interval_minutes(cfg=None) -> int:
    try:
        value = int(getattr(cfg, "collect_interval_minutes", 5) or 5)
    except (TypeError, ValueError):
        value = 5
    return max(1, value)


def source_due(conn: sqlite3.Connection, stream: str, cfg=None, *,
               now=None) -> tuple[bool, str]:
    """Is this source due for another check? Pure DB state plus `db` time: no
    network, no sleep, no source import.

    Returns (due, reason). Reasons are short machine-readable phrases for tests
    and CLI messages, never parsed back into a decision.
    """
    interval = _due_interval_minutes(cfg)
    attempt = last_source_attempt(conn, stream)
    if not attempt:
        return True, "never checked"
    status = (attempt.get("status") or "unknown")
    finished = (attempt.get("finished_at") or "")
    if status not in TRUSTWORTHY_OUTCOMES or not finished:
        return True, "no trustworthy previous state"
    moment = now or db.now_dt()
    try:
        stamp = db.parse_ts(finished)
    except Exception:
        return True, "no trustworthy previous state"
    if stamp > moment:
        # Clock moved backwards (or a future write): collecting again is the
        # conservative direction; suppressing on a future stamp could wait forever.
        return True, "clock moved — collecting rather than trusting a future stamp"
    elapsed_minutes = (moment - stamp).total_seconds() / 60.0
    if elapsed_minutes < interval:
        return False, f"checked {elapsed_minutes:.1f}m ago (< {interval}m)"
    return True, f"interval elapsed ({elapsed_minutes:.1f}m >= {interval}m)"


def select_due(conn: sqlite3.Connection, cfg, candidates: list) -> list:
    """Filter candidate sources (objects with `.name`) down to those due now."""
    return [s for s in candidates
            if source_due(conn, getattr(s, "name", s), cfg)[0]]


def failed_sources(conn: sqlite3.Connection, collection_id: int) -> list[str]:
    """Return error strings formatted as 'stream: message' for failed sources in a pass."""
    return [f"{row['stream']}: {row['error']}" for row in conn.execute(
        "SELECT stream, error FROM collection_sources"
        "  WHERE collection_id = ? AND error IS NOT NULL AND error != ''"
        "  ORDER BY stream", (collection_id,))]


def close_collection(conn: sqlite3.Connection, collection_id: int,
                     error: str | None = None) -> None:
    """Complete a collection pass, aggregating counts and recording top-level errors.

    If no explicit `error` is provided, rolls up errors from `collection_sources`.
    """
    if not collection_id:
        return
    error = error or "; ".join(failed_sources(conn, collection_id))[:400] or None
    conn.execute(
        """UPDATE collections SET finished_at = ?, error = ?,
             read     = (SELECT coalesce(sum(read),0)     FROM collection_sources WHERE collection_id = ?),
             archived = (SELECT coalesce(sum(archived),0) FROM collection_sources WHERE collection_id = ?),
             passed   = (SELECT coalesce(sum(passed),0)   FROM collection_sources WHERE collection_id = ?)
           WHERE id = ?""",
        (db.now(), error, collection_id, collection_id, collection_id, collection_id),
    )
    conn.commit()


def collections(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """Return recent collection passes with queued, filtered, and processed counts."""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM collections ORDER BY id DESC LIMIT ?", (limit,))]
    for row in rows:
        counts = conn.execute(
            """SELECT
                 sum(CASE WHEN a.gated = 1 AND s.processed_at IS NULL THEN 1 ELSE 0 END) waiting,
                 sum(CASE WHEN a.gated = 0 THEN 1 ELSE 0 END) skipped,
                 sum(CASE WHEN s.processed_at IS NOT NULL THEN 1 ELSE 0 END) read_already
               FROM archive a LEFT JOIN spool s ON s.archive_id = a.id
               WHERE a.collection_id = ?""", (row["id"],)).fetchone()
        row.update(waiting=counts["waiting"] or 0, skipped=counts["skipped"] or 0,
                   read_already=counts["read_already"] or 0)
        row["sources"] = [dict(r) for r in conn.execute(
            "SELECT * FROM collection_sources WHERE collection_id = ? ORDER BY stream",
            (row["id"],))]
    return rows
