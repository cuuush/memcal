"""Renders brief.md for persistent agent context."""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from . import activity, archive, db, events, presentation, series, textclean, threads, todos, wiki
from .config import Config

SOURCE_RE = re.compile(r"〔([ETQS]\d+)〕")


def source_tag(kind: str, row_id: int | None) -> str:
    """Returns a short, stable handle from a brief line back to source evidence."""
    prefix = {"event": "E", "todo": "T", "question": "Q", "standing": "S"}.get(kind)
    return f"〔{prefix}{row_id}〕" if prefix and row_id is not None else ""


def parse_source(token: str) -> tuple[str, int] | None:
    text = (token or "").strip().upper()
    if len(text) < 2 or not text[1:].isdigit():
        return None
    kind = {"E": "event", "T": "todo", "Q": "question", "S": "standing"}.get(text[0])
    return (kind, int(text[1:])) if kind else None


def structured(text: str) -> list[dict]:
    """Parses rendered brief text into line entries with extracted source tags."""
    return [{"text": line, "sources": SOURCE_RE.findall(line)}
            for line in (text or "").splitlines()]


#: Explains handles and wiki pages for each surface. Examples use `#` rather than
#: digits to prevent scanners from misinterpreting them as active handles.
#: Agent gets one quiet line; CLI keeps a slightly clearer spelling.
LEGENDS = {
    "agent": "[〔E#〕〔T#〕〔Q#〕 handles and pages open with memcal_open]\n\n",
    "cli": ("[〔E#〕〔T#〕〔Q#〕 handles open with `memcal open E258`. "
            "Pages open with `memcal open <name>`; the names in parentheses "
            "after a page are the facts it holds]\n\n"),
}
DEFAULT_SURFACE = "agent"


def legend(surface: str = DEFAULT_SURFACE) -> str:
    return LEGENDS.get(surface) or LEGENDS[DEFAULT_SURFACE]


#: Default agent legend string for backward compatibility.
LEGEND = legend(DEFAULT_SURFACE)


def render(conn: sqlite3.Connection, cfg: Config, ref: date | None = None,
           surface: str = DEFAULT_SURFACE) -> str:
    ref = ref or db.today()
    # Retain audit rows while removing obligations whose linked event is no longer live.
    todos.expire_event_links(conn)
    # The two window scans (the week range, and the wider Later range) are the
    # expensive part of a render. Run each once here and thread the results
    # through the blocks and the post-trim reconciliation, so nothing re-queries.
    window = events.window(conn, cfg.days_back, cfg.days_forward, ref)
    later = _later_selection(conn, cfg, ref)
    rendered = _rendered_events(conn, window, later[0])
    represented = {ev.key for ev in rendered}
    id_to_key = {ev.id: ev.key for ev in rendered}
    blocks = [
        # Renders first because it represents immediate items rather than future state.
        _now_block(conn),
        _week_block(conn, cfg, ref, window=window, represented=represented),
        _later_block(conn, cfg, ref, selection=later),
        _recurring_block(conn, ref),
        _open_block(conn),
        _ask_block(conn),
        _facts_block(conn, cfg),
    ]
    text = "\n\n".join(b for b in blocks if b).rstrip() + "\n"
    trimmed = _trim(legend(surface) + text, cfg.brief_token_cap)
    return _reconcile_coverage(conn, trimmed, cfg.brief_token_cap, id_to_key=id_to_key)


#: Inline activity hints are the exception path, not the rule: past this many in
#: one block the rest fold into a single overflow line rather than burying plans.
MAX_HINTS_PER_BLOCK = 8

#: Display cap so a never-reviewed / huge thread cannot dominate the brief
#: (2351-style). Past this the brief shows "99+".
HINT_COUNT_CAP = 99

#: Frozen hint copy for Integrations / Hermes to mirror. Hint ≠ apply: this
#: line only flags; it never invents a replacement date, address, or status.
#: See docs/notes/freshness-gap-81.md.
ACTIVITY_HINT_FORMAT = (
    "  ↳ New activity: {where} — {count} message(s){extra} since this plan was "
    "reviewed. It may have changed; open with memcal_activity(handle={handle}) "
    "before giving current details."
)

#: Same flag for a plan that was never reviewed: the signal (a linked thread
#: has traffic) is still worth surfacing, but the copy must not claim a review
#: that never happened. The count is capped (HINT_COUNT_CAP) so a large
#: never-reviewed thread cannot dominate the brief.
ACTIVITY_HINT_UNREVIEWED_FORMAT = (
    "  ↳ New activity: {where} — {count} message(s){extra} on a linked thread "
    "not yet reviewed. It may bear on this plan; open with "
    "memcal_activity(handle={handle}) before giving current details."
)

#: Phone-ish / long opaque ids must never appear in a brief hint label.
_PHONE_LIKE = re.compile(r"^\+?[\d\s\-().]{7,}$")
_LONG_HEX = re.compile(r"^[0-9a-fA-F\-_]{24,}$")
_LONG_B64 = re.compile(r"^[A-Za-z0-9+/=]{28,}$")


