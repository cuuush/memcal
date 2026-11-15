"""Offline correction lifecycle; extraction is supplied, never inferred by a model."""

from __future__ import annotations

import copy
import unittest
from unittest import mock

try:
    from tests._support import Base
except ModuleNotFoundError:
    from _support import Base

from memcal import archive, brief, db, live, mcp_server, trace, wiki
from memcal.dream import apply as apply_stage, propose
from memcal.dream.bundle import Bundle


class TestExplicitPreferenceCorrectionsResolveAnExactPriorSlot(Base):
    old = "avoid mushrooms; dairy is fine"
    new = "avoid dairy; mushrooms are fine"
    quote = ("Correction to my earlier pizza preference: avoid dairy; mushrooms are fine. "
             "This replaces the earlier instruction for pizza.")

    def setUp(self):
        super().setUp()
        db.set_today("2026-10-10T12:00:00+00:00")
        self.first = self.line(5, "For pizza, avoid mushrooms; dairy is fine.")
        self.second = self.line(7, self.quote)
        self.original = self.row("pizza_preferences", self.old, self.first)
        self.correction = self.row("dietary_preferences", self.new, self.second)
        self.correction["supersedes"] = {
            "slot": "pizza_preferences", "previous": self.old,
            "prior_lines": [self.first], "scope": "same", "quote": self.quote,
        }
        self.apply(self.original)

    def line(self, day, text, *, person="me", from_me=True):
        line_id = archive.append(
            self.conn, channel="imessage", external_id=f"synthetic-{day}-{person}-{text}",
            ts=f"2026-10-{day:02d}T10:00:00+00:00", text=text,
            thread="synthetic-pizza", person=person, from_me=from_me)
        self.conn.commit()
        return line_id

    def row(self, slot, value, line_id, **changes):
        return {"page": "me", "section": "people", "slot": slot, "value": value,
                "subject": "me", "subject_role": "speaker", "durability": "lasting",
                "claim_kind": "preference", "cite_ids": [line_id], **changes}

    def proposal(self, row, extra_ids=()):
        ids = sorted(set(row.get("cite_ids", []) + list(extra_ids)))
        items = [self.conn.execute("SELECT * FROM archive WHERE id = ?", (i,)).fetchone()
                 for i in ids]
        return (Bundle(entity="person:me", title="Synthetic pizza", items=items),
                {"wiki": [copy.deepcopy(row)]}, "synthetic-extraction-stub")

    def apply(self, row, extra_ids=()):
        return apply_stage.apply_diffs(
            self.conn, self.cfg, [self.proposal(row, extra_ids)], written_by="dream:test")

    def facts(self, page="me"):
        found = wiki.read(self.cfg.wiki_dir, page)
        return {k: v["value"] for k, v in found.slots.items()} if found else {}

    def assert_rejected(self, row, kind="rejected-correction"):
        before = self.facts()
        history = len(wiki.slot_history(self.conn, "me"))
        _, log = self.apply(row)
        self.assertTrue(any(kind in entry for entry in log), log)
        self.assertEqual(self.facts(), before)
        self.assertEqual(len(wiki.slot_history(self.conn, "me")), history)

    def test_two_labels_have_one_current_value_and_dated_old_new_provenance(self):
        self.apply(self.correction)
        self.assertEqual(self.facts(), {"pizza_preferences": self.new})
        history = wiki.slot_history(self.conn, "me", "pizza_preferences")
        self.assertEqual([(r["old_value"], r["new_value"]) for r in history],
                         [(None, self.old), (self.old, self.new)])
        self.assertEqual([r["evidence_ts"][:10] for r in history],
                         ["2026-10-05", "2026-10-07"])
        evidence = trace.source_rows(self.conn, "wiki", "me.pizza_preferences", context=0)
        self.assertEqual({r["id"] for r in evidence}, {self.first, self.second})
        page = wiki.read(self.cfg.wiki_dir, "me")
        self.assertEqual(page.slots["pizza_preferences"]["lines"], [self.second])
        self.assertIn(self.new, brief.render(self.conn, self.cfg))
        self.assertNotIn(self.old, brief.render(self.conn, self.cfg))
        server = mcp_server.Server.__new__(mcp_server.Server)
        server.conn, server.cfg = self.conn, self.cfg
        opened = server.call("memcal_open", {"ref": "me"})
        self.assertIn(f"**pizza_preferences**: {self.new}", opened)
        self.assertNotIn("**dietary_preferences**:", opened)

    def test_same_key_control_still_updates_and_rejects_stale_replay(self):
        same = self.row("pizza_preferences", self.new, self.second)
        self.apply(same)
        self.assertEqual(self.facts(), {"pizza_preferences": self.new})
        self.assert_rejected(self.original, "rejected-stale")

    def test_explicit_same_key_correction_uses_the_same_validation_and_history(self):
        row = copy.deepcopy(self.correction)
        row["slot"] = "pizza_preferences"
        self.apply(row)
        self.apply(row)
        self.assertEqual(self.facts(), {"pizza_preferences": self.new})
        self.assertEqual(len(wiki.slot_history(self.conn, "me")), 2)

    def test_replaying_new_and_old_saved_receipts_is_idempotent(self):
        self.apply(self.correction)
        before = list(map(tuple, wiki.slot_history(self.conn, "me")))
        self.apply(self.correction)
        # A pre-contract receipt from the duplicated run has no supersedes or
        # claim fields. Its exact source/value resolution survives reopening.
        old_receipt = {k: self.correction[k] for k in ("page", "slot", "value", "cite_ids")}
        self.conn.close()
        self.conn = db.open_db(self.cfg.db_path)
        self.apply(old_receipt)
        self.assert_rejected(self.original, "rejected-stale")
        self.assertEqual(list(map(tuple, wiki.slot_history(self.conn, "me"))), before)
        self.assertEqual(self.facts(), {"pizza_preferences": self.new})

    def test_later_bundle_quoting_the_old_line_cannot_redate_its_cited_source(self):
        self.apply(self.correction)
        later = self.line(9, 'On Oct 5 I wrote "For pizza, avoid mushrooms; dairy is fine."')
        _, log = self.apply(self.original, extra_ids=[later])
        self.assertTrue(any("rejected-stale" in entry for entry in log), log)
        # The extraction stub emits no claim for the later historical quotation.
        quoted = Bundle(entity="person:me", title="Quote", items=[self.conn.execute(
            "SELECT * FROM archive WHERE id = ?", (later,)).fetchone()])
        apply_stage.apply_diffs(self.conn, self.cfg, [(quoted, {"wiki": []})], written_by="dream:test")
        self.assertEqual(self.facts(), {"pizza_preferences": self.new})

    def test_different_or_uncertain_scopes_cannot_supersede(self):
        for scope in ("different", "uncertain", None):
            with self.subTest(scope=scope):
                row = copy.deepcopy(self.correction)
                row["supersedes"]["scope"] = scope
                self.assert_rejected(row)
        # A lasting all-meal fact can independently coexist with pizza context.
        other = self.row("all_meals", "avoid peanuts", self.second)
        self.apply(other)
        self.assertEqual(self.facts(), {"pizza_preferences": self.old, "all_meals": "avoid peanuts"})

    def test_matching_labels_alone_never_resolve_another_person_or_slot(self):
        other = self.line(7, "I avoid dairy through October 11 inclusive.",
                          person="Example Person", from_me=False)
        row = copy.deepcopy(self.correction)
        row.update(page="example-person", subject="Example Person", cite_ids=[other])
        row["supersedes"]["quote"] = "I avoid dairy through October 11 inclusive."
        self.assert_rejected(row)
        self.assertEqual(self.facts("example-person"), {})
        row.update(page="me", subject="me")
        self.assert_rejected(row, "rejected-claim")
        temporary = self.row("dietary_preferences", "avoid dairy through October 11 inclusive", other,
                             page="example-person", subject="Example Person", durability="temporary")
        self.assert_rejected(temporary, "rejected-claim")
        self.assertEqual(self.facts("example-person"), {})

    def test_corrective_passage_must_come_from_the_claimed_speaker(self):
        other = self.line(7, self.quote, person="Example Person", from_me=False)
        own = self.line(7, "Let's order pizza.")
        row = copy.deepcopy(self.correction)
        row["cite_ids"] = [other, own]
        self.assert_rejected(row)

    def test_a_new_id_quoting_the_prior_passage_cannot_act_as_its_correction(self):
        old_text = "For pizza, avoid mushrooms; dairy is fine."
        later = self.line(9, f'Historical quotation: "{old_text}"')
        row = copy.deepcopy(self.correction)
        row["cite_ids"] = [later]
        row["supersedes"]["quote"] = old_text
        self.assert_rejected(row)

    def test_resolution_does_not_alias_the_label_for_new_sources(self):
        self.apply(self.correction)
        other = self.line(8, "For all meals I avoid peanuts.")
        independent = self.row("dietary_preferences", "avoid peanuts", other)
        self.apply(independent)
        self.assertEqual(self.facts(), {"pizza_preferences": self.new,
                                        "dietary_preferences": "avoid peanuts"})

    def test_wrong_snapshot_missing_evidence_or_invented_passage_is_rejected(self):
        changes = [{"previous": "different"}, {"slot": "unrelated"},
                   {"prior_lines": []}, {"prior_lines": [self.second]},
                   {"quote": "invented correction"}]
        for change in changes:
            with self.subTest(change=change):
                row = copy.deepcopy(self.correction)
                row["supersedes"].update(change)
                self.assert_rejected(row)
        row = copy.deepcopy(self.correction)
        row.pop("cite_ids")
        self.assert_rejected(row, "rejected-claim")

    def test_older_correction_and_manual_edits_keep_the_current_decision(self):
        stale = self.line(4, self.quote)
        row = copy.deepcopy(self.correction)
        row["cite_ids"] = [stale]
        self.assert_rejected(row)
        live.note(self.conn, self.cfg, "me", "pizza_preferences", "avoid olives", source="agent")
        self.assert_rejected(self.correction)

    def test_newer_manual_edit_blocks_replay_even_at_the_same_proposed_label(self):
        self.apply(self.correction)
        live.note(self.conn, self.cfg, "me", "pizza_preferences", "avoid olives", source="agent")
        self.assert_rejected(self.correction, "rejected-stale")
        self.assertEqual(self.facts(), {"pizza_preferences": "avoid olives"})

    def test_two_bundles_in_one_transaction_see_the_staged_prior_value(self):
        # Start with a new synthetic page; the first insert is not on disk when
        # the correction in the second bundle validates its prior snapshot.
        wiki.read(self.cfg.wiki_dir, "me").path.unlink()
        self.conn.execute("DELETE FROM slot_history")
        self.conn.execute("DELETE FROM evidence")
        self.conn.commit()
        apply_stage.apply_diffs(self.conn, self.cfg,
                                [self.proposal(self.original), self.proposal(self.correction)],
                                written_by="dream:test")
        self.assertEqual(self.facts(), {"pizza_preferences": self.new})

    def test_already_duplicated_page_is_reconciled_only_for_the_exact_correction(self):
        duplicate = copy.deepcopy(self.correction)
        duplicate.pop("supersedes")
        self.apply(duplicate)
        self.apply(self.correction)
        self.assertEqual(self.facts(), {"pizza_preferences": self.new})
        removed = wiki.slot_history(self.conn, "me", "dietary_preferences")
        self.assertEqual([(r["old_value"], r["new_value"]) for r in removed],
                         [(None, self.new), (self.new, None)])
        self.apply(duplicate)
        self.assertEqual(self.facts(), {"pizza_preferences": self.new})

    def test_existing_distinct_source_or_value_and_manual_duplicate_are_preserved(self):
        third = self.line(8, "For all meals, avoid dairy; mushrooms are fine.")
        for ids, value in [([third], self.new), ([self.second], "avoid olives")]:
            with self.subTest(ids=ids, value=value):
                wiki.set_slot(self.cfg.wiki_dir, "me", "dietary_preferences", value,
                              conn=self.conn, archive_ids=ids, evidence_ts="2026-10-08T10:00:00+00:00")
                self.assert_rejected(self.correction)

    def test_failed_transaction_rolls_back_resolution_history_and_both_slots(self):
        duplicate = copy.deepcopy(self.correction)
        duplicate.pop("supersedes")
        self.apply(duplicate)
        with mock.patch.object(wiki, "set_slot", side_effect=RuntimeError("failed write")):
            with self.assertRaisesRegex(RuntimeError, "failed write"):
                self.apply(self.correction)
        self.assertEqual(self.facts(), {"pizza_preferences": self.old, "dietary_preferences": self.new})
        self.assertEqual(self.conn.execute("SELECT count(*) FROM wiki_slot_resolutions").fetchone()[0], 0)
        self.assertEqual(len(wiki.slot_history(self.conn, "me")), 2)

    def test_old_store_gets_empty_receipt_table_without_merging_existing_pages(self):
        duplicate = copy.deepcopy(self.correction)
        duplicate.pop("supersedes")
        self.apply(duplicate)
        self.conn.execute("DROP TABLE wiki_slot_resolutions")
        self.conn.commit()
        self.conn.close()
        self.conn = db.open_db(self.cfg.db_path)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM wiki_slot_resolutions").fetchone()[0], 0)
        self.assertEqual(len(self.facts()), 2)
        self.apply(self.correction)
        self.assertEqual(self.facts(), {"pizza_preferences": self.new})

    def test_correction_contract_preserves_prior_archive_ids_while_resolving_new_cites(self):
        row = copy.deepcopy(self.correction)
        row.pop("cite_ids")
        row["cites"] = ["L1"]
        bundle = self.proposal(self.correction)[0]
        propose._resolve_cites(bundle, {"wiki": [row]})
        self.assertEqual(row["cite_ids"], [self.second])
        self.assertEqual(row["supersedes"]["prior_lines"], [self.first])


if __name__ == "__main__":
    unittest.main()
