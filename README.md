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
model provider you configure — read the [privacy notes](https://cuuush.github.io/memcal/privacy/)
before connecting real accounts.

## Learn more

Full guides live at **https://cuuush.github.io/memcal/**:

- [Quickstart](https://cuuush.github.io/memcal/quickstart/) — install, first ingest, nightly job
- [Sources](https://cuuush.github.io/memcal/sources/) — iMessage, WhatsApp, Slack, Telegram, Signal, email, calendar
- [CLI](https://cuuush.github.io/memcal/clients/cli/) and [CLI reference](https://cuuush.github.io/memcal/api/cli/) — every command
- [Integrations](https://cuuush.github.io/memcal/integrations/) — Hermes, OpenClaw, any MCP harness
- [Hosting](https://cuuush.github.io/memcal/hosting/installation/) — configuration, scheduling, publishing
- [Evaluation](https://cuuush.github.io/memcal/evaluation/) — how memory quality is measured

Contributing: [CONTRIBUTING.md](CONTRIBUTING.md). Notable changes: [CHANGELOG.md](CHANGELOG.md).
