# Changelog

Notable user-facing changes are recorded here. This project follows [Semantic
Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

### Fixed

- Dream materializes the next occurrence of a standing schedule before reading new
  traffic, so a cancellation updates that occurrence instead of creating duplicate rows.
- Same-day plans still reach Merge when one conversation describes the guest as the
  subject and another describes the user's shared activity.
- A message saying something is ready to pick up at any time produces an open to-do,
  not an event dated on the message timestamp; moved appointments keep the new date.
- Generated series pages are retired when a renamed typed schedule supersedes them,
  while pages containing user-authored facts or questions remain untouched.
- A satisfied wake condition no longer adds a mechanical duplicate when Dream already
  asked about the same linked to-do in better words.
- Merge now treats an identical date and clock time as evidence, so two sources
  describing one appointment no longer land on the brief as separate contradictory rows,
  without joining unrelated same-time appointments that merely share a generic title.
- A conversation whose people are all unresolved is shown the calendar rows its wording
  matches, so it can update them; an email address spelling a participant's name counts
  as a link to that person.
- A person is no longer reported as having no wiki page when one stands under a longer
  or shorter form of their name, which was opening duplicate pages; names sharing only
  their first three letters are not treated as variants.
- Splitting chained identity assumptions in their displayed order restores every handle
  to its original person instead of replaying a merge the user already rejected.
- Codex and Claude Code record the model's reasoning summary and billed reasoning
  tokens, and apply the configured per-model reasoning effort. All three were dropped,
  so `memcal trace` showed no reasoning for any call.
- A pass on a provider that reports no cost says so instead of printing "$0.0000".
- A pass that recovered no longer records itself as failed. Splitting a truncated
  request or re-asking about a skipped bundle went onto `runs.error` alongside timeouts
  and refusals, so `memcal doctor` reported healthy nightly runs as extraction errors.
- `memcal doctor` shows a pass's first failure in full, instead of cutting the joined
  list off mid-word at ninety characters.
- Schedule upgrades preserve retired scripts, retire predecessor agents only after the
  replacement loads, and keep an agent's files if it cannot be unloaded.
- An occurrence belonging to a schedule is no longer published to the calendar on its
  own when its rule is not published. A row carrying a series name with no rule behind
  it — or a rule whose publish was refused, since calendar write access is a separate
  grant — became one standalone calendar event per occurrence, with nothing able to
  take them back.
- Calendar publishing can only be switched on from the store's own `.env`. A setting in
  the checkout's `.env` used to switch it on for every store the process opened.

- A source that has said nothing for longer than the overview window is listed as stale
  rather than disappearing from the table.
- `memcal doctor` reports a collection that started and never finished, instead of
  printing its zero counts as a healthy quiet run.
- Lines the user wrote earlier in an agent session are marked as such wherever they are
  quoted — row detail and both agent search surfaces, not only the Hermes one — so they
  are not read back as independent corroboration.

- The direct chat.db reader now says when it stopped because its page filled, so a
  catch-up can keep going. It is the fallback, used when iMessage is already behind, and
  it was the one reader that never reported it — three collections read exactly 1000
  lines and closed as though the source had run dry. It also counts lines skipped as
  muted or older than the horizon separately from gate rejections, which are three
  different situations that read as "gate passed 0".

- A dream pass that crashes now records that it did. Its run row was left with no finish
  and no error, which is what a pass still running looks like; two such rows are in the
  store. A row an earlier pass left open is named as abandoned on the next run.
- A state review whose reply was cut off no longer applies anything from it. It recorded
  the truncation and then went on to drop the rows the half-reply named.

- `MEMCAL_MATCH_MODEL` now selects the model that decides whether two proposed rows are
  one occasion. It set a value nothing consulted; that stage used the propose model.

- Short display names are no longer refused. A name had to be three characters, which
  rejects complete formal names in Chinese, Japanese and Korean, and everyday ones like
  Jo, Al and Ed; those people's rows filed under a numeral instead. The rule now asks
  whether one character is a whole word in that script rather than counting characters,
  so a single letter of an alphabet is still read as an initial.

- Unnamed GroupMe participants now reach the "name this person" queue. The roster was
  read off the group listing, which is fetched without memberships on purpose, so the
  one call that could queue an unknown handle always received an empty list; the queue
  is filled from the group detail that actually carries a roster.

- An iMessage whose body is only a placeholder is no longer stored as though somebody
  said something. The test was one character wide — an attachment marker — so a message
  that decoded to a bare replacement character survived as a line reading `�` and was
  sent to a model as speech. Any body with nothing visible left in it is now treated as
  no text at all, and existing archived rows are re-derived into the same shape as an
  attachment-only line: text emptied, taken back out of the queue, the row itself kept.
  An emoji or a lone `?` is still a message. GroupMe now uses the same rule, and the
  re-derivation covers every stream rather than iMessage alone, so rows any connector
  stored as a bare placeholder are retired on the next open.
- Token estimates no longer run short of what a request actually costs. One estimator,
  weighted per character class and fitted against the provider's own counts for every
  saved call, now serves packing, brief trimming, and the dry-run and web cost figures;
  the two older rules of thumb it replaces under-counted real traffic by 15 to 18 percent
  every single time, so briefs quietly overran their cap and quoted prices read low.
- Reminders now fire at the intended hour on dates in a different daylight-saving
  regime, instead of an hour early for the whole winter.
- A failure to record what a model call cost is no longer silent: it is kept and
  reported by `memcal doctor`, which now also compares total run cost against the
  generation ledger.
- Emoji reactions of three characters or more can now be pulled into a bundle
  alongside the message they answer, instead of only the shortest ones.
- Muting a chat because the platform muted it is now recorded as an automatic decision
  rather than one made by hand.
- Proton now requests the `Cc` header it reads, so people only ever CC'd are recognized
  as correspondents.
- Merge now reports when a paid arbitration fell back to combining rows locally, and
  distinguishes a reply cut off at its ceiling from a model that declined to answer.
  Its output allowance is sized from the endpoint instead of a fixed 1200 tokens, so a
  model that thinks past that no longer truncates on every conflicted cluster.
- Removed phone numbers and private quoted prose from comments and docstrings, with a
  regression check to keep them out.
- Signing out of WhatsApp and into another account no longer lets reused local message
  IDs collide with or hide the earlier account's archive.
- Proton Bridge login failures now identify stale mailbox credentials and explain how to
  refresh them after a Proton account password change.
- A past event settled to "happened" overnight now records that change in its history,
  so its detail says what it used to be and when it changed rather than showing a state
  nothing accounts for.

### Removed

- The `verify`, `verify_budget` and `pack_cross_reference` settings, which nothing read.
  `MEMCAL_VERIFY_BUDGET` capped a feature that does not exist.

- The calendar no longer records `source.ical.last_count` after each read. Nothing has
  ever read it, so the snapshot size it stored looked like a health signal — "did this
  read come back smaller than usual" — while being a number nothing checked. A test now
  holds every `source.*` marker a source writes to having a reader in the code.

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
