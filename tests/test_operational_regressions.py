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
import tomllib
import unittest
from unittest import mock
from datetime import date, datetime, timedelta, timezone, time as dt_time
from pathlib import Path

try:
    from tests._support import Base
except ModuleNotFoundError:  # Direct execution: python3 tests/test_core.py
    from _support import Base

from memcal import archive, brief, cli, dates, db, detail, events, gate, identity, legacy, live, llm, mcp_server, schedule, series, textclean, threads, todos, trace, web_jobs, wiki  # noqa: E402
from memcal.config import Config  # noqa: E402
from memcal.dream import apply as apply_stage  # noqa: E402
from memcal.dream import bundle as bundle_stage  # noqa: E402
from memcal.dream import propose as propose_stage  # noqa: E402
from memcal.dream import sweep as sweep_stage  # noqa: E402
from memcal import sources  # noqa: E402
from memcal.sources import base, groupme, ical, imessage, proton, providers, spec, whatsapp  # noqa: E402

class TestReleaseVersionAgreement(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent.parent
        self.version = tomllib.loads(
            (self.root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        self.changelog = (self.root / "CHANGELOG.md").read_text(encoding="utf-8")

    def test_declared_version_has_a_changelog_release(self):
        self.assertIn(f"## [{self.version}] - ", self.changelog)
        self.assertLess(self.changelog.index("## [Unreleased]"),
                        self.changelog.index(f"## [{self.version}] - "))

    def test_declared_version_matches_the_newest_tag_when_tags_exist(self):
        if not (self.root / ".git").exists():
            self.skipTest("not a git checkout")
        tags = subprocess.run(
            ["git", "tag", "--sort=-version:refname"], cwd=self.root,
            capture_output=True, text=True, check=True).stdout.splitlines()
        if tags:
            self.assertEqual(tags[0], f"v{self.version}")


class TestStandingRetirementStopsNewWrites(Base):
    def _sourced_standing(self):
        archive_id = archive.append(
            self.conn, stream="agent", external_id="standing-retirement", ts=db.now(),
            text="Use curl before browsers", thread="conversation", person="me",
            from_me=True,
        )
        key, _ = todos.set_standing(
            self.conn, "preference", "Use curl before browsers", written_by="dream:test"
        )
        row = self.conn.execute("SELECT * FROM standing WHERE key = ?", (key,)).fetchone()
        trace.stamp(self.conn, kind="standing", ref=key, verb="added",
                    entity="thread:agent:conversation", stage="propose",
                    archive_ids=[archive_id])
        self.conn.commit()
        return key, row

    def test_retirement_preserves_the_old_handle_and_evidence(self):
        key, row = self._sourced_standing()
        redirect, verb = legacy.retire_standing(
            self.conn, key, destination_kind="wiki",
            destination_ref="agent-behavior.browser tool",
        )
        self.assertEqual("retired", verb)
        self.assertEqual([], todos.standing(self.conn))
        self.assertEqual(row["id"], redirect.old_id)

        resolved = trace.resolve_source(self.conn, f"S{row['id']}")
        self.assertEqual(key, resolved["ref"])
        self.assertEqual({"kind": "wiki", "ref": "agent-behavior.browser tool"},
                         resolved["redirect"])
        self.assertTrue(resolved["evidence"])
        opened = detail.open_handle(self.conn, self.cfg, f"S{row['id']}")
        self.assertIn("retired: wiki:agent-behavior.browser tool", opened)
        self.assertIn("Use curl before browsers", opened)

    def test_repeating_a_retirement_is_a_no_op(self):
        key, _row = self._sourced_standing()
        first, _ = legacy.retire_standing(
            self.conn, key, destination_kind="discarded")
        second, verb = legacy.retire_standing(
            self.conn, key, destination_kind="discarded")
        self.assertEqual("unchanged", verb)
        self.assertEqual(first, second)
        self.assertEqual(1, self.conn.execute(
            "SELECT count(*) AS n FROM standing_redirects").fetchone()["n"])

    def test_a_caller_can_roll_back_the_whole_retirement(self):
        key, _row = self._sourced_standing()
        legacy.retire_standing(
            self.conn, key, destination_kind="discarded", commit=False)
        self.conn.rollback()
        self.assertEqual(key, todos.standing(self.conn)[0]["key"])
        self.assertIsNone(legacy.standing_redirect(self.conn, key))

    def test_a_retired_handle_cannot_be_silently_remapped(self):
        key, _row = self._sourced_standing()
        legacy.retire_standing(self.conn, key, destination_kind="discarded")
        with self.assertRaises(ValueError):
            legacy.retire_standing(
                self.conn, key, destination_kind="config",
                destination_ref="hermes.native_polls")

    def test_a_later_legacy_row_cannot_reuse_a_redirected_handle(self):
        key, row = self._sourced_standing()
        legacy.retire_standing(self.conn, key, destination_kind="discarded")
        newer, _ = todos.set_standing(self.conn, "preference", "A later legacy row")
        newer_row = self.conn.execute(
            "SELECT * FROM standing WHERE key = ?", (newer,)).fetchone()
        self.assertGreater(newer_row["id"], row["id"])
        resolved = trace.resolve_source(self.conn, f"S{row['id']}")
        self.assertEqual("Use curl before browsers", resolved["label"])

    def test_destination_shape_is_validated_before_the_row_moves(self):
        key, _row = self._sourced_standing()
        with self.assertRaises(ValueError):
            legacy.retire_standing(self.conn, key, destination_kind="wiki")
        with self.assertRaises(ValueError):
            legacy.retire_standing(
                self.conn, key, destination_kind="discarded", destination_ref="anything")
        self.assertEqual(key, todos.standing(self.conn)[0]["key"])

    def test_proposal_contracts_have_no_standing_field(self):
        for schema in (propose_stage.BUNDLE_DIFF, propose_stage.BUNDLE_DIFF_V2):
            self.assertNotIn("standing", schema["required"])
            self.assertNotIn("standing", schema["properties"])
        self.assertNotIn("standing", propose_stage.EMPTY_DIFF)
        self.assertNotIn("standing", propose_stage.stage_plan_mod.ALL_FIELDS)

    def test_legacy_rows_do_not_enter_the_shared_prompt(self):
        todos.set_standing(self.conn, "identity", "Legacy Name")
        todos.set_standing(self.conn, "alias", "Legacy Alias")
        prefix = propose_stage.build_prefix(self.conn, self.cfg)
        self.assertNotIn("Legacy Name", prefix)
        self.assertNotIn("Legacy Alias", prefix)

    def test_sweep_cannot_see_or_delete_legacy_rows(self):
        key, _ = todos.set_standing(self.conn, "preference", "Use curl before browsers")
        snapshot = sweep_stage.state_snapshot(self.conn, self.cfg, [])
        self.assertNotIn("Use curl before browsers", snapshot)
        self.assertNotIn("drop_standing", sweep_stage.SWEEP_SCHEMA["properties"])

        class LegacyReply:
            def complete(self, **kwargs):
                return llm.Reply(text="{}", data={
                    "drop_events": [], "drop_todos": [], "questions": [],
                    "drop_standing": [{"key": key, "reason": "junk"}],
                }, model=kwargs.get("model", ""))

        _result, actions = sweep_stage.sweep(LegacyReply(), self.conn, self.cfg, [])
        self.assertEqual([], actions)
        self.assertEqual(key, todos.standing(self.conn)[0]["key"])

    def test_the_old_cli_shape_explains_itself_without_writing(self):
        args = argparse.Namespace(home=str(self.cfg.home), kind="preference",
                                  value="Use curl before browsers", permanent=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(1, cli.cmd_standing(args))
        self.assertIn("read-only legacy data", out.getvalue())
        self.assertEqual([], todos.standing(self.conn))

    def test_legacy_rows_remain_listable(self):
        todos.set_standing(self.conn, "preference", "Use curl before browsers")
        args = argparse.Namespace(home=str(self.cfg.home), kind="all",
                                  value=None, permanent=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, cli.cmd_standing(args))
        self.assertIn("Use curl before browsers", out.getvalue())


class TestANudgeNeedsSomebodyOnTheOtherEndOfIt(Base):
    """Schedule automatic nudges only for commitments involving another person."""

    def _dinner(self, offset=3, participants=("Morgan",), kind="commitment"):
        event, _ = events.upsert(self.conn, {
            "title": "Dinner at Bocca di Bacco", "date": self.d(offset), "time": "17:30",
            "kind": kind, "status": "confirmed",
            "participants": list(participants)}, written_by="dream:nightly")
        return event

    def test_an_obligation_somebody_is_waiting_on_times_itself(self):
        """The dinner, exactly: the user never asked to be reminded, and the reservation stops
        being possible before Morgan turns up expecting a table."""
        todo, _ = todos.open_todo(self.conn, "Find a prix-fixe reservation",
                                  event_id=self._dinner().id, written_by="dream:nightly")
        self.assertTrue(todo.remind_at, "somebody is expecting this to have happened")
        self.assertLess(todo.remind_at[:10], self.d(3), "and it lands before the table")

    def test_its_own_deadline_beats_the_events_when_it_has_one(self):
        # A reservation may have to be made days ahead. The deadline is the booking, not
        # the dinner, and the row says so.
        #
        # The clock is pinned because this assertion is only true while there is usable
        # evening left: opened at 22:00 for tomorrow, the honest answer really is 08:00
        # on the due day, and asserting otherwise was a bet that the suite runs before
        # the evening. It was red between 19:00 and midnight and green the other
        # nineteen hours, and `clock_sweep.py` swept days while taking the hour from
        # whenever it happened to run, so nothing could see it.
        db.set_today(f"{db.today().isoformat()}T09:00")
        self.addCleanup(db.set_today, None)
        todo, _ = todos.open_todo(self.conn, "Book the prix-fixe", due=self.d(1),
                                  event_id=self._dinner(offset=6).id,
                                  written_by="dream:nightly")
        self.assertLess(todo.remind_at[:10], self.d(1))

    def test_the_evening_is_usable_up_to_the_hour_it_ends(self):
        """`WAKING_HOURS = (8, 21)` and the rule is "after 21:00". A to-do opened at
        19:00 for tomorrow used to skip the entire evening, because `>= high` moved
        21:00 itself out of range."""
        db.set_today(f"{db.today().isoformat()}T19:00")
        self.addCleanup(db.set_today, None)
        todo, _ = todos.open_todo(self.conn, "Book the prix-fixe", due=self.d(1),
                                  event_id=self._dinner(offset=6).id,
                                  written_by="dream:nightly")
        self.assertEqual(f"{db.today().isoformat()}T21:00:00", todo.remind_at[:19])

    def test_late_at_night_it_says_the_morning_of_rather_than_nothing(self):
        """The other side of the same rule, asserted rather than assumed. At 23:00 the
        evening really is gone and 08:00 on the due day is the honest answer."""
        db.set_today(f"{db.today().isoformat()}T23:00")
        self.addCleanup(db.set_today, None)
        todo, _ = todos.open_todo(self.conn, "Book the prix-fixe", due=self.d(1),
                                  event_id=self._dinner(offset=6).id,
                                  written_by="dream:nightly")
        self.assertEqual(f"{self.d(1)}T08:00:00", todo.remind_at[:19])

    def test_a_deadline_with_nobody_behind_it_says_nothing(self):
        """The NYU case, and the one the user asked for by name. A date, a real one, and no
        person on the other end of it — so no poke, ever."""
        todo, _ = todos.open_todo(self.conn, "Complete NYU Tandon Bridge coursework",
                                  due=self.d(80), written_by="dream:nightly")
        self.assertIsNone(todo.remind_at)

    def test_an_event_nobody_else_is_part_of_is_not_a_consequence(self):
        solo = self._dinner(participants=())
        todo, _ = todos.open_todo(self.conn, "Print the boarding pass",
                                  event_id=solo.id, written_by="dream:nightly")
        self.assertIsNone(todo.remind_at)

    def test_an_invitation_he_has_not_accepted_is_not_a_commitment(self):
        """`opportunity` is "this exists, the user has not committed" — nobody is relying on
        them for a thing the user has not said yes to."""
        maybe = self._dinner(kind="opportunity")
        todo, _ = todos.open_todo(self.conn, "Find a prix-fixe reservation",
                                  event_id=maybe.id, written_by="dream:nightly")
        self.assertIsNone(todo.remind_at)

    def test_a_standing_obligation_still_says_nothing(self):
        """"Sign the bank paperwork for Mom" — no date, no moment to count back from."""
        todo, _ = todos.open_todo(self.conn, "Sign the bank paperwork for Mom",
                                  written_by="dream:nightly")
        self.assertIsNone(todo.remind_at)

    def test_a_deadline_already_gone_is_not_timed_for_now(self):
        # `remind_when` declines rather than clamping to this second, and the automatic
        # path inherits that: a reminder about something that already happened is worse
        # than silence, because the user will act on it.
        todo, _ = todos.open_todo(self.conn, "Find a prix-fixe reservation",
                                  event_id=self._dinner(offset=-3).id,
                                  written_by="dream:nightly")
        self.assertIsNone(todo.remind_at)

    def test_a_time_he_chose_is_never_overwritten(self):
        todo, _ = todos.open_todo(self.conn, "Call the bank", due=self.d(4),
                                  remind_at=f"{self.d(1)}T07:15:00", written_by="live")
        self.assertEqual(todo.remind_at[:16], f"{self.d(1)}T07:15")

    def test_the_switch_is_the_users_and_turning_it_off_restores_silence(self):
        todo, _ = todos.open_todo(self.conn, "Find a prix-fixe reservation",
                                  event_id=self._dinner().id, auto_remind=False,
                                  written_by="dream:nightly")
        self.assertIsNone(todo.remind_at)

    def test_it_reaches_the_job_that_pokes_him(self):
        """End to end, because the column existing is not the point — being asked about
        it is. `due_reminders` is the whole of what the Telegram job reads."""
        todos.open_todo(self.conn, "Find a prix-fixe reservation", due=self.d(1),
                        event_id=self._dinner(offset=2).id, written_by="dream:nightly")
        due = todos.due_reminders(self.conn, now=f"{self.d(1)}T09:30:00")
        self.assertEqual([t.text for t in due], ["Find a prix-fixe reservation"])


class TestAReminderLandsAtAReasonableHour(unittest.TestCase):
    """`due` is when a thing is due; a reminder is a *time*, and it had nowhere to live.

    The user forgot a dinner reservation Morgan asked for at 12:19 the day before. `due` is a
    date and `wake_condition` is prose matched against later traffic, so nothing in the
    store could say "poke me at nine tomorrow morning". These are the rules that decide
    the hour, in code, with no model anywhere near them.
    """

    def at(self, when: str) -> datetime:
        return datetime.fromisoformat(when)

    def test_nine_the_morning_before(self):
        # The dinner case: asked on the 12th, dinner on the 13th, reminder on the 12th.
        got = todos.remind_when("2026-08-13", now=self.at("2026-08-12T08:00:00"))
        self.assertEqual(got[:16], "2026-08-12T09:00")

    def test_a_reminder_never_fires_in_the_night(self):
        # 09:00 the day before has gone; two hours out would be 01:00, which is not a
        # reminder, it is something to dismiss half asleep.
        got = todos.remind_when("2026-08-13", now=self.at("2026-08-12T23:30:00"))
        self.assertEqual(got[:16], "2026-08-13T08:00")

    def test_a_late_ask_still_gets_a_slot_today(self):
        # Opened at noon for tomorrow: 09:00 today is gone, so it takes the next hour
        # at least two out rather than skipping its only chance.
        got = todos.remind_when("2026-08-13", now=self.at("2026-08-12T12:10:00"))
        self.assertEqual(got[:13], "2026-08-12T14")

    def test_a_reminder_never_arrives_after_the_thing_itself(self):
        # Same-day and already late. A reminder for tonight's dinner at 22:00 tomorrow
        # would be worse than none.
        got = todos.remind_when("2026-08-13", now=self.at("2026-08-13T18:00:00"))
        self.assertLessEqual(got, "2026-08-13T21:00:00")

    def test_nothing_to_count_back_from_invents_nothing(self):
        self.assertIsNone(todos.remind_when(None))
        self.assertIsNone(todos.remind_when("not a date"))

    def test_something_already_past_gets_no_reminder_at_all(self):
        # Found on live data. "Remind you about the hand-poke tattoo session" matched
        # the occurrence on the 11th instead of the one on the 24th, and every clamp
        # above dutifully moved the target forward until the answer was *this second*.
        # A reminder about something that already happened is worse than silence,
        # because the user will act on it.
        self.assertIsNone(todos.remind_when("2026-08-11",
                                            now=self.at("2026-08-13T15:34:00")))

    def test_the_end_of_the_anchor_day_is_the_cutoff(self):
        # Still the same day and still time to act: a reminder, not silence.
        got = todos.remind_when("2026-08-13", now=self.at("2026-08-13T20:00:00"))
        self.assertIsNotNone(got)
        # Past the last waking hour of the day it is about: nothing.
        self.assertIsNone(todos.remind_when("2026-08-13",
                                            now=self.at("2026-08-13T21:30:00")))


class TestAPokeIsNotProofHeWasTold(unittest.TestCase):

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.cfg = Config(home=self.home)
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)
        self.addCleanup(self.conn.close)

    def open(self, text, **kw):
        return todos.open_todo(self.conn, text, **kw)[0]

    def test_a_reminder_that_has_come_due_is_returned(self):
        self.open("book the restaurant", remind_at="2026-08-12T09:00:00")
        due = todos.due_reminders(self.conn, now="2026-08-12T09:00:01")
        self.assertEqual([t.text for t in due], ["book the restaurant"])

    def test_a_reminder_in_the_future_stays_quiet(self):
        self.open("book the restaurant", remind_at="2026-08-12T09:00:00")
        self.assertEqual(todos.due_reminders(self.conn, now="2026-08-12T08:59:00"), [])

    def test_a_todo_with_no_reminder_never_appears(self):
        self.open("sign the bank paperwork")
        self.assertEqual(todos.due_reminders(self.conn, now="2026-12-01T09:00:00"), [])

    def test_a_poke_snoozes_rather_than_nagging(self):
        todo = self.open("book the restaurant", remind_at="2026-08-12T09:00:00")
        todos.mark_reminded(self.conn, todo.key)
        self.assertEqual(todos.due_reminders(self.conn, now=db.now()), [])

    def test_a_poke_the_agent_ignored_comes_back(self):
        # The whole reason `reminded_at` is a snooze and not a tombstone. The agent may
        # have replied [SILENT]; nothing here can see that, and a reminder dropped on
        # the strength of a judgement to *wait* is a judgement to forget.
        todo = self.open("book the restaurant", remind_at="2026-08-12T09:00:00")
        self.conn.execute("UPDATE todos SET reminded_at = ? WHERE key = ?",
                          ("2026-08-12T09:00:00", todo.key))
        later = "2026-08-12T09:00:00"
        hours = todos.REMINDER_SNOOZE_HOURS
        soon = f"2026-08-12T{9 + hours - 1:02d}:00:00"
        after = f"2026-08-12T{9 + hours + 1:02d}:00:00"
        self.assertEqual(todos.due_reminders(self.conn, now=soon), [], "still snoozed")
        self.assertEqual([t.text for t in todos.due_reminders(self.conn, now=after)],
                         ["book the restaurant"], "should come back")
        # And the brief carries it the whole time, because the poke told nobody.
        self.assertEqual(
            [t.text for t in todos.due_reminders(self.conn, now=later, snooze=False)],
            ["book the restaurant"])

    def test_a_closed_todo_stops_reminding(self):
        todo = self.open("book the restaurant", remind_at="2026-08-12T09:00:00")
        todos.close(self.conn, todo.key)
        self.assertEqual(todos.due_reminders(self.conn, now="2026-08-12T10:00:00"), [])

    def test_a_reminder_added_later_reads_the_todo_the_store_already_has(self):
        # The ordinary case, and the one that refused outright: a to-do opened days ago
        # with a due date, given a reminder now. `live.open_todo` read only the
        # arguments of *this* call, so the row's own due date was invisible and the
        # anchor came back empty.
        # Dated forward from today rather than written down: the anchor has to be ahead
        # of now for there to be anything to count back from, so a literal due date is a
        # test that expires — this one raised LiveError from 2026-09-02 onwards.
        due = db.today() + timedelta(days=19)
        live.open_todo(self.conn, self.cfg, "renew the passport", due=due.isoformat())
        todo, verb = live.open_todo(self.conn, self.cfg, "renew the passport",
                                    remind=True)
        self.assertEqual(verb, "updated")
        self.assertTrue(todo.remind_at, "the stored due date should have timed it")
        self.assertEqual(todo.remind_at[:10], (due - timedelta(days=1)).isoformat())


class TestAReminderNeverReachesAPhoneByDefault(unittest.TestCase):
    """Invariant 11, for the loudest thing memcal can write outside itself.

    `publish_calendar` shipped set to "memcal" once and the next test run put ten
    fixtures in a real Mac's Calendar.app, syncing to a phone. A reminder is worse: it
    does not sit quietly in a calendar, it makes the phone buzz at an hour memcal chose.
    Every test builds a Config and none of them asks for a reminder, so the default has
    to be off and `publish_reminder` has to be the one place that decides it.
    """

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.cfg = Config(home=self.home)
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)
        self.addCleanup(self.conn.close)
        # A reminder is timed by counting back from something that has not happened yet,
        # so a literal due date makes the test an appointment: `due="2026-08-14"` was
        # fine on the 13th and raised LiveError from the 15th on, in three tests that are
        # about the switch and not about dates at all.
        self.due = (db.today() + timedelta(days=7)).isoformat()

    def test_the_default_is_off(self):
        self.assertEqual(Config(home=self.home).publish_reminders, "")

    def test_opening_a_todo_with_a_reminder_calls_no_osascript(self):
        from memcal.sources import ical
        with mock.patch.object(ical, "_reminder_call") as called:
            todo, _ = live.open_todo(self.conn, self.cfg, "book the restaurant",
                                     due=self.due, remind=True)
        called.assert_not_called()
        # The reminder is still *recorded* — it just does not leave the process. Turning
        # the switch on later has to be enough to start delivering them.
        self.assertTrue(todo.remind_at)
        self.assertIsNone(todo.reminder_uid)

    def test_a_phone_that_will_not_answer_does_not_lose_the_todo(self):
        from memcal.sources import ical
        self.cfg.publish_reminders = "memcal"
        with mock.patch.object(ical, "_reminder_call",
                               side_effect=ical.ReminderError("Reminders is not authorized")):
            todo, verb = live.open_todo(self.conn, self.cfg, "book the restaurant",
                                        due=self.due, remind=True)
        self.assertEqual(verb, "opened")
        self.assertTrue(todos.get(self.conn, todo.key))

    def test_closing_a_todo_takes_the_reminder_back_off(self):
        from memcal.sources import ical
        self.cfg.publish_reminders = "memcal"
        with mock.patch.object(ical, "_reminder_call",
                               return_value={"uid": "REM-1", "list": "memcal"}):
            todo, _ = live.open_todo(self.conn, self.cfg, "book the restaurant",
                                     due=self.due, remind=True)
        self.assertEqual(todo.reminder_uid, "REM-1")
        with mock.patch.object(ical, "_reminder_call",
                               return_value={"removed": True}) as dropped:
            live.close_todo(self.conn, self.cfg, "book the restaurant")
        self.assertEqual(dropped.call_args.args[0], "drop")


class TestNeverRunIsNotAFailedRun(unittest.TestCase):

    def _status(self, listing: str) -> dict:
        home = Path(tempfile.mkdtemp())
        cfg = Config(home=home)
        cfg.ensure_dirs()
        schedule.script_path(cfg).write_text("#!/bin/sh\nPY=/usr/bin/python3\n")
        # Both plists, not just the nightly one: `catchup_plist_path()` reads
        # `~/Library/LaunchAgents` and this class was green only on a machine where the
        # catch-up agent happened to be installed. On CI it produced a catch-up warning
        # and these two assertions — which are about the *exit code* field — failed for
        # a reason they say nothing about.
        with mock.patch.object(schedule, "_launchctl", return_value=(0, listing)), \
             mock.patch.object(schedule, "plist_path",
                               return_value=home / "com.memcal.nightly.plist"), \
             mock.patch.object(schedule, "catchup_plist_path",
                               return_value=home / "com.memcal.catchup.plist"):
            (home / "com.memcal.nightly.plist").write_bytes(
                plistlib.dumps({"StartCalendarInterval": {"Hour": 3, "Minute": 0}}))
            (home / "com.memcal.catchup.plist").write_bytes(plistlib.dumps({}))
            return schedule.status(cfg)

    def test_a_job_that_has_never_run_is_not_warned_about(self):
        state = self._status("\tlast exit code = (never exited)\n")
        self.assertIsNone(state["warning"])

    def test_a_job_that_exited_zero_is_not_warned_about(self):
        self.assertIsNone(self._status("\tlast exit code = 0\n")["warning"])

    def test_a_job_that_exited_nonzero_is_still_warned_about(self):
        # The failure the check was written for: 127 is the shell's "command not found",
        # which is what eleven nights of a missing nightly.sh looked like.
        state = self._status("\tlast exit code = 127\n")
        self.assertIn("127", state["warning"] or "")


class TestAnIndexOfNamesCannotBeUsedToDecideAnything(Base):

    def _slots(self, slug, **slots):
        for name, value in slots.items():
            wiki.set_slot(self.cfg.wiki_dir, slug, name.replace("_", " "), value,
                          conn=self.conn)

    def test_the_pages_line_names_the_facts_a_page_holds(self):
        self._slots("casey-morgan",
                    current_resume="documents/casey-morgan-resume.pdf",
                    education="computer science")
        line = brief._pages_line(self.cfg)
        self.assertIn("casey-morgan (current resume, education)", line)

    def test_the_reported_question_can_be_routed_from_the_brief_alone(self):
        # The whole point, stated as the report stated it: the words the user is going
        # to use have to appear in the file that is always in context.
        self._slots("casey-morgan",
                    current_resume="documents/casey-morgan-resume.pdf")
        text = brief.render(self.conn, self.cfg)
        self.assertIn("current resume", text)
        self.assertIn("memcal_open_page", text)

    def test_a_page_with_nothing_on_it_is_still_named(self):
        # An empty page has nothing to advertise, and dropping its name would lose the
        # only record in context that it exists at all.
        wiki.ensure(self.cfg.wiki_dir, "breakfast", section="projects")
        wiki.add_alias(self.cfg.wiki_dir, "breakfast", "Morning Thing")
        self._slots("reese", relationship="partner")
        line = brief._pages_line(self.cfg)
        self.assertIn("breakfast ·", line + " ·")
        self.assertNotIn("breakfast (", line)
        self.assertIn("reese (relationship)", line)

    def test_the_index_has_a_ceiling_and_the_contents_are_what_elide(self):
        # It is injected on every turn, so it is capped like everything else here. What
        # gives way under pressure is the contents, never a name.
        for n in range(30):
            self._slots(f"person-{n:02d}",
                        **{f"a_very_long_slot_name_number_{n:02d}": "x"})
        line = brief._pages_line(self.cfg)
        self.assertLessEqual(len(line), brief.PAGES_LINE_MAX_CHARS)
        for n in range(30):
            self.assertIn(f"person-{n:02d}", line)
        described = line.count(" (")
        self.assertGreater(described, 0, "the budget was abandoned, not spent")
        self.assertLess(described, 30, "the ceiling did not bind")

    def test_a_name_is_never_dropped_even_past_the_ceiling(self):
        # Losing a slug loses the only record in context that the page exists at all,
        # which is strictly worse than the bug this whole change is about.
        for n in range(120):
            self._slots(f"person-{n:03d}", **{f"slot_{n:03d}": "x"})
        line = brief._pages_line(self.cfg)
        self.assertGreater(len(line), brief.PAGES_LINE_MAX_CHARS)
        self.assertNotIn("(", line)       # nothing described; everything still named
        for n in range(120):
            self.assertIn(f"person-{n:03d}", line)

    def test_only_four_slots_of_one_page_reach_the_index(self):
        self._slots("quinn-brooks", one="1", two="2", three="3", four="4", five="5")
        self.assertEqual(wiki.slot_index(self.cfg.wiki_dir)["quinn-brooks"],
                         ["one", "two", "three", "four"])

    def test_trimming_the_brief_drops_a_preference_before_the_wiki_index(self):
        # Legacy preferences stay out of the brief, while the wiki index remains.
        self._slots("reese", relationship="partner")
        for n in range(40):
            todos.set_standing(self.conn, "preference",
                               f"Preference number {n} " + "padding " * 12,
                               scope="permanent")
        cfg = Config(home=self.cfg.home, brief_token_cap=260)
        text = brief.render(self.conn, cfg)
        self.assertLess(text.count("Preference number"), 40, "nothing was trimmed")
        self.assertIn("reese (relationship)", text)


class TestAPageReadWasBareMarkdownWrappedInSmallTalk(Base):
    """`memcal_open_page` answered with a markdown blob and four lines of chat.

    Same report. Once the agent did open the page it got the facts only as rendered
    markdown — so the source and date of each one were inside an HTML comment — and a
    `sources` list padded with the thread context around each quote: the slot recording
    where a résumé lives came back with "How r u doin" and an emoji attached.
    """

    def _stated(self, slug, slot, value, said):
        """A fact with a real evidence link, and neighbours in the same thread."""
        stamp = db.now_dt()
        ids = []
        for n, text in enumerate(said):
            ids.append(archive.append(
                self.conn, stream="imessage", external_id=f"{slug}-{slot}-{n}",
                ts=(stamp + timedelta(minutes=n)).isoformat(), text=text,
                thread="+15550000000", person="Colin", from_me=False))
        wiki.set_slot(self.cfg.wiki_dir, slug, slot, value, conn=self.conn)
        trace.stamp(self.conn, kind="wiki", ref=f"{slug}.{slot.lower()}",
                    archive_ids=[ids[1]])
        self.conn.commit()

    def test_a_fact_is_a_field_with_its_source_without_parsing_markdown(self):
        wiki.set_slot(self.cfg.wiki_dir, "casey-morgan", "current resume",
                      "documents/casey-morgan-resume.pdf",
                      source="agent", conn=self.conn)
        profile = wiki.profile(self.conn, self.cfg.wiki_dir, "casey-morgan")
        self.assertEqual(profile["answers"], ["current resume"])
        fact = profile["facts"][0]
        self.assertEqual(fact["slot"], "current resume")
        self.assertEqual(fact["value"], "documents/casey-morgan-resume.pdf")
        self.assertEqual(fact["source"], "agent")
        self.assertEqual(fact["ts"], db.today().isoformat())
        self.assertEqual(profile["title"], "Casey Morgan")
        self.assertEqual(profile["section"], "people")

    def test_the_rendered_page_and_the_sources_still_reach_the_old_callers(self):
        # `/api/wiki` and both agent surfaces read these three. Adding fields above
        # them is the change; removing any of them would be a different one.
        wiki.set_slot(self.cfg.wiki_dir, "reese", "relationship", "partner",
                      conn=self.conn)
        profile = wiki.profile(self.conn, self.cfg.wiki_dir, "reese")
        self.assertIn("- **relationship**: partner", profile["page"])
        self.assertEqual(profile["encounters"]["count"], 0)
        self.assertIn("sources", profile)

    def test_the_page_read_quotes_the_evidence_and_not_the_small_talk(self):
        self._stated("colin", "education", "computer science",
                     ["How r u doin", "decided I wanted to go back to school for cs",
                      "\N{FACE WITH TEARS OF JOY}"])
        rows = wiki.profile(self.conn, self.cfg.wiki_dir, "colin")["sources"]["education"]
        self.assertEqual([r["text"] for r in rows],
                         ["decided I wanted to go back to school for cs"])
        self.assertTrue(all(r["evidence"] for r in rows))

    def test_the_surrounding_conversation_is_still_one_call_away(self):
        # Quieter is only defensible because nothing was lost: `memcal_source` with
        # kind='wiki' is the documented way back to the context the page read dropped.
        self._stated("colin", "education", "computer science",
                     ["How r u doin", "decided I wanted to go back to school for cs",
                      "\N{FACE WITH TEARS OF JOY}"])
        rows = trace.source_rows(self.conn, "wiki", "colin.education")
        self.assertIn("How r u doin", [r["text"] for r in rows])


class TestOnePageToolAnsweredTwoDifferentWays(Base):
    """The MCP surface returned raw markdown while Hermes returned the whole profile.

    `memcal_add` once took `until` on one surface and not the other; this is the read
    half of the same interface drift. `memcal_open_page` is one
    tool name, and the portable surface was the poorer one — no encounter count, and
    no way at all to see the line a fact came from.
    """

    def _server(self):
        server = mcp_server.Server.__new__(mcp_server.Server)
        server.cfg, server.conn = self.cfg, self.conn
        return server

    def test_the_mcp_page_read_carries_the_encounters_and_the_evidence(self):
        stamp = db.now_dt()
        line = archive.append(
            self.conn, stream="imessage", external_id="quinn-1", ts=stamp.isoformat(),
            text="My favorite theater is Alamo Drafthouse", thread="+15550000000",
            person="Quinn Brooks", from_me=False)
        wiki.set_slot(self.cfg.wiki_dir, "quinn-brooks", "favorite theater",
                      "Alamo Drafthouse", conn=self.conn)
        trace.stamp(self.conn, kind="wiki", ref="quinn-brooks.favorite theater",
                    archive_ids=[line])
        events.upsert(self.conn, {"title": "Poker at Robbie's", "date": self.d(-7),
                                  "status": "happened",
                                  "participants": ["Quinn Brooks"]}, match=False)
        self.conn.commit()
        text = self._server().call("memcal_open_page", {"slug": "quinn-brooks"})
        self.assertIn("- **favorite theater**: Alamo Drafthouse", text)
        self.assertIn("Past encounters: 1", text)
        self.assertIn("Alamo Drafthouse", text.split("## Stated by")[1])

    def test_a_slot_with_no_citation_is_not_given_someone_elses_words(self):
        # `source_rows` recovers the whole spool bundle for anything written before
        # line-level citation existed and marks every line of it evidence, so quoting
        # the first two is not a smaller answer, it is a wrong one: the live store
        # attributed "education: computer science" to "Yooooo how's it going".
        stamp = db.now_dt()
        for n, text in enumerate(["Yooooo how's it going", "went back to school for cs"]):
            line = archive.append(
                self.conn, stream="imessage", external_id=f"colin-{n}",
                ts=(stamp + timedelta(minutes=n)).isoformat(), text=text,
                thread="+15550000000", person="Colin", from_me=False)
            archive.spool_add(self.conn, line, "person:Colin")
        self.conn.execute(
            "UPDATE spool SET run_id = 1 WHERE entity = 'person:Colin'")
        wiki.set_slot(self.cfg.wiki_dir, "colin", "education", "computer science",
                      conn=self.conn)
        self.conn.execute(
            "INSERT INTO provenance(kind, ref, verb, entity, stage, run_id, at)"
            " VALUES('wiki','colin.education','set','person:Colin','propose',1,?)",
            (db.now(),))
        self.conn.commit()
        text = self._server().call("memcal_open_page", {"slug": "colin"})
        self.assertIn("no line-level citation", text)
        self.assertNotIn("Yooooo", text)

    def test_an_unknown_slug_still_lists_what_does_exist(self):
        wiki.set_slot(self.cfg.wiki_dir, "reese", "relationship", "partner",
                      conn=self.conn)
        text = self._server().call("memcal_open_page", {"slug": "nobody"})
        self.assertIn("reese", text)

    def test_both_surfaces_point_at_the_line_that_routes_the_call(self):
        # The tool description is what the call routes on. A surface that does not name
        # the brief's index is a surface where the reported bug is still live.
        mcp = next(t for t in mcp_server.TOOLS
                   if t["name"] == "memcal_open_page")["description"]
        hermes = (Path(__file__).resolve().parent.parent / "integrations" / "hermes"
                  / "memcal" / "__init__.py").read_text(encoding="utf-8")
        hermes = hermes.split("OPEN_PAGE = {", 1)[1].split("\n}", 1)[0]
        for text, where in ((mcp, "mcp_server"), (hermes, "hermes")):
            self.assertIn("Pages:", text, where)
            self.assertIn("current resume", text, where)


class TestAPassThatReportedSuccessWithASourceDown(Base):
    """Nine nightly runs failed to read email and all nine exited 0.

    *"Blue Table marketing email you're invited … Not sure IF it got picked up, just look
    into it."* It had not been picked up, and nor had nine days of everything else: the
    Proton Bridge is not running at 03:00, which is the only hour memcal ever tried.
    Every layer recorded the failure honestly and the layer above it summarised the
    failure away.
    """

    def test_a_failed_source_shows_up_on_the_pass_it_failed(self):
        collection = archive.open_collection(self.conn, mode="test")
        archive.record_source(self.conn, collection, base.IngestReport(
            stream="email", error="cannot reach Proton Bridge at 127.0.0.1:1143"))
        archive.record_source(self.conn, collection,
                              base.IngestReport(stream="imessage", read=4))
        archive.close_collection(self.conn, collection)
        row = self.conn.execute("SELECT error FROM collections WHERE id = ?",
                                (collection,)).fetchone()
        self.assertIn("email", row["error"])
        self.assertIn("Proton Bridge", row["error"])

    def test_a_clean_pass_has_no_error_to_learn_to_ignore(self):
        collection = archive.open_collection(self.conn, mode="test")
        archive.record_source(self.conn, collection,
                              base.IngestReport(stream="imessage", read=4))
        archive.close_collection(self.conn, collection)
        row = self.conn.execute("SELECT error FROM collections WHERE id = ?",
                                (collection,)).fetchone()
        self.assertIsNone(row["error"])

    def test_an_explicit_error_still_wins(self):
        """A caller that caught an exception around the whole loop knows more than we do."""
        collection = archive.open_collection(self.conn, mode="test")
        archive.record_source(self.conn, collection,
                              base.IngestReport(stream="email", error="bridge down"))
        archive.close_collection(self.conn, collection, error="the disk filled up")
        row = self.conn.execute("SELECT error FROM collections WHERE id = ?",
                                (collection,)).fetchone()
        self.assertEqual("the disk filled up", row["error"])

    def test_ingest_all_exits_non_zero_when_a_source_fails(self):
        """The line that made it invisible: `failed and len(chosen) == 1`.

        The only unattended caller in the system is the 3am launchd job and it always
        passes `all`, so the one branch that could report a failure was the one branch
        that job could never take.
        """
        class Broken:
            name, in_all = "email", True

            def check(self, cfg):
                return False, "bridge down"

        def catch_up(source, conn, cfg, **kw):
            report = base.IngestReport(stream=source.name)
            if source.name == "email":
                report.error = "cannot reach Proton Bridge"
            archive.record_source(conn, kw.get("collection_id"), report)
            return report

        args = argparse.Namespace(home=str(self.cfg.home), stream="all", stale=False,
                                  limit=10, rounds=1)
        with mock.patch.object(cli.sources, "all_sources", return_value=[Broken()]), \
             mock.patch.object(cli.sources, "catch_up", side_effect=catch_up), \
             contextlib.redirect_stdout(io.StringIO()), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, cli.cmd_ingest(args))

    def test_the_retry_takes_only_what_is_behind_and_answering(self):
        """`--stale`, which is what the catch-up job runs twice a day.

        Both halves matter. Without "behind" it re-pulls everything on a healthy
        machine; without "answering" it spends a round failing to connect to the thing
        that is genuinely down, twice a day, for ever.
        """
        class Source:
            def __init__(self, name, ok):
                self.name, self._ok, self.in_all = name, ok, True

            def check(self, cfg):
                return self._ok, ""

        archive.append(self.conn, stream="email", external_id="e1",
                       ts=(db.now_dt() - timedelta(days=9)).isoformat(), text="hi")
        archive.append(self.conn, stream="whatsapp", external_id="w1",
                       ts=(db.now_dt() - timedelta(days=9)).isoformat(), text="hi")
        archive.append(self.conn, stream="imessage", external_id="i1",
                       ts=db.now_dt().isoformat(), text="hi")
        candidates = [Source("email", True), Source("whatsapp", False),
                      Source("imessage", True)]
        with mock.patch.object(cli.archive, "registered_streams",
                               return_value={"email", "whatsapp", "imessage"}):
            chosen = cli._behind_and_reachable(self.conn, self.cfg, candidates)
        self.assertEqual(["email"], [s.name for s in chosen])


class TestACommandThatWasNeverOnceRun(Base):
    """`memcal who` crashed on `NameError: threads` and had done for as long as git
    remembers — the module was used in the function and never imported.

    It is the command `doctor` recommends for the queue it complains about, so the one
    path from "247 handles have no name" to doing anything about it was a traceback. No
    test called it; every other test called functions, and a missing import is invisible
    until the line that uses it runs.
    """

    def test_no_command_reaches_for_a_name_the_module_does_not_have(self):
        """A NameError at module scope is caught by importing. This is the other kind:
        a global referenced inside a function body that nothing ever executed."""
        import builtins
        source = (Path(cli.__file__)).read_text(encoding="utf-8")
        tree = ast.parse(source)
        known = set(dir(cli)) | set(dir(builtins))
        missing = []
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef) or not node.name.startswith("cmd_"):
                continue
            local = set()
            # Everything the function itself binds: parameters, assignments, loop and
            # `with` targets, comprehension variables, `except … as`, nested defs and
            # their parameters, and imports. Anything left over is a global, and a
            # global the module does not have is the bug.
            for sub in ast.walk(node):
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    spec = sub.args
                    local.update(a.arg for a in
                                 spec.posonlyargs + spec.args + spec.kwonlyargs)
                    for extra in (spec.vararg, spec.kwarg):
                        if extra:
                            local.add(extra.arg)
                    if not isinstance(sub, ast.Lambda):
                        local.add(sub.name)
                if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
                    local.add(sub.id)
                if isinstance(sub, ast.alias):
                    local.add((sub.asname or sub.name).split(".")[0])
                if isinstance(sub, ast.ExceptHandler) and sub.name:
                    local.add(sub.name)
                if isinstance(sub, ast.ClassDef):
                    local.add(sub.name)
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load)
                        and sub.id not in local and sub.id not in known):
                    missing.append(f"{node.name} uses {sub.id!r}, which {cli.__name__} "
                                   f"does not define or import")
        self.assertEqual([], sorted(set(missing)))

    def test_who_actually_runs_with_something_in_the_queue(self):
        """The narrow version, on the command it happened to."""
        identity.note_unresolved(self.conn, "+19175550123", "imessage",
                                 seen_name=None, sample="hey")
        self.conn.commit()
        args = argparse.Namespace(home=str(self.cfg.home), handle=None, person=None,
                                  limit=10, adopt=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, cli.cmd_who(args))
        self.assertIn("+19175550123", out.getvalue())


