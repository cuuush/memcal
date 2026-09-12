"""New-activity nomination and review accounting.

Three separate facts stay separate here:

- collection health (``archive.collection_sources.status``): when a source was
  last attempted and what happened;
- review state (``reviewed_lines`` below): exactly which collected observations
  have been considered for a typed fact, including an explicitly valid
  no-change review;
- rendering time: when context was last drawn, which proves nothing was reviewed.

A high-water mark cannot stand in for the set: reviewing a later message never
proves an earlier one was considered, so coverage is one row per observation
and holes stay holes until something actually covers them.

Associations nominate; the model judges. A strong link is an exact
``(stream, thread)`` already cited as evidence for the fact. A weak link shares
a participant *and* a distinctive title term with it, and is always labeled as
the weaker guess it is. One shared person alone never marks anything changed.

Arrival identity is ``archive.id`` (monotonic on append); source time (``ts``)
is kept for interpretation. A delayed old message is new to the archive with
its old date intact. Same-id redelivery changes nothing, so candidate counts
cannot inflate on replay.
"""

from __future__ import annotations

import re
import sqlite3

from . import db

#: How many weak threads one fact may nominate. A shared participant alone is
#: not a link; participant plus a distinctive term still only nominates.
WEAK_THREAD_CAP = 3

#: Words too common to nominate on. Kept tiny on purpose: this list only
#: narrows candidates, it never decides meaning.
_COMMON_WORDS = frozenset({
    "about", "after", "again", "before", "birthday", "dinner", "drink", "drinks",
    "event", "friday", "group", "lunch", "meeting", "monday", "night", "party",
    "saturday", "sunday", "thursday", "today", "tomorrow", "tonight", "tuesday",
    "wednesday", "weekend", "with",
})


def distinctive_terms(title: str) -> list[str]:
    """Title words worth nominating on: long, alphabetic, uncommon."""
    return sorted({word for word in re.split(r"[^a-z0-9]+", (title or "").lower())
                   if len(word) >= 5 and word not in _COMMON_WORDS})


# ---------------------------------------------------------------- coverage --

def reviewed_ids(conn: sqlite3.Connection, kind: str, ref: str) -> set[int]:
    """Exactly which observations have been considered for this fact."""
    return {row["archive_id"] for row in conn.execute(
        "SELECT archive_id FROM reviewed_lines WHERE kind = ? AND ref = ?",
        (kind, ref))}


def reviewed_max(conn: sqlite3.Connection, kind: str, ref: str) -> int:
    """Newest covered observation, for display. Holes below it stay pending."""
    row = conn.execute(
        "SELECT max(archive_id) AS m FROM reviewed_lines WHERE kind = ? AND ref = ?",
        (kind, ref)).fetchone()
    try:
        return int(row["m"] or 0)
    except (TypeError, ValueError):
        return 0


def note_review(conn: sqlite3.Connection, kind: str, ref: str,
                archive_ids, *, by_run: int | None = None, by_stage: str = "",
                commit: bool = True) -> int:
    """Record that these exact observations were considered for this fact.

    Idempotent: replaying the same review inserts nothing new. Only the given
    ids are covered — a later id never implies an earlier one, so holes,
    omitted pages, and arrivals during the review all stay pending until
    something actually covers them. Returns how many lines were newly covered.
    """
    ids = [int(i) for i in (archive_ids or []) if i]
    if not kind or not ref or not ids:
        return 0
    added = 0
    for aid in dict.fromkeys(ids):
        cur = conn.execute(
            """INSERT OR IGNORE INTO reviewed_lines
                   (kind, ref, archive_id, reviewed_at, by_run, by_stage)
               VALUES(?,?,?,?,?,?)""",
            (kind, ref, aid, db.now(), by_run, by_stage or ""))
        added += cur.rowcount
    if commit:
        conn.commit()
    return added


