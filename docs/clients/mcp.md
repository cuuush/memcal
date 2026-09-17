# Any harness via MCP

The minimum viable integration anywhere: append the brief to a system prompt, and
connect the tool server for depth and writes.

```bash
python3 -m memcal.mcp_server   # stdio MCP server
```

- Read `brief.md` for the snapshot; `MEMCAL_HOME` selects the store.
- `memcal://brief` exposes the same snapshot as a resource.
- **22 tools** on this server (see [MCP server](../api/mcp.md)). Hermes exposes
  **21** of the same surface via prefetch instead of `memcal_brief`.
- Reads (`memcal_brief`, `memcal_open`, `memcal_search_archive`,
  `memcal_conversation`, …) expand depth; typed writes (`memcal_add`,
  `memcal_update`, `memcal_todo`, `memcal_answer`, …) run as code, no model.
- `MEMCAL_HARNESS` / `MEMCAL_SESSION` attribute turns to their conversation.

## Cursor

Add to your MCP config (often `~/.cursor/mcp.json` or project `.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "memcal": {
      "command": "python3",
      "args": ["-m", "memcal.mcp_server"],
      "env": {
        "MEMCAL_HOME": "/home/YOU/.memcal"
      }
    }
  }
}
```

If you run from a git checkout rather than an installed package, set `"cwd"` to
that checkout so `-m memcal.mcp_server` resolves, or use the same Python that
runs `memcal` after `./install.sh`.

## Claude Desktop

Add to `claude_desktop_config.json` (macOS:
`~/Library/Application Support/Claude/claude_desktop_config.json`; Linux:
`~/.config/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "memcal": {
      "command": "python3",
      "args": ["-m", "memcal.mcp_server"],
      "env": {
        "MEMCAL_HOME": "/home/YOU/.memcal"
      }
    }
  }
}
```

Restart the client after editing. Smoke: call `memcal_brief`.

## Linux demo path (email + MCP)

Prefer **email** over macOS-only sources. Slack or GroupMe are fine token-based
alternates if email ingest isn’t set up yet.

1. `./install.sh` (do **not** pass `--nightly` on Linux) then `memcal doctor`
2. `memcal setup` · configure email ingest · `memcal login` for your mail source
3. `memcal ingest email --limit 20` (or your mail source name)
4. Wire MCP with the JSON above · call `memcal_brief` → `memcal_activity` →
   `memcal_update` with flat `source_ids` when correcting from activity

Nightly / daytime collect without launchd: run `memcal ingest email` (or
`memcal schedule run`) from user cron. On Linux, `memcal schedule install`
refuses LaunchAgents and prints a cron snippet — use that instead of launchd.

Tool tables: [MCP server](../api/mcp.md) · [Recall](../api/recall.md) ·
[Remember](../api/remember.md) · [Correct](../api/correct.md).
Native integrations ([Hermes](../integrations/hermes.md),
[OpenClaw](../integrations/openclaw.md)) add per-turn injection and turn archival
on top of this boundary.
