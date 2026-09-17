"""The self-page contract: one page for the user, reached by `me` or their name.

Every acceptance check the wiki-memory work signed up for, exercised through the
real code paths rather than a model:

  - the user's own facts ride the brief's "About you" line and survive trimming;
  - the self page is created by the first fact and never by a read, and an
    established page under the user's name is reused instead of a second one;
  - a namesake sharing a first name is never mistaken for the user;
  - `me`, an established self name, and a recorded alias all land on one page;
  - a direct correction beats a stale dream fact, and replaying the stale fact
    does not undo the correction;
  - the wiki search reads labels and values and carries their provenance;
  - the missing / ambiguous / overflow surfaces read the way the tools promise;
  - non-self pages behave exactly as they did before any of this landed.

The self-address example ("14 Example Lane") is walked end to end through both
the direct-note write path and the ingested/dream apply path.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import archive, brief, config, db, identity, live, wiki  # noqa: E402
from memcal.dream import apply as apply_stage  # noqa: E402
from memcal.dream.bundle import Bundle  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        # Anything built inside a test — an MCP `Server`, a fresh `config.load()` —
        # must land on this scratch home rather than the real ~/.memcal.
        was = os.environ.get("MEMCAL_HOME")
        os.environ["MEMCAL_HOME"] = self.dir
        self.addCleanup(lambda: os.environ.__setitem__("MEMCAL_HOME", was)
                        if was is not None else os.environ.pop("MEMCAL_HOME", None))
        self.cfg = config.load(self.dir)
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)
        self.addCleanup(self.conn.close)

    def tearDown(self):
        db.set_today(None)

    # -- helpers ------------------------------------------------------------

    def pages(self) -> list[str]:
        return wiki.list_pages(self.cfg.wiki_dir)

    def slots(self, slug: str) -> dict:
        page = wiki.read(self.cfg.wiki_dir, slug)
        return {name: info.get("value") for name, info in page.slots.items()} if page else {}

    def ingest_dream_fact(self, *, page: str, slot: str, value: str, ts: str,
                          text: str, entity: str = "person:me", eid: str | None = None):
        """Apply one wiki fact the way a nightly dream pass would, from a dated line.

        The line's timestamp is the evidence date, so a later apply of an older
        line is genuinely older evidence.
        """
        eid = eid or f"ln-{ts}-{slot}-{value}".replace(" ", "-")
        archive.append(self.conn, channel="imessage", external_id=eid, ts=ts,
                       text=text, thread="self", person="me", from_me=True)
        self.conn.commit()
        bundle = Bundle(entity=entity, title="me", items=list(self.conn.execute(
            "SELECT * FROM archive WHERE external_id = ?", (eid,))))
        diff = {"events": [], "todos": [], "questions": [],
                "wiki": [{"page": page, "slot": slot, "value": value}]}
        return apply_stage.apply_diffs(self.conn, self.cfg, [(bundle, diff, "gen-1")],
                                       written_by="dream:test")

    def dream_wiki(self, rows: list[dict], *, entity="person:me", items=None):
        """Apply an arbitrary wiki diff with no dated evidence behind it."""
        bundle = Bundle(entity=entity, title="me", items=items or [])
        diff = {"events": [], "todos": [], "questions": [], "wiki": rows}
        return apply_stage.apply_diffs(self.conn, self.cfg, [(bundle, diff, "gen-1")],
                                       written_by="dream:test")


# ---------------------------------------------------------------- brief ----

class TestTheUsersOwnFactsRideTheBrief(Base):
    """The self page projects onto the brief's About-you line, verbatim and whole."""

    def test_the_self_address_appears_as_a_whole_fact(self):
        ok, _ = live.note(self.conn, self.cfg, "me", "home", "14 Example Lane")
        self.assertTrue(ok)
        text = brief.render(self.conn, self.cfg)
        self.assertIn(brief.ABOUT_YOU_PREFIX, text)
        self.assertIn("home: 14 Example Lane", text)

    def test_two_self_facts_stay_distinct_and_verbatim(self):
        live.note(self.conn, self.cfg, "me", "home", "14 Example Lane")
        live.note(self.conn, self.cfg, "me", "work", "9 Industry Road")
        text = brief.render(self.conn, self.cfg)
        # Stored text, not a summary: home and work do not collapse into one place.
        self.assertIn("home: 14 Example Lane", text)
        self.assertIn("work: 9 Industry Road", text)

    def test_no_self_page_means_no_about_you_line(self):
        # A wiki with only other people's pages must not manufacture a self line.
        wiki.set_slot(self.cfg.wiki_dir, "quinn-brooks", "likes", "Pokemon",
                      source="test", conn=self.conn)
        text = brief.render(self.conn, self.cfg)
        self.assertNotIn("About you", text)

    def test_trimming_collapses_the_facts_to_their_pointer_not_a_cut(self):
        # Values long enough that collapsing them to the pointer actually saves room.
        for i in range(5):
            wiki.set_slot(self.cfg.wiki_dir, "me", f"detail{i}",
                          f"HEAD{i} a long remembered detail value TAIL{i}",
                          source="test", conn=self.conn)
        self.cfg.brief_token_cap = 190
        text = brief.render(self.conn, self.cfg)
        # The whole-value line no longer fits, so it collapses to the pointer — the
        # facts stay one tool call away rather than being sliced.
        self.assertIn(brief.ABOUT_YOU_TRIMMED, text)
        self.assertIn("memcal_open me", text)

    def test_an_address_is_never_bisected_at_any_cap(self):
        for i in range(5):
            wiki.set_slot(self.cfg.wiki_dir, "me", f"detail{i}",
                          f"HEAD{i} a long remembered detail value TAIL{i}",
                          source="test", conn=self.conn)
        for cap in range(50, 260, 10):
            self.cfg.brief_token_cap = cap
            text = brief.render(self.conn, self.cfg)
            for i in range(5):
                # A cut inside a value would keep the head and drop the tail.
                # Collapsing whole About-you values to the pointer before any
                # hard cut, plus a line-boundary hard cut, means a value is
                # present whole or not at all.
                if f"HEAD{i}" in text:
                    self.assertIn(f"TAIL{i}", text,
                                  f"value {i} was bisected at cap {cap}")

    def test_many_self_facts_overflow_with_a_count_not_a_cut(self):
        for i in range(40):
            wiki.set_slot(self.cfg.wiki_dir, "me", f"fact{i:02d}",
                          f"value number {i:02d} here", source="test", conn=self.conn)
        line = brief._about_you_line(self.conn, self.cfg)
        self.assertTrue(line.startswith(brief.ABOUT_YOU_PREFIX))
        self.assertLessEqual(len(line), brief.ABOUT_YOU_MAX_CHARS)
        self.assertRegex(line, r"\+\d+ more")


