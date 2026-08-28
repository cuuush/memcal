"""Staged propose: one rubric per turn, one conversation, one merged result.

Test class names are the thing that would break, per the rules in AGENTS.md.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memcal import db  # noqa: E402
from memcal.config import Config  # noqa: E402
from memcal.dream import propose, stages  # noqa: E402
from memcal.dream.bundle import Bundle  # noqa: E402
from memcal.llm import Reply, Usage  # noqa: E402


def _cfg(spec: str = "on", **kw) -> Config:
    cfg = Config(home=Path("/tmp/memcal-stages-test"))
    cfg.propose_stages = spec
    cfg.propose_model = "stepfun/step-3.7-flash"
    for key, value in kw.items():
        setattr(cfg, key, value)
    return cfg


def _bundle(entity: str = "person:Jordan Lee") -> Bundle:
    return Bundle(entity=entity, title="Jordan Lee", items=[])


class ScriptedClient:
    """Answers each turn from a list, recording exactly what it was sent."""

    def __init__(self, answers: list[dict], truncate_at: int | None = None):
        self.answers = answers
        self.truncate_at = truncate_at
        self.sent: list[dict] = []

    def complete(self, **kw):
        index = len(self.sent)
        self.sent.append(kw)
        body = self.answers[index] if index < len(self.answers) else {"diffs": []}
        return Reply(text=json.dumps(body), data=body, usage=Usage(),
                     model=kw.get("model", ""),
                     generation_id=f"gen-{index}",
                     finish_reason="length" if index == self.truncate_at else "stop")


class TestAStagePlanIsParsedOrRefused(unittest.TestCase):
    def test_empty_means_the_single_call(self):
        self.assertEqual(stages.parse(""), [])
        self.assertEqual(stages.parse(None), [])
        self.assertEqual(stages.parse("off"), [])

    def test_on_expands_to_the_default_order(self):
        self.assertEqual([s.name for s in stages.parse("on")],
                         list(stages.DEFAULT_ORDER))

    def test_an_explicit_list_keeps_its_own_order(self):
        self.assertEqual([s.name for s in stages.parse("questions,calendar")],
                         ["questions", "calendar"])

    def test_a_typo_raises_instead_of_silently_running_a_shorter_plan(self):
        # The whole point of the feature is that a category of memory stopped being
        # asked for. Degrading to three stages because the fourth was misspelt is that
        # same bug, reintroduced by a config file.
        with self.assertRaises(stages.UnknownStage):
            stages.parse("calendar,todoes")

    def test_a_stage_named_twice_is_asked_once(self):
        self.assertEqual([s.name for s in stages.parse("todos,todos")], ["todos"])

    def test_every_diff_array_is_owned_by_exactly_one_default_stage(self):
        owners: dict[str, list[str]] = {}
        for stage in stages.parse("on"):
            for field in stage.fields:
                owners.setdefault(field, []).append(stage.name)
        self.assertEqual(sorted(owners), sorted(stages.ALL_FIELDS))
        for field, names in owners.items():
            self.assertEqual(len(names), 1, f"{field} is claimed by {names}")

    def test_a_partial_plan_reports_what_it_will_never_ask_for(self):
        self.assertEqual(stages.uncovered(stages.parse("calendar,todos")),
                         ["wiki", "standing", "questions"])
        self.assertEqual(stages.uncovered(stages.parse("on")), [])


class TestAStageSchemaCannotExpressAnotherStagesWork(unittest.TestCase):
    """A todos turn that can still return `events` re-sends the calendar it wrote."""

    def test_only_the_owned_arrays_are_in_the_entry(self):
        todos = stages.STAGES["todos"]
        entry = propose.stage_schema(todos, first=False)["properties"]["diffs"]["items"]
        self.assertEqual(sorted(entry["properties"]), ["bundle", "todos"])
        self.assertFalse(entry["additionalProperties"])

    def test_reviewed_rides_on_the_first_turn_only(self):
        first = propose.stage_schema(stages.STAGES["calendar"], first=True)
        later = propose.stage_schema(stages.STAGES["todos"], first=False)
        self.assertIn("reviewed", first["properties"])
        self.assertNotIn("reviewed", later["properties"])
        self.assertEqual(later["required"], ["diffs"])

    def test_every_field_a_stage_claims_exists_in_the_v2_diff(self):
        self.assertTrue(stages.STAGES, "no stages to check")
        for stage in stages.STAGES.values():
            for field in stage.fields:
                self.assertIn(field, propose.BUNDLE_DIFF_V2["properties"], field)


class TestTheBundlesAreSentOnceAndReadBackOnEveryTurnAfter(unittest.TestCase):
    """The cost argument for staging is the cache. If turn 2 re-sends the traffic in
    its own `suffix` the decomposition costs 4x and is not worth having."""

    def setUp(self):
        self.client = ScriptedClient([
            {"reviewed": ["abc123"], "diffs": []},
            {"diffs": []}, {"diffs": []}, {"diffs": []},
        ])
        propose.propose_group(self.client, _cfg(), "PREFIX", [_bundle()],
                              suffix="THE TRAFFIC")

    def test_one_call_per_stage(self):
        self.assertEqual(len(self.client.sent), 4)

    def test_the_traffic_appears_in_exactly_one_message(self):
        self.assertTrue(self.client.sent, "no calls were made")
        for call in self.client.sent:
            wire = json.dumps([call["suffix"], call.get("turns") or []])
            self.assertEqual(wire.count("THE TRAFFIC"), 1)

    def test_the_prefix_is_identical_on_every_turn_so_it_stays_cached(self):
        self.assertEqual({call["prefix"] for call in self.client.sent}, {"PREFIX"})

    def test_later_turns_replay_the_conversation_in_wire_order(self):
        last = self.client.sent[-1]
        roles = [t["role"] for t in last["turns"]]
        # The first ask rode on the suffix, so the replay opens with the answer to it
        # and then alternates. The fourth turn replays three answers and three asks.
        self.assertEqual(roles, ["assistant", "user", "assistant",
                                 "user", "assistant", "user"])

    def test_the_first_ask_rides_on_the_suffix_not_a_second_user_message(self):
        self.assertIsNone(self.client.sent[0].get("turns"))
        self.assertIn("THE CALENDAR", self.client.sent[0]["suffix"])

    def test_each_turn_asks_for_its_own_stage(self):
        asks = [self.client.sent[0]["suffix"]] + [
            call["turns"][-1]["content"] for call in self.client.sent[1:]]
        # `zip` stops at the shorter side, so three asks would check three words.
        self.assertEqual(len(asks), 4)
        for ask, word in zip(asks, ("CALENDAR", "TO-DOS", "PAGES", "QUESTIONS")):
            self.assertIn(word, ask)

    def test_the_model_sees_its_own_previous_answers(self):
        replayed = [t["content"] for t in self.client.sent[-1]["turns"]
                    if t["role"] == "assistant"]
        self.assertEqual(len(replayed), 3)
        self.assertIn("reviewed", replayed[0])


class TestEachStagesRowsKeepTheGenerationIdThatWroteThem(unittest.TestCase):
    """Provenance is per row and a generation id is per call. Merging four turns into
    one payload before routing would file the to-do under the calendar's call."""

    def test_a_turn_is_returned_per_stage_with_its_own_reply(self):
        client = ScriptedClient([
            {"reviewed": ["x"], "diffs": [{"bundle": propose.bundle_id("person:Jordan Lee"),
                                           "events": [{"title": "Poker"}]}]},
            {"diffs": [{"bundle": propose.bundle_id("person:Jordan Lee"),
                        "todos": [{"op": "open", "text": "Bring chips"}]}]},
            {"diffs": []}, {"diffs": []},
        ])
        _group, merged, turns = propose.propose_group(
            client, _cfg(), "P", [_bundle()], suffix="S")
        self.assertEqual([t.stage for t in turns],
                         ["calendar", "todos", "pages", "questions"])
        self.assertEqual([t.reply.generation_id for t in turns],
                         ["gen-0", "gen-1", "gen-2", "gen-3"])
        # The merged payload is what the second-look heuristic reads: everything the
        # request said, in one place, without losing which turn said it.
        self.assertEqual(merged["reviewed"], ["x"])
        self.assertEqual(len(merged["diffs"]), 2)

    def test_the_merged_payload_routes_both_stages_onto_one_bundle(self):
        bundle = _bundle()
        bid = propose.bundle_id(bundle.entity)
        merged = {"reviewed": [bid], "diffs": [
            {"bundle": bid, "events": [{"title": "Poker"}]},
            {"bundle": bid, "todos": [{"op": "open", "text": "Bring chips"}]},
        ]}
        routed, _echoed = propose._route_v2([bundle], merged, [])
        self.assertEqual(len(routed), 1)
        _b, diff = routed[0]
        self.assertEqual(len(diff["events"]), 1)
        self.assertEqual(len(diff["todos"]), 1)


