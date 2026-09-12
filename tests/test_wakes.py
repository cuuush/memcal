"""Semantic wake evaluation: entailment decides, candidates never write.

Deterministic throughout — a fake CompletionClient stands in for the model.
The paid model layer is never run here; the checked-in eval fixture documents
the entailment/negation/delay/paraphrase contract it will grade later.
"""

from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path

try:
    from tests._support import Base
except ModuleNotFoundError:  # Direct execution: python3 tests/test_wakes.py
    from _support import Base

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import archive, db, todos  # noqa: E402
from memcal import llm  # noqa: E402
from memcal.config import Config  # noqa: E402
from memcal.dream import bundle as bundle_stage  # noqa: E402
from memcal.dream import wakes as wakes_stage  # noqa: E402

CONDITION = "Rowan is back from Italy"
TODO_TEXT = "Give Rowan back their EZ-Pass"


class FakeEntailment(llm.CompletionClient):
    """Scripted stand-in for the entailment call. Answers what it is told."""

    def __init__(self, answers=None, *, fail=False, malformed=False, cut_off=False):
        super().__init__()
        self.answers = dict(answers or {})
        self.fail = fail
        self.malformed = malformed
        self.cut_off = cut_off
        self.calls: list[dict] = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("provider exploded")
        if self.malformed:
            data = {"nope": []}
        else:
            decisions = [
                {"todo_key": key, "satisfied": bool(satisfied),
                 "cites": ["L1"] if satisfied else []}
                for key, satisfied in self.answers.items()
            ]
            data = {"decisions": decisions}
        return llm.Reply(text="", data=data, generation_id=f"gen-test-{len(self.calls)}",
                         finish_reason="length" if self.cut_off else "stop")


class WakeBase(Base):
    def setUp(self):
        super().setUp()
        self.cfg.semantic_wakes = True

    def spool(self, entity: str, texts: list[str]) -> None:
        for index, text in enumerate(texts):
            aid = archive.append(
                self.conn, stream="imessage", external_id=f"{entity}:{index}:{text[:12]}",
                ts=db.now(), text=text, thread="t", person=entity.split(":")[-1],
                from_me=False, gated=True, gate_reason="temporal")
            archive.spool_add(self.conn, aid, entity)
        self.conn.commit()

    def bundles(self):
        return bundle_stage.build(self.conn)

    def open_waiter(self, days_ago: int = 1):
        todo, _ = todos.open_todo(self.conn, TODO_TEXT, wake_condition=CONDITION)
        self.conn.execute("UPDATE todos SET opened_at = ? WHERE key = ?",
                          ((db.today() - timedelta(days=days_ago)).isoformat() + "T08:05:00",
                           todo.key))
        self.conn.commit()
        return todos.get(self.conn, todo.key)

    def woke_at(self, key: str) -> str | None:
        return todos.get(self.conn, key).woke_at


class TestCandidatePassNeverWritesWokeAt(WakeBase):
    def test_word_overlap_nominates_but_leaves_the_waiter_asleep(self):
        todo = self.open_waiter()
        found = todos.check_wakes(self.conn, "Rowan is not back from Italy")
        self.assertEqual([t.key for t in found], [todo.key])
        self.assertIsNone(self.woke_at(todo.key))

    def test_per_bundle_candidates_never_write_either(self):
        todo = self.open_waiter()
        self.spool("person:Rowan Vale", ["Rowan is not back from Italy"])
        pairs = todos.find_wake_candidates(self.conn, self.bundles(), since=db.now())
        self.assertTrue(pairs)
        self.assertIsNone(self.woke_at(todo.key))


class TestNegationDoesNotWake(WakeBase):
    def test_not_back_stays_asleep(self):
        todo = self.open_waiter()
        self.spool("person:Rowan Vale", ["Rowan is not back from Italy yet"])
        self.assertTrue(todos.find_wake_candidates(self.conn, self.bundles(), since=db.now()))
        woken, problems = wakes_stage.maybe_wake(
            self.conn, self.cfg, self.bundles(), since=db.now(), run_id=None,
            client=FakeEntailment({todo.key: False}))
        self.assertEqual(woken, [])
        self.assertEqual(problems, [])
        self.assertIsNone(self.woke_at(todo.key))


