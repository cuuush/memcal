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

    def test_an_incoming_dm_is_keyed_on_the_peer_and_carries_their_name(self):
        chat, message = self.source.normalize({"envelope": {
            "source": "+15551112222", "sourceNumber": "+15551112222",
            "sourceName": "Ada", "timestamp": 1712345678000,
            "dataMessage": {"timestamp": 1712345678000, "message": "lunch at one?"}}})
        # The thread is keyed on the peer, not their display name: the name rides on the
        # message so identity resolution (not the thread key) renders "Ada".
        self.assertEqual(chat.id, "dm:+15551112222")
        self.assertEqual(chat.name, "+15551112222")
        self.assertFalse(chat.is_group)
        self.assertFalse(message.from_me)
        self.assertEqual(message.author_id, "+15551112222")
        self.assertEqual(message.author_name, "Ada")

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

    def test_a_message_i_sent_elsewhere_is_mine_and_keyed_on_the_recipient(self):
        # The sent envelope carries *my* sourceName; the thread must still key on the
        # recipient, or my outgoing half of the DM would file under my own name.
        chat, message = self.source.normalize({"envelope": {
            "source": self.source._me, "sourceName": "Me Myself",
            "timestamp": 1712345678000,
            "syncMessage": {"sentMessage": {
                "timestamp": 1712345678000, "destination": "+15551112222",
                "message": "on my way"}}}})
        self.assertTrue(message.from_me)
        self.assertEqual(chat.id, "dm:+15551112222")
        self.assertEqual(chat.name, "+15551112222")
        self.assertEqual(message.author_id, "")

    def test_a_dm_is_one_thread_in_both_directions(self):
        incoming, _ = self.source.normalize({"envelope": {
            "source": "+15551112222", "sourceNumber": "+15551112222",
            "sourceName": "Ada", "timestamp": 1,
            "dataMessage": {"timestamp": 1, "message": "hi"}}})
        outgoing, _ = self.source.normalize({"envelope": {
            "source": self.source._me, "sourceName": "Me Myself", "timestamp": 2,
            "syncMessage": {"sentMessage": {
                "timestamp": 2, "destination": "+15551112222", "message": "yo"}}}})
        self.assertEqual(incoming.name, outgoing.name)
        self.assertEqual(incoming.id, outgoing.id)

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


class TestSignalLoginPrintsTheQrCode(unittest.TestCase):
    def cfg(self):
        import tempfile
        from pathlib import Path
        from memcal.config import Config
        return Config(home=Path(tempfile.mkdtemp(prefix="memcal-test-")), env={})

    def source_with(self, accounts=("+111",), binary="/bin/signal-cli"):
        from types import SimpleNamespace
        source = signal.SignalSource()
        source._cli = lambda cfg: SimpleNamespace(binary=binary,
                                                  accounts=lambda: list(accounts))
        return source

    def test_a_clean_link_reports_success(self):
        from types import SimpleNamespace
        from unittest import mock
        with mock.patch.object(signal.subprocess, "run",
                               return_value=SimpleNamespace(returncode=0)) as run:
            ok, message = self.source_with().setup(self.cfg())
        self.assertTrue(ok)
        self.assertIn("memcal sources", message)
        # No capture: the QR code must render live in the user's terminal.
        self.assertNotIn("capture_output", run.call_args.kwargs)

    def test_a_failed_link_says_to_try_again(self):
        from types import SimpleNamespace
        from unittest import mock
        with mock.patch.object(signal.subprocess, "run",
                               return_value=SimpleNamespace(returncode=1)):
            ok, message = self.source_with().setup(self.cfg())
        self.assertFalse(ok)
        self.assertIn("memcal login signal", message)

    def test_ctrl_c_is_a_clean_cancel(self):
        from unittest import mock
        with mock.patch.object(signal.subprocess, "run",
                               side_effect=KeyboardInterrupt):
            ok, message = self.source_with().setup(self.cfg())
        self.assertFalse(ok)
        self.assertIn("cancelled", message)

    def test_several_accounts_offer_a_choice_and_save_it(self):
        from types import SimpleNamespace
        from unittest import mock
        import memcal.sources.polled as polled
        cfg = self.cfg()
        source = self.source_with(accounts=("+111", "+222"))
        with mock.patch.object(signal.subprocess, "run",
                               return_value=SimpleNamespace(returncode=0)), \
             mock.patch.object(polled, "ask", return_value="2"), \
             mock.patch.object(polled, "save_credential") as save:
            ok, message = source.setup(cfg)
        self.assertTrue(ok)
        save.assert_called_once_with(cfg, "SIGNAL_ACCOUNT", "+222")
        self.assertIn("+222", message)

    def test_one_account_needs_no_choice(self):
        from types import SimpleNamespace
        from unittest import mock
        import memcal.sources.polled as polled
        cfg = self.cfg()
        source = self.source_with(accounts=("+111",))
        with mock.patch.object(signal.subprocess, "run",
                               return_value=SimpleNamespace(returncode=0)), \
             mock.patch.object(polled, "save_credential") as save:
            ok, _message = source.setup(cfg)
        self.assertTrue(ok)
        save.assert_not_called()


class TestSignalCheckMatchesIngest(unittest.TestCase):
    """`check()` must not report usable where `connect()`/ingest will refuse."""

    def cfg(self):
        import tempfile
        from pathlib import Path
        from memcal.config import Config
        return Config(home=Path(tempfile.mkdtemp(prefix="memcal-test-")), env={})

    def source_with(self, accounts, account=""):
        from types import SimpleNamespace
        source = signal.SignalSource()
        source._cli = lambda cfg: SimpleNamespace(
            binary="/bin/signal-cli", account=account,
            accounts=lambda: list(accounts), groups=lambda: {})
        return source

    def test_several_linked_accounts_and_no_choice_reads_as_not_usable(self):
        # connect() raises here; a green check() would leave doctor lying.
        ok, message = self.source_with(("+111", "+222")).check(self.cfg())
        self.assertFalse(ok)
        self.assertIn("signal_account", message)

    def test_a_chosen_account_disambiguates(self):
        ok, message = self.source_with(("+111", "+222"), account="+222").check(self.cfg())
        self.assertTrue(ok)
        self.assertIn("+222", message)

    def test_a_single_linked_account_is_usable(self):
        ok, message = self.source_with(("+111",)).check(self.cfg())
        self.assertTrue(ok)
        self.assertIn("+111", message)


if __name__ == "__main__":
    unittest.main()
