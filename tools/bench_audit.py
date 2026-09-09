#!/usr/bin/env python3
"""Audit a store for suspicious grounding, citation, and extraction gaps."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import dates, db  # noqa: E402

#: Gate verdicts that mean "this line looked like a plan". Mirrors
#: `bundle._PLANNING_REASONS`, `subject-event` deliberately excluded — it is a subject
#: regex that fires on every marketing announcement, and counting it here would report a
#: newsletter backlog as a pile of missed plans (M34).
PLANNING = ("temporal", "invitation", "commitment-verb", "own-commitment", "directive",
            "availability", "question")

_NOISE = {
    "the", "a", "an", "and", "or", "with", "at", "in", "on", "for", "to", "of", "my",
    "our", "we", "us", "is", "are", "be", "night", "day", "morning", "evening", "trip",
    "visit", "meetup", "hang", "out", "event", "thing", "plans", "party", "this", "that",
    "next", "last", "some", "any", "go", "going", "get", "see", "new", "up", "off",
}

#: Language that says an obligation is discharged. Used to find open to-dos the archive
#: has already answered — the general form of M21, where a Venmo receipt sat unread
#: beside an open "Pay Nadia $50".
SETTLED_RE = re.compile(
    r"\b(you paid|paid|sent you|refunded|cancell?ed|completed|delivered|picked up|"
    r"dropped off|done|sorted|taken care of|no longer|rescheduled)\b", re.IGNORECASE)


#: `db.slugify` truncates at 48 characters, which is right for minting a key and silently
#: wrong for reading a corpus: slugifying 1,467 joined lines and splitting the result
#: compares against the first 48 characters of the first line. The `grounding` audit
#: reported that "Poker" shared no word with 1,467 lines that plainly contain it. So
#: tokenising here is done directly, per line, and never through the key-minting helper.
_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    out = set()
    for token in _WORD_RE.findall((text or "").lower()):
        if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        if len(token) > 2 and token not in _NOISE:
            out.add(token)
    return out


def _tokens_of(lines, cap: int = 400) -> set[str]:
    """Every distinctive word across a row's cited lines, unioned line by line."""
    out: set[str] = set()
    for line in lines[:cap]:
        out |= _tokens(str(line["text"])[:2000])
    return out


def _in_archive(conn: sqlite3.Connection, term: str) -> bool:
    """Does this word appear anywhere in the corpus, cited or not?"""
    row = conn.execute("SELECT 1 FROM archive WHERE lower(text) LIKE ? LIMIT 1",
                       (f"%{term.lower()}%",)).fetchone()
    return row is not None


def _evidence(conn: sqlite3.Connection, kind: str) -> dict[str, list[sqlite3.Row]]:
    """ref → the archive rows it cites."""
    out: dict[str, list[sqlite3.Row]] = defaultdict(list)
    rows = conn.execute("""
        SELECT v.ref, a.id, a.ts, a.text, a.person, a.handle, a.stream
        FROM evidence v JOIN archive a ON a.id = v.archive_id
        WHERE v.kind = ?
    """, (kind,)).fetchall()
    for row in rows:
        out[str(row["ref"])].append(row)
    return out


# ------------------------------------------------------------------- audits --


def audit_dates(conn, limit: int) -> tuple[str, int, list[str]]:
    """Does each row's date follow from the words in its own evidence?"""
    findings = []
    events = conn.execute(
        "SELECT key, date, title FROM events WHERE written_by LIKE 'dream:%'").fetchall()
    cited = _evidence(conn, "event")
    checked = 0
    for event in events:
        lines = cited.get(str(event["key"]), [])
        if not lines:
            continue
        candidates: set[str] = set()
        saw_phrase = False
        for line in lines:
            try:
                said_on = db.parse_ts(str(line["ts"]))
            except ValueError:
                continue
            for phrase in dates.claims(str(line["text"])[:2000]):
                saw_phrase = True
                got = dates.resolve(phrase, said_on)
                if got:
                    candidates.add(got)
        if not saw_phrase:
            continue                       # no wording to check against; not a finding
        checked += 1
        if str(event["date"]) not in candidates:
            near = sorted(candidates)[:3]
            findings.append(
                f"{event['key']} · says {event['date']} ({dates.weekday_of(str(event['date']))})"
                f" · evidence supports {', '.join(near) or 'no date at all'}")
    return (f"rows whose date is not supported by their own evidence "
            f"({checked} checkable)"), len(findings), findings[:limit]


