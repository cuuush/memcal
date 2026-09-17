# memcal

**Memory + Calendar for AI agents.** A coding agent can install and wire MemCal
into Hermes, OpenClaw, or any MCP harness from this file — no product tour required.
Contributor layout and hard rules live further down.

Canonical docs: https://cuuush.github.io/memcal/ · skill pack: [`skills/memcal-install`](skills/memcal-install/SKILL.md)

## Install MemCal for an agent harness

Mac-first product path. Coding agents: follow this section (or load
[`skills/memcal-install`](skills/memcal-install/SKILL.md)) — no product tour.

Honest tool counts: **MCP 22** (+ `memcal://brief` resource); **Hermes 21**
(prefetch replaces `memcal_brief`). Host agents are required for Hermes/OpenClaw —
memcal does not install them.

### 0. Preconditions

- Python 3.11+, SQLite with FTS5
- One model backend: Codex (default), Claude Code, Antigravity, Grok, or OpenRouter
- **macOS** for Messages / WhatsApp / Calendar / Reminders / launchd scheduling
- **Linux**: email (IMAP / Proton Bridge) + MCP; optional Slack/GroupMe/Telegram — never promise iMessage or EventKit
- Hermes path needs a current Hermes with memory plugins; OpenClaw path needs `openclaw` CLI ≥ `2026.7.1`

Never write to a live `~/.memcal` during development tests — use tmp homes. For a
real user install, `~/.memcal` is the store (`MEMCAL_HOME`).

### 1. Install the CLI (from this checkout)

```bash
git clone https://github.com/cuuush/memcal.git
cd memcal
./install.sh                 # PATH launcher; do NOT pass --nightly on Linux
export PATH="$HOME/.local/bin:$PATH"
memcal doctor
memcal setup                 # provider + model → ~/.memcal/.env
memcal brief
```

PyPI alternate: `pip install memcal` then the same `doctor` / `setup` / `brief` sequence.
Chat extras: `pip install "memcal[slack]"` or `"memcal[chat]"`.

`MEMCAL_SRC` / checkout `cwd` / `PYTHONPATH` from `install.sh` make `python3 -m memcal.mcp_server` resolve.

### 2. Choose a harness path

| Goal | Path |
|---|---|
| Cursor / Claude Desktop / any MCP client | §3 MCP (works without Hermes/OpenClaw) |
| Hermes agent | §4 (requires Hermes already installed) |
| OpenClaw agent | §5 (requires `openclaw` ≥ 2026.7.1) |
| Linux personal sources | §6 email + MCP |

### 3. MCP first (any harness)

Smoke:

```bash
python3 -m memcal.mcp_server   # stdio JSON-RPC; 22 tools + memcal://brief
```

**Cursor** — `~/.cursor/mcp.json` or project `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "memcal": {
      "command": "python3",
      "args": ["-m", "memcal.mcp_server"],
      "env": {
        "MEMCAL_HOME": "/home/YOU/.memcal"
      }
    }
  }
}
```

From a git checkout (not an installed package), also set `"cwd": "/path/to/memcal"`
so `-m memcal.mcp_server` resolves, or use the same Python that runs `memcal` after
`./install.sh`.

**Claude Desktop** — same JSON shape:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Linux: `~/.config/Claude/claude_desktop_config.json`

Restart the client; smoke with `memcal_brief`. Optional: `MEMCAL_HARNESS` /
`MEMCAL_SESSION` for turn attribution. Stdio only today — no hosted HTTP MCP URL.

Details: https://cuuush.github.io/memcal/clients/mcp/

### 4. Hermes (memory provider)

Requires Hermes already installed.

```bash
mkdir -p ~/.hermes/plugins
ln -sfn "$(pwd)/integrations/hermes/memcal" ~/.hermes/plugins/memcal
hermes memory setup
```

Symlink the **`integrations/hermes/memcal` package directory** (not the parent
`hermes/` folder). Set `MEMCAL_SRC` to this checkout if import fails (Hermes may
default to `~/code/memcal`).

Verify: `MEMCAL SNAPSHOT` / `MEMCAL CURRENT` inject; **21** tools list (`memcal_open`,
`memcal_open_source`, typed writes, … — prefetch replaces `memcal_brief`).

Details: https://cuuush.github.io/memcal/integrations/hermes/

