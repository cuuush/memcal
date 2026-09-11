# Quickstart

## Requirements

- Python 3.11 or newer.
- SQLite with FTS5.
- One model backend: Codex (the default), Claude Code, Antigravity, or OpenRouter.
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

The setup flow asks for the provider and model, then saves only the owned settings in
`~/.memcal/.env`. Existing source credentials and hand-written settings are preserved.

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

To collect and update automatically, install the nightly job:

```bash
memcal schedule install
```
