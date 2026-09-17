---
name: memcal-install
description: >
  Install MemCal and wire it into MCP (Cursor/Claude), Hermes (memory provider),
  or OpenClaw (plugin + MCP). Mac-first; on Linux use email + MCP. TRIGGER when:
  user says "install memcal", "set up memcal", "wire memcal into Hermes/OpenClaw",
  "add memcal memory", or asks how to connect MemCal to an agent. DO NOT TRIGGER
  when: editing memcal internals (read repo AGENTS.md), or product-only questions
  (point at https://cuuush.github.io/memcal/).
license: MIT
metadata:
  author: cuuush
  version: "0.1.1"
  category: agent-memory
  tags: "memory, calendar, mcp, hermes, openclaw, install"
---

# memcal-install

Install MemCal for a personal-assistant agent **without a product tour**. Prefer
copy-paste blocks below. Ground truth: repo `AGENTS.md` (Install section) and
https://cuuush.github.io/memcal/integrations/ · https://cuuush.github.io/memcal/clients/mcp/.

**Counts:** MCP **22** tools (+ `memcal://brief`); Hermes **21** tools. Host agents
required for Hermes/OpenClaw — do not claim memcal installs them. Stdio MCP only
(no hosted HTTP / Smithery URL).

## Decide the path (ask once if unclear)

| User intent | Path |
|---|---|
| Cursor / Claude Desktop / generic MCP | § MCP first |
| Hermes | § Hermes (Hermes already installed) |
| OpenClaw | § OpenClaw (`openclaw` ≥ 2026.7.1) |
| Linux machine | § Linux (email + MCP); never promise iMessage/EventKit |

Default product path is **macOS**. Linux is **email + MCP**.

## Shared install

```bash
git clone https://github.com/cuuush/memcal.git
cd memcal
./install.sh                 # do NOT pass --nightly on Linux
export PATH="$HOME/.local/bin:$PATH"
memcal doctor
memcal setup
memcal brief
```

Alternate: `pip install memcal` then the same sequence.

Env: `MEMCAL_HOME` (default `~/.memcal`), `MEMCAL_SRC` / checkout `cwd` for `-m memcal.mcp_server`.

Before connecting real accounts, show https://cuuush.github.io/memcal/privacy/.

## MCP first (any harness)

```bash
python3 -m memcal.mcp_server
```

Cursor (`~/.cursor/mcp.json` or project `.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "memcal": {
      "command": "python3",
      "args": ["-m", "memcal.mcp_server"],
      "env": { "MEMCAL_HOME": "/home/YOU/.memcal" }
    }
  }
}
```

From a git checkout, add `"cwd": "/path/to/memcal"` if the module does not resolve.
Claude Desktop uses the same JSON (macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`;
Linux: `~/.config/Claude/claude_desktop_config.json`). Restart client; smoke `memcal_brief`.

## Hermes

Requires Hermes already installed.

```bash
mkdir -p ~/.hermes/plugins
ln -sfn "$(pwd)/integrations/hermes/memcal" ~/.hermes/plugins/memcal
hermes memory setup
```

Symlink the **package directory** `integrations/hermes/memcal`, not the parent.
Done when: `MEMCAL SNAPSHOT` / `MEMCAL CURRENT` inject and **21** tools list.

## OpenClaw

Requires `openclaw` ≥ `2026.7.1` on PATH.

```bash
memcal openclaw setup --yes
openclaw gateway restart
memcal openclaw status
```

Done when: status reports plugin + MCP OK. Writes go through MCP; injection is the plugin.

## Linux sources

```bash
# Proton Bridge / IMAP in ~/.memcal/.env
memcal ingest email --limit 50
memcal brief
python3 -m memcal.mcp_server
```

Optional Slack/GroupMe/Telegram. Do not expect launchd from `memcal schedule install`
on Linux (refuses + cron note after #78). Prefer documenting cron → `memcal dream`
rather than inventing schedule config here.

## Verification checklist

- [ ] `memcal doctor` / `memcal brief` run
- [ ] Harness shows injection and/or tools (MCP 22 or Hermes 21)
- [ ] No secrets committed; store under `MEMCAL_HOME`

## Out of scope

- memcal core / dream / field_sources / freshness
- Hygiene version bumps or Linux schedule cron copy (live in #78)
- Fake directory submissions; hosted HTTP MCP
- Replacing an existing memory system silently

## Handoff

Point at Quickstart / Sources: https://cuuush.github.io/memcal/quickstart/
