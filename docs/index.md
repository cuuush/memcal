# Overview

## What is Memcal?

Memcal gives AI agents a memory of your plans, conversations, and commitments.
It brings together sources such as iMessage, email, WhatsApp, and calendars,
then uses a nightly **Dream** pass to update events and other memories as things
change.

Ask your agent “What's my weekend looking like?” and it can draw on plans from
different conversations, such as a possible dinner, a community event, or a
family visit.

Memcal prepares a **brief** for the agent, including the next 30 days by default.
The agent can open a record to inspect its details and evidence, or use tools to
make corrections. During the day, freshness hints flag new activity that may
change an answer; the agent can check the original messages before responding.

## Key components

### Typed stores

Memcal organizes information into these stores:

| Store | What it holds | Example |
|---|---|---|
| **Events** | Dated rows — commitments, others' availability, opportunities, observed past | `〔E42〕 Poker at Jordan's, Sat 8pm — confirmed` |
| **Series** | Recurring rules behind repeated rows | Poker night, every other Friday |
| **To-dos** | Open obligations with ages and wake conditions | `〔T7〕 Send the cabin deposit — due Friday` |
| **Questions** | Uncertainty the agent should ask about, not guess | `〔Q4〕 Is dinner with Jordan Saturday or Sunday?` |
| **Wiki** | Durable facts about people, places, projects, preferences | `jordan` — address, birthday |
| **Archive** | Every raw message, email, and calendar row, full-text indexed | The evidence behind each item above |

During the day, collection runs without model calls. Freshness hints flag activity
that could affect known context and point the agent to the original messages. See [Daytime freshness](architecture/freshness.md).

### The nightly pipeline

Six stages turn raw traffic into the brief:

1. **Observe** — connectors pull new messages, email, and calendar rows into a local
   append-only archive.
2. **Gate** — a deterministic filter (no model) decides what deserves attention.
3. **Propose** — the configured model reads bundled conversations and proposes typed
   changes.
4. **Merge** — deterministic rules join compatible mentions; a model arbitrates only
   real conflicts.
5. **Apply & sweep** — version-checked writes update the stores; cleanup handles stale state,
   and a model reviews the result for duplicates.
6. **Brief** — the current slice of your life renders into a small Markdown snapshot
   the agent receives on every turn.

Details: [Observe](architecture/observe.md) ·
[Gate](architecture/gate.md) · [Dream](architecture/dream.md) ·
[Brief](architecture/brief.md).

## Clients

| Surface | Use it when |
|---|---|
| [CLI](clients/cli.md) | You want the current picture or a direct correction |
| [Web UI](clients/web.md) | You want to review the queue, preview a dream, inspect runs |
| [MCP server](clients/mcp.md) | Any harness: read the brief, connect stdio tools |

## Integrations

Browse all supported integrations in the [Integrations overview](integrations/index.md):
[Hermes](integrations/hermes.md) and [OpenClaw](integrations/openclaw.md) ship native
integrations; anything MCP-speaking works through the generic server.

## Next steps

### Getting started

- [**Quickstart**](quickstart.md) — install, pick a model backend, run the first ingest.
- [**Sources**](sources/index.md) — connect iMessage, WhatsApp, GroupMe, Slack,
  Telegram, Signal, email, calendar.

### Core concepts

- [**Dream**](architecture/dream.md) — how the nightly pass turns traffic into typed state.
- [**Brief**](architecture/brief.md) — the snapshot your agent sees every turn.
- [**Daytime freshness**](architecture/freshness.md) — collection and activity hints between Dream runs.
- [**Identity**](architecture/identity.md) — how memcal decides who is who.

### API & tools

- [**Remember**](api/remember.md) — tell memcal something right now.
- [**Recall**](api/recall.md) — read context, detail, evidence, archive.
- [**Correct**](api/correct.md) — deterministic fixes that survive the night.
- [**MCP server**](api/mcp.md) — the tool surface agents use.

### Deploy

- [**Hosting**](hosting/installation.md) — install, configure, schedule, publish.

## Status

Memcal is experimental but usable. The local archive, typed stores, source
connectors, CLI, web UI, MCP server, Hermes integration, and OpenClaw
integration are implemented. Extraction accuracy remains the main frontier:
the architecture can preserve and reconcile only what the configured model
notices correctly.
