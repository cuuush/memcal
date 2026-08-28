"""Store recurring schedules as rules, separate from their event occurrences.

Rules carry cadence, location, links, and history. Occurrences carry the date, time, and
attendance state; ``roll_forward`` creates occurrences without inventing judgements.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta

from . import db

#: How often, as a closed vocabulary. Open-ended RRULE grammar is not the requirement —
#: what arrives in an email is "weekly", "every other week", "monthly". Anything the
#: vocabulary cannot say is `None`, which means the series is real and its cadence is not
#: known, and `roll_forward` declines to project rather than guessing at one.
CADENCES = ("weekly", "fortnightly", "monthly")

#: Fields a later write may change. `slug` is the identity and `created_at` is history.
MUTABLE = (
    "title", "cadence", "weekday", "day_of_month", "time", "location", "join_url",
    "effective_on", "ends_on", "status", "source",
)

#: What a rule lends to an occurrence it generates, and it is deliberately the same list
#: `events.SERIES_QUALITIES` lends between instances. Adding `status` here would make
#: every future Tuesday `confirmed` on the strength of last Tuesday having happened.
QUALITIES = ("location", "join_url")

STATUSES = ("active", "ended")

_WEEKDAYS = ("Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays",
             "Saturdays", "Sundays")


@dataclass
class Series:
    slug: str
    title: str
    cadence: str | None = None
    weekday: int | None = None
    day_of_month: int | None = None
    time: str | None = None
    location: str | None = None
    join_url: str | None = None
    effective_on: str = ""
    ends_on: str | None = None
    status: str = "active"
    source: str | None = None
    origin: str | None = None
    written_by: str = "cli"
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Series":
        return cls(**{f: row[f] for f in cls.__dataclass_fields__ if f in row.keys()})

    @property
    def phrase(self) -> str:
        """"Tuesdays at 13:00" — the rule as a person says it.

        On the dataclass rather than in each caller, because the brief, the apply log,
        both tool surfaces and the CLI all have to say the same thing about one rule, and
        four renderings of one fact is how a store starts disagreeing with itself.
        """
        if self.cadence == "monthly" and self.day_of_month:
            stem = f"the {self.day_of_month} of each month"
        elif self.weekday is not None and 0 <= self.weekday <= 6:
            stem = _WEEKDAYS[self.weekday]
            if self.cadence == "fortnightly":
                stem = f"every other {stem[:-1]}"
        else:
            stem = self.cadence or "on no fixed schedule"
        # *"if theres no time, we should say so. time tbd/time unknown"* — and this is
        # the one place where a blank genuinely means "unknown" rather than "all day". A
        # birthday with no time is not missing anything; a standing appointment always
        # happens *at* a time, so a rule without one has a hole in it, and rendering that
        # as silence is the same class of failure as a row whose date came from nowhere:
        # the absence is invisible, so nobody asks.
        said = f"{stem} at {self.time}" if self.time else f"{stem}, time TBD"
        return said if self.status == "active" else f"{said} (ended)"

    @property
    def projectable(self) -> bool:
        """Whether this rule can say when the next one is.

        A series with no cadence is not broken — "we meet about monthly" is a true thing
        somebody said, and it is worth holding as a page and a set of qualities. It is
        simply not a schedule, and inventing a weekday for it would be invariant 5.
        """
        if self.status != "active" or self.cadence not in CADENCES:
            return False
        if self.cadence == "monthly":
            return self.day_of_month is not None
        return self.weekday is not None


def get(conn: sqlite3.Connection, slug: str) -> Series | None:
    row = conn.execute("SELECT * FROM series WHERE slug = ?", (db.slugify(slug or ""),)
                       ).fetchone()
    return Series.from_row(row) if row else None


def all_active(conn: sqlite3.Connection) -> list[Series]:
    rows = conn.execute(
        "SELECT * FROM series WHERE status = 'active' ORDER BY slug").fetchall()
    return [Series.from_row(row) for row in rows]


def _clean(fields: dict) -> dict:
    """Normalise what a caller handed us, refusing rather than coercing nonsense."""
    out = dict(fields)
    if out.get("cadence") is not None:
        cadence = str(out["cadence"]).strip().lower()
        # "biweekly" means both things to different people and neither reliably. The
        # vocabulary answers to the unambiguous word and to nothing else.
        cadence = {"every other week": "fortnightly", "every two weeks": "fortnightly",
                   "every week": "weekly", "every month": "monthly"}.get(cadence, cadence)
        out["cadence"] = cadence if cadence in CADENCES else None
    for name in ("weekday", "day_of_month"):
        if out.get(name) is not None:
            try:
                out[name] = int(out[name])
            except (TypeError, ValueError):
                out[name] = None
    if out.get("weekday") is not None and not 0 <= out["weekday"] <= 6:
        out["weekday"] = None
    if out.get("day_of_month") is not None and not 1 <= out["day_of_month"] <= 31:
        out["day_of_month"] = None
    for name in ("effective_on", "ends_on"):
        if out.get(name):
            out[name] = db.parse_date(out[name]).isoformat()
    if out.get("status") not in STATUSES and "status" in out:
        out["status"] = "active"
    return out


def upsert(conn: sqlite3.Connection, fields: dict, *, written_by: str = "cli",
           commit: bool = True) -> tuple[Series, str]:
    """Insert or change one rule."""
    fields = _clean({k: v for k, v in fields.items()
                     if k in MUTABLE or k in ("slug", "origin")})
    slug = db.slugify(str(fields.pop("slug", "") or fields.get("title", "")))
    if not slug:
        raise ValueError("series needs a slug or a title")
    stamp = db.now()
    existing = get(conn, slug)
    if existing is None:
        conn.execute(
            """INSERT INTO series(slug, title, cadence, weekday, day_of_month, time,
                                  location, join_url, effective_on, ends_on, status,
                                  source, origin, written_by, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (slug, fields.get("title") or slug.replace("-", " ").title(),
             fields.get("cadence"), fields.get("weekday"), fields.get("day_of_month"),
             fields.get("time"), fields.get("location"), fields.get("join_url"),
             fields.get("effective_on") or db.today().isoformat(),
             fields.get("ends_on"), fields.get("status") or "active",
             fields.get("source"),
             # Set once, never updated, for the same reason `events.origin` is.
             fields.get("origin") or fields.get("source"),
             written_by, stamp, stamp),
        )
        if commit:
            conn.commit()
        return get(conn, slug), "inserted"  # type: ignore[return-value]

    changes = []
    for name in MUTABLE:
        if name not in fields:
            continue
        before, after = getattr(existing, name), fields[name]
        if after is None or str(before or "") == str(after or ""):
            continue
        changes.append((name, before, after))
    if not changes:
        return existing, "unchanged"
    for name, before, after in changes:
        conn.execute(f"UPDATE series SET {name} = ? WHERE slug = ?", (after, slug))
        conn.execute(
            """INSERT INTO series_history(slug, field, old_value, new_value,
                                          changed_at, written_by)
               VALUES(?,?,?,?,?,?)""",
            (slug, name, None if before is None else str(before), str(after),
             stamp, written_by),
        )
    conn.execute("UPDATE series SET updated_at = ?, written_by = ? WHERE slug = ?",
                 (stamp, written_by, slug))
    if commit:
        conn.commit()
    return get(conn, slug), "updated"  # type: ignore[return-value]


