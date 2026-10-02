"""Shared date labels and successful-resume presentation contracts."""
import json
import tempfile
import unittest
from pathlib import Path

from memcal import calls, db, events, web_memory
from memcal.config import Config
from memcal.dream import retry


class TestCompactCalendarRanges(unittest.TestCase):
    def test_same_month_cross_month_and_cross_year(self):
        self.assertEqual(events.date_phrase('2026-10-09', '2026-10-11'), 'Oct 9–11')
        self.assertEqual(events.date_phrase('2026-10-31', '2026-11-02'), 'Oct 31–Nov 2')
        self.assertEqual(events.date_phrase('2026-12-31', '2027-01-02'), 'Dec 31, 2026–Jan 2, 2027')


class TestSuccessfulResumeResolvesRetryAlert(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def run_row(self, error='', diffs=1):
        return self.conn.execute(
            "INSERT INTO runs(started_at, finished_at, mode, bundles, diffs, error) VALUES('2026-10-01','2026-10-01','dream',1,?,?)",
            (diffs, error)).lastrowid

    def resume(self, run_id, source):
        folder = calls.shard(self.cfg.home, run_id)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'replay.json').write_text(json.dumps({'resumed_from': [source]}))

    def test_successful_resume_preserves_error_but_removes_retry(self):
        failed = self.run_row('failed', 0)
        clean = self.run_row()
        self.resume(clean, failed)
        row = next(r for r in web_memory.runs(self.conn, cfg=self.cfg) if r['id'] == failed)
        self.assertEqual(row['resolved_by'], clean)
        self.assertFalse(row['retryable'])
        self.assertEqual(row['error'], 'failed')
        self.assertEqual(row['outcome'], 'failed')

    def test_unrelated_success_does_not_resolve_failure(self):
        failed = self.run_row('failed', 0)
        self.run_row()
        self.assertNotIn(failed, retry.resolved_runs(self.conn, self.cfg.home))

    def test_failed_resume_remains_actionable_then_success_resolves_chain(self):
        first = self.run_row('failed', 0)
        second = self.run_row('failed', 0)
        self.resume(second, first)
        self.assertEqual(retry.resolved_runs(self.conn, self.cfg.home), {})
        third = self.run_row()
        self.resume(third, second)
        self.assertEqual(retry.resolved_runs(self.conn, self.cfg.home), {first: third, second: third})


if __name__ == '__main__':
    unittest.main()
