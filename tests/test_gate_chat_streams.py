"""Issue #31: chat streams pass the gate in full; only email stays gated."""

from __future__ import annotations

import unittest

try:
    from tests._support import Base
except ModuleNotFoundError:  # Direct execution: python3 tests/test_gate_chat_streams.py
    from _support import Base

from memcal import gate

CHAT_STREAMS = ("bluebubbles", "imessage", "whatsapp", "groupme", "slack",
                "telegram", "signal")


class TestChatShortRepliesPassInFull(Base):
    """The "yeah" / "can't that night" case: short chat replies carry no temporal
    token of their own, so every chat stream passes them without a content test."""

    def test_short_replies_pass_on_every_chat_stream(self):
        self.assertTrue(CHAT_STREAMS, "the chat-stream list must not be empty")
        for stream in CHAT_STREAMS:
            for text in ("yeah", "can't that night"):
                with self.subTest(stream=stream, text=text):
                    verdict = gate.gate_message(text, stream=stream)
                    self.assertTrue(verdict, f"{text!r} on {stream} should pass")
                    self.assertEqual(verdict.reason, f"all-of:{stream}")

    def test_affectionate_noise_passes_on_chat_but_not_email(self):
        for stream in CHAT_STREAMS:
            with self.subTest(stream=stream):
                self.assertTrue(gate.gate_message("i love you", stream=stream))
        self.assertFalse(gate.gate_message("i love you", stream="email"))


class TestBareReactionsPassOnChat(Base):
    """Even a bare reaction passes on chat (issue #31). A lone
    thumbs-up an hour later with no convo around it is still someone saying
    something."""

    def test_bare_emoji_passes_on_every_chat_stream(self):
        self.assertTrue(CHAT_STREAMS, "the chat-stream list must not be empty")
        for stream in CHAT_STREAMS:
            with self.subTest(stream=stream):
                verdict = gate.gate_message("👀", stream=stream)
                self.assertTrue(verdict, "bare 👀 should pass on chat")
                self.assertEqual(verdict.reason, f"all-of:{stream}")

    def test_emoji_beside_words_passes_in_full(self):
        self.assertTrue(CHAT_STREAMS, "the chat-stream list must not be empty")
        for stream in CHAT_STREAMS:
            with self.subTest(stream=stream):
                verdict = gate.gate_message("👀 that place looks good", stream=stream)
                self.assertTrue(verdict)
                self.assertEqual(verdict.reason, f"all-of:{stream}")


class TestBulkEmailStaysGated(Base):
    """The decade-of-newsletters problem: bulk mail is still content-gated."""

    def test_newsletter_body_without_signal_does_not_pass_as_email(self):
        self.assertFalse(gate.gate_message("Huge sale now, shop the new arrivals",
                                           stream="email"))

    def test_bulk_sender_is_read_last_not_excluded(self):
        verdict = gate.gate_email(
            self.conn,
            address="news@retail.example",
            subject="Top stories this week",
            headers={"List-Unsubscribe": "<mailto:leave@retail.example>"},
        )
        self.assertTrue(verdict.low)
        self.assertEqual(verdict.reason, "bulk-headers")


class TestGatedSetHoldsOnlyEmail(Base):
    """Volume/cost sanity: email is the one gated transport."""

    def test_only_email_streams_are_gated(self):
        self.assertTrue(gate.GATED_STREAMS)
        self.assertLessEqual(set(gate.GATED_STREAMS), {"email", "proton"})

    def test_no_chat_stream_is_gated(self):
        self.assertTrue(CHAT_STREAMS, "the chat-stream list must not be empty")
        for stream in CHAT_STREAMS:
            with self.subTest(stream=stream):
                self.assertIn(stream, gate.PASS_ALL_STREAMS)
                self.assertNotIn(stream, gate.GATED_STREAMS)


if __name__ == "__main__":
    unittest.main()
