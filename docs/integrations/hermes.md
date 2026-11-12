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

- **Inject** — `prefetch()` renders a fresh brief plus up to three mentioned wiki
  pages. A changed brief is injected as `MEMCAL SNAPSHOT`; an unchanged turn
  carries a compact `MEMCAL CURRENT` confirmation instead. A snapshot is
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

The default snapshot reaches 30 days ahead and includes month/year lookup guidance.
To confirm a guessed sender name, use the Conversations page or the CLI; Hermes
does not currently expose the MCP-only `memcal_name` tool.
