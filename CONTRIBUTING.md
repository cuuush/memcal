# Contributing

Python 3.11 or newer. The current runtime has no third-party dependencies, but dependencies
are allowed when they materially simplify the implementation or improve correctness. Declare
them in `pyproject.toml` and update `install.sh` so a normal install remains sufficient.

```bash
git clone https://github.com/cuuush/memcal && cd memcal
python3 -m unittest discover -s tests
```

## Core invariants

[AGENTS.md](AGENTS.md) defines repository structure, invariant contracts, and the working flow.

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
  exercised by the multi-day ingest, resolution, storage, dream, or brief scenarios.
- Run the live model layer only to evaluate extraction or prompt behavior with a usable provider.
- **Test class names describe the behavior under test.** New failure modes require dedicated test classes
  rather than appended methods. Distinct class names prevent class shadowing within module namespaces.
- **Tests pin temporal state explicitly.** Date and time values derive from `db.today()`, `db.now_dt()`,
  or explicit `db.set_today` bindings released during teardown. Tests never query the system clock directly.
  `tools/clock_sweep.py` verifies behavior across dates, hours, and time zones ranging from UTC-5 to UTC+14.
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
