"""Shared ingestion pipeline.

Every stream executes three sequential stages: archiving raw items, evaluating the
gate filter, and spooling passing items. Individual connectors define fetch
mechanisms and handle semantics.
"""

from __future__ import annotations

import inspect
import json
import sqlite3
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable

from .. import archive, db, gate, identity, threads


@dataclass
class IngestReport:
    stream: str = ""
    read: int = 0
    archived: int = 0
    passed: int = 0
    error: str | None = None
    notes: list[str] = field(default_factory=list)
    unknown_handles: set = field(default_factory=set)
    #: True when ingest halts due to a page or round budget rather than source exhaustion.
    more: bool = False
    #: Count of gated lines dropped because the corresponding chat is muted.
    muted: int = 0
    #: Count of gated lines older than the spool horizon.
    too_old: int = 0
    #: Spool horizon in days for this ingest run.
    horizon_days: int = archive.SPOOL_HORIZON_DAYS
    #: Optional live status callback for long-running ingest tasks.
    progress: Callable[[str], None] | None = field(default=None, repr=False, compare=False)
    #: Ingest pass identifier stamped onto archived rows for batch grouping.
    collection_id: int | None = None

    @classmethod
    def opened(cls, stream: str, cfg=None) -> "IngestReport":
        """Create an IngestReport initialized with the configured spool horizon."""
        return cls(stream=stream,
                   horizon_days=getattr(cfg, "spool_horizon_days", cls.horizon_days))

    @property
    def unresolved(self) -> int:
        return len(self.unknown_handles)

    def summary(self) -> str:
        if self.error:
            return f"{self.stream}: {self.error}"
        line = (f"{self.stream}: read {self.read}, archived {self.archived}, "
                f"queued {self.passed}")
        # Distinguish items outside the spool horizon from gate rejections.
        if self.too_old:
            line += f", {self.too_old} passed but older than {self.horizon_days}d"
        if self.muted:
            line += f", {self.muted} skipped as muted"
        if self.unresolved:
            line += f", unresolved handles {self.unresolved}"
        if self.more:
            line += "  [more waiting]"
        for note in self.notes:
            line += f"\n  {note}"
        return line

    def absorb(self, other: "IngestReport") -> "IngestReport":
        """Merge metrics and state from another IngestReport into this instance."""
        self.read += other.read
        self.archived += other.archived
        self.passed += other.passed
        self.muted += other.muted
        self.too_old += other.too_old
        self.more = self.more or other.more
        self.error = self.error or other.error
        self.notes.extend(other.notes)
        self.unknown_handles |= other.unknown_handles
        return self

    def __bool__(self) -> bool:
        return self.error is None


def adapt_progress(callback):
    """Wrap a progress callback to accept structured keyword arguments safely.

    Discards keyword arguments (`done`, `total`, `phase`) if the underlying callback
    accepts only positional arguments or a single parameter without `**kwargs`.
    """
    if callback is None:
        return None
    try:
        parameters = inspect.signature(callback).parameters
    except (TypeError, ValueError):                # Built-in without inspectable signature.
        return lambda note="", **_extra: callback(note)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        return callback
    named = {name for name, p in parameters.items()
             if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                           inspect.Parameter.KEYWORD_ONLY)}
    if {"done", "total", "phase"} <= named:
        return callback
    return lambda note="", **_extra: callback(note)


#: Scale denominator used by phased progress reporting.
PHASE_SCALE = 1000


def phased(callback, plan: tuple[tuple[str, float], ...]):
    """Compose a multi-phase progress plan into a single monotonically increasing progress callback.

    Maps distinct phases with individual weights onto a unified 0-1 scale. Progress within
    each phase advances monotonically based on reported `done` and `total` values. Returns
    a standard progress callback or None if no callback was provided.
    """
    if callback is None:
        return None
    start: dict[str, float] = {}
    width: dict[str, float] = {}
    run = 0.0
    for name, weight in plan:
        start[name], width[name] = run, float(weight)
        run += float(weight)
    span = run or 1.0
    high = 0.0
    #: Caches total denominators per phase across progress invocations.
    seen: dict[str, int] = {}

    def report(note: str = "", *, done: int = 0, total: int = 0, phase: str = "") -> None:
        nonlocal high
        if total > 0:
            seen[phase] = total
        against = seen.get(phase, 0)
        share = min(1.0, max(0.0, done / against)) if against > 0 else 0.0
        point = (start.get(phase, run) + width.get(phase, 0.0) * share) / span
        high = max(high, point)
        callback(note, done=round(high * PHASE_SCALE), total=PHASE_SCALE, phase=phase)

    return report