class TestANumeralIsNotAName(Base):
    """247 unnamed handles, 47 of them carrying 25+ messages, and GroupMe had been
    telling us most of those names the whole time.

    Bundling is by entity (invariant 3), so every one of those was a person whose rows
    filed under a numeral and joined up with nothing.
    """

    def test_the_platform_name_is_taken_verbatim(self):
        identity.note_unresolved(self.conn, "groupme:24687395", "groupme",
                                 seen_name="Elias Brenner")
        self.conn.commit()
        self.assertEqual([("groupme:24687395", "Elias Brenner")],
                         identity.adopt_platform_names(self.conn))
        self.assertEqual("Elias Brenner",
                         identity.resolve(self.conn, "groupme:24687395"))

    def test_it_never_goes_through_the_first_name_guess(self):
        """The seven wrong merges this was one step away from making.

        `guess_person` matches on first name and is documented as a prompt pre-fill.
        On this very queue it offered *joe coleman* for Joe Navarro and *jack kirkland*
        for Jack Bartley — the collision beat 7 of the benchmark exists for.
        """
        identity.link(self.conn, "+19175550001", "Joe Coleman", source="contacts")
        identity.note_unresolved(self.conn, "groupme:6014661", "groupme",
                                 seen_name="Joe Navarro")
        self.conn.commit()
        # The guess is wrong, and is still offered to a human who can see it is wrong.
        row = self.conn.execute(
            "SELECT * FROM unresolved WHERE handle = 'groupme:6014661'").fetchone()
        self.assertEqual("Joe Coleman", identity.guess_person(self.conn, row))
        # Adoption ignores it entirely.
        identity.adopt_platform_names(self.conn)
        self.assertEqual("Joe Navarro", identity.resolve(self.conn, "groupme:6014661"))

    def test_an_exact_contact_match_still_wins(self):
        """`link_by_name` runs first, so a name memcal already knows does not fork."""
        identity.link(self.conn, "+19175550002", "Avery Morgan", source="contacts")
        self.conn.commit()
        self.assertEqual("Avery Morgan", identity.link_by_name(
            self.conn, "groupme:30819315", "avery morgan", source="groupme:roster"))

    def test_a_platform_announcement_channel_is_not_a_person(self):
        """`groupme:system` — "A message was deleted", 218 times — was the loudest row
        in the queue, and "name this" has no answer for it."""
        identity.note_unresolved(self.conn, "groupme:system", "groupme",
                                 seen_name="GroupMe")
        self.conn.commit()
        self.assertEqual([], list(identity.unresolved(self.conn)))

    def test_a_bot_is_not_a_person_and_abbot_is(self):
        """Tight on purpose: a person wrongly filtered here cannot be named at all."""
        for name in ("Kanye Bot", "DinoBot", "Copilot", "GroupMe"):
            self.assertFalse(identity.is_person("groupme:1", name), name)
        for name in ("Abbot", "Abbott", "Botond", "Jack Bartley", "SKChats"):
            self.assertTrue(identity.is_person("groupme:1", name), name)

    def test_a_handle_no_platform_can_name_says_where_it_speaks(self):
        """A WhatsApp LID is opaque and matches no contact, so only the user can name it —
        and "whose 472 messages are these" is unanswerable in a way that "whose 472
        messages in the family chat" is not."""
        for n in range(3):
            archive.append(self.conn, stream="whatsapp", external_id=f"w{n}",
                           ts=db.now(), text="hi", thread="Family",
                           handle="whatsapp:lid:137933361279215")
        self.conn.commit()
        self.assertEqual(["Family"],
                         identity.where_seen(self.conn, "whatsapp:lid:137933361279215"))

    def test_adopting_twice_changes_nothing(self):
        identity.note_unresolved(self.conn, "groupme:24687395", "groupme",
                                 seen_name="Elias Brenner")
        self.conn.commit()
        identity.adopt_platform_names(self.conn)
        self.assertEqual([], identity.adopt_platform_names(self.conn))


