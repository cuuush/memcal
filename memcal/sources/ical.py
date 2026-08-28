"""macOS Calendar ingestion through the built-in Calendar scripting interface."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, time as clock_time, timedelta

from .. import archive, db, events, gate, series, todos, trace
from . import base, providers, register
from .spec import Source, SourceError

LOOKBACK_DAYS = 120
LOOKAHEAD_DAYS = 365

#: The two real transports. Every outward call in this file takes `runner=` (or
#: `opener=`) bound at import — see `TestNoTestCanReachTheRealCalendar` — so anything
#: that is not one of these is a seam a caller supplied.
_REAL_TRANSPORT = (subprocess.run, subprocess.Popen)


def _have_osascript() -> bool:
    """Whether this host has `osascript` at all.

    One named place rather than six inline `shutil.which` calls, so a test can say which
    kind of host it is testing instead of inheriting the answer from the machine it runs
    on. `ICalSource.check` is the one caller that wants this on its own.
    """
    return shutil.which("osascript") is not None


def _unavailable(transport) -> bool:
    """The platform gate, asked only when the transport really is `osascript`.

    The gate exists to give a real user a good error instead of a bare `FileNotFoundError`
    out of `subprocess`, so it is about the *real* binary. Asked first and unconditionally
    it also answered for 42 tests that had already handed these functions a fake
    transport — a dependency you can inject and a dependency you can gate on are two
    different dependencies, and the suite failed off a Mac having never intended to touch
    Calendar.app at all.
    """
    return any(transport is real for real in _REAL_TRANSPORT) and not _have_osascript()


#: How far ahead an ordinary run looks. Every read of Calendar.app costs one Apple Event
#: per event and they are ~60ms each, so the window *is* the runtime: a year ahead is 115
#: events and 17 seconds of a person waiting for the Collect button. Six months covers
#: everything the brief, the Later block and a normal lookup ever touch.
NEAR_LOOKAHEAD_DAYS = 180

#: …and the rest of the year is still read, just not on every run. A thing booked eight
#: months out is real and must not go missing; it also does not change hourly.
FULL_SCAN_HOURS = 20

#: The three phases of a read, and roughly what share of the wait each one is.
#:
#: Measured rather than assumed, and the assumption was wrong: querying the calendars is
#: not free next to reading the events. A 60-day window spent 2.4s on ten `whose` queries
#: and 0.9s on 23 property reads — ~240ms per calendar against ~40ms per event. The
#: per-calendar cost is roughly fixed while the per-event cost scales with the window, so
#: on the 300 days a real run asks for, reading pulls ahead again but not by the order of
#: magnitude the ~60ms-per-event note alone suggests.
#:
#: They stay estimates — the phases count unlike things and no single weighting is right
#: for both a quiet Tuesday and a cold start. What they buy is a bar that moves the whole
#: time and never restarts. `filing` is deliberately over-weighted: finishing early reads
#: as a fast run, and stalling at 95% reads as a hang.
SNAPSHOT_PHASES = (("checking", 25), ("reading", 60), ("filing", 15))

# JXA gives us JSON without adding PyObjC.
#
# `calendar.properties()` is not used and must not be reintroduced. On macOS 26 it
# returns an empty object — no exception, no name, and `writable` undefined, which is
# falsy — so every calendar read as a read-only subscription and 70 events the user
# created on their own calendars were filed as `opportunity`/`mentioned` instead of
# `commitment`/`confirmed`. It also spent time failing, once per calendar. The
# individual accessors below work and are the whole fix.
#
# Per-*event* `properties()` stays: it is one Apple Event returning everything, and
# every alternative measured worse. Reading properties one at a time is ten round trips
# instead of one; reading them in bulk off a `whose` specifier re-runs the filter for
# each property (84s against 17s); and bulk `properties()` on a specifier is not
# supported at all ("Can't get object").
#
# The two loops are one pass split in half so that this can *count*. Every calendar is
# queried first — cheap, and it is what turns "some unknown number of events" into a
# denominator — and only then are the properties read, which is where the seconds go.
# Without that the whole read is a single opaque subprocess and the only honest bar for
# it is an indeterminate stripe. `tick` goes to stderr; stdout is the JSON payload and
# must stay clean.
#
# **Every `catch` that drops events names the calendar it dropped them from.** They used
# to swallow: one subscription mid-refresh or one CalDAV account offline contributed zero
# events, the process still exited 0, and a partial read arrived downstream shaped exactly
# like a complete one — where `reconcile_deleted` reads absence as deletion and writes a
# decline per row. A read that cannot say "I did not read everything" must not be allowed
# to drive a deletion, so the payload carries `unreadable` beside `events` and the
# reconciliation stages stand down when it is non-empty.
JXA = r"""
function run(argv) {
  const app = Application("Calendar");
  const lower = new Date(argv[0]);
  const upper = new Date(argv[1]);
  const out = [];
  const unreadable = [];
  const string = (value) => value === null || value === undefined ? "" : String(value);
  const iso = (value) => {
    try { return value ? new Date(value).toISOString() : ""; } catch (_) { return ""; }
  };
  const tick = (phase, done, total) => {
    try { console.log("@@memcal " + phase + " " + done + " " + total); } catch (_) {}
  };
  // A calendar this read did not finish, named so the Python half can say which. The
  // name is the only handle a person has on a calendar; a nameless one is reported as
  // the empty string, which still counts.
  const failed = (name, error) => {
    let detail = "";
    try { detail = string(error && (error.message || error)); } catch (_) {}
    unreadable.push({name: name, detail: detail.slice(0, 160)});
  };
  const calendars = app.calendars();
  const found = [];
  let expected = 0;
  for (let index = 0; index < calendars.length; index++) {
    const calendar = calendars[index];
    let calendarName = "", calendarUid = "", writable = false;
    try { calendarName = string(calendar.name()); } catch (_) {}
    try { writable = Boolean(calendar.writable()); } catch (_) {}
    try { calendarUid = string(calendar.uid()) || calendarName; } catch (_) {
      calendarUid = calendarName;
    }
    let calendarEvents = [];
    try {
      // Calendar can contain an unbounded recurring history. Asking for every event
      // and filtering in JavaScript timed out on the first real library; `whose`
      // turns this into Calendar's native date-range query.
      calendarEvents = calendar.events.whose({
        startDate: {_greaterThanEquals: lower, _lessThan: upper}
      })();
    } catch (error) {
      calendarEvents = [];
      failed(calendarName, error);
    }
    found.push({
      name: calendarName, uid: calendarUid, writable: writable, events: calendarEvents
    });
    expected += calendarEvents.length;
    tick("checking", index + 1, calendars.length);
  }
  tick("reading", 0, expected);
  let seen = 0;
  for (const entry of found) {
    for (const event of entry.events) {
      seen++;
      if (seen % 5 === 0 || seen === expected) tick("reading", seen, expected);
      let ep = {};
      // One event whose properties will not read is the same partial read one calendar
      // at a time: the row is missing from the snapshot and nothing else about the
      // snapshot says so.
      try { ep = event.properties(); } catch (error) { failed(entry.name, error); continue; }
      const start = ep.startDate;
      if (!start || start < lower || start >= upper) continue;
      out.push({
        calendar_name: entry.name,
        calendar_uid: entry.uid,
        writable: entry.writable,
        uid: string(ep.uid || ep.id),
        title: string(ep.summary),
        start: iso(start),
        end: iso(ep.endDate),
        all_day: Boolean(ep.alldayEvent),
        location: string(ep.location),
        description: string(ep.description),
        url: string(ep.url),
        status: string(ep.status),
        recurrence: string(ep.recurrence)
      });
    }
  }
  return JSON.stringify({events: out, unreadable: unreadable});
}
"""

PROBE_JXA = r"""
function run() {
  const app = Application("Calendar");
  return JSON.stringify({calendars: app.calendars.name().length});
}
"""


def permission_status(*, runner=subprocess.run) -> tuple[bool, str]:
    """Make the smallest real Calendar read; this is also what triggers first access."""
    if _unavailable(runner):
        return False, "macOS osascript is unavailable"
    try:
        done = runner(
            ["osascript", "-l", "JavaScript", "-e", PROBE_JXA],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False, "Calendar permission check timed out (a prompt may be waiting)"
    except OSError as exc:
        return False, f"could not run Calendar permission check: {exc}"
    requester = f"{sys.executable} → /usr/bin/osascript → Calendar.app"
    if done.returncode:
        detail = (done.stderr or done.stdout or "access denied").strip()
        return False, (
            f"Calendar access denied for {requester}: {detail[:180]}. "
            "Open System Settings → Privacy & Security → Calendars and Automation."
        )
    return True, f"Calendar read access granted ({requester})"


#: What the JXA writes to stderr for every few events it reads: phase, done, total.
TICK = "@@memcal"

SNAPSHOT_TIMEOUT = 180


def _tick(line: str) -> tuple[str, int, int] | None:
    """One progress line from the JXA, or None for anything else it said."""
    parts = line.split()
    if len(parts) != 4 or parts[0] != TICK:
        return None
    try:
        return parts[1], int(parts[2]), int(parts[3])
    except ValueError:
        return None


def _pump(stream, progress, tail: list[str]) -> None:
    """Read the JXA's stderr as it arrives: ticks drive the bar, the rest is the error.

    Runs on its own thread because the main one is waiting on the process. Anything that
    is not a tick is kept — a Calendar failure explains itself on stderr and that message
    is the whole of what `fetch` has to show the user.
    """
    with stream:
        for line in stream:
            mark = _tick(line)
            if mark is None:
                said = line.strip()
                if said:
                    tail.append(said)
                    del tail[:-8]
                continue
            phase, done, total = mark
            if progress:
                progress(_snapshot_note(phase, done, total),
                         done=done, total=total, phase=phase)


def _snapshot_note(phase: str, done: int, total: int) -> str:
    if phase == "checking":
        return f"asking Calendar.app about {total} calendar(s)"
    return f"{done}/{total} calendar events"


@dataclass(frozen=True)
class Snapshot:
    """One Calendar read: what it found, and what it could not read.

    Deliberately not a bare list. A list is what the read used to return, and a list
    cannot say "one of the nine calendars threw and contributed nothing" — which is the
    difference between an event that is gone and an event nobody looked for. Anything
    that decides a row has *disappeared* has to ask `complete` first.
    """

    items: list[dict]
    #: The calendars this read did not finish, by name, in the order they failed.
    unreadable: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.unreadable


def _calendar_snapshot(start: str, end: str, *, progress=None,
                       opener=subprocess.Popen) -> Snapshot:
    """The whole window, in one osascript call that says how far through it is."""
    if _unavailable(opener):
        raise SourceError("macOS osascript is unavailable")
    tail: list[str] = []
    with tempfile.TemporaryFile() as sink:
        try:
            proc = opener(
                ["osascript", "-l", "JavaScript", "-e", JXA, start, end],
                stdout=sink, stderr=subprocess.PIPE, text=True,
            )
        except OSError as exc:
            raise SourceError(f"could not read Calendar.app: {exc}") from exc
        reader = threading.Thread(target=_pump, args=(proc.stderr, progress, tail),
                                  daemon=True)
        reader.start()
        try:
            code = proc.wait(timeout=SNAPSHOT_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            raise SourceError("Calendar.app did not answer within 3 minutes") from exc
        finally:
            # Joined, not abandoned: a tick landing after this returns would move the
            # bar backwards into a phase the source has already finished.
            reader.join(timeout=5)
        sink.seek(0)
        stdout = sink.read().decode("utf-8", "replace")
    if code:
        detail = ("\n".join(tail) or stdout or "unknown Calendar error").strip()
        raise SourceError(
            "Calendar.app read failed. Grant Calendar access to the terminal/Python "
            f"running memcal, then retry: {detail[:240]}"
        )
    try:
        payload = json.loads(stdout.strip() or "{}")
    except json.JSONDecodeError as exc:
        raise SourceError("Calendar.app returned malformed data") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise SourceError("Calendar.app returned an unexpected result")
    failures = payload.get("unreadable")
    if not isinstance(failures, list):
        raise SourceError("Calendar.app did not say whether it read every calendar")
    # Deduplicated, because a per-event failure reports its calendar once per event, and
    # order-preserving, because the first one is the one worth reading.
    unreadable: list[str] = []
    for failure in failures:
        name = str((failure or {}).get("name") or "") if isinstance(failure, dict) \
            else str(failure or "")
        if name not in unreadable:
            unreadable.append(name)
    return Snapshot(
        items=[item for item in payload["events"] if isinstance(item, dict)],
        unreadable=tuple(unreadable),
    )


def _identity_of(uid: str, occurrence: str) -> str:
    """The one hashing rule, so publish, scan and migration cannot drift apart.

    `occurrence` is empty for a one-off and the occurrence start for a recurring event.
    """
    return hashlib.sha256(f"{uid}\0{occurrence}".encode("utf-8")).hexdigest()


def _identity(item: dict) -> str:
    """Stable across edits, moves *and calendar renames*; occurrences split by start."""
    occurrence = str(item.get("start") or "") if item.get("recurrence") else ""
    return _identity_of(str(item.get("uid", "")), occurrence)


def _revision(identity: str, item: dict) -> str:
    material = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]
    return f"{identity}:{digest}"


def _normalized(item: dict) -> dict | None:
    title = " ".join(str(item.get("title") or "").split())
    start_raw = str(item.get("start") or "")
    uid = str(item.get("uid") or "")
    if not title or not start_raw or not uid:
        return None
    start = db.parse_ts(start_raw).astimezone()
    end_raw = str(item.get("end") or "")
    end = db.parse_ts(end_raw).astimezone() if end_raw else start
    all_day = bool(item.get("all_day"))
    # RFC 5545 says DTEND is exclusive, and it is — in an .ics file. The scan does not
    # read .ics: it reads Calendar.app's `end date` property, which reports 23:59:59 of
    # the last day the event actually occupies. Subtracting a day from that stored every
    # multi-day all-day event one day short — "big trip" ran Apr 17-19 on the calendar
    # and Apr 17-18 here, "Julian's graduation" May 21-24 and May 21-23 — and nothing
    # caught it, because the publish path was a day long in the opposite direction and
    # the round trip agreed with itself.
    #
    # Both conventions are still in play, so decide on the value rather than assume:
    # midnight is an exclusive end and belongs to the next day, anything later is the
    # last day itself.
    until = None
    if end.date() > start.date():
        last = end.date()
        if all_day and (end.hour, end.minute, end.second) == (0, 0, 0):
            last -= timedelta(days=1)
        if last > start.date():
            until = last.isoformat()
    where = " ".join(str(item.get("location") or "").split()) or None
    # A link in the `location` field, which is where calendar apps put it when the event
    # has no other home for it — and where the store had no other home for it either.
    # The live calendar had "location: https://meet.google.com/dcp-uqon-ibe" and, on
    # another row, "141 Worth St, New York, NY 10013, USA; https://meet.google.com/…":
    # an address and a join link sharing one field because only one field existed. Lift
    # the link out and leave the address behind.
    from_where = join_link(where)
    if from_where:
        rest = where.replace(from_where, "").strip(" ;,·|-")
        where = " ".join(rest.split()) or None
    return {
        "title": title,
        "date": start.date().isoformat(),
        "until": until,
        "time": None if all_day else start.strftime("%H:%M"),
        "location": where,
        # The JXA has lifted `description` and `url` since it was written and this
        # narrowed them straight back out, so a join link sitting in the calendar event
        # itself could not reach the store, the archived line, or a model. A connector
        # that reads a field and then drops it is a missing column wearing a disguise.
        "join_url": join_link(item.get("description"), item.get("url")) or from_where,
        "note": _description_note(item.get("description")),
    }


#: `BYDAY` in RFC 5545 order, mapped to memcal's Monday-is-zero weekday.
_RRULE_DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def recurrence_rule(text: str | None) -> dict | None:
    """The cadence Calendar.app is already telling us, in memcal's vocabulary."""
    body = (text or "").strip().upper()
    if not body:
        return None
    parts = dict(piece.split("=", 1) for piece in
                 re.sub(r"^RRULE:", "", body.splitlines()[0]).split(";")
                 if "=" in piece)
    freq, interval = parts.get("FREQ"), int(parts.get("INTERVAL") or 1)
    days = [_RRULE_DAYS[d[-2:]] for d in (parts.get("BYDAY") or "").split(",")
            if d[-2:] in _RRULE_DAYS]
    if freq == "WEEKLY" and len(days) == 1 and interval in (1, 2):
        # More than one BYDAY is a thing that meets twice a week, which the vocabulary
        # cannot say. Recording it as one of the two days would be quietly wrong forever.
        return {"cadence": "weekly" if interval == 1 else "fortnightly",
                "weekday": days[0]}
    if freq == "MONTHLY" and interval == 1 and not days:
        try:
            return {"cadence": "monthly",
                    "day_of_month": int((parts.get("BYMONTHDAY") or "").split(",")[0])}
        except (TypeError, ValueError):
            return None
    return None


