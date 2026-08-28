"""Deterministic-path tests: the parts that must work without a model.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import errno
import io
import json
import os
import plistlib
import re
import socket
import subprocess
import sqlite3
import sys
import tempfile
import threading
import time
import textwrap
import unittest
from unittest import mock
from datetime import date, datetime, timedelta, timezone, time as dt_time
from pathlib import Path

try:
    from tests._support import Base
except ModuleNotFoundError:  # Direct execution: python3 tests/test_core.py
    from _support import Base

from memcal import archive, brief, cli, dates, db, detail, events, gate, identity, live, mcp_server, schedule, series, textclean, threads, todos, trace, web, web_server, wiki  # noqa: E402
from memcal.config import Config  # noqa: E402
from memcal.dream import apply as apply_stage  # noqa: E402
from memcal.dream import bundle as bundle_stage  # noqa: E402
from memcal.dream import propose as propose_stage  # noqa: E402
from memcal.dream import sweep as sweep_stage  # noqa: E402
from memcal import sources  # noqa: E402
from memcal.sources import base, groupme, ical, imessage, proton, providers, spec, whatsapp  # noqa: E402

class TestAWikiRowIsThreeIndependentClaims(Base):
    """Apply each wiki field independently."""

    def _apply(self, **row):
        return apply_stage._apply_wiki(self.conn, self.cfg, {"page": "jordan", **row},
                                       source="test", seen=set())

    def test_a_good_slot_survives_a_garbage_alias_beside_it(self):
        out = self._apply(slot="location", value="Eastwood",
                          question="standing:[]", alias="questions")
        self.assertIn("slot", [o[0] for o in out])
        self.assertEqual(
            wiki.read(self.cfg.wiki_dir, "jordan").slots["location"]["value"], "Eastwood")

    def test_the_garbage_alias_is_rejected_and_says_so(self):
        out = self._apply(slot="location", value="Eastwood", alias="questions")
        self.assertIn("rejected-alias", [o[0] for o in out])
        self.assertEqual(wiki.read(self.cfg.wiki_dir, "jordan").aliases, [])

    def test_a_json_fragment_is_not_a_question(self):
        self._apply(slot="location", value="Eastwood", question="standing:[]")
        self.assertEqual(wiki.read(self.cfg.wiki_dir, "jordan").questions, [])

    def test_a_real_alias_still_lands(self):
        out = self._apply(alias="Jordan J. Lee")
        self.assertEqual([o[0] for o in out], ["alias"])
        self.assertIn("Jordan J. Lee", wiki.read(self.cfg.wiki_dir, "jordan").aliases)

    def test_a_real_question_still_lands(self):
        self._apply(question="Which Jordan is this?")
        self.assertEqual(wiki.read(self.cfg.wiki_dir, "jordan").questions,
                         ["Which Jordan is this?"])

    def test_a_slot_and_a_real_alias_both_apply(self):
        out = self._apply(slot="location", value="Riverton", alias="Jordan J. Lee")
        self.assertEqual(sorted(o[0] for o in out), ["alias", "slot"])

    def test_every_schema_key_is_refused_as_a_name(self):
        for word in ("questions", "standing", "value", "slot", "null", "bundle"):
            self.assertFalse(apply_stage._is_a_name(word), word)

    def test_a_sentence_is_not_a_name_either(self):
        self.assertFalse(apply_stage._is_a_name(
            "the person who lives in the apartment upstairs from them"))

    def test_ordinary_names_pass(self):
        for name in ("Jordan Lee", "Robin West", "Mom", "Alex"):
            self.assertTrue(apply_stage._is_a_name(name), name)


class TestWebEventDetail(Base):
    def test_event_detail_exposes_attendees_range_links_history_and_related_rows(self):
        wiki.set_slot(self.cfg.wiki_dir, "robbie", "address", "42 Example Street",
                      conn=self.conn)
        wiki.add_alias(self.cfg.wiki_dir, "robbie", "Robin West")
        wiki.set_slot(self.cfg.wiki_dir, "camp-echo", "kind", "venue",
                      conn=self.conn, section="places")
        event, _ = events.upsert(self.conn, {
            "title": "Camp Echo Weekend", "date": self.d(3), "until": self.d(5),
            "time": "18:00", "location": "Camp Echo",
            "participants": ["Robin West"], "status": "mentioned",
        }, written_by="dream:nightly", match=False)
        events.upsert(self.conn, {
            "key": event.key, "title": event.title, "date": event.date,
            "status": "confirmed",
        }, written_by="dream:nightly")
        events.upsert(self.conn, {
            "title": "Poker", "date": self.d(-7), "participants": ["Robbie"],
        }, match=False)
        source_id = archive.append(
            self.conn, stream="imessage", external_id="camp-echo-source", ts=db.now(),
            text="Camp Echo runs all weekend with Robin", person="Robin West",
            thread="camp", gated=True)
        trace.stamp(
            self.conn, kind="event", ref=event.key, verb="inserted",
            entity="person:Robin West", stage="propose", run_id=11,
            archive_ids=[source_id])
        self.conn.commit()

        out = web.why(self.conn, "event", event.key, cfg=self.cfg)
        detail = out["detail"]
        self.assertEqual(detail["event"]["participants"], ["Robin West"])
        self.assertEqual(detail["event"]["until"], self.d(5))
        self.assertEqual(detail["event"]["location"], "Camp Echo")
        self.assertEqual(
            {(link["slug"], link["role"]) for link in detail["wiki"]},
            {("robbie", "person"), ("camp-echo", "location")})
        # The related rows moved out of this payload and behind `/api/events`, which the
        # attendee pill now asks. Still resolved through the wiki, so "Robin West" finds
        # the row that says "Robbie".
        self.assertEqual(
            [row["title"] for row in web.event_list(
                self.conn, self.cfg, person="Robin West", exclude=event.key)["events"]],
            ["Poker"])
        self.assertEqual(detail["timeline"]["changes"][0]["field"], "status")
        self.assertEqual(detail["timeline"]["provenance"][0]["run"], 11)
        self.assertEqual(out["highlight_terms"], ["Camp", "Echo", "Weekend"])
        self.assertEqual(
            [row["text"] for row in out["source"] if row["evidence"]],
            ["Camp Echo runs all weekend with Robin"])

    def test_memory_ui_has_one_call_action_and_it_routes_to_runs(self):
        page = web.frontend_source()
        self.assertNotIn("open run ${c.run} · call ${c.call}", page)
        self.assertIn("open the full call · run ${c.run} call ${c.call}", page)
        self.assertIn('location.hash = "runs"', page)
        self.assertIn("appendHighlighted(row, s.text", page)
        self.assertIn("renderEventDetail(out.detail, body)", page)


class TestReportedMemoryFailures(Base):
    """Concrete regressions from the 2026-07-29 product review.

    These are structural guards. Even if a proposal model repeats one of the bad
    outputs verbatim, it must not reach the brief in that shape.
    """

    def _bundle(self, *, text: str, person: str = "Mom",
                thread: str = "family", external_id: str = "review-line"):
        archive_id = archive.append(
            self.conn, stream="groupme", external_id=external_id, ts=db.now(),
            text=text, thread=thread, person=person, gated=True)
        row = self.conn.execute("SELECT * FROM archive WHERE id = ?", (archive_id,)).fetchone()
        return bundle_stage.Bundle(entity=f"person:{person}", items=[row])

    def test_a_vague_question_names_who_asked_it(self):
        bundle = self._bundle(text="When am I coming over again?")
        apply_stage.apply_diffs(
            self.conn, self.cfg, [(bundle, {"questions": [
                "When am I coming over again?"]})], written_by="test")
        texts = [row["text"] for row in todos.open_questions(self.conn)]
        self.assertEqual(texts, ["Mom asked: When am I coming over again?"])

    def test_a_settled_payment_is_archive_only(self):
        bundle = self._bundle(text="Paid Quinn $10", person="me",
                              external_id="venmo-paid")
        counts, _log = apply_stage.apply_diffs(
            self.conn, self.cfg, [(bundle, {"events": [{
                "title": "Paid Quinn $10", "date": db.today().isoformat(),
                "kind": "observed", "status": "happened",
                "participants": ["Quinn Brooks"]}]})], written_by="test")
        self.assertEqual(events.window(self.conn, 1, 1), [])
        self.assertEqual(counts["event:rejected-transaction"], 1)
        self.assertEqual(
            self.conn.execute("SELECT count(*) n FROM archive").fetchone()["n"], 1,
            "rejecting a brief row must never delete its searchable source")

    def test_one_trip_car_permission_is_not_a_permanent_preference(self):
        bundle = self._bundle(
            text="Quinn can borrow my car so the user can drive Katie to Medieval Times",
            person="me", external_id="car-permission")
        counts, _log = apply_stage.apply_diffs(
            self.conn, self.cfg, [(bundle, {"standing": [{
                "kind": "preference", "value": "Quinn may borrow my car",
                "scope": "permanent"}]})], written_by="test")
        self.assertEqual(todos.standing(self.conn, "preference"), [])
        self.assertEqual(counts["standing:rejected-transient"], 1)

    def test_every_material_brief_row_has_an_openable_source(self):
        bundle = self._bundle(text="Poker at Robbie's Saturday", person="Robbie",
                              external_id="robbie-poker")
        apply_stage.apply_diffs(
            self.conn, self.cfg, [(bundle, {"events": [{
                "title": "Poker at Robbie's", "date": self.d(2),
                "participants": ["Robbie"], "status": "confirmed"}],
                "todos": [{"text": "Bring poker chips"}],
                "standing": [{"kind": "identity", "value": "Robbie hosts poker"}],
                "questions": ["What time does poker start?"]})],
            written_by="test")

        text = brief.render(self.conn, self.cfg)
        # A brief with no rows in it satisfies "every row has a handle" perfectly.
        self.assertTrue(brief.SOURCE_RE.findall(text), "nothing rendered to check")
        for line in text.splitlines():
            if line.startswith("- ") or (line and not line.startswith(("#", "[", "("))
                                          and "Pages:" not in line):
                self.assertRegex(line, brief.SOURCE_RE, line)
        for token in brief.SOURCE_RE.findall(text):
            opened = trace.resolve_source(self.conn, token)
            self.assertNotIn("error", opened)
            self.assertTrue(any(row["evidence"] for row in opened["evidence"]), token)

    def test_wiki_profiles_compute_encounters_without_copying_a_ledger(self):
        wiki.set_slot(self.cfg.wiki_dir, "robbie", "address", "42 Example Street",
                      source="groupme", conn=self.conn)
        wiki.add_alias(self.cfg.wiki_dir, "robbie", "Robin West")
        for offset in (-14, -7):
            events.upsert(self.conn, {
                "title": "Poker at Robbie's", "date": self.d(offset),
                "status": "happened", "participants": ["Robin West"],
                "series": "poker-night"}, match=False)
        profile = wiki.profile(self.conn, self.cfg.wiki_dir, "robbie")
        self.assertEqual(profile["encounters"]["count"], 2)
        self.assertEqual(profile["encounters"]["by_activity"][0]["count"], 2)

    def test_wiki_facts_open_the_contact_message_that_stated_them(self):
        bundle = self._bundle(
            text="My favorite movie theater is Alamo Drafthouse",
            person="Quinn Brooks", external_id="quinn-favorite")
        apply_stage.apply_diffs(
            self.conn, self.cfg, [(bundle, {"wiki": [{
                "page": "quinn-brooks", "slot": "favorite movie theater",
                "value": "Alamo Drafthouse"}]})], written_by="test")
        profile = wiki.profile(self.conn, self.cfg.wiki_dir, "quinn-brooks")
        rows = profile["sources"]["favorite movie theater"]
        self.assertEqual(
            [row["text"] for row in rows if row["evidence"]],
            ["My favorite movie theater is Alamo Drafthouse"])

    def test_a_reaction_is_rescued_when_the_topic_appears_after_it(self):
        report = sources.IngestReport.opened("groupme", self.cfg)
        stamp = db.now_dt()
        first = sources.deliver(
            self.conn, report, stream="groupme", external_id="eyes",
            ts=stamp.isoformat(), text="👀", thread="PSK IRL BGN",
            person="Jose", is_group=True)
        sources.deliver(
            self.conn, report, stream="groupme", external_id="board-games",
            ts=(stamp + timedelta(minutes=6)).isoformat(),
            text="I can host board game night in early August",
            thread="PSK IRL BGN", person="Jose", is_group=True)
        row = self.conn.execute(
            """SELECT a.gated, a.gate_reason, s.entity FROM archive a
                 LEFT JOIN spool s ON s.archive_id = a.id WHERE a.id = ?""", (first,)
        ).fetchone()
        self.assertEqual((row["gated"], row["gate_reason"]), (0, "trivial"),
                         "the archive keeps the gate's honest original decision")
        self.assertEqual(row["entity"], "thread:groupme:PSK IRL BGN",
                         "the later context, not the emoji alone, makes it model input")


class TestACalendarRenameIsNotAHundredNewEvents(Base):
    """`_identity` hashed the calendar's uid, and a calendar's uid is not stable.

    On 2026-08-04 Calendar.app began answering `calendar.uid()` and `calendar.name()`
    for a library that had read as one nameless "Calendar" for two days. Every identity
    in the store changed at once, so the scan concluded it had never seen any of it:
    111 duplicate events out of 252, every Partiful invitation declined, and a wiki
    project page per duplicated pair. The fixture that should have caught it derived
    `calendar_uid` from the calendar name, which is the one thing production did not do.
    """

    def item(self, uid: str, title: str, days: int, *, calendar: str = "Personal",
             calendar_uid: str | None = None, writable: bool = True,
             location: str = "") -> dict:
        start = datetime.combine(db.today() + timedelta(days=days),
                                 datetime.min.time(), tzinfo=timezone.utc).replace(hour=19)
        return {
            "calendar_name": calendar,
            "calendar_uid": calendar.lower() if calendar_uid is None else calendar_uid,
            "writable": writable, "uid": uid, "title": title,
            "start": start.isoformat(),
            "end": (start + timedelta(hours=2)).isoformat(),
            "all_day": False, "location": location, "description": "", "url": "",
        }

    def snapshot(self, items):
        return ical.ingest_snapshot(
            self.conn, self.cfg, items,
            scan_start=(db.today() - timedelta(days=120)).isoformat(),
            scan_end=(db.today() + timedelta(days=365)).isoformat())

    def test_the_same_event_under_a_new_calendar_uid_stays_one_row(self):
        db.set_today("2026-08-01")
        # Day one: Calendar.app will not name the calendar, so the JXA sends "".
        self.snapshot([self.item("appt-1", "Neurologist", 4,
                                 calendar="Calendar", calendar_uid="")])
        db.set_today("2026-08-04")
        # Day two: the same Apple event, now correctly attributed to "U&Me".
        self.snapshot([self.item("appt-1", "Neurologist", 1, calendar="U&Me")])

        rows = [row for row in events.window(self.conn, 5, 10)
                if row.title == "Neurologist"]
        self.assertEqual(len(rows), 1, "one Apple event is one memcal row")
        self.assertEqual(
            self.conn.execute("SELECT count(*) AS n FROM calendar_items"
                              "  WHERE event_uid = 'appt-1'").fetchone()["n"], 1)

    def test_moving_an_event_between_calendars_is_not_a_new_event(self):
        db.set_today("2026-08-01")
        self.snapshot([self.item("dinner", "Dinner with Cas", 3, calendar="Home")])
        self.snapshot([self.item("dinner", "Dinner with Cas", 3, calendar="Schedule")])
        self.assertEqual(
            len([r for r in events.window(self.conn, 0, 10)
                 if r.title == "Dinner with Cas"]), 1)

    def test_recurrence_still_splits_by_occurrence_after_the_uid_repair(self):
        """The repair matches on Apple's uid; a weekly class shares one uid all term."""
        db.set_today("2026-08-01")
        first = self.item("weekly", "Language class", 2, calendar="Calendar",
                          calendar_uid="")
        first["recurrence"] = "FREQ=WEEKLY"
        second = self.item("weekly", "Language class", 9, calendar="Calendar",
                           calendar_uid="")
        second["recurrence"] = "FREQ=WEEKLY"
        self.snapshot([first, second])
        db.set_today("2026-08-02")
        moved = [dict(first, calendar_name="Schedule", calendar_uid="Schedule"),
                 dict(second, calendar_name="Schedule", calendar_uid="Schedule")]
        self.snapshot(moved)
        self.assertEqual(
            len([r for r in events.window(self.conn, 0, 20)
                 if r.title == "Language class"]), 2)


class TestAPartifulEventStillOnTheCalendarIsNotDeclined(Base):
    """Fifteen invitations the user had accepted were filed as "not going" in one scan.

    `reconcile_missing` judged absence on memcal's own identity hash. The hash changed;
    the calendar did not. Their birthday, their wedding and the festival the user had bought a
    pass to were all declined on the strength of our own bookkeeping.
    """

    def item(self, uid: str, title: str, days: int, *, calendar_uid: str) -> dict:
        start = datetime.combine(db.today() + timedelta(days=days),
                                 datetime.min.time(), tzinfo=timezone.utc).replace(hour=19)
        return {
            "calendar_name": "Partiful", "calendar_uid": calendar_uid, "writable": False,
            "uid": uid, "title": title, "start": start.isoformat(),
            "end": (start + timedelta(hours=2)).isoformat(), "all_day": False,
            "location": "1 Commercial Blvd", "description": "", "url": "",
        }

    def snapshot(self, items):
        return ical.ingest_snapshot(
            self.conn, self.cfg, items,
            scan_start=(db.today() - timedelta(days=120)).isoformat(),
            scan_end=(db.today() + timedelta(days=365)).isoformat())

    def test_an_identity_change_is_not_an_rsvp(self):
        db.set_today("2026-08-01")
        self.snapshot([self.item("elements", "We're Going to Elements!!!", 6,
                                 calendar_uid="")])
        db.set_today("2026-08-04")
        report = self.snapshot([self.item("elements", "We're Going to Elements!!!", 3,
                                          calendar_uid="Partiful")])
        rows = [r for r in events.window(self.conn, 0, 10)
                if r.title.startswith("We're Going to Elements")]
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0].status, "declined",
                            "the invitation never left the calendar")
        self.assertFalse([n for n in report.notes if "declined" in n])

    def test_a_uid_that_really_vanished_is_still_a_decline(self):
        db.set_today("2026-08-01")
        self.snapshot([self.item("gone", "Rooftop party", 5, calendar_uid="Partiful"),
                       self.item("stays", "Picnic", 7, calendar_uid="Partiful")])
        db.set_today("2026-08-02")
        self.snapshot([self.item("stays", "Picnic", 6, calendar_uid="Partiful")])
        rows = {r.title: r for r in events.window(self.conn, 0, 10)}
        self.assertEqual(rows["Rooftop party"].status, "declined")
        self.assertNotEqual(rows["Picnic"].status, "declined")


class TestMemcalDoesNotReadItsOwnPublishedRowBackIn(Base):
    """A publish record names the row by key, and a key embeds the date.

    "Dungeons & Dragons" was written for 2026-08-02, keyed `dungeons-dragons@2026-08-02`,
    published to the memcal calendar, then corrected to the 16th and re-keyed. The
    publish record still named the dead key, `_diverged` read "no such row" as "the user
    deleted it, read the calendar copy back in", and the store gained a second D&D
    written by memcal from memcal's own calendar entry.
    """

    def test_a_publish_record_follows_its_row_to_a_new_key(self):
        db.set_today("2026-08-02")
        event, _ = events.upsert(self.conn, {"title": "Dungeons & Dragons",
                                             "date": "2026-08-02", "kind": "commitment",
                                             "status": "confirmed"})
        self.assertEqual(event.key, "dungeons-dragons@2026-08-02")
        start = "2026-08-16T22:00:00+00:00"
        self.conn.execute(
            """INSERT INTO calendar_items(identity, calendar_uid, calendar_name,
                   event_uid, event_key, starts_at, subscribed, provider, active,
                   published, revision, last_seen_at, updated_at)
               VALUES('id-dnd','memcal','memcal','dnd-uid',?,?,0,'ical',1,1,'r0',?,?)""",
            (event.key, start, db.now(), db.now()))
        # The row moves and is re-keyed by hand, exactly as invariant 13 asks for.
        self.conn.execute("UPDATE events SET key = ?, date = ? WHERE id = ?",
                          ("dungeons-dragons@2026-08-16", "2026-08-16", event.id))
        self.conn.commit()

        db.set_today("2026-08-04")
        ical.ingest_snapshot(
            self.conn, self.cfg,
            [{"calendar_name": "memcal", "calendar_uid": "memcal", "writable": True,
              "uid": "dnd-uid", "title": "Dungeons & Dragons", "start": start,
              "end": start, "all_day": False, "location": "", "description": "",
              "url": ""}],
            scan_start="2026-05-01", scan_end="2027-05-01")

        rows = [r for r in events.window(self.conn, 0, 30) if r.title == "Dungeons & Dragons"]
        self.assertEqual(len(rows), 1, "memcal's own calendar copy is not a second event")
        self.assertEqual(rows[0].key, "dungeons-dragons@2026-08-16")
        self.assertEqual(
            self.conn.execute("SELECT event_key FROM calendar_items"
                              "  WHERE identity != ''").fetchone()["event_key"],
            "dungeons-dragons@2026-08-16", "the publish record was repaired in passing")