# ------------------------------------------------------ creation rules ----

class TestTheSelfPageIsCreatedOnlyByAWrite(Base):
    def test_resolution_and_reads_never_create_a_page(self):
        self.assertEqual(wiki.self_slug(self.conn, self.cfg.wiki_dir), "me")
        self.assertEqual(wiki.resolve_self_page(self.conn, self.cfg.wiki_dir, "me"), "me")
        self.assertIsNone(wiki.read(self.cfg.wiki_dir, "me"))
        brief.render(self.conn, self.cfg)
        wiki.profile(self.conn, self.cfg.wiki_dir, "me")
        self.assertEqual(self.pages(), [])

    def test_the_first_note_creates_the_self_page(self):
        self.assertEqual(self.pages(), [])
        ok, _ = live.note(self.conn, self.cfg, "me", "home", "14 Example Lane")
        self.assertTrue(ok)
        self.assertEqual(self.pages(), ["me"])
        self.assertEqual(self.slots("me"), {"home": "14 Example Lane"})

    def test_the_first_dream_fact_creates_the_self_page(self):
        self.assertEqual(self.pages(), [])
        self.ingest_dream_fact(page="me", slot="home", value="14 Example Lane",
                               ts="2026-08-01T09:00:00-04:00",
                               text="just moved into 14 Example Lane, finally")
        self.assertEqual(self.pages(), ["me"])
        self.assertEqual(self.slots("me"), {"home": "14 Example Lane"})

    def test_a_dream_question_alone_does_not_open_the_self_page(self):
        _counts, log = self.dream_wiki([
            {"page": "me", "question": "Where does the user actually live now?"}])
        self.assertEqual(self.pages(), [])
        self.assertTrue(any("rejected-empty-self" in line for line in log), log)

    def test_an_established_page_under_the_users_name_is_reused(self):
        identity.set_me(self.conn, "Casey Morgan")
        wiki.set_slot(self.cfg.wiki_dir, "casey-morgan", "dog", "Comet",
                      source="test", conn=self.conn)
        self.assertEqual(self.pages(), ["casey-morgan"])
        ok, _ = live.note(self.conn, self.cfg, "me", "home", "14 Example Lane")
        self.assertTrue(ok)
        # No second `me` page: the write folded onto the established one.
        self.assertEqual(self.pages(), ["casey-morgan"])
        self.assertEqual(self.slots("casey-morgan"),
                         {"dog": "Comet", "home": "14 Example Lane"})


