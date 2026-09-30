"""Review baselines: pre-link history is not "since reviewed".

Pending activity counts arrivals *after* a fact first cited a source. History
already present at link time (outside the unread queue) predates the fact, was
never bundled for it — processed spool never re-bundles — and must not inflate
the brief hint to "99+ message(s) since this plan was reviewed" right after
dream ran. Holes above the baseline (omitted pages, arrivals during a pass)
stay pending exactly as before.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import activity, archive, brief, db, live, trace


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = __import__("memcal.config", fromlist=["Config"]).Config(
            home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)
        db.set_today("2026-09-12")
        self.addCleanup(db.set_today, None)

    def tearDown(self):
        self.conn.close()

    def collect(self, channel, eid, text, thread, handle="friend@example.com",
                ts=None, **kw):
        from memcal.sources import base
        from memcal.sources.base import IngestReport
        base.deliver(self.conn, IngestReport(channel=channel), channel=channel,
                     external_id=eid, ts=ts or db.now(), text=text, thread=thread,
                     handle=handle, **kw)
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM archive WHERE channel = ? AND external_id = ?",
            (channel, eid)).fetchone()
        return row["id"] if row else None

    def process_spool(self):
        """Simulate a dream pass having read everything queued (no diffs)."""
        ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM spool WHERE processed_at IS NULL").fetchall()]
        if ids:
            archive.spool_mark(self.conn, ids, 1)
        return ids


class TestLinkBaselineExcludesProcessedHistory(_Base):
    """A plan linked to a long, already-processed thread starts at zero."""

    def test_linking_a_busy_thread_shows_no_pending(self):
        for i in range(150):
            self.collect("chat", f"old-{i}", f"chatter {i} saturday?",
                         "busy group", ts=f"2026-09-01T10:{i % 60:02d}:00-04:00")
        self.process_spool()
        recent = [r["id"] for r in self.conn.execute(
            "SELECT id FROM archive ORDER BY id DESC LIMIT 2").fetchall()]
        event, _ = live.add_event(self.conn, self.cfg, title="Poker night",
                                  when="2026-09-12", origin=live.Origin.of("test"))
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=recent))
        found = activity.pending(self.conn, "event", event.key,
                                 strong_only=True, conversational_only=True)
        self.assertEqual(found["strong_total"], 0)
        self.assertEqual(found["strong"], [])
        text = brief.render(self.conn, self.cfg)
        self.assertNotIn("New activity", text)
        self.assertNotIn("99+", text)

    def test_genuinely_new_arrivals_still_flag_with_exact_ids(self):
        for i in range(150):
            self.collect("chat", f"old-{i}", f"chatter {i} saturday?",
                         "busy group", ts=f"2026-09-01T10:{i % 60:02d}:00-04:00")
        self.process_spool()
        recent = [r["id"] for r in self.conn.execute(
            "SELECT id FROM archive ORDER BY id DESC LIMIT 2").fetchall()]
        event, _ = live.add_event(self.conn, self.cfg, title="Poker night",
                                  when="2026-09-12", origin=live.Origin.of("test"))
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=recent))
        new_ids = [self.collect("chat", f"new-{i}", f"moved to Sunday? {i}",
                                "busy group")
                   for i in range(3)]
        found = activity.pending(self.conn, "event", event.key,
                                 strong_only=True, conversational_only=True)
        self.assertEqual(found["strong_total"], 3)
        self.assertEqual([i["id"] for i in found["strong"]], new_ids)
        text = brief.render(self.conn, self.cfg)
        self.assertIn("3 message(s) since this plan was reviewed", text)

    def test_a_dream_no_change_review_clears_only_what_it_read(self):
        for i in range(150):
            self.collect("chat", f"old-{i}", f"chatter {i} saturday?",
                         "busy group", ts=f"2026-09-01T10:{i % 60:02d}:00-04:00")
        self.process_spool()
        recent = [r["id"] for r in self.conn.execute(
            "SELECT id FROM archive ORDER BY id DESC LIMIT 2").fetchall()]
        event, _ = live.add_event(self.conn, self.cfg, title="Poker night",
                                  when="2026-09-12", origin=live.Origin.of("test"))
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=recent))
        new_ids = [self.collect("chat", f"new-{i}", f"moved to Sunday? {i}",
                                "busy group")
                   for i in range(3)]
        self.process_spool()
        activity.advance_thread(
            self.conn,
            [dict(channel="chat", thread="busy group", id=i) for i in new_ids],
            by_stage="dream")
        found = activity.pending(self.conn, "event", event.key,
                                 strong_only=True, conversational_only=True)
        self.assertEqual(found["strong_total"], 0)
        self.assertNotIn("New activity", brief.render(self.conn, self.cfg))

    def test_holes_above_the_baseline_stay_pending(self):
        self.collect("chat", "m1", "poker saturday?", "poker group")
        event, _ = live.add_event(self.conn, self.cfg, title="Poker night",
                                  when="2026-09-12", origin=live.Origin.of("test"))
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[
                              self.conn.execute(
                                  "SELECT id FROM archive WHERE external_id='m1'"
                              ).fetchone()["id"]]))
        self.process_spool()
        ids = [self.collect("chat", f"n{i}", f"update {i} sunday?", "poker group")
               for i in range(3)]
        # A partial review covers two of the three: the hole stays pending.
        activity.note_review(self.conn, "event", event.key, ids[:2],
                             by_stage="dream")
        found = activity.pending(self.conn, "event", event.key,
                                 strong_only=True, conversational_only=True)
        self.assertEqual([i["id"] for i in found["strong"]], ids[2:])


class TestUnreadQueueAtLinkStillFlags(_Base):
    """Lines still waiting for dream at link time are not swallowed."""

    def test_unprocessed_spool_members_count_until_dream_reads_them(self):
        ids = [self.collect("chat", f"m{i}", f"planning point {i} saturday?",
                            "poker group")
               for i in range(5)]
        event, _ = live.add_event(self.conn, self.cfg, title="Poker night",
                                  when="2026-09-12", origin=live.Origin.of("test"))
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=ids[:2]))
        found = activity.pending(self.conn, "event", event.key,
                                 strong_only=True, conversational_only=True)
        self.assertEqual([i["id"] for i in found["strong"]], ids[2:])
        # Processing the queue without a review changes nothing; the lines
        # were never considered for this plan.
        self.process_spool()
        found = activity.pending(self.conn, "event", event.key,
                                 strong_only=True, conversational_only=True)
        self.assertEqual([i["id"] for i in found["strong"]], ids[2:])
        activity.advance_thread(
            self.conn,
            [dict(channel="chat", thread="poker group", id=i) for i in ids[2:]],
            by_stage="dream")
        self.assertEqual(activity.pending(
            self.conn, "event", event.key,
            strong_only=True, conversational_only=True)["strong_total"], 0)


class TestLateLinkedThreadBaselinedSeparately(_Base):
    """Each linked scope baselines at its own first cite."""

    def test_a_second_thread_does_not_import_its_history(self):
        for i in range(100):
            self.collect("chat", f"a-{i}", f"crew chatter {i} saturday?",
                         "crew", ts="2026-09-01T10:00:00-04:00")
        for i in range(100):
            self.collect("chat", f"b-{i}", f"neighbors chatter {i} sunday?",
                         "neighbors", ts="2026-09-01T10:00:00-04:00")
        self.process_spool()
        first = self.conn.execute(
            "SELECT id FROM archive WHERE external_id='a-99'").fetchone()["id"]
        event, _ = live.add_event(self.conn, self.cfg, title="Poker night",
                                  when="2026-09-12", origin=live.Origin.of("test"))
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[first]))
        other = self.conn.execute(
            "SELECT id FROM archive WHERE external_id='b-99'").fetchone()["id"]
        live.update_event(self.conn, self.cfg, event.key, note="both rooms",
                          origin=live.Origin.of("test", cited=[other]))
        found = activity.pending(self.conn, "event", event.key,
                                 strong_only=True, conversational_only=True)
        self.assertEqual(found["strong_total"], 0)
        fresh = self.collect("chat", "b-new", "neighbors moved to monday?",
                             "neighbors")
        found = activity.pending(self.conn, "event", event.key,
                                 strong_only=True, conversational_only=True)
        self.assertEqual([i["id"] for i in found["strong"]], [fresh])


class TestBackfillHealsPreUpgradeLinks(_Base):
    """Existing stores gain baselines on open; old threads stop reading as new."""

    def test_reopening_clears_pre_link_history_but_keeps_the_queue(self):
        for i in range(120):
            self.collect("chat", f"old-{i}", f"chatter {i} saturday?",
                         "busy group", ts="2026-09-01T10:00:00-04:00")
        self.process_spool()
        recent = [r["id"] for r in self.conn.execute(
            "SELECT id FROM archive ORDER BY id DESC LIMIT 2").fetchall()]
        event, _ = live.add_event(self.conn, self.cfg, title="Poker night",
                                  when="2026-09-12", origin=live.Origin.of("test"))
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=recent))
        queued = self.collect("chat", "queued", "moved to Sunday?", "busy group")
        # Simulate a pre-upgrade store: evidence but no baseline rows.
        self.conn.execute("DELETE FROM review_baselines")
        self.conn.execute("DELETE FROM meta WHERE key = 'review_baselines.generation'")
        self.conn.commit()
        before = activity.pending(self.conn, "event", event.key,
                                  strong_only=True)["strong_total"]
        self.assertGreaterEqual(before, 118)
        self.conn.close()
        self.conn = db.open_db(self.cfg.db_path)
        after = activity.pending(self.conn, "event", event.key,
                                 strong_only=True)
        # Processed history is baselined away; the still-queued line flags.
        self.assertEqual([i["id"] for i in after["strong"]], [queued])
        self.assertEqual(after["strong_total"], 1)


class TestBaselineLifecycleFollowsEvidence(_Base):
    """Baselines merge with the surviving row and leave with a deleted one."""

    def test_merge_keeps_the_survivors_scopes_and_inherits_missing_ones(self):
        m1 = self.collect("chat", "m1", "poker saturday?", "room a")
        m2 = self.collect("chat", "m2", "poker sunday?", "room b")
        self.process_spool()
        keep, _ = live.add_event(self.conn, self.cfg, title="Poker night",
                                 when="2026-09-12", origin=live.Origin.of("test"))
        live.update_event(self.conn, self.cfg, keep.key, note="keep plan",
                          origin=live.Origin.of("test", cited=[m1]))
        drop, _ = live.add_event(self.conn, self.cfg, title="Poker night",
                                 when="2026-09-13", origin=live.Origin.of("test"))
        live.update_event(self.conn, self.cfg, drop.key, note="drop plan",
                          origin=live.Origin.of("test", cited=[m2]))
        self.assertNotEqual(keep.key, drop.key)
        from memcal import events
        self.assertIsNotNone(events.merge(self.conn, keep.key, drop.key))
        base = activity.baselines(self.conn, "event", keep.key)
        self.assertIn(("t", "chat", "room a"), base)
        self.assertIn(("t", "chat", "room b"), base)
        self.assertEqual(activity.baselines(self.conn, "event", drop.key), {})

    def test_delete_drops_the_baselines(self):
        m1 = self.collect("chat", "m1", "poker saturday?", "room a")
        self.process_spool()
        event, _ = live.add_event(self.conn, self.cfg, title="Poker night",
                                  when="2026-09-12", origin=live.Origin.of("test"))
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        self.assertTrue(activity.baselines(self.conn, "event", event.key))
        from memcal import events
        events.delete(self.conn, event.key)
        self.assertEqual(activity.baselines(self.conn, "event", event.key), {})


class TestFamilyBaselineOnUnthreadedStreams(_Base):
    """Calendar item families baseline like conversations do."""

    def test_old_calendar_revisions_do_not_flag_but_new_ones_do(self):
        from memcal.sources import ical
        item = {"uid": "u-dentist", "title": "Dentist",
                "start": "2026-09-20T14:00:00", "end": "2026-09-20T15:00:00",
                "all_day": False, "location": "", "description": "", "url": "",
                "calendar_name": "Home", "calendar_uid": "cal-1", "writable": True}
        revs = []
        for hour in ("08", "09", "10"):
            rev = ical._revision(ical._identity(item), dict(
                item, start=f"2026-09-20T1{hour}:00:00"))
            self.conn.execute(
                "INSERT INTO archive(channel, external_id, ts, thread, text,"
                " gated, created_at) VALUES('ical', ?, '2026-09-01T10:00:00',"
                " 'cal-1', 'Dentist', 1, ?)",
                (rev, db.now()))
            revs.append(rev)
        self.conn.commit()
        event, _ = live.add_event(self.conn, self.cfg, title="Dentist visit",
                                  when="2026-09-20", origin=live.Origin.of("test"))
        first = self.conn.execute(
            "SELECT id FROM archive WHERE external_id = ?", (revs[-1],)).fetchone()["id"]
        trace.stamp(self.conn, kind="event", ref=event.key, verb="inserted",
                    entity="calendar:Home", stage="ical", archive_ids=[first])
        self.conn.commit()
        self.assertEqual(
            activity.pending(self.conn, "event", event.key)["strong_total"], 0)
        moved = ical._revision(ical._identity(item),
                               dict(item, start="2026-09-20T15:00:00"))
        self.conn.execute(
            "INSERT INTO archive(channel, external_id, ts, thread, text,"
            " gated, created_at) VALUES('ical', ?, '2026-09-01T12:00:00',"
            " 'cal-1', 'Dentist moved', 1, ?)",
            (moved, db.now()))
        self.conn.commit()
        self.assertEqual(
            activity.pending(self.conn, "event", event.key)["strong_total"], 1)


if __name__ == "__main__":
    unittest.main()
