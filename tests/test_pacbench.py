"""Stage 1 evaluation-contract checks: immutable inputs, collection chronology, hidden
expectations kept out of public data, reproducible grading, and correct marking of good,
missing, wrong, and contradictory answers. All free, local, and model-free.
"""

import dataclasses
import re
import unittest
from pathlib import Path

from pacbench import corpus
from pacbench.corpus import poker_move
from pacbench.grade import grade
from pacbench.observation import Observation, chronology_ok, delivery_order
from pacbench.rubric import Verdict

_PKG = Path(__file__).resolve().parents[1] / "pacbench"


def _where(life_id):
    life = next(l for l in corpus.all_lives() if l.id == life_id)
    return life, next(c for c in life.checkpoints if c.id.endswith("/where"))


class PACBenchIsMemcalFree(unittest.TestCase):
    def test_no_memcal_import_anywhere_in_the_package(self):
        offenders = []
        for path in _PKG.rglob("*.py"):
            for i, line in enumerate(path.read_text().splitlines(), 1):
                if re.match(r"\s*(import|from)\s+memcal(\.|\s|$)", line):
                    offenders.append(f"{path.name}:{i}")
        self.assertEqual(offenders, [], f"pacbench must not import memcal: {offenders}")


class ObservationsAreImmutablePublicData(unittest.TestCase):
    def test_frozen(self):
        obs = poker_move.lives()[0].observations[0]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            obs.text = "tampered"

    def test_public_payload_carries_no_expectation(self):
        obs = poker_move.lives()[0].observations[0]
        keys = set(obs.public())
        self.assertNotIn("expected", keys)
        self.assertNotIn("forbidden", keys)
        self.assertEqual(keys, {f.name for f in dataclasses.fields(Observation)})

    def test_question_hides_the_answer_key(self):
        _, where = _where("poker-move")
        prompt = where.public_prompt().lower()
        for leaked in ("jordan", "saturday", "8pm", "44 birch"):
            self.assertNotIn(leaked, prompt)

    def test_delivery_order_is_stable_and_nonmutating(self):
        obs = list(poker_move.lives()[0].observations)
        before = [o.source_id for o in obs]
        ordered = delivery_order(obs)
        self.assertEqual([o.source_id for o in obs], before)          # input untouched
        self.assertEqual([o.collected_at for o in ordered],
                         sorted(o.collected_at for o in ordered))


class CollectionChronologyHolds(unittest.TestCase):
    def test_every_life_is_chronologically_sound(self):
        for life in corpus.all_lives():
            self.assertEqual(chronology_ok(list(life.observations)), [], life.id)


class GradingIsReproducible(unittest.TestCase):
    def test_same_answer_same_verdicts(self):
        _, where = _where("poker-move")
        answer = "Poker's at Jordan's, 44 Birch Ave, this Saturday at 8pm."
        self.assertEqual(grade(where, answer).to_dict(), grade(where, answer).to_dict())


class GraderMarksTheFourOutcomes(unittest.TestCase):
    def setUp(self):
        _, self.where = _where("poker-move")

    def _verdicts(self, answer):
        return {gs.id: gs.verdict for gs in grade(self.where, answer).slots}

    def test_good_answer_is_all_correct_and_complete(self):
        g = grade(self.where, "Poker's at Jordan's, 44 Birch Ave, this Saturday at 8pm.")
        self.assertEqual((g.correct, g.missed, g.misrepresented), (3, 0, 0))
        self.assertTrue(g.complete_plan)

    def test_missing_location_is_missed_not_wrong(self):
        v = self._verdicts("It's this Saturday at 8pm.")
        self.assertEqual(v["loc"], Verdict.MISSED)
        self.assertEqual(v["day"], Verdict.CORRECT)
        self.assertFalse(grade(self.where, "It's this Saturday at 8pm.").complete_plan)

    def test_stale_answer_is_misrepresented(self):
        g = grade(self.where, "Poker is Friday at 7pm at Sam's, 12 Ash Street.")
        self.assertEqual(g.misrepresented, 3)
        self.assertFalse(g.complete_plan)

    def test_asserting_both_as_current_loses_the_slot(self):
        # A genuine double-booking claim, no history framing.
        v = self._verdicts("Poker is Friday and Saturday, at Sam's and Jordan's.")
        self.assertEqual(v["loc"], Verdict.MISREPRESENTED)
        self.assertEqual(v["day"], Verdict.MISREPRESENTED)

    def test_history_aware_answer_is_still_correct(self):
        # Keeping the old plan as history must not be punished as a stale assertion.
        g = grade(self.where,
                  "Poker moved to Saturday 8pm at Jordan's; it used to be Friday 7pm at Sam's.")
        self.assertEqual((g.correct, g.misrepresented), (3, 0))
        self.assertTrue(g.complete_plan)


