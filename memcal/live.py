"""Immediate writes from the user or an agent surface."""

from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass

from . import actions, archive, brief, db, events, gate, series, todos, trace, wiki
from .config import Config
from contextlib import contextmanager
from .dream import apply as apply_stage
from .dream import propose as propose_stage
from .dream.bundle import Bundle
from . import llm


class _Row(dict):
    """A dict that answers to sqlite3.Row-style indexing, so Bundle can render it."""

    def __getitem__(self, key):
        return self.get(key)


LIVE_INSTRUCTIONS = """

DIRECT USER INPUT
The user supplied this text to be recorded now. Preserve every stated fact, plan, and
correction even when the tone is casual. Return an empty diff only for content such as
"thanks" that contains nothing to store. Do not infer missing details."""


def remember(conn: sqlite3.Connection, cfg: Config, text: str, *,
             speaker: str = "me") -> tuple[Counter, list[str]]:
    """One thing said to the agent, written now. Same diff machinery as the dream pass."""
    stamp = db.now()
    archive_id = archive.append(
        conn, channel="agent", external_id=f"live:{stamp}:{db.slugify(text, 32)}",
        ts=stamp, text=text, thread="conversation", person=speaker,
        from_me=(speaker == "me"), addressed_to="machine",
        gated=True, gate_reason="live",
    )
    item = _Row(ts=stamp, channel="agent", thread="conversation", person=speaker,
                handle=None, from_me=(speaker == "me"), text=text,
                addressed_to="machine")
    bundle = Bundle(entity=gate.bundle_entity(speaker, "conversation", "agent"), items=[item])

    client = llm.client_for(cfg)
    prefix = propose_stage.build_prefix(conn, cfg) + LIVE_INSTRUCTIONS
    _bundle, diff, turns = propose_stage.propose_one(client, cfg, prefix, bundle, conn)
    # Staged proposals make multiple calls; record each with its actual ceiling.
    ceiling = propose_stage.model_ceiling(cfg, [bundle])
    suffix = propose_stage.build_suffix(cfg, [bundle], conn)
    for turn in turns:
        trace.record(conn, run_id=None,
                     stage=f"live:{turn.stage}" if turn.stage else "live",
                     label=text[:80], reply=turn.reply, max_tokens=ceiling,
                     home=cfg.home, prefix=prefix, suffix=suffix,
                     bundles=[propose_stage.bundle_ref(bundle)])
    reply = turns[-1].reply
    before_apply = db.now()
    counts, log = apply_stage.apply_diffs(
        conn, cfg, [(bundle, diff, getattr(reply, "generation_id", ""))],
        written_by="live", stage="live")

    for woken in todos.check_wakes(conn, text, since=before_apply):
        log.append(f"woke      {woken.text}")
    if archive_id is not None:
        conn.commit()
    brief.write(conn, cfg)
    return counts, log


# ------------------------------------------------------- typed live writes ----
# Agent-facing writes are typed and deterministic; only `remember()` runs extraction.


class LiveError(Exception):
    """Something the agent asked for that the store will not do, with a way forward."""

    def __init__(self, message: str, **detail):
        super().__init__(message)
        self.detail = detail


# ------------------------------------------------ field-evidence outcomes ----
#: Public caller names that normalize to a stored mutable field.
_FIELD_ALIASES = {"when": "date"}
#: Caller-facing names that assert the participants field after normalization.
_PARTICIPANT_OPS = frozenset({"add_participants", "remove_participants", "participants"})


@dataclass(frozen=True)
class FieldOutcome:
    """Per-field decision from a typed live event write.

    Frozen for Integrations: ``status`` is one of ``applied``, ``unchanged``,
    ``rejected``. ``evidence_advanced`` is True when the value stayed the same
    but a newer supporting statement advanced that field's evidence timestamp.
    """

    field: str
    status: str
    reason: str = ""
    source_ids: tuple[int, ...] = ()
    evidence_advanced: bool = False
    old: str = ""
    new: str = ""

    def as_dict(self) -> dict:
        return {
            "field": self.field,
            "status": self.status,
            "reason": self.reason,
            "source_ids": list(self.source_ids),
            "evidence_advanced": self.evidence_advanced,
            "old": self.old,
            "new": self.new,
        }


@dataclass(frozen=True)
class EventWriteOutcome:
    """Structured result of ``add_event`` / ``update_event``.

    Unpacks as ``(event, verb)`` for add compatibility or ``(event, changed)``
    for update compatibility when ``compat`` is set accordingly. Adapters that
    need per-field decisions read ``fields`` directly — never reconstruct them.
    """

    event: object
    verb: str
    fields: tuple[FieldOutcome, ...] = ()
    compat: str = "verb"  # "verb" | "changed"
    #: When True, ``changed_lines`` is empty so flat callers see a replay no-op
    #: while ``fields`` still carries the recorded structured outcome.
    silent_replay: bool = False

    @property
    def changed_lines(self) -> list[str]:
        if self.silent_replay:
            return []
        lines = []
        for item in self.fields:
            if item.status == "applied" and item.evidence_advanced:
                lines.append(f"{item.field}: evidence advanced")
            elif item.status == "applied":
                lines.append(f"{item.field}: {item.old} → {item.new}")
        return lines

    @property
    def rejected(self) -> tuple[FieldOutcome, ...]:
        return tuple(item for item in self.fields if item.status == "rejected")

    @property
    def applied(self) -> tuple[FieldOutcome, ...]:
        return tuple(item for item in self.fields if item.status == "applied")

    def outcome_dict(self) -> dict:
        return {"verb": self.verb, "fields": [item.as_dict() for item in self.fields]}

    def summary_lines(self) -> list[str]:
        """Human-readable applied / rejected lines for agent surfaces."""
        lines = []
        for item in self.fields:
            if item.status == "applied" and item.evidence_advanced:
                lines.append(f"Applied: {item.field} (evidence advanced)")
            elif item.status == "applied":
                lines.append(f"Applied: {item.field} → {item.new}")
            elif item.status == "rejected":
                support = (", ".join(str(i) for i in item.source_ids)
                           if item.source_ids else "no support")
                reason = item.reason or "rejected"
                lines.append(f"Rejected: {item.field} — {reason} (sources: {support})")
            elif item.status == "unchanged":
                lines.append(f"Unchanged: {item.field}")
        return lines

    def __iter__(self):
        yield self.event
        if self.compat == "changed":
            yield self.changed_lines
        else:
            yield self.verb

    def __getitem__(self, index):
        return (self.event, self.changed_lines if self.compat == "changed"
                else self.verb)[index]