def _looks_like_raw_id(text: str) -> bool:
    """True when a label would leak a phone, email handle, uuid, or opaque id."""
    t = (text or "").strip()
    if not t:
        return True
    if threads.is_opaque(t):
        return True
    if _PHONE_LIKE.match(t):
        return True
    # Bare email / address handles are not brief-safe labels.
    if "@" in t and " " not in t:
        return True
    compact = t.replace("-", "").replace("_", "")
    if len(compact) >= 24 and all(c in "0123456789abcdefABCDEF" for c in compact):
        return True
    if _LONG_HEX.match(t) or _LONG_B64.match(t):
        return True
    return False


def _hint_label(conn: sqlite3.Connection, stream: str, thread: str) -> str:
    """Safe human label for an activity hint.

    Prefer ``threads.label`` / whois display names when richer; else a human
    thread name; else ``threads.title()``. Never print raw phone / email /
    Proton / opaque ids — fall back to "unknown number" (phone-shaped),
    "unknown sender" (email), or a short title.
    """
    stored = ""
    if thread:
        row = conn.execute(
            "SELECT label FROM threads WHERE stream = ? AND thread = ?",
            (stream, thread)).fetchone()
        stored = ((row["label"] if row else None) or "").strip()
    titled = (threads.title(conn, stream, thread) or "").strip() if thread else ""
    thread_name = (thread or "").strip()
    safe_stored = stored if stored and not _looks_like_raw_id(stored) else ""
    safe_titled = titled if titled and not _looks_like_raw_id(titled) else ""
    safe_thread = (thread_name if thread_name and not _looks_like_raw_id(thread_name)
                   else "")
    # CoS: label / whois display first when richer; human room name before a
    # handle-shaped title(); never the raw id.
    if safe_stored and safe_titled:
        pick = safe_stored if len(safe_stored) >= len(safe_titled) else safe_titled
    else:
        pick = safe_stored or safe_titled
    pick = pick or safe_thread
    if pick:
        return pick
    raw = thread_name or (stream or "").strip()
    if not raw:
        return "unknown"
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 7 and (_PHONE_LIKE.match(raw) or raw.startswith("+")
                             or digits == raw.lstrip("+")):
        return "unknown number"
    if _looks_like_raw_id(raw):
        if "@" in raw:
            return "unknown sender"
        return "unknown number" if len(digits) >= 7 else (
            raw[:24] + "…" if len(raw) > 24 else raw)
    return raw if len(raw) <= 40 else raw[:39] + "…"


def _format_hint_count(total: int) -> str:
    if total > HINT_COUNT_CAP:
        return f"{HINT_COUNT_CAP}+"
    return str(total)


def _activity_hint(conn: sqlite3.Connection, event) -> str | None:
    """One compact warning when a plan's linked sources have new traffic.

    Says the plan *may* have changed and names how to read the messages. It
    never invents a replacement date, address, or status. Calendar (UNTHREADED
    ical) self-feed churn is not a hint — only conversational strong links.
    A never-reviewed plan still hints (with honest, capped copy); dropping it
    entirely hid legitimate new activity on the great majority of plans.
    """
    found = activity.pending(
        conn, "event", event.key, limit=3, strong_only=True,
        conversational_only=True)
    if not found["strong_total"]:
        return None
    first = found["strong"][0]
    label = _hint_label(conn, first["stream"], first.get("thread") or "")
    where = f"{first['stream']}/{label}" if label else first["stream"]
    # "+N more" would re-print the raw total the cap is meant to hide, so it is
    # only shown while the count itself is uncapped ("99+" already means "more").
    hidden = found["strong_total"] - len(found["strong"])
    extra = (f" (+{hidden} more)"
             if hidden > 0 and found["strong_total"] <= HINT_COUNT_CAP else "")
    handle = source_tag("event", event.id).strip("〔〕")
    # "since reviewed" only when a review actually happened; otherwise flag the
    # traffic honestly. The count is capped either way so a huge never-reviewed
    # thread cannot dominate the brief (2351-style).
    template = (ACTIVITY_HINT_FORMAT if (found["reviewed"] or found["mark"])
                else ACTIVITY_HINT_UNREVIEWED_FORMAT)
    return template.format(
        where=where, count=_format_hint_count(found["strong_total"]),
        extra=extra, handle=handle)