class TestCorrectingAPublishedRowSticks(Base):
    """memcal published a row, read its own copy back, and undid the correction.

    Chili's was demoted to `opportunity`/`mentioned` and its invented location dropped.
    Twenty minutes later a scan restored `commitment`, `confirmed` and "Chili's",
    stamped `ical:created:memcal`, because `_diverged` compared the calendar copy
    against the *current row* — so a correction made here read as an edit made there.
    The loop re-ran every scan, which made a published row permanently uncorrectable.
    """

    def _published(self, event, state: str, start: str) -> None:
        self.conn.execute(
            """INSERT INTO calendar_items(identity, calendar_uid, calendar_name,
                   event_uid, event_key, starts_at, subscribed, provider, active,
                   published, published_state, revision, last_seen_at, updated_at)
               VALUES('id-1','memcal','memcal','uid-1',?,?,0,'ical',1,1,?,'r0',?,?)""",
            (event.key, start, state, db.now(), db.now()))
        self.conn.commit()

    def _scan(self, item):
        return ical.ingest_snapshot(self.conn, self.cfg, [item],
                                    scan_start="2026-01-01", scan_end="2027-01-01")

    def _item(self, **over):
        base_item = {"calendar_name": "memcal", "calendar_uid": "memcal",
                     "writable": True, "uid": "uid-1", "title": "Chili's",
                     "start": "2026-08-04T00:00:00", "end": "2026-08-05T00:00:00",
                     "all_day": True, "location": "Chili's", "description": "",
                     "url": ""}
        return {**base_item, **over}

    def test_the_calendar_copy_does_not_restore_what_was_corrected_here(self):
        db.set_today("2026-08-04")
        event, _ = events.upsert(self.conn, {
            "title": "Chili's", "date": "2026-08-04", "kind": "commitment",
            "status": "confirmed", "location": "Chili's"})
        self._published(event, "Chili's|2026-08-04|||Chili's", "2026-08-04T00:00:00")
        # The correction: no longer a commitment, and the location was the title again.
        self.conn.execute(
            "UPDATE events SET kind='opportunity', status='mentioned', location=NULL"
            "  WHERE id = ?", (event.id,))
        self.conn.commit()

        self._scan(self._item())
        after = events.get(self.conn, event.key)
        self.assertEqual((after.kind, after.status), ("opportunity", "mentioned"))
        self.assertIsNone(after.location)

    def test_a_time_memcal_cannot_parse_is_not_an_edit(self):
        """"at night" is published as all-day and reads back with no time at all."""
        db.set_today("2026-08-16")
        event, _ = events.upsert(self.conn, {
            "title": "Dungeons & Dragons", "date": "2026-08-16", "time": "at night",
            "kind": "commitment", "status": "tentative"})
        self._published(event, "Dungeons & Dragons|2026-08-16||at night|",
                        "2026-08-16T00:00:00")
        self._scan(self._item(title="Dungeons & Dragons", location="",
                              start="2026-08-16T00:00:00", end="2026-08-17T00:00:00"))
        self.assertEqual(events.get(self.conn, event.key).status, "tentative")

    def test_an_edit_made_in_calendar_app_is_still_read_back(self):
        db.set_today("2026-08-04")
        event, _ = events.upsert(self.conn, {
            "title": "Chili's", "date": "2026-08-04", "kind": "commitment",
            "status": "confirmed", "location": "Chili's"})
        self._published(event, "Chili's|2026-08-04|||Chili's", "2026-08-04T00:00:00")
        # The user moved it in Calendar.app. That is them telling us something.
        self._scan(self._item(start="2026-08-09T00:00:00", end="2026-08-10T00:00:00"))
        self.assertEqual(events.get(self.conn, event.key).date, "2026-08-09")


class TestAPublishedEventKeepsAValidInterval(Base):
    """Calendar.app rejected the whole write: "start date must be before the end date".

    Two independent ways to build a backwards interval. `_event_window` wrapped the end
    hour with `(hour + 1) % 24`, so a 23:00 event ended at 00:00 *the same morning*. And
    the JXA assigned `startDate` before `endDate` when updating, so moving an event
    later left it momentarily ending before it began — and EventKit validates on every
    assignment, not on save.
    """

    def test_a_late_evening_event_does_not_end_before_it_starts(self):
        event, _ = events.upsert(self.conn, {"title": "Last call", "date": "2026-08-10",
                                             "time": "23:00", "kind": "commitment"})
        start, end, all_day = ical._event_window(events.get(self.conn, event.key))
        self.assertFalse(all_day)
        self.assertLess(start, end)
        self.assertTrue(end.startswith("2026-08-11T00:00"), end)

    def test_the_writer_moves_the_far_endpoint_first(self):
        self.assertIn("currentEnd", ical.PUBLISH_JXA)
        order = ical.PUBLISH_JXA.index("event.endDate = end;\n      event.startDate = start;")
        self.assertGreater(order, 0, "the far endpoint has to be able to go first")


class TestAMultiDaySpanSurvivesTheCalendarRoundTrip(Base):
    """Both directions were a day out, in opposite directions, so they cancelled."""

    #: Calendar.app reports wall-clock times in the machine's own zone, and
    #: `ical._normalized` reads them with `.astimezone()`. So a fixture that hard-codes
    #: an offset is asserting something about the machine rather than about the code:
    #: these five tests were written at UTC-04:00 and were red at 23 of the 24 hourly
    #: offsets, including UTC — i.e. on essentially any CI runner. `MEMCAL_TODAY` cannot
    #: help, because the thing being assumed is the *zone*, not the day or the hour.
    #:
    #: Attaching the local offset for that date is what the fixture always meant:
    #: "23:59:59 on the 19th, where this calendar lives". `.astimezone()` on a naive
    #: datetime reads it as local and picks the right offset for the date, so this stays
    #: correct across a DST boundary too — which a literal `-04:00` does not, even here.
    def _local(self, stamp):
        return datetime.fromisoformat(stamp).astimezone().isoformat()

    def _item(self, end, all_day=True, start="2026-04-17T00:00:00"):
        return {"title": "big trip", "uid": "u", "start": self._local(start),
                "end": self._local(end), "all_day": all_day}

    def test_an_all_day_end_is_the_last_day_it_occupies(self):
        self.assertEqual(ical._normalized(self._item("2026-04-19T23:59:59"))["until"],
                         "2026-04-19")

    def test_a_midnight_end_is_still_read_as_exclusive(self):
        """The .ics convention has not gone away; it just is not the only one."""
        self.assertEqual(ical._normalized(self._item("2026-04-19T00:00:00"))["until"],
                         "2026-04-18")

    def test_a_single_day_all_day_event_has_no_span(self):
        self.assertIsNone(ical._normalized(self._item("2026-04-17T23:59:59"))["until"])
        self.assertIsNone(ical._normalized(self._item("2026-04-18T00:00:00"))["until"])

    def test_a_timed_event_keeps_its_last_day(self):
        item = self._item("2026-04-19T18:00:00", all_day=False)
        self.assertEqual(ical._normalized(item)["until"], "2026-04-19")

    def test_what_is_published_is_what_comes_back(self):
        """The property under test is that the two conventions agree end to end."""
        event, _ = events.upsert(self.conn, {
            "title": "Montana trip", "date": "2026-08-15", "until": "2026-08-23",
            "status": "confirmed", "kind": "commitment"}, written_by="live")
        start, end, all_day = ical._event_window(events.get(self.conn, event.key))
        self.assertTrue(all_day)
        # Calendar.app normalises the exclusive end it was given and reports the last
        # day at 23:59:59. That is the shape the scan sees, and it has to survive it.
        last = db.parse_date(end[:10]) - timedelta(days=1)
        item = self._item(f"{last.isoformat()}T23:59:59", start=start)
        self.assertEqual(ical._normalized(item)["until"], "2026-08-23")

    def test_updating_stops_being_all_day_before_the_dates_move(self):
        """The flag has to come off first, or the assigned end gains a day."""
        self.assertIn("event.alldayEvent = false", ical.PUBLISH_JXA)
        cleared = ical.PUBLISH_JXA.index("event.alldayEvent = false")
        for assignment in ("event.endDate = end;", "event.startDate = start;"):
            self.assertLess(cleared, ical.PUBLISH_JXA.index(assignment),
                            f"{assignment} runs while the event is still all-day")


class TestPublishingCanBeTakenBack(Base):
    """Publishing was one-way, so correcting a row could not fix the copy on their phone.

    "Chili's" reached their real calendar as a confirmed commitment out of a chat named
    "We are going to chilis". Demoting it in memcal did nothing about that: nothing
    removed anything, ever, so every mistake that got as far as Calendar.app stayed.
    """

    def _publish_record(self, key: str, uid: str) -> None:
        self.conn.execute(
            """INSERT INTO calendar_items(identity, calendar_uid, calendar_name,
                   event_uid, event_key, starts_at, subscribed, provider, active,
                   published, published_state, revision, last_seen_at, updated_at)
               VALUES(?,'memcal','memcal',?,?,'2026-08-04T00:00:00',0,'ical',1,1,'s','r',?,?)""",
            (f"id-{uid}", uid, key, db.now(), db.now()))
        self.conn.commit()

    def test_a_row_that_stopped_being_a_commitment_comes_off(self):
        self.cfg.publish_calendar = "memcal"
        event, _ = events.upsert(self.conn, {
            "title": "Chili's", "date": self.d(1), "kind": "opportunity",
            "status": "mentioned"})
        self._publish_record(event.key, "uid-chili")
        seen = []
        ical.retract_unpublishable(self.conn, self.cfg, dry_run=False,
                                   runner=lambda cmd, **kw: seen.append(cmd) or
                                   type("D", (), {"returncode": 0, "stdout": "{}",
                                                  "stderr": ""})())
        self.assertEqual(len(seen), 1)
        self.assertIn("uid-chili", seen[0])
        self.assertEqual(self.conn.execute(
            "SELECT published FROM calendar_items WHERE event_uid = 'uid-chili'"
        ).fetchone()["published"], 0)

    def test_a_confirmed_commitment_is_left_alone(self):
        self.cfg.publish_calendar = "memcal"
        event, _ = events.upsert(self.conn, {
            "title": "Tutoring", "date": self.d(1), "kind": "commitment",
            "status": "confirmed"})
        self._publish_record(event.key, "uid-tutoring")
        called = []
        ical.retract_unpublishable(self.conn, self.cfg, dry_run=False,
                                   runner=lambda cmd, **kw: called.append(cmd))
        self.assertEqual(called, [])

    def test_it_is_dry_by_default_because_it_deletes_outside_this_process(self):
        self.cfg.publish_calendar = "memcal"
        event, _ = events.upsert(self.conn, {"title": "Chili's", "date": self.d(1),
                                             "kind": "opportunity"})
        self._publish_record(event.key, "uid-dry")
        log = ical.retract_unpublishable(
            self.conn, self.cfg,
            runner=lambda *a, **k: self.fail("dry run must not touch Calendar"))
        self.assertTrue(any("would remove" in line for line in log))


class TestMemcalsCalendarIsNotLocal(Base):
    """Every event memcal published was on the Mac and on no other device."""

    def _runner(self, replies):
        """Fake osascript. `replies` is consumed in order; each is the JSON stdout."""
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            body = replies.pop(0) if replies else "{}"
            return type("Done", (), {"returncode": 0, "stdout": body, "stderr": ""})()

        return run, calls

    def test_the_publish_script_no_longer_creates_a_calendar(self):
        """The one line that could only ever have made a local calendar."""
        self.assertNotIn("app.calendars.push", ical.PUBLISH_JXA)
        self.assertIn("missing", ical.PUBLISH_JXA)

    def test_a_missing_calendar_is_created_in_icloud_and_the_write_retried(self):
        self.cfg.publish_calendar = "memcal"
        event, _ = events.upsert(self.conn, {
            "title": "Tutoring", "date": self.d(1), "kind": "commitment",
            "status": "confirmed"}, written_by="live")
        run, calls = self._runner([
            '{"missing": true}',
            '{"created": true, "calendar_id": "cal-1", "account": "iCloud", "status": 3}',
            '{"uid": "u-1", "calendar": "memcal", "calendar_uid": "cal-1"}',
        ])
        written = ical.publish(self.conn, self.cfg, event, runner=run)
        self.assertEqual(written["uid"], "u-1")
        self.assertEqual(len(calls), 3)
        # The middle call is the EventKit one, and it is what names the account.
        self.assertIn("ensure", calls[1])
        self.assertIn("EventKit", calls[1][4])

    def test_an_existing_calendar_costs_no_extra_call(self):
        self.cfg.publish_calendar = "memcal"
        event, _ = events.upsert(self.conn, {
            "title": "Poker", "date": self.d(1), "kind": "commitment",
            "status": "confirmed"}, written_by="live")
        run, calls = self._runner(
            ['{"uid": "u-2", "calendar": "memcal", "calendar_uid": "cal-1"}'])
        ical.publish(self.conn, self.cfg, event, runner=run)
        self.assertEqual(len(calls), 1)

    def test_a_local_calendar_is_reported_as_broken(self):
        self.cfg.publish_calendar = "memcal"
        run, _ = self._runner(['{"status": 3, "icloud_account": "iCloud", "found": '
                               '[{"account": "Default", "source_type": 0, '
                               '"syncs": false}]}'])
        ok, message = ical.account_status(self.cfg, runner=run)
        self.assertFalse(ok)
        self.assertIn("local", message)
        self.assertIn("migrate", message)

    def test_an_icloud_calendar_is_reported_as_healthy(self):
        self.cfg.publish_calendar = "memcal"
        run, _ = self._runner(['{"status": 3, "icloud_account": "iCloud", "found": '
                               '[{"account": "iCloud", "source_type": 2, '
                               '"syncs": true}]}'])
        ok, message = ical.account_status(self.cfg, runner=run)
        self.assertTrue(ok)
        self.assertIn("iCloud", message)

    def test_two_calendars_of_the_same_name_are_reported(self):
        """`calendar.uid()` raises on macOS 26, so publishing can only match by name."""
        self.cfg.publish_calendar = "memcal"
        run, _ = self._runner(['{"status": 3, "icloud_account": "iCloud", "found": ['
                               '{"account": "iCloud", "source_type": 2, "syncs": true},'
                               '{"account": "Default", "source_type": 0, "syncs": false}'
                               ']}'])
        ok, message = ical.account_status(self.cfg, runner=run)
        self.assertFalse(ok)
        self.assertIn("two calendars", message)

    def test_without_access_it_refuses_rather_than_guessing(self):
        """Status 0 is not-determined, and the store behind it is a placeholder."""
        self.cfg.publish_calendar = "memcal"
        run, _ = self._runner(['{"status": 0, "icloud_account": "", "found": []}'])
        ok, message = ical.account_status(self.cfg, runner=run)
        self.assertFalse(ok)
        self.assertIn("ical setup", message)

    def test_a_store_that_does_not_publish_never_shells_out(self):
        """Invariant 11: nothing that writes outside this process may default to on."""
        self.cfg.publish_calendar = ""

        def refuse(*args, **kwargs):
            raise AssertionError("account_status ran osascript with no calendar set")

        ok, _message = ical.account_status(self.cfg, runner=refuse)
        self.assertTrue(ok)

    def test_the_account_script_skips_the_virtual_store(self):
        self.assertIn(ical.EK_VIRTUAL_SOURCE, ical.ACCOUNT_JXA)
        self.assertIn("FULL_ACCESS", ical.ACCOUNT_JXA)


