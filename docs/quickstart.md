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
