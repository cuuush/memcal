# memcal

**Memory + Calendar for AI agents**

Most memory systems for agents like OpenClaw or Hermes (Mem0, Hindsight) are good at saving things like preferences and observations, but fail at personal assistant tasks, like remembering you have a dentist appointment next Friday. Memcal attempts to bridge this gap by enabling an agent to maintain its own internal calendar of your life. On top of that, a nightly fact-gathering stage will scan sources like iMessage, email, WhatsApp, iCal, and more to automatically update the agent calendar.

With Memcal, you can ask an agent, “What’s my weekend looking like?” and it will remember that your friend is free for dinner Saturday night, that the nonprofit you follow is having a member day, or even that your family is coming into town...

Memcal is experimental. It reads sensitive personal data and sends selected source text to
the model provider you configure. Read [Privacy and safety](#privacy-and-safety) before
connecting real accounts.

## What memcal keeps track of

Memcal maintains an agent calendar with different categories of elements.

- **Events** — appointments, plans, invitations, opportunities, availability, and things
  that already happened.
- **Recurring plans** — the normal schedule plus one-off moves, cancellations, and other
  exceptions.
- **To-dos** — explicit obligations, due dates, and tasks waiting on a person or event.
- **Open questions** — uncertainty that the agent should ask about instead of guessing.
- **People, places, projects, and preferences** — durable facts stored in readable Markdown
  pages.
- **Evidence** — the messages, emails, calendar rows, and change history behind each item.

Memcal also keeps provenance. Every compact item can be opened to see the source messages,
what changed, and which exact model call proposed the change.

## How it works

A memcal pass has six parts:

1. **Observe.** Connectors pull new messages, email, and calendar records into a local
   append-only archive.
2. **Prepare.** A deterministic gate decides which items deserve attention, then related
   records are bundled into conversations. Rejected messages can still be archived for search.
3. **Propose.** The configured model reads those conversations and proposes typed
   changes: add an event, move a date, open a to-do, save a fact, or ask a question.
4. **Merge.** Deterministic rules join compatible mentions across conversations. A model is
   used again only when real evidence conflicts.
5. **Apply and clean up.** Version-checked writes update the typed stores, then deterministic
   cleanup expires or reconnects state whose conditions changed.
6. **Brief.** Memcal renders the current slice of your life into a small Markdown snapshot
   that the agent receives on every turn.


A generated snapshot looks roughly like this:

```markdown
## This week
〔E12〕 Fri Aug 7  Dentist appointment, 2pm — confirmed · Beacon Dental
〔E18〕 Sat Aug 8  Community garden member day — opportunity

## Open
〔T7〕 Send the cabin deposit — due Friday

## Ask about
〔Q4〕 Is dinner with Jordan Saturday or Sunday?

## People and facts
Pages: jordan (address, birthday) · beacon-dental (phone)
```

The handles—`E12`, `T7`, and `Q4`—open the full row, evidence, and history. The brief stays
small enough to include on ordinary agent turns, while deeper detail remains one tool call
away.

Open questions are durable typed state. When a new conversation may affect one, Memcal shows
the question beside that evidence and records an explicit keep, amendment, answer, or closure.
A deferred question can carry a wait condition; loose word overlap alone does not answer it.

## Sources and surfaces

### Inputs

| Source | What memcal reads |
|---|---|
| iMessage | BlueBubbles when configured, with the local macOS Messages database as a fallback |
| WhatsApp | Groups and direct messages from the macOS WhatsApp database |
| GroupMe | Groups and direct messages through API v3 |
| Slack | Direct messages, group DMs and channels through a user token |
| Telegram | Direct messages, groups and channels through the MTProto user API |
| Signal | Direct messages and groups through `signal-cli`, linked as a device |
| Email | IMAP, including Proton Mail Bridge |
| Apple Calendar | Created calendars and subscribed calendar feeds |
| Agent conversations | Inbound user turns from the Hermes and OpenClaw integrations |

### Chat sources

Slack, Telegram and Signal each need something installed before they can run:
`pip install -e '.[chat]'` covers Slack and Telegram, and Signal wants `brew install
signal-cli`. Credentials go in `~/.memcal/.env` — that file is read from any checkout,
unlike a repo-root `.env` — but you never edit it by hand: each `memcal login`
below pastes, validates, and saves for you, asking before it overwrites anything.
`memcal sources` says which of these is still outstanding, and `memcal doctor`
re-tests every saved key.

**Slack.** `memcal login slack` drives the Slack CLI: it creates an app called `memcal`
with read-only user scopes (`im:history`, `mpim:history`, `channels:history`,
`groups:history`, `users:read`, `channels:read`) and installs it to your workspace.
One step stays human — Slack issues the user token only to you: paste the User OAuth
Token (`xoxp-...`, not the `xoxb-` bot token — bots can't see your DMs) when asked.
It is checked with `auth_test` before anything is written. Then
`memcal ingest slack --limit 20`.

**Telegram.** `memcal login telegram` asks for the `api_id`/`api_hash` pair from
https://my.telegram.org (API development tools), saves them, then asks for your phone
number in international format (e.g. `+15550102030`), the code Telegram sends you,
and your 2FA password if you have one. The session lands at
`~/.memcal/telegram.session` and grants full account access, so guard it like a
password. Then `memcal ingest telegram --limit 20`.

**Signal.** `memcal login signal` prints a QR code — scan it from Signal on your phone
(Settings → Linked devices → +). No token, nothing leaves the machine; with several
linked accounts it asks which one to use and saves that choice. Then
`memcal ingest signal --limit 50` — small on purpose, because `receive` acks messages
off the server queue and a crash before they archive loses them.

Discord is deliberately absent. Its API will not hand a human's direct messages to any
token that does not violate its terms, and server channels alone did not earn a
connector.

Partiful invitations are recognized through Apple Calendar data, provided you subscribe to
the Partiful calendar. Memcal keeps their RSVP links and distinguishes an unanswered
invitation from a confirmed plan.

Custom sources can live in `~/.memcal/plugins/` or register through the `memcal.sources`
Python entry-point group. See [`examples/plugins/rss.py`](examples/plugins/rss.py) for a
small example.

### Outputs

- A fresh agent-context snapshot.
- Read and typed-write tools over MCP or a native harness integration.
- A local CLI and web UI.
- Optional publishing to a dedicated Apple Calendar.
- Optional publishing of due tasks to Apple Reminders.

Publishing to Calendar or Reminders is off by default.

## Quick start

### Requirements

- Python 3.11 or newer.
- SQLite with FTS5.
- One model backend: Codex (the default), Claude Code, or OpenRouter.
- macOS for the local Messages, WhatsApp, Calendar, and Reminders integrations.

The current Python runtime has no third-party dependencies or build step.

### Install

```bash
git clone https://github.com/cuuush/memcal.git
cd memcal
./install.sh
```

The installer creates a `memcal` launcher that points at this checkout, initializes
`~/.memcal` on the first run, imports available contacts, and writes the first brief.

Choose a model backend:

```bash
memcal setup
```

The setup flow asks for the provider and model, then saves only the owned settings in
`~/.memcal/.env`. Existing source credentials and hand-written settings are preserved.

Check the result:

```bash
memcal doctor
memcal brief
memcal ui
```

The web UI listens on `http://127.0.0.1:8765`.

Its **Settings** tab is the full list of what memcal can be told: every `MEMCAL_*`
setting it reads, what each one does, the default, and which file the current value came
from. Saving writes `~/.memcal/.env` — leaving your hand-written lines alone — and
applies to the running process immediately, so the next pass uses it without a restart.
Clearing a field unsets the key and restores the built-in default. Source credentials can
be set there too; the page reports that a credential is present and never reads one back
out. If a `.env` beside your working directory already sets the same key, the tab says
so, because that file still wins the next time memcal starts. The tab also reports what
each source still needs and what the nightly agent is doing.

Nothing there has to be typed from memory. A model field opens onto the models memcal
can price for the provider you have chosen — named the way that provider names them,
with their rates — plus any this store has already run; picking a provider re-asks
before you save, so the list follows the choice you are making. An executable field
offers the absolute path `which` finds, which is what the nightly agent needs. A
calendar field offers the calendars memcal has read.

The model fields are the one place that does restrict. A model belonging to a different
provider than the one selected is refused, because it does not fail one request — it
fails every request in a pass, which is how a run once read 74 conversations and wrote
nothing. Choosing a provider moves any model field naming another provider's model onto
the new provider's default and tells you which and why, and one control sets the propose,
sweep and merge models together. A model memcal simply does not recognise still saves:
these lists lag every release, so an unfamiliar name is a new model at least as often as
it is a mistake. Every other suggestion is a shortcut, never a restriction.

## Choose a model backend

| Backend | Default model | Authentication |
|---|---|---|
| Codex programmatic mode (default) | `gpt-5.6-luna` | Existing Codex login |
| Claude Code programmatic mode | `claude-sonnet-5` | Existing Claude Code login |
| Antigravity programmatic mode | `gemini-3.8-flash-high` | Existing Antigravity login |
| OpenRouter | `openai/gpt-5.6-luna` | OpenRouter API key |

Codex is the default because it runs on a login you already have; OpenRouter needs an
API key before anything works.

The three CLI backends run as one-shot structured completions. Claude Code uses print mode
without persistent sessions or tools. Codex uses ephemeral `exec` sessions with a read-only
sandbox and approvals disabled. Antigravity (`agy`) uses print mode with the sandbox on and
slash commands disabled, and asks for structured output with `--json-schema`. All three
replay staged extraction turns explicitly, so a pass does not depend on hidden session
history.

Antigravity returns `SUCCESS` with an empty response often enough to notice — roughly one
call in five in a small sample. Memcal treats that as a failed call rather than an empty
answer, so it shows up in a pass's `failed` count and the bundle stays available; expect a
dream pass on this backend to lose some calls that way.

Antigravity's print mode wraps every prompt in its own agent preamble, so a single
extraction call bills tens of thousands more input tokens than the same call through
Codex or OpenRouter. On a subscription that is quota rather than money, but it is the
reason to prefer a flash model here.

Antigravity model names carry their own reasoning budget — `gemini-3.8-flash-high` and
`gemini-3.8-flash-low` are separate selections — so memcal leaves `--effort` alone for
those and sets it only for a model that does not state one. `agy models` lists what the
login can reach.

## Retry a pass that failed

A dream pass claims the spooled lines it reads, so a pass that broke part-way through
leaves a queue that no longer holds what it read. The Runs tab says how each pass ended —
ok, partial, failed, running, priced only — and offers a Retry on the ones worth
re-reading; the Dream tab says the same above the button that spends money.

Retrying puts back exactly the lines that pass claimed and dreams over them again with
whatever provider and model are configured **now**, so fix the setting that broke it
first. A pass refused on its first call claimed nothing at all — its traffic was never
removed from the queue — and the page says so, rather than leaving the retry looking as
though it did nothing.

```bash
memcal dream --retry 29
```

The setup flow can also be scripted:

```bash
memcal setup --provider codex
memcal setup --provider claude-code
memcal setup --provider antigravity
memcal setup --provider codex --model gpt-5.6-luna
memcal setup --provider openrouter --api-key "sk-or-..."
```

Authenticate the selected CLI first — `claude auth login`, `codex login`, or a signed-in
`agy` session. Memcal checks
that the command exists; the CLI itself reports authentication trouble on the first real
completion.

## Connect an agent

### OpenClaw

The native OpenClaw integration injects a newly rendered snapshot before every prompt,
prefetches relevant wiki pages, and archives each inbound user turn exactly once. The setup
command links the plugin and registers memcal’s stdio MCP server:

```bash
memcal openclaw setup
openclaw gateway restart
memcal openclaw status
```

The plugin points at this checkout, so code changes do not require reinstalling it.

### Hermes

Hermes uses the native memory-provider integration:

```bash
ln -s /path/to/memcal/integrations/hermes/memcal ~/.hermes/plugins/memcal
hermes memory setup
```

The Hermes and OpenClaw integrations expose the same basic behavior:

- a fresh snapshot on every turn;
- relevant pages when a person, place, or project is mentioned;
- source and conversation lookup;
- deterministic typed writes for events, to-dos, facts, aliases, and answers;
- archival of user turns without ingesting assistant replies, tool output, or injected
  snapshots as if they were user facts.

Another harness can use the same boundary by reading `brief.md` and connecting to:

```bash
python3 -m memcal.mcp_server
```

## Feed memcal

See which connectors are available:

```bash
memcal sources
```

Collect everything currently configured:

```bash
memcal ingest all
```

Preview the size and estimated cost of an extraction pass, then run it:

```bash
memcal dream --dry-run
memcal dream
```

To collect and update automatically, install the nightly launchd job:

```bash
memcal schedule install
memcal schedule status
```

One background item, `memcal-nightly`, listed under that name in System Settings →
Login Items & Extensions. launchd starts it at 03:00, at login, and every 30 minutes —
and an interval that elapsed while the machine slept fires on wake. Each time, the script
asks whether the day's pass is owed:

- **owed** — collect from every source, then run the extraction pass. So a laptop that
  was shut or asleep at 03:00 runs one catch-up pass when the lid opens.
- **not owed** — collect only from sources that are behind *and* reachable right now.
  Cheap, and it cannot cost a model call, which is why it is safe on every wake-up.

```bash
memcal schedule          # includes when the pass last ran, and whether one is owed
memcal schedule run      # run it now regardless
memcal schedule due      # just the answer
```

### Mail memcal has not read yet

Every message inside the folders and time range you configured has its body fetched and
archived before anything decides how relevant it is. Automatic relevance sets a
*priority*, not a verdict: mail from mailing lists, retailers and no-reply addresses is
queued as low priority, read after everything else and in bounded batches, and it stays
searchable and available to the assistant the whole time. The only thing that keeps a
sender out entirely is you saying so — `memcal senders <address> ignore`, "I don't care
about this" to the agent, or the block button in the web queue.

```bash
memcal mail                        # what is queued, by priority, and what has no body
memcal mail --backfill             # what a recovery run would cover — reports only
memcal mail --backfill --apply     # actually re-fetch, updating rows in place
```

The backfill exists because a row archived before this change holds a subject line and
nothing else, and no schema migration can turn that into evidence. It is resumable, it
stops at `--limit`, and it never deletes, moves, or marks anything in your mailbox.

Mail is grouped into conversations by `Message-ID`, `In-Reply-To` and `References`, so a
reply joins the thread it answers and two unrelated messages from one sender stay two
conversations.

## Use it day to day

The main experience is conversational:

> What’s my weekend looking like?
>
> When am I seeing Jordan next?
>
> Is anything still unresolved for the cabin trip?
>
> Move poker to Sunday and remind me to bring cash.

The CLI exposes the same state directly:

```bash
# Read the current picture
memcal brief
memcal week
memcal month
memcal todos

# Open a handle from the brief
memcal E286

# Search original source material
memcal search "dinner next week"

# Tell memcal something immediately
memcal remember "Jordan is free for dinner Saturday night"

# Make deterministic corrections
memcal add "Game night" saturday --time "8pm" --who Jordan
memcal status E286 confirmed
memcal todo "Send the reservation deposit"
memcal done T7
# Durable facts belong on wiki pages; open obligations belong in to-dos.
memcal page travel "preferred flight time" "morning" --section preferences
```

### Who is who

Names are keys, so two spellings of one name are two people until something says
otherwise, and an opaque platform id is nobody. Contacts joins what it can; `memcal who`
is for the rest.

```bash
memcal who                    # what is unnamed, what has been assumed, what is in doubt
memcal who <handle> "<name>"  # name one by hand
memcal who --adopt            # take every name a platform already gave, free
memcal who --resolve          # one model call over the whole picture
memcal who --split 3          # no: undo an assumed merge, handle for handle
memcal who --confirm 3        # yes: stop listing a merge, or act on a doubt
```

`--resolve` is the only one that costs anything. It sends every unnamed handle and every
known person in one request, so a handle is matched against the whole roster rather than
judged alone.

What it concludes is assumed, not decided: merges take effect immediately, are listed by
number, and are reversible. It can also decline to decide, which records a question for
you rather than a guess. Contacts outranks all of it — a model guess cannot rename an
address-book card, and two separate cards stay two people.

Use `memcal --help` for the command list, `memcal help <command>` for one command, and
`memcal completion zsh` or `memcal completion bash` for shell completion.

## Tools available to an agent

The MCP surface provides compact reads and explicit writes:

| Job | Tools |
|---|---|
| Current context | `memcal_brief`, `memcal_list_days`, `memcal_list_month` |
| Detail and evidence | `memcal_open`, `memcal_open_page`, `memcal_source`, `memcal_conversation` |
| Archive search | `memcal_search_archive` |
| Events | `memcal_add`, `memcal_update`, `memcal_merge`, `memcal_drop` |
| Recurrence | `memcal_schedule`, `memcal_move_once` |
| Tasks and facts | `memcal_todo`, `memcal_note`, `memcal_alias`, `memcal_answer` |

Write tools do not call a model. If the user says an event moved, `memcal_update` changes
the typed row, records the old value in history, and updates the next brief. Memcal never
infers that a to-do is complete; it waits for an explicit completion or asks.

## Apple Calendar and Reminders

Memcal’s private event store is not automatically your real calendar. This separation keeps
an agent from creating, moving, or deleting live calendar data just because it inferred a
plan from conversation.

To publish confirmed memcal events to a dedicated Apple Calendar, opt in explicitly:

```bash
# ~/.memcal/.env
MEMCAL_PUBLISH_CALENDAR=memcal

memcal ical setup
```

To publish due tasks to Apple Reminders, configure it separately:

```bash
# ~/.memcal/.env
MEMCAL_PUBLISH_REMINDERS=memcal

memcal reminders setup --yes
```

Both destinations are also editable from the web UI's Settings tab, under *Writing back
out*; like every outward-write setting they are scoped to this store's `.env` alone.

Publishing is idempotent: changes update the matching item, withdrawn rows retract it, and
disabled publishing performs no external writes. Calendar and Reminders require separate
macOS permissions.

## Storage, evidence, and recovery

By default, everything lives under `~/.memcal/`:

```text
~/.memcal/
├── memcal.db       typed state, archive, provenance, and full-text search
├── brief.md        the compact snapshot an agent sees
├── calls/          prompts, replies, usage, and model-call traces
├── plugins/        optional custom source plugins
└── wiki/           readable pages for people, places, projects, and preferences
```

Set `MEMCAL_HOME` to use another directory.

Source messages are append-only. Typed rows can be corrected, merged, or withdrawn, but
their evidence and value history remain available. Model calls are also saved locally, so
`memcal trace` can show what was sent, what came back, and which generation wrote a row.

When the assistant changes something through a typed tool, the change and a record of the
operation are written together: the row it targeted, what actually changed, the message
you were looking at when you said it, and the version of the row it was acting on. The
nightly pass is shown those records beside the conversation that produced them, so
re-reading the sentence that moved an event does not create a second one. A caller that
cannot say which of your messages caused a tool call still works, and the record says
that is why there is no source on it.

A correction you make during the day is not undone by older evidence collected later.
Precedence is decided per field and on when the evidence was *said*: a message written
before your correction cannot revise the field you corrected, while genuinely newer
evidence still can. Nothing becomes permanently unchangeable.

Some observations cannot be placed yet — "that's cancelled", naming no plan and no day.
Those are kept with their evidence rather than dropped or written as an invented event,
retried as later traffic arrives, and turned into a question when more than one plan
could be the one meant.

Durable facts belong on wiki pages. Older installations may still contain legacy standing
rows: their `S` handles remain readable for recovery, but new standing writes are rejected.

## Privacy and safety

Memcal is built for personal data, so its defaults are deliberately conservative:

- Data is stored locally in SQLite and Markdown.
- Only items admitted by the deterministic gate are sent for model extraction.
- Claude Code and Codex runs are stateless and cannot write through their model tools.
- Real Calendar and Reminders publishing is disabled until explicitly configured.
- The web UI binds to loopback, not the public network.
- Test fixtures use fictional people and metadata.

You should still treat `~/.memcal`, `.env` files, model-call traces, and benchmark output as
sensitive. Keep them out of version control and protect them with normal filesystem access
controls. Local Messages and calendar connectors may require Full Disk Access or macOS
Calendar and Reminders permissions.

## Benchmarking

The temporal benchmark follows a synthetic life across several fake days. It can separate a
model-extraction miss from a deterministic merge or storage bug:

```bash
# Free deterministic pipeline
python3 tools/benchmark_temporal.py --layer integration

# Live configured model
python3 tools/benchmark_temporal.py --layer model

# Just the collision corpus: daytime typed writes meeting the nightly pass
python3 tools/benchmark_temporal.py --suite collision --variants 4
python3 tools/benchmark_temporal.py --suite collision --case f3   # one family only
```

The `collision` suite replays ordered, timed operations — a message arriving, a typed
tool call, a nightly pass, a retry — and grades the store at each checkpoint rather than
only at the end. Each message distinguishes when it was written from when it arrived, so
"an email written on Monday and collected on Tuesday" is expressible. Results are
reported by category — duplicate creation, false merges, lost corrections, missed
cancellations, missing evidence — because a matcher broad enough to stop duplicates is
the same matcher that merges two real appointments, and one number cannot show both
moving. `--variants N` retells each scenario N more times — the same story said
differently, with unrelated chatter mixed in, delivered twice, or arriving out of order —
and prints the seed and first failing checkpoint of anything that breaks. It defaults to
1, and to 0 under `--layer model`, where every retelling is a paid model run; six is the
maximum. `tools/BENCHMARK.md` documents every suite and knob.

The live model layer prints its provider, model, fake day, dream stage, dispatched requests,
and completed bundle count. If a CLI model call stays quiet, a flushed heartbeat reports the
active stage every 15 seconds so a slow answer does not look like a hung process.

Benchmark scratch stores never use the live `~/.memcal` directory. Live model evaluation can
cost money or consume subscription capacity; it is not part of the normal development loop.
Benchmark runs print current results and do not update a tracked score ledger.

## Development

Run focused tests while iterating, followed by the full suite:

```bash
python3 -m unittest discover -s tests
```

Run the integration benchmark when a change affects ingest, Merge, typed storage, dream
application, or brief behavior covered by its scenarios. Run the live model layer only when
you are deliberately evaluating extraction or prompt behavior.

Repository guidance lives in [`AGENTS.md`](AGENTS.md), with the contributor workflow in
[`CONTRIBUTING.md`](CONTRIBUTING.md).

## Status

Memcal is experimental but usable. The local archive, typed stores, source connectors, CLI,
web UI, MCP server, Hermes integration, and OpenClaw integration are implemented. Extraction
accuracy remains the main frontier: the architecture can preserve and reconcile only what
the configured model notices correctly.