def deliver(
    conn: sqlite3.Connection,
    report: IngestReport,
    *,
    stream: str,
    external_id: str,
    ts: str,
    text: str,
    thread: str | None = None,
    handle: str | None = None,
    person: str | None = None,
    from_me: bool = False,
    meta: dict | None = None,
    is_group: bool = False,
    top_tier: set[str] | None = None,
    verdict: gate.Verdict | None = None,
    counterpart: str | None = None,
    addressed_to: str = "person",
) -> int | None:
    """Archive one item, gate it, and spool it if it passed. Returns the archive id.

    `counterpart` identifies the exchange participant regardless of direction, ensuring
    conversation turns group into a single bundle entity.

    `addressed_to` specifies the recipient type (`person` or `machine`). `from_me`
    indicates authorship. When addressed to a machine, imperative statements represent
    delegated tasks rather than obligations incurred by the sender.
    """
    report.read += 1
    text = (text or "").strip()
    if not text:
        return None

    handle = identity.normalize(handle) if handle else None
    seen_name = (meta or {}).get("seen_name")
    if person is None and handle:
        person = identity.resolve(conn, handle)
    if handle and not person and not from_me:
        person = (identity.link_by_name(conn, handle, seen_name, source=stream)
                  # Fall back to seen_name when no contact matches; see `identity.adopt_seen_name`.
                  or identity.adopt_seen_name(conn, handle, seen_name,
                                              source=f"{stream}:roster"))
        if not person and not _is_bulk_address(conn, handle):
            identity.note_unresolved(conn, handle, stream, seen_name, text)
            report.unknown_handles.add(handle)
    if thread and handle and not from_me:
        threads.record_members(conn, stream, thread, [(handle, seen_name)])

    # For outgoing messages, the counterpart is the recipient; otherwise use the author.
    other = counterpart or (None if from_me else person)
    if other and not from_me and other == "me":
        other = None
    if counterpart:
        # Only a resolved counterpart may identify a person; raw platform ids are opaque.
        other = identity.resolve(conn, counterpart)
    if not other and thread:
        other = thread_person(conn, stream, thread)

    if verdict is None:
        verdict = gate.gate_message(text, person=person, from_me=from_me, top_tier=top_tier,
                                    stream=stream, is_group=is_group,
                                    addressed_to=addressed_to)
        # Reactions can inherit context from a recent substantive line without a model call.
        if (not verdict and gate.is_reaction(text) and thread
                and _recent_thread_signal(conn, stream, thread, ts)):
            verdict = gate.Verdict(True, "reaction-context")

    archive_id = archive.append(
        conn, stream=stream, external_id=str(external_id), ts=ts, text=text, thread=thread,
        handle=handle, person=person, from_me=from_me, meta=meta or {},
        addressed_to=addressed_to,
        gated=bool(verdict), gate_reason=verdict.reason,
        collection_id=report.collection_id,
    )
    if archive_id is None:
        return None
    report.archived += 1
    # Multiple speakers indicate a group thread; regroup pending spool items accordingly.
    if thread and not is_group and len(thread_speakers(conn, stream, thread)) > 1:
        is_group = True
        regroup_thread(conn, stream, thread)
    # Muting suppresses spooling for model processing while preserving archive storage and search indexing.
    if verdict and threads.is_muted(conn, stream, thread):
        report.muted += 1
        return archive_id
    if verdict:
        if archive.within_horizon(ts, report.horizon_days):
            entity = gate.entity_for(person=other, thread=thread, stream=stream,
                                     is_group=is_group)
            archive.spool_add(conn, archive_id, entity)
            # Spool preceding reactions associated with the active thread topic without modifying archive gate records.
            if thread and not gate.is_reaction(text):
                _rescue_recent_reactions(conn, stream, thread, ts, entity,
                                         report.horizon_days)
            report.passed += 1
        else:
            report.too_old += 1
    return archive_id


def _recent_thread_signal(conn: sqlite3.Connection, stream: str, thread: str,
                          ts: str, minutes: int = 45) -> bool:
    cutoff = (db.parse_ts(ts) - timedelta(minutes=minutes)).isoformat()
    return conn.execute(
        """SELECT 1 FROM archive
            WHERE stream = ? AND thread = ? AND ts BETWEEN ? AND ?
              AND gated = 1 AND gate_reason NOT IN ('reaction-context')
            LIMIT 1""", (stream, thread, cutoff, ts)
    ).fetchone() is not None


