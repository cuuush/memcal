"""Verify Memcal context delivery through the installed Hermes runtime."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
HERMES = Path(os.environ.get("HERMES_TEST_SRC", str(Path.home() / ".hermes/hermes-agent")))
PYTHON = HERMES / "venv/bin/python"


@unittest.skipUnless(PYTHON.is_file(), "Hermes runtime not installed")
class TestHermesDeliveredContext(unittest.TestCase):
    def test_runtime_delivery_and_timeout_recovery(self):
        with tempfile.TemporaryDirectory() as home:
            result = subprocess.run(
                [str(PYTHON), str(ROOT / "tests/helpers/hermes_delivery.py"), "-v"],
                env={**os.environ, "HERMES_HOME": home},
                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
