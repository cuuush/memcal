# memcal

**Agent calendar + nightly reconcile → brief in context**

[![PyPI](https://img.shields.io/pypi/v/memcal)](https://pypi.org/project/memcal/)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Docs](https://img.shields.io/badge/docs-cuuush.github.io-brightgreen)](https://cuuush.github.io/memcal/)

Most agent memory (Mem0, Hindsight, …) is great at preferences and RAG. It fails at personal-assistant questions like “what’s my weekend looking like?” Those need a **calendar of your life**, not another embedding store.

Memcal maintains typed **events / todos / questions / wiki**, fed from the streams you already have (Slack, Messages, email, calendars, chat). A **nightly reconcile** merges the day; every turn your agent gets a small **brief** in context, with tools for depth and corrections.

<!-- Demo: synthetic brief screenshot — drop at docs/assets/demo-brief.png and uncomment:
![Example brief (synthetic)](docs/assets/demo-brief.png)
-->

## Why not just Mem0 / Hindsight?

- **Write-time recency** — when a fact updates, the old value moves to history; read time isn’t a relevance fight ([memcal vs retrieval](https://cuuush.github.io/memcal/architecture/memcal-vs-rag/))
- **Brief in the prompt** — “I’m bored” / “what’s this week” answers from context, not a lucky tool call
- **Typed stores** — events, series, todos, questions, wiki (not a bag of memories)
- **Multi-stream join** — Slack + email + calendar + agent chat resolve to one underlying thing
- **Nightly dream pass** — observe → gate → propose → merge → brief, on a schedule

## Install

Requires Python 3.11+, SQLite with FTS5, and one model backend (Codex by default; Claude Code, Antigravity, or OpenRouter also work).

```bash
pip install memcal
memcal setup
memcal doctor
memcal brief
```

Linux demo path: connect [Slack](https://cuuush.github.io/memcal/sources/slack/) (`pip install 'memcal[slack]'`) and talk to the agent over MCP. macOS also supports Messages, EventKit, and launchd scheduling.

From source (alternate):

```bash
git clone https://github.com/cuuush/memcal.git
cd memcal
./install.sh
```

Memcal reads sensitive personal data and sends selected source text to the model provider you configure — read the [privacy notes](https://cuuush.github.io/memcal/privacy/) before connecting real accounts.

## MCP (any harness)

```bash
python3 -m memcal.mcp_server
```

Native plugins: [Hermes](https://cuuush.github.io/memcal/integrations/hermes/) · [OpenClaw](https://cuuush.github.io/memcal/integrations/openclaw/)

## Learn more

Full guides: **https://cuuush.github.io/memcal/**

- [Quickstart](https://cuuush.github.io/memcal/quickstart/) — install, first ingest, nightly job
- [Sources](https://cuuush.github.io/memcal/sources/) — Slack, Telegram, WhatsApp, iMessage, Signal, email, calendar, …
- [CLI](https://cuuush.github.io/memcal/clients/cli/) · [MCP](https://cuuush.github.io/memcal/clients/mcp/) · [Integrations](https://cuuush.github.io/memcal/integrations/)
- [Evaluation](https://cuuush.github.io/memcal/evaluation/) — how memory quality is measured

Contributing: [CONTRIBUTING.md](CONTRIBUTING.md). Notable changes: [CHANGELOG.md](CHANGELOG.md).

**Status:** experimental but usable. Extraction accuracy is the frontier.
