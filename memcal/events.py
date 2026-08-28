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
    "status", "participants", "series", "note", "source", "part_of", "rsvp_url",
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
            # Whose row this is, when it is not their. `subject` has been a column since
            # `availability` existed and reached no surface but `detail` ("whose:") and
            # the Later block, so a six-day window of somebody else's free time rendered
            # on their week as an occasion with no owner —
            # `〔E278〕 Sun Aug 16 "League or CS2 gaming" (until Fri Aug 21)` — and was
            # reported as a thing that should not be there. The fact was right and the
            # line never said whose it was.
            #
            # Leading, not trailing, because the tail already ends in "with Alex, Sam"
            # and a bare name down there reads as a companion rather than as the person
            # the row is *about*.
            head = f"{self.subject}: {head}"
        if self.time:
            # A title is told not to carry the fields beside it, and when one does
            # anyway, the brief is where it shows: "Ramen, Thu 8:30pm, 8:30pm". Saying
            # it once is the reader's interest; saying it twice is the model's.
            stamp = friendly_time(self.time)
            if stamp and stamp.lower() not in head.lower():
                head += f", {stamp}"
        if self.until and self.until > self.date:
            head += f" (until {db.parse_date(self.until).strftime('%a %b %-d')})"
        bits.append(head)
        tail = [word for word in (self.plain_state(),) if word]
        if self.rsvp_url:
            # The act, next to the state that makes it worth doing. A declined
            # invitation keeps its link too: a birthday you have said no to is exactly
            # the one you still want to open and send a message through.
            tail.append(f"invite: {_short_url(self.rsvp_url)}")
        if self.join_url:
            # Rendered whole rather than shortened. A link the user has to retype is not a
            # link, and this is the one field whose entire value is being pressable —
            # the row it was built for said "Online" and left them hunting an email.
            tail.append(f"join: {self.join_url}")
        if self.location and not overview:
            tail.append(self.location)
        if self.note and not overview and self.note.casefold() not in head.casefold():
            # Seats, confirmation numbers and other occasion-specific details belong
            # here, not in a completed to-do that disappears from the active brief.
            tail.append(self.note)
        if platform:
            # Out of the *name* and into a clause of its own. Every Partiful export
            # carries "| Partiful" in its title, which reads as part of what the thing
            # is called — and `dream/affinity.py` already has a comment about the word
            # linking two unrelated birthdays because it looked distinctive.
            tail.append(f"via {platform}")
        if self.participants:
            # "who is beer with" cost six tool calls — two archive searches, two wiki
            # reads and a session search — to rebuild a list that was already a column
            # on the row. Who is with you is not a detail of an event; for most rows it
            # is the point of the row, and it costs a handful of characters to say.
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
            # An invitation is a fact about how to act on it. "Could go" is memcal's
            # guess about something a friend merely mentioned, and there is nothing to
            # do about it but ask them; an unanswered invitation is a different thing
            # entirely, because there is a button, and the link can be forwarded to
            # your brother. Saying both with the same three words throws that away.
            return "not replied"
        if self.kind == "opportunity":
            # A settled status outranks `kind`. `kind` records how the occasion came to
            # exist — an open invitation rather than a plan made with someone — and
            # `status` records what the user decided about it, so the decision is the later
            # and better fact. A festival the user had said in writing the user was definitely
            # going to rendered "could go" for days: "why is elements showing up as
            # could go. im DEFINITLY going. thats BAD!"
            #
            # Only a *settled* status overrides. A subscribed holiday feed arrives as
            # `opportunity` + `mentioned` and still reads "could go", which is what it
            # is; nothing else in the live store renders differently for this.
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
    """"https://partiful.com/e/abc123" -> "partiful.com/e/abc123".

    The scheme is noise in a line a person reads, and the brief has a token cap.
    """
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
    # A hundred rows sharing a name and a day is no longer a naming problem, but raising
    # here would be the same lost-pass failure this function exists to prevent.
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
    # A cadence change is a boundary in time, and the rule in force places no day before
    # it. The Monday that already happened under the old schedule and the Wednesday that
    # replaces the first new Tuesday sit two days apart and are not the same occasion in
    # any sense — without this they resolve to one slot, the past absorbs the future, and
    # the cadence change deletes the occurrence it was supposed to create.
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
            # Decided for the row and not for one tier. A candidate the rule places on a
            # *different* scheduled day is a different occurrence of the same recurring
            # thing, and refusing it only at tier 3 leaves it to be swept up at tier 2 on
            # the strength of having the same title — which every occurrence of a series
            # has, by construction. Skipped outright, so nothing weaker can rescue it.
            if not _same_occurrence(conn, series, ev, on):
                continue
            tier = 3
        elif db.slugify(ev.title) == slug:
            tier = 2
        elif (participants and set(participants) & set(ev.participants)
              and _title_overlap(ev.title, title, participants)):
            tier = 1
        # Same day, and one name is the other with more said. Worth tier 2, because two
        # descriptions of one day's occasion at different lengths are one occasion —
        # which side is richer is an accident of who happened to write first.
        #
        # It was gated on the other row being a calendar feed row with nobody on it,
        # because that is the shape the bug was found in: a feed row can never reach
        # tier 1, since `ical` has no participants to write. But nor can a model's own
        # terse re-mention. A group chat's "BBQ" and the Partiful email's "Devon's Block
        # Party BBQ" on one day are neither of them observed, share no guest, and stayed
        # two rows in three of six benchmark trials on two different models.
        #
        # Exact date only: this tier is about the name and has
        # no evidence about the day, so the row it matches is the row whose date already
        # agrees. Widening it would silently hand a wording match the authority the
        # `confidence <= 1` guard in `upsert` exists to withhold.
        absorbs = (tier < 2 and ordinal == target.toordinal()
                   and _title_absorbs(ev.title, title))
        if tier or absorbs:
            seen.append((ev, ordinal, abs(ordinal - target.toordinal()), tier, absorbs))

    # Two rows on one day that the same shorter title could equally name are not one row
    # and not a coin toss. "Superman movie" and "Movie with Riley" both sit on Aug 11, so
    # a bare "Movie" answers to either. Leaving a duplicate is something a person can
    # repair; a correct row quietly renamed and re-timed is not, and nothing downstream
    # would ever report it. So the name stops being evidence — and each row falls back to
    # whatever it was worth without it, which for one of those two is a real tier-1 match
    # on its guest list.
    ambiguous = sum(1 for entry in seen if entry[4]) > 1

    scored: list[tuple[int, int, Event, bool]] = []   # confidence, -distance, row, absorbed
    for ev, ordinal, distance, tier, absorbs in seen:
        absorbed = absorbs and not ambiguous
        confidence = 2 if absorbed else tier
        if not confidence:
            continue
        # A weak match may not reach across today. A plan for next Sunday and something
        # that already happened last week are different occasions however much they have
        # in common, and letting one absorb the other loses the one still ahead of them.
        if confidence < 2 and _crosses_today(ordinal, target.toordinal()):
            continue
        # A weak match against a row that *spans* the target date is containment, not
        # identity. "Breakfast at Elements" on the Saturday of a Friday-to-Sunday
        # festival shares one word and one guest with it and is not it — and absorbing
        # it renamed the festival to the breakfast and lost the whole weekend. That
        # relationship has a column of its own now: see `link_contained`.
        #
        # The tier above cannot reach this case: a span only matches on its own start
        # date, and "Breakfast at Elements" is not a subset of "Elements Music Festival"
        # in either direction. Containment stays a property of the weak tier.
        if confidence < 2 and ev.until and ev.until > ev.date \
                and db.parse_date(ev.date) <= target <= db.parse_date(ev.until):
            continue
        # Nothing that already happened gets re-dated into the future.
        if ev.status == "happened" and target.toordinal() > db.today().toordinal():
            continue
        scored.append((confidence, -distance, ev, absorbed))

    # Within a tier, a name that matches outright beats one that merely contains it, and
    # only then does the nearer date decide. Both arms of tier 2 return the same
    # confidence to the caller — they are equally a claim that this is the same row —
    # but with "Movie" and "Movie with Riley" both on the table, the row actually called
    # "Movie" is the one being talked about, and leaving that to SQLite's row order is
    # not an answer.
    best: tuple[int, int, Event, bool] | None = None
    rank: tuple | None = None
    for candidate in scored:
        here = (candidate[0], not candidate[3], candidate[1])
        if best is None or here > rank:
            best, rank = candidate, here
    return (best[2], best[0]) if best else None


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
#: is going belong to the occasion, and lending one of those would be inventing a fact —
#: a new instance is not `confirmed` because the last one happened (invariant 5).
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
    evidence_ts: str | None = None,
    inferred: tuple[str, ...] = (),
    clear: tuple[str, ...] = (),
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
    # A span that ends before it starts is a slip, never a fact about a trip, and it is
    # not a harmless one: `window`'s predicate is `date <= hi AND coalesce(until, date)
    # >= lo`, so an inverted span excludes the row from **every** window — including
    # the one `brief.py` renders. The row stays real and `memcal_list_days`, `memcal_open`
    # and the web UI all show it, while the agent's actual context never mentions it, on
    # its own date, forever.
    #
    # `dream/apply` has guarded this since a model produced one. The typed writers did
    # not, and both agent surfaces expose `until` — so one fat-fingered
    # `memcal_update(when=…, until=…)` with the ends swapped silently deleted a plan
    # from the brief with no error and no history row saying so. The guard belongs here,
    # where every writer passes, rather than in the one caller that remembered.
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
    # Defaults belong to a *new* row. Writing them into `fields` first meant every
    # partial update also asserted them, so marking an opportunity declined promoted it
    # to a commitment (§10 case 3, exactly backwards) and any update omitting `subject`
    # reassigned someone else's row to the user.
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
        # Only a *new* occurrence inherits. An amendment that happens not to restate a
        # location must not silently import a sibling's — the model was told to change
        # one field and would be changing two, and on a two-member series like
        # `poker-night` that is the most-graded row in the corpus quietly moving house.
        _inherit_from_series(conn, fields)
        key = fields.get("key") or _free_key(
            conn,
            make_key(fields.get("title", ""), fields["date"], fields.get("series"), subject),
        )
        stamp = db.now()
        conn.execute(
            """INSERT INTO events(key, date, until, time, kind, subject, title, location, status,
                                  participants, series, note, source, origin, part_of,
                                  rsvp_url, join_url, instead_of, written_by,
                                  created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key, fields["date"], fields.get("until"),
                fields.get("time"), fields.get("kind") or "commitment", subject,
                fields.get("title", ""), fields.get("location"),
                fields.get("status") or "mentioned",
                db.jdump(fields.get("participants") or []), fields.get("series"),
                fields.get("note"), fields.get("source"),
                # Set here and nowhere else. `origin` is not in MUTABLE, so no later
                # write can reach it — which is the whole point of having it.
                fields.get("source"), fields.get("part_of"), fields.get("rsvp_url"),
                fields.get("join_url"), fields.get("instead_of"),
                written_by, stamp, stamp,
            ),
        )
        if commit:
            conn.commit()
        return get(conn, key), "inserted"  # type: ignore[return-value]

    # Precedence protects a settled row from a cheap pass re-reading old traffic. It is
    # not meant to protect it from news.
    #
    # Told "I'm going to the movie on the 11th", an agent writes the row at `live`
    # precedence. Riley cancels the next day, the nightly pass reads them cancelling —
    # and the row was frozen, because the only escape hatch was "written today" and the
    # agent had written it yesterday. Every row the user ever dictated became permanent
    # the moment the clock rolled over, and the pass reported success while declining
    # every update it was handed.
    #
    # So the test is on the evidence, not the calendar day: a lower-precedence write
    # goes through when what it read is newer than the decision it is revising. Traffic
    # that arrived after the agent wrote the row is the next thing that happened.
    last_write = str(conn.execute(
        "SELECT updated_at FROM events WHERE id = ?", (existing.id,)
    ).fetchone()["updated_at"])
    fresher = bool(evidence_ts and str(evidence_ts) > last_write)
    written_today = last_write[:10] == db.today().isoformat()
    if (precedence(written_by) < precedence(existing.written_by)
            and not written_today and not fresher):
        return existing, "unchanged"

    changes: list[tuple[str, str, str]] = []
    updates: dict[str, object] = {}
    for name in clear:
        # Before the value loop, so `clear=("location",)` beside `location="X"` is a
        # caller contradicting itself and the *set* wins — an emptied field that some
        # other argument immediately refills is the more confusing of the two outcomes.
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
            merged = sorted(set(existing.participants) | set(new or []))
            if merged != sorted(existing.participants):
                updates[name] = db.jdump(merged)
                changes.append((name, db.jdump(existing.participants), db.jdump(merged)))
            continue
        old = getattr(existing, name)
        if new in (None, "") or new == old:
            continue
        if name == "status" and STATUSES.index(new) < STATUSES.index(old) and old == "confirmed":
            continue  # don't walk a confirmed row backwards to 'mentioned'
        if name == "title" and _says_less(new, old):
            continue  # a shorter name for what is already here is not news
        if name in inferred and _claimed_by_another(conn, existing.id, name, written_by):
            # The missing half of the observation guard below. That one protects an
            # observed row's date from inference; this protects everyone else's
            # judgement from a *re*-derivation, which is the same principle pointed the
            # other way.
            #
            # `partiful.event_fields` decides kind and status from "does this feed row
            # have a location", on every scan, with no new information. Day 1: the
            # conversation says the user is definitely going and the row becomes a confirmed
            # commitment. Day 3: the calendar is renamed, every revision changes, the
            # whole snapshot is re-derived from the same absent location, and the row is
            # an opportunity again — "why is elements showing up as could go. im
            # DEFINITLY going. thats BAD!"
            #
            # A re-derivation carrying no new information is not news. So a derived
            # field may *create* a value and may never restate one over somebody else's.
            # The legitimate path stays open in both directions: the inference still
            # fills a field nobody has decided, and a disappearance from the feed is an
            # *observation* (`partiful.reconcile_missing`), passes nothing as inferred,
            # and keeps its full authority to decline the row.
            continue
        if name == "date" and confidence <= 1:
            # The weakest tier of match — overlapping participants and a similar title —
            # is enough to say "these two mentions are the same event" and pool what
            # they know. It is not enough to say "the event moved", because the only
            # evidence it has for the new date is the very mention whose date is in
            # question. A key, a series, or an identical title is a deliberate claim
            # about identity; participant overlap is a guess that happened to be right.
            #
            # One plan discussed in three conversations arrives as three proposals,
            # each dated from its own fragment, and whichever parallel call lands last
            # would silently win. That is how a beer garden negotiated down to "Sunday
            # after 6" in the group thread ended up on Saturday, moved there by a
            # passing "next weekend" in an unrelated thread about trust paperwork.
            continue
        if name in ("date", "until", "time") and _observed(existing) \
                and not _observed_writer(written_by):
            # An iCal or Partiful row *is* the calendar. Its date was read off the event
            # itself, not inferred from someone talking about it, and inference must not
            # overwrite an observation however confident it sounds.
            #
            # This is not hypothetical: a chat proposal moved a subscribed festival from
            # 2026-08-07 to 2026-08-01 and rewrote its source to the friend who had
            # mentioned it, and the user had to correct it by hand. What the friend
            # actually knew was the wrong week; what the calendar knew was the answer.
            # A conflict here is worth a question, not a write — and the row is still
            # free to gain a location, a note, or another guest from the conversation.
            continue
        updates[name] = new
        changes.append((name, str(old), str(new)))

    if not updates:
        return existing, "unchanged"

    # The row keeps the highest authority that ever wrote it, not the last one. Per-field
    # provenance lives in `event_history`, which records the real writer of every change;
    # this column exists only to answer "how settled is this row", and the answer does not
    # get less settled because a nightly pass later adjusted a time on it.
    #
    # It used to be overwritten, which quietly disarmed the guard above: the user dictates
    # a row through the agent, that evening's pass touches one field, the row is stamped
    # `dream:nightly`, and from then on any cheap pass re-reading old traffic can walk it
    # anywhere. The protection lasted until the first pass that agreed with it.
    authority = (written_by if precedence(written_by) >= precedence(existing.written_by)
                 else existing.written_by)
    sets = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(
        f"UPDATE events SET {sets}, written_by = ?, updated_at = ? WHERE id = ?",
        (*updates.values(), authority, db.now(), existing.id),
    )
    for field_name, old, new in changes:
        conn.execute(
            "INSERT INTO event_history(event_id, field, old_value, new_value, changed_at, written_by)"
            " VALUES(?,?,?,?,?,?)",
            (existing.id, field_name, old, new, db.now(), written_by),
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
        event_words = set(re.findall(
            r"[a-z0-9']{3,}", " ".join((event.title, event.location or "",
                                         event.series or "")).casefold()))
        lexical = len(words & event_words)
        if not overlap:
            continue
        # One person in a large room is a weak edge: Quinn being in both a rave chat and
        # a dentist appointment does not make the dentist relevant to the rave chat.
        # Two shared people are structural; one shared person needs the bundle itself to
        # name some part of the occasion. DMs remain allowed to match on their one person.
        if group_entity and len(overlap) == 1 and not lexical:
            continue
        related.append((len(overlap), lexical, temporal(event), event))

    source.sort(key=temporal)
    related.sort(key=lambda item: (-item[0], -item[1], item[2], item[3].key))
    return (source[:max(0, source_limit)],
            [item[3] for item in related[:max(0, related_limit)]])


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
    # The two days before the write, and only the lines that carried a signal — the point
    # is to remind the model what was decided, not to re-send the conversation.
    #
    # `exclude` is the bundle's own lines. Without it this hands back the amendment
    # itself whenever the row was written in the same breath, so the model is shown "I
    # can't do Saturday" as the evidence for what Saturday was.
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


def delete(conn: sqlite3.Connection, key: str) -> bool:
    cur = conn.execute("DELETE FROM events WHERE key = ?", (key,))
    conn.commit()
    return cur.rowcount > 0


def merge(conn: sqlite3.Connection, keep_key: str, drop_key: str,
          *, written_by: str = "live") -> Event | None:
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
    conn.execute("DELETE FROM events WHERE id = ?", (drop.id,))
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
        # Prefer the row the user is most likely looking at: near today, and matching
        # more of what they said.
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
    # The corpus is every title in the store, declined ones included: what a word means
    # in this store does not change because a plan fell through, and a threshold that
    # moved when a row was declined would re-nest rows as a side effect of saying no.
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
            # Its own span, not a point inside this one — two overlapping trips are
            # two trips, and neither is inside the other.
            if child.until and child.until > child.date:
                continue
            if not (stem & _title_words(child.title)):
                continue
            endorsed[child.id] = parent.id
    moved = 0
    for child in rows:
        # `None` for a row nothing endorses, which is what clears a nesting this rule no
        # longer stands behind. Only rows loaded here are judged, so a declined row
        # keeps whatever it had rather than being silently un-nested for being declined.
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


def mark_past_happened(conn: sqlite3.Connection) -> int:
    """Mark settled past rows happened; leave mentioned rows for reconciliation."""
    cur = conn.execute(
        "UPDATE events SET status = 'happened', updated_at = ?"
        " WHERE coalesce(nullif(until,''), date) < ? AND status IN ('confirmed','tentative')",
        (db.now(), db.today().isoformat()),
    )
    conn.commit()
    return cur.rowcount
