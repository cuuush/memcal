"""The gap-finding harness is itself deterministic infrastructure."""

from __future__ import annotations

import ast
import io
import re
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from datetime import date
from pathlib import Path

from memcal import db, events, todos
from memcal.sources import whatsapp
from memcal.config import Config
from memcal.dream import sweep
from tests.scenarios import build, collision, expect, probes, skeleton
from tools import audit_questions, benchmark_temporal


class TestBenchmarkStructure(unittest.TestCase):
    def test_model_progress_names_real_stage_and_completed_fraction(self):
        stream = io.StringIO()
        status = benchmark_temporal.BenchmarkStatus(
            True, "core · provider codex · model gpt-5.6-luna",
            every=60, stream=stream)
        status.start()
        status.progress(1, "stage", {
            "stage": "propose", "state": "running", "note": "reading 12 bundles"})
        status.progress(1, "propose_wave", {
            "requests": 2, "bundles": 12, "kind": "main"})
        status.progress(1, "propose_request", {
            "index": 1, "bundles": 6, "ok": True, "done": 6, "total": 12})
        status.close()
        text = stream.getvalue()
        self.assertIn("provider codex · model gpt-5.6-luna", text)
        self.assertIn("propose running · reading 12 bundles", text)
        self.assertIn("2 request(s) · 12 bundle(s)", text)
        self.assertIn("propose 6/12 bundles · request 1 finished", text)

    def test_a_quiet_model_call_emits_a_heartbeat(self):
        class OneBeat:
            calls = 0

            def wait(self, _seconds):
                self.calls += 1
                return self.calls > 1

        stream = io.StringIO()
        status = benchmark_temporal.BenchmarkStatus(
            True, "day 1/4 · propose dispatch", every=15, stream=stream)
        status._stop = OneBeat()
        status.last_event = benchmark_temporal.time.monotonic() - 20
        status._heartbeat()
        text = stream.getvalue()
        self.assertIn("still running · day 1/4 · propose dispatch", text)
        self.assertIn("since the last model event", text)

    def test_benchmark_layer_names_are_clear_and_old_names_remain_aliases(self):
        self.assertEqual(benchmark_temporal.canonical_layer("integration"), "integration")
        self.assertEqual(benchmark_temporal.canonical_layer("model"), "model")
        self.assertEqual(benchmark_temporal.canonical_layer("replay"), "integration")
        self.assertEqual(benchmark_temporal.canonical_layer("live"), "model")

    def test_check_ids_are_unique(self):
        ids = [check.id for check in expect.CHECKS]
        self.assertEqual(len(ids), len(set(ids)))

    def test_fixture_record_ids_are_unique_and_all_have_text(self):
        ids = [row["id"] for row in skeleton.SIGNAL]
        self.assertEqual(len(ids), len(set(ids)))
        expanded = build.expand()
        present = {row["id"] for row in expanded}
        self.assertTrue(set(ids) <= present)

    def test_frontier_is_not_an_xfail_alias(self):
        frontier = [check for check in expect.CHECKS if check.frontier]
        self.assertTrue(frontier)
        self.assertTrue(all(not check.soft for check in frontier))

    def test_saved_response_suite_has_the_contract_seams(self):
        rows = probes.contract_checks()
        ids = {row["id"] for row in rows}
        self.assertIn("contract.truncated-json-not-partial", ids)
        self.assertIn("contract.unknown-never-guessed", ids)
        self.assertIn("contract.missing-reviewed-diagnosed", ids)
        self.assertTrue(all({"id", "challenge", "ok", "frontier"} <= set(row)
                            for row in rows))

    def test_boundary_suite_runs_to_findings_in_an_isolated_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "boundaries"
            first = benchmark_temporal.probe_run("boundaries", home)
            second = benchmark_temporal.probe_run("boundaries", home)
        rows = first["results"]
        ids = {row["id"] for row in rows}
        self.assertIn("boundary.cross-year-update", ids)
        self.assertIn("boundary.old-evidence-cannot-overwrite", ids)
        self.assertIn("boundary.nonexistent-local-time", ids)
        self.assertIn("transaction.resolved-disappears", ids)
        self.assertEqual([(row["id"], row["ok"]) for row in first["results"]],
                         [(row["id"], row["ok"]) for row in second["results"]])

    def test_clock_suite_runs_to_findings_in_an_isolated_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "clock"
            first = benchmark_temporal.probe_run("clock", home)
            second = benchmark_temporal.probe_run("clock", home)
        ids = {row["id"] for row in first["results"]}
        self.assertIn("clock.question-dies-with-its-subject", ids)
        self.assertIn("clock.obligation-is-not-a-question", ids)
        self.assertIn("clock.a-stale-link-is-re-scored", ids)
        self.assertEqual([(row["id"], row["ok"]) for row in first["results"]],
                         [(row["id"], row["ok"]) for row in second["results"]])

    def test_repeat_report_names_unstable_checks(self):
        base = {
            "usage": {"cost": 0.1},
            "results": [{"id": "x", "soft": False, "ok": True, "note": "ok"}],
        }
        other = {
            "usage": {"cost": 0.2},
            "results": [{"id": "x", "soft": False, "ok": False, "note": "miss"}],
        }
        text = benchmark_temporal.repeat_report([base, other])
        self.assertIn("50%", text)
        self.assertIn("x", text)
        self.assertIn("$0.3000", text)


