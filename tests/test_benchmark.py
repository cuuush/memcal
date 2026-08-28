"""The gap-finding harness is itself deterministic infrastructure."""

from __future__ import annotations

import ast
import io
import re
import tempfile
import unittest
from datetime import date
from pathlib import Path

from memcal import db, events
from memcal.config import Config
from tests.scenarios import build, expect, probes, skeleton
from tools import benchmark_temporal


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
    # it is an integrity invariant over whatever was extracted, not a claim that
    # anything was. `brief.every-row-can-be-opened` is the paired check that does fail
    # on an empty store, because an empty brief still prints a row saying so.
    'brief.every-handle-opens', 'brief.every-handle-opens-at-the-end',
    'bailey.not-mom', 'beergarden.not-next-week', 'brief.later-is-visible',
    'brief.no-jargon', 'brief.no-keys', 'brunch.reads-plainly',
    'comet.no-species-claim', 'comet.not-a-horse', 'd1.car-not-standing',
    'd1.junk-aws', 'd1.junk-chase', 'd1.no-vendor-event', 'd1.work-no-row',
    'vendor.no-todo', 'vendor.nothing-at-all',
    'feed.stays-out-of-the-brief',
    'gate.amc-marketing-stays-out', 'gate.amc-wrong-stays-out', 'gate.bulk-stays-out',
    'jordan.not-eastwood', 'junk.no-affection', 'junk.no-amazon',
    'junk.no-aws', 'junk.no-home-logistics', 'junk.no-opinion',
    'junk.no-sale', 'mom.not-bailey', 'movie.not-live',
    'pages.no-bulk-sender', 'pages.no-group-name', 'pages.no-raw-handle',
    'pages.no-self', 'pages.no-shortcode', 'pages.none-empty',
    # 54. A numeral is not a person, no page for it, no question nobody can answer.
    # Paired with `nameless.plan-survives-its-proposer`, which is a real positive and
    # does fail on an empty store — so the beat cannot go green on nothing.
    'nameless.no-page-for-a-number', 'nameless.no-unanswerable-question',
    # 55. Nothing may be created for GroupMe itself. These three have no non-vacuous
    # partner **on purpose**: the beat's two positives are frontier, because
    # `groupme._deliver` drops the whole system channel at ingest and the notice that
    # carries the plan goes with it. The challenge is therefore never counted as fully
    # green, which is the honest reading and the reason not to invent a positive here.
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


