# Integrations overview

| Integration | Shape | Setup |
|---|---|---|
| [Hermes](hermes.md) | Native memory provider: snapshot injection, turn archival, 17 tools | Link the plugin, `hermes memory setup` |
| [OpenClaw](openclaw.md) | Plugin + stdio MCP: prompt injection, turn archival | `memcal openclaw setup` |
| [Any MCP harness](../clients/mcp.md) | Generic: brief + stdio tools | `python3 -m memcal.mcp_server` |
| [Custom sources](custom.md) | New transports as plugins | `~/.memcal/plugins/` or entry points |

All integrations share the same behavior contract: a fresh snapshot on every
turn, relevant pages when a known entity is mentioned, source and conversation
lookup, deterministic typed writes, and archival of user turns — never assistant
replies, tool output, or injected snapshots as if they were user facts.