# ---------------------------------------------------- identity matching ----

class TestWhoCountsAsTheUser(Base):
    def setUp(self):
        super().setUp()
        identity.set_me(self.conn, "Casey Morgan")
        # An established self page, so `self_slug` has a single candidate to resolve to.
        wiki.set_slot(self.cfg.wiki_dir, "casey-morgan", "dog", "Comet",
                      source="test", conn=self.conn)

    def test_me_a_full_name_and_a_recorded_alias_are_one_page(self):
        wiki.add_alias(self.cfg.wiki_dir, "casey-morgan", "CJ")
        live.note(self.conn, self.cfg, "me", "home", "14 Example Lane")
        live.note(self.conn, self.cfg, "Casey Morgan", "work", "9 Industry Road")
        live.note(self.conn, self.cfg, "CJ", "bike", "orange")
        self.assertEqual(self.pages(), ["casey-morgan"])
        self.assertEqual(self.slots("casey-morgan"),
                         {"dog": "Comet", "home": "14 Example Lane",
                          "work": "9 Industry Road", "bike": "orange"})

    def test_a_namesake_sharing_a_first_name_is_not_the_user(self):
        self.assertIsNone(
            wiki.resolve_self_page(self.conn, self.cfg.wiki_dir, "Casey Jones"))
        ok, _ = live.note(self.conn, self.cfg, "Casey Jones", "role", "engineer")
        self.assertTrue(ok)
        # The namesake got their own page; the self page is untouched.
        self.assertIn("casey-jones", self.pages())
        self.assertEqual(self.slots("casey-jones"), {"role": "engineer"})
        self.assertEqual(self.slots("casey-morgan"), {"dog": "Comet"})
        self.assertEqual(wiki.self_slug(self.conn, self.cfg.wiki_dir), "casey-morgan")

    def test_a_dream_fact_for_a_namesake_stays_off_the_self_page(self):
        self.ingest_dream_fact(page="Casey Jones", slot="role", value="engineer",
                               entity="person:Casey Jones",
                               ts="2026-08-01T09:00:00-04:00",
                               text="Casey Jones just started as an engineer")
        self.assertEqual(self.slots("casey-morgan"), {"dog": "Comet"})
        self.assertEqual(self.slots("casey-jones"), {"role": "engineer"})


# --------------------------------------------- correction beats stale ----