class TestAStagedRequestFailsWholeRatherThanPartway(unittest.TestCase):
    """A request that keeps the stages that finished marks its bundles read, and the
    traffic whose to-dos were never asked for leaves the queue looking handled."""

    def test_truncation_on_a_later_turn_raises(self):
        client = ScriptedClient(
            [{"reviewed": ["x"], "diffs": []}, {"diffs": []}, {"diffs": []}],
            truncate_at=2)
        with self.assertRaises(propose.Truncated) as caught:
            propose.propose_group(client, _cfg(), "P", [_bundle()], suffix="S")
        self.assertIn("pages", str(caught.exception))

    def test_truncation_on_the_first_turn_still_raises(self):
        client = ScriptedClient([{"reviewed": [], "diffs": []}], truncate_at=0)
        with self.assertRaises(propose.Truncated):
            propose.propose_group(client, _cfg(), "P", [_bundle()], suffix="S")


class TestStagingIsOffUnlessAskedFor(unittest.TestCase):
    def test_the_default_config_runs_one_call(self):
        cfg = Config(home=Path("/tmp/memcal-stages-test"))
        self.assertEqual(cfg.propose_stages, "")
        self.assertEqual(propose.stage_plan(cfg), [])

    def test_the_single_pass_shape_is_unchanged(self):
        client = ScriptedClient([{"reviewed": ["x"], "diffs": []}])
        _group, payload, turns = propose.propose_group(
            client, _cfg(""), "P", [_bundle()], suffix="S")
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(client.sent[0]["suffix"], "S")
        self.assertIsNone(client.sent[0].get("turns"))
        self.assertEqual(client.sent[0]["schema"], propose.DIFF_SCHEMA_V2)
        self.assertEqual([t.stage for t in turns], [""])
        self.assertEqual(payload["reviewed"], ["x"])

    def test_v1_never_stages_because_it_has_no_bundle_ids_to_amend_by(self):
        cfg = _cfg("on", prompt_version="v1")
        self.assertEqual(propose.stage_plan(cfg), [])


