"""The diagnostic surface, and the two decisions it is allowed to make.

Everything here is deterministic — no model, no server socket. The HTTP layer is a
thin wrapper over these functions, so testing the functions tests the page.
"""

from __future__ import annotations

import ast
import json
import plistlib
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import (archive, brief, db, events, gate, identity, llm, schedule,
                    threads, todos, trace, web, web_dream, web_jobs, web_memory,
                    web_queue, web_server, wiki)  # noqa: E402
from memcal.config import Config  # noqa: E402
from memcal.dream import bundle as bundle_stage  # noqa: E402
from memcal.dream import propose as propose_stage  # noqa: E402
from memcal.dream import run as dream_run  # noqa: E402


class TestWebFacadePreservesOwnershipBoundaries(unittest.TestCase):
    def test_facade_reexports_the_split_implementations(self):
        self.assertIs(web.items, web_queue.items)
        self.assertIs(web.memory, web_memory.memory)
        self.assertIs(web.dream_preview, web_dream.dream_preview)
        self.assertIs(web.start_job, web_jobs.start_job)
        self.assertIs(web.serve, web_server.serve)

    def test_lower_layers_do_not_import_upward(self):
        def imports(module):
            tree = ast.parse(Path(module.__file__).read_text())
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    names.add(node.module or "")
                    if node.module is None:
                        names.update(alias.name for alias in node.names)
            return names

        upward = {"web", "web_server", "web_jobs", "memcal.web",
                  "memcal.web_server", "memcal.web_jobs"}
        for module in (web_queue, web_memory, web_dream):
            self.assertTrue(imports(module).isdisjoint(upward))
        self.assertTrue(imports(web_jobs).isdisjoint(
            {"web", "web_server", "memcal.web", "memcal.web_server"}))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def ts(self, offset: int = 0) -> str:
        return (db.today() + timedelta(days=offset)).isoformat() + "T12:00:00"

    def mail(self, address: str, subject: str, *, gated: bool, reason: str,
             offset: int = 0, spool: bool = None, processed: bool = False) -> int:
        aid = archive.append(
            self.conn, stream="email", external_id=f"{address}:{subject}:{offset}",
            ts=self.ts(offset), text=subject, thread=address, handle=address,
            gated=gated, gate_reason=reason, meta={"subject": subject},
        )
        if spool is None:
            spool = gated
        if spool:
            archive.spool_add(self.conn, aid, f"person:{address}")
            if processed:
                # A run_id is what makes this a read rather than a retirement.
                self.conn.execute(
                    "UPDATE spool SET processed_at = ?, run_id = 1 WHERE archive_id = ?",
                    (db.now(), aid))
        self.conn.commit()
        return aid


class TestQueueState(Base):
    """The gate's verdict and what actually happened to the item are two columns."""

    def test_a_retirement_is_not_a_read(self):
        """`processed_at` means both 'a run consumed this' and 'it was pulled from the
        queue unread'. Only `run_id` separates them, and calling a retirement a read
        overstates what the model has seen by the whole backlog."""
        aid = self.mail("x@x.com", "never read", gated=True, reason="unknown-sender")
        archive.spool_retire(self.conn, db.today().isoformat())
        self.assertEqual(web.items(self.conn)["items"][0]["state"], "queued")

        self.conn.execute("UPDATE archive SET ts = ? WHERE id = ?", (self.ts(-90), aid))
        archive.spool_retire(self.conn, db.today().isoformat())
        self.assertEqual(web.items(self.conn)["items"][0]["state"], "retired")

    def test_four_states_are_distinguished(self):
        self.mail("a@x.com", "read one", gated=True, reason="unknown-sender", processed=True)
        self.mail("b@x.com", "waiting", gated=True, reason="unknown-sender")
        self.mail("c@x.com", "skipped", gated=False, reason="automated-address")
        # Passed the gate, but arrived before the spool horizon, so nothing ever read it.
        self.mail("d@x.com", "too old", gated=True, reason="unknown-sender",
                  offset=-90, spool=False)

        states = {i["subject"]: i["state"] for i in web.items(self.conn)["items"]}
        self.assertEqual(states["read one"], "read")
        self.assertEqual(states["waiting"], "queued")
        self.assertEqual(states["skipped"], "skipped")
        self.assertEqual(states["too old"], "dropped")

    def test_live_writes_are_not_reported_as_missed(self):
        """The live path never queues anything. Reading that as a backlog would be a
        bug report about the one thing working as designed."""
        archive.append(self.conn, stream="agent", external_id="live-1", ts=self.ts(),
                       text="dinner tuesday with alex", gated=True, gate_reason="live")
        self.conn.commit()
        self.assertEqual(web.items(self.conn)["items"][0]["state"], "live")

    def test_structured_calendar_writes_are_not_reported_as_skipped(self):
        archive.append(
            self.conn,
            stream="ical",
            external_id="calendar-1",
            ts=self.ts(),
            text="Dinner — calendar: Partiful (subscribed)",
            gated=False,
            gate_reason="calendar-structured",
        )
        self.conn.commit()
        item = web.items(self.conn)["items"][0]
        self.assertEqual(item["state"], "structured")
        self.assertEqual(web.items(self.conn, verdict="structured")["total"], 1)
        self.assertEqual(web.items(self.conn, verdict="skipped")["total"], 0)
        stream = {row["stream"]: row for row in web.overview(
            self.conn, self.cfg)["streams"]}["ical"]
        self.assertEqual(stream["structured"], 1)

    def test_legacy_calendar_snapshot_marker_is_not_a_gate_item(self):
        archive.append(
            self.conn,
            stream="ical",
            external_id="snapshot:2026-07-30",
            ts=self.ts(),
            text="Calendar snapshot completed: 119 event(s)",
            gated=False,
            gate_reason="calendar-structured",
        )
        self.conn.commit()
        self.assertEqual(web.items(self.conn)["total"], 0)
        self.assertEqual(web.groups(self.conn)["groups"], [])
        self.assertEqual(web.overview(self.conn, self.cfg)["streams"], [])


class TestCounterparts(Base):
    def test_own_messages_name_who_they_are_with(self):
        for i, (from_me, person, text) in enumerate([
            (0, "Harper", "u free tomorrow?"),
            (1, None, "yeah after 6"),
        ]):
            archive.append(self.conn, stream="imessage", external_id=f"m{i}",
                           ts=self.ts(), text=text, thread="+15551234567",
                           person=person, from_me=bool(from_me), gated=True,
                           gate_reason="temporal")
        self.conn.commit()
        who = {i["preview"]: i["who"] for i in web.items(self.conn)["items"]}
        self.assertEqual(who["u free tomorrow?"], "Harper")
        self.assertEqual(who["yeah after 6"], "me → Harper")

    def test_a_group_is_named_as_one(self):
        for i, (from_me, person) in enumerate([(0, "Ann"), (0, "Bo"), (1, None)]):
            archive.append(self.conn, stream="groupme", external_id=f"g{i}",
                           ts=self.ts(), text=f"line {i}", thread="chat-9",
                           person=person, from_me=bool(from_me), gated=True,
                           gate_reason="temporal")
        self.conn.commit()
        mine = [i for i in web.items(self.conn)["items"] if i["from_me"]][0]
        self.assertEqual(mine["who"], "me → group of 2")


class TestFilters(Base):
    def setUp(self):
        super().setUp()
        self.mail("keep@x.com", "poker friday", gated=True, reason="unknown-sender")
        self.mail("junk@x.com", "50% off", gated=False, reason="bulk-headers")
        self.mail("junk@x.com", "70% off", gated=False, reason="bulk-headers")

    def test_verdict_and_reason_filter(self):
        self.assertEqual(web.items(self.conn, verdict="passed")["total"], 1)
        self.assertEqual(web.items(self.conn, verdict="skipped")["total"], 2)
        self.assertEqual(web.items(self.conn, reason="bulk-headers")["total"], 2)

    def test_search_covers_sender_as_well_as_text(self):
        self.assertEqual(web.items(self.conn, q="poker")["total"], 1)
        self.assertEqual(web.items(self.conn, q="junk@")["total"], 2)

    def test_reason_chips_survive_choosing_one(self):
        """Facets ignore the reason filter, or clicking a chip empties the chip row
        and there is no way back without a reload."""
        picked = web.items(self.conn, reason="bulk-headers")
        self.assertEqual({r["reason"] for r in picked["reasons"]},
                         {"unknown-sender", "bulk-headers"})