class TestDoctorListedFactsAndVerdictsInTheSameColumn(Base):
    """Report 22. Twenty-six aligned lines, facts and problems indistinguishable.

    *"make memcal doctor better and have sections and find issues and list them with
    fixes or instead say its all ok."* `wiki pages 14` sat in the same column as
    `source email -- 9 days behind`, and reading it required already knowing what the
    numbers should be — which is the one thing a doctor's reader does not have.
    """

    def setUp(self):
        super().setUp()
        # `doctor` reads launchd, and launchd is a fact about the machine rather than
        # about the code. These nine were green because *their* Mac has
        # `com.memcal.nightly` installed and `~/Library/LaunchAgents` said so; on
        # anything that is not a Mac they raised `FileNotFoundError: 'launchctl'` out of
        # `schedule.status`, and on a Mac without the agent they would have taken the
        # other branch with nothing saying so. Pin the state under test — installed,
        # loaded, last run clean — so a finding is the code's answer and not the host's.
        agents = self.cfg.home / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        nightly = agents / f"{schedule.LABEL}.plist"
        nightly.write_bytes(
            plistlib.dumps({"StartCalendarInterval": {"Hour": 3, "Minute": 0}}))
        catchup = agents / f"{schedule.CATCHUP_LABEL}.plist"
        catchup.write_bytes(plistlib.dumps({}))
        schedule.script_path(self.cfg).write_text(
            f'#!/bin/sh\nPY="{sys.executable}"\n', encoding="utf-8")
        for patch in (mock.patch.object(schedule, "plist_path", return_value=nightly),
                      mock.patch.object(schedule, "catchup_plist_path",
                                        return_value=catchup),
                      mock.patch.object(schedule, "_launchctl",
                                        return_value=(0, "\tlast exit code = 0\n"))):
            patch.start()
            self.addCleanup(patch.stop)

    def _findings(self):
        return {f"{f.section}/{f.name}": f
                for f in cli.doctor_findings(self.conn, self.cfg)}

    def test_every_bad_finding_names_a_command_to_type(self):
        """The half that was missing entirely. A problem with no remedy is an `INFO`."""
        found = cli.doctor_findings(self.conn, self.cfg)
        self.assertTrue(found, "doctor found nothing at all to look at")
        for finding in found:
            if finding.bad:
                self.assertTrue(finding.fix, f"{finding.section}/{finding.name} has no fix")

    def test_every_finding_lands_in_a_section_that_gets_printed(self):
        """A section missing from `SECTIONS` is a finding computed and never shown."""
        found = cli.doctor_findings(self.conn, self.cfg)
        self.assertTrue(found, "doctor found nothing at all to look at")
        for finding in found:
            self.assertIn(finding.section, cli.SECTIONS, finding.name)

    def test_a_stale_but_reachable_source_is_the_one_that_cost_nine_days(self):
        """Reachable *now*, behind because the only scheduled attempt is at an hour its
        dependency is not up. That distinction is the whole fix, so it is graded."""
        archive.append(self.conn, stream="email", external_id="e1",
                       ts=(db.now_dt() - timedelta(days=9)).isoformat(), text="hi")

        class Reachable:
            name, in_all, description = "email", True, "email"

            def check(self, cfg):
                return True, "bridge up"

        with mock.patch.object(cli.archive, "registered_streams", return_value={"email"}), \
             mock.patch.object(cli.sources, "all_sources", return_value=[Reachable()]):
            found = self._findings()
        email = found.get("Sources/email")
        self.assertIsNotNone(email, sorted(found))
        self.assertEqual(cli.FAIL, email.status)
        self.assertIn("--stale", email.fix)

    def test_a_collection_that_failed_a_source_is_a_problem_on_its_own(self):
        """The check `close_collection`'s roll-up made possible. Nine nightly passes
        recorded three cheerful counts and no error at all."""
        collection = archive.open_collection(self.conn, mode="test")
        archive.record_source(self.conn, collection, base.IngestReport(
            stream="email", error="cannot reach Proton Bridge at 127.0.0.1:1143"))
        archive.close_collection(self.conn, collection)
        last = self._findings()["Sources/last collection"]
        self.assertEqual(cli.FAIL, last.status)
        self.assertIn("Proton Bridge", last.detail)

    def test_a_clean_collection_is_not_a_problem(self):
        collection = archive.open_collection(self.conn, mode="test")
        archive.record_source(self.conn, collection,
                              base.IngestReport(stream="imessage", read=3))
        archive.close_collection(self.conn, collection)
        self.assertEqual(cli.OK, self._findings()["Sources/last collection"].status)

    def test_a_quiet_unnamed_handle_is_not_worth_a_warning(self):
        """Thresholding on the raw total warned permanently — there are always hundreds
        of one-off numbers — and a check that always fires is not a check."""
        for n in range(60):
            identity.note_unresolved(self.conn, f"+1917555{n:04d}", "imessage")
        self.conn.commit()
        self.assertEqual(cli.OK, self._findings()["Store/unresolved handles"].status)

    def test_a_handle_carrying_real_traffic_is(self):
        for _ in range(cli.NOISY_HANDLE + 1):
            identity.note_unresolved(self.conn, "+19175550123", "imessage")
        self.conn.commit()
        finding = self._findings()["Store/unresolved handles"]
        self.assertEqual(cli.WARN, finding.status)
        self.assertIn("+19175550123", finding.detail)

    def test_publishing_off_is_a_fact_and_not_a_fault(self):
        """Invariant 11. A store that writes nothing outside itself is the default and
        the correct state, and calling it a problem would teach them to turn it on."""
        self.cfg.publish_calendar = ""
        finding = self._findings()["Calendar/publishing"]
        self.assertEqual(cli.INFO, finding.status)
        self.assertFalse(finding.fix)

    def test_an_optional_source_that_is_off_is_skipped_not_warned(self):
        """BlueBubbles being down is fine — `imessage` falls back to the local chat.db
        and reads more than BlueBubbles would. Printing it beside a real problem in the
        default view is how a diagnosis turns back into a list."""
        statuses = {f.name: f.status for f in cli.doctor_findings(self.conn, self.cfg)
                    if f.section == "Sources"}
        self.assertNotIn(cli.WARN, [statuses.get("bluebubbles")])

    def test_it_says_all_ok_rather_than_printing_nothing(self):
        out = io.StringIO()
        args = argparse.Namespace(home=str(self.cfg.home), verbose=False, json=False)
        with mock.patch.object(cli, "doctor_findings",
                               return_value=[cli.Finding("Store", "home", cli.OK, "fine")]), \
             contextlib.redirect_stdout(out):
            self.assertEqual(0, cli.cmd_doctor(args))
        self.assertIn("All 1 checks pass", out.getvalue())

    def test_a_problem_exits_non_zero_and_is_repeated_at_the_end(self):
        out = io.StringIO()
        args = argparse.Namespace(home=str(self.cfg.home), verbose=False, json=False)
        bad = cli.Finding("Sources", "email", cli.FAIL, "9 days behind",
                          fix="memcal ingest --stale")
        with mock.patch.object(cli, "doctor_findings", return_value=[bad]), \
             contextlib.redirect_stdout(out):
            self.assertEqual(1, cli.cmd_doctor(args))
        text = out.getvalue()
        self.assertIn("1 problem(s)", text)
        self.assertIn("memcal ingest --stale", text)

    def test_a_connector_error_is_cut_at_a_space_not_mid_word(self):
        """`message[:80]` produced `… <urlopen err`, which stops exactly where the
        reason starts: "connection refused" and "name or service not known" are the
        whole diagnosis and both were cut off."""
        long = ("URLError contacting http://localhost:1234/api/v1/ping: "
                "<urlopen error [Errno 61] Connection refused>")
        cut = cli._one_line(long, width=60)
        self.assertLessEqual(len(cut), 61)
        self.assertFalse(cut.rstrip("…").endswith(" "))
        self.assertTrue(cut.endswith("…"))
        self.assertEqual(long, cli._one_line(long, width=500))

    def test_today_is_not_today_ago(self):
        self.assertEqual("today", cli._ago(db.now()))
        self.assertTrue(cli._ago((db.now_dt() - timedelta(days=3)).isoformat())
                        .endswith("ago"))