class TestMigratingTheCalendarKeepsMemcalsBookkeeping(Base):

    def _row(self, uid: str, key: str, *, starts_at: str = "2026-08-04T00:00:00",
             identity: str | None = None) -> str:
        identity = identity or ical._identity_of(uid, "")
        self.conn.execute(
            """INSERT INTO calendar_items(identity, calendar_uid, calendar_name,
                   event_uid, event_key, starts_at, subscribed, provider, active,
                   published, published_state, last_seen_at, updated_at)
               VALUES(?,'memcal','memcal',?,?,?,0,'ical',1,1,'Tutoring|2026-08-04|||',?,?)""",
            (identity, uid, key, starts_at, db.now(), db.now()))
        self.conn.commit()
        return identity

    def _runner(self, body):
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            reply = body.pop(0) if isinstance(body, list) else body
            return type("Done", (), {"returncode": 0, "stdout": reply, "stderr": ""})()

        return run, calls

    def test_a_dry_run_moves_nothing_and_says_what_it_would_move(self):
        self.cfg.publish_calendar = "memcal"
        self._row("old-1", "tutoring@2026-08-04")
        run, calls = self._runner(
            '{"local": true, "removed": 0, "notes": [], "moved": '
            '[{"title": "Tutoring", "start": "2026-08-04T17:00:00Z", "uid": "old-1", '
            '"new_uid": ""}], "status": 3}')
        log = ical.migrate_to_icloud(self.conn, self.cfg, runner=run)
        self.assertTrue(any("would move Tutoring" in line for line in log))
        self.assertIn("plan", calls[0])
        self.assertEqual(self.conn.execute(
            "SELECT event_uid FROM calendar_items").fetchone()["event_uid"], "old-1")

    def test_the_publish_record_follows_the_event_to_its_new_uid(self):
        self.cfg.publish_calendar = "memcal"
        self._row("old-1", "tutoring@2026-08-04")
        run, calls = self._runner(
            '{"local": true, "removed": 1, "notes": [], "moved": '
            '[{"title": "Tutoring", "start": "2026-08-04T17:00:00Z", "uid": "old-1", '
            '"new_uid": "new-1"}], "status": 3}')
        ical.migrate_to_icloud(self.conn, self.cfg, dry_run=False, runner=run)
        # Planned first, applied second: the safety check has to read the plan before
        # anything is deleted.
        self.assertIn("plan", calls[0])
        self.assertIn("apply", calls[1])
        row = self.conn.execute("SELECT * FROM calendar_items").fetchone()
        self.assertEqual(row["event_uid"], "new-1")
        self.assertEqual(row["identity"], ical._identity_of("new-1", ""))
        # Still recognised as memcal's own write, and still current, so the next sweep
        # neither reads it back in as news nor re-publishes it.
        self.assertEqual(row["published"], 1)
        self.assertEqual(row["published_state"], "Tutoring|2026-08-04|||")

    def test_a_recurring_series_keeps_one_row_per_occurrence(self):
        """`identity` splits occurrences by start; collapsing them would lose the term."""
        self.cfg.publish_calendar = "memcal"
        first = ical._identity_of("old-r", "2026-08-04T17:00:00")
        second = ical._identity_of("old-r", "2026-08-11T17:00:00")
        self._row("old-r", "class@2026-08-04", starts_at="2026-08-04T17:00:00",
                  identity=first)
        self._row("old-r", "class@2026-08-11", starts_at="2026-08-11T17:00:00",
                  identity=second)
        run, _ = self._runner(
            '{"local": true, "removed": 1, "notes": [], "moved": '
            '[{"title": "Class", "start": "2026-08-04T17:00:00Z", "uid": "old-r", '
            '"new_uid": "new-r"}], "status": 3}')
        ical.migrate_to_icloud(self.conn, self.cfg, dry_run=False, runner=run)
        rows = self.conn.execute(
            "SELECT identity, event_uid FROM calendar_items ORDER BY starts_at").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["event_uid"] for r in rows], ["new-r", "new-r"])
        self.assertEqual([r["identity"] for r in rows],
                         [ical._identity_of("new-r", "2026-08-04T17:00:00"),
                          ical._identity_of("new-r", "2026-08-11T17:00:00")])

    def test_nothing_local_to_move_is_said_plainly(self):
        self.cfg.publish_calendar = "memcal"
        run, _ = self._runner('{"local": false, "moved": [], "removed": 0, "status": 3}')
        log = ical.migrate_to_icloud(self.conn, self.cfg, runner=run)
        self.assertTrue(any("no local calendar" in line for line in log))

    def test_a_failed_copy_leaves_the_local_calendar_alone(self):
        """Deleting the original after a partial copy would destroy the difference."""
        self.cfg.publish_calendar = "memcal"
        self._row("old-1", "tutoring@2026-08-04")
        run, _ = self._runner([
            # the plan sees it, so the "empty reading" guard does not fire …
            '{"local": true, "removed": 0, "notes": [], "moved": '
            '[{"title": "Tutoring", "start": "2026-08-04T17:00:00Z", "uid": "old-1", '
            '"new_uid": ""}], "status": 3}',
            # … and the copy is what fails.
            '{"local": true, "removed": 0, "notes": ["could not copy Tutoring: nope"], '
            '"moved": [], "status": 3}',
        ])
        log = ical.migrate_to_icloud(self.conn, self.cfg, dry_run=False, runner=run)
        self.assertTrue(any("could not copy" in line for line in log))
        self.assertTrue(any("left in place" in line for line in log))
        # The record still points at the local copy, which is still the one that exists.
        self.assertEqual(self.conn.execute(
            "SELECT event_uid FROM calendar_items").fetchone()["event_uid"], "old-1")

    def test_an_empty_reading_is_refused_when_memcal_published_events_there(self):
        """The signature of a silently-failing enumeration, and the next step deletes.

        `predicateForEventsWithStartDate:endDate:calendars:` spans at most four years and
        matches *nothing* past that rather than raising. A -5y…+5y window returned 0 for
        the calendar holding their 7 published events, which is indistinguishable from an
        empty calendar — and the migration would then have deleted it with everything
        still inside. The window is chunked now; this is the check that would have caught
        it anyway, because the store already knows what it published.
        """
        self.cfg.publish_calendar = "memcal"
        self._row("old-1", "tutoring@2026-08-04")
        run, calls = self._runner(
            '{"local": true, "moved": [], "removed": 0, "notes": [], "status": 3}')
        log = ical.migrate_to_icloud(self.conn, self.cfg, dry_run=False, runner=run)
        self.assertTrue(any("refusing to migrate" in line for line in log))
        # Planned, and then stopped: nothing was applied and nothing was deleted.
        self.assertEqual(len(calls), 1)
        self.assertIn("plan", calls[0])
        self.assertEqual(self.conn.execute(
            "SELECT event_uid FROM calendar_items").fetchone()["event_uid"], "old-1")

    def test_a_genuinely_empty_local_calendar_still_migrates(self):
        """Nothing published there, nothing found: the empty reading is believable."""
        self.cfg.publish_calendar = "memcal"
        run, calls = self._runner(
            '{"local": true, "moved": [], "removed": 1, "notes": [], "status": 3}')
        log = ical.migrate_to_icloud(self.conn, self.cfg, dry_run=False, runner=run)
        self.assertEqual(len(calls), 2)
        self.assertTrue(any("deleted the local" in line for line in log))

    def test_the_event_scan_is_chunked_under_the_four_year_predicate_limit(self):
        self.assertIn("SPAN_YEARS", ical.ACCOUNT_JXA)
        self.assertIn("year < SPAN_YEARS", ical.ACCOUNT_JXA)

    def test_identity_hashing_did_not_change_when_it_was_factored_out(self):
        """`_identity` is what says "I have seen this before" for the whole store."""
        self.assertEqual(ical._identity({"uid": "abc", "recurrence": ""}),
                         ical._identity_of("abc", ""))
        self.assertEqual(
            ical._identity({"uid": "abc", "recurrence": "FREQ=WEEKLY",
                            "start": "2026-08-04T17:00:00"}),
            ical._identity_of("abc", "2026-08-04T17:00:00"))


class TestTwoCopiesOfOneDayAreNotASeries(Base):
    """`link_series` counted a duplicate as a repeat and opened a project page for it.

    Fifty-eight `projects/*.md` pages, each asking who hosts and how often: "Break",
    "Lunch", "Rest 10 min", "Math", "Clean", and Improv 101 as six series of one. The
    feed guard did not fire because the duplicate carried a different origin from the
    original — `ical:created:Schedule` against `ical:subscribed:ical`.
    """

    def test_the_same_day_twice_opens_no_page(self):
        for source in ("ical:subscribed:ical", "ical:created:Schedule"):
            events.upsert(self.conn, {"title": "Rest 10 min", "date": self.d(1),
                                      "source": source}, written_by="ical")
        # Same title, same day, two rows: a duplicate, whatever wrote it.
        self.conn.execute(
            "UPDATE events SET key = 'rest-10-min@dup' WHERE key != 'rest-10-min@'||date"
            "   AND title = 'Rest 10 min'")
        self.conn.commit()
        self.assertEqual(wiki.link_series(self.conn, self.cfg.wiki_dir), [])
        self.assertIsNone(wiki.read(self.cfg.wiki_dir, "rest-10-min"))

    def test_a_real_repeat_on_two_days_still_gets_its_page(self):
        # Far enough apart that `find_match` treats them as two occasions rather than
        # pooling them into one row, which is what its ten-day series window is for.
        events.upsert(self.conn, {"title": "Poker", "date": self.d(1),
                                  "source": "thread:imessage:poker"}, match=False)
        events.upsert(self.conn, {"title": "Poker", "date": self.d(40),
                                  "source": "thread:imessage:poker"}, match=False)
        self.assertIn("poker", wiki.link_series(self.conn, self.cfg.wiki_dir))


class TestAChatNameIsNotEvidenceOfAPlan(Base):
    """A group chat called "We are going to chilis" produced a confirmed commitment.

    The two lines in the bundle read "Hehe jk actually busy" and "Oh wait I am free
    tonight and also tommrooe njgbt". Nothing said Chili's; the thread's name did. The
    model's own reasoning is in the trace saying "the title alone shouldn't be the sole
    basis for inference" and "no clear confirmation of attendance, just availability" —
    and then it wrote `commitment`/`confirmed` at a location called "Chili's".
    """

    def _bundle(self, label: str, lines: list[str]):
        rows = []
        for index, text in enumerate(lines):
            aid = archive.append(self.conn, stream="imessage", external_id=f"c{index}",
                                 ts=db.today().isoformat() + "T10:23:00", text=text,
                                 thread=label, person="me", gated=True)
            archive.spool_add(self.conn, aid, f"thread:imessage:{label}")
            rows.append(self.conn.execute("SELECT * FROM archive WHERE id = ?",
                                          (aid,)).fetchone())
        self.conn.commit()
        return bundle_stage.Bundle(entity=f"thread:imessage:{label}", items=rows)

    def test_a_plan_named_only_by_the_thread_is_an_opportunity(self):
        bundle = self._bundle("We are going to chilis",
                              ["Hehe jk actually busy",
                               "Oh wait I am free tonight and also tommrooe njgbt"])
        diff = {"events": [{"title": "Chili's", "date": db.today().isoformat(),
                            "kind": "commitment", "status": "confirmed",
                            "location": "Chili's"}]}
        apply_stage.apply_diffs(self.conn, self.cfg, [(bundle, diff)], written_by="test")
        row = [r for r in events.window(self.conn, 0, 3) if r.title == "Chili's"][0]
        self.assertEqual(row.kind, "opportunity")
        self.assertEqual(row.status, "mentioned")
        self.assertIsNone(row.location, "a location copied off the title is not a place")

    def test_a_plan_somebody_actually_said_is_untouched(self):
        bundle = self._bundle("We are going to chilis",
                              ["Chili's at 8 tonight, I'm in", "see you there"])
        diff = {"events": [{"title": "Chili's", "date": db.today().isoformat(),
                            "kind": "commitment", "status": "confirmed"}]}
        apply_stage.apply_diffs(self.conn, self.cfg, [(bundle, diff)], written_by="test")
        row = [r for r in events.window(self.conn, 0, 3) if r.title == "Chili's"][0]
        self.assertEqual((row.kind, row.status), ("commitment", "confirmed"))


class TestARescanClobberedJudgement(Base):
    """`partiful.event_fields` re-derives kind and status from "does this feed row have
    a location" on every scan, with no new information. `upsert`'s guard protected an
    observed row's date from inference and nothing protected anyone's judgement from a
    *re*-derivation, so a calendar rename — which changes every revision and forces the
    whole snapshot to be re-derived — walked a confirmed commitment back."""

    def test_a_rederived_field_may_not_restate_itself_over_someone_else(self):
        row, _ = events.upsert(self.conn, {
            "title": "Jack's 30th", "date": self.d(9), "kind": "opportunity",
            "status": "mentioned", "source": "ical:subscribed:Partiful"},
            written_by="ical", inferred=("kind", "status"))
        events.upsert(self.conn, {"key": row.key, "title": "Jack's 30th",
                                  "date": self.d(9), "kind": "commitment",
                                  "status": "confirmed"}, written_by="dream:nightly")
        events.upsert(self.conn, {"key": row.key, "title": "Jack's 30th",
                                  "date": self.d(9), "kind": "opportunity",
                                  "status": "mentioned"},
                      written_by="ical", inferred=("kind", "status"))
        now = events.get_by_id(self.conn, row.id)
        self.assertEqual((now.kind, now.status), ("commitment", "confirmed"))

    def test_a_rederived_field_may_still_create(self):
        """The legitimate path. Nobody has decided anything, so the inference lands."""
        row, _ = events.upsert(self.conn, {
            "title": "Jack's 30th", "date": self.d(9), "kind": "opportunity",
            "status": "mentioned"}, written_by="ical", inferred=("kind", "status"))
        events.upsert(self.conn, {"key": row.key, "title": "Jack's 30th",
                                  "date": self.d(9), "kind": "commitment",
                                  "status": "confirmed"},
                      written_by="ical", inferred=("kind", "status"))
        now = events.get_by_id(self.conn, row.id)
        self.assertEqual((now.kind, now.status), ("commitment", "confirmed"))

    def test_an_observation_keeps_its_authority(self):
        """A disappearance from the feed is read off the calendar, not inferred from
        it, so `reconcile_missing` passes nothing as inferred and still declines."""
        row, _ = events.upsert(self.conn, {
            "title": "Jack's 30th", "date": self.d(9), "kind": "opportunity",
            "status": "mentioned"}, written_by="ical", inferred=("kind", "status"))
        events.upsert(self.conn, {"key": row.key, "title": "Jack's 30th",
                                  "date": self.d(9), "status": "confirmed"},
                      written_by="dream:nightly")
        events.upsert(self.conn, {"key": row.key, "title": "Jack's 30th",
                                  "date": self.d(9), "status": "declined"},
                      written_by="ical")
        self.assertEqual(events.get_by_id(self.conn, row.id).status, "declined")


class TestThreeRowsCalledElementsWereThreeUnrelatedPlans(Base):
    """A festival weekend and the things inside it read as unrelated rows on the same
    days. `part_of` is a column of its own because `series` is read by
    `find_match_scored`, which would merge exactly what this needs kept apart."""

    def _weekend(self):
        festival, _ = events.upsert(self.conn, {
            "title": "Elements Music Festival", "date": self.d(4), "until": self.d(6),
            "kind": "commitment", "status": "confirmed",
            "participants": ["Alex Rivera"]})
        breakfast, _ = events.upsert(self.conn, {
            "title": "Breakfast at Elements", "date": self.d(5), "time": "09:00",
            "kind": "commitment", "status": "confirmed",
            "participants": ["Alex Rivera"]})
        return festival, breakfast

    def test_a_sub_event_is_not_absorbed_by_the_span_it_sits_in(self):
        festival, breakfast = self._weekend()
        self.assertNotEqual(festival.id, breakfast.id)
        self.assertEqual(events.get_by_id(self.conn, festival.id).title,
                         "Elements Music Festival")
        self.assertEqual(events.get_by_id(self.conn, festival.id).until, self.d(6))

    def test_a_row_inside_a_span_that_names_it_is_nested(self):
        festival, breakfast = self._weekend()
        events.link_contained(self.conn)
        self.assertEqual(events.get_by_id(self.conn, breakfast.id).part_of, festival.id)
        self.assertEqual([e.id for e in events.children_of(self.conn, festival.id)],
                         [breakfast.id])

    def test_a_row_inside_a_span_that_does_not_name_it_is_left_alone(self):
        """Invariant 5. Falling on the same weekend is not evidence of anything."""
        festival, _ = self._weekend()
        other, _ = events.upsert(self.conn, {
            "title": "Dentist", "date": self.d(5), "status": "confirmed"})
        events.link_contained(self.conn)
        self.assertIsNone(events.get_by_id(self.conn, other.id).part_of)

    def test_two_overlapping_spans_are_two_plans(self):
        events.upsert(self.conn, {
            "title": "Elements Music Festival", "date": self.d(4), "until": self.d(6),
            "status": "confirmed"})
        trip, _ = events.upsert(self.conn, {
            "title": "Elements road trip", "date": self.d(5), "until": self.d(7),
            "status": "confirmed"})
        events.link_contained(self.conn)
        self.assertIsNone(events.get_by_id(self.conn, trip.id).part_of)

    def test_the_brief_nests_it(self):
        festival, _ = self._weekend()
        events.link_contained(self.conn)
        text = brief.render(self.conn, self.cfg)
        self.assertIn("Elements Music Festival", text)
        self.assertRegex(text, r"↳ .*Breakfast at Elements")


class TestAWordEveryTitleCarriesIsNotAName(Base):
    """`link_contained` said a child is nested only when it "names" its parent and
    tested for any shared token of three characters or more. `and` is three characters.
    On the live store that filed a Partiful party inside "Parents' visit and
    celebration" on the word `and` alone — one of the two false nestings out of four."""

    def _corpus(self):
        # Enough titles carrying the word that it can no longer say which one it means.
        # `NAME_DF_FLOOR` is 3, so this needs a fourth.
        for n, title in enumerate(("Coffee and a walk", "Beer and darts",
                                   "Bagels and the park", "Wine and cheese")):
            events.upsert(self.conn, {"title": title, "date": self.d(20 + n),
                                      "kind": "commitment", "status": "confirmed"})

    def test_a_common_word_does_not_nest(self):
        self._corpus()
        visit, _ = events.upsert(self.conn, {
            "title": "Parents' visit and celebration", "date": self.d(4),
            "until": self.d(5), "kind": "commitment", "status": "confirmed"})
        party, _ = events.upsert(self.conn, {
            "title": "Summer Bash and 1 Year in 3E", "date": self.d(4),
            "kind": "opportunity", "status": "mentioned"})
        events.link_contained(self.conn)
        self.assertIsNone(events.get_by_id(self.conn, party.id).part_of)
        self.assertNotIn(party.id,
                         [e.id for e in events.children_of(self.conn, visit.id)])

    def test_a_rare_word_in_the_same_titles_still_nests(self):
        """The guard is about the word, not about these two rows."""
        self._corpus()
        visit, _ = events.upsert(self.conn, {
            "title": "Parents' visit and Ravenswood celebration", "date": self.d(4),
            "until": self.d(5), "kind": "commitment", "status": "confirmed"})
        party, _ = events.upsert(self.conn, {
            "title": "Ravenswood summer bash", "date": self.d(4),
            "kind": "opportunity", "status": "mentioned"})
        events.link_contained(self.conn)
        self.assertEqual(events.get_by_id(self.conn, party.id).part_of, visit.id)

    def test_the_word_stops_naming_once_enough_rows_carry_it(self):
        """Ten rows about Elements is exactly when "Elements" stops meaning a weekend,
        and refusing to nest is then the answer rather than a regression."""
        span, _ = events.upsert(self.conn, {
            "title": "Elements festival", "date": self.d(4), "until": self.d(6),
            "kind": "commitment", "status": "confirmed"})
        child, _ = events.upsert(self.conn, {
            "title": "Breakfast at Elements", "date": self.d(5),
            "kind": "commitment", "status": "confirmed"})
        events.link_contained(self.conn)
        self.assertEqual(events.get_by_id(self.conn, child.id).part_of, span.id)
        for n in range(4):
            events.upsert(self.conn, {"title": f"Elements planning {n}",
                                      "date": self.d(20 + n), "kind": "commitment",
                                      "status": "confirmed"})
        events.link_contained(self.conn)
        self.assertIsNone(events.get_by_id(self.conn, child.id).part_of)


class TestSomebodyElsesFreeWeekIsNotAPlaceThingsHappenInside(Base):
    """`E278` — Cameron free the 16th to the 21st, `kind=availability` — had adopted
    `E269`, their own Sunday gaming session, because `link_contained` looked at every
    span and never at what kind of row it was. Nothing happens *inside* the week
    somebody else is free, and saying so is false about both rows."""

    def _pair(self, **parent):
        fields = {"title": "League or CS2 gaming", "date": self.d(1),
                  "until": self.d(6), "status": "confirmed"}
        fields.update(parent)
        window, _ = events.upsert(self.conn, fields)
        # `match=False` because when the parent is a commitment `find_match_scored`
        # merges these two into one row — same kind, overlapping words, one day apart —
        # and a single row cannot be nested inside itself. Two rows in every variant is
        # what makes the four cases below a comparison of `kind` and nothing else.
        session, _ = events.upsert(self.conn, {
            "title": "League gaming", "date": self.d(1), "time": "22:00",
            "kind": "commitment", "status": "confirmed"}, match=False)
        self.assertNotEqual(window.id, session.id)
        events.link_contained(self.conn)
        return window, session

    def test_an_availability_window_adopts_nothing(self):
        window, session = self._pair(kind="availability", subject="Cameron Ortiz")
        self.assertIsNone(events.get_by_id(self.conn, session.id).part_of)
        self.assertEqual(events.children_of(self.conn, window.id), [])

    def test_the_same_words_nest_under_an_occasion_he_is_in(self):
        """So the guard is the kind, not the titles."""
        window, session = self._pair(kind="commitment", subject="me")
        self.assertEqual(events.get_by_id(self.conn, session.id).part_of, window.id)

    def test_an_open_invitation_adopts_nothing(self):
        _, session = self._pair(kind="opportunity", subject="me")
        self.assertIsNone(events.get_by_id(self.conn, session.id).part_of)

    def test_someone_elses_span_adopts_nothing_even_as_a_commitment(self):
        _, session = self._pair(kind="commitment", subject="Cameron Ortiz")
        self.assertIsNone(events.get_by_id(self.conn, session.id).part_of)

    def test_can_contain_names_the_rule(self):
        for kind, subject, expected in (("commitment", "me", True),
                                        ("observed", "me", True),
                                        ("availability", "Cameron Ortiz", False),
                                        ("opportunity", "me", False),
                                        ("commitment", "Morgan", False)):
            row, _ = events.upsert(self.conn, {
                "title": f"{kind} {subject} span", "date": self.d(30),
                "until": self.d(31), "kind": kind, "subject": subject,
                "status": "confirmed"})
            self.assertEqual(events.can_contain(row), expected, (kind, subject))


