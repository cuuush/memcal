"""The small graph used to put amendable events beside a conversation."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from memcal import db, events, trace
from memcal.config import Config


class RelevantEventGraphTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.conn: sqlite3.Connection = db.open_db(self.cfg.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def event(self, title: str, offset: int, **fields):
        return events.upsert(
            self.conn,
            {
                "date": (db.today() + timedelta(days=offset)).isoformat(),
                "title": title,
                **fields,
            },
            written_by="dream:nightly",
        )[0]

    def link(self, event, entity: str):
        trace.stamp(
            self.conn,
            kind="event",
            ref=event.key,
            verb="opened",
            entity=entity,
            stage="propose",
            run_id=1,
        )

    def test_same_channel_keeps_three_days_of_history_and_upcoming_events(self):
        entity = "thread:groupme:Movie Night"
        recent = self.event("Movie last Monday", -3, status="happened")
        old = self.event("Movie before that", -4, status="happened")
        upcoming = self.event("Movie in September", 60, status="confirmed")
        for event in (recent, old, upcoming):
            self.link(event, entity)

        same, related = events.amendable_groups(
            self.conn,
            people=[],
            entity=entity,
            text="what time was that?",
        )
        self.assertEqual({event.key for event in same}, {recent.key, upcoming.key})
        self.assertEqual(related, [])

    def test_an_ongoing_span_survives_even_if_it_started_longer_ago(self):
        entity = "thread:imessage:Family"
        trip = self.event(
            "Quinn visiting",
            -8,
            until=(db.today() + timedelta(days=1)).isoformat(),
            status="confirmed",
        )
        self.link(trip, entity)

        same, _related = events.amendable_groups(
            self.conn,
            people=[],
            entity=entity,
        )
        self.assertEqual([event.key for event in same], [trip.key])

    def test_a_dm_reaches_an_event_through_its_other_person(self):
        movie = self.event(
            "Movie night",
            4,
            participants=["Quinn Brooks", "Jamie North"],
            status="confirmed",
        )

        same, related = events.amendable_groups(
            self.conn,
            people=["me", "Quinn Brooks"],
            entity="person:Quinn Brooks",
            text="I can't make the movie",
        )
        self.assertEqual(same, [])
        self.assertEqual([event.key for event in related], [movie.key])

    def test_a_folded_chat_id_keeps_the_original_channels_provenance(self):
        canonical = "thread:imessage:new-chat"
        old_id = "thread:imessage:old-chat"
        dinner = self.event("Dinner", 2, status="confirmed")
        self.link(dinner, old_id)

        same, _related = events.amendable_groups(
            self.conn,
            people=[],
            entity=canonical,
            entities=[old_id],
        )
        self.assertEqual([event.key for event in same], [dinner.key])


if __name__ == "__main__":
    unittest.main()
