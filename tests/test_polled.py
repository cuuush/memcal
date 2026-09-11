"""Invariants PolledSource and StreamSource guarantee to every platform built on them.

These used to be reimplemented per connector. Platform tests cover their own parsing and
rely on these for the loop.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta, timezone
from pathlib import Path

from memcal import db
from memcal.config import Config
from memcal.sources import base
from memcal.sources.polled import (Conversation, Message, PolledSource, SourceError,
                                   StreamSource)


class Boom(Exception):
    """A platform failure that is not a rate limit."""


class Throttled(Exception):
    """A platform failure that is."""


class FakeChat(PolledSource):
    """A platform that does exactly what the test tells it to."""

    name = "faketalk"
    description = "a platform that exists only in this file"
    page = 3
    noun = "chats"

    def __init__(self, conversations, history, *, my_id="me-1"):
        super().__init__()
        self._conversations = conversations
        self._history = history
        self._my_id = my_id
        self.asked: list[tuple[str, str | None, int]] = []

    def connect(self, cfg):
        return object()

    def identify(self, conn, client, report):
        return self._my_id

    def conversations(self, client):
        return list(self._conversations)

    def history(self, client, conversation, since, limit):
        self.asked.append((conversation.id, since, limit))
        page = self._history.get(conversation.id, [])
        if page and isinstance(page[0], Exception):
            raise page[0]
        after = [m for m in page if since is None or m["id"] > since]
        return after[:limit]

    def normalize(self, raw, conversation):
        if raw.get("skip"):
            return Message.passed_over(str(raw["id"]), float(raw["id"]))
        if raw.get("garbled"):
            return None
        return Message(
            external_id=f"{conversation.id}:{raw['id']}",
            ts=raw.get("ts") or db.now(),
            text=raw.get("text", "let us meet on Thursday at four"),
            order=float(raw["id"]),
            author_id=raw.get("author", "them-1"),
            author_name=raw.get("name", "Some Body"),
            from_me=raw.get("author") == self._my_id,
            cursor=str(raw["id"]),
        )

    def is_rate_limit(self, exc):
        return isinstance(exc, Throttled)


def chat(chat_id: str, *, days_ago: int = 1, newest: str = "", group: bool = False):
    return Conversation(
        id=chat_id,
        name=f"Chat {chat_id}",
        is_group=group,
        newest=newest,
        recency=float(1000 - days_ago),
        last_activity=db.now_dt().astimezone(timezone.utc) - timedelta(days=days_ago),
    )


class PolledCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def run_source(self, source, limit=100):
        return source.run(self.conn, self.cfg, limit=limit)

    def watermark(self, source, chat_id):
        return base.watermark(self.conn, f"{source.name}.{chat_id}", "")

    def archived_ids(self, stream="faketalk"):
        return [r["external_id"] for r in self.conn.execute(
            "SELECT external_id FROM archive WHERE stream = ? ORDER BY id", (stream,))]


class TestTheWatermarkAdvancesSafely(PolledCase):
    def test_it_lands_on_the_newest_message_not_the_last_one_returned(self):
        # The platform answers newest-first, as most REST APIs do.
        source = FakeChat([chat("a")], {"a": [{"id": "30"}, {"id": "10"}, {"id": "20"}]})
        report = self.run_source(source)
        self.assertIsNone(report.error)
        self.assertEqual(self.watermark(source, "a"), "30")

    def test_messages_are_delivered_oldest_first_whatever_order_the_platform_used(self):
        source = FakeChat([chat("a")], {"a": [{"id": "30"}, {"id": "10"}, {"id": "20"}]})
        self.run_source(source)
        self.assertEqual(self.archived_ids(), ["a:10", "a:20", "a:30"])

    def test_a_message_passed_over_still_advances_past_it(self):
        # Otherwise every run re-reads the same system notice forever.
        source = FakeChat([chat("a")], {"a": [{"id": "10"}, {"id": "20", "skip": True}]})
        self.run_source(source)
        self.assertEqual(self.watermark(source, "a"), "20")
        self.assertEqual(self.archived_ids(), ["a:10"])

    def test_an_unparseable_message_is_left_behind_the_watermark(self):
        # None means "I could not make sense of this", so a later fix can still reach it.
        source = FakeChat([chat("a")], {"a": [{"id": "10"}, {"id": "20", "garbled": 1}]})
        self.run_source(source)
        self.assertEqual(self.watermark(source, "a"), "10")

    def test_a_second_run_asks_only_for_what_came_after(self):
        source = FakeChat([chat("a")], {"a": [{"id": "10"}, {"id": "20"}]})
        self.run_source(source)
        source.asked.clear()
        self.run_source(source)
        self.assertEqual([since for _id, since, _limit in source.asked], ["20"])


class TestRequestsAreNotWasted(PolledCase):
    def test_a_chat_whose_newest_id_is_already_the_watermark_is_never_read(self):
        source = FakeChat([chat("a", newest="20")], {"a": [{"id": "10"}, {"id": "20"}]})
        self.run_source(source)
        source.asked.clear()
        self.run_source(source)
        self.assertEqual(source.asked, [])

    def test_a_dormant_chat_is_skipped_on_a_first_run(self):
        source = FakeChat([chat("old", days_ago=400), chat("new", days_ago=2)],
                          {"old": [{"id": "1"}], "new": [{"id": "2"}]})
        report = self.run_source(source)
        self.assertEqual([c for c, _s, _l in source.asked], ["new"])
        self.assertIn("1 dormant skipped on initial load", " ".join(report.notes))

    def test_the_dormant_cutoff_applies_only_when_there_is_no_watermark(self):
        # Once a chat is known, silence is not a reason to stop following it.
        source = FakeChat([chat("a", days_ago=2)], {"a": [{"id": "1"}]})
        self.run_source(source)
        source._conversations = [chat("a", days_ago=400)]
        source.asked.clear()
        self.run_source(source)
        self.assertEqual([c for c, _s, _l in source.asked], ["a"])

    def test_a_chat_with_unknown_last_activity_is_read_rather_than_dropped(self):
        unknown = chat("a")
        unknown.last_activity = None
        source = FakeChat([unknown], {"a": [{"id": "1"}]})
        self.run_source(source)
        self.assertEqual([c for c, _s, _l in source.asked], ["a"])


class TestTheBudgetIsSpentOnLiveConversation(PolledCase):
    def test_the_newest_chats_are_read_first(self):
        source = FakeChat(
            [chat("stale", days_ago=20), chat("live", days_ago=1)],
            {"stale": [{"id": "1"}], "live": [{"id": "2"}]})
        self.run_source(source)
        self.assertEqual([c for c, _s, _l in source.asked], ["live", "stale"])

    def test_an_exhausted_budget_stops_and_asks_for_another_round(self):
        source = FakeChat([chat("a", days_ago=1), chat("b", days_ago=2)],
                          {"a": [{"id": "1"}, {"id": "2"}], "b": [{"id": "3"}]})
        report = self.run_source(source, limit=2)
        self.assertTrue(report.more)
        self.assertEqual([c for c, _s, _l in source.asked], ["a"])

    def test_a_full_page_reports_more_waiting(self):
        source = FakeChat([chat("a")],
                          {"a": [{"id": str(n)} for n in range(10, 40, 10)]})
        report = self.run_source(source, limit=100)
        self.assertTrue(report.more)          # page is 3, and 3 came back

    def test_a_short_page_does_not(self):
        source = FakeChat([chat("a")], {"a": [{"id": "10"}]})
        self.assertFalse(self.run_source(source, limit=100).more)


class TestOneBadChatCannotStopTheRun(PolledCase):
    def test_an_unreadable_chat_is_noted_and_the_rest_are_read(self):
        source = FakeChat([chat("bad", days_ago=1), chat("good", days_ago=2)],
                          {"bad": [Boom("403 no access")], "good": [{"id": "5"}]})
        report = self.run_source(source)
        self.assertIsNone(report.error)
        self.assertEqual(self.archived_ids(), ["good:5"])
        self.assertIn("403 no access", " ".join(report.notes))

    def test_the_failed_chat_keeps_no_watermark(self):
        source = FakeChat([chat("bad")], {"bad": [Boom("nope")]})
        self.run_source(source)
        self.assertEqual(self.watermark(source, "bad"), "")

    def test_a_rate_limit_stops_the_round_and_asks_for_another(self):
        source = FakeChat([chat("a", days_ago=1), chat("b", days_ago=2)],
                          {"a": [Throttled("429")], "b": [{"id": "5"}]})
        report = self.run_source(source)
        self.assertTrue(report.more)
        self.assertEqual(self.archived_ids(), [])       # b was never reached
        self.assertIn("rate limited", " ".join(report.notes))

    def test_a_connect_failure_is_reported_cleanly_rather_than_raised(self):
        class Broken(FakeChat):
            def connect(self, cfg):
                raise SourceError("no credential")

        report = self.run_source(Broken([], {}))
        self.assertEqual(report.error, "no credential")


class TestWhatTheRestOfMemcalSees(PolledCase):
    def test_every_conversation_is_recorded_as_a_thread(self):
        source = FakeChat([chat("a", group=True)], {"a": [{"id": "1"}]})
        self.run_source(source)
        row = self.conn.execute(
            "SELECT thread, is_group FROM threads WHERE stream = 'faketalk'").fetchone()
        self.assertEqual(row["thread"], "Chat a")
        self.assertEqual(row["is_group"], 1)

    def test_a_thread_is_recorded_even_when_it_has_nothing_new(self):
        # `memcal who` should still know the chat exists.
        source = FakeChat([chat("a", newest="9")], {"a": [{"id": "9"}]})
        base.set_watermark(self.conn, "faketalk.a", "9")
        self.run_source(source)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM threads WHERE stream = 'faketalk'").fetchone())

    def test_my_own_message_is_stored_as_mine(self):
        source = FakeChat([chat("a")], {"a": [{"id": "1", "author": "me-1"}]})
        self.run_source(source)
        row = self.conn.execute(
            "SELECT from_me, person, handle FROM archive WHERE stream='faketalk'"
        ).fetchone()
        self.assertEqual(row["from_me"], 1)
        self.assertEqual(row["person"], "me")
        self.assertIsNone(row["handle"])

    def test_someone_elses_message_carries_a_prefixed_handle(self):
        source = FakeChat([chat("a")], {"a": [{"id": "1", "author": "u9"}]})
        self.run_source(source)
        row = self.conn.execute(
            "SELECT from_me, handle FROM archive WHERE stream='faketalk'").fetchone()
        self.assertEqual(row["from_me"], 0)
        self.assertEqual(row["handle"], "faketalk:u9")

    def test_an_empty_message_is_never_archived(self):
        source = FakeChat([chat("a")], {"a": [{"id": "1", "text": "   "}]})
        self.run_source(source)
        self.assertEqual(self.archived_ids(), [])

    def test_the_watermark_still_advances_past_an_empty_message(self):
        source = FakeChat([chat("a")], {"a": [{"id": "1", "text": "   "}]})
        self.run_source(source)
        self.assertEqual(self.watermark(source, "a"), "1")


# ------------------------------------------------------------------- stream --

class FakeQueue(StreamSource):
    name = "fakequeue"
    description = "a drain-shaped platform"

    def __init__(self, items):
        super().__init__()
        self._items = items
        self.asked: list[str | None] = []

    def connect(self, cfg):
        return object()

    def stream(self, client, since, limit):
        self.asked.append(since)
        return list(self._items)[:limit]

    def normalize(self, raw):
        # A drain has no watermark to advance past, so there is nothing to pass over:
        # the queue itself is the cursor and the item is already gone from it.
        if raw.get("skip"):
            return None
        conversation = Conversation(id=raw["chat"], name=f"Chat {raw['chat']}",
                                    is_group=raw.get("group", False))
        return conversation, Message(
            external_id=f"{raw['chat']}:{raw['id']}",
            ts=db.now(),
            text=raw.get("text", "dinner on Friday at seven"),
            order=float(raw["id"]),
            author_id=raw.get("author", "them"),
            cursor=str(raw["id"]),
        )


class TestTheDrainShape(PolledCase):
    def test_every_item_is_archived_under_the_chat_it_names(self):
        source = FakeQueue([{"id": "1", "chat": "a"}, {"id": "2", "chat": "b"}])
        report = source.run(self.conn, self.cfg, limit=100)
        self.assertIsNone(report.error)
        self.assertEqual(self.archived_ids("fakequeue"), ["a:1", "b:2"])

    def test_each_chat_is_recorded_once_as_a_thread(self):
        source = FakeQueue([{"id": "1", "chat": "a"}, {"id": "2", "chat": "a"}])
        source.run(self.conn, self.cfg, limit=100)
        rows = self.conn.execute(
            "SELECT thread FROM threads WHERE stream = 'fakequeue'").fetchall()
        self.assertEqual([r["thread"] for r in rows], ["Chat a"])

    def test_the_single_cursor_advances_to_the_last_item(self):
        source = FakeQueue([{"id": "1", "chat": "a"}, {"id": "7", "chat": "b"}])
        source.run(self.conn, self.cfg, limit=100)
        self.assertEqual(base.watermark(self.conn, "fakequeue.cursor", ""), "7")

    def test_a_full_drain_asks_for_another_round(self):
        source = FakeQueue([{"id": str(n), "chat": "a"} for n in range(5)])
        self.assertTrue(source.run(self.conn, self.cfg, limit=5).more)

    def test_an_item_normalize_declines_is_simply_passed_over(self):
        source = FakeQueue([{"id": "1", "chat": "a", "skip": True},
                            {"id": "2", "chat": "a"}])
        source.run(self.conn, self.cfg, limit=100)
        self.assertEqual(self.archived_ids("fakequeue"), ["a:2"])


if __name__ == "__main__":
    unittest.main()