def audit_guests(conn, limit: int) -> tuple[str, int, list[str]]:
    """Is every listed guest actually named in the lines the row cites?

    A fabricated attendee is invisible: the row reads perfectly. M28 found the same
    person listed twice under two spellings, which is the benign version of this; the
    malign version is a name that appears nowhere in the evidence at all.
    """
    findings = []
    events = conn.execute("SELECT key, participants FROM events "
                          "WHERE written_by LIKE 'dream:%'").fetchall()
    cited = _evidence(conn, "event")
    for event in events:
        try:
            people = json.loads(event["participants"] or "[]")
        except json.JSONDecodeError:
            continue
        lines = cited.get(str(event["key"]), [])
        if not people or not lines:
            continue
        blob = " ".join(f"{r['text']} {r['person'] or ''}" for r in lines).lower()
        missing = [p for p in people
                   if isinstance(p, str) and p.split()[0].lower() not in blob]
        for name in missing:
            where = "uncited" if _in_archive(conn, name.split()[0]) else "NOT IN CORPUS"
            findings.append(f"{event['key']} · guest \"{name}\" [{where}] "
                            f"· {len(lines)} cited line(s) never name them")
    return "rows listing a guest their evidence never names", len(findings), findings[:limit]


def audit_grounding(conn, limit: int) -> tuple[str, int, list[str]]:
    """Does the title or location share any distinctive word with the evidence?

    A row whose every field is absent from its sources was not read out of them. This is
    a blunt instrument on purpose — it fires rarely, and when it does the row is usually
    invented or attached to the wrong bundle (M9).
    """
    findings = []
    events = conn.execute("SELECT key, title, location FROM events "
                          "WHERE written_by LIKE 'dream:%'").fetchall()
    cited = _evidence(conn, "event")
    for event in events:
        lines = cited.get(str(event["key"]), [])
        if not lines:
            continue
        blob = _tokens_of(lines)
        title = _tokens(str(event["title"]))
        if title and not (title & blob):
            findings.append(f"{event['key']} · title \"{event['title']}\" shares no word "
                            f"with its {len(lines)} cited line(s)")
            continue
        place = _tokens(str(event["location"] or ""))
        if place and not (place & blob):
            head = sorted(place)[0]
            where = "uncited" if _in_archive(conn, head) else "NOT IN CORPUS"
            findings.append(f"{event['key']} · location \"{event['location']}\" [{where}] "
                            f"· absent from its {len(lines)} cited line(s)")
    return "rows whose title or place is absent from their evidence", len(findings), findings[:limit]


