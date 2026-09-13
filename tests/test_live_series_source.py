"""The live series source is a pointer, not the writer's name.

`series.source` answers "where did this come from" the same way `events.source`
does elsewhere (`thread:…`, `person:…`, `ical:…`). It once said `"agent:live"`,
which answers "what code wrote this" — the job `written_by` already does.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import actions, archive, config, db, events, live, llm, series  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

        was = os.environ.get("MEMCAL_HOME")
        os.environ["MEMCAL_HOME"] = self.dir
        self.addCleanup(lambda: os.environ.__setitem__("MEMCAL_HOME", was)
                        if was is not None else os.environ.pop("MEMCAL_HOME", None))

        self.cfg = config.load(self.dir)
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)
        self.addCleanup(self.conn.close)

        db.set_today("2026-09-08T09:00:00")
        self.addCleanup(db.set_today, None)

        def explode(*_a, **_k):
            raise AssertionError("a live write reached for a model")

        original = llm.OpenRouter.complete
        llm.OpenRouter.complete = explode
        self.addCleanup(lambda: setattr(llm.OpenRouter, "complete", original))

    def _turn(self, text: str, *, thread: str = "hermes:s1") -> int:
        return archive.append(
            self.conn, stream="agent",
            external_id=f"turn:{db.slugify(text, 24)}:{thread}",
            ts=db.now(), text=text, thread=thread, person="me", from_me=True,
            addressed_to="machine", gated=True, gate_reason="directive")


class TestLiveSeriesSourceIsAPointer(Base):
    def test_schedule_from_a_turn_records_the_thread_not_the_writer(self):
        turn = self._turn("tutoring every tuesday at 5")
        rule, _ = live.set_schedule(
            self.conn, self.cfg, "tutoring", cadence="weekly", weekday="tuesday",
            time="17:00", origin=live.Origin.of("hermes", [turn], session="s1"))
        self.assertNotEqual(rule.source, "agent:live")
        self.assertEqual(rule.source, "thread:agent:hermes:s1")
        self.assertEqual(rule.written_by, "live")
        rows = self.conn.execute(
            "SELECT entity, stage FROM provenance WHERE kind = 'series' AND ref = ?"
            " ORDER BY id DESC LIMIT 1", (rule.slug,)).fetchone()
        self.assertIsNotNone(rows)
        self.assertEqual(rows["entity"], "thread:agent:hermes:s1")
        self.assertEqual(rows["stage"], "live")

    def test_written_by_still_identifies_the_writer(self):
        turn = self._turn("physio weekly mondays at 5")
        rule, _ = live.set_schedule(
            self.conn, self.cfg, "physio", cadence="weekly", weekday="monday",
            time="17:00", origin=live.Origin.of("hermes", [turn], session="s1"))
        self.assertEqual(rule.written_by, "live")
        row = self.conn.execute(
            "SELECT written_by FROM series WHERE slug = ?", (rule.slug,)).fetchone()
        self.assertEqual(row["written_by"], "live")

    def test_session_without_an_archived_turn_still_names_the_session(self):
        rule, _ = live.set_schedule(
            self.conn, self.cfg, "tutoring", cadence="weekly", weekday="tuesday",
            time="17:00",
            origin=live.Origin.of("hermes", [], session="s1"))
        self.assertNotEqual(rule.source, "agent:live")
        self.assertEqual(rule.source, "thread:agent:hermes:s1")
        self.assertEqual(rule.written_by, "live")

    def test_schedule_with_no_origin_uses_the_documented_fallback(self):
        rule, _ = live.set_schedule(
            self.conn, self.cfg, "tutoring", cadence="weekly", weekday="tuesday",
            time="17:00")
        self.assertNotEqual(rule.source, "agent:live")
        self.assertEqual(rule.source, live.LIVE_DIRECT_SOURCE)
        self.assertEqual(rule.written_by, "live")
        rows = self.conn.execute(
            "SELECT entity FROM provenance WHERE kind = 'series' AND ref = ?"
            " ORDER BY id DESC LIMIT 1", (rule.slug,)).fetchone()
        self.assertEqual(rows["entity"], live.LIVE_DIRECT_SOURCE)

    def test_ending_a_series_points_source_at_the_ending_turn(self):
        first = self._turn("tutoring every tuesday")
        live.set_schedule(
            self.conn, self.cfg, "tutoring", cadence="weekly", weekday="tuesday",
            time="17:00", origin=live.Origin.of("hermes", [first], session="s1"))
        second = self._turn("tutoring is over")
        rule, _ = live.set_schedule(
            self.conn, self.cfg, "tutoring", ended=True,
            origin=live.Origin.of("hermes", [second], session="s1"))
        self.assertEqual(rule.status, "ended")
        self.assertEqual(rule.source, "thread:agent:hermes:s1")
        self.assertNotEqual(rule.source, "agent:live")


class TestLiveWritesLeaveFieldHistory(Base):
    """Invariant 4: a change to a tracked field leaves a history row.

    Mutate through each live public writer that owns a `*_history` table and
    assert the history grew. To-dos have no field-history table by design; the
    completed-operation record is their durable trace, so those two assert the
    action instead.
    """

    def _history_count(self, table: str, key_col: str, key: str) -> int:
        return self.conn.execute(
            f"SELECT count(*) AS n FROM {table} WHERE {key_col} = ?", (key,)
        ).fetchone()["n"]

    def test_update_event_leaves_event_history(self):
        event = live.add_event(
            self.conn, self.cfg, title="Brunch", when="2026-09-20",
            location="Blue Fern")[0]
        before = self._history_count("event_history", "event_id", event.id)
        turn = self._turn("brunch moved to rosewood")
        live.update_event(
            self.conn, self.cfg, "Brunch", location="Rosewood",
            origin=live.Origin.of("hermes", [turn], session="s1"))
        after = self._history_count("event_history", "event_id", event.id)
        self.assertGreater(after, before)

    def test_set_schedule_leaves_series_history(self):
        rule, _ = live.set_schedule(
            self.conn, self.cfg, "tutoring", cadence="weekly", weekday="tuesday",
            time="17:00",
            origin=live.Origin.of(
                "hermes", [self._turn("tutoring tuesdays at 5")], session="s1"))
        before = self._history_count("series_history", "slug", rule.slug)
        live.set_schedule(
            self.conn, self.cfg, "tutoring", time="18:00",
            origin=live.Origin.of(
                "hermes", [self._turn("tutoring at 6 instead")], session="s1"))
        after = self._history_count("series_history", "slug", rule.slug)
        self.assertGreater(after, before)

    def test_move_one_occurrence_leaves_event_history(self):
        live.set_schedule(
            self.conn, self.cfg, "physio", cadence="weekly", weekday="monday",
            time="17:00", starting="2026-09-14",
            origin=live.Origin.of(
                "hermes", [self._turn("physio mondays at 5")], session="s1"))
        occurrence = self.conn.execute(
            "SELECT key, id FROM events WHERE series = ? ORDER BY date LIMIT 1",
            ("physio",)).fetchone()
        self.assertIsNotNone(occurrence, "roll_forward should project one occurrence")
        before = self._history_count("event_history", "event_id", occurrence["id"])
        live.move_one_occurrence(
            self.conn, self.cfg, occurrence["key"], to="2026-09-16",
            origin=live.Origin.of(
                "hermes", [self._turn("physio wednesday this week")], session="s1"))
        after = self._history_count("event_history", "event_id", occurrence["id"])
        self.assertGreater(after, before)

    def test_merge_events_leaves_event_history(self):
        live.add_event(self.conn, self.cfg, title="Ramen dinner", when="2026-09-24")
        live.add_event(self.conn, self.cfg, title="Ramen with Riley", when="2026-09-24")
        survivor = events.search(self.conn, "Ramen dinner")[0]
        before = self._history_count("event_history", "event_id", survivor.id)
        live.merge_events(
            self.conn, self.cfg, keep="Ramen dinner", drop="Ramen with Riley",
            origin=live.Origin.of(
                "hermes", [self._turn("those are the same dinner")], session="s1"))
        merged = events.get(self.conn, survivor.key)
        after = self._history_count("event_history", "event_id", merged.id)
        self.assertGreater(after, before)
        fields = [row["field"] for row in events.history(self.conn, merged.id)]
        self.assertIn("merged", fields)

    def test_open_and_close_todo_leave_completed_operation_records(self):
        before = self.conn.execute(
            "SELECT count(*) AS n FROM actions WHERE kind = 'todo'").fetchone()["n"]
        todo, _ = live.open_todo(
            self.conn, self.cfg, "Pay Parker for the camping pass",
            origin=live.Origin.of(
                "hermes", [self._turn("remind me to pay parker")], session="s1"))
        live.close_todo(
            self.conn, self.cfg, "Pay Parker",
            origin=live.Origin.of(
                "hermes", [self._turn("i paid parker")], session="s1"))
        after = self.conn.execute(
            "SELECT count(*) AS n FROM actions WHERE kind = 'todo'").fetchone()["n"]
        self.assertGreaterEqual(after - before, 2)
        verbs = [row["verb"] for row in self.conn.execute(
            "SELECT verb FROM actions WHERE kind = 'todo' AND ref = ? ORDER BY id",
            (todo.key,)).fetchall()]
        self.assertIn("opened", verbs)
        self.assertIn("closed", verbs)

    def test_drop_event_leaves_a_completed_operation_record(self):
        event = live.add_event(
            self.conn, self.cfg, title="ASPCA Mobile Clinic", when="2026-09-23",
            kind="opportunity")[0]
        live.drop_event(
            self.conn, self.cfg, "ASPCA",
            origin=live.Origin.of(
                "hermes", [self._turn("that clinic was never real")], session="s1"))
        self.assertIsNone(events.get(self.conn, event.key))
        row = self.conn.execute(
            "SELECT verb FROM actions WHERE kind = 'event' AND ref = ? ORDER BY id DESC"
            " LIMIT 1", (event.key,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["verb"], "dropped")


if __name__ == "__main__":
    unittest.main()
