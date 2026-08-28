"""Regression tests for the SQLite/markdown boundary of wiki slot writes."""

from __future__ import annotations

import sqlite3
import unittest
from unittest import mock

try:
    from tests._support import Base
except ModuleNotFoundError:  # direct execution from tests/
    from _support import Base
from memcal import archive, db, wiki
from memcal.dream import apply as apply_stage
from memcal.dream.bundle import Bundle


class TestAWikiSlotHistoryWriteCannotOutrunItsPage(Base):
    """A failed replacement stays queued and is safe to replay later."""

    def test_file_failure_leaves_the_old_page_and_a_recoverable_publication(self):
        wiki.set_slot(self.cfg.wiki_dir, "jordan", "location", "Eastwood")
        with mock.patch.object(wiki, "_write_rendered", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                wiki.set_slot(self.cfg.wiki_dir, "jordan", "location", "Riverton",
                              source="agent", conn=self.conn)

        self.assertEqual(wiki.read(self.cfg.wiki_dir, "jordan").slots["location"]["value"],
                         "Eastwood")
        self.assertEqual(wiki.slot_history(self.conn, "jordan", "location")[0]["new_value"],
                         "Riverton")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM wiki_pending_writes")
                         .fetchone()[0], 1)

        # Recovery works after a process-shaped close and reopen.
        self.conn.close()
        self.conn = db.open_db(self.cfg.db_path)
        wiki.recover(self.conn, self.cfg.wiki_dir)
        self.assertEqual(wiki.read(self.cfg.wiki_dir, "jordan").slots["location"]["value"],
                         "Riverton")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM wiki_pending_writes")
                         .fetchone()[0], 0)

    def test_recovery_preserves_a_hand_edit_made_while_publication_was_pending(self):
        wiki.set_slot(self.cfg.wiki_dir, "jordan", "location", "Eastwood")
        with mock.patch.object(wiki, "_write_rendered", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                wiki.set_slot(self.cfg.wiki_dir, "jordan", "location", "Riverton",
                              source="agent", conn=self.conn)

        path = wiki.read(self.cfg.wiki_dir, "jordan").path
        path.write_text(path.read_text() + "\nHand-edited note.\n")
        with self.assertRaisesRegex(wiki.WikiWriteConflict, "changed while publication"):
            wiki.recover(self.conn, self.cfg.wiki_dir)
        self.assertIn("Hand-edited note", path.read_text())
        self.assertEqual(self.conn.execute("SELECT count(*) FROM wiki_pending_writes")
                         .fetchone()[0], 1)

    def test_database_failure_does_not_publish_the_new_page(self):
        wiki.set_slot(self.cfg.wiki_dir, "jordan", "location", "Eastwood")
        with mock.patch.object(wiki, "record_slot_change", side_effect=RuntimeError("db failed")):
            with self.assertRaisesRegex(RuntimeError, "db failed"):
                wiki.set_slot(self.cfg.wiki_dir, "jordan", "location", "Riverton",
                              source="agent", conn=self.conn)

        self.assertEqual(wiki.read(self.cfg.wiki_dir, "jordan").slots["location"]["value"],
                         "Eastwood")
        self.assertEqual(wiki.slot_history(self.conn, "jordan", "location"), [])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM wiki_pending_writes")
                         .fetchone()[0], 0)

    def test_recovery_cannot_publish_outside_the_wiki_directory(self):
        outside = self.cfg.home / "outside.md"
        self.conn.execute(
            "INSERT INTO wiki_pending_writes(path, content) VALUES(?, ?)",
            ("../outside.md", "not a wiki page"))
        self.conn.commit()
        with self.assertRaisesRegex(ValueError, "escapes wiki directory"):
            wiki.recover(self.conn, self.cfg.wiki_dir)
        self.assertFalse(outside.exists())


class TestAWikiPageReplacementNeverExposesHalfMarkdown(Base):
    def test_failed_replace_preserves_the_previous_file(self):
        page = wiki.ensure(self.cfg.wiki_dir, "jordan")
        page.body = "before"
        wiki.write(self.cfg.wiki_dir, page)
        page.body = "after"
        with mock.patch.object(wiki.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                wiki.write(self.cfg.wiki_dir, page)

        self.assertIn("before", wiki.read(self.cfg.wiki_dir, "jordan").body)
        self.assertNotIn("after", wiki.read(self.cfg.wiki_dir, "jordan").body)


class TestApplyDiffsCommitsRowsAndProvenanceTogether(Base):
    def _proposal(self):
        archive.append(
            self.conn,
            stream="imessage",
            external_id="atomic-1",
            ts=f"{db.today().isoformat()}T12:00:00-04:00",
            text="Dinner tomorrow. Jordan moved to Riverton and also goes by Jordy.",
            thread="atomic",
            person="Jordan",
        )
        self.conn.commit()
        bundle = Bundle(
            entity="person:Jordan",
            title="Jordan",
            items=list(self.conn.execute(
                "SELECT * FROM archive WHERE external_id = 'atomic-1'"
            )),
        )
        diff = {
            "events": [{"title": "Dinner with Jordan", "date": self.d(1)}],
            "wiki": [{
                "page": "jordan",
                "slot": "location",
                "value": "Riverton",
                "alias": "Jordy",
                "question": "What is Jordan's favorite tea?",
            }],
        }
        return [(bundle, diff, "generation-1")]

    def test_a_provenance_failure_rolls_back_rows_and_staged_pages(self):
        wiki.set_slot(self.cfg.wiki_dir, "jordan", "location", "Eastwood")
        proposals = self._proposal()
        self.conn.execute(
            """CREATE TRIGGER fail_wiki_provenance
               BEFORE INSERT ON provenance
               WHEN NEW.kind = 'wiki'
               BEGIN SELECT RAISE(ABORT, 'provenance failed'); END"""
        )
        self.conn.commit()

        with self.assertRaisesRegex(sqlite3.IntegrityError, "provenance failed"):
            apply_stage.apply_diffs(
                self.conn, self.cfg, proposals, written_by="dream:nightly"
            )

        self.assertEqual(self.conn.execute("SELECT count(*) FROM events").fetchone()[0], 0)
        for table in ("provenance", "evidence", "slot_history", "wiki_pending_writes"):
            self.assertEqual(
                self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0], 0
            )
        page = wiki.read(self.cfg.wiki_dir, "jordan")
        self.assertEqual(page.slots["location"]["value"], "Eastwood")
        self.assertEqual(page.aliases, [])
        self.assertEqual(page.questions, [])

    def test_rows_audit_trail_and_page_publish_together(self):
        apply_stage.apply_diffs(
            self.conn, self.cfg, self._proposal(), written_by="dream:nightly"
        )

        self.assertEqual(self.conn.execute("SELECT count(*) FROM events").fetchone()[0], 1)
        self.assertGreaterEqual(
            self.conn.execute("SELECT count(*) FROM provenance").fetchone()[0], 4
        )
        self.assertGreaterEqual(
            self.conn.execute("SELECT count(*) FROM evidence").fetchone()[0], 2
        )
        self.assertEqual(
            self.conn.execute("SELECT count(*) FROM wiki_pending_writes").fetchone()[0], 0
        )
        page = wiki.read(self.cfg.wiki_dir, "jordan")
        self.assertEqual(page.slots["location"]["value"], "Riverton")
        self.assertIn("Jordy", page.aliases)
        self.assertIn("What is Jordan's favorite tea?", page.questions)


if __name__ == "__main__":
    unittest.main()