def _collection_line(conn: sqlite3.Connection, cfg: Config) -> str | None:
    """What collection knows it does not know, in one line.

    Failed and unavailable attempts are coverage holes. Incomplete ones are a
    different, milder gap — more is waiting, not lost — and read that way. A
    source that never ran on a store that otherwise collects is named rather
    than silently counted as checked; on a virgin store there is nothing to
    contrast it with, and setup (not this line) is the authority.
    """
    try:
        from . import sources as sources_pkg
        known = sorted(s.name for s in sources_pkg.all_sources(cfg) if s.in_all)
    except Exception:
        return None
    if not known:
        return None
    in_use = conn.execute(
        "SELECT 1 FROM collections WHERE finished_at IS NOT NULL LIMIT 1"
    ).fetchone() is not None
    bad = []
    for stream in known:
        attempt = archive.last_source_attempt(conn, stream)
        if attempt is None:
            if in_use:
                bad.append(f"{stream} (not yet checked)")
            continue
        status = attempt.get("status") or ""
        if status in ("failed", "unavailable"):
            bad.append(f"{stream} ({str(attempt.get('error') or 'unavailable')[:60]})")
        elif status == "incomplete":
            bad.append(f"{stream} (incomplete — more waiting)")
    if not bad:
        return None
    shown = ", ".join(bad[:3]) + (f" +{len(bad) - 3} more" if len(bad) > 3 else "")
    return f"[COLLECTION: {shown} — recent messages may be missing]"


def _rendered_events(conn: sqlite3.Connection, window: list[events.Event],
                     later_shown: list[events.Event]) -> list[events.Event]:
    """The event rows the brief actually puts on the page, mirroring `_week_block`.

    A nested (`part_of`) event is rendered only as a child of a main that is
    itself in the week window; a nested window event whose parent falls outside
    the window is never emitted, so it must not be counted as surfaced. This is
    the single definition of "surfaced" — `represented_keys` and `id_to_key`
    both derive from it so pre-trim coverage and post-trim survivors agree.
    """
    mains = [ev for ev in window if not ev.part_of]
    out = list(mains)
    for ev in mains:
        out.extend(events.children_of(conn, ev.id))
    out.extend(later_shown)
    return out


def represented_keys(conn: sqlite3.Connection, cfg: Config,
                     ref: date | None = None) -> set[str]:
    """Keys of the events this brief actually surfaces: the week window's rendered
    rows with their children, plus the Later selection after its cap.

    A thread linked only to an event absent here has no visible activity hint
    standing in for it, so the backlog must not treat it as covered. Date-range
    membership is not enough — an unconfirmed opportunity, an event past the
    Later cap, or a nested event whose parent is outside the window sits in range
    yet is never rendered.
    """
    ref = ref or db.today()
    window = events.window(conn, cfg.days_back, cfg.days_forward, ref)
    later_shown = _later_selection(conn, cfg, ref)[0]
    return {ev.key for ev in _rendered_events(conn, window, later_shown)}


#: Non-PII stand-in when unlinked backlog exists. Never names stream/thread
#: (the old `[UNREVIEWED: stream/thread …]` form leaked phones and handles).
_BACKLOG_NOTICE = (
    "[coverage incomplete — unreviewed traffic not linked to any plan]"
)


def _backlog_lines(conn: sqlite3.Connection, cfg: Config,
                   ref: date | None = None, *,
                   represented: "set[str] | None" = None) -> list[str]:
    """Unreviewed traffic no rendered plan is associated with — one non-PII line.

    Coverage is judged against the events this brief actually surfaces. The
    old per-thread `[UNREVIEWED: stream/thread …]` footer dumped identifiers;
    MVP emits at most a single non-identifying notice (or nothing).
    """
    if represented is None:
        represented = represented_keys(conn, cfg, ref)
    if activity.unlinked_backlog(conn, represented=represented):
        return [_BACKLOG_NOTICE]
    return []


def _block_hints(conn: sqlite3.Connection, ordered) -> dict:
    """Precompute one block's hints in emission order, children included.

    One `pending` lookup per event up front, so the overflow line below can
    name exactly which retained rows it stands in for.
    """
    hints = {}
    for ev in ordered:
        hint = _activity_hint(conn, ev)
        if hint:
            hints[ev.id] = hint
    return {"hints": hints,
            "flagged": [ev.id for ev in ordered if ev.id in hints],
            "shown": 0, "overflow_done": False}


def _hint_after(lines: list[str], event, state: dict) -> None:
    """An inline hint right under its event, so trimming keeps or drops both.

    Past the per-block cap a single overflow line names the remaining affected
    retained rows instead of leaving them warning-less. It is emitted beside
    the last inline hint, so trimming drops named rows before the line naming
    them — never the reverse. `state` comes from `_block_hints`.
    """
    hint = state["hints"].get(event.id)
    if hint is None:
        return
    if state["shown"] < MAX_HINTS_PER_BLOCK:
        lines.append(hint)
        state["shown"] += 1
        return
    if state["overflow_done"]:
        return
    try:
        rest = state["flagged"][state["flagged"].index(event.id):]
    except ValueError:
        rest = [event.id]
    names = " ".join(source_tag("event", i).strip("〔〕") for i in rest[:10])
    extra = f" (+{len(rest) - 10} more)" if len(rest) > 10 else ""
    lines.append(f"  ↳ …and {len(rest)} more row(s) with new activity:"
                 f" {names}{extra}")
    state["overflow_done"] = True


