"""Collisions between daytime typed writes and the nightly pass.

Scenarios pin every operation's clock and distinguish message time from arrival time.
They grade state at checkpoints across three boundaries: deterministic apply and replay,
retrieval, and a live model pass. Ground truth queries user-visible state and is never
derived from the matcher. Oracle diffs are deterministic-layer input only and omit event
keys unless a case specifically tests a keyed amendment.
"""

from __future__ import annotations

import random
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from memcal import (actions, archive, brief, db, events, gate, identity, live,
                    pending, todos, wiki)
from memcal.config import Config
from memcal.dream import apply as apply_stage
from memcal.dream import bundle as bundle_stage
from memcal.dream import merge as merge_stage
from memcal.dream import run as dream_run
from memcal.dream import propose as propose_stage
from memcal.dream import sweep as sweep_stage
from memcal.sources import base

EMPTY = {"events": [], "todos": [], "wiki": [], "questions": []}

#: What a failure is *about*, so a run reports duplicate creation separately from a lost
#: correction. A single blended score hides the trade the whole exercise is about:
#: a matcher broad enough to stop duplicates is the same matcher that merges two real
#: appointments, and one number cannot show both moving.
CATEGORIES = (
    "duplicate",       # the same occasion written twice
    "false-merge",     # two occasions collapsed into one
    "correction",      # a newer decision lost or walked backwards
    "cancellation",    # a cancelled plan still active, or a live one wrongly cancelled
    "evidence",        # the row is right and cannot say what it was built from
    "identity",        # a stable target moved, or a link followed the wrong row
    "retrieval",       # the pass was never shown what it needed
    "replay",          # a re-run duplicated rows or effects
    "brief",           # what the assistant is told about all of it
)

#: Held out from tuning. Nothing in the implementation may be adjusted while looking at
#: these; they exist to say whether a change generalized.
RESERVED = frozenset({"f6.same-provider", "f8.long-gap", "f10.linked-work"})


# --------------------------------------------------------------------- world --

CAST = {
    "Alex Rivera": "+19175551001",
    "Riley Morgan": "+19175551002",
    "Cameron Ortiz": "+19175551003",
    "Devon Park": "+19175551004",
    "Nadia Okoro": "+19175551005",
}

#: The scenarios all live in one fictional fortnight, so "day 1" means the same Monday
#: everywhere and a scenario that needs eleven days of silence can have it.
START = "2026-09-07"          # Monday


def day(offset: int, clock: str = "09:00") -> str:
    """`day(0, "18:30")` — an absolute moment, so no operation depends on the machine."""
    stamp = db.parse_date(START) + timedelta(days=offset)
    return f"{stamp.isoformat()}T{clock}:00"


# ---------------------------------------------------------------- operations --

@dataclass
class Op:
    """One timed thing that happens. `at` is when the store experiences it."""

    at: str
    kind: str                                   # message | tool | pass | retry | mark
    #: For a message: the moment it was *written*, when that differs from arrival.
    written_at: str = ""
    payload: dict = field(default_factory=dict)
    label: str = ""


def msg(at: str, text: str, *, who: str, stream: str = "imessage",
        thread: str | None = None, from_me: bool = False, written_at: str = "",
        alt: str = "", group: bool = False, verdict: str | None = None) -> Op:
    """A source line. `alt` is the same statement said differently, for the wording variant."""
    return Op(at=at, kind="message", written_at=written_at, payload=dict(
        text=text, alt=alt, who=who, stream=stream, thread=thread, from_me=from_me,
        group=group, verdict=verdict))


def agent(at: str, text: str, *, alt: str = "") -> Op:
    """The user talking to their assistant. Arrives on the `agent` stream, addressed to a machine."""
    return Op(at=at, kind="message", payload=dict(
        text=text, alt=alt, who="me", stream="agent", thread="conversation",
        from_me=True, group=False, verdict=None, addressed_to="machine"))


def tool(at: str, call: str, *, drop: tuple[str, ...] = (), **args) -> Op:
    """A typed `memcal.live` call — the same function `mcp_server` dispatches to.

    `drop` names arguments the `sparse` variant removes, so "the agent was told less"
    is expressible without a second copy of the scenario.
    """
    return Op(at=at, kind="tool", payload=dict(call=call, args=args, drop=drop))


def nightly(at: str, *, label: str = "") -> Op:
    return Op(at=at, kind="pass", label=label)


def retry(at: str, *, label: str = "") -> Op:
    """Re-read the traffic the previous pass just read. Must change nothing."""
    return Op(at=at, kind="retry", label=label)


def mark(at: str, name: str) -> Op:
    """A checkpoint. Checks filed `after=name` are graded here, on this state."""
    return Op(at=at, kind="mark", label=name)


# -------------------------------------------------------------------- checks --

@dataclass
class Check:
    id: str
    after: str                                  # the checkpoint name it grades
    category: str
    layer: str                                  # apply | retrieval | model
    fn: Callable[["Ctx"], tuple[bool, str]]
    frontier: bool = False


def check(cid: str, after: str, category: str, fn, *, layer: str = "apply",
          frontier: bool = False) -> Check:
    if category not in CATEGORIES:
        raise ValueError(f"unknown category {category}")
    if layer not in ("apply", "retrieval", "model"):
        raise ValueError(f"unknown layer {layer}")
    return Check(id=cid, after=after, category=category, layer=layer, fn=fn,
                 frontier=frontier)


class Ctx:
    """What a check may look at: the store, and what the last pass was shown."""

    def __init__(self, conn: sqlite3.Connection, cfg: Config):
        self.conn = conn
        self.cfg = cfg
        #: The rendered context of the most recent pass, per bundle entity. Model-free:
        #: this is `propose.build_suffix` output, not a model reply.
        self.shown: dict[str, str] = {}
        self.pass_log: list[str] = []
        #: How many model calls this scenario has actually made. Zero on a `model` run is
        #: the failure that made the whole layer meaningless, so it is counted rather
        #: than assumed.
        self.model_calls = 0

    # -- rows --
    def rows(self, title_like: str, *, active: bool = False) -> list[events.Event]:
        needle = title_like.casefold()
        out = []
        for row in self.conn.execute("SELECT * FROM events ORDER BY date, id"):
            event = events.Event.from_row(row)
            if needle not in (event.title or "").casefold():
                continue
            if active and event.status == "declined":
                continue
            out.append(event)
        return out

    def one(self, title_like: str, **kw) -> events.Event | None:
        found = self.rows(title_like, **kw)
        return found[0] if len(found) == 1 else None

    def on(self, when: str) -> list[events.Event]:
        return [events.Event.from_row(row) for row in self.conn.execute(
            "SELECT * FROM events WHERE date = ? ORDER BY id", (when,))]

    def evidence_text(self, key: str) -> list[str]:
        return [str(row["text"] or "") for row in self.conn.execute(
            """SELECT a.text FROM evidence e JOIN archive a ON a.id = e.archive_id
                WHERE e.kind = 'event' AND e.ref = ? ORDER BY a.ts""", (key,))]

    def provenance(self, key: str) -> list[str]:
        return [f"{row['stage']}:{row['verb']}" for row in self.conn.execute(
            "SELECT stage, verb FROM provenance WHERE kind='event' AND ref = ?"
            " ORDER BY id", (key,))]

    def todos(self, text_like: str) -> list:
        needle = text_like.casefold()
        return [todos.Todo.from_row(row) for row in self.conn.execute(
            "SELECT * FROM todos ORDER BY id")
            if needle in str(row["text"] or "").casefold()]

    def brief(self) -> str:
        return brief.render(self.conn, self.cfg)

    def context_for(self, entity_like: str) -> str:
        for entity, text in self.shown.items():
            if entity_like.casefold() in entity.casefold():
                return text
        return ""


