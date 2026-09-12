# Runner

Five responsibilities stay separate: corpus loading and validation; Hermes
provisioning and control; provider setup, import, readiness, and isolation;
grading; artifact and report generation.

## One isolated life per run

Each run provisions an isolated Hermes home and provider namespace per life,
configuration, and repetition. Mutable memory is never reused across independent
runs. The run manifest pins everything: corpus version, lives and checkpoints,
variants and seeds, Hermes version, provider integration and deployment version,
driver model, provider models, grader, context and tool limits, timeouts, cost
ceilings, and output location.

The user's normal Hermes profile, memcal home, live source accounts, and system
scheduler are never touched. Only required credentials are injected, redacted
from logs and manifests; only run-owned resources are stopped or deleted.

## The memory-only tool boundary

The model may use the selected provider's legitimate memory tools and native
automatic context — search, detail, updates, native reasoning. Shell, unrestricted
files, web, other providers, independent Hermes memory/history retrieval, and
benchmark inputs are blocked at dispatch and context-loading boundaries, not by
prompt. A disallowed attempt ends the probe as a distinct tool-policy failure —
never a factual success — and the story continues without fabricating the missing
action.

Isolation checks plant a unique fact in built-in memory and an unrelated old
session and verify neither route reaches the next question, while normal
conversation persistence and authorship survive. Preflight fails loudly if
isolation cannot be enforced; there is no quiet fallback.

## Time, collection, maintenance

The fictional date is controlled inside the isolated runtime; prompt dates,
session starts, compression, and tool-visible dates all read scenario time while
elapsed-time measurement stays real. The same public observations reach every
system at declared collection boundaries throughout the day; ordinary user turns
execute through Hermes at their simulated times with normal hooks — persistence,
wrong answers, and their consequences included. Maintenance (memcal's dream,
each provider's native consolidation) runs at scheduled night boundaries with
completion verified, never auto-triggered by a question.

## Artifacts

Every run saves an immutable manifest, per-stage status and timings, import
batches, transcripts and tool traces, provider operation ids, raw answers,
per-fact verdicts with judge explanations, usage/cost ledgers, and
machine-readable JSON plus a concise comparison report. Grading re-runs from
saved answers without re-ingestion; interrupted runs resume from persisted
operation ids; budget stops save resumable progress, never a passing score.
Missing runs and incomplete scoring stay visible.
