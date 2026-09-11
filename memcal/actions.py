"""Completed typed actions for replay safety and nightly context.

Each record identifies the operation, its stable target, changed fields, source message
ids, and the state version it acted on. This lets a retry remain a no-op and prevents a
nightly reread from duplicating or undoing a daytime correction. Surfaces pass `Origin`
explicitly because concurrent sessions make global "latest message" state ambiguous.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field

from . import db

#: Recorded when a caller supplies no originating turn. The operation still happens; the
#: limitation is written down rather than left for a reader to mistake for "no cause".
NO_SOURCE_NOTE = "caller supplied no originating turn"


@dataclass(frozen=True)
class Origin:
    """Where a typed write came from, and what the user said that caused it."""

    surface: str = "unknown"
    #: Archive ids of the turn behind this call. Empty is allowed and recorded.
    archive_ids: tuple[int, ...] = ()
    session: str = ""
    note: str = ""
    #: The caller's own idempotency key for this operation, when it has one. A client
    #: retrying a call whose answer it never saw supplies the same key, and the retry
    #: becomes a no-op instead of a second instruction.
    op_id: str = ""

    @classmethod
    def of(cls, surface: str, archive_ids=None, *, session: str = "",
           note: str = "", op_id: str = "") -> "Origin":
        ids = tuple(int(i) for i in (archive_ids or ()) if i)
        return cls(surface=surface or "unknown", archive_ids=ids, session=session,
                   op_id=op_id,
                   note=note or ("" if ids else NO_SOURCE_NOTE))

    @property
    def sourced(self) -> bool:
        return bool(self.archive_ids)


#: The default for a caller that has not been taught to supply context yet. Callers
#: without source context must still work; this is what "and record that limitation"
#: looks like in practice.
UNKNOWN = Origin.of("unknown")


@dataclass
class Action:
    op_id: str
    kind: str
    ref: str
    verb: str
    surface: str
    fields: dict = field(default_factory=dict)
    source_ids: list[int] = field(default_factory=list)
    source_note: str = ""
    based_on: str = ""
    session: str = ""
    at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Action":
        return cls(
            op_id=row["op_id"], kind=row["kind"], ref=row["ref"], verb=row["verb"],
            surface=row["surface"], fields=db.jload(row["fields"], {}),
            source_ids=db.jload(row["source_ids"], []),
            source_note=row["source_note"] or "", based_on=row["based_on"] or "",
            session=row["session"] or "", at=row["at"])

    @property
    def changed(self) -> list[str]:
        return sorted(self.fields)

    def one_line(self) -> str:
        """"11:04 · moved date 2026-09-18 → 2026-09-19" — enough to recognise it."""
        parts = []
        for name in self.changed:
            pair = self.fields.get(name)
            if isinstance(pair, (list, tuple)) and len(pair) == 2:
                old, new = pair
                parts.append(f"{name} {old or '(none)'} → {new or '(none)'}"
                             if str(old) != str(new) else f"{name} {new}")
            else:
                parts.append(f"{name} {pair}")
        detail = "; ".join(parts) if parts else "no field changed"
        return f"{str(self.at)[11:16]} · {self.verb} · {detail}"


def plan(*, kind: str, ref: str, verb: str, origin: Origin, request: dict,
         at: str) -> str:
    """Identify an operation from its request before state changes.

    A caller key wins. Otherwise the digest covers the target, verb, supplied fields,
    and source turn—not mutable before-values—so retries keep one identity. With no
    attributable turn, the timestamp keeps identical unknown calls distinct.
    """
    if origin.op_id:
        return str(origin.op_id)[:64]
    seed = json.dumps({
        "kind": kind, "ref": ref, "verb": verb,
        "surface": origin.surface, "session": origin.session,
        "sources": sorted(origin.archive_ids),
        "request": {k: request[k] for k in sorted(request)},
        "at": "" if origin.sourced else at,
    }, sort_keys=True, default=str)
    return hashlib.sha1(seed.encode()).hexdigest()[:20]


def seen(conn: sqlite3.Connection, identifier: str) -> bool:
    """Has this exact operation already been carried out?

    Called inside the transaction that is about to mutate, so the answer cannot go stale
    between the check and the write.
    """
    if not identifier:
        return False
    return conn.execute("SELECT 1 FROM actions WHERE op_id = ? LIMIT 1",
                        (identifier,)).fetchone() is not None


def record(conn: sqlite3.Connection, *, kind: str, ref: str, verb: str,
           origin: Origin = UNKNOWN, fields: dict | None = None,
           based_on: str | None = None, at: str | None = None,
           op_id: str = "", commit: bool = False) -> str:
    """Write one completed operation. Returns its id; a repeat is a no-op.

    `op_id` is the identity `plan()` computed before the change was made. Deliberately
    never commits by default: the caller commits the state change and this record
    together, or neither happens.
    """
    if not ref:
        return ""
    fields = {k: v for k, v in (fields or {}).items()}
    stamp = at or db.now()
    identifier = op_id or plan(kind=kind, ref=ref, verb=verb, origin=origin,
                               request=fields, at=stamp)
    conn.execute(
        """INSERT INTO actions(op_id, kind, ref, verb, surface, session, fields,
                               source_ids, source_note, based_on, at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(op_id) DO NOTHING""",
        (identifier, kind, ref, verb, origin.surface, origin.session or None,
         db.jdump(fields), db.jdump(list(origin.archive_ids)),
         origin.note or (None if origin.sourced else NO_SOURCE_NOTE),
         based_on or None, stamp),
    )
    if commit:
        conn.commit()
    return identifier


def for_refs(conn: sqlite3.Connection, kind: str, refs, *,
             limit: int = 12) -> list[Action]:
    """Completed operations against these targets, newest last."""
    keys = [str(ref) for ref in refs if ref]
    if not keys:
        return []
    holes = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"SELECT * FROM actions WHERE kind = ? AND ref IN ({holes})"
        f" ORDER BY at DESC, id DESC LIMIT ?", (kind, *keys, limit)).fetchall()
    return [Action.from_row(row) for row in reversed(rows)]


def for_archive(conn: sqlite3.Connection, archive_ids, *,
                limit: int = 12) -> list[Action]:
    """Operations whose originating turn includes any of these archive lines.

    This is the half that makes a *statement* recognisable as already acted on, rather
    than only a row recognisable as already touched.
    """
    wanted = {int(i) for i in (archive_ids or ()) if i}
    if not wanted:
        return []
    out: list[Action] = []
    for row in conn.execute(
            "SELECT * FROM actions ORDER BY at DESC, id DESC LIMIT 400"):
        action = Action.from_row(row)
        if wanted & set(action.source_ids):
            out.append(action)
        if len(out) >= limit:
            break
    return list(reversed(out))


def latest_for(conn: sqlite3.Connection, kind: str, ref: str) -> Action | None:
    row = conn.execute(
        "SELECT * FROM actions WHERE kind = ? AND ref = ? ORDER BY at DESC, id DESC"
        " LIMIT 1", (kind, ref)).fetchone()
    return Action.from_row(row) if row else None


def render(actions: list[Action]) -> str:
    """The block a bundle shows the model. Says what was done and what it was reading."""
    if not actions:
        return ""
    lines = [
        "ALREADY DONE FOR THIS CONVERSATION BY THE ASSISTANT'S TYPED TOOLS.",
        "These changes are already in the store. A line here that only restates one of",
        "them is not news: do not propose it again, and do not create a second row for",
        "it. The rest of the message may still contain facts nothing has acted on.",
    ]
    for action in actions:
        bits = [action.ref, action.one_line()]
        if action.source_ids:
            bits.append("from line(s) " + ", ".join(str(i) for i in action.source_ids))
        elif action.source_note:
            bits.append(f"({action.source_note})")
        lines.append("  " + " | ".join(bits))
    return "\n".join(lines)
