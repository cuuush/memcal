"""M1 FIELD_EVIDENCE: per-field attribution on live event writes."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import actions, activity, archive, db, events, live, llm
from memcal.config import Config
from memcal.sources import base
from memcal.sources.base import IngestReport


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)
        db.set_today("2026-09-12")
        self.addCleanup(db.set_today, None)
        self._client_for = llm.client_for
        def _no_model(*_a, **_k):
            raise AssertionError("field-evidence paths must not call a model")
        llm.client_for = _no_model
        self.addCleanup(setattr, llm, "client_for", self._client_for)

    def tearDown(self):
        self.conn.close()

    def collect(self, stream, eid, text, thread, handle="friend@example.com",
                ts=None, **kw):
        report = IngestReport(stream=stream)
        base.deliver(self.conn, report, stream=stream, external_id=eid,
                     ts=ts or db.now(), text=text, thread=thread,
                     handle=handle, **kw)
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM archive WHERE stream = ? AND external_id = ?",
            (stream, eid)).fetchone()
        return row["id"] if row else None

    def poker(self, title="Poker night", **fields):
        event, _ = live.add_event(
            self.conn, self.cfg, title=title, when="2026-09-12",
            origin=live.Origin.of("test"), **fields)
        return event

    def pending_ids(self, key):
        return [i["id"] for i in activity.pending(
            self.conn, "event", key, strong_only=True)["strong"]]


class TestActivityReadStrongOnly(_Base):
    def test_strong_only_pagination_skips_weak_candidate_scan(self):
        m1 = self.collect("chat", "m1", "poker saturday?", "poker group")
        event = self.poker()
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        self.collect("chat", "m2", "moved to Sunday", "poker group")
        with mock.patch.object(activity, "ref_persons",
                               side_effect=AssertionError("weak scan ran")):
            page = activity.read(self.conn, "event", event.key, limit=10)
        self.assertEqual(len(page["items"]), 1)
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["reviewed"], 1)


class TestFieldSourcesPartialApply(_Base):
    def test_old_address_plus_newer_time_partial_result(self):
        event = self.poker()
        settle = self.collect("chat", "s1", "make it 5 Oak", "poker group",
                              ts="2026-09-12T10:00:00-04:00")
        live.update_event(self.conn, self.cfg, event.key, location="5 Oak",
                          origin=live.Origin.of("test", cited=[settle]))
        old_place = self.collect("chat", "op", "how about 1 Pine?", "poker group",
                                 ts="2026-09-12T09:00:00-04:00")
        new_time = self.collect("chat", "nt", "start at 8pm", "poker group",
                                ts="2026-09-12T11:00:00-04:00")
        # Link activity so pending is measurable.
        live.update_event(self.conn, self.cfg, event.key, note="linked",
                          origin=live.Origin.of("test", cited=[settle]))
        outcome = live.update_event(
            self.conn, self.cfg, event.key, location="1 Pine", time="20:00",
            field_sources={"location": [old_place], "time": [new_time]})
        by_field = {item.field: item for item in outcome.fields}
        self.assertEqual(by_field["time"].status, "applied")
        self.assertEqual(by_field["location"].status, "rejected")
        self.assertEqual(outcome.event.location, "5 Oak")
        self.assertEqual(outcome.event.time, "20:00")
        pending = self.pending_ids(event.key)
        self.assertIn(old_place, pending)
        self.assertNotIn(new_time, pending)

    def test_new_address_plus_older_context_grants_no_authority(self):
        event = self.poker()
        m0 = self.collect("chat", "m0", "poker?", "poker group",
                          ts="2026-09-12T08:00:00-04:00")
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m0]))
        place = self.collect("chat", "p1", "same place — 7 Elm", "poker group",
                             ts="2026-09-12T12:00:00-04:00")
        older_ctx = self.collect("chat", "c1", "we used to meet at 1 Pine",
                                 "poker group", ts="2026-09-12T09:00:00-04:00")
        before_pending = set(self.pending_ids(event.key))
        outcome = live.update_event(
            self.conn, self.cfg, event.key, location="7 Elm",
            field_sources={"location": [place]},
            context_source_ids=[older_ctx])
        self.assertEqual(outcome.event.location, "7 Elm")
        loc = next(item for item in outcome.fields if item.field == "location")
        self.assertEqual(loc.status, "applied")
        stamp = self.conn.execute(
            "SELECT evidence_ts FROM event_history WHERE event_id = ?"
            " AND field = 'location' ORDER BY id DESC LIMIT 1",
            (outcome.event.id,)).fetchone()["evidence_ts"]
        self.assertEqual(db.parse_ts(stamp), db.parse_ts("2026-09-12T12:00:00-04:00"))
        # Context id is not auto-acknowledged.
        self.assertIn(older_ctx, self.pending_ids(event.key))
        action = actions.latest_for(self.conn, "event", event.key)
        self.assertEqual(action.context_source_ids, [older_ctx])

    def test_field_specific_timestamps_with_multiple_supporting_lines(self):
        event = self.poker()
        day_a = self.collect("chat", "d0", "maybe saturday", "poker group",
                             ts="2026-09-12T08:00:00-04:00")
        day_b = self.collect("chat", "d1", "actually sunday", "poker group",
                             ts="2026-09-12T09:00:00-04:00")
        place_a = self.collect("chat", "p0", "5 Oak?", "poker group",
                               ts="2026-09-12T09:30:00-04:00")
        place_b = self.collect("chat", "p1", "confirmed 5 Oak", "poker group",
                               ts="2026-09-12T10:30:00-04:00")
        outcome = live.update_event(
            self.conn, self.cfg, event.key, when="2026-09-13", location="5 Oak",
            field_sources={"when": [day_a, day_b], "location": [place_a, place_b]})
        times = {r["field"]: r["evidence_ts"] for r in self.conn.execute(
            "SELECT field, evidence_ts FROM event_history WHERE event_id = ?"
            " AND field IN ('date', 'location') ORDER BY id",
            (outcome.event.id,))}
        # Newest supporting line per field — never a cross-field max.
        self.assertEqual(db.parse_ts(times["date"]),
                         db.parse_ts("2026-09-12T09:00:00-04:00"))
        self.assertEqual(db.parse_ts(times["location"]),
                         db.parse_ts("2026-09-12T10:30:00-04:00"))


class TestAddMatchClearParticipants(_Base):
    def test_add_new_with_field_sources(self):
        src = self.collect("chat", "a1", "dinner sunday 7pm", "plans",
                           ts="2026-09-12T10:00:00-04:00")
        outcome = live.add_event(
            self.conn, self.cfg, title="Dinner", when="2026-09-13", time="19:00",
            field_sources={"title": [src], "date": [src], "time": [src]})
        self.assertEqual(outcome.verb, "inserted")
        self.assertEqual(outcome.event.time, "19:00")

    def test_add_matching_existing_does_not_duplicate(self):
        event = self.poker(title="Dinner")
        src = self.collect("chat", "a2", "dinner still on", "plans",
                           ts="2026-09-12T11:00:00-04:00")
        before = self.conn.execute("SELECT count(*) n FROM events").fetchone()["n"]
        outcome = live.add_event(
            self.conn, self.cfg, title="Dinner", when="2026-09-12",
            location="5 Oak",
            field_sources={"title": [src], "date": [src], "location": [src]})
        after = self.conn.execute("SELECT count(*) n FROM events").fetchone()["n"]
        self.assertEqual(before, after)
        self.assertEqual(outcome.event.key, event.key)

    def test_clear_and_participants_obey_field_sources(self):
        event = self.poker(location="5 Oak", participants=["Jamie"])
        settle = self.collect("chat", "s1", "at 5 Oak with Jamie", "poker group",
                              ts="2026-09-12T10:00:00-04:00")
        live.update_event(self.conn, self.cfg, event.key, location="5 Oak",
                          origin=live.Origin.of("test", cited=[settle]))
        clear_src = self.collect("chat", "c1", "no place yet", "poker group",
                                 ts="2026-09-12T11:00:00-04:00")
        add_src = self.collect("chat", "c2", "Quinn is coming", "poker group",
                               ts="2026-09-12T11:05:00-04:00")
        outcome = live.update_event(
            self.conn, self.cfg, event.key, location="",
            add_participants=["Quinn"],
            field_sources={"location": [clear_src], "participants": [add_src]})
        self.assertIsNone(outcome.event.location)
        self.assertIn("Quinn", outcome.event.participants)

    def test_different_occurrence_stays_separate(self):
        a = self.poker(title="Poker night")
        src = self.collect("chat", "n1", "poker next week too", "poker group",
                           ts="2026-09-12T12:00:00-04:00")
        outcome = live.add_event(
            self.conn, self.cfg, title="Poker night", when="2026-09-19",
            field_sources={"title": [src], "date": [src]})
        self.assertNotEqual(outcome.event.key, a.key)
        self.assertEqual(outcome.event.date, "2026-09-19")


class TestMalformedAndConflicts(_Base):
    def test_unknown_ids_cause_no_mutation(self):
        event = self.poker(location="5 Oak")
        settle = self.collect("chat", "s1", "5 Oak", "poker group",
                              ts="2026-09-12T10:00:00-04:00")
        live.update_event(self.conn, self.cfg, event.key, location="5 Oak",
                          origin=live.Origin.of("test", cited=[settle]))
        before = self.conn.execute(
            "SELECT count(*) n FROM event_history").fetchone()["n"]
        with self.assertRaises(live.LiveError):
            live.update_event(
                self.conn, self.cfg, event.key, location="1 Pine",
                field_sources={"location": [999999]})
        self.assertEqual(self.conn.execute(
            "SELECT location FROM events WHERE key = ?",
            (event.key,)).fetchone()["location"], "5 Oak")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) n FROM event_history").fetchone()["n"], before)

    def test_bad_timestamp_causes_no_mutation(self):
        event = self.poker(location="5 Oak")
        settle = self.collect("chat", "s1", "5 Oak", "poker group",
                              ts="2026-09-12T10:00:00-04:00")
        live.update_event(self.conn, self.cfg, event.key, location="5 Oak",
                          origin=live.Origin.of("test", cited=[settle]))
        bad = archive.append(
            self.conn, stream="chat", external_id="bad", ts="not-a-timestamp",
            text="1 Pine", thread="poker group", person="friend",
            from_me=False, addressed_to="me")
        self.conn.commit()
        with self.assertRaises(live.LiveError):
            live.update_event(
                self.conn, self.cfg, event.key, location="1 Pine",
                field_sources={"location": [bad]})
        self.assertEqual(self.conn.execute(
            "SELECT location FROM events WHERE key = ?",
            (event.key,)).fetchone()["location"], "5 Oak")

    def test_conflicting_citation_forms_rejected(self):
        event = self.poker()
        src = self.collect("chat", "s1", "5 Oak", "poker group",
                           ts="2026-09-12T10:00:00-04:00")
        with self.assertRaises(live.LiveError) as caught:
            live.update_event(
                self.conn, self.cfg, event.key, location="5 Oak",
                origin=live.Origin.of("test", cited=[src]),
                field_sources={"location": [src]})
        self.assertIn("not both", str(caught.exception))

    def test_timezone_offset_equivalent_timestamps_compare_as_instants(self):
        event = self.poker()
        # 15:00Z == 11:00-04:00
        settle = self.collect("chat", "s1", "5 Oak", "poker group",
                              ts="2026-09-12T15:00:00+00:00")
        live.update_event(self.conn, self.cfg, event.key, location="5 Oak",
                          origin=live.Origin.of("test", cited=[settle]))
        older = self.collect("chat", "o1", "1 Pine", "poker group",
                             ts="2026-09-12T10:00:00-04:00")
        outcome = live.update_event(
            self.conn, self.cfg, event.key, location="1 Pine",
            field_sources={"location": [older]})
        loc = next(item for item in outcome.fields if item.field == "location")
        self.assertEqual(loc.status, "rejected")
        self.assertEqual(outcome.event.location, "5 Oak")


class TestReaffirmation(_Base):
    def test_newer_same_value_advances_evidence_once(self):
        event = self.poker()
        first = self.collect("chat", "f1", "5 Oak", "poker group",
                             ts="2026-09-12T10:00:00-04:00")
        live.update_event(self.conn, self.cfg, event.key, location="5 Oak",
                          origin=live.Origin.of("test", cited=[first]))
        affirm = self.collect("chat", "f2", "yes still 5 Oak", "poker group",
                              ts="2026-09-12T12:00:00-04:00")
        outcome = live.update_event(
            self.conn, self.cfg, event.key, location="5 Oak",
            field_sources={"location": [affirm]})
        loc = next(item for item in outcome.fields if item.field == "location")
        self.assertEqual(loc.status, "applied")
        self.assertTrue(loc.evidence_advanced)
        stamp = self.conn.execute(
            "SELECT evidence_ts FROM event_history WHERE event_id = ?"
            " AND field = 'location' ORDER BY id DESC LIMIT 1",
            (outcome.event.id,)).fetchone()["evidence_ts"]
        self.assertEqual(db.parse_ts(stamp), db.parse_ts("2026-09-12T12:00:00-04:00"))
        # Reread of the same affirming statement is a replay: no new history row.
        before_hist = self.conn.execute(
            "SELECT count(*) n FROM event_history WHERE event_id = ?"
            " AND field = 'location'", (outcome.event.id,)).fetchone()["n"]
        again = live.update_event(
            self.conn, self.cfg, event.key, location="5 Oak",
            field_sources={"location": [affirm]})
        self.assertEqual(self.conn.execute(
            "SELECT count(*) n FROM event_history WHERE event_id = ?"
            " AND field = 'location'", (outcome.event.id,)).fetchone()["n"],
            before_hist)
        stamp2 = self.conn.execute(
            "SELECT evidence_ts FROM event_history WHERE event_id = ?"
            " AND field = 'location' ORDER BY id DESC LIMIT 1",
            (outcome.event.id,)).fetchone()["evidence_ts"]
        self.assertEqual(db.parse_ts(stamp2), db.parse_ts("2026-09-12T12:00:00-04:00"))
        # Intermediate older contradiction cannot undo the advanced evidence.
        older = self.collect("chat", "o1", "1 Pine instead", "poker group",
                             ts="2026-09-12T11:00:00-04:00")
        denied = live.update_event(
            self.conn, self.cfg, event.key, location="1 Pine",
            field_sources={"location": [older]})
        self.assertEqual(
            next(i for i in denied.fields if i.field == "location").status,
            "rejected")
        self.assertEqual(denied.event.location, "5 Oak")


class TestFlatCallersAndMigration(_Base):
    def test_flat_source_ids_still_work_with_oldest_line_rule(self):
        event = self.poker()
        settle = self.collect("chat", "s1", "5 Oak", "poker group",
                              ts="2026-09-12T10:00:00-04:00")
        live.update_event(self.conn, self.cfg, event.key, location="5 Oak",
                          origin=live.Origin.of("test", cited=[settle]))
        old_place = self.collect("chat", "op", "1 Pine", "poker group",
                                 ts="2026-09-12T09:00:00-04:00")
        new_time = self.collect("chat", "nt", "8pm", "poker group",
                                ts="2026-09-12T11:00:00-04:00")
        # Flat citation: oldest line caps both fields — location cannot ride time.
        # Time may still apply when previously empty (no floor); location stays.
        outcome = live.update_event(
            self.conn, self.cfg, event.key, location="1 Pine", time="20:00",
            origin=live.Origin.of("test", cited=[old_place, new_time]))
        self.assertEqual(outcome.event.location, "5 Oak")
        self.assertNotIn("location", " ".join(outcome.changed_lines))
        # All-rejected flat revision still names field_sources in the remedy.
        with self.assertRaises(live.LiveError) as caught:
            live.update_event(
                self.conn, self.cfg, event.key, location="1 Pine",
                origin=live.Origin.of("test", cited=[old_place]))
        msg = str(caught.exception).lower()
        self.assertIn("older", msg)
        self.assertIn("field_sources", msg)
        self.assertNotIn("uncited", msg)
        self.assertNotIn("strip", msg)

    def test_actions_attribution_columns_migrate(self):
        path = Path(self.tmp.name) / "legacy.db"
        conn = db.connect(path)
        try:
            conn.executescript(
                "CREATE TABLE actions ("
                " id INTEGER PRIMARY KEY, op_id TEXT NOT NULL UNIQUE,"
                " kind TEXT NOT NULL, ref TEXT NOT NULL, verb TEXT NOT NULL,"
                " surface TEXT NOT NULL, session TEXT,"
                " fields TEXT NOT NULL DEFAULT '{}',"
                " source_ids TEXT NOT NULL DEFAULT '[]',"
                " source_note TEXT, based_on TEXT, at TEXT NOT NULL);")
            conn.execute(
                "INSERT INTO actions(op_id, kind, ref, verb, surface, fields,"
                " source_ids, at) VALUES('x', 'event', 'k', 'updated', 'test',"
                " '{}', '[]', '2026-09-12T10:00:00')")
            conn.commit()
            db.migrate(conn)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(actions)")}
            self.assertIn("field_sources", cols)
            self.assertIn("context_source_ids", cols)
            self.assertIn("outcome", cols)
            row = conn.execute("SELECT field_sources, context_source_ids, outcome"
                               " FROM actions WHERE op_id = 'x'").fetchone()
            self.assertEqual(db.jload(row["field_sources"], None), {})
            self.assertEqual(db.jload(row["context_source_ids"], None), [])
            self.assertEqual(db.jload(row["outcome"], None), {})
            full = actions.get(conn, "x")
            self.assertEqual(full.field_sources, {})
            self.assertEqual(full.context_source_ids, [])
            self.assertEqual(full.outcome, {})
            db.migrate(conn)  # remigrate harmlessly
        finally:
            conn.close()

    def test_refusal_never_advises_stripping_or_uncited_restatement(self):
        event = self.poker(location="5 Oak")
        settle = self.collect("chat", "s1", "5 Oak", "poker group",
                              ts="2026-09-12T11:00:00-04:00")
        live.update_event(self.conn, self.cfg, event.key, location="5 Oak",
                          origin=live.Origin.of("test", cited=[settle]))
        with self.assertRaises(live.LiveError) as caught:
            live.update_event(
                self.conn, self.cfg, event.key, location="1 Pine",
                origin=live.Origin.of("test", cited=[999999]))
        msg = str(caught.exception).lower()
        self.assertNotIn("uncited", msg)
        self.assertNotIn("restate", msg)
        self.assertNotIn("strip", msg)
        self.assertIn("field_sources", msg)


class TestAdapterSurfaces(_Base):
    def test_mcp_accepts_field_sources_and_reports_partial(self):
        from memcal import mcp_server
        event = self.poker()
        settle = self.collect("chat", "s1", "5 Oak", "poker group",
                              ts="2026-09-12T10:00:00-04:00")
        live.update_event(self.conn, self.cfg, event.key, location="5 Oak",
                          origin=live.Origin.of("test", cited=[settle]))
        old_place = self.collect("chat", "op", "1 Pine", "poker group",
                                 ts="2026-09-12T09:00:00-04:00")
        new_time = self.collect("chat", "nt", "8pm", "poker group",
                                ts="2026-09-12T11:00:00-04:00")
        row = self.conn.execute("SELECT id FROM events WHERE key = ?",
                                (event.key,)).fetchone()
        server = mcp_server.Server.__new__(mcp_server.Server)
        server.conn, server.cfg = self.conn, self.cfg
        server.harness, server.session = "test", ""
        out = server.call("memcal_update", {
            "which": f"E{row['id']}",
            "location": "1 Pine", "time": "20:00",
            "field_sources": {"location": [old_place], "time": [new_time]},
        })
        self.assertIn("Rejected: location", out)
        self.assertIn("Applied: time", out)
        self.assertNotIn("nothing — it already said that", out)

    def test_hermes_handler_passes_field_sources(self):
        import importlib.util
        hermes_path = (Path(__file__).resolve().parent.parent
                       / "integrations" / "hermes" / "memcal" / "__init__.py")
        # Load only the schema dicts without executing plugin registration side effects
        # by compiling the parameter blocks via live path equivalence: the handler
        # source must mention field_sources.
        src = hermes_path.read_text(encoding="utf-8")
        self.assertIn('"field_sources"', src)
        self.assertIn('field_sources=args.get("field_sources")', src)
        event = self.poker()
        settle = self.collect("chat", "s1", "5 Oak", "poker group",
                              ts="2026-09-12T10:00:00-04:00")
        live.update_event(self.conn, self.cfg, event.key, location="5 Oak",
                          origin=live.Origin.of("test", cited=[settle]))
        old_place = self.collect("chat", "op", "1 Pine", "poker group",
                                 ts="2026-09-12T09:00:00-04:00")
        new_time = self.collect("chat", "nt", "8pm", "poker group",
                                ts="2026-09-12T11:00:00-04:00")
        outcome = live.update_event(
            self.conn, self.cfg, event.key, location="1 Pine", time="20:00",
            field_sources={"location": [old_place], "time": [new_time]})
        self.assertTrue(any(i.field == "location" and i.status == "rejected"
                            for i in outcome.fields))
        self.assertTrue(any(i.field == "time" and i.status == "applied"
                            for i in outcome.fields))


class TestFrozenOutcomeType(_Base):
    def test_outcome_type_is_exported_and_jsonable(self):
        self.assertTrue(hasattr(live, "FieldOutcome"))
        self.assertTrue(hasattr(live, "EventWriteOutcome"))
        fo = live.FieldOutcome(field="location", status="rejected",
                               reason="source predates the stored value",
                               source_ids=(1, 2), old="5 Oak", new="1 Pine")
        d = fo.as_dict()
        self.assertEqual(d["status"], "rejected")
        self.assertEqual(d["source_ids"], [1, 2])
        self.assertFalse(d["evidence_advanced"])


if __name__ == "__main__":
    unittest.main()
