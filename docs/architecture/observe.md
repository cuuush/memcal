# Observe

Connectors pull new messages, email, and calendar records into a local append-only
archive. Everything is archived; later stages decide what deserves attention.

## The archive

Each archived row keeps source time separate from collection time: when a message
was *written* versus when memcal *collected* it. A delayed old message is new to
the archive even though its source timestamp is old — per-field precedence later
runs on when evidence was said, so the distinction matters.

Rows deduplicate on `(stream, external_id)`, so re-runs and redelivery are safe.
Source and thread identity travel with every row, which is what lets later stages
link an event's supporting evidence back to the exact thread it came from. The
archive is full-text indexed (SQLite FTS5) and searchable at any time:

```bash
memcal search "dinner next week"
```

Nothing lives only in a derived store. A wrong gate decision costs a search later,
never a loss.

## Transports

A new platform implements hooks, not another ingest loop. There are two shapes:

| Shape | Behavior | Sources |
|---|---|---|
| `PolledSource` | Enumerates conversations, reads each forward from a per-chat watermark | Slack, Telegram, GroupMe |
| `StreamSource` | Drains one ordered queue from a single cursor | Signal, iMessage, WhatsApp, BlueBubbles |

Per-conversation watermarks mean quiet chats cost nothing on re-runs; destructive
queues (Signal) drain whole by design. See [Sources](../sources/index.md) for
per-platform behavior, budgets, and rounds.

## Collection without a model

`memcal ingest` only moves bytes: fetch, deduplicate, archive, spool. It never
calls dream, extraction, semantic matching, or a model:

```bash
memcal ingest slack --limit 20   # one source, small first sip
memcal ingest all                # everything configured
```

`--limit` caps items per round and `--rounds` (default 25) repeats while more work
waits. During the day, collection runs on a short cadence (minutes, while the host
and source are reachable) so fresh traffic is archived long before the next dream.
Daytime collection and the nightly pass share this path — collection never triggers
consolidation by itself.

Lines that pass the [gate](gate.md) are spooled for the model pass. Lines older
than the spool horizon (`MEMCAL_SPOOL_HORIZON_DAYS`, default 30) stay archived and
searchable but are not queued for extraction. Muted and platform-muted threads are
still collected; muting shapes priority and presentation, never collection.

## Due-only collection

Daytime collection asks what is *due*, not what is new. Each source records a
durable outcome per attempt — complete, incomplete, or failed — with the last
time it was completely checked. A later failure never erases the earlier
successful check, and an interrupted attempt never reads as complete:

```bash
memcal ingest all --due   # collect only sources due for another check
```

Due means the configured interval (`MEMCAL_COLLECT_INTERVAL_MINUTES`, default
5 minutes) has elapsed since the source's last attempt — even if no recent
messages exist. A quiet inbox checked at 11:00 is due again at 11:05 regardless
of when its newest message was written. Failed, unavailable, and incomplete
attempts wait at least the interval before automatic retry, and stronger
connector backoff still applies.

Ingest summaries and the queue view report the same per-source outcome:
`[complete]` for an exhausted source, `[incomplete — more waiting]` when work
remains, plus `failed`, `unavailable`, and `unknown` for the rest. A quiet final
page never erases earlier pages' counts; an error preserves partial counts.

When nothing is due, the command is a genuine no-op: no source checks, no
collection records, no brief rewrite, no model work. An explicit ordinary ingest
always forces collection and records its outcome. `--due` and `--stale` do not
combine. A due run that leaves a source unavailable, failed, or incomplete exits
nonzero with the reason.

## Agent conversations

Inbound user turns from the Hermes and OpenClaw integrations archive the same way,
under the agent stream. Only the user's original message is kept as user-authored
evidence — assistant replies, injected snapshots, and tool output are excluded so
they can never become "facts" later. See [Agent conversations](../sources/conversations.md)
and [Daytime freshness](freshness.md).