class TestAWrongNestingWasPermanent(Base):
    """`UPDATE events SET part_of` was the only write to the column in the codebase and
    it could only ever *set*, so every wrong answer it gave outlived the rule that gave
    it. Invariant 13: the known-bad inference is repaired, not preserved."""

    def _bad_nesting(self):
        window, _ = events.upsert(self.conn, {
            "title": "League or CS2 gaming", "date": self.d(1), "until": self.d(6),
            "kind": "availability", "subject": "Cameron Ortiz", "status": "confirmed"})
        session, _ = events.upsert(self.conn, {
            "title": "League gaming", "date": self.d(1), "kind": "commitment",
            "status": "confirmed"})
        # What the old rule wrote, before it had either guard.
        self.conn.execute("UPDATE events SET part_of = ? WHERE id = ?",
                          (window.id, session.id))
        self.conn.commit()
        return window, session

    def test_a_nesting_the_rule_no_longer_endorses_is_cleared(self):
        window, session = self._bad_nesting()
        self.assertEqual(events.get_by_id(self.conn, session.id).part_of, window.id)
        moved = events.link_contained(self.conn)
        self.assertEqual(moved, 1)
        self.assertIsNone(events.get_by_id(self.conn, session.id).part_of)

    def test_the_brief_stops_nesting_it(self):
        self._bad_nesting()
        events.link_contained(self.conn)
        self.assertNotRegex(brief.render(self.conn, self.cfg), r"↳ .*League gaming")

    def test_a_nesting_the_rule_still_endorses_is_left_alone(self):
        span, _ = events.upsert(self.conn, {
            "title": "Ravenswood weekend", "date": self.d(4), "until": self.d(6),
            "kind": "commitment", "status": "confirmed"})
        child, _ = events.upsert(self.conn, {
            "title": "Breakfast at Ravenswood", "date": self.d(5),
            "kind": "commitment", "status": "confirmed"})
        self.assertEqual(events.link_contained(self.conn), 1)
        self.assertEqual(events.link_contained(self.conn), 0)
        self.assertEqual(events.get_by_id(self.conn, child.id).part_of, span.id)


class TestASixDayWindowOfSomebodyElsesTimeReadAsHisOwn(Base):
    """`〔E278〕 Sun Aug 16 "League or CS2 gaming" (until Fri Aug 21)` — reported as a
    thing that should not be there. The row was right: Cameron said the user was free the 16th
    to the 21st. `one_line` renders `participants` and never `subject`, so the one
    column saying whose week it was reached the detail view and the Later block and not
    the week block, which is the one always in context."""

    def _window(self, **over):
        fields = {"title": "League or CS2 gaming", "date": self.d(1),
                  "until": self.d(5), "kind": "availability",
                  "subject": "Cameron Ortiz", "status": "confirmed"}
        fields.update(over)
        row, _ = events.upsert(self.conn, fields)
        return events.get_by_id(self.conn, row.id)

    def test_the_line_says_whose_week_it_is(self):
        self.assertIn("Cameron Ortiz:", self._window().one_line(overview=True))

    def test_the_brief_says_whose_week_it_is(self):
        self._window()
        self.assertIn("Cameron Ortiz:", brief.render(self.conn, self.cfg))

    def test_a_title_that_already_names_them_is_not_doubled(self):
        line = self._window(title="Laura away", subject="Laura").one_line(overview=True)
        self.assertIn("Laura away", line)
        self.assertNotIn("Laura: Laura away", line)

    def test_his_own_rows_are_unchanged(self):
        line = self._window(kind="commitment", subject="me",
                            title="Dentist").one_line(overview=True)
        self.assertNotIn(":", line.split('"')[0])

    def test_the_subject_is_not_mistaken_for_a_companion(self):
        """The tail already ends "with Alex, Sam". A name down there is a companion."""
        line = self._window(participants=["Aaron", "Quinn Brooks"]).one_line(
            overview=True)
        self.assertLess(line.index("Cameron Ortiz:"), line.index("with Aaron"))

    def test_a_subject_who_is_also_a_participant_is_named_once(self):
        """3 of the 6 attributed rows in the live store are this: `whose` and `who` hold
        the same person, and "Morgan: Alatte, 9am — with Morgan" says it twice."""
        line = self._window(title="Alatte", subject="Morgan", kind="commitment",
                            participants=["Morgan"]).one_line(overview=True)
        self.assertIn("with Morgan", line)
        self.assertNotIn("Morgan:", line)

    def test_the_later_block_answers_the_question_the_same_way(self):
        """Two rules for one question is how "Laura away (Laura)" appears on one
        surface and not the other, and a reader who sees both trusts neither."""
        self._window(title="Laura away", subject="Laura", date=self.d(30),
                     until=self.d(34))
        self._window(title="League or CS2 gaming", subject="Cameron Ortiz",
                     date=self.d(30), until=self.d(34))
        text = brief.render(self.conn, self.cfg)
        later = text.split("## Later")[1]
        self.assertIn("(Cameron Ortiz)", later)
        self.assertNotIn("(Laura)", later)

    def test_detail_still_separates_who_from_whose(self):
        """What the line gives up is one handle away, which is why it may give it up."""
        row = self._window(title="Alatte", subject="Morgan", kind="commitment",
                           participants=["Morgan"])
        text = detail.open_handle(self.conn, self.cfg, f"E{row.id}")
        self.assertIn("who: Morgan", text)
        self.assertIn("whose: Morgan", text)


class TestOnlyACommitmentReachesTheRealCalendar(Base):
    """Invariant 11 — nothing that writes outside this process may be true by accident.
    `publishable` has always required `kind == "commitment"`, so Cameron's free week was
    never in danger of reaching Calendar.app; nothing asserted it, and the guard is one
    line beside a status check that reads as the whole rule."""

    def _row(self, **over):
        fields = {"title": "Dinner", "date": self.d(2), "kind": "commitment",
                  "status": "confirmed"}
        fields.update(over)
        row, _ = events.upsert(self.conn, fields)
        return events.get_by_id(self.conn, row.id)

    def test_a_commitment_he_confirmed_publishes(self):
        self.assertTrue(ical.publishable(self._row()))

    def test_no_other_kind_publishes(self):
        for kind in ("availability", "opportunity", "observed"):
            self.assertFalse(ical.publishable(self._row(kind=kind, title=kind)), kind)

    def test_somebody_elses_free_week_never_reaches_the_calendar(self):
        self.assertFalse(ical.publishable(self._row(
            title="League or CS2 gaming", kind="availability",
            subject="Cameron Ortiz", until=self.d(6))))

    def test_every_kind_but_commitment_is_refused(self):
        """So a new kind is refused by default rather than published by default."""
        publishable = [k for k in events.KINDS
                       if ical.publishable(self._row(kind=k, title=k))]
        self.assertEqual(publishable, ["commitment"])

class TestAnInvitationWasJustAnotherMaybe(Base):
    """`partiful.is_partiful` read the feed row's `url` for detection and threw it
    away. An invitation is a thing you RSVP *through* — you can forward the link —
    which is a different fact from something a friend mentioned in passing."""

    def _invite(self, status="mentioned"):
        row, _ = events.upsert(self.conn, {
            "title": "Jack's 30th", "date": self.d(9), "kind": "opportunity",
            "status": status, "rsvp_url": "https://partiful.com/e/jacks30",
            "source": "ical:subscribed:Partiful"}, written_by="ical")
        return row

    def test_an_unanswered_invitation_is_not_could_go(self):
        self.assertEqual(self._invite().plain_state(), "not replied")

    def test_something_merely_mentioned_still_reads_could_go(self):
        row, _ = events.upsert(self.conn, {
            "title": "Some party", "date": self.d(9), "kind": "opportunity",
            "status": "mentioned"})
        self.assertEqual(row.plain_state(), "could go")

    def test_the_link_survives_the_round_trip(self):
        row = self._invite()
        self.assertEqual(events.get_by_id(self.conn, row.id).rsvp_url,
                         "https://partiful.com/e/jacks30")

    def test_a_declined_invitation_stays_visible_with_its_link(self):
        row = self._invite()
        events.upsert(self.conn, {"key": row.key, "title": "Jack's 30th",
                                  "date": self.d(9), "status": "declined"},
                      written_by="ical")
        declined = events.get_by_id(self.conn, row.id)
        self.assertEqual(declined.plain_state(), "not going")
        self.assertTrue(brief._committed(declined))
        self.assertIn("partiful.com/e/jacks30", declined.one_line())

    def test_a_declined_plan_that_is_not_an_invitation_still_disappears(self):
        row, _ = events.upsert(self.conn, {
            "title": "Brunch", "date": self.d(9), "status": "declined"})
        self.assertFalse(brief._committed(row))


class TestACapacityRefusalWasTreatedAsAFault(Base):
    """`_post` gave every retryable status the same six attempts and ~15s of backoff.
    That is right for a 500 and useless for a busy hour: gpt-5.6-sol hit a 429 on day 3
    of a benchmark run, exhausted the budget, and four bundles were left unread — which
    grades identically to the model reading the traffic and understanding none of it."""

    def _client(self, status, *, headers=None):
        import io
        import urllib.error
        from memcal import llm
        calls = {"n": 0}

        def fail(req, timeout=None):
            calls["n"] += 1
            raise urllib.error.HTTPError(
                req.full_url, status, "nope", headers or {}, io.BytesIO(b"nope"))

        client = llm.OpenRouter("sk-or-test")
        self._real, llm.urllib.request.urlopen = llm.urllib.request.urlopen, fail
        self.addCleanup(setattr, llm.urllib.request, "urlopen", self._real)
        return client, calls, llm

    def test_a_queue_is_waited_out_far_past_the_fault_budget(self):
        client, calls, llm = self._client(429, headers={"Retry-After": "0.01"})
        with self.assertRaises(llm.LLMError) as caught:
            client._post("/x", {}, capacity_budget=3.0)
        self.assertGreater(calls["n"], 6)
        self.assertIn("capacity waits", str(caught.exception))

    def test_a_503_counts_as_capacity_too(self):
        client, calls, llm = self._client(503, headers={"Retry-After": "0.01"})
        with self.assertRaises(llm.LLMError):
            client._post("/x", {}, capacity_budget=2.0)
        self.assertGreater(calls["n"], 6)

    def test_a_fault_is_not_a_queue(self):
        """Trying harder does not fix a 500, and a benchmark that spends half an hour
        finding that out is worse than one that says so."""
        client, calls, llm = self._client(500)
        with self.assertRaises(llm.LLMError):
            client._post("/x", {}, capacity_budget=600.0)
        self.assertEqual(calls["n"], 6)

    def test_a_refusal_that_is_not_retryable_raises_at_once(self):
        client, calls, llm = self._client(401)
        with self.assertRaises(llm.LLMError):
            client._post("/x", {}, capacity_budget=600.0)
        self.assertEqual(calls["n"], 1)


class TestARicherTitleMadeASecondRow(Base):
    """`ical` writes no participants, so `find_match` tier 1 — which needs overlapping
    participants — can never fire for a feed row, and an identical title slug was the
    only thing that ever joined one. A model that wrote a *better* title than the
    calendar's therefore got a duplicate: gpt-5.6-terra produced "Jack's 30th birthday"
    beside the feed's "Jack's 30th" in three trials out of three, and the one duplicate
    row cost five separate benchmark checks."""

    def _feed_row(self, title="Jack's 30th", on=None):
        row, _ = events.upsert(self.conn, {
            "title": title, "date": on or self.d(9), "kind": "opportunity",
            "status": "mentioned", "source": "ical:subscribed:Partiful"},
            written_by="ical")
        return row

    def test_a_conversation_joins_the_feed_row_it_is_plainly_about(self):
        feed = self._feed_row()
        row, verb = events.upsert(self.conn, {
            "title": "Jack's 30th birthday", "date": self.d(9),
            "kind": "commitment", "status": "confirmed", "participants": ["Morgan"]},
            written_by="dream:nightly")
        self.assertEqual(verb, "updated")
        self.assertEqual(row.id, feed.id)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM events").fetchone()["n"], 1)

    def test_only_on_the_same_day(self):
        """A richer title is not evidence of a move. The feed is where the date came
        from, and this tier exists for the name, never for the day."""
        self._feed_row()
        _row, verb = events.upsert(self.conn, {
            "title": "Jack's 30th birthday", "date": self.d(11),
            "kind": "commitment", "status": "confirmed"}, written_by="dream:nightly")
        self.assertEqual(verb, "inserted")

    def test_an_occasion_noun_is_not_enough_to_absorb(self):
        """"Dinner" inside "Dinner with Alex" is an occasion noun matching an occasion
        noun — the coincidence this codebase has now paid for three times."""
        self.assertFalse(events._title_absorbs("Dinner", "Dinner with Alex"))
        self.assertFalse(events._title_absorbs("Party", "Party night"))
        self.assertTrue(events._title_absorbs("Jack's 30th", "Jack's 30th birthday"))

    def test_a_row_that_names_people_joins_too(self):
        """This was scoped to feed rows with nobody on them, because that is the shape
        the duplicate was found in. The evidence is the day and the words, and neither
        of them stops being evidence when the row happens to name a guest —
        `TestABbqAndABlockPartyBbqWereTwoRows` is the case that proves it."""
        feed, _ = events.upsert(self.conn, {
            "title": "Jack's 30th", "date": self.d(9), "participants": ["Jack"],
            "status": "confirmed", "source": "ical:subscribed:Partiful"},
            written_by="ical")
        row, verb = events.upsert(self.conn, {
            "title": "Jack's 30th birthday", "date": self.d(9),
            "kind": "commitment", "participants": ["Morgan"]}, written_by="dream:nightly")
        self.assertEqual(verb, "updated")
        self.assertEqual(row.id, feed.id)

    def test_two_unrelated_rows_on_one_day_stay_two(self):
        self._feed_row(title="Statehood Day")
        _row, verb = events.upsert(self.conn, {
            "title": "Beer garden", "date": self.d(9), "status": "confirmed"},
            written_by="dream:nightly")
        self.assertEqual(verb, "inserted")


class TestABbqAndABlockPartyBbqWereTwoRows(Base):

    def _rows(self, on):
        return [events.Event.from_row(r) for r in self.conn.execute(
            "SELECT * FROM events WHERE date = ? ORDER BY id", (on,))]

    def test_the_email_moves_the_row_the_group_chat_wrote(self):
        events.upsert(self.conn, {
            "title": "BBQ", "date": self.d(9), "time": "14:00",
            "status": "mentioned"}, written_by="dream:nightly")
        _row, verb = events.upsert(self.conn, {
            "title": "Devon's Block Party BBQ", "date": self.d(9), "time": "16:00",
            "status": "confirmed"}, written_by="dream:nightly")
        self.assertEqual(verb, "updated")
        rows = self._rows(self.d(9))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].time, "16:00")

    def test_it_works_in_the_order_the_richer_title_came_first(self):
        events.upsert(self.conn, {
            "title": "Devon's Block Party BBQ", "date": self.d(9),
            "status": "confirmed"}, written_by="dream:nightly")
        _row, verb = events.upsert(self.conn, {
            "title": "BBQ", "date": self.d(9), "status": "mentioned"},
            written_by="dream:nightly")
        self.assertEqual(verb, "unchanged")
        self.assertEqual(len(self._rows(self.d(9))), 1)

    def test_a_feed_row_joins_the_conversation_row_that_named_it_first(self):
        """The direction `sources/ical.py` takes: somebody mentions the party, and the
        invitation arrives afterwards carrying the shorter name."""
        events.upsert(self.conn, {
            "title": "Rae's housewarming party", "date": self.d(9),
            "status": "confirmed", "participants": ["Rae"]}, written_by="dream:nightly")
        _row, verb = events.upsert(self.conn, {
            "title": "Rae's housewarming", "date": self.d(9), "kind": "opportunity",
            "source": "ical:subscribed:Partiful"}, written_by="ical")
        self.assertEqual(verb, "updated")
        self.assertEqual(len(self._rows(self.d(9))), 1)

    def test_a_different_day_is_a_different_occasion(self):
        """This tier is about the name and never about the day. The evidence that two
        wordings are one occasion is that they land on the same date; without that
        there is nothing left but the words."""
        events.upsert(self.conn, {
            "title": "Devon's Block Party BBQ", "date": self.d(9),
            "status": "confirmed"}, written_by="dream:nightly")
        _row, verb = events.upsert(self.conn, {
            "title": "BBQ", "date": self.d(11), "status": "mentioned"},
            written_by="dream:nightly")
        self.assertEqual(verb, "inserted")


class TestTwoRowsOnOneDayBothAbsorbedOneTitle(Base):
    """Aug 11 holds "Superman movie" and "Movie with Riley", and a bare "Movie" is
    equally a shorter name for either. Nothing in the store can say which, so the
    tie-break would have been `-distance` — identical — and then SQLite's row order.

    A duplicate is recoverable by hand; a correct row silently renamed and re-timed is
    not, and nothing downstream would ever report it. So ambiguity refuses."""

    def test_an_ambiguous_shorter_title_joins_neither(self):
        for title in ("Superman movie", "Movie with Riley"):
            events.upsert(self.conn, {"title": title, "date": self.d(9),
                                      "status": "confirmed"}, written_by="dream:nightly")
        _row, verb = events.upsert(self.conn, {
            "title": "Movie", "date": self.d(9), "status": "confirmed"},
            written_by="dream:nightly")
        self.assertEqual(verb, "inserted")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM events").fetchone()["n"], 3)

    def test_one_candidate_still_joins(self):
        """The refusal is about there being two answers, not about there being one."""
        events.upsert(self.conn, {"title": "Superman movie", "date": self.d(9),
                                  "status": "confirmed"}, written_by="dream:nightly")
        _row, verb = events.upsert(self.conn, {
            "title": "Movie", "date": self.d(9), "status": "confirmed",
            "location": "AMC"}, written_by="dream:nightly")
        self.assertEqual(verb, "updated")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM events").fetchone()["n"], 1)

    def test_an_exact_title_beats_an_absorption_beside_it(self):
        """Two rows a shorter title could name are ambiguous; a row it names *outright*
        is not. Which of the two SQLite hands back first cannot be the difference."""
        for title in ("Movie", "Movie with Riley"):
            events.upsert(self.conn, {"title": title, "date": self.d(9),
                                      "status": "confirmed"},
                          written_by="dream:nightly", match=False)
        row, verb = events.upsert(self.conn, {
            "title": "Movie", "date": self.d(9), "location": "AMC"},
            written_by="dream:nightly")
        self.assertEqual(verb, "updated")
        self.assertEqual(row.title, "Movie")
        self.assertEqual(row.location, "AMC")


class TestOneSharedWordMeantNamedOnlyByTheThread(Base):
    """`_named_only_by_thread` weakens a row whose title appears nowhere but the chat's
    own name. It matched on
    *any* one word, so a WhatsApp group called "dinner thu" downgraded a confirmed ramen
    plan with a time and three guests to an opportunity, on the word `dinner`, while
    `ramen` — the part that says what the row is — is nowhere in the chat's name."""

    def test_a_title_the_thread_does_not_fully_account_for_is_ordinary(self):
        bundle = bundle_stage.Bundle(
            entity="thread:whatsapp:dinner thu", title="dinner thu",
            items=[], spool_ids=[])
        self.assertFalse(apply_stage._named_only_by_thread(
            bundle, {"title": "Ramen dinner"}))

    def test_a_title_that_is_only_the_chat_name_is_still_weakened(self):
        """The case this guard exists for. A chat called "We are going to chilis"
        produced `Chili's | commitment | confirmed` out of two lines that said nothing
        about Chili's."""
        bundle = bundle_stage.Bundle(
            entity="thread:imessage:We are going to chilis",
            title="We are going to chilis", items=[], spool_ids=[])
        self.assertTrue(apply_stage._named_only_by_thread(
            bundle, {"title": "Chili's"}))

    def test_a_person_bundle_is_never_named_only_by_its_thread(self):
        bundle = bundle_stage.Bundle(
            entity="person:Avery", title="Avery", items=[], spool_ids=[])
        self.assertFalse(apply_stage._named_only_by_thread(
            bundle, {"title": "Avery visits"}))


class TestAPossessiveWasNotTheSameName(Base):
    """`_talks_about_nothing_here` throws away a question whose every name is absent
    from its own evidence. "Devon's" squashes to `devons`, which is not a substring of
    `devonpark`, so "Where is Devon's housewarming?" against a bundle labelled *Devon
    Park* looked invented and was deleted. Found by writing the day-3 beat, which is
    what the corpus is for."""

    def test_a_possessive_matches_the_name_it_possesses(self):
        self.assertEqual(apply_stage._proper_nouns("Where is Devon's housewarming?"),
                         {"devon"})
        self.assertEqual(apply_stage._proper_nouns("Where is Devon’s housewarming?"),
                         {"devon"})

    def test_a_question_about_someones_thing_is_not_thrown_away(self):
        bundle = bundle_stage.Bundle(
            entity="person:Devon Park", title="Devon Park", items=[], spool_ids=[])
        self.assertFalse(apply_stage._talks_about_nothing_here(
            bundle, "Where is Devon's housewarming?"))

    def test_a_genuinely_invented_name_is_still_thrown_away(self):
        bundle = bundle_stage.Bundle(
            entity="person:Devon Park", title="Devon Park", items=[], spool_ids=[])
        self.assertTrue(apply_stage._talks_about_nothing_here(
            bundle, "When are you seeing Spider-Man?"))