def learn_cadence(conn: sqlite3.Connection, item: dict, event) -> None:
    """Teach an existing series what its own calendar already says about it."""
    from .. import series as series_mod                              # noqa: PLC0415
    parsed = recurrence_rule(item.get("recurrence"))
    if not parsed or not event.series:
        return
    rule = series_mod.get(conn, event.series)
    if rule is None or rule.status != "active":
        return
    fields = {**parsed, "time": event.time or rule.time}
    if all(str(getattr(rule, name, None) or "") == str(value or "")
           for name, value in fields.items()):
        return
    series_mod.upsert(conn, {"slug": rule.slug, **fields,
                             "effective_on": max(event.date, db.today().isoformat()),
                             "source": f"ical:{item.get('calendar_name') or 'calendar'}"},
                      written_by="ical")
    series_mod.roll_forward(conn, slug=rule.slug)


#: memcal's own published events carry a description this file writes. Reading it back
#: as though somebody had told us something is the row manufacturing its own evidence —
#: the same loop that had "Chili's" citing memcal's calendar entry as proof of Chili's.
_OUR_NOTE = re.compile(r"^\s*Added by memcal\.", re.IGNORECASE)

#: Long enough for "Suite 300, ring buzzer 4. Bring your insurance card." and short
#: enough that a conferencing app's wall of dial-in numbers does not become the row.
NOTE_CHARS = 240


def _description_note(description: str | None) -> str | None:
    """What the calendar event said about itself, for the half that is not a link."""
    text = " ".join(str(description or "").split())
    if not text or _OUR_NOTE.match(text):
        return None
    # A bare link is already `join_url`; repeating it as prose helps nobody.
    if join_link(text) == text:
        return None
    return text[:NOTE_CHARS].rstrip() or None


#: A link you *attend through*, as opposed to a link about the thing. Deliberately a
#: fixed list of hosts rather than "any URL in the description": descriptions are full
#: of maps, agendas, dial-ins and unsubscribe footers, and picking the wrong one puts a
#: marketing page where the join button should be. Code, no model — invariant 1's
#: reasoning applied one layer out.
#: The scheme is optional because a model strips it. One trial in three wrote
#: `location: us02web.zoom.example/j/8842119` with no `https://` on the front, which the
#: scheme-anchored version read as ordinary text and left sitting in the location field.
#: Hosts this specific are unambiguous bare; a general URL regex would not be.
CONFERENCE_HOSTS = re.compile(
    r"(?:https?://)?[^\s<>\"'/]*(?:"
    r"zoom\.us|zoom\.example|zoomgov\.com"
    r"|meet\.google\.com"
    r"|teams\.(?:microsoft|live)\.com"
    r"|webex\.com"
    r"|whereby\.com"
    r"|meet\.jit\.si"
    r"|chime\.aws"
    r"|bluejeans\.com"
    r"|doxy\.me"
    r"|(?:[\w-]+\.)?gotomeeting\.com"
    r")[^\s<>\"']*",
    re.IGNORECASE,
)


def join_link(*candidates: str | None) -> str | None:
    """The first conferencing URL among these strings, or None.

    Order matters: callers pass the description before the bare `url`, because a
    calendar event's `url` is as often a ticket page as a meeting room.
    """
    for text in candidates:
        found = CONFERENCE_HOSTS.search(str(text or ""))
        if found:
            url = found.group(0).rstrip(".,);]>")
            # Stored with a scheme whatever arrived, so the brief renders something
            # pressable and two spellings of one link are one value.
            return url if url.startswith("http") else f"https://{url}"
    return None


#: How a link and a place share one field on the way back out. Semicolon-space, which
#: is the separator Calendar.app's own rows already use — `fields()` was written against
#: a live event reading "141 Worth St, New York, NY 10013, USA; https://meet.google.com/…"
#: — so what memcal writes is a shape it already knows how to read.
LOCATION_JOIN = "; "


def publish_location(location: str | None, join_url: str | None) -> str:
    """Combine a physical location and join URL for Calendar.app."""
    where = " ".join(str(location or "").split())
    link = " ".join(str(join_url or "").split())
    if not link:
        return where
    # Already carrying the link — a row whose `location` was never split, or a second
    # pass over our own output. Composing again would print it twice.
    if link in where:
        return where
    return f"{where}{LOCATION_JOIN}{link}" if where else link


def _source(item: dict, *, provider: str, subscribed: bool) -> str:
    origin = "subscribed" if subscribed else "created"
    return f"ical:{origin}:{item.get('calendar_name') or provider}"


def _text(item: dict, fields: dict, *, policy, subscribed: bool) -> str:
    origin = "subscribed" if subscribed else "created"
    bits = [
        fields["title"],
        f"{fields['date']}{' ' + fields['time'] if fields.get('time') else ''}",
        f"calendar: {item.get('calendar_name') or '?'} ({origin})",
    ]
    if fields.get("location"):
        bits.append(f"location: {fields['location']}")
    if fields.get("note"):
        bits.append(f"notes: {fields['note']}")
    if fields.get("join_url"):
        # In the archived line too, not only on the row. This is what a model reads,
        # and "Online" on its own is exactly as unhelpful to it as it was to them.
        bits.append(f"join: {fields['join_url']}")
    if policy is not None:
        # Whatever the platform says about its own row, in its own words. This used to
        # be the literal string "Partiful RSVP yes", emitted whenever the location field
        # was non-empty — so an invitation nobody had answered carried a false RSVP into
        # the archive, which is the one store that is never rewritten.
        bits.extend(policy.describe(item, fields))
    return " — ".join(bits)