# ------------------------------------------------------------------ scenario --

#: Every retelling `apply_variant` implements, in the order `--variants N` takes them.
#: A scenario may name a shorter list when one of them cannot express anything about it.
RETELLINGS = ("wording", "distractor", "dupe", "reorder", "batch", "sparse")


@dataclass
class Scenario:
    id: str
    family: int
    title: str
    ops: list[Op]
    checks: list[Check]
    #: Oracle diffs, one entry per nightly pass in order, `{entity: diff}`. Used only by
    #: the deterministic layer: it is "what a perfect extractor returned", so the layer
    #: grades storage and replay rather than extraction. A retry reuses its pass's entry.
    script: list[dict] = field(default_factory=list)
    #: Variants this scenario is exercised under, beyond `plain`.
    variants: tuple[str, ...] = RETELLINGS

    @property
    def reserved(self) -> bool:
        return self.id in RESERVED


def diff(**kw) -> dict:
    return {**EMPTY, **kw}


# ---------------------------------------------------------------- the corpus --

def _f1_same_statement() -> Scenario:
    """1. The assistant files the plan; the pass reads the same sentence differently."""
    said = agent(day(0, "09:10"),
                 "Put dinner with Alex on the calendar for Friday September 18th at 7pm.",
                 alt="dinner w/ alex friday the 18th, 7 — stick it on the calendar")
    return Scenario(
        id="f1.same-statement", family=1,
        title="assistant files a plan, the pass re-reads the sentence",
        ops=[
            said,
            tool(day(0, "09:11"), "add_event", title="Dinner with Alex",
                 when="2026-09-18", time="19:00", status="confirmed",
                 kind="commitment", participants=["Alex Rivera"], drop=("participants",)),
            mark(day(0, "09:12"), "after-tool"),
            nightly(day(0, "23:30")),
            mark(day(1, "07:00"), "after-pass"),
        ],
        # A perfect extractor reading that sentence writes the plan it states, in its own
        # words and with no key. Whether that becomes a second row is the finding.
        script=[{"thread:agent:conversation": diff(events=[{
            "title": "Dinner with Alex Rivera", "date": "2026-09-18", "time": "19:00",
            "kind": "commitment", "status": "confirmed", "subject": "me",
            "participants": ["Alex Rivera"],
        }])}],
        checks=[
            check("f1.tool-wrote-it", "after-tool", "identity",
                  lambda c: (len(c.on("2026-09-18")) == 1,
                             f"{[e.title for e in c.on('2026-09-18')]}")),
            check("f1.one-row", "after-pass", "duplicate",
                  lambda c: (len(c.on("2026-09-18")) == 1,
                             f"{[(e.title, e.written_by) for e in c.on('2026-09-18')]}")),
            check("f1.still-confirmed", "after-pass", "correction",
                  lambda c: _status(c.on("2026-09-18"), "confirmed")),
            check("f1.statement-is-evidence", "after-pass", "evidence",
                  lambda c: _cited(c, c.on("2026-09-18"), "calendar"), frontier=True),
            check("f1.action-visible-to-pass", "after-tool", "retrieval",
                  lambda c: _mentions(c.context_for("agent"), "Dinner with Alex"),
                  layer="retrieval"),
        ],
    )


def _f2_field_change() -> Scenario:
    """2. The assistant moves it; the pass then meets the original confirmation."""
    return Scenario(
        id="f2.field-change", family=2,
        title="assistant moves date and place, the pass reads the original plan",
        ops=[
            msg(day(0, "18:02"),
                "Brunch is on for Saturday the 19th, 11am at Rosewood.",
                who="Alex Rivera",
                alt="ok brunch saturday 19th 11am, rosewood"),
            nightly(day(0, "23:30")),
            mark(day(1, "08:00"), "established"),
            agent(day(1, "09:00"),
                  "Brunch moved to Sunday the 20th and we're going to Blue Fern instead."),
            tool(day(1, "09:01"), "update_event", which="Brunch", when="2026-09-20",
                 location="Blue Fern"),
            mark(day(1, "09:02"), "after-tool"),
            # The original Saturday confirmation is read again by the pass that evening:
            # a second copy of it arrives from a group thread that was slow to collect.
            msg(day(1, "20:00"),
                "Reminder everyone, brunch Saturday the 19th, 11am at Rosewood!",
                who="Cameron Ortiz", written_at=day(0, "18:30"),
                alt="brunch sat 19th 11 rosewood, see you all there"),
            nightly(day(1, "23:30")),
            mark(day(2, "07:00"), "after-pass"),
        ],
        script=[
            {"person:Alex Rivera": diff(events=[{
                "title": "Brunch", "date": "2026-09-19", "time": "11:00",
                "kind": "commitment", "status": "confirmed", "subject": "me",
                "location": "Rosewood", "participants": ["Alex Rivera"],
            }])},
            # A perfect extractor reading Cameron's line says what Cameron said. It is
            # older evidence about a field the user has since corrected.
            {"person:Cameron Ortiz": diff(events=[{
                "title": "Brunch", "date": "2026-09-19", "time": "11:00",
                "kind": "commitment", "status": "confirmed", "subject": "me",
                "location": "Rosewood", "participants": ["Cameron Ortiz"],
            }])},
        ],
        checks=[
            check("f2.moved", "after-tool", "correction",
                  lambda c: _dated(c.one("Brunch"), "2026-09-20")),
            check("f2.correction-held", "after-pass", "correction",
                  lambda c: _dated(c.one("Brunch"), "2026-09-20")),
            check("f2.place-held", "after-pass", "correction",
                  lambda c: _field(c.one("Brunch"), "location", "Blue Fern")),
            check("f2.one-row", "after-pass", "duplicate",
                  lambda c: (len(c.rows("Brunch")) == 1,
                             f"{[(e.date, e.title) for e in c.rows('Brunch')]}")),
            check("f2.pass-sees-the-move", "after-pass", "retrieval",
                  lambda c: _mentions(c.context_for("Cameron"), "Blue Fern"),
                  layer="retrieval", frontier=True),
        ],
    )


