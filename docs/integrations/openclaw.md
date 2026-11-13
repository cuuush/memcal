# OpenClaw

Memcal connects to OpenClaw in two parts: a plugin supplies fresh memory context
and saves incoming user messages, while an MCP server gives the agent tools to
look up and update memory.

## Before you start

Install and set up Memcal and OpenClaw separately. OpenClaw must be on your PATH
and be version 2026.7.1 or newer. Run `memcal doctor` and `memcal brief` to check
your Memcal installation. See [Installation](../hosting/installation.md) for
requirements and first-run setup, and review the [privacy guide](../privacy.md)
before connecting sources.

## Connect Memcal to OpenClaw

1. Run the setup command from the Memcal installation you normally use:

    ```bash
    memcal openclaw setup
    ```

    Confirm the prompt. To skip it in a non-interactive install, use
    `memcal openclaw setup --yes`.

2. Restart the gateway and check the registration:

    ```bash
    openclaw gateway restart
    memcal openclaw status
    ```

    Status checks both the running plugin and the MCP registration. Both should
    report `ok`. If a setup step fails, the command reports which step failed;
    correct the problem and run setup again.

3. Start a conversation and ask about your plans. The plugin supplies a
   `MEMCAL SNAPSHOT` before the prompt, and the agent can use the Memcal MCP
   tools, including `memcal_brief` and `memcal_open`, for further checks.

Setup links and enables the plugin and registers the local stdio MCP server.
The registration uses the Python interpreter running setup, its Memcal source
path, and the selected store. For a packaged install, setup stages the plugin in
`MEMCAL_HOME/integrations/openclaw` (by default `~/.memcal/integrations/openclaw`)
and records the runtime details there. A checkout install links its
`integrations/openclaw` directory instead, so plugin source edits take effect
without reinstalling. Setup and status also remove stale OpenClaw plugin load
paths ending in `integrations/openclaw` when those directories no longer exist.

## Keep the integration up to date

After a direct `pip install --upgrade memcal`, refresh the integration and restart
the gateway:

```bash
memcal openclaw refresh --yes
openclaw gateway restart
memcal openclaw status
```

`memcal update` refreshes existing managed copies automatically; it does not
install new host integrations or change host registration. Locally edited
managed files are preserved, and refresh reports a conflict instead of
overwriting them. Checkout installations use the linked source directory.

## How memory reaches OpenClaw

Before each prompt, the plugin asks Memcal to render a fresh snapshot and add it
to the agent's context. It also retrieves up to three wiki pages named in the
prompt, apart from the user's own page when those facts are already in the
brief. The snapshot replaces older snapshots as the current context. If the
context read fails, the plugin logs a warning.

The plugin also archives nonempty incoming user messages under an OpenClaw
session thread. Messages admitted by Memcal's gate are queued for later
processing. When OpenClaw supplies a message ID, repeated delivery of that ID
within the same session is deduplicated; without an ID, Memcal generates one.
Assistant responses and tool outputs are not archived by this hook.

The MCP server exposes 22 tools and the `memcal://brief` resource. Tools can
open records and evidence, search memory, and make typed updates to Memcal's
private state. These updates do not change events in the user's calendar. See
the [MCP server guide](../api/mcp.md) for the tool reference.
