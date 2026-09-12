# Clients overview

Three surfaces, one store:

| Surface | Shape | Start |
|---|---|---|
| [CLI](cli.md) | Direct commands over the store | `memcal brief` |
| [Web UI](web.md) | Loopback dashboard: queue, dream, runs, settings | `memcal ui` |
| [Any harness via MCP](mcp.md) | `brief.md` plus a stdio tool server | `python3 -m memcal.mcp_server` |

Native [Hermes](../integrations/hermes.md) and [OpenClaw](../integrations/openclaw.md)
integrations build on the same boundary: fresh snapshot every turn, user turns
archived once, typed writes as code.