class TestRollup(Base):
    """Opening a rolled-up conversation has to show that conversation's lines."""

    def chat(self, thread: str, texts: list[str], *, stream: str = "imessage") -> None:
        for i, text in enumerate(texts):
            archive.append(self.conn, stream=stream, external_id=f"{thread}:{i}",
                           ts=self.ts(), text=text, thread=thread,
                           handle=None if i % 2 else "+15550001111",
                           person="Quinn" if i % 2 else None, from_me=bool(i % 2),
                           gated=True, gate_reason="temporal")
        self.conn.commit()

    def test_a_group_opens_onto_its_own_lines(self):
        """The key of an unnamed group chat is an opaque identifier that appears in none
        of its messages. Filtered as a search it matched nothing, so every group in the
        rollup expanded to 'no matching lines' — 569 of them, in one real archive."""
        self.chat("9858b62c161544bca4342589e0344bbe", ["are we still on", "yes 7pm"])
        self.chat("+16467570654", ["unrelated dm"])

        key = web.groups(self.conn)["groups"][0]["key"]
        self.assertEqual(key, "9858b62c161544bca4342589e0344bbe")
        opened = web.items(self.conn, group=key, stream="imessage")
        self.assertEqual(opened["total"], 2)
        self.assertEqual({i["preview"] for i in opened["items"]},
                         {"are we still on", "yes 7pm"})

    def test_opening_a_dm_keeps_my_own_half_of_it(self):
        """`handle` is null on messages I sent, so a search for the DM's key dropped
        every line I wrote — the count on the row and the lines under it disagreed."""
        self.chat("+16467570654", ["u free tomorrow?", "yeah after 6"])
        row = web.groups(self.conn)["groups"][0]
        opened = web.items(self.conn, group=row["key"], stream="imessage")
        self.assertEqual(opened["total"], row["n"])
        self.assertEqual(len([i for i in opened["items"] if i["from_me"]]), 1)

    def test_a_key_is_matched_whole_and_not_as_a_substring(self):
        self.chat("+1646757065", ["short number"])
        self.chat("+16467570654", ["longer number"])
        opened = web.items(self.conn, group="+1646757065", stream="imessage")
        self.assertEqual([i["preview"] for i in opened["items"]], ["short number"])

    def test_email_rolls_up_and_opens_by_address(self):
        self.mail("nytdirect@nytimes.com", "morning briefing", gated=False, reason="bulk-headers")
        self.mail("nytdirect@nytimes.com", "evening briefing", gated=False, reason="bulk-headers")
        row = web.groups(self.conn, stream="email")["groups"][0]
        self.assertEqual(row["key"], "nytdirect@nytimes.com")
        self.assertEqual(web.items(self.conn, group=row["key"], stream="email")["total"], 2)

    def test_an_open_group_still_honours_the_search_box(self):
        """The rollup counts are computed with the search applied, so the lines under a
        row have to be too, or the row says 1 and lists 2."""
        self.chat("chat-9", ["poker friday", "no thanks"], stream="groupme")
        row = web.groups(self.conn, q="poker")["groups"][0]
        opened = web.items(self.conn, group=row["key"], stream="groupme", q="poker")
        self.assertEqual(opened["total"], row["n"])
        self.assertEqual([i["preview"] for i in opened["items"]], ["poker friday"])


class TestSubjectGate(Base):
    """The subject line is free, and it is what the address test could not see."""

    def gate(self, address: str, subject: str, **kw):
        return gate.gate_email(self.conn, address=address, subject=subject, **kw)

    def test_an_appointment_survives_a_noreply_address(self):
        v = self.gate("noreply@e.headway.co",
                      "Reminder: Your appointment with Harper is in 1 hour")
        self.assertTrue(v.passed)
        self.assertEqual(v.reason, "subject-event")

    def test_a_delivery_survives_an_orders_address(self):
        for subject in ("📦 [Delivered] Casey, your Whatnot order has arrived!",
                        "Your Elements 2026 Passes Are On The Way!",
                        "Hooray! Your order ending in 3107 was picked up."):
            self.assertTrue(self.gate("orders@oe.target.com", subject).passed, subject)

    def test_a_list_he_subscribed_to_can_still_invite_him(self):
        """Riders Alliance mails through a bulk sender. Being on a list the user chose to be on
        is not evidence the user does not want to hear that they are holding a gala."""
        head = {"List-Unsubscribe": "<https://x/unsub>"}
        v = self.gate("events@ridersalliance.org",
                      "Save the Date for the Riders Alliance Gala! 🎉", headers=head)
        self.assertTrue(v.passed)
        # …and the newsletter from the same address on the same day still does not.
        self.assertFalse(self.gate("events@ridersalliance.org",
                                   "Why we're not standing with the mayor today",
                                   headers=head).passed)

    def test_a_list_posting_outranks_any_subject(self):
        """Found by tools/benchmark_temporal.py, 2026-07-28.

        "Reminder: AWS Summit NYC networking night is tomorrow" passed on `reminder:`
        before `Precedence: bulk` was ever consulted, and the model duly put a
        conference party on their calendar. The subject cannot be the discriminator here:
        it is lexically identical to "reminder: poker is tomorrow". `List-Id` and
        `Precedence: bulk` are, so they are checked first.
        """
        head = {"List-Unsubscribe": "<https://x/unsub>",
                "List-ID": "<campaigns.awsevents.amazonses.com>",
                "Precedence": "bulk", "Auto-Submitted": "auto-generated"}
        v = self.gate("no-reply@awsevents.amazonses.com",
                      "Reminder: AWS Summit NYC networking night is tomorrow",
                      headers=head)
        self.assertFalse(v.passed)
        self.assertEqual(v.reason, "bulk-headers")

    def test_list_unsubscribe_alone_does_not_outrank_a_subject(self):
        """The counterweight, and why the tiers exist. Gmail and Yahoo now require
        List-Unsubscribe of anyone sending at volume, so a neighbourhood nonprofit
        carries it alongside every retailer — it means "you can get off this list", not
        "this went to a list instead of to you"."""
        head = {"List-Unsubscribe": "<https://x/unsub>"}
        self.assertTrue(self.gate("events@ridersalliance.org",
                                  "Save the Date for the Gala! 🎉", headers=head).passed)

    def test_an_event_update_from_a_machine_still_arrives(self):
        """Partiful. `Auto-Submitted` is true of every appointment reminder, delivery
        notice and invite there is, so it sits below the subject — and "Updated:" is a
        change notice, which is the one thing about an event most worth reading."""
        head = {"Auto-Submitted": "auto-generated", "X-Auto-Response-Suppress": "All"}
        v = self.gate("invites@partiful.com", "Updated: Devon's Block Party BBQ",
                      headers=head)
        self.assertTrue(v.passed)
        self.assertEqual(v.reason, "subject-event")

    def test_a_bare_update_in_a_subject_is_not_a_change_notice(self):
        # Anchored at the start with a colon. "Update to your benefits" is a newsletter.
        self.assertFalse(gate.subject_is_event("Update to your benefits"))
        self.assertFalse(gate.subject_is_event("An update on our privacy policy"))
        self.assertTrue(gate.subject_is_event("Updated: dinner is at 8"))

    def test_the_second_newsletter_is_blocked_on_its_own_headers(self):
        """Not on whether the table happened to learn the sender from the first."""
        head = {"List-ID": "<campaigns.x.com>", "Precedence": "bulk"}
        first = self.gate("news@x.com", "Reminder: our gala is tomorrow", headers=head)
        second = self.gate("news@x.com", "Reminder: our other gala is tomorrow",
                           headers=head)
        self.assertFalse(first.passed)
        self.assertFalse(second.passed)

    def test_a_sale_is_not_an_event_however_it_is_worded(self):
        for subject in ("Last chance — 40% off tickets", "Your Thursday Briefing",
                        "Deals on everything you saved", "New arrivals just landed"):
            self.assertFalse(gate.subject_is_event(subject), subject)

    def test_the_gates_own_guess_is_reopened_but_his_is_not(self):
        """The distinction the whole design turns on."""
        auto = "noreply@e.headway.co"
        self.gate(auto, "Update to your benefits")          # archived on the address
        self.assertEqual(identity.sender_decision(self.conn, auto), "archive")
        self.assertTrue(self.gate(auto, "Reminder: appointment with Harper on 7/27").passed)

        said_no = "aws-marketing@amazon.com"
        identity.set_sender(self.conn, said_no, "ignore", "don't care about AWS",
                            source="agent")
        v = self.gate(said_no, "You're invited: AWS Summit New York, register now")
        self.assertFalse(v.passed)
        self.assertEqual(v.reason, "blocked:agent")


