"""Calendar rows, matching, merging, recurrence, and containment."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import db

KINDS = ("commitment", "availability", "opportunity", "observed")
STATUSES = ("mentioned", "tentative", "confirmed", "declined", "happened")
MATCH_WINDOW_DAYS = 10

# Fields a diff may set. Everything else is bookkeeping.
MUTABLE = (
    "date", "until", "time", "kind", "subject", "title", "location",
    "status", "participants", "hosts", "series", "note", "source", "part_of", "rsvp_url",
    "join_url", "instead_of",
)

#: Fields a caller may deliberately **empty**, and the only ones.
#:
#: `upsert` treats `""` as not supplied so partial diffs cannot blank untouched columns.
#: Clearing is explicit (`upsert(clear=("location",))`) and limited to fields where
#: absence has meaning; required identity fields cannot be cleared.
CLEARABLE = ("location", "note", "time", "until", "join_url", "rsvp_url", "series")

# Write provenance. Nightly may overwrite today's cheaper writes; a cheap pass may not
# walk back over what the frontier pass or the user themselves already settled today.
PRECEDENCE = {"cli": 4, "live": 4, "ical": 4, "sweep": 3, "dream:nightly": 3,
              "dream:ondemand": 2, "dream:realtime": 1}


def precedence(written_by: str) -> int:
    return PRECEDENCE.get(written_by or "", 2)


#: Sources that read an occasion directly rather than from a conversational mention.
OBSERVED_ORIGINS = ("ical:", "partiful:")


def _observed(event: "Event") -> bool:
    return str(event.origin or event.source or "").startswith(OBSERVED_ORIGINS)


#: Words a title can be built from that identify nothing on their own. Kept local
#: rather than imported from `todos.GENERIC`: that set is tuned for matching a question
#: to a row, this one for deciding two titles are the same occasion, and tying them
#: together would make every future edit to one a silent change to the other.
_OCCASION_WORDS = frozenset({
    "the", "a", "an", "at", "with", "and", "for", "my", "our",
    "party", "night", "dinner", "lunch", "meeting", "event", "session", "hangout",
})


def _title_absorbs(one: str, other: str) -> bool:
    """Return whether one title strictly contains the other's identifying words."""
    left = {word for word in db.slugify(one).split("-") if word}
    right = {word for word in db.slugify(other).split("-") if word}
    if not left or not right:
        return False
    smaller, bigger = (left, right) if len(left) <= len(right) else (right, left)
    return smaller < bigger and bool(smaller - _OCCASION_WORDS)


def _says_less(new: str, old: str) -> bool:
    """Return whether an incoming title only removes words from the stored title."""
    words = {word for word in db.slugify(new).split("-") if word}
    older = {word for word in db.slugify(old).split("-") if word}
    return bool(words) and words < older


def _observed_writer(written_by: str) -> bool:
    """Only another observation, or the user, may move an observed row's date."""
    return (written_by or "").split(":", 1)[0] in ("ical", "partiful", "cli", "live")


#: Invite platforms that stamp themselves into an event's *name* on export. Anchored to
#: the end and to a trailing `|`, so a band called "Partiful" or a party genuinely named
#: "Dinner | Partiful Reunion" is untouched — this only strips the export's own tag.
_PLATFORM_TAG = re.compile(r"\s*\|\s*(Partiful|Eventbrite|Evite|Luma|Posh)\s*$", re.I)


def split_platform(title: str) -> tuple[str, str]:
    """Split an invite-platform export suffix from the event title."""
    found = _PLATFORM_TAG.search(title or "")
    if not found:
        return title, ""
    return _PLATFORM_TAG.sub("", title).strip(), found.group(1)


