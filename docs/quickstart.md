# Quickstart

## Requirements

- Python 3.11 or newer.
- SQLite with FTS5.
- One model backend: Codex (the default), Claude Code, Antigravity, Grok, OpenRouter, or an OpenAI-compatible API.
- macOS for the local Messages, WhatsApp, Calendar, and Reminders integrations.

## Install

```bash
git clone https://github.com/cuuush/memcal.git
cd memcal
./install.sh
```

The installer puts a `memcal` launcher on your PATH that points at this checkout,
initializes `~/.memcal` on the first run, imports available contacts, and writes the
first brief.

Chat sources need extra libraries (see each source page), installable together:

```bash
pip install "memcal[chat]"
```

`signal-cli` is not a pip package — it is a JVM program, installed separately
(see [Signal](sources/signal.md)).

## Choose a model backend

```bash
memcal setup
```

Setup asks for a provider and model, then saves its settings in `~/.memcal/.env`.
Existing source credentials and hand-written settings are preserved.
Codex defaults to `gpt-6-luna`; OpenRouter defaults to `openai/gpt-6-luna`. An
explicit saved model remains selected. See [Configuration](hosting/configuration.md)
for other providers, source setup, and advanced settings.

Check the result:

```bash
memcal doctor
memcal brief
```

## Approve macOS sources

Messages, WhatsApp, and Contacts use a local bridge to `memcal.app`, which owns
Full Disk Access. Calendar uses the same app for Automation and Calendar data
access; your terminal or agent does not need these grants.

```bash
memcal permissions setup    # prepare the app and guide Full Disk Access approval
memcal ical setup           # request Calendar access from the Memcal app
```

Enable `memcal.app` when prompted. Calendar publishing also needs Calendar data
access; Reminders publishing has its own setup. See the
[permission helper](hosting/permissions.md), [Calendar](sources/calendar.md), and
[Publishing](hosting/publishing.md).

## First ingest

See which connectors are available, then pull from one source with a small limit
before collecting everything:

```bash
memcal sources
memcal ingest slack --limit 20
memcal ingest all
```

Preview the size and estimated cost of an extraction pass, then run it:

```bash
memcal dream --dry-run
memcal dream
```

On macOS, install the nightly job to collect and update automatically:

```bash
memcal schedule install
```

On Linux, this command prints cron instructions instead of installing a launchd job.

The macOS job also checks for new source records every five minutes by default.
Those periodic checks make no model calls and leave WhatsApp closed unless you
explicitly enable background startup. Closing the web UI does not stop the job.

To stop macOS scheduled work and resume it later:

```bash
memcal disable             # stops nightly Dream and periodic checks
memcal schedule status     # confirms scheduling is disabled
memcal enable              # resumes the installed job
```

The disabled state persists across logins and updates. Your data, app permissions,
and manual commands remain available. See [Scheduling](hosting/scheduling.md).

## Connect an agent

For OpenClaw, with the OpenClaw CLI already installed:

```bash
memcal openclaw setup
openclaw gateway restart
```

For an existing Hermes installation:

```bash
mkdir -p ~/.hermes/plugins
ln -s /path/to/memcal/integrations/hermes/memcal ~/.hermes/plugins/memcal
hermes memory setup
```

For another harness, connect the MCP server with `python3 -m memcal.mcp_server`.
See [MCP setup](clients/mcp.md) and [Integrations](integrations/index.md).

## Next steps

- [Architecture](architecture/index.md) — observe, gate, dream, brief.
- [Remember](api/remember.md), [Recall](api/recall.md), [Correct](api/correct.md) — the tool surface.
- [Sources](sources/index.md) — every input memcal reads.
