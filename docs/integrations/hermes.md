# Hermes

The native memory-provider integration. Thin by design: the memory system is the
resident process, the agent is transient.

```bash
memcal hermes setup --yes
hermes memory setup
```

Both PyPI and checkout installs include the provider. Setup copies the versioned
provider into `MEMCAL_HOME/integrations/hermes/memcal` and links it into
`HERMES_HOME/plugins/memcal` (default `~/.hermes`). It records the installed
MemCal source path and store without editing Hermes configuration. An existing
non-symlink plugin directory is never replaced.

After a direct `pip install --upgrade memcal`, run `memcal hermes refresh --yes`
and restart Hermes. `memcal update` refreshes existing managed copies automatically.
Locally edited managed files are preserved and refresh reports the conflict.
For a live checkout link, the original `integrations/hermes/memcal` path remains
available; set `MEMCAL_SRC` to the checkout when using it manually.

## Every turn

The brief and provider instructions distinguish whole-week overviews (answer from
the summary) from specific-event questions (open the record for details and new
messages). The last successful Dream time describes consolidation, not complete
coverage or review of later messages.

- **Inject** — `prefetch()` reads local state without collecting sources or calling
  a model. The first brief, changed brief contents, and newly collected source
  messages arrive as `MEMCAL SNAPSHOT`. Unchanged state adds nothing to the user
  turn. Up to three wiki pages named in the query can still be recalled
  independently of brief changes. A snapshot is
  deduplicated only once it has been observed in delivered context, including
  image-containing turns and their text projections on resume, so a timed-out
  or discarded render never suppresses the next one, and compression re-emits. If
  rendering fails, `MEMCAL UNAVAILABLE` replaces the snapshot rather than leaving a
  stale one standing.
- **Open** — `memcal_open` includes cited evidence, nearby context (up to 1,500
  estimated tokens), and pending messages (up to 50 within 4,000 estimated tokens).
  The sections are labeled separately. Only omitted pending messages need a
  follow-up `memcal_activity` call; its cursor continues after the last shown
  arrival. Reading never acknowledges a review or changes a plan.
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
turns need no brief context at all. On Hermes hosts exposing
`agent.memory_provider.PREFETCH_FAILURE_CONTEXT`, failed, timed-out, or busy reads
produce `MEMORY UNAVAILABLE`. An ordinary unchanged turn reuses the latest
snapshot without calling `memcal_refresh`; an unavailable notice requires a
refresh before claiming current plans. Older hosts silently discard failed
reads, so Memcal keeps the conservative refresh rule on those versions.
Hermes skips prefetch for bare greetings and acknowledgements, so the initial
snapshot arrives on the first substantive turn rather than a greeting like “hi”.

Periodic collection runs separately when scheduling is enabled (default due
interval: five minutes). New messages can change activity or coverage notices in
the brief, causing an update on the next turn. Nightly Dream consolidates messages
into stored plans and facts. The change detector tracks the newest collected
source arrival as well as brief text, so a new message still triggers an update
when displayed activity counts are capped or an unlinked-backlog notice is
already present. Redelivery of the same source item and archival of ordinary
user turns do not trigger updates.

The default snapshot reaches 30 days ahead and includes month/year lookup guidance.
To confirm a guessed sender name, use the Conversations page or the CLI; Hermes
does not currently expose the MCP-only `memcal_name` tool.
