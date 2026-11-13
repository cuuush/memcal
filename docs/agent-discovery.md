# Agent discovery publication checklist

This document prepares factual listings. It is not evidence that a listing has
been published. MemCal is MIT; only `skills/memcal-install/` is MIT-0.

## Proposed description

MemCal stores events, tasks, questions, notes, and source evidence for
personal-assistant agents. It exposes local stdio MCP tools and native Hermes
and OpenClaw integrations. Python 3.11+ is required. Native macOS sources need
macOS; Linux supports email and MCP with optional chat sources. Storage is local,
but configured remote model backends receive source text and relevant context
and may charge for usage. Local model servers are supported; local-only inference
requires a genuinely local endpoint and no remote fallback. Host agents must be
installed and configured separately.

Do not claim comparative superiority before a controlled benchmark. Do not call
this a hosted HTTP MCP service or promise that all activity remains offline.

## Skills directories

The publishing unit is `skills/memcal-install/`, including its MIT-0 license,
not the application's MIT-licensed source. ClawHub publication needs a logged-in
publisher, an available slug, and successful security checks. Follow the current
[ClawHub publication guide](https://docs.openclaw.ai/clawhub/publishing).
Do not run a publish command until publication is coordinated.

For skills.sh, the public repository installation path is:

```bash
npx skills add https://github.com/cuuush/memcal --skill memcal-install
```

This installs agent instructions, not the MemCal application or an active memory
provider. Do not manufacture installs to affect directory ranking.

## Official MCP Registry

The intended namespace is `io.github.cuuush/memcal`. GitHub-based authentication
must establish control of that namespace. The README includes the ownership
marker, but **the already-published 0.8.11 PyPI description does not**. A subsequent
explicitly approved package release must publish the marker before registration.
Do not overwrite 0.8.11 or tag a release merely to publish this documentation.

Use the current [Registry quickstart](https://modelcontextprotocol.io/registry/quickstart)
and [PyPI ownership requirements](https://modelcontextprotocol.io/registry/package-types).
Minimal metadata should use the official schema, a factual short description,
the GitHub repository, a specific published package version, and stdio transport.

### Launcher blocker

The PyPI console command `memcal` is the application CLI, not an MCP entrypoint.
A bare package listing that launches `uvx memcal` is incorrect. The following
explicit client launch is verified for 0.8.11:

```json
{
  "command": "uvx",
  "args": ["--from", "memcal==0.8.11", "python", "-m", "memcal.mcp_server"]
}
```

Configure an absolute `MEMCAL_HOME` for the desired store. This requires uv and
can download the package. An absolute installed-environment Python with
`-m memcal.mcp_server` also works and avoids a runtime download.

The checked-in `server.json` uses the standard PyPI launcher and package argument
`mcp`, implemented by this branch. It is **a draft, not publishable against the
existing 0.8.11 artifact**, which does not include the new subcommand or README
marker. At the next approved release, synchronize both manifest versions with
the package version, publish the package, verify `uvx memcal@VERSION mcp` against
that actual artifact, then authenticate and publish Registry metadata.

The existing Python module launcher remains compatible. This adds no remote
endpoint and changes no host agent configuration.

### Version checks

MCP `initialize` must report the installed package version. The published 0.8.11
artifact reports a stale server version; the next release must include the dynamic
version fix before Registry publication. Verify actual installed artifacts,
MCP initialization/tools, and the final metadata execution path in temporary
homes. Do not modify host accounts or the live MemCal store for these checks.
