# Supported tools

The CLI and web UI own normal operation. This directory only keeps diagnostics that
still answer a question those surfaces cannot.

**One rule decides whether something belongs here: a tool is either a lab instrument
you will run again, or it was a one-shot and belongs in git history.** A one-shot
repair that has already been applied is worse than useless once it is done — it is
destructive if re-run, and it makes the directory look like a place where the answer
might be, which costs a reading every time. Eleven of them were deleted on 2026-08-05;
`git log --diff-filter=D -- tools/` has them all, with the commit that explains what
each was for.

A probe whose finding is already captured in the README or a regression test is a one-shot.
A probe that is
the *only* check on a code path the unit tests deliberately stub is an instrument, and
stays however long ago it was written.

## The benchmark

- `benchmark_temporal.py` — the gap-finding benchmark, and the thing that actually
  catches regressions. `core` is the native-source **four-day** scenario; `contract`,
  `boundaries`, `clock` and `hermes` probe hostile saved replies, calendar/timezone
  edges, what only elapsed time shows, and the multi-turn memory lifecycle. Use
  `--suite`, `--case`, `--repeat`, `--model`, `--prompt-version`, `--pack`, `--format`,
  or `--dry-run`. Frontier failures are deliberate unmet expectations, not xfails.

  Its `integration` layer injects hand-written oracle diffs, so it tests the
  production-shaped multi-day Merge/apply/render/store lifecycle but deliberately
  does **not** test extraction or whether the prompt contained enough context. The unit
  suite owns narrow deterministic invariants such as candidate-event retrieval; the
  `model` layer tests those candidates and the prompt through an actual model.
  Deprecated `replay`/`live` CLI aliases remain accepted for old scripts.

- `bench_reset.py` — build a scratch store that replays a cold start. Excludes the
  `agent` stream by default, because it is the user correcting the model *after* the
  run being replayed, and leaving it in grades the model on a corpus containing the
  answers. That is not hypothetical: it turned a 1/7 replay into a 6/7 one.
- `bench_outcomes.py` — score a store against known-defect outcomes
  whose right answer is already known. Outcomes, not unit tests: a model is allowed to
  be right in a way the file did not anticipate, so a FAIL is a prompt to look rather
  than proof of a bug. Anything that reproduces deterministically should graduate into
  `tests/`.
- `bench_audit.py` — re-derive claims a row makes from the lines it cited, over a whole
  store.
- `clock_sweep.py` — run the unit suite as of other days, and with `--hours` at other
  times of day, and report what only passes now. An instrument rather than a one-shot,
  because the failure it finds is created fresh every time somebody types a date into a
  test: a weekday assumed, or a literal picked relative to the afternoon it was written.
  `python3 -m unittest discover` cannot see either — it is green that afternoon. Eight
  tests were found this way on 2026-08-13, one of them red that morning.

  **`MEMCAL_TODAY` now takes an optional time** (`2026-08-20T19:00`), and the hour is a
  real axis: one test asserted a reminder lands before its due date, which is true until
  19:00 and false after it, so it was red five hours a night on every commit and this
  tool could not see it — it swept days while taking the hour from whenever it ran.
  See the testing rules in [AGENTS.md](../AGENTS.md).

## Reading what happened

- `read_calls.py`, `dump_generation.py` — inspect saved model calls and backfill old
  ones. **Read the trace before blaming the prompt**: fourteen model-side failures in
  one pass turned out to be thirteen code bugs.
- `state.py` — compact store diagnostics.
- `probe_corpus.py` — what is actually in the archive, by stream and by shape.
- `probe_page.py` — what `memcal_open_page` actually returns for one slug, and how many
  tokens of it. Read-only against the live store. "The page read is too bare / too
  noisy" is a claim with a number attached, and this is where the number comes from:
  the résumé slot came back with three lines of small talk and no structured facts.

## Gate and extraction quality

- `audit_gate.py`, `audit_subject_gate.py` — gate quality over the real archive.
- `audit_questions.py` — replay today's question rules over the questions already in
  the store, using each one's recorded evidence as the bundle it came from. Read-only.
- `audit_duplicates.py` — rows that are the same occasion under two names.
- `probe_affinity.py` — which conversations look like one occasion, without a model.
- `probe_reasoning.py` — measure a model's real reasoning spend before setting its
  `ENDPOINTS` entry. Reads `completion_tokens_details.reasoning_tokens`, never the
  length of the returned reasoning text: OpenAI shows a summary and encrypts the rest,
  so the visible text undercounted by 5.5x on the one bundle it was checked against —
  in the one direction that truncates a request and loses the whole call.
- `probe_nightly.py` — what the scheduled pass would do right now.

## The one probe that touches a real calendar

- `probe_calendar_publish.py` — the Calendar.app write round trip, end to end, against
  a throwaway calendar it cleans up after itself. **Kept when its siblings were
  deleted, and the reason is the rule above:** the unit tests stub `osascript` on
  purpose — a test that writes to the real calendar is the bug this feature shipped
  with, and `TestNoTestEverWritesToTheRealCalendar` holds that line — so this is the
  only thing anywhere that checks the write actually lands, the uid comes back, and the
  next ingest declines to read memcal's own event back in. It is not a record of a past
  finding; it is the only instrument pointed at that seam.

## The one tool something else runs on a schedule

- `due_reminders.py` — prints the reminders that have come due, and prints nothing when
  none have. Hermes' cron runs it every half hour and injects its stdout into an agent
  turn, which decides what to say or replies `[SILENT]`. So this is a lab instrument in
  the strict sense — it runs again, constantly — but the thing running it is outside this
  repo, and **its output reaches a phone**. Run it by hand *without* `--mark` to see what
  the agent would be handed; `--mark` records the poke and snoozes it. Publishing setup is
  documented under [Apple Calendar and Reminders](../README.md#apple-calendar-and-reminders).

## Reading the tracker

- `triage.py` — shows open issues that still need reproduction, issues referenced by
  other open issues, and the rest of the queue. It reports relationships without
  pretending a citation proves dependency or priority. `--issue N` shows both directions
  for one issue; `--needs-reproduction` isolates claims that still need a focused run.

## Housekeeping

- `resweep.py` — re-run the sweep over an existing store. Dry-run first.

## Output

`bench_output/` is gitignored, because a run carries the corpus out with it — real
message text, in a store shaped like the owner's. A benchmark prints its deterministic
score and detailed checks for that run; the repository keeps the scenarios, not a score
ledger.