def _f3_stale_reinstatement() -> Scenario:
    """3. The assistant cancels; older evidence for the plan arrives afterwards."""
    return Scenario(
        id="f3.stale-reinstatement", family=3,
        title="a cancelled plan meets evidence written before the cancellation",
        ops=[
            msg(day(0, "17:45"), "Got you a ticket for the show Friday at 8, Bowery.",
                who="Cameron Ortiz", alt="spare ticket for friday's show, 8pm bowery — yours"),
            nightly(day(0, "23:30")),
            mark(day(1, "08:00"), "established"),
            agent(day(1, "10:00"), "I can't make the Bowery show on Friday, cancel it."),
            tool(day(1, "10:01"), "update_event", which="show", status="declined"),
            mark(day(1, "10:02"), "after-tool"),
            # Written before the cancellation, collected after it. Nothing in it is news.
            msg(day(1, "21:00"),
                "Confirming your ticket: Friday September 18, doors 8pm, Bowery Ballroom.",
                who="Cameron Ortiz", stream="email", thread="tickets@bowery.example",
                written_at=day(1, "07:30"),
                alt="Your ticket is confirmed — Fri Sep 18, doors 8pm, Bowery Ballroom."),
            nightly(day(1, "23:30")),
            mark(day(2, "07:00"), "after-pass"),
        ],
        script=[
            {"person:Cameron Ortiz": diff(events=[{
                "title": "Show at Bowery Ballroom", "date": "2026-09-18", "time": "20:00",
                "kind": "commitment", "status": "confirmed", "subject": "me",
                "location": "Bowery Ballroom", "participants": ["Cameron Ortiz"],
            }])},
            {"thread:email:tickets@bowery.example": diff(events=[{
                "title": "Show at Bowery Ballroom", "date": "2026-09-18", "time": "20:00",
                "kind": "commitment", "status": "confirmed", "subject": "me",
                "location": "Bowery Ballroom",
            }])},
        ],
        checks=[
            check("f3.cancelled", "after-tool", "cancellation",
                  lambda c: _status(c.rows("Bowery"), "declined")),
            check("f3.stays-cancelled", "after-pass", "cancellation",
                  lambda c: _status(c.rows("Bowery"), "declined")),
            check("f3.not-back-as-active", "after-pass", "cancellation",
                  lambda c: (not c.rows("Bowery", active=True),
                             f"active={[e.one_line() for e in c.rows('Bowery', active=True)]}")),
            check("f3.no-second-row", "after-pass", "duplicate",
                  lambda c: (len(c.rows("Bowery")) == 1,
                             f"{[(e.date, e.status) for e in c.rows('Bowery')]}")),
            check("f3.brief-does-not-offer-it", "after-pass", "brief",
                  lambda c: ("Bowery" not in c.brief(),
                             "brief still lists it" if "Bowery" in c.brief() else "clean")),
        ],
    )


def _f4_newer_cancellation() -> Scenario:
    """4. A genuinely newer cancellation must still overturn the assistant's row."""
    return Scenario(
        id="f4.newer-cancellation", family=4,
        title="newer evidence overturns a typed decision",
        ops=[
            agent(day(0, "20:21"),
                  "I'm going to the movie with Riley on Tuesday the 22nd, put it on the calendar."),
            tool(day(0, "20:22"), "add_event", title="Movie with Riley",
                 when="2026-09-22", status="confirmed", kind="commitment",
                 participants=["Riley Morgan"], drop=("participants",)),
            mark(day(0, "20:23"), "after-tool"),
            nightly(day(0, "23:30")),
            msg(day(1, "17:35"),
                "The Tuesday movie is off — that theater is closed for renovation.",
                who="Riley Morgan",
                alt="cant do the movie tuesday, theatre's shut for renovation. sorry"),
            nightly(day(1, "23:30")),
            mark(day(2, "07:00"), "after-pass"),
        ],
        script=[
            {},
            {"person:Riley Morgan": diff(events=[{
                "title": "Movie with Riley", "date": "2026-09-22", "kind": "commitment",
                "status": "declined", "subject": "me", "participants": ["Riley Morgan"],
            }])},
        ],
        checks=[
            check("f4.confirmed-first", "after-tool", "identity",
                  lambda c: _status(c.rows("Movie"), "confirmed")),
            check("f4.newer-cancellation-lands", "after-pass", "cancellation",
                  lambda c: _status(c.rows("Movie"), "declined")),
            check("f4.one-row", "after-pass", "duplicate",
                  lambda c: (len(c.rows("Movie")) == 1,
                             f"{[(e.date, e.status) for e in c.rows('Movie')]}")),
        ],
    )


def _f5_three_names() -> Scenario:
    """5. One occasion, three sources, three names for it."""
    return Scenario(
        id="f5.three-names", family=5,
        title="chat, email and a typed row describe one occasion differently",
        ops=[
            tool(day(0, "08:00"), "add_event", title="Housewarming",
                 when="2026-09-19", status="mentioned", kind="opportunity",
                 drop=("kind",)),
            msg(day(0, "12:00"), "My housewarming is Saturday the 19th, 6pm, 55 Linden Ave.",
                who="Devon Park",
                alt="doing the new-place thing sat the 19th at 6, 55 linden ave"),
            msg(day(0, "13:00"),
                "Devon's Housewarming Party — Saturday, September 19 at 6:00 PM, "
                "55 Linden Avenue. Please RSVP.",
                who="Devon Park", stream="email", thread="invites@partiful.example",
                alt="You're invited to Devon's Housewarming Party on Sat Sep 19, 6 PM."),
            nightly(day(0, "23:30")),
            mark(day(1, "07:00"), "after-pass"),
        ],
        script=[{
            "person:Devon Park": diff(events=[{
                "title": "Housewarming at Devon's", "date": "2026-09-19", "time": "18:00",
                "kind": "commitment", "status": "confirmed", "subject": "me",
                "location": "55 Linden Ave", "participants": ["Devon Park"],
            }]),
            "thread:email:invites@partiful.example": diff(events=[{
                "title": "Devon's Housewarming Party", "date": "2026-09-19",
                "time": "18:00", "kind": "commitment", "status": "confirmed",
                "subject": "me", "location": "55 Linden Avenue",
            }]),
        }],
        checks=[
            check("f5.one-occasion", "after-pass", "duplicate",
                  lambda c: (len(c.on("2026-09-19")) == 1,
                             f"{[e.title for e in c.on('2026-09-19')]}")),
            check("f5.keeps-the-address", "after-pass", "correction",
                  lambda c: (bool((c.on("2026-09-19") or [None])[0]
                                  and "Linden" in ((c.on("2026-09-19")[0].location) or "")),
                             f"{[e.location for e in c.on('2026-09-19')]}")),
        ],
    )


