# Deterministic checks

The temporal benchmark is the gap-finding harness for plumbing: it runs a
synthetic life against a scratch store and reports what went wrong, separated by
*which part* got it wrong. A model-extraction miss, a deterministic merge bug,
and a retrieval failure are three different problems, and a single score hides
all three.

```bash
# Free deterministic pipeline (~1s)
python3 tools/benchmark_temporal.py --layer integration

# Live configured model (costs money/quota — deliberate evaluation only)
python3 tools/benchmark_temporal.py --layer model

# Daytime typed writes meeting the nightly pass
python3 tools/benchmark_temporal.py --suite collision --variants 4
python3 tools/benchmark_temporal.py --suite collision --case f3
```

## Layers

| Layer | Meaning |
|---|---|
| `integration` (default) | Model answers supplied by hand — an oracle of a perfect extractor. Grades storage, matching, merge, replay, rendering. Free, fast, says nothing about model accuracy. |
| `model` | The real dream pass against your configured model. The only layer measuring extraction. Costs money or quota. |
| `both` | Each in turn, with an attribution table per failed check: model problem or code problem. |

A run that lost bundles is **void**, not low-scored — unread traffic grades like
traffic understood nothing of. The report says so loudly; discard the run, do
not record it.

## Suites

| Suite | What it is |
|---|---|
| `core` | Four-day synthetic life across every connector |
| `collision` | Ordered, timed operations — message, typed write, nightly pass, retry — graded at each checkpoint, not just the end |
| `contract` | Hostile model replies through the real parser and router |
| `boundaries` | Calendar edges, timezones, stale evidence, threading |
| `clock` | What only elapsed time reveals — expiry, lapse, no-op rescans |
| `schedule` | Recurrence rules, cadence changes, one-off moves |
| `collection` | Ingest passes, watermarks, partial collection |
| `hermes` | Multi-turn memory lifecycle on the Hermes surface |

Each collision message distinguishes written time from arrival time, so "an
email written Monday, collected Tuesday" is expressible. Results report by
category — duplicates, false merges, lost corrections, missed cancellations,
missing evidence, identity, retrieval, replay, and brief. `--variants N` retells
each scenario differently (`wording`, `distractor`, `dupe`, `reorder`, `batch`,
`sparse`) and prints the seed and first failing checkpoint;
deliberate unmet expectations stay counted apart as frontier gaps.

Scratch stores only — never `~/.memcal`. Runs print current results; no tracked
score ledger. Full knob list: `tools/BENCHMARK.md` in the repo.