class TestACorrectionBeatsAStaleDreamFact(Base):
    def test_older_evidence_never_revises_a_newer_fact_even_on_replay(self):
        # Day 1: the dream learns Eastwood. Day 5: it learns the corrected Riverton.
        self.ingest_dream_fact(page="me", slot="home", value="Eastwood",
                               ts="2026-08-01T09:00:00-04:00",
                               text="the user is in Eastwood now", eid="d1")
        self.ingest_dream_fact(page="me", slot="home", value="Riverton",
                               ts="2026-08-05T09:00:00-04:00",
                               text="correction: the user is actually in Riverton", eid="d5")
        self.assertEqual(self.slots("me")["home"], "Riverton")

        # A stale replay of the day-1 line must not walk the fact back to Eastwood.
        _counts, log = self.ingest_dream_fact(
            page="me", slot="home", value="Eastwood",
            ts="2026-08-01T09:00:00-04:00",
            text="the user is in Eastwood now", eid="d1")
        self.assertEqual(self.slots("me")["home"], "Riverton")
        self.assertTrue(any("rejected-stale" in line for line in log), log)

        # Replaying it a second time is just as inert.
        self.ingest_dream_fact(page="me", slot="home", value="Eastwood",
                               ts="2026-08-01T09:00:00-04:00",
                               text="the user is in Eastwood now", eid="d1")
        self.assertEqual(self.slots("me")["home"], "Riverton")

    def test_a_direct_correction_outlives_a_replayed_dream_fact(self):
        self.ingest_dream_fact(page="me", slot="home", value="Eastwood",
                               ts="2026-08-01T09:00:00-04:00",
                               text="the user is in Eastwood now", eid="d1")
        # The user corrects it by hand; a direct write is authoritative.
        ok, _ = live.note(self.conn, self.cfg, "me", "home", "Riverton")
        self.assertTrue(ok)
        self.assertEqual(self.slots("me")["home"], "Riverton")

        _counts, log = self.ingest_dream_fact(
            page="me", slot="home", value="Eastwood",
            ts="2026-08-01T09:00:00-04:00",
            text="the user is in Eastwood now", eid="d1")
        self.assertEqual(self.slots("me")["home"], "Riverton")
        self.assertTrue(any("rejected-stale" in line for line in log), log)

    def test_the_slot_history_records_the_correction(self):
        self.ingest_dream_fact(page="me", slot="home", value="Eastwood",
                               ts="2026-08-01T09:00:00-04:00",
                               text="the user is in Eastwood now", eid="d1")
        self.ingest_dream_fact(page="me", slot="home", value="Riverton",
                               ts="2026-08-05T09:00:00-04:00",
                               text="actually Riverton", eid="d5")
        history = wiki.slot_history(self.conn, "me", "home")
        values = [row["new_value"] for row in history]
        self.assertEqual(values, ["Eastwood", "Riverton"])


# ------------------------------------------------------ ambiguity ----

class TestTwoSelfPagesBlockEverySelfWrite(Base):
    """When `me` and an established name resolve to two different pages nothing
    guesses a winner: reads name the candidates, writes refuse."""

    def setUp(self):
        super().setUp()
        identity.set_me(self.conn, "Casey Morgan")
        # A literal `me` page and a page under the user's name, both real on disk —
        # the shape a hand edit or a legacy import can leave behind.
        wiki.set_slot(self.cfg.wiki_dir, "me", "home", "14 Example Lane",
                      source="test", conn=self.conn)
        wiki.set_slot(self.cfg.wiki_dir, "casey-morgan", "dog", "Comet",
                      source="test", conn=self.conn)

    def test_self_slug_raises_with_both_candidates(self):
        with self.assertRaises(wiki.SelfAmbiguous) as caught:
            wiki.self_slug(self.conn, self.cfg.wiki_dir)
        self.assertEqual(caught.exception.candidates, ["casey-morgan", "me"])

    def test_the_brief_names_the_candidates_and_projects_nothing(self):
        line = brief._about_you_line(self.conn, self.cfg)
        self.assertIn("ambiguous self page", line)
        self.assertIn("casey-morgan", line)
        self.assertIn("me", line)
        # No fact value leaks out beside an unresolved identity.
        self.assertNotIn("14 Example Lane", line)

    def test_a_direct_note_refuses_and_writes_nothing(self):
        ok, message = live.note(self.conn, self.cfg, "me", "phone", "555-0100")
        self.assertFalse(ok)
        self.assertIn("ambiguous self page", message)
        self.assertNotIn("phone", self.slots("me"))
        self.assertNotIn("phone", self.slots("casey-morgan"))

    def test_a_dream_fact_is_rejected_and_writes_nothing(self):
        _counts, log = self.dream_wiki([{"page": "me", "slot": "phone", "value": "555-0100"}])
        self.assertTrue(any("rejected-ambiguous" in line for line in log), log)
        self.assertNotIn("phone", self.slots("me"))
        self.assertNotIn("phone", self.slots("casey-morgan"))


