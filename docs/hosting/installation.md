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

The default updater targets a published stable PyPI version, or its matching Git
release tag for checkout installations. Development branch updates require the
explicit `--dev` option. Stop running UI and agent sessions before updating.

Updates stage-check the release, take a private store snapshot, refresh the store
and managed integrations, and preserve recovery information on failure. Local
tracked edits and development worktrees are refused; local integration edits are
not overwritten. Restart your hosts after a successful update.

See [Updates and recovery](../updates.md) for exact behavior, backup privacy,
limitations and non-destructive recovery instructions. Use
`memcal --home /path/to/store update` to select a store.

Next: [Configuration](configuration.md) for every knob, [Scheduling](scheduling.md)
for the nightly job.
