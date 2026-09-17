"""Issue #31: chat streams pass the gate in full; only email stays gated."""

from __future__ import annotations

import unittest

try:
    from tests._support import Base
except ModuleNotFoundError:  # Direct execution: python3 tests/test_gate_chat_streams.py
    from _support import Base

from memcal import gate

CHAT_STREAMS = ("imessage", "whatsapp", "groupme", "slack", "telegram", "signal")


class TestChatShortRepliesPassInFull(Base):
    """The "yeah" / "can't that night" case: short chat replies carry no temporal
    token of their own, so every chat channel passes them without a content test."""

    def test_short_replies_pass_on_every_chat_stream(self):
        self.assertTrue(CHAT_STREAMS, "the chat-channel list must not be empty")
        for channel in CHAT_STREAMS:
            for text in ("yeah", "can't that night"):
                with self.subTest(channel=channel, text=text):
                    verdict = gate.gate_message(text, channel=channel)
                    self.assertTrue(verdict, f"{text!r} on {channel} should pass")
                    self.assertEqual(verdict.reason, f"all-of:{channel}")

    def test_affectionate_noise_passes_on_chat_but_not_email(self):
        for channel in CHAT_STREAMS:
            with self.subTest(channel=channel):
                self.assertTrue(gate.gate_message("i love you", channel=channel))
        self.assertFalse(gate.gate_message("i love you", channel="email"))


class TestBareReactionsPassOnChat(Base):
    """Even a bare reaction passes on chat (issue #31). A lone
    thumbs-up an hour later with no convo around it is still someone saying
    something."""

    def test_bare_emoji_passes_on_every_chat_stream(self):
        self.assertTrue(CHAT_STREAMS, "the chat-channel list must not be empty")
        for channel in CHAT_STREAMS:
            with self.subTest(channel=channel):
                verdict = gate.gate_message("👀", channel=channel)
                self.assertTrue(verdict, "bare 👀 should pass on chat")
                self.assertEqual(verdict.reason, f"all-of:{channel}")

    def test_emoji_beside_words_passes_in_full(self):
        self.assertTrue(CHAT_STREAMS, "the chat-channel list must not be empty")
        for channel in CHAT_STREAMS:
            with self.subTest(channel=channel):
                verdict = gate.gate_message("👀 that place looks good", channel=channel)
                self.assertTrue(verdict)
                self.assertEqual(verdict.reason, f"all-of:{channel}")


class TestBulkEmailStaysGated(Base):
    """The decade-of-newsletters problem: bulk mail is still content-gated."""

    def test_newsletter_body_without_signal_does_not_pass_as_email(self):
        self.assertFalse(gate.gate_message("Huge sale now, shop the new arrivals",
                                           channel="email"))

    def test_bulk_sender_is_read_last_not_excluded(self):
        verdict = gate.gate_email(
            self.conn,
            address="news@retail.example",
            subject="Top stories this week",
            headers={"List-Unsubscribe": "<mailto:leave@retail.example>"},
        )
        self.assertTrue(verdict.low)
        self.assertEqual(verdict.reason, "bulk-headers")


class TestChatStreamsPassInFull(Base):
    """Volume/cost sanity: no chat channel is content-gated; email is not in the set."""

    def test_every_chat_stream_passes_in_full(self):
        self.assertTrue(CHAT_STREAMS, "the chat-channel list must not be empty")
        for channel in CHAT_STREAMS:
            with self.subTest(channel=channel):
                self.assertIn(channel, gate.PASS_ALL_STREAMS)

    def test_email_is_not_a_pass_all_stream(self):
        self.assertNotIn("email", gate.PASS_ALL_STREAMS)
        self.assertNotIn("proton", gate.PASS_ALL_STREAMS)


if __name__ == "__main__":
    unittest.main()
