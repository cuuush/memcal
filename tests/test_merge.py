"""The Merge stage: many mentions of one event become one row.

Every case here is drawn from the run that motivated the stage — a beer garden arranged
across four conversations, which arrived as three calendar rows on two different days.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal.config import Config                      # noqa: E402
from memcal.dream import merge as merge_stage         # noqa: E402
from memcal.dream.bundle import Bundle                # noqa: E402


def mention(entity: str, **row):
    """One bundle's proposal, in the shape propose_all emits."""
    row.setdefault("subject", "me")
    return (Bundle(entity=entity), {"events": [row]}, "gen-x")


def rows(proposals):
    return [r for _b, diff, _g in proposals for r in (diff.get("events") or [])]


class FakeClient:
    """Stands in for OpenRouter. Records what it was asked, answers what it is told."""

    def __init__(self, answer=None, fail=False):
        self.answer, self.fail, self.calls = answer, fail, []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("provider exploded")

        class Reply:
            data = self.answer
            usage = type("U", (), {"cost": 0.0, "completion_tokens": 0, "prompt_tokens": 0})()
            generation_id = "gen-r"
            finish_reason = "stop"
            truncated = False
            text = ""
            reasoning = ""
        return Reply()


class TestClustering(unittest.TestCase):
    """Deterministic and free. Getting this right is what keeps the model bill at zero
    on a normal run, where nothing is duplicated."""

    def test_the_real_case_clusters(self):
        # The three rows one live run actually produced for one evening.
        proposals = [
            mention("person:Quinn Brooks", date="2026-08-01",
                    title="Bier gardens with Quinn and Jamie",
                    participants=["Quinn Brooks", "Jamie"]),
            mention("thread:imessage:group", date="2026-08-02",
                    title="Beer garden at Bohemian Hall", location="Harbor Point",
                    participants=["Jamie", "Quinn Brooks"]),
            mention("person:Avery Morgan", date="2026-08-02",
                    title="Beer garden in Queens with Quinn and Jamie",
                    participants=["Avery Morgan", "Quinn Brooks"]),
        ]
        groups = merge_stage.cluster([
            merge_stage.Mention(r, b, d) for b, d, _g in proposals
            for r in d["events"]])
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 3)

    def test_two_poker_nights_a_week_apart_stay_separate(self):
        a = mention("person:Jordan", date="2026-08-01", title="Poker at Robbie's",
                    participants=["Jordan Lee"])
        b = mention("person:Jordan", date="2026-08-15", title="Poker at Robbie's",
                    participants=["Jordan Lee"])
        groups = merge_stage.cluster([
            merge_stage.Mention(r, bd, d) for bd, d, _g in (a, b) for r in d["events"]])
        self.assertEqual(len(groups), 2)

    def test_what_happened_is_not_folded_into_what_is_planned(self):
        """Merging last week's poker into next week's deletes one of two real evenings."""
        past = mention("person:Jordan", date="2026-07-29", title="Poker at Robbie's",
                       status="happened", participants=["Jordan Lee"])
        soon = mention("person:Jordan", date="2026-08-01", title="Poker at Robbie's",
                       status="confirmed", participants=["Jordan Lee"])
        groups = merge_stage.cluster([
            merge_stage.Mention(r, b, d) for b, d, _g in (past, soon) for r in d["events"]])
        self.assertEqual(len(groups), 2)

    def test_a_shared_stopword_is_not_a_match(self):
        a = mention("person:A", date="2026-08-01", title="Dinner with Mom",
                    participants=["Mom"])
        b = mention("person:B", date="2026-08-01", title="Movie with Logan",
                    participants=["Logan"])
        groups = merge_stage.cluster([
            merge_stage.Mention(r, bd, d) for bd, d, _g in (a, b) for r in d["events"]])
        self.assertEqual(len(groups), 2)


