"""One model call over the whole identity picture: names, merges, and non-people.

`identity.py` is a dictionary and stays one. This call reads the unresolved queue
and the roster of known people together, since an opaque handle is identified by
the rest of the board.

Nothing here is a judgement. Merges are recorded and reversible, doubts are
recorded as doubts, and every link is written at a rank Contacts outranks.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from . import db, identity, trace
from .config import Config
from .llm import CompletionClient

#: How many of each kind to put in front of the model, busiest first.
MAX_UNRESOLVED = 120
MAX_PEOPLE = 250
#: Threads listed per person.
MAX_THREADS = 6

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "names": {
            "type": "array",
            "description": "Unresolved handles that belong to a person you can name.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "handle": {"type": "string"},
                    "person": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["handle", "person", "why"],
            },
        },
        "same": {
            "type": "array",
            "description": "Two names already on file that are one human being.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "keep": {"type": "string"},
                    "also": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["keep", "also", "why"],
            },
        },
        "unsure": {
            "type": "array",
            "description": "Anything you suspect but will not commit to. Nothing is "
                           "done with these; they are put to the user as questions.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {"type": "string", "enum": ["merge", "name"]},
                    "about": {"type": "string",
                              "description": "The roster name, or the handle, in question."},
                    "guess": {"type": "string",
                              "description": "Who it might be. Empty if you have no candidate."},
                    "why": {"type": "string",
                            "description": "What is missing, or what would settle it."},
                },
                "required": ["kind", "about", "guess", "why"],
            },
        },
        "not_people": {
            "type": "array",
            "description": "Handles with no human behind them at all.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "handle": {"type": "string"},
                    "label": {"type": "string"},
                    "kind": {"type": "string",
                             "enum": ["service", "bot", "announcement", "mailing-list"]},
                    "why": {"type": "string"},
                },
                "required": ["handle", "label", "kind", "why"],
            },
        },
    },
    "required": ["names", "same", "unsure", "not_people"],
}

INSTRUCTIONS = """\
You are resolving identity for a personal memory system. You will be given every handle
it cannot name, and every person it already knows, with where each one speaks and how
much.

Answer three questions.

1. `names` — which unresolved handles belong to a person you can name?
   Prefer a spelling already in the roster over inventing a new one: the name is a
   database key, so "Cam Ortiz" and "Cameron Ortiz" are two people unless they are
   written the same way. If the roster has the person, use the roster's exact spelling.
   Leave a handle out if the evidence is only that a first name is common.

2. `same` — which two roster names are one human being?
   Assume they are the same person when the evidence points that way; you do not need to
   be certain, because every merge here is recorded and can be split apart again. Name
   variants are the main case: diminutives (Nick/Nicholas, Joe/Joseph, Kate/Katherine),
   a first name against a full name, a maiden against a married surname, a transliteration.
   `keep` is the fuller, more identifying spelling; `also` is folded into it.

   Weigh against a merge:
   - Both speak in the same conversation, especially several. Two people in one group
     chat are usually two people. Not decisive — one human with accounts on two
     platforms can appear twice in a group spanning both — but it is the strongest
     evidence against that you have.
   - Each is a separate card in the address book (handles marked `contacts`). An address
     book listing them separately is a person saying they are two people.
   - A shared given name with no shared surname, handle, or context. Given names repeat.

3. `unsure` — what do you suspect and refuse to commit to?
   Use this freely. It is the answer whenever you can see a possibility but not enough
   to act on it: a handle that might belong to someone on the roster, two names that
   could be one person if you knew one more thing. Nothing is done with these — they
   are put to the user as questions, which is the only way a fact you cannot reach
   from the board ever gets in. Say in `why` what would settle it.

   Never resolve a doubt by guessing into `names` or `same`. Uncertainty belongs here.

4. `not_people` — which handles have no human behind them?
   Companies, no-reply addresses, receipts, notification bots, mailing lists. These stop
   being asked about. `label` is what to call it — the company, not the address.