def _f6_same_provider() -> Scenario:
    """6. Two real appointments that look alike must stay two rows."""
    return Scenario(
        id="f6.same-provider", family=6,
        title="same provider, same title, same time — two distinct appointments",
        ops=[
            tool(day(0, "08:00"), "add_event", title="Physio session",
                 when="2026-09-14", time="17:00", status="confirmed",
                 kind="commitment", location="Riverton PT", drop=("location",)),
            mark(day(0, "08:01"), "first-booked"),
            msg(day(0, "16:00"),
                "Your physio session with Riverton PT is confirmed for Friday "
                "September 18 at 5:00 PM.",
                who="Nadia Okoro", stream="email", thread="clinic@riverton.example",
                alt="Booking confirmed: Physio, Riverton PT, Fri Sep 18, 5:00 PM."),
            nightly(day(0, "23:30")),
            mark(day(1, "07:00"), "after-pass"),
        ],
        script=[{"thread:email:clinic@riverton.example": diff(events=[{
            "title": "Physio session", "date": "2026-09-18", "time": "17:00",
            "kind": "commitment", "status": "confirmed", "subject": "me",
            "location": "Riverton PT",
        }])}],
        # The independent half of the pair. Nothing in this mail says anything moved: it
        # confirms a booking, on its own day, at a provider the user already sees. Sharing a
        # title with a stored row across four days is a reason to *look* at that row, not
        # a decision that this is it — so no key, no series, no move. Its twin below says
        # the same wording plus an identified target is a reschedule.
        checks=[
            check("f6.two-rows", "after-pass", "false-merge",
                  lambda c: (len(c.rows("Physio")) == 2,
                             f"{[(e.date, e.time) for e in c.rows('Physio')]}")),
            check("f6.first-untouched", "after-pass", "false-merge",
                  lambda c: (any(e.date == "2026-09-14" for e in c.rows("Physio")),
                             f"{[e.date for e in c.rows('Physio')]}")),
            check("f6.second-exists", "after-pass", "false-merge",
                  lambda c: (any(e.date == "2026-09-18" for e in c.rows("Physio")),
                             f"{[e.date for e in c.rows('Physio')]}")),
        ],
    )


def _f6b_explicit_reschedule() -> Scenario:
    """6b. The same provider and the same wording, but this one names what it is moving.

    The pair is the point. `f6` and this scenario differ in exactly one thing the store
    can see — whether the incoming mention identifies an existing row — and they must
    come out differently. A matcher broad enough to move `f6` is a matcher that merges
    two real appointments; one narrow enough to keep them apart has to let a genuine
    reschedule through on the identifier it supplies.
    """
    return Scenario(
        id="f6b.explicit-reschedule", family=6,
        title="same provider and wording, but the reschedule names its target",
        ops=[
            tool(day(0, "08:00"), "add_event", title="Physio session",
                 when="2026-09-14", time="17:00", status="confirmed",
                 kind="commitment", location="Riverton PT", drop=("location",)),
            mark(day(0, "08:01"), "first-booked"),
            msg(day(0, "16:00"),
                "Your physio session with Riverton PT has been moved from Monday "
                "September 14 to Friday September 18, still 5:00 PM.",
                who="Nadia Okoro", stream="email", thread="clinic@riverton.example",
                alt="Rescheduled: your Riverton PT physio moves from Mon Sep 14 to "
                    "Fri Sep 18, 5:00 PM."),
            nightly(day(0, "23:30")),
            mark(day(1, "07:00"), "after-pass"),
        ],
        # The row is in this bundle's ALREADY ON THE CALENDAR block, and the instruction
        # beside it is to return that exact key. A reschedule that identifies its target
        # is the thing the store can act on.
        script=[{"thread:email:clinic@riverton.example": diff(events=[{
            "key": "physio-session@2026-09-14",
            "title": "Physio session", "date": "2026-09-18", "time": "17:00",
            "kind": "commitment", "status": "confirmed", "subject": "me",
            "location": "Riverton PT",
        }])}],
        checks=[
            check("f6b.one-row", "after-pass", "duplicate",
                  lambda c: (len(c.rows("Physio")) == 1,
                             f"{[(e.date, e.time) for e in c.rows('Physio')]}")),
            check("f6b.moved", "after-pass", "correction",
                  lambda c: _dated(c.one("Physio"), "2026-09-18")),
            check("f6b.identity-held", "after-pass", "identity",
                  lambda c: (bool(c.one("Physio"))
                             and c.one("Physio").key == "physio-session@2026-09-14",
                             f"key={c.one('Physio').key if c.one('Physio') else None}")),
            check("f6b.target-was-shown", "after-pass", "retrieval",
                  lambda c: _mentions(c.context_for("clinic"), "Physio session"),
                  layer="retrieval"),
        ],
        variants=RETELLINGS,
    )


def _f7_one_occurrence() -> Scenario:
    """7. An update to one occurrence of a recurring thing, not to the rule."""
    return Scenario(
        id="f7.one-occurrence", family=7,
        title="one week moves; the rest of the series does not",
        ops=[
            tool(day(0, "08:00"), "set_schedule", which="physio", cadence="weekly",
                 weekday="monday", time="17:00", location="Riverton PT",
                 starting="2026-09-14"),
            mark(day(0, "08:01"), "series-made"),
            agent(day(0, "09:00"),
                  "Physio on the 14th has to move to the Wednesday, same time."),
            tool(day(0, "09:01"), "move_one_occurrence", which="Physio",
                 to="2026-09-16"),
            mark(day(0, "09:02"), "after-tool"),
            msg(day(0, "18:00"),
                "Confirming your physio moved to Wednesday September 16 at 5pm. "
                "Your other Monday sessions are unchanged.",
                who="Nadia Okoro", stream="email", thread="clinic@riverton.example",
                alt="Moved: physio Wed Sep 16, 5pm. Mondays otherwise as usual."),
            nightly(day(0, "23:30")),
            mark(day(1, "07:00"), "after-pass"),
        ],
        script=[{"thread:email:clinic@riverton.example": diff(events=[{
            "title": "Physio", "date": "2026-09-16", "time": "17:00",
            "kind": "commitment", "status": "confirmed", "subject": "me",
            "series": "physio", "location": "Riverton PT",
        }])}],
        checks=[
            check("f7.moved-week", "after-tool", "correction",
                  lambda c: (any(e.date == "2026-09-16" for e in c.rows("Physio")),
                             f"{sorted(e.date for e in c.rows('Physio'))}")),
            check("f7.stands-in-for-the-monday", "after-tool", "identity",
                  lambda c: _stands_in_for(c, "Physio", "2026-09-14")),
            check("f7.rule-unchanged", "after-pass", "correction",
                  lambda c: _rule_still(c, "physio", weekday=0, time="17:00")),
            check("f7.no-extra-row", "after-pass", "duplicate",
                  lambda c: (len([e for e in c.rows("Physio")
                                  if e.date == "2026-09-16"]) == 1,
                             f"{sorted(e.date for e in c.rows('Physio'))}")),
        ],
        variants=RETELLINGS,
    )


