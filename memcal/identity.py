"""Handle resolution and the sender gate table.

Both are dictionary lookups. No model call ever touches this path: phone number to
person, address to keep-or-ignore. Five minutes at setup, then a trickle. The one call
that does reason about identity lives in `whois.py`, is asked for explicitly, and writes
back through `link` here like any other source.
"""

from __future__ import annotations

import glob
import os
import re
import sqlite3
import subprocess
import unicodedata
from pathlib import Path

from . import db

CONTACTS_GLOBS = (
    "~/Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb",
    "~/Library/Application Support/AddressBook/AddressBook-v22.abcddb",
)


def normalize(handle: str) -> str:
    """Phone numbers to E.164-ish, emails lowercased, opaque ids left alone."""
    h = (handle or "").strip()
    if not h:
        return ""
    if "@" in h:
        return h.lower()
    if ":" in h:  # groupme:1234, discord:5678
        return h.lower()
    digits = re.sub(r"[^\d+]", "", h)
    if digits.startswith("+"):
        return digits
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    return digits or h.lower()


def resolve(conn: sqlite3.Connection, handle: str) -> str | None:
    row = conn.execute("SELECT person FROM handles WHERE handle = ?", (normalize(handle),)).fetchone()
    return row["person"] if row else None


# `handles.source` identifies the evidence behind a link, not its writer. A write
# lands only when it is at least as strong as the existing link.

#: Evidence rank by source suffix, weakest first. Unlisted sources are judgements
#: and outrank every scan, so a newly added scan cannot overwrite Contacts.
EVIDENCE = {
    "roster": 10,           # the platform's per-conversation nickname
    "platform-roster": 10,  # the same thing, reached by the backfill
    "profile": 20,          # the platform's account name; beats a per-chat nickname
    "model": 25,            # `whois.resolve` read the whole board and concluded this
    "contact-match": 30,    # this display name is exactly somebody in Contacts
    "contacts": 40,         # Contacts itself
}
JUDGEMENT = 100


def authority(source: str | None) -> int:
    """Evidence rank for a link written by `source`.

    Sources are `{stream}:{evidence}`, so the suffix decides; a bare string is
    tried whole first.
    """
    key = (source or "").strip().lower()
    if key in EVIDENCE:
        return EVIDENCE[key]
    return EVIDENCE.get(key.rsplit(":", 1)[-1], JUDGEMENT)


def link(conn: sqlite3.Connection, handle: str, person: str, source: str = "cli") -> bool:
    """Point a handle at a person. Returns whether the write landed.

    Stronger evidence wins; equal evidence may overwrite so a scan can revise
    its own guess.
    """
    h = normalize(handle)
    current = conn.execute("SELECT person, source FROM handles WHERE handle = ?",
                           (h,)).fetchone()
    allowed = current is None or authority(source) >= authority(current["source"])
    if allowed:
        conn.execute(
            "INSERT INTO handles(handle, person, source, updated_at) VALUES(?,?,?,?)"
            " ON CONFLICT(handle) DO UPDATE SET person = excluded.person,"
            " source = excluded.source, updated_at = excluded.updated_at",
            (h, person, source, db.now()),
        )
    # Either way this handle now has a person, so it does not belong in the queue.
    conn.execute("DELETE FROM unresolved WHERE handle = ?", (h,))
    conn.commit()
    return allowed


def spelling_in_use(conn: sqlite3.Connection, name: str | None) -> str | None:
    """The spelling this name already goes by, if memcal has met it before.

    `person` is the bundle key; see the entity keys in `gate.bundle_entity`.
    """
    clean = clean_name(name)
    if not name_shaped(clean):
        return None
    row = conn.execute(
        "SELECT person FROM handles WHERE lower(person) = ? LIMIT 1", (clean.lower(),)
    ).fetchone()
    return row["person"] if row else None


def link_by_name(conn: sqlite3.Connection, handle: str, seen_name: str | None,
                 source: str) -> str | None:
    """Auto-link an opaque platform id when its display name is exactly a known contact."""
    person = spelling_in_use(conn, seen_name)
    if not person:
        return None
    stream = (source or "").split(":", 1)[0] or "cli"
    if link(conn, handle, person, source=f"{stream}:contact-match"):
        return person
    return resolve(conn, handle)      # something better already answers for this id


#: A user id that is not a person: platform notices and bots.
NON_PERSON_HANDLES = frozenset({"groupme:system"})