class TestResolving(unittest.TestCase):
    def setUp(self):
        self.cfg = Config(home=Path("/tmp"))

    def test_agreeing_fragments_merge_without_a_model_call(self):
        """Same day, same title, different guests — a union settles it, so paying a
        model to say so would be waste on the most common kind of duplicate."""
        proposals = [
            mention("thread:group", date="2026-08-02", title="Beer garden",
                    participants=["Quinn Brooks"], location="Harbor Point"),
            mention("person:Avery Morgan", date="2026-08-02", title="Beer garden",
                    participants=["Avery Morgan"]),
        ]
        client = FakeClient()
        out, log = merge_stage.merge_all(client, self.cfg, proposals)
        self.assertEqual(client.calls, [])
        self.assertEqual(len(rows(out)), 1)
        self.assertEqual(sorted(rows(out)[0]["participants"]),
                         ["Avery Morgan", "Quinn Brooks"])
        self.assertEqual(rows(out)[0]["location"], "Harbor Point")   # pooled, not lost
        self.assertEqual(len(log), 1)

    def test_a_specific_address_beats_a_vague_name_without_a_model_call(self):
        proposals = [
            mention("thread:groupme:poker", date="2026-08-08",
                    title="Poker at Jordan's", location="Alex's place",
                    participants=["Jordan Lee", "Alex Rivera"]),
            mention("person:Jordan Lee", date="2026-08-08",
                    title="Poker at Jordan's", location="42 Example Street, Alex's place",
                    participants=["Jordan Lee", "Alex Rivera"]),
        ]
        client = FakeClient()
        out, _log = merge_stage.merge_all(client, self.cfg, proposals)
        self.assertEqual(client.calls, [])
        self.assertEqual(rows(out)[0]["location"], "42 Example Street, Alex's place")

    def test_genuinely_different_locations_reach_the_resolver(self):
        proposals = [
            mention("thread:a", date="2026-08-08", title="Poker",
                    location="42 Example Street", participants=["Jordan Lee"]),
            mention("thread:b", date="2026-08-08", title="Poker",
                    location="Bohemian Hall", participants=["Jordan Lee"]),
        ]
        client = FakeClient(answer={
            "same_event": True, "date": "2026-08-08", "title": "Poker",
            "location": "42 Example Street", "participants": ["Jordan Lee"],
            "why": "the arranging thread gave the correction",
        })
        out, _log = merge_stage.merge_all(client, self.cfg, proposals)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(rows(out)[0]["location"], "42 Example Street")

    def test_a_disagreement_is_settled_by_the_model_with_sources_in_view(self):
        proposals = [
            mention("person:Quinn Brooks", date="2026-08-01",
                    title="Bier gardens", participants=["Quinn Brooks", "Jamie"]),
            mention("thread:imessage:trust-paperwork", date="2026-08-01",
                    title="Beer garden with Julian", participants=["Avery Morgan"]),
            mention("thread:imessage:group", date="2026-08-02",
                    title="Beer garden at Bohemian Hall", location="Harbor Point",
                    time="after 6", participants=["Jamie", "Quinn Brooks"]),
        ]
        client = FakeClient(answer={
            "same_event": True, "date": "2026-08-02", "time": "after 6",
            "title": "Beer garden at Bohemian Hall", "location": "Harbor Point",
            "status": "confirmed",
            "participants": ["Quinn Brooks", "Jamie", "Avery Morgan"],
            "why": "the group thread settled on Sunday; the paperwork thread only said "
                   "'next weekend' in passing",
        })
        out, log = merge_stage.merge_all(client, self.cfg, proposals)
        self.assertEqual(len(client.calls), 1)
        # Every fragment reaches the model *with the bundle it came from*: that is the
        # evidence apply never had, and the only basis for preferring one date.
        suffix = client.calls[0]["suffix"]
        self.assertIn("thread:imessage:trust-paperwork", suffix)
        self.assertIn("person:Quinn Brooks", suffix)
        surviving = rows(out)
        self.assertEqual(len(surviving), 1)
        self.assertEqual(surviving[0]["date"], "2026-08-02")
        # The reason is carried into the run log: a merge that silently moved a date is
        # exactly the failure this stage exists to prevent, so it has to say why.
        self.assertIn("only said 'next weekend' in passing", log[0])

    def test_the_model_may_refuse_to_merge(self):
        proposals = [
            mention("person:A", date="2026-08-01", title="Poker night",
                    participants=["Jordan Lee"]),
            mention("person:B", date="2026-08-03", title="Poker night",
                    participants=["Jordan Lee"]),
        ]
        client = FakeClient(answer={"same_event": False, "date": "", "title": "",
                                    "participants": [], "why": "two different games"})
        out, _log = merge_stage.merge_all(client, self.cfg, proposals)
        self.assertEqual(len(rows(out)), 2)

    def test_a_failed_call_merges_on_agreement_rather_than_losing_rows(self):
        """A Merge call that cannot reach the model must not drop anything. Falling back
        to the union is worse than a perfect answer and much better than either
        emitting every duplicate or swallowing a real plan."""
        proposals = [
            mention("person:A", date="2026-08-01", title="Beer garden",
                    participants=["Quinn Brooks"]),
            mention("person:B", date="2026-08-02", title="Beer garden at Bohemian Hall",
                    participants=["Jamie"]),
        ]
        out, log = merge_stage.merge_all(FakeClient(fail=True), self.cfg, proposals)
        surviving = rows(out)
        self.assertEqual(len(surviving), 1)
        self.assertEqual(sorted(surviving[0]["participants"]), ["Jamie", "Quinn Brooks"])
        self.assertIn("merge failed", log[0])

    def test_nothing_to_do_costs_nothing(self):
        proposals = [
            mention("person:A", date="2026-08-01", title="Dentist"),
            mention("person:B", date="2026-08-09", title="Dad's birthday"),
        ]
        client = FakeClient()
        out, log = merge_stage.merge_all(client, self.cfg, proposals)
        self.assertEqual(client.calls, [])
        self.assertEqual(log, [])
        self.assertEqual(len(rows(out)), 2)

    def test_todos_and_wiki_are_never_touched(self):
        bundle = Bundle(entity="person:A")
        diff = {"events": [{"date": "2026-08-01", "title": "Beer garden", "subject": "me"}],
                "todos": [{"text": "ask julian"}], "wiki": [{"page": "robbie", "slot": "hosts"}]}
        other = mention("person:B", date="2026-08-01", title="Beer garden")
        out, _log = merge_stage.merge_all(
            FakeClient(), Config(home=Path("/tmp")), [(bundle, diff, "g"), other])
        self.assertEqual(diff["todos"], [{"text": "ask julian"}])
        self.assertEqual(diff["wiki"], [{"page": "robbie", "slot": "hosts"}])
        self.assertEqual(len(rows(out)), 1)

    def test_an_existing_key_survives_the_merge(self):
        """Without the key, apply mints a second row beside the one being amended."""
        proposals = [
            mention("person:A", date="2026-08-02", title="Beer garden",
                    key="beer-garden@2026-08-02", participants=["Quinn Brooks"]),
            mention("person:B", date="2026-08-02", title="Beer garden",
                    participants=["Jamie"]),
        ]
        out, _log = merge_stage.merge_all(FakeClient(), self.cfg, proposals)
        self.assertEqual(rows(out)[0]["key"], "beer-garden@2026-08-02")


