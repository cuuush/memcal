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

from memcal import archive, brief, cli, dates, db, detail, events, gate, identity, live, mcp_server, questions, schedule, series, textclean, threads, todos, trace, web, wiki  # noqa: E402
from memcal.config import Config  # noqa: E402
from memcal.dream import apply as apply_stage  # noqa: E402
from memcal.dream import bundle as bundle_stage  # noqa: E402
from memcal.dream import propose as propose_stage  # noqa: E402
from memcal.dream import merge as merge_stage  # noqa: E402
from memcal.dream import sweep as sweep_stage  # noqa: E402
from memcal import sources  # noqa: E402
from memcal.sources import base, groupme, ical, imessage, proton, providers, spec, whatsapp  # noqa: E402


class TestMatching(Base):
    """Case 2: mentioned -> confirmed, and 'the game moved to sat'."""

    def test_series_match_updates_rather_than_inserts(self):
        first, verb = events.upsert(self.conn, {
            "title": "Poker at Jordan's", "date": self.d(5), "series": "poker-night",
            "participants": ["Jordan"], "status": "mentioned"})
        self.assertEqual(verb, "inserted")
        moved, verb = events.upsert(self.conn, {
            "title": "Poker", "date": self.d(6), "series": "poker-night", "status": "confirmed"})
        self.assertEqual(verb, "updated")
        self.assertEqual(moved.id, first.id)
        self.assertEqual(moved.date, self.d(6))
        self.assertEqual(moved.status, "confirmed")
        self.assertEqual(len(events.window(self.conn, 3, 10)), 1)

    def test_title_match_within_window(self):
        events.upsert(self.conn, {"title": "dinner with Alex", "date": self.d(2)})
        _, verb = events.upsert(self.conn, {"title": "Dinner With Alex", "date": self.d(3),
                                            "status": "confirmed"})
        self.assertEqual(verb, "updated")

    def test_no_match_outside_window(self):
        events.upsert(self.conn, {"title": "Poker", "date": self.d(2), "series": "poker-night"})
        _, verb = events.upsert(self.conn, {"title": "Poker", "date": self.d(40),
                                            "series": "poker-night"})
        self.assertEqual(verb, "inserted")

    def test_participants_merge_and_history_records_change(self):
        first, _ = events.upsert(self.conn, {"title": "dinner", "date": self.d(1),
                                             "participants": ["Alex"]})
        merged, _ = events.upsert(self.conn, {"key": first.key, "title": "dinner",
                                              "date": self.d(1), "participants": ["Harper"],
                                              "location": "Rubirosa"})
        self.assertEqual(merged.participants, ["Alex", "Harper"])
        fields = {row["field"] for row in events.history(self.conn, first.id)}
        self.assertIn("location", fields)

    def test_confirmed_does_not_walk_backwards(self):
        first, _ = events.upsert(self.conn, {"title": "poker", "date": self.d(3),
                                             "status": "confirmed"})
        again, _ = events.upsert(self.conn, {"key": first.key, "title": "poker",
                                             "date": self.d(3), "status": "mentioned"})
        self.assertEqual(again.status, "confirmed")

    def test_two_alexes_do_not_collide_by_subject(self):
        events.upsert(self.conn, {"title": "free evening", "date": self.d(1),
                                  "kind": "availability", "subject": "Alex (poker)"})
        _, verb = events.upsert(self.conn, {"title": "free evening", "date": self.d(1),
                                            "kind": "availability", "subject": "Alex (work)"})
        self.assertEqual(verb, "inserted")


class TestProvenance(Base):
    """§6.2: nightly may overwrite today's cheap writes; older rows are frozen to them."""

    def _age_row(self, key: str, days: int) -> None:
        stamp = (db.today() - timedelta(days=days)).isoformat() + "T02:00:00"
        self.conn.execute("UPDATE events SET updated_at = ? WHERE key = ?", (stamp, key))
        self.conn.commit()

    def test_nightly_overwrites_a_cheap_write_from_today(self):
        row, _ = events.upsert(self.conn, {"title": "poker", "date": self.d(2)},
                               written_by="dream:realtime")
        updated, verb = events.upsert(self.conn, {"key": row.key, "title": "poker",
                                                  "date": self.d(2), "location": "42 Example Street"},
                                      written_by="dream:nightly")
        self.assertEqual(verb, "updated")
        self.assertEqual(updated.location, "42 Example Street")

    def test_cheap_pass_cannot_rewrite_yesterdays_nightly_row(self):
        row, _ = events.upsert(self.conn, {"title": "poker", "date": self.d(2),
                                           "location": "42 Example Street"},
                               written_by="dream:nightly")
        self._age_row(row.key, 1)
        same, verb = events.upsert(self.conn, {"key": row.key, "title": "poker",
                                               "date": self.d(2), "location": "the frat house"},
                                   written_by="dream:realtime")
        self.assertEqual(verb, "unchanged")
        self.assertEqual(same.location, "42 Example Street")

    def test_the_user_always_wins(self):
        row, _ = events.upsert(self.conn, {"title": "poker", "date": self.d(2)},
                               written_by="dream:nightly")
        self._age_row(row.key, 3)
        updated, verb = events.upsert(self.conn, {"key": row.key, "title": "poker",
                                                  "date": self.d(2), "status": "declined"},
                                      written_by="live")
        self.assertEqual(verb, "updated")
        self.assertEqual(updated.status, "declined")


class TestGate(Base):
    """The cost governor. 'hey' costs nothing; 'we playing at 8?' passes."""

    def test_rejects_noise(self):
        for text in ("hey", "lol", "ok", "😂", "haha same"):
            self.assertFalse(gate.gate_message(text), f"{text!r} should not pass")

    def test_passes_temporal_and_questions(self):
        self.assertEqual(gate.gate_message("we playing at 8?").reason, "temporal")
        self.assertTrue(gate.gate_message("you around tomorrow"))
        self.assertTrue(gate.gate_message("poker friday at jordan's"))
        self.assertTrue(gate.gate_message("what did you think of it?"))
        self.assertEqual(gate.gate_message("i'm free monday night").reason, "temporal")

    def test_passes_availability_and_invites(self):
        self.assertTrue(gate.gate_message("i'm out of town"))
        self.assertTrue(gate.gate_message("come over for dinner"))

    def test_affection_does_not_pass(self):
        # Case 7: "i love you" must not become a stored fact — it never even reaches the model.
        self.assertFalse(gate.gate_message("i love you"))
        self.assertFalse(gate.gate_message("miss you so much"))

    def test_a_named_contact_passes_one_to_one_whatever_they_said(self):
        # "Always process contacts": naming someone outranks every content test. In a
        # group chat it does not — a named contact in the gamer chat is still a hundred
        # lines a day of nothing, so there the content test still decides.
        self.assertEqual(gate.gate_message("thing", person="Mom").reason, "known-contact")
        self.assertFalse(gate.gate_message("thing", person="Mom", is_group=True))
        self.assertTrue(gate.gate_message("we playing at 8?", person="Mom", is_group=True))

    def test_top_tier_sender_always_passes(self):
        self.assertFalse(gate.gate_message("thing", person="Mom", is_group=True))
        self.assertTrue(gate.gate_message("thing", person="Mom", is_group=True,
                                          top_tier={"Mom"}))

    def test_texts_are_read_in_full(self):
        # The user does not get that many, and the skipped half is where the replies live.
        self.assertEqual(gate.gate_message("k", stream="imessage").reason, "all-of:imessage")
        self.assertTrue(gate.gate_message("i love you", stream="imessage"))
        # Email is the other story: volume, and mostly junk.
        self.assertFalse(gate.gate_message("i love you", stream="email"))

    def test_email_sender_table_is_a_lookup_after_one_decision(self):
        verdict = gate.gate_email(self.conn, address="news@aws.amazon.com",
                                  subject="re:Invent night is tomorrow",
                                  headers={"List-Unsubscribe": "<mailto:x>"})
        self.assertFalse(verdict)
        self.assertEqual(identity.sender_decision(self.conn, "news@aws.amazon.com"), "archive")
        # Second time it is a table lookup, no header parsing needed.
        again = gate.gate_email(self.conn, address="news@aws.amazon.com", subject="anything")
        self.assertFalse(again)
        self.assertEqual(again.reason, "sender-table:archive")

    def test_unknown_human_sender_passes_and_is_remembered(self):
        self.assertTrue(gate.gate_email(self.conn, address="jordan@example.com", subject="poker friday"))
        self.assertEqual(identity.sender_decision(self.conn, "jordan@example.com"), "process")

    def test_kindred_can_be_flipped_off_by_hand(self):
        identity.set_sender(self.conn, "hello@kindred.com", "ignore", "don't care")
        self.assertFalse(gate.gate_email(self.conn, address="hello@kindred.com", subject="member event"))


class TestIdentity(Base):
    def test_normalizes_phone_numbers(self):
        self.assertEqual(identity.normalize("(917) 555-1234"), "+19175551234")
        self.assertEqual(identity.normalize("+1 917 555 1234"), "+19175551234")
        self.assertEqual(identity.normalize("Foo@Bar.COM"), "foo@bar.com")

    def test_resolution_is_a_dict_lookup(self):
        identity.link(self.conn, "917-555-1234", "Jordan")
        self.assertEqual(identity.resolve(self.conn, "+19175551234"), "Jordan")

    def test_unresolved_queue_dedupes(self):
        identity.note_unresolved(self.conn, "+15551112222", "imessage", "CJ", "yo")
        identity.note_unresolved(self.conn, "+15551112222", "imessage", "CJ", "yo again")
        self.conn.commit()
        rows = identity.unresolved(self.conn)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count"], 2)


class TestTodos(Base):
    def test_an_existing_store_gains_the_event_link_column(self):
        old = sqlite3.connect(":memory:")
        old.execute(
            """CREATE TABLE todos (
                id INTEGER PRIMARY KEY, key TEXT UNIQUE NOT NULL, text TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open', subject TEXT, due TEXT,
                wake_condition TEXT, woke_at TEXT, source TEXT, written_by TEXT,
                opened_at TEXT, closed_at TEXT, updated_at TEXT
            )"""
        )
        db.migrate(old)
        columns = {row[1] for row in old.execute("PRAGMA table_info(todos)")}
        self.assertIn("event_id", columns)
        old.close()

    def test_closure_is_explicit(self):
        todo, _ = todos.open_todo(self.conn, "Return Rowan's EZ-Pass")
        self.assertEqual(len(todos.open_items(self.conn)), 1)
        self.assertTrue(todos.close(self.conn, todo.key))
        self.assertEqual(todos.open_items(self.conn), [])

    def test_wake_condition_surfaces_on_matching_traffic(self):
        todos.open_todo(self.conn, "Ask Rowan about toll-by-mail",
                        wake_condition="Rowan is back from Italy")
        self.assertEqual(todos.check_wakes(self.conn, "watched the game last night"), [])
        woken = todos.check_wakes(self.conn, "just landed, italy was unreal")
        self.assertEqual(len(woken), 1)

    def test_age_is_rendered(self):
        todo, _ = todos.open_todo(self.conn, "thing")
        self.conn.execute("UPDATE todos SET opened_at = ? WHERE key = ?",
                          ((db.today() - timedelta(days=42)).isoformat() + "T09:00:00", todo.key))
        self.conn.commit()
        self.assertIn("weeks", todos.get(self.conn, todo.key).one_line())

    def test_an_event_link_is_visible_and_expires_with_the_event(self):
        movie, _ = events.upsert(self.conn, {
            "title": "Spider-Man movie", "date": self.d(3), "status": "confirmed",
        })
        todo, _ = todos.open_todo(
            self.conn, "Make sure we have tickets", event_id=movie.id)
        current = todos.get(self.conn, todo.key)
        self.assertEqual(current.event_key, movie.key)
        self.assertIn("for Spider-Man movie", current.one_line())
        self.assertIn("Make sure we have tickets", brief.render(self.conn, self.cfg))

        events.upsert(self.conn, {
            "key": movie.key, "date": movie.date, "status": "happened",
        })
        text = brief.render(self.conn, self.cfg)
        self.assertNotIn("Make sure we have tickets", text)
        self.assertEqual(todos.get(self.conn, todo.key).status, "dropped")

    def test_a_filtered_receipt_can_only_rescue_the_event_it_names(self):
        spider, _ = events.upsert(self.conn, {
            "title": "Spider-Man movie", "date": self.d(3), "status": "confirmed",
        })
        dune, _ = events.upsert(self.conn, {
            "title": "Dune Part Two movie", "date": self.d(5), "status": "confirmed",
        })
        tickets, _ = todos.open_todo(
            self.conn, "Make sure we have Spider-Man tickets", event_id=spider.id)
        todos.open_todo(self.conn, "Make sure we have Dune tickets", event_id=dune.id)

        self.assertTrue(todos.may_contain_event_proof(
            self.conn, "Your AMC order confirmation"))
        matched = todos.matching_event_proofs(
            self.conn,
            "Your tickets are confirmed for Spider-Man. Seats F8 and F9.",
        )
        self.assertEqual([todo.key for todo in matched], [tickets.key])
        self.assertEqual(
            todos.matching_event_proofs(
                self.conn, "Spider-Man tickets are on sale now"),
            [],
        )

    def test_dream_proof_closes_the_link_and_keeps_ticket_details_on_the_event(self):
        movie, _ = events.upsert(self.conn, {
            "title": "Spider-Man movie", "date": self.d(3), "status": "confirmed",
        }, written_by="live")
        todo, _ = todos.open_todo(
            self.conn, "Make sure we have tickets", event_id=movie.id)
        bundle = bundle_stage.Bundle(entity="thread:email:amc", items=[])
        diff = {
            "events": [{
                "key": movie.key, "date": movie.date, "title": movie.title,
                "time": "7:30pm", "location": "AMC Lincoln Square",
                "note": "Auditorium 6, seats F8–F9",
            }],
            "todos": [{
                "op": "close", "key": todo.key, "text": todo.text,
                "event_key": movie.key,
            }],
        }
        apply_stage.apply_diffs(
            self.conn, self.cfg, [(bundle, diff)], written_by="dream:nightly")

        self.assertEqual(todos.get(self.conn, todo.key).status, "closed")
        updated = events.get(self.conn, movie.key)
        self.assertEqual(updated.time, "7:30pm")
        self.assertEqual(updated.location, "AMC Lincoln Square")
        self.assertIn("seats F8–F9", updated.note)
        rendered = brief.render(self.conn, self.cfg)
        self.assertNotIn("Make sure we have tickets", rendered)
        # Place and seat numbers are detail, and detail is one handle away now. The
        # obligation closing is still the point of this test; where its residue lives
        # is not.
        self.assertIn("Spider-Man", rendered)
        opened = detail.open_handle(self.conn, self.cfg, f"E{updated.id}")
        self.assertIn("AMC Lincoln Square", opened)
        self.assertIn("seats F8–F9", opened)

class TestBrief(Base):
    def test_blocks_and_cap(self):
        wiki.set_slot(self.cfg.wiki_dir, "casey", "neighborhood", "North End",
                      source="test", conn=self.conn)
        events.upsert(self.conn, {"title": "Poker at Jordan's", "date": self.d(3),
                                  "time": "~8pm", "location": "42 Example Street", "status": "confirmed"})
        todos.open_todo(self.conn, "Return Rowan's EZ-Pass")
        todos.ask(self.conn, "Did Tuesday dinner happen?")
        text = brief.render(self.conn, self.cfg)
        for header in ("## This week", "## Open", "## Ask about", "## People and facts"):
            self.assertIn(header, text)
        # The address is detail and lives behind the handle. What the brief owes the
        # reader here is that the row is *named* and that its handle opens.
        self.assertIn("Poker at Jordan's", text)
        self.assertNotIn("42 Example Street", text)

    def test_cap_is_enforced(self):
        for i in range(80):
            events.upsert(self.conn, {"title": f"thing number {i} with a long-ish title",
                                      "date": self.d(i % 7)}, match=False)
        self.cfg.brief_token_cap = 120
        text = brief.render(self.conn, self.cfg)
        self.assertLessEqual(brief.approx_tokens(text), 120)
        self.assertIn("## This week", text)


class TestWiki(Base):
    def test_slots_round_trip_and_pages_are_lazy(self):
        self.assertEqual(wiki.list_pages(self.cfg.wiki_dir), [])
        wiki.set_slot(self.cfg.wiki_dir, "jordan", "address", "42 Example Street", source="imessage")
        wiki.add_question(self.cfg.wiki_dir, "jordan", "what does the user drink?")
        page = wiki.read(self.cfg.wiki_dir, "jordan")
        self.assertEqual(page.slots["address"]["value"], "42 Example Street")
        self.assertEqual(page.questions, ["what does the user drink?"])
        self.assertIn("jordan", wiki.list_pages(self.cfg.wiki_dir))

    def test_hand_edits_survive_a_read_write_cycle(self):
        wiki.set_slot(self.cfg.wiki_dir, "alex", "job", "sound engineer")
        path = wiki.path_for(self.cfg.wiki_dir, "alex")
        path.write_text(path.read_text() + "\nHe hates cilantro.\n")
        wiki.set_slot(self.cfg.wiki_dir, "alex", "job", "producer")
        page = wiki.read(self.cfg.wiki_dir, "alex")
        self.assertIn("cilantro", page.body)
        self.assertEqual(page.slots["job"]["value"], "producer")


class TestWikiAliases(Base):
    """One person, one page.

    Learning Robbie's legal name mid-conversation opened `robin-west` beside the
    `robbie` page holding the same three facts, because nothing linked two slugs to one
    entity. The same split put their own cat and dog on `casey-morgan` while `me` held their
    hometown. Merging after the fact is the bandaid; resolving names on the way in is
    the fix, so most of these test the *lookups*, not the merge.
    """

    def test_every_lookup_resolves_through_an_alias(self):
        wiki.set_slot(self.cfg.wiki_dir, "robbie", "hosts", "poker games")
        wiki.add_alias(self.cfg.wiki_dir, "robbie", "Robin West")

        self.assertEqual(wiki.canonical(self.cfg.wiki_dir, "Robin West"), "robbie")
        self.assertTrue(wiki.exists(self.cfg.wiki_dir, "robin-west"))
        self.assertEqual(wiki.read(self.cfg.wiki_dir, "robin-west").slug, "robbie")
        self.assertEqual(wiki.path_for(self.cfg.wiki_dir, "robin-west"),
                         wiki.path_for(self.cfg.wiki_dir, "robbie"))
        # …including the write path: a fact learned under the other name lands here.
        wiki.set_slot(self.cfg.wiki_dir, "Robin West", "works at", "a bank")
        self.assertEqual(wiki.list_pages(self.cfg.wiki_dir), ["robbie"])
        self.assertEqual(wiki.read(self.cfg.wiki_dir, "robbie").slots["works at"]["value"],
                         "a bank")

    def test_aliases_survive_the_file_they_live_in(self):
        wiki.add_alias(self.cfg.wiki_dir, "robbie", "Robin West")
        page = wiki.read(self.cfg.wiki_dir, "robbie")
        self.assertEqual(page.aliases, ["Robin West"])
        self.assertIn("## Also known as", page.path.read_text())

    def test_a_hand_edited_alias_is_picked_up(self):
        """The user edits these in Obsidian. Writing the line by hand has to be enough."""
        wiki.set_slot(self.cfg.wiki_dir, "me", "hometown", "Pine Ridge")
        path = wiki.path_for(self.cfg.wiki_dir, "me")
        path.write_text("# Me\n\n## Also known as\n\n- Casey\n\n" + path.read_text())
        self.assertEqual(wiki.canonical(self.cfg.wiki_dir, "Casey"), "me")

    def test_autocreate_does_not_reopen_a_page_that_is_an_alias(self):
        """Without this the merge heals it and the next dream run undoes the merge —
        the rows still carry both names forever."""
        wiki.set_slot(self.cfg.wiki_dir, "robbie", "hosts", "poker games")
        wiki.add_alias(self.cfg.wiki_dir, "robbie", "Robin West")
        events.upsert(self.conn, {"date": db.today().isoformat(), "title": "Poker",
                                  "subject": "me", "participants": ["Robin West"]},
                      written_by="test")
        self.assertNotIn("robin-west", wiki.autocreate(self.conn, self.cfg.wiki_dir))
        self.assertEqual(wiki.list_pages(self.cfg.wiki_dir), ["robbie"])

    def test_a_name_with_its_own_page_is_not_silently_hidden(self):
        """Aliasing a page that holds facts would strand them. That is a merge."""
        wiki.set_slot(self.cfg.wiki_dir, "robbie", "hosts", "poker games")
        wiki.set_slot(self.cfg.wiki_dir, "robin-west", "works at", "a bank")
        with self.assertRaises(ValueError):
            wiki.add_alias(self.cfg.wiki_dir, "robbie", "Robin West")
        # And if the line is added by hand anyway, the page keeps answering for itself.
        page = wiki.read(self.cfg.wiki_dir, "robbie")
        page.aliases.append("Robin West")
        wiki.write(self.cfg.wiki_dir, page)
        self.assertEqual(wiki.read(self.cfg.wiki_dir, "robin-west").slug,
                         "robin-west")

    def test_merge_keeps_both_sets_of_facts_and_leaves_an_alias(self):
        wiki.set_slot(self.cfg.wiki_dir, "robbie", "hosts", "poker games")
        wiki.set_slot(self.cfg.wiki_dir, "robin-west", "works at", "a bank")
        wiki.add_question(self.cfg.wiki_dir, "robin-west", "robbie: where does the user live?")
        page = wiki.merge(self.cfg.wiki_dir, "robbie", "robin-west")

        self.assertEqual(page.slug, "robbie")
        self.assertEqual(page.slots["hosts"]["value"], "poker games")
        self.assertEqual(page.slots["works at"]["value"], "a bank")
        self.assertIn("robbie: where does the user live?", page.questions)
        self.assertEqual(wiki.list_pages(self.cfg.wiki_dir), ["robbie"])
        # The whole point: the name that just lost its page still finds one.
        self.assertEqual(wiki.canonical(self.cfg.wiki_dir, "Robin West"), "robbie")

    def test_merge_never_overwrites_what_the_survivor_already_says(self):
        wiki.set_slot(self.cfg.wiki_dir, "robbie", "hosts", "poker games")
        wiki.set_slot(self.cfg.wiki_dir, "robin-west", "hosts", "board game night")
        page = wiki.merge(self.cfg.wiki_dir, "robbie", "robin-west")
        self.assertEqual(page.slots["hosts"]["value"], "poker games")

    def test_an_aliased_page_is_never_pruned_as_empty(self):
        """It holds no facts, but it is the only record that two names are one person."""
        wiki.add_alias(self.cfg.wiki_dir, "robbie", "Robin West")
        self.assertEqual(wiki.prune_empty(self.cfg.wiki_dir), [])
        self.assertEqual(wiki.canonical(self.cfg.wiki_dir, "robin-west"), "robbie")

    def test_a_cycle_terminates(self):
        for slug, name in (("a", "B"), ("b", "A")):
            page = wiki.ensure(self.cfg.wiki_dir, slug)
            page.aliases.append(name)
            wiki.write(self.cfg.wiki_dir, page)
        # Both own pages, so neither alias binds — but the resolver must not hang
        # either way, and a hand-edited wiki can always contain a real cycle.
        self.assertIn(wiki.canonical(self.cfg.wiki_dir, "a"), ("a", "b"))

    def test_a_number_nobody_has_named_gets_no_page(self):
        events.upsert(self.conn, {"date": db.today().isoformat(), "title": "D&D",
                                  "subject": "me",
                                  "participants": ["Katie", "+19175551234"]},
                      written_by="test")
        self.assertEqual(wiki.autocreate(self.conn, self.cfg.wiki_dir), ["katie"])

    def test_a_number_that_does_have_a_name_gets_that_name_s_page(self):
        identity.link(self.conn, "+19175551234", "Pat Baker")
        events.upsert(self.conn, {"date": db.today().isoformat(), "title": "D&D",
                                  "subject": "me", "participants": ["+19175551234"]},
                      written_by="test")
        self.assertEqual(wiki.autocreate(self.conn, self.cfg.wiki_dir), ["pat-baker"])

    def test_two_names_for_one_person_are_one_page_in_the_prompt(self):
        """Bundle context used to render the same page twice, paying for it twice."""
        wiki.set_slot(self.cfg.wiki_dir, "robbie", "hosts", "poker games")
        wiki.add_alias(self.cfg.wiki_dir, "robbie", "Robin West")
        context = wiki.context_for(self.cfg.wiki_dir, ["robbie", "robin-west"])
        self.assertEqual(context.count("poker games"), 1)


class TestWikiGrowth(Base):
    """The wiki has to fill itself, or depth-on-demand has nowhere to land.

    This is a regression guard: the wiki was correct but stayed empty on real data
    for a full build, because a page was only ever born from a model-proposed slot.
    """

    def _traffic(self, person: str, count: int) -> None:
        for i in range(count):
            archive.append(self.conn, stream="imessage", external_id=f"{person}{i}",
                           ts=db.now(), text=f"note {i}", thread="t", person=person, gated=True)
        self.conn.commit()

    def test_a_memcal_participant_gets_a_page(self):
        events.upsert(self.conn, {"title": "dinner", "date": self.d(1), "participants": ["Jordan"]})
        created = wiki.autocreate(self.conn, self.cfg.wiki_dir)
        self.assertIn("jordan", created)
        self.assertIn("jordan", wiki.list_pages(self.cfg.wiki_dir))

    def test_a_new_page_asserts_nothing_and_asks_nothing(self):
        # Pages used to be seeded with six boilerplate questions each, which left 32
        # of 49 pages holding nothing else and 192 questions nobody would answer.
        events.upsert(self.conn, {"title": "dinner", "date": self.d(1), "participants": ["Jordan"]})
        wiki.autocreate(self.conn, self.cfg.wiki_dir)
        page = wiki.read(self.cfg.wiki_dir, "jordan")
        self.assertEqual(page.slots, {}, "a new page must assert nothing")
        self.assertEqual(page.questions, [], "a new page must not invent curiosity")

    def test_the_address_book_is_not_prefilled(self):
        # "I have 300 contacts, most don't matter." Being a contact is not evidence.
        identity.link(self.conn, "+19175550001", "Someone Irrelevant")
        self.assertEqual(wiki.autocreate(self.conn, self.cfg.wiki_dir), [])

    def test_traffic_volume_alone_earns_nothing(self):
        # Frequency was the trigger, and it produced pages for an SMS shortcode and
        # for three unrelated Caseys. Being on a row is the evidence that counts.
        self._traffic("Chatty", 20)
        self.assertEqual(wiki.autocreate(self.conn, self.cfg.wiki_dir), [])

    def test_autocreate_is_idempotent_and_bounded(self):
        for i in range(30):
            events.upsert(self.conn, {"title": f"thing {i}", "date": self.d(1),
                                      "participants": [f"Person{i}"]})
        first = wiki.autocreate(self.conn, self.cfg.wiki_dir)
        self.assertLessEqual(len(first), wiki.MAX_NEW_PAGES_PER_RUN)
        second = wiki.autocreate(self.conn, self.cfg.wiki_dir)
        self.assertFalse(set(first) & set(second), "a page must not be created twice")

    def test_repeats_become_a_series_with_one_page(self):
        events.upsert(self.conn, {"title": "Poker at Jordan's", "date": self.d(1),
                                  "location": "42 Example Street"})
        events.upsert(self.conn, {"title": "Poker at Jordan's", "date": self.d(30)})
        linked = wiki.link_series(self.conn, self.cfg.wiki_dir)
        self.assertTrue(linked)
        slug = linked[0]
        page = wiki.read(self.cfg.wiki_dir, slug)
        self.assertIsNotNone(page)
        # "Where was poker last time" is a page read, not an archive search.
        self.assertEqual(page.slots.get("where", {}).get("value"), "42 Example Street")
        rows = self.conn.execute("SELECT series FROM events").fetchall()
        self.assertTrue(all(r["series"] == slug for r in rows))

    def test_a_one_off_does_not_become_a_series(self):
        events.upsert(self.conn, {"title": "one time thing", "date": self.d(1)})
        self.assertEqual(wiki.link_series(self.conn, self.cfg.wiki_dir), [])

    def test_new_pages_reach_the_brief(self):
        # The agent can only open a page if it knows one exists.
        events.upsert(self.conn, {"title": "dinner", "date": self.d(1), "participants": ["Jordan"]})
        wiki.autocreate(self.conn, self.cfg.wiki_dir)
        self.assertIn("jordan", brief.render(self.conn, self.cfg))


class TestArchiveAndBundling(Base):
    def _add(self, text, person=None, thread="t", stream="imessage", ext=None):
        aid = archive.append(self.conn, stream=stream, external_id=ext or text, ts=db.now(),
                             text=text, thread=thread, person=person, gated=True)
        if aid:
            archive.spool_add(self.conn, aid, gate.bundle_entity(person, thread, stream))
        self.conn.commit()
        return aid

    def test_archive_dedupes_by_external_id(self):
        self.assertIsNotNone(self._add("hello", ext="abc"))
        self.assertIsNone(self._add("hello again", ext="abc"))

    def test_search_finds_it(self):
        self._add("poker is at 42 Example Street", person="Jordan", ext="m1")
        rows = archive.search(self.conn, "example")
        self.assertEqual(len(rows), 1)

    def test_bundling_joins_streams_by_person(self):
        # Case 12: the same person on three platforms about one thing -> one bundle.
        self._add("dinner tuesday?", person="Jordan", thread="sms", ext="a")
        self._add("we still on for tuesday", person="Jordan", thread="gamers",
                  stream="groupme", ext="b")
        self._add("tuesday works", person="Jordan", thread="mail", stream="email", ext="c")
        self._add("unrelated", person="Alex", thread="sms", ext="d")
        bundles = bundle_stage.build(self.conn)
        by_entity = {b.entity: b for b in bundles}
        self.assertEqual(len(by_entity["person:Jordan"].items), 3)
        self.assertEqual(len(by_entity["person:Alex"].items), 1)

    def test_spool_is_claimed_once(self):
        self._add("poker friday", person="Jordan", ext="x")
        bundles = bundle_stage.build(self.conn)
        archive.spool_mark(self.conn, [s for b in bundles for s in b.spool_ids], run_id=1)
        self.assertEqual(bundle_stage.build(self.conn), [])


class TestStaleTraffic(Base):
    """Case 1's structural half: a year-old email cannot schedule this week."""

    def _bundle_dated(self, days_ago: int) -> bundle_stage.Bundle:
        ts = (db.today() - timedelta(days=days_ago)).isoformat() + "T12:00:00"
        aid = archive.append(self.conn, stream="email", external_id=f"old{days_ago}", ts=ts,
                             text="poker night this Saturday at the chapter house, 21 Waverly Pl",
                             thread="frat-listserv", person="Listserv", gated=True)
        archive.spool_add(self.conn, aid, "person:Listserv")
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM archive WHERE id = ?", (aid,)).fetchone()
        return bundle_stage.Bundle(entity="person:Listserv", items=[row])

    def test_year_old_email_cannot_create_a_row_this_week(self):
        stale = self._bundle_dated(365)
        diff = {"events": [{"title": "College Club poker night", "date": self.d(5),
                            "location": "21 Waverly Pl", "kind": "commitment"}]}
        counts, log = apply_stage.apply_diffs(self.conn, self.cfg, [(stale, diff)],
                                              written_by="test")
        self.assertEqual(counts["event:rejected-stale"], 1)
        self.assertEqual(events.window(self.conn, 3, 10), [])
        self.assertIn("rejected", log[0])

    def test_fresh_traffic_still_schedules_forward(self):
        fresh = self._bundle_dated(0)
        diff = {"events": [{"title": "Poker at Jordan's", "date": self.d(5),
                            "location": "42 Example Street", "kind": "commitment"}]}
        counts, _ = apply_stage.apply_diffs(self.conn, self.cfg, [(fresh, diff)],
                                            written_by="test")
        self.assertEqual(counts["event:inserted"], 1)

    def test_old_traffic_can_still_record_what_already_happened(self):
        stale = self._bundle_dated(365)
        diff = {"events": [{"title": "College Club poker night", "date": self.d(-365),
                            "kind": "observed", "status": "happened"}]}
        counts, _ = apply_stage.apply_diffs(self.conn, self.cfg, [(stale, diff)],
                                            written_by="test")
        self.assertEqual(counts["event:inserted"], 1)


class TestPrompt(Base):
    def test_shared_prefix_is_byte_identical_across_bundles(self):
        events.upsert(self.conn, {"title": "poker", "date": self.d(2)})
        todos.open_todo(self.conn, "return the ez-pass")
        first = propose_stage.build_prefix(self.conn, self.cfg)
        second = propose_stage.build_prefix(self.conn, self.cfg)
        self.assertEqual(first, second)  # anything varying here would break caching
        self.assertIn("CURRENT MEMCAL", first)
        self.assertIn("poker", first)

    def test_schema_is_strict(self):
        schema = propose_stage.DIFF_SCHEMA
        self.assertFalse(schema["additionalProperties"])
        for name, prop in schema["properties"].items():
            if prop["type"] == "array" and prop["items"].get("type") == "object":
                self.assertFalse(prop["items"]["additionalProperties"], name)
                self.assertEqual(set(prop["items"]["required"]),
                                 set(prop["items"]["properties"]), name)

    def test_every_schema_lists_every_key_as_required(self):
        """Strict json_schema means `required` names every key in `properties`.

        Optionality is expressed as a nullable type, never by leaving a key out. This
        covers every schema in the package rather than one, because the check above
        covered only `DIFF_SCHEMA` and `merge.SCHEMA` was wrong for six keys the whole
        time — accepted by one provider, rejected with HTTP 400 by the next, which turned
        the entire Merge stage into its no-model fallback without failing a run.
        """
        from memcal import live                                    # noqa: PLC0415
        from memcal.dream import merge as merge_stage              # noqa: PLC0415

        def walk(name, node):
            if not isinstance(node, dict):
                return
            if node.get("type") == "object" and "properties" in node:
                self.assertFalse(node.get("additionalProperties", True),
                                 f"{name}: additionalProperties must be False")
                self.assertEqual(set(node.get("required") or ()),
                                 set(node["properties"]),
                                 f"{name}: required must list every property")
            for key, value in (node.get("properties") or {}).items():
                walk(f"{name}.{key}", value)
                if isinstance(value, dict):
                    walk(f"{name}.{key}[]", value.get("items"))

        found = 0
        for module in (propose_stage, merge_stage, sweep_stage, live):
            for attr in dir(module):
                if "SCHEMA" not in attr:
                    continue
                value = getattr(module, attr)
                if isinstance(value, dict) and "properties" in value:
                    walk(f"{module.__name__}.{attr}", value)
                    found += 1
        self.assertGreaterEqual(found, 4, "expected to find the package's schemas")


class TestPacking(Base):
    """§6.1: N independent calls submit as one batch. Packing must not misroute a diff."""

    def _bundle(self, entity: str, items: int = 1) -> bundle_stage.Bundle:
        rows = []
        for i in range(items):
            aid = archive.append(self.conn, stream="imessage", external_id=f"{entity}:{i}",
                                 ts=db.now(), text=f"message {i} about dinner tomorrow",
                                 thread=entity, person=entity.split(":")[-1], gated=True)
            rows.append(self.conn.execute("SELECT * FROM archive WHERE id = ?", (aid,)).fetchone())
        self.conn.commit()
        return bundle_stage.Bundle(entity=entity, items=rows)

    def test_many_bundles_become_few_requests(self):
        bundles = [self._bundle(f"person:P{i}") for i in range(25)]
        groups = propose_stage.pack(self.cfg, bundles)
        self.assertLess(len(groups), len(bundles))
        self.assertLessEqual(max(len(g) for g in groups), propose_stage.PACK_BUNDLES)
        # Every bundle appears exactly once — packing must never drop or duplicate work.
        packed = [b.entity for g in groups for b in g]
        self.assertEqual(sorted(packed), sorted(b.entity for b in bundles))

    def test_warming_the_cache_first_does_not_reorder_results(self):
        """`propose_all` sends one request alone to write the prompt cache, then fans the
        rest out behind it so they read the cache instead of each writing it again.

        That splits one `client.map` into two, and the halves are stitched back together
        by position. Get the stitch wrong and every diff after the first is filed against
        the wrong conversation — silently, because a diff routed to the wrong bundle is a
        perfectly well-formed diff. The routing tested above cannot save it: v2 routes on
        the id the *model* echoed, and the model was answering about a different bundle.
        """
        sent: list[str] = []

        class OrderedClient:
            """Answers with the suffix it was given, so a swap is visible."""

            def complete(self, **kwargs):
                sent.append(kwargs["suffix"])
                return type("R", (), {
                    "data": {"reviewed": [], "diffs": []}, "text": "", "reasoning": "",
                    "generation_id": f"gen-{len(sent)}", "finish_reason": "stop",
                    "truncated": False,
                    "usage": type("U", (), {"cost": 0.0, "prompt_tokens": 0,
                                            "completion_tokens": 0})(),
                })()

            def map(self, jobs, worker, max_parallel=8, on_done=None):
                out = []
                for index, job in enumerate(jobs):
                    value = worker(job)
                    out.append(value)
                    if on_done:
                        on_done(index, value)
                return out

        # More requests than `max_parallel`, which is the condition that turns the
        # warm-then-widen path on at all.
        self.cfg.max_parallel = 2
        self.cfg.pack_bundles = 1
        self.cfg.propose_model = "openai/gpt-5.6-luna"      # a model that has a cache
        bundles = [self._bundle(f"person:P{i}") for i in range(5)]
        good, errors = propose_stage.propose_all(
            OrderedClient(), self.conn, self.cfg, bundles)

        self.assertEqual(errors, [])
        self.assertEqual(len(sent), len(bundles), "every bundle must be sent exactly once")
        # Each request carried a distinct suffix, and the set is the whole input: nothing
        # sent twice, nothing dropped, whatever order the two halves ran in.
        self.assertEqual(len(set(sent)), len(bundles))
        for bundle in bundles:
            self.assertTrue(any(bundle.entity.split(":")[-1] in text for text in sent),
                            f"{bundle.entity} never reached a request")

    def test_a_huge_bundle_does_not_drag_others_over_budget(self):
        big = self._bundle("person:Chatty", items=40)
        smalls = [self._bundle(f"person:Q{i}") for i in range(3)]
        groups = propose_stage.pack(self.cfg, [big] + smalls)
        sizes = [brief.approx_tokens(propose_stage.build_suffix(self.cfg, g)) for g in groups]
        self.assertLessEqual(max(sizes), propose_stage.PACK_TOKENS * 1.5)

    def test_diffs_route_back_by_entity_not_position(self):
        group = [self._bundle("person:Jordan"), self._bundle("person:Alex")]
        payload = {"bundles": [                       # deliberately out of order
            {"entity": "person:Alex", "events": [{"title": "alex thing", "date": self.d(1)}]},
            {"entity": "person:Jordan", "events": [{"title": "jordan thing", "date": self.d(1)}]},
        ]}
        routed = propose_stage._route(group, payload, [])
        by_entity = {bundle.entity: diff for bundle, diff in routed}
        self.assertEqual(by_entity["person:Jordan"]["events"][0]["title"], "jordan thing")
        self.assertEqual(by_entity["person:Alex"]["events"][0]["title"], "alex thing")

    def test_a_diff_for_an_unknown_bundle_is_dropped_not_guessed(self):
        group = [self._bundle("person:Jordan"), self._bundle("person:Alex")]
        errors: list[str] = []
        payload = {"bundles": [
            {"entity": "person:Jordan", "events": []},
            {"entity": "person:Stranger", "events": [{"title": "wrong", "date": self.d(1)}]},
            {"entity": "person:Alex", "events": []},
        ]}
        routed = propose_stage._route(group, payload, errors)
        self.assertEqual(len(routed), 2)
        self.assertTrue(any("Stranger" in e for e in errors))

    def test_positional_fallback_only_when_counts_agree(self):
        group = [self._bundle("person:Jordan"), self._bundle("person:Alex")]
        payload = {"bundles": [{"events": []}, {"events": []}]}   # model omitted entity
        self.assertEqual(len(propose_stage._route(group, payload, [])), 2)
        short = {"bundles": [{"events": []}]}                      # count mismatch: no guessing
        self.assertEqual(propose_stage._route(group, short, []), [])

    def test_malformed_payload_yields_nothing(self):
        group = [self._bundle("person:Jordan")]
        for payload in ({}, {"bundles": None}, {"bundles": "nope"}, {"bundles": [None]}):
            self.assertEqual(propose_stage._route(group, payload, []), [])


