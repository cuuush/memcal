---
name: memcal-install
description: >
  Install MemCal and connect its local stdio MCP server or native Hermes/OpenClaw
  integration. Use when asked to install, set up, or connect MemCal to an agent.
  Not for editing MemCal internals or replacing a memory provider without approval.
license: MIT-0
metadata:
  author: cuuush
  version: "0.2.0"
  category: agent-memory
  tags: "memory, calendar, mcp, hermes, openclaw, install"
---

# Install MemCal

MemCal stores events, tasks, questions, notes, and source evidence locally. This
skill installs MemCal, not the host agent. Requires Python 3.11+ and SQLite FTS5.
macOS supports native Messages/Calendar/Reminders; Linux supports email and MCP,
with optional chat sources. There is no hosted HTTP MCP endpoint.

## Before changing anything

1. Ask which host to connect if unclear: generic MCP, Hermes, or OpenClaw.
2. Check for an existing installation and store. Never overwrite a store or
   silently replace an existing memory provider. Explain host/configuration
   changes and obtain approval before installing or enabling the integration.
3. Show the [privacy guide](https://cuuush.github.io/memcal/privacy/) before
   connecting real accounts. Local storage does not imply local inference:
   selected source text, neighboring messages, and relevant existing memory can
   reach the configured model backend. Remote models can incur external charges;
   MemCal's open-source license does not include model usage or subscriptions.
   A genuinely local compatible model server with remote fallback disabled can
   keep MemCal model inference local. Source services and the host agent have
   their own network behavior and model configuration. Do not promise offline
   operation or that no data leaves the machine.

## Stable install (0.8.12)

Prefer the released PyPI package, not development `main`. Use an isolated virtual
application environment instead of modifying system Python:

```bash
python3 -m venv "$HOME/.local/share/memcal/venv"
"$HOME/.local/share/memcal/venv/bin/python" -m pip install 'memcal==0.8.12'
"$HOME/.local/share/memcal/venv/bin/memcal" doctor
"$HOME/.local/share/memcal/venv/bin/memcal" setup
"$HOME/.local/share/memcal/venv/bin/memcal" brief
```

If that virtual environment already exists, inspect it first instead of
recreating it. Below, `memcal` means this environment's absolute `bin/memcal`
path (or its directory added to PATH). Default store: `~/.memcal`; `MEMCAL_HOME`
selects another store. Setup configures a model backend; it does not grant source
permissions. On macOS, `memcal permissions setup` guides protected-source access.
Do not connect accounts, schedule collection, or grant permissions implicitly.

For checkout development only, use the repository contributor instructions.
Do not run `install.sh` from a worktree, which can repoint the shared launcher.

## Generic MCP

Configure the client's stdio server with the **absolute Python path from the
same environment**, for example (replace `/absolute/path` with the actual path):

```json
{
  "mcpServers": {
    "memcal": {
      "command": "/absolute/path/.local/share/memcal/venv/bin/python",
      "args": ["-m", "memcal.mcp_server"],
      "env": {"MEMCAL_HOME": "/absolute/path/.memcal"}
    }
  }
}
```

Use absolute paths rather than assuming clients expand `~` or `${HOME}`. Merge
this entry into existing configuration; preserve other servers. Restart the
client, confirm MemCal tools are listed, and smoke-test `memcal_brief`. The
0.8.12 `memcal` console command launches the CLI, **not** the MCP server.

Client locations and details: [MCP guide](https://cuuush.github.io/memcal/clients/mcp/).

## Hermes (already installed)

```bash
memcal hermes setup --yes
hermes memory setup
```

`--yes` skips MemCal's prompt; use it only after approval. Setup stages the shipped
provider under `MEMCAL_HOME/integrations/hermes/memcal` and links it into
`HERMES_HOME/plugins/memcal` (default `~/.hermes`). Then select MemCal in Hermes's
memory setup and restart Hermes. Verify a `MEMCAL SNAPSHOT` on the first
substantive turn and the MemCal tools. Unchanged turns need not inject a second
snapshot. No manual checkout symlink is needed for a stable PyPI installation.

After a direct pip upgrade, run `memcal hermes refresh --yes` and restart Hermes.
`memcal update` refreshes existing managed copies; local edits are preserved and
reported as conflicts. [Hermes guide](https://cuuush.github.io/memcal/integrations/hermes/).

## OpenClaw (already installed)

Requires the OpenClaw CLI with plugin and MCP commands (documented minimum:
2026.7.1). After approval:

```bash
memcal openclaw setup --yes
openclaw gateway restart
memcal openclaw status
```

PyPI ships the plugin; setup stages a managed copy under
`MEMCAL_HOME/integrations/openclaw`, enables it, and registers stdio MCP using the
installing Python. The native plugin injects context; writes use MCP tools.
Verify plugin and MCP registration with `status`, then inspect tools and context
in a real session. After a direct pip upgrade, run
`memcal openclaw refresh --yes` and restart the gateway. `memcal update` refreshes
existing managed copies without adding new host integrations.
[OpenClaw guide](https://cuuush.github.io/memcal/integrations/openclaw/).

## Sources and verification

Configure only sources the user requested. Linux cannot provide native macOS
Messages or EventKit. IMAP/Proton Bridge and optional chat extras are documented
in the [source guides](https://cuuush.github.io/memcal/quickstart/).

- Confirm `doctor` and `brief` run; report warnings rather than claiming success.
- Confirm host tools and/or actual injected context, not just installation exit status.
- Keep credentials, stores, model-call traces, and personal source text out of git.
- Test with a temporary `MEMCAL_HOME`, never the live store.
- Do not install scheduling unless requested. Linux `memcal schedule install`
  prints cron instructions, not a macOS launchd installation.

This installation skill alone is MIT-0 (see LICENSE). MemCal application code
remains MIT. Do not advertise unverified superiority over other memory systems.