@dataclass
class Event:
    key: str
    date: str
    title: str
    kind: str = "commitment"
    subject: str = "me"
    until: str | None = None
    time: str | None = None
    location: str | None = None
    status: str = "mentioned"
    participants: list[str] = field(default_factory=list)
    hosts: list[str] = field(default_factory=list)
    series: str | None = None
    note: str | None = None
    source: str | None = None
    #: Where this row came from first. Set once at insert, never updated — `source` is
    #: mutable and answers a different question, "who touched it last".
    origin: str | None = None
    #: The `events.id` of the row this one happens inside. Never a key: a key embeds a
    #: date, so re-dating the parent would orphan every child naming it.
    part_of: int | None = None
    #: Where you reply to this invitation, if it is one.
    rsvp_url: str | None = None
    #: How you attend, when attending means pressing something rather than going
    #: somewhere. Not `location`, which is where; not `rsvp_url`, which is how you
    #: answer. "Online" is a true location and a useless one.
    join_url: str | None = None
    #: The scheduled day of its own series that this row stands in for. Set means "this
    #: week only" — the rule still says Tuesday and this once it is Wednesday. Without
    #: it a moved week and a moved cadence are the same row.
    instead_of: str | None = None
    written_by: str = "cli"
    id: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Event":
        return cls(
            id=row["id"],
            key=row["key"],
            date=row["date"],
            until=row["until"],
            time=row["time"],
            kind=row["kind"],
            subject=row["subject"],
            title=row["title"],
            location=row["location"],
            status=row["status"],
            participants=db.jload(row["participants"], []),
            hosts=db.jload(row["hosts"], []) if "hosts" in row.keys() else [],
            series=row["series"],
            note=row["note"],
            source=row["source"],
            origin=row["origin"] if "origin" in row.keys() else row["source"],
            part_of=row["part_of"] if "part_of" in row.keys() else None,
            rsvp_url=row["rsvp_url"] if "rsvp_url" in row.keys() else None,
            join_url=row["join_url"] if "join_url" in row.keys() else None,
            instead_of=row["instead_of"] if "instead_of" in row.keys() else None,
            written_by=row["written_by"],
        )

    def one_line(self, show_date: bool = True, extra: list[str] | None = None,
                 overview: bool = False) -> str:
        """Render one row; overview mode omits location and notes for brevity."""
        bits = []
        if show_date:
            bits.append(db.parse_date(self.date).strftime("%a %b %-d"))
        head = self.title
        platform = ""
        if overview:
            head, platform = split_platform(head)
            head = f'"{head}"'
        if self.needs_subject():
            # Prefix the owner. A trailing name reads as a companion,
            # not as the person the row is about.
            head = f"{self.subject}: {head}"
        if self.time:
            # Skip the time suffix when the title already states it.
            stamp = friendly_time(self.time)
            if stamp and stamp.lower() not in head.lower():
                head += f", {stamp}"
        if self.until and self.until > self.date:
            head += f" (until {db.parse_date(self.until).strftime('%a %b %-d')})"
        bits.append(head)
        tail = [word for word in (self.plain_state(),) if word]
        if self.rsvp_url:
            # Keep the link even when declined: the invite remains actionable.
            tail.append(f"invite: {_short_url(self.rsvp_url)}")
        if self.join_url:
            # Render in full: a shortened join link is not pressable.
            tail.append(f"join: {self.join_url}")
        if self.location and not overview:
            tail.append(self.location)
        if self.note and not overview and self.note.casefold() not in head.casefold():
            # Keep occasion-specific details on the active row.
            tail.append(self.note)
        if platform:
            # Move the export tag out of the name into its own clause.
            tail.append(f"via {platform}")
        names = self.visible_hosts()
        if names:
            shown = names[:3]
            more = len(names) - len(shown)
            tail.append("hosted by " + ", ".join(shown) + (f" +{more}" if more else ""))
        if self.participants:
            # Participants are the point of most rows; always render them.
            people = self.participants[:4]
            more = len(self.participants) - len(people)
            tail.append("with " + ", ".join(people) + (f" +{more}" if more else ""))
        tail.extend(str(item).strip() for item in (extra or []) if str(item or "").strip())
        line = "  ".join(bits)
        if tail:
            line += " — " + " · ".join(tail)
        return line

    def needs_subject(self) -> bool:
        """Return whether the subject must be prefixed to avoid ambiguity."""
        if not self.subject or self.subject == "me":
            return False
        if self.subject.casefold() in (self.title or "").casefold():
            return False
        return self.subject.casefold() not in {
            str(p).casefold() for p in (self.participants or ())}

    def visible_hosts(self) -> list[str]:
        """Hosts not already identified by the event title."""
        # Strip possessives before matching so "Katie's" matches "Katie O'Rourke".
        title = re.sub(r"[’']s\b", "", self.title, flags=re.IGNORECASE)
        title_words = set(db.slugify(title).split("-"))
        return [name for name in self.hosts
                if not ({word for word in db.slugify(name).split("-") if len(word) > 2}
                        & title_words)]

    @property
    def last_day(self) -> str:
        """The day this row stops being current. Single-day rows end when they start."""
        return self.until if (self.until and self.until > self.date) else self.date

    def covers(self, ref: date) -> bool:
        """Is this row still live on `ref`? A visit is not over on the day it began."""
        return db.parse_date(self.date) <= ref <= db.parse_date(self.last_day)

    def plain_state(self) -> str:
        """Return the compact state phrase used in the brief."""
        if self.status == "declined":
            return "not going"
        if self.rsvp_url and self.status in ("mentioned", "tentative"):
            # Distinguish unanswered invitations ("not replied") from open mentions.
            return "not replied"
        if self.kind == "opportunity":
            # A settled status overrides kind: the decision outranks how the occasion
            # arose. Mentioned opportunities still read "could go".
            if self.status == "confirmed":
                return "confirmed"
            return "could go" if self.status != "happened" else ""
        if self.kind == "availability":
            # The title already names whose state it is ("Harper's flight lands").
            return "" if self.status in ("confirmed", "happened") else "maybe"
        if self.kind == "observed" or self.status == "happened":
            return ""                       # the past marker in the brief says this
        return {"mentioned": "maybe", "tentative": "maybe",
                "confirmed": "confirmed"}.get(self.status, "")

    def state_text(self) -> str:
        """A complete, user-facing description of kind and status."""
        if self.status == "declined":
            return "You're not going" if self.subject == "me" else "Not happening"
        if self.status == "happened" or self.kind == "observed":
            return "Already happened"
        if self.kind == "availability":
            who = self.subject if self.subject and self.subject != "me" else "You"
            return f"{who} may be available" if self.status == "tentative" else f"{who} is available"
        if self.status == "confirmed":
            return "You're going" if self.subject == "me" else "Confirmed"
        if self.kind == "opportunity":
            return "You haven't decided whether to go"
        return "Tentative plan" if self.status == "tentative" else "Possible plan"


def _short_url(url: str) -> str:
    """"https://partiful.com/e/abc123" -> "partiful.com/e/abc123"."""
    return re.sub(r"^https?://(?:www\.)?", "", str(url or "").strip()).rstrip("/")


_CLOCK = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def friendly_time(value: str | None) -> str:
    """Convert a bare 24-hour clock to compact conversational time."""
    text = (value or "").strip()
    match = _CLOCK.match(text)
    if not match:
        return text
    hour, minute = int(match.group(1)), match.group(2)
    suffix = "am" if hour < 12 else "pm"
    hour = hour % 12 or 12
    return f"{hour}{'' if minute == '00' else ':' + minute}{suffix}"


def make_key(title: str, on: str, series: str | None = None, subject: str = "me") -> str:
    stem = series or db.slugify(title)
    prefix = "" if subject in (None, "", "me") else f"{db.slugify(subject)}:"
    return f"{prefix}{stem}@{db.parse_date(on).isoformat()}"


def _free_key(conn: sqlite3.Connection, key: str) -> str:
    """Return ``key`` or its next unused numeric suffix."""
    if conn.execute("SELECT 1 FROM events WHERE key = ?", (key,)).fetchone() is None:
        return key
    for n in range(2, 100):
        candidate = f"{key}~{n}"
        if conn.execute("SELECT 1 FROM events WHERE key = ?",
                        (candidate,)).fetchone() is None:
            return candidate
    # Fall back to a timestamped suffix rather than failing when exhausted.
    return f"{key}~{db.now()}"


def get(conn: sqlite3.Connection, key: str) -> Event | None:
    row = conn.execute("SELECT * FROM events WHERE key = ?", (key,)).fetchone()
    return Event.from_row(row) if row else None


def get_by_id(conn: sqlite3.Connection, event_id: int) -> Event | None:
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return Event.from_row(row) if row else None


def find_match(
    conn: sqlite3.Connection,
    *,
    title: str,
    on: str,
    series: str | None = None,
    participants: list[str] | None = None,
    subject: str = "me",
) -> Event | None:
    """Deterministic match. Returns the row a new mention should update, or None."""
    scored = find_match_scored(conn, title=title, on=on, series=series,
                               participants=participants, subject=subject)
    return scored[0] if scored else None


def _same_occurrence(conn: sqlite3.Connection, series_slug: str, candidate: "Event",
                     on: str) -> bool:
    """Compare two series rows by their scheduled slot when a rule exists."""
    from . import series as series_mod
    rule = series_mod.get(conn, series_slug)
    if rule is None:
        return True
    # A cadence change is a boundary: days on opposite sides of effective_on
    # are never the same occurrence.
    if rule.effective_on:
        before = [str(when) < rule.effective_on
                  for when in (on, candidate.instead_of or candidate.date)]
        if before[0] != before[1]:
            return False
    here = series_mod.slot_for(rule, on)
    there = series_mod.slot_for(rule, candidate.date, candidate.instead_of)
    if here is None or there is None:
        return True
    return here == there