#: A handle namespace that holds no people.
NON_PERSON_PREFIXES = ("whatsapp:bot:",)
NON_PERSON_NAMES = frozenset({"groupme", "copilot", "system", "bot", "notifications",
                              "whatsapp"})


def clean_name(value: str | None) -> str:
    """A display name with the platform's invisible packaging taken off."""
    stripped = "".join(ch for ch in str(value or "")
                       if unicodedata.category(ch) != "Cf")
    return " ".join(stripped.split())

#: A name that announces itself as a bot. Deliberately tight: a wrong filter here
#: silently unnames a real person, so the greedy failure costs more.
BOTTISH = re.compile(r"\bBot\b|[a-z]Bot$")


def is_person(handle: str, seen_name: str | None = None) -> bool:
    """Is there a human behind this id at all? Asked before queueing."""
    name = clean_name(seen_name)
    handle = normalize(handle)
    if handle in NON_PERSON_HANDLES or handle.startswith(NON_PERSON_PREFIXES):
        return False
    if name.lower() in NON_PERSON_NAMES:
        return False
    return not BOTTISH.search(name)


#: A display name that is not a name at all. Matched by punctuation no name carries,
#: not by "looks base64".
ENCODED_BLOB = re.compile(r"^[+/=]|=")


#: Scripts where one character can be a complete name.
WHOLE_WORD_SCRIPTS = ("CJK", "HANGUL", "HIRAGANA", "KATAKANA", "YI SYLLABLE")


def _writes_words(ch: str) -> bool:
    try:
        return unicodedata.name(ch).startswith(WHOLE_WORD_SCRIPTS)
    except ValueError:              # unnamed in this interpreter's tables
        return False


def name_shaped(seen_name: str | None) -> bool:
    """Is this string something a person could be called?

    Rejects what is provably not a name rather than recognising what is one;
    a greedy filter silently unnames real people.
    """
    name = clean_name(seen_name)
    if not name or ENCODED_BLOB.search(name):
        return False
    letters = [ch for ch in name if ch.isalpha()]
    if not letters:
        return False
    # Alphabetic names need two letters; whole-word scripts can use one character.
    if any(_writes_words(ch) for ch in letters):
        return True
    return len(letters) >= 2


def adopt_seen_name(conn: sqlite3.Connection, handle: str, seen_name: str | None,
                    source: str) -> str | None:
    """Assign a platform-provided display name without guessing; preserve known spelling."""
    name = clean_name(seen_name)
    if not is_person(handle, name) or not name_shaped(name):
        return None
    canonical = spelling_in_use(conn, name) or name
    if link(conn, handle, canonical, source=source):
        return canonical
    # Refused: better evidence already names this id.
    return resolve(conn, handle)


