"""Shared deterministic test support."""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memcal import db  # noqa: E402
from memcal.config import Config  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)

    def tearDown(self):
        # Reset process-global date override to prevent state leakage between test cases.
        db.set_today(None)
        self.conn.close()
        self.tmp.cleanup()

    def d(self, offset: int) -> str:
        return (db.today() + timedelta(days=offset)).isoformat()
