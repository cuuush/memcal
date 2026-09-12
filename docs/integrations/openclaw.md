# OpenClaw

The native OpenClaw integration injects a freshly rendered snapshot before every
prompt, prefetches relevant wiki pages, and archives each inbound user turn
exactly once.

```bash
memcal openclaw setup
openclaw gateway restart
memcal openclaw status
```

The plugin points at this checkout, so code changes need no reinstall. At
runtime, `before_prompt_build` injects context and `message_received` archives
the user turn under the session thread (spooling it if gated); writes go through
the MCP tools. `status` verifies the plugin link and the MCP registration.
