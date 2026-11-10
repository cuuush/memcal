# Contributing

Use Python 3.11 or newer. Declare dependencies in `pyproject.toml`, which
`install.sh` reads for checkout installs.

```bash
git clone https://github.com/cuuush/memcal && cd memcal
python3 -m pip install -e .
python3 -m unittest discover -b -s tests
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
- Use `python3 -m unittest -b tests.test_core` for a focused run and
  `python3 -m unittest discover -b -s tests` before handoff. For parallel checks
  during development, run `python3 tools/run_tests.py` with optional module names.
- Run comparative memory-system benchmarks in the separate PACBench project.
- Live provider checks use synthetic data in temporary stores; run them only when
  evaluating model behavior with an authorized provider.
- **Test class names describe the behavior under test.** New failure modes require dedicated test classes
  rather than appended methods. Distinct class names prevent class shadowing within module namespaces.
- **Tests pin temporal state explicitly.** Date and time values derive from `db.today()`, `db.now_dt()`,
  or explicit `db.set_today` bindings released during teardown. Tests never query the system clock directly.
  Pins with explicit timezone offsets retain them; naive pins use the local offset
  on the pinned date, including daylight-saving transitions.
  `tools/clock_sweep.py --cross` checks dates and time zones on separate axes and
  includes daylight-saving transitions. Use `--zones` for the full date-by-zone grid.
- **Entry points reside at the end of test modules.** Test files place `if __name__ == "__main__": unittest.main()`
  at the end of the file to prevent premature termination during test discovery.

## Commits and releases

- Reference an issue in a commit when one exists; creating an issue is not part of the development gate.
- Add notable user-facing changes to the `Unreleased` section of `CHANGELOG.md`.
- Releases follow Semantic Versioning. The changelog heading, `pyproject.toml` version,
  release commit, and annotated `vX.Y.Z` tag must agree.

## Documentation

User-facing setup and operation live in `docs/` (published to GitHub Pages)
with a short pointer in `README.md`. Keep durable explanations there rather
than in isolated logs or dated narrative sections.

Check changes to `docs/` or `mkdocs.yml` with a strict local build.

```bash
pip install -e '.[docs]'
mkdocs build --strict
```

Use `mkdocs serve` to preview locally. To publish, run `mkdocs gh-deploy`.
That builds and pushes the site to the `gh-pages` branch, which GitHub Pages serves.

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
