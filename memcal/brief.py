"""Renders brief.md for persistent agent context."""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from . import archive, db, events, presentation, series, textclean, threads, todos, wiki
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
LEGENDS = {
    "agent": ("handles open with memcal_open", "Pages open with memcal_open_page"),
    "cli": ("handles open with `memcal open E258`", "Pages open with `memcal page <name>`"),
}
DEFAULT_SURFACE = "agent"


def legend(surface: str = DEFAULT_SURFACE) -> str:
    rows, pages = LEGENDS.get(surface) or LEGENDS[DEFAULT_SURFACE]
    return (f"[〔E#〕〔T#〕〔Q#〕 {rows} — full detail: the "
            "address, the links, the messages it came from, and what has changed. "
            f"{pages}; the names in parentheses after a page "
            "are the facts it holds]\n\n")


#: Default agent legend string for backward compatibility.
LEGEND = legend(DEFAULT_SURFACE)


def render(conn: sqlite3.Connection, cfg: Config, ref: date | None = None,
           surface: str = DEFAULT_SURFACE) -> str:
    ref = ref or db.today()
    # Retain audit rows while removing obligations whose linked event is no longer live.
    todos.expire_event_links(conn)
    blocks = [
        # Renders first because it represents immediate items rather than future state.
        _now_block(conn),
        _week_block(conn, cfg, ref),
        _later_block(conn, cfg, ref),
        _recurring_block(conn, ref),
        _open_block(conn),
        _ask_block(conn),
        _facts_block(conn, cfg),
    ]
    text = "\n\n".join(b for b in blocks if b).rstrip() + "\n"
    return _trim(legend(surface) + text, cfg.brief_token_cap)


def _week_block(conn: sqlite3.Connection, cfg: Config, ref: date) -> str:
    rows = events.window(conn, cfg.days_back, cfg.days_forward, ref)
    # Anchors the reference date explicitly to prevent incorrect date inference.
    lines = [f"## This week  (today is {ref.strftime('%A %-d %B %Y')})"]
    if not rows:
        lines.append("(nothing known)")
    else:
        via = attribution(conn)
        asked = todos.questions_by_event(conn)
        nested = {e.id for e in rows if e.part_of}
        for ev in rows:
            if ev.id in nested:
                continue          # Rendered under parent event below.
            marker = "· " if db.parse_date(ev.date) < ref else ""
            lines.append(f"{source_tag('event', ev.id)} {marker}"
                         + ev.one_line(extra=[via.get(ev.key, "")], overview=True))
            lines.extend(_question_lines(asked.get(ev.id, [])))
            lines.extend(_child_lines(conn, ev, asked))
    # Explicit date bounds signal completeness to avoid unnecessary range lookups.
    first = (ref - timedelta(days=cfg.days_back)).strftime("%a %-d %b")
    last = (ref + timedelta(days=cfg.days_forward)).strftime("%a %-d %b")
    lines.append(f"[complete for {first} – {last}; look up anything outside that]")
    # Distinguishes an empty schedule from stale ingestion streams.
    stale = archive.stale_streams(conn, cfg=cfg)
    if stale:
        behind = ", ".join(f"{name} {age}" for name, age in stale)
        lines.append(f"[STALE: no {behind} — this week may be incomplete]")
    return "\n".join(lines)


#: Maximum days beyond the active window to include in the Later section.
LATER_DAYS = 45

#: Maximum number of entries displayed in the Later section.
LATER_LIMIT = 8


def _later_block(conn: sqlite3.Connection, cfg: Config, ref: date) -> str:
    """Renders upcoming committed events beyond the active weekly window."""
    edge = ref + timedelta(days=cfg.days_forward)
    rows = [e for e in events.window(conn, 0, cfg.days_forward + LATER_DAYS, ref)
            if db.parse_date(e.date) > edge and _committed(e)]
    if not rows:
        return ""
    via = attribution(conn)
    asked = todos.questions_by_event(conn)
    lines = ["## Later"]
    for ev in rows[:LATER_LIMIT]:
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
        lines.extend(_question_lines(asked.get(ev.id, [])))
    if len(rows) > LATER_LIMIT:
        lines.append(f"[{len(rows) - LATER_LIMIT} more beyond this; ask for a date]")
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


