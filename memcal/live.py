"""Immediate writes from the user or an agent surface."""

from __future__ import annotations

import sqlite3
from collections import Counter

from . import archive, brief, db, events, gate, series, todos, trace, wiki
from .config import Config
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
    # A row the user just confirmed goes onto their real calendar, so it is on their
    # phone at the door rather than in a private store they have to ask about. Only
    # `confirmed` commitments qualify (`ical.publishable`), and a failure is logged
    # rather than raised: memcal is the system of record and the next pass retries.
    if keys:
        from .sources import ical                                   # noqa: PLC0415
        ical.publish_pending(conn, cfg, keys=[k for k in keys if k])


def find_event(conn: sqlite3.Connection, needle: str) -> events.Event:
    """Find one event by handle, key, or title; reject ambiguous matches."""
    needle = (needle or "").strip()
    if not needle:
        raise LiveError("which row? give its E# handle or the words it is listed under")

    # Read surfaces put E# on a row precisely so a follow-up can name that *row*, even
    # when its title has siblings.  Do this before the loose title search below: E# is
    # an address, not a word that happens to occur in a title.
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


def _stamp_live(conn: sqlite3.Connection, kind: str, ref: str, verb: str) -> None:
    """Record provenance for a direct typed write."""
    trace.stamp(conn, kind=kind, ref=ref, verb=verb, entity="agent:live",
                stage="live", run_id=None, generation_id=None, archive_ids=[])


def add_event(conn: sqlite3.Connection, cfg: Config, *, title: str, when: str,
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
    event, verb = events.upsert(conn, payload, written_by="live")
    _stamp_live(conn, "event", event.key, verb)
    _refresh(conn, cfg, event.key)
    return event, verb


def update_event(conn: sqlite3.Connection, cfg: Config, which: str, *,
                 add_participants: list[str] | None = None,
                 **changes) -> tuple[events.Event, list[str]]:
    """Change a row the user can see. Returns it rendered, so nothing needs re-reading."""
    event = find_event(conn, which)
    payload: dict = {k: v for k, v in changes.items() if v not in (None, "", [])}
    # An argument the caller *passed* as empty is a request to empty that field; one they
    # omitted arrives as None and means nothing at all. `**changes` keeps the two apart,
    # and this is the only layer that can tell — by the time it reaches `upsert` both are
    # falsy. Confined to the typed human-initiated path on purpose: a person saying "no,
    # there's no location for that" is a correction, while a model returning a partial
    # diff omits fields constantly and must never be read as deleting them.
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
    if add_participants:
        payload["participants"] = sorted(set(event.participants) | set(add_participants))
    if not payload and not wipe:
        # An empty value for a field that cannot be emptied is a caller trying to do
        # something real and being told "nothing to change", which reads as a no-op
        # rather than as a refusal. Name the fields that do accept it.
        asked = [name for name, value in changes.items() if value == ""]
        if asked:
            raise LiveError(
                f"{', '.join(asked)} cannot be emptied — only "
                f"{', '.join(events.CLEARABLE)} can. To drop the row entirely, "
                "set status to 'declined'.")
        raise LiveError("nothing to change")

    before = {name: getattr(event, name) for name in events.MUTABLE}
    # Always by key: a row found by its words must not be re-matched by title, or a
    # status change lands on whichever row `find_match` liked better.
    payload["key"] = event.key
    payload.setdefault("date", event.date)
    updated, _verb = events.upsert(conn, payload, written_by="live", match=False,
                                  clear=wipe)
    _stamp_live(conn, "event", updated.key, "updated")
    _refresh(conn, cfg, updated.key)
    changed = [f"{name}: {before[name]} → {getattr(updated, name)}"
               for name in events.MUTABLE
               if str(before[name]) != str(getattr(updated, name))]
    return updated, changed


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
                 ) -> tuple[series.Series, list[str]]:
    """Create or update a recurring schedule, then project its occurrences."""
    slug = db.slugify(which or "")
    if not slug:
        raise LiveError("which recurring thing? give its name, e.g. 'tutoring'")
    known = series.get(conn, slug)
    if known is None:
        # Adopting an existing group of rows is the common case: the user is naming something
        # memcal already has instances of, not inventing a new thing.
        row = conn.execute("SELECT title FROM events WHERE series = ? ORDER BY date DESC"
                           " LIMIT 1", (slug,)).fetchone()
        title = row["title"] if row else which.strip()
    else:
        title = known.title

    if ended:
        if known is None:
            raise LiveError(f"memcal has no recurring {which!r} to end")
        rule = series.end(conn, slug, written_by="live")
        _stamp_live(conn, "series", slug, "ended")
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
        # A new rule, or one whose *shape* changed, takes effect from today unless the user
        # named a day. Restating the link is not a schedule change and must not move the
        # anchor — see `apply._schedule_moved` for the same rule one surface over.
        fields.setdefault("effective_on", db.today().isoformat())
    if len(fields) <= 3:                       # slug, title, source and nothing said
        raise LiveError("say how often it repeats, or what changed about it")

    rule, verb = series.upsert(conn, fields, written_by="live")
    if verb == "unchanged":
        return rule, []
    _stamp_live(conn, "series", slug, verb)
    log = series.roll_forward(conn, slug=slug)
    from .sources import ical                                        # noqa: PLC0415
    log += ical.publish_schedules(conn, cfg, slugs=[slug])
    # Every upcoming occurrence, because a cadence change moves all of them and each one
    # has its own copy on the real calendar to keep in step.
    upcoming = [row["key"] for row in conn.execute(
        "SELECT key FROM events WHERE series = ? AND date >= ?",
        (slug, db.today().isoformat()))]
    _refresh(conn, cfg, *upcoming)
    return rule, log


