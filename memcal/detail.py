"""Assemble exhaustive detail for one row when a caller opens its handle.

The brief stays compact; this payload adds links, participants, source lines, related
pages, recurrence, and history. It composes existing stores and is shared by web and
agent surfaces so they cannot drift.
"""

from __future__ import annotations

import sqlite3

from . import brief, calls, dates, db, events, series, threads, todos, trace, wiki
from .config import Config

#: What `resolve_source` calls each kind, so a caller only ever needs the handle.
KINDS = ("event", "todo", "question", "standing")


def open_handle(conn: sqlite3.Connection, cfg: Config, token: str) -> str:
    """The whole record behind a brief handle, as text a model reads.

    Takes what the brief actually prints — `E258`, `〔T2〕`, `q12` — because asking for
    a stable key the agent has never been shown is our schema leaking into their
    reasoning. `memcal_source` already learned that lesson; this one is born with it,
    which is also why there is no `kind` argument to get wrong.
    """
    handle = brief.parse_source(str(token or "").strip().strip("〔〕"))
    if not handle:
        return (f"(not a memcal handle: {token!r} — the brief prints them like E258, "
                "T2, Q12 or S4)")
    resolved = trace.resolve_source(conn, str(token).strip().strip("〔〕"))
    if resolved.get("error"):
        return resolved["error"]
    kind, ref = resolved["kind"], resolved["ref"]
    body = {
        "event": _event_text,
        "todo": _todo_text,
        "question": _question_text,
        "standing": _standing_text,
    }[kind](conn, cfg, ref)
    return "\n".join([body, _sources_text(conn, kind, ref), _history_text(conn, kind, ref)])


# --------------------------------------------------------------------- events --

def event_record(conn: sqlite3.Connection, cfg: Config, ref: str) -> dict:
    """The structured payload. `web.why` renders this; `open_handle` writes it out.

    Moved here from `web._event_detail` unchanged in substance, so the browser panel
    and the agent cannot drift apart the way the two tool schemas already have.
    """
    row = conn.execute("SELECT * FROM events WHERE key = ?", (ref,)).fetchone()
    if not row:
        return {}
    participants = db.jload(row["participants"], [])
    people = list(dict.fromkeys(
        [name for name in [row["subject"], *participants] if name and name != "me"]
    ))
    wiki_links: list[dict] = []
    seen = set()

    def add_link(name: str, role: str) -> None:
        link = _wiki_link(cfg, name, role=role)
        if link and link["slug"] not in seen:
            seen.add(link["slug"])
            wiki_links.append(link)

    for person in people:
        add_link(person, "person")
    if row["location"]:
        add_link(row["location"], "location")
    if row["series"]:
        add_link(row["series"], "series")

    changes = [{
        "field": change["field"],
        "old": db.jload(change["old_value"], []) if change["field"] == "participants"
               else change["old_value"],
        "new": db.jload(change["new_value"], []) if change["field"] == "participants"
               else change["new_value"],
        "at": str(change["changed_at"])[:19],
        "by": change["written_by"],
    } for change in events.history(conn, row["id"])]
    provenance = [{
        "verb": stamp["verb"] or "written", "stage": stamp["stage"] or "code",
        "at": str(stamp["at"])[:19], "run": stamp["run_id"],
        "call": calls.ordinal(conn, stamp["generation_id"] or ""),
        "gen": stamp["generation_id"] or "",
        "entity": stamp["entity"] or "",
    } for stamp in reversed(trace.history(conn, "event", ref))]
    return {
        "event": {
            "key": row["key"], "title": row["title"], "date": row["date"],
            "until": row["until"] or "", "time": row["time"] or "",
            "location": row["location"] or "", "status": row["status"],
            "kind": row["kind"], "state": events.Event.from_row(row).state_text(),
            "id": row["id"], "subject": row["subject"],
            "participants": participants, "series": row["series"] or "",
            "note": row["note"] or "", "source": row["source"] or "",
            # The two fields whose whole value is being pressable, and the reason a
            # reader opens a row at all. `_event_summary` never carried them, so the
            # browser panel could not show a join link either.
            "rsvp_url": row["rsvp_url"] or "", "join_url": row["join_url"] or "",
            "part_of": row["part_of"],
            "written_by": row["written_by"], "created_at": str(row["created_at"])[:19],
            "updated_at": str(row["updated_at"])[:19],
        },
        "wiki": wiki_links,
        # No `related` block. Every facet of every event was resolved here whether or
        # not a pill was ever clicked, which is where the N+2 scans came from; the
        # pills fetch `/api/events` for the one facet the user actually asked about.
        "timeline": {
            "created": {"at": str(row["created_at"])[:19], "by": row["written_by"]},
            "changes": changes,
            "provenance": provenance,
            "writes": trace.timeline(conn, "event", ref, changes),
        },
    }