def _refresh(conn: sqlite3.Connection, cfg: Config, *keys: str) -> None:
    brief.write(conn, cfg)
    # Publish confirmed commitments; failures log and retry on the next pass.
    if keys:
        from .sources import ical                                   # noqa: PLC0415
        ical.publish_pending(conn, cfg, keys=[k for k in keys if k])


def find_event(conn: sqlite3.Connection, needle: str) -> events.Event:
    """Find one event by handle, key, or title; reject ambiguous matches."""
    needle = (needle or "").strip()
    if not needle:
        raise LiveError("which row? give its E# handle or the words it is listed under")

    # Resolve E# handles before loose title search; a handle is an address.
    handle = brief.parse_source(needle.strip("〔〕"))
    if handle:
        kind, _row_id = handle
        if kind != "event":
            raise LiveError(f"{needle!r} is not an event handle — use the row's E# handle")
        resolved = trace.resolve_source(conn, needle.strip("〔〕"))
        if resolved.get("error"):
            raise LiveError(resolved["error"])
        event = events.get(conn, resolved["ref"])
        if event is not None:
            return event
    exact = events.get(conn, needle)
    if exact:
        return exact
    found = events.search(conn, needle)
    if not found:
        raise LiveError(f"nothing on the calendar matches {needle!r}")
    if len(found) > 1:
        exact_titles = [event for event in found if event.title.casefold() == needle.casefold()]
        if len(exact_titles) == 1:
            return exact_titles[0]
        first, second = found[0], found[1]
        overlap = _score(first, needle) == _score(second, needle)
        if overlap or len(exact_titles) > 1:
            raise LiveError("that matches more than one row — use its E# handle",
                            candidates=[f"{e.one_line()}  {brief.source_tag('event', e.id)}"
                                        for e in found[:4]])
    return found[0]


def _score(event: events.Event, needle: str) -> int:
    wanted = set(db.slugify(needle).split("-"))
    return len(wanted & set(db.slugify(event.title).split("-")))


#: Where a typed write came from. Passed explicitly by every surface that knows; see
#: `actions.Origin` for why this is not module state.
Origin = actions.Origin


@contextmanager
def _atomic(conn: sqlite3.Connection):
    """Commit state changes with their operation records, or neither."""
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN")
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _version_of(conn: sqlite3.Connection, kind: str, ref: str) -> str:
    """The target's state stamp before a write — what the operation was based on."""
    table = {"event": "events", "todo": "todos", "series": "series"}.get(kind)
    if not table or not ref:
        return ""
    row = conn.execute(
        f"SELECT updated_at FROM {table} WHERE key = ?" if table != "series"
        else "SELECT updated_at FROM series WHERE slug = ?", (ref,)).fetchone()
    return str(row["updated_at"]) if row and row["updated_at"] else ""


#: Fallback `source` / provenance `entity` when a typed live write has no
#: originating turn (e.g. CLI, background job). Explicitly not `"agent:live"`:
#: that string names the writer, which is what `written_by` already records.
#: This names the absence of a conversational origin so `series.source` stays
#: readable the same way as `events.source` — a pointer like `thread:…`,
#: `person:…`, `ical:…` answering "where did this come from", never "what code
#: wrote it".
LIVE_DIRECT_SOURCE = "live:direct"


def _origin_pointer(conn: sqlite3.Connection, origin: Origin) -> str:
    """Where the information came from, as a pointer like everywhere else.

    With an archived turn, the turn's own thread address (e.g.
    `thread:agent:hermes:s1`), which preserves the session — a bare
    `person:me` would collapse every live write into one bucket. With a
    session but no archived turn, the session's thread address. With neither,
    `LIVE_DIRECT_SOURCE`, so a background write records that it had no turn
    rather than borrowing the writer's name as its origin.
    """
    for aid in list(getattr(origin, "archive_ids", ()) or ()):
        try:
            row = conn.execute(
                "SELECT channel, thread, person FROM archive WHERE id = ?",
                (int(aid),)).fetchone()
        except (ValueError, TypeError, sqlite3.Error):
            continue
        if row is None:
            continue
        channel = (row["channel"] or "").strip()
        thread = (row["thread"] or "").strip()
        person = (row["person"] or "").strip()
        if thread:
            return f"thread:{channel or 'agent'}:{thread}"
        if person:
            return f"person:{person}"
        if channel:
            return f"channel:{channel}"
    session = (getattr(origin, "session", "") or "").strip()
    surface = (getattr(origin, "surface", "") or "").strip()
    if session and surface and surface != "unknown":
        # No archived turn to read back, but the caller still knows which
        # session it is answering — the same shape Hermes uses for wiki stamps.
        return f"thread:agent:{surface}:{session}"
    return LIVE_DIRECT_SOURCE


def _valid_citations(conn: sqlite3.Connection,
                     origin: Origin) -> tuple[Origin, int]:
    """Keep only cited lines naming real archived observations.

    A typed write may only acknowledge lines that exist. Invented ids are
    dropped (and counted) rather than stored, so a hallucinated citation can
    neither fabricate evidence nor clear pending activity it never read. The
    authorship turn is not evidence and passes through untouched.
    """
    if not origin.cited:
        return origin, 0
    have = {row["id"] for row in conn.execute(
        f"""SELECT id FROM archive WHERE id IN
            ({",".join("?" * len(origin.cited))})""", origin.cited)}
    kept = tuple(i for i in origin.cited if i in have)
    if len(kept) == len(origin.cited):
        return origin, 0
    return actions.Origin.of(origin.surface, origin.archive_ids, cited=kept,
                             session=origin.session, note=origin.note,
                             op_id=origin.op_id), \
        len(origin.cited) - len(kept)


def _cited_or_reject(conn: sqlite3.Connection, origin: Origin) -> Origin:
    """Validate citations at the one mutation boundary; refuse invalid support.

    Every typed write that can mutate a row runs through here so creation and
    correction cannot disagree: a source-backed write whose support does not
    exist must fail, not quietly fall back to the authorship turn (or run time)
    and gain authority its evidence never had. Rejecting all- and mixed-invalid
    alike also blocks laundering — dropping an invalid member could only raise
    the remaining set's authority.
    """
    origin, dropped = _valid_citations(conn, origin)
    if dropped:
        raise LiveError(
            "some cited lines don't exist — re-read the activity and cite real "
            "archive line ids. For corrections that touch more than one field, "
            "pass field_sources so each field names its own supporting lines.")
    return origin