### 5. OpenClaw (plugin + stdio MCP)

Requires `openclaw` on PATH (`>=2026.7.1`).

```bash
memcal openclaw setup --yes   # link plugin, enable, register MCP
openclaw gateway restart
memcal openclaw status        # plugins inspect + mcp show
```

`setup` runs `openclaw plugins install --link <checkout>/integrations/openclaw`,
`plugins enable memcal`, and `mcp set memcal` with `python3 -m memcal.mcp_server`
(`cwd` = checkout, `MEMCAL_HOME` set). Plugin points at the checkout — code edits
need no reinstall; restart the gateway after setup.

Details: https://cuuush.github.io/memcal/integrations/openclaw/

### 6. Linux sources (email + MCP)

```bash
# Proton Bridge / IMAP credentials in ~/.memcal/.env — see docs/sources/email.md
memcal ingest email --limit 50
memcal brief
python3 -m memcal.mcp_server
```

Optional: Slack or GroupMe/Telegram (`pip install "memcal[slack]"` / chat extras),
then ingest with a limit. Do not run `memcal schedule install` expecting launchd on
Linux — post-#78 it refuses and prints cron guidance; prefer documenting cron →
`memcal dream` rather than inventing a schedule here.

### 7. Done when

- `memcal doctor` / `memcal brief` run
- Chosen harness shows memcal tools / injection (MCP 22 or Hermes 21)
- Privacy: user has seen https://cuuush.github.io/memcal/privacy/ before connecting real accounts

---

## Contributing in this repository

Python 3.11+. Prefer well-chosen libraries over hand-rolls; declare runtime
deps in `pyproject.toml`. `./install.sh` puts a `memcal` launcher on PATH via
`PYTHONPATH` — code edits take effect immediately. Never run `./install.sh` from
a worktree (see Worktrees).

No lint, typecheck, formatter, or CI beyond publish. Verification is unittest plus
the temporal benchmark. User-facing operation belongs in `README.md`; contributor
workflow in `CONTRIBUTING.md`; harness install belongs above — do not add dated
narratives here.

## Layout (`memcal/` package — not flat files)

- `memcal/config.py`, `memcal/db.py`, `memcal/schema.sql`: configuration, connection, schema.
  `memcal/settings.py` is the schema for what `config.py` reads — every `MEMCAL_*` knob,
  its meaning and bounds, and the only writer of a store's `.env`. A new setting belongs
  in both (a test holds the two lists to each other).
- `memcal/archive.py`, `memcal/gate.py`, `memcal/identity.py`, `memcal/textclean.py`: ingest
  spine. `identity.py` is a dictionary and stays one — no model call on the per-line path.
  `whois.py` holds the one identity call there is, asked for explicitly, writing back
  through `identity.link` at an evidence rank Contacts still outranks.
- `memcal/sources/`: transports; `memcal/sources/polled.py` holds the two shapes a
  message source can have (`PolledSource`, `StreamSource`) — a new platform implements
  hooks rather than another ingest loop. `memcal/sources/providers/`: platform policy
  on a transport.
- `memcal/dream/`: bundle, propose, merge, apply, sweep, run orchestration.
- `memcal/events.py`, `memcal/series.py`, `memcal/todos.py`, `memcal/questions.py`,
  `memcal/wiki.py`: typed stores and merge rules. `memcal/legacy.py` is temporary read
  compatibility plus idempotent retirement for removed stores.
- `memcal/brief.py`, `memcal/detail.py`, `memcal/presentation.py`, `memcal/live.py`:
  context, row detail, shared user-facing vocabulary, immediate writes.
- `memcal/llm.py`, `memcal/calls.py`, `memcal/trace.py`: provider-neutral model execution
  and durable call records.
- `memcal/web.py`, `memcal/static/`, `memcal/cli.py`, `memcal/mcp_server.py`,
  `memcal/integrations/`: user and agent surfaces.
- `memcal/schedule.py`, `memcal/macos/launcher.c`: launchd scheduling and its app-bundle wrapper.

## Worktrees

- One worktree per track: `git worktree add ~/code/memcal-<topic> -b <branch>`,
  a sibling of this checkout. Work in the worktree, never on main directly.
- Commit when the work is done — never leave a worktree with uncommitted changes.
  The branch is the unit of handoff and merge; unstaged work is invisible to the
  merge and lost if the worktree is removed.