Prefer `unsure` to silence, and silence to a guess. An omission costs one unnamed
handle; a wrong merge folds two people's histories together.
"""


@dataclass
class Resolution:
    """What the call concluded, before any of it is written down."""
    names: list[dict] = field(default_factory=list)
    same: list[dict] = field(default_factory=list)
    unsure: list[dict] = field(default_factory=list)
    not_people: list[dict] = field(default_factory=list)
    truncated: bool = False


# ----------------------------------------------------------------- the board ----

def _threads_for(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Where each known person speaks, busiest first."""
    out: dict[str, list[str]] = {}
    for row in conn.execute(
            "SELECT person, stream, thread, count(*) AS n FROM archive"
            "  WHERE person IS NOT NULL AND person <> '' AND from_me = 0"
            "    AND thread IS NOT NULL AND thread <> ''"
            "  GROUP BY person, stream, thread ORDER BY person, n DESC"):
        seen = out.setdefault(row["person"], [])
        if len(seen) < MAX_THREADS:
            seen.append(f"{row['stream']}/{row['thread']}")
    return out


def roster(conn: sqlite3.Connection) -> list[dict]:
    """Everyone already on file, with what makes them identifiable."""
    volume = {row["person"]: row["n"] for row in conn.execute(
        "SELECT person, count(*) AS n FROM archive"
        "  WHERE person IS NOT NULL AND person <> '' AND from_me = 0 GROUP BY person")}
    handles: dict[str, list[str]] = {}
    for row in conn.execute("SELECT handle, person, source FROM handles"
                            " WHERE person IS NOT NULL AND person <> ''"):
        handles.setdefault(row["person"], []).append(f"{row['handle']} [{row['source']}]")
    where = _threads_for(conn)
    people = sorted(set(volume) | set(handles) | set(where),
                    key=lambda p: (-volume.get(p, 0), p))
    return [{"person": person, "messages": volume.get(person, 0),
             "handles": sorted(handles.get(person, []))[:6],
             "speaks_in": where.get(person, [])}
            for person in people[:MAX_PEOPLE]
            if not identity.is_me(conn, person)]


def queue(conn: sqlite3.Connection) -> list[dict]:
    """The handles nothing can name, busiest first."""
    out = []
    for row in identity.unresolved(conn, limit=MAX_UNRESOLVED):
        # Skip threads that echo the handle; one-to-one email threads are named for it.
        seen = [t for t in identity.where_seen(conn, row["handle"], limit=3)
                if t != row["handle"]]
        out.append({"handle": row["handle"], "stream": row["stream"],
                    "display_name": (row["seen_name"] or "").strip(),
                    "messages": row["count"], "speaks_in": seen,
                    "sample": (row["sample"] or "")[:160]})
    return out


def picture(conn: sqlite3.Connection) -> str:
    """The whole board as one payload."""
    mine = identity.me_names(conn)
    return db.jdump({"me": mine, "unresolved": queue(conn), "roster": roster(conn)})