def _week_block(conn: sqlite3.Connection, cfg: Config, ref: date, *,
                window: "list[events.Event] | None" = None,
                represented: "set[str] | None" = None) -> str:
    rows = window if window is not None \
        else events.window(conn, cfg.days_back, cfg.days_forward, ref)
    # Anchors the reference date explicitly to prevent incorrect date inference.
    lines = [f"## This week  (today is {ref.strftime('%A %-d %B %Y')})"]
    if not rows:
        lines.append("(nothing known)")
    else:
        via = attribution(conn)
        asked = todos.questions_by_event(conn)
        nested = {e.id for e in rows if e.part_of}
        mains = [ev for ev in rows if ev.id not in nested]
        # Emission interleaves each main with its own children, so `ordered`
        # (and thus `flagged`) must be built the same way — otherwise the
        # overflow line's slice names the wrong rows and count.
        ordered = []
        for ev in mains:
            ordered.append(ev)
            ordered.extend(events.children_of(conn, ev.id))
        state = _block_hints(conn, ordered)
        for ev in mains:
            marker = "· " if db.parse_date(ev.date) < ref else ""
            lines.append(f"{source_tag('event', ev.id)} {marker}"
                         + ev.one_line(extra=[via.get(ev.key, "")], overview=True))
            _hint_after(lines, ev, state)
            lines.extend(_question_lines(asked.get(ev.id, [])))
            lines.extend(_child_lines(conn, ev, asked, state))
    # Explicit date bounds signal completeness to avoid unnecessary range lookups.
    first = (ref - timedelta(days=cfg.days_back)).strftime("%a %-d %b")
    last = (ref + timedelta(days=cfg.days_forward)).strftime("%a %-d %b")
    lines.append(f"[complete for {first} – {last}; look up anything outside that]")
    # Distinguishes an empty schedule from stale ingestion streams.
    stale = archive.stale_streams(conn, cfg=cfg)
    if stale:
        behind = ", ".join(f"{name} {age}" for name, age in stale)
        lines.append(f"[STALE: no {behind} — this week may be incomplete]")
    collection = _collection_line(conn, cfg)
    if collection:
        lines.append(collection)
    lines.extend(_backlog_lines(conn, cfg, ref, represented=represented))
    return "\n".join(lines)


#: Maximum days beyond the active window to include in the Later section.
LATER_DAYS = 45

#: Maximum number of entries displayed in the Later section.
LATER_LIMIT = 8


def _later_selection(conn: sqlite3.Connection, cfg: Config,
                     ref: date) -> tuple[list[events.Event], int]:
    """The Later rows and their total. The one place the selection is decided, so
    what the block renders and what the backlog treats as covered cannot diverge."""
    edge = ref + timedelta(days=cfg.days_forward)
    rows = [e for e in events.window(conn, 0, cfg.days_forward + LATER_DAYS, ref)
            if db.parse_date(e.date) > edge and _committed(e)]
    return rows[:LATER_LIMIT], len(rows)


def _later_block(conn: sqlite3.Connection, cfg: Config, ref: date, *,
                 selection: "tuple[list[events.Event], int] | None" = None) -> str:
    """Renders upcoming committed events beyond the active weekly window."""
    shown, total = selection if selection is not None \
        else _later_selection(conn, cfg, ref)
    if not shown:
        return ""
    via = attribution(conn)
    asked = todos.questions_by_event(conn)
    lines = ["## Later"]
    state = _block_hints(conn, shown)
    for ev in shown:
        who = f" ({ev.subject})" if ev.needs_subject() else ""
        invite = [ev.plain_state(), f"invite: {events._short_url(ev.rsvp_url)}"] \
            if ev.rsvp_url else []
        act = [f"join: {ev.join_url}"] if ev.join_url else []
        title, platform = events.split_platform(ev.title)
        home = [f"via {platform}"] if platform else []
        hosts = ev.visible_hosts()
        hosted = ["hosted by " + ", ".join(hosts[:3])
                  + (f" +{len(hosts) - 3}" if len(hosts) > 3 else "")] if hosts else []
        tail = [t for t in (*invite, *act, *home, *hosted,
                            _duration(ev), via.get(ev.key, "")) if t]
        lines.append(f"{source_tag('event', ev.id)} {_when_phrase(ev)}  \"{title}\"{who}"
                     + (" — " + " · ".join(tail) if tail else ""))
        _hint_after(lines, ev, state)
        lines.extend(_question_lines(asked.get(ev.id, [])))
    if total > LATER_LIMIT:
        lines.append(f"[{total - LATER_LIMIT} more beyond this; ask for a date]")
    return "\n".join(lines)


