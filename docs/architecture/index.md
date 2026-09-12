# Architecture overview

Memcal is one resident memory serving many agents. A local store owns the
watchers, the gate, the spool, the dream pass, and brief rendering. Agents —
Hermes, OpenClaw, anything MCP-speaking — are transient: they read the brief, open
detail on demand, and write back through typed tools.

## The loop

```text
streams ──▶ observe ──▶ gate ──▶ spool ──▶ dream (nightly) ──▶ typed stores ──▶ brief
  ▲                                                                     │
  │ daytime collection (model-free)                                     ▼
  └────────────────────── freshness hints ◀────────────────────── agent turns
```

- **Day:** connectors collect new traffic without model calls. Freshness hints flag
  activity that could affect known context.
- **Turn:** the agent receives the brief plus any activity hints, reads original
  messages when an answer needs checking, and applies evidence-backed corrections
  through typed tools.
- **Night:** dream reconciles everything collected into typed state and re-renders
  the brief.

## Design theses

1. **Breadth in context, depth on demand.** A small always-present block carries the
   current week; everything older is fetched deliberately. Thirty relevant lines
   produce lateral connections; two hundred thousand tokens produce mush.
2. **One world model, many streams.** A text, an email, a group message, and
   something said to the agent resolve to the same row. Bundling by entity across
   streams makes cross-platform deduplication structural.
3. **Recency is resolved when writing, not when reading.** The old address moves to
   history at write time. Reads never weigh timestamps against relevance scores.
4. **Extraction is filling known slots.** The model proposes diffs against keys —
   event inserts/updates, to-do opens/closes, wiki slot fills, standing edits,
   questions. Never free-form memories.
5. **Dictionary lookup before model call.** Contacts, sender policy, nicknames, and
   thread links are hash lookups. The model only sees what genuinely needs judgment.
6. **Inferred things become questions, never facts.** One confident wrong write costs
   more trust than ten useful ones earn.

## Map

| Page | Question it answers |
|---|---|
| [Observe](observe.md) | How does raw traffic get in? |
| [Gate](gate.md) | What deserves model attention, and what costs nothing? |
| [Dream](dream.md) | How does traffic become typed state? |
| [Brief](brief.md) | What does the agent see every turn? |
| [Events & series](events.md) | How are plans represented? |
| [To-dos](todos.md) | How are obligations tracked? |
| [Questions](questions.md) | How is uncertainty handled? |
| [Wiki](wiki.md) | Where do durable facts live? |
| [Identity](identity.md) | Who is who? |
| [Daytime freshness](freshness.md) | What happens between dreams? |
| [Evidence & provenance](evidence.md) | Why should I believe a row? |
| [Storage](storage.md) | Where do the bytes live? |
| [Performance](performance.md) | What does it cost, and how fast is it? |
| [memcal vs retrieval](memcal-vs-rag.md) | Why not just vector search? |