class TestTwoBackgroundItemsBothCalledSh(unittest.TestCase):
    """Report 21. *"says 'sh' can run in background. can we make title more descriptive?"*

    macOS names a background item after the **file it executes**. Both plists ran
    `/bin/sh <script>`, so System Settings → Login Items & Extensions listed memcal twice
    as `sh`, with nothing saying which was which or that either was memcal. The `Label`
    is not what that pane reads, so a longer label would have fixed nothing.
    """

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.cfg = Config(home=self.home)
        self.cfg.ensure_dirs()

    def test_the_plists_execute_a_file_that_names_itself(self):
        for plist in (schedule.render_plist(self.cfg),
                      schedule.render_catchup_plist(self.cfg)):
            argv = plist["ProgramArguments"]
            self.assertEqual(1, len(argv), f"{argv} goes through an interpreter again")
            self.assertNotIn("/bin/sh", argv[0])
            self.assertIn("memcal", Path(argv[0]).name)

    def test_the_two_agents_are_told_apart_by_name(self):
        """Two items both called `sh` were indistinguishable; two both called `memcal`
        would only be half a fix."""
        names = {Path(p["ProgramArguments"][0]).name
                 for p in (schedule.render_plist(self.cfg),
                           schedule.render_catchup_plist(self.cfg))}
        self.assertEqual(2, len(names), names)

    def test_the_script_is_executable_or_launchd_cannot_run_it(self):
        """Running the file directly makes the executable bit part of the contract rather than
        decorative — `/bin/sh <script>` worked without it."""
        script = schedule.script_path(self.cfg)
        script.write_text(schedule.render_script(self.cfg), encoding="utf-8")
        script.chmod(0o755)
        self.assertTrue(script.stat().st_mode & 0o111)
        self.assertTrue(schedule.render_script(self.cfg).startswith("#!/bin/sh"))

    def test_the_old_name_is_cleared_when_nothing_was_edited(self):
        (self.home / "nightly.sh").write_text(schedule.render_script(self.cfg))
        told = schedule._retire_legacy(self.cfg, "nightly", schedule.render_script(self.cfg))
        self.assertFalse((self.home / "nightly.sh").exists())
        self.assertIn("removed nightly.sh", told[0])

    def test_a_changed_comment_is_not_a_local_edit(self):
        """It read as one, and a clean machine was told it had edits and left holding a
        `.superseded` copy of a file identical to the new one."""
        body = schedule.render_script(self.cfg).replace(
            "# memcal nightly", "# memcal nightly (an older wording)", 1)
        (self.home / "nightly.sh").write_text(body)
        schedule._retire_legacy(self.cfg, "nightly", schedule.render_script(self.cfg))
        self.assertFalse((self.home / "nightly.sh").exists())

    def test_a_real_edit_is_kept_under_a_name_that_is_not_live(self):
        (self.home / "nightly.sh").write_text(
            schedule.render_script(self.cfg) + '\necho "my own line"\n')
        told = schedule._retire_legacy(self.cfg, "nightly", schedule.render_script(self.cfg))
        self.assertFalse((self.home / "nightly.sh").exists())
        kept = self.home / "nightly.sh.superseded"
        self.assertTrue(kept.exists())
        self.assertIn("my own line", kept.read_text())
        self.assertIn("local edits", told[0])

    def test_running_it_by_hand_runs_it_the_way_launchd_will(self):
        """`schedule run` exists to be the real thing. Shelling out to `/bin/sh` here
        while launchd execs the file would make a green run prove nothing about 3am."""
        script = schedule.script_path(self.cfg)
        script.write_text("#!/bin/sh\nexit 7\n")
        script.chmod(0o755)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(7, schedule.run_now(self.cfg))