class TestAQuestionLinkedOnOneGenericWord(Base):
    """`TITLE_MATCH = 0.5` against a two-word title is satisfied by one shared word.
    A board-game night and a tutor's appointment both filed themselves under
    "Alumni meeting" on `meeting`, and a parking question under "Hang out with Quinn"
    on `out`. Every wrong link scored exactly 0.50 on exactly one word."""

    def test_an_occasion_noun_is_not_a_subject(self):
        events.upsert(self.conn, {"title": "Alumni meeting", "date": self.d(6),
                                  "status": "confirmed"})
        key = todos.ask(self.conn, "Is the PSK meeting still happening on Saturday?")
        todos.relink_questions(self.conn)
        row = self.conn.execute(
            "SELECT about_event FROM questions WHERE key = ?", (key,)).fetchone()
        self.assertIsNone(row["about_event"])

    def test_out_is_not_a_subject(self):
        events.upsert(self.conn, {"title": "Hang out with Quinn Brooks",
                                  "date": self.d(4), "status": "confirmed",
                                  "participants": ["Quinn Brooks"]})
        key = todos.ask(self.conn, "Did you ever sort out the parking permit?")
        todos.relink_questions(self.conn)
        row = self.conn.execute(
            "SELECT about_event FROM questions WHERE key = ?", (key,)).fetchone()
        self.assertIsNone(row["about_event"])

    def test_one_shared_word_is_enough_when_it_names_only_that_row(self):
        """The floor is not "two words always" — "aspca" identifies its row outright."""
        row, _ = events.upsert(self.conn, {"title": "ASPCA clinic", "date": self.d(3),
                                           "status": "confirmed"})
        key = todos.ask(self.conn, "What time does the ASPCA thing start?")
        todos.relink_questions(self.conn)
        linked = self.conn.execute(
            "SELECT about_event FROM questions WHERE key = ?", (key,)).fetchone()
        self.assertEqual(linked["about_event"], row.id)

    def test_a_word_two_rows_share_no_longer_identifies_either(self):
        events.upsert(self.conn, {"title": "Elements festival", "date": self.d(4),
                                  "status": "confirmed"})
        events.upsert(self.conn, {"title": "Breakfast at Elements", "date": self.d(5),
                                  "status": "confirmed"})
        key = todos.ask(self.conn, "Who else is going to Elements?")
        todos.relink_questions(self.conn)
        title = self.conn.execute(
            "SELECT e.title FROM questions q JOIN events e ON e.id = q.about_event"
            " WHERE q.key = ?", (key,)).fetchone()
        self.assertIsNone(title)

    def test_a_named_row_is_still_found(self):
        """Without this, "link nothing to anything" would pass every check above."""
        row, _ = events.upsert(self.conn, {"title": "Board game night at Jose's",
                                           "date": self.d(5), "status": "confirmed"})
        key = todos.ask(self.conn, "Which day is the board game night at Jose's?")
        todos.relink_questions(self.conn)
        linked = self.conn.execute(
            "SELECT about_event FROM questions WHERE key = ?", (key,)).fetchone()
        self.assertEqual(linked["about_event"], row.id)


class TestAStaleQuestionLinkWasNeverReScored(Base):
    """`relink_questions` only considered questions linked to nothing, so a link made
    against the store as it stood that night was never looked at again. One live
    question is stuck on "Breakfast at Elements" because it linked while the festival
    row was wrongly declined — and a declined row is not a candidate."""

    def test_a_better_candidate_moves_the_link(self):
        festival, _ = events.upsert(self.conn, {
            "title": "Elements festival", "date": self.d(4), "until": self.d(6),
            "status": "declined"})
        breakfast, _ = events.upsert(self.conn, {
            "title": "Breakfast at Elements", "date": self.d(5), "status": "confirmed"})
        key = todos.ask(self.conn, "Which days is the Elements festival running?")
        self.conn.execute("UPDATE questions SET about_event = ? WHERE key = ?",
                          (breakfast.id, key))
        self.conn.commit()
        events.upsert(self.conn, {"key": festival.key, "title": "Elements festival",
                                  "date": self.d(4), "until": self.d(6),
                                  "status": "confirmed"}, written_by="live")
        todos.relink_questions(self.conn)
        linked = self.conn.execute(
            "SELECT about_event FROM questions WHERE key = ?", (key,)).fetchone()
        self.assertEqual(linked["about_event"], festival.id)

    def test_no_candidate_leaves_an_existing_link_alone(self):
        """Clearing on no match would unlink every question whose row has since passed
        or been declined — and `expire_questions` needs that link to know its subject
        is over."""
        row, _ = events.upsert(self.conn, {"title": "Climbing gym", "date": self.d(3),
                                           "status": "confirmed"})
        key = todos.ask(self.conn, "Are you still on for climbing?", about_event=row.id)
        events.upsert(self.conn, {"key": row.key, "title": "Climbing gym",
                                  "date": self.d(3), "status": "declined"})
        todos.relink_questions(self.conn)
        linked = self.conn.execute(
            "SELECT about_event FROM questions WHERE key = ?", (key,)).fetchone()
        self.assertEqual(linked["about_event"], row.id)


class TestAQuestionWasNeverClosedByItsAnswer(Base):
    """`todos.answer` was the only writer of `status='answered'` and every caller was
    human-initiated; `expire_questions` was age-only on `created_at` and `questions` has
    no date column at all. So a question outlived its occasion by ten days and sat open
    beside the row that had since answered it."""

    def test_a_question_dies_with_its_subject(self):
        row, _ = events.upsert(self.conn, {"title": "Gym", "date": self.d(1),
                                           "status": "mentioned"})
        key = todos.ask(self.conn, "Are you going to the gym tomorrow?",
                        about_event=row.id)
        db.set_today(db.today() + timedelta(days=3))
        try:
            todos.expire_questions(self.conn)
        finally:
            db.set_today(None)
        row_now = self.conn.execute(
            "SELECT status FROM questions WHERE key = ?", (key,)).fetchone()
        self.assertEqual(row_now["status"], "dropped")

    def test_the_row_outlives_the_question(self):
        row, _ = events.upsert(self.conn, {"title": "Gym", "date": self.d(1),
                                           "status": "mentioned"})
        todos.ask(self.conn, "Are you going to the gym tomorrow?", about_event=row.id)
        db.set_today(db.today() + timedelta(days=3))
        try:
            todos.expire_questions(self.conn)
        finally:
            db.set_today(None)
        self.assertIsNotNone(events.get_by_id(self.conn, row.id))

    def test_a_reconcile_question_is_exempt(self):
        """"Did the standup happen on Tuesday?" is the one question whose subject is
        supposed to be in the past. Expiring it deletes the backward window's only
        output on the pass after it was written."""
        row, _ = events.upsert(self.conn, {"title": "Standup", "date": self.d(-2),
                                           "status": "mentioned"})
        key = todos.ask(self.conn, "Did Standup happen on Monday?",
                        about_event=row.id, written_by="reconcile")
        todos.expire_questions(self.conn)
        row_now = self.conn.execute(
            "SELECT status FROM questions WHERE key = ?", (key,)).fetchone()
        self.assertEqual(row_now["status"], "open")

    def test_a_question_the_row_now_answers_is_dropped(self):
        row, _ = events.upsert(self.conn, {"title": "Tutoring", "date": self.d(6),
                                           "time": "16:00", "status": "confirmed"})
        key = todos.ask(self.conn, "What time is tutoring this week?", about_event=row.id)
        todos.expire_questions(self.conn)
        row_now = self.conn.execute(
            "SELECT status FROM questions WHERE key = ?", (key,)).fetchone()
        self.assertEqual(row_now["status"], "dropped")

    def test_a_dropped_question_records_why(self):
        row, _ = events.upsert(self.conn, {"title": "Tutoring", "date": self.d(6),
                                           "time": "16:00", "status": "confirmed"})
        key = todos.ask(self.conn, "What time is tutoring this week?", about_event=row.id)
        todos.expire_questions(self.conn)
        stamps = trace.history(self.conn, "question", key)
        self.assertTrue(any("already says time" in str(r["verb"]) for r in stamps))

    def test_a_question_the_row_does_not_answer_survives(self):
        row, _ = events.upsert(self.conn, {"title": "Tutoring", "date": self.d(6),
                                           "status": "confirmed"})
        key = todos.ask(self.conn, "What time is tutoring this week?", about_event=row.id)
        todos.expire_questions(self.conn)
        row_now = self.conn.execute(
            "SELECT status FROM questions WHERE key = ?", (key,)).fetchone()
        self.assertEqual(row_now["status"], "open")

    def test_a_mentioned_rows_date_is_a_guess_not_an_answer(self):
        row, _ = events.upsert(self.conn, {"title": "Tutoring", "date": self.d(6),
                                           "status": "mentioned"})
        key = todos.ask(self.conn, "Which day is tutoring this week?", about_event=row.id)
        todos.expire_questions(self.conn)
        row_now = self.conn.execute(
            "SELECT status FROM questions WHERE key = ?", (key,)).fetchone()
        self.assertEqual(row_now["status"], "open")

    def test_an_unlinked_obligation_is_not_a_question_and_survives(self):
        """Invariant 5's counter-case. A to-do with no occasion attached has no day to
        die with, and must not be swept up by a rule written for questions."""
        todo, _ = todos.open_todo(self.conn, "Return Rowan's EZ-Pass")
        db.set_today(db.today() + timedelta(days=30))
        try:
            todos.expire_event_links(self.conn)
            todos.expire_questions(self.conn)
        finally:
            db.set_today(None)
        self.assertEqual(todos.get(self.conn, todo.key).status, "open")