class TestDelayDoesNotWake(WakeBase):
    def test_extended_trip_stays_asleep(self):
        todo = self.open_waiter()
        self.spool("person:Rowan Vale", ["extended the Italy trip another week"])
        woken, _ = wakes_stage.maybe_wake(
            self.conn, self.cfg, self.bundles(), since=db.now(), run_id=None,
            client=FakeEntailment({todo.key: False}))
        self.assertEqual(woken, [])
        self.assertIsNone(self.woke_at(todo.key))


class TestParaphraseWakesWithSupportingContext(WakeBase):
    def test_landed_at_jfk_wakes_and_never_closes(self):
        todo = self.open_waiter()
        self.spool("person:Rowan Vale",
                   ["Rowan landed at JFK this morning, back from Rome", "welcome home!"])
        woken, problems = wakes_stage.maybe_wake(
            self.conn, self.cfg, self.bundles(), since=db.now(), run_id=None,
            client=FakeEntailment({todo.key: True}))
        self.assertEqual(problems, [])
        self.assertEqual([t.key for t in woken], [todo.key])
        current = todos.get(self.conn, todo.key)
        self.assertIsNotNone(current.woke_at)
        self.assertEqual(current.status, "open")


class TestCrossBundleWordsDoNotJointlySatisfy(WakeBase):
    def test_split_words_across_conversations_stay_asleep(self):
        todo = self.open_waiter()
        self.spool("person:Rowan Vale", ["Rowan said hi"])
        self.spool("person:Travel", ["Italy flights are expensive"])
        bundles = self.bundles()
        self.assertGreaterEqual(len(bundles), 2)
        client = FakeEntailment({todo.key: False})
        woken, _ = wakes_stage.maybe_wake(
            self.conn, self.cfg, bundles, since=db.now(), run_id=None, client=client)
        self.assertEqual(woken, [])
        self.assertIsNone(self.woke_at(todo.key))
        suffix = str(client.calls[0]["suffix"])
        block_a = suffix.find("Rowan said hi")
        block_b = suffix.find("Italy flights are expensive")
        self.assertNotEqual(block_a, -1)
        self.assertNotEqual(block_b, -1)
        between = suffix[min(block_a, block_b):max(block_a, block_b)]
        self.assertIn("candidate from BUNDLE", between)


class TestOpenerPassCannotWakeItsOwnWaiter(WakeBase):
    def test_traffic_that_creates_a_waiter_cannot_satisfy_it(self):
        before = db.now()
        todo, _ = todos.open_todo(self.conn, TODO_TEXT, wake_condition=CONDITION)
        self.spool("person:Rowan Vale", ["just landed back from italy"])
        client = FakeEntailment({todo.key: True})
        woken, _ = wakes_stage.maybe_wake(
            self.conn, self.cfg, self.bundles(), since=before, run_id=None, client=client)
        self.assertEqual(woken, [])
        self.assertEqual(client.calls, [])
        self.assertIsNone(self.woke_at(todo.key))


class TestNoCandidatesMakesNoModelCall(WakeBase):
    def test_unrelated_traffic_never_reaches_the_model(self):
        todo = self.open_waiter()
        self.spool("person:Jordan Lee", ["poker moved to saturday"])
        client = FakeEntailment({todo.key: True})
        woken, _ = wakes_stage.maybe_wake(
            self.conn, self.cfg, self.bundles(), since=db.now(), run_id=None, client=client)
        self.assertEqual(woken, [])
        self.assertEqual(client.calls, [])
        self.assertIsNone(self.woke_at(todo.key))