def end(conn: sqlite3.Connection, slug: str, *, on: str | None = None,
        written_by: str = "cli", commit: bool = True) -> Series | None:
    """The user has stopped going. The rule stops applying; nothing is removed.

    Deliberately not a delete. The rows it already generated are true, the page is worth
    keeping, and "this used to be every Monday" is the answer to a question somebody will
    ask later.
    """
    if get(conn, slug) is None:
        return None
    updated, _ = upsert(conn, {"slug": slug, "status": "ended",
                               "ends_on": on or db.today().isoformat()},
                        written_by=written_by, commit=commit)
    return updated


def _step(cadence: str) -> timedelta:
    return timedelta(days=14) if cadence == "fortnightly" else timedelta(days=7)


def occurrences(conn_or_series, start: str | date, end_at: str | date,
                *, series: Series | None = None) -> list[date]:
    """Every day the rule lands on within [start, end_at], inclusive.

    Pure projection with no reference to what is in `events` — the caller joins the two,
    because "what the rule says" and "what the store holds" being separately answerable
    is the whole point of having a rule at all.
    """
    rule = series if series is not None else conn_or_series
    if not isinstance(rule, Series) or not rule.projectable:
        return []
    first = db.parse_date(start)
    last = db.parse_date(end_at)
    if rule.effective_on:
        first = max(first, db.parse_date(rule.effective_on))
    if rule.ends_on:
        last = min(last, db.parse_date(rule.ends_on))
    if first > last:
        return []

    out: list[date] = []
    if rule.cadence == "monthly":
        day = rule.day_of_month or 1
        cursor = first.replace(day=1)
        while cursor <= last:
            try:
                landed = cursor.replace(day=day)
            except ValueError:
                # A rule that says "the 31st" simply does not land in February. Clamping
                # it to the 28th would invent a meeting nobody agreed to.
                landed = None
            if landed is not None and first <= landed <= last:
                out.append(landed)
            cursor = (cursor.replace(day=28) + timedelta(days=7)).replace(day=1)
        return out

    # `effective_on` is the anchor for a fortnightly rule, not merely a lower bound:
    # "every other Tuesday" is only meaningful relative to a Tuesday somebody named.
    anchor = db.parse_date(rule.effective_on) if rule.effective_on else first
    anchor += timedelta(days=(rule.weekday - anchor.weekday()) % 7)
    step = _step(rule.cadence or "weekly")
    cursor = anchor
    if cursor < first:
        skipped = (first - cursor) // step
        cursor += step * skipped
        while cursor < first:
            cursor += step
    while cursor <= last:
        out.append(cursor)
        cursor += step
    return out