def find_match_scored(
    conn: sqlite3.Connection,
    *,
    title: str,
    on: str,
    series: str | None = None,
    participants: list[str] | None = None,
    subject: str = "me",
) -> tuple[Event, int] | None:
    """Return the best deterministic match and its confidence tier."""
    target = db.parse_date(on)
    participants = participants or []
    lo = (target.toordinal() - MATCH_WINDOW_DAYS)
    hi = (target.toordinal() + MATCH_WINDOW_DAYS)
    rows = conn.execute("SELECT * FROM events WHERE subject = ?", (subject,)).fetchall()
    slug = db.slugify(title)

    # Two passes, because one row's tier depends on what the other rows look like: the
    # absorption arm below is only usable when exactly one row on the day answers to the
    # shorter name.
    seen: list[tuple[Event, int, int, int, bool]] = []   # row, ordinal, distance, tier, absorbs
    for row in rows:
        ev = Event.from_row(row)
        try:
            ordinal = db.parse_date(ev.date).toordinal()
        except ValueError:
            continue
        if not (lo <= ordinal <= hi):
            continue

        tier = 0
        if series and ev.series == series:
            # Skip occurrences on a different scheduled day outright;
            # weaker tiers must not rescue them.
            if not _same_occurrence(conn, series, ev, on):
                continue
            tier = 3
        elif db.slugify(ev.title) == slug and (
                ordinal == target.toordinal()
                or on in _dates_held(conn, ev.id)
                or _observed(ev)):
            # Exact names match on the same day, a previously held date, or an
            # observed row. Other cross-date identity needs a key or series claim;
            # wording only nominates.
            tier = 2
        elif (participants and set(participants) & set(ev.participants)
              and _title_overlap(ev.title, title, participants)):
            tier = 1
        # Same-day titles where one absorbs the other match at tier 2.
        # Exact date only: this tier carries no date evidence.
        absorbs = (tier < 2 and ordinal == target.toordinal()
                   and _title_absorbs(ev.title, title))
        if tier or absorbs:
            seen.append((ev, ordinal, abs(ordinal - target.toordinal()), tier, absorbs))

    # When two rows on one day both absorb the same short title, the name
    # stops being evidence and each row falls back to its other tiers.
    ambiguous = sum(1 for entry in seen if entry[4]) > 1

    scored: list[tuple[int, int, Event, bool]] = []   # confidence, -distance, row, absorbed
    for ev, ordinal, distance, tier, absorbs in seen:
        absorbed = absorbs and not ambiguous
        confidence = 2 if absorbed else tier
        if not confidence:
            continue
        # Weak matches must not cross today: past and future occasions stay distinct.
        if confidence < 2 and _crosses_today(ordinal, target.toordinal()):
            continue
        # Weak matches against a spanning row are containment, not identity;
        # see `link_contained`.
        if confidence < 2 and ev.until and ev.until > ev.date \
                and db.parse_date(ev.date) <= target <= db.parse_date(ev.until):
            continue
        # Nothing that already happened gets re-dated into the future.
        if ev.status == "happened" and target.toordinal() > db.today().toordinal():
            continue
        scored.append((confidence, -distance, ev, absorbed))

    # Within a tier, an exact title beats an absorbing one, then nearer dates win.
    best: tuple[int, int, Event, bool] | None = None
    rank: tuple | None = None
    for candidate in scored:
        here = (candidate[0], not candidate[3], candidate[1])
        if best is None or here > rank:
            best, rank = candidate, here
    return (best[2], best[0]) if best else None


def _dates_held(conn: sqlite3.Connection, event_id: int) -> set[str]:
    """Every day this row has ever been on, from its own history."""
    held: set[str] = set()
    for row in conn.execute(
            "SELECT old_value, new_value FROM event_history"
            " WHERE event_id = ? AND field = 'date'", (event_id,)):
        held |= {str(row["old_value"] or ""), str(row["new_value"] or "")}
    return {value for value in held if value}


def _field_versions(conn: sqlite3.Connection, event_id: int,
                    born: str) -> dict[str, str]:
    """When the evidence behind each field was *said*, keyed by field."""
    versions: dict[str, str] = {}
    for row in conn.execute(
            "SELECT field, changed_at, evidence_ts FROM event_history"
            " WHERE event_id = ? ORDER BY id", (event_id,)):
        versions[str(row["field"])] = str(row["evidence_ts"] or row["changed_at"])
    return {name: versions.get(name, born) for name in MUTABLE}


def _crosses_today(a: int, b: int) -> bool:
    """Are these two ordinals on opposite sides of today?"""
    today = db.today().toordinal()
    return (a < today) != (b < today)


def _title_overlap(a: str, b: str, participants: list[str] | None = None) -> bool:
    """Check title overlap after excluding participant names and stop words."""
    stop = {"the", "a", "an", "at", "with", "on", "in", "to", "for", "and", "then"}
    for person in participants or []:
        stop.update(w for w in db.slugify(person).split("-") if w)
    wa = {w for w in db.slugify(a).split("-") if w and w not in stop}
    wb = {w for w in db.slugify(b).split("-") if w and w not in stop}
    return bool(wa & wb)


def _claimed_by_another(conn: sqlite3.Connection, event_id: int, field: str,
                        written_by: str) -> bool:
    """Return whether another writer has already set this field."""
    return conn.execute(
        "SELECT 1 FROM event_history WHERE event_id = ? AND field = ?"
        "   AND written_by <> ? LIMIT 1", (event_id, field, written_by)).fetchone() \
        is not None


#: What a recurring thing is durably true of, as opposed to what one occurrence decided.
#: Where it is and how you join it belong to the series; the day, the time and whether the user
#: is going belong to the occasion. A new instance is not `confirmed` because the last
#: one happened.
SERIES_QUALITIES = ("location", "join_url")


def _series_for(conn: sqlite3.Connection, title: str) -> str | None:
    """Find an existing series by exact title slug."""
    slug = db.slugify(title or "")
    if not slug:
        return None
    row = conn.execute(
        "SELECT series FROM events WHERE series = ? LIMIT 1", (slug,)).fetchone()
    if row:
        return row["series"]
    declared = conn.execute("SELECT slug FROM series WHERE slug = ?", (slug,)).fetchone()
    return declared["slug"] if declared else None


def _inherit_from_series(conn: sqlite3.Connection, fields: dict) -> None:
    """Fill absent series-level qualities from the rule, then recent occurrences."""
    series = fields.get("series") or _series_for(conn, fields.get("title", ""))
    if not series:
        return
    wanted = [name for name in SERIES_QUALITIES if not fields.get(name)]
    if not wanted:
        fields.setdefault("series", series)
        return
    fields.setdefault("series", series)
    rule = conn.execute(
        f"SELECT {', '.join(wanted)} FROM series WHERE slug = ?", (series,)).fetchone()
    if rule is not None:
        for name in list(wanted):
            if rule[name]:
                fields[name] = rule[name]
                wanted.remove(name)
    if not wanted:
        return
    row = conn.execute(
        f"SELECT {', '.join(wanted)} FROM events"
        "  WHERE series = ? AND (" + " OR ".join(f"{n} IS NOT NULL" for n in wanted) + ")"
        "  ORDER BY date DESC, id DESC LIMIT 1", (series,)).fetchone()
    if row is None:
        return
    for name in wanted:
        if row[name]:
            fields[name] = row[name]


