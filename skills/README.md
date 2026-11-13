# MemCal skills for AI coding assistants

Structured skill definitions for Claude Code, Codex, Cursor, OpenClaw, and any
assistant that supports the [skills standard](https://github.com/anthropics/skills).

| Skill | Purpose | Install |
|---|---|---|
| [`memcal-install`](./memcal-install/) | Install MemCal and wire Hermes, OpenClaw, or MCP — Mac-first; email+MCP on Linux | `npx skills add https://github.com/cuuush/memcal --skill memcal-install` |

Also see root [`AGENTS.md`](../AGENTS.md) for the same harness install path without loading a skill.

The installation skill is licensed separately under [MIT-0](memcal-install/LICENSE).
The MemCal application remains MIT. Stable installation uses PyPI 0.8.11;
installing a skill does not install MemCal or activate a host memory provider.
