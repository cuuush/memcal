"""Test package initialization: host isolation applied before any test runs.

Importing anything under ``tests.`` (which discovery and the cross-module
``from tests._support import Base`` imports both do) runs this first, so the
pins below are in place before a single test case executes.
"""

import os

# Pin signal-cli to a path that cannot exist. `doctor` and `SignalSource.check`
# probe every source, and on a developer machine with signal-cli installed that
# meant spawning the real JVM binary (~0.9s per call, dozens of times per suite)
# purely as a side effect of a doctor/bundle/launchd test that asserts nothing
# about Signal. Config.secret("SIGNAL_CLI", ...) reads this first, so `_cli`
# short-circuits shutil.which and check() fails fast and deterministically
# instead of depending on whether the host has signal-cli linked. Tests that
# actually exercise Signal stub `SignalSource._cli`/`subprocess.run` and are
# unaffected.
os.environ.setdefault("SIGNAL_CLI", "/nonexistent/memcal-test-signal-cli")
