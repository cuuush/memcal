# OpenClaw

The native OpenClaw integration injects a freshly rendered snapshot before every
prompt, prefetches relevant wiki pages, and archives each inbound user turn
exactly once.

```bash
memcal openclaw setup --yes   # --yes skips the confirm prompt
openclaw gateway restart
memcal openclaw status
```

PyPI installs ship the native plugin as package data. Setup stages a managed copy
under `MEMCAL_HOME/integrations/openclaw` and records the installing Python,
MemCal source path, store, and version. After a direct `pip install --upgrade memcal`,
run `memcal openclaw refresh --yes` and restart the gateway. `memcal update`
refreshes existing managed copies automatically, but does not install new host
integrations or change host registration. Locally edited managed files are
preserved and refresh reports the conflict.

Checkout installs still link `integrations/openclaw`, so source changes need no
reinstall. At
runtime, `before_prompt_build` injects context and `message_received` archives
the user turn under the session thread (spooling it if gated); writes go through
the MCP tools. `status` verifies the plugin link and the MCP registration.