def next_on(conn: sqlite3.Connection, rule: Series, *, after: str | date | None = None,
            horizon_days: int = 400) -> date | None:
    """The next day this rule lands on, strictly after `after` (default today - 1)."""
    start = db.parse_date(after) + timedelta(days=1) if after else db.today()
    landed = occurrences(None, start, start + timedelta(days=horizon_days), series=rule)
    return landed[0] if landed else None


#: How many occurrences of a rule exist as rows at any moment. One, because that is the
#: policy the prompt has always stated and the brief has always assumed — "a recurring
#: thing is one row for the next occurrence". The *rule* is what says forever now, so
#: materialising a year of Tuesdays would add nothing a reader wants and would put fifty
#: rows in front of every matcher in the store.
MATERIALIZE_AHEAD = 1

#: A row this module projected from a rule, as opposed to one a person or a source
#: observed. Only a projection may be un-projected: if the rule changes, memcal's own
#: guess about next Monday is withdrawn silently, and a Monday that *Calendar.app* or an
#: email put there is left alone and asked about (invariant 5).
WRITER = "series"


def _projection(rule: Series, on: date) -> dict:
    fields = {
        "title": rule.title,
        "date": on.isoformat(),
        "time": rule.time,
        "series": rule.slug,
        # The occasion's own judgement, never the rule's. A standing appointment is a
        # commitment in kind; whether the user is going *this* week is not settled by the fact
        # that a schedule exists, and `status` is the column that would be lying.
        "kind": "commitment",
        "status": "mentioned",
        "source": f"series:{rule.slug}",
    }
    for name in QUALITIES:
        if getattr(rule, name, None):
            fields[name] = getattr(rule, name)
    return fields


def roll_forward(conn: sqlite3.Connection, *, slug: str | None = None,
                 ahead: int = MATERIALIZE_AHEAD, horizon_days: int = 400,
                 written_by: str = WRITER) -> list[str]:
    """Make the store's occurrences agree with the rules that generate them."""
    from . import events, todos  # circular at module scope: events inherits from series

    log: list[str] = []
    rules = [get(conn, slug)] if slug else all_active(conn)
    for rule in [r for r in rules if r is not None]:
        if not rule.projectable:
            continue
        start = db.today()
        if rule.effective_on:
            start = max(start, db.parse_date(rule.effective_on))
        scheduled = occurrences(None, start, start + timedelta(days=horizon_days),
                                series=rule)
        skip = covered(conn, rule.slug)
        wanted = [d for d in scheduled if d.isoformat() not in skip][:max(0, ahead)]
        landing = {d.isoformat() for d in scheduled}

        # The store already knows when the next one is, so the rule has nothing to add.
        # `ahead` is *one row for the next occurrence*, and one already exists — filling
        # in the slots between here and it is the rule inventing meetings nobody
        # mentioned. It did exactly that: told a physio slot was weekly on Wednesdays and
        # that the appointment was Wednesday the 12th, it also wrote a Wednesday the 5th,
        # because the 5th is the first Wednesday the rule lands on. A projection may
        # answer "when is the next one" and may never contradict a source that said.
        # Only rows the rule actually accounts for count: on a day it lands on, or an
        # exception standing in for one. A leftover Monday from the cadence that just
        # changed is not the next occurrence — it is precisely the thing
        # `stale_occurrences` exists to raise, and letting it satisfy the slot would mean
        # a schedule change silently produced no Tuesday at all.
        upcoming = sum(1 for row in conn.execute(
            "SELECT date, instead_of FROM events"
            "  WHERE series = ? AND date >= ? AND status <> 'declined'",
            (rule.slug, db.today().isoformat()))
            if row["date"] in landing or (row["instead_of"] or "") in landing)
        # **Refreshing is not the same job as creating**, and conflating them cost the
        # first live `--at 13:00`: the rule said "Tuesdays at 13:00" while memcal's own
        # already-written Tuesday sat there blank, one line below it in the brief,
        # because the store had an upcoming row and so the whole loop was skipped. A
        # projection is derived from the rule and tracks it for as long as it stays a
        # projection — any other writer takes `written_by` off `series`, and from then on
        # the row is theirs and this leaves it alone.
        for row in conn.execute(
                "SELECT key, date FROM events"
                "  WHERE series = ? AND date >= ? AND written_by = ?",
                (rule.slug, db.today().isoformat(), WRITER)):
            if row["date"] in landing:
                events.upsert(conn,
                              {**_projection(rule, db.parse_date(row["date"])),
                               "key": row["key"]},
                              written_by=written_by, match=False)

        for on in (wanted if upcoming < max(0, ahead) else []):
            if conn.execute("SELECT 1 FROM events WHERE series = ? AND date = ?",
                            (rule.slug, on.isoformat())).fetchone():
                continue
            event, verb = events.upsert(conn, _projection(rule, on),
                                        written_by=written_by, match=False)
            log.append(f"series  {verb} {rule.title} on {on.isoformat()}")
            # A rule that knows the day and not the hour is projecting a row with a hole
            # in it, and a blank time renders as nothing at all — indistinguishable from
            # an all-day thing that never had one. *"If there's no time, we should say
            # so."* The rendering says TBD; this is the half that gets it filled in,
            # through the one choke point every asker goes through, keyed so it is asked
            # once rather than every night.
            if not rule.time:
                todos.ask(conn, f"What time is {rule.title} on "
                                f"{_WEEKDAYS[rule.weekday][:-1]}s now?"
                          if rule.weekday is not None else
                          f"What time is {rule.title}?",
                          key=f"q:series-time:{rule.slug}",
                          about_event=event.id, written_by=written_by)

        # A row memcal projected under the previous rule, standing on a day the rule in
        # force does not land on. `written_by` is the whole test; see the docstring.
        stale = conn.execute(
            """SELECT key, date FROM events
                WHERE series = ? AND date >= ? AND instead_of IS NULL
                  AND written_by = ?""",
            (rule.slug, db.today().isoformat(), WRITER)).fetchall()
        for row in stale:
            if row["date"] in landing:
                continue
            events.delete(conn, row["key"])
            log.append(f"series  withdrew the projected {rule.title} on {row['date']} — "
                       f"the rule no longer lands there")
    conn.commit()
    return log


