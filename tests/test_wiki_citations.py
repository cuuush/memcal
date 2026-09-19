"""Wiki facts cite their exact lines: markdown `#id` comments, narrow evidence,
section groups with the user's page first, and no `preferences/` section."""

from __future__ import annotations

import unittest
from datetime import timedelta

try:
    from tests._support import Base
except ModuleNotFoundError:  # direct execution from tests/
    from _support import Base
from memcal import archive, db, identity, trace, wiki
from memcal import web_memory
from memcal.dream import apply as apply_stage
from memcal.dream import propose as propose_stage
from memcal.dream.bundle import Bundle


def _bundle(conn, entity, texts, thread="t1"):
    stamp = db.now_dt()
    ids = []
    for n, text in enumerate(texts):
        ids.append(archive.append(
            conn, channel="imessage", external_id=f"{entity}-{n}-{stamp.minute}",
            ts=(stamp + timedelta(minutes=n)).isoformat(), text=text,
            thread=thread, person="Jordan", from_me=False))
    conn.commit()
    items = list(conn.execute("SELECT * FROM archive WHERE id IN (%s) ORDER BY id"
                              % ",".join("?" * len(ids)), ids))
    return Bundle(entity=entity, title=entity, items=items), ids


class TestSlotLineRefsRoundTrip(Base):
    """`#12` tokens in the slot comment survive a read and a re-render."""

    def test_set_slot_writes_line_refs_into_the_comment(self):
        wiki.set_slot(self.cfg.wiki_dir, "jordan", "location", "Riverton",
                      source="person:Jordan", archive_ids=[12, 13])
        page = wiki.read(self.cfg.wiki_dir, "jordan")
        self.assertEqual(page.slots["location"]["lines"], [12, 13])
        self.assertIn("<!-- person:Jordan", page.render())
        self.assertIn("#12 #13", page.render())

    def test_old_comments_without_refs_read_as_no_lines(self):
        path = wiki.path_for(self.cfg.wiki_dir, "jordan", "people")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Jordan\n\n## Facts\n\n"
                        "- **location**: Eastwood  <!-- person:Jordan 2026-08-01 -->\n",
                        encoding="utf-8")
        page = wiki.read(self.cfg.wiki_dir, "jordan")
        self.assertEqual(page.slots["location"]["lines"], [])
        self.assertEqual(page.slots["location"]["source"], "person:Jordan")


class TestWikiCitesResolveLikeOtherRows(Base):
    """The model's `cites` on a wiki row become archive ids, not dropped."""

    def test_resolve_cites_fills_cite_ids_on_wiki_rows(self):
        bundle, ids = _bundle(self.conn, "person:Jordan",
                              ["hello there", "I moved to Riverton"])
        diff = {"wiki": [{"page": "jordan", "slot": "location",
                          "value": "Riverton", "cites": ["L2"]}]}
        propose_stage._resolve_cites(bundle, diff)
        self.assertEqual(diff["wiki"][0]["cite_ids"], [ids[1]])


class TestWikiWritesStampNarrowEvidence(Base):
    """A cited wiki fact points at its lines, not the whole bundle."""

    def test_cited_fact_stamps_only_its_lines_and_marks_the_file(self):
        bundle, ids = _bundle(self.conn, "person:Jordan",
                              ["how is the new place",
                               "I moved to Riverton last week",
                               "see you at poker"])
        diff = {"wiki": [{"page": "jordan", "slot": "location",
                          "value": "Riverton", "cite_ids": [ids[1]]}]}
        apply_stage.apply_diffs(self.conn, self.cfg, [(bundle, diff, "gen-1")],
                                written_by="dream:nightly")
        linked = [r["archive_id"] for r in self.conn.execute(
            "SELECT archive_id FROM evidence WHERE kind = 'wiki'")]
        self.assertEqual(linked, [ids[1]])
        page = wiki.read(self.cfg.wiki_dir, "jordan")
        self.assertEqual(page.slots["location"]["lines"], [ids[1]])
        profile = wiki.profile(self.conn, self.cfg.wiki_dir, "jordan")
        self.assertTrue(profile["narrow"]["location"])
        self.assertEqual(profile["cited_ids"]["location"], [ids[1]])
        self.assertEqual(profile["provenance"]["location"]["entity"],
                         "person:Jordan")

    def test_uncited_fact_falls_back_to_its_own_best_lines(self):
        bundle, ids = _bundle(self.conn, "person:Jordan",
                              ["Quinn Brooks: I'm in",
                               "I live at 42 Example Street now",
                               "ok see you then"])
        diff = {"wiki": [{"page": "jordan", "slot": "location",
                          "value": "42 Example Street"}]}
        apply_stage.apply_diffs(self.conn, self.cfg, [(bundle, diff, "gen-1")],
                                written_by="dream:nightly")
        linked = [r["archive_id"] for r in self.conn.execute(
            "SELECT archive_id FROM evidence WHERE kind = 'wiki'")]
        self.assertIn(ids[1], linked)
        self.assertLess(len(linked), len(ids))


