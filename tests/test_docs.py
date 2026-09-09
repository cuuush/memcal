"""Guards on the numbers the documentation states about itself.

A count written by hand is a relation, and a relation written by hand drifts. The fix
is to stop writing scores down in prose and let the tool print the live answer instead;
this keeps every page that way.

Release-version agreement is checked in `test_operational_regressions`.
"""

import re
import unittest
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "docs"


class TestNoPageQuotesABenchmarkScoreByHand(unittest.TestCase):
    """A score in prose is a relation to a program's output, and prose drifts on it.

    The fix was to stop writing it down; this keeps it that way on every page, so a
    number that belongs to `benchmark_temporal` is never copied into documentation where
    it can go stale.
    """

    #: `NNN/NNN hard checks` — the shape `benchmark_temporal` prints and prose copies.
    SCORE = re.compile(r"\b\d+\s*/\s*\d+\s+hard checks\b")

    def test_no_doc_states_a_hard_check_score(self):
        offenders = []
        for page in sorted(DOCS.glob("*.md")):
            for n, line in enumerate(page.read_text(encoding="utf-8").splitlines(), 1):
                # A line showing sample command output is quoting the tool, not asserting
                # a fact about today; those are indented or fenced.
                if line.startswith((" ", "\t", "```")):
                    continue
                if self.SCORE.search(line):
                    offenders.append(f"{page.name}:{n}")
        self.assertEqual(
            offenders, [],
            "a page states a benchmark score in prose; run the benchmark instead — "
            "`--layer integration` prints the current result")


if __name__ == "__main__":
    unittest.main()