class TestSourcePlugins(Base):
    """A third party must be able to add a stream without touching memcal."""

    def _write_plugin(self, filename: str, body: str) -> None:
        # `__TS__` is "an hour ago", filled in as the file is written. A literal
        # timestamp in the plugin body made the assertions about `passed` a bet on the
        # date: the gate spools only what is inside the horizon, so once the real clock
        # walked past it every delivered line archived and none spooled, and a test about
        # plugins read as the gate rejecting them.
        ts = (db.now_dt() - timedelta(hours=1)).isoformat(timespec="seconds")
        self.cfg.plugin_dir.mkdir(parents=True, exist_ok=True)
        (self.cfg.plugin_dir / filename).write_text(
            textwrap.dedent(body).replace("__TS__", ts))
        sources.load_plugin_dir(self.cfg.plugin_dir)

    def test_builtins_are_registered(self):
        registered = sources.names(self.cfg)
        for expected in ("imessage", "groupme", "email", "ical"):
            self.assertIn(expected, registered)

    def test_a_dropped_in_file_becomes_a_source(self):
        self._write_plugin("demo_source.py", '''
            from memcal.sources import Source, register, deliver

            @register
            class Demo(Source):
                name = "demo"
                description = "test plugin"

                def fetch(self, conn, cfg, report, limit):
                    deliver(conn, report, stream=self.name, external_id="d1",
                            ts="__TS__", text="poker friday at 8",
                            thread="demo", person="Jordan")
        ''')
        source = sources.get("demo", self.cfg)
        self.assertIsNotNone(source, "plugin should be discoverable")
        report = source.run(self.conn, self.cfg, limit=10)
        self.assertIsNone(report.error)
        self.assertEqual(report.archived, 1)
        # The shared gate applies to plugins too — nobody gets to bypass it.
        self.assertEqual(report.passed, 1)
        rows = archive.search(self.conn, "poker")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stream"], "demo")

    def test_a_plugin_that_raises_is_contained(self):
        self._write_plugin("bad_source.py", '''
            from memcal.sources import Source, register

            @register
            class Bad(Source):
                name = "bad"
                def fetch(self, conn, cfg, report, limit):
                    raise ValueError("kaboom")
        ''')
        report = sources.get("bad", self.cfg).run(self.conn, self.cfg)
        self.assertIn("kaboom", report.error)      # reported...
        self.assertEqual(report.archived, 0)       # ...and nothing half-written

    def test_an_unimportable_plugin_does_not_break_discovery(self):
        self._write_plugin("wont_import.py", "import a_module_that_does_not_exist\n")
        self.assertIn("imessage", sources.names(self.cfg))
        self.assertTrue(any("wont_import" in err for err in sources.load_errors()))

    def test_gate_verdict_is_honoured_for_plugin_items(self):
        self._write_plugin("noise_source.py", '''
            from memcal.sources import Source, register, deliver

            @register
            class Noise(Source):
                name = "noise"

                def fetch(self, conn, cfg, report, limit):
                    for i, text in enumerate(["hey", "lol", "dinner tomorrow at 7"]):
                        deliver(conn, report, stream=self.name, external_id=f"n{i}",
                                ts="__TS__", text=text, thread="noise")
        ''')
        report = sources.get("noise", self.cfg).run(self.conn, self.cfg)
        self.assertEqual(report.archived, 3)   # everything is archived
        self.assertEqual(report.passed, 1)     # only the one with a temporal token spools


class TestICalendar(Base):
    """Calendar facts are structured; Partiful is a policy over its snapshot."""

    def tearDown(self):
        db.set_today(None)
        super().tearDown()

    def item(self, uid: str, title: str, days: int, *, calendar: str = "Personal",
             writable: bool = True, location: str = "") -> dict:
        start = datetime.combine(
            db.today() + timedelta(days=days),
            datetime.min.time(),
            tzinfo=timezone.utc,
        ).replace(hour=19)
        return {
            "calendar_name": calendar,
            "calendar_uid": f"calendar-{calendar.lower()}",
            "writable": writable,
            "uid": uid,
            "title": title,
            "start": start.isoformat(),
            "end": (start + timedelta(hours=2)).isoformat(),
            "all_day": False,
            "location": location,
            "description": "",
            "url": "",
        }

    def snapshot(self, items: list[dict]):
        return ical.ingest_snapshot(
            self.conn,
            self.cfg,
            items,
            scan_start=(db.today() - timedelta(days=120)).isoformat(),
            scan_end=(db.today() + timedelta(days=365)).isoformat(),
        )

    def test_permission_probe_is_a_real_minimal_calendar_read(self):
        calls = []

        class Done:
            returncode = 0
            stdout = '{"calendars": 4}'
            stderr = ""

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            return Done()

        ok, message = ical.permission_status(runner=runner)
        self.assertTrue(ok)
        self.assertIn("Calendar read access granted", message)
        self.assertEqual(calls[0][0][:4],
                         ["osascript", "-l", "JavaScript", "-e"])
        self.assertEqual(calls[0][1]["timeout"], 30)

    def test_permission_probe_reports_denial_and_settings_path(self):
        class Denied:
            returncode = 1
            stdout = ""
            stderr = "Not authorized to send Apple events. (-1743)"

        ok, message = ical.permission_status(runner=lambda *_a, **_kw: Denied())
        self.assertFalse(ok)
        self.assertIn("Privacy & Security", message)

    def test_status_is_passive_and_never_touches_calendar(self):
        out = io.StringIO()
        with mock.patch.object(
            ical, "permission_status",
            side_effect=AssertionError("status must not trigger Calendar"),
        ), mock.patch.object(
            schedule, "status", return_value={"installed": False},
        ), contextlib.redirect_stdout(out):
            code = cli.main(["--home", str(self.cfg.home), "ical", "status"])
        self.assertEqual(code, 1)
        self.assertIn("passive; no macOS dialogs", out.getvalue())

    def test_source_check_is_passive(self):
        # Says which kind of host it is testing rather than inheriting the answer from
        # the machine: `check` is the source's health declaration and `osascript` is
        # genuinely part of its health, so on a host without one the honest reply is
        # `False`. That is the whole difference between this and the five call sites
        # that take an injected transport.
        with mock.patch.object(
            ical, "permission_status",
            side_effect=AssertionError("source check must not trigger Calendar"),
        ), mock.patch.object(ical, "_have_osascript", return_value=True):
            ok, message = ical.ICalSource().check(self.cfg)
        self.assertTrue(ok)
        self.assertIn("explicitly", message)

    def test_source_check_says_a_host_without_osascript_is_unreachable(self):
        with mock.patch.object(
            ical, "permission_status",
            side_effect=AssertionError("source check must not trigger Calendar"),
        ), mock.patch.object(ical, "_have_osascript", return_value=False):
            ok, message = ical.ICalSource().check(self.cfg)
        self.assertFalse(ok)
        self.assertIn("osascript", message)

    def test_recurring_occurrences_with_one_uid_do_not_collide(self):
        first = self.item("weekly", "Language class", 2)
        first["recurrence"] = "FREQ=WEEKLY"
        second = self.item("weekly", "Language class", 9)
        second["recurrence"] = "FREQ=WEEKLY"
        self.snapshot([first, second])
        count = self.conn.execute(
            "SELECT count(*) AS n FROM calendar_items"
        ).fetchone()["n"]
        self.assertEqual(count, 2)
        self.assertEqual(
            self.conn.execute("SELECT count(*) AS n FROM events").fetchone()["n"], 2
        )

    def test_created_and_subscribed_calendars_have_different_semantics(self):
        report = self.snapshot([
            self.item("mine", "Dentist", 3),
            self.item("feed", "Gallery opening", 4,
                      calendar="Arts listings", writable=False),
        ])
        self.assertEqual(report.archived, 2)
        self.assertEqual(report.passed, 0)  # structured writes never spend a model call
        rows = {row.title: row for row in events.window(self.conn, 0, 10)}
        self.assertEqual(rows["Dentist"].status, "confirmed")
        self.assertEqual(rows["Dentist"].kind, "commitment")
        self.assertEqual(rows["Dentist"].source, "ical:created:Personal")
        self.assertEqual(rows["Gallery opening"].status, "mentioned")
        self.assertEqual(rows["Gallery opening"].kind, "opportunity")
        self.assertEqual(rows["Gallery opening"].source,
                         "ical:subscribed:Arts listings")
        origins = {
            db.jload(row["meta"], {})["calendar_origin"]
            for row in archive.recent(self.conn, stream="ical")
        }
        self.assertEqual(origins, {"created", "subscribed"})

    def test_created_event_reaches_brief_without_dream(self):
        report = self.snapshot([self.item("mine", "Dentist", 3)])
        rendered = brief.render(self.conn, self.cfg)
        self.assertIn("Dentist", rendered)
        self.assertIn("confirmed", rendered)
        self.assertEqual(report.passed, 0)
        self.assertEqual(len(archive.spool_pending(self.conn)), 0)

    def test_partiful_disclosed_location_means_yes_and_confirmed(self):
        self.snapshot([
            self.item("party", "Rooftop party", 5, calendar="Partiful",
                      writable=False, location="123 Orchard St"),
        ])
        row = events.window(self.conn, 0, 10)[0]
        self.assertEqual(row.kind, "commitment")
        self.assertEqual(row.status, "confirmed")
        self.assertEqual(row.location, "123 Orchard St")

    def test_one_partiful_event_missing_overnight_is_declined(self):
        db.set_today("2026-08-01")
        first = self.item("party-1", "Rooftop party", 5, calendar="Partiful",
                          writable=False, location="123 Orchard St")
        second = self.item("party-2", "Picnic", 7, calendar="Partiful",
                           writable=False, location="Prospect Park")
        self.snapshot([first, second])

        db.set_today("2026-08-02")
        report = self.snapshot([second])
        rows = {row.title: row for row in events.window(self.conn, 0, 10)}
        self.assertEqual(rows["Rooftop party"].status, "declined")
        self.assertEqual(rows["Picnic"].status, "confirmed")
        self.assertTrue(any("Partiful declined: Rooftop party" in row
                            for row in report.notes))

    def test_all_partiful_events_missing_means_unsubscribed_not_declined(self):
        db.set_today("2026-08-01")
        # A row on an ordinary calendar, present in both snapshots. It is what makes the
        # second read *"every Partiful event is gone"* rather than *"nothing came back"*,
        # which is a failed read and is judged nowhere — see
        # `TestOneUnreadableCalendarMadeEveryOtherEventLookDeleted`.
        elsewhere = self.item("chore", "Laundry", 6, calendar="Home")
        items = [
            elsewhere,
            self.item("party-1", "Rooftop party", 5, calendar="Partiful",
                      writable=False, location="123 Orchard St"),
            self.item("party-2", "Picnic", 7, calendar="Partiful",
                      writable=False, location="Prospect Park"),
        ]
        self.snapshot(items)

        db.set_today("2026-08-02")
        report = self.snapshot([elsewhere])
        self.assertIn("unsubscribe", " ".join(report.notes))
        rows = {row.title: row.status for row in events.window(self.conn, 0, 10)}
        self.assertEqual(rows["Rooftop party"], "confirmed")
        self.assertEqual(rows["Picnic"], "confirmed")
        self.assertNotIn("declined", set(rows.values()))
        active = self.conn.execute(
            "SELECT sum(active) AS n FROM calendar_items WHERE provider = 'partiful'"
        ).fetchone()["n"]
        self.assertEqual(active, 0)


class TestIMessageDecoding(unittest.TestCase):
    def test_apple_time_handles_both_epochs(self):
        self.assertTrue(imessage.apple_time(700000000).startswith("2023"))
        self.assertTrue(imessage.apple_time(700000000 * 10**9).startswith("2023"))

    def test_attributed_body_is_parsed(self):
        """The length byte here read `\x0e` for a 16-byte string until the parser
        started respecting it. Nothing noticed, because the scraper it replaced read to
        a terminator and ignored the length field entirely — so the fixture had been
        describing a blob macOS would never write."""
        blob = (b"streamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString"
                b"\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94"
                b"\x84\x01+\x10we playing at 8?\x86")
        self.assertEqual("we playing at 8?", imessage.decode_attributed(blob))


class TestTextClean(Base):
    """Post-gate volume is the entire cost story, so this is a cost test."""

    def test_quoted_reply_chain_is_cut(self):
        text = ("That's fine with me!\n\n"
                "-------- Original Message --------\n"
                "On Sunday, 06/14/26 at 11:07 Harper wrote:\n"
                "a long quoted message that we already hold from when it arrived")
        cleaned = textclean.clean_email(text)
        self.assertIn("That's fine", cleaned)
        self.assertNotIn("already hold", cleaned)

    def test_angle_quoted_blocks_are_cut(self):
        text = "sure\n> line one\n> line two\n> line three\n> line four"
        self.assertNotIn("line four", textclean.clean_email(text))

    def test_tracking_urls_keep_only_what_identifies_them(self):
        long_url = "https://click.example.com/track/abc?" + "x=1&" * 60
        cleaned = textclean.clean_email(f"see {long_url} now")
        self.assertIn("click.example.com", cleaned)
        self.assertLess(len(cleaned), 120)

    def test_footers_and_signatures_go(self):
        text = ("Dinner Thursday?\n\nSent from my iPhone\n"
                "Unsubscribe from these emails at any time\n© 2026 Example Inc")
        cleaned = textclean.clean_email(text)
        self.assertIn("Dinner Thursday?", cleaned)
        self.assertNotIn("Unsubscribe", cleaned)

    def test_it_never_eats_a_short_real_message(self):
        for text in ("poker friday at 8?", "i'm free monday night", "Re: dinner\n\nyes!"):
            self.assertIn(text.split("\n")[-1][:12], textclean.clean_email(text))

    def test_truncation_lands_on_a_boundary(self):
        text = "First sentence here. " * 40
        cut = textclean.truncate(text, 100)
        self.assertLessEqual(len(cut), 110)
        self.assertTrue(cut.endswith("…"))

    def test_the_estimator_is_pessimistic_on_junk(self):
        prose = "the quick brown fox jumps over the lazy dog " * 20
        junk = "https://x.com/a?b=1&c=2|||===" * 20
        self.assertGreater(textclean.estimate_tokens(junk) / len(junk),
                           textclean.estimate_tokens(prose) / len(prose))
        # chars/4 under-counted real email by ~2.3x; this must not.
        self.assertGreater(textclean.estimate_tokens(junk), len(junk) / 4)


class TestAutomatedSenders(Base):
    """§5.1: a machine that cannot be replied to is denied once, then never costs again."""

    def test_real_senders_from_the_live_mailbox(self):
        # Asked of `is_automated` rather than of `AUTOMATED_RE`, which is one of its
        # three clauses: `databricks-customer-success@t.databricks.com` is caught by
        # `_sending_subdomain`, and the regex alone says no. This test was shadowed by a
        # second class of the same name for as long as that clause has existed, so it
        # never once ran to say so.
        blocked = ("no.reply.alerts@chase.com", "orders@oe1.target.com",
                   "databricks-customer-success@t.databricks.com",
                   "no-reply@amazonses.com", "express@airbnb.com")
        allowed = ("jordan@example.com", "harper@example.com",
                   "rowan@example.com", "family@example.com")
        for address in blocked:
            self.assertTrue(gate.is_automated(address), address)
        for address in allowed:
            self.assertFalse(gate.is_automated(address), address)

    def test_the_decision_is_made_once(self):
        first = gate.gate_email(self.conn, address="no.reply@chase.com", subject="alert")
        self.assertFalse(first)
        self.assertEqual(first.reason, "automated-address")
        second = gate.gate_email(self.conn, address="no.reply@chase.com", subject="alert")
        self.assertEqual(second.reason, "sender-table:archive")


class TestBundleJoining(Base):
    """§6.1: splitting by source separates the things that must be joined."""

    def test_my_reply_lands_in_the_same_bundle_as_their_message(self):
        report = sources.IngestReport(stream="email")
        identity.link(self.conn, "harper@example.com", "Harper")
        sources.deliver(self.conn, report, stream="email", external_id="in1",
                        ts=db.now(), text="dinner thursday?", thread="harper@example.com",
                        handle="harper@example.com", from_me=False,
                        counterpart="harper@example.com")
        sources.deliver(self.conn, report, stream="email", external_id="out1",
                        ts=db.now(), text="yes! thursday at 7 works", thread="harper@example.com",
                        handle=None, from_me=True, counterpart="harper@example.com")
        self.conn.commit()
        entities = {r["entity"] for r in self.conn.execute("SELECT entity FROM spool")}
        self.assertEqual(entities, {"person:Harper"},
                         "both directions must land in one bundle")


class TestDiffQuality(Base):
    """Guards on what the apply stage will accept from the model.

    Each of these is a real failure seen on live data, not a hypothetical.
    """

    def _bundle(self, entity="person:Quinn"):
        aid = archive.append(self.conn, stream="imessage", external_id=f"q{entity}",
                             ts=db.now(), text="lunging with comet", thread="t",
                             person="Quinn", gated=True)
        row = self.conn.execute("SELECT * FROM archive WHERE id = ?", (aid,)).fetchone()
        self.conn.commit()
        return bundle_stage.Bundle(entity=entity, items=[row])

    def test_a_persons_page_never_lands_in_preferences(self):
        # Live failure: "favorite animal: otters" was filed under preferences/.
        identity.link(self.conn, "+15551234567", "Riley")
        outcome = apply_stage._apply_wiki(
            self.conn, self.cfg,
            {"page": "riley", "section": "preferences",
             "slot": "favorite animal", "value": "otters"},
            source="test", seen=set())
        self.assertEqual(outcome[0][0], "slot")
        self.assertEqual(wiki.read(self.cfg.wiki_dir, "riley").section, "people")

    def test_an_existing_page_keeps_its_section(self):
        wiki.ensure(self.cfg.wiki_dir, "poker-night", section="projects")
        apply_stage._apply_wiki(self.conn, self.cfg,
                                {"page": "poker-night", "section": "people",
                                 "slot": "where", "value": "42 Example Street"},
                                source="test", seen=set())
        self.assertEqual(wiki.read(self.cfg.wiki_dir, "poker-night").section, "projects")

    def test_a_sentence_shaped_slot_value_is_rejected(self):
        verbose = ("Helps care for and train Comet; gave lunging technique advice and "
                   "has been doing this for several years across multiple animals")
        outcome = apply_stage._apply_wiki(
            self.conn, self.cfg,
            {"page": "quinn", "slot": "relationship", "value": verbose},
            source="test", seen=set())
        self.assertEqual(outcome[0][0], "rejected-verbose")
        self.assertIsNone(wiki.read(self.cfg.wiki_dir, "quinn"))

    def test_the_same_slot_is_not_written_twice_in_one_run(self):
        diff = {"wiki": [
            {"page": "quinn", "slot": "relationship", "value": "trains Comet"},
            {"page": "quinn", "slot": "relationship", "value": "trains Comet"},
        ]}
        counts, log = apply_stage.apply_diffs(
            self.conn, self.cfg, [(self._bundle(), diff)], written_by="test")
        self.assertEqual(counts["wiki:slot"], 1, f"one write expected, got: {log}")


class TestSessionRegressions(Base):
    """Bugs found by reading a real Hermes session transcript.

    Session 20260726_190645_f32250: the agent tried to `answer` a to-do and was
    refused, then miscounted the date and memcal stored its confusion as a question.
    """

    def test_resolve_closes_a_todo_not_just_a_question(self):
        todos.open_todo(self.conn, "Pick up Target order #102003589753107 at Herald Square")
        ok, kind = todos.resolve(self.conn, "Target order", "picked it up yesterday")
        self.assertTrue(ok)
        self.assertEqual(kind, "todo")
        self.assertEqual(todos.open_items(self.conn), [])

    def test_resolve_still_answers_a_question(self):
        todos.ask(self.conn, "Did Tuesday dinner happen? who with?")
        ok, kind = todos.resolve(self.conn, "Tuesday dinner", "yes, with Alex")
        self.assertTrue(ok)
        self.assertEqual(kind, "question")

    def test_resolve_reports_failure_rather_than_guessing(self):
        ok, kind = todos.resolve(self.conn, "something nobody mentioned", "x")
        self.assertFalse(ok)
        self.assertEqual(kind, "")

    def test_memcal_never_stores_doubt_about_its_own_date(self):
        # Verbatim from the session — this reached the brief and stayed there.
        bad = ("You referred to yesterday as July 27 and today as July 28, but my date "
               "is Sunday July 26 — should I shift my sense of today's date?")
        self.assertEqual(todos.ask(self.conn, bad), "")
        self.assertEqual(todos.open_questions(self.conn), [])

    def test_real_questions_are_untouched(self):
        good = "You were at Lakeside on Tuesday — did you give Rowan that EZ-Pass back?"
        self.assertNotEqual(todos.ask(self.conn, good), "")
        self.assertEqual(len(todos.open_questions(self.conn)), 1)

    def test_the_brief_states_todays_date(self):
        # The agent did date arithmetic off a row label and landed two days out.
        text = brief.render(self.conn, self.cfg)
        self.assertIn(db.today().strftime("%-d %B %Y"), text)

    def test_the_cap_holds_even_when_the_marker_is_added(self):
        for i in range(60):
            events.upsert(self.conn, {"title": f"a reasonably long row title {i}",
                                      "date": self.d(i % 7)}, match=False)
        self.cfg.brief_token_cap = 90
        self.assertLessEqual(brief.approx_tokens(brief.render(self.conn, self.cfg)), 90)


class TestUserIdentity(Base):
    """Session 20260726_192834: memcal asked its owner "was that you, or a different
    Casey?" — their own duplicate contact cards had landed in the ambiguous list."""

    def setUp(self):
        super().setUp()
        identity.set_me(self.conn, "Casey", "Casey Morgan")
        for handle, person in (("+15550001", "Casey Morgan"), ("+15550002", "Casey Morg"),
                               ("+15550003", "Casey Goodwin"), ("+15550004", "Casey Iverson"),
                               ("+15550005", "Katie")):
            identity.link(self.conn, handle, person)

    def test_his_own_cards_are_him(self):
        for name in ("Casey Morgan", "Casey Morg", "Casey Morgan", "me", "Casey"):
            self.assertTrue(identity.is_me(self.conn, name), name)

    def test_other_people_named_casey_are_not_him(self):
        # Folding someone else's life into their would be worse than the original bug.
        for name in ("Casey Goodwin", "Casey Iverson", "Katie"):
            self.assertFalse(identity.is_me(self.conn, name), name)

    def test_he_is_not_listed_as_ambiguous_with_himself(self):
        # Ambiguity is only worth warning about for people actually in play, and it is
        # keyed on the colliding first name — not on the people who happen to share it.
        events.upsert(self.conn, {"title": "lunch", "date": self.d(1),
                                  "participants": ["Casey Goodwin"]})
        _listed, ambiguous = propose_stage.known_people(self.conn, self.cfg)
        self.assertEqual(list(ambiguous), ["Casey"])
        self.assertIn("Casey Goodwin", ambiguous["Casey"])
        # Their own near-duplicate cards must not appear as rival Caseys.
        self.assertNotIn("Casey Morgan", ambiguous["Casey"])
        self.assertNotIn("Casey Morg", ambiguous["Casey"])

    def test_the_prompt_says_who_he_is(self):
        prefix = propose_stage.build_prefix(self.conn, self.cfg)
        self.assertIn("THE USER IS", prefix)
        self.assertIn("Casey", prefix)

    def test_a_row_about_him_is_subject_me(self):
        # Live failure: key was `casey:lakeside-cabin-trip-with-harper@2026-07-15`.
        diff = {"events": [{"title": "Lakeside cabin trip with Harper",
                            "date": self.d(-11), "subject": "Casey",
                            "kind": "observed", "status": "happened",
                            "participants": ["Harper", "Casey Morgan"]}]}
        bundle = bundle_stage.Bundle(entity="person:me", items=[])
        apply_stage.apply_diffs(self.conn, self.cfg, [(bundle, diff)], written_by="test")
        row = events.between(self.conn, self.d(-12), self.d(-10))[0]
        self.assertEqual(row.subject, "me")
        self.assertNotIn("casey:", row.key)
        self.assertEqual(row.participants, ["Harper"], "the user is not their own participant")


class TestQuestionHygiene(Base):
    """The brief has a hard cap, so a junk question costs a real one its place."""

    def test_asking_the_user_to_ratify_our_own_note_is_blocked(self):
        for bad in ('Which Casey should confirm the "Frozen Far" campaign name?',
                    "Who should confirm this?",
                    "Should I record that as a note?"):
            self.assertEqual(todos.ask(self.conn, bad), "", bad)
        self.assertEqual(todos.open_questions(self.conn), [])

    def test_real_questions_survive(self):
        for good in ("Which Alex hosts poker — Alex or Alex Chen?",
                     "Was the session on July 26 or July 19?",
                     "Did you give Rowan the EZ-Pass back?"):
            self.assertNotEqual(todos.ask(self.conn, good), "", good)
        self.assertEqual(len(todos.open_questions(self.conn)), 3)

    def test_the_same_question_reworded_folds_into_one(self):
        first = todos.ask(self.conn, "Which Casey is the DM of the Crystal Harbor campaign?")
        again = todos.ask(self.conn, "Which Casey runs the Crystal Harbor campaign?")
        self.assertEqual(first, again)
        self.assertEqual(len(todos.open_questions(self.conn)), 1)

    def test_resolving_matches_a_reworded_question(self):
        # The agent had to retry this exact call in the session.
        todos.ask(self.conn, "Which Casey is the DM of the Crystal Harbor campaign?")
        ok, kind = todos.resolve(self.conn, "Which Casey runs the Crystal Harbor campaign",
                                 "Pat Baker is the DM")
        self.assertTrue(ok)
        self.assertEqual(kind, "question")

    def test_resolving_does_not_match_an_unrelated_question(self):
        todos.ask(self.conn, "Which Alex hosts poker on Friday?")
        ok, _kind = todos.resolve(self.conn, "what should I cook for dinner", "pasta")
        self.assertFalse(ok)


