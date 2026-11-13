"""Store recurring schedules as rules, separate from their event occurrences.

Rules carry cadence, location, links, and history. Occurrences carry the date, time, and
attendance state; ``roll_forward`` creates occurrences without inventing judgements.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

from . import db

#: Structured recurrence units; multiple weekdays and an interval customize them.
#: Legacy fortnightly rows remain readable as every-two-week schedules.
CADENCES = ("daily", "weekly", "fortnightly", "monthly")
PUBLIC_CADENCES = ("daily", "weekly", "every_two_weeks", "monthly")

#: Fields a later write may change. `slug` is the identity and `created_at` is history.
MUTABLE = (
    "title", "cadence", "weekday", "weekdays", "interval", "day_of_month", "time", "location", "join_url",
    "effective_on", "ends_on", "status", "source",
)

#: What a rule lends to an occurrence it generates, and it is deliberately the same list
#: `events.SERIES_QUALITIES` lends between instances. Adding `status` here would make
#: every future Tuesday `confirmed` on the strength of last Tuesday having happened.
QUALITIES = ("location", "join_url")

STATUSES = ("active", "paused", "ended")

_WEEKDAYS = ("Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays",
             "Saturdays", "Sundays")


@dataclass
class Series:
    slug: str
    title: str
    cadence: str | None = None
    weekday: int | None = None
    weekdays: list[int] = field(default_factory=list)
    interval: int = 1
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
    changes: list[tuple[str, dict]] = field(default_factory=list, repr=False, compare=False)
    segment_on: str = ''
    segment_until: str = ''

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Series":
        values = {f: row[f] for f in cls.__dataclass_fields__ if f in row.keys()}
        values['weekdays'] = db.jload(values.get('weekdays'), [])
        return cls(**values)

    @property
    def days(self) -> list[int]:
        return self.weekdays or ([self.weekday] if self.weekday is not None else [])

    @property
    def every(self) -> int:
        return 2 if self.cadence == 'fortnightly' else self.interval

    @property
    def phrase(self) -> str:
        """"Tuesdays at 13:00" — the rule as a person says it."""
        if self.cadence == "daily":
            stem = 'daily' if self.every == 1 else f'every {self.every} days'
        elif self.cadence == "monthly" and self.day_of_month:
            stem = (f"the {self.day_of_month} of each month" if self.every == 1 else
                    f"the {self.day_of_month} every {self.every} months")
        elif self.days:
            stem = ', '.join(_WEEKDAYS[day] for day in self.days)
            if self.every > 1:
                stem = f"every {self.every} weeks on " + ', '.join(_WEEKDAYS[d][:-1] for d in self.days)
        else:
            stem = self.cadence or "on no fixed schedule"
        # A rule without a time has a hole in it; render the gap instead of silence.
        said = f"{stem} at {self.time}" if self.time else f"{stem}, time TBD"
        return said if self.status == "active" else f"{said} ({self.status})"

    @property
    def projectable(self) -> bool:
        """Whether this rule can say when the next one is."""
        if self.status != "active" or self.cadence not in CADENCES:
            return False
        if self.cadence == "monthly":
            return self.day_of_month is not None
        return self.cadence == 'daily' or bool(self.days)


def allows_date(conn: sqlite3.Connection, slug: str, on: str) -> bool:
    """A retired schedule cannot lend its identity or access details to a new plan."""
    rule = get(conn, slug, on=on)
    if rule is None:
        return True  # Legacy membership can predate declared schedule rules.
    if rule.status == 'paused' or (rule.status == 'ended' and not rule.ends_on):
        return False
    return (not rule.effective_on or on >= rule.effective_on) and (
        not rule.ends_on or on <= rule.ends_on)


def get(conn: sqlite3.Connection, slug: str, *, on: str | None = None) -> Series | None:
    row = conn.execute("SELECT * FROM series WHERE slug = ?", (db.slugify(slug or ""),)
                       ).fetchone()
    if not row:
        return None
    rule = Series.from_row(row)
    rule.changes = [(r['on_date'], db.jload(r['patch'], {})) for r in conn.execute(
        'SELECT on_date, patch FROM series_changes WHERE slug=? ORDER BY on_date', (rule.slug,))]
    for when, patch in rule.changes:
        if when <= (on or db.today().isoformat()):
            for name, value in patch.items():
                if name in MUTABLE:
                    setattr(rule, name, value)
    return rule


def all_active(conn: sqlite3.Connection) -> list[Series]:
    rules = [get(conn, row['slug']) for row in conn.execute('SELECT slug FROM series ORDER BY slug')]
    return [rule for rule in rules if rule and (rule.status == 'active' or rule.changes)]


def _clean(fields: dict) -> dict:
    """Normalise what a caller handed us, refusing rather than coercing nonsense."""
    out = dict(fields)
    if out.get("cadence") is not None:
        cadence = str(out["cadence"]).strip().lower()
        # Accept only unambiguous cadence words.
        cadence = {"every other week": "fortnightly", "every two weeks": "fortnightly",
                   "every_two_weeks": "fortnightly",
                   "every week": "weekly", "every month": "monthly"}.get(cadence, cadence)
        out["cadence"] = cadence if cadence in CADENCES else None
    if out.get('weekdays') is not None:
        days = db.jload(out['weekdays'], []) if isinstance(out['weekdays'], str) else out['weekdays']
        if not isinstance(days, list) or any(type(d) is not int or not 0 <= d <= 6 for d in days):
            raise ValueError('weekdays must be a list of weekdays, Monday=0 through Sunday=6')
        out['weekdays'] = sorted(set(days))
        if days:
            out['weekday'] = out['weekdays'][0]
    elif out.get('weekday') is not None:
        out['weekdays'] = []
    if out.get('interval') is not None:
        if type(out['interval']) is not int or not 1 <= out['interval'] <= 366:
            raise ValueError('interval must be a whole number from 1 to 366')
    if out.get('cadence') in ('daily', 'monthly'):
        out['weekdays'] = []
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
    if out.get('status') is None:
        out.pop('status', None)
    if out.get("status") not in STATUSES and "status" in out:
        out["status"] = "active"
    return out


def upsert(conn: sqlite3.Connection, fields: dict, *, written_by: str = "cli",
           commit: bool = True, evidence_ts: str | None = None,
           change_on: str | None = None) -> tuple[Series, str]:
    """Insert or change one rule."""
    fields = _clean({k: v for k, v in fields.items()
                     if k in MUTABLE or k in ("slug", "origin")})
    slug = db.slugify(str(fields.pop("slug", "") or fields.get("title", "")))
    if not slug:
        raise ValueError("series needs a slug or a title")
    stamp = db.now()
    applies_on = change_on or fields.get('effective_on') or db.today().isoformat()
    existing = get(conn, slug, on=applies_on)
    if existing is None:
        conn.execute(
            """INSERT INTO series(slug, title, cadence, weekday, weekdays, interval, day_of_month, time,
                                  location, join_url, effective_on, ends_on, status,
                                  source, origin, written_by, created_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (slug, fields.get("title") or slug.replace("-", " ").title(),
             fields.get("cadence"), fields.get("weekday"), db.jdump(fields.get('weekdays') or []),
             fields.get('interval') or 1, fields.get("day_of_month"),
             fields.get("time"), fields.get("location"), fields.get("join_url"),
             fields.get("effective_on") or db.today().isoformat(),
             fields.get("ends_on"), fields.get("status") or "active",
             fields.get("source"),
             # Set once, never updated, for the same reason `events.origin` is.
             fields.get("origin") or fields.get("source"),
             written_by, stamp, stamp),
        )
        for name in MUTABLE:
            value = fields.get(name)
            if value is not None:
                conn.execute("INSERT INTO series_history(slug, field, old_value, new_value, changed_at, evidence_ts, written_by) VALUES(?,?,NULL,?,?,?,?)", (slug, name, str(value), stamp, evidence_ts or stamp, written_by))
        if commit:
            conn.commit()
        return get(conn, slug, on=fields.get('effective_on')), "inserted"  # type: ignore[return-value]

    changes = []
    for name in MUTABLE:
        if name not in fields:
            continue
        before, after = getattr(existing, name), fields[name]
        if after is None or str(before or "") == str(after or ""):
            continue
        version = conn.execute("SELECT evidence_ts, changed_at FROM series_history WHERE slug = ? AND field = ? ORDER BY id DESC LIMIT 1", (slug, name)).fetchone()
        floor = (version["evidence_ts"] or version["changed_at"]) if version else existing.created_at
        if evidence_ts and db.parse_ts(evidence_ts, strict=True) <= db.parse_ts(floor, strict=True):
            continue
        changes.append((name, before, after))
    if not changes:
        return existing, "unchanged"
    # Dated patches preserve the old schedule until a future change takes effect.
    conn.execute('INSERT OR IGNORE INTO series_changes(slug,on_date,patch) VALUES(?,?,?)',
                 (slug, '0001-01-01', db.jdump({name: getattr(existing, name) for name in MUTABLE})))
    on_date = applies_on
    old_patch = conn.execute('SELECT patch FROM series_changes WHERE slug=? AND on_date=?',
                             (slug, on_date)).fetchone()
    patch = db.jload(old_patch['patch'], {}) if old_patch else {}
    patch.update({name: after for name, _, after in changes})
    if any(name == 'status' and after == 'ended' for name, _, after in changes):
        # Stopping a series supersedes previously planned resumes. A later,
        # explicit resume remains possible through the same dated write path.
        for future in conn.execute('SELECT on_date, patch FROM series_changes WHERE slug=? AND on_date>?',
                                   (slug, on_date)).fetchall():
            later = db.jload(future['patch'], {})
            if later.get('status') == 'active':
                later.pop('status')
                later.pop('ends_on', None)
                conn.execute('UPDATE series_changes SET patch=? WHERE slug=? AND on_date=?',
                             (db.jdump(later), slug, future['on_date']))
    conn.execute('INSERT INTO series_changes(slug,on_date,patch) VALUES(?,?,?) '
                 'ON CONFLICT(slug,on_date) DO UPDATE SET patch=excluded.patch',
                 (slug, on_date, db.jdump(patch)))
    for name, before, after in changes:
        conn.execute(
            """INSERT INTO series_history(slug, field, old_value, new_value,
                                          changed_at, evidence_ts, written_by)
               VALUES(?,?,?,?,?,?,?)""",
            (slug, name, None if before is None else str(before), str(after),
             stamp, evidence_ts or stamp, written_by),
        )
    conn.execute("UPDATE series SET updated_at = ?, written_by = ? WHERE slug = ?",
                 (stamp, written_by, slug))
    if commit:
        conn.commit()
    return get(conn, slug, on=on_date), "updated"  # type: ignore[return-value]


