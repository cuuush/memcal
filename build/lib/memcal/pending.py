"""Observations that change something, whose target cannot yet be named.

A cancellation may name no title and no date, or name a booking no row holds yet. Held
here with its evidence rather than dropped or written as a phantom row, and retried as
later evidence lands. The originating bundle is still marked read.
"""

from __future__ import annotations

import re
import sqlite3

from . import db, events

#: How many passes a pending change is retried before it stops asking. It is not
#: deleted: an observation nobody could place is still a true thing somebody said, and
#: the archive keeps it either way.
MAX_ATTEMPTS = 6


def note(conn: sqlite3.Connection, *, kind: str, observation: str,
         entity: str | None = None, candidates: list[str] | None = None,
         observed_at: str | None = None, title: str = "", date: str = "",
         time: str = "", location: str = "",
         target_key: str = "", decided_by: str = "", commit: bool = False) -> str:
    """Record one unplaceable change. Repeating the same observation is a no-op."""
    text = " ".join(str(observation or "").split())[:400]
    if not text:
        return ""
    stamp = db.now()
    conn.execute(
        """INSERT INTO pending_changes(kind, observation, observed_at, subject_title,
                                       subject_date, subject_time, subject_location,
                                       target_key, decided_by, entity,
                                       candidates, status, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?, 'open', ?,?)
           ON CONFLICT(kind, observation) DO UPDATE SET updated_at = excluded.updated_at""",
        (kind, text, observed_at or stamp, title or None, date or None,
         time or None, location or None,
         target_key or None, decided_by or None, entity,
         db.jdump(list(candidates or [])), stamp, stamp))
    if commit:
        conn.commit()
    return text


def open_items(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM pending_changes WHERE status = 'open' ORDER BY id LIMIT ?",
        (limit,)).fetchall()


def resolve(conn: sqlite3.Connection, row_id: int, ref: str, *,
            commit: bool = True) -> None:
    conn.execute(
        "UPDATE pending_changes SET status = 'resolved', resolved_ref = ?,"
        " updated_at = ? WHERE id = ?", (ref, db.now(), row_id))
    if commit:
        conn.commit()


def retry(conn: sqlite3.Connection, *, ask=None) -> list[str]:
    """Apply identified observations and ask about unresolved candidates."""
    log: list[str] = []
    for row in open_items(conn):
        # The answer to the question the last pass asked, if one came back. Without this
        # step the loop had no way to ever finish: `_identified` requires `target_key`,
        # `settle` is the only thing that sets it, and nothing called `settle` — so a
        # cancellation was asked about once and then held open forever.
        picked = _chosen(conn, row)
        if picked == "":
            conn.execute(
                "UPDATE pending_changes SET status = 'abandoned', updated_at = ?"
                " WHERE id = ?", (db.now(), row["id"]))
            log.append(f"dropped {row['observation'][:60]} — you said none of them")
            continue
        if picked and settle(conn, row["id"], picked, commit=False,
                             decided_by=f"you:answered q:pending:{row['id']}"):
            row = conn.execute("SELECT * FROM pending_changes WHERE id = ?",
                               (row["id"],)).fetchone()
        found = _place(conn, row)
        certain = _identified(conn, row)
        conn.execute("UPDATE pending_changes SET attempts = attempts + 1,"
                     " candidates = ?, updated_at = ? WHERE id = ?",
                     (db.jdump([e.key for e in found]), db.now(), row["id"]))
        if certain is not None:
            evidence_at = str(row["observed_at"] or row["created_at"])
            stored = conn.execute(
                "SELECT evidence_ts, created_at FROM events WHERE id = ?",
                (certain.id,)).fetchone()
            status_at = events._field_versions(
                conn, certain.id, str(stored["evidence_ts"] or stored["created_at"]))["status"]
            if db.parse_ts(evidence_at) < db.parse_ts(status_at):
                resolve(conn, row["id"], certain.key, commit=False)
                log.append(f"settled {row['observation'][:60]} → {certain.key}, newer status")
                continue
            events.upsert(conn, {"key": certain.key, "date": certain.date,
                                 "status": "declined"},
                          written_by="dream:nightly", match=False,
                          # The moment it was said. Placing it later does not make it
                          # newer evidence than the correction it might be walking back.
                          evidence_ts=evidence_at,
                          commit=False)
            resolve(conn, row["id"], certain.key, commit=False)
            log.append(f"placed  {row['observation'][:60]} → {certain.key}")
            continue
        if row["target_key"]:
            # Identified, and the row is already off — somebody else got there first, or
            # this observation was applied and is being re-read. Nothing left to do, and
            # holding it open would keep asking about a decision already made.
            known = events.get(conn, str(row["target_key"]))
            if known is not None and known.status == "declined":
                resolve(conn, row["id"], known.key, commit=False)
                log.append(f"settled {row['observation'][:60]} → {known.key}, already off")
                continue
        # Nothing identified it. Everything below is a nomination, and a nomination is
        # not permission to cancel: "Dental cleaning on September 15" nominates the piano
        # lesson on the 15th, because sharing a day is one of the edges that earns a row
        # a look, and "physio on Friday" nominates the physio on Monday because a
        # provider calls every appointment the same thing. Being the only thing shown is
        # not the same as being the thing meant.
        asked = conn.execute("SELECT 1 FROM questions WHERE key = ?",
                             (f"q:pending:{row['id']}",)).fetchone()
        if found and ask is not None and asked is None:
            names = ", ".join(f"{e.title} on {e.date}" for e in found[:3])
            ask(f"Something was cancelled — \"{row['observation'][:80]}\". "
                f"Which one: {names}?", f"q:pending:{row['id']}")
            log.append(f"asked   {row['observation'][:60]} ({len(found)} candidate(s))")
            continue
        if found:
            log.append(f"held    {row['observation'][:60]} — "
                       f"{len(found)} candidate(s), none identified")
            continue
        if row["attempts"] + 1 >= MAX_ATTEMPTS:
            conn.execute(
                "UPDATE pending_changes SET status = 'abandoned', updated_at = ?"
                " WHERE id = ?", (db.now(), row["id"]))
            log.append(f"gave up {row['observation'][:60]} — nothing matched it")
    conn.commit()
    return log


