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

## Reading what happened

- `read_calls.py`, `dump_generation.py` — inspect saved model calls and backfill old
  ones. **Read the trace before blaming the prompt**: fourteen model-side failures in
  one pass turned out to be thirteen code bugs.
- `state.py` — compact store diagnostics.
- `token_calibration.py` — measures `textclean.estimate_tokens` against the
  `usage.prompt_tokens` the provider reported for every saved call, and with `--write`
  refreshes `tests/token_calibration.json`. The fixture holds character-class counts
  only, never message text, which is what makes a private corpus checkable in a public
  repository. Run it after a run adds calls: the estimate must stay above the real
  count, and the guard test in `tests/test_core.py` is what fails when it drifts.
- `probe_page.py` — what `memcal_open_page` actually returns for one slug, and how many
  tokens of it. Read-only against the live store. "The page read is too bare / too
  noisy" is a claim with a number attached, and this is where the number comes from:
  the résumé slot came back with three lines of small talk and no structured facts.

## Gate and extraction quality

- `audit_gate.py`, `audit_subject_gate.py` — gate quality over the real archive.
- `audit_questions.py` — replay today's question rules over the questions already in
  the store, using each one's recorded evidence as the bundle it came from. Read-only.
  Also reports how each open question knows which day it is about: a linked row, a day
  the model happened to spell into the text, or nothing at all. That middle number
  going to zero means expiry has quietly reverted to the flat ten-day TTL, and it is
  the only place that would say so.
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

- `due_reminders.py` — prints the reminders that have come due, and prints a
  `{"wakeAgent": false}` gate line when none have. The cron itself lives outside
  this repo, so this file documents what it should invoke; the cron cannot be
  changed here.
  - Old: the cron ran `python3 tools/due_reminders.py [--mark] [--home ...]` every
    half hour and injected its stdout prose into a detached agent turn, which decided
    what to say or replied `[SILENT]`. The poke reached the phone but was never
    appended to the chat session transcript, so a follow-up ("yeah I'll do it
    tomorrow") had no referent in context (issue #56).
  - New: the cron runs `python3 tools/due_reminders.py --format json [--home ...]`
    (without `--mark`) and feeds that one JSON line to the Hermes delivery entry
    point `deliver_due_reminders()` in `integrations/hermes/memcal/__init__.py`,
    which appends the reminder as an assistant turn inside the target chat session
    (authorship `memcal-reminder`/wake, never the user's words) and wakes the agent
    turn on it; the entry point calls `todos.mark_reminded` after a successful
    append, so a failed delivery does not snooze the reminder. Concretely, the
    outside cron should do the equivalent of:
    `python3 tools/due_reminders.py --format json --home "$MEMCAL_HOME"` →
    parse the `reminders` list (each entry carries `key`, `kind`, `text`, `line`) →
    `deliver_due_reminders(conn, "<chat-session-id>", payload)` → hand the returned
    `wakeText` to the gateway wake path (`gateway/wake.py:deliver_wake`) for that
    session. An empty payload (`{"wakeAgent": false, ...}`) delivers nothing.
  So this is a lab instrument in
  the strict sense — it runs again, constantly — but the thing running it is outside this
  repo, and **its output reaches a phone**. Run it by hand *without* `--mark` to see what
  the agent would be handed; `--mark` records the poke and snoozes it (preserved for
  terminal use and the old flow; the new flow marks on delivery instead). Publishing setup is
  documented under [Apple Calendar and Reminders](../README.md#apple-calendar-and-reminders).

## Reading the tracker

- `triage.py` — shows open issues that still need reproduction, issues referenced by
  other open issues, and the rest of the queue. It reports relationships without
  pretending a citation proves dependency or priority. `--issue N` shows both directions
  for one issue; `--needs-reproduction` isolates claims that still need a focused run.

## Housekeeping

- `resweep.py` — re-run the sweep over an existing store. Dry-run first.

## Verification

- `run_tests.py` runs buffered unittest modules in separate processes; use `-j` to
  limit concurrency or pass module names for focused checks.
- `clock_sweep.py` checks the unit suite across dates and timezones (`--cross`
  for separate axes, `--zones` for the full grid).