def _wiki_link(cfg: Config, name: str, *, role: str) -> dict | None:
    slug = wiki.canonical(cfg.wiki_dir, db.slugify(name))
    page = wiki.read(cfg.wiki_dir, slug)
    if not page:
        return None
    return {"slug": page.slug, "title": page.title or name, "section": page.section,
            "role": role}


def _event_text(conn: sqlite3.Connection, cfg: Config, ref: str) -> str:
    record = event_record(conn, cfg, ref)
    if not record:
        return f"(no event {ref})"
    row = record["event"]
    out = [f"E{row['id']}  {row['title']}", ""]
    out.extend(_fields([
        ("when", _when(row)),
        ("state", row["state"]),
        ("where", row["location"]),
        # Spelled out rather than shortened. The whole reason to open a row is to get
        # the thing the one-line summary could not carry, and a link a reader has to
        # reconstruct is not a link.
        ("join", row["join_url"]),
        ("rsvp", row["rsvp_url"]),
        ("who", ", ".join(row["participants"])),
        ("whose", row["subject"] if row["subject"] != "me" else ""),
        ("note", row["note"]),
        ("source", row["source"]),
    ]))
    out.extend(_series_lines(conn, row))
    out.extend(_containment_lines(conn, ref))
    questions = todos.questions_by_event(conn).get(row["id"], [])
    if questions:
        out.append("")
        out.append("open questions about it:")
        out.extend(f"  Q{q['id']}  {q['text']}" for q in questions)
    if record["wiki"]:
        out.append("")
        out.append("pages: " + " · ".join(
            f"{link['slug']} ({link['role']})" for link in record["wiki"]))
    return "\n".join(out)


def _when(row: dict) -> str:
    when = db.parse_date(row["date"]).strftime("%A %-d %B %Y")
    if row["time"]:
        when += f" at {row['time']}"
    if row["until"] and row["until"] > row["date"]:
        when += f", through {db.parse_date(row['until']).strftime('%A %-d %B')}"
    return when


def _series_lines(conn: sqlite3.Connection, row: dict) -> list[str]:
    """The rule behind an occurrence, which is the thing that answers "and next week".

    An occurrence knows its own day and the *rule* knows the cadence, where it meets and
    how to join it. That arrow only started pointing back recently, and a
    reader opening one Tuesday still cannot see the schedule from the row alone.
    """
    if not row["series"]:
        return []
    rule = series.get(conn, row["series"])
    if rule is None:
        return []
    lines = ["", f"part of the series {rule.slug!r}: {rule.phrase}"]
    if rule.join_url:
        lines.append(f"  the rule's join link: {rule.join_url}")
    if getattr(rule, "where", ""):
        lines.append(f"  the rule's usual place: {rule.where}")
    return lines


def _containment_lines(conn: sqlite3.Connection, ref: str) -> list[str]:
    """`part_of`, both directions. Three rows called "Elements" were three plans."""
    row = conn.execute("SELECT id, part_of FROM events WHERE key = ?", (ref,)).fetchone()
    if row is None:
        return []
    lines = []
    if row["part_of"]:
        parent = conn.execute("SELECT id, title, date FROM events WHERE id = ?",
                              (row["part_of"],)).fetchone()
        if parent:
            lines.append(f"happens inside E{parent['id']} {parent['title']!r} "
                         f"({parent['date']})")
    inside = conn.execute(
        "SELECT id, title, date FROM events WHERE part_of = ? ORDER BY date",
        (row["id"],)).fetchall()
    lines.extend(f"contains E{child['id']} {child['title']!r} ({child['date']})"
                 for child in inside)
    return ["", *lines] if lines else []


# ------------------------------------------------------- todos and questions --

def _todo_text(conn: sqlite3.Connection, cfg: Config, ref: str) -> str:
    row = conn.execute(
        """SELECT t.*, e.title AS event_title, e.date AS event_date
             FROM todos t LEFT JOIN events e ON e.id = t.event_id
            WHERE t.key = ?""", (ref,)).fetchone()
    if not row:
        return f"(no to-do {ref})"
    todo = todos.Todo.from_row(row)
    out = [f"T{todo.id}  {todo.text}", ""]
    out.extend(_fields([
        ("state", todo.status),
        ("opened", todo.opened_at[:10]),
        ("due", todo.due),
        ("for whom", todo.subject if (todo.subject or "me") != "me" else ""),
        # A to-do that only becomes actionable on a condition is otherwise
        # indistinguishable from one that is merely overdue.
        ("waiting on", todo.wake_condition),
        ("woke", todo.woke_at),
        ("source", todo.source),
    ]))
    if todo.event_id:
        out.append("")
        out.append(f"about E{todo.event_id} {row['event_title']!r} on {row['event_date']}")
    questions = todos.questions_by_todo(conn).get(todo.id, [])
    if questions:
        out.append("")
        out.append("open questions about it:")
        out.extend(f"  Q{q['id']}  {q['text']}" for q in questions)
    return "\n".join(out)


