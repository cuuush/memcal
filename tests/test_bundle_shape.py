"""Conversation-shape labels must describe the medium, not the entity-key fallback."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from memcal import archive, db, gate
from memcal.dream.bundle import Bundle


def email_line(thread: str, minute: int) -> dict:
    return {
        "stream": "email",
        "thread": thread,
        "ts": f"2026-07-30T12:{minute:02d}:00-04:00",
    }


class BundleShapeTest(unittest.TestCase):
    def test_an_unresolved_sender_is_still_an_email_thread(self):
        bundle = Bundle(
            entity="thread:email:lyfe@anders.life",
            items=[email_line("Re: :)", 44), email_line("Re: :)", 45)],
        )
        self.assertEqual(
            bundle._shape(),
            "email thread · 2 lines",
        )

    def test_a_known_persons_single_email_conversation_is_an_email_thread_too(self):
        bundle = Bundle(
            entity="person:Rowan",
            items=[email_line("Re: :)", 44)],
        )
        self.assertEqual(bundle._shape(), "email thread · 1 line")

    def test_multiple_email_threads_remain_multiple_conversations(self):
        bundle = Bundle(
            entity="person:Rowan",
            items=[email_line("First subject", 44), email_line("Second subject", 45)],
        )
        self.assertTrue(bundle._shape().startswith("2 conversations on email · 2 lines"))


class TestACommandToAMachineIsNotACommitmentHeMade(unittest.TestCase):
    """Treat directives to an assistant as delegated work, not the user's commitment."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.conn = db.open_db(Path(self.dir) / "memcal.db")
        self.addCleanup(self.conn.close)

    # -- the gate stops asserting a commitment it cannot see ------------------
    def test_an_instruction_to_a_machine_is_not_his_own_commitment(self):
        verdict = gate.gate_message("let's get the seerr issues closed out",
                                    from_me=True, addressed_to="machine")
        self.assertTrue(verdict, "a delegated instruction must still be read")
        self.assertEqual(verdict.reason, "directive")

    def test_the_same_words_to_a_person_are_still_his_own_commitment(self):
        """The decoy for the gate half. Nothing about the person streams moved."""
        verdict = gate.gate_message("let's get the seerr issues closed out",
                                    from_me=True)
        self.assertTrue(verdict)
        self.assertEqual(verdict.reason, "own-commitment")

    def test_the_gate_was_never_the_lever_and_must_not_be_mistaken_for_it(self):
        """`COMMIT_RE` is a *first-person* detector, so the line that started #57 never
        fired `own-commitment` at all — archive 20080 was `no-signal` and reached the
        model as a neighbour of a gated line (`bundle.add_thread_context`). Anyone
        tempted to close this by tightening the gate should read this test first."""
        for text in ("apply for 5 jobs pls",
                     "fix the 2 issues, close them on seerr with comment",
                     "delete bad torrents from qbittorrent and fs"):
            with self.subTest(text=text):
                verdict = gate.gate_message(text, from_me=True, addressed_to="machine")
                self.assertFalse(verdict)
                self.assertEqual(verdict.reason, "no-signal")

    def test_recall_does_not_change(self):
        """Where the gate does fire it still passes. The agent stream is the
        highest-signal one there is, and a fix that quietly stopped reading it would be
        a worse bug than #57 — including for the decoy, which is still their."""
        for text in ("i need to give Rowan back their EZ-Pass",
                     "remind me to venmo Cameron for the ticket"):
            with self.subTest(text=text):
                self.assertTrue(gate.gate_message(text, from_me=True,
                                                  addressed_to="machine"))

    # -- the column exists and survives a round trip -------------------------
    def test_the_archive_records_what_a_line_was_addressed_to(self):
        machine = archive.append(
            self.conn, stream="agent", external_id="a1", ts="2026-08-18T09:58:00-04:00",
            text="apply for 5 jobs pls", thread="hermes:s1", person="me",
            from_me=True, addressed_to="machine", gated=True, gate_reason="directive")
        person = archive.append(
            self.conn, stream="imessage", external_id="b1",
            ts="2026-08-18T09:58:00-04:00", text="ill grab the tickets tonight",
            thread="chat1", person="me", from_me=True, gated=True,
            gate_reason="own-commitment")
        rows = {r["id"]: r["addressed_to"] for r in
                self.conn.execute("SELECT id, addressed_to FROM archive")}
        self.assertEqual(rows[machine], "machine")
        self.assertEqual(rows[person], "person",
                         "every stream that existed before #57 has a person on it")

    def test_a_stream_that_never_said_defaults_to_a_person(self):
        """The migration on an existing store must not relabel eleven thousand texts."""
        row_id = archive.append(
            self.conn, stream="imessage", external_id="c1",
            ts="2026-08-18T10:00:00-04:00", text="you around later", thread="chat1")
        got = self.conn.execute(
            "SELECT addressed_to FROM archive WHERE id = ?", (row_id,)).fetchone()[0]
        self.assertEqual(got, "person")

    # -- the model can finally see the difference ----------------------------
    def _rendered(self, addressed_to: str) -> str:
        bundle = Bundle(entity="person:me", items=[{
            "stream": "agent", "thread": "conversation", "person": "me",
            "handle": None, "from_me": 1, "addressed_to": addressed_to,
            "ts": "2026-08-18T09:58:00-04:00", "text": "apply for 5 jobs pls",
        }])
        return bundle.render()

    def test_a_delegated_line_names_its_addressee(self):
        self.assertIn("me → assistant: apply for 5 jobs pls", self._rendered("machine"))

    def test_a_line_to_a_person_is_unchanged(self):
        rendered = self._rendered("person")
        self.assertIn("me: apply for 5 jobs pls", rendered)
        self.assertNotIn("→", rendered)

    def test_a_row_with_no_such_field_renders_as_before(self):
        """`live.remember` and the benchmark both hand `Bundle` rows built by hand."""
        bundle = Bundle(entity="person:me", items=[{
            "stream": "imessage", "thread": "chat1", "person": "me", "handle": None,
            "from_me": 1, "ts": "2026-08-18T09:58:00-04:00", "text": "on my way",
        }])
        self.assertIn("me: on my way", bundle.render())


if __name__ == "__main__":
    unittest.main()