class TestTheCliCouldNotOpenWhatItPrinted(Base):
    """Report 20. `memcal` printed fourteen 〔E#〕 handles and had no verb for them.

    *"CLI is too confusing to use. Rework it."* The index was fine. Following an entry
    in it required a second identifier that only `week --keys` ever printed, and the
    legend on screen told a human to call `memcal_open`, which is an MCP tool.
    """

    def _event(self):
        event, _ = events.upsert(self.conn, {
            "title": "Tutoring", "date": db.today().isoformat(), "time": "13:00",
            "kind": "commitment", "status": "confirmed"}, written_by="cli")
        return event

    def test_a_handle_off_the_brief_resolves(self):
        event = self._event()
        for spelling in (f"E{event.id}", f"e{event.id}", f"〔E{event.id}〕",
                         f"[E{event.id}]"):
            self.assertEqual(event.key,
                             cli.find_event(self.conn, spelling).key, spelling)

    def test_the_key_and_the_title_still_work(self):
        event = self._event()
        self.assertEqual(event.key, cli.find_event(self.conn, event.key).key)
        self.assertEqual(event.key, cli.find_event(self.conn, "tutoring").key)

    def test_a_handle_of_the_wrong_kind_is_not_silently_an_event(self):
        """`T7` is a to-do. Resolving it to whatever event has id 7 would be worse than
        failing, because it would succeed."""
        self._event()
        todo, _ = todos.open_todo(self.conn, "Apply for 5 jobs", written_by="cli")
        self.assertIsNone(cli.find_event(self.conn, f"T{todo.id}"))
        self.assertEqual(todo.key, cli.find_todo(self.conn, f"T{todo.id}").key)

    def test_open_prints_the_whole_record(self):
        event = self._event()
        args = argparse.Namespace(home=str(self.cfg.home), ref=f"E{event.id}")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, cli.cmd_open(args))
        self.assertIn("Tutoring", out.getvalue())

    def test_open_says_what_a_handle_looks_like_when_given_a_dud(self):
        args = argparse.Namespace(home=str(self.cfg.home), ref="banana")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(1, cli.cmd_open(args))
        self.assertIn("E258", out.getvalue())

    def test_the_legend_names_a_verb_the_reader_can_actually_type(self):
        cli_text = brief.render(self.conn, self.cfg, surface="cli")
        self.assertIn("memcal open", cli_text)
        self.assertNotIn("memcal_open", cli_text)
        agent_text = brief.render(self.conn, self.cfg)
        self.assertIn("memcal_open", agent_text)

    def test_the_agent_legend_is_byte_stable(self):
        """The legend leads the brief, the brief leads the shared prefix, and a byte
        that moves there stops prompt caching paying for the whole run.

        Spelled out rather than compared to a constant, because a constant refactored
        alongside the code it guards proves nothing.
        """
        self.assertEqual(
            "[〔E#〕〔T#〕〔Q#〕 handles open with memcal_open — full detail: the "
            "address, the links, the messages it came from, and what has changed. "
            "Pages open with memcal_open_page; the names in parentheses after a page "
            "are the facts it holds]\n\n",
            brief.legend("agent"))
        self.assertEqual(brief.legend("agent"), brief.legend("nonsense-surface"))

    def test_every_argument_says_what_it_does(self):
        """`memcal week --help` printed `--back`, `--forward`, `--keys` and nothing else.

        Sixty-six of them were like that. Help that lists nouns with no verbs is the
        confusion itself, not a symptom of it, and the only thing that keeps a new flag
        from joining them is a check that counts.
        """
        parser = cli.build_parser()
        bare = [f"{name} {arg.dest}"
                for name, child in parser.memcal_subparsers.choices.items()
                for arg in child._actions
                if not isinstance(arg, argparse._HelpAction)
                and not (arg.help or "").strip()]
        self.assertEqual([], bare, "these arguments explain nothing")

    def test_no_two_visible_commands_do_the_same_job(self):
        """`ui` and `web` were both listed, and one of them said so in its own help."""
        parser = cli.build_parser()
        listed = [n for n in parser.memcal_commands if n not in cli.HIDDEN_COMMANDS]
        funcs = {}
        for name in listed:
            fn = parser.memcal_subparsers.choices[name].get_default("func")
            funcs.setdefault(fn, []).append(name)
        clashes = {fn.__name__: names for fn, names in funcs.items() if len(names) > 1}
        self.assertEqual({}, clashes, "two listed commands, one behaviour")

    def test_a_hidden_alias_still_runs(self):
        """Hidden is not removed. Anything in their shell history keeps working."""
        parser = cli.build_parser()
        self.assertTrue(cli.HIDDEN_COMMANDS, "nothing is hidden, so this proves nothing")
        for name in cli.HIDDEN_COMMANDS:
            self.assertIn(name, parser.memcal_commands)

    def test_the_help_starts_with_something_you_can_type(self):
        parser = cli.build_parser()
        self.assertIn("memcal E286", parser.epilog)
        self.assertIn("start here", parser.epilog)

    def test_completion_describes_the_commands_it_lists(self):
        """It shipped listing every command with an empty description, because
        `_install_grouped_help` clears the very list it was reading."""
        args = argparse.Namespace(home=str(self.cfg.home), shell="zsh")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, cli.cmd_completion(args))
        text = out.getvalue()
        self.assertIn("'week:the memcal window'", text)
        self.assertNotIn("'week:'", text)

    def test_question_and_legacy_standing_handles_still_work(self):
        """Current question handles and retired standing handles remain actionable."""
        todos.ask(self.conn, "What time is the thing?", written_by="cli")
        question = self.conn.execute("SELECT * FROM questions").fetchone()
        args = argparse.Namespace(home=str(self.cfg.home),
                                  question=f"Q{question['id']}", answer="Seven")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, cli.cmd_answer(args))

        key, _ = todos.set_standing(self.conn, "preference", "Curl before browsers")
        standing = self.conn.execute("SELECT * FROM standing WHERE key = ?",
                                     (key,)).fetchone()
        args = argparse.Namespace(home=str(self.cfg.home), key=f"S{standing['id']}")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, cli.cmd_forget(args))
        self.assertEqual([], todos.standing(self.conn, "preference"))

    def test_every_listing_prints_the_handle_the_write_commands_take(self):
        event = self._event()
        args = argparse.Namespace(home=str(self.cfg.home), back=7, forward=7, keys=False,
                                  json=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            cli.cmd_week(args)
        printed = out.getvalue()
        self.assertIn(f"E{event.id}", printed)
        # And the handle it printed is one the next command accepts.
        token = re.search(r"\bE(\d+)\b", printed).group(0)
        self.assertIsNotNone(cli.find_event(self.conn, token))


def _wa_store(members=(), sessions=(), messages=()):
    """A throwaway ChatStorage.sqlite with only the columns these tests read."""
    src = sqlite3.connect(":memory:")
    src.row_factory = sqlite3.Row
    src.executescript("""
        CREATE TABLE ZWAGROUPMEMBER (ZMEMBERJID TEXT, ZCONTACTNAME TEXT);
        CREATE TABLE ZWACHATSESSION (ZCONTACTJID TEXT, ZPARTNERNAME TEXT);
        CREATE TABLE ZWAMESSAGE (ZFROMJID TEXT);
        CREATE TABLE ZWAPROFILEPUSHNAME (ZJID TEXT, ZPUSHNAME TEXT);
    """)
    src.executemany("INSERT INTO ZWAGROUPMEMBER VALUES(?,?)", members)
    src.executemany("INSERT INTO ZWACHATSESSION VALUES(?,?)", sessions)
    src.executemany("INSERT INTO ZWAMESSAGE VALUES(?)", [(m,) for m in messages])
    return src


class TestALinkedIdIsNotAPhoneNumber(Base):
    """91 people, 1,669 messages, none of them ever resolved to anybody.

    WhatsApp moved group members off `@s.whatsapp.net` onto `@lid`, an opaque per-user
    id that exists so a group does not leak everyone's number. Its local part is all
    digits, `phone_of` tested only `isdigit()`, and so every one of them was minted
    into a 14-to-16-digit "phone number" that matched no contact and never could. The
    failure is invisible from the inside: an unresolvable phone number looks exactly
    like somebody you have not saved.
    """

    def test_a_lid_does_not_become_a_phone_number(self):
        self.assertIsNone(whatsapp.phone_of("137933361279215@lid"))
        self.assertEqual("whatsapp:lid:137933361279215",
                         whatsapp.handle_of("137933361279215@lid"))

    def test_a_real_number_still_resolves_against_contacts(self):
        """The point of stripping the costume in the first place — one person across
        iMessage and WhatsApp — must survive the fix."""
        identity.link(self.conn, "+19175551234", "Avery Morgan", source="contacts")
        self.conn.commit()
        self.assertEqual("+19175551234", whatsapp.handle_of("19175551234@s.whatsapp.net"))
        self.assertEqual("Avery Morgan",
                         identity.resolve(self.conn, whatsapp.handle_of("19175551234@s.whatsapp.net")))

    def test_whatsapps_own_account_is_not_a_contact(self):
        """`0@s.whatsapp.net` was becoming the handle `+0`."""
        self.assertIsNone(whatsapp.phone_of("0@s.whatsapp.net"))

    def test_the_profile_cache_is_what_makes_a_lid_nameable(self):
        """`ZCONTACTNAME` is empty for every member row in the real store and
        `ZFIRSTNAME` holds a protobuf blob. The push-name table is the only place a
        linked id has a name at all."""
        src = _wa_store()
        self.addCleanup(src.close)
        src.execute("INSERT INTO ZWAPROFILEPUSHNAME VALUES(?,?)",
                    ("256658722824221@lid", "Debbie Smith"))
        self.assertEqual({"256658722824221@lid": "Debbie Smith"},
                         whatsapp.push_names(src))

    def test_an_older_store_without_the_table_is_not_an_error(self):
        src = sqlite3.connect(":memory:")
        self.addCleanup(src.close)
        src.row_factory = sqlite3.Row
        self.assertEqual({}, whatsapp.push_names(src))

    def test_the_minted_numbers_already_in_the_store_are_rewritten(self):
        """Invariant 13: a known-bad inference is repaired, not preserved."""
        for n in range(3):
            archive.append(self.conn, stream="whatsapp", external_id=f"w{n}",
                           ts=db.now(), text="hi", thread="Family",
                           handle="+137933361279215")
        identity.note_unresolved(self.conn, "+137933361279215", "whatsapp")
        threads.record_members(self.conn, "whatsapp", "Family",
                               [("+137933361279215", None)])
        self.conn.commit()
        src = _wa_store(members=[("137933361279215@lid", None)])
        self.addCleanup(src.close)

        self.assertEqual(5, whatsapp.repair_minted_handles(self.conn, src))
        for table in whatsapp.HANDLE_TABLES:
            left = self.conn.execute(
                f"SELECT count(*) AS n FROM {table} WHERE handle = ?",
                ("+137933361279215",)).fetchone()["n"]
            self.assertEqual(0, left, table)
        self.assertEqual(3, self.conn.execute(
            "SELECT count(*) AS n FROM archive WHERE handle = ?",
            ("whatsapp:lid:137933361279215",)).fetchone()["n"])

    def test_the_repair_reads_the_store_rather_than_counting_digits(self):
        """The tempting test is length — a LID runs 14–16 digits and E.164 runs 11–12.
        It is also how you rename somebody's real Ivorian number."""
        archive.append(self.conn, stream="whatsapp", external_id="w1", ts=db.now(),
                       text="hi", thread="Family", handle="+2250700000000")
        self.conn.commit()
        src = _wa_store(members=[("137933361279215@lid", None)])
        self.addCleanup(src.close)
        whatsapp.repair_minted_handles(self.conn, src)
        self.assertEqual("+2250700000000", self.conn.execute(
            "SELECT handle FROM archive WHERE external_id = 'w1'").fetchone()["handle"])

    def test_repairing_twice_changes_nothing(self):
        archive.append(self.conn, stream="whatsapp", external_id="w1", ts=db.now(),
                       text="hi", thread="Family", handle="+137933361279215")
        self.conn.commit()
        src = _wa_store(messages=["137933361279215@lid"])
        self.addCleanup(src.close)
        self.assertTrue(whatsapp.repair_minted_handles(self.conn, src))
        self.assertEqual(0, whatsapp.repair_minted_handles(self.conn, src))

    def test_the_names_reach_the_history_already_archived(self):
        """Honest is not the same as resolved. Everyone who has gone quiet would stay
        nameless forever, and their history is what we wanted them named for."""
        for n in range(2):
            archive.append(self.conn, stream="whatsapp", external_id=f"w{n}",
                           ts=db.now(), text="hi", thread="Family",
                           handle="whatsapp:lid:256658722824221")
        self.conn.commit()
        src = _wa_store()
        self.addCleanup(src.close)
        src.execute("INSERT INTO ZWAPROFILEPUSHNAME VALUES(?,?)",
                    ("256658722824221@lid", "Debbie Smith"))

        self.assertEqual(1, whatsapp.adopt_push_names(self.conn, src))
        self.assertEqual("Debbie Smith",
                         identity.resolve(self.conn, "whatsapp:lid:256658722824221"))
        self.assertEqual(2, self.conn.execute(
            "SELECT count(*) AS n FROM archive WHERE person = 'Debbie Smith'"
        ).fetchone()["n"])

    def test_a_contact_still_beats_the_push_name(self):
        """The user saved them under a name; WhatsApp's is what they chose today."""
        identity.link(self.conn, "+19175550001", "Deborah Smith", source="contacts")
        archive.append(self.conn, stream="whatsapp", external_id="w1", ts=db.now(),
                       text="hi", thread="Family", handle="whatsapp:lid:5")
        self.conn.commit()
        src = _wa_store()
        self.addCleanup(src.close)
        src.execute("INSERT INTO ZWAPROFILEPUSHNAME VALUES(?,?)", ("5@lid", "Deborah Smith"))
        whatsapp.adopt_push_names(self.conn, src)
        self.assertEqual("Deborah Smith", identity.resolve(self.conn, "whatsapp:lid:5"))

    def test_a_cached_name_for_a_stranger_is_not_a_contact(self):
        """The profile cache holds everyone WhatsApp ever cached. Minting a person out
        of one who has never appeared in a conversation is inventing a contact."""
        src = _wa_store()
        self.addCleanup(src.close)
        src.execute("INSERT INTO ZWAPROFILEPUSHNAME VALUES(?,?)", ("99@lid", "A Stranger"))
        self.assertEqual(0, whatsapp.adopt_push_names(self.conn, src))
        self.assertIsNone(identity.resolve(self.conn, "whatsapp:lid:99"))

    def test_naming_twice_changes_nothing(self):
        archive.append(self.conn, stream="whatsapp", external_id="w1", ts=db.now(),
                       text="hi", thread="Family", handle="whatsapp:lid:5")
        self.conn.commit()
        src = _wa_store()
        self.addCleanup(src.close)
        src.execute("INSERT INTO ZWAPROFILEPUSHNAME VALUES(?,?)", ("5@lid", "Frej"))
        self.assertEqual(1, whatsapp.adopt_push_names(self.conn, src))
        self.assertEqual(0, whatsapp.adopt_push_names(self.conn, src))

    def test_a_dm_partner_is_found_too(self):
        """A DM partner never appears in ZWAGROUPMEMBER, so one table is not enough."""
        src = _wa_store(sessions=[("178868929470511@lid", "Someone")])
        self.addCleanup(src.close)
        self.assertEqual({"178868929470511": "lid"}, whatsapp.opaque_jids(src))

    def test_meta_ai_is_not_a_family_member(self):
        """`867051314767696@bot` is all digits too, so WhatsApp's own assistant became
        the phone number `+867051314767696` and was filed as a group member — and it
        had already passed the gate, because "Hey, I'm here for you! \U0001F60A" has a
        question mark in it a line later."""
        self.assertIsNone(whatsapp.phone_of("867051314767696@bot"))
        self.assertEqual("whatsapp:bot:867051314767696",
                         whatsapp.handle_of("867051314767696@bot"))
        self.assertFalse(identity.is_person("whatsapp:bot:867051314767696", "Meta AI"))

    def test_a_bot_namespace_is_repaired_like_a_lid(self):
        archive.append(self.conn, stream="whatsapp", external_id="w1", ts=db.now(),
                       text="hi", thread="Family", handle="+867051314767696")
        self.conn.commit()
        src = _wa_store(members=[("867051314767696@bot", None)])
        self.addCleanup(src.close)
        self.assertEqual(1, whatsapp.repair_minted_handles(self.conn, src))
        self.assertEqual("whatsapp:bot:867051314767696", self.conn.execute(
            "SELECT handle FROM archive WHERE external_id = 'w1'").fetchone()["handle"])


class TestARosterScanOverwroteAJudgement(Base):
    """`handles.source` recorded who wrote a link, not what it rested on.

    `link_by_name` stamped its Contacts-derived match with the bare stream name,
    "groupme". GroupMe's profile sync then saw a source starting with "groupme", read
    it as its own earlier work, and overwrote it — every sync, for every person. 462
    rows in the live store were written by the scan and 3 survived from the ingest
    path, which is the ratio you get when one of them keeps winning.
    """

    def test_a_scan_cannot_replace_contacts(self):
        identity.link(self.conn, "groupme:20141029", "devin reyes", source="contacts")
        self.assertFalse(identity.link(self.conn, "groupme:20141029", "Devin Reyes",
                                       source="groupme:profile"))
        self.assertEqual("devin reyes", identity.resolve(self.conn, "groupme:20141029"))

    def test_a_scan_cannot_replace_a_contact_match(self):
        """The exact overwrite that happened: `contact-match` is Contacts evidence
        reached through a display name, and outranks the platform's own spelling."""
        identity.link(self.conn, "+17326753346", "devin reyes", source="contacts")
        self.conn.commit()
        self.assertEqual("devin reyes", identity.link_by_name(
            self.conn, "groupme:20141029", "Devin Reyes", source="groupme"))
        identity.adopt_seen_name(self.conn, "groupme:20141029", "Devin Reyes",
                                 source="groupme:profile")
        self.assertEqual("devin reyes", identity.resolve(self.conn, "groupme:20141029"))

    def test_a_scan_may_still_revise_its_own_guess(self):
        """Invariant 10's other half. A nickname is beaten by an account name, and
        Contacts re-imports daily, so equal evidence must overwrite."""
        identity.link(self.conn, "groupme:5", "K", source="groupme:roster")
        self.assertTrue(identity.link(self.conn, "groupme:5", "Devin Reyes",
                                      source="groupme:profile"))
        self.assertTrue(identity.link(self.conn, "groupme:5", "Devin J Reyes",
                                      source="groupme:profile"))
        self.assertEqual("Devin J Reyes", identity.resolve(self.conn, "groupme:5"))

    def test_nothing_outranks_him(self):
        identity.link(self.conn, "groupme:5", "Devin Reyes", source="cli")
        for scan in ("groupme:profile", "groupme:roster", "contacts",
                     "platform-roster", "groupme:contact-match"):
            self.assertFalse(identity.link(self.conn, "groupme:5", "Wrong", source=scan),
                             scan)
        self.assertEqual("Devin Reyes", identity.resolve(self.conn, "groupme:5"))

    def test_an_unranked_source_is_treated_as_a_judgement(self):
        """The safe direction: a scan added later cannot silently acquire the right to
        overwrite Contacts — it can only fail to overwrite a nickname until it is
        ranked."""
        self.assertEqual(identity.JUDGEMENT, identity.authority("cli"))
        self.assertEqual(identity.JUDGEMENT, identity.authority("something-new"))
        self.assertLess(identity.authority("groupme:profile"),
                        identity.authority("contacts"))


class TestTwoCasingsOfOneNameAreTwoPeople(Base):
    """`person` is the bundle key, so `Devin Reyes` and `devin reyes` are two humans.

    Three people were split this way in the live store, each with an iMessage half and
    a GroupMe half that could never join — which is the whole point of bundling by
    entity across streams (invariant 3). Nothing asserted that two `handles` rows never
    hold two spellings of one name; that check would have been red the moment the first
    roster synced.
    """

    def test_adopting_a_name_keeps_the_spelling_already_in_use(self):
        identity.link(self.conn, "+17326753346", "devin reyes", source="contacts")
        self.conn.commit()
        self.assertEqual("devin reyes", identity.adopt_seen_name(
            self.conn, "groupme:20141029", "Devin Reyes", source="groupme:profile"))

    def test_no_caller_has_to_remember_to_ask(self):
        """`base.deliver` remembered; GroupMe's profile sync did not. The check lives
        inside `adopt_seen_name` so that forgetting is not possible."""
        identity.link(self.conn, "+14846538673", "sid", source="contacts")
        self.conn.commit()
        identity.note_unresolved(self.conn, "groupme:26815023", "groupme",
                                 seen_name="Sid")
        self.conn.commit()
        identity.adopt_platform_names(self.conn)
        self.assertEqual("sid", identity.resolve(self.conn, "groupme:26815023"))

    def test_the_store_never_holds_two_spellings_of_one_person(self):
        """The invariant itself, over whatever the store actually contains."""
        for handle, person, source in (("+17326753346", "devin reyes", "contacts"),
                                       ("groupme:20141029", "Devin Reyes", "groupme:profile"),
                                       ("+19089670391", "andrew whitehouse", "contacts"),
                                       ("groupme:22231684", "Andrew Whitehouse", "groupme:profile")):
            identity.adopt_seen_name(self.conn, handle, person, source=source)
        self.conn.commit()
        split = self.conn.execute(
            "SELECT lower(person) AS lp, count(DISTINCT person) AS n FROM handles"
            " GROUP BY lp HAVING count(DISTINCT person) > 1").fetchall()
        self.assertEqual([], [row["lp"] for row in split])

    def test_the_splits_already_written_are_folded_back(self):
        """Invariant 13. Stopping new ones does nothing for the three already there."""
        identity.link(self.conn, "+17326753346", "devin reyes", source="contacts")
        self.conn.execute(
            "INSERT INTO handles(handle, person, source, updated_at) VALUES(?,?,?,?)",
            ("groupme:20141029", "Devin Reyes", "groupme:profile", db.now()))
        archive.append(self.conn, stream="groupme", external_id="g1", ts=db.now(),
                       text="hi", thread="Crew", handle="groupme:20141029",
                       person="Devin Reyes")
        self.conn.commit()

        self.assertEqual([("Devin Reyes", "devin reyes")],
                         identity.collapse_split_spellings(self.conn))
        self.assertEqual("devin reyes", identity.resolve(self.conn, "groupme:20141029"))
        self.assertEqual("devin reyes", self.conn.execute(
            "SELECT person FROM archive WHERE external_id = 'g1'").fetchone()["person"])

    def test_the_better_evidence_picks_the_surviving_spelling(self):
        """Contacts beats the platform's display name — the answer issue #2 asked for."""
        self.conn.executemany(
            "INSERT INTO handles(handle, person, source, updated_at) VALUES(?,?,?,?)",
            [("groupme:1", "SID", "groupme:profile", db.now()),
             ("+14846538673", "sid", "contacts", db.now())])
        self.conn.commit()
        identity.collapse_split_spellings(self.conn)
        self.assertEqual("sid", identity.resolve(self.conn, "groupme:1"))

    def test_folding_twice_changes_nothing(self):
        identity.link(self.conn, "+17326753346", "devin reyes", source="contacts")
        self.conn.execute(
            "INSERT INTO handles(handle, person, source, updated_at) VALUES(?,?,?,?)",
            ("groupme:20141029", "Devin Reyes", "groupme:profile", db.now()))
        self.conn.commit()
        self.assertTrue(identity.collapse_split_spellings(self.conn))
        self.assertEqual([], identity.collapse_split_spellings(self.conn))

    def test_an_invisible_mark_is_not_a_second_person(self):
        """WhatsApp wraps a display name in bidi marks, so `"\u200eFoo"` and `"Foo"`
        are two entities that look identical in every log you would read."""
        identity.link(self.conn, "+19175550001", "Morgan", source="contacts")
        self.conn.commit()
        self.assertEqual("Morgan", identity.adopt_seen_name(
            self.conn, "whatsapp:lid:1", "\u202aMorgan\u202c", source="whatsapp:profile"))

    def test_a_platform_field_holding_a_blob_is_not_a_name(self):
        """WhatsApp's push name is occasionally `+GJXsntMGIAE=`. Adopting one makes a
        person whose page is titled with an encoding artefact — and unlike a wrong
        merge, it is not even wrong about anybody."""
        for junk in ("+GJXsntMGIAE=", "+EAA=", "+1 (313) 555-0002"):
            self.assertFalse(identity.name_shaped(junk), junk)
            self.assertIsNone(identity.adopt_seen_name(
                self.conn, "whatsapp:lid:9", junk, source="whatsapp:profile"), junk)
        for real in ("Constantinescu", "Papadopoulos", "Søren T", "O'Brien", "Alex S"):
            self.assertTrue(identity.name_shaped(real), real)

    def test_whatsapps_own_notice_channel_is_not_a_person(self):
        self.assertFalse(identity.is_person("+0", "\u200eWhatsApp"))

def _attributed(body: bytes, *, mutable: bool = True) -> bytes:
    """A typedstream `attributedBody` shaped the way macOS actually writes one.

    Lengths are in **bytes** and use typedstream's varint: below 0x81 the byte is the
    count; 0x81 introduces a two-byte little-endian count.
    """
    if len(body) < 0x81:
        length = bytes([len(body)])
    else:
        length = b"\x81" + len(body).to_bytes(2, "little")
    head = b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString"
    head += b"\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84"
    if mutable:
        head += b"\x0fNSMutableString\x01\x84\x84\x08NSString\x01\x95\x84\x01+"
    else:
        head += b"\x08NSString\x01\x94\x84\x01+"
    return head + length + body + b"\x86\x84\x02iI\x01\x01\x92\x84\x84\x84\x0cNSDictionary\x00"


class TestAMessageBodyWasScrapedInsteadOfParsed(unittest.TestCase):

    def test_the_class_the_scraper_never_knew_about(self):
        self.assertEqual("Also I get to WFH Thursday",
                         imessage.decode_attributed(_attributed(b"Also I get to WFH Thursday")))

    def test_a_plain_nsstring_still_works(self):
        self.assertEqual("we playing at 8?", imessage.decode_attributed(
            _attributed(b"we playing at 8?", mutable=False)))

    def test_an_apostrophe_survives(self):
        """The last-resort scrub deleted every non-ASCII byte, so "It's OK" was
        archived as "It s OK" — the text reached the model with the wrong words."""
        body = "Also I get to WFH Thursday bc it\u2019s my bday".encode()
        self.assertEqual("Also I get to WFH Thursday bc it\u2019s my bday",
                         imessage.decode_attributed(_attributed(body)))

    def test_an_emoji_survives(self):
        body = "dinner at 8 \U0001f355".encode()
        self.assertEqual("dinner at 8 \U0001f355", imessage.decode_attributed(_attributed(body)))

    def test_a_long_body_reads_its_two_byte_length(self):
        """`+\x81\xc9\x04` is 1,225 bytes, not 129. A one-byte read truncates the
        longest messages in the store, which are the ones carrying plans."""
        body = ("Just wanted to update you guys on Maisy. " * 30).encode()
        self.assertGreater(len(body), 0x81)
        self.assertEqual(body.decode().strip(), imessage.decode_attributed(_attributed(body)))

    def test_an_attachment_only_message_has_no_text(self):
        """2,587 of the 2,605 blobs that now decode to empty have
        `cache_has_attachments` set. An object-replacement character is not a message,
        and saying so lets `deliver` drop it as it drops any empty line."""
        self.assertEqual("", imessage.decode_attributed(_attributed("\ufffc".encode())))

    def test_nothing_it_returns_carries_the_serialisation(self):
        """The property the 424 rows violated, stated directly."""
        for body in (b"hi", b"we playing at 8?", "\u2019".encode(), b"a" * 300):
            got = imessage.decode_attributed(_attributed(body))
            for noise in ("NSMutableString", "NSString", "streamtyped", "NSDictionary",
                          "iI", "__kIM"):
                self.assertNotIn(noise, got, f"{noise!r} survived in {got!r}")

    def test_a_blob_it_cannot_read_yields_nothing_rather_than_debris(self):
        """Returning the scrubbed bytes was worse than returning nothing: it archived
        a line that looks like a message and is not one."""
        self.assertEqual("", imessage.decode_attributed(b"\x04\x0bstreamtyped\x81\xe8\x03"))
        self.assertEqual("", imessage.decode_attributed(b""))
        self.assertEqual("", imessage.decode_attributed(None))

    def test_a_message_that_merely_mentions_kim_is_not_debris(self):
        """The first version of the repair selected rows with `text LIKE '%__kIM%'`.
        `_` is a single-character wildcard in SQL LIKE, so that matches "Definitely
        **Kim**'s song". Searching for known-bad output is the same hand-grown list the
        bug was made of; the repair re-derives instead, so there is no pattern to get
        wrong."""
        self.assertNotIn("_", imessage.DECODER_KEY.replace("imessage.decoder_generation", ""))
        self.assertEqual("Definitely Kim\u2019s song", imessage.decode_attributed(
            _attributed("Definitely Kim\u2019s song".encode())))


class TestTheRepairOnlyFoundTheCorruptionItPredicted(Base):
    """Searching the archive for `NSMutableString` found 483 rows. Re-deriving every
    body from the blob found **2,967** — because the commonest debris was
    `@ + WOW iI i & * q`, which contains no class name at all.

    That gap is the argument against a marker list generally: it can only find what
    somebody already thought of, and the thing you are repairing is by definition
    something nobody thought of.
    """

    def _chat_db(self, rows):
        """A stand-in chat.db with only the columns the repair reads."""
        src = sqlite3.connect(":memory:")
        src.row_factory = sqlite3.Row
        src.execute("CREATE TABLE message (guid TEXT, text TEXT, attributedBody BLOB)")
        src.executemany("INSERT INTO message VALUES(?,?,?)", rows)
        return src

    def _archived(self, guid, text):
        rid = archive.append(self.conn, stream="imessage", external_id=guid, ts=db.now(),
                             text=text, thread="Fam", handle="+19175550001", gated=True)
        self.conn.commit()
        return rid

    def test_debris_with_no_class_name_is_still_repaired(self):
        """2,342 of the 2,967 looked like this and no marker list would have seen them."""
        rid = self._archived("G1", "@ + We re really lucky iI i *")
        src = self._chat_db([("G1", None, _attributed("We\u2019re really lucky".encode()))])
        self.addCleanup(src.close)
        self.assertEqual(1, imessage.repair_decoded_text(self.conn, src))
        self.assertEqual("We\u2019re really lucky", self.conn.execute(
            "SELECT text FROM archive WHERE id = ?", (rid,)).fetchone()["text"])

    def test_a_correct_row_is_left_alone(self):
        self._archived("G1", "we playing at 8?")
        src = self._chat_db([("G1", None, _attributed(b"we playing at 8?"))])
        self.addCleanup(src.close)
        self.assertEqual(0, imessage.repair_decoded_text(self.conn, src))

    def test_a_row_whose_source_had_real_text_is_never_touched(self):
        """Only a body this decoder produced can be wrong. A message that arrived with
        `message.text` set was never decoded at all."""
        rid = self._archived("G1", "Definitely Kim\u2019s song")
        src = self._chat_db([("G1", "Definitely Kim\u2019s song", b"junk")])
        self.addCleanup(src.close)
        imessage.repair_decoded_text(self.conn, src)
        self.assertEqual("Definitely Kim\u2019s song", self.conn.execute(
            "SELECT text FROM archive WHERE id = ?", (rid,)).fetchone()["text"])

    def test_an_attachment_only_row_is_emptied_and_ungated_not_deleted(self):
        """Invariant 7. The row keeps the true part — an attachment arrived, here,
        then, from them — and loses the part that was never true."""
        rid = self._archived("G1", "@ NSMutableString + iI i & * q )at_0_G1")
        archive.spool_add(self.conn, rid, "person:Someone")
        self.conn.commit()
        src = self._chat_db([("G1", None, _attributed("\ufffc".encode()))])
        self.addCleanup(src.close)
        self.assertEqual(1, imessage.repair_decoded_text(self.conn, src))
        row = self.conn.execute("SELECT * FROM archive WHERE id = ?", (rid,)).fetchone()
        self.assertEqual("", row["text"])
        self.assertEqual(0, row["gated"])
        self.assertEqual("attachment-only", row["gate_reason"])
        self.assertIsNotNone(row["ts"], "the row itself must survive")
        self.assertEqual(0, self.conn.execute(
            "SELECT count(*) AS n FROM spool WHERE archive_id = ?", (rid,)).fetchone()["n"])

    def test_it_runs_once_per_decoder_generation(self):
        self._archived("G1", "@ + WOW iI i *")
        src = self._chat_db([("G1", None, _attributed(b"WOW"))])
        self.addCleanup(src.close)
        self.assertEqual(1, imessage.repair_decoded_text(self.conn, src))
        self.assertEqual(0, imessage.repair_decoded_text(self.conn, src))
        # A decoder change re-verifies everything, which is the point of a generation.
        db.set_meta(self.conn, imessage.DECODER_KEY, "0")
        self.assertEqual(0, imessage.repair_decoded_text(self.conn, src),
                         "nothing left to fix, but it looked again")

    def test_an_unreadable_chat_db_leaves_the_marker_unset(self):
        """Otherwise one bad run would record the repair as done forever."""
        self._archived("G1", "@ + WOW iI i *")
        broken = sqlite3.connect(":memory:")
        self.addCleanup(broken.close)
        broken.row_factory = sqlite3.Row
        self.assertEqual(0, imessage.repair_decoded_text(self.conn, broken))
        self.assertEqual("", db.get_meta(self.conn, imessage.DECODER_KEY, ""))

        """Returning the scrubbed bytes was worse than returning nothing: it archived
        a line that looks like a message and is not one."""
        self.assertEqual("", imessage.decode_attributed(b"\x04\x0bstreamtyped\x81\xe8\x03"))
        self.assertEqual("", imessage.decode_attributed(b""))
        self.assertEqual("", imessage.decode_attributed(None))

class TestTheIndexAndTheRowDisagreedAboutWhoSaidIt(Base):

    def _row(self, person=None, text="dinner on thursday"):
        rid = archive.append(self.conn, stream="whatsapp", external_id=f"w{text}{person}",
                             ts=db.now(), text=text, thread="Family",
                             handle="whatsapp:lid:5", person=person)
        self.conn.commit()
        return rid

    def _match(self, query):
        return [r["rowid"] for r in self.conn.execute(
            "SELECT rowid FROM archive_fts WHERE archive_fts MATCH ?", (query,))]

    def test_a_renamed_row_is_findable_by_what_it_now_says(self):
        rid = self._row(person=None)
        self.conn.execute("UPDATE archive SET person = 'Rowan' WHERE id = ?", (rid,))
        self.conn.commit()
        self.assertIn(rid, self._match('person:Rowan'))

    def test_the_old_name_stops_matching(self):
        rid = self._row(person="Devin Reyes")
        self.conn.execute("UPDATE archive SET person = 'Avery Morgan' WHERE id = ?", (rid,))
        self.conn.commit()
        self.assertEqual([], self._match('person:Reyes'))
        self.assertIn(rid, self._match('person:Avery'))

    def test_repaired_text_is_findable_too(self):
        """The same trigger covers `text`, which is what the typedstream repair needs."""
        rid = self._row(text="@ NSMutableString +*we playing at 8?")
        self.conn.execute("UPDATE archive SET text = 'we playing at 8?' WHERE id = ?", (rid,))
        self.conn.commit()
        self.assertEqual([], self._match('NSMutableString'))
        self.assertIn(rid, self._match('playing'))

    def test_the_two_checks_that_reported_success_while_it_was_broken(self):
        """Kept as documentation: neither of these can fail, which is the whole point.

        A test that asserts a passing `integrity-check` proves the fix would have been
        green before the fix existed."""
        rid = self._row(person=None)
        self.conn.execute("UPDATE archive SET person = 'Rowan' WHERE id = ?", (rid,))
        self.conn.commit()
        # Reads through to the content table -- agrees whether or not the index does.
        self.assertEqual(1, self.conn.execute(
            "SELECT count(*) AS n FROM archive_fts WHERE person = 'Rowan'").fetchone()["n"])
        # Validates the index against itself -- passes either way.
        self.conn.execute("INSERT INTO archive_fts(archive_fts) VALUES('integrity-check')")
        # So the assertion that actually bites is the MATCH one, above.
        self.assertIn(rid, self._match('person:Rowan'))

class TestASuiteThatIsGreenOnlyInOneTimeZone(unittest.TestCase):
    """Keep the timezone sweep able to run the suite under every UTC offset."""

    def test_the_instrument_can_still_sweep_zones(self):
        """A guard on the tool, in the spirit of `TestNoTestCanReachTheRealCalendar`
        asserting it still has something to check. If `--zones` is ever removed, this
        axis goes back to being invisible and nothing else would say so."""
        sweep = (Path(__file__).resolve().parent.parent / "tools" / "clock_sweep.py")
        source = sweep.read_text(encoding="utf-8")
        self.assertIn("--zones", source)
        self.assertIn('env["TZ"] = zone', source,
                      "the zone has to reach the subprocess, or --zones is decorative")
        self.assertIn("UTC", source, "UTC is the one a CI runner uses")

    def test_the_calendar_round_trip_no_longer_asks_the_machine(self):
        """The narrow version, on the class it happened to: its fixtures must build
        their offset rather than state it.

        Read through `ast` rather than as text, so the prose explaining the offset does
        not count as writing one — the first draft of this check failed on its own
        comment, which is the smaller cousin of the bug it is guarding.
        """
        offset = re.compile(r"\d{2}:\d{2}(:\d{2})?\s*[-+]\d{2}:?\d{2}")
        tree = ast.parse((Path(__file__).with_name("test_regressions.py")).read_text(
            encoding="utf-8"))
        target = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)
                      and n.name == "TestAMultiDaySpanSurvivesTheCalendarRoundTrip")
        literals = [sub.value for sub in ast.walk(target)
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str)]
        self.assertEqual([], [v for v in literals if offset.search(v)])
        self.assertTrue(any("2026-04-19T23:59:59" == v for v in literals),
                        "the fixture is still here to be checked")
        self.assertIn("astimezone", ast.dump(target))