def ingest_snapshot(
    conn: sqlite3.Connection,
    cfg,
    items: list[dict],
    *,
    scan_start: str,
    scan_end: str,
    report: base.IngestReport | None = None,
    unreadable: tuple[str, ...] | list[str] = (),
) -> base.IngestReport:
    """Apply a complete snapshot. Public so fixtures can test policy without Calendar.

    `unreadable` names the calendars this read did not finish. It is what makes the
    snapshot *complete* or not, and completeness is the precondition on every
    disappearance decision below — filing is unaffected, because an event that did
    arrive is evidence whatever else failed.
    """
    report = report or base.IngestReport.opened("ical", cfg)
    # Per provider, because each one judges its own feed's disappearances and a policy
    # must never be shown another platform's rows. Keyed on `Policy.name`, which is what
    # `calendar_items.provider` stores.
    seen_by_provider: dict[str, set[str]] = {p.name: set() for p in providers.REGISTRY}
    # Every identity this snapshot filed, whichever branch filed it. The per-provider
    # sets above have always been narrowed to platforms that have a disappearance
    # policy; the deletion path needs the same set for plain calendar rows.
    seen_ids: set[str] = set()
    # Every Apple uid in this snapshot, whatever branch filed it. Identity can be
    # rewritten underneath a row — that is what `_identity`'s docstring is about — and
    # an identity set alone cannot tell "this invitation is gone" from "this invitation
    # is filed under a name I no longer use". The uid can, so the decline policy reads
    # this instead.
    seen_uids: set[str] = set()
    # The repeating events memcal published. Checked before anything files anything: a
    # recurring event arrives once *per occurrence* under a single uid, so unguarded, one
    # published schedule becomes fifty archived "messages", fifty rows, and fifty pieces
    # of evidence that the schedule exists — every scan, forever. That is the loop that
    # once had a joke about Chili's citing memcal's own calendar entry as its proof, and
    # a rule repeats it by construction.
    our_schedules = published_series_uids(conn)
    stamp = db.now()
    unchanged = ours = 0

    for index, raw in enumerate(items, 1):
        if report.progress and (index % 5 == 0 or index == len(items)):
            report.progress(f"{index}/{len(items)} filed",
                            done=index, total=len(items), phase="filing")
        item = dict(raw)
        common = _normalized(item)
        if common is None:
            report.notes.append("skipped malformed calendar event")
            continue
        if str(item.get("uid") or "") in our_schedules:
            seen_uids.add(str(item["uid"]))
            ours += 1
            continue
        if item.get("uid"):
            seen_uids.add(str(item["uid"]))
        # A calendar that has not changed still has to prove it is still there — that is
        # what disappearance detection reads — but it does not have to be re-archived,
        # re-gated, re-matched and re-written. On a quiet day that is every row in the
        # window, which is most of what this stage was spending its time on after the
        # Apple Events themselves.
        identity_now = _identity(item)
        seen_ids.add(identity_now)
        revision_now = _revision(identity_now, item)
        known = conn.execute(
            "SELECT revision, provider, published, published_state, event_key"
            "  FROM calendar_items"
            "  WHERE identity = ?", (identity_now,)).fetchone()
        if known is None and item.get("uid"):
            # An event seen before under a different identity. This was once scoped to
            # `published = 1`, on the theory that only memcal's own writes could change
            # identity — Calendar.app refuses to name a calendar it created seconds ago,
            # so the publish recorded a name and the next read found a uid. The scope was
            # the bug: `_identity` used to hash the calendar uid, every calendar in the
            # library changed uid on one macOS update, and 111 subscribed and created
            # rows re-entered the store as new events because none of them was ours.
            #
            # Apple's event uid is the durable name and it is enough on its own. Adopt
            # the prior row rather than open a second one, preferring memcal's own copy,
            # then a record that still resolves to a live event, then the oldest — the
            # copy that has been amended is the copy carrying the history.
            # Every occurrence of a weekly event shares one Apple uid, so for a
            # recurring item the start is part of what is being matched — otherwise
            # this collapses a term of language classes into a single row. Matched
            # through `db.utc_stamp` on both sides, because this compares against a
            # *stored* value and the store held three notations for one instant: a row
            # memcal published carried `2026-08-25T13:00:00-04:00` while the scan asked
            # for `2026-08-25T17:00:00.000Z`, so the one case this block exists for —
            # a uid moving under a row memcal itself wrote — could never match.
            #
            # A `series:` row is excluded on both statements, and that exclusion is part
            # of the same fix rather than a separate opinion. It is the bookkeeping for a
            # published *rule*: its `event_key` is `series:<slug>` and names no
            # `events.key` at all, so adopting it hands `fields["key"]` a string no
            # event will ever have, and deleting it loses the record that stops the next
            # scan reading memcal's own repeating event back in fifty times. The format
            # mismatch was hiding that path; matching the two notations would have opened
            # it, on the one row in the live store written by `publish_series`.
            occurrence = db.utc_stamp(item.get("start") or "") if item.get("recurrence") else ""
            prior = conn.execute(
                """SELECT c.identity, c.revision, c.provider, c.published,
                          c.published_state, c.event_key
                     FROM calendar_items c LEFT JOIN events e ON e.key = c.event_key
                    WHERE c.event_uid = ? AND (? = '' OR c.starts_at = ?)
                      AND c.event_key NOT LIKE 'series:%'
                    ORDER BY c.published DESC, e.id IS NULL, e.id
                    LIMIT 1""", (str(item["uid"]), occurrence, occurrence)).fetchone()
            if prior is not None:
                # Any other rows for this uid and occurrence are the same Apple event
                # counted twice. Identity is the primary key, so they cannot all be
                # re-pointed at it.
                conn.execute(
                    "DELETE FROM calendar_items WHERE event_uid = ? AND identity != ?"
                    "   AND (? = '' OR starts_at = ?)"
                    "   AND event_key NOT LIKE 'series:%'",
                    (str(item["uid"]), prior["identity"], occurrence, occurrence))
                conn.execute(
                    "UPDATE calendar_items SET identity = ? WHERE identity = ?",
                    (identity_now, prior["identity"]))
                known = prior
        # memcal put this on the calendar itself. Reading it back in would archive a
        # message the user never received, re-derive a row from it, and stamp the row's
        # source as the calendar rather than the conversation it actually came from —
        # a loop, with the evidence rewritten at every turn. Skipped *unless the copy
        # has since diverged*, because an event the user then edited in Calendar.app is
        # them telling us something, and that is the one thing worth reading back.
        if known and known["published"]:
            # The publish record names the row by key, and a key embeds the date it was
            # minted with, so re-dating a row can leave the record pointing at nothing.
            # Follow it before asking whether the copy diverged — a stale key is our own
            # bookkeeping going out of date, never the user editing anything.
            key_now = _relocate(conn, known["event_key"], common)
            if key_now != known["event_key"]:
                conn.execute("UPDATE calendar_items SET event_key = ? WHERE identity = ?",
                             (key_now, identity_now))
            if not _user_edited(conn, known, key_now, common):
                conn.execute(
                    "UPDATE calendar_items SET active = 1, last_seen_at = ?, revision = ?"
                    "  WHERE identity = ?", (stamp, revision_now, identity_now))
                ours += 1
                continue
        if known and known["revision"] == revision_now:
            conn.execute(
                "UPDATE calendar_items SET active = 1, last_seen_at = ? WHERE identity = ?",
                (stamp, identity_now))
            if known["provider"] in seen_by_provider:
                seen_by_provider[known["provider"]].add(identity_now)
            unchanged += 1
            continue
        subscribed = not bool(item.get("writable"))
        policy = providers.claiming(item, cfg)
        provider = policy.name if policy else "ical"
        identity = identity_now
        existing = conn.execute(
            "SELECT * FROM calendar_items WHERE identity = ?", (identity,)
        ).fetchone()
        source = _source(item, provider=provider, subscribed=subscribed)
        fields = {
            **common,
            "kind": "opportunity" if subscribed else "commitment",
            "status": "mentioned" if subscribed else "confirmed",
            "source": source,
        }
        if policy is not None:
            # Everything the platform implies — including the reply link, which the
            # policy keeps because it is the policy that knows the `url` on one of its
            # rows is a *reply* link rather than a ticket page.
            fields = policy.fields(item, fields)
            seen_by_provider[policy.name].add(identity)
        if existing:
            fields["key"] = existing["event_key"]
        else:
            # Join a calendar copy to a plan already learned from conversation, but
            # never let two distinct calendar identities share that row. Recurring
            # occurrences have the same title/UID and often sit seven days apart,
            # exactly inside `find_match`'s normal ten-day series window.
            candidate = events.find_match(
                conn,
                title=fields["title"],
                on=fields["date"],
                participants=[],
            )
            occupied = candidate and conn.execute(
                "SELECT 1 FROM calendar_items WHERE event_key = ? AND identity != ?",
                (candidate.key, identity),
            ).fetchone()
            if candidate and not occupied:
                fields["key"] = candidate.key
            else:
                fields["key"] = f"ical-{identity[:16]}@{fields['date']}"

        text = _text(item, fields, policy=policy, subscribed=subscribed)
        archive_id = base.deliver(
            conn,
            report,
            stream="ical",
            external_id=_revision(identity, item),
            ts=stamp,
            text=text,
            thread=str(item.get("calendar_uid") or item.get("calendar_name") or "calendar"),
            meta={
                "calendar": item.get("calendar_name") or "",
                "calendar_origin": "subscribed" if subscribed else "created",
                "provider": provider,
                "event_uid": item.get("uid") or "",
                "url": item.get("url") or "",
            },
            verdict=gate.Verdict(False, "calendar-structured"),
        )
        # `kind` and `status` are the two fields this connector *derives* — from
        # whether the calendar is writable, and for Partiful from whether the feed row
        # carries a location. Everything else here was read off the event itself.
        event, verb = events.upsert(conn, fields, written_by="ical", match=False,
                                    inferred=("kind", "status", "note"))
        # The calendar states its own cadence and nothing has ever read it. A change the user
        # made there is the truth about the schedule, so it lands on the rule rather than
        # being reconstructed from N moved occurrences one at a time.
        learn_cadence(conn, item, event)
        conn.execute(
            """INSERT INTO calendar_items(
                   identity, calendar_uid, calendar_name, event_uid, event_key,
                   starts_at, subscribed, provider, active, revision,
                   last_seen_at, updated_at)
               VALUES(?,?,?,?,?,?,?,?,1,?,?,?)
               ON CONFLICT(identity) DO UPDATE SET
                   calendar_uid=excluded.calendar_uid,
                   calendar_name=excluded.calendar_name,
                   event_uid=excluded.event_uid,
                   event_key=excluded.event_key,
                   starts_at=excluded.starts_at,
                   subscribed=excluded.subscribed,
                   provider=excluded.provider,
                   active=1,
                   revision=excluded.revision,
                   last_seen_at=excluded.last_seen_at,
                   updated_at=excluded.updated_at""",
            (
                identity,
                str(item.get("calendar_uid") or item.get("calendar_name") or ""),
                str(item.get("calendar_name") or "Calendar"),
                str(item.get("uid") or ""),
                event.key,
                # A no-op on what the JXA sends — `db.utc_stamp` is a fixed point of
                # `toISOString()` — and the point is that it is *stated*, so the two
                # publish writers below have something to agree with.
                db.utc_stamp(item.get("start") or ""),
                int(subscribed),
                provider,
                revision_now,
                stamp,
                stamp,
            ),
        )
        if archive_id:
            trace.stamp(
                conn,
                kind="event",
                ref=event.key,
                verb=verb,
                entity=f"calendar:{item.get('calendar_name') or '?'}",
                stage="ical",
                archive_ids=[archive_id],
            )

    # Everything below this line reads *absence* as evidence, and absence is only
    # evidence when the read was whole. Two ways it is not, and they are independent:
    #
    # *A calendar said so.* One `whose()` that throws — a subscription mid-refresh, a
    # CalDAV account offline — used to contribute zero events and still exit 0, so a
    # library of nine calendars missing one arrived shaped exactly like a library of
    # nine calendars where those events had been deleted. The whole pass stands down
    # rather than reconciling within the calendars that did read: an event's calendar
    # is the *least* stable thing about it — `_identity` stopped hashing the calendar
    # uid because a macOS update changed every one of them at once, and dragging an
    # event between calendars is not a new event — so scoping a deletion by calendar
    # would rest the safest decision in this file on the shakiest column in it.
    #
    # *Nothing came back at all.* A read can return an empty list and exit 0 with
    # nothing to report: `app.calendars()` answering before the accounts have loaded
    # does exactly that. `providers.partiful` has declined to act on an empty feed since
    # the beginning; the generic path is the one that never learned to. The asymmetry is
    # the point — a missed decline costs a stale row that the next scan catches, and a
    # wrongful decline costs a real commitment plus the trust that memcal's calendar is
    # true. The cost is bounded and visible: it is reported every run, by name.
    if unreadable:
        report.notes.append(
            f"{len(unreadable)} calendar(s) could not be read "
            f"({', '.join(name or '(unnamed)' for name in unreadable)}); "
            "nothing was judged to have disappeared from an incomplete read")
    else:
        # A policy judges its own feed's disappearances and cannot see past it: with
        # nothing at all in the snapshot, `partiful`'s `seen` is empty and its
        # unsubscribe branch fires — deactivating every sync record it holds and saying
        # the user unsubscribed from a feed the user is still on. Only this function knows the
        # difference between "that platform's events are gone" and "no events came
        # back", so only this function can hold the policies back.
        if seen_ids or seen_uids:
            for policy in providers.REGISTRY:
                policy.reconcile_missing(
                    conn,
                    seen=seen_by_provider[policy.name],
                    seen_uids=seen_uids,
                    scan_start=scan_start,
                    scan_end=scan_end,
                    report=report,
                )
        # Called unconditionally: it owns the empty-snapshot decision itself, and says so.
        reconcile_deleted(
            conn,
            seen=seen_ids,
            seen_uids=seen_uids,
            scan_start=scan_start,
            scan_end=scan_end,
            report=report,
        )
    if unchanged:
        report.notes.append(f"{unchanged} calendar event(s) unchanged since last read")
    if ours:
        report.notes.append(f"{ours} event(s) memcal published, not read back in")
    conn.commit()
    return report