class TestAQuestionAskedAboutADayHeSpentElsewhere(Base):
    """Reject questions contradicted by the user's known location on that day."""

    def _row(self, title, when, *, status="mentioned", location=None, until=None,
             kind="commitment"):
        row, _ = events.upsert(self.conn, {
            "title": title, "date": when, "until": until, "kind": kind,
            "status": status, "subject": "me", "location": location},
            written_by="dream:test")
        return row

    def test_a_question_about_a_day_he_was_elsewhere_is_refused(self):
        self._row("Elements festival", "2026-08-07", until="2026-08-10",
                  status="confirmed", location="Blakeslee, PA")
        birthday = self._row("Dad's birthday", "2026-08-09",
                             location="Peddler's Village")
        self.assertEqual(
            todos.ask(self.conn, "Did Dad's birthday happen on Sunday? who with?",
                      about_event=birthday.id), "")
        self.assertEqual(todos.open_questions(self.conn), [])

    def test_the_refusal_leaves_a_trace(self):
        """Silence and refusal must not look the same from outside."""
        self._row("Elements festival", "2026-08-07", until="2026-08-10",
                  status="confirmed", location="Blakeslee, PA")
        birthday = self._row("Dad's birthday", "2026-08-09",
                             location="Peddler's Village")
        todos.ask(self.conn, "Did Dad's birthday happen on Sunday?",
                  about_event=birthday.id)
        rows = self.conn.execute(
            "SELECT entity FROM provenance WHERE kind='question' AND verb='refused'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("Elements festival", rows[0]["entity"])

    def test_the_same_place_is_not_a_conflict(self):
        """"The user is busy" is not the claim. Being somewhere *else* is."""
        self._row("Elements festival", "2026-08-07", until="2026-08-10",
                  status="confirmed", location="Blakeslee, PA")
        set_time = self._row("Sunrise set", "2026-08-09", location="Blakeslee, PA")
        self.assertNotEqual(
            todos.ask(self.conn, "Did Sunrise set happen on Sunday?",
                      about_event=set_time.id), "")

    def test_an_unsettled_clash_is_not_evidence(self):
        """Two guesses about one day are what questions are *for*.

        Only a `confirmed` or `happened` row can say where the user was. A row still at
        `mentioned` on the same day is the store being unsure twice, and suppressing the
        question would leave nothing able to resolve either of them.
        """
        self._row("Maybe a festival", "2026-08-09", location="Blakeslee, PA")
        birthday = self._row("Dad's birthday", "2026-08-09",
                             location="Peddler's Village")
        self.assertNotEqual(
            todos.ask(self.conn, "Did Dad's birthday happen on Sunday?",
                      about_event=birthday.id), "")

    def test_a_placeless_row_never_convicts(self):
        """Most rows carry no location, and "unknown" is not "elsewhere"."""
        self._row("Something", "2026-08-07", until="2026-08-10", status="confirmed")
        birthday = self._row("Dad's birthday", "2026-08-09",
                             location="Peddler's Village")
        self.assertNotEqual(
            todos.ask(self.conn, "Did Dad's birthday happen on Sunday?",
                      about_event=birthday.id), "")

    def test_a_placeless_subject_is_not_convicted_either(self):
        """Found by replaying the gate over the live store, which the corpus could not.

        The first version required only the *candidate* to name a place, so a question
        about a row with no location read as "elsewhere" from anything located. That
        refused "Tom Klemm asked: What time is the sealed/commander gathering on
        Saturday, August 15?" — a real question, on a day inside the Montana trip, about
        a row whose location nobody had ever recorded. Most rows have no location; a gate
        that convicts on the unknown half of the store is a muzzle.
        """
        self._row("Montana trip", "2026-08-15", until="2026-08-23",
                  status="confirmed", location="Montana")
        gathering = self._row("Sealed gathering", "2026-08-15")
        self.assertNotEqual(
            todos.ask(self.conn, "What time is the sealed gathering on Saturday?",
                      about_event=gathering.id), "")

    def test_a_nested_row_is_the_same_occasion_not_a_clash(self):
        festival = self._row("Elements festival", "2026-08-07", until="2026-08-10",
                             status="confirmed", location="Blakeslee, PA")
        breakfast = self._row("Breakfast at Elements", "2026-08-09",
                              location="Woodlands campground")
        self.conn.execute("UPDATE events SET part_of = ? WHERE id = ?",
                          (festival.id, breakfast.id))
        self.conn.commit()
        self.assertNotEqual(
            todos.ask(self.conn, "Did Breakfast at Elements happen on Sunday?",
                      about_event=breakfast.id), "")


class TestASeriesDidNotCarryItsQualitiesToAMovedInstance(Base):
    """Carry series fields to an explicitly moved occurrence."""

    def _instance(self, title, when, **fields):
        row, _ = events.upsert(self.conn, {
            "key": f"ical-{db.slugify(title)}@{when}", "title": title, "date": when,
            "kind": "commitment", "status": "happened", **fields},
            written_by="ical", match=False)
        return row

    def _series(self, title="Voice lesson", **fields):
        for day in ("2026-07-14", "2026-07-21"):
            self._instance(title, day, time="10:00", **fields)
        wiki.link_series(self.conn, self.cfg.wiki_dir)

    def test_a_moved_instance_carries_the_link_and_the_place(self):
        self._series(location="Online",
                     join_url="https://us02web.zoom.example/j/8842119")
        moved, _ = events.upsert(self.conn, {
            "title": "Voice lesson", "date": "2026-08-26", "time": "12:00",
            "kind": "commitment", "status": "mentioned"}, written_by="dream:web")
        self.assertEqual(moved.series, "voice-lesson")
        self.assertEqual(moved.location, "Online")
        self.assertEqual(moved.join_url, "https://us02web.zoom.example/j/8842119")

    def test_the_day_it_moved_to_is_the_one_thing_that_does_not_come_along(self):
        """"Temporarily moved" is the whole point: the series keeps its pattern."""
        self._series(location="Online")
        moved, _ = events.upsert(self.conn, {
            "title": "Voice lesson", "date": "2026-08-26", "time": "12:00",
            "kind": "commitment", "status": "mentioned"}, written_by="dream:web")
        self.assertEqual((moved.date, moved.time), ("2026-08-26", "12:00"))

    def test_a_stated_place_always_wins(self):
        self._series(location="Online")
        row, _ = events.upsert(self.conn, {
            "title": "Voice lesson", "date": "2026-08-26",
            "kind": "commitment", "location": "14 Example Avenue"},
            written_by="dream:web")
        self.assertEqual(row.location, "14 Example Avenue")

    def test_a_judgement_is_never_lent(self):
        """A new occurrence is not confirmed because the last one happened.

        Invariant 5. `SERIES_QUALITIES` is where it is and how you join it, and stops
        there — status, kind, participants and the note belong to the occasion.
        """
        self._series(location="Online")
        row, _ = events.upsert(self.conn, {
            "title": "Voice lesson", "date": "2026-08-26", "kind": "commitment",
            "status": "mentioned"}, written_by="dream:web")
        self.assertEqual(row.status, "mentioned")
        self.assertEqual(row.participants, [])

    def test_the_newest_instance_is_the_one_that_lends(self):
        """Invariant 4, not a vote: a series that changed venue carries the new one."""
        self._instance("Voice lesson", "2026-07-14", location="Old Hall")
        self._instance("Voice lesson", "2026-07-21", location="New Hall")
        wiki.link_series(self.conn, self.cfg.wiki_dir)
        row, _ = events.upsert(self.conn, {
            "title": "Voice lesson", "date": "2026-08-26", "kind": "commitment"},
            written_by="dream:web")
        self.assertEqual(row.location, "New Hall")

    def test_one_occasion_is_not_a_series(self):
        """Inheritance may not be looser than membership.

        `link_series` needs two distinct dates before it opens a page — the rule that
        stopped every duplicated calendar block inventing a project.
        """
        self._instance("Haircut", "2026-07-15", location="Fade Room")
        row, _ = events.upsert(self.conn, {
            "title": "Haircut", "date": "2026-09-09", "kind": "commitment"},
            written_by="dream:web")
        self.assertIsNone(row.location)

    def test_an_amendment_never_imports_a_siblings_place(self):
        """Only a *new* occurrence inherits, and the first version got this wrong.

        `fields` was enriched before `upsert` had decided insert-versus-update, so an
        amendment that happened not to restate a location would gain one — the model
        told to change a date would be changing two things, and on `poker-night`, a
        two-member series carrying the most-graded rows in the corpus, that is the
        Saturday game quietly moving house.
        """
        self._series(location="Online")
        existing, _ = events.upsert(self.conn, {
            "key": "voice@2026-08-26", "title": "Voice lesson", "date": "2026-08-26",
            "kind": "commitment"}, written_by="ical", match=False)
        self.conn.execute("UPDATE events SET location = NULL, series = NULL WHERE id = ?",
                          (existing.id,))
        self.conn.commit()
        again, verb = events.upsert(self.conn, {
            "key": "voice@2026-08-26", "title": "Voice lesson", "date": "2026-08-27"},
            written_by="dream:web")
        self.assertEqual(verb, "updated")
        self.assertIsNone(again.location)

    def test_a_weak_title_match_does_not_lend_anything(self):
        """`_title_absorbs` was the obvious reach and would reopen a known-expensive bug.

        It absorbs "Elements" into "Breakfast at Elements", which is the collision
        `part_of` exists to prevent — three rows called Elements were three unrelated
        plans. A looser rule here would lend a festival's location to a breakfast.
        """
        self._series("Elements", location="Blakeslee, PA")
        row, _ = events.upsert(self.conn, {
            "title": "Breakfast at Elements", "date": "2026-08-26",
            "kind": "commitment"}, written_by="dream:web")
        self.assertIsNone(row.location)
        self.assertIsNone(row.series)


class TestALinkIsHowYouAttendAndNotAPlace(Base):
    """"the new entry just said online as the location. the info was in my email."

    Both halves were true. `location` answers *where*, `rsvp_url` answers *how you
    reply*, and nothing answered *how you attend* — so a Zoom link had no field to land
    in, and both connectors that had one in hand dropped it: `ical._normalized` narrowed
    the calendar item to five fields, discarding the `description` the JXA has always
    lifted, and `proton._strip_html` kept the anchor's text and threw away its href. The
    live row for e125 said "Online" while the URL sat in the mail that created it.
    """

    def test_an_anchor_keeps_its_href(self):
        text = proton._strip_html(
            '<p>See you then.<br><a href="https://us02web.zoom.example/j/8842119">'
            'Tutoring Meeting Room Link</a></p>')
        self.assertIn("https://us02web.zoom.example/j/8842119", text)
        self.assertIn("Tutoring Meeting Room Link", text)

    def test_a_bare_link_is_not_printed_twice(self):
        text = proton._strip_html(
            '<a href="https://example.com/x">https://example.com/x</a>')
        self.assertEqual(text.count("https://example.com/x"), 1)

    def test_the_calendar_description_reaches_the_row(self):
        ical.ingest_snapshot(self.conn, self.cfg, [{
            "calendar_name": "Personal", "calendar_uid": "cal-1", "writable": True,
            "uid": "TUTORING-1", "title": "Tutoring appointment",
            "start": "2026-08-12T12:00:00-04:00", "end": "2026-08-12T13:00:00-04:00",
            "all_day": False, "location": "Online",
            "description": "Join Zoom Meeting\nhttps://us02web.zoom.example/j/8842119",
            "url": "",
        }], scan_start="2026-08-01", scan_end="2026-09-01")
        row = events.get_by_id(self.conn, self.conn.execute(
            "SELECT id FROM events WHERE title LIKE '%Tutoring%'").fetchone()["id"])
        self.assertEqual(row.join_url, "https://us02web.zoom.example/j/8842119")
        self.assertEqual(row.location, "Online")

    def test_a_link_that_is_not_a_meeting_room_is_left_alone(self):
        """A description is full of maps, agendas and unsubscribe footers.

        Picking "the first URL" would put a marketing page where the join button goes,
        which is why the matcher is a fixed list of conferencing hosts rather than a
        general URL regex.
        """
        self.assertIsNone(ical.join_link(
            "Directions: https://maps.example.com/x — agenda at https://docs.example/y"))
        self.assertEqual(
            ical.join_link("https://meet.google.com/abc-defg-hij"),
            "https://meet.google.com/abc-defg-hij")

    def test_a_link_with_its_scheme_stripped_is_still_a_link(self):
        """A model rewrote it that way, and the scheme-anchored matcher missed it.

        One trial in three put `us02web.zoom.example/j/8842119` in `location`, with no
        `https://` on the front, so the lift did not fire and "Online" was gone. It
        is stored with a scheme however it arrives, so two spellings are one value.
        """
        self.assertEqual(ical.join_link("us02web.zoom.example/j/8842119"),
                         "https://us02web.zoom.example/j/8842119")

    def test_trailing_punctuation_is_not_part_of_the_link(self):
        self.assertEqual(ical.join_link("Join at https://zoom.us/j/123."),
                         "https://zoom.us/j/123")

    def test_a_link_in_the_location_field_is_lifted_out_of_it(self):
        """Found on the live calendar, and the same defect from the other direction.

        With nowhere to put a join link, a calendar app puts it in `location` — so the
        store held `location: https://meet.google.com/dcp-uqon-ibe`, a link pretending
        to be a place, and on another row an address and a link sharing the field.
        """
        fields = ical._normalized({
            "uid": "MEET-1",
            "title": "Meeting with Morgan", "start": "2026-05-15T10:00:00-04:00",
            "end": "2026-05-15T11:00:00-04:00", "all_day": False,
            "location": "141 Worth St, New York, NY 10013, USA; "
                        "https://meet.google.com/noa-ubha-hvc"})
        self.assertEqual(fields["join_url"], "https://meet.google.com/noa-ubha-hvc")
        self.assertEqual(fields["location"], "141 Worth St, New York, NY 10013, USA")

    def test_a_location_that_is_only_a_link_leaves_no_empty_place(self):
        fields = ical._normalized({
            "uid": "MEET-2",
            "title": "Standup", "start": "2026-05-15T10:00:00-04:00",
            "end": "2026-05-15T11:00:00-04:00", "all_day": False,
            "location": "https://meet.google.com/dcp-uqon-ibe"})
        self.assertEqual(fields["join_url"], "https://meet.google.com/dcp-uqon-ibe")
        self.assertIsNone(fields["location"])

    def test_the_brief_shows_it_from_both_blocks(self):
        """Nine days out is exactly when knowing it is a link rather than a room helps.

        `## Later` names rather than details, on purpose — but a link is not detail, it
        is the only act the row affords. Withholding it until the week window opens is
        the column not existing for nine days.
        """
        near, _ = events.upsert(self.conn, {
            "title": "Standup", "date": db.today().isoformat(), "kind": "commitment",
            "status": "confirmed", "join_url": "https://meet.google.com/near-one"},
            written_by="live")
        far, _ = events.upsert(self.conn, {
            "title": "Tutoring appointment",
            "date": (db.today() + timedelta(days=20)).isoformat(),
            "kind": "commitment", "status": "confirmed", "location": "Online",
            "join_url": "https://us02web.zoom.example/j/8842119"}, written_by="live")
        text = brief.render(self.conn, self.cfg)
        self.assertIn("https://meet.google.com/near-one", text)
        self.assertIn("https://us02web.zoom.example/j/8842119", text)
        # `## Later` names rather than details, so it carries no location — which is
        # exactly why the link has to be there: "Online" would not have been.

    def test_a_description_that_is_not_a_link_survives_too(self):
        """"what if this exact same thing happens but with something that ISN'T a url"

        The right question, and the reason `join_url` alone would have been a band-aid.
        The field was not missing because links are special; it was missing because the
        connector returned five fields and the source offered more. An appointment whose
        description says which buzzer to ring loses exactly as much as one carrying a
        Zoom link, and no URL is involved anywhere.
        """
        # The day after today, because the assertion below is that the brief *names* the
        # row and the brief's window is today-relative. Pinned to 2026-08-12 it asserted
        # the buzzer code was reachable for four days and then asserted nothing at all.
        day = (db.today() + timedelta(days=1)).isoformat()
        ical.ingest_snapshot(self.conn, self.cfg, [{
            "calendar_name": "Personal", "calendar_uid": "cal-1", "writable": True,
            "uid": "PHYSIO-1", "title": "Physio", "start": f"{day}T09:00:00-04:00",
            "end": f"{day}T10:00:00-04:00", "all_day": False,
            "location": "Riverton Sports Medicine",
            "description": "Suite 300, ring buzzer 4. Bring your insurance card.",
            "url": "",
        }], scan_start=self.d(-1), scan_end=self.d(30))
        row = events.get_by_id(self.conn, self.conn.execute(
            "SELECT id FROM events WHERE title = 'Physio'").fetchone()["id"])
        self.assertEqual(row.note, "Suite 300, ring buzzer 4. Bring your insurance card.")
        self.assertEqual(row.location, "Riverton Sports Medicine")
        self.assertIsNone(row.join_url)
        # The brief names the row and `memcal_open` carries the detail. Asserted as a
        # *path* rather than as a substring: what this report was actually about is that
        # the buzzer code was unreachable, and "it is on the brief line" was one way to
        # be reachable, not the requirement. This form also fails if the handle stops
        # resolving, which the old one could not see.
        rendered = brief.render(self.conn, self.cfg)
        self.assertIn("Physio", rendered)
        handle = brief.SOURCE_RE.search(
            [line for line in rendered.splitlines() if "Physio" in line][0])
        self.assertTrue(handle, "a brief row with no handle cannot be opened")
        self.assertIn("buzzer 4",
                      detail.open_handle(self.conn, self.cfg, handle.group(1)))

    def test_memcals_own_published_note_is_not_read_back_as_news(self):
        """Otherwise the row cites memcal's calendar entry as evidence for itself."""
        self.assertIsNone(ical._description_note("Added by memcal. With Robbie."))

    def test_a_wall_of_dial_in_numbers_does_not_become_the_row(self):
        note = ical._description_note("x" * 900)
        self.assertLessEqual(len(note), ical.NOTE_CHARS)

    def test_a_model_may_not_put_a_link_in_the_location(self):
        """All three model trials did exactly this, which is why code takes it back out.

        Handed a link and no field for it, a model puts it in `location` — the same move
        that put a date range in `note` when `until` was unreachable. The schema told it
        to; the fix is a field and a lift, not a sentence in the prompt asking it not to.
        """
        self.assertEqual(
            apply_stage._lift_join_link("https://us02web.zoom.example/j/8842119"),
            ("https://us02web.zoom.example/j/8842119", None))
        self.assertEqual(
            apply_stage._lift_join_link("Online; https://zoom.us/j/1"),
            ("https://zoom.us/j/1", "Online"))
        self.assertIsNone(apply_stage._lift_join_link("Riverton Dental"))

    def test_it_is_published_where_a_phone_can_reach_it(self):
        """Which is `location`, not the notes.

        This asserted the notes until 2026-08-20, and the notes were the wrong answer:
        iOS builds the Join button — the one on the 12:58 notification, which is the
        only moment any of this matters — out of `location`.
        """
        event, _ = events.upsert(self.conn, {
            "title": "Tutoring appointment", "date": "2026-08-12", "time": "12:00",
            "kind": "commitment", "status": "confirmed", "location": "Online",
            "join_url": "https://us02web.zoom.example/j/8842119"}, written_by="live")
        self.assertEqual("Online; https://us02web.zoom.example/j/8842119",
                         ical.publish_location(event.location, event.join_url))
        self.assertNotIn("zoom.example", ical._published_note(event))


class TestTheLinkWasLiftedOutOfLocationAndNeverPutBack(Base):
    """`join_link` had no inverse, so memcal could read a convention it
    would not write, and every tutoring occurrence it published had an empty Join button.

    *"Tutoring memcal iCal entry doesn't have url in location field."* The user had already
    hand-edited the one-off copy to the shape the user wanted; the series publish path wrote
    "Online" over the top of it.
    """

    #: The two live rows, as they actually were on 2026-08-20.
    LINK = "https://meet.google.com/dcp-uqon-ibe"

    def test_place_and_link_share_the_field_place_first(self):
        self.assertEqual(f"Online; {self.LINK}",
                         ical.publish_location("Online", self.LINK))

    def test_a_link_with_no_place_is_the_whole_field(self):
        self.assertEqual(self.LINK, ical.publish_location(None, self.LINK))
        self.assertEqual(self.LINK, ical.publish_location("", self.LINK))

    def test_a_place_with_no_link_is_untouched(self):
        self.assertEqual("Online", ical.publish_location("Online", None))
        self.assertEqual("", ical.publish_location(None, None))

    def test_composing_twice_does_not_print_the_link_twice(self):
        once = ical.publish_location("Online", self.LINK)
        self.assertEqual(once, ical.publish_location(once, self.LINK))

    def test_it_is_the_exact_inverse_of_the_read_path(self):
        """The property that makes it safe in `_published_state`.

        If the round trip were lossy, the composed string would not match the row it
        came from and every pass would republish every event for ever.
        """
        for place, link in [("Online", self.LINK), (None, self.LINK),
                            ("141 Worth St, New York, NY 10013, USA", self.LINK),
                            ("Online", None), (None, None)]:
            composed = ical.publish_location(place, link)
            back = ical._normalized({
                "uid": "U1", "title": "Tutoring", "start": "2026-08-25T13:00:00",
                "end": "2026-08-25T14:00:00", "all_day": False, "location": composed,
                "description": "", "url": ""})
            self.assertEqual(place or None, back["location"], composed)
            self.assertEqual(link, back["join_url"], composed)

    def test_a_row_published_before_the_inverse_existed_republishes_itself(self):
        """The repair, and the reason the composed value is in the state string.

        `publish_pending` skips a row whose `published_state` still matches. Comparing
        the raw `location` column would have matched for ever, and the occurrences
        already on their calendar would have kept their empty Join button.
        """
        event, _ = events.upsert(self.conn, {
            "title": "Tutoring", "date": "2026-08-25", "time": "13:00",
            "kind": "commitment", "status": "confirmed", "location": "Online",
            "join_url": self.LINK}, written_by="live")
        self.assertNotEqual("Tutoring|2026-08-25||13:00|Online",
                            ical._published_state(event))
        self.assertIn(self.LINK, ical._published_state(event))

    def test_the_repeating_rule_publishes_the_link_too(self):
        """The row the user reported was the *series*, which is a second publish path.

        Two paths, one convention: a fix that only touched `publish` would have left the
        weekly appointment — the one the user sees every Tuesday — exactly as broken.
        """
        rule, _ = series.upsert(self.conn, {
            "slug": "tutoring", "title": "Tutoring", "cadence": "weekly", "weekday": 1,
            "time": "13:00", "location": "Online", "join_url": self.LINK,
            "effective_on": "2026-08-11"}, written_by="live")
        self.assertIn(self.LINK, ical._series_state(rule))


class TestFreshness(Base):
    """The beer garden case: a stale puller and a brief that never said so.

    Three weeks of iMessage were missing, every ingest run reported success, and the
    brief rendered a clean empty week over the gap. Nothing was wrong with the gate or
    the model — the messages were simply not there.
    """

    def _add(self, stream: str, days_ago: int, text: str = "hi") -> None:
        ts = (db.now_dt() - timedelta(days=days_ago)).isoformat()
        archive.append(self.conn, stream=stream, external_id=f"{stream}:{days_ago}",
                       ts=ts, text=text)

    def test_a_stream_that_stopped_is_reported_stale(self):
        self._add("imessage", 21)
        self._add("email", 0)
        stale = dict(archive.stale_streams(self.conn))
        self.assertIn("imessage", stale)
        self.assertNotIn("email", stale)

    def test_internal_streams_are_never_stale(self):
        # Nobody having talked to the agent for a month is not a broken puller.
        self._add("agent", 30)
        self.assertEqual(archive.stale_streams(self.conn), [])

    def test_snapshot_source_can_report_health_without_a_fake_archive_item(self):
        db.set_meta(self.conn, "source.ical.last_success", db.now())
        fresh = {row["stream"]: row for row in archive.freshness(self.conn)}
        self.assertEqual(fresh["ical"]["n"], 0)
        self.assertEqual(archive.stale_streams(self.conn), [])

    def test_the_brief_says_so_when_the_archive_is_behind(self):
        self._add("imessage", 21)
        text = brief.render(self.conn, self.cfg)
        self.assertIn("STALE", text)
        self.assertIn("imessage", text)

    def test_a_current_brief_carries_no_warning(self):
        self._add("imessage", 0)
        self.assertNotIn("STALE", brief.render(self.conn, self.cfg))


class TestADeletedSourceStopsReportingItselfStale(Base):

    def test_a_stream_with_no_source_behind_it_is_not_stale(self):
        db.set_meta(self.conn, "source.findmy.last_success",
                    (db.now_dt() - timedelta(days=9)).isoformat())
        self.assertNotIn("findmy", dict(archive.stale_streams(self.conn, cfg=self.cfg)))
        self.assertNotIn("findmy", brief.render(self.conn, self.cfg))

    def test_a_registered_stream_is_still_reported(self):
        archive.append(self.conn, stream="imessage", external_id="im:21",
                       ts=(db.now_dt() - timedelta(days=21)).isoformat(),
                       text="hi")
        self.assertIn("imessage", dict(archive.stale_streams(self.conn, cfg=self.cfg)))
        self.assertIn("imessage", brief.render(self.conn, self.cfg))

    def test_freshness_still_remembers_it(self):
        """History is past tense; the stale line is a present-tense claim about health.

        A stream that archived a thousand items and then went away really did do that,
        and `doctor` saying so is honest — the same reason its `collection_sources` rows
        stay. What it may not do is assert that something is currently wrong.
        """
        db.set_meta(self.conn, "source.findmy.last_success",
                    (db.now_dt() - timedelta(days=9)).isoformat())
        self.assertIn("findmy", {row["stream"] for row in archive.freshness(self.conn)})


class TestIngestTruncation(Base):
    """A source that stopped at its page budget must say so, not report success."""

    def test_report_summary_announces_more(self):
        report = sources.base.IngestReport(stream="imessage", read=999, archived=999)
        self.assertNotIn("more waiting", report.summary())
        report.more = True
        self.assertIn("more waiting", report.summary())

    def test_absorb_carries_more_across_a_wrapping_source(self):
        # `imessage` wraps `bluebubbles`; the truncation flag has to survive the hop.
        outer = sources.base.IngestReport(stream="imessage")
        inner = sources.base.IngestReport(stream="bluebubbles", read=5, more=True)
        outer.absorb(inner)
        self.assertTrue(outer.more)
        self.assertEqual(outer.read, 5)

    def test_absorb_keeps_the_first_error(self):
        outer = sources.base.IngestReport(stream="imessage", error="boom")
        outer.absorb(sources.base.IngestReport(stream="bluebubbles", error="later"))
        self.assertEqual(outer.error, "boom")


class TestRouting(Base):
    """Diffs were silently dropped for every model that followed the instructions."""

    def _group(self, *entities):
        return [bundle_stage.Bundle(entity=e, items=[]) for e in entities]

    def test_echoing_the_bundle_line_still_routes(self):
        group = self._group("person:Terry North")
        errors: list[str] = []
        routed = propose_stage._route(
            group, {"bundles": [{"entity": "BUNDLE person:Terry North"}]}, errors)
        self.assertEqual(errors, [])
        self.assertEqual(routed[0][0].entity, "person:Terry North")

    def test_routing_survives_case_and_spacing(self):
        group = self._group("thread:email:careers@globalrelay.net")
        errors: list[str] = []
        routed = propose_stage._route(
            group, {"bundles": [{"entity": "  BUNDLE  Thread:Email:Careers@GlobalRelay.net "}]},
            errors)
        self.assertEqual(errors, [])
        self.assertEqual(len(routed), 1)

    def test_a_partial_reply_routes_by_name_not_position(self):
        # The model returns diffs only for the bundles that had something in them.
        group = self._group("person:Jamie", "person:Quinn Brooks", "person:Mom")
        errors: list[str] = []
        routed = propose_stage._route(
            group, {"bundles": [{"entity": "BUNDLE person:Mom"}]}, errors)
        self.assertEqual(errors, [])
        self.assertEqual(routed[0][0].entity, "person:Mom")

    def test_an_unknown_bundle_is_still_dropped(self):
        group = self._group("person:Jamie", "person:Mom")
        errors: list[str] = []
        routed = propose_stage._route(
            group, {"bundles": [{"entity": "BUNDLE person:Someone Else"}]}, errors)
        self.assertEqual(routed, [])
        self.assertEqual(len(errors), 1)


class TestNote(Base):
    """The agent writes the wiki from what the user said, not from what was mined."""

    def test_a_stated_fact_lands_on_the_page(self):
        from memcal import live
        ok, message = live.note(self.conn, self.cfg, "Quinn Brooks", "likes", "Pokemon")
        self.assertTrue(ok, message)
        page = wiki.read(self.cfg.wiki_dir, "quinn-brooks")
        self.assertEqual(page.slots["likes"]["value"], "Pokemon")
        self.assertEqual(page.section, "people")

    def test_a_sentence_is_refused_because_a_slot_holds_an_answer(self):
        from memcal import live
        ok, message = live.note(self.conn, self.cfg, "Quinn Brooks", "likes",
                                "The user really likes Pokemon, " * 20)
        self.assertFalse(ok)
        self.assertIn("bare answer", message)

    def test_missing_pieces_are_refused_rather_than_guessed(self):
        from memcal import live
        ok, _ = live.note(self.conn, self.cfg, "Quinn Brooks", "likes", "   ")
        self.assertFalse(ok)

    def test_a_person_does_not_land_in_preferences(self):
        # The model once filed someone's favourite animal under the user's preferences.
        from memcal import live
        identity.link(self.conn, "+15551234567", "Quinn Brooks")
        ok, message = live.note(self.conn, self.cfg, "Quinn Brooks", "likes", "Pokemon",
                                section="preferences")
        self.assertTrue(ok)
        self.assertIn("people/", message)


class TestCatchUp(Base):
    """`ingest` means catch up, but never at the cost of hammering the far end."""

    class _Fake(sources.Source):
        name = "fake"

        def __init__(self, rounds_of_work: int, *, dry_but_claims_more: bool = False):
            self.left = rounds_of_work
            self.calls = 0
            self.dry_but_claims_more = dry_but_claims_more

        def run(self, conn, cfg, *, limit=1000):
            self.calls += 1
            report = sources.IngestReport(stream=self.name)
            if self.left > 0:
                self.left -= 1
                report.read = report.archived = 10
            report.more = self.left > 0 or self.dry_but_claims_more
            return report

    def test_it_keeps_pulling_until_the_source_runs_dry(self):
        fake = self._Fake(4)
        report = sources.catch_up(fake, self.conn, self.cfg, limit=10)
        self.assertEqual(fake.calls, 4)
        self.assertEqual(report.archived, 40)
        self.assertFalse(report.more)

    def test_a_round_that_stores_nothing_stops_the_loop(self):
        # GroupMe answers a fast loop with 429s and still reports "more waiting".
        fake = self._Fake(1, dry_but_claims_more=True)
        report = sources.catch_up(fake, self.conn, self.cfg, limit=10, rounds=25)
        self.assertEqual(fake.calls, 2)
        self.assertFalse(report.more)
        self.assertTrue(any("added nothing" in n for n in report.notes))

    def test_the_round_cap_is_reported_rather_than_hidden(self):
        fake = self._Fake(100)
        report = sources.catch_up(fake, self.conn, self.cfg, limit=10, rounds=3)
        self.assertEqual(fake.calls, 3)
        self.assertTrue(report.more)
        self.assertTrue(any("more waiting" in n for n in report.notes))

    def test_an_error_stops_the_loop(self):
        class Broken(sources.Source):
            name = "broken"

            def __init__(self):
                self.calls = 0

            def run(self, conn, cfg, *, limit=1000):
                self.calls += 1
                return sources.IngestReport(stream="broken", error="server down", more=True)

        broken = Broken()
        sources.catch_up(broken, self.conn, self.cfg, limit=10)
        self.assertEqual(broken.calls, 1)


class TestUrlShortening(Base):
    def test_a_hostless_url_does_not_take_down_the_stream(self):
        # One malformed link in one marketing email aborted the whole email ingest.
        for bad in ("https:///weird/path", "http://", "https://www."):
            self.assertTrue(textclean.clean_email(f"see {bad} for details"))

    def test_a_normal_url_still_shortens_to_host_and_path(self):
        out = textclean.clean_email("rsvp at https://partiful.com/e/abc123?utm_source=x")
        self.assertIn("partiful.com", out)
        self.assertNotIn("utm_source", out)


class TestSpoolHorizon(Base):
    """A ten-year mailbox backfill must not become a ten-year model bill.

    The gate judges text and has no opinion about age: "poker Saturday" from 2019
    passes on exactly the same words as "poker Saturday" from yesterday.
    """

    def _spool(self, days_ago: int, text: str = "poker saturday at 8?") -> int:
        ts = (db.today() - timedelta(days=days_ago)).isoformat() + "T12:00:00"
        report = sources.IngestReport(stream="email")
        return sources.deliver(self.conn, report, stream="email",
                               external_id=f"e{days_ago}", ts=ts, text=text,
                               thread="t", handle="a@b.com")

    def test_ancient_traffic_is_archived_but_never_spooled(self):
        self._spool(4000)
        self.assertEqual(len(archive.spool_pending(self.conn)), 0)
        self.assertEqual(
            self.conn.execute("SELECT count(*) n FROM archive").fetchone()["n"], 1)

    def test_recent_traffic_still_spools(self):
        self._spool(2)
        self.assertEqual(len(archive.spool_pending(self.conn)), 1)

    def test_retiring_leaves_the_archive_searchable(self):
        # Retire is not delete — `memcal_search_archive` must still find it.
        self._spool(2)
        archive.spool_retire(self.conn, db.today().isoformat())
        self.assertEqual(len(archive.spool_pending(self.conn)), 0)
        self.assertEqual(len(archive.search(self.conn, "poker")), 1)

    def test_the_budget_goes_to_the_newest_traffic(self):
        # Oldest-first ordering spent the whole run on 2017 before reaching this week.
        for days in (25, 20, 10, 1):
            self._spool(days)
        rows = archive.spool_pending(self.conn, limit=2)
        self.assertEqual([str(r["ts"])[:10] for r in rows],
                         [(db.today() - timedelta(days=d)).isoformat() for d in (10, 1)])

    def test_the_horizon_is_configurable(self):
        # "We probably only need the past three months. Let's start with one month."
        self.assertEqual(self.cfg.spool_horizon_days, 30)
        report = sources.IngestReport.opened("email", self.cfg)
        self.assertEqual(report.horizon_days, 30)
        self.cfg.spool_horizon_days = 90
        self.assertEqual(sources.IngestReport.opened("email", self.cfg).horizon_days, 90)


class TestMarketingMailWasAboutToBeReadAtFullPrice(Base):

    def test_unreplyable_anywhere_in_the_local_part(self):
        # Anchoring at the start meant only a bare `noreply@` was caught.
        for address in ("ads-account-noreply@google.com",
                        "google-maps-noreply@google.com",
                        "system-noreply@nyuce.brightspace.com",
                        "no.reply.alerts@chase.com",
                        "upcoming-invoice+acct_1onsbv@stripe.com"):
            self.assertTrue(gate.is_automated(address), address)

    def test_bulk_sending_subdomains_above_the_first_label(self):
        # The old pattern only looked at the label straight after the `@`.
        for address in ("disneyplus@trx.mail2.disneyplus.com",
                        "geico@et.geico.com",
                        "cvs@mynotifications.cvs.com",
                        "legal@updates.bystadium.com",
                        "rewards@customer-mail.smile.io",
                        "panera@m1.panerabread.com"):
            self.assertTrue(gate.is_automated(address), address)

    def test_people_still_get_through(self):
        # The default has to stay permissive: a Partiful invite from a stranger, a
        # recruiter, a friend on a vanity domain. Blocking these is the worse failure.
        for address in ("person@example.com", "advisor@example.com",
                        "rowan@example.com", "howdy@antiordinary.co",
                        "recruiter@example.com", "harper@example.com",
                        "quinn@example.com", "casey@example.com"):
            self.assertFalse(gate.is_automated(address), address)

    def test_the_registrable_domain_is_never_treated_as_a_sender_label(self):
        # `mail.com` and `news.com` are somebody's actual mailbox, not a sending host.
        self.assertFalse(gate.is_automated("quinn@example.com"))
        self.assertFalse(gate.is_automated("someone@track.io"))
        self.assertTrue(gate.is_automated("someone@track.io.example.com"))

    def test_a_malformed_address_is_not_automated(self):
        self.assertFalse(gate.is_automated(""))
        self.assertFalse(gate.is_automated("not-an-address"))


class TestAnInferenceCannotOverwriteAnObservation(Base):
    """A subscribed calendar knows when its own events are. A friend mentioning one in
    passing is reporting, and may be reporting the wrong week.

    The live failure: a chat proposal moved a subscribed festival from 2026-08-07 to
    2026-08-01 and rewrote its source to the friend who had mentioned it. Nothing ranked
    the two claims, so last write won, and the user corrected it by hand.
    """

    def _ical_row(self):
        event, _ = self.conn and events.upsert(
            self.conn, {"title": "elements", "date": self.d(7),
                        "source": "ical:subscribed:ical"}, written_by="ical")
        return event

    def test_a_chat_proposal_cannot_move_a_subscribed_date(self):
        ical = self._ical_row()
        after, verb = events.upsert(
            self.conn, {"title": "Elements", "date": self.d(1),
                        "participants": ["Rowan Vale"], "source": "person:Rowan Vale"},
            written_by="dream:web")
        self.assertEqual(after.date, ical.date, "the calendar's date is the answer")
        self.assertEqual(verb, "updated")

    def test_but_it_still_gains_what_the_conversation_knew(self):
        """Refusing the date must not refuse the row. A guest list, a place or a note
        from the conversation is real information the calendar export did not have."""
        self._ical_row()
        after, _ = events.upsert(
            self.conn, {"title": "Elements", "date": self.d(1),
                        "participants": ["Rowan Vale"], "location": "Lakewood",
                        "source": "person:Rowan Vale"}, written_by="dream:web")
        self.assertIn("Rowan Vale", after.participants)
        self.assertEqual(after.location, "Lakewood")

    def test_the_user_may_still_correct_it(self):
        ical = self._ical_row()
        fixed, _ = events.upsert(self.conn, {"key": ical.key, "date": self.d(9)},
                                 written_by="live")
        self.assertEqual(fixed.date, self.d(9))

    def test_origin_records_where_a_row_came_from_not_who_touched_it_last(self):
        """`source` is mutable, so it answered "who wrote this last" while being read as
        "where did this come from" — which is how a visit by one person came to display
        the unrelated conversation that amended it afterwards."""
        ical = self._ical_row()
        after, _ = events.upsert(
            self.conn, {"title": "Elements", "date": ical.date,
                        "note": "bring a tent", "source": "person:Rowan Vale"},
            written_by="dream:web")
        self.assertEqual(after.origin, "ical:subscribed:ical")
        self.assertEqual(after.source, "person:Rowan Vale")
        self.assertNotIn("origin", events.MUTABLE, "origin must be unreachable by a diff")
        # Its neighbouring invariant matters too: `events.key` embeds the date it
        # was minted with, so it would orphan `provenance.ref`, `evidence.ref` and
        # `calendar_items.event_key` the moment anything re-dated a row *and* re-keyed
        # it. Nothing here does: the key is written in the INSERT and is not mutable.
        # The one time a row was re-keyed it was an out-of-band raw write, and it took
        # `_relocate` to repair the wreckage.
        self.assertNotIn("key", events.MUTABLE, "a key is minted once and never moves")


class TestWeakMatchBoundaries(Base):
    """A plan for next Sunday is not something that happened last week.

    A beer garden with Quinn and Jamie on Aug 2 was absorbed by a Jul 23 lunch with
    Quinn and Julian: one shared friend, and the word "quinn" in both titles doing
    duty as though it were independent corroboration.
    """

    def test_a_shared_name_alone_does_not_merge_two_occasions(self):
        events.upsert(self.conn, {
            "title": "Lunch then packs at Demo Court with Quinn and Julian",
            "date": self.d(-3), "participants": ["Quinn Brooks", "Avery Morgan"]})
        _, verb = events.upsert(self.conn, {
            "title": "Hang out with Jamie and Quinn", "date": self.d(7),
            "participants": ["Jamie", "Quinn Brooks"]})
        self.assertEqual(verb, "inserted")
        self.assertEqual(len(events.window(self.conn, 10, 10)), 2)

    def test_a_weak_match_does_not_reach_across_today(self):
        events.upsert(self.conn, {"title": "drinks", "date": self.d(-4),
                                  "participants": ["Quinn Brooks"]})
        _, verb = events.upsert(self.conn, {"title": "drinks", "date": self.d(4),
                                            "participants": ["Quinn Brooks"]})
        # Same title is a strong (confidence 2) match, so this one is allowed to move.
        self.assertEqual(verb, "updated")

    def test_something_that_happened_is_never_re_dated_forwards(self):
        past, _ = events.upsert(self.conn, {
            "title": "beer garden", "date": self.d(-5), "status": "happened",
            "participants": ["Quinn Brooks"]})
        future, verb = events.upsert(self.conn, {
            "title": "beer garden", "date": self.d(6), "participants": ["Quinn Brooks"]})
        self.assertEqual(verb, "inserted")
        self.assertNotEqual(future.id, past.id)
        self.assertEqual(events.get(self.conn, past.key).date, self.d(-5))

    def test_a_refused_match_on_the_same_day_does_not_crash_the_pass(self):
        """The guards above decide two rows are different occasions. The key minter,
        which is a pure function of title and date, then mints the same name for both.

        On the same *day* those two facts meet and the insert violated
        `events.key UNIQUE`. `apply_diffs` has no per-row guard, so that raised through
        the whole stage: a cold start died part-way, every later bundle went unwritten,
        and the spool rows stayed queued — which reads afterwards as a model that found
        nothing rather than as a crash.
        """
        day = self.d(6)
        past, _ = events.upsert(self.conn, {
            "title": "Poker", "date": day, "status": "happened"})
        second, verb = events.upsert(self.conn, {
            "title": "Poker", "date": day, "status": "confirmed"})
        self.assertEqual(verb, "inserted")
        self.assertNotEqual(second.key, past.key)
        self.assertEqual(second.status, "confirmed")
        self.assertEqual(events.get(self.conn, past.key).status, "happened")

    def test_a_weak_match_pools_detail_without_moving_the_date(self):
        """The live failure: one plan, three conversations, three proposals.

        The group thread settled it — "Did we say Saturday?" / "We said Sunday" — and
        wrote Sunday. An unrelated thread about trust paperwork mentioned "julian is
        probably coming over next weekend to go to a beer garden", dated that fragment
        Saturday, and matched on participant overlap alone. Merging the two is right;
        letting the passing mention relocate the settled one is not, and which of them
        won was decided by which parallel call happened to finish last.
        """
        settled, _ = events.upsert(self.conn, {
            "title": "Beer garden at Bohemian Hall", "date": self.d(6),
            "time": "after 6", "participants": ["Quinn Brooks", "Jamie"]})
        merged, verb = events.upsert(self.conn, {
            "title": "Beer garden with Julian", "date": self.d(5),
            "participants": ["Avery Morgan", "Quinn Brooks"]})
        self.assertEqual(verb, "updated")
        self.assertEqual(merged.id, settled.id)      # same event, correctly pooled
        self.assertEqual(merged.date, self.d(6))     # but it did not move
        self.assertIn("Avery Morgan", merged.participants)

    def test_a_real_reschedule_still_merges(self):
        # Canonical case: "the game moved to sat" must still update in place.
        first, _ = events.upsert(self.conn, {"title": "Poker at Jordan's", "date": self.d(3),
                                             "series": "poker-night"})
        moved, verb = events.upsert(self.conn, {"title": "Poker", "date": self.d(5),
                                                "series": "poker-night"})
        self.assertEqual(verb, "updated")
        self.assertEqual(moved.id, first.id)

    def test_titles_still_match_on_a_real_shared_word(self):
        events.upsert(self.conn, {"title": "Beer garden in Harbor Point", "date": self.d(6),
                                  "participants": ["Quinn Brooks"]})
        _, verb = events.upsert(self.conn, {"title": "Beer garden with Quinn", "date": self.d(7),
                                            "participants": ["Quinn Brooks"]})
        self.assertEqual(verb, "updated")


class TestAmbiguityIsAboutFirstNames(Base):
    """The warning must name the collision, not the people caught in it.

    The prefix used to print full names under "AMBIGUOUS FIRST NAMES (never resolve
    these...)" — so Pat Baker and Dana Cole, both unmistakable people with their own
    wiki pages, were flagged untouchable. Refusing to write about someone you have the
    full name of loses everything about them, silently.
    """

    def setUp(self):
        super().setUp()
        for handle, person in (("+15550000001", "Pat Baker"), ("+15550000002", "Pat Stone"),
                               ("+15550000003", "Dana Cole"), ("+15550000004", "Katie")):
            identity.link(self.conn, handle, person)
        events.upsert(self.conn, {"title": "session", "date": self.d(1),
                                  "participants": ["Pat Baker", "Dana Cole", "Katie"]})

    def test_the_key_is_the_colliding_first_name(self):
        _listed, ambiguous = propose_stage.known_people(self.conn, self.cfg)
        self.assertEqual(sorted(ambiguous), ["Pat"])
        self.assertEqual(ambiguous["Pat"], ["Pat Baker", "Pat Stone"])

    def test_an_unambiguous_person_is_not_flagged(self):
        _listed, ambiguous = propose_stage.known_people(self.conn, self.cfg)
        flagged = {name for names in ambiguous.values() for name in names}
        self.assertNotIn("Dana Cole", flagged)
        self.assertNotIn("Katie", flagged)

    def test_the_prefix_shows_the_name_and_its_candidates(self):
        prefix = propose_stage.build_prefix(self.conn, self.cfg)
        self.assertIn("Pat → Pat Baker, Pat Stone", prefix)
        # ...and says plainly that a full name is still usable.
        self.assertIn("full name", prefix)

    def test_the_user_is_never_listed_as_another_person(self):
        # Streams stamp the user's own messages `person = "me"`.
        archive.append(self.conn, stream="groupme", external_id="g1",
                       ts=db.now(), text="poker friday?", person="me", from_me=1)
        listed, _ambiguous = propose_stage.known_people(self.conn, self.cfg)
        self.assertNotIn("me", listed)
        self.assertNotIn("PEOPLE KNOWN: me", propose_stage.build_prefix(self.conn, self.cfg))


class TestARowPointsAtTheLinesItCameFrom(Base):
    """Attach each row to cited lines instead of the full bundle."""

    def _bundle(self, texts):
        rows = []
        for i, text in enumerate(texts):
            aid = archive.append(self.conn, stream="imessage", external_id=f"cite:{i}",
                                 ts=db.now(), text=text, thread="+15550001111",
                                 person="Quinn Brooks", gated=True)
            rows.append(self.conn.execute(
                "SELECT * FROM archive WHERE id = ?", (aid,)).fetchone())
        self.conn.commit()
        return bundle_stage.Bundle(entity="person:Quinn Brooks", items=rows)

    def test_the_rendered_line_tag_resolves_to_that_message(self):
        bundle = self._bundle(["hi", "poker saturday at mine", "cool"])
        text = bundle.render("v1")
        self.assertIn("L2 ", text)
        self.assertEqual(bundle.cite(["L2"]), [int(bundle.items[1]["id"])])

    def test_a_cited_row_gets_only_the_lines_it_named(self):
        bundle = self._bundle(["hi", "poker saturday at mine", "ok see you", "unrelated"])
        diff = {"events": [{"title": "Poker", "date": self.d(3), "cites": ["L2"]}],
                "todos": [], "wiki": [], "standing": [], "questions": []}
        propose_stage._resolve_cites(bundle, diff)
        apply_stage.apply_diffs(self.conn, self.cfg, [(bundle, diff, "gen-c")],
                                written_by="dream:test", run_id=None, stage="propose")
        attached = [r["archive_id"] for r in self.conn.execute(
            "SELECT archive_id FROM evidence WHERE kind='event'")]
        self.assertEqual(attached, [int(bundle.items[1]["id"])])

    def test_an_uncited_row_gets_the_lines_that_mention_it(self):
        """`[]` means "I cannot narrow it", and code can narrow it anyway.

        The whole bundle was the old answer and it is a bad one at scale: a question
        about Spider-Man carried 1,725 lines of an unrelated conversation as its
        receipt. Shared words are enough to find the line that said it, and
        `trace.source_rows` still shows the "yeah" that answered it, marked as context
        rather than as evidence.
        """
        bundle = self._bundle(["dinner is happening thursday", "yeah", "ok"])
        diff = {"events": [{"title": "Dinner", "date": self.d(2), "cites": []}],
                "todos": [], "wiki": [], "standing": [], "questions": []}
        propose_stage._resolve_cites(bundle, diff)
        apply_stage.apply_diffs(self.conn, self.cfg, [(bundle, diff, "gen-d")],
                                written_by="dream:test", run_id=None, stage="propose")
        attached = {r["archive_id"] for r in self.conn.execute(
            "SELECT archive_id FROM evidence WHERE kind='event'")}
        self.assertEqual(attached, {int(bundle.items[0]["id"])})

    def test_a_row_no_line_mentions_still_gets_the_whole_bundle(self):
        """The fallback survives for what it was always for: a row that is the gist of
        an exchange rather than a claim in any one line of it."""
        bundle = self._bundle(["yeah", "ok", "sounds good"])
        diff = {"events": [{"title": "Dinner", "date": self.d(2), "cites": []}],
                "todos": [], "wiki": [], "standing": [], "questions": []}
        propose_stage._resolve_cites(bundle, diff)
        apply_stage.apply_diffs(self.conn, self.cfg, [(bundle, diff, "gen-d2")],
                                written_by="dream:test", run_id=None, stage="propose")
        attached = {r["archive_id"] for r in self.conn.execute(
            "SELECT archive_id FROM evidence WHERE kind='event'")}
        self.assertEqual(attached, {int(r["id"]) for r in bundle.items})

    def test_a_citation_out_of_range_is_dropped_not_guessed(self):
        """A wrong pointer is worse than a wide one, so anything unrecognised falls
        back to the bundle rather than resolving to whatever line is nearest."""
        bundle = self._bundle(["a", "b"])
        self.assertEqual(bundle.cite(["L99"]), [])
        self.assertEqual(bundle.cite(["", None, "L0", "banana"]), [])
        diff = {"events": [{"title": "Thing", "date": self.d(1), "cites": ["L99"]}],
                "todos": [], "wiki": [], "standing": [], "questions": []}
        propose_stage._resolve_cites(bundle, diff)
        apply_stage.apply_diffs(self.conn, self.cfg, [(bundle, diff, "gen-e")],
                                written_by="dream:test", run_id=None, stage="propose")
        attached = {r["archive_id"] for r in self.conn.execute(
            "SELECT archive_id FROM evidence WHERE kind='event'")}
        self.assertEqual(attached, {int(r["id"]) for r in bundle.items})

    def test_citing_a_line_narrows_the_window_a_date_may_land_in(self):
        """A bundle spanning weeks admits almost any date; two cited lines do not.

        `_horizon` is the guard against a row landing in the wrong week — the failure
        that put three rows in the previous year. Anchored on the whole bundle it is as
        wide as the conversation, which for a month-long thread is barely a guard at
        all. Anchored on the lines the row cites, it is a few days either side of the
        message that actually said it.
        """
        rows = []
        for i, day in enumerate((60, 30, 1)):        # a bundle spanning two months
            aid = archive.append(self.conn, stream="imessage", external_id=f"h:{i}",
                                 ts=f"{self.d(-day)}T12:00:00-04:00", text=f"line {i}",
                                 thread="+15550002222", person="Quinn Brooks", gated=True)
            rows.append(self.conn.execute(
                "SELECT * FROM archive WHERE id = ?", (aid,)).fetchone())
        self.conn.commit()
        bundle = bundle_stage.Bundle(entity="person:Quinn Brooks", items=rows)

        wide_lo, wide_hi = apply_stage._horizon(bundle)
        tight_lo, tight_hi = apply_stage._horizon(bundle, [int(rows[-1]["id"])])
        self.assertGreater(tight_lo, wide_lo, "citing a recent line must raise the floor")
        self.assertEqual((tight_hi - tight_lo).days, (wide_hi - wide_lo).days - 59)

        # An unrecognised citation must fall back to the bundle, never to an empty set.
        self.assertEqual(apply_stage._horizon(bundle, [999999]), (wide_lo, wide_hi))

    def test_a_tag_is_resolved_before_it_can_leave_its_own_bundle(self):
        """`L7` means the seventh line *of this conversation* and nothing anywhere else.

        `resolve` merges rows across bundles and keeps one of them, so a tag still in
        `L` form when it got there would be read against a conversation it was never
        written about — evidence pointing confidently at the wrong message, which is
        worse than the bundle-wide evidence this replaced. Converting at the propose
        boundary is what makes the tag safe to carry, and this asserts the tag does not
        survive that boundary.
        """
        one = self._bundle(["one-A", "one-B"])
        two = bundle_stage.Bundle(entity="person:Other", items=list(reversed(one.items)))
        diff = {"events": [{"title": "X", "date": self.d(1), "cites": ["L1"]}],
                "todos": [], "wiki": [], "standing": [], "questions": []}
        propose_stage._resolve_cites(one, diff)
        row = diff["events"][0]
        self.assertNotIn("cites", row, "the bundle-scoped tag must not survive")
        self.assertEqual(row["cite_ids"], [int(one.items[0]["id"])])
        # The same tag against the other bundle would have named a different message.
        self.assertNotEqual(one.cite(["L1"]), two.cite(["L1"]))


class TestAnAgentCanReachTheMessageBehindARow(Base):
    """The sharpest asymmetry between the web UI and the agent surface.

    The web UI has had an "Original source" panel for a while. An agent reading `〔E119〕`
    in the brief had no way to reach the email behind it at all: `memcal_search_archive`
    is full-text only and truncates every hit to 300 characters, so "what did the
    invitation actually say?" was answerable by a person clicking and not by the agent
    being asked to answer it.
    """

    def _server(self):
        from memcal.mcp_server import Server                    # noqa: PLC0415
        server = Server.__new__(Server)
        server.conn, server.cfg = self.conn, self.cfg
        return server

    def test_the_tool_returns_the_untruncated_message(self):
        long_body = "Doors open at 6pm. " + ("filler " * 200) + "The film is Off the Rails."
        aid = archive.append(self.conn, stream="email", external_id="src:1", ts=db.now(),
                             text=long_body, thread="hannah@example.org",
                             handle="hannah@example.org", gated=True)
        self.conn.commit()
        event, _ = events.upsert(self.conn, {"title": "Movie night", "date": self.d(1)})
        trace.stamp(self.conn, kind="event", ref=event.key, verb="inserted",
                    entity="thread:email:hannah@example.org", archive_ids=[aid])

        out = self._server().call("memcal_source", {"ref": event.key})
        self.assertIn("Off the Rails", out, "the tail of the body must survive")
        self.assertGreater(len(out), 300, "truncation is what this tool exists to avoid")

    def test_a_row_with_no_recorded_source_says_so(self):
        event, _ = events.upsert(self.conn, {"title": "Typed in by hand",
                                             "date": self.d(1)})
        out = self._server().call("memcal_source", {"ref": event.key})
        self.assertIn("no source", out.lower())

    def test_a_typed_live_write_records_provenance(self):
        """Every timeline entry read "live" because typed writes recorded nothing at
        all — there was no run and no call to show, so the panel had nothing to render
        but the word itself."""
        from memcal import live                                  # noqa: PLC0415
        event, _ = live.add_event(self.conn, self.cfg, title="Dinner", when=self.d(2))
        rows = self.conn.execute(
            "SELECT stage, entity, verb FROM provenance WHERE kind='event' AND ref=?",
            (event.key,)).fetchall()
        self.assertTrue(rows, "a write the user made is worth tracing too")
        self.assertEqual(rows[0]["stage"], "live")
        self.assertEqual(rows[0]["entity"], "agent:live")


class TestTraceRecording(Base):
    """A generation id is the only way back to what was actually sent.

    OpenRouter stores the full prompt, reasoning and completion for every call but has
    no endpoint that lists them. The id arrives in the response and used to be dropped,
    so a run that behaved oddly left nothing behind but token counts.
    """

    class _Reply:
        def __init__(self, gid):
            self.generation_id = gid
            self.model = "anthropic/claude-sonnet-5"
            self.usage = type("U", (), {"prompt_tokens": 100, "completion_tokens": 20,
                                        "cost": 0.01})()

    def setUp(self):
        super().setUp()
        self.conn.execute("INSERT INTO runs(id, started_at, mode) VALUES(7, ?, 'nightly')",
                          (db.now(),))
        self.conn.commit()

    def test_an_id_is_recorded_and_findable_by_run(self):
        trace.record(self.conn, run_id=7, stage="propose", label="person:Harper",
                     reply=self._Reply("gen-abc"))
        rows = trace.find(self.conn, "7")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["generation_id"], "gen-abc")
        self.assertEqual(rows[0]["stage"], "propose")

    def test_it_is_findable_by_generation_id_and_by_label(self):
        trace.record(self.conn, run_id=7, stage="propose", label="person:Quinn Brooks",
                     reply=self._Reply("gen-xyz"))
        self.assertEqual(len(trace.find(self.conn, "gen-xyz")), 1)
        self.assertEqual(len(trace.find(self.conn, "Quinn")), 1)

    def test_recording_the_same_call_twice_is_a_no_op(self):
        for _ in range(2):
            trace.record(self.conn, run_id=7, stage="propose", label="x",
                         reply=self._Reply("gen-dup"))
        self.assertEqual(len(trace.find(self.conn, "gen-dup")), 1)

    def test_a_reply_without_an_id_is_skipped_silently(self):
        trace.record(self.conn, run_id=7, stage="propose", label="x",
                     reply=self._Reply(""))
        self.assertEqual(trace.recent(self.conn), [])

    def test_a_trace_for_a_run_that_does_not_exist_is_dropped_not_fatal(self):
        # The foreign key is what makes traces disappear with their run; a stale id
        # must still not take down the pass that produced it.
        trace.record(self.conn, run_id=999, stage="propose", label="x",
                     reply=self._Reply("gen-orphan"))
        self.assertEqual(trace.find(self.conn, "gen-orphan"), [])

    def test_a_live_call_has_no_run_and_is_still_recorded(self):
        trace.record(self.conn, run_id=None, stage="live", label="the user just said something",
                     reply=self._Reply("gen-live"))
        self.assertEqual(len(trace.find(self.conn, "gen-live")), 1)

    def test_render_lays_out_prompt_reasoning_and_completion(self):
        content = {
            "input": {"messages": [
                {"role": "system", "content": [{"type": "text", "text": "you maintain memcal"}]},
                {"role": "user", "content": "BUNDLE person:Harper"}]},
            "output": {"reasoning": "weighing whether this is a fact",
                       "completion": '{"bundles":[]}'},
        }
        out = trace.render(content)
        self.assertIn("SYSTEM", out)
        self.assertIn("you maintain memcal", out)
        self.assertIn("BUNDLE person:Harper", out)
        self.assertIn("weighing whether this is a fact", out)
        self.assertIn("COMPLETION", out)

    def test_render_survives_a_completion_that_is_not_json(self):
        out = trace.render({"input": {}, "output": {"completion": "not json at all"}})
        self.assertIn("not json at all", out)