class TestBlocking(Base):
    """One verb for "I don't care about this", wherever it came from."""

    def test_blocking_a_sender_is_permanent_and_retires_the_queue(self):
        self.mail("aws@amazon.com", "AWS Summit NYC", gated=True, reason="unknown-sender")
        out = web.block(self.conn, self.cfg,
                        {"address": "aws@amazon.com", "by": "agent",
                         "reason": "the user said the user doesn't care about AWS"})
        self.assertEqual(out["blocked"], "aws@amazon.com")
        self.assertEqual(out["retired"], 1)
        self.assertTrue(identity.sender_blocked(self.conn, "aws@amazon.com"))

    def test_blocking_a_chat_mutes_it(self):
        archive.append(self.conn, stream="groupme", external_id="g1", ts=self.ts(),
                       text="zoom call about the api in 3 days", thread="Dev Chat",
                       gated=True, gate_reason="temporal")
        self.conn.commit()
        out = web.block(self.conn, self.cfg,
                        {"stream": "groupme", "thread": "Dev Chat", "by": "agent"})
        self.assertEqual(out["blocked"], "groupme/Dev Chat")
        self.assertTrue(threads.is_muted(self.conn, "groupme", "Dev Chat"))

    def test_the_gates_own_archiving_is_not_a_block(self):
        """Or every bulk sender the gate ever filed would be beyond the subject test."""
        identity.set_sender(self.conn, "news@x.com", "archive", "bulk-headers")
        self.assertFalse(identity.sender_blocked(self.conn, "news@x.com"))

    def test_blocking_needs_something_to_block(self):
        self.assertIn("error", web.block(self.conn, self.cfg, {"by": "you"}))
        self.assertIn("error", web.block(self.conn, self.cfg,
                                         {"address": "a@b.com", "by": "nobody"}))


class TestSenderTable(Base):
    def test_a_decision_the_current_gate_disagrees_with_is_flagged(self):
        """The table is consulted before every other signal, so a decision made by an
        older build is never revisited on its own."""
        identity.set_sender(self.conn, "travel@m.livekindred.com", "process", "old build")
        self.mail("travel@m.livekindred.com", "new hosting opportunities",
                  gated=True, reason="sender-table:process")
        identity.set_sender(self.conn, "jordan@example.com", "process", "unknown-sender-default")
        self.mail("jordan@example.com", "poker friday", gated=True, reason="sender-table:process")

        rows = {s["address"]: s for s in web.senders(self.conn)}
        self.assertTrue(rows["travel@m.livekindred.com"]["disagrees"])
        self.assertFalse(rows["jordan@example.com"]["disagrees"])

    def test_flipping_to_ignore_retires_what_is_still_queued(self):
        self.mail("spam@x.com", "one", gated=True, reason="unknown-sender")
        self.mail("spam@x.com", "two", gated=True, reason="unknown-sender")
        out = web.set_sender(self.conn, self.cfg, "spam@x.com", "ignore")

        self.assertEqual(out["retired"], 2)
        self.assertEqual(identity.sender_decision(self.conn, "spam@x.com"), "ignore")
        self.assertEqual(len(archive.spool_pending(self.conn)), 0)
        # Retiring is not deleting: the rows stay in the archive and stay searchable.
        self.assertEqual(web.items(self.conn)["total"], 2)

    def test_backfill_queues_past_mail_but_only_inside_the_horizon(self):
        self.mail("party@x.com", "recent invite", gated=False, reason="bulk-headers")
        self.mail("party@x.com", "ancient invite", gated=False, reason="bulk-headers",
                  offset=-(self.cfg.spool_horizon_days + 10))
        out = web.set_sender(self.conn, self.cfg, "party@x.com", "process", backfill=True)

        self.assertEqual(out["queued"], 1)
        pending = archive.spool_pending(self.conn)
        self.assertEqual([r["text"] for r in pending], ["recent invite"])

    def test_flipping_does_not_rewrite_what_the_gate_decided(self):
        """That record is the whole point of the page — the queue carries the override."""
        self.mail("spam@x.com", "one", gated=True, reason="unknown-sender")
        web.set_sender(self.conn, self.cfg, "spam@x.com", "ignore")
        item = web.items(self.conn)["items"][0]
        self.assertEqual(item["reason"], "unknown-sender")
        self.assertTrue(item["gated"])
        self.assertEqual(item["state"], "retired")

    def test_unknown_decision_is_refused(self):
        self.assertIn("error", web.set_sender(self.conn, self.cfg, "a@x.com", "delete"))


class TestQueueOneItem(Base):
    def test_queue_and_retire_a_single_item(self):
        aid = self.mail("x@x.com", "goat volunteering", gated=False, reason="bulk-headers")
        self.assertEqual(web.queue_item(self.conn, self.cfg, aid, "queue")["state"], "queued")
        self.assertEqual(web.queue_item(self.conn, self.cfg, aid, "skip")["state"], "retired")

    def test_requeueing_something_already_read_releases_it(self):
        aid = self.mail("x@x.com", "read already", gated=True, reason="unknown-sender",
                        processed=True)
        self.assertEqual(web.items(self.conn)["items"][0]["state"], "read")
        self.assertEqual(web.queue_item(self.conn, self.cfg, aid, "queue")["state"], "queued")

    def test_beyond_the_horizon_is_refused_rather_than_silently_dropped(self):
        aid = self.mail("x@x.com", "ancient", gated=False, reason="bulk-headers",
                        offset=-(self.cfg.spool_horizon_days + 5))
        out = web.queue_item(self.conn, self.cfg, aid, "queue")
        self.assertIn("horizon", out["error"])
        self.assertEqual(len(archive.spool_pending(self.conn)), 0)

    def test_unknown_item_and_action_are_refused(self):
        self.assertIn("error", web.queue_item(self.conn, self.cfg, 999999, "queue"))
        aid = self.mail("x@x.com", "a", gated=False, reason="no-signal")
        self.assertIn("error", web.queue_item(self.conn, self.cfg, aid, "burn"))


class TestRunHistory(Base):
    def test_a_dry_run_is_not_the_last_pass(self):
        """It is recorded — pricing a run is the point of it — but it wrote nothing by
        design, and shown as the last pass it reads as one that found nothing."""
        for mode in ("nightly", "dry-run"):
            self.conn.execute(
                "INSERT INTO runs(started_at, mode, model, bundles) VALUES(?,?,?,?)",
                (db.now(), mode, "m", 4))
        self.conn.commit()
        self.assertEqual(web.overview(self.conn, self.cfg)["last_run"]["mode"], "nightly")
        # Still listed, so the price you were quoted stays findable.
        self.assertIn("dry-run", [r["mode"] for r in web.runs(self.conn)])

    def test_no_runs_at_all_is_not_an_error(self):
        self.assertIsNone(web.overview(self.conn, self.cfg)["last_run"])


