# Daytime freshness

Dream stays nightly. Between dreams, memcal collects cheaply, flags activity that
could affect known context, and gives the assistant a short route to the original
messages. Most answers use prepared context immediately; exceptions need a bounded
lookup, not a search through the whole history.

## Why nightly, not live

Dream is *consolidation*: it reconciles a day of messages into typed state, and
that work is worth doing once, at night, when it can be amortized over everything
at once. Running a dream pass on every incoming message would be both expensive —
a model call per message — and largely wasted, since most turns touch nothing that
changed.

So the daytime path spends no model calls at all. It collects, and it *flags* that
a plan's source thread has new activity — without deciding what that activity
means. The live intelligence is the assistant, not a miniature dream: when a
question lands on a flagged plan, the assistant reads the few messages behind it
and answers from them.

Say the brief shows soccer practice today, but its group chat is flagged. Asked
"do we still have practice?", the assistant opens the thread, sees it was
canceled, and says so — rather than repeating the stored plan. If it ignores the
flag and parrots the old answer, that is a measured failure. Model cost then
scales with the questions that actually need checking, not with message volume;
nightly dream still applies the change once, under the same event identity.

## The lifecycle

1. Nightly dream establishes poker on Saturday at 8 at Jordan's old address.
2. At 11:00, the poker group moves it to Sunday at a new address. Daytime
   collection archives the message; no model runs.
3. At noon, in yesterday's still-open assistant conversation, you ask: "Where is
   poker?"
4. The current turn receives the latest brief. Poker is marked as having new
   activity in its supporting group chat, with a handle to open that activity.
5. The assistant reads the short conversation, answers with the new address, and
   can apply an evidence-backed typed correction through normal tools. Merely
   reading the messages changes nothing and claims nothing was resolved.
6. If the assistant repeats the old answer instead, that is a measured failure —
   and its prose never becomes new factual evidence for dream.
7. Nightly dream reviews the original messages and genuine user statements, applies
   the Sunday change once under the same event identity, and preserves an earlier
   typed correction without duplication.

## Collect cheaply

Daytime collection reuses the ordinary ingest path on a short cadence — due every
few minutes (`MEMCAL_COLLECT_INTERVAL_MINUTES`, default 5) while the host and source
are reachable — without invoking dream, extraction, semantic matching, or any
model. `memcal ingest all --due` is the manual form: nothing due means a genuine
no-op. Per-turn context reads local state; background collection absorbs
transport latency. Each source exposes its last successful collection and error
status, so a quiet source reads as checked, not as broken —
and a source silent since yesterday gets an honest coverage warning, not "nothing
changed." Explicit exclusions and mutes are preserved throughout.

## Nominate, don't decide

Activity links start from exact associations: an event's supporting evidence
already points at source threads and calendar identifiers. Where evidence supports
broader links, bounded deterministic retrieval over known people, thread names, or
distinctive terms nominates candidates. A shared participant alone never marks
every event involving that person as changed.

The signal means **"new activity in a source associated with this event,"** not
"this event moved." The assistant or the nightly model judges whether the messages
are a correction, a new occurrence, non-attendance, or unrelated chatter. No
inferred date, location, status, identity merge, or source priority belongs in the
detector. Strong thread links sit beside the known item; weaker possible
associations sit in a bounded activity section.

## Track what was actually reviewed

Three facts stay separate: when collection last succeeded, which collected
observations have been considered for the relevant typed state, and when the brief
was rendered. "Rendered at noon" never implies the event was checked at noon.
Arrival cursors detect newly collected observations — a delayed old message is new
to the archive even with an old source timestamp — while same-ID redelivery never
re-raises the count. Rendering a brief, reading a message, or answering a question
does not acknowledge semantic review; only a completed review with its exact
inputs does.

## Warn in current context

```text
〔E42〕 Sat 12 Sep  Poker night, 8pm — confirmed · Jordan's
  ↳ New activity: signal/poker-group — 3 message(s) since this plan was reviewed. It may have changed; open with memcal_activity(handle=E42) before giving current details.
[complete for Wed 9 Sep – Wed 16 Sep; look up anything outside that]
```

The brief never stamps itself freshly verified. A plan with no new activity
carries no warning; a source that has fallen behind shows its own `[STALE: …]` or
`[COLLECTION: …]` line; and the week block states the period it is complete for.

A bounded activity reader returns original text with sender, source time, arrival
time, and provenance — paginated, with omissions disclosed — so the assistant
never has to invent a keyword search for a flagged thread. Freshness metadata
survives brief trimming beside its event: an event and its warning trim as one
unit, and if a warning that stood in for a thread's backlog is dropped by the
token budget, the completeness claim goes with it and a compact `[coverage
incomplete …]` line takes its place — a trimmed brief never reads as exhaustive.
New opportunities with no event row yet appear as unreviewed-activity notices, so
broad questions ("Anything fun this weekend?") never imply exhaustive coverage
while fresh material sits unprocessed. Coverage is judged against the plans the
brief actually renders, not date-range membership: a thread linked only to an
unconfirmed opportunity or an event past the Later cap still surfaces its traffic.

## Reach long-running sessions

Hints travel on the existing per-turn prefetch path, not a second copy in a cached
system prompt: the current brief and activity hints arrive before the assistant
answers a substantive turn, with the user's original message kept separate from
injected context. A changed brief is re-injected as a `MEMCAL SNAPSHOT`; an
unchanged turn carries a compact `MEMCAL CURRENT` confirmation with the matching
snapshot id instead. Prepared memory counts as current only when the turn carries
one or the other — so a snapshot is deduplicated only once it has actually been
observed in delivered context, and a render that timed out or was discarded never
suppresses the next one. Same-session next-day turns, resumed sessions, and short
follow-ups ("and where?") all receive the latest snapshot, which supersedes older
snapshots and assistant summaries; compression re-emits it, since a generated
summary may not preserve the exact brief and its warnings. If neither a snapshot
nor a confirmation arrives — or prefetch fails and a compact freshness-unavailable
warning replaces the snapshot — the assistant refreshes before claiming current
plans; silently falling back to a stale one is unacceptable. A cheap brief-refresh
read covers adapters without reliable per-turn injection.

## Keep assistant prose out of evidence

User statements, corrections, and instructions are eligible evidence; external
source messages and valid evidence-backed typed writes are too. Everything else —
assistant replies, generated summaries, injected memory blocks, quoted tool
output — is preserved for audit outside factual ingestion with its original
roles. "Is poker still Saturday?" asks; it does not affirm Saturday. "You told me
Saturday" attributes a claim to the assistant; it is not an independent
correction. A successful tool write has an observable effect; a false success
claim does not create one. For a change discovered through archive reading, the
original source ids and evidence time are preserved — an old message is never
re-stamped as a new correction because the assistant read it today.

A cited correction carries the evidence time of its *oldest* cited line, so a
stale line cannot ride a newer one's hour; a line with no readable timestamp, or a
`source_id` that names no real archive row, is refused outright rather than
silently falling back to run time and minting authority it never had. Values a
row is born holding are floored at their founding evidence time (to the day when
that time is only the creation instant), so the first correction — however old its
source — cannot erase a plan dream just established; a destructive clear clears the
same bar a replacement must.