class TestTheAnswerKeyCouldNotSeeAnsweredQuestions(unittest.TestCase):
    """`Ctx.questions` hardcoded `status = 'open'`, so no check could grade the second
    half of a question's life. Every fix to the question lifecycle was unprovable and
    every regression in it silent."""

    def _store(self, tmp):
        cfg = Config(home=Path(tmp) / "lifecycle")
        cfg.ensure_dirs()
        conn = db.open_db(cfg.db_path)
        db.set_today(date(2026, 8, 5))
        # Registered rather than released at the end of each test body: `set_today` is
        # process-global, so the first assertion that fails takes the release with it
        # and every test that runs afterwards lives on 2026-08-05.
        self.addCleanup(db.set_today, None)
        return conn, cfg

    def _ask(self, conn, key, text, *, status="open", about_event=None):
        conn.execute(
            "INSERT INTO questions(key, text, about_event, status, created_at)"
            " VALUES(?,?,?,?,?)", (key, text, about_event, status, db.now()))
        conn.commit()

    def test_a_dropped_question_is_visible_to_the_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn, cfg = self._store(tmp)
            self.addCleanup(conn.close)
            self._ask(conn, "q:gone", "Are you going to the gym?", status="dropped")
            ctx = expect.Ctx(conn, cfg)
            self.assertEqual(ctx.questions(r"gym"), [])
            self.assertEqual(len(ctx.questions(r"gym", status=None)), 1)
            self.assertEqual(ctx.question(r"gym")["status"], "dropped")
            self.assertTrue(expect.question_status(r"gym", "dropped")(ctx)[0])
            self.assertFalse(expect.question_status(r"gym", "asked")(ctx)[0])
            self.assertFalse(expect.question_status(r"gym", "absent")(ctx)[0])
            self.assertTrue(expect.question_status(r"never asked", "absent")(ctx)[0])

    def test_a_question_linked_to_the_wrong_row_is_not_a_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn, cfg = self._store(tmp)
            self.addCleanup(conn.close)
            wrong, _ = events.upsert(conn, {
                "title": "Alumni meeting", "date": "2026-08-11", "status": "confirmed"})
            self._ask(conn, "q:board", "Which day is the board game night?",
                      about_event=wrong.id)
            ctx = expect.Ctx(conn, cfg)
            check = expect.question_links_to(r"board game", r"board game")
            self.assertFalse(check(ctx)[0])
            self.assertTrue(expect.question_links_to(r"board game", r"Alumni")(ctx)[0])
            self.assertFalse(expect.question_links_to(r"board game", None)(ctx)[0])

    def test_a_question_its_own_row_answers_is_caught(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn, cfg = self._store(tmp)
            self.addCleanup(conn.close)
            row, _ = events.upsert(conn, {
                "title": "Tutoring", "date": "2026-08-11", "status": "confirmed"})
            self._ask(conn, "q:tutoring", "When is tutoring this week?",
                      about_event=row.id)
            ctx = expect.Ctx(conn, cfg)
            ok, note = expect.no_question_answered_by_a_row(ctx)
            self.assertFalse(ok)
            self.assertIn("date", note)
            # ...and a question about a field the row does not carry is fair to ask.
            conn.execute("UPDATE questions SET text = ? WHERE key = 'q:tutoring'",
                         ("What time is tutoring this week?",))
            conn.commit()
            self.assertTrue(expect.no_question_answered_by_a_row(ctx)[0])


#: Checks that are green against a store containing nothing at all. Every one is a
#: "must not" — a decoy that has to stay absent — and for those, absent-because-empty
#: and absent-because-correct really are the same outcome. They are safe *only* because
#: something else asserts the corresponding row exists.
#:
#: The list is pinned rather than counted so that adding a vacuously-green check is a
#: decision somebody made on purpose. Two got in without one: `evidence.dates-are-
#: supported` and `evidence.guests-are-named` awarded themselves a pass when no row had
#: any evidence to check — green at exactly the moment extraction had failed hardest.
VACUOUS_ON_AN_EMPTY_STORE = {
    # "no handle in the brief is a dead end" is trivially true of a brief with no
    # handles, and that is the honest shape of the check rather than a weakness in it:
    # it is an integrity check over whatever was extracted, not a claim that
    # anything was. `brief.every-row-can-be-opened` is the paired check that does fail
    # on an empty store, because an empty brief still prints a row saying so.
    'brief.every-handle-opens', 'brief.every-handle-opens-at-the-end',
    'bailey.not-mom', 'beergarden.not-next-week', 'brief.later-is-visible',
    'brief.no-jargon', 'brief.no-keys', 'brunch.reads-plainly',
    'comet.no-species-claim', 'comet.not-a-horse', 'd1.car-not-standing',
    'd1.junk-aws', 'd1.junk-chase', 'd1.no-vendor-event', 'd1.work-no-row',
    'vendor.no-todo', 'vendor.nothing-at-all',
    'feed.stays-out-of-the-brief',
    'jordan.not-eastwood', 'junk.no-affection', 'junk.no-amazon',
    'junk.no-aws', 'junk.no-home-logistics', 'junk.no-opinion',
    'junk.no-sale', 'mom.not-bailey', 'movie.not-live',
    'pages.no-bulk-sender', 'pages.no-group-name', 'pages.no-raw-handle',
    'pages.no-shortcode', 'pages.none-empty',
    # 54. A numeral is not a person, no page for it, no question nobody can answer.
    # Paired with `nameless.plan-survives-its-proposer`, which is a real positive and
    # does fail on an empty store — so the beat cannot go green on nothing.
    'nameless.no-page-for-a-number', 'nameless.no-unanswerable-question',
    # 55. Nothing may be created for GroupMe itself. The positive checks separately
    # require authored text inside an edit notice to survive.
    'platform.no-row-for-the-app', 'platform.no-page-for-the-app',
    'platform.not-in-the-name-queue',
    'poker.no-friday', 'state.no-question-a-row-answers',
    'state.no-self-answerable-question', 'state.only-declared-aliases',
    'state.only-declared-events', 'state.only-declared-pages',
    'state.only-declared-questions', 'state.only-declared-standing',
    'state.only-declared-todos', 'state.only-declared-wiki-values',
    'state.only-declared-events-at-the-end',
    'state.only-declared-questions-at-the-end',
    'state.only-declared-todos-at-the-end',
    'tickets.no-batman-row', 'venmo.no-event', 'venmo.not-in-brief',
    'voice.no-bookkeeping', 'voice.no-identifiers', 'voice.second-person',
    'wiki.every-fact-has-source',
}


class TestAGreenCheckOnAnEmptyStoreProvesNothing(unittest.TestCase):
    """"No such row" is a correct outcome and no evidence at all — testing.md has said
    so for months and nothing measured it. A check that passes on an empty store hands
    out a point in the one case worth catching loudest: a run where the pass wrote
    nothing whatsoever."""

    def _grade_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(home=Path(tmp) / "empty")
            cfg.ensure_dirs()
            conn = db.open_db(cfg.db_path)
            db.set_today(date(2026, 8, 6))
            self.addCleanup(db.set_today, None)
            ctx = expect.Ctx(conn, cfg)
            green, exploded = set(), []
            for check in expect.CHECKS:
                try:
                    ok, _note = check.fn(ctx)
                except Exception as exc:                      # noqa: BLE001
                    exploded.append(f"{check.id}: {type(exc).__name__}: {exc}")
                    continue
                if ok:
                    green.add(check.id)
            conn.close()
        return green, exploded

    def test_no_check_raises_on_an_empty_store(self):
        """`grade` turns a raise into a failure, so this would score as a red check
        rather than as the broken check it is."""
        _green, exploded = self._grade_empty()
        self.assertEqual(exploded, [])

    def test_the_vacuously_green_set_is_exactly_what_was_signed_off(self):
        green, _exploded = self._grade_empty()
        self.assertEqual(
            green - VACUOUS_ON_AN_EMPTY_STORE, set(),
            "new check(s) pass on a store containing nothing. Either assert the row "
            "exists too, or add the id to VACUOUS_ON_AN_EMPTY_STORE on purpose.")
        self.assertEqual(
            VACUOUS_ON_AN_EMPTY_STORE - green, set(),
            "check(s) no longer vacuous — good; drop them from the list.")

    def test_most_of_the_key_still_needs_a_real_store(self):
        """A blunt floor, so the ratio cannot drift check by check."""
        green, _exploded = self._grade_empty()
        self.assertLess(len(green), len(expect.CHECKS) * 0.4)


#: Probe checks whose verdict is a pure negative — green whenever the thing they forbid
#: is absent, and so green on a run that built nothing at all. This is the same hazard
#: `VACUOUS_ON_AN_EMPTY_STORE` pins for `expect.CHECKS`, but the probe suites cannot be
#: re-graded against an empty store: each suite *is* its own fixture, so an empty home
#: only makes it seed itself again, and neutering the writers kills four of the six at
#: their first check instead of grading them.
#:
#: So each one names the check in the same suite that goes red when nothing was built —
#: which is the argument `probes.py` already makes in prose beside every one of them,
#: as "the counterweight" or "the decoy". Pinned here, that argument is asserted rather
#: than believed: silence the whole feature and the counterweight fails.
NEGATIVE_PROBE_CHECKS = {
    "contract.truncated-json-not-partial": ("contract.fenced-empty",),
    "brief.no-unregistered-stream": ("brief.registered-stream-still-warns",),
    "transaction.no-calendar-ledger": ("transaction.closes-matching-todo",),
    # Two counterweights: one says the transaction was processed at all, the other says
    # `brief.render` in this suite returns something to search.
    "transaction.resolved-disappears": ("transaction.closes-matching-todo",
                                        "brief.registered-stream-still-warns"),
    "collect.a-clean-pass-stays-clean": ("collect.a-failed-source-fails-the-pass",),
    "identity.a-first-name-guess-is-never-applied":
        ("identity.a-platform-name-is-taken-verbatim",),
    "identity.an-announcement-channel-is-not-a-person":
        ("identity.a-platform-name-is-taken-verbatim",),
    "clock.generic-word-is-not-a-subject": ("clock.a-named-row-is-still-found",),
    "clock.out-is-not-a-subject": ("clock.a-named-row-is-still-found",),
    "clock.no-question-about-a-day-the user-was-elsewhere":
        ("clock.same-place-is-not-a-conflict",),
    "series.one-occasion-is-not-a-series": ("series.a-stated-place-wins",),
    "schedule.a-known-time-is-not-asked-about":
        ("schedule.an-unknown-time-is-asked-about",),
    "schedule.the-excepted-day-is-not-recreated": ("schedule.the-past-is-not-retro-dated",),
    "schedule.an-ended-series-stops-projecting":
        ("schedule.a-projection-follows-its-rule",),
    "hermes.only-user-crosses-boundary": ("hermes.turn-sync-deduplicates",),
    "brief.a-quiet-snapshot-is-not-stale": ("brief.stale-stream-named",),
}

#: The six suites `benchmark_temporal.probe_run` dispatches. Named rather than derived
#: so that adding a seventh and forgetting this file is a failure, not a silent gap.
PROBE_SUITES = ("contract", "boundaries", "hermes", "clock", "schedule", "collection")


class TestEveryNegativeProbeCheckHasACounterweight(unittest.TestCase):
    """`TestAGreenCheckOnAnEmptyStoreProvesNothing` covers `expect.CHECKS` and nothing
    else, so roughly a quarter of the graded total — 81 hard checks across six probe
    suites — was held to no vacuity standard at all. A negative check is where one
    lands: "findmy is not in the brief" is true of a brief with no lines in it."""

    @classmethod
    def setUpClass(cls):
        cls.rows = {}
        with tempfile.TemporaryDirectory() as tmp:
            for name in PROBE_SUITES:
                run = benchmark_temporal.probe_run(name, Path(tmp) / name)
                for row in run["results"]:
                    cls.rows[row["id"]] = row
        db.set_today(None)

    def _negative(self, node) -> bool:
        """`not x`, `x not in y`, `x is None`, `x == 0`, `x == []`, and ands of those."""
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return True
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
            return all(self._negative(value) for value in node.values)
        if isinstance(node, ast.Compare):
            # `is not None` is the opposite claim — that something was built — so it
            # is a positive, and belongs among the counterweights rather than here.
            if any(isinstance(op, (ast.NotIn, ast.NotEq)) for op in node.ops):
                return True
            return (all(isinstance(op, (ast.Eq, ast.Is)) for op in node.ops)
                    and all(self._empty(c) for c in node.comparators))
        return False

    def _empty(self, node) -> bool:
        if isinstance(node, ast.Constant):
            return node.value in (0, "", None) or node.value is False
        return (isinstance(node, (ast.List, ast.Tuple, ast.Set))
                and not node.elts) or (isinstance(node, ast.Dict) and not node.keys)

    def _declared(self):
        """Every graded `result(...)` in probes.py, as (id, is-negative)."""
        source = Path(probes.__file__).read_text(encoding="utf-8")
        for call in [c for c in ast.walk(ast.parse(source)) if isinstance(c, ast.Call)
                     and ast.unparse(c.func) == "result" and len(c.args) >= 3]:
            flags = {k.arg for k in call.keywords
                     if isinstance(k.value, ast.Constant) and k.value.value is True}
            if flags & {"frontier", "soft"}:
                continue                    # neither awards a point, so neither can be
            check_id = ast.literal_eval(call.args[0])
            yield check_id, self._negative(call.args[2])

    def test_every_negative_check_is_pinned_with_a_counterweight(self):
        found = {check_id for check_id, negative in self._declared() if negative}
        self.assertEqual(
            sorted(found - set(NEGATIVE_PROBE_CHECKS)), [],
            "new probe check(s) pass whenever the thing they forbid is absent. Name the "
            "check in the same suite that goes red on an empty run, in "
            "NEGATIVE_PROBE_CHECKS.")
        self.assertEqual(
            sorted(set(NEGATIVE_PROBE_CHECKS) - found), [],
            "pinned check(s) are no longer a bare negative — good; drop them.")

    def test_every_counterweight_is_green_when_its_suite_runs(self):
        red = self._counterweights_that_failed(self.rows)
        self.assertEqual(red, [],
                         "the counterweight failed, so its negative partner is green "
                         "for a reason nobody has checked")

    def _counterweights_that_failed(self, rows):
        failed = []
        for check_id, partners in NEGATIVE_PROBE_CHECKS.items():
            if check_id not in rows:
                continue        # its suite did not run here — Hermes needs a checkout
            for partner in partners:
                if partner not in rows:
                    failed.append(f"{check_id}: counterweight {partner} was not run")
                elif not rows[partner]["ok"]:
                    failed.append(f"{check_id}: counterweight {partner} is red")
        return failed

    def test_a_counterweight_that_goes_red_is_reported(self):
        """The proof it can fail, without breaking a real suite to get it."""
        broken = {name: dict(row) for name, row in self.rows.items()}
        broken["brief.stale-stream-named"]["ok"] = False
        self.assertEqual(
            self._counterweights_that_failed(broken),
            ["brief.a-quiet-snapshot-is-not-stale: counterweight "
             "brief.stale-stream-named is red"])

    def test_the_scan_has_something_to_scan(self):
        """`result(` renamed, or a suite dropped, and every assertion above is empty."""
        declared = list(self._declared())
        self.assertGreater(len(declared), 70)
        self.assertGreater(sum(1 for _id, negative in declared if negative), 10)
        graded = [row for row in self.rows.values()
                  if not row["frontier"] and not row["soft"]]
        self.assertGreater(len(graded), 70)


class TestOneProbeSuiteMayNotScoreTheNextOne(unittest.TestCase):
    """The Hermes plugin hot-reloads by dropping every `memcal.*` module out of
    `sys.modules`. Whatever ran next imported a second copy of `memcal.db`, whose
    pinned test clock was unset, so it read the wall clock — and three question-linking
    checks in `clock` went red. A wrong score, silently. `--suite all` escapes it only
    because `hermes` happens to be last in `valid_suites`."""

    def _clock_reds(self, home):
        run = benchmark_temporal.probe_run("clock", home)
        return sorted(row["id"] for row in run["results"]
                      if not row["ok"] and not row["frontier"] and not row["soft"])

    def test_the_module_table_survives_the_hermes_suite(self):
        if probes._load_hermes_provider() is None:
            self.skipTest("Hermes checkout is not installed on this machine")
        with tempfile.TemporaryDirectory() as tmp:
            probes.hermes_checks(Path(tmp) / "hermes")
        self.assertIs(probes.db, sys.modules["memcal.db"],
                      "a later suite would import a second memcal with its own clock")

    def test_the_clock_suite_scores_the_same_before_and_after_hermes(self):
        """The behaviour rather than the mechanism, and the shape any future suite that
        reaches for the module table would be caught by too."""
        if probes._load_hermes_provider() is None:
            self.skipTest("Hermes checkout is not installed on this machine")
        with tempfile.TemporaryDirectory() as tmp:
            alone = self._clock_reds(Path(tmp) / "clock-alone")
            probes.hermes_checks(Path(tmp) / "hermes")
            after = self._clock_reds(Path(tmp) / "clock-after")
        self.assertEqual(after, alone)


class TestChallengeNumbersStoppedMappingOntoBeats(unittest.TestCase):
    """33, 34 and 35 were each used by two different challenge strings, so the report's
    buckets no longer lined up with BEATS.md's unique 1–37."""

    def test_each_challenge_number_names_one_challenge(self):
        seen: dict[str, set[str]] = {}
        for check in expect.CHECKS:
            number = check.challenge.split()[0]
            if number.isdigit():
                seen.setdefault(number, set()).add(check.challenge)
        clashes = {n: sorted(v) for n, v in seen.items() if len(v) > 1}
        self.assertEqual(clashes, {})

    def test_every_numbered_challenge_has_a_beat(self):
        beats = (Path(__file__).resolve().parent / "scenarios" / "BEATS.md").read_text()
        numbered = {check.challenge.split()[0] for check in expect.CHECKS
                    if check.challenge.split()[0].isdigit()}
        missing = sorted(n for n in numbered if f"\n### {n}. " not in beats)
        self.assertEqual(missing, [])


#: `expect.py` helpers whose first string argument picks an **event row by its title**.
#: A title is written by a model, so every one of these is a fuzzy lookup over prose
#: somebody else worded. Negative helpers (`no_row`, `brief_lacks`, `slot_never_says`)
#: are deliberately absent: their words are supposed to be missing from the store, and
#: several of them name schema vocabulary that appears in no message.
TITLE_SELECTORS = frozenset({
    "one_row", "row_on", "field_is", "changed", "count_rows",
    "source_says", "event_current_source_first", "written_by_at_least",
})

#: Selectors allowed to pin a title's shape, and why. A waiver is not "this one is
#: awkward to fix" — it is "the corpus puts two rows the same words could name on one
#: day, and the shape is the only thing that separates them".
SHAPE_WAIVERS = {
    # Challenge 8's row and challenge 37's Superman row are both on 2026-08-11 and the
    # word "movie" is in both titles, so `on=` cannot separate them. The anchor here
    # excludes the *other* row by the name only it carries, which is the opposite of
    # pinning this one's wording: "Movie", "Movie with Riley" and "Movie night with
    # Riley" all still match.
    r"^(?!.*superman).*\bmovie\b": "excludes the Superman row that shares Aug 11",
    r"^(?!.*superman).*\bmovie\b|theater|cinema": "as above, plus the cancelled-row words",
}


def _corpus_text() -> str:
    """Every word the run can legitimately produce a title out of.

    The built fixtures are what the model reads. `skeleton.ACTIONS` is the other half:
    those rows are written into the store by `memcal.live` before any model sees the
    day, so a check may name their titles without any message having said them.
    """
    import json as _json

    parts = [_json.dumps(skeleton.ACTIONS)]
    root = Path(__file__).resolve().parent / "scenarios" / "fixtures"
    for path in sorted(root.rglob("*")):
        if path.is_file():
            parts.append(path.read_bytes().decode("utf-8", "ignore"))
    return "\n".join(parts).casefold()


def _title_selectors() -> list[tuple[str, str]]:
    """(check id, pattern) for every event-title lookup in the key.

    Parsed rather than introspected because the patterns are closed over inside the
    lambdas `Check` holds, exactly as `TestEveryTestInThisDirectoryActuallyRuns` parses
    files for a fact the imported module cannot be asked for.
    """
    source = (Path(__file__).resolve().parent / "scenarios" / "expect.py")
    tree = ast.parse(source.read_text())
    found: list[tuple[str, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "Check" and node.args
                and isinstance(node.args[0], ast.Constant)):
            continue
        check_id = node.args[0].value
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            # A bare lambda over `Ctx` is the escape hatch from the helpers, and two
            # `^poker at` lookups were sitting in one.
            if isinstance(inner.func, ast.Attribute) and inner.func.attr in ("one", "rows"):
                if inner.args and isinstance(inner.args[0], ast.Constant) \
                        and isinstance(inner.args[0].value, str):
                    found.append((check_id, inner.args[0].value))
                continue
            if not isinstance(inner.func, ast.Name):
                continue
            if inner.func.id in TITLE_SELECTORS and inner.args \
                    and isinstance(inner.args[0], ast.Constant) \
                    and isinstance(inner.args[0].value, str):
                found.append((check_id, inner.args[0].value))
            elif inner.func.id == "only_expected_events" and inner.args:
                for spec in getattr(inner.args[0], "elts", []):
                    first = getattr(spec, "elts", [None])[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        found.append((check_id, first.value))
    return found


class TestTheKeyAskedForAWordingRatherThanAnOccasion(unittest.TestCase):
    """Grade stored occasions rather than exact model wording."""

    def test_every_title_selector_uses_words_the_corpus_says(self):
        corpus = _corpus_text()
        missing = []
        for check_id, pattern in _title_selectors():
            branches = [b for b in re.split(r"(?<!\\)\|", pattern) if b.strip()]
            for branch in branches:
                # A lookaround says what must be *absent*, and `\b`/`\d`/`\s` are
                # syntax; neither is a word the traffic owes anybody.
                branch = re.sub(r"\(\?[!=<][^)]*\)", " ", branch)
                branch = re.sub(r"\\[a-zA-Z]", " ", branch)
                words = re.findall(r"[a-z][a-z']{2,}", branch.casefold())
                if not words or all(word in corpus for word in words):
                    break
            else:
                missing.append(f"{check_id}: {pattern!r}")
        self.assertEqual(
            missing, [],
            "title selector(s) requiring words no fixture contains. The key cannot ask "
            "for a word the traffic never says — fix the prose or the pattern.")

    def test_no_title_selector_requires_two_words_in_order(self):
        # A preposition between two words is a *description* of an occasion — "breakfast
        # at elements" — where a bare multi-word run is usually its name: "beer garden",
        # "capture the flag", "neon garden". Only the first kind gets reworded. This
        # caught three more checks after the first two rules had run, on a trial where
        # luna wrote "Breakfast before Elements soundcheck" and the row was otherwise
        # perfect.
        joined = r"[a-z']\s+(?:at|with|for|before|after|from|to|in|on|of|and)\s+[a-z']"
        rigid = []
        for check_id, pattern in _title_selectors():
            if pattern in SHAPE_WAIVERS:
                continue
            if re.search(r"\w(?:\\?\.)?\.\*.*?\w", pattern) or pattern.startswith("^") \
                    or re.search(joined, pattern, re.I):
                rigid.append(f"{check_id}: {pattern!r}")
        self.assertEqual(
            rigid, [],
            "title selector(s) pinning the shape of a model-written title. Discriminate "
            "by date (`on=`) or by a distinctive word, or add a waiver saying which two "
            "rows share a day and need the anchor.")


class TestTheAuditKnowsWhichAskerCitesState(unittest.TestCase):
    """`audit_questions` must not read the reconciler's own convention as a defect.

    `sweep.reconcile_backward_window` stamps with no `archive_ids` on purpose — it reads
    state, not the archive — so every question it writes has zero `evidence` rows. Judged
    by the general rule they read as unjudgeable forever, and an instrument that cries
    wolf on a healthy row teaches you to skim its output.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)
        self.addCleanup(self.conn.close)
        db.set_today(date(2026, 8, 15))
        self.addCleanup(db.set_today, None)

    def question(self, key: str):
        return self.conn.execute(
            "SELECT id, key, text, written_by, about_event FROM questions WHERE key = ?",
            (key,)).fetchone()

    def test_a_reconcile_question_with_no_evidence_reads_as_expected(self):
        event, _ = events.upsert(
            self.conn, {"date": "2026-08-14", "title": "Play Half-Life 2",
                        "status": "mentioned"}, written_by="dream:nightly")
        self.assertTrue(sweep.reconcile_backward_window(self.conn, self.cfg))
        row = self.question(f"q:resolve:{event.key}")
        self.assertEqual(row["written_by"], "reconcile")
        bundle = audit_questions.bundle_for(self.conn, row["key"])
        self.assertEqual(bundle.items, [])

        verdict = audit_questions.verdict_for(row, bundle)
        self.assertTrue(verdict.startswith("expected:"), verdict)
        self.assertIn(f"event {row['about_event']}", verdict)
        self.assertNotIn("cannot judge", verdict)

    def test_any_other_asker_with_no_evidence_still_cannot_be_judged(self):
        key = todos.ask(self.conn, "Where is the party?", written_by="sweep")
        row = self.question(key)
        verdict = audit_questions.verdict_for(
            row, audit_questions.bundle_for(self.conn, key))
        self.assertEqual(verdict, "no evidence recorded — cannot judge")


class TestQuestionExpiryDateCoverage(unittest.TestCase):
    """Report how open questions acquire dates used by expiry."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)
        self.addCleanup(self.conn.close)
        db.set_today(date(2026, 8, 20))
        self.addCleanup(db.set_today, None)

    def test_each_arm_of_expiry_is_counted_separately(self):
        event, _ = events.upsert(self.conn, {"title": "Poker at Jordan's",
                                             "date": "2026-08-22"}, written_by="cli")
        todos.ask(self.conn, "What time is poker?", about_event=event.id,
                  written_by="dream:nightly")
        todos.ask(self.conn, "What time is the League game on Sunday, August 23?",
                  written_by="dream:nightly")
        todos.ask(self.conn, "Which Casey runs the game?", written_by="dream:nightly")
        cover = audit_questions.date_coverage(self.conn)
        self.assertEqual((cover["linked"], cover["dated"], cover["undated"]), (1, 1, 1))

    def test_alarm_fires_when_no_unlinked_question_has_a_date(self):
        todos.ask(self.conn, "Which Casey runs the game?", written_by="dream:nightly")
        report = audit_questions.report_coverage(
            audit_questions.date_coverage(self.conn))
        self.assertTrue(any("ALARM" in line for line in report), report)
        todos.ask(self.conn, "Is dinner still on August 24?", written_by="dream:nightly")
        self.assertFalse(any("ALARM" in line for line in audit_questions.report_coverage(
            audit_questions.date_coverage(self.conn))), "one dated question is enough")

    def test_bare_weekday_is_reported_as_forward_only(self):
        todos.ask(self.conn, "What time is dinner on Saturday?", written_by="dream:nightly")
        cover = audit_questions.date_coverage(self.conn)
        self.assertEqual([day for _id, day in cover["weekday_only"]], ["2026-08-22"])
        todos.ask(self.conn, "Is the trip still on August 24?", written_by="dream:nightly")
        self.assertEqual(len(audit_questions.date_coverage(self.conn)["weekday_only"]), 1)

    def test_reconciler_question_is_exempt(self):
        event, _ = events.upsert(self.conn, {"title": "Play Half-Life 2",
                                             "date": "2026-08-19"}, written_by="cli")
        sweep.reconcile_backward_window(self.conn, self.cfg)
        cover = audit_questions.date_coverage(self.conn)
        self.assertEqual(cover["exempt"], 1)
        self.assertEqual(cover["undated"], 0)
        self.assertTrue(event.id)


class TestWhatsAppDirectMessageFixture(unittest.TestCase):
    """The fixture covers direct messages and both group-detection signals."""

    def setUp(self):
        self.store = (Path(build.__file__).resolve().parent / "fixtures" / "whatsapp"
                      / "ChatStorage.sqlite")
        self.src = sqlite3.connect(f"file:{self.store}?mode=ro", uri=True)
        self.src.row_factory = sqlite3.Row
        self.addCleanup(self.src.close)

    def _sessions(self):
        return {row["ZPARTNERNAME"]: row for row in self.src.execute(
            "SELECT ZCONTACTJID, ZPARTNERNAME, ZSESSIONTYPE FROM ZWACHATSESSION")}

    def _rows(self, partner):
        session = self._sessions()[partner]
        return [{"chat_jid": session["ZCONTACTJID"],
                 "session_type": session["ZSESSIONTYPE"]}]

    def test_fixture_contains_direct_message(self):
        dm = [name for name, row in self._sessions().items()
              if not str(row["ZCONTACTJID"]).endswith("@g.us")]
        self.assertTrue(dm, "every WhatsApp session is a group again")
        for name in dm:
            self.assertFalse(whatsapp.is_group(self._rows(name)[0]), name)

    def test_direct_message_has_no_group_member(self):
        dm = next(name for name, row in self._sessions().items()
                  if not str(row["ZCONTACTJID"]).endswith("@g.us"))
        pk = self.src.execute("SELECT Z_PK FROM ZWACHATSESSION WHERE ZPARTNERNAME = ?",
                              (dm,)).fetchone()["Z_PK"]
        members = self.src.execute(
            "SELECT count(*) AS n FROM ZWAMESSAGE"
            " WHERE ZCHATSESSION = ? AND ZGROUPMEMBER IS NOT NULL", (pk,)).fetchone()
        self.assertEqual(members["n"], 0)

    def test_group_jid_works_without_session_type(self):
        disagreeing = [name for name, row in self._sessions().items()
                       if str(row["ZCONTACTJID"]).endswith("@g.us")
                       and not row["ZSESSIONTYPE"]]
        self.assertTrue(disagreeing, "no session exercises the JID read on its own")
        for name in disagreeing:
            self.assertTrue(whatsapp.is_group(self._rows(name)[0]), name)

    def test_ingest_records_direct_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config(home=Path(tmp))
            cfg.ensure_dirs()
            conn = db.open_db(cfg.db_path)
            self.addCleanup(conn.close)
            whatsapp.ingest(conn, cfg, db_path=str(self.store))
            rows = conn.execute(
                "SELECT thread, is_group FROM threads WHERE stream = 'whatsapp'"
            ).fetchall()
        shape = {row["thread"]: bool(row["is_group"]) for row in rows}
        self.assertIn("Rae", shape, f"the DM did not arrive: {sorted(shape)}")
        self.assertFalse(shape["Rae"])
        self.assertTrue(shape["doggo park"],
                        "the session whose type says 0 must still read as a group")

    def test_undeclared_thread_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(build, "OUT", Path(tmp)):
                with self.assertRaises(KeyError):
                    build.build_whatsapp([
                        {"src": "wa", "thread": "a thread nobody declared", "who": "me",
                         "day": 1, "time": "09:00", "text": "hello"}])


class TestTheCollisionModelLayerActuallyCallsAModel(unittest.TestCase):
    """A layer named `model` that runs the oracle is the deterministic layer in disguise.

    It graded as a model measurement for as long as nobody checked, which is the same
    class of mistake as a void run: unread traffic and traffic the model understood
    nothing of score identically, so the number looks like a measurement and is not one.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.dir, ignore_errors=True)
        self.addCleanup(db.set_today, None)

    def _stub(self):
        """A client that answers every request with an empty, well-formed review."""
        from memcal import llm
        calls = []

        class Client(llm.CompletionClient):
            def complete(_self, *, prefix="", suffix="", **kwargs):
                calls.append({"prefix": prefix, "suffix": suffix, **kwargs})
                ids = re.findall(r"BUNDLE ID (\w{6})", suffix)
                return llm.Reply(text="", data={"reviewed": ids, "diffs": []},
                                 generation_id=f"gen-{len(calls)}")

        return Client(), calls

    def test_model_mode_dispatches_the_pass_through_dream(self):
        client, calls = self._stub()
        scenario = next(s for s in collision.SCENARIOS if s.id == "f1.same-statement")
        with mock.patch("memcal.llm.client_for", return_value=client):
            rows = collision.run_scenario(
                scenario, Path(self.dir) / "model", layer="model")
        self.assertTrue(calls, "model mode made no model call")
        self.assertTrue(all(row["model_calls"] > 0 for row in rows
                            if row["checkpoint"] == "after-pass"), rows)

    def test_the_oracle_answers_are_never_put_in_front_of_the_model(self):
        """The scenario's own answer key must be unreachable from the prompt."""
        client, calls = self._stub()
        scenario = next(s for s in collision.SCENARIOS if s.id == "f1.same-statement")
        with mock.patch("memcal.llm.client_for", return_value=client):
            collision.run_scenario(scenario, Path(self.dir) / "leak", layer="model")
        sent = "\n".join(f"{c['prefix']}\n{c['suffix']}" for c in calls)
        answers = [row["title"] for diff in scenario.script for entry in diff.values()
                   for row in entry.get("events") or []]
        self.assertTrue(answers, "this scenario has no oracle answers to leak")
        self.assertTrue(sent, "nothing was sent to the model")
        for title in answers:
            self.assertNotIn(title, sent)

    def test_the_apply_layer_still_makes_no_model_call(self):
        client, calls = self._stub()
        scenario = next(s for s in collision.SCENARIOS if s.id == "f1.same-statement")
        with mock.patch("memcal.llm.client_for", return_value=client):
            rows = collision.run_scenario(
                scenario, Path(self.dir) / "apply", layer="apply")
        self.assertEqual(calls, [])
        self.assertTrue(all(row["model_calls"] == 0 for row in rows), rows)


if __name__ == "__main__":
    unittest.main()