def _committed(event: events.Event) -> bool:
    """Returns True if the event qualifies for the Later block.

    Includes commitments, relevant availability, confirmed opportunities, and events
    with RSVP URLs. Excludes declined events unless an RSVP URL remains present.
    """
    if event.status == "declined":
        # Retains declined invitations with an RSVP link for response management.
        return bool(event.rsvp_url)
    return (event.kind in ("commitment", "availability")
            or (event.kind == "opportunity" and event.status == "confirmed")
            or bool(event.rsvp_url))


def _when_phrase(event: events.Event) -> str:
    """Formats the date range occupied by an event spanning across multiple days."""
    start = db.parse_date(event.date)
    if not (event.until and event.until > event.date):
        return start.strftime("%a %-d %b")
    end = db.parse_date(event.until)
    # Omits the starting month if start and end share month and year.
    head = start.strftime("%a %-d") if end.month == start.month and end.year == start.year \
        else start.strftime("%a %-d %b")
    return f"{head} – {end.strftime('%a %-d %b')}"


def _duration(event: events.Event) -> str:
    """Formats multi-day span duration in days."""
    if not (event.until and event.until > event.date):
        return ""
    days = (db.parse_date(event.until) - db.parse_date(event.date)).days + 1
    return f"{days} days"


def _child_lines(conn: sqlite3.Connection, parent: events.Event, asked: dict,
                 state: dict | None = None) -> list[str]:
    """Formats child events and associated questions nested under a parent event."""
    out: list[str] = []
    for child in events.children_of(conn, parent.id):
        out.append(f"  ↳ {source_tag('event', child.id)} "
                   + child.one_line(overview=True))
        if state is not None:
            _hint_after(out, child, state)
        out.extend(f"  {line}" for line in _question_lines(asked.get(child.id, [])))
    return out


def _question_lines(questions: list) -> list[str]:
    """Formats open questions for display directly under their associated event or to-do."""
    return [f"  ↳ {source_tag('question', q['id'])} {presentation.question_line(q)}"
            for q in questions]


def attribution(conn: sqlite3.Connection) -> dict[str, str]:
    """Maps event keys to source chat or thread labels for events lacking participants."""
    names = threads.titles(conn)
    out: dict[str, str] = {}
    for row in conn.execute(
            "SELECT key, participants, subject, origin, source FROM events"):
        if db.jload(row["participants"], []) or (row["subject"] or "me") != "me":
            continue
        label = _origin_label(names, row["origin"] or row["source"] or "")
        if label:
            out[row["key"]] = f"from {label}"
    return out


def _origin_label(names: dict[tuple, str], origin: str) -> str:
    """Resolves an origin identifier to a human-readable name, omitting opaque IDs."""
    kind, _, rest = str(origin or "").partition(":")
    if kind == "person":
        name = rest.strip()
    elif kind == "thread":
        stream, _, thread = rest.partition(":")
        name = names.get((stream, thread), "").strip()
    else:
        return ""                      # ical:, partiful:, agent: — self-sourced rows
    return "" if not name or threads.is_opaque(name) else name


def _recurring_block(conn: sqlite3.Connection, ref: date) -> str:
    """Renders active series rules and their next scheduled occurrences."""
    rules = [r for r in series.all_active(conn) if r.projectable]
    if not rules:
        return ""
    lines = ["## Regularly"]
    for rule in rules:
        nxt = series.next_on(conn, rule, after=(ref - timedelta(days=1)).isoformat())
        row = None
        # A cancellation still occupies and explains this week's slot, but it is not
        # the next appointment. Walk to the first materialized, non-cancelled one.
        for _attempt in range(8):
            if nxt is None:
                break
            row = conn.execute(
                "SELECT id, date, time, status FROM events"
                "  WHERE series = ? AND (date = ? OR instead_of = ?) LIMIT 1",
                (rule.slug, nxt.isoformat(), nxt.isoformat())).fetchone()
            if row is None or row["status"] != "declined":
                break
            nxt = series.next_on(conn, rule, after=nxt.isoformat())
        # Skips rules lacking a materialized event occurrence.
        if row is None or nxt is None:
            continue
        said = f"{rule.title} — {rule.phrase}"
        if row["date"] != nxt.isoformat():
            said += (f"; this once on {db.parse_date(row['date']).strftime('%a %-d %b')}"
                     + (f" at {row['time']}" if row["time"] else ""))
        else:
            said += f"; next {nxt.strftime('%a %-d %b')}"
        if rule.join_url:
            said += f" · join: {rule.join_url}"
        lines.append(f"{source_tag('event', row['id'])} {said}")
    return "\n".join(lines) if len(lines) > 1 else ""


def _now_block(conn: sqlite3.Connection) -> str:
    """Renders open to-dos with due reminders, ignoring snooze state."""
    due = todos.due_reminders(conn, snooze=False)
    if not due:
        return ""
    lines = ["## Now"]
    for todo in due:
        lines.append(f"{source_tag('todo', todo.id)} {todo.one_line()} — reminder due")
    return "\n".join(lines)