def reconcile_deleted(
    conn: sqlite3.Connection,
    *,
    seen: set[str],
    seen_uids: set[str],
    scan_start: str,
    scan_end: str,
    report: base.IngestReport,
) -> None:
    """Reconcile future events missing from a complete, non-empty calendar snapshot."""
    if not seen and not seen_uids:
        report.notes.append(
            "Calendar.app returned no events at all; treated as a failed read rather "
            "than as every event having disappeared")
        return
    today = db.today().isoformat()
    rows = conn.execute(
        """SELECT * FROM calendar_items
            WHERE provider = 'ical' AND active = 1 AND published = 0
              AND starts_at >= ? AND starts_at < ? AND substr(last_seen_at, 1, 10) < ?
            ORDER BY starts_at""",
        (db.utc_stamp(scan_start), db.utc_stamp(scan_end), today),
    ).fetchall()
    missing = [row for row in rows
               if row["identity"] not in seen and str(row["event_uid"]) not in seen_uids]
    if not missing:
        return

    asked: set[str] = set()
    gone = 0
    for row in missing:
        conn.execute(
            "UPDATE calendar_items SET active = 0, updated_at = ? WHERE identity = ?",
            (db.now(), row["identity"]))
        event = events.get(conn, row["event_key"])
        if event is None or event.date < today:
            continue
        gone += 1
        text = (f"{event.title} — removed from the {row['calendar_name']} calendar "
                f"— no longer on their calendar")
        archive_id = archive.append(
            conn,
            stream="ical",
            external_id=f"{row['identity']}:deleted:{today}",
            ts=db.now(),
            thread=row["calendar_uid"],
            text=text,
            meta={"calendar": row["calendar_name"], "calendar_origin": "created",
                  "provider": "ical", "state": "deleted"},
            gated=False,
            gate_reason="calendar-structured",
        )
        updated, verb = events.upsert(
            conn,
            {"key": event.key, "title": event.title, "date": event.date,
             "status": "declined",
             "source": f"ical:{'subscribed' if row['subscribed'] else 'created'}:"
                       f"{row['calendar_name']}"},
            written_by="ical",
            match=False,
        )
        if archive_id:
            report.archived += 1
            trace.stamp(conn, kind="event", ref=updated.key, verb=verb,
                        entity=f"calendar:{row['calendar_name']}", stage="ical",
                        archive_ids=[archive_id])
        # The rule is the thing at stake, and memcal is not entitled to guess at it.
        if event.series and event.series not in asked:
            asked.add(event.series)
            rule = series.get(conn, event.series)
            if rule is not None and rule.status == "active":
                todos.ask(
                    conn,
                    f"{rule.title} disappeared from the calendar — "
                    f"have you stopped going, or was it just this once?",
                    key=f"q:series-gone:{rule.slug}",
                    about_event=updated.id,
                    written_by="ical",
                )
    if gone:
        report.notes.append(
            f"{gone} event(s) removed from the calendar since the last read")


def _due_a_full_scan(conn: sqlite3.Connection) -> bool:
    """Has it been long enough to pay for the far half of the year again?"""
    last = db.get_meta(conn, "source.ical.last_full_scan", "") or ""
    if not last:
        return True
    try:
        return (db.now_dt() - db.parse_ts(last)).total_seconds() >= FULL_SCAN_HOURS * 3600
    except (ValueError, TypeError):
        return True


def _relocate(conn: sqlite3.Connection, event_key: str, fields: dict) -> str:
    """Where the row a publish record names has moved to, or the key unchanged."""
    if conn.execute("SELECT 1 FROM events WHERE key = ?", (event_key,)).fetchone():
        return event_key
    stem, _, _ = event_key.rpartition("@")
    if not stem:
        return event_key
    moved = conn.execute(
        """SELECT key FROM events WHERE key GLOB ?
            ORDER BY abs(julianday(date) - julianday(?)) LIMIT 1""",
        (f"{stem}@*", fields.get("date") or db.today().isoformat()),
    ).fetchone()
    return moved["key"] if moved else event_key


def _user_edited(conn: sqlite3.Connection, known, event_key: str, fields: dict) -> bool:
    """Did the user change memcal's published copy *in Calendar.app*?"""
    baseline = (known["published_state"] if "published_state" in known.keys() else "") or ""
    if not baseline:
        return _diverged(conn, event_key, fields)      # pre-`published_state` rows
    want = (baseline.split("|") + [""] * 5)[:5]
    got = [str(fields.get(name) or "") for name in ("title", "date", "until", "time",
                                                    "location")]
    for index, (mine, theirs) in enumerate(zip(want, got)):
        if index == 3 and not re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", mine.strip()):
            continue                # published as all-day; whatever comes back agrees
        if " ".join(mine.split()).casefold() != " ".join(theirs.split()).casefold():
            return True
    return False


def _diverged(conn: sqlite3.Connection, event_key: str, fields: dict) -> bool:
    """Has the Calendar.app copy stopped agreeing with the memcal row behind it?

    Only the four fields memcal publishes are compared. Anything else the user adds over
    there — an alert, an invitee, a note — is theirs and is not a disagreement.
    """
    row = conn.execute("SELECT title, date, time, location FROM events WHERE key = ?",
                       (event_key,)).fetchone()
    if not row:
        # No row answers to this key even after `_relocate` looked. Reading the copy
        # back in would file memcal's own calendar entry as an inbound event, sourced
        # to the calendar rather than to the conversation it came from — the loop this
        # whole branch exists to prevent. A publish record with nothing behind it is a
        # bookkeeping fault, and the safe reading of a bookkeeping fault is "ours".
        return False
    return any(
        " ".join(str(row[name] or "").split()).casefold()
        != " ".join(str(fields.get(name) or "").split()).casefold()
        for name in ("title", "date", "time", "location"))


# ------------------------------------------------------------------ account --
# Calendar.app scripting cannot select an account; EventKit must create the calendar
# in a syncing source. Require full access and reject its unauthorized virtual source.
# This path never prompts: consent belongs to `memcal ical setup`.

#: EventKit's `EKAuthorizationStatusFullAccess`. Anything less and the store is a
#: placeholder — see above.
EK_FULL_ACCESS = 3

#: The source EventKit hands an unauthorized caller. It looks exactly like iCloud.
EK_VIRTUAL_SOURCE = "VIRTUAL_APP_SOURCE_UUID"

ACCOUNT_JXA = r"""
ObjC.import('EventKit');
ObjC.import('Foundation');

const EVENT = $.EKEntityTypeEvent;
const LOCAL = 0, CALDAV = 2, MOBILEME = 3;
const VIRTUAL = "VIRTUAL_APP_SOURCE_UUID";
const FULL_ACCESS = 3;

function text(value) { try { return String(value.js); } catch (_) { return ""; } }

function iso(date) {
  try {
    if (!date || date.isNil()) return "";
    return String($.NSISO8601DateFormatter.alloc.init.stringFromDate(date).js);
  } catch (_) { return ""; }
}

function why(ref) {
  try { return String(ref[0].localizedDescription.js); } catch (_) { return ""; }
}

function status() {
  return Number($.EKEventStore.authorizationStatusForEntityType(EVENT));
}

function sourcesOf(store) {
  const out = [];
  const all = store.sources;
  for (let i = 0; i < all.count; i++) {
    const source = all.objectAtIndex(i);
    // The placeholder an unauthorized caller gets. Writing into it silently loses data.
    if (text(source.sourceIdentifier) === VIRTUAL) continue;
    out.push(source);
  }
  return out;
}

function calendarsIn(source) {
  const out = [];
  try {
    const set = source.calendarsForEntityType(EVENT).allObjects;
    for (let i = 0; i < set.count; i++) out.push(set.objectAtIndex(i));
  } catch (_) {}
  return out;
}

// iCloud reports as CalDAV. Title first so a self-hosted CalDAV account cannot win, then
// any remote account, because a calendar that syncs anywhere beats one that syncs nowhere.
function icloudSource(store) {
  let fallback = null;
  for (const source of sourcesOf(store)) {
    const kind = Number(source.sourceType);
    if (kind !== CALDAV && kind !== MOBILEME) continue;
    if (text(source.title) === "iCloud") return source;
    if (!fallback) fallback = source;
  }
  return fallback;
}

function describe(store, wanted) {
  const found = [];
  for (const source of sourcesOf(store)) {
    const kind = Number(source.sourceType);
    for (const calendar of calendarsIn(source)) {
      if (text(calendar.title) !== wanted) continue;
      found.push({
        account: text(source.title),
        source_type: kind,
        syncs: kind === CALDAV || kind === MOBILEME,
        calendar_id: text(calendar.calendarIdentifier),
        immutable: Boolean(calendar.immutable)
      });
    }
  }
  const target = icloudSource(store);
  return {found: found, icloud_account: target ? text(target.title) : ""};
}

function ensure(store, wanted) {
  const target = icloudSource(store);
  if (!target) return {error: "no iCloud (or other syncing) account is set up in Calendar"};
  for (const calendar of calendarsIn(target)) {
    if (text(calendar.title) === wanted) {
      return {created: false, calendar_id: text(calendar.calendarIdentifier),
              account: text(target.title)};
    }
  }
  const calendar = $.EKCalendar.calendarForEntityTypeEventStore(EVENT, store);
  calendar.title = $(wanted);
  calendar.source = target;
  const err = Ref();
  if (!store.saveCalendarCommitError(calendar, true, err)) {
    return {error: "could not create the calendar in " + text(target.title) + ": " + why(err)};
  }
  return {created: true, calendar_id: text(calendar.calendarIdentifier),
          account: text(target.title)};
}

//: Only genuinely *local* calendars are migration candidates. A calendar the user has
//: deliberately put in Exchange, or a subscription, is theirs and is not a mistake to fix.
function localNamed(store, wanted) {
  const out = [];
  for (const source of sourcesOf(store)) {
    if (Number(source.sourceType) !== LOCAL) continue;
    for (const calendar of calendarsIn(source)) {
      if (text(calendar.title) === wanted) out.push(calendar);
    }
  }
  return out;
}

//: A decade around today, walked one year at a time.
//:
//: The chunking is not tidiness. `predicateForEventsWithStartDate:endDate:calendars:` is
//: documented to span at most four years, and past that it does not raise or clamp — it
//: matches *nothing*. A single -5y…+5y predicate returned 0 events for a calendar holding
//: 7, and since "no events" is also what a genuinely empty calendar looks like, the
//: migration would have copied nothing, seen no failures, and deleted the original with
//: everything still in it. One year per window is comfortably inside the limit.
const SPAN_YEARS = 5, WINDOW = 365 * 86400;

function eventsIn(store, calendar) {
  const seen = {}, out = [];
  for (let year = -SPAN_YEARS; year < SPAN_YEARS; year++) {
    const from = $.NSDate.dateWithTimeIntervalSinceNow(year * WINDOW);
    const to = $.NSDate.dateWithTimeIntervalSinceNow((year + 1) * WINDOW);
    const found = store.eventsMatchingPredicate(
      store.predicateForEventsWithStartDateEndDateCalendars(from, to, $([calendar])));
    for (let i = 0; i < found.count; i++) {
      const event = found.objectAtIndex(i);
      // A recurring event arrives once per occurrence and is one item; it also spans
      // windows. Copying each occurrence would turn a weekly class into fifty events.
      const id = text(event.calendarItemIdentifier);
      if (seen[id]) continue;
      seen[id] = true;
      out.push(event);
    }
  }
  return out;
}

function migrate(store, wanted, apply) {
  const locals = localNamed(store, wanted);
  if (!locals.length) return {local: false, moved: [], removed: 0, notes: []};
  let destination = null;
  if (apply) {
    const made = ensure(store, wanted);
    if (made.error) return made;
    destination = store.calendarWithIdentifier($(made.calendar_id));
    if (!destination || destination.isNil()) {
      return {error: "the iCloud calendar could not be reopened after being created"};
    }
  }
  const moved = [], notes = [];
  for (const calendar of locals) {
    for (const event of eventsIn(store, calendar)) {
      const record = {title: text(event.title), start: iso(event.startDate),
                      uid: text(event.calendarItemIdentifier), new_uid: ""};
      if (!apply) { moved.push(record); continue; }
      const copy = $.EKEvent.eventWithEventStore(store);
      copy.calendar = destination;
      copy.title = event.title;
      copy.startDate = event.startDate;
      copy.endDate = event.endDate;
      copy.allDay = event.allDay;
      // Everything the user may have added to memcal's copy travels with it. Attendees
      // cannot: EventKit exposes them read-only, and memcal never writes any.
      for (const field of ["location", "notes", "url", "timeZone", "recurrenceRules",
                           "alarms"]) {
        try {
          const value = event[field];
          if (value && !value.isNil()) copy[field] = value;
        } catch (_) {}
      }
      const err = Ref();
      if (!store.saveEventSpanCommitError(copy, $.EKSpanFutureEvents, true, err)) {
        notes.push("could not copy " + record.title + ": " + why(err));
        continue;
      }
      record.new_uid = text(copy.calendarItemIdentifier);
      moved.push(record);
    }
  }
  // Removing the calendar removes its events, so it happens only once every copy landed.
  // A partial migration that then deleted the original would destroy the difference.
  let removed = 0;
  if (apply && !notes.length) {
    for (const calendar of locals) {
      const err = Ref();
      if (store.removeCalendarCommitError(calendar, true, err)) removed++;
      else notes.push("could not remove the local calendar: " + why(err));
    }
  }
  return {local: true, moved: moved, removed: removed, notes: notes};
}

function request(store) {
  let answered = false, granted = false;
  const done = function (ok, _err) { granted = ok; answered = true; };
  try {
    if (store.requestFullAccessToEventsWithCompletion) {
      store.requestFullAccessToEventsWithCompletion(done);       // macOS 14+
    } else {
      store.requestAccessToEntityTypeCompletion(EVENT, done);
    }
  } catch (exc) {
    return {granted: false, answered: false, error: String(exc)};
  }
  const deadline = $.NSDate.dateWithTimeIntervalSinceNow(120);
  while (!answered && $.NSDate.date.compare(deadline) < 0) {
    $.NSRunLoop.currentRunLoop.runModeBeforeDate(
      $.NSDefaultRunLoopMode, $.NSDate.dateWithTimeIntervalSinceNow(0.25));
  }
  return {granted: Boolean(granted), answered: answered};
}

//: A repeating event, which the *scripting* interface cannot make at all — Calendar.app's
//: dictionary exposes `recurrence` as a string it will not let you assign, so every
//: publish before this one wrote a single dated copy and the standing appointment the user
//: could see in memcal was never a standing appointment on their phone. EventKit names
//: recurrence properly, and this file already reaches EventKit for the same reason it had
//: to for accounts: the only call available through the other interface has
//: one destination and it is not the one we want.
//: The three-argument initialiser — frequency, interval, end — and not the nine-argument
//: one, which takes `daysOfTheWeek` and friends.
//:
//: Two reasons, and the first is that the long one does not survive contact with JXA. Its
//: selector is a single identifier 108 characters long, and a JavaScript author who wraps
//: it across two lines has written property access on an undefined value, which is
//: exactly what happened: "undefined is not an object". The short form is one line.
//:
//: The second is that it is *sufficient*. A rule with no `daysOfTheWeek` repeats on the
//: weekday of the event's own start date, and `publish_series` already anchors the start
//: on the first scheduled occurrence — so the weekday and the day-of-month are both
//: already encoded in the thing the rule is attached to. Passing them again would be
//: saying the same fact twice, in two places that can then disagree.
function ruleFor(spec) {
  const frequency = spec.cadence === "monthly"
    ? $.EKRecurrenceFrequencyMonthly : $.EKRecurrenceFrequencyWeekly;
  const interval = spec.cadence === "fortnightly" ? 2 : 1;
  return $.EKRecurrenceRule.alloc.initRecurrenceWithFrequencyIntervalEnd(
    frequency, interval, $());
}

//: Publish or update one recurring event, and return the identifier to recognise it by.
//:
//: `EKSpanFutureEvents` on the save is the whole point: it moves the series rather than
//: detaching the first occurrence, which is exactly the difference between "tutoring is
//: Tuesdays now" and "one Tuesday happened once".
function repeat(store, wanted, spec) {
  const target = icloudSource(store);
  if (!target) return {error: "no syncing account is set up in Calendar"};
  let calendar = null;
  for (const candidate of calendarsIn(target)) {
    if (text(candidate.title) === wanted) { calendar = candidate; break; }
  }
  if (!calendar) return {missing: true};

  let event = null;
  if (spec.uid) {
    const found = store.eventWithIdentifier($(spec.uid));
    if (found && !found.isNil()) event = found;
  }
  const fresh = !event;
  if (fresh) {
    event = $.EKEvent.eventWithEventStore(store);
    event.calendar = calendar;
  }
  event.title = $(spec.title);
  const formatter = $.NSISO8601DateFormatter.alloc.init;
  const start = formatter.dateFromString($(spec.start));
  const end = formatter.dateFromString($(spec.end));
  if (!start || start.isNil() || !end || end.isNil()) {
    return {error: "could not read the start or end of the series"};
  }
  // Far endpoint first, for the same reason `PUBLISH_JXA` does it: each assignment is
  // validated on its own, and moving a series later leaves it momentarily ending before
  // it begins, which EventKit refuses outright.
  let currentEnd = null;
  try { currentEnd = event.endDate; } catch (_) {}
  if (!fresh && currentEnd && !currentEnd.isNil()
      && start.compare(currentEnd) > 0) {
    event.endDate = end; event.startDate = start;
  } else {
    event.startDate = start; event.endDate = end;
  }
  event.location = $(spec.location || "");
  event.notes = $(spec.notes || "");
  const rule = ruleFor(spec);
  if (!rule || rule.isNil()) return {error: "could not build the recurrence rule"};
  event.recurrenceRules = $([rule]);
  const err = Ref();
  if (!store.saveEventSpanCommitError(event, $.EKSpanFutureEvents, true, err)) {
    return {error: "Calendar refused the repeating event: " + why(err)};
  }
  return {uid: text(event.calendarItemIdentifier), calendar: text(calendar.title),
          created: fresh};
}

//: Take a repeating event back off the calendar. Whole series, by the uid memcal
//: recorded, in memcal's own calendar — the same three bounds `retract_unpublishable`
//: works under, because this deletes from outside this process too.
function unrepeat(store, wanted, spec) {
  const found = spec.uid ? store.eventWithIdentifier($(spec.uid)) : null;
  if (!found || found.isNil()) return {removed: false};
  if (text(found.calendar.title) !== wanted) return {removed: false, foreign: true};
  const err = Ref();
  if (!store.removeEventSpanCommitError(found, $.EKSpanFutureEvents, true, err)) {
    return {error: "could not remove the repeating event: " + why(err)};
  }
  return {removed: true};
}

function run(argv) {
  const verb = argv[0], wanted = argv[1] || "";
  const store = $.EKEventStore.alloc.init;
  let out;
  if (verb === "request") {
    out = request(store);
  } else if (verb === "where") {
    out = describe(store, wanted);
  } else if (status() !== FULL_ACCESS) {
    // Everything below writes. Without full access the store is a placeholder and a
    // "successful" write goes nowhere at all.
    out = {error: "memcal does not have Calendar access yet"};
  } else if (verb === "ensure") {
    out = ensure(store, wanted);
  } else if (verb === "migrate") {
    out = migrate(store, wanted, argv[2] === "apply");
  } else if (verb === "repeat") {
    out = repeat(store, wanted, JSON.parse(argv[2] || "{}"));
  } else if (verb === "unrepeat") {
    out = unrepeat(store, wanted, JSON.parse(argv[2] || "{}"));
  } else {
    out = {error: "unknown verb " + verb};
  }
  out.status = status();
  return JSON.stringify(out);
}
"""


