"""A record open includes bounded pending activity and contextual source messages."""

from __future__ import annotations

import sys
import re
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import activity, archive, brief, db, detail, live, textclean, threads, todos, trace


class OpenIncludesPendingActivity(unittest.TestCase):
    """A brief freshness hint resolves with a single `open_handle` call."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from memcal.config import Config
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)
        db.set_today("2026-09-12")
        self.addCleanup(db.set_today, None)

    def tearDown(self):
        self.conn.close()

    def test_flagged_row_opens_citations_and_pending_messages_in_one_read(self):
        m1 = archive.append(
            self.conn, channel="imessage", external_id="o1",
            ts="2026-09-10T10:00:00", thread="t1", text="poker saturday?",
            person="Jordan", handle="j", from_me=False)
        self.conn.commit()
        event, _ = live.add_event(
            self.conn, self.cfg, title="Poker night", when="2026-09-12",
            origin=live.Origin.of("test"))
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        m2 = archive.append(
            self.conn, channel="imessage", external_id="o2",
            ts="2026-09-11T10:00:00", thread="t1", text="moved to Sunday?",
            person="Jordan", handle="j", from_me=False)
        self.conn.commit()

        text = brief.render(self.conn, self.cfg)
        self.assertIn("New activity:", text)
        self.assertIn(f"memcal_open(ref=E{event.id})", text)

        opened = detail.open_handle(self.conn, self.cfg, f"E{event.id}")
        self.assertIn("moved to Sunday?", opened)
        self.assertIn(f"[{m2}]", opened)
        self.assertIn("pending messages (not cited evidence", opened)
        self.assertNotIn("memcal_activity", opened)
        self.assertNotIn("memcal_activity", text)
        self.assertNotIn("full thread", opened)
        self.assertIn("poker saturday?", opened)
        self.assertNotIn("moved to Sunday?", detail._sources_text(self.conn, "event", event.key))
        self.assertEqual(activity.reviewed_ids(self.conn, "event", event.key), {m1})

        page = activity.read(self.conn, "event", event.key)
        self.assertIn(m2, [item['id'] for item in page['items']])

    def test_quiet_row_open_carries_no_activity_section(self):
        event, _ = live.add_event(
            self.conn, self.cfg, title="Quiet dinner", when="2026-09-13",
            origin=live.Origin.of("test"))
        opened = detail.open_handle(self.conn, self.cfg, f"E{event.id}")
        self.assertNotIn("pending messages (", opened)


class TestCappedOpenMessages(unittest.TestCase):
    def setUp(self):
        from memcal.config import Config
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)
        self.addCleanup(self.conn.close)
        db.set_today("2032-03-04T12:00:00-05:00")
        self.addCleanup(db.set_today, None)
        self.sequence = 0
        first = self.line("Let's visit the studio tomorrow")
        self.event, _ = live.add_event(self.conn, self.cfg, title="Studio visit",
                                        when="2032-03-05",
                                        origin=live.Origin.of("test", cited=[first]))

    def line(self, text):
        self.sequence += 1
        aid = archive.append(self.conn, channel="imessage", external_id=str(self.sequence),
                             ts=db.now(), thread="studio", person="Rowan", handle="rowan", text=text)
        self.conn.commit()
        return aid

    def test_overflow_cursor_continues_without_losing_a_correction_or_marking_review(self):
        for n in range(55):
            self.line(f"Short message {n}")
        correction = self.line("The studio visit is cancelled")
        handle = f"E{self.event.id}"
        opened = detail.open_handle(self.conn, self.cfg, handle)
        self.assertIn("omitted", opened)
        cursor = int(re.search(r"Continue with memcal_activity\(handle=E\d+, cursor=(\d+)\)", opened)[1])
        rest = activity.read(self.conn, "event", self.event.key, cursor=cursor, limit=50)
        self.assertIn(correction, [row["id"] for row in rest["items"]])
        self.assertEqual(activity.read(self.conn, "event", self.event.key)["total"], 56)
        self.assertEqual(detail.open_handle(self.conn, self.cfg, handle), opened)

    def test_one_huge_message_is_omitted_whole_with_cursor_zero(self):
        self.line("huge-message-fragment " * 8000 + "cancel the visit")
        opened = detail.open_handle(self.conn, self.cfg, f"E{self.event.id}")
        section = opened.split("pending messages (", 1)[1]
        self.assertLessEqual(textclean.estimate_tokens("pending messages (" + section),
                             detail.PENDING_TOKEN_CAP)
        self.assertIn("cursor=0", section)
        self.assertIn("1 omitted", section)
        self.assertNotIn("nothing new", section)
        self.assertNotIn("huge-message-fragment", opened)

    def test_muted_pending_and_nearby_context_stay_hidden(self):
        self.line("Hidden studio discussion")
        threads.decide(self.conn, "imessage", "studio", "mute")
        opened = detail.open_handle(self.conn, self.cfg, f"E{self.event.id}")
        self.assertNotIn("Hidden studio discussion", opened)


class TestTerseTodoCitationContext(unittest.TestCase):
    def test_open_restores_original_context_without_promoting_it_to_citations(self):
        from memcal.config import Config
        with tempfile.TemporaryDirectory() as temp:
            cfg = Config(home=Path(temp)); cfg.ensure_dirs()
            conn = db.open_db(cfg.db_path)
            self.addCleanup(conn.close)
            db.set_today("2032-03-04")
            self.addCleanup(db.set_today, None)
            ids = []
            for n, message in enumerate(("We need a portable mixer for the rehearsal",
                                         "I have the microphones already",
                                         "I thought I bought a mixer",
                                         "I'll look around")):
                ids.append(archive.append(conn, channel="imessage", external_id=f"old-{n}",
                                           ts=f"2031-08-26T16:37:{n:02d}-04:00", thread="band",
                                           text=message, from_me=True))
            todo, _ = todos.open_todo(conn, "Look for the mixer", written_by="dream")
            trace.stamp(conn, kind="todo", ref=todo.key, archive_ids=[ids[-1]])
            conn.commit()
            opened = detail.open_handle(conn, cfg, f"T{todo.id}")
            self.assertIn("portable mixer for the rehearsal", opened)
            self.assertIn("nearby source context (not citations", opened)
            self.assertEqual(activity.reviewed_ids(conn, "todo", todo.key), {ids[-1]})
            cited = {row[0] for row in conn.execute("SELECT archive_id FROM evidence WHERE kind='todo'")}
            self.assertEqual(cited, {ids[-1]})


class WholeThreadTailStaysFiltered(unittest.TestCase):
    """`thread_tail` uses the same mute/explicit-ignore filter as pending."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        from memcal.config import Config
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)
        db.set_today("2026-09-12")
        self.addCleanup(db.set_today, None)

    def tearDown(self):
        self.conn.close()

    def test_muted_thread_tail_hides_its_lines(self):
        from memcal import threads
        m1 = archive.append(
            self.conn, channel="imessage", external_id="o1",
            ts="2026-09-10T10:00:00", thread="t1", text="hello?",
            person="Jordan", handle="j", from_me=False)
        self.conn.commit()
        threads.decide(self.conn, "imessage", "t1", "mute")
        self.conn.commit()
        lines, total = activity.thread_tail(self.conn, "imessage", "t1")
        self.assertEqual(total, 0)
        self.assertEqual(lines, [])
        # The line is really there — the filter hides it, not absence.
        self.assertIsNotNone(
            self.conn.execute("SELECT id FROM archive WHERE id = ?",
                              (m1,)).fetchone())


if __name__ == "__main__":
    unittest.main()