class TestOpenQuestionsFraming(Base):
    """An open question means unresolved, not handled."""

    def test_the_question_is_beside_its_conversation_not_in_every_prefix(self):
        key = todos.ask(self.conn, "Which Sunday is the beer garden?")
        trace.stamp(self.conn, kind="question", ref=key, verb="asked",
                    entity="person:Quinn Brooks", stage="propose")
        archive_id = archive.append(
            self.conn, stream="imessage", external_id="beer-garden-reply",
            ts=db.now(), text="Maybe the beer garden after I get back",
            thread="quinn", person="Quinn Brooks", gated=True)
        row = self.conn.execute("SELECT * FROM archive WHERE id = ?", (archive_id,)).fetchone()
        bundle = bundle_stage.Bundle(entity="person:Quinn Brooks", items=[row])
        prefix = propose_stage.build_prefix(self.conn, self.cfg)
        block = propose_stage.build_bundle_block(self.cfg, bundle, self.conn)
        self.assertNotIn("Which Sunday is the beer garden?", prefix)
        self.assertIn("Which Sunday is the beer garden?", block)
        self.assertIn(key, block)
        self.assertIn("MEMCAL HINT (not source evidence)", block)


class TestQuestionCoverageRepair(Base):

    def _bundle(self):
        archive_id = archive.append(
            self.conn, stream="imessage", external_id="coverage-line", ts=db.now(),
            text="I am away through August", thread="quinn", person="Quinn Brooks",
            gated=True)
        row = self.conn.execute("SELECT * FROM archive WHERE id = ?", (archive_id,)).fetchone()
        return bundle_stage.Bundle(entity="person:Quinn Brooks", items=[row])

    def _reviews(self, bundle):
        bid = propose_stage.bundle_id(bundle.entity)
        candidate = questions.Candidate(
            key="q:league", text="When will you next play League with Quinn?",
            version="v1", wake_condition=None, likely_lines=(1,))
        return bid, {bid: {"bundle": bid, "entity": bundle.entity,
                           "candidates": [candidate], "overflow": 0}}

    def test_one_narrow_repair_fills_only_the_missing_disposition(self):
        from memcal import llm
        bundle = self._bundle()
        bid, reviews = self._reviews(bundle)

        class Client:
            def __init__(self):
                self.calls = []

            def complete(inner, **kw):
                inner.calls.append(kw)
                if kw["schema_name"] == "memcal_question_repair":
                    data = {"diffs": [{"bundle": bid, "questions": [{
                        "action": "keep", "key": "q:league", "version": "v1",
                        "text": None, "answer": None, "wake_condition": None,
                        "cites": [],
                    }]}]}
                else:
                    data = {"reviewed": [bid], "diffs": []}
                return llm.Reply(text=json.dumps(data), data=data, usage=llm.Usage(calls=1),
                                 model="test", generation_id=f"g{len(inner.calls)}")

        client = Client()
        _group, payload, turns = propose_stage.propose_group(
            client, self.cfg, "prefix", [bundle], suffix="source", reviews=reviews)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual([turn.stage for turn in turns], ["", "question-repair"])
        self.assertEqual(payload["diffs"][0]["questions"][0]["key"], "q:league")
        self.assertEqual(payload["diffs"][0]["questions"][0]["_generation_id"], "g2")
        self.assertIn("Review only the entries below", client.calls[1]["turns"][-1]["content"])

    def test_an_incomplete_repair_defers_the_whole_bundle(self):
        from memcal import llm
        bundle = self._bundle()
        bid, reviews = self._reviews(bundle)

        class Client:
            def complete(self, **kw):
                data = ({"diffs": []} if kw["schema_name"] == "memcal_question_repair"
                        else {"reviewed": [bid], "diffs": [{"bundle": bid,
                              "events": [{"title": "should also be deferred"}]}]})
                return llm.Reply(text=json.dumps(data), data=data, usage=llm.Usage(calls=1),
                                 model="test", generation_id="g")

        _group, payload, _turns = propose_stage.propose_group(
            Client(), self.cfg, "prefix", [bundle], suffix="source", reviews=reviews)
        self.assertNotIn(bid, payload["reviewed"])
        self.assertEqual(payload["diffs"], [])
        self.assertTrue(any("incomplete question review" in error
                            for error in payload["_coverage_errors"]))


class TestQuestionActionsAreVersioned(Base):

    def test_a_stale_drop_cannot_close_a_newer_question(self):
        key = todos.ask(self.conn, "When is beach day?")
        row = self.conn.execute("SELECT * FROM questions WHERE key = ?", (key,)).fetchone()
        version = row["updated_at"] or row["created_at"]
        self.conn.execute("UPDATE questions SET text = ?, updated_at = ? WHERE key = ?",
                          ("When is beach day in September?", version + "+new", key))
        outcome = questions.apply_action(
            self.conn, {"action": "drop", "key": key, "version": version,
                        "text": None, "answer": None, "wake_condition": None},
            written_by="dream", commit=False)
        self.assertEqual(outcome[0], "rejected-stale")
        self.assertEqual(
            self.conn.execute("SELECT status FROM questions WHERE key = ?", (key,)).fetchone()[0],
            "open")


class TestQuestionActionsMeetInMerge(Base):

    def _proposal(self, entity, action, *, evidence_id):
        bundle = bundle_stage.Bundle(entity=entity, items=[])
        diff = {"questions": [{
            "action": action, "key": "q:beach", "version": "v1",
            "text": "When is beach day?" if action == "amend" else None,
            "answer": "September 7 works" if action == "resolve" else None,
            "wake_condition": "after August" if action == "amend" else None,
            "cite_ids": [evidence_id],
        }]}
        return (bundle, diff, "generation")

    def test_conflicting_actions_are_decided_from_evidence(self):
        from memcal import llm
        first_id = archive.append(
            self.conn, stream="imessage", external_id="beach-delay", ts=db.now(),
            text="I cannot go during August", person="Quinn", gated=True)
        second_id = archive.append(
            self.conn, stream="imessage", external_id="beach-date", ts=db.now(),
            text="September 7 works", person="Quinn", gated=True)
        proposals = [self._proposal("person:Quinn", "amend", evidence_id=first_id),
                     self._proposal("thread:imessage:beach", "resolve",
                                    evidence_id=second_id)]

        class Client:
            def complete(self, **kw):
                self.suffix = kw["suffix"]
                data = {"choice": 2, "why": "The later source states a date."}
                return llm.Reply(text=json.dumps(data), data=data,
                                 usage=llm.Usage(calls=1), generation_id="merge")

        client = Client()
        merged, log = merge_stage.merge_all(
            client, self.cfg, proposals, conn=self.conn, run_id=None)
        actions = [row for _bundle, diff, _gen in merged
                   for row in diff.get("questions") or []]
        self.assertEqual([row["action"] for row in actions], ["resolve"])
        self.assertIn("I cannot go during August", client.suffix)
        self.assertIn("September 7 works", client.suffix)
        self.assertTrue(any("merged question q:beach as resolve" in line for line in log))

    def test_an_unresolved_conflict_defers_every_involved_bundle(self):
        from memcal import llm
        first_id = archive.append(
            self.conn, stream="imessage", external_id="beach-a", ts=db.now(),
            text="Maybe later", person="Quinn", gated=True)
        second_id = archive.append(
            self.conn, stream="groupme", external_id="beach-b", ts=db.now(),
            text="No idea", person="Quinn", gated=True)
        proposals = [self._proposal("person:Quinn", "amend", evidence_id=first_id),
                     self._proposal("thread:groupme:beach", "drop", evidence_id=second_id)]

        class Client:
            def complete(self, **kw):
                data = {"choice": None, "why": "The evidence does not decide."}
                return llm.Reply(text=json.dumps(data), data=data,
                                 usage=llm.Usage(calls=1), generation_id="merge")

        merged, log = merge_stage.merge_all(
            Client(), self.cfg, proposals, conn=self.conn, run_id=None)
        self.assertEqual(merged, [])
        self.assertTrue(any("deferred 2 bundle(s)" in line for line in log))


class TestKeysAreOpaque(Base):
    """A key records where a row started; the date field says where it is now.

    The sweep asked "key says 2026-08-01 but the date says 2026-08-02 — which is
    correct?" in two consecutive runs. Both are correct: the row moved. Only memcal
    could act on the answer, so it is bookkeeping, not curiosity.
    """

    def test_the_sweep_is_told_keys_never_move(self):
        from memcal.dream import sweep as sweep_stage
        self.assertIn("KEYS ARE OPAQUE", sweep_stage.SWEEP_INSTRUCTIONS)
        self.assertIn("The date field is the truth", sweep_stage.SWEEP_INSTRUCTIONS)

    def test_a_key_versus_date_question_never_reaches_the_brief(self):
        for text in (
            "The event key 'beer-garden@2026-08-01' has a date suffix of 2026-08-01, "
            "but the row's date field says 2026-08-02 — which is correct?",
            "The key suffix and the stored date disagree. Which date is correct?",
            "Event key harper@2026-07-27 has a date field of 2026-07-26 — which is right?",
        ):
            self.assertTrue(todos.is_self_referential(text), text)
            self.assertEqual(todos.ask(self.conn, text), "")
        self.assertEqual(todos.open_questions(self.conn), [])

    def test_a_real_date_question_still_gets_through(self):
        # The shape is similar; the difference is that the user can answer it.
        text = "Which Sunday is the beer garden — the 2nd or the 9th?"
        self.assertFalse(todos.is_self_referential(text))
        self.assertTrue(todos.ask(self.conn, text))

    def test_moving_a_row_keeps_its_key(self):
        first, _ = events.upsert(self.conn, {"title": "beer garden", "date": self.d(6),
                                             "series": "beer-garden"})
        moved, verb = events.upsert(self.conn, {"title": "beer garden", "date": self.d(7),
                                                "series": "beer-garden"})
        self.assertEqual(verb, "updated")
        self.assertEqual(moved.key, first.key)      # the name does not follow the date
        self.assertEqual(moved.date, self.d(7))


class TestWikiPagesEarnTheirPlace(Base):
    """32 of 49 pages held nothing but six boilerplate questions apiece.

    Every slug is charged to the prompt on every call forever, and the user's own
    steer was that the wiki should hold what the user tells it, not what can be mined out
    of their address book.
    """

    def setUp(self):
        super().setUp()
        identity.set_me(self.conn, "Casey", "Casey Morgan")

    def _row_with(self, *names):
        events.upsert(self.conn, {"title": "dinner", "date": self.d(1),
                                  "participants": list(names)})

    def test_being_on_a_row_earns_a_page(self):
        self._row_with("Quinn Brooks")
        self.assertIn("quinn-brooks", wiki.autocreate(self.conn, self.cfg.wiki_dir))

    def test_texting_a_lot_does_not(self):
        # This is what produced a page for a Pokémon Center SMS shortcode.
        identity.link(self.conn, "26349", "Pokémon Center")
        for i in range(12):
            archive.append(self.conn, stream="imessage", external_id=f"p{i}",
                           ts=db.now(), text="your order ships tomorrow",
                           person="Pokémon Center", gated=1)
        self.assertEqual(wiki.autocreate(self.conn, self.cfg.wiki_dir), [])

    def test_a_new_page_carries_no_boilerplate(self):
        self._row_with("Katie")
        wiki.autocreate(self.conn, self.cfg.wiki_dir)
        page = wiki.read(self.cfg.wiki_dir, "katie")
        self.assertEqual(page.questions, [])

    def test_the_user_never_gets_a_page_about_themselves(self):
        events.upsert(self.conn, {"title": "flight", "date": self.d(1),
                                  "subject": "Casey Morgan"})
        self.assertNotIn("casey-morgan", wiki.autocreate(self.conn, self.cfg.wiki_dir))

    def test_create_and_prune_do_not_fight_each_other(self):
        # autocreate opens an empty page for someone on a row; prune deletes empty
        # pages. Run back to back they would churn forever unless both read the same
        # judgement of who is worth a page.
        events.upsert(self.conn, {"title": "D&D session", "date": self.d(1),
                                  "participants": ["Pat Baker"]})
        for _ in range(3):
            created = wiki.autocreate(self.conn, self.cfg.wiki_dir)
            gone = wiki.prune_empty(self.cfg.wiki_dir,
                                    keep=set(wiki.page_worthy(self.conn)))
            self.assertNotIn("pat-baker", gone)
        self.assertIn("pat-baker", wiki.list_pages(self.cfg.wiki_dir))
        self.assertEqual(created, [], "the page should only ever be created once")

    def test_someone_who_dropped_off_the_calendar_is_pruned(self):
        wiki.write(self.cfg.wiki_dir,
                   wiki.ensure(self.cfg.wiki_dir, "old-friend", title="Old Friend"))
        gone = wiki.prune_empty(self.cfg.wiki_dir, keep=set(wiki.page_worthy(self.conn)))
        self.assertIn("old-friend", gone)

    def test_pruning_removes_an_empty_page(self):
        wiki.write(self.cfg.wiki_dir, wiki.ensure(self.cfg.wiki_dir, "eli-grant",
                                                  title="Eli Grant"))
        self.assertIn("eli-grant", wiki.prune_empty(self.cfg.wiki_dir))
        self.assertIsNone(wiki.read(self.cfg.wiki_dir, "eli-grant"))

    def test_pruning_keeps_a_page_with_a_fact(self):
        wiki.set_slot(self.cfg.wiki_dir, "quinn-brooks", "likes", "Pokemon", source="cli")
        self.assertEqual(wiki.prune_empty(self.cfg.wiki_dir), [])

    def test_pruning_keeps_a_real_question_but_drops_the_stubs(self):
        wiki.add_question(self.cfg.wiki_dir, "mom", "mom: Who is Bailey?")
        wiki.add_question(self.cfg.wiki_dir, "mom", "Mom: birthday?")
        self.assertEqual(wiki.prune_empty(self.cfg.wiki_dir), [])
        page = wiki.read(self.cfg.wiki_dir, "mom")
        self.assertEqual(page.questions, ["mom: Who is Bailey?"])

    def test_boilerplate_is_recognised_by_its_shape(self):
        for stub in ("Katie: birthday?", "Eli Grant: what they're into?", "x: work?"):
            self.assertTrue(wiki.is_boilerplate(stub), stub)
        for real in ("mom: Who is Bailey, and is her birthday June 26?",
                     "Which Sunday is the beer garden?"):
            self.assertFalse(wiki.is_boilerplate(real), real)


class TestThirdPartyTraffic(Base):
    """Their friends settling a plan in front of them is still their world.

    From a real trace: "this exchange is between Jamie and Quinn, not involving me
    directly. If Casey isn't part of that conversation, is it even relevant to track?"
    — and it dropped the bundle.
    """

    def test_the_prompt_says_he_need_not_be_speaking(self):
        prefix = propose_stage.build_prefix(self.conn, self.cfg)
        self.assertIn("THE USER DOES NOT HAVE TO BE SPEAKING", prefix)
        self.assertIn("do not skip a bundle because the user is not in the exchange",
                      prefix.lower())

    def test_a_row_without_him_still_applies(self):
        diff = {"events": [{"title": "Beer garden", "date": self.d(7), "subject": "me",
                            "participants": ["Jamie", "Quinn Brooks"],
                            "kind": "commitment", "status": "mentioned"}]}
        bundle = bundle_stage.Bundle(entity="person:Quinn Brooks", items=[])
        apply_stage.apply_diffs(self.conn, self.cfg, [(bundle, diff)], written_by="test")
        rows = events.window(self.conn, 1, 10)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].participants, ["Jamie", "Quinn Brooks"])



class TestPromptUnderWriting(Base):
    """Three ways the prompt was talking the model out of correct writes.

    All three came out of a single reasoning trace, in the model's own words.
    """

    def setUp(self):
        super().setUp()
        self.prefix = propose_stage.build_prefix(self.conn, self.cfg)

    def test_a_question_never_stands_in_for_a_row(self):
        # "Since the open question already captures this... adding a new event row
        # wouldn't resolve whether the user is actually playing. I'll skip it."
        self.assertIn("A QUESTION IS NOT A ROW", self.prefix)
        self.assertIn("never satisfy each other", self.prefix)

    def test_caution_is_scoped_to_inference_not_to_stated_facts(self):
        # "it's casual enough that I'm hesitant to record it as a durable fact given
        # the conservative approach I'm taking" — about a username the user was just given.
        self.assertIn("This caution is about things nobody said", self.prefix)
        self.assertIn("Being hesitant about stated facts is not conservatism", self.prefix)

    def test_the_agent_stream_is_never_skipped(self):
        # "this is actually Casey messaging the agent (Hermes), not a memcal entry —
        # I should skip it entirely." That stream is the live path's whole point.
        self.assertIn("agent STREAM IS THEM INSTRUCTING A MACHINE", self.prefix)
        self.assertIn("Never skip an agent line", self.prefix)

    def test_the_prompt_separates_what_he_states_from_what_he_delegates(self):
        # #57. The old wording called the agent stream "the one place a fact arrives
        # already confirmed", which is true of what the user states there and false of what
        # the user tells it to do — and four of six open to-dos were work the user had handed off.
        self.assertIn("work handed off, not work the user owes", self.prefix)
        self.assertIn("only when the doer is them", self.prefix)

    def test_an_agent_line_reaches_the_model_labelled(self):
        # The stream is on every line and the prompt leans on it to recognise this one.
        # The thread is not: in a single-conversation bundle it is the same string on
        # every line, and it used to be a phone number repeated forty-five times.
        archive.append(self.conn, stream="agent", external_id="a1", ts=db.now(),
                       text="Quinn is the DM for Frozen Far", thread="conversation",
                       person="me", from_me=1, gated=1)
        row = self.conn.execute("SELECT * FROM archive WHERE external_id='a1'").fetchone()
        bundle = bundle_stage.Bundle(entity="person:me", items=[row])
        self.assertIn("(agent)", bundle.render())
        self.assertIn("on agent", bundle.render())
        self.assertIn("`me → assistant` is an instruction", self.prefix)

    def test_a_bundle_says_when_it_is_only_a_window(self):
        # Without this, the newest 44 of 339 lines reads as the whole relationship, and
        # the window's own edge reads as a three-week silence.
        rows = []
        for i in range(3):
            archive.append(self.conn, stream="imessage", external_id=f"w{i}",
                           ts=(db.now_dt() - timedelta(hours=i)).isoformat(
                               timespec="seconds"),
                           text=f"line {i}", thread="+15551110000", person="Rowan Vale",
                           gated=1)
            rows.append(self.conn.execute("SELECT * FROM archive WHERE external_id=?",
                                          (f"w{i}",)).fetchone())
        bundle = bundle_stage.Bundle(entity="person:Rowan Vale", items=rows, waiting=336)
        self.assertIn("newest 3 of 339 lines", bundle.render())
        whole = bundle_stage.Bundle(entity="person:Rowan Vale", items=rows)
        self.assertIn("3 lines", whole.render())
        self.assertNotIn("newest", whole.render())

    def test_a_real_silence_is_marked_and_an_overnight_gap_is_not(self):
        def line(days_ago, text):
            key = f"s{days_ago}"
            archive.append(self.conn, stream="imessage", external_id=key,
                           ts=(db.now_dt() - timedelta(days=days_ago)).isoformat(
                               timespec="seconds"),
                           text=text, thread="+15551110000", person="Katie", gated=1)
            return self.conn.execute("SELECT * FROM archive WHERE external_id=?",
                                     (key,)).fetchone()
        far = bundle_stage.Bundle(entity="person:Katie", items=[line(9, "a"), line(0, "b")])
        far.items.sort(key=lambda r: str(r["ts"]))
        self.assertIn("9 days with nothing said", far.render())
        near = bundle_stage.Bundle(entity="person:Katie",
                                   items=[line(1, "c"), line(0, "d")])
        near.items.sort(key=lambda r: str(r["ts"]))
        self.assertNotIn("nothing said", near.render())


class TestWhatsApp(Base):
    """Built against the real ZWA* schema, since there is no live store to read here."""

    GROUP_JID = "12345-67890@g.us"
    MUM_JID = "19175550001@s.whatsapp.net"

    def _store(self, rows) -> str:
        path = Path(self.tmp.name) / "ChatStorage.sqlite"
        src = sqlite3.connect(path)
        src.executescript("""
            CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT,
                                         ZPARTNERNAME TEXT, ZSESSIONTYPE INTEGER);
            CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT,
                                         ZCONTACTNAME TEXT);
            CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, ZTEXT TEXT,
                                     ZMESSAGEDATE REAL, ZISFROMME INTEGER, ZFROMJID TEXT,
                                     ZMESSAGETYPE INTEGER, ZCHATSESSION INTEGER,
                                     ZGROUPMEMBER INTEGER);
        """)
        src.execute("INSERT INTO ZWACHATSESSION VALUES(1,?,?,1)", (self.GROUP_JID, "Family"))
        src.execute("INSERT INTO ZWACHATSESSION VALUES(2,?,?,0)", (self.MUM_JID, "Mum"))
        src.execute("INSERT INTO ZWAGROUPMEMBER VALUES(1,?,?)", (self.MUM_JID, "Mum"))
        src.executemany(
            "INSERT INTO ZWAMESSAGE VALUES(?,?,?,?,?,?,?,?)", rows)
        src.commit()
        src.close()
        return str(path)

    def _seconds(self, days_ago=0):
        stamp = db.now_dt() - timedelta(days=days_ago)
        return (stamp - datetime(2001, 1, 1, tzinfo=timezone.utc)).total_seconds()

    def test_a_group_message_is_archived_and_gated(self):
        path = self._store([
            (1, "we playing at 8?", self._seconds(), 0, self.GROUP_JID, 0, 1, 1),
            (2, "haha", self._seconds(), 0, self.GROUP_JID, 0, 1, 1),
        ])
        report = whatsapp.ingest(self.conn, self.cfg, db_path=path)
        self.assertIsNone(report.error)
        self.assertEqual(report.archived, 2)
        self.assertEqual(report.passed, 1, "only the temporal line should pass the gate")

    def test_the_sender_in_a_group_is_the_member_not_the_group(self):
        identity.link(self.conn, "+19175550001", "Mum")
        path = self._store([(1, "dinner tomorrow?", self._seconds(), 0,
                             self.GROUP_JID, 0, 1, 1)])
        whatsapp.ingest(self.conn, self.cfg, db_path=path)
        row = self.conn.execute("SELECT * FROM archive WHERE stream='whatsapp'").fetchone()
        self.assertEqual(row["person"], "Mum")
        self.assertEqual(row["thread"], "Family")

    def test_a_jid_resolves_against_the_same_contacts_as_imessage(self):
        # One person is one person across streams, or bundling splits them in two.
        self.assertEqual(whatsapp.phone_of("19175550001@s.whatsapp.net"), "+19175550001")
        self.assertIsNone(whatsapp.phone_of("12345-67890@g.us"))
        self.assertIsNone(whatsapp.phone_of(None))

    def test_non_text_messages_are_skipped(self):
        path = self._store([(1, None, self._seconds(), 0, self.GROUP_JID, 1, 1, 1)])
        report = whatsapp.ingest(self.conn, self.cfg, db_path=path)
        self.assertEqual(report.archived, 0)

    def test_the_watermark_stops_a_replay(self):
        path = self._store([(1, "poker friday", self._seconds(), 0, self.GROUP_JID, 0, 1, 1)])
        first = whatsapp.ingest(self.conn, self.cfg, db_path=path)
        second = whatsapp.ingest(self.conn, self.cfg, db_path=path)
        self.assertEqual(first.archived, 1)
        self.assertEqual(second.archived, 0)

    def test_the_watermark_advances_past_skipped_rows(self):
        # A run of media messages must not pin the watermark forever.
        path = self._store([(i, None, self._seconds(), 0, self.GROUP_JID, 1, 1, 1)
                            for i in range(1, 6)])
        whatsapp.ingest(self.conn, self.cfg, db_path=path)
        self.assertEqual(db.get_meta(self.conn, "watermark.whatsapp.rowid"), "5")

    def test_a_dm_carries_a_counterpart_so_it_bundles_with_the_person(self):
        identity.link(self.conn, "+19175550001", "Mum")
        path = self._store([(1, "are you free sunday?", self._seconds(), 0,
                             self.MUM_JID, 0, 2, None)])
        whatsapp.ingest(self.conn, self.cfg, db_path=path)
        row = self.conn.execute("SELECT entity FROM spool").fetchone()
        self.assertEqual(row["entity"], "person:Mum")

    def test_a_missing_store_is_reported_not_raised(self):
        report = whatsapp.ingest(self.conn, self.cfg, db_path="/nope/ChatStorage.sqlite")
        self.assertIn("no WhatsApp store found", report.error or "")

    def test_a_changed_schema_is_reported_not_raised(self):
        path = Path(self.tmp.name) / "empty.sqlite"
        sqlite3.connect(path).close()
        report = whatsapp.ingest(self.conn, self.cfg, db_path=str(path))
        self.assertIn("schema", report.error or "")


class TestCrossPlatformIdentity(Base):
    """Spec §10 case 12: one person on three platforms, about one thing, is one row.

    This is the claim §6.1 makes for bundling by entity rather than by stream. Adding
    a stream is the moment it can quietly stop being true — a new source that invents
    its own handle format splits a person in half and nothing complains.
    """

    def setUp(self):
        super().setUp()
        for handle in ("+17579710065", "groupme:4471"):
            identity.link(self.conn, handle, "Quinn Brooks")

    def _deliver(self, stream, handle, text, **kw):
        report = sources.IngestReport.opened(stream, self.cfg)
        sources.deliver(self.conn, report, stream=stream, external_id=f"{stream}-1",
                        ts=db.now(), text=text, thread=kw.pop("thread", "t"),
                        handle=handle, counterpart=handle, **kw)

    def test_three_streams_land_in_one_bundle(self):
        self._deliver("imessage", "+17579710065", "beer garden sunday?")
        self._deliver("whatsapp", whatsapp.phone_of("17579710065@s.whatsapp.net"),
                      "still on for sunday")
        self._deliver("groupme", "groupme:4471", "sunday works for me")

        bundles = bundle_stage.build(self.conn)
        self.assertEqual([b.entity for b in bundles], ["person:Quinn Brooks"],
                         "three streams about one plan must not become three bundles")
        self.assertEqual(len(bundles[0].items), 3)

    def test_a_whatsapp_jid_and_an_imessage_number_are_the_same_person(self):
        self._deliver("imessage", "+17579710065", "you free sunday?")
        self._deliver("whatsapp", whatsapp.phone_of("17579710065@s.whatsapp.net"),
                      "yeah sunday works")
        people = {r["person"] for r in
                  self.conn.execute("SELECT person FROM archive WHERE person IS NOT NULL")}
        self.assertEqual(people, {"Quinn Brooks"})

    def test_an_unresolved_handle_does_not_masquerade_as_a_person(self):
        self._deliver("whatsapp", "+15550009999", "party saturday at 9")
        rows = identity.unresolved(self.conn)
        self.assertEqual([r["handle"] for r in rows], ["+15550009999"])


class TestContactsRefresh(Base):
    """Spec §5.2: "Refresh daily." It was imported once at init and never again.

    Everyone added to the address book afterwards stayed an opaque handle forever, on
    every stream at once — a number nobody has named cannot resolve anywhere.
    """

    def setUp(self):
        super().setUp()
        self.imports = 0

        def counted(conn):
            self.imports += 1
            return 1, "linked 1 handle"

        self._real = identity.import_contacts
        identity.import_contacts = counted

    def tearDown(self):
        identity.import_contacts = self._real
        super().tearDown()

    def test_the_first_call_imports(self):
        identity.refresh_contacts(self.conn)
        self.assertEqual(self.imports, 1)

    def test_a_second_call_the_same_day_does_not(self):
        identity.refresh_contacts(self.conn)
        identity.refresh_contacts(self.conn)
        self.assertEqual(self.imports, 1)

    def test_it_re_imports_once_a_day_has_passed(self):
        identity.refresh_contacts(self.conn)
        stale = (db.now_dt() - timedelta(hours=25)).isoformat()
        db.set_meta(self.conn, "contacts.imported_at", stale)
        identity.refresh_contacts(self.conn)
        self.assertEqual(self.imports, 2)

    def test_force_ignores_the_clock(self):
        identity.refresh_contacts(self.conn)
        identity.refresh_contacts(self.conn, force=True)
        self.assertEqual(self.imports, 2)


class TestPlainLanguage(Base):
    """The brief goes into an agent's context whole, so its words become its words.

    It was rendering raw enum values, and Hermes read them out: "Griffin's birthday
    party — opportunity". The user's reply was "what does opportunity even mean".
    """

    def _line(self, **fields):
        ev, _ = events.upsert(self.conn, {"title": "thing", "date": self.d(1), **fields})
        return ev.one_line()

    def test_no_schema_word_reaches_the_brief(self):
        for kind in events.KINDS:
            for status in events.STATUSES:
                events.upsert(self.conn, {"title": f"{kind}-{status}", "date": self.d(1),
                                          "kind": kind, "status": status},
                              written_by="cli")
        text = brief.render(self.conn, self.cfg)
        for jargon in ("opportunity", "availability", "mentioned", "tentative", "avail"):
            # The titles in this fixture contain the words; the *tails* must not.
            tails = [l.split(" — ", 1)[1] for l in text.splitlines() if " — " in l]
            self.assertFalse(any(jargon in t for t in tails), f"{jargon} leaked: {tails}")

    def test_an_invite_reads_as_something_he_could_do(self):
        self.assertIn("could go", self._line(kind="opportunity"))

    def test_a_plan_reads_as_maybe_or_confirmed(self):
        self.assertIn("maybe", self._line(status="mentioned"))
        self.assertIn("confirmed", self._line(status="confirmed"))

    def test_declining_reads_as_not_going(self):
        self.assertIn("not going", self._line(status="declined"))

    def test_someone_elses_confirmed_plan_needs_no_tag(self):
        # "Harper's flight lands, home from France — avail · confirmed" said nothing.
        line = self._line(kind="availability", status="confirmed", title="Harper's flight")
        self.assertNotIn("—", line)


