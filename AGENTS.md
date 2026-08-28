# memcal

Python 3.11 or newer. Prefer the standard library for small adapters, but use a maintained
dependency when it materially removes custom machinery or improves correctness. Declare it
in `pyproject.toml`, keep installation reproducible, and update `install.sh` in the same change.

This file is a map and a short set of hard rules. User-facing operation belongs in
`README.md`; do not add dev notes or dated narratives here.

## System shape

Main ownership:

- `config.py`, `db.py`, `schema.sql`: configuration, connection, and schema.
- `archive.py`, `gate.py`, `identity.py`, `textclean.py`: ingest spine.
- `sources/`: transports. `sources/providers/`: platform policy layered on a transport.
- `dream/`: bundle, propose, resolve, apply, sweep, and run orchestration.
- `events.py`, `series.py`, `todos.py`, `wiki.py`: typed stores and merge rules.
- `brief.py`, `detail.py`, `live.py`: context, row detail, and immediate writes.
- `llm.py`, `calls.py`, `trace.py`: provider-neutral model execution and durable call records.
- `web.py`, `static/`, `cli.py`, `mcp_server.py`, `integrations/`: user and agent surfaces.
- `schedule.py`: nightly and catch-up launchd jobs.

For new schema columns, update `schema.sql`, `db.ADDED_COLUMNS`, and every named-column
`INSERT`/upsert path.

All application model calls go through `llm.client_for(cfg)`. Construct provider clients
directly only in provider contract tests and provider-specific inspection commands.

The product goal is calendar and to-dos in conversational context: broad brief, deep
detail on demand, typed state instead of free-form memory, and a harness-agnostic core.

## Working flow

- Build the requested change directly. GitHub issues are optional coordination records,
  never a prerequisite for implementation.
- Run the smallest relevant unittest modules while iterating, then run
  `python3 -m unittest discover -s tests` before handoff.
- Run `python3 tools/benchmark_temporal.py --layer integration` when a change affects
  ingest, resolution, typed storage, dream application, or brief behavior that the
  scenario corpus covers. A before/after pair is useful for ambiguous regressions, not
  required by default.
- Run the live model layer only when evaluating extraction or prompt behavior and usable
  provider credentials are available. A capacity or authentication void is diagnostic,
  not a release signal.
- Keep generated benchmark history only when the run is an intentional evaluation result.
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
  invariants and navigation.
