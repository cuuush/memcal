"""Placing a cancellation on the row it names, across bundles and transports."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _support import Base as StoreTest                  # noqa: E402
from memcal import archive, db, events, live, pending, threads  # noqa: E402
from memcal.dream import apply as apply_stage          # noqa: E402
from memcal.dream import merge as merge_stage          # noqa: E402
from memcal.dream.bundle import Bundle                 # noqa: E402
from memcal.sources import ical                        # noqa: E402


class Base(StoreTest):
    def setUp(self):
        super().setUp()
        db.set_today("2026-09-10T09:00:00")
        self.addCleanup(db.set_today, None)


def mention(entity: str, **row):
    row.setdefault("subject", "me")
    return (Bundle(entity=entity), {"events": [row]}, "gen-x")


class TestACancellationPlacesItselfOnTheSlotItNamed(Base):
    def setUp(self):
        super().setUp()
        for title, clock in (("Therapy", "13:00"), ("Tire appointment", "13:45")):
            events.upsert(self.conn, {"title": title, "date": self.d(6),
                                      "time": clock, "status": "confirmed"},
                          written_by="dream:nightly")

    def test_the_row_in_that_slot_is_only_nominated(self):
        pending.note(self.conn, kind="cancellation",
                     observation=f"Tire installation on {self.d(6)}",
                     title="Tire installation", date=self.d(6), time="13:45",
                     entity="thread:email:costco", commit=True)
        log = pending.retry(self.conn)
        self.assertTrue(any("held" in line for line in log), log)
        self.assertEqual(events.get(self.conn, f"tire-appointment@{self.d(6)}").status,
                         "confirmed")
        self.assertEqual(events.get(self.conn, f"therapy@{self.d(6)}").status,
                         "confirmed")

    def test_a_matching_hour_does_not_authorize_a_cancellation(self):
        events.upsert(self.conn, {"key": f"therapy@{self.d(6)}", "date": self.d(6),
                                  "time": "11:00"}, written_by="dream:nightly")
        pending.note(self.conn, kind="cancellation",
                     observation=f"Tire installation on {self.d(6)}",
                     title="Tire installation", date=self.d(6), time="1:50 PM",
                     entity="thread:email:costco", commit=True)
        pending.retry(self.conn)
        self.assertEqual(events.get(self.conn, f"tire-appointment@{self.d(6)}").status,
                         "confirmed")

    def test_two_rows_in_one_hour_settle_nothing(self):
        pending.note(self.conn, kind="cancellation",
                     observation=f"Tire installation on {self.d(6)}",
                     title="Tire installation", date=self.d(6), time="1:50 PM",
                     entity="thread:email:costco", commit=True)
        pending.retry(self.conn)
        self.assertEqual(events.get(self.conn, f"tire-appointment@{self.d(6)}").status,
                         "confirmed")

    def test_two_rows_in_one_slot_settle_nothing(self):
        events.upsert(self.conn, {"title": "Oil change", "date": self.d(6),
                                  "time": "13:45", "status": "confirmed"},
                      written_by="dream:nightly")
        pending.note(self.conn, kind="cancellation",
                     observation=f"Tire installation on {self.d(6)}",
                     title="Tire installation", date=self.d(6), time="13:45",
                     entity="thread:email:costco", commit=True)
        pending.retry(self.conn)
        self.assertEqual(events.get(self.conn, f"tire-appointment@{self.d(6)}").status,
                         "confirmed")
        self.assertEqual(events.get(self.conn, f"oil-change@{self.d(6)}").status,
                         "confirmed")


class TestAStatedReplacementOnlyNominatesWhatWasCancelled(Base):
    def test_a_replacement_link_does_not_authorize_a_cancellation(self):
        for offset in (6, 1):
            events.upsert(self.conn, {"title": "Tire appointment", "date": self.d(offset),
                                      "time": "13:45", "status": "confirmed"},
                          written_by="dream:nightly")
        events.link(self.conn, f"tire-appointment@{self.d(1)}",
                    f"tire-appointment@{self.d(6)}", "replaces", written_by="test")
        pending.note(self.conn, kind="cancellation",
                     observation="Tire appointment cancelled",
                     title="Tire appointment", date="", entity="thread:imessage:costco",
                     commit=True)
        pending.retry(self.conn)
        self.assertEqual(events.get(self.conn, f"tire-appointment@{self.d(6)}").status,
                         "confirmed")
        self.assertEqual(events.get(self.conn, f"tire-appointment@{self.d(1)}").status,
                         "confirmed")


class TestOneSidedEmailSendersFoldIntoOneConversation(Base):
    def deliver(self, thread: str, label: str, text: str, ident: str) -> None:
        archive.append(self.conn, stream="email", external_id=ident,
                       ts=f"{self.d(0)}T10:00:00", text=text, thread=thread,
                       handle="costco@waitwhile.com", person="Costco")
        threads.record(self.conn, "email", thread, label=label)

    def test_a_subject_per_message_does_not_split_the_sender(self):
        self.deliver("t1", "Your Sep 15 booking is confirmed", "confirmed", "e1")
        self.deliver("t2", "Your booking was cancelled", "cancelled", "e2")
        folded = threads.aliases(self.conn)
        self.assertEqual(len({threads.fold_entity(f"thread:email:{t}", folded)
                              for t in ("t1", "t2")}), 1)

    def test_a_thread_the_user_answered_keeps_its_subject(self):
        self.deliver("t1", "Lunch?", "lunch?", "e1")
        self.deliver("t2", "Dinner?", "dinner?", "e2")
        archive.append(self.conn, stream="email", external_id="mine",
                       ts=f"{self.d(0)}T11:00:00", text="sure", thread="t1",
                       handle="me@example.com", person="me", from_me=True)
        folded = threads.aliases(self.conn)
        self.assertEqual(len({threads.fold_entity(f"thread:email:{t}", folded)
                              for t in ("t1", "t2")}), 2)

    def test_other_streams_are_untouched(self):
        for ident, thread in (("i1", "timezone"), ("i2", "late reply")):
            archive.append(self.conn, stream="imessage", external_id=ident,
                           ts=f"{self.d(0)}T10:00:00", text="hi", thread=thread,
                           handle="+19175550999", person="Tester")
            threads.record(self.conn, "imessage", thread, label=thread)
        folded = threads.aliases(self.conn)
        self.assertEqual(len({threads.fold_entity(f"thread:imessage:{t}", folded)
                              for t in ("timezone", "late reply")}), 2)


class TestOneSenderOneDayClusters(unittest.TestCase):
    def test_a_shared_word_is_enough_from_one_sender(self):
        proposals = [
            mention("thread:email:x@waitwhile.com", title="Tire installation",
                    date="2026-09-15", time="13:45", location="Costco — QUEENS",
                    status="declined"),
            mention("thread:email:y@waitwhile.com", title="Tire appointment",
                    date="2026-09-15", time="13:45", location="Costco - QUEENS NY",
                    status="confirmed"),
        ]
        group = merge_stage.cluster([merge_stage.Mention(d["events"][0], b, d)
                                     for b, d, _g in proposals])
        self.assertEqual(len(group), 1)

    def test_a_different_sender_on_one_day_does_not_cluster_on_a_word_alone(self):
        proposals = [
            mention("thread:email:x@waitwhile.com", title="Tire installation",
                    date="2026-09-15", time="13:45", location="Costco"),
            mention("thread:email:y@dealership.com", title="Tire rotation",
                    date="2026-09-15", time="09:00", location="Elsewhere"),
        ]
        group = merge_stage.cluster([merge_stage.Mention(d["events"][0], b, d)
                                     for b, d, _g in proposals])
        self.assertEqual(len(group), 2)


class TestStatedLinksCluster(Base):
    def test_linked_rows_cluster_however_differently_worded(self):
        events.upsert(self.conn, {"title": "Tyre fitting", "date": self.d(2)})
        events.upsert(self.conn, {"title": "Wheels", "date": self.d(2)})
        one, two = f"tyre-fitting@{self.d(2)}", f"wheels@{self.d(2)}"
        events.link(self.conn, one, two, "same_as", written_by="test")
        made = [
            merge_stage.Mention({"key": one, "title": "Tyre fitting", "date": self.d(2),
                                 "subject": "me"}, Bundle(entity="a"), {"events": []}),
            merge_stage.Mention({"key": two, "title": "Wheels", "date": self.d(2),
                                 "subject": "me"}, Bundle(entity="b"), {"events": []}),
        ]
        groups = merge_stage.cluster(made, None, events.linked_pairs(self.conn))
        self.assertEqual(len(groups), 1)


class TestMergeComparesAgainstRowsAlreadyStored(Base):
    def test_a_stored_row_pulls_a_later_proposal_into_its_cluster(self):
        events.upsert(self.conn, {"title": "Tire appointment", "date": self.d(6),
                                  "time": "13:45", "status": "confirmed",
                                  "source": "thread:email:costco"})
        proposals = [mention("thread:email:costco", title="Tire installation",
                             date=self.d(6), time="13:45", status="declined")]
        stored = merge_stage._stored_near(
            self.conn, [merge_stage.Mention(proposals[0][1]["events"][0],
                                            proposals[0][0], proposals[0][1])])
        self.assertTrue(any(m.row["key"] == f"tire-appointment@{self.d(6)}"
                            for m in stored))
        pool = [merge_stage.Mention(proposals[0][1]["events"][0], proposals[0][0],
                                    proposals[0][1])] + stored
        groups = [g for g in merge_stage.cluster(pool) if len(g) > 1]
        self.assertEqual(len(groups), 1)
        self.assertTrue(merge_stage._conflicted(groups[0]))

    def test_a_row_apply_will_reach_on_its_own_is_left_to_apply(self):
        events.upsert(self.conn, {"title": "Movie with Riley", "date": self.d(6),
                                  "status": "confirmed"}, written_by="dream:nightly")
        proposal = mention("person:Riley", title="Movie with Riley", date=self.d(6),
                           status="declined")
        live = merge_stage.Mention(proposal[1]["events"][0], proposal[0], proposal[1])
        self.assertEqual(merge_stage._stored_near(self.conn, [live]), [])

    def test_a_stored_row_is_never_the_survivor_a_diff_is_written_onto(self):
        events.upsert(self.conn, {"title": "Tire appointment", "date": self.d(6)})
        proposal = mention("thread:email:costco", title="Tire appointment",
                           date=self.d(6))
        live = merge_stage.Mention(proposal[1]["events"][0], proposal[0], proposal[1])
        stored = merge_stage._stored_near(self.conn, [live])
        merge_stage._collapse([live, *stored], {"title": "Merged", "date": self.d(6)})
        self.assertEqual(proposal[1]["events"][0]["title"], "Merged")

    def test_a_local_merge_keeps_the_stored_key(self):
        event, _ = events.upsert(
            self.conn, {"title": "Appointment", "date": self.d(6),
                        "subject": "Riley", "participants": []})
        proposal = mention("person:Riley", title="Appointment", date=self.d(6),
                           subject="me", participants=["Riley"])
        live = merge_stage.Mention(proposal[1]["events"][0], proposal[0], proposal[1])
        stored = merge_stage._stored_near(self.conn, [live])
        self.assertEqual(len(stored), 1)
        merged = merge_stage._merge_locally([live, *stored])
        self.assertEqual(merged["key"], event.key)


class TestSweepCannotOverrideStateWithoutEvidence(Base):
    def test_a_summary_cannot_downgrade_a_live_decision(self):
        events.upsert(self.conn, {"title": "Tire appointment", "date": self.d(6),
                                  "status": "confirmed"}, written_by="live")
        key = f"tire-appointment@{self.d(6)}"
        self.assertFalse(events.set_status(self.conn, key, "declined",
                                           written_by="sweep"))
        found = events.get(self.conn, key)
        self.assertEqual(found.status, "confirmed")
        self.assertEqual(found.written_by, "live")

    def test_an_unknown_status_is_refused(self):
        events.upsert(self.conn, {"title": "Tire appointment", "date": self.d(6)})
        self.assertFalse(events.set_status(self.conn, f"tire-appointment@{self.d(6)}",
                                           "cancelled", written_by="sweep"))


class TestAClarificationSurvivesBackgroundRetries(Base):
    def test_a_retry_without_an_ask_callback_does_not_consume_the_question(self):
        events.upsert(self.conn, {"title": "Tire appointment", "date": self.d(6),
                                  "source": "thread:email:booking"})
        pending.note(self.conn, kind="cancellation",
                     observation="Tire appointment cancelled", title="Tire appointment",
                     date=self.d(6), entity="thread:email:booking", commit=True)
        pending.retry(self.conn)
        asked = []
        pending.retry(self.conn, ask=lambda text, key: asked.append((text, key)))
        self.assertEqual(len(asked), 1)
        self.assertTrue(asked[0][1].startswith("q:pending:"))


class TestRelationshipOnlyApplication(Base):
    def test_an_unchanged_event_can_gain_a_replacement_link(self):
        for title in ("Old appointment", "New appointment"):
            events.upsert(self.conn, {"title": title, "date": self.d(6)})
        old, new = (f"old-appointment@{self.d(6)}", f"new-appointment@{self.d(6)}")
        proposal = mention(
            "thread:email:booking", key=new, title="New appointment", date=self.d(6),
            links=[{"kind": "replaces", "key": old}],
        )
        counts, _log = apply_stage.apply_diffs(
            self.conn, self.cfg, [proposal], written_by="dream:nightly")
        self.assertEqual(counts["event:unchanged"], 0)
        self.assertEqual([event.key for event in events.links_for(self.conn, new, "replaces")],
                         [old])


class TestMergePreservesFieldEvidence(unittest.TestCase):
    def test_an_explicitly_uncited_field_does_not_inherit_row_citations(self):
        first = mention("thread:a", title="Appointment", date="2026-09-15",
                        status="confirmed", cite_ids=[11], field_cite_ids={})
        second = mention("thread:b", title="Appointment", date="2026-09-15",
                         status="confirmed", cite_ids=[12],
                         field_cite_ids={"status": [12]})
        group = [merge_stage.Mention(diff["events"][0], bundle, diff)
                 for bundle, diff, _gen in (first, second)]
        merged = merge_stage._merge_locally(group)
        cites = merge_stage._field_cites(group, merged)
        self.assertNotIn("title", cites)
        self.assertEqual(cites["status"], [12])

    def test_a_unioned_guest_list_keeps_each_source(self):
        first = mention("thread:a", title="Appointment", date="2026-09-15",
                        participants=["Avery"], field_cite_ids={"participants": [11]})
        second = mention("thread:b", title="Appointment", date="2026-09-15",
                         participants=["Riley"], field_cite_ids={"participants": [12]})
        group = [merge_stage.Mention(diff["events"][0], bundle, diff)
                 for bundle, diff, _gen in (first, second)]
        merged = merge_stage._merge_locally(group)
        self.assertEqual(merge_stage._field_cites(group, merged)["participants"], [11, 12])


class TestEventMergePreservesRelationships(Base):
    def test_symmetric_links_remain_canonical(self):
        drop, _ = events.upsert(self.conn, {"title": "Duplicate", "date": self.d(1)})
        other, _ = events.upsert(self.conn, {"title": "Related", "date": self.d(2)},
                                 match=False)
        keep, _ = events.upsert(self.conn, {"title": "Canonical", "date": self.d(1)},
                                match=False)
        self.assertTrue(events.link(self.conn, drop.key, other.key, "related"))

        events.merge(self.conn, keep.key, drop.key)

        edge = self.conn.execute(
            "SELECT from_id, to_id FROM event_links WHERE kind = 'related'").fetchone()
        self.assertEqual((edge["from_id"], edge["to_id"]),
                         tuple(sorted((keep.id, other.id))))
        self.assertFalse(events.link(self.conn, keep.key, other.key, "related"))


class _MergeClient:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answer

        class Reply:
            data = answer
            truncated = False
            finish_reason = "stop"
            generation_id = "gen-test"
            usage = type("U", (), {"cost": 0.0, "completion_tokens": 0,
                                    "prompt_tokens": 0})()
            text = reasoning = ""
        return Reply()


class TestCancellationAndImmediateRebookingLifecycle(Base):
    def bundle(self, entity: str, stream: str, external_id: str, ts: str, text: str):
        archive_id = archive.append(
            self.conn, stream=stream, external_id=external_id, ts=ts, text=text,
            thread=external_id, handle=entity, person="Booking service")
        row = self.conn.execute("SELECT * FROM archive WHERE id = ?", (archive_id,)).fetchone()
        return Bundle(entity=entity, items=[row]), archive_id

    def test_fresh_undated_sms_and_dated_emails_keep_two_targets(self):
        sms, sms_id = self.bundle("person:Booking service", "imessage", "sms-1",
                                  f"{self.d(0)}T10:00:00", "Your appointment was canceled")
        old_mail, old_id = self.bundle("thread:email:booking", "email", "mail-old",
                                       f"{self.d(0)}T10:01:00",
                                       f"Appointment on {self.d(6)} was canceled")
        new_mail, new_id = self.bundle("thread:email:booking", "email", "mail-new",
                                       f"{self.d(0)}T10:02:00",
                                       f"Appointment booked for {self.d(12)}")
        old_key = f"appointment@{self.d(6)}"
        proposals = [
            (old_mail, {"events": [{"title": "Appointment", "date": self.d(6),
                                     "status": "declined", "subject": "me",
                                     "cite_ids": [old_id]}]}, "gen-old"),
            (new_mail, {"events": [{"title": "Appointment", "date": self.d(12),
                                     "status": "confirmed", "subject": "me",
                                     "cite_ids": [new_id],
                                     "links": [{"kind": "replaces", "key": old_key}]}]},
             "gen-new"),
            (sms, {"events": [{"title": "Appointment cancellation", "date": None,
                                "status": "declined", "subject": "me",
                                "cite_ids": [sms_id]}]}, "gen-sms"),
        ]
        client = _MergeClient({
            "same_event": False, "date": "", "title": "", "until": None,
            "time": None, "location": None, "kind": None, "status": None,
            "participants": [], "note": None, "citations": [], "links": [],
            "pending_targets": [],
            "observation_targets": [{"observation": 3, "target": 1}],
            "why": "the first date was cancelled and a different date was booked",
        })
        merged, _log = merge_stage.merge_all(client, self.cfg, proposals, conn=self.conn)
        self.assertIn(f"SOURCE {sms_id} at", client.calls[0]["suffix"])
        apply_stage.apply_diffs(self.conn, self.cfg, merged, written_by="dream:nightly")
        pending.retry(self.conn)
        self.assertEqual(events.get(self.conn, old_key).status, "declined")
        self.assertEqual(events.get(self.conn, f"appointment@{self.d(12)}").status,
                         "confirmed")
        self.assertEqual(events.links_for(self.conn, f"appointment@{self.d(12)}",
                                          "replaces")[0].key, old_key)
        apply_stage.apply_diffs(self.conn, self.cfg, merged, written_by="dream:nightly")
        self.assertEqual(len(events.between(self.conn, self.d(6), self.d(12))), 2)

    def test_old_pending_sms_can_target_a_stored_row_without_an_event_edit(self):
        events.upsert(self.conn, {"title": "Appointment", "date": self.d(6),
                                  "status": "confirmed"}, written_by="dream:nightly")
        pending.note(self.conn, kind="cancellation", observation="Appointment cancelled",
                     title="Appointment", observed_at=f"{self.d(0)}T10:00:00", commit=True)
        new_mail, new_id = self.bundle("thread:email:booking", "email", "newer",
                                       f"{self.d(0)}T10:01:00",
                                       f"Appointment booked for {self.d(12)}")
        proposal = (new_mail, {"events": [{"title": "Appointment", "date": self.d(12),
                                            "status": "confirmed", "subject": "me",
                                            "cite_ids": [new_id]}]}, "gen-new")
        pending_id = pending.open_items(self.conn)[0]["id"]
        client = _MergeClient({
            "same_event": False, "date": "", "title": "", "until": None,
            "time": None, "location": None, "kind": None, "status": None,
            "participants": [], "note": None, "citations": [], "links": [],
            "pending_targets": [{"pending_id": pending_id, "proposal": 2}],
            "observation_targets": [], "why": "the old row is the cancelled booking",
        })
        merged, _log = merge_stage.merge_all(client, self.cfg, [proposal], conn=self.conn)
        self.assertIsNone(pending.open_items(self.conn)[0]["target_key"])
        apply_stage.apply_diffs(self.conn, self.cfg, merged, written_by="dream:nightly")
        pending.retry(self.conn)
        self.assertEqual(events.get(self.conn, f"appointment@{self.d(6)}").status,
                         "declined")
        self.assertEqual(events.get(self.conn, f"appointment@{self.d(12)}").status,
                         "confirmed")


class TestMergedEvidenceHonorsLiveCorrections(Base):
    bundle = TestCancellationAndImmediateRebookingLifecycle.bundle

    def test_merging_an_undated_notice_does_not_remove_the_survivor(self):
        sms, sid = self.bundle("clinic", "imessage", "cancel",
                               f"{self.d(0)}T10:00:00", "Appointment cancelled")
        mail, mid = self.bundle("clinic", "email", "confirm",
                                f"{self.d(0)}T11:00:00", "Appointment reinstated")
        proposals = [
            (mail, {"events": [{"title": "Appointment", "date": self.d(6),
                                "status": "confirmed", "cite_ids": [mid]}]}, "g1"),
            (sms, {"events": [{"title": "Appointment", "date": None,
                               "status": "declined", "cite_ids": [sid]}]}, "g2"),
        ]
        answer = {"same_event": True, "title": "Appointment", "date": self.d(6),
                  "status": "confirmed", "citations": [
                      {"field": "status", "source_ids": [mid]}],
                  "observation_targets": [{"observation": 2, "target": 1}]}
        merged, _ = merge_stage.merge_all(_MergeClient(answer), self.cfg, proposals,
                                          conn=self.conn)
        apply_stage.apply_diffs(self.conn, self.cfg, merged, written_by="dream:nightly")
        pending.retry(self.conn)
        found = events.between(self.conn, self.d(6), self.d(6))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].status, "confirmed")

    def test_cross_bundle_time_changes_without_restoring_an_old_location(self):
        event, _ = live.add_event(self.conn, self.cfg, title="Appointment",
                                  when=self.d(6), time="13:00", location="New clinic",
                                  status="confirmed")
        old, old_id = self.bundle("thread:email:clinic", "email", "old-place",
                                  f"{self.d(0)}T08:00:00", "Appointment at Old clinic")
        newer, new_id = self.bundle("person:Clinic", "imessage", "new-time",
                                    f"{self.d(0)}T10:00:00", "Appointment is now at 14:00")
        proposals = [
            (old, {"events": [{"title": "Appointment", "date": self.d(6),
                               "location": "Old clinic", "cite_ids": [old_id],
                               "field_cite_ids": {"location": [old_id]}}]}, "g1"),
            (newer, {"events": [{"title": "Appointment", "date": self.d(6),
                                 "time": "14:00", "cite_ids": [new_id],
                                 "field_cite_ids": {"time": [new_id]}}]}, "g2"),
        ]
        merged, _ = merge_stage.merge_all(_MergeClient({}), self.cfg, proposals,
                                          conn=self.conn)
        apply_stage.apply_diffs(self.conn, self.cfg, merged, written_by="dream:nightly")
        found = events.get(self.conn, event.key)
        self.assertEqual((found.time, found.location), ("14:00", "New clinic"))
        self.assertEqual(found.written_by, "live")
        history = events.history(self.conn, event.id)
        apply_stage.apply_diffs(self.conn, self.cfg, merged, written_by="dream:nightly")
        self.assertEqual(events.history(self.conn, event.id), history)

    def test_model_can_cite_only_the_source_lines_it_was_shown(self):
        bundle, shown = self.bundle("thread:email:clinic", "email", "shown",
                                    f"{self.d(0)}T10:00:00", "Appointment is at 14:00")
        hidden_bundle, hidden = self.bundle("thread:email:clinic", "email", "hidden",
                                            f"{self.d(0)}T11:00:00", "Unrelated news")
        bundle.items.extend(hidden_bundle.items)
        row = {"title": "Appointment", "date": self.d(6), "cite_ids": [shown]}
        group = [merge_stage.Mention(row, bundle, {"events": [row]})]
        cites = merge_stage._answer_citations(self.conn, group, {"citations": [
            {"field": "time", "source_ids": [shown]},
            {"field": "status", "source_ids": [hidden, 99999]},
        ]}, {"time": "14:00", "status": "declined"})
        self.assertEqual(cites, {"time": [shown]})

    def test_target_decisions_roll_back_with_failed_application(self):
        event, _ = events.upsert(self.conn, {"title": "Appointment", "date": self.d(6)})
        pending.note(self.conn, kind="cancellation", observation="Appointment cancelled",
                     commit=True)
        ident = pending.open_items(self.conn)[0]["id"]
        proposal = (Bundle(entity="clinic"), {
            "_pending_targets": [{"id": ident, "key": event.key}],
            "todos": [{"text": "test"}],
        }, "g1")
        with patch.object(apply_stage, "_apply_todo", side_effect=ValueError("invalid")):
            with self.assertRaises(ValueError):
                apply_stage.apply_diffs(self.conn, self.cfg, [proposal],
                                        written_by="dream:nightly")
        self.assertIsNone(pending.open_items(self.conn)[0]["target_key"])

    def test_generic_notice_keeps_all_competing_booking_dates_in_view(self):
        groups = []
        for offset in (1, 6, 20):
            bundle, diff, _ = mention("provider", title="Tire appointment",
                                      date=self.d(offset), status="confirmed")
            groups.append([merge_stage.Mention(diff["events"][0], bundle, diff)])
        bundle, diff, _ = mention("sms", title="Appointment cancellation",
                                  date=None, status="declined")
        observation = merge_stage.Mention(diff["events"][0], bundle, diff)
        grouped = merge_stage._with_observations(groups, [observation])
        self.assertEqual(len(grouped), 1)
        self.assertEqual({m.date for m in grouped[0] if m.date},
                         {self.d(1), self.d(6), self.d(20)})

    def test_a_late_placed_cancellation_does_not_undo_a_newer_confirmation(self):
        event, _ = events.upsert(
            self.conn, {"title": "Appointment", "date": self.d(6), "status": "confirmed"},
            evidence_ts=f"{self.d(0)}T12:00:00", written_by="dream:nightly")
        pending.note(self.conn, kind="cancellation", observation="Appointment cancelled",
                     observed_at=f"{self.d(0)}T10:00:00", target_key=event.key, commit=True)
        pending.retry(self.conn)
        self.assertEqual(events.get(self.conn, event.key).status, "confirmed")
        self.assertFalse(pending.open_items(self.conn))

    def test_a_newer_cancellation_can_change_a_dream_confirmation(self):
        event, _ = events.upsert(
            self.conn, {"title": "Appointment", "date": self.d(6), "status": "confirmed"},
            evidence_ts=f"{self.d(0)}T10:00:00", written_by="dream:nightly")
        pending.note(self.conn, kind="cancellation", observation="Appointment cancelled",
                     observed_at=f"{self.d(0)}T12:00:00", target_key=event.key, commit=True)
        pending.retry(self.conn)
        self.assertEqual(events.get(self.conn, event.key).status, "declined")


class TestAContestedRowIsNotPublished(Base):
    def test_a_row_an_open_cancellation_names_is_held_back(self):
        events.upsert(self.conn, {"title": "Tire appointment", "date": self.d(6),
                                  "time": "13:45", "status": "confirmed"})
        key = f"tire-appointment@{self.d(6)}"
        pending.note(self.conn, kind="cancellation", observation="something cancelled",
                     candidates=[key], entity="thread:email:costco", commit=True)
        self.assertIn(key, ical._contested(self.conn))
        self.cfg.publish_calendar = "memcal"

        def runner(*args, **kwargs):
            raise AssertionError("nothing contested may reach the calendar")

        log = ical.publish_pending(self.conn, self.cfg, keys=[key], runner=runner)
        self.assertTrue(any("not publishing" in line for line in log), log)


if __name__ == "__main__":
    unittest.main()
