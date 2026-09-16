#!/usr/bin/env python3
"""Run the unittest suite in parallel, one module per process.

`python3 -m unittest discover -s tests` runs all ~2200 tests in a single process,
which is CPU-bound and serial. This runner hands each `tests/test_*.py` module to
its own interpreter and fans them across cores, cutting wall time roughly in line
with core count. Separate processes are also stricter isolation than discover gives
— no module can leak process-global state (an unreset `db.set_today`, an imported
sys.path edit) into the next — so a green run here is at least as trustworthy.

Behaviour matches `python -m unittest`: exit status is non-zero if any module fails,
and each failing module's full output is printed. It is an accelerator, not a
replacement; the documented `python3 -m unittest discover -s tests` still works.

    python3 tools/run_tests.py                 # whole suite, one proc per core
    python3 tools/run_tests.py -j 4            # cap parallelism
    python3 tools/run_tests.py test_core test_web   # only these modules
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"


def _modules(names: list[str]) -> list[str]:
    """Resolve requested names (or all `test_*` modules) to bare stems.

    Ordered biggest-file-first so the long-running modules start while the pool is
    empty and don't become the tail everything else waits on. File size is a cheap
    stand-in for runtime and does not need to be exact — it only shapes scheduling.
    """
    if names:
        stems = [n[len("tests."):] if n.startswith("tests.") else n for n in names]
        stems = [n[:-len(".py")] if n.endswith(".py") else n for n in stems]
        return stems
    by_size = sorted(TESTS.glob("test_*.py"),
                     key=lambda p: p.stat().st_size, reverse=True)
    return [p.stem for p in by_size]


# `python -m unittest tests.<module>` does not put tests/ on sys.path the way
# `discover -s tests` does, so modules that import `from _support import Base`
# (rather than `from tests._support`) would fail to import. Add both tests/ and
# the repo root to PYTHONPATH for the workers so either spelling resolves, exactly
# as it does under discover.
_ENV = dict(os.environ, PYTHONPATH=os.pathsep.join(
    [str(TESTS), str(ROOT), os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep))


def _run_one(module: str) -> tuple[str, int, str]:
    """Run one module in a fresh interpreter; return (module, returncode, output)."""
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", f"tests.{module}"],
        cwd=str(ROOT), env=_ENV, capture_output=True, text=True)
    # unittest writes its dots and summary to stderr; keep stdout too for stray prints.
    return module, proc.returncode, (proc.stderr or "") + (proc.stdout or "")


def _ran_count(output: str) -> int:
    for line in output.splitlines():
        if line.startswith("Ran ") and " test" in line:
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return 0
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("modules", nargs="*",
                        help="test modules to run (default: every tests/test_*.py)")
    parser.add_argument("-j", "--jobs", type=int, default=min(os.cpu_count() or 4, 12),
                        help="parallel processes (default: one per core, capped at 12)")
    args = parser.parse_args()

    modules = _modules(args.modules)
    if not modules:
        print("no test modules found", file=sys.stderr)
        return 1

    start = time.time()
    failed: list[tuple[str, str]] = []
    total = 0
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        futures = {pool.submit(_run_one, m): m for m in modules}
        for future in as_completed(futures):
            module, rc, output = future.result()
            total += _ran_count(output)
            mark = "ok" if rc == 0 else "FAIL"
            print(f"  {mark:>4}  {module}")
            if rc != 0:
                failed.append((module, output))

    elapsed = time.time() - start
    print(f"\nRan {total} tests across {len(modules)} modules "
          f"in {elapsed:.1f}s on {args.jobs} processes")

    if failed:
        for module, output in failed:
            print(f"\n{'=' * 70}\n{module}\n{'=' * 70}\n{output.rstrip()}")
        print(f"\nFAILED — {len(failed)} module(s): "
              f"{', '.join(m for m, _ in failed)}")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