# ------------------------------------------------------ wiki search ----

class TestSearchingRememberedFacts(Base):
    def setUp(self):
        super().setUp()
        wiki.set_slot(self.cfg.wiki_dir, "quinn-brooks", "employer", "Acme Corp",
                      source="imessage", conn=self.conn)
        wiki.add_alias(self.cfg.wiki_dir, "quinn-brooks", "Q")

    def test_a_label_match_carries_the_slots_provenance(self):
        hits = wiki.search_facts(self.cfg.wiki_dir, "employer")
        self.assertEqual([h["slug"] for h in hits], ["quinn-brooks"])
        hit = hits[0]
        self.assertIn("label", hit["matched"])
        self.assertEqual(hit["provenance"]["employer"]["source"], "imessage")
        self.assertTrue(hit["provenance"]["employer"]["ts"])

    def test_a_value_match_carries_the_slots_provenance(self):
        hit = wiki.search_facts(self.cfg.wiki_dir, "acme")[0]
        self.assertIn("value", hit["matched"])
        self.assertEqual(hit["facts"]["employer"]["value"], "Acme Corp")
        self.assertEqual(hit["provenance"]["employer"]["source"], "imessage")

    def test_names_and_aliases_are_searchable(self):
        self.assertEqual([h["slug"] for h in
                          wiki.search_facts(self.cfg.wiki_dir, "quinn")], ["quinn-brooks"])
        alias_hit = wiki.search_facts(self.cfg.wiki_dir, "Q")[0]
        self.assertIn("alias", alias_hit["matched"])

    def test_no_match_returns_an_empty_list(self):
        self.assertEqual(wiki.search_facts(self.cfg.wiki_dir, "nonexistent"), [])
        self.assertEqual(wiki.search_facts(self.cfg.wiki_dir, ""), [])

    def test_the_result_is_bounded_and_flags_truncation(self):
        for i in range(12):
            wiki.set_slot(self.cfg.wiki_dir, f"person-{i:02d}", "hobby", "widget making",
                          source="test", conn=self.conn)
        hits = wiki.search_facts(self.cfg.wiki_dir, "widget", limit=5)
        self.assertEqual(len(hits), 5)  # len == limit is the truncation signal


# ------------------------------------------------------ tool surfaces ----

