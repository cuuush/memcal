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

The pass is six stages, `bundle → propose → merge → apply → sweep → render`. Three
are pure code; three call a model, each with its own model knob so you can run a
strong model where judgment matters and a cheap one elsewhere.

| # | Stage | Actor | Reads | Model knob |
|---|-------|-------|-------|------------|
| 1 | Bundle | code | gated traffic since the watermark | — |
| 2 | Propose | model | one bundle group + current state | `MEMCAL_PROPOSE_MODEL` |
| 3 | Merge | code + model on conflict | **proposals + their source lines** | `MEMCAL_MATCH_MODEL` |
| 4 | Apply | code | merged diffs + stored rows | — |
| 5 | Sweep | model | **resulting state + diff log** (no source) | `MEMCAL_SWEEP_MODEL` |
| 6 | Render | code | the typed stores | — |

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

Within a call, propose can be split into **ordered field passes** —
calendar, then to-dos, then wiki pages, then questions — over one cached
conversation, so each pass sees what the ones before it wrote (a question can name
a plan the way the calendar just named it). This is off by default
(`MEMCAL_PROPOSE_STAGES` empty = one pass) and mainly earns its keep on a cold
start, which also fans propose into `MEMCAL_COLD_START_WAVES` waves so a
hundred-plus bundles against an empty store don't all read the same empty snapshot.

**3. Merge (code, model only on conflict).** Reconciles the proposals from *all*
bundles against each other and against stored rows, **before anything is written,
with the original source lines in hand.** Most of it is deterministic: keyed diffs
collide on their key and merge, two bundles touching one wiki page apply per-slot,
and a `cluster()` step groups near-duplicate events by near-day / shared-guest /
wording heuristics with no model call at all — on a nightly run that is often zero
model calls. A model is asked **only for a genuinely conflicted cluster** ("are
these two mentions the same event?"), one small call per cluster over just that
group's evidence. Because merge can read source, it is the stage allowed to
**fuse rows and authorize status corrections** (a booking that was cancelled →
`declined`). It fails safe: a timeout or an "unresolved" answer keeps the rows
**separate**, never merged on a guess.

**4. Apply (code).** Version-checked writes update the typed stores. Every row
records which pass wrote it, and write operations carry the originating message
plus the row version they acted on — so re-reading the sentence that moved an
event does not create a second one. See [Evidence & provenance](evidence.md).

**5. Sweep (one cheap model call).** A post-write **janitor**, not a second merge.
It reads the *resulting* state plus the day's diff log — around 2k tokens, **not**
the day's traffic and **not** the source lines — and looks for damage the earlier
stages left: a duplicate to drop, obvious junk (banter, newsletter events, facts
about software), a contradiction to raise as a question. Because it has no source
evidence it **cannot correct** anything — status changes and merges are left to
Merge and Apply, which can compare source. That evidence-free, output-only view is
exactly why it is cheap. Skippable with `--no-sweep`.

**Merge vs. sweep, in one line:** merge runs *before* the write, *with* source, and
*fixes* things (fuse, correct status); sweep runs *after* the write, on a summary
*without* source, and only *drops or flags* what slipped through.

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
