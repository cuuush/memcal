# Architecture overview

Memcal keeps a local memory store that can serve multiple agents. It collects
source messages, processes them in Dream, and renders a brief. Agents such as
Hermes and OpenClaw, or any MCP client, can read the brief, open details, and
write corrections through typed tools.

## The loop

```text
streams ──▶ observe ──▶ gate ──▶ spool ──▶ dream (nightly) ──▶ typed stores ──▶ brief
  ▲                                                                     │
  │ daytime collection (model-free)                                     ▼
  └────────────────────── freshness hints ◀────────────────────── agent turns
```

- **Day:** connectors collect new traffic without model calls. Freshness hints flag
  activity that could affect known context.
- **Turn:** the agent receives the brief plus any freshness hints, reads original
  messages when an answer needs checking, and applies evidence-backed corrections
  through typed tools.
- **Night:** dream reconciles everything collected into typed state and re-renders
  the brief.

## Design principles

1. **A brief with details on demand.** The brief includes the next 30 days by
   default; tools retrieve other dates and supporting evidence.
2. **Related information across sources.** Memcal groups and reconciles mentions
   of the same person or plan from messages, email, and agent conversations.
3. **Evidence-based updates.** Corrections are applied per field according to when
   the evidence was said, with previous values retained in history.
4. **Structured changes.** The model proposes updates to events, to-dos, wiki
   entries, and questions rather than saving an unstructured list of memories.
5. **Use model calls where judgment helps.** Ingest uses dictionary lookups for
   identity and policy; model calls handle extraction and ambiguous matches.
6. **Preserve uncertainty.** When evidence is insufficient, Memcal can raise a
   question rather than turn a guess into a fact.

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
