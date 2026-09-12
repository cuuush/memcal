# Daytime freshness

Dream stays nightly. Between dreams, memcal collects cheaply, flags activity that
could affect known context, and gives the assistant a short route to the original
messages. Most answers use prepared context immediately; exceptions need a bounded
lookup, not a search through the whole history.

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
Memcal rendered 12:00. Sources checked through 11:58.
E42 Poker — last confirmed: Saturday, 8 pm, Jordan's.
  New activity: poker group, 3 messages since this plan was reviewed.
  This plan may have changed. Open the activity before giving current details.
```

A bounded activity reader returns original text with sender, source time, arrival
time, and provenance — paginated, with omissions disclosed — so the assistant
never has to invent a keyword search for a flagged thread. Freshness metadata
survives brief trimming beside its event, and a general backlog notice survives
with it. New opportunities with no event row yet appear as unreviewed-activity
notices, so broad questions ("Anything fun this weekend?") never imply exhaustive
coverage while fresh material sits unprocessed.

## Reach long-running sessions

Hints travel on the existing per-turn prefetch path, not a second copy in a cached
system prompt: the current brief and activity hints arrive before the assistant
answers a substantive turn, with the user's original message kept separate from
injected context. Same-session next-day turns, resumed sessions, compression, and
short follow-ups ("and where?") all receive the latest snapshot, which supersedes
older snapshots and assistant summaries — its confirmed facts qualified by
collection and review coverage throughout. If prefetch fails, a compact
freshness-unavailable warning replaces the snapshot; silently falling back to a
stale one is unacceptable. A cheap brief-refresh read covers adapters without
reliable per-turn injection.

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