class TestSpans(Base):
    """Multi-day rows were crammed into titles, so nothing could act on them.

    "Avery visiting NYC (through Wed morning Jul 29)" showed as past on day two —
    while the user was still asleep in the next room.
    """

    def _visit(self, start, end, **kw):
        ev, _ = events.upsert(self.conn, {"title": "Avery visiting NYC",
                                          "date": self.d(start), "until": self.d(end),
                                          **kw}, written_by="cli")
        return ev

    def test_a_span_still_shows_while_it_is_running(self):
        self._visit(-2, 3)
        titles = [e.title for e in events.window(self.conn, 1, 1)]
        self.assertIn("Avery visiting NYC", titles)

    def test_a_span_that_started_before_the_window_is_still_included(self):
        self._visit(-9, 2)          # began outside a 3-day backward window
        self.assertTrue(events.window(self.conn, 3, 7))

    def test_a_finished_span_drops_out(self):
        self._visit(-20, -11)
        self.assertEqual(events.window(self.conn, 3, 7), [])

    def test_the_end_is_rendered_rather_than_left_in_the_title(self):
        self.assertIn("until", self._visit(-1, 3).one_line())

    def test_a_running_span_is_not_marked_happened(self):
        self._visit(-2, 3, status="confirmed")
        events.mark_past_happened(self.conn)
        self.assertEqual(events.window(self.conn, 3, 7)[0].status, "confirmed")

    def test_a_finished_span_is_marked_happened(self):
        self._visit(-9, -2, status="confirmed")
        events.mark_past_happened(self.conn)
        row = self.conn.execute("SELECT status FROM events").fetchone()
        self.assertEqual(row["status"], "happened")

    def test_covers_answers_whether_a_row_is_live(self):
        ev = self._visit(-2, 3)
        self.assertTrue(ev.covers(db.today()))
        self.assertFalse(ev.covers(db.today() + timedelta(days=9)))

    def test_a_backwards_span_is_dropped_not_stored(self):
        diff = {"events": [{"title": "trip", "date": self.d(3), "until": self.d(1)}]}
        bundle = bundle_stage.Bundle(entity="person:x", items=[])
        apply_stage.apply_diffs(self.conn, self.cfg, [(bundle, diff)], written_by="test")
        self.assertIsNone(self.conn.execute("SELECT until FROM events").fetchone()["until"])

    def test_a_single_day_row_has_no_span(self):
        ev, _ = events.upsert(self.conn, {"title": "dinner", "date": self.d(1)})
        self.assertEqual(ev.last_day, ev.date)
        self.assertNotIn("until", ev.one_line())


class TestThreadIdentity(Base):
    """One conversation, one bundle — even when the source is wrong about the chat.

    BlueBubbles reported a three-way chat as a DM with an opaque id. Their twelve
    messages bundled under `person:2d9a5f7b2dbc49a8…`, Avery's under their name and
    Quinn's under their: one afternoon's plan split three ways, which is exactly what
    bundling by entity exists to prevent.
    """

    GUID = "2d9a5f7b2dbc49a89a4857ea7cbb6d8f"

    def _msg(self, ext, text, *, person=None, from_me=False, thread=None, is_group=False):
        report = sources.IngestReport.opened("imessage", self.cfg)
        sources.deliver(self.conn, report, stream="imessage", external_id=ext,
                        ts=db.now(), text=text, thread=thread or self.GUID,
                        handle=None if from_me else "+16097222075", person=person,
                        from_me=from_me, is_group=is_group,
                        counterpart=None if from_me else "+16097222075")

    def _entities(self):
        return {r["entity"] for r in self.conn.execute("SELECT DISTINCT entity FROM spool")}

    def test_an_opaque_chat_id_never_becomes_a_person(self):
        self._msg("m1", "demo court this week possibly?", from_me=True)
        self.assertFalse(any(self.GUID in e for e in self._entities() if e.startswith("person:")),
                         self._entities())

    def test_his_side_joins_the_person_when_the_thread_is_a_dm(self):
        identity.link(self.conn, "+16097222075", "Avery Morgan")
        self._msg("m1", "hop on the server tonight?", person="Avery Morgan")
        self._msg("m2", "yeah what time you thinking, like 2:30?", from_me=True)
        self.assertEqual(self._entities(), {"person:Avery Morgan"})

    def test_a_thread_with_two_speakers_is_a_group_and_keys_on_the_thread(self):
        identity.link(self.conn, "+16097222075", "Avery Morgan")
        identity.link(self.conn, "+17579710065", "Quinn Brooks")
        self._msg("m1", "lunch at 2:30 tomorrow?", person="Avery Morgan")
        report = sources.IngestReport.opened("imessage", self.cfg)
        sources.deliver(self.conn, report, stream="imessage", external_id="m2",
                        ts=db.now(), text="I'm free tomorrow too", thread=self.GUID,
                        handle="+17579710065", person="Quinn Brooks")
        self._msg("m3", "cool, meet at 2:30 then", from_me=True)
        # One afternoon, one bundle — and the first speaker's messages get pulled back
        # in, since "this is a group" is only provable once the second person speaks.
        self.assertEqual(self._entities(), {f"thread:imessage:{self.GUID}"})

    def test_thread_person_needs_evidence_before_it_answers(self):
        self.assertIsNone(sources.base.thread_person(self.conn, "imessage", self.GUID))

    def test_a_real_group_thread_is_not_mistaken_for_a_person(self):
        identity.link(self.conn, "+16097222075", "Avery Morgan")
        identity.link(self.conn, "+17579710065", "Quinn Brooks")
        for ext, handle, person in (("g1", "+16097222075", "Avery Morgan"),
                                    ("g2", "+17579710065", "Quinn Brooks")):
            report = sources.IngestReport.opened("imessage", self.cfg)
            sources.deliver(self.conn, report, stream="imessage", external_id=ext,
                            ts=db.now(), text="see you at 8 tomorrow", thread="Crystal Harbor",
                            handle=handle, person=person, is_group=True)
        self.assertIsNone(sources.base.thread_person(self.conn, "imessage", "Crystal Harbor"))

    def test_regrouping_leaves_already_processed_items_alone(self):
        # Their bundle has been and gone; re-keying it would only corrupt the record
        # of what a past run actually read.
        identity.link(self.conn, "+16097222075", "Avery Morgan")
        self._msg("m1", "lunch at 2:30 tomorrow?", person="Avery Morgan")
        self.conn.execute("UPDATE spool SET processed_at = ?", (db.now(),))
        self.conn.commit()
        moved = sources.base.regroup_thread(self.conn, "imessage", self.GUID)
        self.assertEqual(moved, 0)
        self.assertEqual(self._entities(), {"person:Avery Morgan"})


class TestEntityIsAPerson(Base):
    """`person:` must name a person. It was naming whatever the source happened to send.

    The live spool held `person:no.reply.alerts@chase.com` (599 items),
    `person:2d9a5f7b2dbc49a89a4857ea7cbb6d8f`, and `person:9858b62c1615…` — an email
    robot and two opaque chat ids, sitting in the bundle list as though they were
    friends, and each one costing a model call to read.
    """

    def _entity_for(self, **kw):
        report = sources.IngestReport.opened(kw.pop("stream", "imessage"), self.cfg)
        sources.deliver(self.conn, report, stream=kw.pop("_stream", "imessage"),
                        external_id="x1", ts=db.now(), text="poker friday at 8", **kw)
        row = self.conn.execute("SELECT entity FROM spool").fetchone()
        return row["entity"] if row else None

    def test_an_unresolved_chat_id_keys_on_the_thread(self):
        entity = self._entity_for(thread="2d9a5f7b2dbc49a8", from_me=True,
                                  counterpart="2d9a5f7b2dbc49a8")
        self.assertEqual(entity, "thread:imessage:2d9a5f7b2dbc49a8")

    def test_an_unresolved_email_address_is_not_a_person(self):
        entity = self._entity_for(_stream="email", thread="no.reply.alerts@chase.com",
                                  handle="no.reply.alerts@chase.com",
                                  counterpart="no.reply.alerts@chase.com")
        self.assertFalse(entity.startswith("person:"), entity)

    def test_a_resolved_handle_still_keys_on_the_person(self):
        identity.link(self.conn, "+16097222075", "Avery Morgan")
        entity = self._entity_for(thread="+16097222075", handle="+16097222075",
                                  counterpart="+16097222075")
        self.assertEqual(entity, "person:Avery Morgan")

    def test_naming_the_handle_later_moves_his_side_to_the_person(self):
        # The queue self-heals once `memcal who` names an unknown number.
        entity = self._entity_for(thread="+15550001111", handle="+15550001111",
                                  counterpart="+15550001111")
        self.assertTrue(entity.startswith("thread:"), entity)
        identity.link(self.conn, "+15550001111", "Terry North")
        report = sources.IngestReport.opened("imessage", self.cfg)
        sources.deliver(self.conn, report, stream="imessage", external_id="x2",
                        ts=db.now(), text="see you tomorrow at 6", thread="+15550001111",
                        handle="+15550001111", counterpart="+15550001111")
        entities = {r["entity"] for r in self.conn.execute("SELECT entity FROM spool")}
        self.assertIn("person:Terry North", entities)


class TestEvidenceMeetsObligation(Base):
    """memcal held the receipt and the obligation and never joined them.

        Open:      Venmo Emery for the artwork
        Ask about: Did the $50 Venmo to Emery Wells on Jul 7 cover the artwork?

    Not closing it by inference is right — a to-do dies conversationally. Showing the
    two as strangers is not: neither the agent nor the user made the connection.
    """

    def test_the_receipt_lands_under_the_obligation(self):
        todos.open_todo(self.conn, "Venmo Emery for the artwork")
        todos.ask(self.conn, 'Did the $50 Venmo to Emery Wells on Jul 7 '
                             '(note "Art") cover the artwork you owed her for?')
        text = brief.render(self.conn, self.cfg)
        self.assertIn("↳", text)
        open_block = text.split("## Open")[1].split("##")[0]
        self.assertIn("Emery Wells", open_block)

    def test_a_linked_question_is_not_also_listed_separately(self):
        todos.open_todo(self.conn, "Venmo Emery for the artwork")
        todos.ask(self.conn, "Did the $50 Venmo to Emery Wells cover the artwork?")
        text = brief.render(self.conn, self.cfg)
        self.assertEqual(text.count("Emery Wells"), 1)

    def test_a_generic_word_does_not_make_a_link(self):
        # "Plan Coney Island trip with Avery" collected nine questions, including
        # "Is the Disney/Kissimmee trip August 2027?", on the strength of "trip".
        todos.open_todo(self.conn, "Plan Coney Island trip with Avery")
        todos.ask(self.conn, "Is the Disney/Kissimmee trip August 2027, or August 2026?")
        self.assertEqual(todos.questions_by_todo(self.conn), {})

    def test_sharing_only_a_name_is_not_enough(self):
        todos.open_todo(self.conn, "Plan Coney Island trip with Avery")
        todos.ask(self.conn, "Which day next weekend is Avery coming for the beer garden?")
        self.assertEqual(todos.questions_by_todo(self.conn), {})

    def test_relinking_repairs_questions_written_before_the_link_existed(self):
        todo, _ = todos.open_todo(self.conn, "Venmo Emery for the artwork")
        todos.ask(self.conn, "Did the $50 Venmo to Emery cover the artwork?")
        self.conn.execute("UPDATE questions SET about_todo = NULL")
        self.conn.commit()
        self.assertEqual(todos.relink_questions(self.conn), 1)
        self.assertIn(todo.id, todos.questions_by_todo(self.conn))

    def test_closing_the_todo_detaches_its_questions(self):
        todo, _ = todos.open_todo(self.conn, "Venmo Emery for the artwork")
        todos.ask(self.conn, "Did the $50 Venmo to Emery cover the artwork?")
        todos.close(self.conn, todo.key)
        self.assertEqual(todos.questions_by_todo(self.conn), {})
        # ...and it goes back to being an ordinary question rather than vanishing.
        self.assertIn("Emery", brief.render(self.conn, self.cfg))


class TestResolvingWhatIsAlreadyDone(Base):
    """An agent writing the same fact twice in one turn must not be told it failed.

    Real session: memcal_remember closed "Mail the signed trust paperwork back", then
    memcal_answer was called about the same thing and got "nothing open matches that"
    — which reads as a bug and invites a retry against something already correct.
    """

    def test_an_already_closed_todo_reports_settled_not_missing(self):
        todo, _ = todos.open_todo(self.conn, "Mail the signed trust paperwork back")
        todos.close(self.conn, todo.key)
        ok, kind = todos.resolve(self.conn, "Mail the signed trust paperwork back",
                                 "handing it to Avery instead")
        self.assertTrue(ok)
        self.assertEqual(kind, "already")

    def test_an_already_answered_question_reports_settled(self):
        todos.ask(self.conn, "Is Avery Morgan the brother visiting NYC?")
        todos.answer(self.conn, "Is Avery Morgan the brother visiting NYC?", "yes")
        ok, kind = todos.resolve(self.conn, "Is Avery Morgan the brother visiting NYC?",
                                 "yes, same person")
        self.assertTrue(ok)
        self.assertEqual(kind, "already")

    def test_something_genuinely_unknown_still_fails(self):
        todos.open_todo(self.conn, "Venmo Emery for the artwork")
        ok, kind = todos.resolve(self.conn, "did I renew the passport", "yes")
        self.assertFalse(ok)
        self.assertEqual(kind, "")

    def test_an_open_item_is_still_closed_rather_than_called_settled(self):
        todos.open_todo(self.conn, "Venmo Emery for the artwork")
        ok, kind = todos.resolve(self.conn, "Venmo Emery for the artwork", "paid her")
        self.assertTrue(ok)
        self.assertEqual(kind, "todo")


class TestQuestionsWorthAsking(Base):
    """The user read five questions and dismissed four. They share a shape, not a topic.

    "why do u really care haha…. Creeeeeeepy", "that's also a silly question",
    "why is that on there. That's been resolved a while ago." The bar is not whether
    something is unknown — almost everything is. It is whether the answer changes what
    the user does next.
    """

    DISMISSED = [
        "Your car lease ends in March and you were talking about parking it cheaply in "
        "Queens — is finding a lot something you want tracked?",
        'Is "Mullin" the same person as Drew Lane, who you paid $100 for dinner on Jul 2?',
        "Logan Hayes sent you $100 and $122 for Electric Forest expenses and car "
        "camping — were you fronting the group's festival costs?",
        "Is PSK a fraternity you were in — and where?",
        "Your AXS billing address is 246 6th St, Lakeside, NJ — is that still current?",
    ]
    KEPT = [
        'Jamie\'s "next Sunday" plan (going earlier than 6, Q joining after) — which Sunday?',
        "Which day next weekend is Julian coming over for the beer garden?",
        "Are you going to Elements Aug 7-9, and do you have a ticket yet?",
        "Does 'I guess I could mail it this week instead' refer to the trust paperwork?",
        "What time is the D&D session on Sunday?",
    ]

    def test_the_dismissed_ones_never_get_stored(self):
        for text in self.DISMISSED:
            self.assertFalse(todos.is_worth_asking(text), text)
            self.assertEqual(todos.ask(self.conn, text), "")
        self.assertEqual(todos.open_questions(self.conn), [])

    def test_the_useful_ones_still_get_through(self):
        for text in self.KEPT:
            self.assertTrue(todos.is_worth_asking(text), text)
            self.assertTrue(todos.ask(self.conn, text), text)
        self.assertEqual(len(todos.open_questions(self.conn)), len(self.KEPT))

    def test_the_prompt_states_the_bar(self):
        prefix = propose_stage.build_prefix(self.conn, self.cfg)
        self.assertIn("does the answer change what the user", prefix.lower())
        self.assertIn("does next?", prefix.lower())
        self.assertIn("If you would not interrupt a friend to ask it", prefix)


class TestQuestionsExpire(Base):
    """A question nobody engaged with holds a slot against one that matters.

    "why is that on there. That's been resolved a while ago." — three days stale, and
    it was still taking a place in a six-line list.
    """

    def _age(self, days: int) -> None:
        stamp = (db.now_dt() - timedelta(days=days)).isoformat()
        self.conn.execute("UPDATE questions SET created_at = ?", (stamp,))
        self.conn.commit()

    def test_a_stale_question_is_dropped(self):
        todos.ask(self.conn, "Which day next weekend is Julian coming over?")
        self._age(todos.QUESTION_TTL_DAYS + 1)
        self.assertEqual(todos.expire_questions(self.conn), 1)
        self.assertEqual(todos.open_questions(self.conn), [])

    def test_a_fresh_question_is_left_alone(self):
        todos.ask(self.conn, "Which day next weekend is Julian coming over?")
        self._age(2)
        self.assertEqual(todos.expire_questions(self.conn), 0)

    def test_a_question_about_tonight_dies_with_tonight(self):
        todos.ask(self.conn, "The Lootbox chat was lining up an 8/8:30 game session "
                             "tonight — are you playing, or is that just them?")
        self._age(todos.TONIGHT_TTL_DAYS + 1)
        self.assertEqual(todos.expire_questions(self.conn), 1)

    def test_a_question_about_tonight_survives_tonight(self):
        todos.ask(self.conn, "Are you playing the game session tonight?")
        self.assertEqual(todos.expire_questions(self.conn), 0)

    def test_an_undated_question_gets_the_longer_life(self):
        todos.ask(self.conn, "Which day next weekend is Julian coming over?")
        self._age(todos.TONIGHT_TTL_DAYS + 1)
        self.assertEqual(todos.expire_questions(self.conn), 0)

    def test_expiry_does_not_touch_answered_ones(self):
        todos.ask(self.conn, "Which day next weekend is Julian coming over?")
        todos.answer(self.conn, "Which day next weekend", "Sunday")
        self._age(99)
        todos.expire_questions(self.conn)
        row = self.conn.execute("SELECT status, answer FROM questions").fetchone()
        self.assertEqual(row["status"], "answered")
        self.assertEqual(row["answer"], "Sunday")


class TestFriendlyTime(Base):
    """"military time is crazy lol u think I'm Danish?" """

    def test_a_24_hour_clock_becomes_how_someone_says_it(self):
        for stored, spoken in (("18:00", "6pm"), ("09:30", "9:30am"), ("00:00", "12am"),
                               ("12:00", "12pm"), ("23:45", "11:45pm"), ("7:05", "7:05am")):
            self.assertEqual(events.friendly_time(stored), spoken, stored)

    def test_free_text_passes_through_untouched(self):
        for text in ("~8pm", "evening", "after work", "6ish", ""):
            self.assertEqual(events.friendly_time(text), text)

    def test_the_brief_never_shows_a_24_hour_clock(self):
        events.upsert(self.conn, {"title": "Hangout with Jamie and Quinn",
                                  "date": self.d(1), "time": "18:00"})
        text = brief.render(self.conn, self.cfg)
        self.assertIn("6pm", text)
        self.assertNotIn("18:00", text)


class TestAgeIsNotADeadline(Base):
    """"why is the due date for setting up brightspace mfa today? i can do it any time" """

    def test_an_age_says_opened_not_due(self):
        todos.open_todo(self.conn, "Set up MFA on the laptop")
        line = todos.open_items(self.conn)[0].one_line()
        self.assertIn("opened today", line)
        self.assertNotIn("(today)", line)
        self.assertNotIn("due", line)

    def test_a_real_due_date_still_reads_as_one(self):
        todos.open_todo(self.conn, "Sign the trust paperwork", due=self.d(6))
        line = todos.open_items(self.conn)[0].one_line()
        self.assertIn("opened today", line)
        self.assertIn(f"due {self.d(6)}", line)

    def test_the_brief_never_calls_an_age_a_date(self):
        # The agent reads this file verbatim, and it told the user a to-do with no due
        # date was due today. One bare "(today)" per row was all it took.
        todos.open_todo(self.conn, "Log into Brightspace")
        text = brief.render(self.conn, self.cfg)
        self.assertIn("Log into Brightspace (opened today)", text)


class TestTypingMemcal(unittest.TestCase):
    """Thirty-seven commands is a lot to meet as one alphabetical wall."""

    def setUp(self):
        self.parser = cli.build_parser()

    def test_no_subcommand_prints_the_help(self):
        """It printed the brief until 2026-08-20.

        *"make memcal command print help, and not brief instead."* The brief is what you
        want once you know what memcal is; `memcal` alone is the keystroke you type
        before you know anything, and it answered with two hundred lines of somebody's
        week. `cmd_help` reads `topic` off the namespace and the bare path has no
        subparser to default it, so the parser owes it.
        """
        args = self.parser.parse_args([])
        self.assertIs(args.func, cli.cmd_help)
        self.assertIsNone(args.topic)

    def test_the_brief_is_still_one_word_away(self):
        self.assertIs(self.parser.parse_args(["brief"]).func, cli.cmd_brief)

    def test_a_subcommand_still_wins_over_the_default(self):
        self.assertIs(self.parser.parse_args(["todos"]).func, cli.cmd_todos)
        self.assertTrue(self.parser.parse_args(["brief", "--write"]).write)

    def test_every_command_appears_in_the_grouped_help(self):
        # The grouping is hand-written and the commands are not. Anything added to
        # one and not the other would silently drop out of --help.
        #
        # `HIDDEN_COMMANDS` is the one exemption and it is deliberate: `web` is `ui`
        # under its old name, and listing both asked the reader to choose between two
        # spellings of one thing. Hidden is not gone — `test_a_hidden_alias_still_runs`
        # is the other half — but it is not an accident either, so it is named here
        # rather than allowed to fall out of the loop quietly.
        help_text = self.parser.format_help()
        self.assertTrue(self.parser.memcal_commands, "no commands to check")
        for name in self.parser.memcal_commands:
            if name in cli.HIDDEN_COMMANDS:
                self.assertNotIn(f"    {name} ", help_text,
                                 f"{name} is meant to be hidden and is listed")
                continue
            self.assertIn(f"    {name} ", help_text, f"{name} missing from --help")

    def test_the_flat_list_is_gone_so_nothing_prints_twice(self):
        # Guards the one argparse private we touch: if a future version stops
        # exposing _choices_actions we fall back to the flat list, and this notices.
        help_text = self.parser.format_help()
        self.assertEqual(help_text.count("list open to-dos"), 1)
        self.assertIn("Read it", help_text)

    def test_a_near_miss_is_named_rather_than_answered_with_all_37(self):
        cmds = self.parser.memcal_commands
        self.assertEqual(cli._unknown_command(["todoo"], cmds)[1][:1], ["todo"])
        self.assertEqual(cli._unknown_command(["breif"], cmds)[1], ["brief"])
        self.assertIsNone(cli._unknown_command(["todos"], cmds))
        self.assertIsNone(cli._unknown_command([], cmds))

    def test_the_value_of_home_is_not_mistaken_for_a_command(self):
        cmds = self.parser.memcal_commands
        self.assertIsNone(cli._unknown_command(["--home", "/tmp/mc", "todos"], cmds))
        self.assertIsNone(cli._unknown_command(["--home=/tmp/mc", "todos"], cmds))
        # ...but a typo after the flag is still a typo.
        self.assertEqual(cli._unknown_command(["--home", "/tmp/mc", "todoo"], cmds)[0], "todoo")

    def test_a_word_like_nothing_gets_no_guess(self):
        self.assertEqual(cli._unknown_command(["zzzzz"], self.parser.memcal_commands)[1], [])

    def test_doctor_reports_and_completes_a_missing_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".memcal"
            out = io.StringIO()
            with (
                mock.patch.object(cli.sources, "all_sources", return_value=[]),
                mock.patch.object(cli.schedule, "status", return_value={"installed": False}),
                contextlib.redirect_stdout(out),
            ):
                cli.main(["--home", str(home), "doctor"])

            report = out.getvalue()
            self.assertIn(f"{home} — created just now", report)
            self.assertTrue((home / "memcal.db").is_file())
            self.assertTrue((home / "wiki").is_dir())
            self.assertTrue((home / "brief.md").is_file())
            self.assertIn("## This week", (home / "brief.md").read_text(encoding="utf-8"))



class TestGroupsStayGroups(Base):
    """A line from a thirty-person chat must never file itself under one speaker."""

    def test_a_group_beats_the_person_whoever_asks(self):
        # Four call sites used to decide this independently; two kept the person.
        self.assertEqual(
            gate.entity_for(person="parker shaw", thread="Alumni Chat",
                            stream="groupme", is_group=True),
            "thread:groupme:Alumni Chat")
        self.assertEqual(
            gate.entity_for(person="parker shaw", thread="Parker Shaw",
                            stream="groupme", is_group=False),
            "person:parker shaw")

    def test_an_unnamed_dm_keys_on_its_thread(self):
        self.assertEqual(
            gate.entity_for(person=None, thread="+15551234567", stream="imessage",
                            is_group=False),
            "thread:imessage:+15551234567")

    def test_pending_group_lines_get_re_filed(self):
        aid = archive.append(
            self.conn, stream="groupme", external_id="g1", ts=db.now(),
            text="yo ravers, midnight tyrannosaurus next saturday", thread="Alumni Chat",
            handle="groupme:1", person="parker shaw", from_me=False,
            meta={"group": True}, gated=True, gate_reason="temporal")
        archive.spool_add(self.conn, aid, "person:parker shaw")   # what the bug wrote
        self.conn.commit()

        self.assertEqual(archive.spool_rekey_groups(self.conn), 1)
        entity = self.conn.execute(
            "SELECT entity FROM spool WHERE archive_id = ?", (aid,)).fetchone()["entity"]
        self.assertEqual(entity, "thread:groupme:Alumni Chat")
        # Idempotent, and it leaves genuine person bundles alone.
        self.assertEqual(archive.spool_rekey_groups(self.conn), 0)

    def test_a_processed_row_is_left_where_it_is(self):
        aid = archive.append(
            self.conn, stream="groupme", external_id="g2", ts=db.now(), text="old news",
            thread="Alumni Chat", handle="groupme:1", person="parker shaw",
            from_me=False, meta={"group": True}, gated=True, gate_reason="temporal")
        archive.spool_add(self.conn, aid, "person:parker shaw")
        self.conn.execute("UPDATE spool SET processed_at = ? WHERE archive_id = ?",
                          (db.now(), aid))
        self.conn.commit()
        self.assertEqual(archive.spool_rekey_groups(self.conn), 0)


class TestUnresolvedIsForPeople(Base):
    """The name-this-person queue must not open on Kohl's."""

    def test_a_sender_the_gate_already_archived_never_joins_the_queue(self):
        # How Kohl's actually got caught: not by its address, by its bulk headers. The
        # decision lives in the senders table, which is the point of that table.
        identity.set_sender(self.conn, "kohls@s.kohls.com", "archive", "bulk-headers")
        report = sources.base.IngestReport.opened("email")
        sources.base.deliver(
            self.conn, report, stream="email", external_id="k1", ts=db.now(),
            text="It's HERE save an extra 15%", handle="kohls@s.kohls.com",
            verdict=gate.Verdict(False, "test"))
        self.assertEqual(identity.unresolved(self.conn), [])
        self.assertEqual(report.unknown_handles, set())

    def test_an_obvious_machine_never_joins_it_even_unseen(self):
        report = sources.base.IngestReport.opened("email")
        sources.base.deliver(
            self.conn, report, stream="email", external_id="n1", ts=db.now(),
            text="your statement is ready", handle="no.reply.alerts@chase.com",
            verdict=gate.Verdict(False, "test"))
        self.assertEqual(identity.unresolved(self.conn), [])

    def test_a_single_letter_sending_subdomain_is_recognised(self):
        # s.kohls.com is the same shape as e.target.com and t.target.com, and only
        # labels above the registrable domain are ever examined.
        self.assertTrue(gate.is_automated("kohls@s.kohls.com"))
        self.assertFalse(gate.is_automated("casey@kohls.com"))

    def test_a_person_still_joins_it(self):
        report = sources.base.IngestReport.opened("imessage")
        sources.base.deliver(
            self.conn, report, stream="imessage", external_id="p1", ts=db.now(),
            text="hey its alex from the climbing gym", handle="+15559876543",
            verdict=gate.Verdict(True, "test"))
        self.assertEqual([r["handle"] for r in identity.unresolved(self.conn)],
                         ["+15559876543"])

    def test_the_queue_heals_what_an_older_build_wrote(self):
        identity.note_unresolved(self.conn, "kohls@s.kohls.com", "email", None, "15% off")
        identity.note_unresolved(self.conn, "sale@e.target.com", "email", None, "deals")
        identity.note_unresolved(self.conn, "+15551112222", "imessage", None, "hi")
        self.conn.commit()
        self.assertEqual(identity.forget_bulk_unresolved(self.conn), 2)
        self.assertEqual([r["handle"] for r in identity.unresolved(self.conn)],
                         ["+15551112222"])

    def test_a_sender_he_archived_by_hand_is_dropped_too(self):
        identity.set_sender(self.conn, "person@somewhere.com", "ignore", "you")
        identity.note_unresolved(self.conn, "person@somewhere.com", "email", None, "x")
        self.conn.commit()
        self.assertEqual(identity.forget_bulk_unresolved(self.conn), 1)


class TestConversationsHaveNames(Base):
    """A bundle called `thread:imessage:9858b62c161544bca4342589e0344bbe`."""

    def line(self, thread, text, *, person=None, mine=False, stream="imessage",
             label=None, offset=0, external=None):
        aid = archive.append(
            self.conn, stream=stream, external_id=external or f"x{id(text)}{offset}{person}",
            ts=(db.now_dt() - timedelta(hours=offset)).isoformat(
                timespec="seconds"),
            text=text, thread=thread,
            handle=None if mine else (f"+1555000{abs(hash(person or '')) % 10000:04d}"
                                      if person else None),
            person="me" if mine else person, from_me=mine,
            meta={"group": True}, gated=True, gate_reason="all-of:imessage")
        threads.record(self.conn, stream, thread, label=label, is_group=True)
        if aid:
            archive.spool_add(self.conn, aid, f"thread:{stream}:{thread}")
        self.conn.commit()
        return aid

    def test_an_unnamed_group_chat_is_named_by_who_is_in_it(self):
        guid = "9858b62c161544bca4342589e0344bbe"
        self.line(guid, "what time sunday", person="Quinn")
        self.line(guid, "going earlier than 6", person="Jamie", offset=1)
        self.line(guid, "i'm in", mine=True, offset=2)
        threads.refresh(self.conn)
        # Not the guid, and not "Quinn" either — the user is in this conversation.
        self.assertEqual(threads.title(self.conn, "imessage", guid),
                         "Me, Jamie, and Quinn")

    def test_a_chat_with_a_real_name_keeps_it(self):
        self.line("Crystal Harbor", "beach house is booked", person="Katie")
        threads.refresh(self.conn)
        self.assertEqual(threads.title(self.conn, "imessage", "Crystal Harbor"), "Crystal Harbor")

    def test_a_bundle_carries_the_name_into_the_prompt(self):
        guid = "chat984297825501853979"
        self.line(guid, "poker at 8?", person="Quinn")
        self.line(guid, "i'm down", person="Jamie", offset=1)
        threads.refresh(self.conn)
        bundle = bundle_stage.build(self.conn)[0]
        self.assertEqual(bundle.label, "Jamie and Quinn")
        # The entity line stays verbatim so the model's echo still routes.
        self.assertIn(f"BUNDLE thread:imessage:{guid}", bundle.render())
        self.assertIn("(Jamie and Quinn)", bundle.render())

    def test_an_id_is_never_mistaken_for_a_name(self):
        self.assertTrue(threads._opaque("9858b62c161544bca4342589e0344bbe"))
        self.assertTrue(threads._opaque("chat984297825501853979"))
        self.assertTrue(threads._opaque(""))
        self.assertFalse(threads._opaque("Crystal Harbor"))
        self.assertFalse(threads._opaque("Alumni Chat"))
        self.assertFalse(threads._opaque("24273"))          # a short code is a name of sorts


class TestConversationMembership(Base):
    def test_groupme_roster_is_stable_ids_not_nicknames(self):
        members = groupme.group_members({
            "members": [
                {"user_id": "123", "nickname": "DJ Pickle"},
                {"user_id": "456", "nickname": "Nolan"},
            ]
        })
        self.assertEqual(members, [
            ("groupme:123", "DJ Pickle"),
            ("groupme:456", "Nolan"),
        ])
        threads.record(self.conn, "groupme", "Ravers",
                       participants=[handle for handle, _name in members],
                       is_group=True)
        # The source's roster metadata is normalized as part of recording the thread.
        # A later message supplies names for the already-stored stable ids.
        threads.record_members(self.conn, "groupme", "Ravers", members)
        row = self.conn.execute(
            "SELECT participants FROM threads WHERE stream='groupme' AND thread='Ravers'"
        ).fetchone()
        self.assertEqual(db.jload(row["participants"], []),
                         ["groupme:123", "groupme:456"])

    def test_changed_and_per_group_names_are_kept_on_one_handle(self):
        threads.record_members(
            self.conn, "groupme", "Ravers", [("groupme:123", "DJ Pickle")])
        threads.record_members(
            self.conn, "groupme", "Ravers", [("groupme:123", "Alexander")])
        threads.record_members(
            self.conn, "groupme", "Family", [("groupme:123", "Alex")])
        self.assertEqual(
            set(threads.names_for_handle(self.conn, "groupme:123")),
            {"DJ Pickle", "Alexander", "Alex"})

    def test_linking_a_handle_joins_old_memberships_without_backfill(self):
        threads.record_members(
            self.conn, "groupme", "Ravers", [("groupme:123", "DJ Pickle")])
        self.assertEqual(
            threads.entity_people(self.conn, "thread:groupme:Ravers"), [])
        identity.link(self.conn, "groupme:123", "Alexander Rivera", source="cli")
        self.assertEqual(
            threads.entity_people(self.conn, "thread:groupme:Ravers"),
            ["Alexander Rivera"])

    def test_existing_archive_and_thread_metadata_backfills_memberships(self):
        threads.record(
            self.conn, "imessage", "Old Group",
            participants=["+15551234567"], is_group=True)
        archive.append(
            self.conn, stream="groupme", external_id="old-roster-1", ts=self.d(-10),
            text="elements soon", thread="Old Ravers", handle="groupme:987",
            meta={"seen_name": "DJ Turnip"}, gated=True)
        archive.append(
            self.conn, stream="groupme", external_id="old-roster-2", ts=self.d(-2),
            text="got my ticket", thread="Old Ravers", handle="groupme:987",
            meta={"seen_name": "Alex"}, gated=True)
        count = threads.refresh_members(self.conn)
        self.assertGreaterEqual(count, 2)
        handles = {
            row["handle"] for row in self.conn.execute(
                "SELECT handle FROM thread_members")
        }
        self.assertIn("+15551234567", handles)
        self.assertIn("groupme:987", handles)
        self.assertEqual(
            set(threads.names_for_handle(self.conn, "groupme:987")),
            {"DJ Turnip", "Alex"})


class TestOneConversationOneBundle(Base):
    """iMessage files a chat twice when somebody's phone drops to SMS."""

    def line(self, thread, text, person, *, offset=0, label=None):
        aid = archive.append(
            self.conn, stream="imessage", external_id=f"{thread}:{text}",
            ts=(db.now_dt() - timedelta(hours=offset)).isoformat(
                timespec="seconds"),
            text=text, thread=thread, handle=f"+1555{abs(hash(person)) % 1000000:06d}",
            person=person, from_me=False, meta={"group": True},
            gated=True, gate_reason="all-of:imessage")
        threads.record(self.conn, "imessage", thread, label=label or thread, is_group=True)
        archive.spool_add(self.conn, aid, f"thread:imessage:{thread}")
        self.conn.commit()
        return aid

    def test_the_same_chat_under_two_ids_becomes_one_bundle(self):
        # Their real case: one row carrying SMS+RCS, another carrying RCS, differing by a
        # trailing space in the display name.
        self.line("Crystal Harbor", "who's driving friday", "Katie")
        self.line("Crystal Harbor", "i can take 4", "Pat Baker", offset=1)
        self.line("Crystal Harbor ", "leaving at 3", "Katie", offset=2)
        self.line("Crystal Harbor ", "see you there", "Pat Baker", offset=3)
        threads.refresh(self.conn)

        bundles = bundle_stage.build(self.conn)
        self.assertEqual(len(bundles), 1, [b.entity for b in bundles])
        self.assertEqual(len(bundles[0].items), 4)
        self.assertEqual(len(bundles[0].merged), 1)
        # Both halves get marked read, or the one that lost the merge is read forever.
        self.assertEqual(len(bundles[0].spool_ids), 4)

    def test_two_real_chats_that_share_a_name_stay_apart(self):
        # Same name, no overlap in who is in them. Merging these would write one set of
        # people's plans onto another's.
        self.line("Family", "dinner sunday", "Mom")
        self.line("Family", "i'll bring wine", "Dad", offset=1)
        self.line("Family ", "standup at 10", "Cameron Reed", offset=2)
        self.line("Family ", "pushed to 11", "Eli Grant", offset=3)
        threads.refresh(self.conn)
        self.assertEqual(len(bundle_stage.build(self.conn)), 2)

    def test_a_chat_split_across_ids_with_no_name_merges_on_its_roster(self):
        self.line("6f2a9c1e4b8d47a591c3e07f2ab6d5c4", "sunday still on?", "Quinn",
                  label="6f2a9c1e4b8d47a591c3e07f2ab6d5c4")
        self.line("b1d84f0c93ae42fb87e5a26c1f9037bd", "yeah 6ish", "Quinn", offset=1,
                  label="b1d84f0c93ae42fb87e5a26c1f9037bd")
        threads.refresh(self.conn)
        self.assertEqual(len(bundle_stage.build(self.conn)), 1)


class TestAChatCanBeMuted(Base):
    """The dev chat the user cared about eight years ago, and the dog park the user still does."""

    def busy_chat(self, thread, *, mine=0, speakers=("Stranger",)):
        for i in range(threads.REVIEW_MIN_ITEMS + 2):
            who = speakers[i % len(speakers)]
            archive.append(
                self.conn, stream="groupme", external_id=f"{thread}:{i}", ts=db.now(),
                text=f"chatter {i}", thread=thread, handle=f"groupme:{who}",
                person=who, from_me=False, meta={"group": True},
                gated=True, gate_reason="temporal")
        for i in range(mine):
            archive.append(
                self.conn, stream="groupme", external_id=f"{thread}:mine{i}", ts=db.now(),
                text="i'm in", thread=thread, person="me", from_me=True,
                meta={"group": True}, gated=True, gate_reason="own-commitment")
        threads.record(self.conn, "groupme", thread, label=thread, is_group=True)
        self.conn.commit()

    def test_a_chat_he_is_not_part_of_is_raised_as_a_question(self):
        self.busy_chat("Public GroupMe API Development Chat")
        threads.refresh(self.conn)
        self.assertEqual([t["title"] for t in threads.review(self.conn)],
                         ["Public GroupMe API Development Chat"])

    def test_a_chat_he_posts_in_is_never_raised(self):
        self.busy_chat("PSK Gamin", mine=3)
        threads.refresh(self.conn)
        self.assertEqual(threads.review(self.conn), [])

    def test_a_chat_full_of_people_he_knows_elsewhere_is_never_raised(self):
        # The mutual-friend graph: the user says nothing in the ravers chat, but the people in
        # it are people the user talks to. That is their world.
        archive.append(self.conn, stream="imessage", external_id="dm1", ts=db.now(),
                       text="you around?", thread="+15551110000", handle="groupme:Logan",
                       person="Logan", from_me=False, gated=True, gate_reason="question")
        archive.append(self.conn, stream="imessage", external_id="dm2", ts=db.now(),
                       text="yeah", thread="+15551110000", person="me", from_me=True,
                       gated=True, gate_reason="all-of:imessage")
        self.busy_chat("Alumni Chat", speakers=("Logan",))
        threads.refresh(self.conn)
        self.assertEqual(threads.review(self.conn), [])

    def test_muting_clears_the_backlog_and_stops_the_next_one(self):
        self.busy_chat("Public GroupMe API Development Chat")
        for row in self.conn.execute(
                "SELECT id FROM archive WHERE thread = 'Public GroupMe API Development Chat'"):
            archive.spool_add(self.conn, row["id"],
                              "thread:groupme:Public GroupMe API Development Chat")
        self.conn.commit()
        out = threads.decide(self.conn, "groupme",
                             "Public GroupMe API Development Chat", "mute")
        self.assertEqual(out["retired"], threads.REVIEW_MIN_ITEMS + 2)
        self.assertEqual(bundle_stage.build(self.conn), [])
        # And it is no longer asked about, because it has been answered.
        threads.refresh(self.conn)
        self.assertEqual(threads.review(self.conn), [])

    def test_a_muted_chat_is_still_archived_and_searchable(self):
        self.busy_chat("Public GroupMe API Development Chat")
        threads.decide(self.conn, "groupme", "Public GroupMe API Development Chat", "mute")
        self.assertTrue(archive.search(self.conn, "chatter"))