class TestOneFriendGroupIsNotOneEvent(unittest.TestCase):

    def _groups(self, *mentions):
        return merge_stage.cluster([
            merge_stage.Mention(r, b, d) for b, d, _g in mentions for r in d["events"]])

    def test_three_plans_one_crew_stay_three(self):
        crew = ["Alex Rivera", "Cameron Ortiz"]
        ramen = mention("thread:whatsapp:dinner", date="2026-08-06",
                        title="Ramen dinner", participants=crew + ["Riley Morgan"])
        poker = mention("thread:groupme:poker", date="2026-08-07",
                        title="Poker at Jordan's", participants=crew + ["Jordan Lee"])
        beer = mention("thread:groupme:beer", date="2026-08-08",
                       title="Beer garden", participants=crew)
        self.assertEqual(len(self._groups(ramen, poker, beer)), 3)

    def test_the_same_guests_on_the_same_evening_still_cluster(self):
        # The case the rule was written for must survive the bound.
        a = mention("person:Quinn", date="2026-08-01",
                    title="Drinks", participants=["Quinn Brooks", "Jamie"])
        b = mention("thread:imessage:g", date="2026-08-01",
                    title="Bohemian Hall", participants=["Jamie", "Quinn Brooks"])
        self.assertEqual(len(self._groups(a, b)), 1)

    def test_a_day_apart_with_nothing_else_in_common_is_two_events(self):
        # The bound is zero rather than one because consecutive days chain: ramen
        # Thursday matched poker Friday, poker Friday matched the beer garden Saturday,
        # and union-find made that one cluster. Anything wider needs a real case.
        a = mention("person:Quinn", date="2026-08-01",
                    title="Drinks", participants=["Quinn Brooks", "Jamie"])
        b = mention("thread:imessage:g", date="2026-08-02",
                    title="Bohemian Hall", participants=["Jamie", "Quinn Brooks"])
        self.assertEqual(len(self._groups(a, b)), 2)

    def test_wording_still_clusters_across_the_wider_window(self):
        # Narrowing the guest-list rule must not narrow the title rule with it: two
        # mentions that share real words are still one event four days apart.
        a = mention("person:Quinn", date="2026-08-01", title="Beer garden in Queens",
                    participants=["Quinn Brooks"])
        b = mention("thread:imessage:g", date="2026-08-04",
                    title="Beer garden at Bohemian Hall", participants=["Jamie"])
        self.assertEqual(len(self._groups(a, b)), 1)