def _f8_long_gap() -> Scenario:
    """8. The correction arrives from someone else, on another channel, eleven days later."""
    return Scenario(
        id="f8.long-gap", family=8,
        title="a correction from a different person after a long silence",
        ops=[
            msg(day(0, "12:40"), "Dinner Thursday the 24th at 7 at the ramen place?",
                who="Alex Rivera", alt="ramen thursday the 24th, 7?"),
            nightly(day(0, "23:30")),
            mark(day(1, "07:00"), "established"),
            msg(day(11, "10:15"), "Dinner is 8:30 now, not 7. Alex can't get there early.",
                who="Cameron Ortiz",
                alt="pushing dinner to 8.30 — alex cant make 7"),
            nightly(day(11, "23:30")),
            mark(day(12, "07:00"), "after-pass"),
        ],
        script=[
            {"person:Alex Rivera": diff(events=[{
                "title": "Ramen dinner", "date": "2026-09-24", "time": "19:00",
                "kind": "commitment", "status": "confirmed", "subject": "me",
                "participants": ["Alex Rivera"],
            }])},
            {"person:Cameron Ortiz": diff(events=[{
                "title": "Ramen dinner", "date": "2026-09-24", "time": "20:30",
                "kind": "commitment", "status": "confirmed", "subject": "me",
                "participants": ["Cameron Ortiz"],
            }])},
        ],
        checks=[
            check("f8.correction-lands", "after-pass", "correction",
                  lambda c: _field(c.one("Ramen"), "time", "20:30")),
            check("f8.one-row", "after-pass", "duplicate",
                  lambda c: (len(c.rows("Ramen")) == 1,
                             f"{[(e.date, e.time) for e in c.rows('Ramen')]}")),
            check("f8.target-was-shown", "after-pass", "retrieval",
                  lambda c: _mentions(c.context_for("Cameron"), "Ramen dinner"),
                  layer="retrieval", frontier=True),
        ],
        variants=RETELLINGS,
    )


def _f9_retry() -> Scenario:
    """9. A pass is re-run, and a proposal built against an older row version lands late."""
    return Scenario(
        id="f9.retry", family=9,
        title="retry, and a stale proposal applied after a live correction",
        ops=[
            msg(day(0, "11:04"), "Poker is Friday the 18th at 8, at my place.",
                who="Riley Morgan", alt="poker friday 18th, 8pm, mine"),
            nightly(day(0, "23:30")),
            mark(day(1, "07:00"), "established"),
            retry(day(1, "07:30")),
            mark(day(1, "07:31"), "after-retry"),
            agent(day(1, "09:00"), "Poker moved to Saturday the 19th."),
            tool(day(1, "09:01"), "update_event", which="Poker", when="2026-09-19"),
            mark(day(1, "09:02"), "after-tool"),
            # The same pass runs again against traffic it already read. Its conclusion
            # was formed before the correction and must not overwrite it.
            retry(day(1, "23:30"), label="stale"),
            mark(day(2, "07:00"), "after-stale-retry"),
        ],
        script=[{"person:Riley Morgan": diff(events=[{
            "title": "Poker at Riley's", "date": "2026-09-18", "time": "20:00",
            "kind": "commitment", "status": "confirmed", "subject": "me",
            "location": "Riley's place", "participants": ["Riley Morgan"],
        }])}],
        checks=[
            check("f9.retry-no-duplicate", "after-retry", "replay",
                  lambda c: (len(c.rows("Poker")) == 1,
                             f"{[(e.date, e.title) for e in c.rows('Poker')]}")),
            check("f9.retry-no-duplicate-provenance", "after-retry", "replay",
                  lambda c: _no_repeat_provenance(c, "Poker"), frontier=True),
            check("f9.stale-does-not-overwrite", "after-stale-retry", "correction",
                  lambda c: _dated(c.one("Poker"), "2026-09-19")),
            check("f9.still-one-row", "after-stale-retry", "duplicate",
                  lambda c: (len(c.rows("Poker")) == 1,
                             f"{[(e.date, e.title) for e in c.rows('Poker')]}")),
        ],
        variants=RETELLINGS,
    )


def _f10_linked_work() -> Scenario:
    """10. A corrected event's linked obligations must keep pointing at it."""
    return Scenario(
        id="f10.linked-work", family=10,
        title="a linked to-do follows the row through a rename and a move",
        ops=[
            tool(day(0, "08:00"), "add_event", title="Spider-Man movie",
                 when="2026-09-17", time="19:40", status="confirmed",
                 kind="commitment", drop=("time",)),
            tool(day(0, "08:01"), "open_todo",
                 text="Make sure we have Spider-Man tickets", event="Spider-Man"),
            mark(day(0, "08:02"), "linked"),
            agent(day(0, "09:00"),
                  "The Spider-Man screening moved to the 19th and it's the IMAX one now."),
            tool(day(0, "09:01"), "update_event", which="Spider-Man",
                 when="2026-09-19", title="Spider-Man IMAX screening"),
            mark(day(0, "09:02"), "after-tool"),
            msg(day(0, "18:00"),
                "Your tickets are confirmed for Spider-Man at the IMAX on Saturday "
                "September 19 at 7:40 PM. Seats H8 and H9.",
                who="Devon Park", stream="email", thread="orders@cinema.example",
                alt="Order confirmed — Spider-Man, IMAX, Sat Sep 19, 7:40 PM, H8/H9."),
            nightly(day(0, "23:30")),
            mark(day(1, "07:00"), "after-pass"),
        ],
        script=[{"thread:email:orders@cinema.example": diff(
            events=[{
                "title": "Spider-Man IMAX screening", "date": "2026-09-19",
                "time": "19:40", "kind": "commitment", "status": "confirmed",
                "subject": "me", "location": "IMAX", "note": "Seats H8 and H9",
            }],
            todos=[{"op": "close", "key": "todo:make-sure-we-have-spider-man-tickets",
                    "text": "Make sure we have Spider-Man tickets", "subject": "",
                    "due": "", "wake_condition": ""}],
        )}],
        checks=[
            check("f10.link-survives-rename", "after-tool", "identity",
                  lambda c: _todo_points_at(c, "Spider-Man tickets",
                                            "Spider-Man IMAX screening")),
            check("f10.one-row", "after-pass", "duplicate",
                  lambda c: (len(c.rows("Spider-Man")) == 1,
                             f"{[(e.date, e.title) for e in c.rows('Spider-Man')]}")),
            check("f10.work-closed-against-the-right-row", "after-pass", "identity",
                  lambda c: _todo_closed(c, "Spider-Man tickets")),
        ],
        variants=RETELLINGS,
    )


SCENARIOS = [
    _f1_same_statement(), _f2_field_change(), _f3_stale_reinstatement(),
    _f4_newer_cancellation(), _f5_three_names(), _f6_same_provider(),
    _f6b_explicit_reschedule(), _f7_one_occurrence(), _f8_long_gap(), _f9_retry(), _f10_linked_work(),
]


# ------------------------------------------------------------ check helpers --

def _status(rows, wanted: str) -> tuple[bool, str]:
    if not rows:
        return False, "no row"
    return (all(e.status == wanted for e in rows),
            f"{[(e.title, e.status) for e in rows]}")


def _dated(event, wanted: str) -> tuple[bool, str]:
    if event is None:
        return False, "no single row"
    return event.date == wanted, f"date {event.date}, wanted {wanted}"


def _field(event, name: str, wanted: str) -> tuple[bool, str]:
    if event is None:
        return False, "no single row"
    value = getattr(event, name, None)
    return str(value or "") == wanted, f"{name}={value!r}, wanted {wanted!r}"


def _mentions(text: str, needle: str) -> tuple[bool, str]:
    if not text:
        return False, "nothing was shown for that bundle"
    found = needle.casefold() in text.casefold()
    return found, (f"{needle!r} was shown" if found
                   else f"{len(text)} chars shown, none naming {needle!r}")