class TestOneNameOnTwoPlatformsIsTwoPeopleForever(Base):

    def _said(self, person, thread, stream="groupme", n=1):
        for i in range(n):
            archive.append(self.conn, stream=stream,
                           external_id=f"{stream}{person}{thread}{i}", ts=db.now(),
                           text="hi", thread=thread, handle=f"{stream}:{person}",
                           person=person)
        identity.link(self.conn, f"{stream}:{person}", person, source="cli")
        self.conn.commit()

    def test_a_unique_stem_across_two_streams_is_worth_asking_about(self):
        self._said("Rohan", "DMs", stream="imessage")
        self._said("Rohan Kapoor", "The Crew", stream="groupme")
        self.assertEqual([("Rohan", "Rohan Kapoor")],
                         identity.merge_candidates(self.conn))

    def test_two_people_who_have_spoken_in_one_conversation_are_two_people(self):
        """`Casey` and `Casey Iverson` both speak in Alumni chat. This is the
        signal Contacts does not have, derived in code with no model call."""
        self._said("Casey", "Alumni chat")
        self._said("Casey Iverson", "Alumni chat")
        self.assertEqual([], identity.merge_candidates(self.conn))

    def test_silence_is_not_absence(self):
        """`Joe` matches seven names and six of them have never sent a message. Count
        only speakers and one match is left standing, which makes `Joe` look
        unambiguous — the exact wrong merge `adopt_seen_name` documents."""
        self._said("Joe", "The Crew")
        self._said("joe coleman", "DMs", stream="imessage")
        # A silent namesake, of which the live store has five more.
        identity.link(self.conn, "groupme:9", "Joe Navarro", source="cli")
        self.conn.commit()
        self.assertEqual([], identity.merge_candidates(self.conn))

    def test_a_name_that_has_never_spoken_costs_nothing_to_leave_alone(self):
        self._said("Nik", "DMs", stream="imessage")
        identity.link(self.conn, "groupme:8", "Nik Pavincic", source="cli")
        self.conn.commit()
        self.assertEqual([], identity.merge_candidates(self.conn))

    def test_a_stem_may_not_split_a_word(self):
        """`P S` matching `Peyton` is how the seven wrong merges happened."""
        self.assertTrue(identity._name_stem("Nik", "Nik Pavincic"))
        self.assertFalse(identity._name_stem("Nik", "Nikita Popov"))
        self.assertFalse(identity._name_stem("P", "Peyton"))

    def test_it_shows_rather_than_merging(self):
        """Nothing here writes a link. The user does, with `memcal who`."""
        self._said("Rohan", "DMs", stream="imessage")
        self._said("Rohan Kapoor", "The Crew", stream="groupme")
        lines = identity.candidate_lines(self.conn)
        self.assertEqual(1, len(lines))
        self.assertIn("Rohan", lines[0])
        # and the two are still two, until the user says otherwise
        self.assertEqual("Rohan", identity.resolve(self.conn, "imessage:Rohan"))
        self.assertEqual("Rohan Kapoor",
                         identity.resolve(self.conn, "groupme:Rohan Kapoor"))

    def test_it_never_becomes_a_question_in_the_brief(self):
        """The user was shown *'Is "Mullin" the same person as Drew Lane…?'* and dismissed it
        — "why do u really care haha…. Creeeeeeepy" — and it is one of the five in
        `TestQuestionsWorthAsking.DISMISSED`. `NOT_WORTH_ASKING` has had a branch for
        that sentence ever since, under "memcal's own homework, not their life".

        So the first version of this feature, which opened a question per pair, was
        re-proposing something already refused. Invariant 5's "becomes a question" does
        not have to mean *asked*.
        """
        self._said("Rohan", "DMs", stream="imessage")
        self._said("Rohan Kapoor", "The Crew", stream="groupme")
        identity.candidate_lines(self.conn)
        self.assertEqual(0, self.conn.execute(
            "SELECT count(*) AS n FROM questions").fetchone()["n"])
        self.assertFalse(hasattr(identity, "ask_about_candidates"),
                         "asking is the thing the user said no to")

    def test_the_sentence_he_dismissed_is_still_refused(self):
        """Belt and braces: whatever wording this feature ever grows, the shape the user
        rejected must stay unaskable."""
        self.assertFalse(todos.is_worth_asking(
            'Is "Mullin" the same person as Drew Lane, who you paid $100 for dinner?'))

    def test_it_never_asks_whether_he_is_himself(self):
        identity.set_me(self.conn, "Casey Morgan")
        self._said("Casey Morgan", "DMs", stream="imessage")
        self._said("Casey Morgan Jr", "The Crew", stream="groupme")
        self.assertEqual([], identity.merge_candidates(self.conn))