#: An answer that names no candidate at all. Narrow on purpose: "not the physio one" is
#: a choice between the others and must not read as a refusal of all of them.
_DECLINED = frozenset({"none", "neither", "no", "nothing", "cancel", "ignore"})


def _chosen(conn: sqlite3.Connection, row: sqlite3.Row) -> str | None:
    """Which candidate the *user* picked, from their answer to our question.

    `None` if they have not answered, `""` if they answered that it was none of them,
    and otherwise the event key. A person choosing between rows we showed them is the
    citation this module wants; what it refuses is code picking for them, which is why
    an answer naming two of the candidates equally settles nothing and waits.
    """
    asked = conn.execute(
        "SELECT answer FROM questions WHERE key = ? AND status = 'answered'",
        (f"q:pending:{row['id']}",)).fetchone()
    if asked is None:
        return None
    said = " ".join(str(asked["answer"] or "").split()).casefold()
    if not said:
        return None
    words = set(re.findall(r"[a-z0-9']+", said))
    scored: list[tuple[int, str]] = []
    for key in db.jload(row["candidates"], []):
        event = events.get(conn, str(key))
        if event is None:
            continue
        title = {w for w in re.findall(r"[a-z0-9']{3,}", event.title.casefold())}
        hits = len(words & title) + (2 if event.date in said else 0)
        if hits:
            scored.append((hits, event.key))
    if not scored:
        return "" if words & _DECLINED else None
    scored.sort(reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None                    # they named two of them; that settles nothing
    return scored[0][1]


def _identified(conn: sqlite3.Connection, row: sqlite3.Row) -> events.Event | None:
    """The row this observation is about, when that is established rather than guessed.

    A stable key or a cited semantic decision establishes it. Dates, times, titles and
    replacement links nominate candidates for that decision; they do not authorize a
    cancellation on their own.
    """
    key = str(row["target_key"] or "").strip()
    if not key:
        return None
    event = events.get(conn, key)
    if event is None or event.status == "declined":
        return None
    return event


def settle(conn: sqlite3.Connection, row_id: int, event_key: str, *,
           decided_by: str, commit: bool = True) -> bool:
    """Record the decision that says which row an observation was about.

    `decided_by` is the citation: who or what settled it — an answered question, a model
    judgement with the lines it read. The next `retry()` applies it; nothing is written
    here, so the evidence-ordering rules still get their say.
    """
    if events.get(conn, event_key) is None:
        return False
    conn.execute(
        "UPDATE pending_changes SET target_key = ?, decided_by = ?, updated_at = ?"
        " WHERE id = ? AND status = 'open'",
        (event_key, decided_by, db.now(), row_id))
    if commit:
        conn.commit()
    return True


def _place(conn: sqlite3.Connection, row: sqlite3.Row) -> list[events.Event]:
    """Which upcoming rows this observation could be about. Nominations, not a match."""
    terms = [row["observation"], row["subject_title"], row["subject_date"],
             row["subject_time"], row["subject_location"]]
    _same, related, nominated, _cut = events.candidates(
        conn, people=[], entity=row["entity"],
        text=" ".join(str(term) for term in terms if term),
        entities=[])
    seen: dict[str, events.Event] = {}
    for event in [*related, *nominated]:
        if event.status != "declined":
            seen.setdefault(event.key, event)
    return list(seen.values())
