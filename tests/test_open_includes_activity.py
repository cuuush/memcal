"""`memcal_open` carries pending activity so a flagged row needs one call."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import activity, archive, brief, db, detail, live


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

    def test_flagged_row_opens_with_its_new_messages(self):
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
        self.assertIn("new activity since last review", opened)
        self.assertIn(f"memcal_activity(handle=E{event.id})", opened)
        self.assertIn("full thread imessage/t1", opened)
        # Reviewed context rides along unmarked; pending is marked new.
        self.assertIn("poker saturday?", opened)
        self.assertIn("(new)", opened)

        page = activity.read(self.conn, "event", event.key)
        for item in page["items"]:
            self.assertIn(f"[{item['id']}]", opened)

    def test_quiet_row_open_carries_no_activity_section(self):
        event, _ = live.add_event(
            self.conn, self.cfg, title="Quiet dinner", when="2026-09-13",
            origin=live.Origin.of("test"))
        opened = detail.open_handle(self.conn, self.cfg, f"E{event.id}")
        self.assertNotIn("new activity since last review", opened)


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
        self.assertIsNotNone(m1)


if __name__ == "__main__":
    unittest.main()