def ceiling(payload: str) -> int:
    """Room to answer about every row, not just the first few."""
    return min(24000, max(4000, 1200 + len(payload) // 6))


# -------------------------------------------------------------------- the call --

def resolve(client: CompletionClient, conn: sqlite3.Connection, cfg: Config, *,
            run_id: int | None = None) -> Resolution:
    """One call. Returns what it concluded; writes nothing."""
    payload = picture(conn)
    room = ceiling(payload)
    suffix = payload + "\n\nResolve these identities."
    reply = client.complete(
        model=cfg.sweep_model,
        prefix=INSTRUCTIONS,
        suffix=suffix,
        schema=SCHEMA,
        schema_name="memcal_identity",
        max_tokens=room,
        reasoning_effort=cfg.reasoning_effort or None,
    )
    trace.record(conn, run_id=run_id, stage="whois", label="identity picture",
                 reply=reply, max_tokens=room, home=cfg.home, prefix=INSTRUCTIONS,
                 suffix=suffix)
    data = reply.data if isinstance(reply.data, dict) else {}
    # A truncated reply is a partial merge list, which cannot be applied safely.
    if reply.truncated:
        return Resolution(truncated=True)
    return Resolution(names=list(data.get("names") or []),
                      same=list(data.get("same") or []),
                      unsure=list(data.get("unsure") or []),
                      not_people=list(data.get("not_people") or []))


# ------------------------------------------------------------ writing it down --

#: Hard rails on merges. The prompt argues; these refuse.
def _refusal(conn: sqlite3.Connection, keep: str, also: str) -> str | None:
    """Why this merge must not happen, or None."""
    if not keep or not also or keep == also:
        return "not two names"
    if identity.is_me(conn, keep) or identity.is_me(conn, also):
        return "one of them is you"
    known = {row["person"] for row in conn.execute(
        "SELECT DISTINCT person FROM handles WHERE person IS NOT NULL")}
    known |= {row["person"] for row in conn.execute(
        "SELECT DISTINCT person FROM archive WHERE person IS NOT NULL AND person <> ''")}
    missing = [n for n in (keep, also) if n not in known]
    if missing:
        return f"no such person: {', '.join(missing)}"
    # Two address-book cards for distinct handles assert two people.
    cards = {}
    for name in (keep, also):
        cards[name] = {row["handle"] for row in conn.execute(
            "SELECT handle FROM handles WHERE person = ? AND source = 'contacts'",
            (name,))}
    if cards[keep] and cards[also] and not (cards[keep] & cards[also]):
        return "separate cards in Contacts"
    return None


def assume(conn: sqlite3.Connection, keep: str, also: str, why: str = "",
           source: str = "model") -> int | None:
    """Fold `also` into `keep`, recording enough to undo it exactly.

    Returns the assumption's id, or None if a rail refused it.
    """
    if _refusal(conn, keep, also):
        return None
    moved = [row["handle"] for row in conn.execute(
        "SELECT handle FROM handles WHERE person = ?", (also,))]
    cur = conn.execute(
        "INSERT INTO identity_assumptions(kind, keep, also, handles_moved, why, state,"
        " source, created_at) VALUES('merge',?,?,?,?,'assumed',?,?)",
        (keep, also, db.jdump(sorted(moved)), why or "", source, db.now()))
    conn.execute("UPDATE handles SET person = ? WHERE person = ?", (keep, also))
    conn.execute("UPDATE archive SET person = ? WHERE person = ?", (keep, also))
    conn.commit()
    return int(cur.lastrowid)


def _rebuild_merges(conn: sqlite3.Connection) -> None:
    """Rebuild affected handle names from their baseline and the active merge edges."""
    rows = conn.execute(
        "SELECT id, keep, also, handles_moved, state FROM identity_assumptions"
        " WHERE kind = 'merge' ORDER BY id"
    ).fetchall()
    baseline: dict[str, str] = {}
    moved_by: list[tuple[sqlite3.Row, list[str]]] = []
    for row in rows:
        moved = [str(handle) for handle in db.jload(row["handles_moved"], [])]
        moved_by.append((row, moved))
        for handle in moved:
            baseline.setdefault(handle, row["also"])

    rebuilt = dict(baseline)
    for row, moved in moved_by:
        if row["state"] not in ("assumed", "confirmed"):
            continue
        for handle in moved:
            if rebuilt.get(handle) == row["also"]:
                rebuilt[handle] = row["keep"]

    for handle, person in rebuilt.items():
        conn.execute("UPDATE handles SET person = ? WHERE handle = ?", (person, handle))
        conn.execute("UPDATE archive SET person = ? WHERE handle = ?", (person, handle))


def split(conn: sqlite3.Connection, assumption_id: int) -> str | None:
    """Undo one merge. Returns the name put back, or None if there was nothing to undo.

    Only handles recorded at merge time go back; rows written under the merged name
    since keep pointing at `keep`.
    """
    row = conn.execute("SELECT * FROM identity_assumptions WHERE id = ?",
                       (assumption_id,)).fetchone()
    if not row or row["state"] == "split":
        return None
    # A doubt moved nothing, so declining it only records the answer.
    if row["state"] == "unsure":
        conn.execute("UPDATE identity_assumptions SET state = 'split', decided_at = ?,"
                     " source = 'you' WHERE id = ?", (db.now(), assumption_id))
        conn.commit()
        return row["also"]
    conn.execute("UPDATE identity_assumptions SET state = 'split', decided_at = ?"
                 " WHERE id = ?", (db.now(), assumption_id))
    # Later assumptions may depend on handles this one moved; rebuild the chain
    # instead of replaying stored lists directly.
    _rebuild_merges(conn)
    conn.commit()
    return row["also"]


def doubt(conn: sqlite3.Connection, kind: str, about: str, guess: str = "",
          why: str = "", source: str = "model") -> int | None:
    """Record something suspected and not acted on, so it can be asked rather than lost."""
    if kind not in ("merge", "name") or not about.strip():
        return None
    if conn.execute("SELECT 1 FROM identity_assumptions WHERE kind = ? AND also = ?"
                    " AND state = 'unsure'", (kind, about)).fetchone():
        return None                      # already open; asking twice is not asking harder
    cur = conn.execute(
        "INSERT INTO identity_assumptions(kind, keep, also, handles_moved, why, state,"
        " source, created_at) VALUES(?,?,?,'[]',?,'unsure',?,?)",
        (kind, guess.strip(), about.strip(), why or "", source, db.now()))
    conn.commit()
    return int(cur.lastrowid)


def confirm(conn: sqlite3.Connection, assumption_id: int) -> str | None:
    """"Yes." On a merge already in effect that is bookkeeping; on a doubt it acts."""
    row = conn.execute("SELECT * FROM identity_assumptions"
                       " WHERE id = ? AND state IN ('assumed', 'unsure')",
                       (assumption_id,)).fetchone()
    if not row:
        return None
    said = f"{row['also']} → {row['keep']}"
    if row["state"] == "unsure":
        if not row["keep"]:
            return None                  # no candidate to confirm; name it by hand
        if row["kind"] == "merge":
            if _refusal(conn, row["keep"], row["also"]):
                return None
            moved = [r["handle"] for r in conn.execute(
                "SELECT handle FROM handles WHERE person = ?", (row["also"],))]
            conn.execute("UPDATE handles SET person = ? WHERE person = ?",
                         (row["keep"], row["also"]))
            conn.execute("UPDATE archive SET person = ? WHERE person = ?",
                         (row["keep"], row["also"]))
            conn.execute("UPDATE identity_assumptions SET handles_moved = ? WHERE id = ?",
                         (db.jdump(sorted(moved)), assumption_id))
        else:
            identity.link(conn, row["also"], row["keep"], source="cli")
    conn.execute("UPDATE identity_assumptions SET state = 'confirmed', decided_at = ?,"
                 " source = 'you' WHERE id = ?", (db.now(), assumption_id))
    conn.commit()
    return said


def assumptions(conn: sqlite3.Connection, state: str = "assumed") -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM identity_assumptions WHERE state = ? ORDER BY id", (state,)
    ).fetchall()


def mark_not_person(conn: sqlite3.Connection, handle: str, label: str = "",
                    kind: str = "service", why: str = "",
                    source: str = "model") -> bool:
    """Take a handle out of the name-this-person queue for good."""
    h = identity.normalize(handle)
    if not h or identity.resolve(conn, h):
        return False
    conn.execute(
        "INSERT INTO non_people(handle, label, kind, why, source, decided_at)"
        " VALUES(?,?,?,?,?,?) ON CONFLICT(handle) DO UPDATE SET label = excluded.label,"
        " kind = excluded.kind, why = excluded.why, source = excluded.source,"
        " decided_at = excluded.decided_at",
        (h, (label or "").strip(), kind, why, source, db.now()))
    conn.execute("DELETE FROM unresolved WHERE handle = ?", (h,))
    conn.commit()
    return True


def apply(conn: sqlite3.Connection, found: Resolution) -> list[str]:
    """Write down what the call concluded. Returns one line per thing that changed."""
    log: list[str] = []
    if found.truncated:
        return ["identity reply was cut off at its ceiling — nothing from it was applied"]

    for row in found.not_people:
        handle = str(row.get("handle") or "")
        if mark_not_person(conn, handle, str(row.get("label") or ""),
                           str(row.get("kind") or "service"), str(row.get("why") or "")):
            log.append(f"not a person  {handle} — {row.get('label') or row.get('kind')}")

    for row in found.names:
        handle, person = str(row.get("handle") or ""), str(row.get("person") or "").strip()
        if not handle or not identity.name_shaped(person):
            continue
        if identity.link(conn, handle, person, source="model"):
            log.append(f"named         {identity.normalize(handle)} → {person}")
        else:
            log.append(f"kept          {identity.normalize(handle)} — better evidence "
                       f"already names it {identity.resolve(conn, handle)}")

    for row in found.same:
        keep, also = str(row.get("keep") or ""), str(row.get("also") or "")
        refused = _refusal(conn, keep, also)
        if refused:
            log.append(f"refused merge {also} → {keep} — {refused}")
            continue
        number = assume(conn, keep, also, why=str(row.get("why") or ""))
        if number:
            log.append(f"assumed same  [{number}] {also} → {keep} — {row.get('why') or ''}")

    for row in found.unsure:
        number = doubt(conn, str(row.get("kind") or ""), str(row.get("about") or ""),
                       str(row.get("guess") or ""), str(row.get("why") or ""))
        if number:
            guess = row.get("guess") or "?"
            log.append(f"not sure      [{number}] {row.get('about')} — {guess}? "
                       f"{row.get('why') or ''}")
    return log