class TestTheOpenPageAndSearchToolSurfaces(Base):
    """The MCP tool strings for the missing / ambiguous / found cases."""

    def _server(self):
        from memcal import mcp_server
        server = mcp_server.Server()
        self.addCleanup(server.conn.close)
        return server

    def test_open_page_me_before_any_self_page(self):
        out = self._server().call("memcal_open_page", {"slug": "me"})
        self.assertIn("No self page yet", out)
        self.assertIn("memcal_note", out)
        self.assertEqual(self.pages(), [])  # a miss creates nothing

    def test_open_page_resolves_me_to_the_self_page(self):
        live.note(self.conn, self.cfg, "me", "home", "14 Example Lane")
        out = self._server().call("memcal_open_page", {"slug": "me"})
        self.assertIn("14 Example Lane", out)

    def test_open_page_missing_offers_the_next_steps(self):
        out = self._server().call("memcal_open_page", {"slug": "nobody"})
        self.assertIn("No page for 'nobody'", out)
        self.assertIn("memcal_search_wiki", out)

    def test_open_page_ambiguous_names_the_candidates(self):
        identity.set_me(self.conn, "Casey Morgan")
        wiki.set_slot(self.cfg.wiki_dir, "me", "home", "14 Example Lane",
                      source="test", conn=self.conn)
        wiki.set_slot(self.cfg.wiki_dir, "casey-morgan", "dog", "Comet",
                      source="test", conn=self.conn)
        out = self._server().call("memcal_open_page", {"slug": "me"})
        self.assertIn("Ambiguous self page", out)
        self.assertIn("casey-morgan", out)

    def test_search_wiki_no_match_is_not_never_told(self):
        out = self._server().call("memcal_search_wiki", {"query": "nonexistent"})
        self.assertIn("No matching wiki fact", out)
        self.assertIn("not that the user never told us", out)

    def test_search_wiki_reports_matches_and_bounds(self):
        for i in range(12):
            wiki.set_slot(self.cfg.wiki_dir, f"person-{i:02d}", "hobby", "widget making",
                          source="test", conn=self.conn)
        out = self._server().call("memcal_search_wiki", {"query": "widget", "limit": 5})
        self.assertIn("widget making", out)
        self.assertIn("bounded", out)

    def test_search_wiki_is_advertised_on_both_surfaces(self):
        from memcal import mcp_server
        self.assertIn("memcal_search_wiki", {t["name"] for t in mcp_server.TOOLS})
        # The Hermes plugin shares the app package name and needs its host runtime
        # to import, so assert against its source: the tool is defined and listed.
        plugin = (Path(__file__).resolve().parent.parent
                  / "integrations" / "hermes" / "memcal" / "__init__.py")
        source = plugin.read_text(encoding="utf-8")
        self.assertIn('"name": "memcal_search_wiki"', source)
        listed = source.split("return [OPEN,", 1)[1].split("]", 1)[0]
        self.assertIn("SEARCH_WIKI", {name.strip().rstrip(",") for name in listed.split(",")})


# ------------------------------------------------------ compatibility ----

class TestNonSelfPagesAreUnchanged(Base):
    """The self machinery must not perturb an ordinary page. Casey is the user
    here, so a contact who is plainly not the user is the control."""

    def setUp(self):
        super().setUp()
        identity.set_me(self.conn, "Casey Morgan")

    def test_a_contact_note_lands_on_their_own_page(self):
        ok, _ = live.note(self.conn, self.cfg, "Quinn Brooks", "likes", "Pokemon")
        self.assertTrue(ok)
        self.assertIsNone(wiki.resolve_self_page(self.conn, self.cfg.wiki_dir, "Quinn Brooks"))
        self.assertEqual(self.slots("quinn-brooks"), {"likes": "Pokemon"})

    def test_a_contact_dream_fact_lands_on_their_own_page(self):
        self.ingest_dream_fact(page="Quinn Brooks", slot="likes", value="Pokemon",
                               entity="person:Quinn Brooks",
                               ts="2026-08-01T09:00:00-04:00",
                               text="Quinn is really into Pokemon")
        self.assertEqual(self.slots("quinn-brooks"), {"likes": "Pokemon"})

    def test_mention_recall_still_finds_a_material_page(self):
        wiki.set_slot(self.cfg.wiki_dir, "quinn-brooks", "likes", "Pokemon",
                      source="test", conn=self.conn)
        found = wiki.mentioned_pages(self.cfg.wiki_dir, "what is Quinn Brooks into?")
        self.assertIn("quinn-brooks", [p.slug for p in found])


# --------------------------------------------- 14 Example Lane lifecycle ----