class TestNoScoreEverSurvivedTheSession(unittest.TestCase):
    """Two docs cited a `bench_output/temporal/RESULTS.md` that did not exist and would
    have been gitignored. Regression detection depended on a human remembering the last
    number."""

    def _run(self, results, layer="integration"):
        return {"suite": "core", "layer": layer, "model": "none", "prompt": "v2",
                "format": "v1", "results": results}

    def test_history_round_trips_and_flags_green_to_red(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = benchmark_temporal.HISTORY
            benchmark_temporal.HISTORY = Path(tmp) / "bench_history.jsonl"
            try:
                rows = [{"id": "a.one", "challenge": "1 date move", "ok": True,
                         "soft": False, "frontier": False},
                        {"id": "a.two", "challenge": "1 date move", "ok": True,
                         "soft": False, "frontier": False}]
                benchmark_temporal.record_history(self._run(rows), "20260805-090000")
                broken = [dict(rows[0]), {**rows[1], "ok": False}]
                benchmark_temporal.record_history(
                    self._run(broken), "20260805-100000")
                text = benchmark_temporal.history_report(5)
            finally:
                benchmark_temporal.HISTORY = original
        self.assertIn("2/2 hard", text)
        self.assertIn("1/2 hard", text)
        self.assertIn("a.two", text)
        self.assertIn("went green -> red", text)

    def test_history_carries_no_message_text(self):
        """The reason this file can be committed at all."""
        with tempfile.TemporaryDirectory() as tmp:
            original = benchmark_temporal.HISTORY
            benchmark_temporal.HISTORY = Path(tmp) / "bench_history.jsonl"
            try:
                benchmark_temporal.record_history(self._run([
                    {"id": "a.one", "challenge": "1 date move", "ok": False,
                     "soft": False, "frontier": False,
                     "note": "poker at Robbie's moved to 42 Example Street"},
                ]), "20260805-090000")
                written = benchmark_temporal.HISTORY.read_text()
            finally:
                benchmark_temporal.HISTORY = original
        self.assertNotIn("Example Street", written)
        self.assertNotIn("note", written)

    def test_old_complete_runs_rotate_out_of_the_committed_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = benchmark_temporal.HISTORY
            original_keep = benchmark_temporal.HISTORY_RUNS_PER_STREAM
            benchmark_temporal.HISTORY = Path(tmp) / "bench_history.jsonl"
            benchmark_temporal.HISTORY_RUNS_PER_STREAM = 2
            try:
                rows = [
                    {"id": "a.one", "challenge": "date move", "ok": True,
                     "soft": False, "frontier": False},
                    {"id": "a.two", "challenge": "date move", "ok": False,
                     "soft": False, "frontier": False},
                ]
                for hour in range(3):
                    benchmark_temporal.record_history(
                        self._run(rows), f"20260805-{hour:02d}0000")
                recent = benchmark_temporal._history_runs()
                complete = benchmark_temporal._history_runs(include_archive=True)
                archived = benchmark_temporal._archive_path().read_text()
            finally:
                benchmark_temporal.HISTORY = original
                benchmark_temporal.HISTORY_RUNS_PER_STREAM = original_keep
        self.assertEqual(len(recent), 2)
        self.assertEqual(len(complete), 3)
        self.assertIn("20260805-000000", archived)


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


class TestADiscardedTrialLookedExactlyLikeNoTrial(unittest.TestCase):

    def _history(self, rows):
        import json
        path = Path(tempfile.mkdtemp()) / "history.jsonl"
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        real, benchmark_temporal.HISTORY = benchmark_temporal.HISTORY, path
        self.addCleanup(setattr, benchmark_temporal, "HISTORY", real)
        return path

    def _scored(self, at, trial, ok):
        return {"at": at, "commit": "abc", "suite": "core", "layer": "model",
                "trial": trial, "check": "a.b", "challenge": "c",
                "ok": ok, "soft": False, "frontier": False}

    def _void(self, at, trial):
        return {"at": at, "commit": "abc", "suite": "core", "layer": "model",
                "trial": trial, "void": True,
                "lost": ["day 1: 6 bundle(s): rate-limited upstream"]}

    def _plain(self, text):
        return re.sub(r"\033\[[0-9;]*m", "", text)

    def test_a_void_trial_is_named_in_the_history(self):
        self._history([self._scored("20260813-100000", 1, True),
                       self._void("20260813-110000", 2)])
        report = self._plain(benchmark_temporal.history_report(5))
        self.assertIn("VOID", report)
        self.assertIn("rate-limited upstream", report)

    def test_the_rate_of_useless_trials_is_stated(self):
        """One void trial is weather; two in three is a broken instrument."""
        self._history([self._void("20260813-100000", 1),
                       self._void("20260813-110000", 2),
                       self._scored("20260813-120000", 3, True)])
        report = self._plain(benchmark_temporal.history_report(5))
        self.assertIn("2 of the last 3 trial(s) produced no measurement", report)

    def test_a_void_trial_scores_nothing(self):
        """The original protection has to survive: it must not read as a measurement."""
        self._history([self._void("20260813-100000", 1)])
        runs = benchmark_temporal._history_runs()
        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0]["void"])
        self.assertEqual(runs[0]["checks"], {})

    def test_a_void_trial_does_not_break_the_regression_comparison(self):
        """green -> red must compare the two scored runs, not a scored one and a hole."""
        self._history([self._scored("20260813-100000", 1, True),
                       self._void("20260813-110000", 2),
                       self._scored("20260813-120000", 3, False)])
        report = self._plain(benchmark_temporal.history_report(5))
        self.assertIn("went green -> red: a.b", report)

    def test_a_stream_of_nothing_but_voids_says_so(self):
        self._history([self._void("20260813-100000", 1)])
        report = self._plain(benchmark_temporal.history_report(5))
        self.assertIn("no scored run in this stream", report)


if __name__ == "__main__":
    unittest.main()