- Remove the worktree when its branch merges.
- Never run `./install.sh` from a worktree: it repoints the single `memcal` on PATH at
  that worktree, so every later command — including ones run from this checkout — executes
  the worktree's code, and removing the worktree leaves the launcher pointing at a path
  that no longer exists. Run it from this checkout only, and re-run it here if a worktree
  ever claimed the launcher. `grep MEMCAL_ROOT "$(command -v memcal)"` says which tree
  the command actually runs.

## Hard rules

- New schema columns: update `schema.sql`, `db.ADDED_COLUMNS`, and every named-column
  `INSERT`/upsert path (`migrate()` runs `CREATE TABLE IF NOT EXISTS` — schema edits alone
  do not migrate existing databases).
- All application model calls go through `llm.client_for(cfg)`. Construct provider clients
  directly only in provider contract tests and provider-specific inspection commands.
- Prompt and heuristic-regex edits are the lowest-priority fix. Prefer schema constraints
  or structured lookup logic.
- External writes (Calendar/Reminders publishing) default to disabled.
- Daytime writes and dream preserve the same event identity and evidence: re-reading a
  statement must not duplicate its change; per-field precedence runs on when evidence was
  *said* — older evidence cannot undo a correction, genuinely newer evidence stays actionable.
- For ambiguous association, heuristics nominate and the model judges; code owns identities,
  validation, transactions, replay safety. Keep dream nightly; immediate corrections belong
  to typed tools.
- Automatic email relevance sets priority, never permanent exclusion. Preserve explicit user
  ignores and mutes (a blocked sender's body is never fetched or stored).

## Verify

- Iterate with `python3 -m unittest tests.test_<module>`, then
  `python3 -m unittest discover -s tests` before handoff.
- When touching ingest, merge, typed storage, dream application, or brief rendering, also run
  `python3 tools/benchmark_temporal.py --layer integration` (free, ~1s; oracle answers, so a
  green run says nothing about model accuracy). A before/after pair helps ambiguous
  regressions. `--layer model` costs money/quota — only for deliberate extraction/prompt
  evaluation. A capacity/auth void is diagnostic, not a release signal.
- Benchmarks never touch `~/.memcal` (scratch stores only) and never mutate tracked files;
  output belongs in ignored `tools/bench_output/` or CI artifacts. Keep benchmark
  expectations out of model input; report retrieval, semantic, and application failures
  separately.
- Matching/write-precedence changes need lifecycle tests spanning typed tool calls, source
  ingestion, dream, and replay — covering both duplicate prevention and false merges.
- Tests pin time through `db` (`set_today`/`today()`/`now_dt()`), never the machine clock;
  `tearDown` must release the override. `tools/clock_sweep.py --cross` is the cheap
  timezone check, `--zones` the full grid.
- A deterministic regression gets a new unittest class named for the behavior; never reuse a
  class name in one scope. End every test file with
  `if __name__ == "__main__": unittest.main()`.
- A checked-in tool must be safe to run again. One-shot repairs belong in git history only.

## Releases and safety

- User-facing changes go under `CHANGELOG.md` `Unreleased` (Added/Changed/Fixed/Removed
  headings, omit empties) as part of the change. Never bump the version per change. When
  asked to release, pick from Unreleased (major = breaking/removal, minor = new feature,
  patch = fixes only), then: move entries under `## [X.Y.Z] - YYYY-MM-DD`, restore empty
  Unreleased, set the same version in `pyproject.toml`, commit, annotated `vX.Y.Z` tag.
  Never tag first (a test enforces changelog/version/tag agreement).
- Bump the version only as part of an explicit release, never automatically — not when
  merging a branch, shipping a feature, or "to line up the next tag". If unsure whether
  a release was asked for, leave the version alone.
- Pushing a `vX.Y.Z` tag triggers `.github/workflows/publish.yml`, which builds the
  sdist/wheel and uploads to PyPI via Trusted Publishing (OIDC, no stored token). The
  tag must match `pyproject.toml` or PyPI rejects the upload.
- Never read or write the live `~/.memcal` store during development; tests use tmp dirs,
  benchmarks use scratch homes. Keep secrets, `calls/`, `tools/bench_output/`,
  `transcripts/`, and `*.db` out of version control (see `.gitignore`).