def _open_block(conn: sqlite3.Connection) -> str:
    items = todos.open_items(conn)
    if not items:
        return ""
    evidence = todos.questions_by_todo(conn)
    lines = ["## Open"]
    for todo in items:
        lines.append(f"{source_tag('todo', todo.id)} {todo.one_line()}")
        for question in evidence.get(todo.id, []):
            lines.append(f"  ↳ {source_tag('question', question['id'])} "
                         f"{presentation.question_line(question)}")
    return "\n".join(lines)


def _ask_block(conn: sqlite3.Connection) -> str:
    # Excludes questions already rendered under their associated event or to-do.
    attached = {q["id"] for group in todos.questions_by_todo(conn).values() for q in group}
    attached |= {q["id"] for group in todos.questions_by_event(conn).values() for q in group}
    questions = [q for q in todos.open_questions(conn, limit=12) if q["id"] not in attached]
    if not questions:
        return ""
    return "\n".join(
        ["## Ask about"]
        + [f"{source_tag('question', q['id'])} {presentation.question_line(q)}"
           for q in questions[:6]]
    )


def _facts_block(conn: sqlite3.Connection, cfg: Config) -> str:
    lines = ["## People and facts"]
    about = _about_you_line(conn, cfg)
    if about:
        lines.append(about)
    index = _pages_line(cfg)
    if index:
        lines.append(index)
    return "\n".join(lines) if len(lines) > 1 else ""


#: Maximum character budget for the About-you line in the brief. Mirrors
#: PAGES_LINE_MAX_CHARS: one bounded line, whole facts only, never a cut value.
ABOUT_YOU_MAX_CHARS = 700

#: Stable pointer prefix for the self facts. `me` resolves through
#: `wiki.self_slug()` so the brief never hardcodes a personal slug.
ABOUT_YOU_PREFIX = "About you (open with memcal_open me): "

#: Overflow placeholder when trimming must drop self fact values. Keeps the
#: pointer so the facts stay one tool call away.
ABOUT_YOU_TRIMMED = ("About you (open with memcal_open me): "
                     "[trimmed — open the page for your facts]")


def _about_you_line(conn: sqlite3.Connection, cfg: Config) -> str:
    """One line of the user's own facts, verbatim, or "" when there is nothing to show.

    Only the resolved self slug's facts appear here; every other page keeps the
    existing page index plus on-demand access. Values are stored text, never a
    model summary, so home vs work addresses stay distinct and unknown facts
    stay absent. Reads never create a page.

    Budget/omission: whole facts in file/slot order (the order the slots were
    stored on the page) up to ABOUT_YOU_MAX_CHARS; trailing wholes are omitted
    with a "+N more" count and the prefix pointer is retained. The page index's
    four-label limit does not apply here. Missing/empty self page -> absent
    (no line). Ambiguity -> names the candidates with no projection.
    """
    try:
        slug = wiki.self_slug(conn, cfg.wiki_dir)
    except wiki.SelfAmbiguous as exc:
        return ("About you: ambiguous self page "
                f"({', '.join(exc.candidates)}) — open one with "
                "memcal_open <name> to inspect")
    if not wiki.exists(cfg.wiki_dir, slug):
        return ""
    page = wiki.read(cfg.wiki_dir, slug)
    if not page or not page.slots:
        return ""
    items = [f"{slot}: {(info or {}).get('value', '')}"
             for slot, info in page.slots.items()]
    full = ABOUT_YOU_PREFIX + " · ".join(items)
    if len(full) <= ABOUT_YOU_MAX_CHARS:
        return full
    total = len(items)
    # Largest leading run that fits with its "+N more" suffix; file order.
    kept = 0
    for count in range(1, total):
        candidate = (ABOUT_YOU_PREFIX + " · ".join(items[:count])
                     + f" · +{total - count} more")
        if len(candidate) <= ABOUT_YOU_MAX_CHARS:
            kept = count
        else:
            break
    if kept:
        return (ABOUT_YOU_PREFIX + " · ".join(items[:kept])
                + f" · +{total - kept} more")
    if total == 1:
        return full       # One fact: show it whole even past the ceiling.
    first = ABOUT_YOU_PREFIX + items[0] + f" · +{total - 1} more"
    if len(ABOUT_YOU_PREFIX) + len(items[0]) <= ABOUT_YOU_MAX_CHARS:
        return first      # First fact fits alone; rest elide with a count.
    # Even the first value exceeds the budget: omit values whole, keep pointer.
    return ABOUT_YOU_PREFIX + f"+{total} more (open the page for the rest)"


#: Maximum character budget for the wiki pages index line in the brief.
PAGES_LINE_MAX_CHARS = 700


