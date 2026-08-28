<!--
  A version's one sentence is its tag message, its entries are its commit subjects, and
  its issue list is its commit bodies. Three facts, each written exactly once. Put
  `Closes #N` in the *commit body*, not only here — `tools/changelog.py` reads it out of
  the commit range when a version is cut.
-->

## What was assumed, and what was actually true

<!-- The defect as the gap between the two. Not the symptom. -->

## The class that stops it coming back

<!--
  An issue closes referencing the unittest class that stops it recurring. The class is
  the fix; the issue is the record that it was once true. Closing without a class is
  allowed only when the thing cannot reproduce deterministically — and then say so here.

  Test class names are the bug they were written for: add a class rather than extending
  an old one, because the names are the changelog.
-->

`Test...`

## Watched failing

<!--
  A guard that cannot be shown failing is not a guard. Paste the failure message each
  new test gave against the unfixed code.

  If the red is weak — a TypeError because your new API did not exist yet, rather than a
  statement about behaviour — say so, and mutate the *fixed* code instead to prove the
  test is load-bearing. Guards have passed against broken code here more than once: an
  AST check defeated by its own docstring, a JXA check defeated by a comment, and five
  fixtures that would have gone green against a feature that had been removed.
-->

## Numbers, before and after

- suite:
- `benchmark_temporal.py --layer integration`:

## Checklist

- [ ] Not a band-aid: the fix is not new prompt text, and not rule N+1 on a hand-grown list. If it is rung 4, the body says why it could not go lower.
- [ ] New columns are in `db.ADDED_COLUMNS` **and** in `upsert`'s INSERT — `schema.sql` alone never reaches an existing store.
- [ ] Tests say what day it is (`db.today()` / `db.set_today`), never a literal date.
- [ ] `if __name__ == "__main__": unittest.main()` is at the very end of the file.
- [ ] Nothing that writes outside this process defaults to on (invariant 11).
- [ ] A known-bad row in the live store was repaired, not preserved (invariant 13) — or there was none, and the query showing that is in the body.
- [ ] Dev notes went on the `docs/` page they belong to. No new file, no dated narrative section.