def end(conn: sqlite3.Connection, slug: str, *, on: str | None = None,
        written_by: str = "cli", commit: bool = True,
        evidence_ts: str | None = None) -> Series | None:
    """The user has stopped going. The rule stops applying; nothing is removed."""
    if get(conn, slug) is None:
        return None
    updated, _ = upsert(conn, {"slug": slug, "status": "ended",
                               "ends_on": on or db.today().isoformat()},
                        written_by=written_by, evidence_ts=evidence_ts, commit=commit)
    return updated


def segments(rule: Series, start: str | date, end_at: str | date | None = None) -> list[Series]:
    """Continuous schedule versions, preserving anchors across location edits."""
    first = db.parse_date(start).isoformat()
    last = db.parse_date(end_at).isoformat() if end_at else '9999-12-31'
    if not rule.changes:
        return [replace(rule, changes=[])]
    current = replace(rule, changes=[])
    out = []
    began = '0001-01-01'
    for when, patch in rule.changes:
        if when != '0001-01-01':
            until = (db.parse_date(when) - timedelta(days=1)).isoformat()
            if until >= first and began <= last:
                out.append(replace(current, segment_on=began, segment_until=until))
        current = replace(current, **{name: value for name, value in patch.items() if name in MUTABLE})
        began = when
    if began <= last:
        out.append(replace(current, segment_on=began))
    return out