def _cited(ctx: Ctx, rows, needle: str) -> tuple[bool, str]:
    if not rows:
        return False, "no row"
    cited = ctx.evidence_text(rows[0].key)
    return (any(needle.casefold() in line.casefold() for line in cited),
            f"evidence={[line[:40] for line in cited]}")


def _no_repeat_provenance(ctx: Ctx, title_like: str) -> tuple[bool, str]:
    rows = ctx.rows(title_like)
    if not rows:
        return False, "no row"
    stamps = ctx.provenance(rows[0].key)
    return len(stamps) == len(set(stamps)), f"provenance={stamps}"


def _stands_in_for(ctx: Ctx, title_like: str, scheduled: str) -> tuple[bool, str]:
    rows = [e for e in ctx.rows(title_like) if e.instead_of]
    if not rows:
        return False, f"nothing stands in for anything: {[e.date for e in ctx.rows(title_like)]}"
    return (rows[0].instead_of == scheduled,
            f"instead_of={rows[0].instead_of!r}, wanted {scheduled!r}")


def _rule_still(ctx: Ctx, slug: str, *, weekday: int, time: str) -> tuple[bool, str]:
    from memcal import series                                       # noqa: PLC0415
    rule = series.get(ctx.conn, slug)
    if rule is None:
        return False, f"no {slug} rule"
    return (rule.weekday == weekday and str(rule.time or "") == time,
            f"weekday={rule.weekday} time={rule.time!r} cadence={rule.cadence!r}")


def _todo_points_at(ctx: Ctx, todo_like: str, title: str) -> tuple[bool, str]:
    found = ctx.todos(todo_like)
    if len(found) != 1:
        return False, f"{len(found)} matching to-dos"
    row = ctx.conn.execute("SELECT title FROM events WHERE id = ?",
                           (found[0].event_id,)).fetchone()
    got = row["title"] if row else None
    return got == title, f"linked to {got!r}, wanted {title!r}"


def _todo_closed(ctx: Ctx, todo_like: str) -> tuple[bool, str]:
    found = ctx.todos(todo_like)
    if len(found) != 1:
        return False, f"{len(found)} matching to-dos"
    return found[0].status == "closed", f"status={found[0].status}"


# ------------------------------------------------------------------ variants --

#: How each retelling changes a scenario, said once so a reader can tell what a failing
#: variant name means without reading the generator.
VARIANTS = {
    "plain":      "as written",
    "wording":    "every line said differently, same facts",
    "sparse":     "the assistant is told less; optional tool arguments are dropped",
    "distractor": "unrelated traffic in the same threads",
    "dupe":       "each source line delivered twice, once under a second external id",
    "reorder":    "arrival order permuted within a day; written times unchanged",
    "batch":      "a day's traffic collected in one ingest rather than as it happens",
}

_DISTRACTORS = (
    "did you ever find that charger",
    "the dog rolled in something horrific again",
    "lol",
    "sending you the photos later tonight",
)


def apply_variant(scenario: Scenario, variant: str, seed: int) -> list[Op]:
    """The same story told again, differently. Bounded and reproducible from the seed.

    Written time is never permuted. A permuted *arrival* order still delivers Monday's
    sentence with Monday's timestamp, so the semantic chronology the store reasons about
    is unchanged and a difference in outcome is a real finding rather than a
    contradictory history the corpus never claimed had one answer.
    """
    rng = random.Random(f"{scenario.id}:{variant}:{seed}")
    ops = [Op(at=op.at, kind=op.kind, written_at=op.written_at,
              payload=dict(op.payload), label=op.label) for op in scenario.ops]

    if variant == "wording":
        for op in ops:
            if op.kind == "message" and op.payload.get("alt"):
                op.payload["text"] = op.payload["alt"]
    elif variant == "sparse":
        for op in ops:
            if op.kind == "tool":
                for name in op.payload.get("drop") or ():
                    op.payload["args"].pop(name, None)
    elif variant == "distractor":
        extra = []
        for op in ops:
            if op.kind != "message" or op.payload.get("stream") == "agent":
                continue
            noise = dict(op.payload)
            noise["text"] = rng.choice(_DISTRACTORS)
            noise["alt"] = ""
            extra.append(Op(at=_shift(op.at, minutes=-3), kind="message",
                            written_at="", payload=noise))
        ops = _in_order(ops + extra)
    elif variant == "dupe":
        extra = []
        for index, op in enumerate(ops):
            if op.kind != "message":
                continue
            copy = dict(op.payload)
            copy["dupe_of"] = index
            extra.append(Op(at=_shift(op.at, minutes=1), kind="message",
                            written_at=op.written_at, payload=copy))
        ops = _in_order(ops + extra)
    elif variant == "reorder":
        ops = _permute_within_day(ops, rng)
    elif variant == "batch":
        ops = _batch_by_day(ops)
    return ops


def _shift(at: str, *, minutes: int) -> str:
    return (datetime.fromisoformat(at) + timedelta(minutes=minutes)).isoformat()


def _in_order(ops: list[Op]) -> list[Op]:
    return sorted(ops, key=lambda op: (op.at, 0 if op.kind == "message" else 1))


def _permute_within_day(ops: list[Op], rng: random.Random) -> list[Op]:
    """Shuffle the *delivery* of one day's messages between two fixed points.

    Only messages move, and only among themselves, and only between the operations that
    bracket them: a tool call, a pass, or a checkpoint is a fixed point, because moving
    a message across one of those is not a delivery-order variant, it is a different
    story.
    """
    out: list[Op] = []
    run: list[Op] = []
    for op in ops:
        if op.kind == "message":
            run.append(op)
            continue
        if len(run) > 1:
            stamps = [item.at for item in run]
            rng.shuffle(run)
            for item, stamp in zip(run, stamps):
                item.at = stamp                     # arrival slots keep their moments
        out.extend(run)
        run = []
        out.append(op)
    out.extend(run)
    return out


def _batch_by_day(ops: list[Op]) -> list[Op]:
    """Collect each day's messages at the last moment before the next fixed point."""
    out: list[Op] = []
    run: list[Op] = []
    for op in ops:
        if op.kind == "message":
            run.append(op)
            continue
        if run:
            latest = max(item.at for item in run)
            for item in run:
                item.at = latest
        out.extend(run)
        run = []
        out.append(op)
    out.extend(run)
    return out


# ------------------------------------------------------------------- runner --

def seed_home(home: Path, settings: dict | None = None
              ) -> tuple[sqlite3.Connection, Config]:
    resolved = home.expanduser().resolve()
    if resolved == (Path.home() / ".memcal").resolve():
        raise SystemExit("refusing to run the collision suite against ~/.memcal")
    cfg = Config(home=resolved)
    # The run's own configuration — model, packing, wire format, stage plan. A model
    # layer that quietly used the defaults would report on a deployment nobody selected.
    for name, value in (settings or {}).items():
        if value not in (None, ""):
            setattr(cfg, name, value)
    cfg.ensure_dirs()
    conn = db.open_db(cfg.db_path)
    identity.link(conn, "+19178889999", "me", source="fixture")
    db.set_meta(conn, "identity.me", db.jdump(["Casey", "Casey Morgan"]))
    for name, phone in CAST.items():
        identity.link(conn, phone, name, source="fixture")
    conn.commit()
    return conn, cfg


