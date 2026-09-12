"""Immediate writes from the user or an agent surface."""

from __future__ import annotations

import sqlite3
from collections import Counter

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
        conn, stream="agent", external_id=f"live:{stamp}:{db.slugify(text, 32)}",
        ts=stamp, text=text, thread="conversation", person=speaker,
        from_me=(speaker == "me"), addressed_to="machine",
        gated=True, gate_reason="live",
    )
    item = _Row(ts=stamp, stream="agent", thread="conversation", person=speaker,
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


def _cited_ts(conn: sqlite3.Connection, ids: tuple[int, ...]) -> str | None:
    """When the cited observations were *said*: the newest source timestamp.

    Precedence runs on evidence time, so a source-backed correction carries
    its messages' hour — never the tool invocation's. Unparseable rows fall
    back to the archive, never to now: inventing an instant would mint
    authority from nothing.
    """
    stamps = []
    for row in conn.execute(
            f"""SELECT ts FROM archive WHERE id IN
                ({",".join("?" * len(ids))})""", ids):
        try:
            stamps.append(db.parse_ts(str(row["ts"] or "")))
        except (TypeError, ValueError):
            continue
    if not stamps:
        return None
    return max(stamps).isoformat(timespec="seconds")


def _stamp_live(conn: sqlite3.Connection, kind: str, ref: str, verb: str, *,
                origin: Origin = actions.UNKNOWN, fields: dict | None = None,
                based_on: str = "", op_id: str = "", commit: bool = True) -> None:
    """Record provenance, evidence and the completed operation for a typed write.

    Provenance says which writer touched the row; evidence points at the lines the user
    was looking at when they said it, which a caller can only supply if it knows its own
    turn; and the action record is the part the nightly pass reads. Only explicitly
    cited lines count as reviewed — the authorship turn never does.
    """
    origin, _dropped = _valid_citations(conn, origin)
    trace.stamp(conn, kind=kind, ref=ref, verb=verb, entity="agent:live",
                stage="live", run_id=None, generation_id=None,
                archive_ids=[*origin.archive_ids, *origin.cited],
                review_ids=list(origin.cited))
    actions.record(conn, kind=kind, ref=ref, verb=verb, origin=origin,
                   fields=fields or {}, based_on=based_on, op_id=op_id,
                   commit=commit)


def add_event(conn: sqlite3.Connection, cfg: Config, *, title: str, when: str,
              origin: Origin = actions.UNKNOWN,
              **fields) -> tuple[events.Event, str]:
    """A plan the user just described. No model: they said the fields, the agent has them."""
    title = (title or "").strip()
    if not title:
        raise LiveError("an event needs a title")
    start, _span = db.parse_when(when)
    payload = {k: v for k, v in fields.items() if v not in (None, "", [])}
    payload.update(title=title, date=start.isoformat())
    if payload.get("until"):
        payload["until"] = db.parse_when(str(payload["until"]))[0].isoformat()
    op = actions.plan(kind="event", ref=f"new:{db.slugify(title, 48)}",
                      verb="inserted", origin=origin, request=payload, at=db.now())
    with _atomic(conn):
        if actions.seen(conn, op):
            # Replay inside the write transaction so the check cannot go stale.
            existing = events.find_match(conn, title=title, on=payload["date"],
                                         participants=payload.get("participants") or [])
            if existing is not None:
                return existing, "unchanged"
        event, verb = events.upsert(conn, payload, written_by="live", commit=False)
        if verb != "unchanged":
            _stamp_live(conn, "event", event.key, verb, origin=origin, commit=False,
                        op_id=op,
                        fields={name: ["", str(payload.get(name) or "")]
                                for name in ("date", "time", "title", "location",
                                             "status")
                                if payload.get(name)})
    _refresh(conn, cfg, event.key)
    return event, verb


def update_event(conn: sqlite3.Connection, cfg: Config, which: str, *,
                 add_participants: list[str] | None = None,
                 remove_participants: list[str] | None = None,
                 origin: Origin = actions.UNKNOWN,
                 **changes) -> tuple[events.Event, list[str]]:
    """Change a row the user can see. Returns it rendered, so nothing needs re-reading."""
    event = find_event(conn, which)
    payload: dict = {k: v for k, v in changes.items() if v not in (None, "", [])}
    # Empty string means clear; omitted means untouched. Only the typed path may clear.
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
    if add_participants or remove_participants:
        removed = {name.casefold() for name in (remove_participants or [])}
        payload["participants"] = sorted(
            (set(event.participants) | set(add_participants or []))
            - {name for name in event.participants if name.casefold() in removed})
    if not payload and not wipe:
        # Name clearable fields so a refused clear reads as a refusal, not a no-op.
        asked = [name for name, value in changes.items() if value == ""]
        if asked:
            raise LiveError(
                f"{', '.join(asked)} cannot be emptied — only "
                f"{', '.join(events.CLEARABLE)} can. To drop the row entirely, "
                "set status to 'declined'.")
        raise LiveError("nothing to change")

    before = {name: getattr(event, name) for name in events.MUTABLE}
    based_on = _version_of(conn, "event", event.key)
    # Always by key so re-matching cannot retarget the write.
    payload["key"] = event.key
    payload.setdefault("date", event.date)
    # Citations are validated before the mutation, and only cited lines count as
    # reviewed: the authorship turn proves who asked, never what was considered.
    origin, dropped = _valid_citations(conn, origin)
    # A source-backed correction carries its messages' hour into precedence: an
    # old line cannot undo a newer settlement, while a genuinely newer line
    # applies whenever it was read. A plain user correction carries its turn's
    # hour the same way — what the user said at 11:00 outranks a 10:00 message
    # and yields to genuinely newer evidence either side of it. Only a write
    # with neither turn nor citation keeps the old run-time semantics. One
    # stamp covers the changed fields — per-line field attribution is not
    # recoverable from text — while untouched fields keep whatever evidence
    # time they already hold.
    evidence_ts = None
    support = list(origin.cited) or list(origin.archive_ids)
    if support:
        said_at = _cited_ts(conn, tuple(support))
        if said_at is not None:
            evidence_ts = {name: said_at for name in payload
                           if name in events.MUTABLE}
    # Plan replay keys from the request, not current values, so retries stay recognisable.
    op = actions.plan(kind="event", ref=event.key, verb="updated", origin=origin,
                      request={**payload, "clear": sorted(wipe)}, at=db.now())
    with _atomic(conn):
        if actions.seen(conn, op):
            return event, []
        updated, _verb = events.upsert(conn, payload, written_by="live", match=False,
                                       clear=wipe, evidence_ts=evidence_ts,
                                       replace_participants=bool(remove_participants),
                                       commit=False)
        moved = {name: [str(before[name]), str(getattr(updated, name))]
                 for name in events.MUTABLE
                 if str(before[name]) != str(getattr(updated, name))}
        if not moved and evidence_ts and _would_change(before, payload, wipe):
            # Everything asked for lost on evidence time: the cited lines are
            # older than what settled these fields. Say so plainly instead of
            # reporting a no-op — and record nothing, so no mark moves either.
            raise LiveError(
                "those lines are older than what's stored — they can't revise it. "
                "Cite newer evidence, or restate this as a new correction.")
        # Record operations only when something moved; no-op calls are not decisions.
        if moved:
            _stamp_live(conn, "event", updated.key, "updated", origin=origin,
                        fields=moved, based_on=based_on, op_id=op, commit=False)
    _refresh(conn, cfg, updated.key)
    changed = [f"{name}: {old} → {new}" for name, (old, new) in sorted(moved.items())]
    return updated, changed


def _would_change(before: dict, payload: dict, wipe: tuple) -> bool:
    """Did the request ask for anything different from the stored row?"""
    if wipe:
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
        with _atomic(conn):
            rule = series.end(conn, slug, written_by="live", commit=False)
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
              "join_url": join_url, "source": "agent:live"}
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
    """Write one durable fact directly to a wiki slot."""
    page, slot, value = (page or "").strip(), (slot or "").strip(), (value or "").strip()
    if not (page and slot and value):
        return False, "page, slot and value are all required"
    if len(value) > apply_stage.MAX_SLOT_VALUE:
        return False, (f"value is {len(value)} chars; a slot holds a bare answer "
                       f"(under {apply_stage.MAX_SLOT_VALUE}), not a sentence")

    slug = db.slugify(page)
    resolved = apply_stage.resolve_section(conn, cfg, slug, section)
    wiki.ensure(cfg.wiki_dir, slug, title=page, section=resolved)
    wiki.set_slot(cfg.wiki_dir, slug, slot, value, source=source, section=resolved,
                  conn=conn)
    # The wiki page list is part of the brief, so a new page has to show up there.
    brief.write(conn, cfg)
    return True, f"{resolved}/{slug}.{slot} = {value}"