def _cited_ts(conn: sqlite3.Connection, ids: tuple[int, ...]) -> str | None:
    """The evidence time a citation may safely claim: its *oldest* line.

    Precedence runs per field on evidence time, but which cited line supports
    which changed field is not recoverable from text. Granting every changed
    field the newest cited line's hour would let a stale claim ride a fresh
    line's timestamp and undo a settlement made between the two; so a citation
    is only as authoritative as its oldest line, and a field genuinely backed
    by a newer line is simply held until it is cited on its own. A single-line
    citation is unaffected (oldest == newest).

    A missing or unparseable source time is refused, never quietly replaced
    with now: `db.parse_ts` substitutes the current instant on a bad value, so
    an invalid stored `ts` would otherwise mint fresh authority from nothing.
    One bad line poisons the set — its true hour is unknown, so it could be the
    oldest, and dropping it could only *raise* the remaining authority.
    """
    found = {row["id"]: row["ts"] for row in conn.execute(
        f"""SELECT id, ts FROM archive WHERE id IN
            ({",".join("?" * len(ids))})""", ids)}
    stamps = []
    for i in ids:
        try:
            stamps.append(db.parse_ts(str(found.get(i) or ""), strict=True))
        except (TypeError, ValueError):
            raise LiveError(
                "a cited line has no readable timestamp — its evidence time "
                "can't be trusted; cite lines with valid times. For mixed-age "
                "corrections, pass field_sources so each field keeps its own "
                "supporting timestamp.")
    if not stamps:
        return None
    return min(stamps).isoformat(timespec="seconds")



def _newest_ts(conn: sqlite3.Connection, ids: list[int] | tuple[int, ...]) -> str:
    """Newest strict timestamp among archive ids (field-local authority)."""
    if not ids:
        raise LiveError("a field_sources entry needs at least one archive line id")
    found = {row["id"]: row["ts"] for row in conn.execute(
        f"""SELECT id, ts FROM archive WHERE id IN
            ({",".join("?" * len(ids))})""", ids)}
    missing = [i for i in ids if i not in found]
    if missing:
        raise LiveError(
            "some cited lines don't exist — re-read the activity and cite real "
            "archive line ids. For corrections that touch more than one field, "
            "pass field_sources so each field names its own supporting lines.",
            missing=missing)
    stamps = []
    for i in ids:
        try:
            stamps.append(db.parse_ts(str(found[i] or ""), strict=True))
        except (TypeError, ValueError):
            raise LiveError(
                "a cited line has no readable timestamp — its evidence time "
                "can't be trusted; cite lines with valid times. For mixed-age "
                "corrections, pass field_sources so each field keeps its own "
                "supporting timestamp.")
    return max(stamps).isoformat(timespec="seconds")


def _normalize_field_name(name: str) -> str:
    """Map public aliases (e.g. ``when``) to stored mutable fields."""
    key = str(name or "").strip()
    if key in _FIELD_ALIASES:
        return _FIELD_ALIASES[key]
    if key in _PARTICIPANT_OPS:
        return "participants"
    return key


