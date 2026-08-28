"""Focused tests for the GroupMe connector's request budget."""

from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from memcal import archive, db, identity, threads
from memcal.config import Config
from memcal.sources import base, groupme


def rate_limit_error(retry_after: str | None = None, code: int = 429) -> base.HttpError:
    class Cause(Exception):
        pass

    cause = Cause("Rate Limited")
    cause.code = code
    cause.headers = {"Retry-After": retry_after} if retry_after is not None else {}
    error = base.HttpError(f"HTTP {code} from GroupMe")
    error.__cause__ = cause
    return error


class TestGroupMeRateLimitBackoff(unittest.TestCase):
    def client(self) -> groupme.GroupMe:
        client = object.__new__(groupme.GroupMe)
        client.token = "secret"
        client._rate_lock = threading.Lock()
        client._rate_until = 0.0
        client._retry_lock = threading.Lock()
        return client

    def test_429_retries_with_exponential_full_jitter(self):
        clock = [100.0]
        sleeps: list[float] = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        replies = [
            rate_limit_error(),
            rate_limit_error(),
            {"response": {"ok": True}},
        ]
        with (
            mock.patch.object(groupme.time, "monotonic", side_effect=lambda: clock[0]),
            mock.patch.object(groupme.time, "sleep", side_effect=sleep),
            mock.patch.object(groupme.random, "uniform", side_effect=[0.5, 1.0]),
            mock.patch.object(base, "get_json", side_effect=replies) as get,
        ):
            result = self.client()._get("groups")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(sleeps, [1.0, 2.0])
        self.assertEqual(get.call_count, 3)

    def test_groupmes_documented_420_is_rate_limited_too(self):
        clock = [100.0]

        def sleep(seconds):
            clock[0] += seconds

        with (
            mock.patch.object(groupme.time, "monotonic", side_effect=lambda: clock[0]),
            mock.patch.object(groupme.time, "sleep", side_effect=sleep),
            mock.patch.object(groupme.random, "uniform", return_value=0.0),
            mock.patch.object(
                base, "get_json",
                side_effect=[rate_limit_error(code=420), {"response": []}],
            ) as get,
        ):
            self.client()._get("groups")

        self.assertEqual(get.call_count, 2)

    def test_retry_after_is_a_minimum_and_is_capped(self):
        clock = [100.0]

        def sleep(seconds):
            clock[0] += seconds

        with (
            mock.patch.object(groupme.time, "monotonic", side_effect=lambda: clock[0]),
            mock.patch.object(groupme.time, "sleep", side_effect=sleep) as wait,
            mock.patch.object(groupme.random, "uniform", return_value=0.25),
            mock.patch.object(
                base, "get_json",
                side_effect=[rate_limit_error("9999"), {"response": []}],
            ),
        ):
            self.client()._get("groups")

        wait.assert_called_once_with(groupme.RATE_LIMIT_MAX_RETRY_AFTER)

    def test_exhausted_retries_still_surface_429_to_stop_the_round(self):
        clock = [100.0]

        def sleep(seconds):
            clock[0] += seconds

        errors = [rate_limit_error() for _ in range(groupme.RATE_LIMIT_RETRIES + 1)]
        with (
            mock.patch.object(groupme.time, "monotonic", side_effect=lambda: clock[0]),
            mock.patch.object(groupme.time, "sleep", side_effect=sleep),
            mock.patch.object(groupme.random, "uniform", return_value=0.0),
            mock.patch.object(base, "get_json", side_effect=errors) as get,
        ):
            with self.assertRaisesRegex(base.HttpError, "HTTP 429"):
                self.client()._get("groups")

        self.assertEqual(get.call_count, groupme.RATE_LIMIT_RETRIES + 1)


