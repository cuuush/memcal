"""Signal envelopes: incoming vs. sent, and which chat each belongs to."""

from __future__ import annotations

import unittest

from memcal.sources import signal
from memcal.sources.polled import StreamSource


class TestSignalText(unittest.TestCase):
    def test_words_win(self):
        self.assertIn("dinner", signal._text("dinner on Friday", []))

    def test_an_attachment_with_no_words_is_summarised(self):
        self.assertEqual(signal._text("", [{"contentType": "image/jpeg"}]), "[image]")

    def test_nothing_at_all_is_empty(self):
        self.assertEqual(signal._text("", []), "")


class TestSignalEnvelopes(unittest.TestCase):
    def setUp(self):
        self.source = signal.SignalSource()
        self.source._me = "+15550000000"
        self.source._groups = {"gid-1": "Board Games"}

    def test_an_incoming_dm_is_named_for_the_sender(self):
        chat, message = self.source.normalize({"envelope": {
            "source": "+15551112222", "sourceNumber": "+15551112222",
            "sourceName": "Ada", "timestamp": 1712345678000,
            "dataMessage": {"timestamp": 1712345678000, "message": "lunch at one?"}}})
        self.assertEqual(chat.name, "Ada")
        self.assertFalse(chat.is_group)
        self.assertFalse(message.from_me)
        self.assertEqual(message.author_id, "+15551112222")

    def test_an_incoming_group_message_is_named_for_the_group(self):
        chat, _message = self.source.normalize({"envelope": {
            "source": "+15551112222", "sourceName": "Ada", "timestamp": 1712345678000,
            "dataMessage": {"timestamp": 1712345678000, "message": "who is in?",
                            "groupInfo": {"groupId": "gid-1"}}}})
        self.assertEqual(chat.name, "Board Games")
        self.assertTrue(chat.is_group)

    def test_an_unknown_group_is_still_readable_rather_than_a_raw_blob(self):
        chat, _message = self.source.normalize({"envelope": {
            "source": "+1", "timestamp": 1, "dataMessage": {
                "timestamp": 1, "message": "hi",
                "groupInfo": {"groupId": "unlisted-group-id"}}}})
        self.assertTrue(chat.name.startswith("Signal group"))

    def test_a_message_i_sent_elsewhere_is_mine_and_named_for_the_recipient(self):
        chat, message = self.source.normalize({"envelope": {
            "source": self.source._me, "timestamp": 1712345678000,
            "syncMessage": {"sentMessage": {
                "timestamp": 1712345678000, "destination": "+15551112222",
                "message": "on my way"}}}})
        self.assertTrue(message.from_me)
        self.assertEqual(chat.id, "dm:+15551112222")
        self.assertEqual(message.author_id, "")

    def test_a_receipt_carries_no_conversation_and_is_ignored(self):
        self.assertIsNone(self.source.normalize(
            {"envelope": {"source": "+1", "receiptMessage": {"when": 1}}}))

    def test_a_typing_notice_is_ignored(self):
        self.assertIsNone(self.source.normalize(
            {"envelope": {"source": "+1", "typingMessage": {"action": "STARTED"}}}))

    def test_the_external_id_is_scoped_to_its_chat(self):
        _chat, message = self.source.normalize({"envelope": {
            "source": "+15551112222", "timestamp": 1712345678000,
            "dataMessage": {"timestamp": 1712345678000, "message": "hi"}}})
        self.assertEqual(message.external_id, "dm:+15551112222:1712345678000")


class TestSignalIsADrainNotAPoll(unittest.TestCase):
    def test_it_is_built_on_the_stream_shape(self):
        # Signal has no per-chat history to page; the server queue is the cursor.
        # Both names are imported at module scope on purpose: `test_hermes` drops every
        # `memcal.*` module from sys.modules mid-suite, and a class imported inside this
        # method would be a different object than the one `signal` was built on.
        self.assertTrue(issubclass(signal.SignalSource, StreamSource))


if __name__ == "__main__":
    unittest.main()
