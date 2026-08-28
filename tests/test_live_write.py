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

from memcal import config, db, events, live, llm, todos, wiki  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