class TestTheAsksAreNumberedAgainstThePlanTheyRunIn(unittest.TestCase):
    def test_a_two_stage_plan_says_two(self):
        plan = stages.parse("calendar,todos")
        self.assertIn("PASS 1 OF 2", stages.ask_for(plan, 0))
        self.assertIn("PASS 2 OF 2", stages.ask_for(plan, 1))

    def test_no_ask_leaks_an_unformatted_placeholder(self):
        plan = stages.parse("on")
        self.assertTrue(plan, "an empty plan leaks nothing and proves nothing")
        for index in range(len(plan)):
            self.assertNotIn("{", stages.ask_for(plan, index))




class TestTheFlexTierIsAskedForAndPricedAsAsked(unittest.TestCase):
    """Half the rate for slower service. The trap is quoting one and billing the other."""

    def test_luna_asks_for_flex_and_step_does_not(self):
        from memcal import llm
        self.assertEqual(llm.endpoint("openai/gpt-5.6-luna").service_tier, "flex")
        self.assertIsNone(llm.endpoint("stepfun/step-3.7-flash").service_tier)

    def test_the_quote_uses_the_flex_rate_not_the_standard_one(self):
        from memcal import llm
        self.assertEqual(llm.rates("openai/gpt-5.6-luna"), (0.05, 0.30))
        self.assertEqual(llm.PRICES["openai/gpt-5.6-luna"], (0.10, 0.60))
        # A million input tokens at flex is half of what the standard table says.
        self.assertAlmostEqual(llm.price("openai/gpt-5.6-luna", 1_000_000), 0.05)
        self.assertAlmostEqual(
            llm.price("openai/gpt-5.6-luna", 1_000_000, output=True), 0.30)

    def test_a_model_with_no_flex_endpoint_prices_at_its_standard_rate(self):
        from memcal import llm
        self.assertEqual(llm.rates("stepfun/step-3.7-flash"),
                         llm.PRICES["stepfun/step-3.7-flash"])

    def test_the_estimate_survives_a_flex_model(self):
        from memcal import llm
        quote = llm.packed_cost("openai/gpt-5.6-luna", prefix_tokens=1000,
                                suffix_tokens=1000, output_tokens=1000,
                                requests=4, max_parallel=8)
        self.assertTrue(quote["priced"])