class TestNoConversationIsStarved(Base):
    """`ORDER BY ts DESC LIMIT 500` was two days of the two loudest people."""

    def loud(self, person, n, *, start=0):
        for i in range(n):
            aid = archive.append(
                self.conn, stream="imessage", external_id=f"{person}:{i}",
                ts=(db.now_dt() - timedelta(minutes=start + i)).isoformat(
                    timespec="seconds"),
                text=f"{person} line {i}", thread=f"+1555{abs(hash(person)) % 1000000:06d}",
                handle=f"+1555{abs(hash(person)) % 1000000:06d}", person=person,
                from_me=False, gated=True, gate_reason="all-of:imessage")
            archive.spool_add(self.conn, aid, f"person:{person}")
        self.conn.commit()

    def test_a_quiet_conversation_still_gets_read(self):
        self.loud("Harper", 200)
        self.loud("Quinn", 200, start=1)
        # Older than every one of those, and the only thing on the calendar.
        self.loud("Mom", 1, start=5000)
        entities = {b.entity for b in bundle_stage.build(self.conn, limit=50)}
        self.assertIn("person:Mom", entities)

    def test_the_cap_is_per_conversation_not_global(self):
        self.loud("Harper", 200)
        bundle = bundle_stage.build(self.conn, limit=500, per_entity=30)[0]
        self.assertEqual(len(bundle.spool_ids), 30)
        self.assertEqual(bundle.waiting, 170)

    def test_what_is_left_behind_is_counted_not_dropped_silently(self):
        self.loud("Harper", 90)
        bundle = bundle_stage.build(self.conn, limit=40)[0]
        self.assertEqual(bundle.waiting, 50)
        # Still pending, so the next pass reads it.
        self.assertEqual(self.conn.execute(
            "SELECT count(*) n FROM spool WHERE processed_at IS NULL").fetchone()["n"], 90)


class TestAnAmendmentFindsWhatItAmends(Base):
    """"actually I can't do Saturday, let's do Sunday" — which Saturday?

    A pass reads a group chat, writes "Beer garden, Sat Aug 2", and marks that traffic
    read. Two days later the only new line is the amendment, and the conversation that
    gives it a referent is gone from the queue. The row is on the calendar, but the
    calendar was listed in the shared prefix undifferentiated — thirty rows with nothing
    saying which one belongs to the people in front of you.
    """

    def setUp(self):
        super().setUp()
        self.saturday = self.d(6)
        self.key = events.upsert(
            self.conn,
            {"date": self.saturday, "kind": "commitment", "subject": "me",
             "title": "Beer garden in Harbor Point", "location": "Harbor Point",
             "status": "mentioned", "participants": ["Quinn Brooks", "Rowan Vale"]},
            written_by="dream:nightly")[0].key

    def settled_it(self, entity, text, *, person="Quinn Brooks", days_ago=2):
        """A line a past pass was reading when it wrote the row, and the link to it."""
        stream, thread = "imessage", "+15559990000"
        if entity.startswith("thread:"):
            _kind, stream, thread = entity.split(":", 2)
        archive.append(
            self.conn, stream=stream, external_id=f"old:{entity}:{text[:20]}",
            ts=(db.now_dt() - timedelta(days=days_ago)).isoformat(
                timespec="seconds"),
            text=text, thread=thread, handle="+15559990000", person=person,
            from_me=False, gated=True, gate_reason="temporal")
        trace.stamp(self.conn, kind="event", ref=self.key, verb="opened", entity=entity,
                    stage="propose", run_id=1)
        self.conn.commit()

    def amendment(self, entity, text, person="Quinn Brooks", *, gate_reason="temporal"):
        archive.append(self.conn, stream="imessage", external_id=f"new:{text[:20]}",
                       ts=db.now(), text=text, thread="+15559990000",
                       handle="+15559990000", person=person, from_me=False,
                       gated=True, gate_reason=gate_reason)
        row = self.conn.execute("SELECT * FROM archive WHERE external_id = ?",
                                (f"new:{text[:20]}",)).fetchone()
        return bundle_stage.Bundle(entity=entity, items=[row], title=person)

    def test_the_row_arrives_with_its_key_so_the_diff_can_update_it(self):
        bundle = self.amendment("person:Quinn Brooks",
                                "actually I can't do Saturday, let's do Sunday")
        block = propose_stage.build_open_rows(self.conn, bundle)
        self.assertIn(self.key, block)
        self.assertIn("Beer garden in Harbor Point", block)
        self.assertIn("that exact key", block)

    def test_a_silent_generic_message_pass_gets_one_event_graph_retry(self):
        """A transport verdict must not hide a likely amendment from recovery.

        This is the saved hostile reply shape: the model listed the bundle in
        ``reviewed`` and returned no diff. BlueBubbles traffic carries the generic
        ``all-of:imessage`` reason, so the old planning-reason check retired it without
        the isolated second look that already protects explicit gate verdicts.
        """
        from memcal import llm

        self.settled_it("person:Quinn Brooks", "beer garden saturday? czech beer")
        bundle = self.amendment(
            "person:Quinn Brooks", "could we move it to sunday at noon instead",
            gate_reason="all-of:imessage")
        bid = propose_stage.bundle_id(bundle.entity)

        class SilentThenCorrect:
            def __init__(self):
                self.calls = 0

            def complete(inner, **kw):
                inner.calls += 1
                if inner.calls == 1:
                    payload = {"reviewed": [bid], "diffs": []}
                else:
                    payload = {"reviewed": [bid], "diffs": [{
                        "bundle": bid,
                        "events": [{"key": self.key,
                                    "title": "Beer garden in Harbor Point",
                                    "date": self.d(7), "time": "12:00"}],
                    }]}
                return llm.Reply(
                    text=json.dumps(payload), data=payload, usage=llm.Usage(calls=1),
                    model=kw.get("model", ""), generation_id=f"retry-{inner.calls}",
                    finish_reason="stop")

            def map(inner, jobs, worker, max_parallel=8, on_done=None):
                out = []
                for index, job in enumerate(jobs):
                    value = worker(job)
                    out.append(value)
                    if on_done:
                        on_done(index, value)
                return out

        client = SilentThenCorrect()
        self.cfg.pack_bundles = 1
        proposed, errors = propose_stage.propose_all(
            client, self.conn, self.cfg, [bundle])
        event_diffs = [diff["events"] for _bundle, diff, _gen in proposed
                       if diff.get("events")]
        self.assertEqual(client.calls, 2, "the silent likely amendment was not re-asked")
        self.assertEqual(event_diffs[0][0]["key"], self.key)
        self.assertTrue(any("event graph" in error for error in errors))

    def test_the_traffic_that_settled_it_comes_along(self):
        # Without this the model knows a Saturday row exists; it does not know that this
        # conversation is where the Saturday came from.
        self.settled_it("person:Quinn Brooks", "beer garden saturday? czech beer")
        bundle = self.amendment("person:Quinn Brooks",
                                "actually I can't do Saturday, let's do Sunday")
        block = propose_stage.build_open_rows(self.conn, bundle)
        self.assertIn("←from here", block)
        self.assertIn("beer garden saturday", block)

    def test_a_row_written_out_of_this_very_conversation_is_marked(self):
        self.settled_it("thread:groupme:PSK Gamin", "beer garden saturday?")
        bundle = self.amendment("thread:groupme:PSK Gamin", "cant do saturday")
        bundle.entity = "thread:groupme:PSK Gamin"
        block = propose_stage.build_open_rows(self.conn, bundle)
        self.assertIn("←from here", block)

    def test_somebody_elses_plan_is_not_offered(self):
        events.upsert(self.conn,
                      {"date": self.saturday, "kind": "commitment", "subject": "Mullin",
                       "title": "Mullin's dentist", "status": "confirmed",
                       "participants": ["Mullin"]}, written_by="dream:nightly")
        bundle = self.amendment("person:Quinn Brooks", "cant do saturday")
        block = propose_stage.build_open_rows(self.conn, bundle)
        self.assertIn("Beer garden", block)
        self.assertNotIn("dentist", block)

    def test_a_plan_further_out_than_the_brief_window_is_still_offered(self):
        # The brief shows a week because that is what the user reads. An amendment can be about
        # something two months away, and the prefix would never have mentioned it.
        far = events.upsert(
            self.conn, {"date": self.d(70), "kind": "commitment", "subject": "me",
                        "title": "Rowan's thing", "status": "confirmed",
                        "participants": ["Rowan Vale"]}, written_by="dream:nightly")[0].key
        self.assertNotIn(far, [e.key for e in events.window(
            self.conn, self.cfg.days_back, self.cfg.days_forward)])
        bundle = self.amendment("person:Rowan Vale", "can we push it a week",
                                person="Rowan Vale")
        self.assertIn(far, propose_stage.build_open_rows(self.conn, bundle))

    def test_a_bundle_with_nothing_to_amend_carries_no_block(self):
        # Most bundles. The block has to cost nothing when there is nothing to say.
        bundle = self.amendment("person:Terry North", "lol", person="Terry North")
        self.assertEqual(propose_stage.build_open_rows(self.conn, bundle), "")

    def test_a_declined_row_is_not_offered_back(self):
        events.upsert(self.conn,
                      {"key": self.key, "date": self.saturday, "kind": "commitment",
                       "subject": "me", "title": "Beer garden in Harbor Point",
                       "status": "declined", "participants": ["Quinn Brooks"]},
                      written_by="dream:nightly")
        bundle = self.amendment("person:Quinn Brooks", "cant do saturday")
        self.assertEqual(propose_stage.build_open_rows(self.conn, bundle), "")

    def test_the_amendment_is_never_quoted_back_as_its_own_evidence(self):
        # The bug this catches: written_from selected everything up to the write, and the
        # write happened in the same breath, so the model was shown "I can't do Saturday"
        # as the evidence for what Saturday was.
        self.settled_it("person:Quinn Brooks", "beer garden saturday? czech beer")
        bundle = self.amendment("person:Quinn Brooks", "cant do saturday, sunday?")
        block = propose_stage.build_open_rows(self.conn, bundle)
        self.assertIn("beer garden saturday", block)
        self.assertEqual(block.count("cant do saturday"), 0)

    def test_a_dm_finds_an_event_created_in_a_group_through_a_person(self):
        self.settled_it("thread:groupme:Beer Crew", "beer garden saturday?")
        bundle = self.amendment(
            "person:Quinn Brooks", "by the way I can't go to the beer thing")
        block = propose_stage.build_open_rows(self.conn, bundle)
        self.assertIn("OTHER POSSIBLY RELATED EVENTS INVOLVING PEOPLE HERE", block)
        self.assertIn(self.key, block)
        # The GroupMe's old traffic is not falsely described as coming from this DM.
        self.assertNotIn("←from here", block)

    def test_me_does_not_connect_a_group_chat_to_every_personal_event(self):
        cat, _ = events.upsert(
            self.conn,
            {"date": self.d(2), "kind": "commitment", "subject": "me",
             "title": "Take Mom's cat to the vet", "status": "confirmed"},
            written_by="dream:nightly")
        trace.stamp(
            self.conn, kind="event", ref=cat.key, verb="opened",
            entity="person:Mom", stage="propose", run_id=2)
        mine = self.amendment(
            "thread:groupme:Ravers", "I got the festival tickets",
            person="me")
        self.assertNotIn(cat.key, propose_stage.build_open_rows(self.conn, mine))

    def test_historical_group_roster_finds_a_shared_event(self):
        identity.link(self.conn, "groupme:42", "Quinn Brooks", source="test")
        threads.record(
            self.conn, "groupme", "Ravers", label="Ravers", is_group=True)
        threads.record_members(
            self.conn, "groupme", "Ravers", [("groupme:42", "Q-Money")])
        # The new line itself names nobody; the durable group roster supplies Quinn.
        bundle = self.amendment(
            "thread:groupme:Ravers", "can't make the beer thing", person=None)
        block = propose_stage.build_open_rows(self.conn, bundle)
        self.assertIn(self.key, block)
        self.assertIn("OTHER POSSIBLY RELATED EVENTS INVOLVING PEOPLE HERE", block)

    def test_one_shared_group_member_without_event_words_is_too_weak(self):
        identity.link(self.conn, "groupme:42", "Quinn Brooks", source="test")
        threads.record(
            self.conn, "groupme", "Ravers", label="Ravers", is_group=True)
        threads.record_members(
            self.conn, "groupme", "Ravers", [("groupme:42", "Q-Money")])
        dentist, _ = events.upsert(
            self.conn,
            {"date": self.d(3), "kind": "commitment", "subject": "me",
             "title": "Dentist appointment", "status": "confirmed",
             "participants": ["Quinn Brooks"]},
            written_by="dream:nightly")
        bundle = self.amendment(
            "thread:groupme:Ravers", "festival car camping passes", person=None)
        self.assertNotIn(dentist.key, propose_stage.build_open_rows(self.conn, bundle))

    def test_an_event_name_alone_does_not_create_a_global_edge(self):
        old, _ = events.upsert(
            self.conn,
            {"date": self.d(-15), "kind": "observed", "subject": "me",
             "title": "Elements meetup", "status": "happened",
             "participants": []},
            written_by="live")
        upcoming, _ = events.upsert(
            self.conn,
            {"date": self.d(8), "kind": "commitment", "subject": "me",
             "title": "Elements Music & Arts Festival", "status": "confirmed",
             "participants": []},
            written_by="live")
        reese, _ = events.upsert(
            self.conn,
            {"date": self.d(1), "kind": "commitment", "subject": "me",
             "title": "Pick up Morgan", "status": "confirmed",
             "participants": ["Morgan"]},
            written_by="live")
        threads.record(
            self.conn, "groupme", "Phi Sig Ravers",
            label="Phi Sig Ravers", is_group=True)
        bundle = self.amendment(
            "thread:groupme:Phi Sig Ravers",
            "I got the Elements tickets but no word on the car ticket",
            person=None)
        block = propose_stage.build_open_rows(self.conn, bundle)
        self.assertNotIn(upcoming.key, block)
        self.assertNotIn(old.key, block)
        self.assertNotIn(reese.key, block)

    def test_old_word_mentions_do_not_become_a_hidden_global_edge(self):
        upcoming, _ = events.upsert(
            self.conn,
            {"date": self.d(8), "kind": "commitment", "subject": "me",
             "title": "Elements Music & Arts Festival", "status": "confirmed",
             "participants": []},
            written_by="live")
        threads.record(
            self.conn, "groupme", "Phi Sig Ravers",
            label="Phi Sig Ravers", is_group=True)
        for i, text in enumerate((
                "hello people going to Elements",
                "did everyone get their Elements wristbands")):
            archive.append(
                self.conn, stream="groupme", external_id=f"old-elements-{i}",
                ts=self.d(-i - 2), text=text, thread="Phi Sig Ravers",
                handle=f"groupme:{100 + i}", gated=True)
        # Neither today's words nor old archive words are provenance or a people edge.
        # Repetition must not silently turn word overlap into a third graph path.
        bundle = self.amendment(
            "thread:groupme:Phi Sig Ravers",
            "still no word on the car camping ticket", person=None)
        block = propose_stage.build_open_rows(self.conn, bundle)
        self.assertNotIn(upcoming.key, block)

    def test_generic_event_words_do_not_form_global_edges(self):
        movie, _ = events.upsert(
            self.conn,
            {"date": self.d(5), "kind": "commitment", "subject": "me",
             "title": "Spider-Man movie", "status": "confirmed",
             "participants": []},
            written_by="live")
        bundle = self.amendment(
            "thread:groupme:Ravers", "maybe we should do a movie", person=None)
        self.assertNotIn(movie.key, propose_stage.build_open_rows(self.conn, bundle))

    def test_one_beer_garden_can_be_reached_from_two_groups_and_a_dm(self):
        trace.stamp(
            self.conn, kind="event", ref=self.key, verb="opened",
            entity="thread:groupme:Beer Crew", stage="propose", run_id=1)
        trace.stamp(
            self.conn, kind="event", ref=self.key, verb="updated",
            entity="thread:imessage:Weekend Plans", stage="propose", run_id=2)

        first = self.amendment(
            "thread:groupme:Beer Crew", "actually Sunday?", person=None)
        second = self.amendment(
            "thread:imessage:Weekend Plans", "what time was it?", person=None)
        dm = self.amendment(
            "person:Quinn Brooks", "I can't go after all", person="Quinn Brooks")

        self.assertIn(self.key, propose_stage.build_open_rows(self.conn, first))
        self.assertIn(self.key, propose_stage.build_open_rows(self.conn, second))
        dm_block = propose_stage.build_open_rows(self.conn, dm)
        self.assertIn(self.key, dm_block)
        self.assertIn("INVOLVING PEOPLE HERE", dm_block)


class TestAPlatformMuteIsEvidenceNotADecision(Base):
    """The user mutes GroupMe chats the user cares about. Measured: 16 muted, 15 with mutuals."""

    def chat(self, thread, *, mine=0, mutuals=False, muted=False):
        speakers = ("Logan",) if mutuals else ("Stranger",)
        if mutuals:
            # A conversation the user speaks in, so Logan becomes a mutual.
            archive.append(self.conn, stream="imessage", external_id=f"dm{thread}",
                           ts=db.now(), text="you around?", thread=f"dm{thread}",
                           handle="groupme:Logan", person="Logan", from_me=False, gated=True)
            archive.append(self.conn, stream="imessage", external_id=f"dmme{thread}",
                           ts=db.now(), text="yeah", thread=f"dm{thread}", person="me",
                           from_me=True, gated=True)
        for i in range(threads.REVIEW_MIN_ITEMS + 2):
            archive.append(self.conn, stream="groupme", external_id=f"{thread}:{i}",
                           ts=db.now(), text=f"chatter {i}", thread=thread,
                           handle=f"groupme:{speakers[0]}", person=speakers[0],
                           from_me=False, meta={"group": True}, gated=True,
                           gate_reason="temporal")
        for i in range(mine):
            archive.append(self.conn, stream="groupme", external_id=f"{thread}:m{i}",
                           ts=db.now(), text="i'm in", thread=thread, person="me",
                           from_me=True, meta={"group": True}, gated=True)
        threads.record(self.conn, "groupme", thread, label=thread, is_group=True,
                       platform_muted=muted,
                       platform_note="muted in GroupMe" if muted else "")
        self.conn.commit()
        threads.refresh(self.conn)

    def test_muting_alone_never_raises_a_chat_for_review(self):
        # "Game Night": muted, and full of people the user talks to every day.
        self.chat("Game Night", mutuals=True, muted=True)
        self.assertEqual(threads.review(self.conn), [])

    def test_muting_is_still_recorded_and_reported(self):
        self.chat("Game Night", mutuals=True, muted=True)
        card = [t for t in threads.rows(self.conn) if t["thread"] == "Game Night"][0]
        self.assertTrue(card["platform_muted"])
        self.assertEqual(card["platform_note"], "muted in GroupMe")
        self.assertEqual(card["decision"], "")          # evidence, not a decision

    def test_the_ask_policy_widens_review_to_muted_chats(self):
        self.chat("Game Night", mutuals=True, muted=True)
        self.assertEqual(threads.review(self.conn), [])
        self.assertEqual([t["thread"] for t in threads.review(self.conn, policy="ask")],
                         ["Game Night"])

    def test_the_ask_policy_still_respects_him_speaking(self):
        # The user posts in it. Muted or not, that is not a question.
        self.chat("House chat", mine=4, mutuals=True, muted=True)
        self.assertEqual(threads.review(self.conn, policy="ask"), [])

    def test_the_mute_policy_takes_the_platforms_word(self):
        self.chat("Game Night", mutuals=True, muted=True)
        self.chat("Alumni Chat", mutuals=True, muted=False)
        self.assertEqual(threads.apply_platform_mutes(self.conn, "mute"), 1)
        self.assertEqual(threads.muted(self.conn), {("groupme", "Game Night")})
        # Idempotent, and it never overrides a decision the user made by hand.
        self.assertEqual(threads.apply_platform_mutes(self.conn, "mute"), 0)

    def test_the_default_policy_changes_nothing(self):
        self.chat("Game Night", mutuals=True, muted=True)
        self.assertEqual(threads.apply_platform_mutes(self.conn, "show"), 0)
        self.assertEqual(threads.muted(self.conn), set())

    def test_a_hand_made_decision_survives_the_mute_policy(self):
        self.chat("Game Night", mutuals=True, muted=True)
        threads.decide(self.conn, "groupme", "Game Night", "read")
        self.assertEqual(threads.apply_platform_mutes(self.conn, "mute"), 0)
        self.assertEqual(threads.muted(self.conn), set())

    def test_an_expired_mute_is_not_a_mute(self):
        from memcal.sources import groupme as gm
        self.assertFalse(gm._muted(None))
        self.assertFalse(gm._muted(1714400237))          # 2024, long gone
        self.assertTrue(gm._muted(253402300800))         # GroupMe's "forever"
        self.assertFalse(gm._muted("not a number"))

    def test_recording_without_a_mute_flag_leaves_the_flag_alone(self):
        # Every message calls record(); only the group walk knows the mute state, so the
        # per-message calls must not blank it.
        self.chat("Game Night", mutuals=True, muted=True)
        threads.record(self.conn, "groupme", "Game Night", label="Game Night", is_group=True)
        card = [t for t in threads.rows(self.conn) if t["thread"] == "Game Night"][0]
        self.assertTrue(card["platform_muted"])


class TestWhenPhrases(Base):
    """"am I free saturday" has to resolve to a Saturday, not to a month listing."""

    MONDAY = date(2026, 7, 27)

    def test_a_bare_weekday_is_the_next_one_coming(self):
        self.assertEqual(db.parse_when("saturday", ref=self.MONDAY)[0], date(2026, 8, 1))
        self.assertEqual(db.parse_when("sat", ref=self.MONDAY)[0], date(2026, 8, 1))
        # Asked on the day itself, it means today — not a week away.
        self.assertEqual(db.parse_when("saturday", ref=date(2026, 8, 1))[0], date(2026, 8, 1))

    def test_next_weekday_skips_the_current_week(self):
        # Monday's "next tuesday" is not tomorrow, which is the reading that makes
        # the answer wrong in a way nobody notices.
        self.assertEqual(db.parse_when("next tuesday", ref=self.MONDAY)[0], date(2026, 8, 4))

    def test_a_weekend_is_two_days(self):
        start, span = db.parse_when("this weekend", ref=self.MONDAY)
        self.assertEqual((start, span), (date(2026, 8, 1), 2))
        self.assertEqual(db.parse_when("next weekend", ref=self.MONDAY)[0], date(2026, 8, 8))

    def test_an_iso_date_and_relative_words_both_work(self):
        self.assertEqual(db.parse_when("2026-08-02", ref=self.MONDAY)[0], date(2026, 8, 2))
        self.assertEqual(db.parse_when("tomorrow", ref=self.MONDAY)[0], date(2026, 7, 28))
        self.assertEqual(db.parse_when("today", ref=self.MONDAY)[0], self.MONDAY)

    def test_nonsense_falls_back_to_today_rather_than_raising(self):
        self.assertEqual(db.parse_when("whenever", ref=self.MONDAY)[0], self.MONDAY)
        self.assertEqual(db.parse_when("", ref=self.MONDAY)[0], self.MONDAY)


class TestRun5LostSixBundles(Base):

    def setUp(self):
        super().setUp()
        self.cfg.prompt_version = "v2"
        self.group = [bundle_stage.Bundle(entity="thread:imessage:61301", title="61301"),
                      bundle_stage.Bundle(entity="person:Anthony", title="Anthony"),
                      bundle_stage.Bundle(entity="person:Quinn Brooks")]
        self.ids = [propose_stage.bundle_id(b.entity) for b in self.group]

    def test_reviewed_with_no_diffs_marks_every_bundle_read(self):
        errors = []
        routed, _echoed = propose_stage._route_v2(
            self.group, {"reviewed": self.ids, "diffs": []}, errors)
        self.assertEqual(len(routed), 3)
        self.assertEqual([b.entity for b, _ in routed], [b.entity for b in self.group])
        self.assertTrue(all(d == propose_stage.EMPTY_DIFF for _b, d in routed))
        self.assertEqual(errors, [])

    def test_an_empty_reply_still_leaves_every_bundle_queued(self):
        # The other half of the guarantee. Saying nothing at all is still not an answer,
        # and must not retire traffic nobody looked at.
        routed, _echoed = propose_stage._route_v2(self.group, {}, [])
        self.assertEqual(routed, [])

    def test_a_diff_routes_by_id_without_the_bundle_being_reviewed(self):
        # A diff is itself proof the bundle was read. Only the other direction is unsafe.
        routed, _echoed = propose_stage._route_v2(
            self.group,
            {"reviewed": [], "diffs": [{"bundle": self.ids[1], "events": [{"title": "x"}]}]},
            [])
        self.assertEqual([b.entity for b, _ in routed], ["person:Anthony"])
        self.assertEqual(routed[0][1]["events"], [{"title": "x"}])

    def test_an_invented_id_is_dropped_rather_than_filed_positionally(self):
        # v1 fell back to position whenever the counts agreed, which silently writes a
        # memory onto the wrong person. There is no positional fallback in v2.
        errors = []
        routed, _echoed = propose_stage._route_v2(
            self.group,
            {"reviewed": self.ids,
             "diffs": [{"bundle": "ffffff", "events": [{"title": "wrong person"}]}]},
            errors)
        self.assertEqual(len(routed), 3)
        self.assertTrue(all(not d["events"] for _b, d in routed))
        self.assertTrue(any("unknown bundle id" in e for e in errors))

    def test_reviewing_a_bundle_that_was_not_sent_is_reported_not_obeyed(self):
        errors = []
        routed, _echoed = propose_stage._route_v2(
            self.group, {"reviewed": self.ids + ["abc123"], "diffs": []}, errors)
        self.assertEqual(len(routed), 3)
        self.assertTrue(any("not in this request" in e for e in errors))

    def test_the_id_the_model_is_given_is_the_id_it_must_echo(self):
        # The whole point of the change: what goes out and what routes back are the
        # same six characters, so there is nothing left for a normaliser to get wrong.
        block = propose_stage.build_bundle_block(self.cfg, self.group[0], self.conn)
        self.assertIn(f"BUNDLE ID {self.ids[0]}", block)
        suffix = propose_stage.build_suffix(self.cfg, self.group, self.conn)
        for bid in self.ids:
            self.assertIn(bid, suffix)
        self.assertIn("`reviewed`", suffix)

    def test_v1_still_routes_by_echoed_header_for_comparison(self):
        # v1 stays runnable, or the benchmark has nothing to measure against.
        self.cfg.prompt_version = "v1"
        routed = propose_stage._route(
            self.group,
            {"bundles": [{"entity": "BUNDLE person:Anthony   (Anthony)"}]},
            [])
        self.assertEqual([b.entity for b, _ in routed], ["person:Anthony"])
        self.assertNotIn("BUNDLE ID", propose_stage.build_suffix(
            self.cfg, self.group, self.conn))


class TestCallsAreKeptOnDisk(Base):
    """"Why did it write that?" should not be a network request to a third party.

    The trace used to live only at OpenRouter, which keeps content for calls made after
    logging was switched on and is under no obligation to keep it forever. The process
    making the call already has the prompt in hand and gets the reasoning back in the
    same response body.
    """

    class _Reply:
        text = '{"reviewed": ["abc123"], "diffs": []}'
        data = {"reviewed": ["abc123"], "diffs": []}
        reasoning = "thought about it"
        generation_id = "gen-test-1"
        model = "stepfun/step-3.7-flash"
        finish_reason = "stop"
        truncated = False
        usage = type("U", (), {"prompt_tokens": 10, "completion_tokens": 2,
                               "cached_tokens": 0, "cost": 0.001})()

    def test_a_call_round_trips_through_disk(self):
        from memcal import calls
        path = calls.save(self.cfg.home, reply=self._Reply(), stage="propose", run_id=7,
                          label="person:Anthony", prefix="INSTRUCTIONS", suffix="BUNDLE ID abc123",
                          max_tokens=9000, bundles=[{"id": "abc123", "entity": "person:Anthony",
                                                     "label": "Anthony", "lines": 3}])
        self.assertTrue(path.is_file())
        blob = calls.load(self.cfg.home, "gen-test-1", 7)
        self.assertEqual(blob["prefix"], "INSTRUCTIONS")
        self.assertEqual(blob["suffix"], "BUNDLE ID abc123")
        self.assertEqual(blob["reasoning"], "thought about it")
        self.assertEqual(blob["bundles"][0]["id"], "abc123")

    def test_a_call_is_findable_without_knowing_its_run(self):
        from memcal import calls
        calls.save(self.cfg.home, reply=self._Reply(), stage="propose", run_id=7)
        self.assertIsNotNone(calls.load(self.cfg.home, "gen-test-1"))
        self.assertIsNone(calls.load(self.cfg.home, "gen-nope"))

    def test_routing_outcomes_are_annotated_after_the_fact(self):
        # Which bundles a reply actually reached is decided a moment after the file is
        # written, and it is the single most useful thing about a call that misbehaved.
        from memcal import calls
        calls.save(self.cfg.home, reply=self._Reply(), stage="propose", run_id=7)
        calls.annotate(self.cfg.home, "gen-test-1", 7,
                       unrouted=[{"id": "abc123", "label": "Anthony"}])
        blob = calls.load(self.cfg.home, "gen-test-1", 7)
        self.assertEqual(blob["unrouted"][0]["label"], "Anthony")
        self.assertEqual(blob["prefix"], "")   # untouched fields survive the update


class TestTheClockCanBePinned(Base):
    """A multi-day benchmark has to be able to be Tuesday.

    Not cosmetic. Four behaviours read the clock and decide something structural with
    it: `upsert`'s written_today freeze, `_crosses_today`, the spool horizon, and the
    brief window. Two passes run a minute apart in real time both believe they are the
    same day, so day one's rows stay revisable when production would have frozen them —
    a benchmark without a pinned clock measures a path that never runs.
    """

    def tearDown(self):
        db.set_today(None)
        super().tearDown()

    @contextlib.contextmanager
    def no_env_pin(self):
        """Run without whatever day the surrounding process asked to be.

        `del os.environ["MEMCAL_TODAY"]` was how two of these used to clean up, and a
        delete is not a restore: under `MEMCAL_TODAY=2026-09-13 python3 -m unittest
        discover` the first of them unpinned the *run*, and every test after it in the
        process quietly went back to the real clock.
        """
        with mock.patch.dict(os.environ):
            os.environ.pop("MEMCAL_TODAY", None)
            yield

    def test_pinning_moves_today(self):
        db.set_today("2026-08-04")
        self.assertEqual(db.today(), date(2026, 8, 4))

    def test_releasing_hands_it_back_to_the_real_clock(self):
        # Only the *real* clock is underneath an in-process pin when nothing outside the
        # process has pinned it — `MEMCAL_TODAY` is the outer pin and releasing lands on
        # that instead, which is why the environment is cleared here rather than assumed
        # empty. Run the suite under a pinned day and the unguarded form fails.
        with self.no_env_pin():
            db.set_today("2026-08-04")
            db.set_today(None)
            self.assertEqual(db.today(), date.today())

    def test_the_environment_can_pin_it_for_a_subprocess(self):
        with mock.patch.dict(os.environ, {"MEMCAL_TODAY": "2026-08-04"}):
            self.assertEqual(db.today(), date(2026, 8, 4))

    def test_an_unparseable_pin_is_ignored_rather_than_fatal(self):
        with mock.patch.dict(os.environ, {"MEMCAL_TODAY": "not-a-date"}):
            self.assertEqual(db.today(), date.today())

    def test_now_moves_with_it_but_keeps_the_time_of_day(self):
        db.set_today("2026-08-04")
        stamp = db.parse_ts(db.now())
        self.assertEqual(stamp.date(), date(2026, 8, 4))
        # Ordering within a run has to stay real, or two writes in one pass can't be
        # told apart. Only the date moves.
        self.assertEqual(stamp.hour, datetime.now().hour)

    def test_a_row_written_on_a_pinned_yesterday_is_frozen_today(self):
        # The behaviour the whole hook exists for. A cheap pass may revise what it
        # wrote today and may not touch what an earlier day wrote.
        db.set_today("2026-08-03")
        events.upsert(self.conn, {"title": "Poker at Jordan's", "date": "2026-08-07",
                                  "time": "20:00"}, written_by="dream:nightly")
        db.set_today("2026-08-04")
        event, outcome = events.upsert(
            self.conn, {"title": "Poker at Jordan's", "date": "2026-08-07",
                        "time": "21:00"}, written_by="dream:cheap")
        self.assertEqual(outcome, "unchanged")
        self.assertEqual(event.time, "20:00")

    def test_the_same_row_is_still_revisable_within_one_pinned_day(self):
        db.set_today("2026-08-03")
        events.upsert(self.conn, {"title": "Poker at Jordan's", "date": "2026-08-07",
                                  "time": "20:00"}, written_by="dream:nightly")
        event, outcome = events.upsert(
            self.conn, {"title": "Poker at Jordan's", "date": "2026-08-07",
                        "time": "21:00"}, written_by="dream:cheap")
        self.assertEqual(outcome, "updated")
        self.assertEqual(event.time, "21:00")

    def test_the_brief_window_follows_the_pin(self):
        db.set_today("2026-08-03")
        events.upsert(self.conn, {"title": "Poker at Jordan's", "date": "2026-08-07"},
                      written_by="cli")
        self.assertEqual([e.title for e in events.window(self.conn, 3, 7)],
                         ["Poker at Jordan's"])
        # Same row, same database, a month later: outside the window, still stored.
        db.set_today("2026-09-03")
        self.assertEqual(events.window(self.conn, 3, 7), [])
        self.assertIsNotNone(events.get(self.conn, events.make_key(
            "Poker at Jordan's", "2026-08-07")))

    def test_a_wiki_slot_is_stamped_with_the_pinned_day(self):
        db.set_today("2026-08-04")
        wiki.set_slot(self.cfg.wiki_dir, "jordan", "where they live", "Eastwood")
        page = wiki.read(self.cfg.wiki_dir, "jordan")
        self.assertEqual(page.slots["where they live"]["ts"], "2026-08-04")


class TestTheWireFormatIsAKnob(Base):
    """What a bundle looks like to the model is a variable, not a taste.

    The corpus a benchmark feeds in is neutral — who said what, where, when — and the
    question "does it do better without `(imessage)` on all 45 lines" is answered by
    re-rendering the same records under another format. Two things every format owes
    the code rather than the reader: the `BUNDLE <entity>` head, and one line per
    message.
    """

    def _bundle(self, stream="imessage", n=3):
        for index in range(n):
            archive.append(self.conn, stream=stream, external_id=f"x{index}",
                           ts=f"2026-08-03T1{index}:00:00", text=f"line {index}",
                           thread="t1", person="Jordan", from_me=False)
        rows = self.conn.execute("SELECT * FROM archive ORDER BY ts").fetchall()
        return bundle_stage.Bundle(entity="person:Jordan", items=list(rows),
                                   title="Jordan")

    def test_v1_is_the_default_and_unchanged(self):
        b = self._bundle()
        self.assertEqual(b.render(), b.render("v1"))
        self.assertIn(" (imessage) Jordan: line 0", b.render())

    def test_the_quiet_format_drops_a_tag_the_header_already_carries(self):
        b = self._bundle()
        quiet = b.render("v2-quiet-stream")
        self.assertNotIn("(imessage)", quiet)
        self.assertIn("Jordan: line 0", quiet)
        # The shape line still says what stream it was, so nothing is actually lost.
        self.assertIn("on imessage", quiet)

    def test_it_is_shorter_which_is_the_entire_point(self):
        b = self._bundle(n=20)
        self.assertLess(len(b.render("v2-quiet-stream")), len(b.render("v1")))

    def test_every_format_keeps_the_head_and_one_line_per_message(self):
        b = self._bundle(n=5)
        self.assertTrue(bundle_stage.FORMATS, "no formats to check")
        for name in bundle_stage.FORMATS:
            text = b.render(name)
            self.assertIn("BUNDLE person:Jordan", text, name)
            for index in range(5):
                self.assertIn(f"line {index}", text, name)

    def test_the_agent_tag_survives_because_the_prompt_leans_on_it(self):
        # "Lines whose source is `agent` are the user talking to their assistant" — that
        # tag is the only thing marking the most reliable source in the system.
        b = self._bundle(stream="agent")
        self.assertIn("(agent)", b.render("v2-quiet-stream"))

    def test_a_multi_conversation_bundle_keeps_its_tags(self):
        archive.append(self.conn, stream="imessage", external_id="a", ts="2026-08-03T10:00:00",
                       text="hi", thread="t1", person="Jordan", from_me=False)
        archive.append(self.conn, stream="groupme", external_id="b", ts="2026-08-03T11:00:00",
                       text="yo", thread="t2", person="Jordan", from_me=False)
        rows = self.conn.execute("SELECT * FROM archive ORDER BY ts").fetchall()
        b = bundle_stage.Bundle(entity="person:Jordan", items=list(rows), title="Jordan",
                                convo_titles={("imessage", "t1"): "Jordan",
                                              ("groupme", "t2"): "poker crew"})
        # Two conversations in one bundle: the tag is doing disambiguating work now.
        self.assertIn("imessage/", b.render("v2-quiet-stream"))
        self.assertIn("groupme/", b.render("v2-quiet-stream"))

    def test_the_config_selects_one(self):
        from memcal.dream import propose as propose_stage
        self.assertEqual(propose_stage._fmt(self.cfg), "v1")
        self.cfg.bundle_format = "v2-quiet-stream"
        self.assertEqual(propose_stage._fmt(self.cfg), "v2-quiet-stream")


class TestARowCannotPredateItsOwnTraffic(Base):

    def _bundle(self, *stamps):
        for index, ts in enumerate(stamps):
            archive.append(self.conn, stream="imessage", external_id=f"h{index}",
                           ts=ts, text="poker friday", thread="t", person="Jordan")
        rows = self.conn.execute("SELECT * FROM archive ORDER BY ts").fetchall()
        return bundle_stage.Bundle(entity="person:Jordan", items=list(rows))

    def _apply(self, bundle, date_value):
        diff = {"events": [{"title": "Poker at Jordan's", "date": date_value}]}
        counts, log = apply_stage.apply_diffs(
            self.conn, self.cfg, [(bundle, diff, None)], written_by="dream:test")
        return counts, log

    def test_a_row_a_year_before_the_traffic_is_rejected(self):
        bundle = self._bundle("2026-08-03T18:00:00")
        counts, log = self._apply(bundle, "2025-08-07")
        self.assertTrue(any("rejected" in k for k in counts))
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM events").fetchone()["n"], 0)
        self.assertIn("source traffic starts", log[0])

    def test_the_forward_bound_still_works(self):
        bundle = self._bundle("2026-08-03T18:00:00")
        counts, _log = self._apply(bundle, "2027-08-07")
        self.assertTrue(any("rejected" in k for k in counts))

    def test_a_normal_row_still_lands(self):
        bundle = self._bundle("2026-08-03T18:00:00")
        self._apply(bundle, "2026-08-07")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM events").fetchone()["n"], 1)

    def test_looking_back_is_still_allowed(self):
        # `observed` rows and a late "how was dinner" both point backwards. The bound is
        # against a wrong year, not against the past.
        bundle = self._bundle("2026-08-03T18:00:00")
        self._apply(bundle, "2026-07-20")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM events").fetchone()["n"], 1)

    def test_the_window_is_measured_from_the_traffic_not_from_today(self):
        # A bundle of genuinely old traffic may write rows around itself — that is what
        # reading a backlog is. It is the distance from its own lines that is bounded.
        bundle = self._bundle("2026-02-01T18:00:00", "2026-02-02T18:00:00")
        self._apply(bundle, "2026-02-10")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM events").fetchone()["n"], 1)

    def test_an_empty_bundle_is_bounded_around_today(self):
        empty = bundle_stage.Bundle(entity="person:Nobody", items=[])
        earliest, latest = apply_stage._horizon(empty)
        self.assertLess(earliest, db.today())
        self.assertGreater(latest, db.today())


