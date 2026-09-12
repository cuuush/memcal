# Dream

Dream is a batch transform, not an agent: input in, a structured diff out. No
loop, no wandering, no per-message tool calls. It runs nightly (and on demand),
reads everything gated since the last watermark, and reconciles it into typed
state.

```bash
memcal dream --dry-run   # preview size and estimated cost; calls nothing
memcal dream             # run the pass
```

## The stages

**1. Bundle (code).** Everything gated since the last watermark is grouped **by
entity or thread, across all streams** — Jordan's text, the group-chat line where
Jordan spoke, and Jordan's email land in one bundle. A typical day is 5–15
bundles. This is the key choice: splitting by source would separate the things
that must be joined, while grouping by subject makes cross-platform deduplication
structural. Three messages about one dinner arrive in one context, so no model can
create three memories from them.

**2. Propose (model, parallel, packed).** Each call takes up to
`MEMCAL_PACK_BUNDLES` (default 6) bundles and sees their content
plus current state — the event window, open to-dos, standing, and the wiki pages
for its entities. That state is identical across calls, so it caches. Each returns
a **typed, keyed diff**: event inserts and updates, to-do opens and closes, wiki
slot fills, standing edits, questions to ask. Never free-form memories — only
diffs against keys, which is what makes deduplication automatic.

**3. Merge (code, model only on conflict).** Keyed diffs merge deterministically.
Two bundles proposing the same row collide on the key and merge. Two bundles
touching one wiki page apply per-slot. Only genuinely ambiguous near-horizon cases
get a model call.

**4. Apply (code).** Version-checked writes update the typed stores. Every row
records which pass wrote it, and write operations carry the originating message
plus the row version they acted on — so re-reading the sentence that moved an
event does not create a second one. See [Evidence & provenance](evidence.md).

**5. Sweep (one cheap model call).** Reads the *resulting* state plus the day's
diffs — around 2k tokens, not the day's traffic. Duplicates? Contradictions?
Obvious junk? Healing lives here, and it is cheap precisely because it reads
output rather than input. Skippable with `--no-sweep`.

**6. Render (code).** Writes `brief.md`, trimmed to the token cap. See
[Brief](brief.md).

## Write precedence

A correction made during the day is not undone by older evidence collected later.
Precedence runs per field on when evidence was *said*: a message written before a
correction cannot revise the corrected field, while genuinely newer evidence still
can. Cancelling one booking and making another keeps the old row declined and the
new row confirmed; an older notice cannot undo a newer confirmation. Nothing
becomes permanently unchangeable.

Dream reviews original source messages and genuine user statements — never the
assistant's own prose. A wrong assistant answer stays a wrong answer until real
evidence corrects it; reprocessing never promotes it into a correction. Typed
corrections applied during the day survive the nightly pass without duplication:
the same event identity, the same evidence.

## Modes and recovery

One watermark-driven program with `--mode nightly|ondemand|realtime`
(`ondemand` by default; `nightly` is the scheduled pass). Short-cadence
collection without consolidation runs between passes. `--rounds` repeats until the spool drains; `--redo`
un-claims processed items for reprocessing (merge-on-key, so corrections are not
duplicated); `--retry RUN` requeues a failed or partial run's readable lines.
The Runs view shows whether each pass was ok, partial, failed, running, or priced
only. See [Dream runs](../api/dream.md).
