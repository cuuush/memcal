# Evaluation overview

LifeTrace measures how well a memory system supports a personal assistant through
a realistic seven-day life: correct current answers, useful suggestions, and
remembered context — delivered quickly and affordably.

Each run provisions real, isolated Hermes instances with one memory integration
each. Five channels of raw personal information arrive at declared collection
times throughout the day; natural assistant conversations run inside the
continuing life; each provider uses its supported memory lifecycle. Memcal
collects cheaply during the day and dreams nightly. Scoring counts facts
correctly recovered, missed, and misrepresented — alongside response latency and
total cost.

The comparison starts with Hermes + Memcal and one external memory provider on the
same corpus; further providers follow once the two-provider pilot is validated. The generic runner never imports memcal: scored answers come
from Hermes using the installed integration, never from scripted tool calls or
fabricated confirmations.

| Page | Covers |
|---|---|
| [Corpus](corpus.md) | Observations vs expectations, interleaved lives, controls |
| [Runner](runner.md) | Provisioning, isolation, tool boundary, artifacts |
| [Grading](grading.md) | Facts, mistakes, latency, cost, auditability |
| [Deterministic checks](deterministic.md) | The free oracle-layer harness for plumbing regressions |

No claim about quality rests on deterministic scores alone: a green oracle run
says the plumbing does the right thing *given* the right answer. It says nothing
about model accuracy. Product ranking comes from the live experiment, frozen
corpus and recipes, held-out lives, and hand-audited pilot grades.