class TestTheNightlyPassPutItBackEveryNight(Base):

    def _poker(self, location="42 Example Street"):
        events.upsert(self.conn, {"title": "Poker night", "date": self.d(0),
                                  "kind": "commitment", "series": "poker-night",
                                  "location": location}, written_by="dream:nightly")

    def _where(self):
        return (wiki.read(self.cfg.wiki_dir, "poker-night").slots.get("where") or {})

    def test_a_correction_survives_the_next_pass(self):
        self._poker()
        wiki.ensure_series(self.conn, self.cfg.wiki_dir, "poker-night")
        wiki.set_slot(self.cfg.wiki_dir, "poker-night", "where", "the frat house",
                      source="you", section="projects", conn=self.conn)

        wiki.ensure_series(self.conn, self.cfg.wiki_dir, "poker-night")
        self.assertEqual("the frat house", self._where().get("value"))
        self.assertEqual("you", self._where().get("source"))

    def test_it_survives_being_put_back_every_night_for_a_week(self):
        """It reverted on *every* pass, so once is not the assertion."""
        self._poker()
        wiki.set_slot(self.cfg.wiki_dir, "poker-night", "where", "the frat house",
                      source="you", section="projects", conn=self.conn)
        for _ in range(7):
            wiki.ensure_series(self.conn, self.cfg.wiki_dir, "poker-night")
        self.assertEqual("the frat house", self._where().get("value"))

    def test_the_inferred_value_is_written_through_the_history(self):
        """Invariant 4. The page keeps what is true now and the row keeps what it said."""
        self._poker()
        wiki.ensure_series(self.conn, self.cfg.wiki_dir, "poker-night")
        self.assertEqual([(None, "42 Example Street", "memcal")],
                         [(r["old_value"], r["new_value"], r["source"])
                          for r in wiki.slot_history(self.conn, "poker-night")])

    def test_an_inference_leaves_its_question_open(self):
        """Invariant 5. Deleting the question deletes the only route to a correction."""
        self._poker()
        page = wiki.ensure_series(self.conn, self.cfg.wiki_dir, "poker-night")
        self.assertIn("where?", [q.lower() for q in page.questions])

    def test_a_stated_value_still_closes_its_question(self):
        """The other half — `inferred` is what distinguishes them, not the slot."""
        self._poker()
        wiki.ensure_series(self.conn, self.cfg.wiki_dir, "poker-night")
        page = wiki.set_slot(self.cfg.wiki_dir, "poker-night", "where",
                             "the frat house", source="you", section="projects",
                             conn=self.conn)
        self.assertNotIn("where?", [q.lower() for q in page.questions])

    def test_the_rule_owns_where_it_happens_not_the_newest_instance(self):
        """Invariant 12. A series that has moved says so in its rule; the instances are
        the fallback for a series observed before it was ever declared."""
        self._poker(location="42 Example Street")
        series.upsert(self.conn, {"slug": "poker-night", "title": "Poker night",
                                  "cadence": "weekly", "weekday": 2,
                                  "location": "the new place",
                                  "effective_on": self.d(0)}, written_by="live")
        wiki.ensure_series(self.conn, self.cfg.wiki_dir, "poker-night")
        self.assertEqual("the new place", self._where().get("value"))

    def test_a_re_derivation_may_still_revise_itself(self):
        """The guard is about overruling *somebody else*, not about never updating."""
        self._poker(location="42 Example Street")
        wiki.ensure_series(self.conn, self.cfg.wiki_dir, "poker-night")
        events.upsert(self.conn, {"title": "Poker night", "date": self.d(7),
                                  "kind": "commitment", "series": "poker-night",
                                  "location": "9 Other Road"}, written_by="dream:nightly")
        wiki.ensure_series(self.conn, self.cfg.wiki_dir, "poker-night")
        self.assertEqual("9 Other Road", self._where().get("value"))

    def test_nothing_else_writes_a_slot_behind_set_slots_back(self):
        """The property, not the instance. `set_slot` is where history and precedence
        live, so a direct assignment anywhere is this bug waiting to happen again."""
        offenders = []
        for path in sorted(Path(wiki.__file__).parent.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Subscript):
                    continue
                target = ast.unparse(node)
                if re.search(r"\.slots\[", target):
                    parent_is_store = any(
                        isinstance(a, ast.Assign) and node in ast.walk(a)
                        for a in ast.walk(tree))
                    if parent_is_store and path.name != "wiki.py":
                        offenders.append(f"{path.name}:{node.lineno} {target}")
        self.assertEqual([], offenders)

class TestAToolCanBeAdvertisedAndUnreachable(Base):

    def _server(self):
        """`Server.__init__` loads the real config; bind this one to the test store."""
        server = object.__new__(mcp_server.Server)
        server.conn, server.cfg = self.conn, self.cfg
        return server

    def test_every_advertised_tool_actually_routes(self):
        """The property both bugs violated, over whatever `TOOLS` says today."""
        server = self._server()
        unroutable = []
        for tool in mcp_server.TOOLS:
            try:
                server.call(tool["name"], {})
            except (ValueError, UnboundLocalError) as exc:
                if "unknown tool" in str(exc) or isinstance(exc, UnboundLocalError):
                    unroutable.append(f"{tool['name']}: {type(exc).__name__} {exc}")
            except Exception:
                pass          # a real handler objecting to empty args is fine
        self.assertEqual([], unroutable)

    def test_memcal_open_reaches_the_one_assembler(self):
        """`detail.open_handle` is what the CLI, the web UI and Hermes all read. This
        surface threw instead, so three surfaces agreed and the fourth had a traceback."""
        event, _ = events.upsert(self.conn, {
            "title": "Poker at Robbie's", "date": self.d(2), "kind": "commitment",
            "status": "confirmed"}, written_by="live")
        out = self._server().call("memcal_open", {"ref": f"E{event.id}"})
        self.assertIn("Poker", out)

    def test_the_two_lists_of_write_tools_cannot_drift(self):
        """`WRITE_TOOLS` is the single declaration; `_write` refuses anything outside it
        and `call` routes on it, so there is no second list to fall out of step."""
        self.assertEqual(9, len(mcp_server.WRITE_TOOLS),
                         "a green loop over an empty set proves nothing")
        for name in sorted(mcp_server.WRITE_TOOLS):
            self.assertIn(name, [t["name"] for t in mcp_server.TOOLS],
                          f"{name} is routable but not advertised")

    def test_an_unknown_write_no_longer_writes_an_alias(self):
        """`_write` ended in a bare fallthrough, so any name reaching it that matched
        nothing quietly added a wiki alias."""
        with self.assertRaises(ValueError):
            self._server()._write("memcal_not_a_tool", {"page": "x", "name": "y"})

    def test_a_rule_can_be_written_from_this_surface(self):
        """Invariant 12, on the surface that could not express it."""
        events.upsert(self.conn, {"title": "Tutoring", "date": self.d(1),
                                  "kind": "commitment"}, written_by="live")
        out = self._server().call("memcal_schedule",
                                  {"which": "Tutoring", "cadence": "weekly", "weekday": 1})
        self.assertIn("Tutoring", out)
        self.assertIsNotNone(series.get(self.conn, "tutoring"))

class TestTheNightlyJobCouldNotReportFailing(Base):

    def _run(self, ingest: int, dream: int) -> int:
        """The script's tail, with the two commands stubbed to chosen exit codes."""
        script = schedule.render_script(self.cfg)
        tail = script[script.index("echo \"=== $(date"):]
        tail = tail.replace('"$PY" -m memcal ingest all', f"(exit {ingest})")
        tail = tail.replace('"$PY" -m memcal dream --mode nightly', f"(exit {dream})")
        return subprocess.run(["sh", "-c", tail], capture_output=True).returncode

    def test_a_failing_dream_is_reported(self):
        self.assertEqual(1, self._run(ingest=0, dream=1))

    def test_a_failing_ingest_is_reported(self):
        self.assertEqual(2, self._run(ingest=2, dream=0))

    def test_a_clean_run_is_still_zero(self):
        self.assertEqual(0, self._run(ingest=0, dream=0))

    def test_the_script_does_not_end_on_an_echo(self):
        """The property, stated where the next edit will see it."""
        body = [line for line in schedule.render_script(self.cfg).strip().splitlines()
                if line.strip() and not line.strip().startswith("#")]
        self.assertTrue(body[-1].startswith("exit "), body[-1])

    def test_the_catchup_script_still_does_the_same(self):
        """It was already right, and is the reason this was findable."""
        body = [line for line in schedule.render_catchup_script(self.cfg).strip().splitlines()
                if line.strip() and not line.strip().startswith("#")]
        self.assertTrue(body[-1].startswith("exit "), body[-1])


class TestAFinishedJobLookedBusyForTwentyFiveSeconds(unittest.TestCase):
    """`job.done = True` was set bare in a `finally`, without the lock and without a bump.

    Every other mutator on `_Job` — `say`, `plan`, `step` — increments `version` and
    calls `notify_all`. This one changed the flag and woke nobody, so
    `wait_for_change` returned only on its 25-second timeout: the Gather/Dream button
    stayed disabled and the bar stayed mid-flight for 25s after the work had finished.

    The error path was worse. `job.error` is also set silently, so a job that had failed
    looked identical to one still running, for the same 25 seconds.
    """

    def test_the_done_frame_arrives_when_the_job_does(self):
        job = web_jobs._Job("probe")
        frames, start = [], time.monotonic()

        def reader():
            seen = 0
            for _ in range(3):
                snap = job.wait_for_change(seen, timeout=5.0)
                frames.append((time.monotonic() - start, snap["done"]))
                seen = snap["version"]
                if snap["done"]:
                    return

        thread = threading.Thread(target=reader)
        thread.start()
        time.sleep(0.1)
        with job.lock:                      # exactly what `start_job.run` does now
            job.done = True
            job._bump()
        thread.join(timeout=8)

        self.assertTrue(frames, "the reader never woke at all")
        elapsed, done = frames[-1]
        self.assertTrue(done)
        self.assertLess(elapsed, 2.0,
                        "the finished job was not announced; it timed out instead")

    def test_finishing_bumps_the_version(self):
        """The mechanical half: a reader that missed the frame can still tell."""
        job = web_jobs._Job("probe")
        before = job.version
        with job.lock:
            job.done = True
            job._bump()
        self.assertGreater(job.version, before)

    def test_start_job_sets_it_under_the_lock(self):
        """Read as text, because the bug is what the line does *not* do and only the
        source says so."""
        source = Path(web_jobs.__file__).read_text(encoding="utf-8")
        run = source[source.index("def start_job("):]
        run = run[:run.index("\ndef ")]
        self.assertIn("job._bump()", run)
        self.assertNotIn("\n            job.done = True\n\n", run)

