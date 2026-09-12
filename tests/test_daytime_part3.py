"""Part 3: review accounting and activity nomination — no model, no fact changes."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import activity, archive, db, identity, live, threads, trace
from memcal.config import Config
from memcal.sources import base
from memcal.sources.spec import Source


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)
        db.set_today("2026-09-12")
        self.addCleanup(db.set_today, None)

    def tearDown(self):
        self.conn.close()

    def collect(self, stream, eid, text, thread, handle="friend@example.com",
                ts=None, **kw):
        """One message through the real delivery path. Returns its archive id."""
        from memcal.sources.base import IngestReport
        report = IngestReport(stream=stream)
        base.deliver(self.conn, report, stream=stream, external_id=eid,
                     ts=ts or db.now(), text=text, thread=thread,
                     handle=handle, **kw)
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM archive WHERE stream = ? AND external_id = ?",
            (stream, eid)).fetchone()
        return row["id"] if row else None

    def poker(self, title="Poker night"):
        event, _verb = live.add_event(
            self.conn, self.cfg, title=title, when="2026-09-12",
            origin=live.Origin.of("test"))
        return event


class TestNewActivityNominatesWithoutChangingFacts(_Base):
    def test_thread_with_new_messages_produces_a_candidate(self):
        from memcal import llm
        m1 = self.collect("chat", "m1", "poker saturday 8pm at Jordan's?",
                          "poker group")
        event = self.poker()
        # Cite the establishing line: evidence links the thread, mark covers m1.
        live.update_event(self.conn, self.cfg, event.key, note="saturday plan",
                          origin=live.Origin.of("test", cited=[m1]))
        before = (event.title, event.date, event.location, event.status)
        with __import__("unittest.mock", fromlist=["patch"]).patch.object(
                llm, "client_for",
                side_effect=AssertionError("nomination runs no model")):
            found = activity.pending(self.conn, "event", event.key)
            links = activity.associations(self.conn, "event", event.key)
        self.assertEqual(links["strong"], [{"stream": "chat",
                                            "thread": "poker group"}])
        m2 = self.collect("chat", "m2", "moved to Sunday at my new place",
                          "poker group")
        m3 = self.collect("chat", "m3", "bring chips", "poker group")
        found = activity.pending(self.conn, "event", event.key)
        self.assertEqual([i["id"] for i in found["strong"]], [m2, m3])
        row = found["strong"][0]
        self.assertEqual(
            (row["stream"], row["thread"], row["text"]),
            ("chat", "poker group", "moved to Sunday at my new place"))
        self.assertTrue(row["ts"])
        after = self.conn.execute(
            "SELECT title, date, location, status FROM events WHERE key = ?",
            (event.key,)).fetchone()
        self.assertEqual(tuple(after), before)

    def test_duplicate_delivery_does_not_inflate_the_count(self):
        m1 = self.collect("chat", "m1", "poker saturday?", "poker group")
        event = self.poker()
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        m2 = self.collect("chat", "m2", "moved to Sunday", "poker group")
        self.assertEqual(activity.pending(self.conn, "event", event.key)
                         ["strong_total"], 1)
        self.collect("chat", "m2", "moved to Sunday", "poker group")
        found = activity.pending(self.conn, "event", event.key)
        self.assertEqual(found["strong_total"], 1)
        self.assertEqual([i["id"] for i in found["strong"]], [m2])

    def test_a_late_old_message_is_new_with_its_old_date(self):
        m1 = self.collect("chat", "m1", "poker saturday?", "poker group")
        event = self.poker()
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        old = self.collect("chat", "old9", "actually Jordan confirmed Friday",
                           "poker group", ts="2020-05-01T19:00:00")
        found = activity.pending(self.conn, "event", event.key)
        self.assertEqual([i["id"] for i in found["strong"]], [old])
        self.assertEqual(found["strong"][0]["ts"], "2020-05-01T19:00:00")

    def test_calendar_revisions_only_archive_real_changes(self):
        from memcal.sources import ical
        item = {"uid": "u1", "title": "Dentist", "start": "2026-09-20T14:00:00",
                "end": "2026-09-20T15:00:00", "all_day": False, "location": "",
                "description": "", "url": "", "calendar_name": "Home",
                "calendar_uid": "c1", "writable": True}
        ident = ical._identity(item)
        self.assertEqual(ical._revision(ident, item), ical._revision(ident, dict(item)))
        moved = dict(item, start="2026-09-20T15:00:00")
        self.assertNotEqual(ical._revision(ident, item), ical._revision(ident, moved))
        self.assertEqual(ical._identity(moved), ident)


class TestReviewClearsOnlyWhatItCovered(_Base):
    def _poker_with_two_waiting(self):
        m1 = self.collect("chat", "m1", "poker saturday 8pm?", "poker group")
        event = self.poker()
        live.update_event(self.conn, self.cfg, event.key, note="saturday plan",
                          origin=live.Origin.of("test", cited=[m1]))
        m2 = self.collect("chat", "m2", "moved to Sunday?", "poker group")
        m3 = self.collect("chat", "m3", "new address is 5 Oak?", "poker group")
        return event, m1, m2, m3

    def test_a_cited_correction_clears_only_its_lines(self):
        event, m1, m2, m3 = self._poker_with_two_waiting()
        live.update_event(self.conn, self.cfg, event.key, when="2026-09-13",
                          origin=live.Origin.of("test", cited=[m2]))
        found = activity.pending(self.conn, "event", event.key)
        self.assertEqual([i["id"] for i in found["strong"]], [m3])
        self.assertEqual(activity.reviewed_ids(self.conn, "event", event.key),
                         {m1, m2})

    def test_an_unrelated_correction_acknowledges_nothing_here(self):
        event, _m1, m2, _m3 = self._poker_with_two_waiting()
        other = self.collect("mail", "o1", "brunch Sunday?", "brunch thread")
        lunch, _ = live.add_event(self.conn, self.cfg, title="Brunch",
                                  when="2026-09-13",
                                  origin=live.Origin.of("test"))
        live.update_event(self.conn, self.cfg, lunch.key, note="confirmed",
                          origin=live.Origin.of("test", cited=[other]))
        found = activity.pending(self.conn, "event", event.key)
        self.assertEqual([i["id"] for i in found["strong"]], [m2, found["strong"][1]["id"]])
        self.assertEqual(len(found["strong"]), 2)

    def test_no_change_review_clears_exactly_what_was_read(self):
        event, _m1, m2, m3 = self._poker_with_two_waiting()
        with self.assertRaises(live.LiveError):
            live.reviewed(self.conn, self.cfg, event.key,
                          origin=live.Origin.of("test"))
        self.assertEqual(len(activity.pending(self.conn, "event", event.key)
                             ["strong"]), 2)
        msg = live.reviewed(self.conn, self.cfg, event.key,
                            origin=live.Origin.of("test", cited=[m2]))
        self.assertIn("no change", msg)
        found = activity.pending(self.conn, "event", event.key)
        self.assertEqual([i["id"] for i in found["strong"]], [m3])
        # Retry-safe: the same review repeated changes nothing further.
        msg2 = live.reviewed(self.conn, self.cfg, event.key,
                             origin=live.Origin.of("test", cited=[m2]))
        self.assertIn("already", msg2)
        self.assertEqual([i["id"] for i in activity.pending(
            self.conn, "event", event.key)["strong"]], [m3])

    def test_invented_citations_clear_nothing(self):
        event, _m1, m2, _m3 = self._poker_with_two_waiting()
        with self.assertRaises(live.LiveError):
            live.reviewed(self.conn, self.cfg, event.key,
                          origin=live.Origin.of("test", cited=[999999]))
        self.assertEqual(len(activity.pending(self.conn, "event", event.key)
                             ["strong"]), 2)

    def test_arrivals_during_a_pass_survive_it(self):
        m1 = self.collect("chat", "m1", "poker saturday?", "poker group")
        event = self.poker()
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        m2 = self.collect("chat", "m2", "moved to Sunday?", "poker group")
        # The pass reads what it snapshotted (m1, m2)...
        self.assertEqual(activity.note_review(self.conn, "event", event.key,
                                              [m1, m2], by_stage="dream"), 1)
        self.assertEqual(activity.pending(self.conn, "event", event.key)
                         ["strong"], [])
        # ...but what lands mid-pass is newer than the snapshot.
        m3 = self.collect("chat", "m3", "at 5 Oak?", "poker group")
        found = activity.pending(self.conn, "event", event.key)
        self.assertEqual([i["id"] for i in found["strong"]], [m3])
        # A failed bundle advances nothing at all.
        self.assertEqual(activity.note_review(self.conn, "event", "no-such-ref",
                                              [], by_stage="dream"), 0)
        self.assertEqual([i["id"] for i in activity.pending(
            self.conn, "event", event.key)["strong"]], [m3])

    def test_correction_then_redelivery_keeps_identity(self):
        event, _m1, m2, _m3 = self._poker_with_two_waiting()
        live.update_event(self.conn, self.cfg, event.key, when="2026-09-13",
                          origin=live.Origin.of("test", cited=[m2]))
        self.assertEqual(self.conn.execute(
            "SELECT date FROM events WHERE key = ?", (event.key,)).fetchone()["date"],
            "2026-09-13")
        before = self.conn.execute(
            "SELECT count(*) n FROM evidence WHERE kind='event' AND ref=?",
            (event.key,)).fetchone()["n"]
        self.collect("chat", "m2", "moved to Sunday?", "poker group")
        after = self.conn.execute(
            "SELECT count(*) n FROM evidence WHERE kind='event' AND ref=?",
            (event.key,)).fetchone()["n"]
        self.assertEqual(before, after)
        self.assertEqual(self.conn.execute(
            "SELECT date FROM events WHERE key = ?", (event.key,)).fetchone()["date"],
            "2026-09-13")


class TestNoSilentMerges(_Base):
    def test_busy_unrelated_chatter_is_not_nominated(self):
        m1 = self.collect("chat", "m1", "poker saturday?", "poker group")
        event = self.poker()
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        # Same participant, different conversation, nothing distinctive in common.
        self.collect("chat", "n1", "game tonight?", "video games",
                     handle="friend@example.com")
        links = activity.associations(self.conn, "event", event.key)
        self.assertEqual(links["weak"], [])
        self.assertEqual(activity.pending(self.conn, "event", event.key)
                         ["weak_total"], 0)

    def test_shared_participant_plus_term_is_weak_not_strong(self):
        m1 = self.collect("chat", "m1", "poker saturday?", "poker group")
        event = self.poker(title="Poker championship")
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        self.collect("chat", "w1", "brackets are out?", "championship planning",
                     handle="friend@example.com")
        links = activity.associations(self.conn, "event", event.key)
        self.assertEqual(len(links["weak"]), 1)
        self.assertIn("championship", links["weak"][0]["why"])
        found = activity.pending(self.conn, "event", event.key)
        self.assertEqual(found["strong"], [])
        self.assertEqual(len(found["weak"]), 1)

    def test_two_occasions_in_one_thread_stay_distinct(self):
        m1 = self.collect("chat", "m1", "poker saturday?", "poker group")
        first = self.poker(title="Poker Saturday")
        live.update_event(self.conn, self.cfg, first.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        second, _ = live.add_event(self.conn, self.cfg, title="Poker Sunday",
                                   when="2026-09-13",
                                   origin=live.Origin.of("test"))
        live.update_event(self.conn, self.cfg, second.key, note="other plan",
                          origin=live.Origin.of("test", cited=[m1]))
        m2 = self.collect("chat", "m2", "moved?", "poker group")
        for ev in (first, second):
            found = activity.pending(self.conn, "event", ev.key)
            self.assertEqual([i["id"] for i in found["strong"]], [m2])
        live.update_event(self.conn, self.cfg, first.key, when="2026-09-13",
                          origin=live.Origin.of("test", cited=[m2]))
        self.assertEqual(activity.pending(self.conn, "event", first.key)["strong"],
                         [])
        self.assertEqual([i["id"] for i in activity.pending(
            self.conn, "event", second.key)["strong"]], [m2])


class TestIdentityChangesKeepCoverage(_Base):
    def test_rename_and_reschedule_follow_the_same_thread(self):
        m1 = self.collect("chat", "m1", "poker saturday?", "poker group")
        event = self.poker()
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        live.update_event(self.conn, self.cfg, event.key, title="Poker night finals",
                          origin=live.Origin.of("test", cited=[m1]))
        live.update_event(self.conn, self.cfg, event.key, when="2026-09-13",
                          origin=live.Origin.of("test", cited=[m1]))
        event = live.find_event(self.conn, event.key)
        m2 = self.collect("chat", "m2", "moved again?", "poker group")
        found = activity.pending(self.conn, "event", event.key)
        self.assertEqual([i["id"] for i in found["strong"]], [m2])

    def test_merge_pools_evidence_and_keeps_the_newest_mark(self):
        m1 = self.collect("chat", "m1", "poker saturday?", "poker group")
        keep = self.poker(title="Poker A")
        drop, _ = live.add_event(self.conn, self.cfg, title="Poker B",
                                 when="2026-09-12",
                                 origin=live.Origin.of("test"))
        live.update_event(self.conn, self.cfg, keep.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        m2 = self.collect("chat", "m2", "sunday?", "poker group")
        live.update_event(self.conn, self.cfg, drop.key, note="other",
                          origin=live.Origin.of("test", cited=[m2]))
        live.merge_events(self.conn, self.cfg, keep.key, drop.key,
                          origin=live.Origin.of("test"))
        self.assertEqual(activity.reviewed_ids(self.conn, "event", keep.key),
                         {m1, m2})
        self.assertEqual(activity.reviewed_ids(self.conn, "event", drop.key), set())
        m3 = self.collect("chat", "m3", "monday?", "poker group")
        self.assertEqual([i["id"] for i in activity.pending(
            self.conn, "event", keep.key)["strong"]], [m3])

    def test_delete_drops_coverage_with_the_row(self):
        m1 = self.collect("chat", "m1", "poker saturday?", "poker group")
        event = self.poker()
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        live.drop_event(self.conn, self.cfg, event.key,
                        origin=live.Origin.of("test"))
        self.assertEqual(activity.reviewed_ids(self.conn, "event", event.key),
                         set())


class TestMutesIgnoresAndQuietRelevance(_Base):
    def test_muted_threads_and_ignored_senders_stay_out(self):
        m1 = self.collect("chat", "m1", "poker saturday?", "poker group")
        event = self.poker()
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        self.collect("chat", "m2", "moved to Sunday?", "poker group")
        threads.record(self.conn, "chat", "poker group", is_group=True)
        self.conn.execute("UPDATE threads SET decision='mute'"
                          " WHERE stream='chat' AND thread='poker group'")
        self.conn.commit()
        self.assertEqual(activity.pending(self.conn, "event", event.key)
                         ["strong_total"], 0)
        self.conn.execute("UPDATE threads SET decision=NULL"
                          " WHERE stream='chat' AND thread='poker group'")
        identity.set_sender(self.conn, "loud@example.com", "ignore", "no",
                            source="you")
        m3 = self.collect("chat", "m3", "moved again?", "poker group",
                          handle="loud@example.com")
        # Unmuting resurfaced m2; the ignored sender's m3 stays out of both
        # the rows and the count.
        found = activity.pending(self.conn, "event", event.key)
        self.assertEqual(found["strong_total"], 1)
        self.assertNotIn(m3, [i["id"] for i in found["strong"]])

    def test_automatic_low_relevance_hides_nothing(self):
        m1 = self.collect("chat", "m1", "poker saturday?", "poker group")
        event = self.poker()
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        m2 = self.collect("chat", "m2", "newsletter-ish poker trivia", "poker group")
        self.conn.execute("UPDATE spool SET priority='low'"
                          " WHERE archive_id = ?", (m2,))
        self.conn.commit()
        found = activity.pending(self.conn, "event", event.key)
        self.assertEqual([i["id"] for i in found["strong"]], [m2])


class TestThreadLevelNoChangeReview(_Base):
    """A bundle read in full with no diff is a no-change review of its thread."""

    def _rows(self, *ids):
        return [self.conn.execute("SELECT * FROM archive WHERE id = ?", (i,)).fetchone()
                for i in ids]

    def test_read_thread_advances_its_facts_but_no_other_thread(self):
        m1 = self.collect("chat", "m1", "poker saturday?", "poker group")
        poker = self.poker()
        live.update_event(self.conn, self.cfg, poker.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        m2 = self.collect("chat", "m2", "bring chips?", "poker group")
        other = self.collect("mail", "o1", "brunch Sunday?", "brunch thread")
        lunch, _ = live.add_event(self.conn, self.cfg, title="Brunch",
                                  when="2026-09-13",
                                  origin=live.Origin.of("test"))
        live.update_event(self.conn, self.cfg, lunch.key, note="plan",
                          origin=live.Origin.of("test", cited=[other]))
        m3 = self.collect("mail", "o2", "brunch moved?", "brunch thread")
        # The pass reads poker's thread and finds nothing actionable there.
        moved = activity.advance_thread(self.conn, self._rows(m1, m2),
                                        by_stage="dream")
        self.assertEqual(moved, 1)
        self.assertEqual(activity.pending(self.conn, "event", poker.key)["strong"],
                         [])
        # Brunch's thread was never read: still pending, untouched.
        self.assertEqual([i["id"] for i in activity.pending(
            self.conn, "event", lunch.key)["strong"]], [m3])

    def test_authored_turns_advance_nothing(self):
        from memcal import archive as archive_mod
        m1 = self.collect("chat", "m1", "poker saturday?", "poker group")
        event = self.poker()
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        turn_id = archive_mod.append(
            self.conn, stream="agent", external_id="hermes:s:turn:1",
            ts=db.now(), text="is poker still saturday?", thread="hermes:s",
            person="me", from_me=True, addressed_to="machine", gated=True,
            gate_reason="question")
        self.conn.commit()
        turn = self.conn.execute(
            "SELECT * FROM archive WHERE id = ?", (turn_id,)).fetchone()
        self.assertEqual(activity.advance_thread(self.conn, [turn],
                                                by_stage="dream"), 0)

    def test_calendar_revisions_nominate_only_their_item(self):
        from memcal.sources import ical
        one = {"uid": "u1", "title": "Dentist", "start": "2026-09-20T14:00:00",
               "end": "2026-09-20T15:00:00", "all_day": False, "location": "",
               "description": "", "url": "", "calendar_name": "Home",
               "calendar_uid": "cal-1", "writable": True}
        two = dict(one, uid="u2", title="Haircut")
        rev1 = ical._revision(ical._identity(one), one)
        rev2 = ical._revision(ical._identity(two), two)
        for eid, rev, title in (("c1", rev1, "Dentist"), ("c2", rev2, "Haircut")):
            self.conn.execute(
                "INSERT INTO archive(stream, external_id, ts, thread, text,"
                " created_at) VALUES('ical', ?, '2026-09-01T10:00:00', 'cal-1', ?, ?)",
                (rev, title, db.now()))
        self.conn.commit()
        dentist, _ = live.add_event(self.conn, self.cfg, title="Dentist visit",
                                    when="2026-09-20",
                                    origin=live.Origin.of("test"))
        from memcal import trace
        first = self.conn.execute(
            "SELECT id FROM archive WHERE external_id = ?", (rev1,)).fetchone()["id"]
        trace.stamp(self.conn, kind="event", ref=dentist.key, verb="inserted",
                    entity="calendar:Home", stage="ical", archive_ids=[first])
        self.conn.commit()
        # A changed haircut revision is activity on nobody's dentist appointment.
        changed = dict(two, start="2026-09-20T16:00:00")
        rev2b = ical._revision(ical._identity(two), changed)
        self.conn.execute(
            "INSERT INTO archive(stream, external_id, ts, thread, text, created_at)"
            " VALUES('ical', ?, '2026-09-01T11:00:00', 'cal-1', 'Haircut moved', ?)",
            (rev2b, db.now()))
        self.conn.commit()
        self.assertEqual(activity.pending(self.conn, "event", dentist.key)
                         ["strong_total"], 0)
        # A changed dentist revision is.
        moved = dict(one, start="2026-09-20T15:00:00")
        rev1b = ical._revision(ical._identity(one), moved)
        self.conn.execute(
            "INSERT INTO archive(stream, external_id, ts, thread, text, created_at)"
            " VALUES('ical', ?, '2026-09-01T12:00:00', 'cal-1', 'Dentist moved', ?)",
            (rev1b, db.now()))
        self.conn.commit()
        found = activity.pending(self.conn, "event", dentist.key)
        self.assertEqual(found["strong_total"], 1)
        self.assertIn("moved", found["strong"][0]["text"])


class TestSelectiveCoverage(_Base):
    """A later citation never covers the earlier lines it skipped."""

    def test_ack_three_while_two_is_unread(self):
        m1 = self.collect("chat", "m1", "poker saturday?", "poker group")
        event = self.poker()
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        m2 = self.collect("chat", "m2", "moved to Sunday?", "poker group")
        m3 = self.collect("chat", "m3", "at 5 Oak?", "poker group")
        self.assertEqual(activity.note_review(self.conn, "event", event.key,
                                              [m3], by_stage="test"), 1)
        found = activity.pending(self.conn, "event", event.key)
        self.assertEqual([i["id"] for i in found["strong"]], [m2])
        seen = activity.read(self.conn, "event", event.key)
        self.assertEqual([i["id"] for i in seen["items"]], [m2])

    def test_two_channels_stay_independent(self):
        a1 = self.collect("chat", "a1", "poker saturday?", "poker group")
        b1 = self.collect("mail", "b1", "poker saturday?", "poker list")
        event = self.poker()
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[a1, b1]))
        a2 = self.collect("chat", "a2", "moved to Sunday?", "poker group")
        b2 = self.collect("mail", "b2", "at 5 Oak?", "poker list")
        activity.note_review(self.conn, "event", event.key, [a2],
                             by_stage="test")
        found = activity.pending(self.conn, "event", event.key)
        self.assertEqual([i["id"] for i in found["strong"]], [b2])

    def test_unbundled_earlier_row_survives_a_thread_read(self):
        from memcal import gate
        m1 = self.collect("chat", "m1", "poker saturday?", "poker group")
        event = self.poker()
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        # A gated-out aside the bundle never carries...
        aside = self.collect("chat", "z0", "x", "poker group")
        self.conn.execute("UPDATE archive SET gated = 0 WHERE id = ?", (aside,))
        self.conn.commit()
        m2 = self.collect("chat", "m2", "moved to Sunday?", "poker group")
        rows = [self.conn.execute("SELECT * FROM archive WHERE id = ?", (i,)).fetchone()
                for i in (m1, m2)]
        activity.advance_thread(self.conn, rows, by_stage="dream")
        found = activity.pending(self.conn, "event", event.key)
        # ...while the read lines clear and the unread aside stays open.
        self.assertEqual([i["id"] for i in found["strong"]], [aside])

    def test_merge_keeps_holes_on_both_sides(self):
        t0 = self.collect("chat", "t0", "poker?", "poker group")
        t1 = self.collect("chat", "t1", "poker saturday?", "poker group")
        keep = self.poker(title="Poker A")
        live.update_event(self.conn, self.cfg, keep.key, note="plan",
                          origin=live.Origin.of("test", cited=[t1]))
        t2 = self.collect("chat", "t2", "poker sunday?", "poker group")
        drop, _ = live.add_event(self.conn, self.cfg, title="Poker B",
                                 when="2026-09-12",
                                 origin=live.Origin.of("test"))
        live.update_event(self.conn, self.cfg, drop.key, note="other",
                          origin=live.Origin.of("test", cited=[t2]))
        live.merge_events(self.conn, self.cfg, keep.key, drop.key,
                          origin=live.Origin.of("test"))
        self.assertEqual(activity.reviewed_ids(self.conn, "event", keep.key),
                         {t1, t2})
        found = activity.pending(self.conn, "event", keep.key)
        self.assertEqual([i["id"] for i in found["strong"]], [t0])


class TestSourceBackedPrecedence(_Base):
    """Cited evidence carries its own hour into write precedence."""

    def _set_address(self, event, address, **kw):
        return live.update_event(self.conn, self.cfg, event.key,
                                 location=address, **kw)

    def _turn_at(self, ts, text="user correction"):
        from memcal import archive as archive_mod
        turn_id = archive_mod.append(
            self.conn, stream="agent", external_id=f"hermes:s:turn:{ts}",
            ts=ts, text=text, thread="hermes:s", person="me", from_me=True,
            addressed_to="machine", gated=True, gate_reason="question")
        self.conn.commit()
        return turn_id

    def test_older_source_cannot_undo_a_newer_settlement(self):
        m0 = self.collect("chat", "m0", "dinner at 5 Oak?", "poker group",
                          ts="2026-09-12T11:00:00-04:00")
        event = self.poker()
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m0]))
        turn11 = self._turn_at("2026-09-12T11:00:00-04:00", "make it 5 Oak")
        self._set_address(event, "5 Oak",
                          origin=live.Origin.of("test", [turn11]))
        old = self.collect("chat", "o1", "how about 1 Pine?", "poker group",
                           ts="2024-05-01T19:00:00-04:00")
        before_history = self.conn.execute(
            "SELECT count(*) n FROM event_history").fetchone()["n"]
        with self.assertRaises(live.LiveError):
            self._set_address(event, "1 Pine",
                              origin=live.Origin.of("test", cited=[old]))
        row = self.conn.execute("SELECT location FROM events WHERE key = ?",
                                (event.key,)).fetchone()
        self.assertEqual(row["location"], "5 Oak")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) n FROM event_history").fetchone()["n"],
            before_history)
        # Refused, so unacknowledged: still pending, still no evidence row.
        self.assertEqual([i["id"] for i in activity.pending(
            self.conn, "event", event.key)["strong"]], [old])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) n FROM evidence WHERE kind='event' AND ref=?"
            " AND archive_id = ?", (event.key, old)).fetchone()["n"], 0)

    def test_newer_source_revises_despite_later_execution(self):
        m0 = self.collect("chat", "m0", "dinner?", "poker group",
                          ts="2026-09-12T10:00:00-04:00")
        event = self.poker()
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m0]))
        turn11 = self._turn_at("2026-09-12T11:00:00-04:00", "make it 5 Oak")
        self._set_address(event, "5 Oak",
                          origin=live.Origin.of("test", [turn11]))
        noon = self.collect("chat", "n1", "actually 7 Elm?", "poker group",
                            ts="2026-09-12T11:30:00-04:00")
        updated, changed = self._set_address(
            event, "7 Elm", origin=live.Origin.of("test", cited=[noon]))
        self.assertTrue(any("location" in c for c in changed))
        self.assertEqual(updated.location, "7 Elm")
        stamp = self.conn.execute(
            "SELECT evidence_ts FROM event_history WHERE event_id = ?"
            " AND field = 'location' ORDER BY id DESC LIMIT 1",
            (updated.id,)).fetchone()["evidence_ts"]
        self.assertEqual(db.parse_ts(stamp), db.parse_ts("2026-09-12T11:30:00-04:00"))

    def test_separate_fields_keep_separate_evidence_times(self):
        event = self.poker()
        day = self.collect("chat", "d1", "sunday?", "poker group",
                           ts="2026-09-12T09:00:00-04:00")
        place = self.collect("chat", "p1", "5 Oak?", "poker group",
                             ts="2026-09-12T10:00:00-04:00")
        live.update_event(self.conn, self.cfg, event.key, when="2026-09-13",
                          origin=live.Origin.of("test", cited=[day]))
        live.update_event(self.conn, self.cfg, event.key, location="5 Oak",
                          origin=live.Origin.of("test", cited=[place]))
        times = {r["field"]: r["evidence_ts"] for r in self.conn.execute(
            "SELECT field, evidence_ts FROM event_history WHERE event_id = ?",
            (event.id,))}
        self.assertEqual(db.parse_ts(times["date"]),
                         db.parse_ts("2026-09-12T09:00:00-04:00"))
        self.assertEqual(db.parse_ts(times["location"]),
                         db.parse_ts("2026-09-12T10:00:00-04:00"))

    def test_replay_of_a_cited_correction_is_a_no_op(self):
        event = self.poker()
        m1 = self.collect("chat", "m1", "moved to Sunday?", "poker group",
                          ts="2026-09-12T11:00:00-04:00")
        origin = live.Origin.of("test", cited=[m1])
        live.update_event(self.conn, self.cfg, event.key, when="2026-09-13",
                          origin=origin)
        before = self.conn.execute(
            "SELECT count(*) n FROM evidence WHERE kind='event' AND ref=?",
            (event.key,)).fetchone()["n"]
        _again, changed = live.update_event(self.conn, self.cfg, event.key,
                                            when="2026-09-13", origin=origin)
        self.assertEqual(changed, [])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) n FROM evidence WHERE kind='event' AND ref=?",
            (event.key,)).fetchone()["n"], before)
        self.assertEqual(self.conn.execute(
            "SELECT date FROM events WHERE key = ?", (event.key,)).fetchone()["date"],
            "2026-09-13")


if __name__ == "__main__":
    unittest.main()