class TestASubjectNamesAPerson(Base):

    def setUp(self):
        super().setUp()
        identity.link(self.conn, "+15551110001", "Jordan Lee", source="test")
        identity.set_me(self.conn, "Casey", "Casey Morgan")

    def _write(self, **row):
        bundle = bundle_stage.Bundle(entity="person:Jordan Lee", items=[])
        diff = {"events": [{"title": "Ramen dinner", "date": self.d(2), **row}]}
        apply_stage.apply_diffs(self.conn, self.cfg, [(bundle, diff, None)],
                                written_by="dream:test")
        return events.Event.from_row(
            self.conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 1").fetchone())

    def test_a_title_in_the_subject_field_becomes_me(self):
        event = self._write(subject="Ramen, Thu 8:30pm")
        self.assertEqual(event.subject, "me")

    def test_the_key_is_not_prefixed_with_a_title(self):
        event = self._write(subject="Ramen, Thu 8:30pm")
        self.assertFalse(event.key.startswith("ramen-thu"))

    def test_a_known_contact_is_kept(self):
        event = self._write(subject="Jordan Lee")
        self.assertEqual(event.subject, "Jordan Lee")

    def test_a_participant_on_the_row_is_kept(self):
        # Somebody the store has not met yet, but who is plainly in this plan.
        event = self._write(subject="Morgan Blake", participants=["Morgan Blake"])
        self.assertEqual(event.subject, "Morgan Blake")

    def test_the_user_is_always_me(self):
        event = self._write(subject="Casey")
        self.assertEqual(event.subject, "me")

    def test_a_stranger_nobody_mentioned_falls_back_to_me(self):
        event = self._write(subject="Some Person Nobody Named")
        self.assertEqual(event.subject, "me")

    def test_no_page_is_opened_for_an_evening(self):
        self._write(subject="Ramen, Thu 8:30pm")
        self.assertNotIn("ramen-thu-8-30pm", wiki.page_worthy(self.conn))


class TestKeysAreMintedHereNotByTheModel(Base):
    """Found by tools/benchmark_temporal.py, 2026-07-28.

    The same run returned `"key": "alpha"` and `"key": "beta"` for two new rows, and
    both went in and stayed — a permanent identifier for a poker game, invented.

    A key names a row this store already has. One that matches nothing is not an update
    to anything, so it is dropped and `find_match` does the work it exists to do.
    """

    def _apply(self, **row):
        bundle = bundle_stage.Bundle(entity="person:Jordan", items=[])
        diff = {"events": [{"title": "Poker at Jordan's", "date": self.d(3), **row}]}
        apply_stage.apply_diffs(self.conn, self.cfg, [(bundle, diff, None)],
                                written_by="dream:test")

    def test_an_invented_key_is_not_honoured(self):
        self._apply(key="beta")
        self.assertIsNone(events.get(self.conn, "beta"))
        row = self.conn.execute("SELECT key FROM events").fetchone()
        self.assertIn("@", row["key"])

    def test_a_real_key_still_updates_its_row(self):
        self._apply()
        key = self.conn.execute("SELECT key FROM events").fetchone()["key"]
        self._apply(key=key, location="42 Example Street")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM events").fetchone()["n"], 1)
        self.assertEqual(events.get(self.conn, key).location, "42 Example Street")


class TestPrecedenceGuardsAgainstStaleness(Base):

    def setUp(self):
        super().setUp()
        self.event, _ = events.upsert(
            self.conn, {"title": "Movie with Riley", "date": self.d(7),
                        "status": "confirmed"}, written_by="live")
        # Backdate the write so "today" is not what lets anything through.
        yesterday = (db.today() - timedelta(days=1)).isoformat() + "T20:21:00"
        self.conn.execute("UPDATE events SET updated_at = ? WHERE id = ?",
                          (yesterday, self.event.id))
        self.conn.commit()

    def _nightly(self, **fields):
        return events.upsert(self.conn, {"key": self.event.key, "date": self.d(7),
                                         **fields},
                             written_by="dream:nightly", **{
                                 k: v for k, v in fields.pop("_kw", {}).items()})

    def test_a_stale_reread_is_still_refused(self):
        _row, verb = events.upsert(
            self.conn, {"key": self.event.key, "date": self.d(7), "status": "declined"},
            written_by="dream:nightly",
            evidence_ts=(db.today() - timedelta(days=4)).isoformat() + "T09:00:00")
        self.assertEqual(verb, "unchanged")
        self.assertEqual(events.get(self.conn, self.event.key).status, "confirmed")

    def test_newer_traffic_gets_through(self):
        _row, verb = events.upsert(
            self.conn, {"key": self.event.key, "date": self.d(7), "status": "declined"},
            written_by="dream:nightly",
            evidence_ts=db.today().isoformat() + "T17:35:00")
        self.assertEqual(verb, "updated")
        self.assertEqual(events.get(self.conn, self.event.key).status, "declined")

    def test_no_evidence_at_all_is_still_refused(self):
        _row, verb = events.upsert(
            self.conn, {"key": self.event.key, "date": self.d(7), "status": "declined"},
            written_by="dream:nightly")
        self.assertEqual(verb, "unchanged")

    def test_written_by_is_a_high_water_mark(self):
        # The pass adjusts a field it is allowed to adjust; the row does not thereby
        # stop being something the user settled.
        events.upsert(self.conn, {"key": self.event.key, "date": self.d(7),
                                  "location": "the Angelika"},
                      written_by="dream:nightly",
                      evidence_ts=db.today().isoformat() + "T17:35:00")
        self.assertEqual(events.get(self.conn, self.event.key).written_by, "live")

    def test_the_bundle_supplies_the_evidence_stamp(self):
        archive.append(self.conn, stream="imessage", external_id="x1",
                       ts=db.today().isoformat() + "T17:35:00",
                       text="movie's off", thread="t", person="Riley")
        rows = list(self.conn.execute("SELECT * FROM archive"))
        bundle = bundle_stage.Bundle(entity="person:Riley", items=rows)
        self.assertEqual(apply_stage._newest(bundle), rows[0]["ts"])


class TestAWakeConditionDoesNotFireOnItsOwnSentence(Base):

    def test_it_does_not_wake_in_the_pass_that_opened_it(self):
        before = db.now()
        todos.open_todo(self.conn, "Give Rowan back their EZ-Pass",
                        wake_condition="Rowan is back from Italy")
        woken = todos.check_wakes(
            self.conn, "i need to give rowan their ezpass back when hes home from italy",
            since=before)
        self.assertEqual(woken, [])

    def test_it_wakes_on_the_next_pass(self):
        todo, _verb = todos.open_todo(self.conn, "Give Rowan back their EZ-Pass",
                                      wake_condition="Rowan is back from Italy")
        # Yesterday's pass opened it; this one is reading traffic it has never seen.
        self.conn.execute("UPDATE todos SET opened_at = ? WHERE key = ?",
                          ((db.today() - timedelta(days=1)).isoformat() + "T08:05:00",
                           todo.key))
        self.conn.commit()
        woken = todos.check_wakes(self.conn, "welcome back! how was italy?",
                                  since=db.now())
        self.assertEqual([t.text for t in woken], ["Give Rowan back their EZ-Pass"])

    def test_unrelated_traffic_still_does_not_wake_it(self):
        todo, _verb = todos.open_todo(self.conn, "Give Rowan back their EZ-Pass",
                                      wake_condition="Rowan is back from Italy")
        self.conn.execute("UPDATE todos SET opened_at = ? WHERE key = ?",
                          ((db.today() - timedelta(days=1)).isoformat() + "T08:05:00",
                           todo.key))
        self.conn.commit()
        self.assertEqual(
            todos.check_wakes(self.conn, "poker moved to saturday", since=db.now()), [])


class TestSlotsRememberWhatTheyUsedToSay(Base):
    """The wiki's `event_history`, missing until 2026-07-28.

    Events resolve recency at write time and push the old value into `event_history`.
    Slots resolved recency at write time and pushed the old value into nothing, so when
    Jordan's Eastwood lease fell through, Eastwood simply stopped ever having been the
    case — no record of what changed, when, or on whose word.
    """

    def test_a_replaced_value_is_recorded(self):
        wiki.set_slot(self.cfg.wiki_dir, "jordan", "location", "Eastwood",
                      source="person:Jordan", conn=self.conn)
        wiki.set_slot(self.cfg.wiki_dir, "jordan", "location", "Riverton",
                      source="person:Jordan", conn=self.conn)
        moves = [(r["old_value"], r["new_value"])
                 for r in wiki.slot_history(self.conn, "jordan")]
        self.assertIn(("Eastwood", "Riverton"), moves)

    def test_the_page_holds_only_what_is_true_now(self):
        wiki.set_slot(self.cfg.wiki_dir, "jordan", "location", "Eastwood", conn=self.conn)
        wiki.set_slot(self.cfg.wiki_dir, "jordan", "location", "Riverton", conn=self.conn)
        page = wiki.read(self.cfg.wiki_dir, "jordan")
        self.assertEqual(page.slots["location"]["value"], "Riverton")
        self.assertNotIn("Eastwood", page.render())

    def test_the_first_write_records_an_empty_predecessor(self):
        wiki.set_slot(self.cfg.wiki_dir, "jordan", "location", "Eastwood", conn=self.conn)
        rows = wiki.slot_history(self.conn, "jordan", "location")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["old_value"])

    def test_rewriting_the_same_value_records_nothing(self):
        wiki.set_slot(self.cfg.wiki_dir, "jordan", "location", "Eastwood", conn=self.conn)
        wiki.set_slot(self.cfg.wiki_dir, "jordan", "location", "Eastwood", conn=self.conn)
        self.assertEqual(len(wiki.slot_history(self.conn, "jordan")), 1)

    def test_a_disk_only_caller_still_works(self):
        # The merge tool and the tests write without a database in hand.
        page = wiki.set_slot(self.cfg.wiki_dir, "jordan", "location", "Eastwood")
        self.assertEqual(page.slots["location"]["value"], "Eastwood")


class TestABundleHasOneNameInTheRequest(Base):

    def _bundle(self, entity="thread:agent:conversation"):
        archive.append(self.conn, stream="agent", external_id="a1",
                       ts=db.today().isoformat() + "T08:05:00",
                       text="remind me to give rowan their ezpass back", thread="conversation",
                       from_me=True, person="me")
        rows = list(self.conn.execute("SELECT * FROM archive"))
        return bundle_stage.Bundle(entity=entity, items=rows, title="conversation")

    def _block(self, bundle, version="v2"):
        cfg = Config(home=self.cfg.home)
        cfg.prompt_version = version
        return propose_stage.build_bundle_block(cfg, bundle, self.conn)

    def test_v2_never_states_the_entity_as_a_second_name(self):
        block = self._block(self._bundle())
        self.assertNotIn("BUNDLE thread:agent:conversation", block)
        self.assertNotIn("BUNDLE agent:conversation", block)

    def test_v1_still_states_the_entity_because_that_is_what_it_routes_on(self):
        block = self._block(self._bundle(), version="v1")
        self.assertIn("BUNDLE thread:agent:conversation", block)

    def test_the_id_is_said_once_next_to_the_traffic_before_context(self):
        bundle = self._bundle("person:Jordan Lee")
        events.upsert(self.conn, {"title": "Poker", "date": self.d(3),
                                  "participants": ["Jordan Lee"]},
                      written_by="dream:test")
        block = self._block(bundle)
        bid = propose_stage.bundle_id(bundle.entity)
        self.assertEqual(block.count(f"BUNDLE ID {bid}"), 1)
        self.assertLess(block.index("remind me to give rowan"),
                        block.index("ALREADY ON THE CALENDAR"))

    def test_it_is_not_said_twice_in_a_row_when_nothing_intervenes(self):
        block = self._block(self._bundle())
        bid = propose_stage.bundle_id("thread:agent:conversation")
        self.assertEqual(block.count(f"BUNDLE ID {bid}"), 1)

    def test_context_does_not_duplicate_the_head(self):
        bundle = self._bundle("person:Jordan Lee")
        events.upsert(self.conn, {"title": "Poker", "date": self.d(3),
                                  "participants": ["Jordan Lee"]},
                      written_by="dream:test")
        heads = [l for l in self._block(bundle).splitlines() if l.startswith("BUNDLE ID")]
        self.assertEqual(len(heads), 1)


class TestV2RoutingIsForgivingButNeverGuesses(Base):
    """The other half of the same finding, 2026-07-28.

    `_route_v2` matched on the id alone and overwrote on collision. When the model
    mislabelled two diffs with the *same* wrong id, the second silently replaced the
    first — and the one that vanished carried a climbing confirmation and a movie
    cancellation, both of which the model had reasoned out correctly.

    Widening the key table is safe because every key in it is a string printed on the
    block, matched exactly. Positional fallback is not, and stays out.
    """

    def _group(self):
        return [bundle_stage.Bundle(entity="person:Riley Morgan", title="Riley Morgan"),
                bundle_stage.Bundle(entity="thread:agent:conversation",
                                    title="conversation")]

    def _route(self, diffs, reviewed=None):
        group = self._group()
        errors = []
        payload = {"reviewed": reviewed if reviewed is not None else [], "diffs": diffs}
        routed, _echoed = propose_stage._route_v2(group, payload, errors)
        return {b.entity: d for b, d in routed}, errors

    def test_the_id_routes(self):
        bid = propose_stage.bundle_id("person:Riley Morgan")
        routed, errors = self._route([{"bundle": bid, "events": [{"title": "x"}]}])
        self.assertIn("person:Riley Morgan", routed)
        self.assertEqual(errors, [])

    def test_the_entity_routes_too(self):
        routed, errors = self._route(
            [{"bundle": "thread:agent:conversation", "todos": [{"text": "x"}]}])
        self.assertIn("thread:agent:conversation", routed)
        self.assertEqual(errors, [])

    def test_a_unique_label_routes_too(self):
        routed, errors = self._route([{"bundle": "Riley Morgan", "events": []}])
        self.assertIn("person:Riley Morgan", routed)
        self.assertEqual(errors, [])

    def test_an_invented_id_is_still_dropped(self):
        routed, errors = self._route([{"bundle": "ffffff", "events": [{"title": "x"}]}])
        self.assertEqual(routed, {})
        self.assertIn("unknown bundle id", errors[0])

    def test_two_diffs_for_one_bundle_merge_instead_of_overwriting(self):
        bid = propose_stage.bundle_id("person:Riley Morgan")
        routed, _errors = self._route([
            {"bundle": bid, "events": [{"title": "Climbing gym", "status": "confirmed"}]},
            {"bundle": bid, "events": [{"title": "Movie", "status": "declined"}]},
        ])
        titles = [e["title"] for e in routed["person:Riley Morgan"]["events"]]
        self.assertEqual(titles, ["Climbing gym", "Movie"])

    def test_merging_pools_every_array_not_only_events(self):
        bid = propose_stage.bundle_id("person:Riley Morgan")
        routed, _errors = self._route([
            {"bundle": bid, "questions": ["a"]},
            {"bundle": bid, "questions": ["b"], "todos": [{"text": "t"}]},
        ])
        diff = routed["person:Riley Morgan"]
        self.assertEqual(diff["questions"], ["a", "b"])
        self.assertEqual(len(diff["todos"]), 1)

    def test_reviewed_accepts_an_entity_as_readily_as_an_id(self):
        routed, errors = self._route([], reviewed=["thread:agent:conversation"])
        self.assertIn("thread:agent:conversation", routed)
        self.assertEqual(errors, [])


class TestTheCeilingCoversTheThinking(Base):

    REASONER = "stepfun/step-3.7-flash"

    def test_the_sweep_ceiling_is_boosted_for_a_reasoning_model(self):
        snapshot = "x" * 4000
        plain = sweep_stage.sweep_ceiling(snapshot, "anthropic/claude-sonnet-5")
        boosted = sweep_stage.sweep_ceiling(snapshot, self.REASONER)
        self.assertGreater(boosted, plain)

    def test_the_sweep_ceiling_clears_what_actually_truncated(self):
        # The run that failed: a ~1,550-character snapshot priced at 1,693 tokens.
        self.assertGreater(sweep_stage.sweep_ceiling("x" * 1550, self.REASONER), 3500)

    def test_a_small_request_is_not_starved(self):
        # Four tiny bundles is four judgements, however little text it carries — the
        # group that kept truncating while a bigger one finished.
        cfg = Config(home=self.cfg.home)
        cfg.propose_model = self.REASONER
        four = [bundle_stage.Bundle(entity=f"person:P{n}") for n in range(4)]
        self.assertGreaterEqual(propose_stage.model_ceiling(cfg, four), 16000)

    def test_the_floor_scales_with_the_number_of_judgements(self):
        cfg = Config(home=self.cfg.home)
        cfg.propose_model = self.REASONER
        one = [bundle_stage.Bundle(entity="person:A")]
        six = [bundle_stage.Bundle(entity=f"person:P{n}") for n in range(6)]
        self.assertGreater(propose_stage.model_ceiling(cfg, six),
                           propose_stage.model_ceiling(cfg, one) * 3)

    def test_a_big_request_still_gets_more_than_the_floor(self):
        cfg = Config(home=self.cfg.home)
        cfg.propose_model = self.REASONER
        big = [bundle_stage.Bundle(entity="person:Harper")]
        big[0].items = [None] * 900
        self.assertGreater(propose_stage.model_ceiling(cfg, big), 12000)

    def test_a_model_that_does_not_think_gets_no_floor(self):
        cfg = Config(home=self.cfg.home)
        cfg.propose_model = "anthropic/claude-sonnet-5"
        one = [bundle_stage.Bundle(entity="person:Mom")]
        self.assertEqual(propose_stage.model_ceiling(cfg, one),
                         propose_stage.output_ceiling(one))


class TestTheDaysAreNamedNotCounted(Base):
    """Found by tools/benchmark_temporal.py, 2026-07-28.

    "beer garden saturday? like 3", said on Monday the 3rd, landed on Sunday the 9th in
    two live runs out of four. Naming today and leaving the rest as arithmetic is asking
    a model to count, and it miscounts — a whole plan on the wrong day out of a sentence
    with no ambiguity in it. The strip lives in the shared prefix, which is the cached
    half, so it is paid for once per pass.
    """

    def test_every_day_in_reach_is_named(self):
        strip = propose_stage._calendar_strip(date(2026, 8, 3))
        for expected in ("Sat 8 Aug", "Sun 9 Aug", "Tue 11 Aug", "Sat 15 Aug"):
            self.assertIn(expected, strip)

    def test_today_is_marked(self):
        self.assertIn("Mon 3 Aug (TODAY)",
                      propose_stage._calendar_strip(date(2026, 8, 3)))

    def test_yesterday_is_there_because_traffic_refers_backwards(self):
        self.assertIn("Sun 2 Aug", propose_stage._calendar_strip(date(2026, 8, 3)))

    def test_it_reaches_the_prefix(self):
        prefix = propose_stage.build_prefix(self.conn, self.cfg)
        self.assertIn("THE DAYS BY NAME", prefix)

    def test_it_costs_about_a_line(self):
        self.assertLess(len(propose_stage._calendar_strip(date(2026, 8, 3))), 400)


class TestTheBriefSaysTheTimeOnce(Base):
    """A title is told not to carry the fields beside it. When one does anyway, the
    brief is where it shows: "Ramen, Thu 8:30pm, 8:30pm" — which is what a live run
    actually rendered, every row, for two days."""

    def test_a_title_carrying_the_time_does_not_get_it_twice(self):
        row = events.Event(key="k", date=self.d(1), title="Ramen, Thu 8:30pm",
                           time="20:30")
        self.assertEqual(row.one_line().count("8:30pm"), 1)

    def test_a_clean_title_still_gets_the_time(self):
        row = events.Event(key="k", date=self.d(1), title="Ramen dinner", time="20:30")
        self.assertIn("8:30pm", row.one_line())

    def test_the_match_is_on_the_rendered_form_not_the_stored_one(self):
        row = events.Event(key="k", date=self.d(1), title="Beer garden, Sat 3pm",
                           time="15:00")
        self.assertEqual(row.one_line().count("3pm"), 1)


class TestPageWorthinessUsesThePinnedClock(Base):
    """`page_worthy` filtered on SQLite's `date('now')` while everything else in the
    pass reasoned about `db.today()`. Under `--as-of` or a pinned test clock the wiki
    decided who mattered on a different day from the rest of the run."""

    def tearDown(self):
        db.set_today(None)
        super().tearDown()

    def test_a_row_near_the_pinned_today_counts(self):
        db.set_today(date(2026, 8, 4))
        events.upsert(self.conn, {"title": "Poker", "date": "2026-08-08",
                                  "participants": ["Jordan Lee"]},
                      written_by="dream:test")
        self.assertIn("jordan-lee", wiki.page_worthy(self.conn))

    def test_a_row_long_before_the_pinned_today_does_not(self):
        db.set_today(date(2026, 8, 4))
        events.upsert(self.conn, {"title": "Old thing", "date": "2026-01-02",
                                  "participants": ["Nobody Relevant"]},
                      written_by="dream:test")
        self.assertNotIn("nobody-relevant", wiki.page_worthy(self.conn))


class TestTheLaterBlockIsAboutThingsHeIsDoing(Base):
    """"Only add committed things to later. Lets not show opportunities that far out."

    A subscribed US-holidays feed had five of the eight Later slots — Rosh Hashanah, Yom
    Kippur, Hanukkah, Lunar New Year, Tax Day — and none of them was a plan. Past the
    window the bar rises from "memcal knows about it" to "the user is in it".
    """

    def setUp(self):
        super().setUp()
        db.set_today(date(2026, 8, 2))

    def tearDown(self):
        db.set_today(None)
        super().tearDown()

    def _later(self) -> str:
        return brief._later_block(self.conn, self.cfg, db.today())

    def test_a_subscribed_holiday_stays_out(self):
        events.upsert(self.conn, {"title": "Rosh Hashanah", "date": "2026-09-12",
                                  "kind": "opportunity", "status": "mentioned"},
                      written_by="ical")
        self.assertNotIn("Rosh Hashanah", self._later())

    def test_a_trip_he_is_taking_stays_in(self):
        events.upsert(self.conn, {"title": "Montana trip", "date": "2026-08-15",
                                  "until": "2026-08-23", "kind": "commitment",
                                  "status": "tentative"}, written_by="dream:test")
        self.assertIn("Montana trip", self._later())

    def test_a_commitment_he_has_declined_stays_out(self):
        events.upsert(self.conn, {"title": "Jack's 30th", "date": "2026-08-22",
                                  "kind": "commitment", "status": "declined"},
                      written_by="dream:test")
        self.assertNotIn("Jack's 30th", self._later())


class TestALaterRowSaysHowManyDaysItEats(Base):
    """"Montana trip doesn't list the date range; it's important to say it's a week."

    The span was in the `until` column and on no line anybody read, so a nine-day trip
    rendered identically to a haircut.
    """

    def setUp(self):
        super().setUp()
        db.set_today(date(2026, 8, 2))
        events.upsert(self.conn, {"title": "Montana trip", "date": "2026-08-15",
                                  "until": "2026-08-23", "kind": "commitment",
                                  "status": "tentative"}, written_by="dream:test")

    def tearDown(self):
        db.set_today(None)
        super().tearDown()

    def test_both_ends_are_named(self):
        line = brief._later_block(self.conn, self.cfg, db.today())
        self.assertIn("Sat 15", line)
        self.assertIn("Sun 23 Aug", line)

    def test_the_length_is_stated_rather_than_left_as_arithmetic(self):
        self.assertIn("9 days", brief._later_block(self.conn, self.cfg, db.today()))

    def test_a_single_day_row_says_nothing_about_a_span(self):
        events.upsert(self.conn, {"title": "Photoshoot", "date": "2026-08-20",
                                  "kind": "commitment", "status": "confirmed"},
                      written_by="dream:test")
        line = [l for l in brief._later_block(self.conn, self.cfg, db.today()).splitlines()
                if "Photoshoot" in l][0]
        self.assertNotIn("days", line)
        self.assertNotIn("–", line)


class TestARowWithNobodyOnItSaysWhereItCameFrom(Base):
    """"Sealed gathering isn't descriptive enough and needs to track who said they'd go."

    The row was true and unusable: no people, no place, nothing to ask anyone about. The
    missing half was never a longer title — it came out of a nine-person group chat, and
    the group's name is provenance rather than inference, so it is code.
    """

    def setUp(self):
        super().setUp()
        # Two speakers, because one makes it a DM and a DM is correctly named after the
        # person rather than the room.
        for handle, person, text in (("groupme:1", "Tom Klemm", "we on for the 15th?"),
                                     ("groupme:2", "Joe", "yep that's the plan")):
            archive.append(self.conn, stream="groupme", external_id=f"g-{handle}",
                           ts=db.now(), text=text,
                           thread="Lootbox Addicts Support Group", person=person,
                           handle=handle, meta={"group": True}, gated=True)
        threads.refresh(self.conn)
        events.upsert(self.conn, {"title": "Sealed gathering", "date": self.d(9),
                                  "kind": "opportunity", "status": "mentioned",
                                  "source": "thread:groupme:Lootbox Addicts Support Group"},
                      written_by="dream:test")

    def test_the_conversation_is_named_on_the_line(self):
        via = brief.attribution(self.conn)
        self.assertEqual(via["sealed-gathering@" + self.d(9)],
                         "from Lootbox Addicts Support Group")

    def test_a_row_that_already_names_people_is_left_alone(self):
        events.upsert(self.conn, {"title": "Poker", "date": self.d(3),
                                  "participants": ["Cameron Ortiz"],
                                  "source": "thread:groupme:Lootbox Addicts Support Group"},
                      written_by="dream:test")
        self.assertNotIn("poker@" + self.d(3), brief.attribution(self.conn))

    def test_an_opaque_chat_id_is_never_shown_to_a_person(self):
        events.upsert(self.conn, {"title": "Beer garden", "date": self.d(4),
                                  "source": "thread:imessage:9858b62c161544bca4342589e0344bbe"},
                      written_by="dream:test")
        self.assertNotIn("beer-garden@" + self.d(4), brief.attribution(self.conn))


class TestAQuestionThatNamesADayIsARow(Base):
    """"Q16 should be an event instead of a question."

    "Will you attend the 30th Precinct meeting on September 25, 2026?" names a thing and
    the day it happens. As a question it sat in "Ask about" until it expired, answered no
    date lookup, and made them tell memcal something it had already been told.
    """

    def setUp(self):
        super().setUp()
        # The beat's own August. `dates.resolve` reads a month and a day and anchors
        # them to the moment the line was said — the stated year is not what makes this
        # 2026 — so against the real clock the assertion below held only while it was
        # 2026 and turned into a 2027 date in February 2027.
        db.set_today("2026-08-05")

    def _bundle(self, texts):
        rows = []
        for text in texts:
            aid = archive.append(self.conn, stream="whatsapp",
                                 external_id=f"dp-{text[:12]}-{len(rows)}", ts=db.now(),
                                 text=text, thread="Doggo Park 142", person="A Neighbour",
                                 handle="wa:1", gated=True)
            rows.append(self.conn.execute(
                "SELECT * FROM archive WHERE id = ?", (aid,)).fetchone())
        self.conn.commit()
        return bundle_stage.Bundle(entity="thread:whatsapp:Doggo Park 142", items=rows)

    def test_an_attendance_question_with_a_date_becomes_an_opportunity(self):
        bundle = self._bundle(["the 30th precinct meeting about the dog run is on "
                               "September 25", "we should all go"])
        row = apply_stage._dated_occasion(
            "Will you attend the 30th Precinct community concerns meeting about the "
            "dog-run issues on September 25, 2026?", bundle)
        self.assertIsNotNone(row)
        self.assertEqual(row["date"], "2026-09-25")
        self.assertEqual(row["kind"], "opportunity")
        self.assertEqual(row["status"], "mentioned")

    def test_the_title_names_the_thing_and_the_detail_moves_to_the_note(self):
        bundle = self._bundle(["30th precinct meeting september 25"])
        row = apply_stage._dated_occasion(
            "Will you attend the 30th Precinct community concerns meeting about the "
            "dog-run issues on September 25, 2026?", bundle)
        self.assertNotIn("September", row["title"])
        self.assertNotIn("Will you", row["title"])
        self.assertIn("dog-run", row["note"])

    def test_it_reads_past_the_speaker_prefix_code_adds(self):
        """A question already in the store carries "In Doggo Park 142: " in front of it.
        A rule that stops applying to everything already written is not a rule."""
        bundle = self._bundle(["30th precinct meeting september 25"])
        self.assertIsNotNone(apply_stage._dated_occasion(
            "In Doggo Park 142: Will you attend the 30th Precinct meeting on "
            "September 25, 2026?", bundle))

    def test_a_question_with_no_day_in_it_stays_a_question(self):
        bundle = self._bundle(["we should do something about the dog run"])
        self.assertIsNone(apply_stage._dated_occasion(
            "Will you attend the next dog-run meeting?", bundle))

    def test_a_status_check_on_an_existing_plan_is_not_a_new_row(self):
        bundle = self._bundle(["gym sunday?"])
        self.assertIsNone(apply_stage._dated_occasion(
            "Are you and Morgan still going to the gym on Sunday, Aug 2?", bundle))


class TestAQuestionAboutSomethingNobodyMentioned(Base):
    """"Q1 doesn't have evidence clearly listed. The source isn't talking about it."

    "Morgan asked: When and where are you going to see Spider-Man?" carried 1,725 lines
    of an unrelated conversation as its receipt, and the phrase came from an *example* in
    the propose prompt. A row whose every name is absent from its own bundle is invented
    or misrouted, and nothing downstream can tell either from a real one.
    """

    def _bundle(self, texts, entity="person:Morgan"):
        rows = []
        for index, text in enumerate(texts):
            aid = archive.append(self.conn, stream="imessage",
                                 external_id=f"t-{entity}-{index}", ts=db.now(), text=text,
                                 thread="+15550001111", person="Morgan", gated=True)
            rows.append(self.conn.execute(
                "SELECT * FROM archive WHERE id = ?", (aid,)).fetchone())
        self.conn.commit()
        return bundle_stage.Bundle(entity=entity, items=rows)

    def test_a_question_naming_only_absent_things_is_refused(self):
        bundle = self._bundle(["what do you want from taco bell?", "crunchwrap supreme"])
        self.assertTrue(apply_stage._talks_about_nothing_here(
            bundle, "When and where are you going to see Spider-Man?"))

    def test_it_is_written_the_way_the_conversation_spelled_it(self):
        """"Spider-Man" against a thread that said "Spiderman" and "Spider man" matched
        neither under a word-boundary regex — and the question was real."""
        bundle = self._bundle(["Do we have ticket for Spiderman?", "no idea when"])
        self.assertFalse(apply_stage._talks_about_nothing_here(
            bundle, "When and where are you going to see Spider-Man?"))

    def test_one_name_present_is_enough(self):
        """A question naming the beer garden and asking whether Aaron is coming is a
        normal question about someone who has not spoken yet."""
        bundle = self._bundle(["bohemian hall sunday?"])
        self.assertFalse(apply_stage._talks_about_nothing_here(
            bundle, "Is the Bohemian Hall outing still on, and is Aaron confirmed?"))

    def test_a_question_naming_nothing_in_particular_is_left_alone(self):
        bundle = self._bundle(["you around later?"])
        self.assertFalse(apply_stage._talks_about_nothing_here(
            bundle, "When are you two getting together again?"))

    def test_an_attribution_is_never_invented_from_a_stray_question_mark(self):
        """"Morgan asked:" was attached because hers was the last line with a "?" in it.
        Scored that way, the winner in a bundle with no relevant line is whoever last
        typed one."""
        bundle = self._bundle(["what do you want from taco bell?"])
        self.assertNotIn("asked", apply_stage._standalone_question(
            bundle, "When is the dentist appointment?"))


class TestAQuestionSitsWithTheRowItIsAbout(Base):
    """"Q12 is related to E117 — does it mean what time? It should say what time."

    memcal held "Montana trip, Sat 15 – Sun 23 Aug" and, in a different block, "When are
    you and Priya leaving for the Montana trip?" — which reads as a memory system that
    has not read its own calendar.
    """

    def setUp(self):
        super().setUp()
        db.set_today(date(2026, 8, 2))
        self.trip, _ = events.upsert(
            self.conn, {"title": "Montana trip", "date": "2026-08-15",
                        "until": "2026-08-23", "status": "tentative",
                        "participants": ["Priya", "Morgan"]}, written_by="dream:test")

    def tearDown(self):
        db.set_today(None)
        super().tearDown()

    def test_the_question_finds_its_row(self):
        todos.ask(self.conn, "When are you and Priya leaving for the Montana trip "
                             "with Morgan?", written_by="dream:test")
        todos.relink_questions(self.conn)
        self.assertIn(self.trip.id, todos.questions_by_event(self.conn))

    def test_it_asks_for_the_time_once_the_day_is_settled(self):
        todos.ask(self.conn, "When are you and Priya leaving for the Montana trip "
                             "with Morgan?", written_by="dream:test")
        todos.relink_questions(self.conn)
        text = todos.open_questions(self.conn)[0]["text"]
        self.assertTrue(text.startswith("What time are you"), text)

    def test_a_guest_in_a_title_does_not_drag_in_every_question_about_her(self):
        """"Gym with Morgan" has her name in its title, so half its words matched every
        question that mentioned her: Spider-Man, Orlando and a tutor's appointment
        all filed themselves under a gym session."""
        gym, _ = events.upsert(self.conn, {"title": "Gym with Morgan", "date": "2026-08-02",
                                           "kind": "opportunity",
                                           "participants": ["Morgan"]},
                               written_by="dream:test")
        todos.ask(self.conn, "Morgan asked: When and where are you going to see "
                             "Spider-Man?", written_by="dream:test")
        todos.relink_questions(self.conn)
        self.assertNotIn(gym.id, todos.questions_by_event(self.conn))

    def test_a_question_shown_under_its_row_is_not_shown_again_below(self):
        todos.ask(self.conn, "When are you and Priya leaving for the Montana trip?",
                  written_by="dream:test")
        todos.relink_questions(self.conn)
        text = brief.render(self.conn, self.cfg)
        self.assertEqual(text.count("leaving for the Montana trip"), 1)


class TestNoTestEverWritesToTheRealCalendar(Base):

    def test_publishing_is_off_unless_asked_for(self):
        self.assertEqual(Config(home=Path("/tmp/nowhere")).publish_calendar, "")

    def test_a_config_loaded_for_a_scratch_home_does_not_publish(self):
        from memcal import config as config_mod
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(config_mod.load(home).publish_calendar, "")

    def test_publish_pending_does_nothing_without_a_calendar(self):
        """The guard is in the orchestrator, so no caller has to remember it."""
        events.upsert(self.conn, {"title": "Poker", "date": self.d(2),
                                  "status": "confirmed", "kind": "commitment"},
                      written_by="live")

        def refuse(*args, **kwargs):                 # any call at all is the failure
            raise AssertionError("publish_pending ran osascript with no calendar set")

        self.cfg.publish_calendar = ""
        self.assertEqual(ical.publish_pending(self.conn, self.cfg, runner=refuse), [])


class TestEveryTestInThisDirectoryActuallyRuns(Base):

    def _files(self):
        here = Path(__file__).resolve().parent
        return sorted(p for p in here.glob("*.py") if p.name.startswith("test_"))

    def test_there_are_test_files_to_check(self):
        self.assertGreater(len(self._files()), 1)

    def test_nothing_is_defined_after_the_entry_point(self):
        import ast
        self.assertTrue(self._files())
        for path in self._files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            entry = [n for n in tree.body
                     if isinstance(n, ast.If) and ast.unparse(n.test).startswith("__name__")]
            if not entry:
                continue
            after = [n for n in tree.body
                     if n.lineno > entry[-1].lineno
                     and isinstance(n, (ast.ClassDef, ast.FunctionDef))]
            self.assertEqual(
                [n.name for n in after], [],
                f"{path.name}: defined after `if __name__ == \"__main__\"`, so running "
                f"the file directly never sees them")

    def test_the_entry_point_is_the_last_statement(self):
        import ast
        self.assertTrue(self._files())
        for path in self._files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            entry = [n for n in tree.body
                     if isinstance(n, ast.If) and ast.unparse(n.test).startswith("__name__")]
            if not entry:
                continue
            self.assertIs(entry[-1], tree.body[-1], path.name)

    def test_running_this_file_directly_collects_what_discovery_does(self):
        """The claim in one line, checked rather than reasoned about."""
        import unittest as ut
        direct = ut.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
        found = ut.defaultTestLoader.discover(
            str(Path(__file__).resolve().parent), pattern="test_core.py")
        self.assertEqual(direct.countTestCases(), found.countTestCases())


def _test_sources() -> list[Path]:
    """Every file the checks below read as text, rather than run.

    The three classes that follow are all the same shape — a suite cannot notice a test
    it never ran, so something has to read the files and say so.
    """
    here = Path(__file__).resolve().parent
    return sorted([here / "_support.py", *here.glob("test_*.py"),
                   *here.glob("scenarios/*.py")])


