---
name: memcal-install
description: >
  Install MemCal and wire it into Hermes (memory provider), OpenClaw (plugin + MCP),
  or any stdio MCP harness. Mac-first product path; on Linux use email + MCP (optional
  Slack/Telegram). TRIGGER when: user says "install memcal", "set up memcal", "wire
  memcal into Hermes/OpenClaw", "add memcal memory", or asks how to connect MemCal to
  an agent. DO NOT TRIGGER when: editing memcal internals (read repo AGENTS.md), or
  when the ask is only product explanation (point at https://cuuush.github.io/memcal/).
license: MIT
metadata:
  author: cuuush
  version: "0.1.0"
  category: agent-memory
  tags: "memory, calendar, mcp, hermes, openclaw, install"
---

# memcal-install

Install MemCal for a personal-assistant agent **without a product tour**. Prefer the
commands below over inventing config. Ground truth: repo `AGENTS.md` (Install section),
https://cuuush.github.io/memcal/integrations/, and https://cuuush.github.io/memcal/clients/mcp/.

## Decide the path (ask once if unclear)

| User intent | Path |
|---|---|
| Hermes | § Hermes |
| OpenClaw | § OpenClaw |
| Cursor / Claude Code / generic MCP | § MCP-only |
| Linux machine | § Linux (email + MCP); never promise iMessage/EventKit |

Default product path is **macOS** (Messages, Calendar, launchd). Linux is **email + MCP**.

## Shared install

```bash
git clone https://github.com/cuuush/memcal.git
cd memcal
./install.sh
memcal setup
memcal doctor
memcal brief
```

Alternate: `pip install memcal` then the same `setup` / `doctor` / `brief` sequence.

Env: `MEMCAL_HOME` (default `~/.memcal`), `MEMCAL_SRC` (checkout root for Hermes imports).

Before connecting real accounts, show https://cuuush.github.io/memcal/privacy/.

## Hermes

```bash
mkdir -p ~/.hermes/plugins
ln -sfn "$(pwd)/integrations/hermes/memcal" ~/.hermes/plugins/memcal
hermes memory setup
```

Done when: Hermes injects `MEMCAL SNAPSHOT` / `MEMCAL CURRENT`, and memcal tools list
(`memcal_open`, `memcal_activity`, typed writes, …). If import fails, set `MEMCAL_SRC`
to this checkout (Hermes defaults to `~/code/memcal`).

Docs: https://cuuush.github.io/memcal/integrations/hermes/

## OpenClaw

```bash
memcal openclaw setup
openclaw gateway restart
memcal openclaw status
```

Done when: `memcal openclaw status` reports plugin + MCP OK. Writes go through MCP;
injection is the OpenClaw plugin.

Docs: https://cuuush.github.io/memcal/integrations/openclaw/

## MCP-only (any harness)

```bash
python3 -m memcal.mcp_server
```

Register that stdio command as an MCP server in the client. Optional: prepend
`$MEMCAL_HOME/brief.md` to the system prompt. Resource: `memcal://brief`.

Docs: https://cuuush.github.io/memcal/clients/mcp/

## Linux sources

Do **not** run macOS-only setup (iMessage, EventKit, `memcal schedule install` as launchd).

```bash
# Proton Bridge / IMAP credentials in ~/.memcal/.env — see docs
memcal ingest email --limit 50
memcal brief
python3 -m memcal.mcp_server
```

Optional: `pip install "memcal[slack]"` or `"memcal[telegram]"`, then ingest with a limit.
For nightly reconcile on Linux, use cron → `memcal dream` (document the crontab; don’t
fake launchd).

## Verification checklist

- [ ] `memcal doctor` runs
- [ ] `memcal brief` prints a snapshot
- [ ] Harness shows injection and/or memcal tools
- [ ] No secrets committed; store stays under `MEMCAL_HOME`

## Out of scope

- Rewriting memcal core, dream, or field_sources
- Fake star campaigns / directory submissions
- Replacing an existing memory system silently — MemCal is additive for the agent
- Touching the user’s live `~/.memcal` from test code (use tmp homes in tests)

## Handoff

If install succeeds, point the user at Quickstart and Sources for connecting more
feeds: https://cuuush.github.io/memcal/quickstart/
