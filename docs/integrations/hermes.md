# Hermes

The native memory-provider integration. Thin by design: the memory system is the
resident process, the agent is transient.

```bash
ln -s /path/to/memcal/integrations/hermes/memcal ~/.hermes/plugins/memcal
hermes memory setup
```

## Every turn

- **Inject** — `prefetch()` reads local state without collecting sources or calling
  a model. The first brief and later changes arrive as `MEMCAL SNAPSHOT`; an
  unchanged brief adds nothing to the user turn. Up to three wiki pages named in
  the query can still be recalled independently of brief changes. A snapshot is
  deduplicated only once it has been observed in delivered context, so a timed-out
  or discarded render never suppresses the next one, and compression re-emits. If
  rendering fails, `MEMCAL UNAVAILABLE` replaces the snapshot rather than leaving a
  stale one standing.
- **Archive** — `on_turn_start` / `sync_turn` file the clean user message under
  the agent channel. Assistant replies, injected snapshots, and tool outputs are
  excluded on these paths.
- **Tools** — 21 schemas: reads (`memcal_open`, `memcal_open_page`,
  `memcal_search_wiki`, `memcal_open_source`, `memcal_conversation`,
  `memcal_list_days`, `memcal_list_month`, `memcal_search_archive`,
  `memcal_activity`, `memcal_refresh`) and typed writes (`memcal_add`,
  `memcal_update`, `memcal_schedule`, `memcal_move_once`, `memcal_merge`,
  `memcal_drop`, `memcal_todo`, `memcal_answer`, `memcal_note`, `memcal_alias`,
  `memcal_reviewed`), executed as code with same-transaction evidence.
- **Session end** — the brief rewrites, so the next session starts current.

`MEMCAL_HOME` (default `~/.memcal`) and `MEMCAL_SRC` select the store and
checkout. Freshness hints travel on the prefetch path, so long-running sessions
receive current context — including next-day turns, resumes, and compression —
without a second injection copy. See [Daytime freshness](../architecture/freshness.md).

Hermes caches the provider's system block for the conversation, so it contains
stable instructions rather than the changing brief. Snapshot updates use Hermes's
model-facing user-message context; the original user text is preserved. Unchanged
turns need no brief context at all. Since Hermes can also skip or time out a read,
silence does not verify freshness: the assistant uses `memcal_refresh` before
claiming current plans on a turn without a snapshot.
Hermes skips prefetch for bare greetings and acknowledgements, so the initial
snapshot arrives on the first substantive turn rather than a greeting like “hi”.

Periodic collection runs separately when scheduling is enabled (default due
interval: five minutes). New messages can change activity or coverage notices in
the brief, causing an update on the next turn. Nightly Dream consolidates messages
into stored plans and facts. Messages that do not change the rendered brief do
not trigger a duplicate snapshot.

The default snapshot reaches 30 days ahead and includes month/year lookup guidance.
To confirm a guessed sender name, use the Conversations page or the CLI; Hermes
does not currently expose the MCP-only `memcal_name` tool.