class HistoricalAndControlProbes(unittest.TestCase):
    def test_old_address_recovers_history(self):
        life = next(l for l in corpus.all_lives() if l.id == "poker-move")
        hist = next(c for c in life.checkpoints if c.id.endswith("/old-address"))
        g = grade(hist, "It was originally at Sam's, 12 Ash Street.")
        self.assertEqual(g.correct, 1)

    def test_control_answers_the_unchanged_plan(self):
        _, where = _where("poker-unchanged")
        g = grade(where, "Poker is Friday at 7pm at Sam's, 12 Ash Street.")
        self.assertEqual((g.correct, g.misrepresented), (3, 0))
        self.assertTrue(g.complete_plan)


class OpenEndedScoresOptionsNotProse(unittest.TestCase):
    def setUp(self):
        life = next(l for l in corpus.all_lives() if l.id == "weekend-plans")
        self.fun = next(c for c in life.checkpoints if c.id.endswith("/anything-fun"))

    def test_full_recall_no_traps(self):
        g = grade(self.fun, "You've got Members' Day at the Foundry Saturday, "
                            "dinner with Dana, and family visiting Sunday.")
        self.assertEqual((g.options_found, g.options_total), (3, 3))
        self.assertEqual(g.suggested_forbidden, 0)
        self.assertEqual((g.recall, g.precision), (1.0, 1.0))

    def test_missing_an_option_lowers_recall(self):
        g = grade(self.fun, "There's Members' Day at the Foundry, and dinner with Dana.")
        self.assertEqual(g.options_found, 2)
        self.assertAlmostEqual(g.recall, 2 / 3)
        self.assertEqual(g.precision, 1.0)

    def test_suggesting_a_forbidden_option_lowers_precision(self):
        g = grade(self.fun, "You could hit the museum gala, the Foundry members' day, "
                            "dinner with Dana, or family Sunday.")
        self.assertEqual(g.suggested_forbidden, 1)
        self.assertEqual(g.recall, 1.0)
        self.assertAlmostEqual(g.precision, 3 / 4)


class AbstentionIsScoredSeparately(unittest.TestCase):
    def setUp(self):
        life = next(l for l in corpus.all_lives() if l.id == "weekend-plans")
        self.dinner = next(c for c in life.checkpoints if c.id.endswith("/dinner-day"))

    def test_hedging_is_correct(self):
        g = grade(self.dinner,
                  "Dana hasn't confirmed which day yet — Saturday or Sunday. Want me to ask?")
        self.assertEqual(g.abstention, 1.0)
        self.assertEqual(g.slots[0].verdict, Verdict.CORRECT)

    def test_guessing_a_day_is_misrepresented(self):
        g = grade(self.dinner, "Dinner with Dana is on Sunday.")
        self.assertEqual(g.abstention, 0.0)
        self.assertEqual(g.slots[0].verdict, Verdict.MISREPRESENTED)


class ImplicitControlIsAnsweredByRetrieval(unittest.TestCase):
    def test_pointed_question_resolves(self):
        life = next(l for l in corpus.all_lives() if l.id == "dentist-friday")
        when = life.checkpoints[0]
        g = grade(when, "Your dental cleaning is Friday at 2:30pm.")
        self.assertEqual((g.correct, g.misrepresented), (1, 0))


class RendererPreservesTheKey(unittest.TestCase):
    def setUp(self):
        from pacbench.render import Beat
        self.beat = Beat(channel="groupme", sender="Jordan Vance",
                         draft="moving poker to Saturday 8pm at 44 Birch Ave",
                         must_include=("Saturday", "8pm", "44 Birch"))

    def test_no_generator_is_the_draft_verbatim(self):
        from pacbench.render import render
        self.assertEqual(render(self.beat), self.beat.draft)

    def test_entails_reports_dropped_tokens(self):
        from pacbench.render import entails
        self.assertEqual(entails("poker is Saturday at 8pm", self.beat), ["44 Birch"])
        self.assertEqual(entails("Saturday 8pm at 44 Birch Ave, be there", self.beat), [])

    def test_render_refuses_a_rewrite_that_drops_a_fact(self):
        from pacbench.render import RenderError, render
        with self.assertRaises(RenderError):
            render(self.beat, generate=lambda _p: "poker moved to Saturday night, come by")

    def test_render_accepts_a_faithful_rewrite(self):
        from pacbench.render import render
        out = render(self.beat,
                     generate=lambda _p: "heads up — poker's Saturday 8pm now, 44 Birch Ave")
        self.assertIn("44 Birch", out)


if __name__ == "__main__":
    unittest.main()
