# The temporal benchmark

`benchmark_temporal.py` is the gap-finding harness. It runs a synthetic life against a
scratch store and reports what the pipeline got wrong, separated by *which part* got it
wrong — a model-extraction miss, a deterministic merge bug, and a retrieval failure are
three different problems and a single score hides all three.

Nothing here touches `~/.memcal`. Every suite seeds its own scratch home and refuses one
that resolves to the real store. Output goes to `tools/bench_output/`, which is ignored.

```bash
python3 tools/benchmark_temporal.py                      # free, deterministic, ~1s
python3 tools/benchmark_temporal.py --layer model        # costs real model calls
```

---

## The two layers

**`--layer integration`** (the default) runs the whole pipeline with the model's answers
supplied by hand — an oracle of what a perfect extractor would have returned. It grades
storage, matching, merge, replay and rendering. It is free, it is fast, and it says
nothing whatever about model accuracy: a green run means the plumbing does the right
thing *given* the right answer.

**`--layer model`** runs the real `dream` pass against your configured model. This is the
only layer that measures extraction and semantic judgement. It costs money or quota.

**`--layer both`** runs each in turn and prints an attribution table: for every check the
model layer fails, whether integration passed it (a model problem) or failed it too (a
code problem).

A run that lost bundles is not a low score, it is a **void** one — unread traffic grades
identically to traffic the model read and understood nothing of. The report says so
loudly at the top and the run should be discarded, not recorded.

---

## The suites

`--suite` takes a comma-separated list and defaults to `all`.

| Suite | What it is |
|---|---|
| `core` | The four-day synthetic life: chat, email, calendar and agent traffic across every connector, with the model's answers supplied at the integration layer. |
| `collision` | Daytime typed writes meeting the nightly pass. See below. |
| `contract` | Saved hostile model replies through the real JSON parser and router. |
| `boundaries` | Calendar edges, timezones, stale evidence, long-gap context, email threading. |
| `clock` | What only elapsed time reveals — questions expiring, rows lapsing, a rescan learning nothing. |
| `schedule` | Recurrence rules, cadence changes, one-off moves. |
| `collection` | Ingest passes, watermarks, and what a partial collection leaves behind. |
| `hermes` | The multi-turn memory lifecycle on the Hermes surface. |

Frontier failures (`gap`) are deliberate unmet expectations that have been looked at and
left open on purpose. They are not xfails and they are not excused — they are counted
separately from established failures so a new break is visible against them.

---

## The collision suite

Added most recently, and different in shape from the others. Every other suite asks "did
the pass read this correctly". This one asks the question underneath: two writers touch
the same plan on the same day — the assistant through a typed tool while the user is
talking to it, and `dream` hours later reading the very sentence that produced that tool
call — and the store has to end up with one row, the newer decision, and its evidence.

Three things make it work differently.

**Every operation carries its own moment.** A scenario is an ordered list of timed
operations — a message arriving, a tool call, a nightly pass, a retry, a checkpoint — and
the clock is pinned to each one through `db`. A message distinguishes *when it was
written* from *when it arrived*, because an email written on Monday and collected on
Tuesday is the shape of half the failures here.

**State is graded at checkpoints, not at the end.** A row that is right on Thursday
because two wrongs cancelled on Tuesday is not right.

**The evaluation boundary is named on every check.** `apply` grades deterministic storage
and replay with the decisions supplied. `retrieval` grades what the pass would have been
*shown* — model-free, no oracle, so it runs on every invocation and costs nothing.
`model` grades a real pass end to end.

### The ten families

1. The assistant files a plan; the pass re-reads the same sentence, worded differently.
2. The assistant moves a date and a place; the pass then meets the original confirmation.
3. The assistant cancels; evidence written *before* the cancellation arrives after it.
4. A genuinely newer cancellation overturns a typed decision.
5. One occasion in chat, email and a typed row, under three different names.
6. Two distinct appointments sharing provider, title and time stay separate — with `6b`,
   its twin, where the same wording *identifies its target* and is a reschedule.
7. An update to one occurrence of a recurring thing, not to the rule.
8. A correction from a different person, on another channel, eleven days later.
9. A retry, and a proposal formed against an older row version landing late.
10. A corrected event whose linked to-do must keep pointing at it.

### Findings by category

The report never blends these, because the trade between them is the whole exercise: a
matcher broad enough to stop duplicates is the same matcher that merges two real
appointments, and one number cannot show both moving.

`duplicate` · `false-merge` · `correction` · `cancellation` · `evidence` · `identity` ·
`retrieval` · `replay` · `brief`

Three scenarios are **reserved** — held out from tuning, and reported on their own line —
so a change can be asked whether it generalised.

### Retellings (`--variants N`)

Each scenario can be told again with something changed. `N` is how many retellings to run
beyond the plain one, taken in this order:

| | |
|---|---|
| `wording` | every line said differently, same facts |
| `distractor` | unrelated chatter mixed into the same threads |
| `dupe` | each source line delivered twice, once under a second id |
| `reorder` | arrival order shuffled within a day; written times unchanged |
| `batch` | a day's traffic collected in one ingest rather than as it happens |
| `sparse` | the assistant is told less — optional tool arguments dropped |

**Six is the maximum**, and `--variants 6` is the most demanding run available. Higher
numbers add nothing.

Retellings are reproducible from the seed, and a failure prints its seed and the first
checkpoint that broke. Written time is never shuffled: a reordered delivery still carries
Monday's timestamp on Monday's sentence, so the semantic chronology is unchanged and a
difference in outcome is a real finding rather than a contradictory history the corpus
never claimed had one answer.

