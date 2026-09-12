# Evidence & provenance

Every compact item opens onto why it exists. Provenance is not a debug extra — it
is how corrections survive the night and how a wrong row gets fixed instead of
argued with.

## Open any row

```bash
memcal E286     # event: sources, value history, change records
memcal T7       # to-do: origin, wake condition, state changes
memcal Q4       # question: evidence, keep/amend/answer trail
```

Each view shows the source messages behind the item, what changed and when, and
which model call proposed the change. Source messages are append-only; typed rows
can be corrected, merged, or withdrawn, but their evidence and value history
remain available.

## Model calls are saved

Prompts, replies, usage, and traces land in `calls/`, so `memcal trace` can show
what was sent, what came back, and which generation wrote a row. `memcal review`
summarizes what the last pass wrote, what it wants to know, and what it could not
resolve.

## Writes carry their cause

When the assistant changes something through a typed tool, the change and the
operation record land together: the row targeted, what actually changed, the
message being looked at when it was said, and the row version acted on. The
nightly pass sees those records beside the conversation that produced them, so
re-reading the sentence that moved an event does not create a second one — and a
caller that cannot name its causing message still works, with the record saying
so.

## Replay-safe by construction

Same event identity and same evidence across daytime writes and dream: re-reading
a statement never duplicates its change. Explicit user corrections are preserved
without letting a generic user-priority rule override genuinely newer source
evidence. Merge compares timestamped source messages when placing cancellations —
a matching time nominates a candidate, and the meaning of the evidence decides.
