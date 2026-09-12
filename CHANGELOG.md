# Changelog

Notable user-facing changes are recorded here. This project follows [Semantic
Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- memcal.app carries the handle-grid icon ([E]/[T]/[Q] pills over a calendar and
  store) instead of a generic executable glyph, and the web UI serves the same
  art as its favicon. `memcal schedule install` converts `memcal/macos/icon.png`
  to `AppIcon.icns` before signing; a missing icon toolset still builds an
  iconless bundle rather than failing.

### Fixed

- `memcal_answer` on the MCP surface resolves to-dos as well as questions, matching
  Hermes and the CLI: saying a to-do is done closes it instead of answering "no
  matching open question". Hermes `ADD_TODO` accepts `done=true` to close, mirroring
  `memcal_todo(done=true)` on MCP, which previously could only open.
- Dream `--dry-run` prices multi-wave cold starts as multi-wave: the shared prompt
  prefix is stable within a wave and rebuilt across waves by design, and the estimate
  now counts each wave's cache writes instead of quoting a single wave.
- `memcal ical setup` accepts full Calendar access on macOS 14+, where EventKit
  reports it as `4` rather than the `3` that was the only value checked — a correct
  grant read as "no access" forever. Add-Only is detected as its own state with the
  manual flip to Full Access spelled out, since macOS asks only once per app.
- The EventKit consent wait is bounded (45s) and announced instead of hanging
  silently, and memcal.app runs as an agent that can show the consent dialog
  rather than background-only, which could not.
- EventKit consent and account checks run as memcal through a temporary launchd
  agent instead of in-process: from a terminal they resolved to the terminal's
  identity, granting or failing the wrong app. Doctor reads setup's stamp rather
  than re-checking the wrong identity live.
- The web UI's frontend bundle reads only script and style assets, so a binary
  file under `memcal/static/` no longer breaks every page that embeds it.

## [0.7.0] - 2026-09-11

### Added

- A documentation site (MkDocs Material, deployed to GitHub Pages) with quickstart
  and Slack, Telegram, and Signal source setup guides.
- Event updates can explicitly remove named participants as well as add them.

- Events can record duplicate, replacement, and related-event links across sources.
- One-way email threads from the same sender are grouped across changing subject lines.
- Events named by unresolved cancellations are withheld from calendar publishing.

- Failed and partial dream passes can be retried from the Dream tab or with
  `memcal dream --retry RUN`. Runs show their outcome and retain claimed lines that have
  aged past the model horizon.
- Settings offer models for the selected provider, reject known cross-provider
  mismatches, and can set all three dream-stage models together. Antigravity uses
  `agy models` when available and a bundled fallback otherwise.

- A Settings tab edits every `MEMCAL_*` option without flattening hand-written `.env`
  content. It validates the whole save, applies changes to the running process, reports
  stronger configuration sources, and shows provider, source, credential, schedule,
  model, executable, and calendar status without exposing secret values.
- Antigravity (`agy`) joins Codex and Claude Code as a model backend that runs on a login
  you already have: `memcal setup --provider antigravity`, defaulting to
  `gemini-3.8-flash-high`. Its Gemini, Claude and open-weight models are all reachable
  with `memcal setup --model`, and the reasoning budget named in a model id is respected
  rather than overridden.
- Email is now included by default. Every message inside the configured folders and sync
  range has its body fetched and archived before anything decides how relevant it is,
  and automatic relevance sets a processing priority instead of an exclusion: mail from
  lists, retailers and no-reply addresses is read after everything else, in bounded
  batches, and stays searchable and available to the assistant throughout. Only an
  explicit "no" from you keeps a sender out — and it keeps them out completely: a blocked
  sender's body is never fetched, never stored, and never picked up by `--backfill`.
- `memcal mail` shows what is queued by priority and how much archived mail has no body
  on file. `memcal mail --backfill` reports what a recovery run would cover;
  `--apply` runs it, updating rows in place so a second run cannot duplicate mail.
  It never deletes, moves, or marks anything in your mailbox.
- Mail is grouped into conversations by `Message-ID`, `In-Reply-To` and `References`, so
  a reply joins the thread it answers and two unrelated messages from one sender stay two
  conversations.
- The queue view can be filtered to the quiet backlog and shows, per message, whether its
  body was read whole, shortened, or never fetched.
- A typed tool call now records the operation alongside the change it made — the row it
  targeted, the fields that moved, the message that caused it, and the row version it
  acted on — in the same transaction, and the nightly pass is shown those records beside
  the conversation that produced them.
- An observation that plainly changes something but names no target — "that's cancelled",
  with no plan and no day — is kept with its evidence instead of being dropped or written
  as an invented event. It is retried as later traffic arrives, and becomes a question
  when more than one plan could be the one meant. Answering that question is what applies
  it; answering that it was none of them puts it away.
- `tools/benchmark_temporal.py --provider` selects the backend a run measures, and
  Antigravity is reachable from it. The backend was fully supported and unreachable from
  the tool: there was no flag, and the `--model` guard refused every name without an
  `llm.ENDPOINTS` entry, which is all three CLI backends' native names. That guard now
  applies only to OpenRouter, where its reasoning about unpinned provider routing
  actually holds. Documented in `tools/BENCHMARK.md`.
- A dry run against a subscription backend says the tokens are the size of the run rather
  than telling you to add a per-token price it will never bill.

### Fixed

- GroupMe edit notices retain the edited message and its named author while ordinary
  platform bookkeeping remains ignored.
- Calendar timestamps are rendered in local time, nonexistent daylight-saving wall
  times are not stored, and weekday-plus-date phrases must agree.
- Deterministic dream reruns no longer add duplicate audit entries; derived wiki facts
  retain source evidence.

- Merge uses timestamped source evidence to keep cancelled bookings separate from their
  replacements. Field citations and event links survive merging and replay.
- Cancellation candidates require an identified target, respect newer confirmations,
  and keep clarification questions available across dream waves.
- Sweep cannot change appointment status from a summary without source evidence.

- The Dream tab folds long bundle previews into a bounded scrolling section.
- Models previously used through another provider are no longer suggested for the
  selected provider.
- Antigravity requests no longer fail wholesale. `agy` runs its own five-minute clock
  over a turn and, when it expires, returns partial output with a success exit code —
  either an error status or a success carrying an empty response. memcal never told it
  how long it had and waited fifteen minutes for a command that had given up at five, so
  every propose request in a real pass failed while a one-line probe succeeded. The CLI
  is now given the same deadline memcal is waiting out, and the "success with no
  response" message names the deadline as the likely cause instead of only the symptom.
- A backend reporting that its subscription allowance is spent stops the pass instead of
  being retried. Splitting a request that failed for want of allowance produces two
  requests that fail for want of allowance; the run now says so once, with the provider's
  own reset window, rather than spending its wall-clock budget on the same refusal.
- A benchmark suite for collisions between daytime writes and the nightly pass:
  `python3 tools/benchmark_temporal.py --suite collision`. It replays timed operations,
  grades the store at each checkpoint, reports duplicates, false merges, lost corrections
  and missed cancellations separately, and can replay every scenario under bounded
  perturbations with `--variants N`.

- `memcal who --resolve` names unresolved handles, folds name variants into one person,
  and drops handles with nobody behind them, in one model call over the whole roster.
- Its merges are assumptions: `memcal who` lists them by number, `--split <n>` undoes
  one, `--confirm <n>` stops listing it. A handle ruled out as a person stops being
  asked about; its mail is still collected and filed.
- The call can answer "not sure", recording a question rather than a guess. `memcal who`
  lists those separately, and `--confirm <n>` on one performs the merge or link it would
  not commit to.
- A nightly pass missed because the machine was asleep, shut, or logged out now runs the
  next time the machine is awake, instead of waiting for the following night.
  `memcal schedule` and `memcal doctor` report when the pass last ran and whether one is
  owed; `memcal schedule due` gives the answer on its own.
- Source timelines now mark changes in date, channel, or conversation.
- The web brief highlights rows created by the latest dream pass in green and rows
  edited by it in yellow.
- Partiful invitations record their public hosts and name them in the brief when the
  title does not already make the host clear.

### Changed

- The Hermes integration now injects a snapshot only when the brief has actually
  changed. Hermes pins each injected snapshot to its turn and replays it verbatim for
  prompt-cache stability, so re-emitting an unchanged brief every turn stacked
  near-duplicate copies through a conversation. An unchanged turn now injects nothing and
  the snapshot already in history stays authoritative; a data edit, a day rollover, or a
  reminder coming due turns the brief over and re-injects it.

- Every context that reaches Calendar now runs through a local `memcal.app`, so macOS
  attributes the access to **memcal** rather than to Python, osascript, or Terminal —
  and there is one grant to approve instead of a separate one per launcher. The nightly
  job runs through it, and a manual `memcal ingest`, the web server, and the MCP server
  re-execute themselves through it on start (only `schedule`, `help`, and `completion`
  never re-exec; read-only commands still go through the bundle when it exists, at the
  cost of one fork+exec). Because the grant is keyed to the bundle's signature rather
  than the interpreter's path, it survives `brew upgrade python`. `memcal schedule
  install` builds and ad-hoc-signs the wrapper when a compiler is available; without one,
  the interpreter path remains the fallback. Re-run the install command once to adopt it;
  the first Calendar prompt afterward reads memcal. Later installs skip the rebuild while
  the bundle is newer than its source, so the grant is not disturbed; pass `--rebuild`
  to force one. `memcal doctor` reports a missing, stale, or half-built bundle and a
  plist whose bundle association does not match what is installed. `memcal schedule run`
  goes through the bundle too, so a manual run uses the same grant as the 03:00 job.
- A correction made during the day is no longer undone by older evidence collected
  later. Write precedence is decided per field and on when the evidence was said, rather
  than on whether the row happened to be written the same calendar day. Genuinely newer
  evidence still lands, and no row becomes permanently unchangeable.
- Precedence now compares two source times rather than a source time against a
  processing time. A pass applying the morning's message late at night no longer makes
  that field look newer than a message sent at noon, which used to lose the later of two
  messages purely because of when a batch job ran.
- Each field is dated from the lines the pass says it read for *that field*, validated
  against the conversation it came from. A fragment whose only fresh line was about the
  time no longer carries that line's authority into the location, which is how a
  hand-corrected address was overwritten. Inferring this from the text cannot work: a
  line may quote the old arrangement, deny it, or move something without repeating a
  single value. A field with no supporting line may add a value but not overrule one
  already settled.
- A cancellation whose target is not established is never applied. Establishing it means
  a verified identifier or a recorded decision naming the row; the matcher that reunites
  a mention with a row is deliberately not enough, because it is tuned to be forgiving
  and a cancelled plan leaves nothing a person would think to look for. "Dental cleaning
  on the 15th" no longer cancels a piano lesson that day, and "your physio on Friday" no
  longer cancels Monday's. Ambiguity becomes a question; the observation is retained.
- Sharing a title across different days no longer decides that two rows are one occasion.
  A provider calls every appointment the same thing, so an independent second booking was
  indistinguishable from a reschedule and the store kept only one of two real
  appointments. A reschedule now identifies the row it moves; wording nominates a
  candidate for the pass to judge. A restatement of a day the row has actually held still
  finds it, so a straggler repeating the old date does not become a duplicate, and a
  mention of a subscribed calendar event still joins it whatever day it names.
- A retried tool call is recognised as a retry even after a later call changed the same
  row. Operations are identified from what was requested, checked inside the transaction
  that would mutate, and a caller may supply its own idempotency key. Replaying an
  earlier move no longer undoes a newer one.
- Email backfill proves a message's identity before writing anything: the stored UID is
  trusted only alongside the UIDVALIDITY it was issued under and a matching `Message-ID`,
  and otherwise the message is searched for by id. A message that cannot be identified
  leaves its archive row untouched and is reported.
- Mail already archived under the bracketed form of its `Message-ID` is recognised as the
  same message, so re-collecting it does not write a second copy.
- Cross-conversation merging no longer merges when it cannot tell. A model call that
  times out, is cut off, or answers "unresolved" leaves the rows separate and says so;
  two rows can be merged later, one row built out of two plans cannot be taken apart.
- The nightly pass is offered a wider set of candidate rows: as well as rows this
  conversation wrote and rows sharing its people, rows are now nominated when the message
  names a person on them or a day they are on. Nominations are presented as questions for
  the model to decide, never as matches, and a truncated candidate list says so.
- `memcal dream` prints progress as the pass runs, instead of nothing until it ends.
- The schedule is one launchd agent instead of two. It wakes at 03:00, at login, and
  every 30 minutes, running the pass when one is owed and otherwise doing what the
  12:00/19:00 catch-up job used to do. `memcal schedule install` retires the old
  `com.memcal.catchup` agent.
- The default model backend is Codex instead of OpenRouter, so a fresh install runs on a
  login the user already has rather than waiting for an API key. An existing
  `MEMCAL_LLM_PROVIDER` is unaffected; `memcal setup` still offers all three.
- `memcal doctor` reports a schedule that is installed but not loaded.
- `memcal setup` records the absolute path of the `claude` or `codex` executable, so a
  CLI outside launchd's PATH still runs from the nightly job.
- Proton Bridge email automatically uses implicit SSL or STARTTLS, matching the mode
  selected in Bridge.
- Temporal benchmarks no longer mutate a tracked score-history ledger during normal runs.
- Event detail describes state in plain English.
- Relevant open questions are reviewed beside their conversations and can be kept,
  amended with a wait condition, answered, or closed through cited, version-checked
  actions; deferred questions retain their wording history.
- Brief, detail, and web views share concise state and change labels, with diagnostics
  kept off the main overview.
- Dream's cross-conversation stage is now named Merge; old recorded `resolve` stages
  still display with the same label.
- Standing is no longer offered to dream or Hermes as a general memory store.
- Legacy standing rows remain readable, but new writes are rejected and normal prompts,
  briefs, and command listings use typed identity, wiki, event, to-do, and question state.
  Retired rows keep their old `S` handles, evidence, and explicit typed destination so
  migration can be safely repeated.
- Dream and live-write instructions, comments, and docstrings are shorter and focused
  on current behavior.
- The GitHub bug-report form now uses a conventional open-source layout.
- Subscribed holiday calendars remain reference information in Calendar.app instead of
  becoming Memcal events.
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


### Fixed

- `Config.secret` family alias no longer prefix-matches siblings (`SLACK_TOKEN` vs `SLACK_USER_ID`).
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