class TestModelFailureLeavesWaiterAsleep(WakeBase):
    def test_unavailable_model_is_recorded_and_writes_nothing(self):
        todo = self.open_waiter()
        self.spool("person:Rowan Vale", ["just landed back from italy"])
        woken, problems = wakes_stage.maybe_wake(
            self.conn, self.cfg, self.bundles(), since=db.now(), run_id=None,
            client=FakeEntailment({todo.key: True}, fail=True))
        self.assertEqual(woken, [])
        self.assertTrue(problems)
        self.assertIsNone(self.woke_at(todo.key))
        failures = list((self.cfg.home / "calls" / "live").glob("fail-*.json"))
        self.assertTrue(failures)

    def test_malformed_output_is_ignored(self):
        todo = self.open_waiter()
        self.spool("person:Rowan Vale", ["just landed back from italy"])
        woken, problems = wakes_stage.maybe_wake(
            self.conn, self.cfg, self.bundles(), since=db.now(), run_id=None,
            client=FakeEntailment({todo.key: True}, malformed=True))
        self.assertEqual(woken, [])
        self.assertTrue(problems)
        self.assertIsNone(self.woke_at(todo.key))

    def test_truncated_output_is_ignored(self):
        todo = self.open_waiter()
        self.spool("person:Rowan Vale", ["just landed back from italy"])
        woken, problems = wakes_stage.maybe_wake(
            self.conn, self.cfg, self.bundles(), since=db.now(), run_id=None,
            client=FakeEntailment({todo.key: True}, cut_off=True))
        self.assertEqual(woken, [])
        self.assertTrue(problems)
        self.assertIsNone(self.woke_at(todo.key))


class TestStageDisabledByDefault(WakeBase):
    def test_fresh_config_leaves_waiters_asleep_without_a_call(self):
        fresh = Config(home=self.cfg.home)
        self.assertFalse(fresh.semantic_wakes)
        todo = self.open_waiter()
        self.spool("person:Rowan Vale", ["just landed back from italy"])
        client = FakeEntailment({todo.key: True})
        woken, _ = wakes_stage.maybe_wake(
            self.conn, fresh, self.bundles(), since=db.now(), run_id=None, client=client)
        self.assertEqual(woken, [])
        self.assertEqual(client.calls, [])
        self.assertIsNone(self.woke_at(todo.key))


class TestEvalFixtureRunner(WakeBase):
    def test_each_fixture_case_resolves_as_labeled(self):
        import tempfile

        cases = wakes_stage.load_eval_cases()
        self.assertGreaterEqual(len(cases), 8)
        categories = {case["category"] for case in cases}
        self.assertTrue({"entailment", "negation", "delay", "paraphrase"} <= categories)
        for case in cases:
            with self.subTest(case=case["name"]):
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                cfg = Config(home=Path(tmp.name))
                cfg.ensure_dirs()
                cfg.semantic_wakes = True
                conn = db.open_db(cfg.db_path)
                try:
                    key = f"todo:eval-{case['name']}"
                    todos.open_todo(conn, f"eval {case['name']}",
                                    key=key, wake_condition=case["wake_condition"])
                    conn.execute(
                        "UPDATE todos SET opened_at = ? WHERE key = ?",
                        ((db.today() - timedelta(days=1)).isoformat() + "T08:05:00", key))
                    for index, line in enumerate(case["lines"]):
                        aid = archive.append(
                            conn, stream="imessage",
                            external_id=f"eval-{case['name']}-{index}", ts=db.now(),
                            text=line, thread="t", person="Rowan Vale",
                            from_me=False, gated=True, gate_reason="temporal")
                        archive.spool_add(conn, aid, "person:Rowan Vale")
                    conn.commit()
                    bundles = bundle_stage.build(conn)
                    client = FakeEntailment({key: case["expected"]})
                    woken, problems = wakes_stage.maybe_wake(
                        conn, cfg, bundles, since=db.now(), run_id=None, client=client)
                    self.assertEqual(problems, [])
                    if case["expected"]:
                        self.assertEqual([t.key for t in woken], [key])
                        self.assertIsNotNone(todos.get(conn, key).woke_at)
                        self.assertEqual(todos.get(conn, key).status, "open")
                    else:
                        self.assertEqual(woken, [])
                        self.assertIsNone(todos.get(conn, key).woke_at)
                finally:
                    conn.close()


if __name__ == "__main__":
    unittest.main()