def collapse_split_spellings(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Fold two casings of one name back into one person."""
    groups = conn.execute(
        "SELECT lower(person) AS lp FROM handles WHERE person IS NOT NULL AND person <> ''"
        " GROUP BY lp HAVING count(DISTINCT person) > 1").fetchall()
    folded: list[tuple[str, str]] = []
    for group in groups:
        rows = conn.execute(
            "SELECT person, source FROM handles WHERE lower(person) = ?",
            (group["lp"],)).fetchall()
        seen: dict[str, int] = {}
        for row in rows:
            best = max(seen.get(row["person"], -1), authority(row["source"]))
            seen[row["person"]] = best
        def rank(person: str) -> tuple[int, int, str]:
            used = conn.execute(
                "SELECT count(*) AS n FROM archive WHERE person = ?", (person,)
            ).fetchone()["n"]
            return (seen[person], used, person)
        keep = max(sorted(seen), key=rank)
        for person in seen:
            if person == keep:
                continue
            conn.execute("UPDATE handles SET person = ? WHERE person = ?", (keep, person))
            conn.execute("UPDATE archive SET person = ? WHERE person = ?", (keep, person))
            folded.append((person, keep))
    if folded:
        conn.commit()
    return folded


def adopt_platform_names(conn: sqlite3.Connection, *, floor: int = 1) -> list[tuple[str, str]]:
    """Link every queued id whose platform already supplied a name. Idempotent."""
    done = []
    for row in conn.execute(
            "SELECT handle, seen_name, count FROM unresolved"
            " WHERE seen_name IS NOT NULL AND seen_name <> '' AND count >= ?"
            " ORDER BY count DESC", (floor,)).fetchall():
        if resolve(conn, row["handle"]):
            continue
        person = adopt_seen_name(conn, row["handle"], row["seen_name"],
                                 source="platform-roster")
        if person:
            done.append((row["handle"], person))
    return done


# ------------------------------------------------ same person, two spellings ----
# Contacts joins platforms only when a phone number or email is available. Opaque IDs
# leave names as the only join, which is unsafe; ambiguous name matches become questions,
# never automatic identity facts.


def _name_stem(shorter: str, longer: str) -> bool:
    """Is `shorter` the opening words of `longer`, whole words only?"""
    a, b = shorter.lower().split(), longer.lower().split()
    return bool(a) and bool(b) and a != b and len(a) < len(b) and b[:len(a)] == a


def _spoken_in(conn: sqlite3.Connection) -> dict[str, set[tuple[str, str]]]:
    """Every conversation each person has actually spoken in, read from the archive."""
    seen: dict[str, set[tuple[str, str]]] = {}
    for row in conn.execute(
            "SELECT DISTINCT person, stream, thread FROM archive"
            "  WHERE person IS NOT NULL AND person <> '' AND from_me = 0"
            "    AND thread IS NOT NULL"):
        seen.setdefault(row["person"], set()).add((row["stream"], row["thread"]))
    return seen


def merge_candidates(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Pairs of names that are plausibly one person, safe enough to ask about."""
    spoken = _spoken_in(conn)
    known = sorted({row["person"] for row in conn.execute(
        "SELECT DISTINCT person FROM handles WHERE person IS NOT NULL AND person <> ''")}
        | set(spoken))
    rivals: dict[str, int] = {}
    for name in known:
        for other in known:
            if _name_stem(name, other) or _name_stem(other, name):
                rivals[name] = rivals.get(name, 0) + 1

    found: list[tuple[str, str]] = []
    for short in known:
        for long_ in known:
            if not _name_stem(short, long_):
                continue
            if rivals.get(short, 0) > 1 or rivals.get(long_, 0) > 1:
                continue
            if not (spoken.get(short) and spoken.get(long_)):
                continue
            if spoken[short] & spoken[long_]:
                continue
            if is_me(conn, short) or is_me(conn, long_):
                continue
            found.append((short, long_))
    return sorted(found)


def candidate_lines(conn: sqlite3.Connection) -> list[str]:
    """`merge_candidates` rendered for a human who came looking."""
    lines = []
    spoken = _spoken_in(conn)
    for short, long_ in merge_candidates(conn):
        where_short = ", ".join(sorted({s for s, _t in spoken[short]}))
        where_long = ", ".join(sorted({s for s, _t in spoken[long_]}))
        lines.append(f"  {short} ({where_short})  ==  {long_} ({where_long})?")
    return lines


def forget_non_people(conn: sqlite3.Connection) -> int:
    """Drop the platform's own announcement channels out of the queue."""
    doomed = [row["handle"] for row in conn.execute(
        "SELECT handle, seen_name FROM unresolved")
        if not is_person(row["handle"], row["seen_name"])
        or settled_non_person(conn, row["handle"])]
    for handle in doomed:
        conn.execute("DELETE FROM unresolved WHERE handle = ?", (handle,))
    if doomed:
        conn.commit()
    return len(doomed)


def settled_non_person(conn: sqlite3.Connection, handle: str) -> bool:
    """Has this handle already been ruled out as a person?"""
    return bool(conn.execute("SELECT 1 FROM non_people WHERE handle = ?",
                             (normalize(handle),)).fetchone())


def non_person_label(conn: sqlite3.Connection, handle: str) -> str | None:
    """What to call a handle with nobody behind it — "Venmo", not the address."""
    row = conn.execute("SELECT label FROM non_people WHERE handle = ?",
                       (normalize(handle),)).fetchone()
    return (row["label"] or None) if row else None


def note_unresolved(conn: sqlite3.Connection, handle: str, stream: str,
                    seen_name: str | None = None, sample: str | None = None) -> None:
    h = normalize(handle)
    if not h or not is_person(h, seen_name) or settled_non_person(conn, h):
        return
    stamp = db.now()
    conn.execute(
        """INSERT INTO unresolved(handle, stream, seen_name, sample, count, first_seen, last_seen)
           VALUES(?,?,?,?,1,?,?)
           ON CONFLICT(handle) DO UPDATE SET count = count + 1, last_seen = excluded.last_seen,
             seen_name = coalesce(excluded.seen_name, seen_name),
             sample = coalesce(sample, excluded.sample)""",
        (h, stream, seen_name, (sample or "")[:200], stamp, stamp),
    )


def forget_bulk_unresolved(conn: sqlite3.Connection) -> int:
    """Drop machines out of the name-this-person queue.

    The address stays in the archive and in the senders table.
    """
    from . import gate                    # gate imports this module, so import late
    doomed = [row["handle"] for row in conn.execute("SELECT handle FROM unresolved")
              if "@" in (row["handle"] or "")
              and (gate.is_automated(row["handle"])
                   or sender_decision(conn, row["handle"]) in ("archive", "ignore"))]
    for handle in doomed:
        conn.execute("DELETE FROM unresolved WHERE handle = ?", (handle,))
    if doomed:
        conn.commit()
    return len(doomed)


def where_seen(conn: sqlite3.Connection, handle: str, limit: int = 2) -> list[str]:
    """The conversations a handle actually speaks in, busiest first."""
    return [str(row["thread"]) for row in conn.execute(
        "SELECT thread, count(*) AS n FROM archive"
        "  WHERE handle = ? AND thread IS NOT NULL AND thread <> ''"
        "  GROUP BY thread ORDER BY n DESC LIMIT ?", (normalize(handle), limit))]


def unresolved(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM unresolved ORDER BY count DESC, last_seen DESC LIMIT ?", (limit,)
    ).fetchall()


def guess_person(conn: sqlite3.Connection, row: sqlite3.Row) -> str | None:
    """Fuzzy guess to pre-fill the CLI prompt: a display name we've already linked."""
    name = (row["seen_name"] or "").strip()
    if not name:
        return None
    known = [r["person"] for r in conn.execute("SELECT DISTINCT person FROM handles").fetchall()]
    first = name.split()[0].lower()
    for person in known:
        if person.lower() == name.lower() or person.lower().startswith(first):
            return person
    return name or None


REFRESH_AFTER_HOURS = 24


def refresh_contacts(conn: sqlite3.Connection, *, force: bool = False) -> tuple[int, str]:
    """Re-read Contacts if it has been a day."""
    last = db.get_meta(conn, "contacts.imported_at", "")
    if last and not force:
        age = (db.parse_ts(db.now()) - db.parse_ts(last)).total_seconds()
        if age < REFRESH_AFTER_HOURS * 3600:
            return 0, "fresh"
    count, message = import_contacts(conn)
    db.set_meta(conn, "contacts.imported_at", db.now())
    return count, message


def import_contacts(conn: sqlite3.Connection) -> tuple[int, str]:
    """Read macOS Contacts once into the handles table. Returns (count, message).

    Needs Full Disk Access for whatever runs it; that failure is reported, not raised.
    """
    paths: list[str] = []
    for pattern in CONTACTS_GLOBS:
        paths.extend(glob.glob(os.path.expanduser(pattern)))
    if not paths:
        return 0, "no AddressBook database found"

    total, errors = 0, []
    for path in paths:
        try:
            src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            src.row_factory = sqlite3.Row
            rows = src.execute(
                """SELECT r.ZFIRSTNAME AS first, r.ZLASTNAME AS last, r.ZORGANIZATION AS org,
                          p.ZFULLNUMBER AS phone, e.ZADDRESS AS email
                   FROM ZABCDRECORD r
                   LEFT JOIN ZABCDPHONENUMBER p ON p.ZOWNER = r.Z_PK
                   LEFT JOIN ZABCDEMAILADDRESS e ON e.ZOWNER = r.Z_PK
                   WHERE p.ZFULLNUMBER IS NOT NULL OR e.ZADDRESS IS NOT NULL"""
            ).fetchall()
        except sqlite3.Error as exc:
            errors.append(f"{Path(path).parent.name}: {exc}")
            continue
        for row in rows:
            name = " ".join(x for x in (row["first"], row["last"]) if x).strip() or (row["org"] or "")
            if not name:
                continue
            for handle in (row["phone"], row["email"]):
                if handle:
                    link(conn, handle, name, source="contacts")
                    total += 1
        src.close()
    if errors and total == 0:
        return 0, ("could not read Contacts (grant Full Disk Access to your terminal): "
                   + "; ".join(errors))
    return total, f"linked {total} handles from {len(paths)} source(s)"


# Who the user is.
def set_me(conn: sqlite3.Connection, *names: str) -> list[str]:
    """Record the names that mean 'the user'. Aliases welcome — people have several."""
    clean = [n.strip() for n in names if n and n.strip()]
    if clean:
        db.set_meta(conn, "identity.me", db.jdump(sorted(set(clean))))
    return me_names(conn)


def me_names(conn: sqlite3.Connection) -> list[str]:
    """Every name that means the user."""
    stored = db.jload(db.get_meta(conn, "identity.me", ""), [])
    if stored:
        return stored

    found: set[str] = set()
    # The macOS account's full name is right far more often than not.
    try:
        full = subprocess.run(["id", "-F"], capture_output=True, text=True,
                              timeout=5).stdout.strip()
        if full and " " in full:
            found.add(full)
    except (OSError, subprocess.SubprocessError):
        pass
    if found:
        db.set_meta(conn, "identity.me", db.jdump(sorted(found)))
    return sorted(found)


def is_me(conn: sqlite3.Connection, name: str) -> bool:
    """Does this name refer to the user?

    Strict about surnames: near-duplicate self cards must match, but unrelated
    people sharing a given name must not. A bare first name stays unresolved;
    the prompt tells the model to use "me" for self-reference.
    """
    candidate = (name or "").strip().lower()
    if not candidate:
        return False
    if candidate in ("me", "i", "myself", "self"):
        return True

    mine_names = [m.lower() for m in me_names(conn)]
    if candidate in mine_names:
        return True

    parts = candidate.split()
    if len(parts) < 2:
        return False
    for mine in mine_names:
        mine_parts = mine.split()
        if len(mine_parts) < 2:
            continue
        same_given = parts[0][:3] == mine_parts[0][:3]
        surname, mine_surname = parts[-1], mine_parts[-1]
        related = surname.startswith(mine_surname[:4]) or mine_surname.startswith(surname[:4])
        if same_given and related:
            return True
    return False


# ---------------------------------------------------------------- top tier ----

def top_tier(conn: sqlite3.Connection) -> set[str]:
    return {r["person"] for r in conn.execute("SELECT person FROM top_tier").fetchall()}


def add_top_tier(conn: sqlite3.Connection, person: str) -> None:
    conn.execute("INSERT INTO top_tier(person, added_at) VALUES(?,?) ON CONFLICT DO NOTHING",
                 (person, db.now()))
    conn.commit()


def remove_top_tier(conn: sqlite3.Connection, person: str) -> None:
    conn.execute("DELETE FROM top_tier WHERE person = ?", (person,))
    conn.commit()


# ------------------------------------------------------------------ senders ----

#: Who set a sender's decision. `auto` is revisable bookkeeping; `you` and `agent`
#: are judgements nothing reopens.
SENDER_SOURCES = ("auto", "you", "agent")


def sender_decision(conn: sqlite3.Connection, address: str) -> str | None:
    row = conn.execute("SELECT decision FROM senders WHERE address = ?",
                       (normalize(address),)).fetchone()
    return row["decision"] if row else None


def sender_row(conn: sqlite3.Connection, address: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM senders WHERE address = ?",
                        (normalize(address),)).fetchone()


def sender_blocked(conn: sqlite3.Connection, address: str) -> bool:
    """Has a person — or the agent on their behalf — said no to this sender?

    A user exclusion is final; a gate `archive` guess is still overturned by a
    subject reporting an event.
    """
    row = sender_row(conn, address)
    return bool(row and row["decision"] in ("archive", "ignore")
                and (row["source"] or "auto") != "auto")


def set_sender(conn: sqlite3.Connection, address: str, decision: str,
               reason: str | None = None, source: str = "auto") -> None:
    if decision not in ("ignore", "archive", "process"):
        raise ValueError("decision must be ignore | archive | process")
    if source not in SENDER_SOURCES:
        raise ValueError(f"source must be one of {', '.join(SENDER_SOURCES)}")
    conn.execute(
        "INSERT INTO senders(address, decision, reason, count, updated_at, source)"
        " VALUES(?,?,?,0,?,?)"
        " ON CONFLICT(address) DO UPDATE SET decision = excluded.decision,"
        " reason = excluded.reason, updated_at = excluded.updated_at,"
        " source = excluded.source",
        (normalize(address), decision, reason, db.now(), source),
    )
    conn.commit()


def bump_sender(conn: sqlite3.Connection, address: str) -> None:
    conn.execute("UPDATE senders SET count = count + 1 WHERE address = ?", (normalize(address),))


def senders(conn: sqlite3.Connection, decision: str | None = None) -> list[sqlite3.Row]:
    if decision:
        return conn.execute(
            "SELECT * FROM senders WHERE decision = ? ORDER BY count DESC", (decision,)
        ).fetchall()
    return conn.execute("SELECT * FROM senders ORDER BY decision, count DESC").fetchall()