class TestAQuestionWithNoEventLinkHadNoDayToDieWith(Base):
    """`_subject_has_passed` reached a question's day only through `about_event`, so an
    inner join dropped every question that had no row beside it. On the live store that
    was 7 of 12 open questions — all written by the ordinary nightly path — and two of
    them were asking what time the user was playing on a Sunday five days gone. `about_date`
    is the missing column and the rule is total against it."""

    def named(self, offset: int) -> str:
        """A day written the way a model writes it: "Sunday, August 16"."""
        return (db.today() + timedelta(days=offset)).strftime("%A, %B %-d")

    def test_an_unlinked_question_dies_with_the_day_it_names(self):
        key = todos.ask(self.conn,
                        f"What time are you playing League on {self.named(1)}?")
        self.assertIsNone(self.conn.execute(
            "SELECT about_event FROM questions WHERE key = ?", (key,)).fetchone()[0])
        db.set_today(db.today() + timedelta(days=2))
        try:
            todos.expire_questions(self.conn)
        finally:
            db.set_today(None)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM questions WHERE key = ?", (key,)).fetchone()[0],
            "dropped")

    def test_the_drop_records_which_day_it_was_waiting_on(self):
        """`_redundant_with_linked_event` has stamped its reason since it was written and
        this arm never did. It decides most of the drops now."""
        key = todos.ask(self.conn,
                        f"What time are you playing League on {self.named(1)}?")
        db.set_today(db.today() + timedelta(days=2))
        try:
            todos.expire_questions(self.conn)
        finally:
            db.set_today(None)
        self.assertTrue(any(f"({self.d(1)}) has passed" in str(row["verb"])
                            for row in trace.history(self.conn, "question", key)))

    def test_it_survives_the_day_it_is_about(self):
        """Litter starts the day after, not on the day itself."""
        key = todos.ask(self.conn,
                        f"What time are you playing League on {self.named(1)}?")
        db.set_today(db.today() + timedelta(days=1))
        try:
            todos.expire_questions(self.conn)
        finally:
            db.set_today(None)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM questions WHERE key = ?", (key,)).fetchone()[0], "open")

    def test_the_last_day_named_is_the_one_it_dies_with(self):
        """"Come Saturday and stay through Sunday" is live until the Sunday — the same
        reason the linked rule reads `coalesce(until, date)` rather than `date`."""
        key = todos.ask(self.conn, f"Morgan asked: do your parents want to come "
                                   f"{self.named(2)} and stay through {self.named(3)}?")
        db.set_today(db.today() + timedelta(days=3))
        try:
            todos.expire_questions(self.conn)
        finally:
            db.set_today(None)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM questions WHERE key = ?", (key,)).fetchone()[0], "open")

    def test_a_question_naming_no_day_waits_out_its_ttl(self):
        """The fallback is the rule that was already there. A question with no day in
        it has nothing to die with, and inventing one would be invariant 5."""
        key = todos.ask(self.conn,
                        "When are you and Cameron Ortiz playing the next League game?")
        self.assertIsNone(self.conn.execute(
            "SELECT about_date FROM questions WHERE key = ?", (key,)).fetchone()[0])
        db.set_today(db.today() + timedelta(days=todos.QUESTION_TTL_DAYS - 1))
        try:
            todos.expire_questions(self.conn)
            self.assertEqual(self.conn.execute(
                "SELECT status FROM questions WHERE key = ?",
                (key,)).fetchone()[0], "open")
            db.set_today(db.today() + timedelta(days=2))
            todos.expire_questions(self.conn)
        finally:
            db.set_today(None)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM questions WHERE key = ?", (key,)).fetchone()[0],
            "dropped")

    def test_a_cadence_is_not_a_day(self):
        """`series.roll_forward` asks "What time is Poker on Fridays now?" once, keyed,
        and `ON CONFLICT DO NOTHING` means a question dropped can never be re-asked.
        A plural weekday names no particular Friday and must resolve to nothing."""
        key = todos.ask(self.conn, "What time is Poker on Fridays now?")
        self.assertIsNone(self.conn.execute(
            "SELECT about_date FROM questions WHERE key = ?", (key,)).fetchone()[0])

    def test_the_row_decides_when_there_is_one(self):
        """A row is the current truth about its own day; the question's wording is a
        snapshot of what somebody said. A plan that moved must not be expired by the
        sentence that named the day it moved off."""
        row, _ = events.upsert(self.conn, {"title": "Escape room", "date": self.d(6),
                                           "status": "confirmed"})
        key = todos.ask(self.conn,
                        f"Are you still doing the escape room, was it {self.named(-3)}?",
                        about_event=row.id)
        stored = self.conn.execute(
            "SELECT about_date FROM questions WHERE key = ?", (key,)).fetchone()[0]
        self.assertEqual(stored, self.d(-3))
        todos.expire_questions(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM questions WHERE key = ?", (key,)).fetchone()[0], "open")

    def test_a_reconcile_question_is_still_exempt(self):
        """The backward window asks about the past on purpose, and now there is a column
        that could convict it. The exemption has to survive the column."""
        key = todos.ask(self.conn, f"Did the standup happen on {self.named(-2)}?",
                        written_by="reconcile")
        todos.expire_questions(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM questions WHERE key = ?", (key,)).fetchone()[0], "open")

    def test_a_reconcile_question_carries_no_day_at_all(self):
        """`resolve` only ever answers forward. "Did Play Half-Life 2 happen on Friday?",
        asked on the Saturday about the Friday before, resolves to the Friday *after* —
        a wrong date nothing reads today is a wrong date something reads later."""
        weekday = (db.today() - timedelta(days=1)).strftime("%A")
        key = todos.ask(self.conn, f"Did the standup happen on {weekday}?",
                        written_by="reconcile")
        self.assertIsNone(self.conn.execute(
            "SELECT about_date FROM questions WHERE key = ?", (key,)).fetchone()[0])
        # And the backfill does not put one on next pass either — write time and repair
        # time have to agree, or every night undoes the other.
        todos.backfill_about_date(self.conn)
        self.assertIsNone(self.conn.execute(
            "SELECT about_date FROM questions WHERE key = ?", (key,)).fetchone()[0])

    def test_the_backfill_reaches_rows_written_before_the_column(self):
        """Deriving at write time only helps the questions written since. The seven on
        the live store were already there."""
        self.conn.execute(
            "INSERT INTO questions(key, text, written_by, created_at)"
            " VALUES(?,?,?,?)",
            ("q:old", f"What time are you playing games on {self.named(1)} night?",
             "dream:nightly", db.now()))
        self.conn.commit()
        todos.relink_questions(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT about_date FROM questions WHERE key = 'q:old'").fetchone()[0],
            self.d(1))

    def test_the_backfill_reads_a_weekday_against_the_day_it_was_asked(self):
        """"Sunday" means the Sunday near whoever said it. Resolving an old question's
        weekday against today walks its subject forward a week every night it stays
        open, and it would never expire."""
        weekday = (db.today() + timedelta(days=1)).strftime("%A")
        self.conn.execute(
            "INSERT INTO questions(key, text, written_by, created_at) VALUES(?,?,?,?)",
            ("q:weekday", f"What time are you playing League on {weekday}?",
             "dream:nightly", db.now()))
        self.conn.commit()
        db.set_today(db.today() + timedelta(days=14))
        try:
            todos.relink_questions(self.conn)
        finally:
            db.set_today(None)
        self.assertEqual(self.conn.execute(
            "SELECT about_date FROM questions WHERE key = 'q:weekday'").fetchone()[0],
            self.d(1))

    def test_an_existing_store_gains_the_column(self):
        """`schema.sql` is `CREATE TABLE IF NOT EXISTS`, so a bare schema edit never
        reaches a database that already has the table. `ADDED_COLUMNS` is the half that
        does, and it is the half that is easy to forget."""
        path = Path(self.tmp.name) / "old.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE questions (id INTEGER PRIMARY KEY, key TEXT UNIQUE"
                     " NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL"
                     " DEFAULT 'open', written_by TEXT NOT NULL DEFAULT 'cli',"
                     " created_at TEXT NOT NULL)")
        conn.commit()
        conn.close()
        upgraded = db.open_db(path)
        try:
            columns = {row[1] for row in upgraded.execute("PRAGMA table_info(questions)")}
            self.assertIn("about_date", columns)
        finally:
            upgraded.close()


class TestAConfirmedRowReadCouldGo(Base):
    """`plain_state` tested `kind == "opportunity"` before it looked at status, so a
    festival the user had said in writing the user was definitely going to rendered "could go" —
    and `brief._committed` dropped it out of `## Later` entirely."""

    def test_a_settled_status_outranks_the_kind(self):
        row, _ = events.upsert(self.conn, {
            "title": "Elements festival", "date": self.d(4), "kind": "opportunity",
            "status": "confirmed"})
        self.assertEqual(row.plain_state(), "confirmed")

    def test_an_unsettled_opportunity_still_reads_could_go(self):
        """The subscribed holiday feed arrives as opportunity + mentioned and is
        exactly what `_committed` exists to keep out."""
        row, _ = events.upsert(self.conn, {
            "title": "Bank holiday", "date": self.d(4), "kind": "opportunity",
            "status": "mentioned"})
        self.assertEqual(row.plain_state(), "could go")
        self.assertFalse(brief._committed(row))

    def test_a_confirmed_opportunity_reaches_the_later_block(self):
        row, _ = events.upsert(self.conn, {
            "title": "Elements festival", "date": self.d(4), "kind": "opportunity",
            "status": "confirmed"})
        self.assertTrue(brief._committed(row))

    def test_a_declined_opportunity_still_reads_not_going(self):
        row, _ = events.upsert(self.conn, {
            "title": "Housewarming", "date": self.d(4), "kind": "opportunity",
            "status": "declined"})
        self.assertEqual(row.plain_state(), "not going")


class TestTheSweepAskedTheUserToDoMemcalsHomework(Base):
    """Two live questions were `written_by=sweep`, and one was literally "Were the
    three August 7 Elements entries the same event…?" — which the sweep's own
    instructions already forbid. Both carried zero evidence and zero provenance."""

    def test_deduplicating_the_calendar_is_not_a_question(self):
        self.assertFalse(todos.is_worth_asking(
            "Were the three August 7 Elements entries the same event or three separate?"))
        self.assertFalse(todos.is_worth_asking("Are these two rows the same event?"))

    def test_a_real_question_is_untouched_by_that_rule(self):
        self.assertTrue(todos.is_worth_asking("Are you and Riley still on for dinner?"))
        self.assertTrue(todos.is_worth_asking(
            "Were the tickets the same price as last time?"))

    def test_the_backward_window_stamps_what_it_asks(self):
        row, _ = events.upsert(self.conn, {"title": "Standup", "date": self.d(-1),
                                           "kind": "commitment", "status": "mentioned"})
        sweep_stage.reconcile_backward_window(self.conn, self.cfg)
        key = f"q:resolve:{row.key}"
        self.assertTrue(trace.history(self.conn, "question", key))

    def test_the_sweep_can_see_what_it_already_asked(self):
        todos.ask(self.conn, "Who is coming to the block party on Saturday?")
        snapshot = sweep_stage.state_snapshot(self.conn, self.cfg, [])
        self.assertIn("QUESTIONS ALREADY OPEN", snapshot)
        self.assertIn("block party", snapshot)


class TestEachWeekOfASeriesAbsorbedTheLastOne(Base):
    """`find_match_scored`'s strongest tier is "same series, within ten days", and a
    weekly series is seven days apart — so occurrence N+1 matched N, `upsert` re-dated
    it, and a standing appointment presented as **one row walking around the calendar**
    rather than a series with a history. The live store's tutoring row is the evidence:
    one id, dated 08-03, then 08-11, then 08-10, then 08-12, with `series` never set.

    Ten days was a proxy for a cadence nothing recorded. With the rule in the store the
    question is a lookup — do these two land on the same scheduled day."""

    def _weekly(self, slug="tutoring", weekday=1, time="13:00", start="2026-08-11"):
        rule, _ = series.upsert(self.conn, {
            "slug": slug, "title": "Tutoring", "cadence": "weekly", "weekday": weekday,
            "time": time, "effective_on": start}, written_by="cli")
        return rule

    def test_two_weeks_of_one_series_are_two_rows(self):
        self._weekly()
        first, _ = events.upsert(self.conn, {
            "title": "Tutoring", "date": "2026-08-11", "time": "13:00",
            "series": "tutoring", "kind": "commitment"}, written_by="cli")
        second, verb = events.upsert(self.conn, {
            "title": "Tutoring", "date": "2026-08-18", "time": "13:00",
            "series": "tutoring", "kind": "commitment"}, written_by="cli")
        self.assertEqual(verb, "inserted")
        self.assertNotEqual(first.id, second.id)

    def test_the_same_week_still_merges(self):
        """The counterweight. If a different scheduled day is enough to refuse a match,
        the same scheduled day must still make one, or this is just "never merge"."""
        self._weekly()
        first, _ = events.upsert(self.conn, {
            "title": "Tutoring", "date": "2026-08-11", "series": "tutoring",
            "kind": "commitment", "status": "mentioned"}, written_by="cli")
        again, verb = events.upsert(self.conn, {
            "title": "Tutoring", "date": "2026-08-11", "series": "tutoring",
            "kind": "commitment", "status": "confirmed"}, written_by="cli")
        self.assertEqual(verb, "updated")
        self.assertEqual(again.id, first.id)

    def test_a_series_with_no_rule_keeps_the_old_window(self):
        """`poker-night` and the moved physio slot have no cadence recorded, and for
        them "near enough" is genuinely the best available reading. This must not have
        quietly become stricter for every series in the store."""
        first, _ = events.upsert(self.conn, {
            "title": "Poker at Jordan's", "date": self.d(5), "series": "poker-night",
            "kind": "commitment"}, written_by="cli")
        moved, verb = events.upsert(self.conn, {
            "title": "Poker", "date": self.d(6), "series": "poker-night",
            "kind": "commitment"}, written_by="cli")
        self.assertEqual(verb, "updated")
        self.assertEqual(moved.id, first.id)

    def test_a_different_week_is_not_rescued_by_the_shared_title(self):
        """Refusing only at tier 3 left the row to be swept up at tier 2 on the strength
        of having the same title — which every occurrence of a series has, by
        construction. That is how this stayed broken after the first fix."""
        self._weekly()
        events.upsert(self.conn, {
            "key": "ical-tutoring@2026-08-18", "title": "Tutoring", "date": "2026-08-18",
            "series": "tutoring", "kind": "commitment"}, written_by="ical", match=False)
        moved, verb = events.upsert(self.conn, {
            "title": "Tutoring", "date": "2026-08-12", "time": "12:00",
            "series": "tutoring", "kind": "commitment",
            "instead_of": "2026-08-11"}, written_by="cli")
        self.assertEqual(verb, "inserted")
        self.assertEqual(events.get(self.conn, "ical-tutoring@2026-08-18").date,
                         "2026-08-18")

    def test_the_old_cadence_does_not_absorb_the_new_one(self):
        """A Monday that happened under the old schedule and the Wednesday standing in
        for the first new Tuesday sit two days apart. `effective_on` is the boundary:
        the rule in force places no day before it."""
        events.upsert(self.conn, {
            "key": "ical-tutoring@2026-08-10", "title": "Tutoring", "date": "2026-08-10",
            "time": "10:00", "series": "tutoring", "kind": "commitment",
            "status": "happened"}, written_by="ical", match=False)
        self._weekly()
        moved, verb = events.upsert(self.conn, {
            "title": "Tutoring", "date": "2026-08-12", "time": "12:00",
            "series": "tutoring", "kind": "commitment",
            "instead_of": "2026-08-11"}, written_by="cli")
        self.assertEqual(verb, "inserted")
        self.assertEqual(events.get(self.conn, "ical-tutoring@2026-08-10").date,
                         "2026-08-10")


class TestASeriesHadNoScheduleToChange(Base):
    """*"can we move to tuesday at 1pm going forward"* had nowhere to land: `series` was
    a wiki slug, and the store held occurrences and never the rule that generates them.
    So the Mondays could not end — there were only Mondays in the past, and the past is
    not wrong."""

    def test_a_rule_projects_its_own_next_occurrence(self):
        rule, _ = series.upsert(self.conn, {
            "slug": "tutoring", "title": "Tutoring", "cadence": "weekly", "weekday": 1,
            "time": "13:00", "effective_on": "2026-08-11"}, written_by="cli")
        landed = series.occurrences(None, "2026-08-01", "2026-09-01", series=rule)
        self.assertEqual([d.isoformat() for d in landed],
                         ["2026-08-11", "2026-08-18", "2026-08-25", "2026-09-01"])

    def test_a_fortnightly_rule_is_anchored_on_its_effective_day(self):
        """"Every other Tuesday" is only meaningful relative to a Tuesday somebody
        named, so `effective_on` is the anchor and not merely a lower bound."""
        rule, _ = series.upsert(self.conn, {
            "slug": "standup", "title": "Standup", "cadence": "fortnightly",
            "weekday": 1, "effective_on": "2026-08-11"}, written_by="cli")
        landed = series.occurrences(None, "2026-08-01", "2026-09-10", series=rule)
        self.assertEqual([d.isoformat() for d in landed],
                         ["2026-08-11", "2026-08-25", "2026-09-08"])

    def test_a_monthly_rule_does_not_invent_a_day_february_lacks(self):
        """Clamping "the 31st" to the 28th would invent a meeting nobody agreed to."""
        rule, _ = series.upsert(self.conn, {
            "slug": "rent", "title": "Rent", "cadence": "monthly", "day_of_month": 31,
            "effective_on": "2027-01-01"}, written_by="cli")
        landed = series.occurrences(None, "2027-01-01", "2027-04-01", series=rule)
        self.assertEqual([d.isoformat() for d in landed],
                         ["2027-01-31", "2027-03-31"])

    def test_a_cadence_change_moves_the_old_rule_to_history(self):
        series.upsert(self.conn, {
            "slug": "tutoring", "title": "Tutoring", "cadence": "weekly", "weekday": 0,
            "time": "10:00", "effective_on": "2026-06-01"}, written_by="cli")
        series.upsert(self.conn, {
            "slug": "tutoring", "cadence": "weekly", "weekday": 1, "time": "13:00",
            "effective_on": "2026-08-11"}, written_by="cli")
        rule = series.get(self.conn, "tutoring")
        self.assertEqual((rule.weekday, rule.time), (1, "13:00"))
        moved = {(r["field"], r["old_value"], r["new_value"]) for r in self.conn.execute(
            "SELECT * FROM series_history WHERE slug = 'tutoring'")}
        self.assertIn(("weekday", "0", "1"), moved)
        self.assertIn(("time", "10:00", "13:00"), moved)

    def test_a_series_with_no_cadence_refuses_to_project(self):
        """"We meet about monthly" is a true thing somebody said and is not a schedule.
        Inventing a weekday for it would be invariant 5."""
        rule, _ = series.upsert(self.conn, {
            "slug": "book-club", "title": "Book club"}, written_by="cli")
        self.assertFalse(rule.projectable)
        self.assertEqual(series.roll_forward(self.conn, slug="book-club"), [])

    def test_ending_a_series_keeps_every_row_it_produced(self):
        events.upsert(self.conn, {
            "key": "tutoring@2026-08-04", "title": "Tutoring", "date": "2026-08-04",
            "series": "tutoring", "kind": "commitment"}, written_by="cli", match=False)
        series.upsert(self.conn, {
            "slug": "tutoring", "title": "Tutoring", "cadence": "weekly", "weekday": 1,
            "effective_on": "2026-08-04"}, written_by="cli")
        series.end(self.conn, "tutoring", on="2026-09-01", written_by="live")
        self.assertEqual(series.get(self.conn, "tutoring").status, "ended")
        self.assertIsNotNone(events.get(self.conn, "tutoring@2026-08-04"))


class TestOneWeekMovedIsNotTheCadenceMoving(Base):
    """*"i had to cancel ONCE this week so i can move it to wednesday"*. Both a moved
    week and a moved cadence arrive as a Wednesday, and until `instead_of` existed there
    was nothing to tell them apart."""

    def setUp(self):
        super().setUp()
        series.upsert(self.conn, {
            "slug": "tutoring", "title": "Tutoring", "cadence": "weekly", "weekday": 1,
            "time": "13:00", "location": "Online",
            "join_url": "https://zoom.example/j/1", "effective_on": "2026-08-11"},
            written_by="cli")

    def test_the_excepted_day_is_not_materialised_again(self):
        events.upsert(self.conn, {
            "title": "Tutoring", "date": "2026-08-12", "time": "12:00",
            "series": "tutoring", "kind": "commitment",
            "instead_of": "2026-08-11"}, written_by="cli")
        db.set_today(date(2026, 8, 11))
        try:
            series.roll_forward(self.conn, slug="tutoring")
            dates = [r["date"] for r in self.conn.execute(
                "SELECT date FROM events WHERE series = 'tutoring' ORDER BY date")]
        finally:
            db.set_today(None)
        self.assertNotIn("2026-08-11", dates)
        self.assertIn("2026-08-12", dates)

    def test_a_projection_carries_the_rules_qualities_and_none_of_its_judgements(self):
        db.set_today(date(2026, 8, 11))
        try:
            series.roll_forward(self.conn, slug="tutoring")
            row = events.get(self.conn, "tutoring@2026-08-11")
        finally:
            db.set_today(None)
        self.assertEqual(row.join_url, "https://zoom.example/j/1")
        self.assertEqual(row.location, "Online")
        self.assertEqual(row.status, "mentioned")   # never inherited

    def test_a_rule_never_invents_a_week_earlier_than_what_a_source_said(self):
        db.set_today(date(2026, 8, 3))          # the Monday before the beat's two Wednesdays
        try:
            series.upsert(self.conn, {
                "slug": "physio", "title": "Physio", "cadence": "weekly", "weekday": 2,
                "time": "17:00", "effective_on": "2026-08-03"}, written_by="dream:day1")
            events.upsert(self.conn, {
                "key": "physio@later", "title": "Physio", "date": "2026-08-12",
                "time": "17:00", "series": "physio", "kind": "commitment"},
                written_by="dream:day1", match=False)
            series.roll_forward(self.conn, slug="physio")
            dates = [r["date"] for r in self.conn.execute(
                "SELECT date FROM events WHERE series = 'physio' ORDER BY date")]
        finally:
            db.set_today(None)
        self.assertEqual(dates, ["2026-08-12"])   # and no 2026-08-05

    def test_a_leftover_from_the_old_cadence_does_not_satisfy_the_slot(self):
        """The mirror of it. A Monday the rule no longer lands on is not "the next one"
        — it is the thing `stale_occurrences` exists to raise — and letting it count
        would mean a schedule change silently produced no Tuesday at all."""
        events.upsert(self.conn, {
            "key": "ical-tutoring@old", "title": "Tutoring", "date": "2026-08-17",
            "time": "10:00", "series": "tutoring", "kind": "commitment"},
            written_by="ical", match=False)
        db.set_today(date(2026, 8, 11))
        try:
            series.roll_forward(self.conn, slug="tutoring")
            dates = [r["date"] for r in self.conn.execute(
                "SELECT date FROM events WHERE series = 'tutoring' ORDER BY date")]
        finally:
            db.set_today(None)
        self.assertIn("2026-08-11", dates)

    def test_only_a_projection_may_be_withdrawn(self):
        """A Monday off Calendar.app is an observation, and no cadence change entitles
        memcal to delete an observation."""
        events.upsert(self.conn, {
            "key": "ical-tutoring@2026-08-17", "title": "Tutoring", "date": "2026-08-17",
            "series": "tutoring", "kind": "commitment"}, written_by="ical", match=False)
        db.set_today(date(2026, 8, 11))
        try:
            series.roll_forward(self.conn, slug="tutoring")
            stale = [r["date"] for r in series.stale_occurrences(self.conn, "tutoring")]
        finally:
            db.set_today(None)
        self.assertIsNotNone(events.get(self.conn, "ical-tutoring@2026-08-17"))
        self.assertIn("2026-08-17", stale)


class TestADeletedCalendarEventKeptAssertingItself(Base):
    """`partiful.reconcile_missing` has existed since the beginning and ran at the end of
    `ingest_snapshot`; the plain-iCal half was never built, so `ical` only ever wrote
    `active = 1`. A calendar named `Schedule` was removed from a real Mac and its
    thirty-one rows sat at `active = 1` through two later scans — memcal could not say
    what had happened and could not say that nothing had."""

    def _item(self, uid, title, when, calendar="Home"):
        return {"calendar_name": calendar, "calendar_uid": f"cal-{calendar}",
                "writable": True, "uid": uid, "title": title,
                "start": f"{when}T14:00:00.000Z", "end": f"{when}T15:00:00.000Z",
                "all_day": False, "location": "", "description": "", "url": "",
                "status": "confirmed", "recurrence": ""}

    def _survivor(self):
        """An event present in every snapshot here, so the later ones are *reads*.

        A snapshot with nothing in it is a failed read and is judged nowhere — see
        `TestOneUnreadableCalendarMadeEveryOtherEventLookDeleted`. Each fixture below
        used to empty the calendar completely to make one event disappear, which is a
        library that returned nothing rather than one entry someone deleted, and after
        the guard landed three of these four would have passed on a feature that had
        been removed."""
        return self._item("SURVIVOR", "Standing thing", self.d(3))

    def test_an_event_that_disappears_stops_being_asserted(self):
        soon = self.d(9)
        ical.ingest_snapshot(self.conn, self.cfg, [self._item("A1", "Dentist", soon)],
                             scan_start="2000-01-01T00:00:00",
                             scan_end="2100-01-01T00:00:00")
        self.conn.execute("UPDATE calendar_items SET last_seen_at = '2020-01-01T00:00:00'")
        ical.ingest_snapshot(self.conn, self.cfg, [self._survivor()],
                             scan_start="2000-01-01T00:00:00",
                             scan_end="2100-01-01T00:00:00")
        active = self.conn.execute(
            "SELECT active FROM calendar_items WHERE event_uid = 'A1'").fetchone()
        self.assertEqual(active["active"], 0)
        self.assertEqual(events.search(self.conn, "Dentist")[0].status, "declined")

    def test_a_past_event_is_not_un_happened_by_being_tidied_away(self):
        """"Did this happen" is answered by the day it was on, not by whether the
        calendar entry survives into September."""
        gone = self.d(-9)
        ical.ingest_snapshot(self.conn, self.cfg, [self._item("B1", "Bloodwork", gone)],
                             scan_start="2000-01-01T00:00:00",
                             scan_end="2100-01-01T00:00:00")
        before = events.search(self.conn, "Bloodwork")[0].status
        self.conn.execute("UPDATE calendar_items SET last_seen_at = '2020-01-01T00:00:00'")
        ical.ingest_snapshot(self.conn, self.cfg, [self._survivor()],
                             scan_start="2000-01-01T00:00:00",
                             scan_end="2100-01-01T00:00:00")
        self.assertEqual(events.search(self.conn, "Bloodwork")[0].status, before)

    def test_absence_from_a_window_nobody_read_is_not_evidence(self):
        soon = self.d(40)
        ical.ingest_snapshot(self.conn, self.cfg, [self._item("C1", "Recital", soon)],
                             scan_start="2000-01-01T00:00:00",
                             scan_end="2100-01-01T00:00:00")
        self.conn.execute("UPDATE calendar_items SET last_seen_at = '2020-01-01T00:00:00'")
        ical.ingest_snapshot(self.conn, self.cfg, [self._survivor()],
                             scan_start=f"{self.d(0)}T00:00:00",
                             scan_end=f"{self.d(7)}T00:00:00")
        self.assertNotEqual(events.search(self.conn, "Recital")[0].status, "declined")

    def test_a_series_member_disappearing_is_asked_about_not_concluded(self):
        """*"if i delete tutoring, perhaps a todo would be nice, where agent asks, did
        you stop going to tutoring?"* — skipping one week and having stopped want
        opposite things done about the rule, so memcal does not pick one."""
        soon = self.d(9)
        ical.ingest_snapshot(self.conn, self.cfg, [self._item("D1", "Tutoring", soon)],
                             scan_start="2000-01-01T00:00:00",
                             scan_end="2100-01-01T00:00:00")
        row = events.search(self.conn, "Tutoring")[0]
        self.conn.execute("UPDATE events SET series = 'tutoring' WHERE id = ?", (row.id,))
        series.upsert(self.conn, {
            "slug": "tutoring", "title": "Tutoring", "cadence": "weekly", "weekday": 1,
            "effective_on": self.d(-30)}, written_by="cli")
        self.conn.execute("UPDATE calendar_items SET last_seen_at = '2020-01-01T00:00:00'")
        ical.ingest_snapshot(self.conn, self.cfg, [self._survivor()],
                             scan_start="2000-01-01T00:00:00",
                             scan_end="2100-01-01T00:00:00")
        asked = self.conn.execute(
            "SELECT text FROM questions WHERE key = 'q:series-gone:tutoring'").fetchone()
        self.assertIsNotNone(asked)
        self.assertIn("stopped going", asked["text"])
        self.assertEqual(series.get(self.conn, "tutoring").status, "active")


class TestTheWebPortWasTakenAndSaidNothingUseful(Base):
    """Reported as "memcal web doesnt work also". It worked; port 8765 was held by an
    unrelated `wsserver.py` that had been running since the previous day. The raw
    `address already in use` is a true sentence that leaves you with nothing to do, and
    from the message alone that is indistinguishable from memcal being broken."""

    def test_a_taken_port_names_what_has_it_and_what_to_type(self):
        port = 8765
        with mock.patch.object(
            web_server, "ThreadingHTTPServer",
            side_effect=OSError(errno.EADDRINUSE, "address already in use"),
        ), mock.patch.object(web_server, "_who_has", return_value="python (pid 42)"):
            with self.assertRaises(web.WebError) as caught:
                web.serve(self.cfg, port=port, open_browser=False)
        said = str(caught.exception)
        self.assertIn(str(port), said)
        self.assertIn(f"--port {port + 1}", said)

    def test_a_different_failure_is_still_raised(self):
        """Only the taken-port case is translated. Swallowing every OSError here would
        turn a real bind failure into advice about ports."""
        with mock.patch.object(
            web_server, "ThreadingHTTPServer",
            side_effect=OSError(errno.EINVAL, "bad address"),
        ), self.assertRaises(OSError):
            web.serve(self.cfg, host="127.0.0.1", port=9, open_browser=False)


class TestTheCalendarKnewTheCadenceAndNobodyRead(Base):
    """The JXA has read `recurrence` since it was written and nothing ever looked at it —
    the same shape as the previously ignored `description` and `url`. The one place in the
    whole system that *knew* tutoring was every Monday at ten was Calendar.app, and the
    knowledge stopped at the connector."""

    def test_a_weekly_rrule_is_read_in_memcals_own_vocabulary(self):
        self.assertEqual(ical.recurrence_rule("FREQ=WEEKLY;INTERVAL=1;BYDAY=TU"),
                         {"cadence": "weekly", "weekday": 1})
        self.assertEqual(ical.recurrence_rule("RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=TH"),
                         {"cadence": "fortnightly", "weekday": 3})
        self.assertEqual(ical.recurrence_rule("FREQ=MONTHLY;BYMONTHDAY=15"),
                         {"cadence": "monthly", "day_of_month": 15})

    def test_what_the_vocabulary_cannot_say_is_refused_not_approximated(self):
        """A schedule memcal projects wrongly is worse than one it declines to project.
        Twice a week is not "Tuesdays", and a yearly rule is not a cadence here."""
        self.assertIsNone(ical.recurrence_rule("FREQ=WEEKLY;BYDAY=MO,WE"))
        self.assertIsNone(ical.recurrence_rule("FREQ=YEARLY"))
        self.assertIsNone(ical.recurrence_rule("FREQ=WEEKLY;INTERVAL=3;BYDAY=MO"))
        self.assertIsNone(ical.recurrence_rule(""))

    def test_an_edit_on_his_calendar_moves_the_rule(self):
        series.upsert(self.conn, {
            "slug": "tutoring", "title": "Tutoring", "cadence": "weekly", "weekday": 0,
            "time": "10:00", "effective_on": self.d(-30)}, written_by="cli")
        item = {"calendar_name": "Home", "calendar_uid": "cal-home", "writable": True,
                "uid": "R1", "title": "Tutoring", "start": f"{self.d(2)}T17:00:00.000Z",
                "end": f"{self.d(2)}T18:00:00.000Z", "all_day": False, "location": "",
                "description": "", "url": "", "status": "confirmed",
                "recurrence": "FREQ=WEEKLY;INTERVAL=1;BYDAY=TU"}
        ical.ingest_snapshot(self.conn, self.cfg, [item],
                             scan_start="2000-01-01T00:00:00",
                             scan_end="2100-01-01T00:00:00")
        row = events.search(self.conn, "Tutoring")[0]
        self.conn.execute("UPDATE events SET series = 'tutoring' WHERE id = ?", (row.id,))
        ical.learn_cadence(self.conn, item, events.get_by_id(self.conn, row.id))
        self.assertEqual(series.get(self.conn, "tutoring").weekday, 1)

    def test_a_recurring_event_does_not_open_a_series_of_its_own(self):
        """A `series` row per recurring calendar entry would open a schedule for every
        gym block and standup in the library — the fifty-eight-wiki-pages mistake."""
        item = {"calendar_name": "Home", "calendar_uid": "cal-home", "writable": True,
                "uid": "R2", "title": "Deep block", "start": f"{self.d(2)}T13:00:00.000Z",
                "end": f"{self.d(2)}T14:00:00.000Z", "all_day": False, "location": "",
                "description": "", "url": "", "status": "confirmed",
                "recurrence": "FREQ=WEEKLY;BYDAY=MO"}
        ical.ingest_snapshot(self.conn, self.cfg, [item],
                             scan_start="2000-01-01T00:00:00",
                             scan_end="2100-01-01T00:00:00")
        self.assertIsNone(series.get(self.conn, "deep-block"))


class TestAStandingAppointmentReachedTheCalendarAsOneDatedCopy(Base):
    """*"a series continuing forever still at tuesday at 1"* — on the phone, not only in
    memcal. `publish_pending` writes the next occurrence and nothing else, so a standing
    appointment arrived as a single event that quietly moved each week: delete it and the
    whole series is gone, which is what "now the series is gone from my cal" looked like
    from the outside. There was no alternative through the scripting interface, which
    exposes `recurrence` as a string it will not let you assign. The available call has
    only one destination."""

    def _runner(self, replies):
        calls = []

        def run(cmd, **kwargs):
            calls.append(cmd)
            body = replies.pop(0) if replies else "{}"
            return type("Done", (), {"returncode": 0, "stdout": body, "stderr": ""})()

        return run, calls

    def _rule(self, **over):
        fields = {"slug": "tutoring", "title": "Tutoring", "cadence": "weekly",
                  "weekday": 1, "time": "13:00", "effective_on": self.d(0),
                  "join_url": "https://zoom.example/j/1"}
        fields.update(over)
        rule, _ = series.upsert(self.conn, fields, written_by="cli")
        return rule

    def test_the_scripting_interface_could_never_have_done_this(self):
        self.assertNotIn("recurrenceRules", ical.PUBLISH_JXA)
        self.assertIn("EKRecurrenceRule", ical.ACCOUNT_JXA)
        self.assertIn("EKSpanFutureEvents", ical.ACCOUNT_JXA)

    def test_a_rule_is_published_as_a_repeating_event(self):
        self.cfg.publish_calendar = "memcal"
        rule = self._rule()
        run, calls = self._runner(
            ['{"uid": "s-1", "calendar": "memcal", "created": true, "status": 3}'])
        written = ical.publish_series(self.conn, self.cfg, rule, runner=run)
        self.assertEqual(written["uid"], "s-1")
        spec = json.loads(calls[0][-1])
        self.assertEqual((spec["cadence"], spec["weekday"], spec["title"]),
                         ("weekly", 1, "Tutoring"))

    def test_no_objc_selector_is_wrapped_across_lines(self):
        """A JXA selector is one identifier, and wrapping it looks like ordinary code.

        `initRecurrenceWithFrequencyIntervalDaysOfTheWeek…SetPositionsEnd` is 108
        characters; broken over two lines it becomes property access on an undefined
        value, and the first real publish died with "undefined is not an object" — after
        the tests passed, because every one of them stubs `osascript`. Nothing below the
        runner boundary is exercised by anything except running it, so this is the one
        check that can see the shape of the mistake."""
        self.assertTrue(ical.ACCOUNT_JXA and ical.PUBLISH_JXA and ical.JXA,
                        "empty scripts pass every line check there is")
        for name, source in (("ACCOUNT_JXA", ical.ACCOUNT_JXA),
                             ("PUBLISH_JXA", ical.PUBLISH_JXA),
                             ("JXA", ical.JXA)):
            for number, line in enumerate(source.splitlines(), 1):
                stripped = line.strip()
                self.assertFalse(
                    stripped.startswith(".") and stripped[1:2].isupper(),
                    f"{name}:{number} continues a selector on a new line: {stripped[:60]}")

    def test_the_repeat_does_not_anchor_on_a_week_that_was_moved(self):
        """The rule has been in force since Tuesday and that Tuesday did not happen — it
        moved to Wednesday, which is published separately as the exception. Anchoring on
        `effective_on` would have shown them both, one of them a meeting nobody attended,
        on a real calendar on their phone."""
        self.cfg.publish_calendar = "memcal"
        db.set_today(date(2026, 8, 11))          # the Tuesday the rule takes effect
        try:
            rule = self._rule(effective_on="2026-08-11")
            events.upsert(self.conn, {
                "title": "Tutoring", "date": "2026-08-12", "time": "12:00",
                "series": "tutoring", "kind": "commitment", "status": "confirmed",
                "instead_of": "2026-08-11"}, written_by="live")
            run, calls = self._runner(
                ['{"uid": "s-1", "calendar": "memcal", "created": true, "status": 3}'])
            ical.publish_series(self.conn, self.cfg, rule, runner=run)
        finally:
            db.set_today(None)
        started = json.loads(calls[0][-1])["start"][:10]
        self.assertNotEqual(started, "2026-08-11")   # the week that moved
        self.assertEqual(started, "2026-08-18")      # the next one that did not

    def test_the_timestamps_carry_a_timezone(self):
        """EventKit's `NSISO8601DateFormatter` defaults to `withInternetDateTime` and
        returns **nil** for a string with no offset, while `PUBLISH_JXA`'s JavaScript
        `new Date()` reads a naive one happily. Two interfaces, two parsers, one strict —
        so the first real publish came back "could not read the start or end of the
        series" and the repeating event could never have been created."""
        self.cfg.publish_calendar = "memcal"
        rule = self._rule()
        run, calls = self._runner(
            ['{"uid": "s-1", "calendar": "memcal", "created": true, "status": 3}'])
        ical.publish_series(self.conn, self.cfg, rule, runner=run)
        spec = json.loads(calls[0][-1])
        for field in ("start", "end"):
            stamp = datetime.fromisoformat(spec[field])
            self.assertIsNotNone(stamp.tzinfo, f"{field}={spec[field]!r} has no offset")

    def test_an_unchanged_schedule_costs_no_call(self):
        self.cfg.publish_calendar = "memcal"
        self._rule()
        run, calls = self._runner(
            ['{"uid": "s-1", "calendar": "memcal", "created": true, "status": 3}'])
        ical.publish_schedules(self.conn, self.cfg, runner=run)
        before = len(calls)
        ical.publish_schedules(self.conn, self.cfg, runner=run)
        self.assertEqual(len(calls), before)

    def test_a_moved_schedule_updates_the_same_repeating_event(self):
        self.cfg.publish_calendar = "memcal"
        self._rule()
        run, calls = self._runner([
            '{"uid": "s-1", "calendar": "memcal", "created": true, "status": 3}',
            '{"uid": "s-1", "calendar": "memcal", "created": false, "status": 3}',
        ])
        ical.publish_schedules(self.conn, self.cfg, runner=run)
        self._rule(weekday=3, effective_on=self.d(7))
        ical.publish_schedules(self.conn, self.cfg, runner=run)
        self.assertEqual(json.loads(calls[-1][-1])["uid"], "s-1")
        rows = self.conn.execute(
            "SELECT count(*) AS n FROM calendar_items WHERE event_key = 'series:tutoring'"
        ).fetchone()
        self.assertEqual(rows["n"], 1)

    def test_memcals_own_repeating_event_is_not_read_back_fifty_times(self):
        """A recurring event arrives once per occurrence under one uid. Unguarded, one
        published schedule becomes fifty rows and fifty pieces of evidence that the
        schedule exists — every scan, forever."""
        self.cfg.publish_calendar = "memcal"
        rule = self._rule()
        run, _ = self._runner(
            ['{"uid": "s-1", "calendar": "memcal", "created": true, "status": 3}'])
        ical.publish_series(self.conn, self.cfg, rule, runner=run)
        occurrences = [{
            "calendar_name": "memcal", "calendar_uid": "cal-1", "writable": True,
            "uid": "s-1", "title": "Tutoring",
            "start": f"2026-08-{day:02d}T17:00:00.000Z",
            "end": f"2026-08-{day:02d}T18:00:00.000Z", "all_day": False,
            "location": "", "description": "Added by memcal.", "url": "",
            "status": "confirmed", "recurrence": "FREQ=WEEKLY",
        } for day in (11, 18, 25)]
        ical.ingest_snapshot(self.conn, self.cfg, occurrences,
                             scan_start="2000-01-01T00:00:00",
                             scan_end="2100-01-01T00:00:00")
        self.assertEqual(events.search(self.conn, "Tutoring"), [])

    def test_a_rule_on_the_calendar_stops_its_rows_being_published_too(self):
        """Otherwise the user sees two tutoring appointments on one Tuesday, both real, both
        memcal's — one from the rule and one from the row it generated."""
        self.cfg.publish_calendar = "memcal"
        rule = self._rule()
        run, calls = self._runner(
            ['{"uid": "s-1", "calendar": "memcal", "created": true, "status": 3}'])
        ical.publish_series(self.conn, self.cfg, rule, runner=run)
        events.upsert(self.conn, {
            "title": "Tutoring", "date": self.d(1), "series": "tutoring",
            "kind": "commitment", "status": "confirmed"}, written_by="live")
        before = len(calls)
        ical.publish_pending(self.conn, self.cfg, runner=run)
        self.assertEqual(len(calls), before)

    def test_the_week_that_contradicts_the_rule_is_still_published(self):
        """The exception is by definition the one the repeating event gets wrong."""
        self.cfg.publish_calendar = "memcal"
        rule = self._rule()
        run, calls = self._runner([
            '{"uid": "s-1", "calendar": "memcal", "created": true, "status": 3}',
            '{"uid": "e-1", "calendar": "memcal", "calendar_uid": "cal-1"}',
        ])
        ical.publish_series(self.conn, self.cfg, rule, runner=run)
        events.upsert(self.conn, {
            "title": "Tutoring", "date": self.d(2), "time": "12:00", "series": "tutoring",
            "kind": "commitment", "status": "confirmed",
            "instead_of": self.d(1)}, written_by="live")
        before = len(calls)
        ical.publish_pending(self.conn, self.cfg, runner=run)
        self.assertGreater(len(calls), before)

    def test_ending_a_series_takes_the_repeating_event_back(self):
        self.cfg.publish_calendar = "memcal"
        self._rule()
        run, calls = self._runner([
            '{"uid": "s-1", "calendar": "memcal", "created": true, "status": 3}',
            '{"removed": true, "status": 3}',
        ])
        ical.publish_schedules(self.conn, self.cfg, runner=run)
        series.end(self.conn, "tutoring", written_by="live")
        log = ical.publish_schedules(self.conn, self.cfg, runner=run)
        self.assertIn("unrepeat", calls[-1])
        self.assertTrue(any("removed" in line for line in log))

    def test_publishing_stays_off_unless_a_calendar_is_named(self):
        """Invariant 11. Nothing that writes outside this process defaults to on, and a
        repeating event is the most durable thing memcal can put out there."""
        self.cfg.publish_calendar = ""
        self._rule()
        run, calls = self._runner([])
        self.assertEqual(ical.publish_schedules(self.conn, self.cfg, runner=run), [])
        self.assertEqual(calls, [])


class TestAWithheldLocationWasReadAsAnRSVP(Base):
    """Treat a withheld Partiful location as unknown RSVP state."""

    def item(self, uid: str, title: str, days: int, *, location: str,
             description: str = "") -> dict:
        start = datetime.combine(db.today() + timedelta(days=days),
                                 datetime.min.time(), tzinfo=timezone.utc).replace(hour=19)
        return {
            "calendar_name": "Partiful", "calendar_uid": "cal-partiful",
            "writable": False, "uid": uid, "title": title,
            "start": start.isoformat(),
            "end": (start + timedelta(hours=2)).isoformat(), "all_day": False,
            "location": location, "description": description,
            "url": f"https://partiful.com/e/{uid}",
        }

    def snapshot(self, items):
        return ical.ingest_snapshot(
            self.conn, self.cfg, items,
            scan_start=(db.today() - timedelta(days=120)).isoformat(),
            scan_end=(db.today() + timedelta(days=365)).isoformat())

    def one(self, title: str = "Stage Reading"):
        rows = [r for r in events.window(self.conn, 0, 30) if title in r.title]
        self.assertEqual(len(rows), 1, f"expected one {title!r} row, got {len(rows)}")
        return rows[0]

    def test_the_placeholder_is_not_an_rsvp(self):
        self.snapshot([self.item("aldon", "Stage Reading", 5,
                                 location="Location available once RSVP'd")])
        row = self.one()
        self.assertEqual(row.kind, "opportunity")
        self.assertEqual(row.status, "mentioned")
        self.assertEqual(row.plain_state(), "not replied")

    def test_the_placeholder_is_not_a_location(self):
        """A status message in the venue field is the second bug underneath the first."""
        self.snapshot([self.item("aldon", "Stage Reading", 5,
                                 location="Location available once RSVP'd")])
        self.assertFalse(self.one().location or "")

    def test_a_disclosed_location_still_confirms(self):
        """The decoy. The rule was right; only its test for "no location" was wrong."""
        self.snapshot([self.item("party", "Rooftop party", 5,
                                 location="123 Orchard St")])
        row = self.one("Rooftop")
        self.assertEqual(row.status, "confirmed")
        self.assertEqual(row.location, "123 Orchard St")

    def test_the_invitations_own_words_are_not_overwritten(self):
        """`note` held the string "Partiful RSVP yes" on 17 of 18 live rows.

        `_normalized` lifts the calendar description into `note`, and the RSVP
        inference then replaced it — so what the invitation said about itself was
        destroyed at ingest, and not recoverable from the archive either, because the
        archived line is built from `note` after the overwrite.
        """
        self.snapshot([self.item("aldon", "Stage Reading", 5,
                                 location="Location available once RSVP'd",
                                 description="Doors 6:30. Ask for Nadia at the desk.")])
        self.assertIn("Nadia", self.one().note or "")

    def test_a_withheld_location_is_never_a_decline(self):
        """The user attended their own ceremony without ever tapping the button.

        A withheld location is evidence about the *feed*, not about them. Only a
        disappearance — an observation — may decline a row.
        """
        self.snapshot([self.item("aldon", "Stage Reading", 5,
                                 location="Location available once RSVP'd")])
        self.assertNotEqual(self.one().status, "declined")

    def test_the_archived_line_does_not_claim_an_rsvp(self):
        """The archive is the one store that is never rewritten.

        `_text` appended "Partiful RSVP yes" whenever the location was non-empty, so a
        model reading the raw source was told the same falsehood as the row.
        """
        self.snapshot([self.item("aldon", "Stage Reading", 5,
                                 location="Location available once RSVP'd")])
        lines = [r["text"] for r in self.conn.execute(
            "SELECT text FROM archive WHERE text LIKE '%Stage Reading%'")]
        self.assertTrue(lines)
        for line in lines:
            self.assertNotIn("RSVP yes", line)
            self.assertNotIn("Location available once", line)

    def test_a_rescan_does_not_re_derive_it(self):
        """A rename changes every revision and re-derives the snapshot."""
        db.set_today("2026-08-01")
        self.snapshot([self.item("aldon", "Stage Reading", 5,
                                 location="Location available once RSVP'd")])
        db.set_today("2026-08-02")
        renamed = self.item("aldon", "Stage Reading", 4,
                            location="Location available once RSVP'd")
        renamed["calendar_name"] = "Partiful Invites"
        self.snapshot([renamed])
        row = self.one()
        self.assertEqual(row.plain_state(), "not replied")
        self.assertFalse(row.location or "")
        db.set_today(None)


class TestTheBriefLineWasTheOnlyWayIn(Base):
    """"we should enpower the LLM to dig into something."

    `web._event_detail` assembled participants, wiki links, series, full field history
    and per-write provenance for the browser panel, and an agent holding `〔E119〕` could
    reach none of it. The brief line was therefore the whole of what a model knew, which
    is why every field anybody ever wanted ended up crammed onto it — the line this
    report is about carried a state, an invite link, a placeholder venue and a note, and
    two of them contradicted each other.
    """

    def _event(self, **kw):
        fields = {"title": "Mount Aldon Stage Reading", "date": db.today().isoformat(),
                  "time": "19:00", "kind": "opportunity", "status": "mentioned",
                  "rsvp_url": "https://partiful.com/e/aldon",
                  "note": "Doors 6:30. Ask for Nadia at the desk.",
                  "source": "ical:subscribed:Partiful"}
        fields.update(kw)
        event, _ = events.upsert(self.conn, fields, written_by="ical")
        return event

    def test_a_handle_opens_the_row_it_names(self):
        event = self._event()
        out = detail.open_handle(self.conn, self.cfg, f"E{event.id}")
        self.assertIn("Mount Aldon", out)
        self.assertIn("You haven't decided whether to go", out)

    def test_it_carries_what_the_line_does_not(self):
        """The whole point: the fields the index is not going to spend tokens on."""
        event = self._event(location="Somewhere Real", join_url="https://meet.example/x")
        out = detail.open_handle(self.conn, self.cfg, f"E{event.id}")
        for expected in ("Somewhere Real", "https://meet.example/x",
                         "https://partiful.com/e/aldon", "Nadia"):
            self.assertIn(expected, out)

    def test_the_brackets_the_brief_prints_are_accepted(self):
        """`〔E12〕` is what a caller has in hand, so refusing it is our schema leaking."""
        event = self._event()
        for spelling in (f"〔E{event.id}〕", f"e{event.id}", f" E{event.id} "):
            self.assertIn("Mount Aldon",
                          detail.open_handle(self.conn, self.cfg, spelling))

    def test_a_todo_and_a_question_open_too(self):
        """"apply this to also open things, todo stuff"."""
        event = self._event()
        todo, _ = todos.open_todo(self.conn, "Reply to the stage reading invite",
                                  event_id=event.id, written_by="cli")
        asked = todos.ask(self.conn, "Are you going to the stage reading?",
                          about_event=event.id, written_by="cli")
        self.assertTrue(asked, "the question gate refused the fixture question")
        question = self.conn.execute(
            "SELECT id FROM questions WHERE key = ?", (asked,)).fetchone()
        self.assertIn("Reply to the stage reading",
                      detail.open_handle(self.conn, self.cfg, f"T{todo.id}"))
        out = detail.open_handle(self.conn, self.cfg, f"Q{question['id']}")
        self.assertIn("Are you going", out)
        self.assertIn(f"E{event.id}", out, "a question must name the row it is about")

    def test_a_bad_handle_says_what_a_good_one_looks_like(self):
        out = detail.open_handle(self.conn, self.cfg, "the stage reading thing")
        self.assertIn("E258", out, "an error that does not show the shape teaches nothing")

    def test_a_missing_row_is_not_an_exception(self):
        self.assertIn("no row", detail.open_handle(self.conn, self.cfg, "E99999").lower())

    def test_the_panel_and_the_agent_read_one_assembler(self):
        """Two assemblers would be two answers to one question."""
        event = self._event(location="Somewhere Real")
        self.assertEqual(web._event_detail(self.conn, self.cfg, event.key),
                         detail.event_record(self.conn, self.cfg, event.key))

    def test_the_panel_payload_carries_the_links(self):
        """`_event_summary` never had them, so the browser could not show a join link."""
        event = self._event(join_url="https://meet.example/x")
        record = web._event_detail(self.conn, self.cfg, event.key)
        self.assertEqual(record["event"]["join_url"], "https://meet.example/x")
        self.assertEqual(record["event"]["rsvp_url"], "https://partiful.com/e/aldon")


class TestTheWeekdayIsNotTheModelsToWorkOut(Base):
    """Derive weekdays from stored dates instead of model prose."""

    def _todo_with_evidence(self):
        aid = archive.append(
            self.conn, stream="imessage", external_id="tay-1",
            ts="2026-08-10T14:52:00-04:00",
            text="Would you be able to help me bring my studio equipment back Wednesday?",
            thread="+15550001111", handle="+15550001111", person="Morgan",
            from_me=False, gated=True)
        key = todos.open_todo(self.conn, "Help Morgan bring back her studio equipment",
                              written_by="dream:test")[0].key
        trace.stamp(self.conn, kind="todo", ref=key, verb="opened",
                    entity="person:Morgan", archive_ids=[aid])
        self.conn.commit()
        return key

    def test_the_citation_stamps_name_their_weekday(self):
        key = self._todo_with_evidence()
        cited = trace.citations(self.conn, "todo", key)
        self.assertEqual(cited["first"], "2026-08-10T14:52")   # unchanged, for machines
        self.assertIn("Mon", cited["first_said"])
        self.assertIn("Mon", cited["last_said"])

    def test_the_lines_a_model_reads_say_which_day_they_were_said(self):
        key = self._todo_with_evidence()
        opened = detail.open_handle(self.conn, self.cfg, f"T{todos.get(self.conn, key).id}")
        self.assertIn("Mon 10 Aug", opened)

    def test_a_stamp_it_cannot_read_is_passed_through_rather_than_invented(self):
        # The contract of the module this borrows from: never guess. A malformed stamp
        # gets shown as-is, not rendered as some plausible Monday.
        self.assertEqual(dates.said_on("not-a-timestamp"), "not-a-timestamp")
        self.assertEqual(dates.said_on(""), "")

    def test_it_agrees_with_the_function_that_already_knew(self):
        self.assertTrue(dates.said_on("2026-08-10T14:52").startswith("Mon"))
        self.assertEqual(dates.weekday_of("2026-08-10"), "monday")


class TestTheBriefLineSaidFourThingsAtOnce(Base):

    def _row(self, **kw):
        fields = {"title": "Mount Aldon Stage Reading | Partiful",
                  "date": db.today().isoformat(), "time": "19:00",
                  "kind": "opportunity", "status": "mentioned",
                  "location": "27 Alder St", "note": "Doors 6:30, ask for Nadia.",
                  "rsvp_url": "https://partiful.com/e/aldon"}
        fields.update(kw)
        event, _ = events.upsert(self.conn, fields, written_by="ical")
        return event

    def test_description_leaves_the_line_and_the_acts_stay(self):
        line = self._row().one_line(overview=True)
        self.assertNotIn("27 Alder St", line)
        self.assertNotIn("Nadia", line)
        self.assertIn("not replied", line)
        self.assertIn("partiful.com/e/aldon", line)

    def test_everything_that_is_not_the_brief_still_says_it_all(self):
        """A write receipt that hides what it wrote is a worse bug than a long line."""
        line = self._row().one_line()
        self.assertIn("27 Alder St", line)
        self.assertIn("Nadia", line)

    def test_the_title_is_quoted_so_its_boundary_is_findable(self):
        """The title contains a dash; so does the line's own separator."""
        line = self._row(title="Mount Aldon -Stage Reading").one_line(overview=True)
        self.assertIn('"Mount Aldon -Stage Reading"', line)

    def test_the_platform_leaves_the_name_and_becomes_a_clause(self):
        line = self._row().one_line(overview=True)
        self.assertIn('"Mount Aldon Stage Reading"', line)
        self.assertIn("via Partiful", line)

    def test_a_platform_word_inside_a_real_title_is_left_alone(self):
        """The decoy. `split_platform` strips an export tag, not any occurrence."""
        self.assertEqual(events.split_platform("Partiful Reunion"),
                         ("Partiful Reunion", ""))
        self.assertEqual(events.split_platform("Dinner | Partiful Reunion"),
                         ("Dinner | Partiful Reunion", ""))
        self.assertEqual(events.split_platform("Jack's 30th | Partiful"),
                         ("Jack's 30th", "Partiful"))

    def test_the_handle_leads_the_line(self):
        event = self._row()
        line = [l for l in brief.render(self.conn, self.cfg).splitlines()
                if "Mount Aldon" in l][0]
        self.assertTrue(line.startswith(f"〔E{event.id}〕"),
                        f"handle is not first: {line!r}")

    def test_the_legend_carries_no_openable_handle(self):
        """`〔E12〕` as an example read as a real row to everything that scans this file.

        `SOURCE_RE` cannot tell an illustration from a citation, so the legend has to
        avoid the shape entirely rather than rely on anybody noticing.
        """
        self.assertFalse(brief.SOURCE_RE.findall(brief.LEGEND))

    def test_every_handle_in_a_rendered_brief_opens(self):
        self._row()
        text = brief.render(self.conn, self.cfg)
        handles = brief.SOURCE_RE.findall(text)
        self.assertTrue(handles, "a brief with no handles is an index of nothing")
        for token in handles:
            opened = detail.open_handle(self.conn, self.cfg, token)
            self.assertNotIn("no row", opened.lower(), f"{token} is a dead end")


class TestAFieldCouldBeSetByAnythingAndEmptiedByNothing(Base):

    def _row(self, **kw):
        fields = {"title": "Dinner", "date": "2026-08-20", "location": "Somewhere Wrong",
                  "note": "an old note", "time": "19:00"}
        fields.update(kw)
        event, _ = events.upsert(self.conn, fields, written_by="cli")
        return event

    def test_a_wrong_location_can_be_removed(self):
        self._row()
        updated, changed = live.update_event(self.conn, self.cfg, "Dinner", location="")
        self.assertIsNone(updated.location)
        self.assertTrue(changed, "a removal that reports no change reads as a no-op")

    def test_removal_is_recorded_like_any_other_correction(self):
        """A correction nothing can see is the failure this exists to fix."""
        event = self._row()
        live.update_event(self.conn, self.cfg, "Dinner", location="")
        moved = [(r["field"], r["old_value"], r["new_value"]) for r in
                 self.conn.execute("SELECT * FROM event_history WHERE event_id = ?",
                                   (event.id,))]
        self.assertIn(("location", "Somewhere Wrong", ""), moved)

    def test_an_omitted_field_is_still_left_alone(self):
        """The decoy, and the reason `""` cannot simply be made to mean "clear".

        A partial diff omits far more than it states. If absence meant deletion, every
        model write would blank every column it did not mention.
        """
        self._row()
        events.upsert(self.conn, {"title": "Dinner", "date": "2026-08-20",
                                  "status": "confirmed"}, written_by="dream")
        after = events.search(self.conn, "Dinner")[0]
        self.assertEqual(after.location, "Somewhere Wrong")
        self.assertEqual(after.note, "an old note")

    def test_the_nightly_pass_cannot_empty_anything(self):
        """`clear` is reachable from the typed human path and from nowhere else."""
        event = self._row()
        events.upsert(self.conn, {"key": event.key, "title": "Dinner",
                                  "date": "2026-08-20", "location": ""},
                      written_by="dream")
        self.assertEqual(events.get(self.conn, event.key).location, "Somewhere Wrong")

    def test_a_field_whose_absence_would_break_the_row_is_refused(self):
        self._row()
        with self.assertRaises(live.LiveError) as caught:
            live.update_event(self.conn, self.cfg, "Dinner", title="")
        self.assertIn("cannot be emptied", str(caught.exception))
        # The refusal has to say what *would* work, or it reads as a dead end.
        self.assertIn("location", str(caught.exception))

    def test_upsert_refuses_to_clear_something_it_should_not(self):
        event = self._row()
        with self.assertRaises(ValueError):
            events.upsert(self.conn, {"key": event.key, "title": "Dinner",
                                      "date": "2026-08-20"},
                          written_by="cli", clear=("title",))

    def test_setting_and_clearing_at_once_sets(self):
        """A caller contradicting itself gets the outcome that keeps information."""
        event = self._row()
        updated, _ = events.upsert(
            self.conn, {"key": event.key, "title": "Dinner", "date": "2026-08-20",
                        "location": "Right Place"},
            written_by="cli", clear=("location",))
        self.assertEqual(updated.location, "Right Place")

    def test_clearing_what_is_already_empty_changes_nothing(self):
        event = self._row(location=None)
        events.upsert(self.conn, {"key": event.key, "title": "Dinner",
                                  "date": "2026-08-20"},
                      written_by="cli", clear=("location",))
        self.assertEqual(
            self.conn.execute("SELECT count(*) AS n FROM event_history WHERE event_id = ?",
                              (event.id,)).fetchone()["n"], 0)


class TestEventDetailUsesPlainEnglishState(Base):
    def test_a_confirmed_commitment_does_not_expose_schema_terms(self):
        event, _ = events.upsert(
            self.conn, {"title": "Tattoo session", "date": self.d(2),
                        "kind": "commitment", "status": "confirmed", "subject": "me"})

        opened = detail.open_handle(self.conn, self.cfg, f"E{event.id}")

        self.assertIn("state: You're going", opened)
        self.assertNotIn("kind commitment", opened)
        self.assertNotIn("status confirmed", opened)


class TestSourceTimelineMarksConversationAndDateShifts(Base):
    def test_direct_messages_and_a_later_hermes_chat_have_separate_headings(self):
        event, _ = events.upsert(
            self.conn, {"title": "Tattoo session", "date": "2026-08-24"})
        ids = [
            archive.append(
                self.conn, stream="imessage", external_id="tattoo-direct",
                ts="2026-08-02T10:00:00-04:00", thread="+15551234567",
                text="Let's do August 24", person="Rae", gated=True),
            archive.append(
                self.conn, stream="agent", external_id="tattoo-hermes",
                ts="2026-08-26T20:00:00-04:00", thread="hermes:session-1",
                text="The tattoo session is confirmed", person="me", from_me=True,
                gated=True),
        ]
        trace.stamp(self.conn, kind="event", ref=event.key, verb="updated",
                    entity="person:Rae", archive_ids=ids)

        rows = trace.source_rows(self.conn, "event", event.key, context=0)

        self.assertIn("iMessage", rows[0]["source_heading"])
        self.assertIn("Sun Aug 2, 2026 10:00", rows[0]["source_heading"])
        self.assertIn("Hermes chat", rows[1]["source_heading"])
        self.assertIn("Wed Aug 26, 2026 20:00", rows[1]["source_heading"])
        self.assertIn("24 days later", rows[1]["source_heading"])


class TestACalendarIsNotAConversation(Base):

    def _archived(self, title: str, when: str) -> int:
        return archive.append(
            self.conn, stream="ical", external_id=f"cal:{title}",
            ts=when, thread="Partiful", text=f"{title} — {when[:10]} — calendar: Partiful",
            meta={"calendar": "Partiful"}, gated=False, gate_reason="calendar-structured")

    def test_a_calendar_row_cites_only_itself(self):
        event, _ = events.upsert(self.conn, {"title": "Stage Reading",
                                             "date": "2026-08-16"}, written_by="ical")
        mine = self._archived("Stage Reading", "2026-08-05T11:40:00-04:00")
        self._archived("Someone Else's Party", "2026-08-05T11:41:00-04:00")
        self._archived("A Third Party", "2026-08-05T11:39:00-04:00")
        trace.stamp(self.conn, kind="event", ref=event.key, verb="created",
                    entity="calendar:Partiful", stage="ical", archive_ids=[mine])
        rows = trace.source_rows(self.conn, "event", event.key)
        self.assertEqual([r["id"] for r in rows], [mine])

    def test_a_chat_row_still_gets_its_context(self):
        """The decoy: this is the behaviour that must survive, not be traded away."""
        event, _ = events.upsert(self.conn, {"title": "Poker", "date": "2026-08-16"},
                                 written_by="dream")
        ids = [archive.append(
            self.conn, stream="imessage", external_id=f"m{n}",
            ts=f"2026-08-05T1{n}:00:00-04:00", thread="poker-crew",
            text=f"line {n}", person="Jordan", gated=True, gate_reason="top-tier")
            for n in range(3)]
        trace.stamp(self.conn, kind="event", ref=event.key, verb="created",
                    entity="thread:imessage:poker-crew", stage="dream",
                    archive_ids=[ids[1]])
        rows = trace.source_rows(self.conn, "event", event.key)
        self.assertEqual(len(rows), 3, "a chat citation keeps its neighbours")
        self.assertEqual([r["evidence"] for r in rows], [False, True, False])


class TestARateLimitArrivedAsASuccessAndWasNeverRetried(Base):
    """A 200 is not a success, and the whole capacity system read only the status code."""

    def _client(self, body):
        import io
        import json as jsonlib
        from memcal import llm
        calls = {"n": 0}

        def respond(req, timeout=None):
            calls["n"] += 1
            payload = body(calls["n"]) if callable(body) else body

            class _Resp(io.BytesIO):
                def __enter__(self_inner): return self_inner
                def __exit__(self_inner, *a): return False
            return _Resp(jsonlib.dumps(payload).encode())

        client = llm.OpenRouter("sk-or-test")
        real = llm.urllib.request.urlopen
        llm.urllib.request.urlopen = respond
        self.addCleanup(setattr, llm.urllib.request, "urlopen", real)
        return client, calls, llm

    #: The body observed in `tools/bench_output/temporal/core-model-v1-trial1-*.json`.
    UPSTREAM = {"error": {"message": "openai/gpt-5.6-luna is temporarily rate-limited "
                                     "upstream. Please retry shortly, or add your own key"}}

    def test_the_observed_body_is_waited_out_not_raised(self):
        """Before the fix this made exactly one attempt and raised.

        The assertion is on the *path* rather than on a call count: an in-body limit
        arrives on a 200, so there is no `Retry-After` header to shrink the backoff and
        how many attempts fit in a budget is exponential-backoff arithmetic, not the
        property under test. What matters is that it went down the capacity road at all.
        """
        client, calls, llm = self._client(self.UPSTREAM)
        # The waiting itself is not under test and a real backoff curve would put ~15
        # seconds of sleep in the suite. Neutralise it so the *loop* is exercised.
        real_sleep, llm.time.sleep = llm.time.sleep, lambda _s: None
        self.addCleanup(setattr, llm.time, "sleep", real_sleep)
        with self.assertRaises(llm.LLMError) as caught:
            client._post("/x", {}, capacity_budget=100.0)
        self.assertGreater(calls["n"], 5, "an in-body rate limit was barely retried")
        self.assertIn("capacity waits", str(caught.exception))
        self.assertNotIn("HTTP", str(caught.exception), "this did not arrive as a status")

    def test_it_succeeds_once_the_provider_comes_back(self):
        """The point of retrying: the bundle gets read rather than left queued."""
        answer = {"choices": [{"message": {"content": "ok"}}]}
        client, calls, _llm = self._client(
            lambda n: self.UPSTREAM if n < 3 else answer)
        self.assertEqual(client._post("/x", {}, capacity_budget=30.0), answer)
        self.assertEqual(calls["n"], 3)

    def test_a_usable_completion_is_never_discarded(self):
        """Some providers attach a non-fatal error beside a real answer.

        Treating that as a failure throws away work that was already paid for.
        """
        both = {"choices": [{"message": {"content": "ok"}}], "error": {"message": "warn"}}
        client, calls, _llm = self._client(both)
        self.assertEqual(client._post("/x", {}), both)
        self.assertEqual(calls["n"], 1)

    def test_an_in_body_fault_is_still_not_a_queue(self):
        """Trying harder does not fix a bad request, in a body any more than in a status."""
        client, calls, llm = self._client({"error": {"code": 400, "message": "bad schema"}})
        with self.assertRaises(llm.LLMError):
            client._post("/x", {}, capacity_budget=600.0)
        self.assertEqual(calls["n"], 1, "a 400 in the body must raise at once")

    def test_a_structured_code_outranks_the_wording(self):
        """The word list is a fallback, never the mechanism."""
        from memcal import llm
        self.assertEqual(llm._error_status(
            {"error": {"code": 503, "message": "nothing about limits here"}}), 503)
        self.assertEqual(llm._error_status(
            {"error": {"code": 400, "message": "you are being rate-limited"}}), 400)


class TestTheTransportKnewItsProvidersByName(Base):
    """"how can we decouple these bespoke things from our core code."

    `ical.py` carried six `provider == "partiful"` tests and built the literal string
    "Partiful RSVP yes" into the line it archives, so a second platform meant editing
    the transport and the transport could not be read without reading one company's
    business rules. A policy is a `Policy` in `sources.providers` now, and this asserts
    the seam rather than trusting it to stay clean.
    """

    def test_the_calendar_connector_names_no_provider(self):
        text = Path(ical.__file__).read_text(encoding="utf-8")
        code = "\n".join(line for line in text.splitlines()
                         if not line.lstrip().startswith("#"))
        # Docstrings still name Partiful as the worked example, which is what makes the
        # seam legible; what must not come back is a branch on a provider's name.
        for offender in ('"partiful"', "'partiful'", "Partiful RSVP"):
            self.assertNotIn(offender, code,
                             f"{offender} is back in the transport")

    def test_every_registered_policy_satisfies_the_protocol(self):
        self.assertTrue(providers.REGISTRY, "no provider policies registered")
        for policy in providers.REGISTRY:
            self.assertIsInstance(policy, providers.Policy)
            self.assertTrue(policy.name)
            self.assertIs(providers.by_name(policy.name), policy)

    def test_an_unregistered_provider_resolves_to_nothing(self):
        """A policy removed from the registry must not take its stored rows with it."""
        self.assertIsNone(providers.by_name("eventbrite"))

    def test_a_plain_calendar_row_is_claimed_by_nobody(self):
        self.assertIsNone(providers.claiming(
            {"calendar_name": "Personal", "url": "", "description": ""}))



def setUpModule():
    db.set_today(None)


def tearDownModule():
    db.set_today(None)


if __name__ == "__main__":
    unittest.main()