def advance_thread(conn: sqlite3.Connection, items, *,
                   by_run: int | None = None, by_stage: str = "",
                   commit: bool = True) -> int:
    """Record a thread-level no-change review for one read bundle's items.

    A bundle read in full with no actionable diff still considered exactly the
    lines it carried: for each conversation in the bundle, the facts evidenced
    there cover precisely those bundled ids — nothing above them (later gated
    rows the bundle never carried stay pending) and nothing in other
    conversations. Failed or unread bundles never reach this call. Authored
    turns are not observations, and streams without conversations keep their
    item-family separation, so neither advances anything here.
    """
    from . import archive as archive_mod  # noqa: PLC0415
    from . import trace as trace_mod  # noqa: PLC0415
    skip = set(archive_mod.INTERNAL_STREAMS) | set(trace_mod.UNTHREADED_STREAMS)
    by_thread: dict[tuple[str, str], list[int]] = {}
    for item in items:
        try:
            stream, thread, aid = item["stream"], item["thread"] or "", int(item["id"])
        except (KeyError, TypeError, ValueError):
            continue
        if not aid or stream in skip:
            continue
        by_thread.setdefault((stream, thread), []).append(aid)
    moved = 0
    for (stream, thread), ids in by_thread.items():
        refs = conn.execute(
            """SELECT DISTINCT e.kind, e.ref FROM evidence e
                 JOIN archive a ON a.id = e.archive_id
                WHERE a.stream = ? AND coalesce(a.thread, '') = ?""",
            (stream, thread)).fetchall()
        for row in refs:
            moved += note_review(conn, row["kind"], row["ref"], ids,
                                 by_run=by_run, by_stage=by_stage, commit=False)
    if commit:
        conn.commit()
    return moved


# ------------------------------------------------------------ nomination --

def evidence_threads(conn: sqlite3.Connection, kind: str, ref: str,
                     ) -> list[tuple[str, str]]:
    """Exact source conversations already cited for this fact.

    Authored turns (``agent``, ``cli``) are not collected observations: the
    author already knows what they said, so a later turn in the same session
    is never another fact's "new activity". Streams without conversations
    (see ``trace.UNTHREADED_STREAMS``) are covered by item families instead.
    """
    from . import archive as archive_mod  # noqa: PLC0415
    from . import trace as trace_mod  # noqa: PLC0415
    unthreaded = trace_mod.UNTHREADED_STREAMS
    return [(row["stream"], row["thread"] or "") for row in conn.execute(
        """SELECT DISTINCT a.stream AS stream, coalesce(a.thread, '') AS thread
             FROM evidence e JOIN archive a ON a.id = e.archive_id
            WHERE e.kind = ? AND e.ref = ? AND a.thread IS NOT NULL
              AND a.stream NOT IN (%s)
            ORDER BY 1, 2""" % ",".join("?" * (len(archive_mod.INTERNAL_STREAMS)
                                                + len(unthreaded))),
        (kind, ref, *archive_mod.INTERNAL_STREAMS, *unthreaded))]


def evidence_families(conn: sqlite3.Connection, kind: str, ref: str,
                      ) -> list[tuple[str, str]]:
    """Cited item families for streams without conversations.

    On an unthreaded stream (a calendar) the thread names a whole collection,
    so sharing it proves nothing. What recurs is the item: for those streams
    the external id leads with a stable identity (`identity:digest`), and a
    new revision of a cited item is activity on exactly that item.
    """
    from . import trace as trace_mod  # noqa: PLC0415
    unthreaded = trace_mod.UNTHREADED_STREAMS
    if not unthreaded:
        return []
    return [(row["stream"], row["family"]) for row in conn.execute(
        """SELECT DISTINCT a.stream AS stream,
                  substr(a.external_id, 1, instr(a.external_id, ':') - 1) AS family
             FROM evidence e JOIN archive a ON a.id = e.archive_id
            WHERE e.kind = ? AND e.ref = ? AND a.stream IN (%s)
              AND instr(a.external_id, ':') > 0
            ORDER BY 1, 2""" % ",".join("?" * len(unthreaded)),
        (kind, ref, *unthreaded))]


def ref_persons(conn: sqlite3.Connection, kind: str, ref: str) -> set[str]:
    """People and handles behind this fact's cited lines."""
    out: set[str] = set()
    for row in conn.execute(
            """SELECT DISTINCT a.person, a.handle FROM evidence e
                 JOIN archive a ON a.id = e.archive_id
                WHERE e.kind = ? AND e.ref = ?""", (kind, ref)):
        for value in (row["person"], row["handle"]):
            if value and value != "me":
                out.add(str(value))
    return out


def _title_for(conn: sqlite3.Connection, kind: str, ref: str) -> str:
    if kind == "event":
        from . import events  # noqa: PLC0415
        row = events.get(conn, ref)
        return row.title if row else ""
    if kind == "todo":
        from . import todos  # noqa: PLC0415
        try:
            row = todos.get(conn, ref)
        except Exception:
            return ""
        return row.text if row else ""
    if kind == "wiki":
        return str(ref).split(".")[0].replace("-", " ")
    return ""


