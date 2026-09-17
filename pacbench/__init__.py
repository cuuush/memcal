"""PACBench — the Personal-Assistant Calendar Bench.

Measures how well a memory system supports a personal assistant through a realistic
week of life. Deliberately free of any `memcal` import: the corpus, the grader, and
(later) the runner must never reach into one product's store — a green grade has to come
from a memory system's own answer, not from privileged knowledge of how Memcal happens
to lay its rows out. A test enforces the boundary (`tests/test_pacbench.py`).
"""

CORPUS_VERSION = "0.1.0"