The default is **1**, and **0 under `--layer model`**, where each retelling is a paid run.

---

## Narrowing a run

`--case` matches a substring against a check id or a challenge name. On most suites it
narrows *grading* while the traffic still runs. On `collision` it narrows **execution** —
nothing else is built, so one family costs one scenario's worth of time and model spend.

```bash
python3 tools/benchmark_temporal.py --suite collision --case f3
python3 tools/benchmark_temporal.py --suite collision --case 6      # by family number
```

---

## Choosing a backend

`--provider` picks which of the four supported backends the run measures. Without it the
run inherits whatever `MEMCAL_LLM_PROVIDER` says, which is easy to forget and easy to
misread later.

```bash
python3 tools/benchmark_temporal.py --layer model --provider antigravity
python3 tools/benchmark_temporal.py --layer model --provider claude-code
python3 tools/benchmark_temporal.py --layer model --provider codex
python3 tools/benchmark_temporal.py --layer model --provider openrouter --model z-ai/glm-5.2
```

Passing `--provider` without `--model` brings that provider's own default model with it
(`gemini-3.8-flash-high`, `claude-sonnet-5`, `gpt-5.6-luna`). That pairing matters: the
config resolves the default model from the provider it read at load time, so overriding
one without the other leaves an OpenRouter id pointed at a CLI backend, where it names
nothing.

The override applies to every stage — propose, sweep and match — because a run that reads
bundles on one backend and arbitrates merges on another is measuring neither.

**The three CLI backends bill against a subscription, not per token.** The dry run says so
rather than quoting a price, and the token counts it prints are the *size* of the run —
which still matters there, because capacity is the scarce thing rather than money.

## Spending money carefully

```bash
python3 tools/benchmark_temporal.py --layer model --dry-run
```

Prices the run without making a call: request count, packing, prefix tokens, and the
cached share. **Read this first.** On OpenRouter, `NO PRICE ON FILE` means the model has
no `llm.PRICES` entry and the run would be unpriced spend rather than free.

On OpenRouter, `--model` refuses a name with no `llm.ENDPOINTS` entry rather than warning.
An unpinned request lets OpenRouter choose the provider, and providers of identical
weights disagree about `response_format` — one such accident produced six bundles
reasoned through correctly and answered `{"bundles": []}`, which reads as "the model is
worse" and is a routing fault.

That refusal is scoped to OpenRouter on purpose. A CLI backend talks to one vendor over
one authenticated command, so there is no routing to leave unpinned; their native model
names are deliberately absent from `ENDPOINTS`, and Antigravity in particular serves
Gemini, Claude and open-weight models from a single command, so no one vendor prefix
would find the right row. If such a model has no tuned reasoning budget the run says so
and continues — `--effort` is the knob that pins one.

`--repeat N` runs the model suite N independent times and reports per-check reliability
rather than one lucky score, including how much of the headline was ever in play.

Other knobs, all of which the collision suite now also honours so a model measurement
names the deployment it measured: `--format`, `--prompt-version`, `--effort`, `--stages`,
`--pack`.

---

## Reading the output

- `ok` — passed.
- `FAIL` — an established failure. Something is wrong.
- `gap` — a frontier check: a known, deliberate unmet expectation.
- `soft` — informational, not counted against the score.

The final line counts hard checks, fully-green challenges, open frontier gaps and
established failures separately. A count of failing checks is *total minus passed*, and
that number includes the frontier gaps — read the two together or a known-open gap looks
like a regression.

Scores are not recorded in any tracked file, and `tests/test_docs.py` enforces that no
page quotes one in prose: a number written by hand is a relation to a program's output,
and prose drifts on it. Run the tool.

---

## What changed most recently

The work these notes were written for, in the order it landed:

- **The `collision` suite exists** — the ten families above, the checkpoint grading, the
  timed operations, the three boundaries, the category reporting, and the retellings.
- **`load.agent_actions` honours each action's recorded time.** It used to drop it, so
  two tool calls in one day could not be ordered against each other or against the
  traffic between them — which is exactly what write precedence is decided on.
- **The `model` layer of `collision` actually calls a model.** It ran the oracle path
  under the model layer's name, and graded as a model measurement. The report now counts
  dispatched calls and says `NO MODEL WAS CALLED` when the count is zero.
- **`--variants` reworded and re-defaulted.** "Retellings", not "perturbations"; 0 under
  `--layer model` so asking for the model layer does not quietly multiply the bill.
- **All six retellings are reachable.** `batch` and `sparse` were implemented and
  unselected, and several scenarios declared shorter lists than they needed to.
- **`--provider` exists, and Antigravity is reachable.** The backend was fully supported
  in `llm.py` and unreachable from this tool: there was no flag, and the `--model` guard
  refused every name without an `ENDPOINTS` entry — which is all three CLI backends'
  native names, not just Antigravity's. The guard is now scoped to OpenRouter, where its
  reasoning actually applies, and the dry run gives subscription backends the right
  advice instead of telling you to add a per-token price.
- **Fixture corrections that came out of this**, worth knowing when reading a diff:
  reschedules in the core corpus now supply the key the prompt shows the model and tells
  it to return (beats 35 and 57), matching the pattern already used for Elements and
  Spider-Man; the email suites key bundles by conversation root rather than by sender;
  and the gate checks assert that bulk mail arrives *quietly* rather than not at all.