def _pages_line(cfg: Config) -> str:
    """Renders the wiki index line with slot summaries within the character budget.

    All slugs are preserved; slot descriptions are included in ascending order of length
    until the character budget is exhausted.
    """
    index = wiki.slot_index(cfg.wiki_dir)
    if not index:
        return ""
    described: dict[str, list[str]] = {}
    budget = PAGES_LINE_MAX_CHARS - len("Pages: ") - sum(
        len(slug) + 3 for slug in index)
    for slug, slots in sorted(index.items(), key=lambda kv: (len(", ".join(kv[1])), kv[0])):
        if not slots:
            continue
        cost = len(", ".join(slots)) + 3        # Account for " (" and ")".
        if cost > budget:
            break
        budget -= cost
        described[slug] = slots
    return "Pages: " + " · ".join(
        f"{slug} ({', '.join(described[slug])})" if slug in described else slug
        for slug in index)


def _trim(text: str, token_cap: int) -> str:
    """Trims brief text to fit within token_cap by dropping trailing lines from lower-priority sections.

    An event row and its activity warning drop as one unit: a retained row never
    loses its freshness qualification to trimming, and a dropped row takes its
    warning with it. Coverage footers survive whenever anything they cover does.
    """
    if textclean.estimate_tokens(text) <= token_cap:
        return text
    lines = text.splitlines()
    # Preserves headers and initial lines while dropping trailing lines until within budget.
    while textclean.estimate_tokens("\n".join(lines)) > token_cap and len(lines) > 8:
        drop = _dropped_index(lines)
        if drop is None:
            break
        del lines[drop[0]:drop[1]]
    out = "\n".join(lines).rstrip() + "\n"
    if textclean.estimate_tokens(out) <= token_cap:
        return out
    # Collapse self fact values to their pointer before any hard cut so an
    # address or URL is never bisected; the pointer keeps the facts one call away.
    collapsed = [ABOUT_YOU_TRIMMED if (line.startswith(ABOUT_YOU_PREFIX)
                                       and line != ABOUT_YOU_TRIMMED)
                 else line for line in lines]
    if collapsed != lines:
        squashed = "\n".join(collapsed).rstrip() + "\n"
        if textclean.estimate_tokens(squashed) <= token_cap:
            return squashed
        lines, out = collapsed, squashed
    if textclean.estimate_tokens(out) > token_cap:
        # A line-boundary cut keeps event/warning units whole and, since it never
        # cuts mid-line, an About-you value is dropped whole rather than bisected.
        out = _hard_cut(lines, token_cap)
    return out


def _is_event_line(line: str) -> bool:
    text = line.lstrip()
    return text.startswith("〔") or text.startswith("↳ 〔")


def _is_hint_line(line: str) -> bool:
    return line.startswith("  ↳ New activity:")


def _is_protected(line: str) -> bool:
    """Coverage footers: trimming eats events first.

    The overflow notice is deliberately not protected — it names retained rows,
    and once trimming reaches it those rows are going too.
    """
    return line.startswith("[") or line.startswith("Pages: ")


#: Lines that disclose a coverage hole (uncollected, stale, or unreviewed input).
#: `[UNREVIEWED: stream/thread…]` was removed — it leaked PII; backlog uses
#: `_BACKLOG_NOTICE` / `_COVERAGE_TRIMMED` instead.
_COVERAGE_PREFIXES = ("[COLLECTION:", "[STALE:", "[coverage incomplete")
#: The claim that everything in a range is accounted for.
_COMPLETE_PREFIX = "[complete for"
#: Compact stand-in when the detailed coverage warnings do not fit the budget.
_COVERAGE_TRIMMED = "[coverage incomplete — unreviewed or uncollected input not shown]"


def _is_coverage(line: str) -> bool:
    return line.lstrip() == _COVERAGE_TRIMMED or line.lstrip().startswith(_COVERAGE_PREFIXES)


def _dropped_index(lines: list[str]) -> tuple[int, int] | None:
    prefer = ("## People and facts", "## Ask about", "## Open", "## This week")
    for header in prefer:
        try:
            start = lines.index(header)
        except ValueError:
            continue
        end = start + 1
        while end < len(lines) and not lines[end].startswith("## "):
            end += 1
        # Keep the wiki index and the About-you line at the end of the
        # people-and-facts block; both survive ordinary trimming.
        while end - 1 > start and lines[end - 1].startswith(("Pages: ", "About you")):
            end -= 1
        if end - start <= 2:
            continue
        # Walk down past coverage footers: they outlive the rows they qualify.
        candidate = end - 1
        while candidate > start + 1 and _is_protected(lines[candidate]):
            candidate -= 1
        if _is_protected(lines[candidate]):
            continue
        # An event row and its warning are one unit, whichever side we land on.
        if _is_hint_line(lines[candidate]) and candidate > start + 1 \
                and _is_event_line(lines[candidate - 1]):
            return (candidate - 1, candidate + 1)
        if _is_event_line(lines[candidate]) and candidate + 1 < end \
                and _is_hint_line(lines[candidate + 1]):
            return (candidate, candidate + 2)
        return (candidate, candidate + 1)
    return None