def upsert(
    conn: sqlite3.Connection,
    fields: dict,
    *,
    written_by: str = "cli",
    match: bool = True,
    evidence_ts: "str | dict[str, str] | None" = None,
    inferred: tuple[str, ...] = (),
    clear: tuple[str, ...] = (),
    replace_participants: bool = False,
    commit: bool = True,
) -> tuple[Event, str]:
    """Insert or update one event, preserving history and write precedence."""
    fields = {k: v for k, v in fields.items() if k in MUTABLE or k == "key"}
    if "title" not in fields and "key" not in fields:
        raise ValueError("event needs a title or a key")
    on = fields.get("date")
    if not on:
        raise ValueError("event needs a date")
    fields["date"] = db.parse_date(on).isoformat()
    if fields.get("time") and not db.valid_local_time(fields["date"], str(fields["time"])):
        fields["time"] = None
    # An inverted span would exclude the row from every window; keep the row, drop `until`.
    if fields.get("until"):
        try:
            fields["until"] = (fields["until"]
                               if db.parse_date(fields["until"]).isoformat() >= fields["date"]
                               else None)
        except (ValueError, TypeError):
            fields["until"] = None
    if fields.get("kind") not in KINDS and "kind" in fields:
        fields["kind"] = "commitment"
    if fields.get("status") not in STATUSES and "status" in fields:
        fields["status"] = "mentioned"
    # Defaults apply to new rows only; partial updates must not assert them.
    subject = fields.get("subject") or "me"

    existing: Event | None = None
    # How sure we are this is the same row: 4 when the diff named the key outright,
    # then find_match's own ladder beneath that. Only the date update consults it.
    confidence = 0
    if fields.get("key"):
        existing = get(conn, fields["key"])
        confidence = 4 if existing is not None else 0
    if existing is None and match:
        scored = find_match_scored(
            conn,
            title=fields.get("title", ""),
            on=fields["date"],
            series=fields.get("series"),
            participants=fields.get("participants") or [],
            subject=subject,
        )
        if scored:
            existing, confidence = scored
    if existing is None:
        # Only new occurrences inherit series qualities; amendments must not.
        _inherit_from_series(conn, fields)
        key = fields.get("key") or _free_key(
            conn,
            make_key(fields.get("title", ""), fields["date"], fields.get("series"), subject),
        )
        stamp = db.now()
        # `born` floors the per-field guard below for fields nothing has revised yet.
        born = (max((str(v) for v in evidence_ts.values() if v), default=None)
                if isinstance(evidence_ts, dict) else evidence_ts)
        conn.execute(
            """INSERT INTO events(key, date, until, time, kind, subject, title, location, status,
                                  participants, hosts, series, note, source, origin, part_of,
                                  rsvp_url, join_url, instead_of, written_by,
                                  created_at, updated_at, evidence_ts)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key, fields["date"], fields.get("until"),
                fields.get("time"), fields.get("kind") or "commitment", subject,
                fields.get("title", ""), fields.get("location"),
                fields.get("status") or "mentioned",
                db.jdump(fields.get("participants") or []),
                db.jdump(fields.get("hosts") or []), fields.get("series"),
                fields.get("note"), fields.get("source"),
                # Set here and nowhere else. `origin` is not in MUTABLE, so no later
                # write can reach it — which is the whole point of having it.
                fields.get("source"), fields.get("part_of"), fields.get("rsvp_url"),
                fields.get("join_url"), fields.get("instead_of"),
                written_by, stamp, stamp, str(born) if born else None,
            ),
        )
        if commit:
            conn.commit()
        return get(conn, key), "inserted"  # type: ignore[return-value]

    # Precedence guards settled rows from cheaper passes; newer evidence still applies.
    # The test is on evidence time, not the calendar day.
    row = conn.execute(
        "SELECT updated_at, created_at, evidence_ts FROM events WHERE id = ?",
        (existing.id,)
    ).fetchone()
    last_write = str(row["updated_at"])
    # Guarded means a cheaper writer revises a row a more authoritative writer settled.
    # The verdict is per field, on evidence time: a bundle's newest line about the time
    # carries no authority about the place.
    per_field = isinstance(evidence_ts, dict)
    stamps = dict(evidence_ts) if per_field else {}
    default_ts = None if per_field else evidence_ts

    def evidence_for(name: str) -> str | None:
        return stamps.get(name, default_ts)

    def _stale(here: str, version: str) -> bool:
        # A live correction restating its own hour still decides: two turns in
        # one second are ordered by arrival, not by a clock that cannot tell
        # them apart. Replay protection stays with the operation record, which
        # already turned the identical retry into a no-op before this point.
        if per_field and written_by == "live":
            return str(here) < (version or "")
        return str(here) <= (version or "")

    guarded = precedence(written_by) < precedence(existing.written_by)
    # An empty mapping claims no line supports these fields; never fall back to the day.
    dated = (per_field or bool(evidence_ts)) and (
        guarded or (per_field and written_by == "live"))
    # A cited typed correction carries its messages' evidence time into the same
    # per-field guards dream writes go through: without this a live write always
    # outranks on writer precedence and its execution time becomes the decision's
    # time, letting an old retrieved line undo a newer settlement. Uncited live
    # writes keep the existing run-time semantics below.
    # Floor for fields nobody revised: the creating write's evidence time. A
    # cited live correction speaks for the user about those lines, so its first
    # decision is free of any wall-clock floor — the founding message always
    # arrives before the row typed from it, and run time must not pose as
    # evidence. Every other writer keeps the established floor.
    if per_field and written_by == "live":
        born = str(row["evidence_ts"] or "")
    else:
        born = str(row["evidence_ts"] or row["created_at"] or last_write)
    settled = _field_versions(conn, existing.id, born) if dated else {}
    stale: list[str] = []
    if guarded and not dated and last_write[:10] != db.today().isoformat():
        # Writers without evidence timestamps may only revise rows written today.
        return existing, "unchanged"

    changes: list[tuple[str, str, str]] = []
    updates: dict[str, object] = {}
    for name in clear:
        # Clears run before sets so an explicit set beside a clear wins.
        if name not in CLEARABLE:
            raise ValueError(f"{name} cannot be cleared; one of {', '.join(CLEARABLE)}")
        if fields.get(name):
            continue
        old = getattr(existing, name)
        if old in (None, ""):
            continue
        updates[name] = None
        changes.append((name, str(old), ""))
    for name in MUTABLE:
        if name not in fields:
            continue
        new = fields[name]
        if name == "participants":
            merged = sorted(set(new or []) if replace_participants
                            else set(existing.participants) | set(new or []))
            if merged != sorted(existing.participants):
                updates[name] = db.jdump(merged)
                changes.append((name, db.jdump(existing.participants), db.jdump(merged)))
            continue
        if name == "hosts":
            replacement = list(dict.fromkeys(str(item).strip() for item in (new or [])
                                             if str(item).strip()))
            if replacement != existing.hosts:
                updates[name] = db.jdump(replacement)
                changes.append((name, db.jdump(existing.hosts), db.jdump(replacement)))
            continue
        old = getattr(existing, name)
        if new in (None, "") or new == old:
            continue
        if name == "status" and STATUSES.index(new) < STATUSES.index(old) and old == "confirmed":
            continue  # don't walk a confirmed row backwards to 'mentioned'
        if name == "title" and _says_less(new, old):
            continue  # a shorter name for what is already here is not news
        if name in inferred and _claimed_by_another(conn, existing.id, name, written_by):
            # Derived fields may create values but never restate them
            # over another writer's decision.
            continue
        if name == "date" and confidence <= 1:
            # Tier-1 matches may pool knowledge but may not move the date;
            # participant overlap asserts identity too weakly.
            continue
        if name in ("date", "until", "time") and _observed(existing) \
                and not _observed_writer(written_by):
            # Only observations or the user may move an observed row's schedule.
            # Conversations may still add location, notes, or guests.
            continue
        here = evidence_for(name)
        if dated and (not here or _stale(here, settled.get(name, ""))):
            # Older than the decision it would revise; genuinely newer evidence lands.
            stale.append(name)
            continue
        updates[name] = new
        changes.append((name, str(old), str(new)))

    if not updates:
        return existing, "unchanged"

    # The row keeps the highest authority that ever wrote it, not the last one.
    # Per-field provenance lives in `event_history`.
    authority = (written_by if precedence(written_by) >= precedence(existing.written_by)
                 else existing.written_by)
    sets = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(
        f"UPDATE events SET {sets}, written_by = ?, updated_at = ? WHERE id = ?",
        (*updates.values(), authority, db.now(), existing.id),
    )
    for field_name, old, new in changes:
        conn.execute(
            "INSERT INTO event_history(event_id, field, old_value, new_value,"
            " changed_at, evidence_ts, written_by) VALUES(?,?,?,?,?,?,?)",
            (existing.id, field_name, old, new, db.now(),
             (evidence_for(field_name) or default_ts or None), written_by),
        )
    if commit:
        conn.commit()
    return get_by_id(conn, existing.id), "updated"  # type: ignore[return-value]


def window(conn: sqlite3.Connection, days_back: int, days_forward: int, ref: date | None = None) -> list[Event]:
    """Return every event that overlaps the requested date window."""
    lo, hi = db.window_bounds(days_back, days_forward, ref)
    rows = conn.execute(
        "SELECT * FROM events WHERE date <= ? AND coalesce(nullif(until,''), date) >= ?"
        " ORDER BY date, coalesce(time,''), id",
        (hi, lo),
    ).fetchall()
    return [Event.from_row(r) for r in rows]


def between(conn: sqlite3.Connection, lo: str, hi: str) -> list[Event]:
    rows = conn.execute(
        "SELECT * FROM events WHERE date BETWEEN ? AND ? ORDER BY date, coalesce(time,''), id",
        (lo, hi),
    ).fetchall()
    return [Event.from_row(r) for r in rows]


#: Relevant-event context is for amendments, not history search. Keep only the last
#: three days (including multi-day events still in progress), while looking far enough
#: ahead for a conversation to amend a plan that is not yet in the seven-day brief.
AMENDABLE_DAYS_FORWARD = 120
AMENDABLE_DAYS_BACK = 3


#: Below this an address local part is a role, not a person.
_ADDRESS_MIN = 6
#: Shared words needed to relate an event to a bundle whose people are all unresolved.
NO_ROSTER_WORDS = 2


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").casefold())


def address_keys(entities: list[str]) -> set[str]:
    """Compacted local parts of email-thread entities, as person keys."""
    keys = set()
    for entity in entities:
        if not entity.startswith("thread:email:") or "@" not in entity:
            continue
        local = _compact(entity.split(":", 2)[2].split("@", 1)[0])
        if len(local) >= _ADDRESS_MIN:
            keys.add(local)
    return keys


def name_keys(name: str) -> set[str]:
    """Compaction variants of one person's name, for address comparison."""
    head = str(name or "").split(",", 1)[0]
    tokens = re.findall(r"[a-z0-9]+", head.casefold())
    if not tokens:
        return set()
    keys = {"".join(tokens[:2])}
    if len(tokens) > 2:
        keys.add(tokens[0] + tokens[-1])
    return {k for k in keys if len(k) >= _ADDRESS_MIN}


def amendable_groups(conn: sqlite3.Connection, *, people: list[str],
                     entity: str | None = None, text: str = "",
                     source_limit: int = 4,
                     related_limit: int = 4,
                     entities: list[str] | None = None,
                     ) -> tuple[list[Event], list[Event]]:
    """Return recent/upcoming rows linked by source entity or non-user people."""
    lo, hi = db.window_bounds(AMENDABLE_DAYS_BACK, AMENDABLE_DAYS_FORWARD)
    rows = conn.execute(
        "SELECT * FROM events WHERE date <= ? AND coalesce(nullif(until,''), date) >= ?"
        " AND status != 'declined' ORDER BY date", (hi, lo)).fetchall()
    named = {p.casefold() for p in people if p and p.casefold() != "me"}
    from_address = address_keys(
        [candidate for candidate in [entity, *(entities or [])] if candidate])
    graph_entities = list(dict.fromkeys(
        candidate for candidate in [entity, *(entities or [])] if candidate
    ))
    group_entity = False
    for candidate in graph_entities:
        if not candidate.startswith("thread:"):
            continue
        _kind, stream, thread = candidate.split(":", 2)
        shape = conn.execute(
            "SELECT is_group FROM threads WHERE stream = ? AND thread = ?",
            (stream, thread),
        ).fetchone()
        group_entity = group_entity or bool(shape and shape["is_group"])

    from_here: set[str] = set()
    if graph_entities:
        placeholders = ",".join("?" for _ in graph_entities)
        from_here = {r["ref"] for r in conn.execute(
            f"""SELECT DISTINCT ref FROM provenance
                 WHERE kind = 'event' AND entity IN ({placeholders})""",
            graph_entities,
        )}

    source: list[Event] = []
    related: list[tuple[int, int, tuple, Event]] = []
    words = {w for w in re.findall(r"[a-z0-9']{3,}", text.casefold())
             if w not in {"this", "that", "with", "from", "have", "will", "about"}}
    today = db.today()

    def temporal(event: Event) -> tuple:
        start = db.parse_date(event.date)
        end = db.parse_date(event.until or event.date)
        # Upcoming/ongoing first, nearest occurrence first; recently happened after.
        return (0, max(0, (start - today).days)) if end >= today else (
            1, (today - end).days)

    for row in rows:
        event = Event.from_row(row)
        mine = row["key"] in from_here
        if mine:
            source.append(event)
            continue
        who = {p.casefold() for p in event.participants if p.casefold() != "me"}
        if event.subject and event.subject.casefold() != "me":
            who.add(event.subject.casefold())
        overlap = who & named
        # A compacted address matching a compacted name links them. Exact whole-value
        # match only, so role addresses match nothing.
        by_address = bool(from_address and {k for p in event.participants
                                            for k in name_keys(p)} & from_address)
        if by_address:
            overlap = overlap or {"~address"}
        event_words = set(re.findall(
            r"[a-z0-9']{3,}", " ".join((event.title, event.location or "",
                                         event.series or "")).casefold()))
        lexical = len(words & event_words)
        # Title words only; location and series carry exporter text a person did not write.
        title_lexical = len(words & set(re.findall(r"[a-z0-9']{3,}", event.title.casefold())))
        if not overlap:
            # With no roster, shared title wording is the only edge; require two words.
            # Group threads keep the stricter rule.
            if named or group_entity or title_lexical < NO_ROSTER_WORDS:
                continue
        # One shared person is weak in a large room without lexical overlap.
        # DMs may still match on one person.
        if group_entity and len(overlap) == 1 and not lexical:
            continue
        related.append((len(overlap), lexical, temporal(event), event))

    source.sort(key=temporal)
    related.sort(key=lambda item: (-item[0], -item[1], item[2], item[3].key))
    return (source[:max(0, source_limit)],
            [item[3] for item in related[:max(0, related_limit)]])


#: Title words a bundle must share with a row before the row is *nominated* on wording
#: alone. Two, because one generic word ("dinner") nominates half the calendar.
NOMINATION_WORDS = 2

#: How far a nomination sweep may look when the ordinary edges found nothing. Bounded,
#: and the caller is told when it was cut off — an invisible truncation is how a missing
#: candidate looks exactly like a decision that there was none.
NOMINATION_LIMIT = 4


def candidates(conn: sqlite3.Connection, *, people: list[str],
               entity: str | None = None, text: str = "",
               entities: list[str] | None = None,
               source_limit: int = 4, related_limit: int = 4,
               nominated_limit: int = NOMINATION_LIMIT,
               ) -> tuple[list[Event], list[Event], list[Event], int]:
    """Linked rows, related rows, and rows nominated by wording, people, or a date.

    Nominations are worth considering, not matches; the model judges while code
    keeps identities. The overflow count returns with the list.
    """
    same, related = amendable_groups(
        conn, people=people, entity=entity, text=text, entities=entities,
        source_limit=source_limit, related_limit=related_limit)
    if nominated_limit <= 0:
        return same, related, [], 0
    seen = {event.key for event in [*same, *related]}
    body = text.casefold()
    words = {w for w in re.findall(r"[a-z0-9']{3,}", body)
             if w not in _NOMINATION_STOPWORDS}
    spoken = _people_named(conn, body)
    lo, hi = db.window_bounds(AMENDABLE_DAYS_BACK, AMENDABLE_DAYS_FORWARD)
    scored: list[tuple[int, str, Event]] = []
    for row in conn.execute(
            "SELECT * FROM events WHERE date <= ? AND coalesce(nullif(until,''), date) >= ?"
            " ORDER BY date", (hi, lo)):
        event = Event.from_row(row)
        if event.key in seen:
            continue
        title_words = set(re.findall(r"[a-z0-9']{3,}", event.title.casefold()))
        # Any one of wording, person, or date nominates; requiring all misses
        # cross-conversation corrections.
        by_words = len(words & title_words) >= NOMINATION_WORDS
        by_person = bool(spoken & {p.casefold() for p in event.participants}
                         | (spoken & {(event.subject or "").casefold()} - {"me"}))
        by_date = _names_a_date_of(conn, event, body)
        strength = sum((by_words, by_person, by_date))
        if not strength:
            continue
        scored.append((-strength, event.date, event))
    scored.sort(key=lambda item: (item[0], item[1], item[2].key))
    kept = [item[2] for item in scored[:nominated_limit]]
    return same, related, kept, max(0, len(scored) - len(kept))


def _people_named(conn: sqlite3.Connection, body: str) -> set[str]:
    """Known people this text names, as a lowercase set."""
    out: set[str] = set()
    for row in conn.execute("SELECT DISTINCT person FROM handles"):
        person = str(row["person"] or "").strip()
        if not person or person == "me":
            continue
        first = person.split()[0].casefold()
        if len(first) < 3:
            continue
        if re.search(rf"\b{re.escape(first)}\b", body):
            out.add(person.casefold())
    return out


#: Rendered forms of one date that a person actually writes. Anchored on the day number
#: *and* a month or weekday word, because a bare "19" occurs in prices, addresses and
#: phone numbers and would nominate the whole calendar.
def _names_a_date_of(conn: sqlite3.Connection, event: "Event", body: str) -> bool:
    """Does this text name a day this row is on, or has ever been on?"""
    days = {event.date, event.until or event.date}
    for row in conn.execute(
            "SELECT old_value, new_value FROM event_history"
            " WHERE event_id = ? AND field IN ('date','until')", (event.id,)):
        days |= {str(row["old_value"] or ""), str(row["new_value"] or "")}
    for value in days:
        try:
            day = db.parse_date(value)
        except (ValueError, TypeError):
            continue
        if value in body:
            return True
        number = str(day.day)
        if not re.search(rf"\b{number}(?:st|nd|rd|th)?\b", body):
            continue
        month = day.strftime("%B").casefold()
        weekday = day.strftime("%A").casefold()
        if month[:3] in body or weekday[:3] in body:
            return True
    return False


#: Words that carry no identity. Kept apart from `_OCCASION_WORDS`, which decides
#: whether two titles name one occasion; this one decides whether a sentence is talking
#: about a row at all, and tying them together would make each edit to one a silent
#: change to the other.
_NOMINATION_STOPWORDS = frozenset({
    "this", "that", "with", "from", "have", "will", "about", "the", "and", "for",
    "you", "your", "our", "was", "are", "not", "but", "can", "now", "just",
})


def amendable(conn: sqlite3.Connection, *, people: list[str], entity: str | None = None,
              text: str = "", limit: int = 8) -> list[Event]:
    """Compatibility wrapper returning the ranked union of the two graph paths."""
    source_limit = min(4, max(0, limit))
    same, related = amendable_groups(
        conn, people=people, entity=entity, text=text,
        source_limit=source_limit, related_limit=max(0, limit - source_limit))
    return (same + related)[:max(0, limit)]


def written_from(conn: sqlite3.Connection, key: str,
                 exclude: set[int] | None = None,
                 entity: str | None = None) -> list[sqlite3.Row]:
    """Return archive evidence or nearby source lines behind an event write."""
    excluded = exclude or set()
    evidence = conn.execute(
        """SELECT DISTINCT a.id, a.ts, a.person, a.from_me, a.text
             FROM evidence e JOIN archive a ON a.id = e.archive_id
            WHERE e.kind = 'event' AND e.ref = ?
              AND (? IS NULL OR e.entity = ?)
            ORDER BY a.ts DESC LIMIT 12""",
        (key, entity, entity),
    ).fetchall()
    keep = [r for r in evidence if r["id"] not in excluded]
    if keep:
        return keep[:4]

    stamps = conn.execute(
        """SELECT p.entity, p.at FROM provenance p
            WHERE p.kind = 'event' AND p.ref = ? AND p.entity IS NOT NULL
              AND (? IS NULL OR p.entity = ?)
            ORDER BY p.id LIMIT 1""", (key, entity, entity)).fetchall()
    if not stamps:
        return []
    entity, at = stamps[0]["entity"], str(stamps[0]["at"])
    kind, _, rest = entity.partition(":")
    if kind == "person":
        where, args = "a.person = ?", [rest]
    elif kind == "thread":
        stream, _, thread = rest.partition(":")
        where, args = "a.stream = ? AND a.thread = ?", [stream, thread]
    else:
        return []
    # Remind the model what was decided, not the whole conversation;
    # exclude the bundle's own lines.
    rows = conn.execute(
        f"""SELECT a.id, a.ts, a.person, a.from_me, a.text FROM archive a
             WHERE {where} AND a.gated = 1
               AND a.ts <= ? AND a.ts >= date(?, '-2 days')
             ORDER BY a.ts DESC LIMIT 12""", args + [at, at[:10]]).fetchall()
    keep = [r for r in rows if r["id"] not in excluded]
    return keep[:4]


def by_series(conn: sqlite3.Connection, series: str, limit: int = 20) -> list[Event]:
    rows = conn.execute(
        "SELECT * FROM events WHERE series = ? ORDER BY date DESC LIMIT ?", (series, limit)
    ).fetchall()
    return [Event.from_row(r) for r in rows]


def history(conn: sqlite3.Connection, event_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM event_history WHERE event_id = ? ORDER BY changed_at", (event_id,)
    ).fetchall()


def delete(conn: sqlite3.Connection, key: str, *, commit: bool = True) -> bool:
    cur = conn.execute("DELETE FROM events WHERE key = ?", (key,))
    conn.execute("DELETE FROM reviewed_lines WHERE kind = 'event' AND ref = ?", (key,))
    if commit:
        conn.commit()
    return cur.rowcount > 0


LINK_KINDS = ("same_as", "replaces", "related")


def link(conn: sqlite3.Connection, from_key: str, to_key: str, kind: str,
         *, written_by: str = "", commit: bool = True) -> bool:
    """Record a stated relationship between two rows. Unknown keys are a no-op."""
    if kind not in LINK_KINDS or from_key == to_key:
        return False
    here, there = get(conn, from_key), get(conn, to_key)
    if here is None or there is None:
        return False
    # `replaces` is directional; the symmetric kinds are stored one way only.
    pair = (here.id, there.id)
    if kind != "replaces":
        pair = tuple(sorted(pair))
    cur = conn.execute(
        "INSERT INTO event_links(from_id, to_id, kind, written_by, created_at)"
        " VALUES(?,?,?,?,?) ON CONFLICT(from_id, to_id, kind) DO NOTHING",
        (*pair, kind, written_by, db.now()))
    if commit:
        conn.commit()
    return cur.rowcount > 0


def linked_pairs(conn: sqlite3.Connection) -> set[frozenset]:
    """Every linked pair, as keys. Kind-agnostic: all three force co-evaluation."""
    out: set[frozenset] = set()
    for row in conn.execute(
            "SELECT a.key AS one, b.key AS two FROM event_links l"
            "  JOIN events a ON a.id = l.from_id JOIN events b ON b.id = l.to_id"):
        out.add(frozenset((row["one"], row["two"])))
    return out


def links_for(conn: sqlite3.Connection, key: str, kind: str = "") -> list[Event]:
    """Rows linked to this one, newest first."""
    found = get(conn, key)
    if found is None:
        return []
    sql = ("SELECT e.* FROM event_links l JOIN events e"
           "  ON e.id = CASE WHEN l.from_id = ? THEN l.to_id ELSE l.from_id END"
           " WHERE (l.from_id = ? OR l.to_id = ?)")
    args: list = [found.id, found.id, found.id]
    if kind:
        sql += " AND l.kind = ?"
        args.append(kind)
    return [Event.from_row(r) for r in conn.execute(sql + " ORDER BY e.date DESC", args)]


def set_status(conn: sqlite3.Connection, key: str, status: str, *,
               written_by: str, evidence_ts: str | dict[str, str] | None = None,
               commit: bool = True) -> bool:
    """Correct one row's status through the normal evidence and authority checks."""
    if status not in ("mentioned", "tentative", "confirmed", "declined", "happened"):
        return False
    found = get(conn, key)
    if found is None or found.status == status:
        return False
    if written_by in ("sweep", "dream:nightly") and evidence_ts is None:
        return False
    _event, verb = upsert(
        conn, {"key": key, "date": found.date, "status": status},
        written_by=written_by, evidence_ts=evidence_ts, match=False, commit=commit)
    return verb == "updated"


def merge(conn: sqlite3.Connection, keep_key: str, drop_key: str,
          *, written_by: str = "live", commit: bool = True) -> Event | None:
    """Pool two duplicate rows onto the survivor and retain merge history."""
    keep, drop = get(conn, keep_key), get(conn, drop_key)
    if keep is None or drop is None or keep.id == drop.id:
        return None

    updates: dict[str, object] = {}
    for name in ("time", "location", "until", "series", "note", "source"):
        if not getattr(keep, name) and getattr(drop, name):
            updates[name] = getattr(drop, name)
    people = sorted(set(keep.participants) | set(drop.participants))
    if people != sorted(keep.participants):
        updates["participants"] = db.jdump(people)
    hosts = sorted(set(keep.hosts) | set(drop.hosts))
    if hosts != sorted(keep.hosts):
        updates["hosts"] = db.jdump(hosts)
    # The more settled status wins: merging a confirmed plan into a mentioned one must
    # not lose the confirmation.
    if STATUSES.index(drop.status) > STATUSES.index(keep.status) and drop.status != "happened":
        updates["status"] = drop.status
    if len(drop.title) > len(keep.title) and keep.title.lower() in drop.title.lower():
        updates["title"] = drop.title

    stamp = db.now()
    if updates:
        sets = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(f"UPDATE events SET {sets}, written_by = ?, updated_at = ? WHERE id = ?",
                     (*updates.values(), written_by, stamp, keep.id))
        for name, value in updates.items():
            conn.execute(
                "INSERT INTO event_history(event_id, field, old_value, new_value, changed_at,"
                " written_by) VALUES(?,?,?,?,?,?)",
                (keep.id, name, str(getattr(keep, name)), str(value), stamp, written_by))
    conn.execute(
        "INSERT INTO event_history(event_id, field, old_value, new_value, changed_at, written_by)"
        " VALUES(?,'merged',?,?,?,?)", (keep.id, drop.key, keep.key, stamp, written_by))

    # A merge removes only a duplicate row, not anything that explained or referred to
    # it. Repoint every durable edge before the cascading delete.
    linked = conn.execute(
        "SELECT from_id, to_id, kind, written_by, created_at FROM event_links"
        " WHERE from_id = ? OR to_id = ?", (drop.id, drop.id)).fetchall()
    conn.execute("DELETE FROM event_links WHERE from_id = ? OR to_id = ?",
                 (drop.id, drop.id))
    for edge in linked:
        source = keep.id if edge["from_id"] == drop.id else edge["from_id"]
        target = keep.id if edge["to_id"] == drop.id else edge["to_id"]
        if source == target:
            continue
        if edge["kind"] != "replaces":
            source, target = sorted((source, target))
        conn.execute(
            "INSERT OR IGNORE INTO event_links"
            " (from_id, to_id, kind, written_by, created_at) VALUES(?,?,?,?,?)",
            (source, target, edge["kind"], edge["written_by"], edge["created_at"]))
    conn.execute("UPDATE event_history SET event_id = ? WHERE event_id = ?",
                 (keep.id, drop.id))
    conn.execute("UPDATE events SET part_of = ? WHERE part_of = ? AND id != ?",
                 (keep.id, drop.id, keep.id))
    conn.execute("UPDATE todos SET event_id = ? WHERE event_id = ?", (keep.id, drop.id))
    conn.execute("UPDATE questions SET about_event = ? WHERE about_event = ?",
                 (keep.id, drop.id))
    conn.execute("UPDATE calendar_items SET event_key = ? WHERE event_key = ?",
                 (keep.key, drop.key))
    conn.execute("UPDATE provenance SET ref = ? WHERE kind = 'event' AND ref = ?",
                 (keep.key, drop.key))
    conn.execute(
        """INSERT OR IGNORE INTO evidence(kind, ref, archive_id, entity, run_id,
                                           generation_id, attached_at)
           SELECT kind, ?, archive_id, entity, run_id, generation_id, attached_at
             FROM evidence WHERE kind = 'event' AND ref = ?""",
        (keep.key, drop.key),
    )
    conn.execute("DELETE FROM evidence WHERE kind = 'event' AND ref = ?", (drop.key,))
    # Review coverage follows the surviving row line by line; the dropped key
    # keeps nothing. Covered lines stay covered, pending lines stay pending —
    # a larger watermark on either side never becomes blanket coverage.
    conn.execute(
        """INSERT OR IGNORE INTO reviewed_lines(kind, ref, archive_id, reviewed_at,
                                                by_run, by_stage)
           SELECT 'event', ?, archive_id, reviewed_at, by_run, by_stage
             FROM reviewed_lines WHERE kind = 'event' AND ref = ?""",
        (keep.key, drop.key))
    conn.execute("DELETE FROM reviewed_lines WHERE kind = 'event' AND ref = ?",
                 (drop.key,))
    conn.execute("DELETE FROM events WHERE id = ?", (drop.id,))
    if commit:
        conn.commit()
    return get_by_id(conn, keep.id)


def search(conn: sqlite3.Connection, needle: str, *, limit: int = 6) -> list[Event]:
    """Rank rows by token overlap and proximity to today."""
    wanted = {w for w in re.split(r"[^a-z0-9]+", (needle or "").lower()) if len(w) > 2}
    if not wanted:
        return []
    scored: list[tuple[float, Event]] = []
    for row in conn.execute("SELECT * FROM events"):
        event = Event.from_row(row)
        haystack = " ".join([event.title, event.location or "", event.series or "",
                             " ".join(event.participants)]).lower()
        words = {w for w in re.split(r"[^a-z0-9]+", haystack) if len(w) > 2}
        hits = len(wanted & words)
        if not hits:
            continue
        # Rank near today first, then by overlap size.
        away = abs((db.parse_date(event.date) - db.today()).days)
        scored.append((hits - min(away, 60) / 400.0, event))
    scored.sort(key=lambda pair: (-pair[0], pair[1].date))
    return [event for _score, event in scored[:limit]]


#: A parent-identifying title word must remain rare as the event corpus grows.
NAME_DF_FRACTION = 0.03
NAME_DF_FLOOR = 3


def _title_words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9']{3,}", (text or "").lower()))


def _naming_words(titles: list[str]) -> set[str]:
    """The words rare enough across `titles` to identify one row."""
    seen: dict[str, int] = {}
    for title in titles:
        for word in _title_words(title):
            seen[word] = seen.get(word, 0) + 1
    ceiling = max(NAME_DF_FLOOR, int(NAME_DF_FRACTION * len(titles)))
    return {word for word, count in seen.items() if count <= ceiling}


def can_contain(event: "Event") -> bool:
    """Return whether an event may act as a container for sub-events."""
    return (event.kind in ("commitment", "observed")
            and (event.subject or "me") == "me")


def link_contained(conn: sqlite3.Connection) -> int:
    """Recompute explicit title-backed containment and clear stale links."""
    rows = [Event.from_row(r) for r in conn.execute(
        "SELECT * FROM events WHERE status != 'declined' ORDER BY date, id")]
    # Include declined titles so the threshold stays stable when plans fall through.
    naming = _naming_words([r["title"] for r in conn.execute("SELECT title FROM events")])
    spans = [e for e in rows if e.until and e.until > e.date]
    endorsed: dict[int, int] = {}
    for parent in spans:
        if not can_contain(parent):
            continue
        stem = _title_words(parent.title) & naming
        if not stem:
            continue
        for child in rows:
            if child.id == parent.id:
                continue
            if not (parent.date <= child.date <= parent.until):
                continue
            # Overlapping spans are siblings, not containers.
            if child.until and child.until > child.date:
                continue
            if not (stem & _title_words(child.title)):
                continue
            endorsed[child.id] = parent.id
    moved = 0
    for child in rows:
        # `None` clears a nesting this rule no longer endorses. Declined rows keep
        # whatever they had.
        want = endorsed.get(child.id)
        if child.part_of == want:
            continue
        conn.execute("UPDATE events SET part_of = ? WHERE id = ?", (want, child.id))
        moved += 1
    if moved:
        conn.commit()
    return moved


def children_of(conn: sqlite3.Connection, event_id: int) -> list["Event"]:
    return [Event.from_row(r) for r in conn.execute(
        "SELECT * FROM events WHERE part_of = ? ORDER BY date, coalesce(time,''), id",
        (event_id,))]


def mark_past_happened(conn: sqlite3.Connection, *,
                       written_by: str = "code:past") -> int:
    """Mark settled past rows happened and record their prior status."""
    stamp, cutoff = db.now(), db.today().isoformat()
    where = (" WHERE coalesce(nullif(until,''), date) < ?"
             " AND status IN ('confirmed','tentative')")
    conn.execute(
        "INSERT INTO event_history(event_id, field, old_value, new_value, changed_at,"
        " written_by) SELECT id, 'status', status, 'happened', ?, ? FROM events" + where,
        (stamp, written_by, cutoff),
    )
    cur = conn.execute(
        "UPDATE events SET status = 'happened', updated_at = ?" + where, (stamp, cutoff))
    conn.commit()
    return cur.rowcount
