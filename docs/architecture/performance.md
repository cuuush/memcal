# Performance

Cost is an architecture property, not a tuning accident. The whole pipeline is
shaped so ordinary operation stays fast and cheap, with model spend concentrated
where judgment is genuinely needed.

## Where the money does not go

- **The gate** admits a fraction of traffic before any model sees it. "Hey" costs
  nothing; newsletters cost a table lookup.
- **Identity** is dictionary lookups at ingest — Contacts, sender policy,
  nicknames — never per-line model calls.
- **Daytime collection and brief rendering** make zero model calls. Freshness hints
  are deterministic; only requested evidence interpretation spends.
- **Prompt caching** does real work: the shared prefix (event window, to-dos,
  standing) is identical across every bundle call in a run.

## Where it does go

- **Propose**: one call per packed group per pass (up to `MEMCAL_PACK_BUNDLES`,
  default 6, bundles per call; a typical day is 5–15 bundles total),
  bounded by `MEMCAL_ITEM_BUDGET` and `MEMCAL_PACK_TOKENS`. Parallelism caps at
  `MEMCAL_MAX_PARALLEL` (default 8).
- **Sweep**: one cheap call re-reading resulting state, not the day's traffic.
- **Merge arbitration**: only genuinely ambiguous near-horizon conflicts.
- **`memcal who --resolve`**: one call over the whole identity picture, asked for
  explicitly.

`memcal stats` reports post-gate volume and run history — instrument before
optimizing. The gate's effectiveness is the entire cost story, and no estimate is
worth more than a week of real numbers.

## Latency shape

Prepared answers read the brief: immediate. Flagged items add one bounded
activity read. Nothing in the turn path waits on extraction, consolidation, or
history-wide search. Response latency, source reads, driver and provider model
calls, and nightly processing cost are recorded separately, so a slow correct
answer and an immediate correct answer are never confused. See
[Grading](../evaluation/grading.md).

## Backends

| Backend | Default model | Authentication |
|---|---|---|
| Codex programmatic mode (default) | `gpt-5.6-luna` | Existing Codex login |
| Claude Code programmatic mode | `claude-sonnet-5` | Existing Claude Code login |
| Antigravity programmatic mode | `gemini-3.8-flash-high` | Existing Antigravity login |
| OpenRouter | `openai/gpt-5.6-luna` | OpenRouter API key |

The three CLI backends run one-shot structured completions with no persistent
sessions or hidden history. Prefer a flash model on Antigravity: its print mode
wraps every prompt in an agent preamble that bills far more input tokens per call.
See [Configuration](../hosting/configuration.md).