def _deliver(conn, cfg, op: Op, *, index: int, run_tag: str) -> int | None:
    payload = op.payload
    stream = payload.get("stream") or "imessage"
    who = payload.get("who") or "Alex Rivera"
    thread = payload.get("thread") or (
        "conversation" if stream == "agent" else who)
    # Written time is what the store reasons about; arrival time is when it learns it.
    ts = op.written_at or op.at
    report = base.IngestReport.opened(stream, cfg)
    suffix = "dupe" if payload.get("dupe_of") is not None else str(index)
    external = f"{run_tag}:{stream}:{index}:{suffix}"
    if stream == "email":
        return base.deliver(
            conn, report, stream="email", external_id=external, ts=ts,
            text=payload["text"], thread=thread, handle=thread, person=None,
            from_me=False, counterpart=thread,
            meta={"folder": "INBOX", "subject": payload["text"][:60]},
            verdict=gate.Verdict(True, "unknown-sender"))
    if stream == "agent":
        return base.deliver(
            conn, report, stream="agent", external_id=external, ts=ts,
            text=payload["text"], thread="conversation", from_me=True, person="me",
            addressed_to="machine", verdict=gate.Verdict(True, "directive"))
    return base.deliver(
        conn, report, stream=stream, external_id=external, ts=ts,
        text=payload["text"], thread=thread, handle=CAST.get(who), person=who,
        from_me=bool(payload.get("from_me")), is_group=bool(payload.get("group")),
        verdict=gate.Verdict(True, payload.get("verdict") or "temporal"))


def _run_tool(conn, cfg, op: Op, origin: actions.Origin) -> str:
    """The same typed call `mcp_server` dispatches to, with the turn that caused it.

    A surface that archives the user's turns knows which one it is answering and hands
    it over; `live` writes the evidence link and the completed-operation record in the
    same transaction as the change. Modelled here rather than skipped, because a store
    where the tool call has no recoverable cause is a different store from production's.
    """
    fn = getattr(live, op.payload["call"])
    try:
        fn(conn, cfg, origin=origin, **op.payload["args"])
        return f"{op.payload['call']} ok"
    except Exception as exc:                    # a refused tool call is a real finding
        return f"{op.payload['call']} FAILED: {exc}"


