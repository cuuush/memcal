# Integrations overview

| Integration | Shape | Setup |
|---|---|---|
| [Hermes](hermes.md) | Native memory provider: snapshot injection, turn archival, **21 tools** | Link the plugin, `hermes memory setup` |
| [OpenClaw](openclaw.md) | Plugin + stdio MCP: prompt injection, turn archival | `memcal openclaw setup` |
| [Any MCP harness](../clients/mcp.md) | Generic: brief + stdio tools (**22 tools**) | `python3 -m memcal.mcp_server` |
| [Custom sources](custom.md) | New transports as plugins | `~/.memcal/plugins/` or entry points |

Coding agents: follow the Install section in [`AGENTS.md`](https://github.com/cuuush/memcal/blob/main/AGENTS.md) or load [`skills/memcal-install`](https://github.com/cuuush/memcal/tree/main/skills/memcal-install) (`npx skills add https://github.com/cuuush/memcal --skill memcal-install`).

All integrations share the same behavior contract: a fresh snapshot on every
turn, relevant pages when a known entity is mentioned, source and conversation
lookup, deterministic typed writes, and archival of user turns — never assistant
replies, tool output, or injected snapshots as if they were user facts.

**Linux tip:** Email + stdio MCP is the best demo path (Slack or GroupMe
also work). Skip iMessage / WhatsApp / Calendar. On Linux, prefer user cron —
`memcal schedule install` refuses launchd there and prints a cron snippet.