class TestTheSelfAddressLifecycle(Base):
    """The example the spec walks: the user's home address, from first statement
    to correction, through both write paths and out onto the brief."""

    def _assert_on_brief(self, value: str):
        text = brief.render(self.conn, self.cfg)
        self.assertIn(brief.ABOUT_YOU_PREFIX, text)
        self.assertIn(f"home: {value}", text)

    def test_direct_note_path(self):
        # State it: a page is born holding it, and it reaches the brief.
        ok, _ = live.note(self.conn, self.cfg, "me", "home", "14 Example Lane")
        self.assertTrue(ok)
        self.assertEqual(self.slots("me"), {"home": "14 Example Lane"})
        self._assert_on_brief("14 Example Lane")

        # Open it back by `me`: the fact reads out with its value.
        from memcal import mcp_server
        server = mcp_server.Server()
        self.addCleanup(server.conn.close)
        self.assertIn("14 Example Lane",
                      server.call("memcal_open_page", {"slug": "me"}))

        # Correct it: the same slot replaces its value and history keeps the old one.
        ok, _ = live.note(self.conn, self.cfg, "me", "home", "22 New Street")
        self.assertTrue(ok)
        self.assertEqual(self.slots("me"), {"home": "22 New Street"})
        self._assert_on_brief("22 New Street")
        self.assertEqual([r["new_value"] for r in wiki.slot_history(self.conn, "me", "home")],
                         ["14 Example Lane", "22 New Street"])

    def test_ingested_dream_path(self):
        # A first-person line the nightly pass reads opens the page and fills it.
        self.assertEqual(self.pages(), [])
        self.ingest_dream_fact(page="me", slot="home", value="14 Example Lane",
                               ts="2026-08-01T09:00:00-04:00",
                               text="just moved into 14 Example Lane", eid="d1")
        self.assertEqual(self.slots("me"), {"home": "14 Example Lane"})
        self._assert_on_brief("14 Example Lane")

        # A later, dated line corrects it; the newer evidence wins.
        self.ingest_dream_fact(page="me", slot="home", value="22 New Street",
                               ts="2026-08-05T09:00:00-04:00",
                               text="actually the new place is 22 New Street", eid="d5")
        self.assertEqual(self.slots("me"), {"home": "22 New Street"})
        self._assert_on_brief("22 New Street")

    def test_both_paths_agree_on_where_the_fact_lives(self):
        # Whichever path states it, the fact lands on the one self page and the
        # store never grows a second one.
        identity.set_me(self.conn, "Casey Morgan")
        wiki.set_slot(self.cfg.wiki_dir, "casey-morgan", "dog", "Comet",
                      source="test", conn=self.conn)
        live.note(self.conn, self.cfg, "me", "home", "14 Example Lane")
        self.ingest_dream_fact(page="Casey Morgan", slot="phone", value="555-0100",
                               entity="person:me", ts="2026-08-02T09:00:00-04:00",
                               text="my number is 555-0100", eid="p1")
        self.assertEqual(self.pages(), ["casey-morgan"])
        self.assertEqual(self.slots("casey-morgan"),
                         {"dog": "Comet", "home": "14 Example Lane", "phone": "555-0100"})