def _question_text(conn: sqlite3.Connection, cfg: Config, ref: str) -> str:
    row = conn.execute("SELECT * FROM questions WHERE key = ?", (ref,)).fetchone()
    if not row:
        return f"(no question {ref})"
    columns = set(row.keys())
    out = [f"Q{row['id']}  {row['text']}", ""]
    out.extend(_fields([
        ("state", row["status"]),
        ("asked", str(row["created_at"])[:10]),
        # The day it dies with when no row answers that — otherwise a question that
        # vanishes overnight has nothing on its own page saying why it was going to.
        ("about day", row["about_date"] if "about_date" in columns else ""),
        ("answer", row["answer"] if "answer" in columns else ""),
        ("asked by", row["written_by"] if "written_by" in columns else ""),
    ]))
    about = row["about_event"] if "about_event" in columns else None
    if about:
        event = conn.execute(
            "SELECT id, title, date, time, status FROM events WHERE id = ?",
            (about,)).fetchone()
        if event:
            out.append("")
            out.append(f"about E{event['id']} {event['title']!r} on {event['date']}"
                       + (f" at {event['time']}" if event["time"] else "")
                       + f" ({event['status']})")
    return "\n".join(out)


def _standing_text(conn: sqlite3.Connection, cfg: Config, ref: str) -> str:
    row = conn.execute("SELECT * FROM standing WHERE key = ?", (ref,)).fetchone()
    if not row:
        return f"(no standing row {ref})"
    columns = set(row.keys())
    out = [f"S{row['id']}  {row['value']}", ""]
    out.extend(_fields([
        ("kind", row["kind"] if "kind" in columns else ""),
        ("scope", row["scope"] if "scope" in columns else ""),
        ("recorded", str(row["created_at"])[:10] if "created_at" in columns else ""),
    ]))
    return "\n".join(out)


# ------------------------------------------------------------------- shared --

def _fields(pairs: list[tuple[str, object]]) -> list[str]:
    """`label: value`, one per line, empties dropped.

    Labelled rather than run together with separators. The line this whole change came
    out of read `…, 7pm — confirmed · invite: … · Location available once RSVP'd ·
    Partiful RSVP yes` — four clauses, four delimiter styles, and no way for a reader
    to tell which fragment was a field and which was prose about a field.
    """
    return [f"{label}: {value}" for label, value in pairs if value]


def _sources_text(conn: sqlite3.Connection, kind: str, ref: str) -> str:
    """The lines somebody actually said, which is the point of opening anything.

    Marked, not filtered: `evidence` rows are what the row was built from and the rest
    is neighbouring context, which is what makes a two-word "yeah" readable. Truncated
    here because this is a summary view — `memcal_source` returns them whole.
    """
    rows = trace.source_rows(conn, kind=kind, ref=ref)
    if not rows:
        return ("\nsources: none recorded — this row was written directly rather than "
                "read out of a message")
    cited = trace.citations(conn, kind, ref)
    out = ["", f"sources ({len(rows)} line(s); * = what the row was built from):"]
    if not cited["narrow"]:
        out.append("  (!) no line-level citation — these are the conversation it came "
                   "out of, not the lines it was built from")
    names = threads.titles(conn)
    for row in rows[:12]:
        if row.get("source_heading"):
            out.append(f"  — {row['source_heading']} —")
        mark = "*" if row.get("evidence") else " "
        where = names.get((row["stream"], row.get("thread") or ""), "") or row["stream"]
        text = " ".join(str(row["text"]).split())
        if len(text) > 240:
            text = text[:240] + "…"
        out.append(f" {mark} [{row['id']}] {where} · {dates.said_on(row['ts'])} · "
                   f"{row['who']}: {text}")
    if len(rows) > 12:
        out.append(f"  … {len(rows) - 12} more; memcal_conversation reads around any "
                   "[n] above")
    return "\n".join(out)


def _history_text(conn: sqlite3.Connection, kind: str, ref: str) -> str:
    """What has changed about this row, so a reader can see a correction happened."""
    if kind != "event":
        return ""
    row = conn.execute("SELECT id FROM events WHERE key = ?", (ref,)).fetchone()
    if row is None:
        return ""
    changes = events.history(conn, row["id"])
    if not changes:
        return ""
    out = ["", "changes:"]
    for change in changes[-8:]:
        out.append(f"  {str(change['changed_at'])[:16]}  {change['field']}: "
                   f"{change['old_value']!r} -> {change['new_value']!r} "
                   f"(by {change['written_by']})")
    return "\n".join(out)