class TestASpanThatEndsBeforeItStartsDeletesItselfFromTheBrief(Base):

    def setUp(self):
        super().setUp()
        db.set_today("2026-09-01")
        self.addCleanup(db.set_today, None)

    def test_an_inverted_span_is_dropped_not_stored(self):
        event, _ = live.add_event(self.conn, self.cfg, title="Trip",
                                  when="2026-09-01", until="2026-08-01")
        self.assertIsNone(event.until)

    def test_the_row_still_reaches_the_brief(self):
        """The consequence, not the field. Dropping `until` keeps the row; keeping it
        hid the row from the window query on both ends."""
        live.add_event(self.conn, self.cfg, title="Trip",
                       when="2026-09-01", until="2026-08-01")
        self.assertIn("Trip", [e.title for e in events.window(self.conn, 30, 30)])

    def test_a_real_span_is_untouched(self):
        event, _ = live.add_event(self.conn, self.cfg, title="Real trip",
                                  when="2026-09-01", until="2026-09-05")
        self.assertEqual("2026-09-05", event.until)
        self.assertIn("Real trip", [e.title for e in events.window(self.conn, 30, 30)])

    def test_an_update_that_swaps_the_ends_cannot_hide_the_row(self):
        """The fat-fingered `memcal_update`, which is how this reaches a user."""
        live.add_event(self.conn, self.cfg, title="Trip",
                       when="2026-09-01", until="2026-09-05")
        live.update_event(self.conn, self.cfg, "Trip", when="2026-09-05",
                          until="2026-09-01")
        self.assertIn("Trip", [e.title for e in events.window(self.conn, 30, 30)])

    def test_an_unparseable_until_is_dropped_too(self):
        event, _ = events.upsert(self.conn, {"title": "Trip", "date": "2026-09-01",
                                             "until": "not a date",
                                             "kind": "commitment"}, written_by="cli")
        self.assertIsNone(event.until)

    def test_the_guard_is_at_the_choke_point_not_at_one_caller(self):
        """`upsert` is what every writer goes through; `apply` remembering was the bug."""
        event, _ = events.upsert(self.conn, {"title": "Direct", "date": "2026-09-01",
                                             "until": "2026-08-01",
                                             "kind": "commitment"}, written_by="cli")
        self.assertIsNone(event.until)


class TestOneColumnHeldThreeTimeFormats(Base):
    """Store calendar start times in one canonical UTC representation."""

    #: The two files allowed to touch this column, and the marker of a statement that
    #: compares or writes it. `_rebind` reads it as a hash input and is deliberately not
    #: matched — it wants the stored bytes, not an interpretation of them.
    COLUMN_FILES = ("memcal/sources/ical.py", "memcal/sources/providers/partiful.py")
    TOUCHES = ("starts_at=excluded.starts_at", "starts_at >= ?", "starts_at < ?",
               "starts_at = ?")

    def setUp(self):
        super().setUp()
        # These fixtures publish a rule whose first occurrence is deliberately seven
        # days ahead.  The benchmark module runs before this one under discovery and
        # pins its own scenario day, so borrowing that process-global clock can make
        # the rule look already expired.  The test's calendar is its own fixture.
        db.set_today("2026-08-20")
        self.addCleanup(db.set_today, None)

    def _iso_z(self, stamp: datetime) -> str:
        """What `toISOString()` would print, built without the code under test."""
        utc = stamp.astimezone(timezone.utc)
        return (utc.strftime("%Y-%m-%dT%H:%M:%S.")
                + f"{utc.microsecond // 1000:03d}Z")

    def _local(self, day: str, hour: int, minute: int = 0) -> datetime:
        """A wall-clock moment on this machine, whatever zone this machine is in."""
        return datetime.combine(db.parse_date(day),
                                dt_time(hour, minute)).astimezone()

    def _runner(self, replies):
        def run(cmd, **kwargs):
            body = replies.pop(0) if replies else "{}"
            return type("Done", (), {"returncode": 0, "stdout": body, "stderr": ""})()

        return run

    def _item(self, **over):
        base_item = {"calendar_name": "Home", "calendar_uid": "cal-home",
                     "writable": True, "uid": "u-1", "title": "Bridge club",
                     "all_day": False, "location": "", "description": "", "url": ""}
        return {**base_item, **over}

    def _starts(self) -> list[str]:
        return [row["starts_at"] for row in
                self.conn.execute("SELECT starts_at FROM calendar_items")]

    # -- the three writers ---------------------------------------------------

    def test_the_scan_stores_what_the_calendar_said_unaltered(self):
        """Normalising the scan has to be a no-op or every recurring identity moves."""
        moment = self._local(self.d(7), 19, 30)
        self.conn.execute("DELETE FROM calendar_items")
        ical.ingest_snapshot(
            self.conn, self.cfg,
            [self._item(start=self._iso_z(moment), end=self._iso_z(moment))],
            scan_start=self.d(-1), scan_end=self.d(30))
        self.assertEqual(self._starts(), [self._iso_z(moment)])

    def test_publishing_a_row_stores_the_instant_and_not_the_local_clock(self):
        self.cfg.publish_calendar = "memcal"
        moment = self._local(self.d(7), 19, 30)
        event, _ = events.upsert(self.conn, {
            "title": "Bridge club", "date": self.d(7), "time": "19:30",
            "kind": "commitment", "status": "confirmed"}, written_by="live")
        ical.publish(self.conn, self.cfg, event,
                     runner=self._runner(['{"uid": "u-p", "calendar": "memcal",'
                                          ' "calendar_uid": "cal-1"}']))
        self.assertEqual(self._starts(), [self._iso_z(moment)])

    def test_publishing_a_rule_stores_the_instant_too(self):
        self.cfg.publish_calendar = "memcal"
        moment = self._local(self.d(7), 19, 30)
        rule, _ = series.upsert(self.conn, {
            "slug": "bridge", "title": "Bridge club", "cadence": "weekly",
            "weekday": moment.weekday(), "time": "19:30",
            # Anchored on the day itself, so the first occurrence ahead of the rule is
            # the one this test is about rather than whichever weekday comes first.
            "effective_on": self.d(7)}, written_by="cli")
        ical.publish_series(self.conn, self.cfg, rule,
                            runner=self._runner(['{"uid": "u-s", "calendar": "memcal",'
                                                 ' "created": true, "status": 3}']))
        self.assertEqual(self._starts(), [self._iso_z(moment)])

    def test_all_three_writers_spell_one_moment_one_way(self):
        """The bug, in a single assertion: three paths, one instant, one string."""
        self.cfg.publish_calendar = "memcal"
        moment = self._local(self.d(7), 19, 30)
        event, _ = events.upsert(self.conn, {
            "title": "Bridge club", "date": self.d(7), "time": "19:30",
            "kind": "commitment", "status": "confirmed"}, written_by="live")
        ical.publish(self.conn, self.cfg, event,
                     runner=self._runner(['{"uid": "u-p", "calendar": "memcal",'
                                          ' "calendar_uid": "cal-1"}']))
        rule, _ = series.upsert(self.conn, {
            "slug": "bridge", "title": "Bridge club", "cadence": "weekly",
            "weekday": moment.weekday(), "time": "19:30",
            "effective_on": self.d(7)}, written_by="cli")
        ical.publish_series(self.conn, self.cfg, rule,
                            runner=self._runner(['{"uid": "u-s", "calendar": "memcal",'
                                                 ' "created": true, "status": 3}']))
        ical.ingest_snapshot(
            self.conn, self.cfg,
            [self._item(uid="u-scan", start=self._iso_z(moment),
                        end=self._iso_z(moment))],
            scan_start=self.d(-1), scan_end=self.d(30))
        stored = self._starts()
        self.assertEqual(len(stored), 3, "one row per writer, or this proves nothing")
        self.assertEqual(set(stored), {self._iso_z(moment)})
        for value in stored:
            back = datetime.fromisoformat(value)
            self.assertIsNotNone(back.tzinfo, "a stored instant carries its zone")
            self.assertEqual(back, moment, "and round-trips to the moment written")

    # -- what the notation was costing ---------------------------------------

    def test_a_published_occurrence_is_re_adopted_rather_than_duplicated(self):
        self.cfg.publish_calendar = "memcal"
        moment = self._local(self.d(7), 19, 30)
        event, _ = events.upsert(self.conn, {
            "title": "Bridge club", "date": self.d(7), "time": "19:30",
            "kind": "commitment", "status": "confirmed"}, written_by="live")
        ical.publish(self.conn, self.cfg, event,
                     runner=self._runner(['{"uid": "u-rec", "calendar": "memcal",'
                                          ' "calendar_uid": "cal-home"}']))
        item = self._item(uid="u-rec", recurrence="FREQ=WEEKLY;BYDAY=TU",
                          location="", start=self._iso_z(moment),
                          end=self._iso_z(moment))
        self.assertIsNone(
            self.conn.execute("SELECT 1 FROM calendar_items WHERE identity = ?",
                              (ical._identity(item),)).fetchone(),
            "the occurrence must arrive under an identity the store has never held")

        ical.ingest_snapshot(self.conn, self.cfg, [item],
                             scan_start=self.d(-1), scan_end=self.d(30))

        rows = self.conn.execute(
            "SELECT identity, event_key FROM calendar_items").fetchall()
        self.assertEqual(len(rows), 1, "the prior row was adopted, not duplicated")
        self.assertEqual(rows[0]["identity"], ical._identity(item))
        self.assertEqual(rows[0]["event_key"], event.key)
        self.assertEqual(
            len([e for e in events.window(self.conn, 0, 30)
                 if e.title == "Bridge club"]), 1,
            "memcal read its own published copy back in as a second event")

    def test_a_published_rule_is_never_adopted_as_an_occurrence(self):
        """`series:<slug>` is bookkeeping for a rule and names no `events.key`.

        Matching the notations is what makes this reachable at all: the row `publish_series`
        writes now sorts alongside the occurrences a scan reports for the same uid. Adopt
        it and `fields["key"]` becomes a string no event answers to; delete it and the
        record that stops a repeating event being read back in fifty times is gone.
        """
        moment = self._local(self.d(7), 19, 30)
        item = self._item(uid="u-rule", recurrence="FREQ=WEEKLY;BYDAY=TU",
                          start=self._iso_z(moment), end=self._iso_z(moment))
        self.conn.execute(
            """INSERT INTO calendar_items(identity, calendar_uid, calendar_name,
                   event_uid, event_key, starts_at, subscribed, provider, active,
                   published, published_state, revision, last_seen_at, updated_at)
               VALUES('identity-of-the-rule','','memcal','u-rule',
                      'series:bridge',?,0,'ical',1,1,'s',NULL,?,?)""",
            (db.utc_stamp(moment), db.now(), db.now()))
        self.conn.commit()

        ical.ingest_snapshot(self.conn, self.cfg, [item],
                             scan_start=self.d(-1), scan_end=self.d(30))

        kept = self.conn.execute(
            "SELECT identity FROM calendar_items WHERE event_key = 'series:bridge'"
        ).fetchone()
        self.assertIsNotNone(kept, "the rule's own record was deleted by an occurrence")
        self.assertEqual(kept["identity"], "identity-of-the-rule")
        self.assertFalse(
            [row for row in events.window(self.conn, 0, 30)
             if row.key.startswith("series:")],
            "a rule's bookkeeping key leaked into events.key")

    def test_an_evening_at_either_edge_of_the_window_is_inside_it(self):
        """Both edges, because which one slips depends on the machine's zone.

        East of UTC an event just after local midnight buckets onto the previous UTC
        day and falls off the *start* of the window; west of it a late-evening event
        buckets onto the next UTC day and falls off the *end*. Comparing an instant
        against a bare local date is wrong in one direction or the other everywhere.
        """
        first, last = self.d(-3), self.d(2)
        moments = [self._local(first, 0, 30), self._local(first, 23, 30),
                   self._local(last, 0, 30), self._local(last, 23, 30)]
        self.assertEqual(len(moments), 4, "nothing to judge means nothing is proven")
        for index, moment in enumerate(moments):
            event, _ = events.upsert(self.conn, {
                "title": f"Edge {index}", "date": moment.date().isoformat(),
                "kind": "commitment", "status": "confirmed"}, written_by="live")
            self.conn.execute(
                """INSERT INTO calendar_items(identity, calendar_uid, calendar_name,
                       event_uid, event_key, starts_at, subscribed, provider, active,
                       published, revision, last_seen_at, updated_at)
                   VALUES(?,'cal-home','Home',?,?,?,0,'ical',1,0,NULL,?,?)""",
                (f"edge-{index}", f"uid-{index}", event.key, db.utc_stamp(moment),
                 # Last seen before today, or absence is a same-day refresh.
                 self.d(-1), db.now()))
        self.conn.commit()

        report = base.IngestReport(stream="ical")
        # A snapshot that read *something* — an identity this store does not hold. An
        # empty one is a failed read and is judged nowhere, which would make every
        # assertion below pass without the window bounds being right at all.
        ical.reconcile_deleted(self.conn, seen={"some-other-identity"},
                               seen_uids={"some-other-uid"},
                               scan_start=first, scan_end=self.d(3), report=report)
        still_active = [row["identity"] for row in self.conn.execute(
            "SELECT identity FROM calendar_items WHERE active = 1")]
        self.assertEqual(still_active, [],
                         "an event inside the scanned window was never judged")

    # -- and the reason the form is what it is -------------------------------

    def test_the_form_is_a_fixed_point_of_the_calendars_own(self):
        """`_identity` hashes the scan's string and `_rebind` reads it back out.

        So the canonical form is not a matter of taste: it has to leave what the JXA
        sends byte-identical, or an account migration recomputes every recurring
        identity from a string that is not the one that was hashed.
        """
        for hour in (0, 4, 12, 23):
            raw = self._iso_z(self._local(self.d(3), hour, 30))
            self.assertEqual(db.utc_stamp(raw), raw)
            self.assertEqual(db.utc_stamp(db.utc_stamp(raw)), raw)

    def test_the_notations_that_were_in_the_store_all_land_on_one_string(self):
        moment = self._local(self.d(7), 19, 30)
        naive = moment.replace(tzinfo=None).isoformat(timespec="seconds")
        self.assertEqual(
            {db.utc_stamp(self._iso_z(moment)),          # the scan
             db.utc_stamp(naive),                        # publish
             db.utc_stamp(moment.isoformat(timespec="seconds"))},   # publish_series
            {self._iso_z(moment)})

    def test_a_bare_day_is_read_as_local_midnight_and_empty_stays_empty(self):
        day = db.parse_date(self.d(4))
        self.assertEqual(db.utc_stamp(self.d(4)),
                         self._iso_z(self._local(self.d(4), 0)))
        self.assertEqual(db.utc_stamp(day), db.utc_stamp(self.d(4)))
        self.assertEqual(db.utc_stamp(""), "")

    def test_something_that_is_not_a_timestamp_is_never_turned_into_now(self):
        """`parse_ts` answers `now()` for junk, which is right for a read and not here.

        Inventing a moment nothing observed would put a wrong instant in the store and,
        because `_identity` hashes this string, would re-key the row that carried it.
        """
        for junk in ("not a date", "Busy", "2026-13-40T99:99"):
            self.assertEqual(db.utc_stamp(junk), junk)

    def test_no_statement_that_compares_or_writes_this_column_skips_it(self):
        """A fourth writer is what put the third notation there; this is the guard.

        Read as text rather than exercised, because the failure is a *new* code path
        and no test can be written in advance for one that does not exist yet.
        """
        root = Path(__file__).resolve().parent.parent
        touching = []
        for name in self.COLUMN_FILES:
            source = (root / name).read_text(encoding="utf-8")
            tree = ast.parse(source)
            for fn in [n for n in ast.walk(tree)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                body = ast.get_source_segment(source, fn) or ""
                if any(marker in body for marker in self.TOUCHES):
                    # Real calls, not the word. `"utc_stamp" in body` reads the
                    # docstring and the comments too, so the sentence explaining why a
                    # bound is converted satisfies the check that it was — and two of
                    # the five below carry exactly that sentence.
                    calls = sum(1 for node in ast.walk(fn) if isinstance(node, ast.Call)
                                and getattr(node.func, "attr",
                                            getattr(node.func, "id", None)) == "utc_stamp")
                    touching.append((name, fn.name, calls))
        self.assertEqual(
            sorted((name, fn) for name, fn, _ in touching),
            [("memcal/sources/ical.py", "ingest_snapshot"),
             ("memcal/sources/ical.py", "publish"),
             ("memcal/sources/ical.py", "publish_series"),
             ("memcal/sources/ical.py", "reconcile_deleted"),
             ("memcal/sources/providers/partiful.py", "reconcile_missing")],
            "the set of statements touching starts_at moved; re-read them all")
        self.assertEqual(
            [f"{name}:{fn}" for name, fn, calls in touching if not calls], [],
            "compares or writes calendar_items.starts_at without db.utc_stamp")



def setUpModule():
    db.set_today(None)


def tearDownModule():
    db.set_today(None)


if __name__ == "__main__":
    unittest.main()