def _normalize_field_sources(raw) -> dict[str, list[int]]:
    """Validate and normalize a field_sources map; reject conflicts/empties."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise LiveError("field_sources must be a map of field name → archive line ids")
    out: dict[str, list[int]] = {}
    seen_alias: dict[str, str] = {}
    for key, ids in raw.items():
        public = str(key or "").strip()
        stored = _normalize_field_name(public)
        if public in _FIELD_ALIASES and stored in raw and public != stored:
            raise LiveError(
                f"conflicting field aliases {public!r} and {stored!r} — supply one")
        if stored in seen_alias and seen_alias[stored] != public:
            raise LiveError(
                f"conflicting field aliases {seen_alias[stored]!r} and {public!r} "
                f"— both map to {stored!r}")
        seen_alias[stored] = public
        if stored not in events.MUTABLE and stored not in events.CLEARABLE:
            raise LiveError(f"unknown field in field_sources: {public!r}")
        if not isinstance(ids, (list, tuple)) or not ids:
            raise LiveError(
                f"field_sources[{public!r}] needs a non-empty list of archive line ids")
        try:
            cleaned = [int(i) for i in ids]
        except (TypeError, ValueError):
            raise LiveError(
                f"field_sources[{public!r}] must be archive line ids, e.g. [12, 13]")
        # Stable unique order: first occurrence wins, duplicates ignored.
        uniq: list[int] = []
        for i in cleaned:
            if i not in uniq:
                uniq.append(i)
        out[stored] = uniq
    return out


def _normalize_context_ids(raw) -> list[int]:
    if raw in (None, "", []):
        return []
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    try:
        ids = [int(i) for i in raw]
    except (TypeError, ValueError):
        raise LiveError("context_source_ids must be archive line ids, e.g. [90]")
    uniq: list[int] = []
    for i in ids:
        if i not in uniq:
            uniq.append(i)
    return uniq


def _asserted_fields(payload: dict, wipe: tuple[str, ...] = (),
                     *, include_routing: bool = False) -> set[str]:
    """Fields the caller is asserting or clearing (not internal routing defaults)."""
    names = set()
    for name, value in payload.items():
        if name == "key":
            continue
        if name not in events.MUTABLE:
            continue
        if value in (None, [], "") and name not in wipe:
            continue
        names.add(name)
    names.update(n for n in wipe if n in events.CLEARABLE or n in events.MUTABLE)
    return names


def _require_field_coverage(asserted: set[str], field_sources: dict[str, list[int]],
                            *, created: dict | None = None) -> None:
    """Every asserted field needs explicit support; reject extras too."""
    covered = set(field_sources)
    missing = sorted(asserted - covered)
    if missing:
        raise LiveError(
            "field_sources must cover every asserted field: missing "
            + ", ".join(missing),
            missing=missing)
    # Extra keys that are not being asserted are an agent error.
    extra = sorted(covered - asserted)
    if extra and created is None:
        raise LiveError(
            "field_sources names fields that were not asserted: "
            + ", ".join(extra),
            extra=extra)


def _validate_context_ids(conn: sqlite3.Connection, ids: list[int]) -> None:
    if not ids:
        return
    have = {row["id"] for row in conn.execute(
        f"SELECT id FROM archive WHERE id IN ({','.join('?' * len(ids))})", ids)}
    missing = [i for i in ids if i not in have]
    if missing:
        raise LiveError(
            "some context_source_ids don't exist — re-read the activity and cite "
            "real archive line ids",
            missing=missing)


def _field_evidence_map(conn: sqlite3.Connection,
                        field_sources: dict[str, list[int]]) -> dict[str, str]:
    """Per-field newest supporting timestamp — never a max across fields."""
    return {name: _newest_ts(conn, ids) for name, ids in field_sources.items()}


def _review_ids_for(outcomes: tuple[FieldOutcome, ...]) -> list[int]:
    """Acknowledge only evidence accepted for this decision; shared IDs stay pending."""
    accepted: set[int] = set()
    rejected: set[int] = set()
    for item in outcomes:
        if item.status == "rejected":
            rejected.update(item.source_ids)
        elif item.status in ("applied", "unchanged"):
            accepted.update(item.source_ids)
    return sorted(accepted - rejected)


def _outcomes_from_report(
        report, *, before: dict, after, requested: dict[str, object],
        wipe: tuple[str, ...] = (),
        field_sources: dict[str, list[int]] | None = None,
        flat_ids: list[int] | None = None) -> tuple[FieldOutcome, ...]:
    """Build frozen FieldOutcome values from an UpsertReport + request."""
    sources = field_sources or {}
    flat = tuple(flat_ids or ())
    applied = set(report.applied)
    stale = set(report.stale)
    advanced = set(report.advanced)
    out: list[FieldOutcome] = []
    names = sorted(set(requested) | set(wipe) | set(sources))
    for name in names:
        if name == "key":
            continue
        if name not in events.MUTABLE and name not in events.CLEARABLE:
            continue
        ids = tuple(sources.get(name, flat))
        old_v = before.get(name, "")
        new_v = getattr(after, name, "") if after is not None else requested.get(name, "")
        old_s = "" if old_v in (None, []) else (
            db.jdump(old_v) if isinstance(old_v, list) else str(old_v))
        new_s = "" if new_v in (None, []) else (
            db.jdump(new_v) if isinstance(new_v, list) else str(new_v))
        if name in wipe and name not in requested:
            new_s = ""
        if name in advanced:
            out.append(FieldOutcome(
                field=name, status="applied", source_ids=ids,
                evidence_advanced=True, old=old_s, new=new_s))
        elif name in applied:
            out.append(FieldOutcome(
                field=name, status="applied", source_ids=ids,
                old=old_s, new=new_s))
        elif name in stale:
            out.append(FieldOutcome(
                field=name, status="rejected",
                reason="source predates the stored value",
                source_ids=ids, old=old_s, new=str(requested.get(name, new_s) or "")))
        else:
            out.append(FieldOutcome(
                field=name, status="unchanged", source_ids=ids,
                old=old_s, new=new_s))
    return tuple(out)


def _replay_outcome(conn: sqlite3.Connection, op_id: str, event,
                    *, compat: str) -> EventWriteOutcome | None:
    """Return a recorded outcome for an already-completed operation."""
    prior = actions.get(conn, op_id)
    if prior is None:
        return None
    raw = prior.outcome or {}
    items = []
    for entry in raw.get("fields") or []:
        items.append(FieldOutcome(
            field=str(entry.get("field") or ""),
            status=str(entry.get("status") or "unchanged"),
            reason=str(entry.get("reason") or ""),
            source_ids=tuple(int(i) for i in (entry.get("source_ids") or [])),
            evidence_advanced=bool(entry.get("evidence_advanced")),
            old=str(entry.get("old") or ""),
            new=str(entry.get("new") or "")))
    return EventWriteOutcome(
        event=event, verb=str(raw.get("verb") or prior.verb),
        fields=tuple(items), compat=compat)


def _stamp_live(conn: sqlite3.Connection, kind: str, ref: str, verb: str, *,
                origin: Origin = actions.UNKNOWN, fields: dict | None = None,
                based_on: str = "", op_id: str = "", commit: bool = True,
                review_ids: list[int] | None = None,
                field_sources: dict | None = None,
                context_source_ids: list[int] | None = None,
                outcome: dict | None = None) -> None:
    """Record provenance, evidence and the completed operation for a typed write.

    Provenance says which conversation the information came from; evidence points
    at the lines the user was looking at when they said it, which a caller can
    only supply if it knows its own turn; and the action record is the part the
    nightly pass reads. The writer identity lives in `written_by`, never in the
    provenance entity — see `_origin_pointer`. Only explicitly cited lines count
    as reviewed — the authorship turn never does. When `review_ids` is passed
    (field-attributed writes), only those ids are acknowledged — never the whole
    citation union, and never context-only ids.
    """
    origin, _dropped = _valid_citations(conn, origin)
    reviewed = (list(review_ids) if review_ids is not None else list(origin.cited))
    archive_ids = [*origin.archive_ids, *origin.cited]
    if context_source_ids:
        archive_ids = [*archive_ids, *[i for i in context_source_ids
                                       if i not in archive_ids]]
    trace.stamp(conn, kind=kind, ref=ref, verb=verb,
                entity=_origin_pointer(conn, origin),
                stage="live", run_id=None, generation_id=None,
                archive_ids=archive_ids,
                review_ids=reviewed)
    actions.record(conn, kind=kind, ref=ref, verb=verb, origin=origin,
                   fields=fields or {}, based_on=based_on, op_id=op_id,
                   commit=commit, field_sources=field_sources,
                   context_source_ids=context_source_ids, outcome=outcome)


def add_event(conn: sqlite3.Connection, cfg: Config, *, title: str, when: str,
              origin: Origin = actions.UNKNOWN,
              field_sources=None, context_source_ids=None,
              **fields) -> EventWriteOutcome:
    """A plan the user just described. No model: they said the fields, the agent has them.

    Optional ``field_sources`` maps each asserted field to supporting archive line
    ids (newest timestamp per field). ``context_source_ids`` are background only.
    Returns an ``EventWriteOutcome`` unpackable as ``(event, verb)``.
    """
    title = (title or "").strip()
    if not title:
        raise LiveError("an event needs a title")
    start, _span = db.parse_when(when)
    # Pop attribution kwargs that must not land in the event payload.
    raw_fs = field_sources
    raw_ctx = context_source_ids
    payload = {k: v for k, v in fields.items()
               if k not in ("field_sources", "context_source_ids")
               and v not in (None, "", [])}
    payload.update(title=title, date=start.isoformat())
    if payload.get("until"):
        payload["until"] = db.parse_when(str(payload["until"]))[0].isoformat()

    flat_cited = bool(origin.cited)
    fs = _normalize_field_sources(raw_fs)
    ctx = _normalize_context_ids(raw_ctx)
    if fs and flat_cited:
        raise LiveError(
            "pass field_sources or source_ids, not both — a flat citation set "
            "cannot be combined with per-field attribution")
    if ctx and not fs:
        raise LiveError(
            "context_source_ids require field_sources — background context has "
            "no authority without explicit per-field support")

    if fs:
        # Creation asserts every supplied mutable field (title+date always).
        asserted = _asserted_fields(payload)
        _require_field_coverage(asserted, fs)
        _validate_context_ids(conn, ctx)
        evidence_ts = _field_evidence_map(conn, fs)
        # Origin citations for review come from accepted fields after the write.
        origin = actions.Origin.of(
            origin.surface, origin.archive_ids, cited=(),
            session=origin.session, note=origin.note or "field_sources attribution",
            op_id=origin.op_id)
    else:
        origin = _cited_or_reject(conn, origin)
        evidence_ts = None
        support = list(origin.cited) or list(origin.archive_ids)
        if support:
            said_at = _cited_ts(conn, tuple(support))
            if said_at is not None:
                evidence_ts = {name: said_at for name in payload
                               if name in events.MUTABLE}

    request = {**payload}
    if fs:
        request["field_sources"] = {k: list(v) for k, v in sorted(
            (n, ids) for n, ids in fs.items())}
        request["context_source_ids"] = list(ctx)
    op = actions.plan(kind="event", ref=f"new:{db.slugify(title, 48)}",
                      verb="inserted", origin=origin, request=request, at=db.now())
    with _atomic(conn):
        if actions.seen(conn, op):
            existing = events.find_match(conn, title=title, on=payload["date"],
                                         participants=payload.get("participants") or [])
            if existing is not None:
                replayed = _replay_outcome(conn, op, existing, compat="verb")
                if replayed is not None:
                    return replayed
                return EventWriteOutcome(existing, "unchanged", compat="verb")
        report = events.upsert(conn, payload, written_by="live",
                               evidence_ts=evidence_ts, commit=False)
        event, verb = report.event, report.verb
        before = {name: "" for name in events.MUTABLE}
        outcomes = _outcomes_from_report(
            report, before=before, after=event,
            requested={n: payload[n] for n in payload if n in events.MUTABLE},
            field_sources=fs or None,
            flat_ids=list(origin.cited) or list(origin.archive_ids))
        if verb != "unchanged":
            review = _review_ids_for(outcomes) if fs else None
            moved = {name: ["", str(payload.get(name) or "")]
                     for name in ("date", "time", "title", "location", "status")
                     if payload.get(name)}
            _stamp_live(conn, "event", event.key, verb, origin=origin, commit=False,
                        op_id=op, fields=moved, review_ids=review,
                        field_sources=fs or None, context_source_ids=ctx or None,
                        outcome={"verb": verb,
                                 "fields": [o.as_dict() for o in outcomes]})
    _refresh(conn, cfg, event.key)
    return EventWriteOutcome(event, verb, outcomes, compat="verb")


def update_event(conn: sqlite3.Connection, cfg: Config, which: str, *,
                 add_participants: list[str] | None = None,
                 remove_participants: list[str] | None = None,
                 origin: Origin = actions.UNKNOWN,
                 field_sources=None, context_source_ids=None,
                 **changes) -> EventWriteOutcome:
    """Change a row the user can see. Returns an EventWriteOutcome unpackable as
    ``(event, changed_lines)``.
    """
    event = find_event(conn, which)
    raw_fs = field_sources
    raw_ctx = context_source_ids
    # Drop attribution keys if they arrived via **changes.
    changes = {k: v for k, v in changes.items()
               if k not in ("field_sources", "context_source_ids")}
    payload: dict = {k: v for k, v in changes.items() if v not in (None, "", [])}
    wipe = tuple(name for name, value in changes.items()
                 if value == "" and name in events.CLEARABLE)
    if payload.get("when"):
        payload["date"] = db.parse_when(str(payload.pop("when")))[0].isoformat()
    if payload.get("until"):
        payload["until"] = db.parse_when(str(payload["until"]))[0].isoformat()
    if payload.get("status") and payload["status"] not in events.STATUSES:
        raise LiveError(f"status must be one of {', '.join(events.STATUSES)}")
    if payload.get("kind") and payload["kind"] not in events.KINDS:
        raise LiveError(f"kind must be one of {', '.join(events.KINDS)}")
    participant_asserted = bool(add_participants or remove_participants
                                or "participants" in changes)
    if add_participants or remove_participants:
        removed = {name.casefold() for name in (remove_participants or [])}
        payload["participants"] = sorted(
            (set(event.participants) | set(add_participants or []))
            - {name for name in event.participants if name.casefold() in removed})
    if not payload and not wipe:
        asked = [name for name, value in changes.items() if value == ""]
        if asked:
            raise LiveError(
                f"{', '.join(asked)} cannot be emptied — only "
                f"{', '.join(events.CLEARABLE)} can. To drop the row entirely, "
                "set status to 'declined'.")
        raise LiveError("nothing to change")

    before = {name: getattr(event, name) for name in events.MUTABLE}
    based_on = _version_of(conn, "event", event.key)
    payload["key"] = event.key
    # Date added only to address an existing event is routing, not an assertion —
    # unless the caller explicitly supplied when/date.
    # MCP/Hermes pass when=None for unused optional kwargs — that is not an assertion.
    date_asserted = (changes.get("when") not in (None, "")
                     or changes.get("date") not in (None, ""))
    if not date_asserted:
        payload.setdefault("date", event.date)

    flat_cited = bool(origin.cited)
    fs = _normalize_field_sources(raw_fs)
    ctx = _normalize_context_ids(raw_ctx)
    if fs and flat_cited:
        raise LiveError(
            "pass field_sources or source_ids, not both — a flat citation set "
            "cannot be combined with per-field attribution")
    if ctx and not fs:
        raise LiveError(
            "context_source_ids require field_sources — background context has "
            "no authority without explicit per-field support")

    if fs:
        asserted = set()
        for name in payload:
            if name in ("key",):
                continue
            if name == "date" and not date_asserted:
                continue
            if name in events.MUTABLE:
                asserted.add(name)
        asserted.update(wipe)
        if participant_asserted:
            asserted.add("participants")
        _require_field_coverage(asserted, fs)
        _validate_context_ids(conn, ctx)
        evidence_ts = _field_evidence_map(conn, fs)
        origin = actions.Origin.of(
            origin.surface, origin.archive_ids, cited=(),
            session=origin.session, note=origin.note or "field_sources attribution",
            op_id=origin.op_id)
    else:
        origin = _cited_or_reject(conn, origin)
        evidence_ts = None
        support = list(origin.cited) or list(origin.archive_ids)
        if support:
            said_at = _cited_ts(conn, tuple(support))
            if said_at is not None:
                # Routing-only date (addressing the row) is not an assertion and
                # must not receive this citation's evidence time — otherwise a
                # same-value reaffirmation path would advance date spuriously.
                evidence_ts = {name: said_at for name in (*payload, *wipe)
                               if name in events.MUTABLE
                               and (name != "date" or date_asserted)}

    request = {**payload, "clear": sorted(wipe)}
    if fs:
        request["field_sources"] = {k: list(v) for k, v in sorted(fs.items())}
        request["context_source_ids"] = list(ctx)
    op = actions.plan(kind="event", ref=event.key, verb="updated", origin=origin,
                      request=request, at=db.now())
    with _atomic(conn):
        if actions.seen(conn, op):
            current = events.get(conn, event.key) or event
            replayed = _replay_outcome(conn, op, current, compat="changed")
            if fs and replayed is not None:
                # Field-attributed replay returns the recorded structured outcome.
                return replayed
            # Flat callers treat replay as a silent no-op (empty changed list).
            return EventWriteOutcome(
                current, "unchanged",
                fields=() if replayed is None else replayed.fields,
                compat="changed", silent_replay=True)
        report = events.upsert(conn, payload, written_by="live", match=False,
                               clear=wipe, evidence_ts=evidence_ts,
                               replace_participants=bool(remove_participants),
                               commit=False)
        updated = report.event
        requested = {n: payload[n] for n in payload
                     if n in events.MUTABLE and (n != "date" or date_asserted)}
        for name in wipe:
            requested.setdefault(name, "")
        outcomes = _outcomes_from_report(
            report, before=before, after=updated, requested=requested,
            wipe=wipe, field_sources=fs or None,
            flat_ids=list(origin.cited) or list(origin.archive_ids))

        if not fs:
            # Legacy flat path: all-rejected genuine revision still raises.
            moved_legacy = {name: [str(before[name]), str(getattr(updated, name))]
                            for name in events.MUTABLE
                            if str(before[name]) != str(getattr(updated, name))}
            if (not moved_legacy and not report.advanced and evidence_ts
                    and _would_change(before, payload, wipe)):
                affected = sorted(
                    n for n in (*payload, *wipe)
                    if n in events.MUTABLE and n != "key"
                    and (n != "date" or date_asserted))
                named = ", ".join(affected) if affected else "those fields"
                if origin.cited:
                    raise LiveError(
                        f"those lines are older than what's stored for {named} — "
                        "they can't revise it. Resubmit with field_sources so each "
                        "field cites its own supporting lines (field_sources), or "
                        "cite newer evidence for the affected fields.")
                raise LiveError(
                    f"what's stored already reflects newer evidence for {named} — "
                    "this correction can't revise it. Cite the newer messages it's "
                    "based on via field_sources for each affected field.")
            if moved_legacy or report.advanced:
                fields_moved = {
                    name: [str(before[name]), str(getattr(updated, name))]
                    for name in events.MUTABLE
                    if str(before[name]) != str(getattr(updated, name))
                    or name in report.advanced}
                _stamp_live(conn, "event", updated.key, "updated", origin=origin,
                            fields=fields_moved, based_on=based_on, op_id=op,
                            commit=False,
                            outcome={"verb": "updated",
                                     "fields": [o.as_dict() for o in outcomes]})
        elif report.applied or report.advanced:
            # Field-attributed partial/full success.
            review = _review_ids_for(outcomes)
            fields_moved = {
                name: [str(before[name]), str(getattr(updated, name))]
                for name in events.MUTABLE
                if str(before[name]) != str(getattr(updated, name))
                or name in report.advanced}
            _stamp_live(conn, "event", updated.key, "updated", origin=origin,
                        fields=fields_moved, based_on=based_on, op_id=op,
                        commit=False, review_ids=review,
                        field_sources=fs, context_source_ids=ctx,
                        outcome={"verb": "updated",
                                 "fields": [o.as_dict() for o in outcomes]})
        # Wholly rejected or genuine no-op: leave event/history/review untouched.
    _refresh(conn, cfg, updated.key)
    verb = ("updated" if (report.applied or report.advanced)
            else "unchanged")
    return EventWriteOutcome(updated, verb, outcomes, compat="changed")


def _would_change(before: dict, payload: dict, wipe: tuple) -> bool:
    """Did the request ask for anything different from the stored row?"""
    # A clear revises the row only if a named field actually holds a value;
    # clearing an already-empty field is a no-op, not a rejected revision.
    if any(before.get(name) not in (None, "") for name in wipe):
        return True
    for name, value in payload.items():
        if name == "key":
            continue
        if name == "participants" and set(value or []) - set(before.get(name) or []):
            return True
        if name in events.MUTABLE and name != "participants" \
                and str(value) != str(before.get(name)):
            return True
    return False



def reviewed(conn: sqlite3.Connection, cfg: Config, which: str, *,
             origin: Origin = actions.UNKNOWN) -> str:
    """Record that the cited activity was read and changes nothing about this row.

    The explicit no-change path: new messages were opened, the stored plan still
    stands, and only the lines actually cited stop raising hints. Cites nothing,
    clears nothing — a review with no stated scope is not a review.
    """
    event = find_event(conn, which)
    origin, dropped = _valid_citations(conn, origin)
    if not origin.cited:
        raise LiveError("cite the activity lines this review covered"
                        + (" — unknown lines were dropped" if dropped else "")
                        + " (source_ids from the activity read)")
    based_on = _version_of(conn, "event", event.key)
    op = actions.plan(kind="event", ref=event.key, verb="reviewed", origin=origin,
                      request={"key": event.key}, at=db.now())
    with _atomic(conn):
        if actions.seen(conn, op):
            return f"{event.one_line()} — already marked reviewed"
        _stamp_live(conn, "event", event.key, "reviewed", origin=origin,
                    fields={}, based_on=based_on, op_id=op, commit=False)
    _refresh(conn, cfg, event.key)
    return (f"{event.one_line()} — reviewed, no change"
            + (f" ({dropped} unknown line(s) ignored)" if dropped else ""))


_WEEKDAY_WORDS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}


def set_schedule(conn: sqlite3.Connection, cfg: Config, which: str, *,
                 cadence: str | None = None, weekday: str | int | None = None,
                 day_of_month: int | None = None, time: str | None = None,
                 location: str | None = None, join_url: str | None = None,
                 starting: str | None = None, ended: bool = False,
                 origin: Origin = actions.UNKNOWN,
                 ) -> tuple[series.Series, list[str]]:
    """Create or update a recurring schedule, then project its occurrences."""
    slug = db.slugify(which or "")
    if not slug:
        raise LiveError("which recurring thing? give its name, e.g. 'tutoring'")
    known = series.get(conn, slug)
    if known is None:
        # Adopt existing rows with the same series slug when creating a rule.
        row = conn.execute("SELECT title FROM events WHERE series = ? ORDER BY date DESC"
                           " LIMIT 1", (slug,)).fetchone()
        title = row["title"] if row else which.strip()
    else:
        title = known.title

    if ended:
        if known is None:
            raise LiveError(f"memcal has no recurring {which!r} to end")
        based_on = _version_of(conn, "series", slug)
        pointer = _origin_pointer(conn, origin)
        with _atomic(conn):
            rule = series.end(conn, slug, written_by="live", commit=False)
            if rule is not None and (rule.source or "") != pointer:
                rule, _ = series.upsert(conn, {"slug": slug, "source": pointer},
                                        written_by="live", commit=False)
            _stamp_live(conn, "series", slug, "ended", origin=origin,
                        fields={"status": ["active", "ended"]}, based_on=based_on,
                        commit=False)
        _refresh(conn, cfg)
        return rule, [f"{title} is no longer recurring"]

    if isinstance(weekday, str) and weekday.strip():
        wanted = _WEEKDAY_WORDS.get(weekday.strip().lower())
        if wanted is None:
            raise LiveError(f"{weekday!r} is not a day of the week")
        weekday = wanted
    if cadence and str(cadence).strip().lower() not in series.CADENCES:
        raise LiveError(f"cadence must be one of {', '.join(series.CADENCES)}")

    fields = {"slug": slug, "title": title, "cadence": cadence, "weekday": weekday,
              "day_of_month": day_of_month, "time": time, "location": location,
              "join_url": join_url, "source": _origin_pointer(conn, origin)}
    fields = {k: v for k, v in fields.items() if v not in (None, "")}
    if starting:
        fields["effective_on"] = db.parse_when(str(starting))[0].isoformat()
    elif known is None or cadence or weekday is not None or day_of_month is not None:
        # New or reshaped rules take effect today; restating a link never moves the anchor.
        fields.setdefault("effective_on", db.today().isoformat())
    if len(fields) <= 3:                       # slug, title, source and nothing said
        raise LiveError("say how often it repeats, or what changed about it")

    based_on = _version_of(conn, "series", slug)
    with _atomic(conn):
        rule, verb = series.upsert(conn, fields, written_by="live", commit=False)
        if verb != "unchanged":
            _stamp_live(conn, "series", slug, verb, origin=origin,
                        fields={k: ["", str(v)] for k, v in fields.items()
                                if k not in ("slug", "source")},
                        based_on=based_on, commit=False)
    if verb == "unchanged":
        return rule, []
    log = series.roll_forward(conn, slug=slug)
    from .sources import ical                                        # noqa: PLC0415
    log += ical.publish_schedules(conn, cfg, slugs=[slug])
    # Publish every upcoming occurrence after a cadence change.
    upcoming = [row["key"] for row in conn.execute(
        "SELECT key FROM events WHERE series = ? AND date >= ?",
        (slug, db.today().isoformat()))]
    _refresh(conn, cfg, *upcoming)
    return rule, log


def move_one_occurrence(conn: sqlite3.Connection, cfg: Config, which: str, *,
                        to: str, time: str | None = None, cancelled: bool = False,
                        origin: Origin = actions.UNKNOWN) -> tuple[events.Event, str]:
    """Move one occurrence without changing its series schedule."""
    event = find_event(conn, which)
    if not event.series:
        raise LiveError(f"{event.title!r} is not part of a recurring thing — "
                        "change the date on it directly instead",
                        row=event.one_line())
    rule = series.get(conn, event.series)
    replaced = series.slot_for(rule, event.date, event.instead_of) if rule else None
    payload = {"key": event.key, "date": db.parse_when(to)[0].isoformat(),
               "instead_of": replaced or event.instead_of or event.date}
    if time:
        payload["time"] = time
    if cancelled:
        payload["status"] = "declined"
    based_on = _version_of(conn, "event", event.key)
    op = actions.plan(kind="event", ref=event.key, verb="moved-once", origin=origin,
                      request=payload, at=db.now())
    with _atomic(conn):
        if actions.seen(conn, op):
            return event, replaced or event.date
        updated, _ = events.upsert(conn, payload, written_by="live", match=False,
                                   commit=False)
        _stamp_live(conn, "event", updated.key, "moved-once", origin=origin,
                    fields={"date": [event.date, updated.date],
                            **({"status": [event.status, "declined"]} if cancelled else {})},
                    based_on=based_on, op_id=op, commit=False)
    series.roll_forward(conn, slug=event.series)
    _refresh(conn, cfg, updated.key)
    return updated, replaced or event.date


def merge_events(conn: sqlite3.Connection, cfg: Config, keep: str, drop: str, *,
                 origin: Origin = actions.UNKNOWN) -> events.Event:
    """The user says two rows are one thing. They are the authority; just do it."""
    survivor, doomed = find_event(conn, keep), find_event(conn, drop)
    if survivor.key == doomed.key:
        raise LiveError("those are the same row already", row=survivor.one_line())
    based_on = _version_of(conn, "event", survivor.key)
    op = actions.plan(kind="event", ref=survivor.key, verb="merged", origin=origin,
                      request={"keep": survivor.key, "drop": doomed.key}, at=db.now())
    # Merge atomically with its record so a crash cannot leave an unrecorded merge.
    with _atomic(conn):
        if actions.seen(conn, op):
            return survivor
        merged = events.merge(conn, survivor.key, doomed.key, commit=False)
        if merged is None:
            raise LiveError("could not merge those two")
        _stamp_live(conn, "event", merged.key, "merged", origin=origin,
                    fields={"merged": [doomed.key, merged.key]},
                    based_on=based_on, op_id=op, commit=False)
    _refresh(conn, cfg, merged.key)
    return merged


def drop_event(conn: sqlite3.Connection, cfg: Config, which: str, *,
               origin: Origin = actions.UNKNOWN) -> str:
    """Delete a spurious row; declined real events should be updated instead."""
    event = find_event(conn, which)
    line = event.one_line()
    based_on = _version_of(conn, "event", event.key)
    op = actions.plan(kind="event", ref=event.key, verb="dropped", origin=origin,
                      request={"key": event.key}, at=db.now())
    with _atomic(conn):
        if actions.seen(conn, op):
            return line
        events.delete(conn, event.key, commit=False)
        _stamp_live(conn, "event", event.key, "dropped", origin=origin,
                    fields={"status": [event.status, "deleted"]}, based_on=based_on,
                    op_id=op, commit=False)
    _refresh(conn, cfg)
    return line


def open_todo(conn: sqlite3.Connection, cfg: Config, text: str, *, due: str | None = None,
              remind: str | bool | None = None,
              wake_condition: str | None = None,
              event: str | None = None,
              key: str | None = None,
              origin: Origin = actions.UNKNOWN) -> tuple[todos.Todo, str]:
    """Open a to-do and optionally schedule a reminder."""
    text = (text or "").strip()
    if not text:
        raise LiveError("a to-do needs text")
    linked = find_event(conn, event) if (event or "").strip() else None
    when_due = db.parse_when(due)[0].isoformat() if due else None
    remind_at = None
    if isinstance(remind, str) and remind.strip():
        remind_at = remind.strip()
    elif remind:
        # Try anchors in authority order and take the first still ahead.
        existing = todos.get(conn, key or f"todo:{db.slugify(text, 64)}")
        anchors = [linked.date if linked else None, when_due]
        if existing:
            anchors += [existing.event_date, existing.due]
        anchors = [a for a in anchors if a]
        for anchor in anchors:
            remind_at = todos.remind_when(anchor)
            if remind_at:
                break
        if not remind_at:
            raise LiveError(
                f"nothing to time a reminder against for {text!r} — everything it is "
                f"anchored to has already passed ({', '.join(anchors)}); give it a due "
                "date or an explicit time"
                if anchors else
                f"nothing to time a reminder against for {text!r} — give it a due date, "
                "link it to an event, or pass an explicit time")
    op = actions.plan(kind="todo", ref=key or f"todo:{db.slugify(text, 64)}",
                      verb="opened", origin=origin,
                      request={"text": text, "due": when_due,
                               "event": linked.key if linked else "",
                               "wake": (wake_condition or "").strip()},
                      at=db.now())
    with _atomic(conn):
        if actions.seen(conn, op):
            known = todos.get(conn, key or f"todo:{db.slugify(text, 64)}")
            if known is not None:
                return known, "unchanged"
        todo, verb = todos.open_todo(
            conn, text, key=key, due=when_due, remind_at=remind_at,
            wake_condition=(wake_condition or "").strip() or None,
            event_id=linked.id if linked else None, written_by="live",
            auto_remind=cfg.remind_deadlines, commit=False)
        _stamp_live(conn, "todo", todo.key, verb, origin=origin, commit=False, op_id=op,
                    fields={"text": ["", todo.text],
                            **({"event": ["", linked.key]} if linked else {}),
                            **({"due": ["", when_due]} if when_due else {})})
    if todo.remind_at and not todo.reminder_uid:
        _push_reminder(conn, cfg, todo)
        todo = todos.get(conn, todo.key) or todo
    _refresh(conn, cfg)
    return todo, verb


def _push_reminder(conn: sqlite3.Connection, cfg: Config, todo: todos.Todo) -> None:
    """Publish a reminder without making external failure fail the local write."""
    from .sources import ical                                       # noqa: PLC0415
    try:
        result = ical.publish_reminder(cfg, todo)
    except ical.ReminderError:
        return
    if result.get("uid"):
        conn.execute("UPDATE todos SET reminder_uid = ? WHERE key = ?",
                     (result["uid"], todo.key))
        conn.commit()


def close_todo(conn: sqlite3.Connection, cfg: Config, which: str, *,
               origin: Origin = actions.UNKNOWN) -> todos.Todo:
    """Close a to-do the user explicitly completed."""
    todo = todos.find(conn, (which or "").strip())
    if not todo:
        raise LiveError(f"no open to-do matches {which!r}")
    based_on = _version_of(conn, "todo", todo.key)
    op = actions.plan(kind="todo", ref=todo.key, verb="closed", origin=origin,
                      request={"key": todo.key}, at=db.now())
    with _atomic(conn):
        if not actions.seen(conn, op):
            todos.close(conn, todo.key, commit=False)
            _stamp_live(conn, "todo", todo.key, "closed", origin=origin,
                        fields={"status": ["open", "closed"]}, based_on=based_on,
                        op_id=op, commit=False)
    # Retract the reminder so it stops buzzing after the to-do closes.
    if todo.reminder_uid:
        from .sources import ical                                   # noqa: PLC0415
        try:
            ical.retract_reminder(cfg, todo)
        except ical.ReminderError:
            pass
    _refresh(conn, cfg)
    return todos.get(conn, todo.key) or todo


def note(conn: sqlite3.Connection, cfg: Config, page: str, slot: str, value: str,
         *, section: str | None = None, source: str = "agent") -> tuple[bool, str]:
    """Write one durable fact directly to a wiki slot.

    `page='me'`, an established self name, or a recorded alias thereof
    resolves through `wiki.self_slug()` and writes to that canonical slug.
    Ambiguity returns `(False, ...)` naming the candidates and writes
    nothing. With no candidate, the literal `me` page is created
    (people/me.md) via the existing ensure/set_slot path.
    """
    page, slot, value = (page or "").strip(), (slot or "").strip(), (value or "").strip()
    if not (page and slot and value):
        return False, "page, slot and value are all required"
    if len(value) > apply_stage.MAX_SLOT_VALUE:
        return False, (f"value is {len(value)} chars; a slot holds a bare answer "
                       f"(under {apply_stage.MAX_SLOT_VALUE}), not a sentence")

    try:
        self_target = wiki.resolve_self_page(conn, cfg.wiki_dir, page)
    except wiki.SelfAmbiguous as exc:
        return False, (f"ambiguous self page for {page!r}: "
                       f"{', '.join(exc.candidates)} — open one explicitly to resolve")
    slug = self_target if self_target is not None else db.slugify(page)
    resolved = apply_stage.resolve_section(conn, cfg, slug, section)
    wiki.ensure(cfg.wiki_dir, slug, title=page, section=resolved)
    wiki.set_slot(cfg.wiki_dir, slug, slot, value, source=source, section=resolved,
                  conn=conn)
    # The wiki page list is part of the brief, so a new page has to show up there.
    brief.write(conn, cfg)
    return True, f"{resolved}/{slug}.{slot} = {value}"
