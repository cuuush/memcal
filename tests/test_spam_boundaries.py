"""Regression boundaries for spam that reached the first-load dream preview."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from memcal import db, events, gate
from memcal.config import Config


TASK_SCAM = """Hi! Avery Quinn reaching out from Amazon's Remote Recruitment Team.
We're reaching out to share a flexible online opening that may interest you.
Help Amazon merchants update products and reach a larger customer audience.
Time commitment: 60–90 minutes/day, 4 days a week
Pay range: $100–$600 daily (base $5,300 per 30 working days)
Only 18 positions are currently available.
If you're at least 22 and interested, text "More Info" to 3473229586."""


class SpamBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.conn: sqlite3.Connection = db.open_db(self.cfg.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_task_scam_is_blocked_even_when_imessage_is_read_in_full(self):
        verdict = gate.gate_message(
            TASK_SCAM,
            stream="imessage",
            is_group=True,
        )
        self.assertFalse(verdict)
        self.assertEqual(verdict.reason, "task-scam")

    def test_ordinary_recruiter_message_is_not_called_a_scam(self):
        text = ("I'm recruiting for a remote Python role at Example. "
                "The salary range is $140k–$170k. Are you interested?")
        self.assertFalse(gate.is_task_scam(text))
        self.assertTrue(gate.gate_message(text, stream="imessage"))

    def test_partiful_placeholder_words_do_not_link_unrelated_events(self):
        for offset, title in ((8, "We're Going to Elements!!!"), (23, "Jack's 30th")):
            events.upsert(
                self.conn,
                {
                    "date": (db.today() + timedelta(days=offset)).isoformat(),
                    "kind": "commitment",
                    "subject": "me",
                    "title": title,
                    "location": "Partiful",
                    "series": "Location available once RSVP'd",
                    "status": "confirmed",
                },
                written_by="ical",
            )

        same, related = events.amendable_groups(
            self.conn,
            people=[],
            entity="thread:imessage:jordan@example.com",
            text=TASK_SCAM,
        )
        self.assertEqual(same, [])
        self.assertEqual(related, [])


if __name__ == "__main__":
    unittest.main()