class TestWhatWasAskedForIsNotWhatWasServed(unittest.TestCase):
    """A flex request to a model with no flex endpoint is filled at standard rates and
    says nothing about it. Recording the served tier is what makes that checkable."""

    def _client(self, response: dict):
        from memcal.llm import OpenRouter
        sent: list[dict] = []

        class Recorder(OpenRouter):
            def __init__(self):
                self.headers, self.timeout, self.usage = {}, 300.0, Usage()

            def _post(self, path, payload, **kw):
                sent.append({**kw, "payload": payload})
                return response

        return Recorder(), sent

    def _ok(self, **extra):
        return {"id": "gen-x", "choices": [{"message": {"content": "{}"},
                                            "finish_reason": "stop"}],
                "usage": {}, **extra}

    def test_a_flex_endpoint_sends_the_tier_top_level(self):
        client, sent = self._client(self._ok(service_tier="flex"))
        reply = client.complete(model="openai/gpt-5.6-luna", prefix="p", suffix="s")
        self.assertEqual(sent[0]["payload"]["service_tier"], "flex")
        self.assertNotIn("service_tier", sent[0]["payload"]["provider"])
        self.assertEqual(reply.service_tier, "flex")

    def test_a_flex_request_waits_longer_than_the_default_read_timeout(self):
        from memcal import llm
        client, sent = self._client(self._ok())
        client.complete(model="openai/gpt-5.6-luna", prefix="p", suffix="s")
        self.assertEqual(sent[0]["timeout"], llm.FLEX_TIMEOUT)

    def test_a_standard_endpoint_sends_no_tier_and_keeps_its_timeout(self):
        client, sent = self._client(self._ok())
        client.complete(model="stepfun/step-3.7-flash", prefix="p", suffix="s")
        self.assertNotIn("service_tier", sent[0]["payload"])
        self.assertIsNone(sent[0]["timeout"])

    def test_a_caller_can_force_the_default_tier_back_on(self):
        client, sent = self._client(self._ok())
        client.complete(model="openai/gpt-5.6-luna", prefix="p", suffix="s",
                        service_tier="")
        self.assertNotIn("service_tier", sent[0]["payload"])

    def test_a_provider_that_reports_nothing_leaves_the_field_empty(self):
        client, _sent = self._client(self._ok())
        reply = client.complete(model="openai/gpt-5.6-luna", prefix="p", suffix="s")
        self.assertEqual(reply.service_tier, "")

if __name__ == "__main__":
    unittest.main()
