"""Sender evidence and explicit rejection across email ingestion and Dream input."""

from email.message import EmailMessage
from unittest.mock import Mock
import unittest

from tests._support import Base
from memcal import archive, db, detail, events, identity, trace, web_queue
from memcal.dream import bundle
from memcal.sources import base, proton


class EmailSenderEvidenceTest(Base):
    def setUp(self):
        super().setUp()
        db.set_today("2026-10-25T12:00:00-04:00")
        self.address = "examiner@trademark-review.example"
        self.thread = "opaque-root@mail.example"

    def mail(self, *, address=None, channel="email", from_me=False):
        return archive.append(
            self.conn, channel=channel, external_id=f"mail-{db.now()}-{address}-{channel}-{from_me}",
            ts=db.now(), thread=self.thread, handle=address or self.address,
            person="Trademark Examiner", from_me=from_me,
            text="Required verification call tomorrow at 4:15 PM", gated=True)

    def event(self, ids, entity="person:Trademark Examiner"):
        event = events.upsert(self.conn, {
            "title": "Trademark verification call", "date": "2026-10-26",
            "kind": "commitment", "status": "confirmed",
        }, match=False).event
        trace.stamp(self.conn, kind="event", ref=event.key,
                    entity=entity, archive_ids=ids)
        self.conn.commit()
        return event

    def test_named_sender_address_survives_both_dream_formats_and_open(self):
        aid = self.mail()
        event = self.event([aid])
        row = self.conn.execute("SELECT * FROM archive WHERE id=?", (aid,)).fetchone()
        b = bundle.Bundle(entity="person:Trademark Examiner", items=[row])
        for fmt in bundle.FORMATS:
            with self.subTest(fmt=fmt):
                self.assertIn(f"Trademark Examiner <{self.address}>", b.render(fmt))
                self.assertEqual(b.cite(["L1"]), [aid])
        self.assertIn(f"Trademark Examiner <{self.address}>",
                      detail.open_handle(self.conn, self.cfg, f"E{event.id}"))
        self.assertIn(self.address, trace.line(self.conn, aid)["who"])

    def test_email_evidence_blocks_named_sender_and_stops_future_body_fetches(self):
        aid = self.mail()
        archive.spool_add(self.conn, aid, "person:Trademark Examiner")
        event = self.event([aid])
        out = web_queue.block(self.conn, self.cfg, {"event_id": event.id, "by": "agent"})
        self.assertEqual(out["blocked"], self.address)
        self.assertEqual(out["retired"], 1)
        self.assertTrue(identity.sender_blocked(self.conn, self.address))
        headers = EmailMessage()
        headers["From"] = f'"Trademark Examiner" <{self.address}>'
        headers["Subject"] = "Reminder: appointment confirmed for tomorrow"
        headers["Date"] = "Sun, 25 Oct 2026 12:01:00 -0400"
        headers["Message-ID"] = "<next-message@sender.example>"
        bridge = Mock()
        proton._handle_message(self.conn, self.cfg, base.IngestReport("email"),
                               bridge, 2, headers, "INBOX")
        bridge.message.assert_not_called()
        bridge.body.assert_not_called()
        new = self.conn.execute("SELECT * FROM archive WHERE external_id=?",
                                ("next-message@sender.example",)).fetchone()
        self.assertEqual(new["text"], str(headers["Subject"]))
        self.assertFalse(new["gated"])
        self.assertEqual(bundle.build(self.conn), [])
        # Blocking alone never silently removes a real calendar commitment.
        self.assertIsNotNone(events.get(self.conn, event.key))

    def test_message_id_thread_without_citations_resolves_actual_sender(self):
        self.mail()
        event = self.event([], entity=f"thread:email:{self.thread}")
        out = web_queue.block(self.conn, self.cfg, {"event_id": event.id})
        self.assertEqual(out["blocked"], self.address)
        self.assertIsNone(identity.sender_row(self.conn, self.thread))

    def test_ambiguous_or_mixed_evidence_requires_an_explicit_target(self):
        first = self.mail()
        for other in (self.mail(address="lawyer@example.org"), self.mail(channel="imessage")):
            event = self.event([first, other])
            out = web_queue.block(self.conn, self.cfg, {"event_id": event.id})
            self.assertIn("explicit", out["error"])
            self.assertFalse(identity.sender_blocked(self.conn, self.address))
        self.assertEqual(identity.senders(self.conn), [])

    def test_own_mail_is_not_a_sender_to_block(self):
        event = self.event([self.mail(from_me=True)])
        self.assertIn("explicit", web_queue.block(
            self.conn, self.cfg, {"event_id": event.id})["error"])

    def test_named_chat_speaker_is_unchanged(self):
        aid = self.mail(channel="imessage")
        self.assertEqual(trace.line(self.conn, aid)["who"], "Trademark Examiner")


if __name__ == "__main__":
    unittest.main()
