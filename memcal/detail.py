"""Assemble exhaustive detail for one row when a caller opens its handle.

The brief stays compact; this payload adds links, participants, source lines, related
pages, recurrence, and history. It composes existing stores and is shared by web and
agent surfaces so they cannot drift.
"""

from __future__ import annotations

import re
import sqlite3

from . import calls, dates, db, events, legacy, presentation, series, threads, todos, trace, wiki
from .config import Config

#: What `resolve_source` calls each kind, so a caller only ever needs the handle.
KINDS = ("event", "todo", "question", "standing")
HANDLE_RE = re.compile(r"^[〔\[]?\s*([ETQSetqs])\s*(\d+)\s*[〕\]]?$")


def parse_handle(token: str) -> str | None:
    match = HANDLE_RE.match(str(token or "").strip())
    return f"{match.group(1).upper()}{match.group(2)}" if match else None


#: How many pending lines `open_handle` carries inline. Matches the activity
#: pager's max so a normal flagged row shows all of them; `memcal_activity`
#: pages past this.
OPEN_ACTIVITY_LIMIT = 50

#: Whole-thread tails per open. Tokens are cheap; a second call is not. Capped
#: so one pathological thread cannot dominate the payload.
OPEN_THREAD_LIMIT = 60
OPEN_MAX_THREADS = 3


def open_handle(conn: sqlite3.Connection, cfg: Config, token: str) -> str:
    """The whole record behind a brief handle, as text a model reads.

    Accepts printed handles (`E258`, `T2`, `Q12`); no `kind` argument needed.
    Includes pending new activity inline, so a row the brief flagged needs no
    second `memcal_activity` call unless the preview is truncated.
    """
    handle = parse_handle(token)
    if not handle:
        return (f"(not a memcal handle: {token!r} — the brief prints them like E258, "
                "T2, Q12 or S4)")
    resolved = trace.resolve_source(conn, handle)
    if resolved.get("error"):
        return resolved["error"]
    kind, ref = resolved["kind"], resolved["ref"]
    body = {
        "event": _event_text,
        "todo": _todo_text,
        "question": _question_text,
        "standing": _standing_text,
    }[kind](conn, cfg, ref)
    return "\n".join([
        body,
        _sources_text(conn, kind, ref),
        _history_text(conn, kind, ref),
        _activity_text(conn, kind, ref, handle),
    ])


# --------------------------------------------------------------------- events --

def event_record(conn: sqlite3.Connection, cfg: Config, ref: str) -> dict:
    """The structured payload shared by web and agent surfaces."""
    row = conn.execute("SELECT * FROM events WHERE key = ?", (ref,)).fetchone()
    if not row:
        return {}
    participants = db.jload(row["participants"], [])
    hosts = db.jload(row["hosts"], []) if "hosts" in row.keys() else []
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
    for person in hosts:
        add_link(person, "host")
    if row["location"]:
        add_link(row["location"], "location")
    if row["series"]:
        add_link(row["series"], "series")

    changes = [{
        "field": change["field"],
        "old": db.jload(change["old_value"], []) if change["field"] in ("participants", "hosts")
               else change["old_value"],
        "new": db.jload(change["new_value"], []) if change["field"] in ("participants", "hosts")
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
            "participants": participants, "hosts": hosts, "series": row["series"] or "",
            "note": row["note"] or "", "source": row["source"] or "",
            # Full pressable URLs omitted from the one-line summary.
            "rsvp_url": row["rsvp_url"] or "", "join_url": row["join_url"] or "",
            "part_of": row["part_of"],
            "written_by": row["written_by"], "created_at": str(row["created_at"])[:19],
            "updated_at": str(row["updated_at"])[:19],
        },
        "wiki": wiki_links,
        # Facets load on demand via `/api/events`.
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
        # Full URLs; the detail view carries what the summary cannot.
        ("join", row["join_url"]),
        ("rsvp", row["rsvp_url"]),
        ("who", ", ".join(row["participants"])),
        ("hosted by", ", ".join(row["hosts"])),
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
    """The rule behind an occurrence: cadence, place, and join link live on the rule."""
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
    """`part_of` in both directions."""
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
        ("state", presentation.question_state(row)),
        ("asked", str(row["created_at"])[:10]),
        # The day it expires with when no row answers it.
        ("about day", row["about_date"] if "about_date" in columns else ""),
        ("answer", row["answer"] if "answer" in columns else ""),
        ("waiting for", row["wake_condition"] if "wake_condition" in columns else ""),
        ("asked by", row["written_by"] if "written_by" in columns else ""),
    ]))
    changes = conn.execute(
        "SELECT field, old_value, new_value, changed_at FROM question_history"
        " WHERE question_id = ? ORDER BY id", (row["id"],)).fetchall()
    if changes:
        out.extend(["", "changes:"])
        out.extend(f"  {change['field']}: {change['old_value'] or '—'} → "
                   f"{change['new_value'] or '—'}" for change in changes)
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
        redirect = legacy.standing_redirect(conn, ref)
        if redirect is None:
            return f"(no standing row {ref})"
        return "\n".join([
            f"S{redirect.old_id}  {redirect.old_value}", "",
            f"kind: {redirect.old_kind}",
            f"retired: {redirect.destination}",
            f"recorded: {redirect.old_created_at[:10]}",
        ])
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
    """`label: value`, one per line, empties dropped."""
    return [f"{label}: {value}" for label, value in pairs if value]