#: Reminders is a **separate** EventKit entity type with its own authorization and its
#: own TCC consent, so Calendar access grants nothing here. `EKEntityTypeReminder` is 1.
REMINDERS_JXA = r"""
ObjC.import('EventKit');
ObjC.import('Foundation');

const REMINDER = $.EKEntityTypeReminder;
const FULL_ACCESS = 3;

function text(value) { try { return String(value.js); } catch (_) { return ""; } }
function why(ref) {
  try { return String(ref[0].localizedDescription.js); } catch (_) { return ""; }
}
function status() {
  return Number($.EKEventStore.authorizationStatusForEntityType(REMINDER));
}

//: The list memcal owns, in whichever account Reminders defaults to. Unlike calendars,
//: a reminder list does not need coaxing into iCloud: `defaultCalendarForNewReminders`
//: already resolves to the syncing account when there is one, which is the whole of the
//: problem that `ACCOUNT_JXA` exists to solve one entity type over.
function listNamed(store, wanted) {
  const all = store.calendarsForEntityType(REMINDER);
  for (let i = 0; i < all.count; i++) {
    const candidate = all.objectAtIndex(i);
    if (text(candidate.title) === wanted) return candidate;
  }
  return null;
}

function ensureList(store, wanted) {
  const found = listNamed(store, wanted);
  if (found) return found;
  const fresh = $.EKCalendar.calendarForEntityTypeEventStore(REMINDER, store);
  fresh.title = $(wanted);
  const fallback = store.defaultCalendarForNewReminders;
  if (fallback && !fallback.isNil()) fresh.source = fallback.source;
  const err = Ref();
  if (!store.saveCalendarCommitError(fresh, true, err)) {
    return null;
  }
  return fresh;
}

function components(iso) {
  const date = $.NSISO8601DateFormatter.alloc.init.dateFromString($(iso));
  if (!date || date.isNil()) return null;
  const units = $.NSCalendarUnitYear | $.NSCalendarUnitMonth | $.NSCalendarUnitDay
              | $.NSCalendarUnitHour | $.NSCalendarUnitMinute;
  return {date: date,
          parts: $.NSCalendar.currentCalendar.componentsFromDate(units, date)};
}

//: Create or move one reminder. `uid` is memcal's handle on a row it already wrote, so
//: re-running is an update rather than a second copy on their phone.
function put(store, wanted, spec) {
  const list = ensureList(store, wanted);
  if (!list) return {error: "could not create the reminders list " + wanted};
  let item = null;
  if (spec.uid) {
    const found = store.calendarItemWithIdentifier($(spec.uid));
    if (found && !found.isNil()) item = found;
  }
  const fresh = !item;
  if (fresh) {
    item = $.EKReminder.reminderWithEventStore(store);
    item.calendar = list;
  }
  item.title = $(spec.title);
  if (spec.notes) item.notes = $(spec.notes);
  const when = components(spec.due);
  if (!when) return {error: "could not read the reminder time"};
  item.dueDateComponents = when.parts;
  // The alarm is the point. A reminder with a due date and no alarm sits in the app
  // and never says anything, which is indistinguishable from not having set one.
  item.alarms = $([]);
  item.addAlarm($.EKAlarm.alarmWithAbsoluteDate(when.date));
  const err = Ref();
  if (!store.saveReminderCommitError(item, true, err)) {
    return {error: "Reminders refused it: " + why(err)};
  }
  return {uid: text(item.calendarItemIdentifier), list: text(list.title), created: fresh};
}

//: Take one back off, by the uid memcal recorded, and only out of memcal's own list —
//: the same bound `unrepeat` works under, because this deletes outside this process.
function drop(store, wanted, spec) {
  const found = spec.uid ? store.calendarItemWithIdentifier($(spec.uid)) : null;
  if (!found || found.isNil()) return {removed: false};
  if (text(found.calendar.title) !== wanted) return {removed: false, foreign: true};
  const err = Ref();
  if (!store.removeReminderCommitError(found, true, err)) {
    return {error: "could not remove the reminder: " + why(err)};
  }
  return {removed: true};
}

function run(argv) {
  const verb = argv[0], wanted = argv[1] || "";
  const store = $.EKEventStore.alloc.init;
  let out;
  if (verb === "request") {
    let done = false, granted = false;
    store.requestFullAccessToRemindersWithCompletion(function (ok, _err) {
      granted = ok; done = true;
    });
    const until = $.NSDate.dateWithTimeIntervalSinceNow(60);
    while (!done && $.NSDate.date.compare(until) < 0) {
      $.NSRunLoop.currentRunLoop.runModeBeforeDate(
        $.NSDefaultRunLoopMode, $.NSDate.dateWithTimeIntervalSinceNow(0.1));
    }
    out = {granted: granted, answered: done};
  } else if (verb === "status") {
    // Passive, and deliberately above the gate below: "have I been authorized" is the
    // one question that has to be answerable when the answer is no.
    out = {granted: status() === FULL_ACCESS};
  } else if (status() !== FULL_ACCESS) {
    // Everything below writes, and an unauthorized store accepts writes into a
    // placeholder that never reaches the phone — the failure `ACCOUNT_JXA` was
    // rebuilt around. Refuse rather than succeed into nowhere.
    out = {error: "Reminders access has not been granted (status " + status() + ")"};
  } else if (verb === "put") {
    out = put(store, wanted, JSON.parse(argv[2] || "{}"));
  } else if (verb === "drop") {
    out = drop(store, wanted, JSON.parse(argv[2] || "{}"));
  } else {
    out = {error: "unknown verb " + verb};
  }
  out.status = status();
  return JSON.stringify(out);
}
"""


class ReminderError(RuntimeError):
    """Reminders would not take it, or was never authorized to be asked."""


