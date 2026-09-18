"""Regression tests for the six boundaries found in the freshness review.

Each guards one finding from DAYTIME_FRESHNESS_REVIEW_7614BCE.md: evidence
precedence must hold at a row's creation, through clears, and against invalid or
unreadable citations; the backlog must not hide new traffic behind a stale link;
and the hard trim must keep an event and its warning as one unit.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import activity, archive, brief, db, live, textclean, threads, trace


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

    def turn_at(self, ts, text="user turn"):
        turn = archive.append(
            self.conn, channel="agent", external_id=f"turn:{ts}:{text}",
            ts=ts, text=text, thread="conversation", person="me", from_me=True,
            addressed_to="machine", gated=True, gate_reason="question")
        self.conn.commit()
        return turn

    def location_of(self, event):
        return self.conn.execute(
            "SELECT location FROM events WHERE key = ?", (event.key,)).fetchone()[
            "location"]


class TestCreationEvidenceBaseline(_Base):
    """Finding 1: a value established at creation is protected before its first edit."""

    def test_a_year_old_source_cannot_overwrite_a_freshly_created_field(self):
        turn = self.turn_at("2026-09-12T11:00:00-04:00", "poker at 5 Oak")
        event, _ = live.add_event(self.conn, self.cfg, title="Poker",
                                  when="2026-09-12", location="5 Oak",
                                  origin=live.Origin.of("test", [turn]))
        self.assertEqual(self.location_of(event), "5 Oak")
        old = self.collect("chat", "o1", "how about 1 Pine?", "poker group",
                           ts="2024-05-01T19:00:00-04:00")
        before = self.conn.execute(
            "SELECT count(*) n FROM event_history").fetchone()["n"]
        with self.assertRaises(live.LiveError):
            live.update_event(self.conn, self.cfg, event.key, location="1 Pine",
                              origin=live.Origin.of("test", cited=[old]))
        self.assertEqual(self.location_of(event), "5 Oak")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) n FROM event_history").fetchone()["n"], before)

    def test_a_newer_source_wins_even_when_it_predates_execution(self):
        # Founding evidence 11:00; the tool ran at noon. A source said at 11:30
        # is older than execution but newer than the founding evidence, so it
        # must apply — the floor is the evidence time, never run time.
        db.set_today("2026-09-12T12:00:00-04:00")
        turn = self.turn_at("2026-09-12T11:00:00-04:00", "poker at 5 Oak")
        event, _ = live.add_event(self.conn, self.cfg, title="Poker",
                                  when="2026-09-12", location="5 Oak",
                                  origin=live.Origin.of("test", [turn]))
        newer = self.collect("chat", "n1", "actually 7 Elm", "poker group",
                             ts="2026-09-12T11:30:00-04:00")
        _updated, changed = live.update_event(
            self.conn, self.cfg, event.key, location="7 Elm",
            origin=live.Origin.of("test", cited=[newer]))
        self.assertTrue(any("location" in c for c in changed))
        self.assertEqual(self.location_of(event), "7 Elm")

    def test_an_unsourced_creation_is_protected_from_a_year_old_source(self):
        # No originating turn, so the value is established at creation. A source
        # from before that day cannot overwrite it on the first edit, but a
        # genuinely newer source still can.
        db.set_today("2026-09-12T14:00:00-04:00")
        event, _ = live.add_event(self.conn, self.cfg, title="Poker",
                                  when="2026-09-12", location="5 Oak",
                                  origin=live.Origin.of("test"))
        old = self.collect("chat", "o1", "1 Pine?", "poker group",
                           ts="2024-05-01T19:00:00-04:00")
        with self.assertRaises(live.LiveError):
            live.update_event(self.conn, self.cfg, event.key, location="1 Pine",
                              origin=live.Origin.of("test", cited=[old]))
        self.assertEqual(self.location_of(event), "5 Oak")
        newer = self.collect("chat", "n1", "7 Elm", "poker group",
                             ts="2026-09-13T10:00:00-04:00")
        live.update_event(self.conn, self.cfg, event.key, location="7 Elm",
                          origin=live.Origin.of("test", cited=[newer]))
        self.assertEqual(self.location_of(event), "7 Elm")

    def test_a_legacy_row_without_an_evidence_stamp_is_still_protected(self):
        # An upgraded row: a value with no field history and no evidence stamp,
        # as rows created before per-field creation provenance existed. It is
        # floored at the day it was created, not left unprotected.
        event, _ = live.add_event(self.conn, self.cfg, title="Poker",
                                  when="2026-09-12", location="5 Oak",
                                  origin=live.Origin.of("test"))
        self.conn.execute("DELETE FROM event_history WHERE event_id = ?", (event.id,))
        self.conn.execute(
            "UPDATE events SET evidence_ts = NULL, created_at = ? WHERE id = ?",
            ("2026-09-10T10:00:00-04:00", event.id))
        self.conn.commit()
        old = self.collect("chat", "o1", "1 Pine?", "poker group",
                           ts="2024-05-01T19:00:00-04:00")
        with self.assertRaises(live.LiveError):
            live.update_event(self.conn, self.cfg, event.key, location="1 Pine",
                              origin=live.Origin.of("test", cited=[old]))
        self.assertEqual(self.location_of(event), "5 Oak")
        newer = self.collect("chat", "n1", "7 Elm", "poker group",
                             ts="2026-09-11T10:00:00-04:00")
        live.update_event(self.conn, self.cfg, event.key, location="7 Elm",
                          origin=live.Origin.of("test", cited=[newer]))
        self.assertEqual(self.location_of(event), "7 Elm")

    def test_a_known_creation_time_keeps_full_precision_ordering(self):
        # The founding evidence time is known (an 11:00 turn), so a retrieved
        # 10:00 line — older, same day — cannot revise it. Only genuinely unknown
        # creation times are weighed to the day.
        turn = self.turn_at("2026-09-12T11:00:00-04:00", "poker at 5 Oak")
        event, _ = live.add_event(self.conn, self.cfg, title="Poker",
                                  when="2026-09-12", location="5 Oak",
                                  origin=live.Origin.of("test", [turn]))
        older = self.collect("chat", "o1", "1 Pine?", "poker group",
                             ts="2026-09-12T10:00:00-04:00")
        before = self.conn.execute(
            "SELECT count(*) n FROM event_history").fetchone()["n"]
        with self.assertRaises(live.LiveError):
            live.update_event(self.conn, self.cfg, event.key, location="1 Pine",
                              origin=live.Origin.of("test", cited=[older]))
        self.assertEqual(self.location_of(event), "5 Oak")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) n FROM event_history").fetchone()["n"], before)
        # A genuinely newer same-day line still revises it.
        newer = self.collect("chat", "n1", "7 Elm", "poker group",
                             ts="2026-09-12T11:30:00-04:00")
        live.update_event(self.conn, self.cfg, event.key, location="7 Elm",
                          origin=live.Origin.of("test", cited=[newer]))
        self.assertEqual(self.location_of(event), "7 Elm")

    def test_an_older_same_day_line_is_refused_on_the_mcp_write_path(self):
        # The same rule at the surface the agent actually calls, not just the
        # lower-level guard.
        from memcal import mcp_server
        turn = self.turn_at("2026-09-12T11:00:00-04:00", "poker at 5 Oak")
        event, _ = live.add_event(self.conn, self.cfg, title="Poker",
                                  when="2026-09-12", location="5 Oak",
                                  origin=live.Origin.of("test", [turn]))
        older = self.collect("chat", "o1", "1 Pine?", "poker group",
                             ts="2026-09-12T10:00:00-04:00")
        server = mcp_server.Server.__new__(mcp_server.Server)
        server.conn, server.cfg = self.conn, self.cfg
        server.harness, server.session = "openclaw", ""
        row = self.conn.execute("SELECT id FROM events WHERE key = ?",
                                (event.key,)).fetchone()
        out = server.call("memcal_update",
                          {"which": f"E{row['id']}", "location": "1 Pine",
                           "source_ids": [older]})
        self.assertIn("older", out.lower())
        self.assertEqual(self.location_of(event), "5 Oak")

    def test_a_schema_default_is_not_floored_by_creation(self):
        # A status nobody set at creation is a placeholder, not an established
        # value: an earlier-dated source that first decides it must land.
        event, _ = live.add_event(self.conn, self.cfg, title="Poker",
                                  when="2026-09-20", origin=live.Origin.of("test"))
        earlier = self.collect("chat", "e1", "poker is on", "poker group",
                               ts="2026-09-05T19:00:00-04:00")
        live.update_event(self.conn, self.cfg, event.key, status="confirmed",
                          origin=live.Origin.of("test", cited=[earlier]))
        self.assertEqual(self.conn.execute(
            "SELECT status FROM events WHERE key = ?", (event.key,)).fetchone()[
            "status"], "confirmed")


class TestClearsObeyPrecedence(_Base):
    """Finding 2: a destructive clear clears the same evidence bar as a set."""

    def _settled_poker(self):
        event, _ = live.add_event(self.conn, self.cfg, title="Poker",
                                  when="2026-09-12", origin=live.Origin.of("test"))
        settle = self.collect("chat", "s1", "make it 5 Oak", "poker group",
                              ts="2026-09-12T11:00:00-04:00")
        live.update_event(self.conn, self.cfg, event.key, location="5 Oak",
                          origin=live.Origin.of("test", cited=[settle]))
        return event

    def test_an_older_cited_clear_cannot_erase_a_newer_value(self):
        event = self._settled_poker()
        old = self.collect("chat", "o1", "no location", "poker group",
                           ts="2026-09-12T09:00:00-04:00")
        reviewed_before = activity.reviewed_ids(self.conn, "event", event.key)
        with self.assertRaises(live.LiveError):
            live.update_event(self.conn, self.cfg, event.key, location="",
                              origin=live.Origin.of("test", cited=[old]))
        self.assertEqual(self.location_of(event), "5 Oak")
        self.assertEqual(activity.reviewed_ids(self.conn, "event", event.key),
                         reviewed_before)

    def test_a_newer_cited_clear_succeeds_with_its_evidence_time(self):
        event = self._settled_poker()
        newer = self.collect("chat", "n1", "no location now", "poker group",
                             ts="2026-09-12T13:00:00-04:00")
        _updated, changed = live.update_event(
            self.conn, self.cfg, event.key, location="",
            origin=live.Origin.of("test", cited=[newer]))
        self.assertIn("location", " ".join(changed))
        self.assertIsNone(self.location_of(event))
        stamp = self.conn.execute(
            "SELECT evidence_ts FROM event_history WHERE event_id = ?"
            " AND field = 'location' ORDER BY id DESC LIMIT 1",
            (event.id,)).fetchone()["evidence_ts"]
        self.assertEqual(db.parse_ts(stamp),
                         db.parse_ts("2026-09-12T13:00:00-04:00"))

    def test_a_mixed_clear_and_set_on_an_older_line_both_lose(self):
        event = self._settled_poker()
        # Also settle a note at 11:00 so the clear has something newer to face.
        note_src = self.collect("chat", "ns", "note: bring chips", "poker group",
                                ts="2026-09-12T11:00:00-04:00")
        live.update_event(self.conn, self.cfg, event.key, note="bring chips",
                          origin=live.Origin.of("test", cited=[note_src]))
        old = self.collect("chat", "o1", "1 Pine, drop the note", "poker group",
                           ts="2026-09-12T09:00:00-04:00")
        with self.assertRaises(live.LiveError):
            live.update_event(self.conn, self.cfg, event.key, location="1 Pine",
                              note="", origin=live.Origin.of("test", cited=[old]))
        self.assertEqual(self.location_of(event), "5 Oak")
        self.assertEqual(self.conn.execute(
            "SELECT note FROM events WHERE key = ?", (event.key,)).fetchone()[
            "note"], "bring chips")


class TestInvalidCitationsAreRefused(_Base):
    """Finding 3: an invalid citation fails; it never becomes a fresh assertion."""

    def _settled_poker(self):
        event, _ = live.add_event(self.conn, self.cfg, title="Poker",
                                  when="2026-09-12", origin=live.Origin.of("test"))
        settle = self.collect("chat", "s1", "make it 5 Oak", "poker group",
                              ts="2026-09-12T11:00:00-04:00")
        live.update_event(self.conn, self.cfg, event.key, location="5 Oak",
                          origin=live.Origin.of("test", cited=[settle]))
        return event

    def test_all_invalid_citations_fail_even_with_an_authoring_turn(self):
        event = self._settled_poker()
        turn = self.turn_at("2026-09-12T12:00:00-04:00", "where is poker?")
        with self.assertRaises(live.LiveError):
            live.update_event(self.conn, self.cfg, event.key, location="1 Pine",
                              origin=live.Origin.of("test", [turn], cited=[999999]))
        self.assertEqual(self.location_of(event), "5 Oak")

    def test_all_invalid_citations_fail_without_a_turn(self):
        event = self._settled_poker()
        with self.assertRaises(live.LiveError):
            live.update_event(self.conn, self.cfg, event.key, location="1 Pine",
                              origin=live.Origin.of("test", cited=[999999]))
        self.assertEqual(self.location_of(event), "5 Oak")

    def test_a_mixed_valid_and_invalid_citation_cannot_gain_authority(self):
        event = self._settled_poker()
        newer = self.collect("chat", "n1", "7 Elm", "poker group",
                             ts="2026-09-12T13:00:00-04:00")
        before = self.conn.execute(
            "SELECT count(*) n FROM evidence WHERE kind='event' AND ref=?",
            (event.key,)).fetchone()["n"]
        with self.assertRaises(live.LiveError):
            live.update_event(self.conn, self.cfg, event.key, location="7 Elm",
                              origin=live.Origin.of("test", cited=[newer, 999999]))
        self.assertEqual(self.location_of(event), "5 Oak")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) n FROM evidence WHERE kind='event' AND ref=?",
            (event.key,)).fetchone()["n"], before)

    def test_an_unreadable_source_timestamp_is_refused_not_stamped_now(self):
        # Finding 6: db.parse_ts substitutes now on a bad value, so an invalid
        # stored ts must be refused at the evidence boundary — never allowed to
        # borrow the current instant's authority.
        event = self._settled_poker()
        bad = archive.append(
            self.conn, channel="chat", external_id="bad", ts="not-a-timestamp",
            text="1 Pine", thread="poker group", person="friend",
            from_me=False, addressed_to="me")
        self.conn.commit()
        with self.assertRaises(live.LiveError):
            live.update_event(self.conn, self.cfg, event.key, location="1 Pine",
                              origin=live.Origin.of("test", cited=[bad]))
        self.assertEqual(self.location_of(event), "5 Oak")

    def test_an_empty_source_timestamp_is_refused_too(self):
        event = self._settled_poker()
        empty = archive.append(
            self.conn, channel="chat", external_id="empty", ts="",
            text="1 Pine", thread="poker group", person="friend",
            from_me=False, addressed_to="me")
        self.conn.commit()
        with self.assertRaises(live.LiveError):
            live.update_event(self.conn, self.cfg, event.key, location="1 Pine",
                              origin=live.Origin.of("test", cited=[empty]))
        self.assertEqual(self.location_of(event), "5 Oak")


class TestBacklogDisclosesUnrepresentedThreads(_Base):
    """Findings 4 & 2: coverage is judged by what the brief actually renders,
    not by date-range membership."""

    def _event_with_thread_evidence(self, when, thread, cite_text, **fields):
        event, _ = live.add_event(self.conn, self.cfg, title=f"Plan {thread}",
                                  when=when, origin=live.Origin.of("test"),
                                  **fields)
        src = self.collect("chat", f"src-{thread}", cite_text, thread)
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[src]))
        return event

    def _backlog_threads(self):
        return {(i["channel"], i["thread"]) for i in activity.unlinked_backlog(
            self.conn, represented=brief.represented_keys(self.conn, self.cfg))}

    def test_a_new_invitation_in_a_past_events_thread_is_not_hidden(self):
        self._event_with_thread_evidence("2025-09-12", "friends", "poker?")
        self.collect("chat", "new", "rooftop party this Sunday at 7pm?", "friends")
        self.assertIn(("chat", "friends"), self._backlog_threads())
        rendered = brief.render(self.conn, self.cfg)
        self.assertNotIn("[UNREVIEWED:", rendered)
        self.assertIn("coverage incomplete", rendered)

    def test_a_thread_linked_to_an_in_window_event_stays_covered(self):
        self._event_with_thread_evidence("2026-09-12", "crew", "poker?")
        self.collect("chat", "new", "moved to Sunday?", "crew")
        self.assertNotIn(("chat", "crew"), self._backlog_threads())

    def test_a_trimmed_representing_hint_reexposes_its_backlog(self):
        # A confirmed Later event whose thread also has new traffic: its hint
        # suppresses the backlog. When trimming removes that hint, the brief must
        # not keep implying exhaustive coverage of traffic it no longer shows.
        for n in range(6):
            live.add_event(self.conn, self.cfg, title=f"Weekend outing {n}",
                           when="2026-09-13", origin=live.Origin.of("test"))
        poker, _ = live.add_event(self.conn, self.cfg, title="Poker night",
                                  when="2026-10-01", status="confirmed",
                                  origin=live.Origin.of("test"))
        src = self.collect("chat", "src", "poker still on?", "friends")
        live.update_event(self.conn, self.cfg, poker.key, note="plan",
                          origin=live.Origin.of("test", cited=[src]))
        self.collect("chat", "new", "rooftop party this Sunday at 7pm?", "friends")
        full = brief.render(self.conn, self.cfg)
        self.assertNotIn("[UNREVIEWED:", full)
        self.assertIn("New activity: chat/friends", full)

        saw_trimmed = False
        for cap in range(140, 460, 20):
            self.cfg.brief_token_cap = cap
            out = brief.render(self.conn, self.cfg)
            hint_present = "New activity: chat/friends" in out
            disclosed = "coverage incomplete" in out
            if not hint_present and "〔E" in out:
                saw_trimmed = True
            # Invariant: never claim completeness while friends is uncovered and
            # its representing hint is gone.
            if "[complete for" in out and not hint_present:
                self.assertTrue(disclosed, (cap, out))
        self.assertTrue(saw_trimmed, "no cap dropped the representing hint")

    def test_an_unconfirmed_later_opportunity_does_not_cover_its_thread(self):
        # October 1 is inside the Later date range but an unconfirmed opportunity
        # is never rendered, so it suppresses nothing.
        self._event_with_thread_evidence("2026-10-01", "friends", "maybe a thing?",
                                          kind="opportunity", status="tentative")
        self.collect("chat", "new", "rooftop party this Sunday at 7pm?", "friends")
        self.assertIn(("chat", "friends"), self._backlog_threads())
        rendered = brief.render(self.conn, self.cfg)
        self.assertNotIn("[UNREVIEWED:", rendered)
        self.assertIn("coverage incomplete", rendered)


class TestHardCutKeepsUnits(_Base):
    """Finding 5: the hard trim keeps a fitting [event, warning] pair whole."""

    def test_a_complete_warning_is_kept_with_its_event(self):
        lines = [
            "## This week  (today is Saturday 12 September 2026)",
            '〔E1〕 Sun Sep 13 "Outing 01" — maybe',
            "  ↳ New activity: chat/g — 1 message(s) since this plan was reviewed.",
            '〔E2〕 Sun Sep 13 "Outing 02" — maybe',
            "  ↳ New activity: chat/h — 1 message(s) since this plan was reviewed.",
            "[complete for Wed 9 Sep – Sat 19 Sep; look up anything outside that]",
        ]
        # A cap that fits the header, the first event, and its whole warning —
        # but not the following event.
        cap = textclean.estimate_tokens(
            "\n".join(lines[:3]).rstrip() + "\n… (trimmed)\n")
        out = brief._hard_cut(lines, cap)
        self.assertIn("… (trimmed)", out)
        self.assertIn("Outing 01", out)
        self.assertIn("New activity: chat/g", out)     # its warning survived
        self.assertNotIn("Outing 02", out)
        # No warning is left standing without the row it qualifies.
        for i, line in enumerate(out.splitlines()):
            if line.startswith("  ↳ New activity:"):
                self.assertTrue(i > 0 and out.splitlines()[i - 1].startswith("〔"))

    def test_an_event_whose_warning_was_cut_is_dropped(self):
        lines = [
            "## This week  (today is Saturday 12 September 2026)",
            '〔E1〕 Sun Sep 13 "Outing 01" — maybe',
            "  ↳ New activity: chat/g — 1 message(s) since this plan was reviewed.",
        ]
        # A cap that fits the header and the event row but not its warning.
        cap = textclean.estimate_tokens(
            "\n".join(lines[:2]).rstrip() + "\n… (trimmed)\n")
        out = brief._hard_cut(lines, cap)
        self.assertNotIn("Outing 01", out)             # row goes with its lost warning
        self.assertNotIn("New activity", out)


class TestHardCutKeepsCoverageHonest(_Base):
    """Finding 3: a hard cut that drops the coverage warnings must not leave a
    completeness claim implying exhaustive coverage."""

    def test_a_trimmed_backlog_warning_drops_the_completeness_claim(self):
        lines = [
            "## This week  (today is Saturday 12 September 2026)",
            '〔E1〕 Sun Sep 13 "Outing 01" — maybe',
            '〔E2〕 Sun Sep 13 "Outing 02" — maybe',
            "[complete for Wed 9 Sep – Sat 19 Sep; look up anything outside that]",
            "[coverage incomplete — unreviewed traffic not linked to any plan]",
        ]
        # A cap that keeps the events (and the honest notice) but not the
        # detailed coverage warning.
        cap = textclean.estimate_tokens(
            "\n".join(lines[:3]).rstrip()
            + "\n[coverage incomplete — unreviewed or uncollected input not shown]"
            + "\n… (trimmed)\n")
        out = brief._hard_cut(lines, cap)
        self.assertNotIn("[UNREVIEWED:", out)
        self.assertNotIn("[complete for", out)          # false claim removed
        self.assertIn("coverage incomplete", out)       # hole disclosed compactly

    def test_a_cut_that_keeps_every_warning_keeps_the_completeness_claim(self):
        lines = [
            "## This week  (today is Saturday 12 September 2026)",
            '〔E1〕 Sun Sep 13 "Outing 01" — maybe',
            "[complete for Wed 9 Sep – Sat 19 Sep; look up anything outside that]",
        ]
        cap = textclean.estimate_tokens("\n".join(lines).rstrip() + "\n… (trimmed)\n")
        out = brief._hard_cut(lines, cap)
        self.assertIn("[complete for", out)
        self.assertNotIn("coverage incomplete", out)

    def test_the_real_renderer_does_not_claim_completeness_when_it_trimmed_backlog(self):
        for n in range(6):
            live.add_event(self.conn, self.cfg, title=f"Outing {n}",
                           when="2026-09-13", origin=live.Origin.of("test"))
        self.collect("chat", "inv", "rooftop party this Sunday at 7pm?", "crew")
        full = brief.render(self.conn, self.cfg)
        self.assertNotIn("[UNREVIEWED:", full)
        self.assertIn("coverage incomplete", full)
        self.cfg.brief_token_cap = 180
        out = brief.render(self.conn, self.cfg)
        if "[complete for" in out:
            # If the completeness claim survived, the coverage hole must be shown.
            self.assertIn("coverage incomplete", out)



class TestFreshnessCorrectness81(_Base):
    """Issue #81: ical churn ≠ hint; safe labels; no UNREVIEWED PII footer."""

    def test_ical_only_pending_does_not_hint_on_brief(self):
        from memcal.sources import ical
        item = {"uid": "u-dentist", "title": "Dentist",
                "start": "2026-09-20T14:00:00", "end": "2026-09-20T15:00:00",
                "all_day": False, "location": "", "description": "", "url": "",
                "calendar_name": "Home", "calendar_uid": "cal-1", "writable": True}
        rev1 = ical._revision(ical._identity(item), item)
        self.conn.execute(
            "INSERT INTO archive(channel, external_id, ts, thread, text, created_at)"
            " VALUES('ical', ?, '2026-09-01T10:00:00', 'cal-1', 'Dentist', ?)",
            (rev1, db.now()))
        self.conn.commit()
        event, _ = live.add_event(self.conn, self.cfg, title="Dentist visit",
                                  when="2026-09-20", origin=live.Origin.of("test"))
        first = self.conn.execute(
            "SELECT id FROM archive WHERE external_id = ?", (rev1,)).fetchone()["id"]
        trace.stamp(self.conn, kind="event", ref=event.key, verb="inserted",
                    entity="calendar:Home", stage="ical", archive_ids=[first])
        self.conn.commit()
        moved = dict(item, start="2026-09-20T15:00:00")
        rev2 = ical._revision(ical._identity(moved), moved)
        self.conn.execute(
            "INSERT INTO archive(channel, external_id, ts, thread, text, created_at)"
            " VALUES('ical', ?, '2026-09-01T12:00:00', 'cal-1', 'Dentist moved', ?)",
            (rev2, db.now()))
        self.conn.commit()
        # activity.pending still sees the family revision (reader path).
        self.assertEqual(
            activity.pending(self.conn, "event", event.key)["strong_total"], 1)
        # Brief must not raise a hint for calendar self-feed churn alone.
        text = brief.render(self.conn, self.cfg)
        self.assertNotIn("New activity", text)
        self.assertIn("Dentist visit", text)

    def test_chat_strong_link_hints_with_safe_label_not_raw_phone(self):
        phone = "+15551234567"
        m1 = self.collect("imessage", "m1", "poker saturday?", phone,
                          handle=phone)
        threads.record(self.conn, "imessage", phone, label=None, is_group=False)
        self.conn.commit()
        event, _ = live.add_event(self.conn, self.cfg, title="Poker night",
                                  when="2026-09-12", origin=live.Origin.of("test"))
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        self.collect("imessage", "m2", "moved to Sunday?", phone, handle=phone)
        text = brief.render(self.conn, self.cfg)
        self.assertIn("New activity:", text)
        self.assertNotIn(phone, text)
        self.assertNotIn("+1555", text)
        self.assertIn("unknown number", text)
        # Frozen copy shape (Integrations mirror FRESHNESS_HINT_FORMAT).
        self.assertIn("since this plan was reviewed", text)
        self.assertIn("may have changed", text)
        self.assertIn("memcal_activity(handle=", text)

    def test_email_strong_link_hint_says_sender_not_number(self):
        addr = "billing@vendor.example"
        m1 = self.collect("email", "e1", "your invoice", addr, handle=addr)
        threads.record(self.conn, "email", addr, label=None, is_group=False)
        self.conn.commit()
        event, _ = live.add_event(self.conn, self.cfg, title="Pay invoice",
                                  when="2026-09-12", origin=live.Origin.of("test"))
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        self.collect("email", "e2", "reminder: invoice due", addr, handle=addr)
        text = brief.render(self.conn, self.cfg)
        self.assertIn("New activity:", text)
        self.assertNotIn(addr, text)
        self.assertNotIn("unknown number", text)
        self.assertIn("unknown sender", text)

    def test_a_guessed_sender_shows_as_maybe_not_a_number(self):
        from memcal import identity
        phone = "+15557654321"
        m1 = self.collect("imessage", "m1", "your appointment is confirmed", phone,
                          handle=phone)
        threads.record(self.conn, "imessage", phone, label=None, is_group=False)
        # Dream named this otherwise-nameless sender.
        identity.guess_name(self.conn, phone, "Tire shop scheduling",
                            channel="imessage")
        self.conn.commit()
        event, _ = live.add_event(self.conn, self.cfg, title="Tire appointment",
                                  when="2026-09-12", origin=live.Origin.of("test"))
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        self.collect("imessage", "m2", "your appointment was cancelled", phone,
                     handle=phone)
        text = brief.render(self.conn, self.cfg)
        self.assertIn("New activity:", text)
        self.assertNotIn(phone, text)
        self.assertIn("maybe: Tire shop scheduling", text)

    def test_chat_label_preferred_when_richer_than_raw_thread(self):
        phone = "+15559876543"
        m1 = self.collect("imessage", "m1", "poker saturday?", phone,
                          handle=phone, person="Jordan")
        threads.record(self.conn, "imessage", phone, label="Jordan",
                       is_group=False)
        self.conn.commit()
        event, _ = live.add_event(self.conn, self.cfg, title="Poker night",
                                  when="2026-09-12", origin=live.Origin.of("test"))
        live.update_event(self.conn, self.cfg, event.key, note="plan",
                          origin=live.Origin.of("test", cited=[m1]))
        self.collect("imessage", "m2", "moved to Sunday?", phone,
                     handle=phone, person="Jordan")
        text = brief.render(self.conn, self.cfg)
        self.assertIn("New activity: imessage/Jordan", text)
        self.assertNotIn(phone, text)

    def test_unlinked_backlog_has_no_unreviewed_pii_footer(self):
        self.collect("chat", "u0", "rooftop party friday?", "rooftop crew")
        text = brief.render(self.conn, self.cfg)
        self.assertNotIn("[UNREVIEWED:", text)
        self.assertNotIn("rooftop crew", text)  # thread id must not leak in footer
        self.assertIn("coverage incomplete", text)



if __name__ == "__main__":
    unittest.main()