class TestPreferencesLeavesTheWiki(Base):
    """`preferences/` is not a section: new writes land in people, old files move."""

    def test_a_preferences_section_coerces_to_people(self):
        apply_stage._apply_wiki(
            self.conn, self.cfg,
            {"page": "riley", "section": "preferences",
             "slot": "favorite animal", "value": "otters"},
            source="test", seen=set())
        self.assertEqual(wiki.read(self.cfg.wiki_dir, "riley").section, "people")

    def test_stray_preference_files_migrate_into_people(self):
        legacy = self.cfg.wiki_dir / "preferences"
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "riley.md").write_text(
            "# Riley\n\n## Facts\n\n- **favorite animal**: otters  <!-- test -->\n",
            encoding="utf-8")
        self.assertEqual(wiki.migrate_preferences(self.cfg.wiki_dir), ["riley"])
        page = wiki.read(self.cfg.wiki_dir, "riley")
        self.assertEqual(page.section, "people")
        self.assertEqual(page.slots["favorite animal"]["value"], "otters")
        self.assertFalse((self.cfg.wiki_dir / "preferences").exists())

    def test_staged_write_to_a_legacy_page_forks_no_second_file(self):
        legacy = self.cfg.wiki_dir / "preferences"
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "riley.md").write_text(
            "# Riley\n\n## Facts\n\n- **work**: baker  <!-- test 2026-01-01 -->\n",
            encoding="utf-8")
        self.conn.execute("BEGIN")
        try:
            wiki.set_slot(self.cfg.wiki_dir, "riley", "work", "cook",
                          section="preferences", conn=self.conn, commit=False)
            staged = [r["path"] for r in self.conn.execute(
                "SELECT path FROM wiki_pending_writes")]
            self.assertEqual(staged, ["preferences/riley.md"])
            self.assertFalse(
                (self.cfg.wiki_dir / "people" / "riley.md").exists())
        finally:
            self.conn.rollback()

    def test_migration_conflict_prefers_newer_evidence_and_keeps_history(self):
        people = self.cfg.wiki_dir / "people"
        people.mkdir(parents=True, exist_ok=True)
        (people / "riley.md").write_text(
            "# Riley\n\n## Facts\n\n"
            "- **work**: baker  <!-- test 2026-01-01 -->\n", encoding="utf-8")
        legacy = self.cfg.wiki_dir / "preferences"
        legacy.mkdir(parents=True, exist_ok=True)
        (legacy / "riley.md").write_text(
            "# Riley\n\n## Facts\n\n"
            "- **work**: cook  <!-- test 2026-09-01 -->\n", encoding="utf-8")
        self.assertEqual(
            wiki.migrate_preferences(self.cfg.wiki_dir, conn=self.conn), ["riley"])
        page = wiki.read(self.cfg.wiki_dir, "riley")
        self.assertEqual(page.slots["work"]["value"], "cook")
        trail = wiki.slot_history(self.conn, "riley", "work")
        self.assertEqual([(r["old_value"], r["new_value"]) for r in trail],
                         [("baker", "cook")])

    def test_sections_hold_no_preferences(self):
        self.assertNotIn("preferences", wiki.SECTIONS)
        self.assertEqual(wiki.SLOTS.get("preferences", None), None)


class TestSelfPageSortsFirst(Base):
    """The user's own page leads the index and carries the mark."""

    def test_self_page_is_first_and_flagged(self):
        identity.set_me(self.conn, "Casey", "Casey Morgan")
        wiki.set_slot(self.cfg.wiki_dir, "amy", "work", "baker")
        wiki.set_slot(self.cfg.wiki_dir, "casey-morgan", "work",
                      "carpenter")
        out = web_memory.wiki_pages(self.conn, self.cfg)
        self.assertEqual(out["pages"][0]["slug"], "casey-morgan")
        self.assertTrue(out["pages"][0]["is_self"])
        self.assertFalse(any(p["is_self"] for p in out["pages"][1:]))
        profile = wiki.profile(self.conn, self.cfg.wiki_dir,
                               "casey-morgan")
        self.assertTrue(profile["is_self"])
        other = wiki.profile(self.conn, self.cfg.wiki_dir, "amy")
        self.assertFalse(other["is_self"])


class TestWikiProfileKeepsItsContract(Base):
    """The profile additions are additive: old readers keep working."""

    def test_old_keys_survive_alongside_provenance(self):
        wiki.set_slot(self.cfg.wiki_dir, "reese", "relationship", "partner",
                      conn=self.conn)
        profile = wiki.profile(self.conn, self.cfg.wiki_dir, "reese")
        self.assertIn("- **relationship**: partner", profile["page"])
        self.assertIn("sources", profile)
        self.assertIn("provenance", profile)
        self.assertIn("cited_ids", profile)
        self.assertIn("is_self", profile)


if __name__ == "__main__":
    unittest.main()
