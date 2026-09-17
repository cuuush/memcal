# memcal

**Memory + Calendar for AI agents.** A coding agent can install and wire MemCal
into Hermes, OpenClaw, or any MCP harness from this file — no product tour required.
Contributor layout and hard rules live further down.

Canonical docs: https://cuuush.github.io/memcal/ · skill pack: [`skills/memcal-install`](skills/memcal-install/SKILL.md)

## Install MemCal for an agent harness

### 0. Preconditions

- Python 3.11+, SQLite with FTS5
- One model backend: Codex (default), Claude Code, Antigravity, Grok, or OpenRouter
- **macOS** for Messages / WhatsApp / Calendar / Reminders / launchd scheduling
- **Linux**: use email (IMAP / Proton Bridge) + MCP (and Slack/Telegram if desired); do not promise iMessage or EventKit

Never write to a live `~/.memcal` during development tests — use tmp homes. For a
real user install, `~/.memcal` is the store.

### 1. Install the CLI (from this checkout)

```bash
git clone https://github.com/cuuush/memcal.git
cd memcal
./install.sh          # PATH launcher via PYTHONPATH; edits apply immediately
memcal setup          # provider + model → ~/.memcal/.env
memcal doctor
memcal brief
```

PyPI alternate: `pip install memcal` then the same `setup` / `doctor` / `brief` sequence.
Chat extras: `pip install "memcal[slack]"` or `"memcal[chat]"`.

`MEMCAL_HOME` (default `~/.memcal`) selects the store; `MEMCAL_SRC` points Hermes at
this checkout when not on `PYTHONPATH`.

### 2. Choose a harness path

| Goal | Path |
|---|---|
| Hermes agent | Native memory provider — §3 |
| OpenClaw agent | Plugin + MCP — §4 |
| Cursor / Claude Code / any MCP client | Stdio MCP only — §5 |
| Linux personal sources | Email (+ optional Slack/Telegram) — §6 |

### 3. Hermes (memory provider)

```bash
# From the memcal checkout:
mkdir -p ~/.hermes/plugins
ln -sfn "$(pwd)/integrations/hermes/memcal" ~/.hermes/plugins/memcal
hermes memory setup
```

Verify: start a Hermes session and confirm a `MEMCAL SNAPSHOT` / `MEMCAL CURRENT`
block appears; tools `memcal_open`, `memcal_activity`, `memcal_add`, … should list.
Set `MEMCAL_SRC` if Hermes cannot import `memcal` (defaults to `~/code/memcal`).

Details: https://cuuush.github.io/memcal/integrations/hermes/

### 4. OpenClaw (plugin + MCP)

```bash
memcal openclaw setup     # links plugin, enables it, registers stdio MCP
openclaw gateway restart
memcal openclaw status    # plugin + MCP checks
```

Non-interactive / CI: ensure `openclaw` is on PATH; `memcal openclaw setup` may
prompt — prefer answering yes, or run the underlying `openclaw plugins install
--link …` / `plugins enable memcal` / `mcp set memcal …` sequence the CLI uses.

Details: https://cuuush.github.io/memcal/integrations/openclaw/

### 5. Any MCP harness (Cursor, Claude Code, …)

```bash
python3 -m memcal.mcp_server   # stdio
```

Register that command as an MCP server in the client. Resource `memcal://brief`
exposes the snapshot; tools cover recall and typed writes.
Optional: inject `brief.md` from `$MEMCAL_HOME` into the system prompt.

Details: https://cuuush.github.io/memcal/clients/mcp/

### 6. Linux sources (email + MCP)

Mac-local Messages / EventKit are not available. Minimum credible path:

```bash
# After memcal setup:
# Put Proton Bridge (or IMAP) credentials in ~/.memcal/.env — see docs/sources/email.md
memcal ingest email --limit 50
memcal brief
python3 -m memcal.mcp_server
```

Optional chat on Linux: Slack or Telegram (`pip install "memcal[slack]"` /
`"memcal[telegram]"`), then `memcal ingest slack --limit 20`.

Nightly scheduling on Linux is not launchd — use cron calling `memcal dream`
(or refuse / document; see hosting docs). Do not run `memcal schedule install`
expecting macOS launchd behavior on Linux.

### 7. Done when

- `memcal doctor` is clean enough to run
- `memcal brief` prints a snapshot (even if empty)
- Chosen harness shows memcal tools / injection
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
