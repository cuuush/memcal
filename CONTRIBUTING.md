# Contributing

Python 3.11 or newer. The current runtime has no third-party dependencies, but dependencies
are allowed when they materially simplify the implementation or improve correctness. Declare
them in `pyproject.toml` and update `install.sh` so a normal install remains sufficient.

```bash
git clone https://github.com/cuuush/memcal && cd memcal
python3 -m unittest discover -s tests
```

## Core rules

[AGENTS.md](AGENTS.md) defines repository structure, behavioral contracts, and the working flow.

Key requirements:

1. **Prompt modifications and heuristic regex additions are lowest-priority remediation paths.**
   State handling belongs in schema constraints or structured lookup logic whenever possible.
2. **Schema migrations require column registration.**
   New columns require updates to `schema.sql`, `db.ADDED_COLUMNS`, and `upsert` `INSERT` statements.
   `migrate()` executes `CREATE TABLE IF NOT EXISTS` on database initialization; schema edits alone
   do not migrate existing databases.
3. **External mutation operations must default to disabled.**
   Settings such as `publish_calendar` must remain disabled by default to prevent unintended writes
   to host systems during testing or execution.

## Testing flow

- Run the smallest relevant unittest modules while iterating, then the full suite before handoff.
- Run `python3 tools/benchmark_temporal.py --layer integration` when a change affects behavior
  exercised by the multi-day ingest, Merge, storage, dream, or brief scenarios.
- Run the live model layer only to evaluate extraction or prompt behavior with a usable provider.
- Benchmark runs report current results without changing tracked files.
- **Test class names describe the behavior under test.** New failure modes require dedicated test classes
  rather than appended methods. Distinct class names prevent class shadowing within module namespaces.
- **Tests pin temporal state explicitly.** Date and time values derive from `db.today()`, `db.now_dt()`,
  or explicit `db.set_today` bindings released during teardown. Tests never query the system clock directly.
  `tools/clock_sweep.py` verifies behavior across dates, hours, and time zones ranging from UTC-5 to UTC+14;
  `--cross` is the cheap shape and the one to reach for, `--zones` the full grid.
- **Entry points reside at the end of test modules.** Test files place `if __name__ == "__main__": unittest.main()`
  at the end of the file to prevent premature termination during test discovery.

## Commits and releases

- Reference an issue in a commit when one exists; creating an issue is not part of the development gate.
- Add notable user-facing changes to the `Unreleased` section of `CHANGELOG.md`.
- Releases follow Semantic Versioning. The changelog heading, `pyproject.toml` version,
  release commit, and annotated `vX.Y.Z` tag must agree.

## Documentation

User-facing setup and operation live in `README.md`. Keep durable explanations there rather
than in isolated logs or dated narrative sections.

## Citing code from an issue or a report

An issue's value is that it says where it looked and how it checked. A citation the
reader cannot open is asking to be taken on trust, which is the opposite.

- **Cite by commit SHA, never by branch.** `.../blob/<sha>/memcal/events.py#L580` is
  stable for ever; the same link against `main` drifts by tens of lines with the next
  refactor, and points at nothing at all once a file moves. `main` was re-founded as an
  orphan commit on 2026-08-28 (`be7168c`); everything before it is on
  `archive/full-history`, whose tip `3a288bc` is where `docs/` was removed —
  so `3a288bc^` is the last SHA at which those pages exist.
- **Quote anything outside the tree rather than referencing it.** A paragraph pasted into
  the issue survives the file being moved, renamed or deleted. A path does not.
- **Say what you re-derived and what you copied.** A line number that was accurate when
  written and is stale now reads as a fixed defect: an issue citing code that is no
  longer at the cited line looks closed and may not be.