class TestGroupMeInitialLoad(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    @staticmethod
    def group(group_id: str, days_ago: int) -> dict:
        created = int((db.now_dt() - timedelta(days=days_ago)).timestamp())
        return {
            "id": group_id,
            "name": f"group {group_id}",
            "messages": {
                "last_message_id": f"message-{group_id}",
                "last_message_created_at": created,
            },
        }

    def run_with(self, groups: list[dict]):
        calls: list[tuple[str, str | None]] = []

        class Client:
            def me(self):
                return {}

            def groups(self):
                return groups

            def group(self, group_id):
                source = next(group for group in groups if group["id"] == group_id)
                return {**source, "members": []}

            def group_messages(self, group_id, since_id, limit=groupme.PAGE):
                calls.append((group_id, since_id))
                source = next(group for group in groups if group["id"] == group_id)
                return [{
                    "id": source["messages"]["last_message_id"],
                    "created_at": source["messages"]["last_message_created_at"],
                    "text": "poker tomorrow at 8",
                    "user_id": "friend",
                    "name": "Friend",
                }]

            def chats(self):
                return []

        with mock.patch.object(groupme, "GroupMe", return_value=Client()):
            report = groupme.ingest(self.conn, self.cfg, limit=500)
        return report, calls

    def test_initial_load_fetches_only_groups_active_in_last_30_days(self):
        report, calls = self.run_with([
            self.group("recent", 5),
            self.group("boundary", 30),
            self.group("dormant", 31),
        ])

        self.assertEqual({group_id for group_id, _since in calls}, {"recent", "boundary"})
        self.assertEqual(
            {row["thread"] for row in archive.search(self.conn, "poker")},
            {"group recent", "group boundary"},
        )
        self.assertTrue(any("1 dormant groups skipped" in note for note in report.notes))

    def test_cutoff_only_applies_when_the_group_has_no_watermark(self):
        dormant = self.group("dormant", 90)
        base.set_watermark(self.conn, "groupme.group.dormant", "older-message")

        _report, calls = self.run_with([dormant])

        self.assertEqual(calls, [("dormant", "older-message")])


class TestGroupMeProfileResolution(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def seed(self, *, thread: str = "Ravers", gated: bool = True,
             person: str | None = None) -> None:
        archive.append(
            self.conn, stream="groupme", external_id=f"old-{thread}",
            ts=db.now(), text="festival tomorrow", thread=thread,
            handle="groupme:42", person=person, from_me=False,
            meta={"seen_name": "Q-Money"}, gated=gated,
        )
        base.set_watermark(self.conn, f"groupme.group.{thread}", f"message-{thread}")
        self.conn.commit()

    @staticmethod
    def summary(group_id: str = "Ravers", thread: str = "Ravers") -> dict:
        return {
            "id": group_id,
            "name": thread,
            "messages": {
                "last_message_id": f"message-{group_id}",
                "last_message_created_at": int(db.now_dt().timestamp()),
            },
        }

    @staticmethod
    def detail(group_id: str = "Ravers", thread: str = "Ravers",
               account_name: str = "Quinn Brooks") -> dict:
        return {
            "id": group_id,
            "name": thread,
            "members": [{
                "user_id": "42",
                "nickname": "Q-Money",
                "name": account_name,
            }],
        }

    def run_with(self, summary: dict, detail: dict) -> list[str]:
        detail_calls: list[str] = []

        class Client:
            def me(self):
                return {"id": "me", "name": "Casey"}

            def groups(self):
                return [summary]

            def group(self, group_id):
                detail_calls.append(group_id)
                return detail

            def group_messages(self, group_id, since_id, limit=groupme.PAGE):
                return []

            def chats(self):
                return []

        with mock.patch.object(groupme, "GroupMe", return_value=Client()):
            groupme.ingest(self.conn, self.cfg, limit=500)
        return detail_calls

    def test_existing_gated_archive_is_repaired_without_replaying_messages(self):
        self.seed()
        summary = self.summary()

        calls = self.run_with(summary, self.detail())

        self.assertEqual(calls, ["Ravers"])
        row = self.conn.execute(
            "SELECT person, meta FROM archive WHERE external_id = 'old-Ravers'"
        ).fetchone()
        self.assertEqual(row["person"], "Quinn Brooks")
        self.assertEqual(db.jload(row["meta"], {})["seen_name"], "Q-Money")
        self.assertEqual(identity.resolve(self.conn, "groupme:42"), "Quinn Brooks")
        self.assertEqual(
            self.conn.execute(
                "SELECT name FROM groupme_profiles WHERE user_id = '42'"
            ).fetchone()["name"],
            "Quinn Brooks",
        )
        self.assertEqual(
            set(threads.names_for_handle(self.conn, "groupme:42")), {"Q-Money"}
        )

        # Even if the group has a new message, a fresh roster with no unknown gated
        # speaker is free; the full roster is not tied to every message watermark.
        changed = self.summary()
        changed["messages"]["last_message_id"] = "message-Ravers-next"
        self.assertEqual(self.run_with(changed, self.detail()), [])

    def test_new_gated_unknown_speaker_invalidates_a_fresh_roster(self):
        self.seed()
        summary = self.summary()
        self.run_with(summary, self.detail())
        archive.append(
            self.conn, stream="groupme", external_id="new-speaker",
            ts="9999-01-01T00:00:00+00:00", text="tomorrow?",
            thread="Ravers", handle="groupme:99", from_me=False,
            meta={"seen_name": "New Kid"}, gated=True,
        )
        detail = {
            **self.detail(),
            "members": [
                *self.detail()["members"],
                {"user_id": "99", "nickname": "New Kid", "name": "Nina Smith"},
            ],
        }

        self.assertEqual(self.run_with(summary, detail), ["Ravers"])
        self.assertEqual(identity.resolve(self.conn, "groupme:99"), "Nina Smith")

    def test_ungated_and_muted_groups_do_not_fetch_profile_details(self):
        self.seed(thread="Noise", gated=False)
        self.assertEqual(
            self.run_with(self.summary("Noise", "Noise"),
                          self.detail("Noise", "Noise")),
            [],
        )

        self.conn.execute("UPDATE archive SET gated = 1 WHERE thread = 'Noise'")
        threads.record(self.conn, "groupme", "Noise", is_group=True)
        threads.decide(self.conn, "groupme", "Noise", "mute")
        self.assertEqual(
            self.run_with(self.summary("Noise", "Noise"),
                          self.detail("Noise", "Noise")),
            [],
        )

    def test_contacts_identity_wins_while_groupme_cache_still_refreshes(self):
        self.seed(person="Quinn B.")
        identity.link(self.conn, "groupme:42", "Quinn B.", source="contacts")

        self.run_with(self.summary(), self.detail(account_name="Quinn Brooks"))

        self.assertEqual(identity.resolve(self.conn, "groupme:42"), "Quinn B.")
        self.assertEqual(
            self.conn.execute(
                "SELECT person FROM archive WHERE external_id = 'old-Ravers'"
            ).fetchone()["person"],
            "Quinn B.",
        )
        self.assertEqual(
            self.conn.execute(
                "SELECT name FROM groupme_profiles WHERE user_id = '42'"
            ).fetchone()["name"],
            "Quinn Brooks",
        )


if __name__ == "__main__":
    unittest.main()