class TestOverview(Base):
    def test_counts_and_json_round_trip(self):
        self.mail("a@x.com", "one", gated=True, reason="unknown-sender")
        self.mail("b@x.com", "two", gated=False, reason="bulk-headers")
        out = web.overview(self.conn, self.cfg)
        stream = {s["stream"]: s for s in out["streams"]}["email"]
        self.assertEqual((stream["n"], stream["gated"]), (2, 1))
        self.assertEqual(out["pending"], 1)
        # The handler serialises whatever these return; a stray Row or date would 500.
        for payload in (out, web.memory(self.conn, self.cfg),
                        web.items(self.conn), {"runs": web.runs(self.conn)},
                        {"senders": web.senders(self.conn)}):
            json.dumps(payload)


class TestSchedule(Base):
    def launchctl(self, code: int, out: str = ""):
        """Answer for launchd. `_launchctl` reads returncode, stdout and stderr."""
        class Answer:
            returncode, stdout, stderr = code, out, ""

        return lambda *_a, **_kw: Answer()

    def test_plist_runs_the_script_nightly(self):
        data = schedule.render_plist(self.cfg, hour=3, minute=0)
        self.assertEqual(data["StartCalendarInterval"], {"Hour": 3, "Minute": 0})
        # argv[0] *is* the script — not `/bin/sh <script>`. macOS lists a background
        # item by the filename it executes, and the old shape put two entries called
        # `sh` in Login Items with nothing identifying either as memcal.
        self.assertEqual(data["ProgramArguments"],
                         [str(schedule.script_path(self.cfg))])
        self.assertFalse(data["RunAtLoad"])
        # Must survive a real plist round-trip, or launchd rejects the job at load.
        self.assertEqual(plistlib.loads(plistlib.dumps(data)), data)

    def test_script_ingests_before_dreaming(self):
        script = schedule.render_script(self.cfg, python="/usr/bin/python3")
        self.assertLess(script.index("ingest all"), script.index("dream --mode nightly"))
        self.assertIn(f'export MEMCAL_HOME="{self.cfg.home}"', script)
        # The script owns its log handle so trimming cannot orphan launchd's.
        self.assertIn('exec >> "$LOG" 2>&1', script)

    def test_calendar_probe_uses_the_nightly_pinned_python(self):
        script = schedule.script_path(self.cfg)
        script.write_text(schedule.render_script(
            self.cfg, python=sys.executable), encoding="utf-8")
        self.assertEqual(schedule.pinned_python(self.cfg), sys.executable)

    def test_status_reports_a_missing_install_rather_than_raising(self):
        # Named for the behaviour and, until #49, testing the host instead: it called
        # the real `launchctl list`, so it raised `FileNotFoundError` off a Mac and read
        # the developer's own launchd on one. `runner=` answers for launchd.
        st = schedule.status(self.cfg, runner=self.launchctl(3, "Could not find service"))
        self.assertIn("installed", st)
        self.assertIsNone(st["log"])


class TestDreamPreview(Base):
    """Reading the pass out of OpenRouter's logs should not be how bugs get found."""

    def spool_line(self, person: str, text: str, *, mine: bool, offset: int = 0,
                   gated: bool = True) -> int:
        aid = archive.append(
            self.conn, stream="imessage", external_id=f"{person}:{text}:{offset}",
            ts=self.ts(offset), text=text, thread=f"thread-{person}", handle=person,
            person=person, from_me=mine, gated=gated, gate_reason="test",
        )
        if gated:
            archive.spool_add(self.conn, aid, f"person:{person}")
        self.conn.commit()
        return aid

    def test_preview_claims_nothing(self):
        self.spool_line("Quinn", "poker friday?", mine=False)
        before = self.conn.execute(
            "SELECT count(*) n FROM spool WHERE processed_at IS NULL").fetchone()["n"]
        web.dream_preview(self.conn, self.cfg)
        after = self.conn.execute(
            "SELECT count(*) n FROM spool WHERE processed_at IS NULL").fetchone()["n"]
        self.assertEqual(before, after, "previewing must not consume the spool")

    def test_it_carries_the_text_the_model_will_actually_get(self):
        self.spool_line("Quinn", "poker friday?", mine=False)
        card = web.dream_preview(self.conn, self.cfg)["bundles"][0]
        self.assertIn("poker friday?", card["text"])
        self.assertTrue(card["text"].startswith("BUNDLE ") or "BUNDLE " in card["text"])

    def test_a_bundle_of_only_your_own_words_is_flagged(self):
        # Only the user's half of a Hermes turn is spooled, so these bundles read as a
        # monologue. Unflagged, that looks like data loss rather than a design choice.
        for i, line in enumerate(["hi bud", "i just did tutoring", "heh meow"]):
            self.spool_line("me", line, mine=True, offset=-i)
        card = [b for b in web.dream_preview(self.conn, self.cfg)["bundles"]
                if b["label"] == "me"][0]
        self.assertTrue(card["monologue"])
        self.assertEqual(card["mine"], card["count"])

    def test_a_two_sided_bundle_is_not_flagged(self):
        self.spool_line("Quinn", "poker friday?", mine=False)
        self.spool_line("Quinn", "yeah I'm in", mine=True, offset=-1)
        card = web.dream_preview(self.conn, self.cfg)["bundles"][0]
        self.assertFalse(card["monologue"])

    def test_gate_rejected_neighbours_are_marked_as_context(self):
        # add_thread_context pulls these in for readability; they are not why the
        # bundle exists, and the page has to be able to say which is which.
        self.spool_line("Quinn", "poker friday?", mine=False, offset=0)
        self.spool_line("Quinn", "ok", mine=True, offset=0, gated=False)
        items = web.dream_preview(self.conn, self.cfg)["bundles"][0]["items"]
        by_text = {i["text"]: i for i in items}
        self.assertFalse(by_text["poker friday?"]["context"])
        if "ok" in by_text:                     # only if the window picked it up
            self.assertTrue(by_text["ok"]["context"])

    def test_a_backlog_behind_the_page_cap_is_reported(self):
        for i in range(7):
            self.spool_line("Quinn", f"line {i}", mine=False, offset=-i)
        spool = web.dream_preview(self.conn, self.cfg, limit=3)["spool"]
        self.assertEqual(spool["taken"], 3)
        self.assertEqual(spool["pending"], 7)
        # The tail of a conversation that *is* being read. Nothing is unreached: with one
        # conversation waiting, it gets the whole budget.
        self.assertEqual(spool["left_behind"], 4)
        self.assertEqual(spool["unreached"], 0)

    def test_one_loud_conversation_cannot_crowd_out_the_others(self):
        # The bug this replaces: `ORDER BY ts DESC LIMIT 500` meant their partner and their
        # best friend filled the window, and the other hundred-odd conversations fell off
        # the end without appearing anywhere as dropped.
        for i in range(40):
            self.spool_line("Harper", f"chatter {i}", mine=False, offset=-i)
        self.spool_line("Mom", "are you coming sunday?", mine=False, offset=-5)
        labels = {b["label"] for b in
                  web.dream_preview(self.conn, self.cfg, limit=6)["bundles"]}
        self.assertIn("Mom", labels)
        self.assertIn("Harper", labels)

    def test_the_preview_reads_what_the_run_would_read(self):
        """A preview whose budget is not the run's budget is a different pass. The HTTP
        layer defaulted this to 500 and clamped it to 500, so their largest conversation —
        1,508 lines, and the whole reason to look at the page — previewed as eight."""
        for i in range(200):
            self.spool_line("Harper", f"line {i}", mine=False, offset=-(i % 20))
        self.cfg.item_budget = 20_000
        self.cfg.items_per_entity = 2_000

        card = web.dream_preview(self.conn, self.cfg)["bundles"][0]
        self.assertEqual(card["count"], 200)
        self.assertEqual(card["waiting"], 0)
        # An explicit limit is still honoured — it is a cheaper preview, on request.
        small = web.dream_preview(self.conn, self.cfg, limit=25)["bundles"][0]
        self.assertEqual(small["count"], 25)
        self.assertEqual(small["waiting"], 175)

    def test_output_ceilings_are_shown_per_request(self):
        self.spool_line("Quinn", "poker friday?", mine=False)
        req = web.dream_preview(self.conn, self.cfg)["requests"][0]
        # 1200 + 900 per bundle + 8 per line, and thinking is spent from the same
        # allowance. The line term is what keeps a 1,500-line bundle from being given
        # less room to report than six ten-line ones.
        base = 1200 + 900 * req["bundles"] + 8 * req["items"]
        # The preview has to show the ceiling the configured model will actually get,
        # not the base formula: a model that reasons its way through the allowance is
        # given a multiple of it — and a floor beneath that, because thinking is largely
        # a per-request cost that a multiplier cannot reach at the small end. A preview
        # quoting the base number would under-report what the run is about to buy.
        spec = llm.endpoint(self.cfg.propose_model)
        self.assertEqual(req["max_tokens"],
                         min(32000, max(int(base * spec.ceiling_boost),
                                        spec.think_tokens * max(1, req["bundles"]))))
        self.assertGreater(req["input_tokens"], 0)

    def test_a_big_bundle_gets_more_room_than_a_small_one(self):
        """The allowance used to count bundles only, so the single conversation with
        everything in it was the one given the least room to say anything about it."""
        small = propose_stage.output_ceiling([bundle_stage.Bundle(entity="person:Mom")])
        big = bundle_stage.Bundle(entity="person:Harper")
        big.items = [None] * 400
        self.assertGreater(propose_stage.output_ceiling([big]), small)

    def test_past_saturation_is_read_off_the_record_not_guessed(self):
        cur = self.conn.execute(
            """INSERT INTO runs(started_at, mode, model, bundles, items)
               VALUES(?, 'test', 'm', 1, 1)""", (db.now(),))
        run_id = cur.lastrowid
        # (completion, the ceiling that call was given)
        for out, cap in ((10200, 10200), (4711, 8000), (2100, 2100), (900, 0)):
            self.conn.execute(
                """INSERT INTO generations(run_id, generation_id, stage, label, model,
                                           prompt_tokens, completion_tokens, cost_usd,
                                           max_tokens, created_at)
                   VALUES(?, ?, 'propose', 'x', 'm', 100, ?, 0.0, ?, ?)""",
                (run_id, f"gen-{out}", out, cap, db.now()))
        self.conn.commit()
        budget = web.dream_preview(self.conn, self.cfg)["budget"]
        self.assertEqual(budget["calls"], 4)
        # Two ended on the ceiling they were actually given; 4711 stopped well short.
        self.assertEqual(budget["saturated"], 2)
        self.assertEqual(budget["at"], [2100, 10200])
        # The one recorded before the column existed is not guessed at either way.
        self.assertEqual(budget["unrecorded"], 1)

    def test_an_empty_spool_previews_without_raising(self):
        p = web.dream_preview(self.conn, self.cfg)
        self.assertEqual(p["bundles"], [])
        self.assertEqual(p["requests"], [])
        self.assertFalse(p["cost"]["priced"])


