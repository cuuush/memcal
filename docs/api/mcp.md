# MCP server

A small MCP server over stdio — the tool boundary every harness integration
shares. Native Hermes and OpenClaw integrations wrap it; any other harness can
use the same boundary by reading `brief.md` and connecting:

```bash
python3 -m memcal.mcp_server
```

The server also exposes the brief as the `memcal://brief` resource.

## Reads

`memcal_brief`, `memcal_open`, `memcal_open_page`, `memcal_list_days`,
`memcal_list_month`, `memcal_search_archive`, `memcal_source`,
`memcal_conversation`. See [Recall](recall.md).

## Writes

`memcal_add`, `memcal_update`, `memcal_schedule`, `memcal_move_once`,
`memcal_merge`, `memcal_drop`, `memcal_todo`, `memcal_note`, `memcal_alias`,
`memcal_answer`. Writes run as code — no model — with the change and its
operation record written together. See [Remember](remember.md) and
[Correct](correct.md).

## Sessions

`MEMCAL_HARNESS` and `MEMCAL_SESSION` attribute turns to their conversation, so
archived user turns file under the right thread and write operations carry their
cause. Write tools stay enabled; nothing here calls a model on the harness's
behalf.