def occurrences(conn_or_series, start: str | date, end_at: str | date,
                *, series: Series | None = None) -> list[date]:
    """Every day the rule lands on within [start, end_at], inclusive.

    Pure projection; the caller joins rules to stored rows.
    """
    rule = series if series is not None else conn_or_series
    if isinstance(rule, Series) and rule.changes:
        return sorted({day for part in segments(rule, start, end_at)
                       for day in occurrences(None, start, end_at, series=part)})
    if not isinstance(rule, Series) or not rule.projectable:
        return []
    first = db.parse_date(start)
    last = db.parse_date(end_at)
    if rule.effective_on:
        first = max(first, db.parse_date(rule.effective_on))
    if rule.ends_on:
        last = min(last, db.parse_date(rule.ends_on))
    if rule.segment_on:
        first = max(first, db.parse_date(rule.segment_on))
    if rule.segment_until:
        last = min(last, db.parse_date(rule.segment_until))
    if first > last:
        return []

    out: list[date] = []
    anchor = db.parse_date(rule.effective_on) if rule.effective_on else first
    if rule.cadence == 'daily':
        cursor = first + timedelta(days=(-(first - anchor).days) % rule.every)
        while cursor <= last:
            out.append(cursor)
            cursor += timedelta(days=rule.every)
        return out
    if rule.cadence == "monthly":
        day = rule.day_of_month or 1
        cursor = first.replace(day=1)
        while cursor <= last:
            try:
                landed = cursor.replace(day=day)
            except ValueError:
                # Skip months without that day rather than clamping.
                landed = None
            months = (cursor.year - anchor.year) * 12 + cursor.month - anchor.month
            if landed is not None and first <= landed <= last and months % rule.every == 0:
                out.append(landed)
            cursor = (cursor.replace(day=28) + timedelta(days=7)).replace(day=1)
        return out

    # `effective_on` anchors a fortnightly rule, not just bounds it.
    # Anchor on the week of the first matching day, preserving old every-other-week rules.
    anchor += timedelta(days=min((day - anchor.weekday()) % 7 for day in rule.days))
    week_zero = anchor - timedelta(days=anchor.weekday())
    cursor = first
    while cursor <= last:
        week = (cursor - week_zero).days // 7
        if cursor.weekday() in rule.days and week % rule.every == 0:
            out.append(cursor)
        cursor += timedelta(days=1)
    return out