def associations(conn: sqlite3.Connection, kind: str, ref: str) -> dict:
    """Strong and weak source associations for one fact.

    Strong: exact conversations already cited as its evidence. Weak: at most a
    few threads sharing a participant *and* a distinctive title term — a
    nomination for the model, never a verdict.
    """
    strong = [{"stream": s, "thread": t} for s, t in evidence_threads(conn, kind, ref)]
    strong += [{"stream": s, "family": f} for s, f in evidence_families(conn, kind, ref)]
    strong_keys = {(item["stream"], item.get("thread") or "") for item in strong}
    weak: list[dict] = []
    persons = ref_persons(conn, kind, ref)
    terms = distinctive_terms(_title_for(conn, kind, ref))
    if persons and terms:
        candidates = conn.execute(
            """SELECT DISTINCT stream, thread FROM archive
                WHERE thread IS NOT NULL AND thread != ''
                  AND (person IN (%s) OR handle IN (%s))""" % (
                ",".join("?" * len(persons)), ",".join("?" * len(persons))),
            [*persons, *persons]).fetchall()
        for row in candidates:
            key = (row["stream"], row["thread"] or "")
            if key in strong_keys or not key[1]:
                continue
            lowered = key[1].lower()
            hit = next((term for term in terms if term in lowered), "")
            if not hit:
                continue
            weak.append({"stream": key[0], "thread": key[1],
                         "why": f"shared participant plus {hit!r}"})
            if len(weak) >= WEAK_THREAD_CAP:
                break
    return {"strong": strong, "weak": weak}


def _pending_rows(conn: sqlite3.Connection, pairs: list[dict],
                  covered: set[int], *, limit: int,
                  after: int = 0) -> tuple[list[dict], int]:
    """Associated arrivals minus exactly the covered observations, oldest first.

    `limit` bounds the returned rows; the total rides alongside so callers can
    say what was omitted. `after` pages after an archive id without covering
    anything. Muted threads and explicitly ignored senders are excluded from
    both — they must not leak, nor inflate the count. Automatic low relevance
    alone hides nothing.

    Pairs name either a conversation (`stream` + `thread`) or, on streams
    without conversations, one cited item (`stream` + `family`).
    """
    if not pairs:
        return [], 0
    clauses, args = [], []
    for pair in pairs:
        if pair.get("family"):
            clauses.append("(a.stream = ? AND (a.external_id = ?"
                           " OR a.external_id LIKE ? ESCAPE '\\'))")
            args += [pair["stream"], pair["family"],
                     pair["family"].replace("\\", "\\\\")
                     .replace("%", "\\%").replace("_", "\\_") + ":%"]
        else:
            clauses.append("(a.stream = ? AND coalesce(a.thread, '') = ?)")
            args += [pair["stream"], pair.get("thread") or ""]
    clause = " OR ".join(clauses)
    scope = ("LEFT JOIN threads t ON t.stream = a.stream AND t.thread = a.thread"
             " LEFT JOIN senders s ON s.address = a.handle")
    hidden = ("coalesce(t.decision, '') != 'mute'"
              " AND NOT (s.decision IN ('archive', 'ignore')"
              " AND coalesce(s.source, 'auto') != 'auto')")
    seen = ""
    if covered:
        seen = f" AND a.id NOT IN ({','.join('?' * len(covered))})"
        args = [*args, *sorted(covered)]
    if after:
        args = [*args, after]
        paging = " AND a.id > ?"
    else:
        paging = ""
    total = conn.execute(
        f"SELECT count(*) AS n FROM archive a {scope}"
        f" WHERE ({clause}) AND {hidden}{seen}{paging}",
        args).fetchone()["n"]
    rows = conn.execute(
        f"""SELECT a.* FROM archive a {scope}
            WHERE ({clause}) AND {hidden}{seen}{paging}
            ORDER BY a.id LIMIT ?""", [*args, limit]).fetchall()
    items = [{
        "id": row["id"],
        "ts": str(row["ts"]),
        "arrived": str(row["created_at"] or ""),
        "stream": row["stream"],
        "thread": row["thread"] or "",
        "who": ("me" if row["from_me"]
                else (row["person"] or row["handle"] or "?")),
        "text": row["text"] or "",
    } for row in rows]
    return items, int(total or 0)


def pending(conn: sqlite3.Connection, kind: str, ref: str,
            *, limit: int = 50) -> dict:
    """Associated observations minus exactly the covered ones."""
    links = associations(conn, kind, ref)
    covered = reviewed_ids(conn, kind, ref)
    strong_items, strong_total = _pending_rows(conn, links["strong"], covered,
                                              limit=limit)
    weak_items, weak_total = _pending_rows(conn, links["weak"], covered,
                                          limit=limit)
    return {"strong": strong_items, "weak": weak_items,
            "strong_total": strong_total, "weak_total": weak_total,
            "reviewed": len(covered), "mark": reviewed_max(conn, kind, ref)}