class TestBundleIsReadable(Base):
    """The card has to say which conversations it is made of, and which are groups."""

    def line(self, *, thread: str, person: str, text: str, group: bool,
             stream: str = "groupme", offset: int = 0) -> None:
        aid = archive.append(
            self.conn, stream=stream, external_id=f"{thread}:{text}:{offset}",
            ts=self.ts(offset), text=text, thread=thread, handle=f"h:{person}",
            person=person, from_me=False, meta={"group": group},
            gated=True, gate_reason="test",
        )
        archive.spool_add(self.conn, aid, f"person:{person}")
        self.conn.commit()

    def test_a_group_line_is_marked_as_one(self):
        self.line(thread="Alumni Chat", person="parker", text="yo ravers, show friday",
                  group=True)
        card = web.dream_preview(self.conn, self.cfg)["bundles"][0]
        self.assertTrue(card["items"][0]["group"])
        self.assertTrue(card["conversations"][0]["group"])

    def test_a_bundle_says_how_many_conversations_it_joined(self):
        # This is the Parker Shaw card: one name, lines from three different places,
        # one of them a group chat of thirty people.
        self.line(thread="Parker Shaw", person="parker", text="tech question if youre free",
                  group=False)
        self.line(thread="Alumni Chat", person="parker", text="yo ravers", group=True)
        self.line(thread="+15551234567", person="parker", text="you on palworld?",
                  group=False, stream="imessage")
        card = web.dream_preview(self.conn, self.cfg)["bundles"][0]
        self.assertEqual(len(card["conversations"]), 3)
        self.assertEqual(sum(c["n"] for c in card["conversations"]), 3)
        self.assertEqual([c["group"] for c in card["conversations"]].count(True), 1)

    def test_the_id_is_stable_and_follows_the_entity(self):
        self.line(thread="Parker Shaw", person="parker", text="hello there", group=False)
        first = web.dream_preview(self.conn, self.cfg)["bundles"][0]
        again = web.dream_preview(self.conn, self.cfg)["bundles"][0]
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(len(first["id"]), 6)
        self.assertNotEqual(first["id"], web._bundle_id("person:someone else"))