def _reminder_call(verb: str, *args: str, runner=subprocess.run,
                   timeout: int = 120) -> dict:
    """One Reminders verb, as JSON. Mirrors `_account_call` deliberately."""
    if _unavailable(runner):
        raise ReminderError("macOS osascript is unavailable")
    try:
        done = runner(["osascript", "-l", "JavaScript", "-e", REMINDERS_JXA, verb, *args],
                      capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ReminderError(f"Reminders did not answer the {verb} request") from exc
    except OSError as exc:
        raise ReminderError(f"could not reach Reminders: {exc}") from exc
    if done.returncode:
        detail = (done.stderr or done.stdout or "unknown error").strip()
        raise ReminderError(detail[:240])
    try:
        payload = json.loads((done.stdout or "").strip() or "{}")
    except json.JSONDecodeError as exc:
        raise ReminderError("Reminders returned malformed data") from exc
    if payload.get("error"):
        raise ReminderError(str(payload["error"]))
    return payload


def request_reminders_access(*, runner=subprocess.run) -> tuple[bool, str]:
    """Ask macOS for Reminders access. **Opens a consent dialog** — setup only.

    Separate from Calendar in every way that matters: its own entity type, its own TCC
    prompt, its own denial. Granting one grants nothing about the other, and a publish
    that discovers this at 3am has already lost.
    """
    try:
        payload = _reminder_call("request", "", runner=runner, timeout=90)
    except ReminderError as exc:
        return False, f"Reminders access failed: {exc}"
    if payload.get("granted"):
        return True, "Reminders access granted — memcal can write reminders to your phone"
    if not payload.get("answered"):
        return False, "the Reminders access dialog was not answered"
    return False, ("Reminders access denied. Open System Settings → Privacy & Security → "
                   "Reminders and allow the app that runs memcal.")


def publish_reminder(cfg, todo, *, runner=subprocess.run) -> dict:
    """Put one to-do's reminder on their phone. Returns {} when publishing is off.

    Callers must not check `publish_reminders` themselves — this does, so that "off"
    is one decision in one place. Off is the default and stays the default: this writes
    outside the process, which is the whole of invariant 11.
    """
    name = (getattr(cfg, "publish_reminders", "") or "").strip()
    if not name or not todo.remind_at:
        return {}
    spec = {"title": todo.text, "due": todo.remind_at, "uid": todo.reminder_uid or "",
            "notes": f"memcal · {todo.event_title}" if todo.event_title else "memcal"}
    return _reminder_call("put", name, json.dumps(spec), runner=runner)


def retract_reminder(cfg, todo, *, runner=subprocess.run) -> dict:
    """Take a reminder back off the phone when its to-do is closed or dropped.

    A reminder that outlives the thing it was about is an orphaned record
    left behind by something deleted, still asserting itself once a day.
    """
    name = (getattr(cfg, "publish_reminders", "") or "").strip()
    if not name or not todo.reminder_uid:
        return {}
    return _reminder_call("drop", name, json.dumps({"uid": todo.reminder_uid}),
                          runner=runner)


class CalendarAccountError(RuntimeError):
    """EventKit would not say where the calendar lives, or would not put it there."""


def _account_call(verb: str, *args: str, runner=subprocess.run,
                  timeout: int = 120) -> dict:
    """One EventKit verb, as JSON. Raises `CalendarAccountError` with what macOS said."""
    if _unavailable(runner):
        raise CalendarAccountError("macOS osascript is unavailable")
    try:
        done = runner(["osascript", "-l", "JavaScript", "-e", ACCOUNT_JXA, verb, *args],
                      capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise CalendarAccountError(f"Calendar did not answer the {verb} request") from exc
    except OSError as exc:
        raise CalendarAccountError(f"could not ask Calendar about accounts: {exc}") from exc
    if done.returncode:
        detail = (done.stderr or done.stdout or "unknown error").strip()
        raise CalendarAccountError(detail[:240])
    try:
        payload = json.loads((done.stdout or "").strip() or "{}")
    except json.JSONDecodeError as exc:
        raise CalendarAccountError("Calendar returned malformed account data") from exc
    if not isinstance(payload, dict):
        raise CalendarAccountError("Calendar returned an unexpected account result")
    if payload.get("error"):
        raise CalendarAccountError(str(payload["error"]))
    return payload


def request_calendar_access(*, runner=subprocess.run) -> tuple[bool, str]:
    """Ask macOS for EventKit access. **Opens a consent dialog** — `ical setup` only.

    Separate from `permission_status`, which tests the *Apple Events* path the scan uses.
    They are two different macOS permissions and granting one says nothing about the
    other: the scan can be reading the calendar happily while publishing cannot so much
    as name an account.
    """
    try:
        answer = _account_call("request", runner=runner, timeout=150)
    except CalendarAccountError as exc:
        return False, f"Calendar (EventKit) access failed: {exc}"
    if answer.get("granted"):
        return True, "Calendar (EventKit) access granted — memcal can create its calendar"
    if not answer.get("answered"):
        return False, "Calendar (EventKit) access dialog was not answered"
    return False, (
        "Calendar (EventKit) access denied. Open System Settings → Privacy & Security → "
        "Calendars and allow the terminal/Python running memcal."
    )


def account_status(cfg, *, runner=subprocess.run) -> tuple[bool, str]:
    """Where `publish_calendar` lives, and whether that is somewhere that syncs.

    Passive: reads the authorization status and the account list, and opens no dialog, so
    `doctor` can call it. Callers must skip it when `publish_calendar` is empty — nothing
    here should run for a store that has not opted into publishing at all.
    """
    name = (getattr(cfg, "publish_calendar", "") or "").strip()
    if not name:
        return True, "publishing is off (publish_calendar is empty)"
    try:
        where = _account_call("where", name, runner=runner, timeout=60)
    except CalendarAccountError as exc:
        return False, f"could not read Calendar accounts: {exc}"
    if int(where.get("status") or 0) != EK_FULL_ACCESS:
        return False, (f"no Calendar access yet, so '{name}' cannot be placed in iCloud; "
                       "run `memcal ical setup`")
    found = [item for item in where.get("found") or [] if isinstance(item, dict)]
    syncing = [item for item in found if item.get("syncs")]
    local = [item for item in found if not item.get("syncs")]
    if not found:
        target = where.get("icloud_account") or "iCloud"
        if not where.get("icloud_account"):
            return False, f"no syncing account in Calendar to create '{name}' in"
        return True, f"'{name}' will be created in {target} on the first publish"
    if syncing and local:
        # `publish` matches by name, and `calendar.uid()` raises on macOS 26, so there is
        # no identifier to disambiguate with: whichever Calendar.app lists first wins.
        return False, (f"two calendars named '{name}': one in "
                       f"{syncing[0].get('account')} and one local — publishing picks "
                       "whichever is listed first; run `memcal ical migrate`")
    if local:
        return False, (f"'{name}' is local ({local[0].get('account') or 'On My Mac'}) and "
                       "never leaves this Mac; run `memcal ical migrate`")
    return True, f"'{name}' is in {syncing[0].get('account')} and syncs"


def ensure_icloud_calendar(cfg, *, runner=subprocess.run) -> dict:
    """Guarantee `publish_calendar` exists **in a syncing account**, creating it if not."""
    name = (getattr(cfg, "publish_calendar", "") or "").strip()
    if not name:
        raise CalendarAccountError("no calendar to create (publish_calendar is empty)")
    return _account_call("ensure", name, runner=runner)


def migrate_to_icloud(conn: sqlite3.Connection, cfg, *, dry_run: bool = True,
                      runner=subprocess.run) -> list[str]:
    """Move a local `publish_calendar` and everything in it into iCloud."""
    name = (getattr(cfg, "publish_calendar", "") or "").strip()
    log: list[str] = []
    if not name:
        return log
    try:
        # Always planned first, even when applying, because the plan is what the safety
        # check below reads and it has to be read before anything is deleted.
        plan = _account_call("migrate", name, "plan", runner=runner, timeout=300)
    except CalendarAccountError as exc:
        return [f"calendar  could not migrate '{name}': {exc}"]
    if not plan.get("local"):
        log.append(f"calendar  no local calendar named '{name}' to migrate")
        return log

    found = [item for item in plan.get("moved") or [] if isinstance(item, dict)]
    if dry_run:
        for item in found:
            log.append(f"calendar  would move {item.get('title') or '?'} "
                       f"({(item.get('start') or '?')[:10]}) to iCloud")
        log.append(f"calendar  would move {len(found)} event(s) and delete the local "
                   f"'{name}'")
        return log

    # memcal knows what it put there, and that is an independent second opinion on an
    # answer of "nothing". An empty read is also what a silently-failing enumeration
    # returns — a predicate spanning more than four years matches nothing at all — and
    # the step after this one deletes the calendar. Disagreement stops the migration
    # rather than trusting the reading that loses data.
    expected = conn.execute(
        "SELECT count(*) AS n FROM calendar_items"
        "  WHERE published = 1 AND calendar_name = ?", (name,)).fetchone()["n"]
    if not found and expected:
        return [f"calendar  refusing to migrate '{name}': Calendar reports it empty but "
                f"memcal published {expected} event(s) into it. Nothing was changed."]

    try:
        result = _account_call("migrate", name, "apply", runner=runner, timeout=300)
    except CalendarAccountError as exc:
        return [f"calendar  could not migrate '{name}': {exc}"]
    moved = [item for item in result.get("moved") or [] if isinstance(item, dict)]

    rebound = 0
    for item in moved:
        rebound += _rebind(conn, str(item.get("uid") or ""), str(item.get("new_uid") or ""))
        log.append(f"calendar  moved {item.get('title') or '?'} to iCloud")
    conn.commit()
    for note in result.get("notes") or []:
        log.append(f"calendar  {note}")
    if result.get("removed"):
        log.append(f"calendar  deleted the local '{name}'")
    else:
        log.append(f"calendar  the local '{name}' was left in place")
    log.append(f"calendar  {len(moved)} event(s) moved, {rebound} memcal record(s) re-pointed")
    return log


def _rebind(conn: sqlite3.Connection, old_uid: str, new_uid: str) -> int:
    """Re-point every `calendar_items` row for `old_uid` at the copy in iCloud."""
    if not old_uid or not new_uid or old_uid == new_uid:
        return 0
    changed = 0
    for row in conn.execute(
        "SELECT identity, starts_at FROM calendar_items WHERE event_uid = ?", (old_uid,)
    ).fetchall():
        occurrence = ("" if row["identity"] == _identity_of(old_uid, "")
                      else str(row["starts_at"] or ""))
        moved_to = _identity_of(new_uid, occurrence)
        if moved_to == row["identity"]:
            continue
        # Anything already sitting on the new identity is a copy of this same event; the
        # primary key cannot hold both.
        conn.execute("DELETE FROM calendar_items WHERE identity = ?", (moved_to,))
        conn.execute(
            "UPDATE calendar_items SET identity = ?, event_uid = ? WHERE identity = ?",
            (moved_to, new_uid, row["identity"]))
        changed += 1
    return changed


# --------------------------------------------------------------- publishing --
# This is the only external write path. Publish only committed rows to memcal's own
# calendar, never the user's default calendar, so mistaken writes remain visible and
# reversible by removing that calendar.

PUBLISH_JXA = r"""
function run(argv) {
  const app = Application("Calendar");
  const wanted = argv[0];
  let calendar = null;
  for (const candidate of app.calendars()) {
    let name = "";
    try { name = String(candidate.name()); } catch (_) { continue; }
    let writable = false;
    try { writable = Boolean(candidate.writable()); } catch (_) {}
    if (name === wanted && writable) { calendar = candidate; break; }
  }
  // Deliberately does not create the calendar. Pushing a new one onto the application's
  // calendar list can only make a *local* one — Calendar.app's scripting dictionary has
  // no account class at all, so there is no other destination to ask for — and
  // a local calendar never leaves this Mac, so every published event was invisible on
  // the phone it was published for. Creation belongs to `ensure_icloud_calendar`, which
  // goes through EventKit and can name the account. `publish` calls it and retries.
  if (!calendar) return JSON.stringify({missing: true});
  const spec = JSON.parse(argv[1]);
  const start = new Date(spec.start);
  const end = new Date(spec.end);
  let event = null;
  if (spec.uid) {
    try {
      const found = calendar.events.whose({uid: spec.uid})();
      if (found.length) event = found[0];
    } catch (_) {}
  }
  if (!event) {
    event = app.Event({summary: spec.title, startDate: start, endDate: end});
    calendar.events.push(event);
  } else {
    event.summary = spec.title;
    // A new event is built timed and flipped to all-day *after* its dates are set, so
    // the exclusive DTEND lands on the right last day. An event already carrying
    // `alldayEvent` snaps an assigned `endDate` to a whole day it then occupies, so the
    // identical value runs one day long — which is why correcting the Montana trip to
    // end Aug 23 put Aug 24 on the real calendar. Drop the flag first and update takes
    // the same path as create; it goes back on below.
    if (spec.all_day) { try { event.alldayEvent = false; } catch (_) {} }
    // Each assignment is validated on its own, so moving an event *later* by writing
    // startDate first leaves it momentarily ending before it begins and Calendar.app
    // refuses the whole write: "The start date must be before the end date." Push the
    // far endpoint out first, whichever one that is.
    let currentEnd = null;
    try { currentEnd = event.endDate(); } catch (_) {}
    if (currentEnd && start > currentEnd) {
      event.endDate = end;
      event.startDate = start;
    } else {
      event.startDate = start;
      event.endDate = end;
    }
  }
  event.alldayEvent = Boolean(spec.all_day);
  if (spec.location !== null) event.location = spec.location;
  if (spec.description !== null) event.description = spec.description;
  // `calendar.uid()` raises -10000 on a calendar created a moment ago — after the
  // event has already been written, so an unguarded read here loses a successful write
  // and reports a failure. The name is the fallback and `publish_pending` does not
  // depend on it: a published event is recognised again by its own uid.
  let calendarUid = "";
  try { calendarUid = String(calendar.uid()); } catch (_) { calendarUid = String(calendar.name()); }
  return JSON.stringify({uid: String(event.uid()), calendar: String(calendar.name()),
                         calendar_uid: calendarUid});
}
"""


RETRACT_JXA = r"""
function run(argv) {
  const app = Application("Calendar");
  const wanted = argv[0], uid = argv[1];
  for (const candidate of app.calendars()) {
    let name = "";
    try { name = String(candidate.name()); } catch (_) { continue; }
    if (name !== wanted) continue;
    let found = [];
    try { found = candidate.events.whose({uid: uid})(); } catch (_) { found = []; }
    if (found.length) { found[0].delete(); return JSON.stringify({removed: true}); }
  }
  return JSON.stringify({removed: false});
}
"""


class PublishError(RuntimeError):
    """Calendar.app would not take the write, with the reason it gave."""


def _event_window(event) -> tuple[str, str, bool]:
    """Start and end as Calendar.app wants them, plus whether it is an all-day event.

    A memcal row often has a day and no time, which is exactly an all-day event. One
    with a time gets an hour, because Calendar.app has no notion of "starts at 7 and I
    have not thought about the end" and a zero-length event renders as a sliver.
    """
    start = db.parse_date(event.date)
    last = db.parse_date(event.until) if (event.until and event.until > event.date) else start
    clock = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", (event.time or "").strip())
    if not clock:
        # DTEND is exclusive for all-day events, the same rule `_normalized` reads back.
        return (f"{start.isoformat()}T00:00:00",
                f"{(last + timedelta(days=1)).isoformat()}T00:00:00", True)
    hour, minute = int(clock.group(1)), int(clock.group(2))
    finish = last if last > start else start
    # Arithmetic on the datetime, not `(hour + 1) % 24` on the clock: an event at 23:00
    # wrapped to 00:00 on the *same* day, which is an end before its own start, and
    # Calendar.app rejects the whole write rather than any one field.
    begins = datetime.combine(start, clock_time(hour, minute))
    ends = datetime.combine(finish, clock_time(hour, minute)) + timedelta(hours=1)
    return begins.isoformat(timespec="seconds"), ends.isoformat(timespec="seconds"), False


def _write_event(name: str, spec: dict, *, runner=subprocess.run) -> dict:
    """One `PUBLISH_JXA` call. `{"missing": True}` means the calendar is not there yet."""
    try:
        done = runner(["osascript", "-l", "JavaScript", "-e", PUBLISH_JXA, name,
                       json.dumps(spec)], capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired as exc:
        raise PublishError("Calendar.app did not answer within a minute") from exc
    except OSError as exc:
        raise PublishError(f"could not write to Calendar.app: {exc}") from exc
    if done.returncode:
        detail = (done.stderr or done.stdout or "unknown Calendar error").strip()
        raise PublishError(f"Calendar.app refused the write: {detail[:240]}")
    try:
        written = json.loads((done.stdout or "").strip() or "{}")
    except json.JSONDecodeError as exc:
        raise PublishError("Calendar.app returned malformed data") from exc
    if not isinstance(written, dict):
        raise PublishError("Calendar.app returned an unexpected result")
    return written


def publish(conn: sqlite3.Connection, cfg, event, *, runner=subprocess.run) -> dict:
    """Put one committed row on the user's real calendar. Returns what was written.

    Idempotent by `calendar_items`: the same memcal row updates the same Calendar.app
    event rather than making a second one, which matters because a row is confirmed once
    and then moved, renamed and re-confirmed.
    """
    name = (getattr(cfg, "publish_calendar", "") or "").strip()
    if not name:
        raise PublishError("no calendar to publish to (publish_calendar is empty)")
    if _unavailable(runner):
        raise PublishError("macOS osascript is unavailable")

    start, end, all_day = _event_window(event)
    existing = conn.execute(
        """SELECT identity, event_uid FROM calendar_items
            WHERE event_key = ? AND published = 1 ORDER BY updated_at DESC LIMIT 1""",
        (event.key,)).fetchone()
    spec = {
        "uid": existing["event_uid"] if existing else "",
        "title": event.title,
        "start": start,
        "end": end,
        "all_day": all_day,
        # Place and link together — see `publish_location`. `location` is what the phone
        # turns into a Join button, so a online row that publishes "Online" and
        # keeps the URL in the notes has made the trip and left the useful half behind.
        "location": publish_location(event.location, event.join_url),
        # Whose idea this was, in the one field a calendar entry has for saying so. On a
        # phone at the door this is the difference between a mystery and a plan.
        "description": _published_note(event),
    }
    written = _write_event(name, spec, runner=runner)
    if written.get("missing"):
        # The calendar does not exist yet, or the user deleted it. Make it *in iCloud* —
        # the scripting interface would have made a local one, which is the whole defect
        # this path exists to avoid — and write again. Self-healing, and it costs the
        # extra call only on a cold start.
        try:
            ensure_icloud_calendar(cfg, runner=runner)
        except CalendarAccountError as exc:
            raise PublishError(f"no calendar named '{name}' and it could not be "
                               f"created in iCloud: {exc}") from exc
        written = _write_event(name, spec, runner=runner)
        if written.get("missing"):
            raise PublishError(f"Calendar.app cannot see the '{name}' calendar even "
                               "after it was created in iCloud")

    stamp = db.now()
    identity = _identity({"calendar_uid": written.get("calendar_uid", ""),
                          "uid": written.get("uid", ""), "recurrence": ""})
    conn.execute(
        """INSERT INTO calendar_items(
               identity, calendar_uid, calendar_name, event_uid, event_key, starts_at,
               subscribed, provider, active, published, published_state,
               last_seen_at, updated_at)
           VALUES(?,?,?,?,?,?,0,'ical',1,1,?,?,?)
           ON CONFLICT(identity) DO UPDATE SET
               event_key=excluded.event_key, starts_at=excluded.starts_at,
               published=1, active=1, revision=NULL,
               published_state=excluded.published_state,
               last_seen_at=excluded.last_seen_at, updated_at=excluded.updated_at""",
        (identity, written.get("calendar_uid", ""), written.get("calendar", name),
         # `start` is what Calendar.app was *handed* — naive local, because that is what
         # `PUBLISH_JXA`'s `new Date()` wants. What goes in the column is the instant.
         written.get("uid", ""), event.key, db.utc_stamp(start), _published_state(event),
         stamp, stamp))
    conn.commit()
    return {"calendar": written.get("calendar", name), "uid": written.get("uid", ""),
            "title": event.title, "start": start, "all_day": all_day}


#: The `calendar_items.event_key` a published *rule* is filed under. Not an `events.key`,
#: because a rule is not an occurrence and has no date to embed — which is the one thing
#: `events.key` always does, and the reason `calendar_items.event_key` went stale on a
#: re-dated row in the first place.
def series_key(slug: str) -> str:
    return f"series:{db.slugify(slug or '')}"


def _series_window(rule, on) -> tuple[str, str]:
    """The first occurrence, as a start and end EventKit will accept.

    The rule supplies the day and the clock; an hour is the same default a timed row
    already gets from `_event_window`, for the same reason — Calendar has no notion of
    "starts at 1 and I have not thought about the end".
    """
    clock = re.match(r"^([01]?\d|2[0-3]):([0-5]\d)$", (rule.time or "").strip())
    hour, minute = (int(clock.group(1)), int(clock.group(2))) if clock else (9, 0)
    # EventKit's ISO-8601 parser rejects naive timestamps; include the local offset.
    begins = datetime.combine(on, clock_time(hour, minute)).astimezone()
    return (begins.isoformat(timespec="seconds"),
            (begins + timedelta(hours=1)).isoformat(timespec="seconds"))


def _series_state(rule) -> str:
    """What memcal published about a rule, so a later run can tell "already there" from
    "the schedule moved". Same trick as `_published_state`, same reason."""
    fields = ("title", "cadence", "weekday", "day_of_month", "time",
              "effective_on", "status")
    return "|".join(
        [str(getattr(rule, name, "") or "") for name in fields]
        + [publish_location(getattr(rule, "location", ""),
                            getattr(rule, "join_url", ""))])


def publish_series(conn: sqlite3.Connection, cfg, rule, *,
                   runner=subprocess.run) -> dict:
    """Put the *rule* on their real calendar, as one genuinely repeating event."""
    name = (getattr(cfg, "publish_calendar", "") or "").strip()
    if not name:
        raise PublishError("no calendar to publish to (publish_calendar is empty)")
    if not rule.projectable:
        raise PublishError(f"{rule.slug} has no schedule to publish")
    from .. import series as series_mod                              # noqa: PLC0415
    # Anchored on the first scheduled day that is **still ahead and not already excepted**.
    # Not `effective_on`: a schedule in force since Tuesday would put a repeating event on
    # that Tuesday, and the whole reason this rule exists is that the Tuesday in question
    # did not happen — it moved to Wednesday, which is published separately as the
    # exception. Anchoring there would have shown them both, one of them a meeting nobody
    # attended, on a real calendar on their phone.
    start = db.today()
    if rule.effective_on:
        start = max(start, db.parse_date(rule.effective_on))
    skip = series_mod.covered(conn, rule.slug)
    first = next((day for day in series_mod.occurrences(
        None, start, start + timedelta(days=400), series=rule)
        if day.isoformat() not in skip), None)
    if first is None:
        raise PublishError(f"{rule.slug} has no upcoming occurrence to anchor on")

    key = series_key(rule.slug)
    known = conn.execute(
        "SELECT identity, event_uid, published_state FROM calendar_items"
        "  WHERE event_key = ? AND published = 1 LIMIT 1", (key,)).fetchone()
    start, end = _series_window(rule, first)
    spec = {
        "uid": known["event_uid"] if known else "",
        "title": rule.title,
        "start": start, "end": end,
        "cadence": rule.cadence, "weekday": rule.weekday,
        "day_of_month": rule.day_of_month,
        "location": publish_location(rule.location, rule.join_url),
        "notes": "Added by memcal.",
    }
    written = _account_call("repeat", name, json.dumps(spec), runner=runner)
    if written.get("missing"):
        ensure_icloud_calendar(cfg, runner=runner)
        written = _account_call("repeat", name, json.dumps(spec), runner=runner)
    if written.get("missing"):
        raise PublishError(f"Calendar cannot see the '{name}' calendar even after it "
                           "was created in iCloud")

    stamp = db.now()
    identity = _identity_of(written.get("uid", ""), "")
    conn.execute(
        """INSERT INTO calendar_items(
               identity, calendar_uid, calendar_name, event_uid, event_key, starts_at,
               subscribed, provider, active, published, published_state,
               last_seen_at, updated_at)
           VALUES(?,?,?,?,?,?,0,'ical',1,1,?,?,?)
           ON CONFLICT(identity) DO UPDATE SET
               event_key=excluded.event_key, starts_at=excluded.starts_at,
               published=1, active=1, revision=NULL,
               published_state=excluded.published_state,
               last_seen_at=excluded.last_seen_at, updated_at=excluded.updated_at""",
        # `start` carries an offset because EventKit's parser rejects a naive string —
        # see `_series_window`. That is a fact about the *interface*, and it does not get
        # to decide what the column looks like.
        (identity, "", written.get("calendar", name), written.get("uid", ""), key,
         db.utc_stamp(start), _series_state(rule), stamp, stamp))
    conn.commit()
    return {"calendar": written.get("calendar", name), "uid": written.get("uid", ""),
            "title": rule.title, "start": start, "created": written.get("created")}


def publish_schedules(conn: sqlite3.Connection, cfg, *, slugs=None,
                      runner=subprocess.run) -> list[str]:
    """Every active rule that is not already on the calendar as it now stands.

    Errors are returned, never raised, for the same reason `publish_pending` does it:
    Calendar being closed or slow is not a reason for a nightly pass to fail.
    """
    if not (getattr(cfg, "publish_calendar", "") or "").strip():
        return []
    from .. import series as series_mod                              # noqa: PLC0415
    if slugs is None:
        # Active rules *and* whatever is already on the calendar. Iterating the active
        # ones alone means a series the user has stopped going to leaves the calendar list the
        # moment it is ended, so nothing is ever left to take the repeating event back
        # down — publishing one-way again, which is the bug `retract_unpublishable` was
        # written for one level down.
        slugs = [row["slug"] for row in conn.execute(
            "SELECT slug FROM series WHERE status = 'active'"
            " UNION SELECT substr(event_key, 8) FROM calendar_items"
            "  WHERE published = 1 AND event_key LIKE 'series:%'")]
    rules = [r for r in (series_mod.get(conn, s) for s in slugs) if r]
    log = []
    for rule in rules:
        known = conn.execute(
            "SELECT published_state FROM calendar_items"
            "  WHERE event_key = ? AND published = 1 LIMIT 1",
            (series_key(rule.slug),)).fetchone()
        if rule.status != "active" or not rule.projectable:
            if known:
                log.extend(retract_series(conn, cfg, rule.slug, runner=runner))
            continue
        if known and known["published_state"] == _series_state(rule):
            continue
        try:
            written = publish_series(conn, cfg, rule, runner=runner)
        except (PublishError, CalendarAccountError) as exc:
            log.append(f"calendar  could not publish the {rule.title} schedule: {exc}")
            continue
        log.append(f"calendar  {'added' if written.get('created') else 'updated'} "
                   f"{rule.title} — {rule.phrase} — in {written['calendar']}")
        trace.stamp(conn, kind="series", ref=rule.slug, verb="published",
                    entity=f"calendar:{written['calendar']}", stage="ical")
    conn.commit()
    return log


def retract_series(conn: sqlite3.Connection, cfg, slug: str, *,
                   runner=subprocess.run) -> list[str]:
    """Take a repeating event back off the calendar, whole.

    Only ever an event memcal published, by the uid it recorded, in memcal's own
    calendar — the JXA checks the third of those itself, because this deletes from
    outside this process and the bounds belong next to the delete.
    """
    name = (getattr(cfg, "publish_calendar", "") or "").strip()
    key = series_key(slug)
    known = conn.execute(
        "SELECT identity, event_uid FROM calendar_items"
        "  WHERE event_key = ? AND published = 1 LIMIT 1", (key,)).fetchone()
    if not name or not known:
        return []
    try:
        done = _account_call("unrepeat", name, json.dumps({"uid": known["event_uid"]}),
                             runner=runner)
    except CalendarAccountError as exc:
        return [f"calendar  could not retract the {slug} schedule: {exc}"]
    if done.get("error"):
        return [f"calendar  could not retract the {slug} schedule: {done['error']}"]
    conn.execute(
        "UPDATE calendar_items SET published = 0, published_state = NULL, active = 0,"
        "       updated_at = ? WHERE identity = ?", (db.now(), known["identity"]))
    conn.commit()
    return [f"calendar  removed the {slug} schedule from {name}"] if done.get("removed") \
        else []


def published_series_uids(conn: sqlite3.Connection) -> set[str]:
    """Apple uids of the repeating events memcal itself put on the calendar.

    A recurring event comes back from a scan once per occurrence, each with the same uid
    and a different start, so without this the next read files fifty new rows from
    memcal's own write and then attaches them to the series as evidence that the series
    exists. That is the loop that made a joke about Chili's cite memcal's own calendar
    entry as proof, and it is worse here because it repeats forever.
    """
    return {str(row["event_uid"]) for row in conn.execute(
        "SELECT event_uid FROM calendar_items"
        "  WHERE published = 1 AND event_key LIKE 'series:%' AND event_uid <> ''")}


def _published_state(event) -> str:
    """What memcal published, so a later run can tell "already there" from "has moved".

    Only the fields memcal writes. It cannot be compared against the calendar copy
    instead, because the user is allowed to add things to their copy — an alert, an
    invitee — and none of that means the row changed.
    """
    # `location` is the *composed* one, so a row whose join link changed — or one
    # published before `publish_location` existed — reads as moved and re-publishes
    # itself once. Comparing the raw column instead is what would have left every
    # already-published tutoring occurrence sitting there with an empty Join button.
    fields = ("title", "date", "until", "time")
    return "|".join(
        [" ".join(str(getattr(event, name, "") or "").split()) for name in fields]
        + [publish_location(getattr(event, "location", ""),
                            getattr(event, "join_url", ""))])


def _published_note(event) -> str:
    bits = ["Added by memcal."]
    if event.participants:
        bits.append("With " + ", ".join(event.participants) + ".")
    if event.note:
        bits.append(event.note)
    # No `Join:` line. The link goes in `location` now, which is the field the Join
    # button is built from; printing it here as well put the same URL on the row twice
    # and taught nobody where it belongs.
    return " ".join(bits)


def publish_pending(conn: sqlite3.Connection, cfg, *, keys=None,
                    runner=subprocess.run) -> list[str]:
    """Put every committed row that is not already on the real calendar onto it.

    Idempotent and cheap to call: a row already published, and unchanged since, costs
    one SQL lookup and no Apple Event. `keys` narrows it to what a caller just wrote;
    without it, this is the sweep that catches up anything a failed write missed.

    Errors are returned, never raised. Calendar.app being closed, denied or slow is not
    a reason for a dream pass to fail — the row is already in memcal, which is the
    system of record, and the next pass will try again.
    """
    if not (getattr(cfg, "publish_calendar", "") or "").strip():
        return []
    if keys is None:
        rows = conn.execute(
            "SELECT * FROM events WHERE status = 'confirmed' AND date >= ?",
            ((db.today() - timedelta(days=1)).isoformat(),)).fetchall()
    else:
        marks = ",".join("?" * len(keys)) or "''"
        rows = conn.execute(
            f"SELECT * FROM events WHERE key IN ({marks})", list(keys)).fetchall()
    log = []
    for row in rows:
        event = events.Event.from_row(row)
        if not publishable(event):
            continue
        # A rule already on the calendar puts its own occurrence there. Publishing the
        # row as well would show them two tutoring appointments on one Tuesday, both real,
        # both memcal's. The exception is exactly the row that contradicts the rule, so
        # it is the one that still needs a copy of its own.
        if event.series and not event.instead_of and conn.execute(
                "SELECT 1 FROM calendar_items WHERE event_key = ? AND published = 1",
                (series_key(event.series),)).fetchone():
            continue
        known = conn.execute(
            "SELECT identity, published_state FROM calendar_items"
            "  WHERE event_key = ? AND published = 1", (event.key,)).fetchone()
        if known and known["published_state"] == _published_state(event):
            continue
        try:
            written = publish(conn, cfg, event, runner=runner)
        except PublishError as exc:
            log.append(f"calendar  could not publish {event.title}: {exc}")
            continue
        log.append(f"calendar  {'updated' if known else 'added'} "
                   f"{written['title']} to {written['calendar']}")
        trace.stamp(conn, kind="event", ref=event.key, verb="published",
                    entity=f"calendar:{written['calendar']}", stage="ical")
    conn.commit()
    return log


def retract_unpublishable(conn: sqlite3.Connection, cfg, *, dry_run: bool = True,
                          runner=subprocess.run) -> list[str]:
    """Take back the rows memcal put on the real calendar that no longer belong there."""
    name = (getattr(cfg, "publish_calendar", "") or "").strip()
    log: list[str] = []
    if not name:
        return log
    for row in conn.execute(
        """SELECT c.event_key, c.event_uid, c.identity FROM calendar_items c
            WHERE c.published = 1 AND c.calendar_name = ? AND c.event_uid != ''""",
        (name,),
    ).fetchall():
        event = events.get(conn, row["event_key"])
        if event is not None and publishable(event):
            continue
        why = ("no memcal row answers to it any more" if event is None
               else f"{event.kind}/{event.status}, not a confirmed commitment")
        title = event.title if event is not None else row["event_key"]
        if dry_run:
            log.append(f"calendar  would remove {title} from {name} — {why}")
            continue
        try:
            done = runner(["osascript", "-l", "JavaScript", "-e", RETRACT_JXA, name,
                           row["event_uid"]], capture_output=True, text=True, timeout=60)
        except (subprocess.TimeoutExpired, OSError) as exc:
            log.append(f"calendar  could not remove {title}: {exc}")
            continue
        if done.returncode:
            log.append(f"calendar  could not remove {title}: "
                       f"{(done.stderr or '').strip()[:160]}")
            continue
        conn.execute(
            "UPDATE calendar_items SET published = 0, published_state = NULL, active = 0,"
            "       updated_at = ? WHERE identity = ?", (db.now(), row["identity"]))
        log.append(f"calendar  removed {title} from {name} — {why}")
        trace.stamp(conn, kind="event", ref=row["event_key"], verb="retracted",
                    entity=f"calendar:{name}", stage="ical")
    conn.commit()
    return log


def publishable(event) -> bool:
    """Is this a thing the user has actually committed to?

    `confirmed` is the store's word for "the user said yes, or it is booked" — the only status
    that means a person expects them somewhere. Everything else on the memcal calendar is
    optimistic by design, and optimism does not belong on a real calendar.
    """
    return (getattr(event, "status", "") == "confirmed"
            and getattr(event, "kind", "") == "commitment"
            # A row that came *from* a calendar is already on one.
            and not str(getattr(event, "origin", "") or "").startswith(
                events.OBSERVED_ORIGINS))


@register
class ICalSource(Source):
    name = "ical"
    description = "macOS Calendar.app (created and subscribed calendars)"
    order = 45
    # A calendar that has not changed since yesterday is a healthy calendar, and
    # `reconcile_deleted` depends on reading a *complete* snapshot rather than a
    # trickle of new items. This is the source the `last_success` marker was added for.
    health = "snapshot"

    def fetch(self, conn: sqlite3.Connection, cfg, report, limit: int) -> None:
        # A snapshot must be complete for disappearance to mean anything, so `limit`
        # intentionally does not truncate this local read.
        #
        # What it *is* bounded by is the window, because the window is the runtime: one
        # Apple Event per event, ~60ms each. Reading a full year every time someone
        # presses Collect spends most of those seconds on the far half of it, where
        # nothing has moved since yesterday — Rosh Hashanah 2027 does not need checking
        # at lunchtime. So the near half is read every run and the whole year daily, and
        # `scan_end` tells `reconcile_missing` which of the two it is looking at, since
        # a row can only be judged missing from a window that was actually read.
        lower = db.today() - timedelta(days=LOOKBACK_DAYS)
        full = _due_a_full_scan(conn)
        upper = db.today() + timedelta(
            days=LOOKAHEAD_DAYS if full else NEAR_LOOKAHEAD_DAYS)
        # One bar over all three phases, including `ingest_snapshot`'s, which is why the
        # report's own callback is replaced for the rest of this fetch.
        report.progress = bar = base.phased(report.progress, SNAPSHOT_PHASES)
        if bar:
            bar("asking Calendar.app for the window", phase="checking")
        snapshot = _calendar_snapshot(lower.isoformat(), upper.isoformat(), progress=bar)
        items = snapshot.items
        if full:
            db.set_meta(conn, "source.ical.last_full_scan", db.now())
        else:
            report.notes.append(
                f"read {NEAR_LOOKAHEAD_DAYS} days ahead; the full year is read once a day")
        ingest_snapshot(
            conn,
            cfg,
            items,
            scan_start=lower.isoformat(),
            scan_end=upper.isoformat(),
            report=report,
            unreadable=snapshot.unreadable,
        )
        # An unchanged calendar still needs to prove it is healthy, but operational
        # bookkeeping is not a source item and does not belong in the Gate/archive.
        #
        # A partial read still writes this: the events that did arrive are as fresh as
        # any other run's and the source is reachable, which is the whole of what this
        # marker asserts. What the partial read cost is in `report.notes`, which is
        # persisted to `collection_sources.note` and rendered by both surfaces — not
        # folded into a health flag whose meaning is "did the read happen".
        db.set_meta(conn, "source.ical.last_success", db.now())
        db.set_meta(conn, "source.ical.last_count", str(len(items)))

    def check(self, cfg) -> tuple[bool, str]:
        # The one place in this file where the platform question is the whole answer:
        # `check` is the source's health declaration, and on a host with no `osascript`
        # this source really is unreachable. It takes no transport, so there is nothing
        # a caller could have injected to make it otherwise — see `_unavailable` for the
        # five sites where there is.
        if not _have_osascript():
            return False, "macOS osascript is unavailable"
        # A real read is the only reliable permission test, and a real read may open
        # macOS consent UI. `sources` and `doctor` must never surprise someone with it.
        return True, "available; permission is checked explicitly by `memcal ical setup`"