class TestASuiteThatIsGreenOnlyOnWednesdays(Base):
    """Reject tests that read the wall clock or leak a pinned test clock."""

    #: The clock's own tests, which are about `date.today()` and have to name it.
    EXEMPT = {"TestTheClockCanBePinned"}

    #: `db.today()`/`db.now_dt()` honour a pin; these do not, so a test using one reads
    #: a different day from the code it is testing the moment anything pins the clock.
    WALL_CLOCK = ("datetime.now", "date.today", "datetime.today", "time.time")

    def _files(self):
        return _test_sources()

    def _classes(self, tree):
        return [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]

    def _calls(self, node, names):
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and ast.unparse(sub.func).endswith(names):
                yield sub

    def _pins(self, node) -> bool:
        return any(not (call.args and ast.unparse(call.args[0]) == "None")
                   for call in self._calls(node, "set_today"))

    def _releases(self, node) -> bool:
        """Hands the clock back, now or on the way out however the test ends."""
        if any(call.args and ast.unparse(call.args[0]) == "None"
               for call in self._calls(node, "set_today")):
            return True
        return any("set_today" in ast.unparse(call)
                   for call in self._calls(node, "addCleanup"))

    def test_there_are_test_files_to_check(self):
        self.assertGreater(len(self._files()), 5)

    def test_no_test_reads_the_wall_clock_directly(self):
        offenders = []
        for path in self._files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for cls in self._classes(tree):
                if cls.name in self.EXEMPT:
                    continue
                offenders += [f"{path.name}:{call.lineno} {cls.name} "
                              f"{ast.unparse(call.func)}()"
                              for call in self._calls(cls, self.WALL_CLOCK)]
        self.assertEqual(offenders, [],
                         "read the clock through db.today()/db.now_dt(), which a pinned "
                         "day moves and the wall clock does not")

    def test_a_pinned_day_is_always_handed_back(self):
        offenders = []
        support = ast.parse(
            (Path(__file__).resolve().parent / "_support.py").read_text(encoding="utf-8"))
        support_classes = {cls.name: cls for cls in self._classes(support)}
        for path in self._files():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            # Split test modules inherit the one canonical fixture from _support.py.
            # Include it in the local inheritance graph so moving a class between files
            # does not make its clock cleanup invisible to this textual guard.
            classes = {**support_classes,
                       **{cls.name: cls for cls in self._classes(tree)}}

            def released_by_fixture(cls, seen=()):
                """The class, or something it inherits from in this file, releases it."""
                methods = [m for m in cls.body if isinstance(m, ast.FunctionDef)]
                if any(self._releases(m) for m in methods
                       if m.name in ("setUp", "tearDown")):
                    return True
                return any(released_by_fixture(classes[base.id], seen + (cls.name,))
                           for base in cls.bases
                           if isinstance(base, ast.Name) and base.id in classes
                           and base.id not in seen)

            for cls in classes.values():
                if released_by_fixture(cls):
                    continue
                offenders += [f"{path.name}:{m.lineno} {cls.name}.{m.name}"
                              for m in cls.body
                              if isinstance(m, ast.FunctionDef)
                              and self._pins(m) and not self._releases(m)]
        self.assertEqual(offenders, [],
                         "a pin is process-global: release it in tearDown, or register "
                         "self.addCleanup(db.set_today, None) where you pin it")


class TestTwoClassesWithOneNameAreOneClass(Base):
    """`class TestAutomatedSenders(Base)` appeared twice in this file, 800 lines apart."""

    def _scopes(self, tree):
        """Every body in which two definitions would collide: module, class, function."""
        yield tree.body
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                yield node.body

    def _collisions(self, body):
        seen: dict[str, int] = {}
        for node in body:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
                if node.name in seen:
                    yield node.name, seen[node.name], node.lineno
                seen[node.name] = node.lineno

    def test_nothing_is_defined_twice_in_one_scope(self):
        offenders = []
        for path in _test_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for body in self._scopes(tree):
                offenders += [f"{path.name}: {name} defined at line {first}, "
                              f"replaced at line {again}"
                              for name, first, again in self._collisions(body)]
        self.assertEqual(offenders, [],
                         "the second definition wins and the first never runs; rename "
                         "one for the bug it was written for")

    def test_a_definition_in_a_different_scope_is_not_a_collision(self):
        """The decoy, and the reason this is scope-aware rather than a name count.

        Two test classes may each keep a private `_Reply` stub, and `test_groupme` and
        `test_web` may both define a `Client`. Those shadow nothing. Counting names
        across a whole file reports five of them and the real one drowns.
        """
        tree = ast.parse("class A:\n    class S: pass\nclass B:\n    class S: pass\n")
        self.assertEqual([c for body in self._scopes(tree)
                          for c in self._collisions(body)], [])
        # ...and the same file with both `S` in one body is caught.
        shadowed = ast.parse("class A:\n    class S: pass\n    class S: pass\n")
        self.assertEqual([name for body in self._scopes(shadowed)
                          for name, _first, _again in self._collisions(body)], ["S"])


class TestALoopOverNothingIsAlwaysGreen(Base):

    LITERAL = (ast.List, ast.Tuple, ast.Set, ast.Dict, ast.Constant, ast.JoinedStr)
    #: Iteration helpers that are empty exactly when what they wrap is.
    WRAPPERS = ("enumerate", "zip", "sorted", "reversed", "range", "tuple", "list", "set")

    def _literal_locals(self, fn) -> set[str]:
        """Names bound to a literal in this function — `blocked = ("a@b", …)`."""
        return {ast.unparse(t) for node in ast.walk(fn)
                if isinstance(node, ast.Assign) and isinstance(node.value, self.LITERAL)
                for t in node.targets if isinstance(t, ast.Name)}

    def _cannot_be_empty(self, node, names) -> bool:
        if isinstance(node, self.LITERAL):
            return True
        if isinstance(node, ast.Name) and node.id in names:
            return True
        if isinstance(node, ast.Call) and ast.unparse(node.func) in self.WRAPPERS:
            return bool(node.args) and self._cannot_be_empty(node.args[0], names)
        return False

    def _asserts(self, node):
        return [c for c in ast.walk(node) if isinstance(c, ast.Call)
                and ast.unparse(c.func).rsplit(".", 1)[-1].startswith("assert")]

    def _asserts_outside_loops(self, fn):
        """Assertions this call reaches without going through a loop, helpers included.

        A precondition factored into `not_empty(self, register())` is still a stated
        precondition; a check that only credits an inline `assertTrue` would push people
        to write it inline, which is a worse test for the same guarantee.
        """
        inside = {id(c) for loop in ast.walk(fn)
                  if isinstance(loop, (ast.For, ast.While))
                  for c in self._asserts(loop)}
        return [c for c in self._asserts(fn) if id(c) not in inside]

    def _helpers_called(self, fn, scope):
        for call in [c for c in ast.walk(fn) if isinstance(c, ast.Call)]:
            name = ast.unparse(call.func).rsplit(".", 1)[-1]
            if name in scope and scope[name] is not fn:
                yield scope[name]

    def test_a_test_whose_assertions_all_sit_in_a_loop_says_what_it_needs_first(self):
        offenders = []
        for path in _test_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            module = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
            for cls in [c for c in ast.walk(tree) if isinstance(c, ast.ClassDef)]:
                scope = {**module,
                         **{m.name: m for m in cls.body
                            if isinstance(m, ast.FunctionDef)}}
                for fn in [m for m in cls.body if isinstance(m, ast.FunctionDef)
                           and m.name.startswith("test")]:
                    if not self._asserts(fn):
                        continue
                    names = self._literal_locals(fn)
                    risky = [loop for loop in ast.walk(fn)
                             if isinstance(loop, ast.For)
                             and not self._cannot_be_empty(loop.iter, names)
                             and self._asserts(loop)]
                    if not risky:
                        continue
                    reachable = self._asserts_outside_loops(fn) + [
                        a for helper in self._helpers_called(fn, scope)
                        for a in self._asserts_outside_loops(helper)]
                    if not reachable:
                        offenders.append(f"{path.name}:{fn.lineno} {cls.name}.{fn.name}"
                                         f"  for … in "
                                         f"{ast.unparse(risky[0].iter)[:40]}")
        self.assertEqual(offenders, [],
                         "every assertion is inside a loop over something that can come "
                         "back empty; assert it is not, first")

    def test_a_loop_over_a_literal_is_not_flagged(self):
        """The decoy. Thirty-nine tests iterate a tuple written two lines above, and a
        check that reports those is a check nobody reads twice."""
        fn = ast.parse("def test_x(self):\n"
                       "    blocked = ('a@b', 'c@d')\n"
                       "    for a in blocked:\n"
                       "        self.assertTrue(a)\n").body[0]
        names = self._literal_locals(fn)
        self.assertTrue(self._cannot_be_empty(fn.body[1].iter, names))
        self.assertFalse(self._cannot_be_empty(
            ast.parse("conn.execute('select 1')").body[0].value, names))


class TestNoTestCanReachTheRealCalendar(Base):

    SWITCHES = ("publish_calendar", "publish_reminders", "publish_schedules")

    def _switched_on(self, fn):
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and any(
                    ast.unparse(t).rsplit(".", 1)[-1] in self.SWITCHES
                    for t in node.targets):
                if ast.unparse(node.value) not in ('""', "''"):
                    yield node.lineno

    def _stubbed(self, *nodes):
        """An explicit runner, or `ical` itself patched out. Nothing else counts."""
        for node in nodes:
            for call in [c for c in ast.walk(node) if isinstance(c, ast.Call)]:
                if any(kw.arg == "runner" for kw in call.keywords):
                    return True
                name = ast.unparse(call.func)
                if "patch" in name and "ical" in ast.unparse(call):
                    return True
        return False

    def _switching_tests(self):
        for path in _test_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for cls in [c for c in ast.walk(tree) if isinstance(c, ast.ClassDef)]:
                fixture = [m for m in cls.body if isinstance(m, ast.FunctionDef)
                           and m.name in ("setUp", "setUpClass")]
                for fn in [m for m in cls.body if isinstance(m, ast.FunctionDef)]:
                    lines = list(self._switched_on(fn))
                    if lines:
                        yield path, cls, fn, lines[0], fixture

    def test_every_test_that_publishes_hands_it_somewhere_to_write(self):
        offenders = [f"{path.name}:{line} {cls.name}.{fn.name}"
                     for path, cls, fn, line, fixture in self._switching_tests()
                     if not self._stubbed(fn, *fixture)]
        self.assertEqual(offenders, [],
                         "pass runner= or patch ical; the default runner is the real "
                         "osascript and this machine has a real Calendar on it")

    def test_the_check_has_something_to_check(self):
        """A guard over an empty set is a green light, which is the one thing this
        must never be: `publish_calendar` renamed and every hit above disappears."""
        self.assertGreater(len(list(self._switching_tests())), 20)


class _PartialCalendar:
    """An osascript that answers with a payload of the caller's choosing.

    Local to this class rather than shared: what is under test here is the *shape* of
    the payload, so a fake that helpfully normalises it would be testing itself.
    """

    def __init__(self, payload: str, code: int = 0):
        self.payload, self.code = payload, code

    def __call__(self, _command, stdout=None, stderr=None, text=True):
        stdout.write(self.payload.encode("utf-8"))
        self.stderr = io.StringIO("")
        return self

    def wait(self, timeout=None):
        return self.code

    def kill(self):
        pass


class TestOneUnreadableCalendarMadeEveryOtherEventLookDeleted(Base):
    """`catch (_) { calendarEvents = []; }` — one calendar whose `whose()` query threw
    contributed zero events and the snapshot still exited 0, so a partial read arrived
    downstream shaped exactly like a complete one. `reconcile_deleted` reads absence as
    deletion, so every row on that calendar was declined with an archived *"no longer on
    their calendar"* line and a question per series rule. `providers.partiful` has declined
    to act on an empty feed since the beginning; the generic half never learned to."""

    def _item(self, uid, title, when, calendar="Home"):
        return {"calendar_name": calendar, "calendar_uid": f"cal-{calendar}",
                "writable": True, "uid": uid, "title": title,
                "start": f"{when}T14:00:00.000Z", "end": f"{when}T15:00:00.000Z",
                "all_day": False, "location": "", "description": "", "url": "",
                "status": "confirmed", "recurrence": ""}

    def _scan(self, items, **kw):
        return ical.ingest_snapshot(self.conn, self.cfg, items,
                                    scan_start=self.d(-120), scan_end=self.d(365), **kw)

    def _age(self):
        """Push every row's last sight into the past, so absence is not a same-day blip."""
        self.conn.execute("UPDATE calendar_items SET last_seen_at = ?",
                          (f"{self.d(-30)}T00:00:00",))

    def _statuses(self):
        return {row.title: row.status for row in events.window(self.conn, 120, 365)}

    # -- the read now says what it could not read ----------------------------

    def test_a_calendar_that_failed_to_read_is_named_by_the_snapshot(self):
        snapshot = ical._calendar_snapshot(
            self.d(-1), self.d(1),
            opener=_PartialCalendar(json.dumps({
                "events": [{"uid": "a"}],
                "unreadable": [{"name": "Work", "detail": "Can't get object"},
                               {"name": "Work", "detail": "Can't get object"}]})))
        self.assertEqual(snapshot.items, [{"uid": "a"}])
        self.assertEqual(snapshot.unreadable, ("Work",),
                         "one calendar that failed twice is one calendar")
        self.assertFalse(snapshot.complete)

    def test_a_payload_that_cannot_say_what_it_missed_is_refused(self):
        """The old shape was a bare array, which has no way to report a partial read.

        Accepting it "for compatibility" is the bug itself: a payload memcal cannot
        interrogate would be read as complete, which is the one reading that costs
        commitments.
        """
        for payload in ('[{"uid": "a"}]', '{"events": []}', '{"unreadable": []}'):
            with self.assertRaises(spec.SourceError, msg=payload):
                ical._calendar_snapshot(
                    self.d(-1), self.d(1), opener=_PartialCalendar(payload))

    def test_no_catch_in_the_scan_drops_events_without_naming_the_calendar(self):
        """Read as text, because the failure is a *new* swallowing `catch` and no test
        can be written in advance for a code path that does not exist yet.

        Structural rather than a word count: a `try` that acquires events must have a
        `catch` that calls `failed(`. `"failed" in body` would be satisfied by the
        comment above it, which is how the `utc_stamp` guard was defeated once already.
        """
        def block(source, opening):
            depth = 0
            for pos in range(opening, len(source)):
                if source[pos] == "{":
                    depth += 1
                elif source[pos] == "}":
                    depth -= 1
                    if depth == 0:
                        return source[opening + 1:pos], pos + 1
            raise AssertionError("unbalanced braces in the JXA")

        source, pairs, cursor = ical.JXA, [], 0
        while (found := re.search(r"\btry\s*\{", source[cursor:])) is not None:
            body, after = block(source, cursor + found.end() - 1)
            caught = re.match(r"\s*catch\s*\([^)]*\)\s*\{", source[after:])
            if caught:
                handler, after = block(source, after + caught.end() - 1)
                pairs.append((body, handler))
            cursor = after
        acquiring = [pair for pair in pairs
                     if "whose(" in pair[0] or ".properties()" in pair[0]]
        self.assertEqual(len(acquiring), 2,
                         "the scan acquires events in two places; if that moved, "
                         "re-read both — this check is worthless over an empty list")
        for body, handler in acquiring:
            self.assertIn("failed(", handler,
                          f"a read that can lose events swallows the failure: {body[:60]!r}")

    # -- and nothing judges a row missing from a read that did not finish ----

    def test_one_unreadable_calendar_declines_nothing_it_could_not_read(self):
        """The headline. Two rows on a calendar that threw, one on a calendar that did
        not; the snapshot is missing the two and says why."""
        here = self._item("HOME-1", "Standing thing", self.d(3))
        self._scan([here,
                    self._item("WORK-1", "Board review", self.d(5), calendar="Work"),
                    self._item("WORK-2", "One-on-one", self.d(6), calendar="Work")])
        self._age()

        report = self._scan([here], unreadable=("Work",))

        self.assertEqual(self._statuses(),
                         {"Standing thing": "confirmed", "Board review": "confirmed",
                          "One-on-one": "confirmed"})
        self.assertEqual(
            self.conn.execute(
                "SELECT count(*) n FROM calendar_items WHERE active = 0").fetchone()["n"],
            0, "a row was retired on the strength of a read that did not finish")
        self.assertEqual(
            self.conn.execute("SELECT count(*) n FROM archive"
                              "  WHERE external_id LIKE '%:deleted:%'").fetchone()["n"], 0)
        said = " ".join(report.notes)
        self.assertIn("Work", said, "the calendar that failed is not named anywhere")
        self.assertIn("could not be read", said)

    def test_the_events_that_did_arrive_are_still_filed(self):
        """Standing down is only about *absence*. An event that arrived is evidence
        whatever else failed, and dropping the whole pass would trade one wrong write
        for a stalled calendar."""
        self._scan([self._item("HOME-1", "Standing thing", self.d(3))],
                   unreadable=("Work",))
        self.assertEqual(self._statuses(), {"Standing thing": "confirmed"})

    def test_an_empty_snapshot_is_a_failed_read_not_a_mass_deletion(self):
        """Nothing came back, so nothing came back *about* any particular row. A read
        can return an empty list and exit 0 — `app.calendars()` answering before the
        accounts have loaded does exactly that, and reports no error at all."""
        self._scan([self._item("HOME-1", "Standing thing", self.d(3)),
                    self._item("HOME-2", "Dentist", self.d(4))])
        self._age()

        report = self._scan([])

        self.assertEqual(self._statuses(),
                         {"Standing thing": "confirmed", "Dentist": "confirmed"})
        self.assertIn("no events at all", " ".join(report.notes))

    def test_the_empty_guard_is_in_reconcile_deleted_and_not_only_at_the_call_site(self):
        """Partiful's guard lives in the function that decides; this one has to as well,
        or the next caller re-opens the hole by not knowing to check."""
        self._scan([self._item("HOME-1", "Standing thing", self.d(3))])
        self._age()
        report = base.IngestReport(stream="ical")

        ical.reconcile_deleted(self.conn, seen=set(), seen_uids=set(),
                               scan_start=self.d(-120), scan_end=self.d(365),
                               report=report)

        self.assertEqual(self._statuses(), {"Standing thing": "confirmed"})
        self.assertEqual(
            self.conn.execute("SELECT count(*) n FROM calendar_items"
                              "  WHERE active = 0").fetchone()["n"], 0)

    def test_the_connector_hands_the_partial_read_on_to_the_filing_pass(self):
        """The wiring, which every other test here would pass without.

        `fetch` is the only place the two halves meet: the read knows which calendars
        failed and the filing pass is what stands down. Drop the one keyword between
        them and the guard is present, tested and never reached in production.
        """
        here = self._item("HOME-1", "Standing thing", self.d(3))
        self._scan([here, self._item("WORK-1", "Board review", self.d(5),
                                     calendar="Work")])
        self._age()
        report = base.IngestReport(stream="ical")

        with mock.patch.object(
            ical, "_calendar_snapshot",
            lambda *_a, **_kw: ical.Snapshot(items=[here], unreadable=("Work",)),
        ):
            ical.ICalSource().fetch(self.conn, self.cfg, report, 1000)

        self.assertEqual(self._statuses(),
                         {"Standing thing": "confirmed", "Board review": "confirmed"})
        self.assertIn("could not be read", " ".join(report.notes))

    def test_an_incomplete_read_is_not_a_platform_unsubscribe_either(self):
        """A policy judges its own feed and cannot see past it: with nothing in the
        snapshot, `partiful`'s `seen` is empty and it deactivates every sync record it
        holds, saying the user left a feed the user is still on. Only `ingest_snapshot` knows the
        difference between an empty feed and an empty read."""
        here = self._item("HOME-1", "Standing thing", self.d(3))
        self._scan([here,
                    self._item("P-1", "Rooftop party", self.d(5), calendar="Partiful"),
                    self._item("P-2", "Picnic", self.d(6), calendar="Partiful")])
        self._age()

        # A surviving row on an ordinary calendar, so the snapshot is not *empty* — the
        # other guard would catch that one, and this test would pass for a reason that
        # has nothing to do with the calendar that failed.
        report = self._scan([here], unreadable=("Partiful",))

        active = self.conn.execute(
            "SELECT count(*) n FROM calendar_items"
            "  WHERE provider = 'partiful' AND active = 1").fetchone()["n"]
        self.assertEqual(active, 2, "a failed read was recorded as an unsubscribe")
        self.assertNotIn("unsubscribe", " ".join(report.notes))


class TestARunSpentAnHourAndReportedNoCalls(Base):

    def _client(self, reply_for):
        """A client whose `urlopen` is `reply_for(n)`, with the backoff neutralised."""
        import io
        import json as jsonlib
        from memcal import llm
        seen = {"n": 0}

        def respond(req, timeout=None):
            seen["n"] += 1
            body = reply_for(seen["n"])

            class _Resp(io.BytesIO):
                def __enter__(self_inner): return self_inner
                def __exit__(self_inner, *a): return False
            return _Resp(jsonlib.dumps(body).encode())

        real = llm.urllib.request.urlopen
        llm.urllib.request.urlopen = respond
        self.addCleanup(setattr, llm.urllib.request, "urlopen", real)
        # The waiting is not under test and a real backoff curve would put half an hour
        # of sleep in the suite. A *clock* rather than a no-op sleep, because
        # `capacity_budget` is spent in wall-clock: neutralise the sleep alone and the
        # give-up condition never arrives and the loop runs until the heat death.
        clock = {"t": 0.0}
        real_sleep, real_mono = llm.time.sleep, llm.time.monotonic
        llm.time.sleep = lambda s: clock.__setitem__("t", clock["t"] + s)
        llm.time.monotonic = lambda: clock["t"]
        self.addCleanup(setattr, llm.time, "sleep", real_sleep)
        self.addCleanup(setattr, llm.time, "monotonic", real_mono)
        return llm.OpenRouter("sk-or-test"), seen, llm

    #: The body the live store actually recorded, all 76 times.
    REFUSAL = {"error": {"message": "openai/gpt-5.6-luna is temporarily rate-limited "
                                    "upstream. Please retry shortly", "code": 429}}
    ANSWER = {"id": "gen-ok", "choices": [{"message": {"content": "{}"},
                                           "finish_reason": "stop"}],
              "usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.001}}

    def test_requests_are_counted_even_when_not_one_of_them_succeeds(self):
        client, seen, llm = self._client(lambda _n: self.REFUSAL)
        with self.assertRaises(llm.LLMError):
            client.complete(model="openai/gpt-5.6-luna", prefix="p", suffix="s")
        self.assertEqual(client.usage.calls, 0, "nothing returned, so nothing to count")
        self.assertEqual(client.usage.requests, seen["n"])
        self.assertGreater(client.usage.requests, 5, "a refused call made real requests")
        self.assertEqual(client.usage.failed, 1)
        self.assertGreater(client.usage.waited, 0, "the hour it spent asleep is the "
                                                   "whole reason nothing looked wrong")

    def test_a_reply_retried_into_existence_says_how_many_it_took(self):
        client, _seen, _llm = self._client(
            lambda n: self.REFUSAL if n < 3 else self.ANSWER)
        reply = client.complete(model="openai/gpt-5.6-luna", prefix="p", suffix="s")
        self.assertEqual(reply.requests, 3)
        self.assertGreater(reply.waited, 0)
        self.assertEqual(client.usage.calls, 1, "one prompt, one generation id, one call")
        self.assertEqual(client.usage.requests, 3)
        self.assertEqual(client.usage.failed, 0)

    def test_a_call_that_went_straight_through_reads_exactly_as_before(self):
        """The tail must be silence on a healthy pass or nobody will read it on a bad one."""
        client, _seen, _llm = self._client(lambda _n: self.ANSWER)
        client.complete(model="openai/gpt-5.6-luna", prefix="p", suffix="s")
        self.assertEqual(client.usage.requests, client.usage.calls)
        self.assertEqual(client.usage.summary(),
                         "1 calls · 10 in (0 cached) · 2 out · $0.0010")

    def test_the_summary_can_no_longer_say_nothing_happened(self):
        from memcal import llm
        line = llm.Usage(requests=76, failed=4, waited=6571.0).summary()
        self.assertIn("0 calls", line)
        self.assertIn("76 requests", line)
        self.assertIn("4 failed", line)
        self.assertIn("6571s in backoff", line)

    def test_the_run_row_records_what_no_completion_can_account_for(self):
        from memcal import llm
        from memcal.dream import run as dream_run
        cur = self.conn.execute(
            "INSERT INTO runs(started_at, mode, model) VALUES(?,?,?)",
            (db.now(), "nightly", "openai/gpt-5.6-luna"))
        run_id = int(cur.lastrowid)
        self.conn.commit()
        dream_run._finish(self.conn, run_id, dream_run.DreamResult(run_id=run_id),
                          usage=llm.Usage(requests=76, failed=4, waited=6571.0))
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        self.assertEqual(row["requests"], 76)
        self.assertEqual(row["failed_calls"], 4)
        self.assertAlmostEqual(row["wait_seconds"], 6571.0, places=1)
        self.assertEqual(row["cost_usd"], 0.0, "the dollars really were zero")

    def test_the_attempt_count_reaches_the_generations_row(self):
        from memcal import llm
        trace.record(self.conn, run_id=None, stage="live", label="patient",
                     reply=llm.Reply(text="{}", data={}, usage=llm.Usage(calls=1),
                                     generation_id="gen-patient", requests=7))
        row = self.conn.execute(
            "SELECT requests FROM generations WHERE generation_id = 'gen-patient'"
        ).fetchone()
        self.assertEqual(row["requests"], 7)


class TestTheUnattendedPathNeverHeardOnRetry(Base):
    """`OpenRouter.on_retry` exists because "a run that waits out a busy hour in silence
    is indistinguishable from one that has died". `dream` wires it to `emit`, and `emit`
    is a no-op when `progress is None` — which only `web.py` ever supplied.

    So the two paths that actually run unattended, the nightly launchd job and every CLI
    run, got nothing: run 13 made roughly 280 of those calls and `nightly.log` is silent
    from 03:00:04 to the summary at 03:56:39. A parameter that looks live and is reached
    by no caller is the same defect as a column written and read by nothing.
    """

    def test_the_cli_hands_dream_somewhere_to_say_it_is_waiting(self):
        seen = {}

        def fake_dream(conn, cfg, **kw):
            seen.update(kw)
            return type("R", (), {"errors": [], "nothing_new": True, "report": lambda s: "",
                                  "usage_summary": ""})()

        real, cli.dream = cli.dream, fake_dream
        self.addCleanup(setattr, cli, "dream", real)
        args = argparse.Namespace(home=str(self.cfg.home), mode="nightly", model=None,
                                  limit=0, dry_run=False, no_sweep=True, redo=None,
                                  rounds=1)
        with contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_dream(args)
        self.assertIsNotNone(seen.get("progress"),
                             "the unattended path passed no progress callback")

    def test_a_wait_is_printed_and_everything_else_stays_quiet(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli._dream_waits("stage", {"stage": "model", "state": "waiting",
                                       "note": "capacity; waiting 300s (600s so far)"})
            cli._dream_waits("stage", {"stage": "apply", "state": "done", "note": "7 writes"})
            cli._dream_waits("propose_request", {"index": 1, "ok": True})
        printed = out.getvalue()
        self.assertIn("capacity; waiting 300s", printed)
        self.assertNotIn("7 writes", printed)
        self.assertEqual(len(printed.strip().splitlines()), 1)

    def test_the_client_a_pass_builds_talks_to_the_callback_it_was_given(self):
        """The whole chain, not its ends: `on_retry` → `emit` → the caller's progress.

        Asserting the CLI passes *a* callback and separately that the callback prints
        leaves the middle link — the one that was actually broken — untested.
        """
        from memcal import llm
        from memcal.dream import run as dream_run
        heard: list[tuple[str, dict]] = []

        class Waiting:
            def __init__(self, key, *, on_retry=None):
                self.on_retry, self.usage = on_retry, llm.Usage()

            def complete(self, **kw):
                self.on_retry("capacity; waiting 300s (600s so far) — in-body 429")
                return llm.Reply(text="{}", data={"reviewed": [], "diffs": []},
                                 usage=llm.Usage(calls=1), model=kw.get("model", ""),
                                 generation_id="gen-waited", finish_reason="stop")

            def map(self, jobs, worker, max_parallel=8, on_done=None):
                out = []
                for index, job in enumerate(jobs):
                    value = llm._safe(worker, job)
                    out.append(value)
                    if on_done:
                        on_done(index, value)
                return out

        aid = archive.append(self.conn, stream="imessage", external_id="w1", ts=db.now(),
                             text="dinner tomorrow at 8?", thread="t", person="Jordan",
                             gated=True)
        archive.spool_add(self.conn, aid, "person:Jordan")
        self.conn.commit()
        with mock.patch.object(
                dream_run.llm, "client_for",
                side_effect=lambda _cfg, on_retry=None: Waiting("", on_retry=on_retry)):
            dream_run.dream(self.conn, self.cfg, mode="nightly", skip_sweep=True,
                            progress=lambda event, data: heard.append((event, data)))
        waits = [d for e, d in heard if e == "stage" and d.get("state") == "waiting"]
        self.assertTrue(waits, "on_retry reached nobody")
        self.assertIn("capacity", waits[0]["note"])


class TestTheCallsThatFailedNeverReachedDisk(Base):

    def _run_row(self) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs(started_at, mode, model) VALUES(?,?,?)",
            (db.now(), "nightly", "openai/gpt-5.6-luna"))
        self.conn.commit()
        return int(cur.lastrowid)

    def _bundle(self, entity: str) -> bundle_stage.Bundle:
        aid = archive.append(self.conn, stream="imessage", external_id=f"{entity}:0",
                             ts=db.now(), text="dinner tomorrow at 8?", thread=entity,
                             person=entity.split(":")[-1], gated=True)
        row = self.conn.execute("SELECT * FROM archive WHERE id = ?", (aid,)).fetchone()
        return bundle_stage.Bundle(entity=entity, items=[row])

    @staticmethod
    def _mapper(cls):
        from memcal import llm

        def map(self, jobs, worker, max_parallel=8, on_done=None):
            out = []
            for index, job in enumerate(jobs):
                value = llm._safe(worker, job)
                out.append(value)
                if on_done:
                    on_done(index, value)
            return out
        cls.map = map
        return cls

    def test_a_truncated_reply_leaves_the_record_a_success_leaves(self):
        from memcal import calls as calls_mod
        from memcal import llm

        @self._mapper
        class Truncating:
            def complete(self, **kw):
                return llm.Reply(text='{"reviewed": [', data=None,
                                 usage=llm.Usage(calls=1, prompt_tokens=5_000,
                                                 completion_tokens=1_800, cost=0.0043),
                                 model=kw.get("model", ""), generation_id="gen-cut",
                                 finish_reason="length")

        run_id = self._run_row()
        self.cfg.pack_bundles = 1
        _good, errors = propose_stage.propose_all(
            Truncating(), self.conn, self.cfg, [self._bundle("person:Jordan")],
            run_id=run_id)
        self.assertTrue(any("cut off" in e for e in errors))

        row = self.conn.execute(
            "SELECT * FROM generations WHERE generation_id = 'gen-cut'").fetchone()
        self.assertIsNotNone(row, "the most expensive call in the pass was not recorded")
        self.assertEqual(row["run_id"], run_id)
        self.assertAlmostEqual(row["cost_usd"], 0.0043, places=6)
        blob = calls_mod.load(self.cfg.home, "gen-cut", run_id)
        self.assertTrue(blob["truncated"])
        self.assertIn("cut off", blob.get("failed", ""))
        self.assertEqual([b["entity"] for b in blob["unrouted"]], ["person:Jordan"])

    def test_a_request_that_never_returned_leaves_what_was_sent(self):
        from memcal import calls as calls_mod
        from memcal import llm

        @self._mapper
        class Refusing:
            def complete(self, **kw):
                exc = llm.LLMError("gave up after 1589s of capacity waits "
                                   "(20 of them, 20 requests): in-body 429")
                exc.tally = llm.Tally(requests=20, waits=20, waited=1589.0)
                raise exc

        run_id = self._run_row()
        self.cfg.pack_bundles = 1
        propose_stage.propose_all(Refusing(), self.conn, self.cfg,
                                  [self._bundle("person:Quinn")], run_id=run_id)
        failures = calls_mod.failures_for_run(self.cfg.home, run_id)
        self.assertEqual(len(failures), 1, "a refused request left nothing behind")
        one = failures[0]
        self.assertIn("gave up after 1589s", one["error"])
        self.assertEqual(one["requests"], 20, "the attempt count died with the exception")
        self.assertAlmostEqual(one["waited"], 1589.0, places=1)
        self.assertEqual([b["entity"] for b in one["bundles"]], ["person:Quinn"])
        self.assertIn("dinner tomorrow", one["suffix"], "what was sent is not recoverable")

    def test_a_failure_is_never_filed_as_a_generation(self):
        """`generations.generation_id` is the key *OpenRouter* files a call under.

        A synthetic id there would be a $0.0000 call that `memcal trace` can never open
        and that every cost query would join against — so the count lives on the run row
        and the detail lives in the shard.
        """
        from memcal import llm

        @self._mapper
        class Refusing:
            def complete(self, **kw):
                raise llm.LLMError("[Errno 54] Connection reset by peer")

        run_id = self._run_row()
        self.cfg.pack_bundles = 1
        propose_stage.propose_all(Refusing(), self.conn, self.cfg,
                                  [self._bundle("person:Riley")], run_id=run_id)
        n = self.conn.execute("SELECT count(*) n FROM generations WHERE run_id = ?",
                              (run_id,)).fetchone()["n"]
        self.assertEqual(n, 0)

    def test_every_turn_of_a_live_write_is_written_down(self):
        """`propose_one` returned only the last reply, so with `propose_stages` on the
        live path made N calls and recorded one — and recorded that one with
        `max_tokens: 0`, which a saturation check reads as *unsaturated*."""
        from memcal import llm
        seen = {"n": 0}

        class Staged:
            def complete(self, **kw):
                seen["n"] += 1
                return llm.Reply(text="{}", data={"reviewed": [], "diffs": []},
                                 usage=llm.Usage(calls=1), model=kw.get("model", ""),
                                 generation_id=f"gen-live-{seen['n']}",
                                 finish_reason="stop")

        self.cfg.propose_stages = "on"
        with mock.patch.object(live.llm, "client_for", return_value=Staged()):
            live.remember(self.conn, self.cfg, "poker at Robbie's saturday, I'm going")
        self.assertGreater(seen["n"], 1, "staging is off, so this proved nothing")
        rows = self.conn.execute(
            "SELECT stage, max_tokens FROM generations WHERE stage LIKE 'live%'").fetchall()
        self.assertEqual(len(rows), seen["n"],
                         f"{seen['n']} calls were made and {len(rows)} recorded")
        self.assertTrue(all(r["max_tokens"] > 0 for r in rows),
                        "a ceiling of 0 reads as a call that could not truncate")


class TestARecentReplyLeavesQuestionMeaningToTheModel(Base):
    def setUp(self):
        super().setUp()
        db.set_today(f"{db.today().isoformat()}T19:00")

    def _line(self, external_id, text, *, from_me, minute):
        aid = archive.append(
            self.conn, stream="imessage", external_id=external_id,
            ts=f"{db.today().isoformat()}T20:{minute:02d}:00-04:00", text=text,
            thread="+19175550013", person="Quinn Brooks", from_me=from_me,
            gated=True, gate_reason="temporal")
        archive.spool_add(self.conn, aid, "person:Quinn Brooks")
        self.conn.execute("UPDATE spool SET processed_at = ? WHERE archive_id = ?",
                          (db.now(), aid))
        return self.conn.execute("SELECT * FROM archive WHERE id = ?", (aid,)).fetchone()

    def test_word_overlap_does_not_turn_a_vacation_into_an_answer(self):
        key = todos.ask(
            self.conn, "When are you and Quinn Brooks playing the next League five-man?",
            written_by="dream:nightly")
        self.assertTrue(key)
        trace.stamp(self.conn, kind="question", ref=key, verb="asked",
                    entity="person:Quinn Brooks", stage="propose")
        self._line("league-ask", "5 player league??", from_me=True, minute=4)
        self._line("league-reply", "I am on vacation, wish I could play", from_me=False,
                   minute=5)

        # The exchange was consumed by an older run, so no Connor bundle is pending.
        from memcal.dream import run as dream_run
        result = dream_run.dream(self.conn, self.cfg)

        row = self.conn.execute("SELECT * FROM questions WHERE key = ?", (key,)).fetchone()
        self.assertTrue(result.nothing_new)
        self.assertEqual(result.diffs, 0)
        self.assertEqual(row["status"], "open")
        self.assertIsNone(row["answer"])

    def test_a_typed_amend_defers_and_preserves_the_old_wording(self):
        key = todos.ask(
            self.conn, "When are you and Quinn Brooks playing the next League five-man?",
            written_by="dream:nightly")
        trace.stamp(self.conn, kind="question", ref=key, verb="asked",
                    entity="person:Quinn Brooks", stage="propose")
        sent = self._line("league-amend-ask", "5 player league??", from_me=True, minute=4)
        reply = self._line("league-amend-reply", "I am on vacation through August",
                           from_me=False, minute=5)
        before = self.conn.execute("SELECT * FROM questions WHERE key = ?", (key,)).fetchone()
        bundle = bundle_stage.Bundle(entity="person:Quinn Brooks", items=[sent, reply])
        counts, _log = apply_stage.apply_diffs(
            self.conn, self.cfg, [(bundle, {"questions": [{
                "action": "amend", "key": key,
                "version": before["updated_at"] or before["created_at"],
                "text": "When will you next play League with Quinn Brooks after August?",
                "answer": None, "wake_condition": "Quinn is back from vacation",
                "cite_ids": [sent["id"], reply["id"]],
            }]})], written_by="dream:nightly")
        row = self.conn.execute("SELECT * FROM questions WHERE key = ?", (key,)).fetchone()
        history = self.conn.execute(
            "SELECT field, old_value, new_value FROM question_history"
            " WHERE question_id = ? ORDER BY id", (row["id"],)).fetchall()
        self.assertEqual(counts["question:amended"], 1)
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["wake_condition"], "Quinn is back from vacation")
        self.assertIn(("text", before["text"], row["text"]), [tuple(item) for item in history])

    def test_a_deferred_question_survives_the_ordinary_age_limit(self):
        key = todos.ask(self.conn, "When will you next play League with Quinn Brooks?")
        self.conn.execute(
            "UPDATE questions SET created_at = ?, wake_condition = ? WHERE key = ?",
            ("2026-01-01T00:00:00-05:00", "Quinn is back from vacation", key))
        self.conn.commit()
        self.assertEqual(todos.expire_questions(self.conn), 0)
        self.assertEqual(
            self.conn.execute("SELECT status FROM questions WHERE key = ?", (key,)).fetchone()[0],
            "open")

    def test_automatic_expiry_is_part_of_question_history(self):
        key = todos.ask(self.conn, "When will you next play League with Quinn Brooks?")
        self.conn.execute("UPDATE questions SET created_at = ? WHERE key = ?",
                          ("2026-01-01T00:00:00-05:00", key))
        self.conn.commit()
        self.assertEqual(todos.expire_questions(self.conn), 1)
        history = self.conn.execute(
            "SELECT field, old_value, new_value, written_by FROM question_history"
            " WHERE question_id = (SELECT id FROM questions WHERE key = ?)",
            (key,)).fetchall()
        self.assertIn(("status", "open", "dropped", "code:expire"),
                      [tuple(row) for row in history])

    def test_an_unrelated_recent_exchange_does_not_close_it(self):
        key = todos.ask(
            self.conn, "When are you and Quinn Brooks playing the next League five-man?",
            written_by="dream:nightly")
        trace.stamp(self.conn, kind="question", ref=key, verb="asked",
                    entity="person:Quinn Brooks", stage="propose")
        self._line("dinner-ask", "want dinner tomorrow?", from_me=True, minute=4)
        self._line("dinner-reply", "I am on vacation", from_me=False, minute=5)

        apply_stage.apply_diffs(self.conn, self.cfg, [], written_by="dream:nightly")

        row = self.conn.execute("SELECT status FROM questions WHERE key = ?", (key,)).fetchone()
        self.assertEqual(row["status"], "open")


def setUpModule():
    db.set_today(None)


def tearDownModule():
    db.set_today(None)


if __name__ == "__main__":
    unittest.main()
