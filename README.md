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
| Email | IMAP, including Proton Mail Bridge |
| Apple Calendar | Created calendars and subscribed calendar feeds |
| Agent conversations | Inbound user turns from the Hermes and OpenClaw integrations |

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
- One model backend: OpenRouter, Claude Code, or Codex.
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

## Choose a model backend

| Backend | Default model | Authentication |
|---|---|---|
| OpenRouter | `openai/gpt-5.6-luna` | OpenRouter API key |
| Claude Code programmatic mode | `claude-sonnet-5` | Existing Claude Code login |
| Codex programmatic mode | `gpt-5.6-luna` | Existing Codex login |

The two CLI backends run as one-shot structured completions. Claude Code uses print mode
without persistent sessions or tools. Codex uses ephemeral `exec` sessions with a read-only
sandbox and approvals disabled. Both replay staged extraction turns explicitly, so a pass
does not depend on hidden session history.

The setup flow can also be scripted:

```bash
memcal setup --provider claude-code
memcal setup --provider codex
memcal setup --provider codex --model gpt-5.6-luna
memcal setup --provider openrouter --api-key "sk-or-..."
```

Authenticate the selected CLI first with `claude auth login` or `codex login`. Memcal checks
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

The schedule also includes catch-up opportunities for sources that were unavailable during
the main nightly run.

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
```

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
