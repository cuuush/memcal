"""Overview routing and consolidation dates stay explicit in prepared context."""

import unittest

from tests._support import Base
from memcal import brief, db


class TestOverviewRoutingAndDreamTime(Base):
    def setUp(self):
        super().setUp()
        db.set_today("2032-03-04T12:00:00-05:00")

    def run_row(self, stamp, *, mode="nightly", error=None, finished=True):
        self.conn.execute(
            "INSERT INTO runs(started_at, finished_at, mode, error) VALUES(?,?,?,?)",
            (stamp, stamp if finished else None, mode, error))
        self.conn.commit()

    def test_brief_distinguishes_overview_from_event_details_without_forcing_both_tools(self):
        text = brief.render(self.conn, self.cfg)
        self.assertIn("whole-week overviews use this summary directly", text)
        self.assertIn("do not open rows just to double-check", text)
        self.assertIn("For a specific event, open its handle first", text)
        self.assertIn("no successful completed run recorded", text)
        self.assertNotIn("memcal_activity", text)
        self.assertNotIn("whole-week overviews", brief.render(self.conn, self.cfg, surface="cli"))

    def test_failed_partial_dry_and_running_passes_do_not_claim_new_consolidation(self):
        self.run_row("2032-03-03T04:00:00-05:00")
        self.run_row("2032-03-03T10:00:00-05:00", error="one bundle failed")
        self.run_row("2032-03-03T11:00:00-05:00", mode="dry-run")
        self.run_row("2032-03-04T11:00:00-05:00", finished=False)
        text = brief.render(self.conn, self.cfg)
        self.assertIn("finished 2032-03-03T04:00:00-05:00 (nightly)", text)
        self.assertNotIn("finished 2032-03-04", text)
        self.assertIn("newer messages may be unreviewed", text)
        self.assertIn("Coverage warnings still apply", text)

    def test_a_completed_ondemand_pass_names_its_scope_and_changes_snapshot(self):
        self.run_row("2032-03-03T04:00:00-05:00")
        first = brief.render(self.conn, self.cfg)
        self.run_row("2032-03-04T11:00:00-05:00", mode="ondemand")
        latest = brief.render(self.conn, self.cfg)
        self.assertIn("finished 2032-03-04T11:00:00-05:00 (ondemand)", latest)
        self.assertNotEqual(first, latest)


if __name__ == "__main__":
    unittest.main()