def move_one_occurrence(conn: sqlite3.Connection, cfg: Config, which: str, *,
                        to: str, time: str | None = None,
                        cancelled: bool = False) -> tuple[events.Event, str]:
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
    updated, _ = events.upsert(conn, payload, written_by="live", match=False)
    _stamp_live(conn, "event", updated.key, "moved-once")
    series.roll_forward(conn, slug=event.series)
    _refresh(conn, cfg, updated.key)
    return updated, replaced or event.date


def merge_events(conn: sqlite3.Connection, cfg: Config, keep: str,
                 drop: str) -> events.Event:
    """The user says two rows are one thing. They are the authority; just do it."""
    survivor, doomed = find_event(conn, keep), find_event(conn, drop)
    if survivor.key == doomed.key:
        raise LiveError("those are the same row already", row=survivor.one_line())
    merged = events.merge(conn, survivor.key, doomed.key)
    if merged is None:
        raise LiveError("could not merge those two")
    _refresh(conn, cfg, merged.key)
    return merged


def drop_event(conn: sqlite3.Connection, cfg: Config, which: str) -> str:
    """Delete a spurious row; declined real events should be updated instead."""
    event = find_event(conn, which)
    line = event.one_line()
    events.delete(conn, event.key)
    _stamp_live(conn, "event", event.key, "dropped")
    _refresh(conn, cfg)
    return line


def open_todo(conn: sqlite3.Connection, cfg: Config, text: str, *, due: str | None = None,
              remind: str | bool | None = None,
              wake_condition: str | None = None,
              event: str | None = None,
              key: str | None = None) -> tuple[todos.Todo, str]:
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
        # What this to-do already knows counts. Adding a reminder to something that has
        # been open for a week is the ordinary case, and reading only the arguments of
        # *this* call made the store's own due date and event link invisible.
        #
        # Several things can anchor a reminder and they disagree, so try them in order
        # of authority and take the first that still lies ahead. A to-do can outlive the
        # occurrence it was linked to — "remind you about the tattoo session" points at
        # the one on the 11th, which has happened, while its own due date points at the
        # session on the 24th, which has not. Stopping at the first anchor that *exists*
        # reads the stalest one and concludes there is nothing to remind about.
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
    todo, verb = todos.open_todo(
        conn, text, key=key, due=when_due, remind_at=remind_at,
        wake_condition=(wake_condition or "").strip() or None,
        event_id=linked.id if linked else None, written_by="live",
        auto_remind=cfg.remind_deadlines)
    _stamp_live(conn, "todo", todo.key, verb)
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


def close_todo(conn: sqlite3.Connection, cfg: Config, which: str) -> todos.Todo:
    """Close a to-do the user explicitly completed."""
    todo = todos.find(conn, (which or "").strip())
    if not todo:
        raise LiveError(f"no open to-do matches {which!r}")
    todos.close(conn, todo.key)
    _stamp_live(conn, "todo", todo.key, "closed")
    # And take it back off their phone. A reminder that survives the thing it was about
    # is an orphaned record left behind by something deleted, still
    # asserting itself once a day — and here it asserts itself by buzzing.
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