def _hard_cut(lines: list[str], token_cap: int) -> str:
    """Last resort for tiny budgets: keep whole leading lines plus a marker.

    Cutting mid-line could strand an event row without its warning (or a
    warning without its row), so the cut backtracks to a line boundary and
    then drops a trailing orphaned half-unit if one remains.
    """
    marker = "\n… (trimmed)\n"
    # A coverage warning cut away would leave a retained "[complete for …]" claim
    # implying exhaustive coverage that no longer holds. Budget for a compact
    # stand-in up front so the honest notice is never itself what overflows.
    coverage_total = sum(1 for line in lines if _is_coverage(line))
    tail = (f"\n{_COVERAGE_TRIMMED}{marker}" if coverage_total else marker)
    keep = len(lines)
    while keep > 0 and textclean.estimate_tokens(
            "\n".join(lines[:keep]).rstrip() + tail) > token_cap:
        keep -= 1
    kept = lines[:keep]
    # An event and its warning are one unit. A trailing [event, warning] pair is
    # whole and stays; only a dangling half is peeled — an event whose warning
    # was cut would read as freshly verified, and a warning whose event was cut
    # is orphaned. A warning always sits directly under its event, so checking
    # the two ends independently (the old bug) dropped the warning of a fitting
    # pair and stranded its row.
    if kept and _is_hint_line(kept[-1]):
        if len(kept) < 2 or not _is_event_line(kept[-2]):
            kept = kept[:-1]                       # orphaned warning
    elif kept and _is_event_line(kept[-1]) \
            and keep < len(lines) and _is_hint_line(lines[keep]):
        kept = kept[:-1]                           # its warning was the cut line
    # If any coverage warning did not survive, a retained completeness claim is
    # now false: drop it and disclose the hole in one compact line, so a trimmed
    # brief never reads as exhaustive.
    if sum(1 for line in kept if _is_coverage(line)) < coverage_total:
        kept = [line for line in kept if not line.lstrip().startswith(_COMPLETE_PREFIX)]
        return "\n".join(kept).rstrip() + f"\n{_COVERAGE_TRIMMED}{marker}"
    return "\n".join(kept).rstrip() + marker


def _surviving_ids(text: str) -> set[int]:
    """Event ids whose handle still appears in the rendered brief.

    Read from the final text, so an event whose hint (and thus the row) trimming
    removed no longer counts as coverage. Event and hint drop as a unit, so a
    surviving row still carries its hint. Uses the module's shared `SOURCE_RE`
    rather than a private copy of the handle syntax.
    """
    return {int(tag[1:]) for tag in SOURCE_RE.findall(text) if tag.startswith("E")}


def _surviving_event_keys(conn: sqlite3.Connection, text: str,
                          id_to_key: "dict[int, str] | None" = None) -> set[str]:
    """Keys of the event rows that actually remain in the rendered brief.

    `id_to_key` is the map of the events `render` surfaced; every surviving id is
    one of them, so it resolves keys without a database round-trip. Only the
    standalone/legacy path (no map supplied) falls back to a query.
    """
    ids = _surviving_ids(text)
    if not ids:
        return set()
    if id_to_key is not None:
        return {id_to_key[i] for i in ids if i in id_to_key}
    placeholders = ",".join("?" * len(ids))
    return {row["key"] for row in conn.execute(
        f"SELECT key FROM events WHERE id IN ({placeholders})", tuple(ids))}


def _reconcile_coverage(conn: sqlite3.Connection, text: str, token_cap: int, *,
                        id_to_key: "dict[int, str] | None" = None) -> str:
    """Post-trim honesty: coverage is judged against what actually survived.

    `represented_keys` is computed before trimming, so an event whose hint stood
    in for a thread's backlog can be cut by the budget while its suppression
    stands. Recompute the backlog against the surviving events; for any pending
    traffic now uncovered and not already disclosed, drop the completeness claim
    and disclose the hole compactly, so a trimmed brief never implies exhaustive
    coverage of input it no longer shows.
    """
    surviving = _surviving_event_keys(conn, text, id_to_key)
    uncovered = activity.unlinked_backlog(conn, represented=surviving)
    if not uncovered:
        return text
    lines = text.splitlines()
    if any("coverage incomplete" in line for line in lines):
        return text                         # already disclosed (non-PII)
    lines = [line for line in lines if not line.lstrip().startswith(_COMPLETE_PREFIX)]
    lines.append(_COVERAGE_TRIMMED)
    return _trim("\n".join(lines).rstrip() + "\n", token_cap)


def write(conn: sqlite3.Connection, cfg: Config, ref: date | None = None) -> Path:
    cfg.ensure_dirs()
    text = render(conn, cfg, ref)
    cfg.brief_path.write_text(text, encoding="utf-8")
    return cfg.brief_path