def _child_lines(conn: sqlite3.Connection, parent: events.Event, asked: dict) -> list[str]:
    """Formats child events and associated questions nested under a parent event."""
    out: list[str] = []
    for child in events.children_of(conn, parent.id):
        out.append(f"  ↳ {source_tag('event', child.id)} "
                   + child.one_line(overview=True))
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
ABOUT_YOU_PREFIX = "About you (open with memcal_open_page me): "

#: Overflow placeholder when trimming must drop self fact values. Keeps the
#: pointer so the facts stay one tool call away.
ABOUT_YOU_TRIMMED = ("About you (open with memcal_open_page me): "
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
                "memcal_open_page <name> to inspect")
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
    """Trims brief text to fit within token_cap by dropping trailing lines from lower-priority sections."""
    if textclean.estimate_tokens(text) <= token_cap:
        return text
    lines = text.splitlines()
    # Preserves headers and initial lines while dropping trailing lines until within budget.
    while textclean.estimate_tokens("\n".join(lines)) > token_cap and len(lines) > 8:
        drop = _dropped_index(lines)
        if drop is None:
            break
        lines.pop(drop)
    out = "\n".join(lines).rstrip() + "\n"
    if textclean.estimate_tokens(out) <= token_cap:
        return out
    # Collapse self fact values to their pointer before any character cut so an
    # address or URL is never bisected; the pointer keeps the facts one call away.
    collapsed = [ABOUT_YOU_TRIMMED if (line.startswith(ABOUT_YOU_PREFIX)
                                       and line != ABOUT_YOU_TRIMMED)
                 else line for line in lines]
    if collapsed != lines:
        squashed = "\n".join(collapsed).rstrip() + "\n"
        if textclean.estimate_tokens(squashed) <= token_cap:
            return squashed
        lines, out = collapsed, squashed
    # Shrinks toward the cap by the overshoot ratio; the estimate only grows with
    # length, so each pass cuts at least one character and the loop terminates.
    marker = "\n… (trimmed)\n"
    room = len(out)
    while room > 0:
        over = textclean.estimate_tokens(out[:room] + marker)
        if over <= token_cap:
            break
        room = min(room - 1, int(room * token_cap / over))
    room = _about_you_safe_room(out, room)
    base = out[:max(0, room)].rstrip()
    omitted = _omitted_about_you(out, room)
    if omitted:
        # Under extreme limits the whole About-you line goes, but a pointer or
        # overflow note stays. Ambiguity keeps its candidate names verbatim.
        keep = omitted[0] if omitted[0].startswith("About you: ambiguous") \
            else ABOUT_YOU_TRIMMED
        candidate = (base + "\n" + keep + marker) if base else (keep + marker)
        if textclean.estimate_tokens(candidate) <= token_cap:
            return candidate
        return base + marker
    return base + marker


def _about_spans(out: str) -> list[tuple[str, int, int]]:
    """(line, start, end) for every About-you line in `out`."""
    spans: list[tuple[str, int, int]] = []
    pos = 0
    for line in out.splitlines(keepends=True):
        body = line.rstrip("\n")
        if body.startswith("About you"):
            spans.append((body, pos, pos + len(body)))
        pos += len(line)
    return spans


def _about_you_safe_room(out: str, room: int) -> int:
    """Back a character-cut point off an About-you line so facts are never bisected."""
    for _line, start, end in _about_spans(out):
        if start < room < end:
            return start
    return room


def _omitted_about_you(out: str, room: int) -> list[str]:
    """About-you lines a cut at `room` would drop."""
    return [line for line, start, _end in _about_spans(out) if start >= room]


def _dropped_index(lines: list[str]) -> int | None:
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
        if end - start > 2:
            return end - 1
    return None


def write(conn: sqlite3.Connection, cfg: Config, ref: date | None = None) -> Path:
    cfg.ensure_dirs()
    text = render(conn, cfg, ref)
    cfg.brief_path.write_text(text, encoding="utf-8")
    return cfg.brief_path
