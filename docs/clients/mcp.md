# Any harness via MCP

The minimum viable integration anywhere: append the brief to a system prompt, and
connect the tool server for depth and writes.

```bash
python3 -m memcal.mcp_server   # stdio MCP server
```

- Read `brief.md` for the snapshot; `MEMCAL_HOME` selects the store.
- `memcal://brief` exposes the same snapshot as a resource.
- Reads (`memcal_brief`, `memcal_open`, `memcal_search_archive`,
  `memcal_conversation`, …) expand depth; typed writes (`memcal_add`,
  `memcal_update`, `memcal_todo`, `memcal_answer`, …) run as code, no model.
- `MEMCAL_HARNESS` / `MEMCAL_SESSION` attribute turns to their conversation.

Tool tables: [MCP server](../api/mcp.md) · [Recall](../api/recall.md) ·
[Remember](../api/remember.md) · [Correct](../api/correct.md).
Native integrations ([Hermes](../integrations/hermes.md),
[OpenClaw](../integrations/openclaw.md)) add per-turn injection and turn archival
on top of this boundary.
