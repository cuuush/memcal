# Hermes

The native memory-provider integration. Thin by design: the memory system is the
resident process, the agent is transient.

```bash
ln -s /path/to/memcal/integrations/hermes/memcal ~/.hermes/plugins/memcal
hermes memory setup
```

## Every turn

- **Inject** — `prefetch()` renders a fresh brief plus up to three mentioned wiki
  pages (`MEMCAL SNAPSHOT`), deduped per session so repeats cost nothing.
- **Archive** — `on_turn_start` / `sync_turn` file the clean user message under
  the agent stream. Assistant replies, injected snapshots, and tool outputs are
  excluded on these paths.
- **Tools** — 17 schemas: reads (`memcal_open`, `memcal_open_page`,
  `memcal_open_source`, `memcal_conversation`, `memcal_list_days`,
  `memcal_list_month`, `memcal_search_archive`) and typed writes (`memcal_add`,
  `memcal_update`, `memcal_schedule`, `memcal_move_once`, `memcal_merge`,
  `memcal_drop`, `memcal_todo`, `memcal_answer`, `memcal_note`, `memcal_alias`),
  executed as code with same-transaction evidence.
- **Session end** — the brief rewrites, so the next session starts current.

`MEMCAL_HOME` (default `~/.memcal`) and `MEMCAL_SRC` select the store and
checkout. Freshness hints travel on the prefetch path, so long-running sessions
receive current context — including next-day turns, resumes, and compression —
without a second injection copy. See [Daytime freshness](../architecture/freshness.md).
