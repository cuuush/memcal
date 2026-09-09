# memcal

Python 3.11 or newer. Prefer the standard library for small adapters, but use a maintained
dependency when it materially removes custom machinery or improves correctness. Declare it
in `pyproject.toml`, keep installation reproducible, and update `install.sh` in the same change.

This file is a map and a short set of hard rules. User-facing operation belongs in
`README.md`; do not add dev notes or dated narratives here.

## System shape

Main ownership:

- `config.py`, `db.py`, `schema.sql`: configuration, connection, and schema.
  `settings.py` is the schema for what `config.py` reads — every `MEMCAL_*` knob, its
  meaning and bounds, and the only writer of a store's `.env`. A new setting belongs in
  both, and a test holds the two lists to each other.
- `archive.py`, `gate.py`, `identity.py`, `textclean.py`: ingest spine. `identity.py`
  is a dictionary and stays one — no model call belongs on the path every arriving line
  takes. `whois.py` holds the one identity call there is, asked for explicitly, and
  writes back through `identity.link` at an evidence rank Contacts still outranks.
- `sources/`: transports. `sources/providers/`: platform policy layered on a transport.
- `dream/`: bundle, propose, merge, apply, sweep, and run orchestration.
- `events.py`, `series.py`, `todos.py`, `questions.py`, `wiki.py`: typed stores and merge rules.
- `legacy.py`: temporary read compatibility and idempotent retirement for removed stores.
- `brief.py`, `detail.py`, `presentation.py`, `live.py`: context, row detail, shared
  user-facing vocabulary, and immediate writes.
- `llm.py`, `calls.py`, `trace.py`: provider-neutral model execution and durable call records.
- `web.py`, `static/`, `cli.py`, `mcp_server.py`, `integrations/`: user and agent surfaces.
- `schedule.py`: the launchd agent — nightly pass, wake-up catch-up.

For new schema columns, update `schema.sql`, `db.ADDED_COLUMNS`, and every named-column
`INSERT`/upsert path.

All application model calls go through `llm.client_for(cfg)`. Construct provider clients
directly only in provider contract tests and provider-specific inspection commands.

The product goal is calendar and to-dos in conversational context: broad brief, deep
detail on demand, typed state instead of free-form memory, and a harness-agnostic core.

- Keep dream nightly; immediate corrections belong to the assistant's typed tools.
- Daytime writes and dream must preserve the same event identity and supporting evidence.
  Re-reading a statement must not duplicate its applied change. Older evidence must not
  undo a correction; genuinely newer evidence must remain actionable.
- For ambiguous event association, heuristics nominate candidates and the model judges
  meaning. Code owns exact identities, validation, transactions, and replay safety.
- Automatic email relevance should control priority and presentation, not permanently
  exclude content from review. Preserve explicit user exclusions and mutes.

## Working flow

- Build the requested change directly. GitHub issues are optional coordination records,
  never a prerequisite for implementation.
- Run the smallest relevant unittest modules while iterating, then run
  `python3 -m unittest discover -s tests` before handoff.
- Run `python3 tools/benchmark_temporal.py --layer integration` when a change affects
  ingest, Merge, typed storage, dream application, or brief behavior that the
  scenario corpus covers. A before/after pair is useful for ambiguous regressions, not
  required by default.
- Run the live model layer only when evaluating extraction or prompt behavior and usable
  provider credentials are available. A capacity or authentication void is diagnostic,
  not a release signal.
- Benchmark runs must not mutate tracked files. Keep intentional output in ignored local
  artifacts or CI artifacts.
- Matching and write-precedence changes need lifecycle tests combining typed tool calls,
  source ingestion, dream, and replay. Check both duplicate prevention and false merges.
- Keep benchmark expectations independent of implementation output and out of model input.
  Report retrieval, semantic, and application failures separately; oracle-driven
  integration checks do not establish model accuracy.
- A deterministic regression gets a unittest class whose name describes the behavior;
  do not reuse a class name in one scope.
- Keep `if __name__ == "__main__": unittest.main()` at the end of a test file.
- Tests pin their date/time through `db`; they never depend on the machine clock.
- Keep user-facing changes under `CHANGELOG.md`'s `Unreleased` section as part of the
  change that introduces them. Use the Added, Changed, Fixed, and Removed headings as
  needed; omit empty headings.
- Releases use Semantic Versioning. To release, move the Unreleased entries under a
  `## [X.Y.Z] - YYYY-MM-DD` heading, restore an empty Unreleased section, set the same
  version in `pyproject.toml`, commit, and create an annotated `vX.Y.Z` tag. Never tag
  first: the release commit must already contain its changelog and version.
- A checked-in tool must be safe and useful to run again. One-shot repairs belong only in
  git history.
- Put durable user-facing findings in `README.md`; keep this file limited to repository
  rules and navigation.
