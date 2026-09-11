"""The live write path: typed, deterministic, no model.

Every case here is something an agent tried to do in one real conversation on
2026-07-27 and could not, because the only write tool took a sentence and sent it to a
model to be turned back into fields the agent already had. In that one session the
model wrote a free-text note instead of setting a status (twice), filled an unrelated
wiki slot instead of merging two rows, and finally truncated at its token ceiling —
while the user watched, saying "memcal is fucking broken haha".
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import actions, archive, config, db, events, live, llm, todos, wiki  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

        # Passing the scratch home to `config.load` only covers what this file builds
        # itself. Anything constructed *inside* a test calls `config.load()` with no
        # argument and lands on the real `~/.memcal` — which is how a test below
        # rewrote the owner's Montana trip and republished it to Calendar.app.
        # `MEMCAL_HOME` is the only lever that reaches those.
        was = os.environ.get("MEMCAL_HOME")
        os.environ["MEMCAL_HOME"] = self.dir
        self.addCleanup(lambda: os.environ.__setitem__("MEMCAL_HOME", was)
                        if was is not None else os.environ.pop("MEMCAL_HOME", None))

        self.cfg = config.load(self.dir)
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)
        self.addCleanup(self.conn.close)

        # Nothing below may reach the network. That is the property under test as much
        # as any assertion in it.
        def explode(*_a, **_k):
            raise AssertionError("a live write reached for a model")

        original = llm.OpenRouter.complete
        llm.OpenRouter.complete = explode
        self.addCleanup(lambda: setattr(llm.OpenRouter, "complete", original))

    def add(self, title, when="saturday", **fields):
        return live.add_event(self.conn, self.cfg, title=title, when=when, **fields)[0]


class TestAdding(Base):
    def test_a_plan_lands_whole(self):
        event, verb = live.add_event(
            self.conn, self.cfg, title="Beer garden at Bohemian Hall", when="sunday",
            time="after 6", location="Harbor Point, Queens", status="confirmed",
            participants=["Jamie", "Quinn Brooks"])
        self.assertEqual(verb, "inserted")
        line = event.one_line()
        for expected in ("Beer garden at Bohemian Hall", "after 6", "confirmed",
                         "Harbor Point, Queens", "Jamie"):
            self.assertIn(expected, line)

    def test_the_words_the_user_used_resolve_to_a_date(self):
        event = self.add("Dinner", when="tomorrow")
        self.assertEqual(event.date, (db.today() + __import__("datetime").timedelta(days=1))
                         .isoformat())

    def test_a_title_alone_is_refused_rather_than_guessed(self):
        with self.assertRaises(live.LiveError):
            live.add_event(self.conn, self.cfg, title="", when="saturday")


class TestUpdating(Base):
    def test_not_going_sets_the_status_not_a_note(self):
        """The exact failure. "Not going to the Meowser vet visit" came back "written"
        and left the row saying nothing of the kind, twice, because a model decided a
        free-text note was the right home for it."""
        self.add("Meowser vet visit", when="today", status="confirmed")
        event, changed = live.update_event(self.conn, self.cfg, "meowser",
                                           status="declined")
        self.assertEqual(event.status, "declined")
        self.assertIn("not going", event.one_line())
        self.assertTrue(any("status" in c for c in changed))

    def test_a_correction_moves_the_row_and_confirms_it_in_one_call(self):
        self.add("Rowan meetup", when="saturday")
        event, _changed = live.update_event(self.conn, self.cfg, "Rowan meetup",
                                            when="sunday", status="confirmed")
        self.assertEqual(event.date, db.parse_when("sunday")[0].isoformat())
        self.assertEqual(event.status, "confirmed")

    def test_someone_else_is_coming_too(self):
        self.add("Beer garden", participants=["Jamie"])
        event, _changed = live.update_event(self.conn, self.cfg, "beer garden",
                                            add_participants=["Avery Morgan"])
        self.assertEqual(event.participants, ["Avery Morgan", "Jamie"])

    def test_a_participant_can_be_removed(self):
        self.add("Beer garden", participants=["Avery Morgan", "Jamie"])
        event, changed = live.update_event(
            self.conn, self.cfg, "beer garden", remove_participants=["Jamie"])
        self.assertEqual(event.participants, ["Avery Morgan"])
        self.assertTrue(any("participants" in item for item in changed))

    def test_a_no_op_says_so_rather_than_reporting_success(self):
        """An agent told only "written" re-sent the same correction three times, harder
        each time, because nothing in the reply distinguished done from ignored."""
        self.add("Poker", status="confirmed")
        _event, changed = live.update_event(self.conn, self.cfg, "poker",
                                            status="confirmed")
        self.assertEqual(changed, [])

    def test_an_ambiguous_name_asks_instead_of_editing_the_wrong_row(self):
        self.add("Poker at Robbie's", when="saturday")
        self.add("Poker at Jordan's", when="sunday")
        with self.assertRaises(live.LiveError) as caught:
            live.update_event(self.conn, self.cfg, "poker", status="declined")
        self.assertEqual(len(caught.exception.detail["candidates"]), 2)

    def test_naming_a_row_exactly_wins_over_the_ambiguity_guard(self):
        self.add("Poker at Robbie's", when="saturday")
        self.add("Poker at Jordan's", when="sunday")
        event, _changed = live.update_event(self.conn, self.cfg, "Poker at Robbie's",
                                            status="declined")
        self.assertEqual(event.title, "Poker at Robbie's")

    def test_a_row_that_does_not_exist_says_so(self):
        with self.assertRaises(live.LiveError):
            live.update_event(self.conn, self.cfg, "brunch with nobody", status="declined")

    def test_a_bad_status_is_refused_with_the_list(self):
        self.add("Poker")
        with self.assertRaises(live.LiveError) as caught:
            live.update_event(self.conn, self.cfg, "poker", status="not going")
        self.assertIn("declined", str(caught.exception))

    def test_an_update_never_lands_on_a_row_it_was_not_pointed_at(self):
        """`upsert` re-matches on title by default; a status change arriving without a
        key can land on whichever nearby row `find_match` prefers."""
        keep, _v = events.upsert(
            self.conn, {"title": "Poker at Robbie's", "date": db.parse_when("saturday")[0]
                        .isoformat(), "participants": ["Jordan Lee"]},
            written_by="live", match=False)
        other, _v = events.upsert(
            self.conn, {"title": "Poker at Robbie's", "date": db.parse_when("sunday")[0]
                        .isoformat(), "participants": ["Jordan Lee"]},
            written_by="live", match=False)
        self.assertNotEqual(keep.key, other.key)
        live.update_event(self.conn, self.cfg, other.key, status="declined")
        self.assertEqual(events.get(self.conn, keep.key).status, "mentioned")
        self.assertEqual(events.get(self.conn, other.key).status, "declined")


class TestMerging(Base):
    def test_two_rows_become_one_and_pool_what_they_know(self):
        """"Also the Rowan meetup IS the beer garden lol" — said once, tried three ways,
        and the two rows sat there through all of it because nothing could do this."""
        self.add("Rowan meetup", when="sunday", status="confirmed",
                 participants=["Rowan Vale"])
        self.add("Beer garden at Bohemian Hall", when="sunday", time="after 6",
                 location="Harbor Point, Queens", participants=["Jamie", "Quinn Brooks"])
        merged = live.merge_events(self.conn, self.cfg, keep="beer garden",
                                   drop="Rowan meetup")
        self.assertEqual(len(events.between(self.conn, merged.date, merged.date)), 1)
        self.assertIn("Rowan Vale", merged.participants)
        self.assertIn("Jamie", merged.participants)
        self.assertEqual(merged.location, "Harbor Point, Queens")
        self.assertEqual(merged.status, "confirmed")   # the settled one survives

    def test_the_disappearing_row_is_recorded_not_just_deleted(self):
        self.add("Rowan meetup", when="sunday")
        keep = self.add("Beer garden", when="sunday")
        live.merge_events(self.conn, self.cfg, keep="Beer garden", drop="Rowan meetup")
        fields = [h["field"] for h in events.history(self.conn, keep.id)]
        self.assertIn("merged", fields)

    def test_merging_a_row_with_itself_is_refused(self):
        self.add("Beer garden", when="sunday")
        with self.assertRaises(live.LiveError):
            live.merge_events(self.conn, self.cfg, keep="beer garden", drop="beer garden")


class TestDropping(Base):
    def test_a_row_that_was_never_real_goes_away(self):
        self.add("ASPCA Mobile Clinic", when="wednesday", kind="opportunity")
        line = live.drop_event(self.conn, self.cfg, "ASPCA")
        self.assertIn("ASPCA", line)
        self.assertEqual(events.search(self.conn, "ASPCA"), [])

    def test_declining_an_opportunity_leaves_it_an_opportunity(self):
        """§10 case 3, exactly backwards: `upsert` defaulted `kind` to 'commitment' for
        every write, so saying "I don't care about BondVet" promoted a thing the user was
        never doing into a thing the user was."""
        self.add("BondVet dog first aid Zoom", when="thursday", kind="opportunity")
        event, _changed = live.update_event(self.conn, self.cfg, "BondVet",
                                            status="declined")
        self.assertEqual(event.kind, "opportunity")

    def test_an_update_does_not_reassign_someone_elses_row_to_the_user(self):
        """`subject` had the same defaulting bug: any update omitting it said "me"."""
        events.upsert(self.conn, {"title": "Avery visiting NYC", "date": "2026-08-01",
                                  "subject": "Avery Morgan"}, written_by="live")
        event, _changed = live.update_event(self.conn, self.cfg, "Avery visiting",
                                            status="confirmed")
        self.assertEqual(event.subject, "Avery Morgan")

    def test_declining_keeps_the_row_so_it_stops_being_offered(self):
        """The distinction the tools have to make plain: not attending is not the same
        as never happened, and deleting the row invites the next pass to re-add it."""
        self.add("BondVet dog first aid Zoom", when="thursday", kind="opportunity")
        event, _changed = live.update_event(self.conn, self.cfg, "BondVet",
                                            status="declined")
        self.assertIsNotNone(events.get(self.conn, event.key))
        self.assertIn("not going", event.one_line())


class TestAReadHandleCanTargetTheSameRowForWriting(Base):
    """A handle a read returns is the exact row a later write must change.

    Titles are deliberately duplicated: accepting the handle is not a convenience for
    a unique title, it is what prevents a correction from landing on its sibling.
    """

    def test_a_listed_event_handle_updates_its_duplicate_title_only(self):
        from memcal import mcp_server

        first = self.add("Studio appointment", when="2031-03-03")
        # The normal add path deliberately coalesces likely duplicate plans. Build the
        # existing duplicate-title state directly, because that is the dangerous store
        # state a correction must survive.
        second, _ = events.upsert(
            self.conn,
            {"title": "Studio appointment", "date": "2031-03-07", "status": "mentioned"},
            written_by="live", match=False)
        server = mcp_server.Server()
        self.addCleanup(server.conn.close)

        # The brief is an index, but a day lookup is a read too: its returned handle
        # must remain an address rather than becoming an unusable display-only id.
        handle = f"E{second.id}"
        listed = server.call("memcal_list_days", {"when": second.date})
        self.assertIn(handle, listed)
        self.assertIn("Studio appointment", server.call("memcal_open", {"ref": handle}))

        out = server.call("memcal_update", {"which": handle, "status": "declined"})
        self.assertIn("not going", out)
        self.assertEqual(events.get(self.conn, first.key).status, "mentioned")
        self.assertEqual(events.get(self.conn, second.key).status, "declined")

        with self.assertRaises(live.LiveError) as caught:
            live.update_event(self.conn, self.cfg, "Studio appointment", status="confirmed")
        self.assertIn(handle, "\n".join(caught.exception.detail["candidates"]))


class TestTodosAndPages(Base):
    def test_a_todo_opens_with_a_due_date_in_the_users_words(self):
        todo, verb = live.open_todo(self.conn, self.cfg, "Pay Parker for the camping pass",
                                    due="friday")
        self.assertEqual(verb, "opened")
        self.assertEqual(todo.due, db.parse_when("friday")[0].isoformat())
        self.assertIn(todo.key, [t.key for t in todos.open_items(self.conn)])

    def test_an_alias_lands_on_the_page_that_exists(self):
        wiki.set_slot(self.cfg.wiki_dir, "robbie", "hosts", "poker games")
        page = wiki.add_alias(self.cfg.wiki_dir, "robbie", "Robin West")
        self.assertEqual(page.aliases, ["Robin West"])
        self.assertEqual(wiki.canonical(self.cfg.wiki_dir, "Robin West"), "robbie")


class TestTheBriefStaysCurrent(Base):
    def test_every_write_re_renders_the_brief(self):
        """The brief is the whole interface. A write that does not reach it is a write
        the next turn cannot see, which is exactly what "feels broken" means."""
        self.add("Beer garden at Bohemian Hall", when="sunday")
        self.assertIn("Beer garden at Bohemian Hall",
                      (Path(self.dir) / "brief.md").read_text())
        live.update_event(self.conn, self.cfg, "beer garden", status="declined")
        self.assertIn("not going", (Path(self.dir) / "brief.md").read_text())
        live.drop_event(self.conn, self.cfg, "beer garden")
        self.assertNotIn("Beer garden at Bohemian Hall",
                         (Path(self.dir) / "brief.md").read_text())


class TestAnEndDateIsCorrectableAndNotOnlySettable(Base):
    """Allow an explicit end-date correction to replace an existing span."""

    def _trip(self):
        return live.add_event(self.conn, self.cfg, title="Montana trip",
                              when="2026-08-15", until="2026-08-23",
                              status="confirmed")[0]

    def test_the_end_of_a_span_moves(self):
        self._trip()
        event, changed = live.update_event(self.conn, self.cfg, "Montana",
                                           when="2026-08-15", until="2026-08-23")
        self.assertEqual((event.date, event.until), ("2026-08-15", "2026-08-23"))
        # A correction that lands silently is one the agent re-sends, harder each time.
        live.update_event(self.conn, self.cfg, "Montana", until="2026-08-16")
        event, changed = live.update_event(self.conn, self.cfg, "Montana",
                                           until="2026-08-23")
        self.assertEqual(event.until, "2026-08-23")
        self.assertIn("until: 2026-08-16 → 2026-08-23", changed)

    def test_the_row_stays_on_the_brief_for_every_day_of_it(self):
        """The failure that made it cost something: `window` keys on `until`, so a trip
        cut short vanishes from the brief while the user is still on it."""
        self._trip()
        live.update_event(self.conn, self.cfg, "Montana", until="2026-08-16")
        self.assertEqual(
            [e.key for e in events.window(self.conn, 0, 1, ref=db.parse_date("2026-08-20"))],
            [])
        live.update_event(self.conn, self.cfg, "Montana", until="2026-08-23")
        self.assertEqual(
            [e.title for e in events.window(self.conn, 0, 1, ref=db.parse_date("2026-08-20"))],
            ["Montana trip"])

    def test_the_tool_that_takes_corrections_can_express_one(self):
        """`memcal_add` accepting a field `memcal_update` refuses is the whole bug, and
        it is invisible from inside either schema. Compare them."""
        from memcal import mcp_server
        schemas = {tool["name"]: set(tool["inputSchema"]["properties"])
                   for tool in mcp_server.TOOLS}
        settable = schemas["memcal_add"] - {"title", "when", "participants"}
        correctable = schemas["memcal_update"] | {"which", "when"}
        self.assertEqual(settable - correctable, set(),
                         "memcal_add can set a field memcal_update cannot correct")

    def test_the_handler_passes_what_the_schema_promises(self):
        """A field in the schema and not in the call is worse than no field at all: the
        agent is told it worked and the store never hears about it."""
        from memcal import mcp_server
        server = mcp_server.Server()
        self.addCleanup(server.conn.close)
        self.assertEqual(server.cfg.home, Path(self.dir))   # never the real store
        server.call("memcal_add", {"title": "Montana trip", "when": "2026-08-15",
                                   "until": "2026-08-16", "status": "confirmed"})
        out = server.call("memcal_update", {"which": "Montana", "until": "2026-08-23"})
        self.assertIn("until Sun Aug 23", out)


class TestNoTestEverWritesToTheRealStore(Base):

    def test_a_server_built_inside_a_test_lands_on_the_scratch_home(self):
        from memcal import mcp_server
        server = mcp_server.Server()
        self.addCleanup(server.conn.close)
        self.assertEqual(server.cfg.home, Path(self.dir))
        self.assertEqual(server.cfg.publish_calendar, "")

    def test_the_default_home_is_never_what_a_test_gets(self):
        self.assertNotEqual(Path(self.dir), Path(config.DEFAULT_HOME).expanduser())
        self.assertEqual(config.load().home, Path(self.dir))


class TestACompletedToolCallLeavesARecordTheNightlyPassCanRead(Base):
    """The change and the record of it commit together, or neither happens.

    The nightly pass meeting "move poker to Saturday" has to be able to tell an
    instruction already carried out from one still outstanding. Everything it needs to
    do that is written here, at the moment the tool runs.
    """

    def _turn(self, text: str) -> int:
        return archive.append(
            self.conn, stream="agent", external_id=f"turn:{db.slugify(text, 24)}",
            ts=db.now(), text=text, thread="hermes:s1", person="me", from_me=True,
            addressed_to="machine", gated=True, gate_reason="directive")

    def test_an_update_records_the_target_the_change_and_the_turn(self):
        turn = self._turn("move poker to saturday")
        event = self.add("Poker", when="2026-09-18")
        live.update_event(self.conn, self.cfg, "Poker", when="2026-09-19",
                          origin=live.Origin.of("hermes", [turn], session="s1"))
        record = actions.latest_for(self.conn, "event", event.key)
        self.assertIsNotNone(record)
        self.assertEqual(record.verb, "updated")
        self.assertEqual(record.ref, event.key)
        self.assertEqual(record.source_ids, [turn])
        self.assertEqual(record.fields.get("date"), ["2026-09-18", "2026-09-19"])
        self.assertTrue(record.based_on)

    def test_a_caller_with_no_turn_context_still_works_and_says_so(self):
        event = self.add("Dentist", when="2026-09-18")
        record = actions.latest_for(self.conn, "event", event.key)
        self.assertEqual(record.source_ids, [])
        self.assertEqual(record.source_note, actions.NO_SOURCE_NOTE)

    def test_the_same_operation_replayed_from_one_turn_is_recorded_once(self):
        turn = self._turn("cancel the show")
        self.add("Show", when="2026-09-18", status="confirmed")
        origin = live.Origin.of("hermes", [turn], session="s1")
        live.update_event(self.conn, self.cfg, "Show", status="declined", origin=origin)
        try:
            live.update_event(self.conn, self.cfg, "Show", status="declined",
                              origin=origin)
        except live.LiveError:
            pass                       # "nothing to change" is the ordinary second call
        wrote = self.conn.execute(
            "SELECT count(*) AS n FROM actions WHERE verb = 'updated'").fetchone()["n"]
        self.assertEqual(wrote, 1)

    def test_the_turn_becomes_the_rows_evidence(self):
        turn = self._turn("dinner with alex on the 18th at 7")
        live.add_event(self.conn, self.cfg, title="Dinner with Alex", when="2026-09-18",
                       origin=live.Origin.of("hermes", [turn], session="s1"))
        cited = [row["archive_id"] for row in self.conn.execute(
            "SELECT archive_id FROM evidence WHERE kind = 'event'")]
        self.assertEqual(cited, [turn])


class TestOlderEvidenceCannotUndoATypedCorrection(Base):
    """The row was corrected at 09:01; a straggler collected at 20:00 is not news.

    `written_today` used to be the escape hatch, and it answered the wrong question:
    the row *had* been written today, by the user, which is the reason to refuse the
    write rather than the reason to allow it.
    """

    def test_a_pass_reading_older_traffic_leaves_the_correction_alone(self):
        db.set_today("2026-09-08T09:01:00")
        self.addCleanup(db.set_today, None)
        self.add("Brunch", when="2026-09-19", location="Rosewood")
        live.update_event(self.conn, self.cfg, "Brunch", when="2026-09-20",
                          location="Blue Fern")
        db.set_today("2026-09-08T23:30:00")
        event, outcome = events.upsert(
            self.conn, {"title": "Brunch", "date": "2026-09-19",
                        "location": "Rosewood"},
            written_by="dream:nightly", evidence_ts="2026-09-07T18:30:00")
        self.assertEqual(outcome, "unchanged")
        self.assertEqual(event.date, "2026-09-20")
        self.assertEqual(event.location, "Blue Fern")

    def test_genuinely_newer_evidence_still_lands(self):
        db.set_today("2026-09-07T20:22:00")
        self.addCleanup(db.set_today, None)
        self.add("Movie with Riley", when="2026-09-22", status="confirmed")
        db.set_today("2026-09-08T23:30:00")
        event, outcome = events.upsert(
            self.conn, {"title": "Movie with Riley", "date": "2026-09-22",
                        "status": "declined"},
            written_by="dream:nightly", evidence_ts="2026-09-08T17:35:00")
        self.assertEqual(outcome, "updated")
        self.assertEqual(event.status, "declined")

    def test_the_guard_is_per_field_not_per_row(self):
        db.set_today("2026-09-08T09:01:00")
        self.addCleanup(db.set_today, None)
        self.add("Ramen dinner", when="2026-09-24", time="19:00")
        live.update_event(self.conn, self.cfg, "Ramen dinner", time="20:30")
        db.set_today("2026-09-08T23:30:00")
        # Newer than the time decision, so the note lands; the time it also carries is
        # the same one already stored, so nothing walks backwards either way.
        event, outcome = events.upsert(
            self.conn, {"title": "Ramen dinner", "date": "2026-09-24",
                        "note": "table for four"},
            written_by="dream:nightly", evidence_ts="2026-09-08T15:00:00")
        self.assertEqual(outcome, "updated")
        self.assertEqual(event.note, "table for four")
        self.assertEqual(event.time, "20:30")


class TestEvidenceIsWeighedOnTheClockItWasSaidOn(Base):
    """Source time and processing time are two clocks, and mixing them inverts the guard.

    A nightly pass reads the day's traffic at 23:30. Stamping the *write* time onto the
    field it decided meant the next pass compared a message said at noon against 23:30
    and refused it as stale — so the newer of two source messages lost to the older one
    purely because of when a batch job happened to run.
    """

    def test_a_later_message_still_wins_over_an_earlier_one_applied_at_night(self):
        db.set_today("2026-09-08T09:00:00")
        self.addCleanup(db.set_today, None)
        self.add("Ramen dinner", when="2026-09-24", time="19:00")
        # The 10am message, applied by the pass that runs at 23:30.
        db.set_today("2026-09-08T23:30:00")
        events.upsert(self.conn, {"title": "Ramen dinner", "date": "2026-09-24",
                                  "time": "20:00"},
                      written_by="dream:nightly", evidence_ts="2026-09-08T10:00:00")
        # The noon message, read the following night. Newer than the 10am one it revises,
        # and older than the moment that one was written to the store.
        db.set_today("2026-09-09T23:30:00")
        event, outcome = events.upsert(
            self.conn, {"title": "Ramen dinner", "date": "2026-09-24",
                        "time": "20:30"},
            written_by="dream:nightly", evidence_ts="2026-09-08T12:00:00")
        self.assertEqual(outcome, "updated")
        self.assertEqual(event.time, "20:30")

    def test_the_stored_field_carries_the_moment_it_was_said(self):
        db.set_today("2026-09-08T08:00:00")
        self.addCleanup(db.set_today, None)
        events.upsert(self.conn, {"title": "Ramen dinner", "date": "2026-09-24",
                                  "time": "19:00"}, written_by="live")
        db.set_today("2026-09-08T23:30:00")
        events.upsert(self.conn, {"title": "Ramen dinner", "date": "2026-09-24",
                                  "time": "20:30"},
                      written_by="dream:nightly", evidence_ts="2026-09-08T10:00:00")
        row = self.conn.execute(
            "SELECT changed_at, evidence_ts FROM event_history WHERE field = 'time'"
            " ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["evidence_ts"], "2026-09-08T10:00:00")
        self.assertNotEqual(row["evidence_ts"], row["changed_at"])

    def test_a_row_the_pass_built_at_night_is_dated_by_what_it_read(self):
        """The floor for a field nobody has revised is the creating write's evidence.

        `created_at` is a processing time. Using it put the two clocks back on opposite
        sides of the comparison: a nightly pass filing a plan at 23:30 made every message
        said earlier that day permanently unable to correct any field of it.
        """
        db.set_today("2026-09-08T23:30:00")
        self.addCleanup(db.set_today, None)
        events.upsert(self.conn, {"title": "Ramen dinner", "date": "2026-09-24",
                                  "location": "Blue Fern"},
                      written_by="dream:nightly", evidence_ts="2026-09-08T10:00:00")
        # The noon message, which moved it. Newer than the 10am line the row was built
        # from, and older than the moment the batch job wrote that line down.
        db.set_today("2026-09-09T08:00:00")
        event, outcome = events.upsert(
            self.conn, {"title": "Ramen dinner", "date": "2026-09-24",
                        "location": "Rosewood"},
            written_by="dream:realtime", evidence_ts="2026-09-08T12:00:00")
        self.assertEqual(outcome, "updated")
        self.assertEqual(event.location, "Rosewood")

    def test_evidence_older_than_what_the_row_was_built_from_is_refused(self):
        """The same comparison, the other way. Raising the floor is not removing it."""
        db.set_today("2026-09-08T23:30:00")
        self.addCleanup(db.set_today, None)
        events.upsert(self.conn, {"title": "Ramen dinner", "date": "2026-09-24",
                                  "location": "Blue Fern"},
                      written_by="dream:nightly", evidence_ts="2026-09-08T10:00:00")
        db.set_today("2026-09-09T08:00:00")
        event, outcome = events.upsert(
            self.conn, {"title": "Ramen dinner", "date": "2026-09-24",
                        "location": "Rosewood"},
            written_by="dream:realtime", evidence_ts="2026-09-08T08:00:00")
        self.assertEqual(outcome, "unchanged")
        self.assertEqual(event.location, "Blue Fern")

    def test_newer_evidence_about_one_field_cannot_re_decide_another(self):
        db.set_today("2026-09-08T09:00:00")
        self.addCleanup(db.set_today, None)
        self.add("Brunch", when="2026-09-20", location="Blue Fern")
        db.set_today("2026-09-08T23:30:00")
        # Fresh news about the time; nothing new about the place, and the place was
        # corrected by them this morning.
        event, _outcome = events.upsert(
            self.conn, {"title": "Brunch", "date": "2026-09-20", "time": "11:30",
                        "location": "Rosewood"},
            written_by="dream:nightly",
            evidence_ts={"time": "2026-09-08T20:00:00",
                         "location": "2026-09-07T18:30:00"})
        self.assertEqual(event.time, "11:30")
        self.assertEqual(event.location, "Blue Fern")


class TestReplayingAnOperationCannotUndoALaterOne(Base):
    """A → B → retry A. The retry must be a no-op, not a restoration of A.

    The operation record used to be written after the mutation, hashed over the row's
    before-values. By the time A is replayed its "before" is B's result, so the replay
    hashed differently, was not recognised as a repeat, ran, and put the row back where
    A had left it — silently discarding a newer decision.
    """

    def _turn(self, text: str) -> int:
        return archive.append(
            self.conn, stream="agent", external_id=f"turn:{db.slugify(text, 24)}",
            ts=db.now(), text=text, thread="hermes:s1", person="me", from_me=True,
            addressed_to="machine", gated=True, gate_reason="directive")

    def test_a_replayed_move_does_not_restore_the_date_a_later_move_replaced(self):
        turn_a = self._turn("move poker to the 19th")
        turn_b = self._turn("actually the 20th")
        self.add("Poker", when="2026-09-18")
        move_a = live.Origin.of("hermes", [turn_a], session="s1")
        move_b = live.Origin.of("hermes", [turn_b], session="s1")

        live.update_event(self.conn, self.cfg, "Poker", when="2026-09-19",
                          origin=move_a)
        live.update_event(self.conn, self.cfg, "Poker", when="2026-09-20",
                          origin=move_b)
        event, changed = live.update_event(self.conn, self.cfg, "Poker",
                                           when="2026-09-19", origin=move_a)
        self.assertEqual(event.date, "2026-09-20")
        self.assertEqual(changed, [])
        moves = self.conn.execute(
            "SELECT count(*) AS n FROM actions WHERE verb = 'updated'").fetchone()["n"]
        self.assertEqual(moves, 2)

    def test_a_callers_own_key_makes_the_retry_a_no_op(self):
        self.add("Poker", when="2026-09-18")
        origin = live.Origin.of("mcp", op_id="client-op-1")
        live.update_event(self.conn, self.cfg, "Poker", when="2026-09-19",
                          origin=origin)
        live.update_event(self.conn, self.cfg, "Poker", when="2026-09-20",
                          origin=live.Origin.of("mcp", op_id="client-op-2"))
        event, changed = live.update_event(self.conn, self.cfg, "Poker",
                                           when="2026-09-19", origin=origin)
        self.assertEqual(event.date, "2026-09-20")
        self.assertEqual(changed, [])

    def test_a_genuinely_new_instruction_from_a_later_turn_still_applies(self):
        """The counterweight: deduplication must not swallow a real second decision."""
        self.add("Poker", when="2026-09-18")
        live.update_event(self.conn, self.cfg, "Poker", when="2026-09-19",
                          origin=live.Origin.of("hermes", [self._turn("to the 19th")],
                                                session="s1"))
        event, changed = live.update_event(
            self.conn, self.cfg, "Poker", when="2026-09-18",
            origin=live.Origin.of("hermes", [self._turn("back to the 18th")],
                                  session="s1"))
        self.assertEqual(event.date, "2026-09-18")
        self.assertTrue(changed)

    def test_a_merge_records_its_operation_in_the_same_transaction(self):
        """The one write path that committed the change and the record separately.

        A crash between the two left a merged row with nothing saying it had been merged
        — which is exactly the state the nightly pass reads as untouched and writes over.
        """
        turn = self._turn("those are the same dinner")
        origin = live.Origin.of("hermes", [turn], session="s1")
        self.add("Ramen dinner", when="2026-09-24")
        self.add("Ramen with Riley", when="2026-09-24")
        real = actions.record
        actions.record = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("crash"))
        try:
            with self.assertRaises(RuntimeError):
                live.merge_events(self.conn, self.cfg, "Ramen dinner",
                                  "Ramen with Riley", origin=origin)
        finally:
            actions.record = real
        # Neither half landed: the second row is still there to merge again.
        rows = self.conn.execute("SELECT count(*) AS n FROM events").fetchone()["n"]
        self.assertEqual(rows, 2)

    def test_a_merge_leaves_a_record_carrying_the_turn_that_asked_for_it(self):
        turn = self._turn("those are the same dinner")
        self.add("Ramen dinner", when="2026-09-24")
        self.add("Ramen with Riley", when="2026-09-24")
        live.merge_events(self.conn, self.cfg, "Ramen dinner", "Ramen with Riley",
                          origin=live.Origin.of("hermes", [turn], session="s1"))
        rows = self.conn.execute(
            "SELECT source_ids FROM actions WHERE verb = 'merged'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(db.jload(rows[0]["source_ids"], []), [turn])

    def test_a_replayed_close_does_not_reopen_or_double_record(self):
        turn = self._turn("i venmo'd them")
        origin = live.Origin.of("hermes", [turn], session="s1")
        live.open_todo(self.conn, self.cfg, "Venmo Cameron for the ticket")
        live.close_todo(self.conn, self.cfg, "Venmo Cameron", origin=origin)
        # The second call never finds an open to-do, which is the informative refusal
        # rather than a silent success. What matters is what it leaves behind.
        with self.assertRaises(live.LiveError):
            live.close_todo(self.conn, self.cfg, "Venmo Cameron", origin=origin)
        closed = self.conn.execute(
            "SELECT count(*) AS n FROM actions WHERE verb = 'closed'").fetchone()["n"]
        self.assertEqual(closed, 1)
        self.assertEqual(
            todos.get(self.conn, "todo:venmo-cameron-for-the-ticket").status, "closed")


if __name__ == "__main__":
    unittest.main()
