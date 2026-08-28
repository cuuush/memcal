"""Renders brief.md for persistent agent context."""

from __future__ import annotations

import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from . import archive, db, events, series, threads, todos, wiki
from .config import Config

CHARS_PER_TOKEN = 4  # Character-to-token approximation factor for token budget calculations.
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


def approx_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


#: Explains handles and wiki pages for each surface. Examples use `#` rather than
#: digits to prevent scanners from misinterpreting them as active handles.
LEGENDS = {
    "agent": ("handles open with memcal_open", "Pages open with memcal_open_page"),
    "cli": ("handles open with `memcal open E258`", "Pages open with `memcal page <name>`"),
}
DEFAULT_SURFACE = "agent"


def legend(surface: str = DEFAULT_SURFACE) -> str:
    rows, pages = LEGENDS.get(surface) or LEGENDS[DEFAULT_SURFACE]
    return (f"[〔E#〕〔T#〕〔Q#〕〔S#〕 {rows} — full detail: the "
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
        _standing_block(conn, cfg),
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
        # Uses `needs_subject` to maintain consistent subject attribution across blocks.
        who = f" ({ev.subject})" if ev.needs_subject() else ""
        # Includes RSVP status and link for invitations requiring action.
        invite = [ev.plain_state(), f"invite: {events._short_url(ev.rsvp_url)}"] \
            if ev.rsvp_url else []
        # Includes video meeting join links when available.
        act = [f"join: {ev.join_url}"] if ev.join_url else []
        # Formats title and platform consistently with the week block.
        title, platform = events.split_platform(ev.title)
        home = [f"via {platform}"] if platform else []
        tail = [t for t in (*invite, *act, *home, _duration(ev), via.get(ev.key, "")) if t]
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
    return [f"  ↳ {source_tag('question', q['id'])} {q['text']}" for q in questions]


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
        if nxt is None:
            continue
        row = conn.execute(
            "SELECT id, date, time, status FROM events"
            "  WHERE series = ? AND (date = ? OR instead_of = ?) LIMIT 1",
            (rule.slug, nxt.isoformat(), nxt.isoformat())).fetchone()
        # Skips rules lacking a materialized event occurrence.
        if row is None:
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
        # Renders questions associated with a to-do directly beneath it.
        for question in evidence.get(todo.id, []):
            lines.append(f"  ↳ {source_tag('question', question['id'])} {question['text']}")
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
        + [f"{source_tag('question', q['id'])} {q['text']}" for q in questions[:6]]
    )


def _standing_block(conn: sqlite3.Connection, cfg: Config) -> str:
    lines = ["## People and facts"]
    identity_rows = todos.standing(conn, "identity")
    identity_lines = [r["value"] + " " + source_tag("standing", r["id"])
                      for r in identity_rows]
    if identity_lines:
        lines.append(" ".join(identity_lines))
    for row in todos.standing(conn, "alias"):
        lines.append(row["value"] + " " + source_tag("standing", row["id"]))
    index = _pages_line(cfg)
    if index:
        lines.append(index)
    return "\n".join(lines) if len(lines) > 1 else ""


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
    if approx_tokens(text) <= token_cap:
        return text
    lines = text.splitlines()
    budget = token_cap * CHARS_PER_TOKEN
    # Preserves headers and initial lines while dropping trailing lines until within budget.
    while sum(len(l) + 1 for l in lines) > budget and len(lines) > 8:
        drop = _dropped_index(lines)
        if drop is None:
            break
        lines.pop(drop)
    out = "\n".join(lines).rstrip() + "\n"
    if approx_tokens(out) > token_cap:
        # Reserves space for the truncation marker within the character budget.
        marker = "\n… (trimmed)\n"
        room = max(0, budget - len(marker))
        out = out[:room].rstrip() + marker
    return out


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
        # Keep the wiki index at the end of the people-and-facts block.
        while end - 1 > start and lines[end - 1].startswith("Pages: "):
            end -= 1
        if end - start > 2:
            return end - 1
    return None


def write(conn: sqlite3.Connection, cfg: Config, ref: date | None = None) -> Path:
    cfg.ensure_dirs()
    text = render(conn, cfg, ref)
    cfg.brief_path.write_text(text, encoding="utf-8")
    return cfg.brief_path