def audit_missed(conn, limit: int) -> tuple[str, int, list[str]]:
    """Plan-shaped lines that name a day and that nothing anywhere cites."""
    findings = []
    # Selecting on `gate_reason` alone made this blind to the largest stream in the
    # store. The gate gives email and GroupMe a verdict per message, but passes iMessage
    # wholesale as `all-of:imessage` — all 5,866 of them — so no iMessage line can ever
    # match a planning reason and none could ever be reported missed. That produced a
    # confident "0% miss rate in large bundles", which was only true because large
    # bundles are the iMessage ones. `bundle.importance` documents this exact asymmetry;
    # any metric keyed on the gate verdict inherits it.
    rows = conn.execute("""
        SELECT a.id, a.ts, a.person, a.handle, a.stream, a.text, a.gate_reason
        FROM archive a
        JOIN spool s ON s.archive_id = a.id
        WHERE a.id NOT IN (SELECT archive_id FROM evidence)
          AND a.stream <> 'agent'
        ORDER BY a.ts DESC
    """).fetchall()
    for row in rows:
        if str(row["gate_reason"] or "") not in PLANNING and not _PLANLIKE_RE.search(
                str(row["text"])[:400]):
            continue
        text = str(row["text"])[:400]
        said_on = None
        try:
            said_on = db.parse_ts(str(row["ts"]))
        except ValueError:
            pass
        if not any(dates.resolve(p, said_on) for p in dates.claims(text)):
            continue
        who = row["person"] or row["handle"] or row["stream"]
        findings.append(f"{row['id']} · {str(row['ts'])[:10]} · {who} · "
                        + " ".join(text.split())[:110])
    return "plan-shaped dated lines that no row cites", len(findings), findings[:limit]


def audit_settled(conn, limit: int) -> tuple[str, int, list[str]]:
    """Open to-dos the archive has already answered.

    The general form of M21: "Pay Nadia $50" stayed open while a Venmo receipt saying
    "You paid Nadia Zimmermann $50.00" sat unread in the same store. Rather than
    special-casing payments, this looks for any open to-do whose distinctive words later
    co-occur with settlement language.
    """
    findings = []
    todos = conn.execute("SELECT key, text, opened_at FROM todos "
                         "WHERE status = 'open'").fetchall()
    for todo in todos:
        words = [w for w in _tokens(str(todo["text"])) if len(w) > 3]
        if not words:
            continue
        clauses = " OR ".join("lower(text) LIKE ?" for _ in words)
        rows = conn.execute(
            f"SELECT id, ts, substr(text,1,90) t FROM archive WHERE ({clauses}) "
            "ORDER BY ts DESC LIMIT 25",
            [f"%{w}%" for w in words]).fetchall()
        for row in rows:
            if SETTLED_RE.search(str(row["t"])):
                findings.append(f"{todo['key']} · open · but archive {row['id']} "
                                f"({str(row['ts'])[:10]}) says: "
                                + " ".join(str(row["t"]).split())[:80])
                break
    return "open to-dos the archive appears to have settled", len(findings), findings[:limit]


def audit_thin(conn, limit: int) -> tuple[str, int, list[str]]:
    """Find rows with either too little evidence or bundle-wide citations."""
    counts = conn.execute("""
        SELECT e.key, count(v.id) n FROM events e
        LEFT JOIN evidence v ON v.kind='event' AND v.ref = e.key
        WHERE e.written_by LIKE 'dream:%'
        GROUP BY e.key ORDER BY n DESC
    """).fetchall()
    fat = [f"{r['key']} · {r['n']} lines" for r in counts if r["n"] > 40]
    thin = [f"{r['key']} · {r['n']} line" for r in counts if r["n"] == 1]
    findings = fat + thin
    return (f"rows on suspicious evidence ({len(fat)} bundle-wide, {len(thin)} single-line)",
            len(findings), findings[:limit])


#: Language that tells you what to *do* at an event, as against what the event is.
#: Deliberately narrow — this is the small set of things that are worth money or
#: embarrassment to forget at the door, not everything an email says.
#: Unambiguous wherever it appears. Nobody says "we will check you in at the door" in
#: casual conversation.
_STRONG_RE = re.compile(
    r"\b(check(?:ed|ing)? you in|check[- ]in|checking in|doors? (?:will )?open|"
    r"will call|first come|first[- ]served|seats? are not assigned|"
    r"rsvp by|reply by|respond by|register by|dress code|black tie|"
    r"bring (?:photo )?id|no (?:ticket|entry|refund)s?\b|gate opens?)\b",
    re.IGNORECASE)

