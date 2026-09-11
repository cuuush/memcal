# memcal

Python 3.11+, standard library only (`pyproject.toml` has no dependencies). `./install.sh`
puts a `memcal` launcher on PATH that runs from this checkout via `PYTHONPATH` — code
edits take effect immediately, no reinstall. If dependencies are ever added, declare them
in `pyproject.toml` and update `install.sh` in the same change.

No lint, typecheck, formatter, or CI. Verification is unittest plus the temporal benchmark.
User-facing operation belongs in `README.md`; contributor workflow in `CONTRIBUTING.md`;
do not add dev notes or dated narratives here.

## Layout (`memcal/` package — not flat files)

- `memcal/config.py`, `memcal/db.py`, `memcal/schema.sql`: configuration, connection, schema.
  `memcal/settings.py` is the schema for what `config.py` reads — every `MEMCAL_*` knob,
  its meaning and bounds, and the only writer of a store's `.env`. A new setting belongs
  in both (a test holds the two lists to each other).
- `memcal/archive.py`, `memcal/gate.py`, `memcal/identity.py`, `memcal/textclean.py`: ingest
  spine. `identity.py` is a dictionary and stays one — no model call on the per-line path.
  `whois.py` holds the one identity call there is, asked for explicitly, writing back
  through `identity.link` at an evidence rank Contacts still outranks.
- `memcal/sources/`: transports; `memcal/sources/providers/`: platform policy on a transport.
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
- Never read or write the live `~/.memcal` store during development; tests use tmp dirs,
  benchmarks use scratch homes. Keep secrets, `calls/`, `tools/bench_output/`,
  `transcripts/`, and `*.db` out of version control (see `.gitignore`).
