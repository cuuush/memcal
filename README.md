# memcal

**Memory and calendar for AI agents**

[![PyPI](https://img.shields.io/pypi/v/memcal)](https://pypi.org/project/memcal/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-cuuush.github.io-brightgreen)](https://cuuush.github.io/memcal/)

Most memory systems for agents like OpenClaw or Hermes (Mem0, Hindsight) are good at saving things like preferences and observations, but fail at personal assistant tasks, like remembering you have a dentist appointment next Friday. Memcal attempts to bridge this gap by enabling an agent to maintain its own internal calendar of your life. On top of that, a nightly fact-gathering stage will scan sources like iMessage, email, WhatsApp, iCal, and more to automatically update the agent calendar.

With Memcal, you can ask an agent, “What’s my weekend looking like?” and it will remember that your friend is free for dinner Saturday night, that the nonprofit you follow is having a member day, or even that your family is coming into town...

![A vague plan becomes a confirmed event with participants](docs/assets/demo-chat.png)

Memcal prepares a brief of the next 30 days so an agent can answer common questions without a separate lookup. The brief tells you if its token budget leaves events out. Events keep their history and evidence: a past appointment does not automatically mean you attended it.

## Get started

You'll need Python 3.11+, SQLite with FTS5, and a model backend. Codex is the default; Claude Code, Antigravity, Grok, OpenRouter, and OpenAI-compatible APIs are also supported.

```bash
pip install memcal
memcal setup
memcal doctor
memcal brief
```

Setup walks you through your provider and sources. For Codex via OAuth, sign in with `codex login` first; setup lists available models without making an inference call. The default model is `gpt-6-luna` for Codex and `openai/gpt-6-luna` for OpenRouter. Your saved model choice takes precedence. Optional provider fallback, WhatsApp startup
and cleanup, and recent unnamed-sender windows are configurable in Settings; see
[Configuration](docs/hosting/configuration.md).

Memcal is built first for **macOS**, where it can use Messages, Calendar, and launchd scheduling. iMessage reads the local Messages database by default and uses a local bridge to `memcal.app`, which holds Full Disk Access. Calendar and Reminders also use the app bridge, including during `memcal dream`; their consent belongs to Memcal. Your terminal or agent does not need these grants. Use `memcal permissions setup` for guided macOS approval and read checks, or open Settings → Full Disk Access in the web UI. See the [permission helper](docs/hosting/permissions.md) and [iMessage setup](docs/sources/imessage.md) for details. Email, MCP, and optional chat integrations also work on Linux; install Slack support with `pip install 'memcal[slack]'`.

Memcal reads sensitive personal data and sends selected source text to your configured model provider. Read the [privacy notes](https://cuuush.github.io/memcal/privacy/) before connecting real accounts.

For a checkout instead of PyPI:

```bash
git clone https://github.com/cuuush/memcal.git
cd memcal
./install.sh
```

See the [quickstart](https://cuuush.github.io/memcal/quickstart/) for your first ingest and nightly schedule.

## Connect your agent

Memcal works with any MCP client and has native integrations for [Hermes](https://cuuush.github.io/memcal/integrations/hermes/) and [OpenClaw](https://cuuush.github.io/memcal/integrations/openclaw/). To start the MCP server:

```bash
python3 -m memcal.mcp_server
```

See the [MCP guide](https://cuuush.github.io/memcal/clients/mcp/) for client configuration. Agents can also look beyond the brief with tools such as `memcal_list_month(month="2026-10")` and `memcal_list_days(when="2026-01-01", days=365)`.

## Using Memcal

- `memcal dream` collects and processes updates, then shows a summary. Add `--verbose` for details or `--dry-run` for a cost estimate without model calls.
- `memcal review` shows recent changes and open questions. Answer a question using its displayed handle, for example `memcal answer Q12 "your answer"`.
- `memcal open E42` shows a record, its changes, and cited source messages. Use `memcal activity E42` to see newer or uncited conversation traffic.
- `memcal schedule install` installs the macOS scheduler; `memcal schedule status` checks it. Reinstall the schedule after changing runtime locations.
- `memcal disable` stops macOS scheduled Dream and periodic message checks until `memcal enable`. The disabled state survives logins and updates; the app, permissions, data, and manual commands stay available. Closing the web UI alone does not stop scheduling.
- `memcal update` updates a checkout or pip installation from GitHub, upgrades dependencies, migrates the store, and refreshes the brief and installed scheduler. Disabled scheduling stays disabled. Tracked edits or unpublished commits stop a checkout update; unrelated untracked outputs are preserved. Restart running UI and agent sessions afterward.

Run `memcal setup --advanced` for model, brief, Dream, and publishing settings, or `memcal setup --section imessage` to revisit a section. See [configuration](docs/hosting/configuration.md) for provider and model options, including OpenAI-compatible endpoints.

## How it works

Memcal stores events, recurring series, todos, questions, and wiki entries in typed stores. It joins related information across sources, keeps older values in history, and applies updates using the time their evidence was said. The nightly Dream pass collects information, proposes and merges changes, and refreshes the brief. See [Memcal vs. retrieval](https://cuuush.github.io/memcal/architecture/memcal-vs-rag/) for the design.

## Learn more

- [Documentation](https://cuuush.github.io/memcal/) · [Quickstart](https://cuuush.github.io/memcal/quickstart/) · [Sources](https://cuuush.github.io/memcal/sources/)
- [CLI](https://cuuush.github.io/memcal/clients/cli/) · [MCP](https://cuuush.github.io/memcal/clients/mcp/) · [Integrations](https://cuuush.github.io/memcal/integrations/)
- [Contributing](CONTRIBUTING.md) · [Changelog](CHANGELOG.md)