class _Arbiter:
    """Deterministic stand-in for same-day, same-hour proposal arbitration.

    Source timestamps describe when messages arrived, not when their events occur.
    Semantic interpretation of those messages belongs to the live model layer.
    """

    def complete(self, *, suffix: str = "", **_kwargs):
        from memcal import llm                                       # noqa: PLC0415
        source_ids = [int(value) for value in re.findall(r"^\s+SOURCE (\d+) at", suffix, re.M)]
        suffix = "\n".join(line for line in suffix.splitlines() if line.startswith("  - "))
        dates = set(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", suffix))
        times = set(re.findall(r"\b\d{2}:\d{2}\b", suffix))
        titles = re.findall(r"^  - (.+?)  \[", suffix, re.M)
        if len(dates) == 1 and len(times) <= 1 and titles:
            # The fullest wording anybody used for each field, which is what a reader
            # keeps. A model that answered with only a title would silently empty the
            # rest, and a merged row that lost its address is not a merged row.
            answer = {"same_event": True, "date": next(iter(dates)),
                      "title": max(titles, key=len), "why": "same day and hour"}
            for field in ("time", "location", "status", "kind"):
                found = re.findall(rf"{field} ([^;\]]+)", suffix)
                if found:
                    answer[field] = max(found, key=len).strip()
            answer["citations"] = [
                {"field": field, "source_ids": source_ids}
                for field in ("date", "title", "time", "location", "status", "kind")
                if field in answer]
            return llm.Reply(text="", data=answer)
        return llm.Reply(text="", data={"same_event": "unresolved",
                                        "why": "differing days"})


def _deterministic_pass(conn, cfg, ctx: Ctx, table: dict, *, tag: str) -> str:
    """One pass with the decisions supplied. Storage and replay are what is graded."""
    bundles = bundle_stage.build(conn, limit=cfg.item_budget,
                                 per_entity=cfg.items_per_entity)
    if not bundles:
        return f"{tag}: nothing queued"
    # Capture what a model *would* have been shown, before anything is applied. Model
    # free, so the retrieval boundary costs nothing and is graded on every run.
    for bundle in bundles:
        ctx.shown[bundle.entity] = propose_stage.build_bundle_block(cfg, bundle, conn)
    proposals = [(b, table.get(b.entity, EMPTY), None) for b in bundles]
    proposals, _resolved = merge_stage.merge_all(_Arbiter(), cfg, proposals, conn=conn)
    before = db.now()
    counts, _log = apply_stage.apply_diffs(conn, cfg, proposals,
                                           written_by=f"dream:{tag}", stage="propose")
    archive.spool_mark(conn, [sid for b in bundles for sid in b.spool_ids], None)
    for todo in todos.check_wakes(conn, bundle_stage.all_text(bundles), since=before):
        todos.ask(conn, f"{todo.text} — {todo.wake_condition} now looks true. Still open?",
                  key=f"q:wake:{todo.key}", about_todo=todo.id, written_by="dream")
    wiki.prune_empty(cfg.wiki_dir)
    events.mark_past_happened(conn)
    pending.retry(conn, ask=lambda text, key: todos.ask(
        conn, text, key=key, written_by="dream"))
    events.link_contained(conn)
    todos.relink_questions(conn)
    todos.expire_questions(conn)
    sweep_stage.reconcile_backward_window(conn, cfg)
    brief.write(conn, cfg)
    return f"{tag}: {len(bundles)} bundles, {sum(counts.values())} writes"


def run_scenario(scenario: Scenario, home: Path, *, variant: str = "plain",
                 seed: int = 0, layer: str = "apply",
                 settings: dict | None = None) -> list[dict]:
    """Replay one scenario and grade every checkpoint it passes through."""
    ops = (scenario.ops if variant == "plain"
           else apply_variant(scenario, variant, seed))
    conn, cfg = seed_home(home, settings)
    ctx = Ctx(conn, cfg)
    tag = f"{scenario.id}:{variant}:{seed}"
    by_mark: dict[str, list[Check]] = {}
    for item in scenario.checks:
        if item.layer == "model" and layer != "model":
            continue
        by_mark.setdefault(item.after, []).append(item)

    rows: list[dict] = []
    passes = 0
    first_failure = ""
    #: The most recent turn the user addressed to their assistant. A tool call answers
    #: that turn and nothing older; a scenario whose tool call follows no statement at
    #: all leaves this empty, which is the "caller had no source context" case.
    turn: list[int] = []
    try:
        for index, op in enumerate(ops):
            db.set_today(op.at)
            if op.kind == "message":
                archive_id = _deliver(conn, cfg, op, index=index, run_tag=tag)
                if archive_id and op.payload.get("stream") == "agent":
                    turn = [archive_id]
                conn.commit()
            elif op.kind == "tool":
                ctx.pass_log.append(_run_tool(
                    conn, cfg, op, actions.Origin.of("benchmark", turn, session=tag)))
                # What the pass would see *now* — after the tool call and before any
                # pass — is the whole retrieval question in this corpus.
                _capture_context(conn, cfg, ctx)
            elif op.kind == "pass":
                passes += 1
                ctx.pass_log.append(run_pass(
                    conn, cfg, ctx, scenario, passes, layer=layer, tag=f"p{passes}"))
            elif op.kind == "retry":
                # A retry re-reads what the previous pass read, with the conclusion that
                # pass reached. Nothing about the store may change.
                archive.spool_reset(conn, since=op.at[:10])
                ctx.pass_log.append(run_pass(
                    conn, cfg, ctx, scenario, passes, layer=layer,
                    tag=f"p{passes}-retry"))
            elif op.kind == "mark":
                for item in by_mark.get(op.label, ()):
                    row = _grade(item, ctx, scenario, variant, seed, op.label)
                    rows.append(row)
                    if not row["ok"] and not first_failure:
                        first_failure = f"{op.label}/{item.id}"
    finally:
        db.set_today(None)
        conn.close()
    for row in rows:
        row["first_failure"] = first_failure
    return rows


def run_pass(conn, cfg, ctx: Ctx, scenario: Scenario, pass_no: int, *,
             layer: str, tag: str) -> str:
    """One nightly pass, at the boundary this run is grading.

    `apply` supplies the decisions and grades storage and replay. `model` runs the real
    `dream` against the configured model and grades extraction and semantic judgement
    end to end — the oracle answers in `Scenario.script` are never built, never rendered
    and never reachable from the prompt, because a benchmark that shows the model the
    answer measures nothing.
    """
    if layer == "model":
        return _model_pass(conn, cfg, ctx, tag=tag)
    return _deterministic_pass(conn, cfg, ctx, _script_for(scenario, pass_no), tag=tag)


def _model_pass(conn, cfg, ctx: Ctx, *, tag: str) -> str:
    """The production pass, with the production configuration."""
    # Captured before the pass consumes the queue, so a retrieval check grades what the
    # model was actually shown rather than what survived the run.
    _capture_context(conn, cfg, ctx)
    result = dream_run.dream(conn, cfg, mode="nightly")
    ctx.model_calls += conn.execute(
        "SELECT count(*) AS n FROM generations").fetchone()["n"] - ctx.model_calls
    brief.write(conn, cfg)
    note = (result.report() or "").splitlines()
    return f"{tag}: " + (note[0] if note else "(nothing)") + "".join(
        f"\n  {error}" for error in result.errors)


def _script_for(scenario: Scenario, pass_no: int) -> dict:
    if 1 <= pass_no <= len(scenario.script):
        return scenario.script[pass_no - 1]
    return {}


def _capture_context(conn, cfg, ctx: Ctx) -> None:
    """Render the context for everything currently queued, without consuming it."""
    for bundle in bundle_stage.build(conn, limit=cfg.item_budget,
                                     per_entity=cfg.items_per_entity):
        ctx.shown[bundle.entity] = propose_stage.build_bundle_block(cfg, bundle, conn)


def _grade(item: Check, ctx: Ctx, scenario: Scenario, variant: str, seed: int,
           checkpoint: str) -> dict:
    try:
        ok, note = item.fn(ctx)
    except Exception as exc:                     # a check must never take a run down
        ok, note = False, f"check raised: {type(exc).__name__}: {exc}"
    suffix = "" if variant == "plain" else f"[{variant}/{seed}]"
    return {
        "id": f"{item.id}{suffix}",
        "challenge": f"{scenario.family} {scenario.title}",
        "day": 0,
        "ok": bool(ok),
        "soft": False,
        "frontier": item.frontier,
        "category": item.category,
        "layer": item.layer,
        "checkpoint": checkpoint,
        "variant": variant,
        "seed": seed,
        "reserved": scenario.reserved,
        "model_calls": ctx.model_calls,
        "note": str(note)[:200],
    }


def collision_checks(home: Path, case: str | None = None, *, layer: str = "apply",
                     variants: int = 1, settings: dict | None = None) -> list[dict]:
    """Every scenario, plain plus `variants` retellings of each.

    `case` limits what actually *runs*, not just what is graded: a substring match
    against the scenario id, its family number or its title. Nothing else is executed,
    so narrowing to one family costs one scenario's worth of time and model spend.
    """
    rows: list[dict] = []
    for scenario in SCENARIOS:
        if not _selected(scenario, case):
            continue
        plans = [("plain", 0)]
        for offset, name in enumerate(scenario.variants[:max(0, variants)]):
            plans.append((name, offset + 1))
        for variant, seed in plans:
            scratch = home / f"{scenario.id}-{variant}-{seed}"
            rows.extend(run_scenario(scenario, scratch, variant=variant, seed=seed,
                                     layer=layer, settings=settings))
    return rows


def _selected(scenario: Scenario, case: str | None) -> bool:
    if not case:
        return True
    needle = case.casefold()
    return (needle in scenario.id.casefold()
            or needle in scenario.title.casefold()
            or needle == str(scenario.family))


def category_report(rows: list[dict]) -> str:
    """Duplicates, false merges, lost corrections and missed evidence, separately."""
    if not rows:
        return ""
    lines = ["", "collision findings by category (a blended score hides the trade-off):"]
    calls = sum({(row["id"], row["variant"], row["seed"]): row.get("model_calls", 0)
                 for row in rows}.values())
    if any(row.get("layer") == "model" for row in rows) or calls:
        # Said out loud, because a "model" run that made no model call is not a low
        # score — it is the deterministic layer wearing the model layer's name, and it
        # graded as one for as long as nobody counted.
        lines.append(f"  model calls dispatched: {calls}"
                     + ("   NO MODEL WAS CALLED — this is not a model measurement"
                        if not calls else ""))
    for name in CATEGORIES:
        here = [row for row in rows if row.get("category") == name]
        if not here:
            continue
        failed = [row for row in here if not row["ok"]]
        lines.append(f"  {name:14} {len(here) - len(failed):3}/{len(here):<3} ok"
                     + (f"   first: {failed[0]['id']} — {failed[0]['note'][:60]}"
                        if failed else ""))
    by_layer = {}
    for row in rows:
        by_layer.setdefault(row.get("layer", "apply"), []).append(row)
    lines.append("  " + " · ".join(
        f"{name}: {sum(1 for r in items if r['ok'])}/{len(items)}"
        for name, items in sorted(by_layer.items())))
    held = [row for row in rows if row.get("reserved")]
    if held:
        lines.append(f"  reserved (never tuned against): "
                     f"{sum(1 for r in held if r['ok'])}/{len(held)}")
    seeds = sorted({(row["id"], row["seed"], row["first_failure"])
                    for row in rows if not row["ok"] and row.get("first_failure")})
    if seeds:
        lines.append("  failing seeds (first failing checkpoint kept):")
        for cid, seed, where in seeds[:8]:
            lines.append(f"    {cid} seed={seed} first={where}")
    return "\n".join(lines)
