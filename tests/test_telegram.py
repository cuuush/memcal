"""Telegram naming, and what its message objects mean."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from memcal.sources import telegram
from memcal.sources.polled import Conversation


class TestTelegramNaming(unittest.TestCase):
    def test_a_group_is_called_by_its_title(self):
        self.assertEqual(telegram._entity_name(SimpleNamespace(title="Board Games")),
                         "Board Games")

    def test_a_person_is_called_by_their_name(self):
        who = SimpleNamespace(title=None, first_name="Ada", last_name="Lovelace")
        self.assertEqual(telegram._entity_name(who), "Ada Lovelace")

    def test_a_first_name_alone_is_enough(self):
        who = SimpleNamespace(title=None, first_name="Ada", last_name=None)
        self.assertEqual(telegram._entity_name(who), "Ada")

    def test_a_username_is_the_last_resort(self):
        who = SimpleNamespace(title=None, first_name=None, last_name=None,
                              username="ada")
        self.assertEqual(telegram._entity_name(who), "@ada")

    def test_whitespace_is_not_part_of_a_name(self):
        self.assertEqual(telegram._entity_name(SimpleNamespace(title="  Board   Games ")),
                         "Board Games")


class TestTelegramMessageText(unittest.TestCase):
    def test_words_win(self):
        self.assertIn("dinner", telegram.message_text(
            SimpleNamespace(message="dinner on Friday", media=None)))

    def test_media_with_no_words_is_summarised(self):
        class MessageMediaPhoto:
            pass
        rendered = telegram.message_text(
            SimpleNamespace(message="", media=MessageMediaPhoto()))
        self.assertEqual(rendered, "[photo]")

    def test_nothing_at_all_is_empty(self):
        self.assertEqual(
            telegram.message_text(SimpleNamespace(message="", media=None)), "")


class TestTelegramNormalizing(unittest.TestCase):
    def setUp(self):
        self.source = telegram.TelegramSource()
        self.source._me = "77"
        self.chat = Conversation(id="42", name="Board Games", is_group=True)

    def message(self, **kw):
        base = dict(id=5, action=None, message="lunch?", media=None, out=False,
                    sender_id=99, sender=SimpleNamespace(title=None, first_name="Ada",
                                                         last_name=None),
                    date=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc))
        base.update(kw)
        return SimpleNamespace(**base)

    def test_a_real_message_is_archived(self):
        message = self.source.normalize(self.message(), self.chat)
        self.assertFalse(message.skip)
        self.assertEqual(message.author_name, "Ada")
        self.assertEqual(message.external_id, "42:5")

    def test_a_service_message_is_passed_over_rather_than_dropped(self):
        message = self.source.normalize(self.message(action=object()), self.chat)
        self.assertTrue(message.skip)
        self.assertEqual(message.cursor, "5")

    def test_my_own_message_is_mine_by_the_out_flag(self):
        self.assertTrue(self.source.normalize(self.message(out=True), self.chat).from_me)

    def test_my_own_message_is_mine_by_sender_id_too(self):
        message = self.source.normalize(self.message(sender_id=77), self.chat)
        self.assertTrue(message.from_me)

    def test_the_sort_key_is_the_message_id(self):
        # Telegram ids increase per chat, which is exactly what the watermark needs.
        self.assertEqual(self.source.normalize(self.message(id=12), self.chat).order, 12.0)

    def test_a_naive_timestamp_is_read_as_utc_not_local(self):
        naive = datetime(2026, 3, 1, 12, 0)
        message = self.source.normalize(self.message(date=naive), self.chat)
        self.assertTrue(message.ts)


if __name__ == "__main__":
    unittest.main()
