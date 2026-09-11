# Changelog

Notable user-facing changes are recorded here. This project follows [Semantic
Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Slack, Telegram and Signal sources: `slack=xoxp-...`, `telegram_api_id=`/`telegram_api_hash=` +
  `memcal login telegram`, Signal via `signal-cli` (local-only, optional `signal_account=`). See README Chat sources; `memcal sources` flags gaps.
- `memcal login <source>` runs a source's one-time interactive device sign-in.
- `memcal login slack` provisions the app through the Slack CLI (manifest + install);
  only the `xoxp-` copy-paste stays human. Telegram asks for international phone format;
  Signal prints its QR code in-terminal.
- `memcal login` pastes, validates and saves chat credentials itself (asking before it
  overwrites); `memcal doctor` re-tests every saved key through each source's check.
- Shared source base: `PolledSource` (per-conversation watermarks), `StreamSource` (single cursor);
  platforms implement auth/listing/speech, inheriting dormant-chat skipping, budgets and backoff.
- Settings tab in web UI for every `MEMCAL_*` setting; saves `~/.memcal/.env` and applies
  immediately. Shows provider reachability, source needs, credential presence, agent state.
- Antigravity (`agy`) backend: `memcal setup --provider antigravity` (default `gemini-3.8-flash-high`).
- Email bodies are fetched and archived before relevance; automatic relevance sets priority only.
  Only an explicit ignore (`memcal senders <address> ignore`, agent, web queue) skips fetch.
- `memcal mail` shows queue by priority and bodyless count; `memcal mail --backfill [--apply]`
  previews and runs resumable in-place recovery.
- Mail threads by `Message-ID`, `In-Reply-To`, `References`.
- Queue view filters to the low-priority backlog with per-message body status.
- Typed tool calls record operation, row, changed fields, causing message and row version.
- Untargeted change observations are kept with evidence, retried, and raised as questions.
- Failed dream passes are retryable from Runs/Dream tabs or `memcal dream --retry RUN`.
- `tools/benchmark_temporal.py --provider` selects the backend (incl. Antigravity).
- `tools/benchmark_temporal.py --suite collision [--variants N]` replays timed ops, grading
  duplicates, false merges, lost corrections and missed cancellations separately.
- `memcal who --resolve` names unresolved handles and folds variants in one call; `memcal who`
  lists assumed merges and open doubts; `--split <n>` / `--confirm <n>` acts on one.
- Missed passes run on next wake; `memcal schedule` / `memcal doctor` show last run and owed
  state; `memcal schedule due` answers alone.
- Subscription-backend dry runs report run-sized tokens.

### Changed

- Settings model fields list only the selected provider's models (Antigravity via `agy models`);
  cross-provider values move to the new default, unknown names still save.
- One control sets propose/sweep/merge models; reasoning effort is ignored for models naming
  their own budget.
- Daytime corrections survive older evidence collected later; precedence is per field on when
  evidence was said, comparing source time against source time.
- Fields are dated from lines read for that field; unsupported fields can add but not overrule.
- Cancellations without a verified identifier or recorded decision are never applied.
- Shared titles across days don't merge; reschedules identify their row, restated old dates
  still find it, subscribed-calendar mentions join.
- Retried tool calls are idempotent by requested operation (optional caller key), checked
  in-transaction; replays never undo newer moves.
- Email backfill verifies UID + UIDVALIDITY + `Message-ID`, else searches by id; bracketed
  `Message-ID` forms match the same mail.
- Merge leaves rows separate on timeout, cutoff or `unresolved`, and says so.
- Nightly pass nominates candidates by authorship, person and day as questions, noting truncation.
- `memcal dream` prints progress while running.
- One launchd agent (03:00, login, every 30 min); `memcal schedule install` retires `com.memcal.catchup`.
- Default backend is Codex; existing `MEMCAL_LLM_PROVIDER` is unaffected.
- `memcal doctor` reports installed-but-unloaded schedules and started-but-unfinished collections.
- `memcal setup` records absolute `claude` / `codex` executable paths.
- Proton Bridge email auto-selects implicit SSL or STARTTLS; Proton requests `Cc`.
- Temporal benchmarks don't mutate the tracked score ledger.
- Event detail uses plain-English state; brief, detail and web share concise labels.
- Open questions are reviewable beside conversations (keep/amend/answer/close) with history.
- Cross-conversation stage is named Merge; old `resolve` rows display the same.
- Standing is retired as a general store: new writes rejected, legacy `S` rows stay readable.
- `MEMCAL_MATCH_MODEL` selects the row-identity judge.
- Single fitted token estimator serves packing, trimming and cost figures.
- Reminders fire at the intended hour across DST boundaries.
- Cost-recording failures are kept and reported by `memcal doctor` against the ledger.
- Reactions of 3+ chars join bundles; platform mutes record as automatic.
- Merge reports local-fallback arbitration and cutoff-vs-decline; output sized from the endpoint.
- WhatsApp re-login no longer collides reused local IDs across accounts.
- Proton Bridge login failures name stale mailbox credentials after password changes.
- Overnight `happened` settlements record history.
- Source timelines mark date/channel/conversation changes.
- Web brief highlights latest-pass creates (green) and edits (yellow).
- Partiful invites record public hosts, named in brief when the title omits them.
- Dream materializes the next standing occurrence before new traffic.
- Same-day plans with differing subject framing still reach Merge.
- Ready-anytime pickups become open to-dos; moves keep new dates.
- Generated series pages retire on typed-schedule rename; user-authored pages untouched.
- Satisfied wake conditions don't duplicate when Dream already asked in better words.
- Merge treats identical date+time as evidence, without joining unrelated same-time titles.
- All-unresolved conversations still see wording-matched rows; name-spelling emails link.
- Wiki lookup matches longer/shorter name forms, without first-three-letter collisions.
- Splitting chained assumptions in display order restores original persons.

### Fixed

- `Config.secret` family alias no longer prefix-matches siblings (`SLACK_TOKEN` vs `SLACK_USER_ID`).
- Dream tab folds bundle cards by default with internal scroll.
- Store-run models are offered only under their own provider.
- Antigravity calls get memcal's deadline; empty successes name deadline-expiry as likely cause.
- Subscription-allowance exhaustion stops the pass with the provider's reset window.
- Codex/Claude Code record reasoning summaries, billed reasoning tokens and effort (`memcal trace`).
- Costless-provider passes say so instead of `$0.0000`.
- Recovered passes no longer record `runs.error`; `memcal doctor` stays clean.
- `memcal doctor` shows a pass's first failure in full.
- Schedule upgrades preserve retired scripts, retire predecessors only after replacement loads.
- Unpublished schedule occurrences (no rule, refused publish) aren't published standalone.
- Calendar publishing enables only from the store's own `.env`.
- Silent sources past the overview window list as stale.
- Agent-session user lines are attributed everywhere quoted.
- Direct `chat.db` reader reports page-full stops; muted/over-horizon skips split from gate rejects.
- Crashed passes record errors; previously open rows are marked abandoned on next run.
- Truncated state reviews apply nothing.
- Short display names allowed (CJK single-char words; `Jo`/`Al`/`Ed`); single letters stay initials.
- Unnamed GroupMe participants queue from group detail rosters.
- Textless bodies (bare `�`) store as attachment-only with no queued text; emoji/`?` still count.
- Phone numbers and private quoted prose removed from comments/docstrings, with regression check.

### Removed

- Discord source removed: no compliant token reads human DMs. `discord.*` watermarks ignored.
- Unread `verify`, `verify_budget`, `pack_cross_reference` settings (`MEMCAL_VERIFY_BUDGET`).
- `source.ical.last_count` no longer recorded; every `source.*` marker has a reader (tested).


## [0.6.0] - 2026-08-14

- Dates stop being guessed and reminders stop needing to be requested: stated dates win,
  and obligations involving another person can schedule themselves.

## [0.5.0] - 2026-08-13

- Collection health is measured from stored data, and reminders reach connected agent
  and device surfaces.

## [0.4.0] - 2026-08-13

- Fixed deleted-source alarms, invalid link locations, series storage, and withheld
  fields being treated as values.

## [0.3.0] - 2026-08-06

- Deterministic matching merges multiple wordings of the same occasion.

## [0.2.0] - 2026-08-05

- Added persistent benchmark scoring and separated developer documentation.

## [0.1.0] - 2026-07-30

- Initial release of the calendar, to-do, source-ingestion, and agent-context core.
