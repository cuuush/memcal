## Change

<!-- Describe the issue, the user-visible impact, and what changed. -->

## Verification

<!-- List the focused regression tests and the result of the buffered full suite.
     For documentation changes, include the result of mkdocs build --strict.
     If you ran a comparative evaluation in PACBench, link the run here. -->

## Checklist

- [ ] New schema columns are registered in `db.ADDED_COLUMNS` and covered by the relevant writes.
- [ ] Tests use isolated stores and pin time when behavior depends on it.
- [ ] External writes remain opt-in.
- [ ] User-facing changes are documented in `CHANGELOG.md` under Unreleased.