class TestUnifiedOpenRouting(Base):
    def setUp(self):
        super().setUp()
        from memcal import mcp_server
        db.set_today("2026-08-10")
        self.server = mcp_server.Server.__new__(mcp_server.Server)
        self.server.cfg, self.server.conn = self.cfg, self.conn

    def cli(self, command, target, *extra):
        from memcal import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = cli.main(["--home", self.dir, command, target, *extra])
        return status, out.getvalue()

    def assert_routes(self, target, expected, status=0):
        results = [self.cli(command, target) for command in ("open", "page")]
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0][0], status)
        self.assertIn(expected, results[0][1])
        opened = self.server.call("memcal_open", {"ref": target})
        self.assertEqual(opened, self.server.call("memcal_open_page", {"slug": target}))
        self.assertIn(expected, opened)

    def test_handles_keep_their_detail_route_including_legacy_and_brackets(self):
        from memcal import events, todos
        event, _ = events.upsert(self.conn, {"title": "Routing dinner", "date": "2026-08-10"})
        todo, _ = todos.open_todo(self.conn, "Routing task")
        todos.ask(self.conn, "Routing question?")
        question = self.conn.execute("SELECT id FROM questions").fetchone()
        for target, expected in ((f"E{event.id}", "Routing dinner"),
                                 (f" [ e {event.id} ] ", "Routing dinner"),
                                 (f"〔T{todo.id}〕", "Routing task"),
                                 (f"Q{question['id']}", "Routing question?"),
                                 ("S999999", "no row")):
            with self.subTest(target=target):
                self.assert_routes(target, expected, 1 if target == "S999999" else 0)

    def test_self_names_aliases_and_other_pages_read_without_writes(self):
        identity.set_me(self.conn, "Casey Morgan")
        wiki.set_slot(self.cfg.wiki_dir, "casey-morgan", "dog", "Comet",
                      source="test", conn=self.conn)
        wiki.add_alias(self.cfg.wiki_dir, "casey-morgan", "CJ")
        wiki.set_slot(self.cfg.wiki_dir, "quinn-brooks", "likes", "Pokemon",
                      source="test", conn=self.conn)
        wiki.add_alias(self.cfg.wiki_dir, "quinn-brooks", "Q")
        before = {slug: wiki.read(self.cfg.wiki_dir, slug).render() for slug in self.pages()}
        for target in ("me", "Casey Morgan", "CJ"):
            with self.subTest(target=target):
                self.assert_routes(target, "Comet")
        for target in ("quinn-brooks", "Quinn Brooks", "Q"):
            with self.subTest(target=target):
                self.assert_routes(target, "Pokemon")
        self.assertEqual(before, {slug: wiki.read(self.cfg.wiki_dir, slug).render()
                                  for slug in self.pages()})

    def test_missing_handles_do_not_fall_back_to_pages(self):
        wiki.set_slot(self.cfg.wiki_dir, "e999999", "likes", "Pokemon",
                      source="test", conn=self.conn)
        self.assert_routes("E999999", "no row", 1)

    def test_missing_pages_and_self_reads_create_nothing(self):
        for target in ("nobody", "me"):
            with self.subTest(target=target):
                self.assertEqual(self.cli("open", target)[0], 1)
                opened = self.server.call("memcal_open", {"ref": target})
                self.assertIn("No self page yet" if target == "me" else "No page", opened)
                self.assertEqual(opened, self.server.call("memcal_open_page", {"slug": target}))
        self.assertEqual(self.pages(), [])

    def test_ambiguous_self_keeps_candidates_and_errors(self):
        identity.set_me(self.conn, "Casey Morgan")
        for slug in ("me", "casey-morgan"):
            wiki.set_slot(self.cfg.wiki_dir, slug, "dog", "Comet",
                          source="test", conn=self.conn)
        self.assert_routes("me", "Ambiguous self page", 1)
        self.assertIn("casey-morgan", self.cli("open", "me")[1])
        opened = self.server.call("memcal_open", {"ref": "me"})
        self.assertIn("memcal_open with ref=", opened)
        self.assertNotIn("memcal_open_page", opened)
        self.assertEqual(self.pages(), ["casey-morgan", "me"])

    def test_page_slot_write_form_is_preserved(self):
        status, _ = self.cli("page", "me", "home", "14 Example Lane")
        self.assertEqual(status, 0)
        self.assertEqual(self.slots("me"), {"home": "14 Example Lane"})
        self.assert_routes("me", "14 Example Lane")

    def test_search_hints_use_unified_open(self):
        wiki.set_slot(self.cfg.wiki_dir, "quinn-brooks", "likes", "Pokemon",
                      source="test", conn=self.conn)
        found = self.server.call("memcal_search_wiki", {"query": "Pokemon"})
        self.assertIn("memcal_open ref='quinn-brooks'", found)
        self.assertNotIn("memcal_open_page", found)


if __name__ == "__main__":
    unittest.main()