class TestProvenance(Base):
    """"Where did this question come from?" has to be a lookup, not a guess."""

    def test_a_stamped_row_reports_the_call_that_wrote_it(self):
        self.conn.execute(
            "INSERT INTO runs(started_at, mode, model) VALUES(?,?,?)",
            (db.now(), "web", "anthropic/claude-opus-5"))
        run_id = self.conn.execute("SELECT last_insert_rowid() i").fetchone()["i"]
        self.conn.execute(
            """INSERT INTO generations(run_id, generation_id, stage, label, model,
                                       prompt_tokens, completion_tokens, cost_usd, created_at)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (run_id, "gen-abc", "propose", "person:Harper", "anthropic/claude-opus-5",
             4000, 900, 0.031, db.now()))
        trace.stamp(self.conn, kind="question", ref="q:is-shayla-harper", verb="asked",
                    entity="person:Harper", stage="propose", run_id=run_id,
                    generation_id="gen-abc")
        self.conn.commit()

        out = web.why(self.conn, "question", "q:is-shayla-harper")
        self.assertEqual(len(out["calls"]), 1)
        call = out["calls"][0]
        self.assertEqual(call["gen"], "gen-abc")
        self.assertEqual(call["entity"], "person:Harper")
        self.assertEqual(call["verb"], "asked")
        self.assertEqual(call["model"], "claude-opus-5")
        self.assertEqual(call["cost"], 0.031)

    def test_a_row_nobody_stamped_says_so_instead_of_inventing_one(self):
        out = web.why(self.conn, "question", "q:written-by-hand")
        self.assertEqual(out["calls"], [])

    def test_one_call_can_report_everything_it_wrote(self):
        for kind, ref in (("todo", "todo:mfa"), ("event", "ev:poker"),
                          ("question", "q:shayla")):
            trace.stamp(self.conn, kind=kind, ref=ref, verb="opened",
                        generation_id="gen-xyz")
        self.conn.commit()
        wrote = trace.wrote(self.conn, "gen-xyz")
        self.assertEqual(len(wrote), 3)
        self.assertEqual({r["kind"] for r in wrote}, {"todo", "event", "question"})


class TestCollectProgress(Base):
    """A spinner does not say which source it is stuck on. A plan does."""

    def test_the_plan_is_published_before_the_work_starts(self):
        job = web_jobs._Job("collect")
        job.plan(["contacts", "imessage", "groupme", "email"])
        first = job.snapshot()
        self.assertEqual(first["total"], 4)
        self.assertEqual(first["finished"], 0)
        self.assertTrue(all(s["state"] == "waiting" for s in first["steps"]))

    def test_every_terminal_state_counts_toward_finished(self):
        job = web_jobs._Job("collect")
        job.plan(["a", "b", "c", "d"])
        job.step("a", "done", "12 new")
        job.step("b", "skipped", "no token")
        job.step("c", "failed", "HTTP 500")
        job.step("d", "running")
        snap = job.snapshot()
        self.assertEqual(snap["finished"], 3)
        self.assertEqual(snap["total"], 4)
        self.assertEqual(snap["steps"][0]["note"], "12 new")


class TestTheProposeBarWasAnAnimationRatherThanAFraction(Base):
    """`propose` reported a note and no counts, so the longest stage of the longest job
    drew the indeterminate stripe — motion that says "working" for minutes and never
    says how far through. Every number needed for a real fraction was already in hand:
    the bundle count is fixed before the first call, and every reply says how many
    bundles it carried."""

    def test_the_denominator_exists_before_the_first_reply(self):
        bar = dream_run._ProposeBar(planned=30)
        first = bar.see("propose_wave", {"requests": 5, "bundles": 30, "kind": "main"})
        self.assertEqual((first["done"], first["total"]), (0, 30))

    def test_each_reply_moves_it_by_the_bundles_that_reply_carried(self):
        bar = dream_run._ProposeBar(planned=30)
        bar.see("propose_wave", {"requests": 5, "bundles": 30, "kind": "main"})
        seen = [bar.see("propose_request", {"bundles": 6, "ok": True})["done"]
                for _ in range(3)]
        self.assertEqual(seen, [6, 12, 18])

    def test_a_failed_request_does_not_count_as_read(self):
        bar = dream_run._ProposeBar(planned=12)
        moved = bar.see("propose_request", {"bundles": 6, "ok": False})
        self.assertEqual(moved["done"], 0)

    def test_a_wave_that_was_not_planned_widens_the_bar_instead_of_overflowing_it(self):
        """A truncated request is re-sent as two, and a second look re-asks bundles the
        model passed over. Both are real work arriving after the plan was made: counted
        on one side only, the bar sits at 100% while the pass runs on."""
        bar = dream_run._ProposeBar(planned=12)
        bar.see("propose_wave", {"requests": 2, "bundles": 12, "kind": "main"})
        bar.see("propose_request", {"bundles": 6, "ok": True})
        bar.see("propose_request", {"bundles": 6, "ok": True})
        full = bar.see("propose_request", {"bundles": 0, "ok": True})
        self.assertEqual((full["done"], full["total"]), (12, 12))

        widened = bar.see("propose_wave", {"requests": 3, "bundles": 3,
                                           "kind": "second-look"})
        self.assertEqual((widened["done"], widened["total"]), (12, 15))
        self.assertEqual(bar.see("propose_request", {"bundles": 3, "ok": True})["done"],
                         15)

    def test_the_bar_never_reads_past_full(self):
        bar = dream_run._ProposeBar(planned=4)
        over = bar.see("propose_request", {"bundles": 99, "ok": True})
        self.assertEqual((over["done"], over["total"]), (4, 4))

    def test_propose_says_which_of_its_waves_were_in_the_plan(self):
        """The arithmetic above is only honest if `propose_all` labels a re-send. A
        truncated request is split and sent again, and the halves must not read as
        bundles the pass had never heard of."""
        seen: list[tuple[str, str]] = []

        class Truncating:
            """Truncates the first reply, answers the two halves cleanly."""

            def __init__(self):
                self.calls = 0

            def complete(self, **kw):
                self.calls += 1
                cut = self.calls == 1
                return llm.Reply(text="{}", data={"reviewed": [], "diffs": []},
                                 usage=llm.Usage(), model=kw.get("model", ""),
                                 generation_id=f"gen-{self.calls}",
                                 finish_reason="length" if cut else "stop")

            def map(self, jobs, worker, max_parallel=8, on_done=None):
                out = []
                for index, job in enumerate(jobs):
                    value = llm._safe(worker, job)
                    out.append(value)
                    if on_done:
                        on_done(index, value)
                return out

        def watch(event, data):
            if event == "propose_wave":
                seen.append((event, data.get("kind", "")))

        self.cfg.pack_bundles = 2
        propose_stage.propose_all(
            Truncating(), self.conn, self.cfg,
            [bundle_stage.Bundle(entity="person:Jordan"),
             bundle_stage.Bundle(entity="person:Alex")],
            progress=watch)
        self.assertEqual([kind for _e, kind in seen][:2], ["main", "split"])

    def test_the_lane_draws_a_percentage_rather_than_a_stripe(self):
        """`total` 0 is the page's signal to animate instead of measure. The whole point
        of the arithmetic above is that this step never sends one."""
        job = web_jobs._Job("dream")
        job.plan(["prepare", "propose", "merge"])
        job.step("propose", "running", web_jobs._read_so_far({"done": 9, "total": 30}),
                 done=9, total=30)
        step = job.snapshot()["steps"][1]
        self.assertEqual((step["done"], step["total"]), (9, 30))
        self.assertEqual(step["note"], "9/30 bundles read")


class TestAStepWithNoHalfwayStillHasToSaySomething(Base):
    """`sweep` is one model call over the whole resulting state and Merge is one per
    disagreement: nothing inside `client.complete` can report a fraction, so both drew
    the sliding stripe — which says "working" identically at four seconds and at four
    minutes. How long it has been running is a fact, and it is the one the page needs."""

    def test_a_running_step_reports_how_long_it_has_been_running(self):
        job = web_jobs._Job("dream")
        job.plan(["propose", "sweep"])
        job.step("sweep", "running", "reviewing 88 write(s) against the whole store")
        sweep = job.snapshot()["steps"][1]
        self.assertEqual(sweep["total"], 0, "there is no denominator to invent here")
        self.assertGreaterEqual(sweep["running_for"], 0.0)

    def test_a_step_that_has_not_started_has_no_clock(self):
        job = web_jobs._Job("dream")
        job.plan(["propose", "sweep"])
        self.assertEqual(job.snapshot()["steps"][1]["running_for"], 0.0)

    def test_the_clock_stops_when_the_step_does(self):
        job = web_jobs._Job("dream")
        job.plan(["sweep"])
        job.step("sweep", "running")
        job.step("sweep", "done", "3 action(s)")
        self.assertEqual(job.snapshot()["steps"][0]["running_for"], 0.0)

    def test_counted_progress_does_not_restart_the_clock(self):
        """The source counts from inside its loop while the runner sets state around it.
        Restarting the clock on every tick would peg the elapsed bar at zero for ever."""
        job = web_jobs._Job("gather")
        job.plan(["groupme"])
        job.step("groupme", "running", phase="reading groups")
        first = job.snapshot()["steps"][0]["running_for"]
        job.step("groupme", done=3, total=9)
        job.step("groupme", "running", phase="reading DMs")
        self.assertGreaterEqual(job.snapshot()["steps"][0]["running_for"], first)

    def test_the_page_never_receives_the_raw_clock_reading(self):
        """`since` is a `monotonic()` reading and means nothing in a browser. Only the
        difference crosses the wire."""
        job = web_jobs._Job("dream")
        job.plan(["sweep"])
        job.step("sweep", "running")
        self.assertNotIn("since", job.snapshot()["steps"][0])


class TestClickingAFacetHadNowhereToGo(Base):
    """M17. The matching was written and reachable only inside one event's `/api/why`.

    So every attendee, location and series of every event opened was resolved whether
    or not anything was clicked, and clicking a pill could never ask a question of its
    own. `/api/events` is that question.
    """

    def rows(self):
        for fields in (
            {"title": "Ramen with Ann", "date": self.ts(1)[:10],
             "participants": ["Ann Park"], "location": "UCB SoHo", "series": "supper"},
            {"title": "Poker night", "date": self.ts(2)[:10],
             "participants": ["Ann Park", "Hannah Weiss"], "location": "UCB SoHo",
             "series": "poker"},
            {"title": "Standup", "date": self.ts(3)[:10],
             "participants": ["Hannah Weiss"], "location": "Equinox West 92nd",
             "series": "poker"},
        ):
            events.upsert(self.conn, fields, match=False)
        self.conn.commit()

    def test_a_person_facet_lists_every_row_they_are_on(self):
        self.rows()
        out = web.event_list(self.conn, self.cfg, person="Ann Park")
        self.assertEqual([e["title"] for e in out["events"]],
                         ["Poker night", "Ramen with Ann"])
        self.assertEqual(out["total"], 2)
        self.assertEqual((out["facet"], out["value"]), ("person", "Ann Park"))

    def test_a_short_name_is_not_a_substring_of_a_longer_one(self):
        """The reason participants are resolved in Python and not matched as text."""
        self.rows()
        out = web.event_list(self.conn, self.cfg, person="Ann Park")
        self.assertNotIn("Standup", [e["title"] for e in out["events"]])

    def test_location_and_series_each_filter_to_their_own_rows(self):
        self.rows()
        self.assertEqual(
            {e["title"] for e in
             web.event_list(self.conn, self.cfg, location="ucb soho")["events"]},
            {"Ramen with Ann", "Poker night"})
        self.assertEqual(
            {e["title"] for e in
             web.event_list(self.conn, self.cfg, series="poker")["events"]},
            {"Poker night", "Standup"})

    def test_the_row_being_read_is_excluded(self):
        """"Other entries involving Ann" must not open with the one already on screen."""
        self.rows()
        key = self.conn.execute(
            "SELECT key FROM events WHERE title = 'Poker night'").fetchone()["key"]
        out = web.event_list(self.conn, self.cfg, person="Ann Park", exclude=key)
        self.assertEqual([e["title"] for e in out["events"]], ["Ramen with Ann"])
        self.assertEqual(out["total"], 1)

    def test_no_facet_is_the_whole_calendar(self):
        self.rows()
        out = web.event_list(self.conn, self.cfg)
        self.assertEqual(out["total"], 3)
        self.assertEqual([e["date"] for e in out["events"]],
                         sorted((e["date"] for e in out["events"]), reverse=True))
        self.assertEqual((out["facet"], out["value"]), ("", ""))

    def test_a_facet_nobody_shares_is_empty_and_not_an_error(self):
        self.rows()
        out = web.event_list(self.conn, self.cfg, person="Nobody Here")
        self.assertEqual(out["events"], [])
        self.assertEqual(out["total"], 0)
        self.assertNotIn("error", out)

    def test_an_empty_store_answers_rather_than_raising(self):
        out = web.event_list(self.conn, self.cfg)
        self.assertEqual((out["events"], out["total"]), ([], 0))

    def test_two_facets_at_once_is_refused(self):
        """An intersection is a query the page cannot express, so it is not silently
        answered as one of the two."""
        self.rows()
        out = web.event_list(self.conn, self.cfg, person="Ann Park", location="UCB SoHo")
        self.assertIn("error", out)
        self.assertNotIn("events", out)

    def test_the_index_follows_the_store(self):
        """The scan is cached, so a row written after the first lookup has to invalidate
        it — a stale calendar that never updates is worse than a slow one."""
        self.rows()
        self.assertEqual(web.event_list(self.conn, self.cfg, series="poker")["total"], 2)
        events.upsert(self.conn, {"title": "Poker again", "date": self.ts(9)[:10],
                                  "series": "poker"}, match=False)
        self.conn.commit()
        self.assertEqual(web.event_list(self.conn, self.cfg, series="poker")["total"], 3)

    def test_why_no_longer_carries_a_related_block(self):
        """It was resolved for every facet of every event opened, clicked or not."""
        self.rows()
        key = self.conn.execute(
            "SELECT key FROM events WHERE title = 'Poker night'").fetchone()["key"]
        detail = web.why(self.conn, "event", key, self.cfg)["detail"]
        self.assertNotIn("related", detail)
        self.assertEqual(detail["event"]["participants"], ["Ann Park", "Hannah Weiss"])

    def test_the_pills_ask_the_endpoint(self):
        html = web.frontend_source()
        self.assertIn("/api/events?", html)
        self.assertIn("openRelated(related, facet, value, e.key)", html)


class TestWikiTab(Base):
    """The wiki was reachable only through an event that happened to link a page.

    It is a store in its own right, so it is browsable on its own: a directory of what
    each page can answer, and a page opened with every fact beside the line it came from.
    """

    def test_the_directory_lists_pages_with_what_they_answer(self):
        wiki.set_slot(self.cfg.wiki_dir, "jordan", "address", "42 Example St",
                      source="imessage")
        wiki.add_question(self.cfg.wiki_dir, "jordan", "what does the user drink?")
        wiki.add_alias(self.cfg.wiki_dir, "jordan", "Jordy")

        out = web.wiki_pages(self.conn, self.cfg)
        self.assertEqual(out["total"], 1)
        page = out["pages"][0]
        self.assertEqual(page["slug"], "jordan")
        self.assertEqual(page["facts"], 1)
        self.assertEqual(page["questions"], 1)
        self.assertIn("address", page["answers"])
        self.assertIn("Jordy", page["aliases"])
        json.dumps(out)

    def test_search_matches_title_slug_and_alias(self):
        wiki.set_slot(self.cfg.wiki_dir, "robbie", "hosts", "poker")
        wiki.add_alias(self.cfg.wiki_dir, "robbie", "Robin West")
        wiki.set_slot(self.cfg.wiki_dir, "alex", "job", "producer")

        self.assertEqual(
            [p["slug"] for p in web.wiki_pages(self.conn, self.cfg, q="robin")["pages"]],
            ["robbie"])
        self.assertEqual(
            [p["slug"] for p in web.wiki_pages(self.conn, self.cfg, q="alex")["pages"]],
            ["alex"])

    def test_an_empty_wiki_answers_rather_than_raising(self):
        out = web.wiki_pages(self.conn, self.cfg)
        self.assertEqual((out["pages"], out["total"]), ([], 0))
        json.dumps(out)

    def test_a_pages_past_events_carry_the_key_to_open_them(self):
        """A page's encounters were countable but not followable — "seen 3 times" with
        no way to reach any of the three. The key is what makes each one openable."""
        wiki.set_slot(self.cfg.wiki_dir, "robbie", "hosts", "poker games")
        event, _ = events.upsert(self.conn, {
            "title": "Poker at Robbie's", "date": self.ts(-3)[:10],
            "participants": ["Robbie"], "status": "happened"})
        self.conn.commit()

        profile = wiki.profile(self.conn, self.cfg.wiki_dir, "robbie")
        recent = profile["encounters"]["recent"]
        self.assertTrue(recent)
        self.assertEqual(recent[0]["key"], event.key)

    def test_the_page_serves_the_tab_and_its_loader(self):
        html = web.frontend_source()
        self.assertIn('data-view="wiki"', html)
        self.assertIn("/api/wiki_pages", html)
        self.assertIn("renderWikiProfile", html)


class TestNoDiffIsNotAVerdict(unittest.TestCase):
    """"No diff came back" was being reported as a failure it could not establish.

    Run 5 call 17 got no diff for six bundles, and the banner said so as if the model
    had gone missing. The reasoning shows it read all six — a shortcode, a T-Mobile
    bill, festival marketing, spam — and was right that none of them was worth a row.
    Under the v1 contract those two outcomes are the same evidence, so the page must
    say what happened and let the reasoning settle which it was.
    """

    def test_a_reply_listing_what_it_reviewed_is_v2(self):
        self.assertEqual(
            web._contract({"parsed": {"reviewed": ["abc123"], "diffs": []}}), "v2")

    def test_a_reply_with_only_diffs_is_v1(self):
        self.assertEqual(web._contract({"parsed": {"bundles": []}}), "v1")
        self.assertEqual(web._contract({"parsed": None}), "v1")
        self.assertEqual(web._contract({}), "v1")


class TestMemoryIsTheBrief(Base):
    def test_memory_has_one_rendering_and_click_targets(self):
        event, _ = events.upsert(
            self.conn, {"title": "Poker at Robbie's", "date": self.ts(2)[:10]})
        todo, _ = todos.open_todo(self.conn, "Bring poker chips")
        question = todos.ask(self.conn, "What time does poker start?")
        standing, _ = todos.set_standing(self.conn, "identity", "Robbie hosts poker")

        out = web.memory(self.conn, self.cfg)
        self.assertEqual(set(out), {"brief", "lines", "targets"})
        self.assertEqual(
            "\n".join(line["text"] for line in out["lines"]) + "\n", out["brief"])
        for token, kind, ref in (
            (f"E{event.id}", "event", event.key),
            (f"T{todo.id}", "todo", todo.key),
            (f"Q{self.conn.execute('SELECT id FROM questions WHERE key=?', (question,)).fetchone()[0]}",
             "question", question),
        ):
            self.assertEqual(out["targets"][token]["kind"], kind)
            self.assertEqual(out["targets"][token]["ref"], ref)
        legacy_token = (
            f"S{self.conn.execute('SELECT id FROM standing WHERE key=?', (standing,)).fetchone()[0]}"
        )
        self.assertNotIn(legacy_token, out["targets"])


class TestLatestDreamChangesAreMarkedOnTheBrief(Base):
    def test_created_rows_are_green_candidates_and_edits_are_yellow_candidates(self):
        created, _ = events.upsert(
            self.conn, {"title": "New dinner", "date": self.ts(2)[:10]})
        edited, _ = events.upsert(
            self.conn, {"title": "Moved movie", "date": self.ts(3)[:10]})
        run = self.conn.execute(
            """INSERT INTO runs(started_at, finished_at, mode, model, diffs)
               VALUES(?, ?, 'nightly', 'test', 2)""", (db.now(), db.now())).lastrowid
        trace.stamp(self.conn, kind="event", ref=created.key, verb="inserted", run_id=run)
        trace.stamp(self.conn, kind="event", ref=edited.key, verb="updated", run_id=run)
        self.conn.commit()

        targets = web.memory(self.conn, self.cfg)["targets"]

        self.assertEqual(targets[f"E{created.id}"]["last_dream_change"], "new")
        self.assertEqual(targets[f"E{edited.id}"]["last_dream_change"], "edited")

    def test_why_opens_original_evidence_before_any_model_call(self):
        archive.append(
            self.conn, stream="groupme", external_id="aspca-before",
            ts=self.ts().replace("12:00", "11:58"),
            text="Is this the same clinic we discussed?", thread="Doggo Park 142",
            person="Rae", gated=False)
        for index in range(5):
            archive.append(
                self.conn, stream="groupme", external_id=f"unrelated-{index}",
                ts=self.ts(), text="unrelated interleaved traffic",
                thread=f"elsewhere-{index}", person="Someone", gated=False)
        archive_id = archive.append(
            self.conn, stream="groupme", external_id="aspca-source", ts=self.ts(),
            text="The ASPCA mobile clinic will be at the Doggo Park run Wednesday 10–3",
            thread="Doggo Park 142", person="Rae", gated=True)
        for index in range(5, 10):
            archive.append(
                self.conn, stream="groupme", external_id=f"unrelated-{index}",
                ts=self.ts(), text="more unrelated interleaved traffic",
                thread=f"elsewhere-{index}", person="Someone", gated=False)
        archive.append(
            self.conn, stream="groupme", external_id="aspca-after",
            ts=self.ts().replace("12:00", "12:02"),
            text="Yep, it is at our run.", thread="Doggo Park 142",
            person="Rae", gated=False)
        event, _ = events.upsert(
            self.conn, {"title": "ASPCA mobile clinic", "date": self.ts(1)[:10],
                        "location": "Doggo Park run"})
        trace.stamp(self.conn, kind="event", ref=event.key, verb="inserted",
                    entity="thread:whatsapp:Doggo Park 142", stage="live",
                    archive_ids=[archive_id])
        self.conn.commit()

        out = web.why(self.conn, "event", event.key, self.cfg)
        self.assertEqual(out["calls"], [])
        self.assertEqual(
            [row["text"] for row in out["source"] if row["evidence"]],
            ["The ASPCA mobile clinic will be at the Doggo Park run Wednesday 10–3"])
        self.assertEqual(
            [row["text"] for row in out["source"] if not row["evidence"]],
            ["Is this the same clinic we discussed?", "Yep, it is at our run."])

    def test_why_excerpts_show_context_around_relevant_mentions(self):
        text = ("unrelated preamble " * 80
                + "The source says ASPCA mobile clinic at the Doggo Park run. "
                + "This is the fact that writes the location."
                + " unrelated tail" * 80)
        excerpts = web._excerpts(text, ["ASPCA mobile clinic", "Doggo Park run"], radius=40)
        self.assertTrue(excerpts)
        self.assertTrue(any("Doggo Park" in excerpt for excerpt in excerpts))
        self.assertTrue(all(len(excerpt) < len(text) for excerpt in excerpts))

    def test_ui_defaults_to_relevant_evidence_with_full_call_one_level_deeper(self):
        html = web.frontend_source()
        self.assertIn("Original source", html)
        self.assertIn("Relevant reasoning", html)
        self.assertIn("open the full call", html)
        self.assertNotIn('id="events"', html)
        self.assertNotIn('id="todos"', html)

    def test_price_estimate_respects_an_endpoint_with_no_prompt_cache(self):
        estimate = llm.packed_cost(
            "stepfun/step-3.7-flash", prefix_tokens=1000, suffix_tokens=0,
            output_tokens=0, requests=4, max_parallel=3)
        self.assertFalse(estimate["cache"])
        self.assertEqual(
            estimate["input"],
            round(llm.price("stepfun/step-3.7-flash", 4000), 4))

        cached = llm.packed_cost(
            "anthropic/claude-sonnet-5", prefix_tokens=1000, suffix_tokens=0,
            output_tokens=0, requests=4, max_parallel=3)
        self.assertTrue(cached["cache"])
        self.assertLess(cached["prefix_warmed"], cached["prefix_now"])

    def test_a_follow_up_turn_keeps_the_conversation_in_front_of_the_model(self):
        """Staged prompting is one exchange, not several calls.

        The point is the cache: separate calls re-send the conversation every time,
        while turns send it once and read it back. Measured against the live endpoint,
        a second turn read 99% of its input from cache — which is what makes asking in
        four small stages cost about 1.3 prompts instead of four.

        So the ordering matters and is asserted: system, the bundle, then the exchange.
        Put a turn before the bundle and the cached span ends at the first difference,
        which quietly turns every later turn back into a full-price call.
        """
        sent = {}

        class Recorder(llm.OpenRouter):
            def __init__(self):
                pass                                   # no key needed; _post is stubbed

            def _post(self, path, payload, **kw):
                sent.update(payload)
                return {"id": "gen-t", "choices": [{"message": {"content": "{}"},
                                                    "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

        client = Recorder()
        client.usage = llm.Usage()
        client.complete(model="openai/gpt-5.6-luna", prefix="RULES", suffix="THE BUNDLE",
                        turns=[{"role": "assistant", "content": "yes"},
                               {"role": "user", "content": "now the calendar rows"}])
        roles = [m["role"] for m in sent["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant", "user"])
        self.assertEqual(sent["messages"][1]["content"], "THE BUNDLE")
        self.assertEqual(sent["messages"][-1]["content"], "now the calendar rows")

    def test_omitting_turns_sends_exactly_what_it_always_did(self):
        sent = {}

        class Recorder(llm.OpenRouter):
            def __init__(self):
                pass

            def _post(self, path, payload, **kw):
                sent.update(payload)
                return {"id": "gen-u", "choices": [{"message": {"content": "{}"},
                                                    "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

        client = Recorder()
        client.usage = llm.Usage()
        client.complete(model="openai/gpt-5.6-luna", prefix="RULES", suffix="THE BUNDLE")
        self.assertEqual([m["role"] for m in sent["messages"]], ["system", "user"])


if __name__ == "__main__":
    unittest.main()
