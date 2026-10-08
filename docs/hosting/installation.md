# Installation

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
first brief. Code edits take effect immediately — no reinstall.

Chat sources need extra libraries (see each [source](../sources/index.md) page),
installable together:

```bash
pip install "memcal[chat]"
```

`signal-cli` is not a pip package — it is a JVM program, installed separately
(see [Signal](../sources/signal.md)).

Docs tooling is its own extra, only needed to build this site:

```bash
pip install "memcal[docs]"
```

## First run

```bash
memcal setup        # provider and model, saved to ~/.memcal/.env
memcal doctor       # what's wrong, and what to type about it
memcal brief        # the first snapshot
memcal ui           # the web UI on 127.0.0.1:8765
```

## Updating

```bash
memcal update
```

For a checkout, this fetches its configured upstream, advances it with a fast-forward,
and upgrades dependencies and the existing launcher with the same Python interpreter.
Local edits, unpublished commits, or a development worktree stop the update with an
explanation. A default branch missing its upstream is reconnected automatically;
other branches require explicit tracking. It does not run ingestion or change your
saved provider settings.

For a pip installation, it installs the latest code directly from
`https://github.com/cuuush/memcal` using the Python that runs the command; Git must
be on PATH. Existing Slack and Telegram extras are upgraded too.

Both paths run the newly installed code to migrate the existing store and refresh
its saved brief. On macOS, an installed scheduler is regenerated and reloaded,
including the app wrapper, while preserving its scheduled time and pending retry.
An update does not enable scheduling on a store that did not have it. Use
`memcal --home /path/to/store update` to select the store to refresh.

Linked Hermes and OpenClaw plugins and MCP servers use the updated code on their
next start. Restart running UI or agent sessions afterward; for OpenClaw, run
`openclaw gateway restart`.
The update command can repair missing application dependencies because it loads
before the rest of Memcal.

If your installation predates `memcal update`, bootstrap it once with `git pull
--ff-only` and `./install.sh --no-init` in the installed checkout, or
`python3 -m pip install --upgrade memcal` for a pip installation.

Next: [Configuration](configuration.md) for every knob, [Scheduling](scheduling.md)
for the nightly job.