def stale_occurrences(conn: sqlite3.Connection, slug: str) -> list[sqlite3.Row]:
    """Future rows of a series that its rule does not account for and cannot withdraw.

    These are the observations — a Monday still sitting on Calendar.app after the
    tutor moved everything to Tuesday. memcal is not entitled to delete them and is
    not entitled to assume they are wrong, so it asks. Returned rather than acted on,
    because who asks and where differs between the nightly pass and a live write.
    """
    rule = get(conn, slug)
    if rule is None or not rule.projectable:
        return []
    start = db.today()
    landing = {d.isoformat() for d in
               occurrences(None, start, start + timedelta(days=400), series=rule)}
    rows = conn.execute(
        """SELECT * FROM events
            WHERE series = ? AND date >= ? AND instead_of IS NULL
              AND written_by <> ? AND status NOT IN ('declined', 'happened')
            ORDER BY date""",
        (db.slugify(slug or ""), start.isoformat(), WRITER)).fetchall()
    return [row for row in rows if row["date"] not in landing]


def slot_for(rule: Series, on: str | date, instead_of: str | None = None) -> str | None:
    """Which scheduled day an occurrence belongs to, or None if the rule cannot say."""
    if not rule.projectable:
        return None
    if instead_of:
        return db.parse_date(instead_of).isoformat()
    day = db.parse_date(on)
    reach = 14 if rule.cadence == "fortnightly" else (16 if rule.cadence == "monthly" else 7)
    landing = occurrences(None, day - timedelta(days=reach), day + timedelta(days=reach),
                          series=rule)
    if not landing:
        return None
    if day in landing:
        return day.isoformat()
    # Half a cycle either way: a Wednesday belongs to the Tuesday it is nearest, and a
    # day equidistant between two scheduled ones belongs to neither.
    near = sorted(landing, key=lambda d: (abs((d - day).days), d))
    best = near[0]
    if abs((best - day).days) > reach // 2:
        return None
    if len(near) > 1 and abs((near[1] - day).days) == abs((best - day).days):
        return None
    return best.isoformat()


def covered(conn: sqlite3.Connection, slug: str) -> set[str]:
    """Scheduled days some row already stands in for.

    An exception names the day it replaces, so this is a lookup rather than a judgement:
    the Tuesday that a Wednesday row was written *instead of* must not be materialised
    again a minute later by the same rule that produced it.
    """
    rows = conn.execute(
        "SELECT instead_of FROM events WHERE series = ? AND instead_of IS NOT NULL",
        (db.slugify(slug or ""),)).fetchall()
    return {str(row["instead_of"]) for row in rows if row["instead_of"]}