#: The same words that mean "instruction" from an organiser mean nothing in chat:
#: "show your team what champion you intend to use", "It's saying I arrive at 1", "a guy
#: in my parking garage" were all reported as dropped arrival instructions on the first
#: run of this. Counted only when the line is an email, which is where an organiser
#: writes and where the actual misses have all been.
_WEAK_RE = re.compile(
    r"\b(parking|arrive (?:by|before|at)|get there by|present your|show your|"
    r"pick ?up (?:at|from)|(?:please )?bring (?:your|a|an|the))\b", re.IGNORECASE)

_SENTENCE_RE = re.compile(r"[^.!?\n]{10,240}[.!?]")

#: Plan-shaped language, for the streams the gate passes wholesale rather than judging
#: line by line. Paired with a resolvable date in `audit_missed`, never alone — "wanna"
#: on its own is half the corpus.
_PLANLIKE_RE = re.compile(
    r"\b(wanna|want to|let'?s|are you free|you free|u free|can you|could you|"
    r"i'?ll be|we'?re going|going to|come over|coming over|meet(?:ing)? (?:up|at)|"
    r"see you|plan(?:s|ning)? to|down to|are we|should we|you in|rsvp|"
    r"what time|when (?:is|are|do|does|should))\b", re.IGNORECASE)


def audit_details(conn, limit: int) -> tuple[str, int, list[str]]:
    """Arrival instructions that were in the evidence and did not reach the row."""
    findings = []
    events = conn.execute(
        "SELECT key, title, note, location, time FROM events "
        "WHERE written_by LIKE 'dream:%'").fetchall()
    cited = _evidence(conn, "event")
    for event in events:
        lines = cited.get(str(event["key"]), [])
        if not lines:
            continue
        carried = _tokens(" ".join(str(event[f] or "")
                                   for f in ("title", "note", "location", "time")))
        dropped: list[str] = []
        for line in lines:
            is_email = str(line["stream"] or "") == "email"
            for sentence in _SENTENCE_RE.findall(str(line["text"])[:4000]):
                strong = _STRONG_RE.search(sentence)
                if not strong and not (is_email and _WEAK_RE.search(sentence)):
                    continue
                said = _tokens(sentence)
                # Already recorded if the row repeats most of what the sentence says.
                if said and len(said & carried) / len(said) > 0.4:
                    continue
                dropped.append(" ".join(sentence.split())[:120])
        # Every distinct instruction, not just the first. Breaking after one meant the
        # Riders row reported "Doors will open at 6pm" — which it had already recorded —
        # and never mentioned the check-in rule sitting two sentences later, which was
        # the one that mattered.
        for sentence in dict.fromkeys(dropped):
            findings.append(f"{event['key']} · dropped: {sentence}")
    return ("rows whose evidence carries arrival instructions the row does not",
            len(findings), findings[:limit])


AUDITS = [
    ("details", audit_details),
    ("dates", audit_dates),
    ("guests", audit_guests),
    ("grounding", audit_grounding),
    ("evidence", audit_thin),
    ("settled", audit_settled),
    ("missed", audit_missed),
]


def run(home: Path, only: str, limit: int) -> int:
    conn = sqlite3.connect(home / "memcal.db")
    conn.row_factory = sqlite3.Row
    print(f"\n=== {home}")
    total = 0
    for name, fn in AUDITS:
        if only and only != name:
            continue
        try:
            heading, count, samples = fn(conn, limit)
        except Exception as exc:
            print(f"\n  {name.upper():<10} audit raised {type(exc).__name__}: {exc}")
            continue
        total += count
        print(f"\n  {name.upper():<10} {count:>5}  {heading}")
        for sample in samples:
            print(f"             · {sample}")
        if count > len(samples):
            print(f"             … {count - len(samples)} more (--limit to see them)")
    conn.close()
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("homes", nargs="+", type=Path)
    ap.add_argument("--audit", default="", help="run only this one")
    ap.add_argument("--limit", type=int, default=8, help="samples printed per audit")
    args = ap.parse_args()
    for home in args.homes:
        run(home, args.audit, args.limit)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
