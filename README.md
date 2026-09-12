# memcal

**Memory + Calendar for AI agents**

Most memory systems for agents like OpenClaw or Hermes (Mem0, Hindsight) are good at saving things like preferences and observations, but fail at personal assistant tasks, like remembering you have a dentist appointment next Friday. Memcal attempts to bridge this gap by enabling an agent to maintain its own internal calendar of your life. On top of that, a nightly fact-gathering stage will scan sources like iMessage, email, WhatsApp, iCal, and more to automatically update the agent calendar.

With Memcal, you can ask an agent, “What’s my weekend looking like?” and it will remember that your friend is free for dinner Saturday night, that the nonprofit you follow is having a member day, or even that your family is coming into town...

## Getting started

Requires Python 3.11+, SQLite with FTS5, and one model backend (Codex by
default; Claude Code, Antigravity, or OpenRouter also work).

```bash
git clone https://github.com/cuuush/memcal.git
cd memcal
./install.sh
memcal setup
memcal doctor
memcal brief
```

Memcal reads sensitive personal data and sends selected source text to the
model provider you configure — read the [privacy notes](docs/privacy.md)
before connecting real accounts.

## Learn more

Full guides live at **https://cuuush.github.io/memcal/**:

- [Quickstart](docs/quickstart.md) — install, first ingest, nightly job
- [Sources](docs/sources/index.md) — iMessage, WhatsApp, Slack, Telegram, Signal, email, calendar
- [CLI](docs/clients/cli.md) and [CLI reference](docs/api/cli.md) — every command
- [Integrations](docs/integrations/index.md) — Hermes, OpenClaw, any MCP harness
- [Hosting](docs/hosting/installation.md) — configuration, scheduling, publishing
- [Evaluation](docs/evaluation/index.md) — how memory quality is measured

Contributing: [CONTRIBUTING.md](CONTRIBUTING.md). Notable changes: [CHANGELOG.md](CHANGELOG.md).
