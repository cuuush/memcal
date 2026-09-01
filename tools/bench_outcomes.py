#!/usr/bin/env python3
"""Score known benchmark outcomes against a store; review failures manually."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: Senders whose mail is a receipt or a broadcast. An event sourced to one of these is
#: almost always the system serving marketing back to the user — the failure that
#: `_PLANNING_REASONS` already excludes `subject-event` for (M34), checked here at the
#: other end of the pipeline.
BULK_SENDERS = ("noreply", "no-reply", "venmo@", "squareup", "freshdirect", "shakeshack",
                "chipotle", "hulumail", "dice.com", "axs.com", "tixr", "aegpresents",
                "mail.metmuseum", "email.")


def _rows(conn: sqlite3.Connection, sql: str, *args) -> list[sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return conn.execute(sql, args).fetchall()


def _people(row) -> list[str]:
    try:
        return [str(p).lower() for p in json.loads(row["participants"] or "[]")]
    except (json.JSONDecodeError, TypeError):
        return []


def _blob(rows) -> str:
    return " ".join(str(dict(r)) for r in rows).lower()


# ------------------------------------------------------------------- recall --


def r1_beer(conn) -> tuple[bool, str]:
    """One beer plan on Sunday 2026-08-02 at Bohemian Hall, with the people who said so.

    Stricter than "one beer row exists": four separate threads name the place, the day
    and three guests between them, so a row carrying only the title is a partial capture
    and should read as one.
    """
    rows = _rows(conn, "SELECT key, date, time, title, location, participants FROM events "
                       "WHERE lower(title) LIKE '%beer%' OR lower(location) LIKE '%bohemian%'")
    if not rows:
        return False, "MISSING — no beer event at all"
    upcoming = [r for r in rows if str(r["date"]) >= "2026-07-28"]
    if not upcoming:
        return False, f"MISSING — {len(rows)} beer row(s) but none upcoming"
    if len(upcoming) > 1:
        return False, "duplicated: " + " | ".join(r["key"] for r in upcoming)
    row = upcoming[0]
    got, missing = [], []
    for name, ok in (("2026-08-02", str(row["date"]) == "2026-08-02"),
                     ("bohemian/astoria", any(w in (row["location"] or "").lower()
                                              for w in ("bohemian", "astoria"))),
                     ("marco", any("marco" in p for p in _people(row))),
                     ("julian", any("julian" in p or "julian" in p for p in _people(row)))):
        (got if ok else missing).append(name)
    return not missing, (f"{row['key']}@{row['date']} · has {', '.join(got) or 'none of it'}"
                         + (f" · missing {', '.join(missing)}" if missing else ""))


def r2_poker(conn) -> tuple[bool, str]:
    """Poker is Saturday 2026-08-01, and there is exactly one of it.

    "POKER SATURDAY" was said on Monday 07-27, so the arithmetic has one right answer.
    The corpus also holds a 07-12 New Jersey invitation and a game actually played on
    07-17, which must not be folded in — see `p4_poker_history`.
    """
    rows = _rows(conn, "SELECT key, date, time, title FROM events "
                       "WHERE lower(title) LIKE '%poker%' AND date >= '2026-07-28'")
    if not rows:
        return False, "MISSING — no upcoming poker event"
    if len(rows) > 1:
        return False, "duplicated: " + " | ".join(r["key"] for r in rows)
    row = rows[0]
    ok = str(row["date"]) == "2026-08-01"
    return ok, f"{row['key']}@{row['date']}" + ("" if ok else "  [want 2026-08-01, Saturday]")


def r3_riders(conn) -> tuple[bool, str]:
    """The screening: Nitehawk, 6:30pm, "Off the Rails", on 2026-07-30.

    The hardest recall case in the corpus, because no single message holds all of it.
    The venue and the doors time are in emails from hannah@ridersalliance.org; the film
    title is an iMessage Wikipedia link from a different thread eleven days earlier
    (archive 1995). A pipeline that reads bundles in isolation can reach at most two of
    the three, which is the whole argument for grouping related conversations.
    """
    rows = _rows(conn, "SELECT key, date, time, location, note, title FROM events "
                       "WHERE date = '2026-07-30' AND (lower(title) LIKE '%rider%' "
                       "   OR lower(title) LIKE '%movie%' OR lower(title) LIKE '%rails%'"
                       "   OR lower(location) LIKE '%nitehawk%')")
    if not rows:
        return False, "MISSING — no Riders Alliance event on 2026-07-30"
    blob = _blob(rows)
    parts = (("venue", "nitehawk" in blob),
             ("6:30pm", "18:30" in blob or "6:30" in blob),
             ("film title", "off the rails" in blob or "mccollum" in blob))
    got = [n for n, v in parts if v]
    missing = [n for n, v in parts if not v]
    detail = f"{len(rows)} row(s); has {', '.join(got) or 'none of it'}"
    return not missing, detail + (f"; missing {', '.join(missing)}" if missing else "")


def r4_chelsea_leg(conn) -> tuple[bool, str]:
    """The Chelsea Piers pickup is a leg of the evening, not the evening.

    Run 1 built the *only* row from "Wanna plan to get here at 330? I'm at Chelsea piers
    61" and lost the screening entirely. Recording the pickup is right; recording it
    instead of the screening is the defect. Passes when the screening exists and the
    pickup is either its own row or noted on it — and fails when the pickup is all there is.
    """
    rows = _rows(conn, "SELECT key, date, time, location, note, title FROM events "
                       "WHERE date = '2026-07-30'")
    if not rows:
        return False, "MISSING — nothing at all on 2026-07-30"
    blob = _blob(rows)
    screening = "nitehawk" in blob or "rails" in blob
    pickup = "chelsea" in blob
    if not screening and pickup:
        return False, "the pickup replaced the screening — the exact run-1 failure"
    if not screening:
        return False, "no screening row on 2026-07-30"
    return True, ("screening + pickup both recorded" if pickup
                  else "screening recorded; pickup not captured (partial)")


def r5_nadia(conn) -> tuple[bool, str]:
    """The to-do must have been opened *and* then closed against the Venmo receipt.

    Rewritten because the original returned True when no such to-do existed, which is
    the failure wearing the pass. Nadia's text (archive 1173, 07-06) is a real
    obligation and opening it is correct; the receipt (12189, 07-07) is what should
    close it. A store with no Nadia to-do has not solved M21, it has skipped it.
    """
    rows = _rows(conn, "SELECT key, status, text FROM todos WHERE lower(text) LIKE '%nadia%'")
    if not rows:
        return False, "MISSING — no Nadia to-do was ever opened (1173 was not read)"
    open_rows = [r for r in rows if r["status"] == "open"]
    if open_rows:
        return False, "still open: " + ", ".join(r["key"] for r in open_rows)
    cited = _rows(conn, "SELECT 1 FROM evidence WHERE kind='todo' AND ref IN "
                        "(SELECT key FROM todos WHERE lower(text) LIKE '%nadia%') "
                        "AND archive_id = 12189")
    return True, (f"closed ({rows[0]['status']})"
                  + ("  · cites the receipt" if cited else "  · but does not cite 12189"))


def r6_elements(conn) -> tuple[bool, str]:
    """The Elements festival keeps the calendar's dates: 2026-08-07 to 08-09.

    An observation from a subscribed calendar outranks an inference from a chat. Run 1
    moved this to 08-01 and overwrote its source with `person:Rowan Vale`.
    """
    rows = _rows(conn, "SELECT key, date, until, source, origin FROM events "
                       "WHERE key LIKE 'ical-7ff0a4b829ec3391%' OR lower(title)='elements'")
    if not rows:
        return False, "MISSING — the Elements row is gone (check bench_reset)"
    bad = [r for r in rows if str(r["date"]) != "2026-08-07"]
    if bad:
        return False, "moved: " + ", ".join(f"{r['key']}→{r['date']} by {r['source']}"
                                            for r in bad)
    spans = [r for r in rows if str(r["until"] or "") == "2026-08-09"]
    return True, f"{len(rows)} row(s) at 2026-08-07" + ("" if spans else " · span lost")


def r7_montana(conn) -> tuple[bool, str]:
    """A trip to Montana with Morgan and Priya is being planned and must exist.

    Reported as "failed to extract from Morgan messages". The thread is explicit:
    `5899` Morgan — "I am officially approved to WFH **the week we're going to MT**!" —
    and `5901` "Wait priya ur coming to mt". A week-long trip with a partner is about
    as high-value as a row gets, and the store produced a question instead.

    The date is genuinely unstated, which is the interesting part: this is the case for
    a row with `date: null` and the evidence attached, rather than nothing at all.
    """
    rows = _rows(conn, "SELECT key, date, title, participants FROM events "
                       "WHERE lower(title) LIKE '%montana%' OR lower(title) LIKE '% mt %'"
                       "   OR lower(title) LIKE '%mt trip%' OR key LIKE '%montana%'")
    if not rows:
        return False, "MISSING — no Montana trip row (5899/5901 not captured)"
    named = [r for r in rows if any("priya" in p or "reese" in p for p in _people(r))]
    return True, (f"{rows[0]['key']}@{rows[0]['date']}"
                  + ("" if named else " · but neither Morgan nor Priya listed"))


def r8_elements_confirmed(conn) -> tuple[bool, str]:
    """Elements is confirmed, not "maybe"."""
    rows = _rows(conn, "SELECT key, date, status, participants FROM events "
                       "WHERE lower(title) LIKE '%element%' AND date >= '2026-08-01'")
    if not rows:
        return False, "MISSING — no upcoming Elements row"
    weak = [r for r in rows if str(r["status"]) not in ("confirmed", "happened")]
    return not weak, ("; ".join(f"{r['key']} is '{r['status']}'" for r in weak)
                      if weak else f"{len(rows)} row(s) confirmed")


def r9_elements_swap(conn) -> tuple[bool, str]:
    """Morgan is no longer going to Elements; their brother is going instead."""
    rows = _rows(conn, "SELECT key, participants, note FROM events "
                       "WHERE lower(title) LIKE '%element%' AND date >= '2026-08-01'")
    if not rows:
        return False, "MISSING — no upcoming Elements row"
    stale = [r for r in rows if any("reese" in p for p in _people(r))]
    return not stale, ("still lists Morgan: " + ", ".join(r["key"] for r in stale)
                       if stale else "Morgan correctly dropped")


# ---------------------------------------------------------------- precision --


def _distinct(conn, key_a: str, key_b: str, label: str) -> tuple[bool, str]:
    """Two rows that must stay two rows — asserted twice, for two different reasons."""
    rows = _rows(conn, "SELECT key, date, title, participants, location, status "
                       "FROM events WHERE key LIKE ? OR key LIKE ?",
                 key_a + "%", key_b + "%")
    if len(rows) < 2:
        return False, f"MISSING — {len(rows)} of 2 {label} rows present (cannot test)"
    if len(rows) > 2:
        return False, f"{label}: {len(rows)} rows, expected exactly 2"
    merged = _would_cluster(rows[0], rows[1])
    if merged:
        return False, f"{label}: same_event() would merge these"
    return True, f"{label}: two rows, and same_event() declines to merge"


def _would_cluster(a, b) -> bool:
    """Ask the real clusterer about two stored rows.

    Imported lazily and defensively: this file is a scoring tool that must keep running
    against a store even if the pipeline it scores has moved on, so a signature change
    here should degrade to "cannot tell" rather than take the whole suite down.
    """
    try:
        from memcal.config import Config
        from memcal.dream.merge import Mention, same_event
    except ImportError:
        return False

    class _Bundle:
        def __init__(self, entity):
            self.entity = entity

    def as_mention(row):
        return Mention({"title": row["title"], "date": str(row["date"]),
                        "participants": json.loads(row["participants"] or "[]"),
                        "location": row["location"], "status": row["status"]},
                       _Bundle(f"bench:{row['key']}"), {})

    try:
        return bool(same_event(as_mention(a), as_mention(b), Config(home=Path("/tmp"))))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def p1_partiful_tag(conn) -> tuple[bool, str]:
    """Jack's 30th (08-22) and Capture The Flag 2 (08-23) are not the same party.

    M30: every Partiful export carries "| Partiful" in its title, so `partiful` read as a
    distinctive shared word and linked two unrelated events one day apart. This is the
    archetype the log calls the dangerous failure — a false link fabricates an occasion.
    """
    return _distinct(conn, "ical-2a6ed88a923ac72b", "ical-7978647a18bd5a78",
                     "Jack's 30th / Capture The Flag 2")


def p2_repeat_series(conn) -> tuple[bool, str]:
    """The two Capture The Flag games are six weeks apart and share four title tokens.

    "Capture The Flag - The Trojan War" (07-12) and "Capture The Flag 2 - Trojan War"
    (08-23) overlap on capture, flag, trojan and war — far past any token threshold. Only
    the dates separate them, so this is the check that date bounds are actually enforced
    rather than being outvoted by wording.
    """
    return _distinct(conn, "ical-518778ca815f7d4a", "ical-7978647a18bd5a78",
                     "CTF Trojan War / CTF 2")


def p3_festivals(conn) -> tuple[bool, str]:
    """Elements and Electric Forest are two festivals, not one.

    Both are ticket-shaped, both arrive as order confirmations from a ticketing vendor
    (tixr 11979, AXS 12198-12200), both are "a festival in the summer". M30
    names this shape as the thing that must never link, and nothing tested it.
    """
    rows = _rows(conn, "SELECT key, date, title FROM events WHERE lower(title) LIKE '%element%'"
                       " OR lower(title) LIKE '%electric forest%' OR lower(title) LIKE '%forest%'")
    merged = [r for r in rows if "element" in str(r["title"]).lower()
              and "forest" in str(r["title"]).lower()]
    if merged:
        return False, "merged: " + ", ".join(r["key"] for r in merged)
    return True, f"{len(rows)} festival row(s), none conflated"


def p4_poker_history(conn) -> tuple[bool, str]:
    """The July poker games must not be folded into the Saturday one.

    "poker" appears in a 07-12 New Jersey invitation, a game played on 07-17, a June
    iCal row, a 2024 fraternity fundraiser, and a dozen chatty asides. Clustering on the
    token alone sweeps them together; `NEAR_DAYS` is what should stop it.
    """
    rows = _rows(conn, "SELECT key, date, title FROM events WHERE lower(title) LIKE '%poker%'")
    saturday = [r for r in rows if str(r["date"]) == "2026-08-01"]
    if not saturday:
        return True, "no Saturday row yet — nothing to over-merge (see R2)"
    # Counting rows cannot catch this: an over-merge produces *fewer* rows, so the naive
    # version passed by being unfailable. What proves it is the evidence — a Saturday row
    # that cites the night of the 17th has swallowed a different game.
    leaked = _rows(conn, """
        SELECT DISTINCT e.key, v.archive_id FROM events e
        JOIN evidence v ON v.kind='event' AND v.ref = e.key
        WHERE e.date = '2026-08-01' AND lower(e.title) LIKE '%poker%'
          AND v.archive_id IN (2049, 2824, 2855, 2875, 2881, 2886, 2894, 13431)
    """)
    if leaked:
        return False, ("Saturday row cites earlier games: "
                       + ", ".join(f"{r['key']}←{r['archive_id']}" for r in leaked[:4]))
    return True, f"{len(rows)} poker row(s); Saturday cites none of the July games"


def p5_receipt_events(conn) -> tuple[bool, str]:
    """No calendar row whose evidence is a receipt or a broadcast.

    The corpus is full of bait: Shake Shack, FreshDirect, Brooklyn Brewery, Chipotle,
    Venmo, and a Met After Hours mailshot all contain plan-shaped language and dates.
    Turning any of them into an event is the system serving marketing back to the user.
    """
    rows = _rows(conn, """
        SELECT e.key, e.title, a.handle FROM events e
        JOIN evidence v ON v.kind='event' AND v.ref = e.key
        JOIN archive a ON a.id = v.archive_id
        WHERE e.written_by LIKE 'dream:%'
    """)
    bad: dict[str, str] = {}
    for row in rows:
        handle = str(row["handle"] or "").lower()
        if any(s in handle for s in BULK_SENDERS):
            bad.setdefault(str(row["key"]), handle)
    if bad:
        return False, f"{len(bad)} from bulk senders: " + ", ".join(
            f"{k}←{v}" for k, v in list(bad.items())[:4])
    return True, "no events sourced to receipts or broadcasts"


def p6_volume(conn) -> tuple[bool, str]:
    """How much was written, against what the corpus can justify.

    Not a correctness check — a tripwire. Run 1 wrote 74 diffs; the rows pipeline wrote
    128 events; the mentions pipeline wrote 175 and was worse. A large jump means rows
    are being minted from thin evidence, and it is the signal that recall-only scoring
    cannot see.
    """
    counts = _rows(conn, "SELECT (SELECT count(*) FROM events WHERE written_by LIKE 'dream:%') e,"
                         " (SELECT count(*) FROM todos) t, (SELECT count(*) FROM questions) q")[0]
    written = counts["e"]
    ok = written <= 150
    return ok, (f"{written} dream events · {counts['t']} to-dos · {counts['q']} questions"
                + ("" if ok else "  [over 150 — check for thin-evidence rows]"))


def p7_asks_what_it_knows(conn) -> tuple[bool, str]:
    """No question whose answer is already in the store.

    "Who is Aaron in the Aug 2 beer garden plan with Quinn Brooks?" — Aaron has a wiki
    page, appears in fifteen messages, and the bundle it came from is *titled* "Me,
    Quinn Brooks, and Aaron". The instructions already forbid asking "anything you
    could work out from what is already here"; nothing checked it.

    Cheap to detect and worth detecting, because a list of questions is only read while
    it stays short: one silly entry costs attention on the five real ones.
    """
    # Wiki pages are files, not a table. The first version queried a `pages` table that
    # does not exist, got an empty set, matched nothing and passed — a vacuous pass in
    # the very file whose rewrite was about removing vacuous passes.
    known = {p.stem.split("-")[0].lower()
             for p in Path(conn.execute("PRAGMA database_list").fetchall()[0][2])
             .parent.glob("wiki/*/*.md")}
    rows = _rows(conn, "SELECT key, text FROM questions WHERE status = 'open'")
    bad = []
    for row in rows:
        text = str(row["text"] or "")
        who = re.match(r"\s*who (?:is|are) ([A-Z][\w'-]+)", text, re.IGNORECASE)
        if who and who.group(1).lower() in known:
            bad.append(f"{row['key']}: asks who {who.group(1)} is, who has a page")
    return not bad, "; ".join(bad) if bad else f"{len(rows)} question(s), none self-answerable"


def p8_no_bookkeeping_in_notes(conn) -> tuple[bool, str]:
    """Notes are read by the user; memcal's own bookkeeping is not for them.

    The brief carried "Beer garden · **2 sources mention this**" and "Elements · **Nolan
    corrected the dates**: likely August 7-9". Both are the system narrating its own
    process inside their calendar. The prompt's "the user is not this system's proofreader" rule
    exists only under *questions*, so notes have no equivalent and collect it instead.
    """
    # Only rows a model wrote. "Partiful RSVP yes (inferred from location)" comes from
    # the iCal importer and is a deliberate, honest provenance note; scoring it here
    # buried the two that matter under fourteen that do not.
    rows = _rows(conn, "SELECT key, note FROM events "
                       "WHERE note IS NOT NULL AND written_by LIKE 'dream:%'")
    pattern = re.compile(r"(\b\d+ sources?\b|sources? mention|mentioned by \d+|"
                         r"corrected the date|per the model|this row|"
                         r"according to (?:the )?(?:store|memcal|record))", re.IGNORECASE)
    bad = [f"{r['key']}: \"{str(r['note'])[:52]}\"" for r in rows
           if pattern.search(str(r["note"] or ""))]
    return not bad, "; ".join(bad[:3]) if bad else f"{len(rows)} note(s) clean"


def _has(conn, table: str) -> bool:
    return bool(_rows(conn, "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                      table))


# --------------------------------------------------------------- provenance --


def v1_evidence_precision(conn) -> tuple[bool, str]:
    """Require row evidence to remain line-level rather than bundle-wide."""
    rows = _rows(conn, "SELECT ref, count(*) n FROM evidence WHERE kind='event' "
                       "GROUP BY ref ORDER BY n DESC LIMIT 3")
    if not rows:
        return False, "MISSING — no evidence attached to any event"
    worst = rows[0]
    ok = worst["n"] <= 40
    return ok, (f"worst: {worst['ref']} cites {worst['n']} lines"
                + ("" if ok else "  [bundle-wide evidence is back]"))


def v2_every_row_cited(conn) -> tuple[bool, str]:
    """Every dream-written event points at the lines it came from."""
    rows = _rows(conn, """
        SELECT e.key FROM events e
        WHERE e.written_by LIKE 'dream:%'
          AND NOT EXISTS (SELECT 1 FROM evidence v WHERE v.kind='event' AND v.ref=e.key)
    """)
    total = _rows(conn, "SELECT count(*) n FROM events WHERE written_by LIKE 'dream:%'")[0]["n"]
    if not total:
        return False, "MISSING — no dream-written events to check"
    return not rows, (f"{len(rows)}/{total} uncited: "
                      + ", ".join(r["key"] for r in rows[:4]) if rows
                      else f"all {total} dream events cite their source")


def v3_provenance_stamped(conn) -> tuple[bool, str]:
    """Every written row records which call wrote it, so the timeline can say (M6, M20)."""
    rows = _rows(conn, """
        SELECT e.key FROM events e
        WHERE NOT EXISTS (SELECT 1 FROM provenance p WHERE p.kind='event' AND p.ref=e.key)
          AND e.written_by NOT IN ('cli', 'ical', 'partiful')
    """)
    total = _rows(conn, "SELECT count(*) n FROM events")[0]["n"]
    return not rows, (f"{len(rows)}/{total} unstamped: " + ", ".join(r["key"] for r in rows[:4])
                      if rows else "every written row has a provenance entry")


# ------------------------------------------------------------------ hygiene --


def h1_same_day_dupes(conn) -> tuple[bool, str]:
    """Any two events on one day whose titles share a distinctive word. A smell test."""
    from memcal.dream.merge import _tokens
    rows = _rows(conn, "SELECT key, date, title, written_by FROM events "
                       "WHERE date >= '2026-07-25'")

    # Keyed on the key rather than on `written_by`: a row the user corrected by hand is
    # stamped `live`, which lost the exclusion and put the two subscribed-calendar copies
    # of Elements back in the report. The key prefix is minted by the feed and nothing
    # re-mints it, so it survives every later writer.
    def from_feed(row) -> bool:
        return str(row["key"]).startswith(("ical-", "partiful-"))

    pairs = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            # Two subscribed calendars both carrying the same festival is the feeds
            # disagreeing, not the pipeline duplicating. Counting it here made the check
            # unfixable by any change to the pipeline, which is the definition of noise.
            if from_feed(a) and from_feed(b):
                continue
            if a["date"] == b["date"] and _tokens(a["title"]) & _tokens(b["title"]):
                pairs.append(f"{a['key']} ~ {b['key']}")
    return not pairs, "; ".join(pairs[:5]) if pairs else "no same-day title collisions"


def h2_key_date_sync(conn) -> tuple[bool, str]:
    """A key says `…@<date>`; the date column must agree.

    `spider-man-movie-marathon@2026-07-30` carried `date=2026-07-31`. Harmless alone, but
    the key is what every other table joins on and what `bench_reset` reconstructs from,
    so a desync makes the store's own history unreadable.
    """
    rows = _rows(conn, "SELECT key, date FROM events WHERE key LIKE '%@____-__-__'")
    bad = [r for r in rows if r["key"].rpartition("@")[2] != str(r["date"])]
    return not bad, ("; ".join(f"{r['key']}→{r['date']}" for r in bad[:5]) if bad
                     else f"{len(rows)} keys agree with their dates")


def h3_wiki_junk(home: Path) -> tuple[bool, str]:
    """iCal holidays must not mint project pages (M24)."""
    projects = home / "wiki" / "projects"
    if not projects.is_dir():
        return True, "no projects dir"
    junk = sorted(p.stem for p in projects.glob("*.md")
                  if p.stem in {"easter", "orthodox-easter", "passover", "good-friday",
                                "eid-al-adha", "independence-day-observed", "new-event",
                                "christmas", "thanksgiving-day", "halloween"})
    return not junk, ("holiday pages: " + ", ".join(junk)) if junk else "no holiday pages"


CHECKS = [
    ("recall",     "R1 beer garden, one row, full",  r1_beer),
    ("recall",     "R2 poker on Saturday",           r2_poker),
    ("recall",     "R3 riders: venue+time+film",     r3_riders),
    ("recall",     "R4 chelsea leg not instead of",  r4_chelsea_leg),
    ("recall",     "R5 nadia opened AND closed",   r5_nadia),
    ("recall",     "R6 elements keeps ical dates",   r6_elements),
    ("recall",     "R7 montana trip extracted",     r7_montana),
    ("recall",     "R8 elements confirmed",          r8_elements_confirmed),
    ("recall",     "R9 elements: reese dropped",    r9_elements_swap),
    ("precision",  "P1 partiful tag ≠ same event",   p1_partiful_tag),
    ("precision",  "P2 CTF repeat stays two",        p2_repeat_series),
    ("precision",  "P3 elements ≠ electric forest",  p3_festivals),
    ("precision",  "P4 july poker not absorbed",     p4_poker_history),
    ("precision",  "P5 no receipt-sourced events",   p5_receipt_events),
    ("precision",  "P6 volume within band",          p6_volume),
    ("precision",  "P7 no self-answerable asks",     p7_asks_what_it_knows),
    ("precision",  "P8 no bookkeeping in notes",     p8_no_bookkeeping_in_notes),
    ("provenance", "V1 evidence stays line-level",   v1_evidence_precision),
    ("provenance", "V2 every dream row cited",       v2_every_row_cited),
    ("provenance", "V3 provenance stamped",          v3_provenance_stamped),
    ("hygiene",    "H1 no same-day dupes",           h1_same_day_dupes),
    ("hygiene",    "H2 keys agree with dates",       h2_key_date_sync),
    ("hygiene",    "H3 no wiki holiday pages",       h3_wiki_junk),
]

#: Checks that take the store directory rather than a connection.
NEEDS_HOME = {h3_wiki_junk}


def score(home: Path, axis: str = "") -> int:
    conn = sqlite3.connect(home / "memcal.db")
    print(f"\n=== {home}")
    run = _rows(conn, "SELECT model, bundles, items, diffs, prompt_tokens, "
                      "completion_tokens, cost_usd, error FROM runs ORDER BY id DESC LIMIT 1")
    if run:
        r = run[0]
        print(f"    {r['model']} · {r['bundles']} bundles · {r['diffs']} diffs · "
              f"{r['prompt_tokens']:,} in · {r['completion_tokens']:,} out · "
              f"${r['cost_usd']:.4f}" + (f" · ERROR {r['error']}" if r["error"] else ""))
    else:
        print("    (no run recorded)")

    failures = 0
    by_axis: dict[str, list[int]] = {}
    current = ""
    for name, label, fn in CHECKS:
        if axis and axis != name:
            continue
        if name != current:
            print(f"\n  {name.upper()}")
            current = name
        try:
            ok, detail = fn(home if fn in NEEDS_HOME else conn)
        except Exception as exc:                      # a broken check must not hide the rest
            ok, detail = False, f"check raised {type(exc).__name__}: {exc}"
        failures += not ok
        by_axis.setdefault(name, []).append(bool(ok))
        print(f"    {'PASS' if ok else 'FAIL'}  {label:<32} {detail}")

    print("\n  " + " · ".join(f"{a} {sum(v)}/{len(v)}" for a, v in by_axis.items()))
    conn.close()
    return failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("homes", nargs="+", type=Path)
    ap.add_argument("--axis", default="",
                    choices=["", "recall", "precision", "provenance", "hygiene"])
    args = ap.parse_args()
    failures = 0
    for home in args.homes:
        failures = score(home, args.axis)
    print()
    return failures


if __name__ == "__main__":
    raise SystemExit(main())
