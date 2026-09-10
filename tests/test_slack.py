"""Slack id markup, conversation naming, and what counts as speech."""

from __future__ import annotations

import unittest

from memcal.sources import slack
from memcal.sources.polled import Conversation


USERS = {"U1": "Ada Lovelace", "U2": "Grace Hopper"}
CHANNELS = {"C9": "plans"}


def text(body, **extra):
    return slack.message_text({"text": body, **extra}, USERS, CHANNELS)


class TestSlackMarkupBecomesReadable(unittest.TestCase):
    def test_a_mention_reads_as_a_name(self):
        self.assertIn("@Ada Lovelace", text("hey <@U1> are you free"))

    def test_a_mention_the_roster_does_not_know_does_not_leak_a_raw_id(self):
        rendered = text("ping <@U404> please")
        self.assertNotIn("U404", rendered)
        self.assertIn("@someone", rendered)

    def test_an_inline_label_beats_the_roster(self):
        self.assertIn("@Ada", text("hey <@U1|Ada> there"))

    def test_a_channel_link_reads_as_a_channel_name(self):
        self.assertIn("#plans", text("posted in <#C9> already"))

    def test_a_link_keeps_the_words_a_human_wrote(self):
        rendered = text("see <https://example.com/x|the agenda> first")
        self.assertIn("the agenda", rendered)
        self.assertNotIn("https://example.com", rendered)

    def test_a_bare_link_survives_as_its_target(self):
        # textclean shortens a bare URL to `<host/path>`; the point here is that Slack's
        # angle-bracket markup is unwrapped first rather than reaching the archive raw.
        self.assertEqual(text("see <https://example.com/x>"), "see <example.com/x>")

    def test_a_broadcast_reads_as_an_address(self):
        self.assertIn("@here", text("<!here> standup in five"))

    def test_slacks_three_escapes_are_undone(self):
        self.assertIn("a < b && c > d", text("a &lt; b &amp;&amp; c &gt; d"))

    def test_a_file_only_message_is_summarised(self):
        self.assertEqual(
            slack.message_text({"text": "", "files": [{"mimetype": "image/png"}]},
                               USERS, CHANNELS), "[image]")

    def test_a_message_with_nothing_in_it_is_empty(self):
        self.assertEqual(slack.message_text({}, USERS, CHANNELS), "")


class TestSlackConversationNaming(unittest.TestCase):
    def source(self):
        source = slack.SlackSource()
        source._me = "U1"
        source._team = "Acme"
        source._users = dict(USERS)
        return source

    def test_a_dm_is_named_for_the_other_person(self):
        found = self.source()._conversation({"id": "D1", "is_im": True, "user": "U2"})
        self.assertEqual(found.name, "Grace Hopper")
        self.assertFalse(found.is_group)

    def test_the_note_to_self_dm_is_not_a_conversation(self):
        self.assertIsNone(
            self.source()._conversation({"id": "D0", "is_im": True, "user": "U1"}))

    def test_a_channel_carries_the_workspace_so_it_cannot_collide(self):
        found = self.source()._conversation({"id": "C1", "name": "general"})
        self.assertEqual(found.name, "Acme #general")
        self.assertTrue(found.is_group)

    def test_a_muted_channel_says_so(self):
        found = self.source()._conversation(
            {"id": "C1", "name": "noise", "is_muted": True})
        self.assertTrue(found.muted)
        self.assertEqual(found.muted_note, "muted in Slack")


class TestWhatSlackCountsAsSpeech(unittest.TestCase):
    def setUp(self):
        self.source = slack.SlackSource()
        self.source._me = "U1"
        self.source._users = dict(USERS)
        self.chat = Conversation(id="C1", name="Acme #general", is_group=True)

    def normalize(self, raw):
        return self.source.normalize(raw, self.chat)

    def test_a_person_speaking_is_archived(self):
        message = self.normalize(
            {"ts": "1712345678.000200", "user": "U2", "text": "lunch at one?"})
        self.assertFalse(message.skip)
        self.assertEqual(message.author_id, "U2")
        self.assertEqual(message.author_name, "Grace Hopper")

    def test_my_own_message_is_mine(self):
        message = self.normalize({"ts": "1712345678.0001", "user": "U1", "text": "yes"})
        self.assertTrue(message.from_me)

    def test_a_join_notice_is_passed_over_rather_than_dropped(self):
        # Dropped would mean re-reading it on every run for the rest of time.
        message = self.normalize(
            {"ts": "1712345678.0001", "subtype": "channel_join", "user": "U2"})
        self.assertTrue(message.skip)
        self.assertEqual(message.cursor, "1712345678.0001")

    def test_an_app_post_is_passed_over(self):
        message = self.normalize({"ts": "1712345678.0001", "bot_id": "B1", "text": "hi"})
        self.assertTrue(message.skip)

    def test_a_message_with_no_timestamp_cannot_be_placed_at_all(self):
        self.assertIsNone(self.normalize({"user": "U2", "text": "hi"}))

    def test_the_external_id_is_scoped_to_its_channel(self):
        # Slack timestamps are unique per channel, not globally.
        message = self.normalize({"ts": "1712345678.0001", "user": "U2", "text": "hi"})
        self.assertEqual(message.external_id, "C1:1712345678.0001")

    def test_the_sort_key_is_the_timestamp(self):
        message = self.normalize({"ts": "1712345678.0002", "user": "U2", "text": "hi"})
        self.assertAlmostEqual(message.order, 1712345678.0002, places=3)


class TestSlackNeedsNoSecondCredential(unittest.TestCase):
    def test_the_token_alias_cannot_collide_with_another_setting(self):
        # A `*_USER_ID` sibling is what makes `Config.secret`'s prefix match pick the
        # wrong value; Slack derives identity from auth_test instead.
        self.assertEqual(slack.SlackSource.secrets, ("SLACK_TOKEN",))


if __name__ == "__main__":
    unittest.main()