def _rescue_recent_reactions(conn: sqlite3.Connection, stream: str, thread: str,
                             ts: str, entity: str, horizon_days: int,
                             minutes: int = 45) -> int:
    cutoff = (db.parse_ts(ts) - timedelta(minutes=minutes)).isoformat()
    rows = conn.execute(
        """SELECT a.* FROM archive a LEFT JOIN spool s ON s.archive_id = a.id
            WHERE a.stream = ? AND a.thread = ? AND a.ts BETWEEN ? AND ?
              AND a.gated = 0 AND a.gate_reason = 'trivial' AND s.id IS NULL
            ORDER BY a.ts""", (stream, thread, cutoff, ts)
    ).fetchall()
    rescued = 0
    for row in rows:
        if gate.is_reaction(row["text"]) and archive.within_horizon(row["ts"], horizon_days):
            archive.spool_add(conn, row["id"], entity)
            rescued += 1
    return rescued


def _is_bulk_address(conn: sqlite3.Connection, handle: str) -> bool:
    """Return True if the handle represents an automated or bulk sender rather than an individual.

    Checks automated handle patterns and senders table classifications (`archive` or `ignore`)
    to exclude bulk addresses from the unresolved handle queue.
    """
    if "@" not in (handle or ""):
        return False
    if gate.is_automated(handle):
        return True
    return identity.sender_decision(conn, handle) in ("archive", "ignore")


def thread_speakers(conn: sqlite3.Connection, stream: str, thread: str) -> list[str]:
    """Return distinct participants who have authored messages in the thread besides the account owner.

    Speaker counts provide evidence for group classification when source metadata is ambiguous.
    """
    return [row["person"] for row in conn.execute(
        "SELECT DISTINCT person FROM archive WHERE stream = ? AND thread = ?"
        " AND from_me = 0 AND person IS NOT NULL AND person != 'me' LIMIT 3",
        (stream, thread),
    )]


def thread_person(conn: sqlite3.Connection, stream: str, thread: str) -> str | None:
    """Return the unique participant in a direct conversation, or None for group/unresolved threads."""
    speakers = thread_speakers(conn, stream, thread)
    return speakers[0] if len(speakers) == 1 else None


def regroup_thread(conn: sqlite3.Connection, stream: str, thread: str) -> int:
    """Update unprocessed spool rows for a thread to use the group entity key once identified as a group."""
    entity = gate.bundle_entity(None, thread, stream)
    cur = conn.execute(
        """UPDATE spool SET entity = ?
           WHERE processed_at IS NULL AND entity != ? AND archive_id IN
             (SELECT id FROM archive WHERE stream = ? AND thread = ?)""",
        (entity, entity, stream, thread),
    )
    return cur.rowcount


def watermark(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    return db.get_meta(conn, f"watermark.{key}", default) or default


def set_watermark(conn: sqlite3.Connection, key: str, value: str) -> None:
    db.set_meta(conn, f"watermark.{key}", str(value))


# ------------------------------------------------------------------- http utils --

class HttpError(RuntimeError):
    pass


def get_json(url: str, *, headers: dict | None = None, timeout: float = 30.0):
    return _request("GET", url, None, headers, timeout)


def post_json(url: str, payload: dict, *, headers: dict | None = None, timeout: float = 30.0):
    return _request("POST", url, payload, headers, timeout)


def _request(method: str, url: str, payload, headers, timeout):
    body = json.dumps(payload).encode() if payload is not None else None
    head = {"Accept": "application/json", "User-Agent": "memcal/0.1"}
    if body is not None:
        head["Content-Type"] = "application/json"
    head.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=head, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        finally:
            exc.close()
        raise HttpError(f"HTTP {exc.code} from {_redact(url)}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise HttpError(f"{type(exc).__name__} contacting {_redact(url)}: {exc}") from exc
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise HttpError(f"non-JSON response from {_redact(url)}: {raw[:200]}") from exc


def _redact(url: str) -> str:
    """Redact authentication tokens and passwords from URLs for safe logging and error reporting."""
    out = url
    for marker in ("password=", "token=", "access_token="):
        head, sep, tail = out.partition(marker)
        if sep:
            rest = tail.split("&", 1)
            out = head + marker + "***" + ("&" + rest[1] if len(rest) > 1 else "")
    return out