def next_on(conn: sqlite3.Connection, rule: Series, *, after: str | date | None = None,
            horizon_days: int = 400) -> date | None:
    """The next day this rule lands on, strictly after `after` (default today - 1)."""
    start = db.parse_date(after) + timedelta(days=1) if after else db.today()
    landed = occurrences(None, start, start + timedelta(days=horizon_days), series=rule)
    return landed[0] if landed else None


def materialize_between(conn: sqlite3.Connection, start: str | date, end_at: str | date) -> None:
    """Give requested calendar slots stable handles without creating extra rules."""
    from . import events
    first, last = db.parse_date(start), db.parse_date(end_at)
    if (last - first).days > 3660:
        raise ValueError('request at most ten years at a time')
    for row in conn.execute('SELECT slug FROM series').fetchall():
        rule = get(conn, row['slug'])
        skip = covered(conn, rule.slug)
        for on in occurrences(None, first, last, series=rule):
            if on.isoformat() in skip:
                continue
            if conn.execute('SELECT 1 FROM events WHERE series=? AND date=?', (rule.slug, on.isoformat())).fetchone():
                continue
            events.upsert(conn, _projection(rule, on), written_by=WRITER, match=False)


#: Projected occurrences kept per rule. The rule holds the long-term schedule.
MATERIALIZE_AHEAD = 1

#: Rows this module projected, as opposed to rows a person or source observed.
#: Only projections may be withdrawn when the rule changes.
WRITER = "series"


