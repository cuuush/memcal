"""People remain visible as an event moves into the brief's active window."""

import unittest
from datetime import date

from _support import Base
from memcal import brief, db, events, mcp_server, web_memory


class TestLaterEventsPreservePeople(Base):
    def setUp(self):
        super().setUp()
        db.set_today(date(2026, 10, 8))
        self.cfg.days_forward = 7

    def visit(self, **fields):
        return events.upsert(self.conn, {
            "title": "Visit Casey", "date": "2026-10-16",
            "kind": "availability", "subject": "Rowan Vale",
            "participants": ["Rowan Vale"], "status": "tentative",
            "location": "Casey's place", "note": "Considering a weekend visit.",
            **fields,
        }, written_by="dream:test", match=False).event

    def line(self, event, text=None):
        text = brief.render(self.conn, self.cfg) if text is None else text
        return next(line for line in text.splitlines()
                    if line.startswith(brief.source_tag("event", event.id)))

    def test_subject_in_participants_is_visible_in_later_and_this_week(self):
        event = self.visit()
        later = self.line(event)
        self.assertIn("Rowan Vale", later)
        self.assertIn("maybe", later)
        self.assertNotIn("Casey's place", later)
        self.assertNotIn("Considering a weekend visit", later)
        db.set_today(date(2026, 10, 9))
        self.assertEqual(self.line(event), later)

    def test_user_event_keeps_companions_and_caps_large_groups(self):
        event = self.visit(subject="me", participants=[
            "Alex", "Blair", "Drew", "Harper", "Rowan"])
        self.assertIn("with Alex, Blair, Drew, Harper +1", self.line(event))

    def test_subject_outside_participants_remains_distinct(self):
        event = self.visit(participants=["Harper"])
        line = self.line(event)
        self.assertIn('Rowan Vale: "Visit Casey"', line)
        self.assertIn("with Harper", line)

    def test_web_memory_exposes_the_same_people_on_the_brief_line(self):
        event = self.visit()
        result = web_memory.memory(self.conn, self.cfg)
        self.assertIn("Rowan Vale", self.line(event, result["brief"]))
        target = result["targets"][f"E{event.id}"]
        self.assertEqual(target["ref"], event.key)

    def test_later_keeps_time_links_hosts_and_span_duration(self):
        event = self.visit(
            title="Weekend gathering | Partiful", until="2026-10-18", time="19:00",
            hosts=["Blair"], rsvp_url="https://partiful.com/e/example",
            join_url="https://meet.example/join")
        line = self.line(event)
        for text in ("Oct 16–18", "7pm", "not replied", "with Rowan Vale",
                     "hosted by Blair", "via Partiful", "3 days",
                     "invite:", "join: https://meet.example/join"):
            self.assertIn(text, line)


class TestMonthBriefAndYearLookup(Base):
    def setUp(self):
        super().setUp()
        db.set_today(date(2026, 10, 8))

    def event(self, day, title):
        return events.upsert(self.conn, {
            "date": day, "title": title, "status": "confirmed",
            "participants": ["Rowan Vale"],
        }, match=False).event

    def test_default_reaches_thirty_days_and_preserves_custom_window(self):
        self.event("2026-11-07", "Within thirty days")
        self.event("2026-11-08", "Outside thirty days")
        self.assertEqual(self.cfg.days_forward, 30)
        block = brief._week_block(self.conn, self.cfg, db.today())
        self.assertIn("Within thirty days", block)
        self.assertNotIn("Outside thirty days", block)
        self.assertIn("## Upcoming", block)
        self.assertIn("2026-11-07", block)
        self.cfg.days_forward = 7
        self.assertNotIn("Within thirty days",
                         brief._week_block(self.conn, self.cfg, db.today()))

    def test_brief_suggests_exact_month_and_year_calls_including_leap_years(self):
        for year, days in ((2026, 365), (2028, 366)):
            db.set_today(date(year, 12, 20))
            block = brief._week_block(self.conn, self.cfg, db.today())
            self.assertIn(f"memcal_list_month(month='{year}-12')", block)
            self.assertIn(f"memcal_list_days(when='{year}-01-01', days={days})", block)
            self.assertIn(f"{year + 1}-01-19", block)

    def test_year_lookup_includes_december_31_and_excludes_next_year(self):
        server = mcp_server.Server.__new__(mcp_server.Server)
        server.conn, server.cfg = self.conn, self.cfg
        for year, days in ((2026, 365), (2028, 366)):
            with self.subTest(year=year):
                last = self.event(f"{year}-12-31", f"Last day of {year}")
                self.event(f"{year + 1}-01-01", f"First day of {year + 1}")
                result = server.call("memcal_list_days", {
                    "when": f"{year}-01-01", "days": days})
                self.assertIn(f"{year}-01-01 – {year}-12-31", result)
                self.assertIn(last.title, result)
                self.assertNotIn(f"First day of {year + 1}", result)

    def test_day_month_and_year_queries_include_visits_already_in_progress(self):
        event = events.upsert(self.conn, {
            "date": "2025-12-29", "until": "2026-01-03", "title": "Winter visit",
            "kind": "availability", "subject": "Rowan Vale",
            "participants": ["Rowan Vale"], "status": "confirmed",
        }).event
        server = mcp_server.Server.__new__(mcp_server.Server)
        server.conn, server.cfg = self.conn, self.cfg
        for name, args in (
            ("memcal_list_days", {"when": "2026-01-02"}),
            ("memcal_list_days", {"when": "2026-01-03"}),
            ("memcal_list_month", {"month": "2026-01"}),
            ("memcal_list_days", {"when": "2026-01-01", "days": 365}),
        ):
            with self.subTest(name=name, args=args):
                result = server.call(name, args)
                self.assertIn("Winter visit", result)
                self.assertIn(f"E{event.id}", result)
        self.assertNotIn("Winter visit", server.call(
            "memcal_list_days", {"when": "2026-01-04"}))
        self.assertNotIn("Winter visit", server.call(
            "memcal_list_month", {"month": "2026-02"}))

    def test_trimmed_month_does_not_claim_complete_event_coverage(self):
        self.cfg.brief_token_cap = 300
        for day in range(9, 30):
            self.event(f"2026-10-{day:02d}", f"Gathering {day} with a long description " * 3)
        result = brief.render(self.conn, self.cfg)
        self.assertIn("coverage incomplete", result)
        self.assertNotIn("[complete for", result)
        self.assertIn("memcal_list_month", result)

    def test_existing_input_warning_does_not_hide_omitted_known_events(self):
        event = self.event("2026-10-16", "Visit")
        text = ("## Upcoming\n[complete for 2026-10-05 – 2026-11-07]\n"
                "[coverage incomplete — unreviewed traffic not linked to any plan]\n")
        result = brief._reconcile_coverage(
            self.conn, text, 1500, id_to_key={event.id: event.key})
        self.assertNotIn("[complete for", result)
        self.assertEqual(result.count("coverage incomplete"), 1)


if __name__ == "__main__":
    unittest.main()