class TestTheRowsThatMostNeedMergingAreTheField_PoorOnes(unittest.TestCase):

    def _groups(self, *mentions, cfg=None):
        return merge_stage.cluster(
            [merge_stage.Mention(r, b, d)
             for b, d, _g in mentions for r in d["events"]], cfg)

    def test_beer_hall_and_beer_garden_on_one_evening_are_one_cluster(self):
        a = mention("thread:imessage:reese", date="2026-08-02", title="Beer hall")
        b = mention("person:Quinn Brooks", date="2026-08-02", title="Beer garden")
        self.assertEqual(len(self._groups(a, b)), 1)

    def test_poker_and_poker_game_on_one_evening_are_one_cluster(self):
        a = mention("thread:groupme:poker", date="2026-08-01", title="Poker")
        b = mention("person:Jordan Lee", date="2026-08-01", title="Poker game")
        self.assertEqual(len(self._groups(a, b)), 1)

    def test_a_row_with_guests_or_a_place_still_needs_two_words(self):
        """The loosened threshold applies only where the fields are missing. Rows that
        do name people and places are matchable on those, and one word between them is
        the "poker and lunch, both with Quinn on Tuesday" case that must stay two."""
        a = mention("person:Quinn", date="2026-08-04", title="Poker",
                    participants=["Quinn Brooks"], location="Alex's place")
        b = mention("thread:imessage:g", date="2026-08-04", title="Poker lunch",
                    participants=["Jamie"], location="Bohemian Hall")
        self.assertEqual(len(self._groups(a, b)), 2)

    def test_one_field_poor_row_is_enough_to_lower_the_bar(self):
        # Either, not both. A duplicate is a pair, and it only takes one side with
        # nothing but a title for the pair to be out of reach at two words — the richer
        # row cannot find its twin on fields the twin does not have.
        a = mention("thread:a", date="2026-08-04", title="Drinks")
        b = mention("thread:b", date="2026-08-04", title="Drinks reservation",
                    location="Bohemian Hall")
        self.assertEqual(len(self._groups(a, b)), 1)

    def test_a_month_apart_is_still_two_events_however_poor_the_rows(self):
        """The date bound does the work that makes the loosened threshold safe: a weekly
        poker game must not absorb next month's on the strength of one word."""
        a = mention("thread:groupme:poker", date="2026-08-01", title="Poker")
        b = mention("person:Jordan Lee", date="2026-09-01", title="Poker game")
        self.assertEqual(len(self._groups(a, b)), 2)

    def test_a_platform_tag_is_never_what_makes_two_rows_one(self):
        """Partiful stamps "| Partiful" on every title it exports, so `partiful` is a
        shared distinctive word between every party in the store. "Jack's 30th" on 08-22
        and "Capture The Flag 2 - Trojan War" on 08-23 share nothing else, carry no
        participants, and are one day apart."""
        a = mention("thread:email:partiful", date="2026-08-22",
                    title="Jack's 30th | Partiful")
        b = mention("thread:email:partiful", date="2026-08-23",
                    title="Capture The Flag 2 - Trojan War | Partiful")
        self.assertEqual(len(self._groups(a, b)), 2)

    def test_the_thresholds_come_from_the_config(self):
        """Both knobs shipped unread. A threshold nothing consults is indistinguishable
        from a threshold that does not exist, so the test asserts the wiring rather than
        the numbers."""
        a = mention("thread:a", date="2026-08-02", title="Beer hall")
        b = mention("thread:b", date="2026-08-02", title="Beer garden")
        strict = Config(home=Path("/tmp"))
        strict.same_event_poor_tokens = 2
        self.assertEqual(len(self._groups(a, b, cfg=strict)), 2)
        self.assertEqual(len(self._groups(a, b, cfg=Config(home=Path("/tmp")))), 1)


if __name__ == "__main__":
    unittest.main()