def _projection(rule: Series, on: date) -> dict:
    if rule.changes:
        rule = segments(rule, on, on)[0]
    fields = {
        "title": rule.title,
        "date": on.isoformat(),
        "time": rule.time,
        "series": rule.slug,
        # New projections stay mentioned; the rule never settles attendance.
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
    rules = [get(conn, slug)] if slug else [
        get(conn, row['slug']) for row in conn.execute("SELECT slug FROM series ORDER BY slug")]
    for rule in [r for r in rules if r is not None]:
        scheduled = occurrences(None, db.today(), db.today() + timedelta(days=horizon_days), series=rule)
        if not scheduled:
            for row in conn.execute("SELECT key, date FROM events WHERE series = ? AND date >= ? AND written_by = ?", (rule.slug, db.today().isoformat(), WRITER)).fetchall():
                if not occurrences(None, row['date'], row['date'], series=rule):
                    events.delete(conn, row["key"])
            continue
        start = db.today()
        scheduled = occurrences(None, start, start + timedelta(days=horizon_days),
                                series=rule)
        skip = covered(conn, rule.slug)
        wanted = [d for d in scheduled if d.isoformat() not in skip][:max(0, ahead)]
        landing = {d.isoformat() for d in scheduled}

        # An existing upcoming row satisfies the slot; never invent meetings between
        # here and it, and only rule-covered days count toward the slot.
        upcoming = sum(1 for row in conn.execute(
            "SELECT date, instead_of FROM events"
            "  WHERE series = ? AND date >= ? AND status <> 'declined'",
            (rule.slug, db.today().isoformat()))
            if row["date"] in landing or (row["instead_of"] or "") in landing)
        # Projections track the rule until another writer claims the row.
        for row in conn.execute(
                "SELECT key, date FROM events"
                "  WHERE series = ? AND date >= ? AND written_by = ?",
                (rule.slug, db.today().isoformat(), WRITER)):
            if row["date"] in landing or occurrences(None, row['date'], row['date'], series=rule):
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
            # A rule without an hour projects a row with a hole; ask for it once here.
            if not rule.time:
                todos.ask(conn, f"What time is {rule.title} on "
                                f"{_WEEKDAYS[rule.weekday][:-1]}s now?"
                          if rule.weekday is not None else
                          f"What time is {rule.title}?",
                          key=f"q:series-time:{rule.slug}",
                          about_event=event.id, written_by=written_by)

        # Projected rows on days the rule no longer lands on; withdrawn silently.
        stale = conn.execute(
            """SELECT key, date FROM events
                WHERE series = ? AND date >= ? AND instead_of IS NULL
                  AND written_by = ?""",
            (rule.slug, db.today().isoformat(), WRITER)).fetchall()
        for row in stale:
            if row["date"] in landing or occurrences(None, row['date'], row['date'], series=rule):
                continue
            events.delete(conn, row["key"])
            log.append(f"series  withdrew the projected {rule.title} on {row['date']} — "
                       f"the rule no longer lands there")
    conn.commit()
    return log


def stale_occurrences(conn: sqlite3.Connection, slug: str) -> list[sqlite3.Row]:
    """Future rows the rule no longer accounts for and cannot withdraw.

    Returned for the caller to ask about; this never deletes observations.
    """
    rule = get(conn, slug)
    if rule is None:
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
    if not rule.projectable and not rule.changes:
        return None
    if instead_of:
        return db.parse_date(instead_of).isoformat()
    day = db.parse_date(on)
    reach = (16 if rule.cadence == 'monthly' else 1 if rule.cadence == 'daily' else 7) * rule.every
    landing = occurrences(None, day - timedelta(days=reach), day + timedelta(days=reach),
                          series=rule)
    if not landing:
        return None
    if day in landing:
        return day.isoformat()
    # Half a cycle either way; equidistant days belong to neither.
    near = sorted(landing, key=lambda d: (abs((d - day).days), d))
    best = near[0]
    if abs((best - day).days) > reach // 2:
        return None
    if len(near) > 1 and abs((near[1] - day).days) == abs((best - day).days):
        return None
    return best.isoformat()


def covered(conn: sqlite3.Connection, slug: str) -> set[str]:
    """Scheduled days some row already stands in for, by date or `instead_of`."""
    rows = conn.execute(
        "SELECT coalesce(instead_of, date) AS instead_of FROM events WHERE series = ?"
        " AND (instead_of IS NOT NULL OR status = 'declined')",
        (db.slugify(slug or ""),)).fetchall()
    return {str(row["instead_of"]) for row in rows if row["instead_of"]}