def _sources_text(conn: sqlite3.Connection, kind: str, ref: str) -> str:
    """Cited source lines with neighbours for context; `*` marks evidence.

    Truncated here; `memcal_source` returns them whole.
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
        where = names.get((row["channel"], row.get("thread") or ""), "") or row["channel"]
        text = " ".join(str(row["text"]).split())
        if len(text) > 240:
            text = text[:240] + "…"
        own = " (your own earlier turn)" if presentation.self_written(row["channel"]) else ""
        out.append(f" {mark} [{row['id']}] {where}{own} · {dates.said_on(row['ts'])} · "
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


def _activity_text(conn: sqlite3.Connection, kind: str, ref: str,
                   handle: str) -> str:
    """Pending messages plus the whole thread behind them, so one open is enough.

    Returns "" when nothing is pending, keeping opens without fresh traffic
    byte-identical to before. Otherwise every strong conversational thread is
    shown tail-first (oldest first, `(new)` marking what is still unreviewed)
    with full text — tokens are cheap, a second call is not. Non-threaded
    (calendar-family) pending rides in an "other new messages" list. Anything
    cut by a cap names its cursor for `memcal_activity`. Reading changes
    nothing.
    """
    from . import activity as activity_mod  # noqa: PLC0415 late, mirrors surfaces
    try:
        page = activity_mod.read(conn, kind, ref, cursor=0,
                                 limit=OPEN_ACTIVITY_LIMIT)
    except Exception:
        return ""
    if not page.get("total"):
        return ""
    try:
        links = activity_mod.associations(conn, kind, ref, strong_only=True)["strong"]
    except Exception:
        links = []
    try:
        weak = activity_mod.associations(conn, kind, ref)["weak"]
    except Exception:
        weak = []
    covered = activity_mod.reviewed_ids(conn, kind, ref)
    seen: set[int] = set()
    lines = ["", f"new activity since last review ({page['total']} message(s)"
             + (f", {page['omitted']} omitted" if page.get("omitted") else "")
             + f"; {page.get('reviewed', 0)} already reviewed):"]
    threads_shown = 0
    for link in links:
        if threads_shown >= OPEN_MAX_THREADS:
            break
        if link.get("family") or not link.get("thread"):
            continue
        if link["channel"] in trace.UNTHREADED_STREAMS:
            continue
        channel, thread = link["channel"], link["thread"]
        try:
            tail, total = activity_mod.thread_tail(
                conn, channel, thread, limit=OPEN_THREAD_LIMIT)
        except Exception:
            continue
        if not tail:
            continue
        threads_shown += 1
        if total > len(tail):
            lines.append(f"full thread {channel}/{thread} "
                         f"({len(tail)} of {total} shown)")
        else:
            lines.append(f"full thread {channel}/{thread} "
                         f"({len(tail)} message(s)):")

        for item in tail:
            mark = " (new)" if item["id"] not in covered else ""
            seen.add(item["id"])
            lines.append(f"[{item['id']}] {str(item['ts'])[:16]} · "
                         f"{item['who']}{mark}:")
            lines.append(f"  {item['text']}")
    rest = [item for item in (page.get("items") or []) if item["id"] not in seen]
    if rest:
        lines.append("other new messages:")
        for item in rest:
            seen.add(item["id"])
            lines.append(f"[{item['id']}] {str(item['ts'])[:16]} "
                         f"{item['channel']}/{item['thread']} · {item['who']}:")
            lines.append(f"  {item['text']}")
    truncated = bool(page.get("omitted")) or any(
        True for _ in links[OPEN_MAX_THREADS:] if not _.get("family"))
    if page.get("items"):
        tail_note = (f"memcal_activity(handle={handle}) pages past this view, "
                     f"next cursor {page.get('next_cursor', 0)}"
                     if truncated else
                     f"all pending shown; memcal_activity(handle={handle}) "
                     f"re-reads them paged")
        lines.append(
            f"(cite [ids] in memcal_update or memcal_reviewed — reading changes "
            f"nothing; {tail_note})")
    for thread in weak or []:
        lines.append(f"(possibly related: {thread['channel']}/"
                     f"{thread['thread']} — {thread['why']})")
    return "\n".join(lines)