def read(conn: sqlite3.Connection, kind: str, ref: str, *,
         cursor: int = 0, limit: int = 20) -> dict:
    """One bounded page of uncovered observations, in arrival order.

    `cursor` is an archive id; the next page passes back `next_cursor`.
    Already-covered observations are skipped even below the cursor, so a
    selective review never resurfaces what it acknowledged. Reading never
    marks anything reviewed.
    """
    links = associations(conn, kind, ref)
    covered = reviewed_ids(conn, kind, ref)
    start = int(cursor or 0)
    items, total = _pending_rows(conn, links["strong"], covered,
                                 limit=limit + 1, after=start)
    omitted = max(0, total - len(items[:limit]))
    page = items[:limit]
    return {"items": page, "reviewed": len(covered),
            "mark": reviewed_max(conn, kind, ref),
            "next_cursor": page[-1]["id"] if page else start,
            "omitted": omitted, "total": total}


def unlinked_backlog(conn: sqlite3.Connection, *, limit: int = 5) -> list[dict]:
    """Pending spool traffic in conversations no fact is associated with.

    For broad questions ("anything fun this weekend?") so unreviewed material
    with no typed row yet cannot hide behind an exhaustive-coverage claim — and
    is never presented as a confirmed opportunity. Muted and ignored material
    stays out.
    """
    associated = {(row["stream"], row["thread"] or "") for row in conn.execute(
        """SELECT DISTINCT a.stream AS stream, coalesce(a.thread, '') AS thread
             FROM evidence e JOIN archive a ON a.id = e.archive_id
            WHERE a.thread IS NOT NULL""")}
    from . import archive as archive_mod  # noqa: PLC0415
    internal = set(archive_mod.INTERNAL_STREAMS)
    rows = conn.execute(
        """SELECT a.stream, a.thread, count(*) AS n, max(a.ts) AS newest
             FROM spool s JOIN archive a ON a.id = s.archive_id
            WHERE s.processed_at IS NULL
            GROUP BY a.stream, a.thread ORDER BY newest DESC""").fetchall()
    out = []
    for row in rows:
        key = (row["stream"], row["thread"] or "")
        if key in associated or not key[1] or key[0] in internal:
            continue
        from . import threads  # noqa: PLC0415
        if threads.is_muted(conn, key[0], key[1]):
            continue
        out.append({"stream": key[0], "thread": key[1], "waiting": row["n"],
                    "newest": str(row["newest"] or "")[:16]})
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------- surfaces --

def resolve_handle(conn: sqlite3.Connection, handle: str) -> tuple[str, str]:
    """A brief handle (``E42``) to ``(kind, ref)``. Raises `LookupError`."""
    from . import brief, trace  # noqa: PLC0415
    parsed = brief.parse_source(str(handle or "").strip().strip("〔〕"))
    if not parsed:
        raise LookupError(f"give a brief handle such as E42, not {handle!r}")
    resolved = trace.resolve_source(conn, str(handle).strip().strip("〔〕"))
    if resolved.get("error"):
        raise LookupError(resolved["error"])
    return resolved["kind"], resolved["ref"]


def format_read(page: dict, weak: list[dict], *, label: str) -> str:
    """One bounded activity page as plain text. Shared by CLI, MCP, and Hermes.

    Reading changes nothing; the counts ride along so the caller can see what
    a cited correction or explicit review would acknowledge.
    """
    lines = [f"new activity for {label} ({page['total']} message(s)"
             + (f", {page['omitted']} omitted" if page["omitted"] else "")
             + f"; {page['reviewed']} already reviewed)"]
    if not page["items"]:
        lines.append("(nothing new since the last review)")
    for item in page["items"]:
        lines.append(f"[{item['id']}] {item['ts'][:16]} (collected {item['arrived'][:16]})"
                     f"  {item['stream']}/{item['thread']} · {item['who']}:")
        lines.append(f"  {item['text']}")
    if page["items"]:
        lines.append(f"(next cursor: {page['next_cursor']}; reading changes nothing — "
                     f"cite [ids] in a correction to acknowledge them)")
    for weak_thread in weak:
        lines.append(f"(possibly related: {weak_thread['stream']}/"
                     f"{weak_thread['thread']} — {weak_thread['why']})")
    return "\n".join(lines)
