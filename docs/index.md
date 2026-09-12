# Overview

## Why memcal?

**Memory + Calendar for AI agents.**

Most memory systems for agents like OpenClaw or Hermes (Mem0, Hindsight) are good at saving things like preferences and observations, but fail at personal assistant tasks, like remembering you have a dentist appointment next Friday. Memcal attempts to bridge this gap by enabling an agent to maintain its own internal calendar of your life. On top of that, a nightly fact-gathering stage will scan sources like iMessage, email, WhatsApp, iCal, and more to automatically update the agent calendar.

With Memcal, you can ask an agent, “What’s my weekend looking like?” and it will remember that your friend is free for dinner Saturday night, that the nonprofit you follow is having a member day, or even that your family is coming into town...

**The problem is harder than it looks:**

- **Recency is decided at write time, not read time.** If Jordan texts a new poker
  address, the old address moves to history when the update lands. At read time there
  is one value and nothing to weigh — relevance scoring never gets a chance to drown
  out the timestamp the way it does when a year-old email answers for last week.
- **Implicit questions need context, not retrieval.** "I'm bored," "what's going on
  this week," "should I invite anyone" are only answerable from what is already
  sitting in the prompt. No tool call finds those answers.
- **One life, many streams.** A text, an email, a group-chat line, and something said
  to the agent all resolve to the same underlying thing. They differ only in where
  they came from — so memcal joins them structurally instead of storing five copies.
- **Extraction is slot-filling, never "find memories."** The model is asked whether
  anything changes a calendar row, a to-do, or a named fact. Most days the answer is
  no, which is correct — and keeps junk ("i love you" → "Harper loves Casey") out of
  memory.

Memcal solves these with an agent calendar that lives in context, fed by the streams
you already get, reconciled every night.

## What memcal does

**Your agent** reads a small rendered snapshot (the **brief**) on every turn, opens
detail and evidence through typed handles, and writes corrections back through typed
tools — all against its dedicated **memory store**.

Overnight, the **dream** pass reads the day's new traffic and proposes typed updates:
new events, moved dates, opened to-dos, saved facts, questions to ask. Deterministic
rules merge them; a model is consulted again only for genuine conflicts.

## Key components

### Typed stores

Memcal keeps one world model with a fixed shape:

| Store | What it holds | Example |
|---|---|---|
| **Events** | Dated rows — commitments, others' availability, opportunities, observed past | `〔E42〕 Poker at Jordan's, Sat 8pm — confirmed` |
| **Series** | Recurring rules behind repeated rows | Poker night, every other Friday |
| **To-dos** | Open obligations with ages and wake conditions | `〔T7〕 Send the cabin deposit — due Friday` |
| **Questions** | Uncertainty the agent should ask about, not guess | `〔Q4〕 Is dinner with Jordan Saturday or Sunday?` |
| **Wiki** | Durable facts about people, places, projects, preferences | `jordan` — address, birthday |
| **Archive** | Every raw message, email, and calendar row, full-text indexed | The evidence behind each item above |

During the day, collection is cheap and model-free; freshness hints flag activity
that could affect known context, with a short route to the original messages when an
answer needs checking. See [Daytime freshness](architecture/freshness.md).

### The nightly pipeline

Six stages turn raw traffic into the brief:

1. **Observe** — connectors pull new messages, email, and calendar rows into a local
   append-only archive.
2. **Gate** — a deterministic filter (no model) decides what deserves attention.
3. **Propose** — the configured model reads bundled conversations and proposes typed
   changes.
4. **Merge** — deterministic rules join compatible mentions; a model arbitrates only
   real conflicts.
5. **Apply & sweep** — version-checked writes land, cleanup expires or reconnects
   stale state, one cheap model call re-reads the result for duplicates.
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
- [**Daytime freshness**](architecture/freshness.md) — cheap collection and activity
  hints between dreams.
- [**Identity**](architecture/identity.md) — how memcal decides who is who.

### API & tools

- [**Remember**](api/remember.md) — tell memcal something right now.
- [**Recall**](api/recall.md) — read context, detail, evidence, archive.
- [**Correct**](api/correct.md) — deterministic fixes that survive the night.
- [**MCP server**](api/mcp.md) — the tool surface agents use.

### Deploy & evaluate

- [**Hosting**](hosting/installation.md) — install, configure, schedule, publish.
- [**Evaluation**](evaluation/index.md) — how memory quality is measured, from
  deterministic checks to full seven-day lives.
