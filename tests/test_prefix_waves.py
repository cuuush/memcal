"""Cold-start prefix stability is per-wave, and dry-run prices every wave."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memcal import db, events, llm  # noqa: E402
from memcal.config import Config  # noqa: E402
from memcal.dream import bundle as bundle_stage  # noqa: E402
from memcal.dream import propose as propose_stage  # noqa: E402
from memcal.dream import run as dream_run  # noqa: E402


class TestColdStartPrefixIsPerWave(unittest.TestCase):
    """The shared prefix caches within a wave and is rebuilt across waves.

    Wave mode writes each wave before reading the next (see dream/run.py), so
    the next wave's prefix deliberately contains rows the previous wave wrote.
    The old per-run wording claimed one prefix for the whole run; this pins the
    honest contract instead, plus the dry-run quote for the run it prices.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)
        db.set_today("2026-09-01")

    def tearDown(self):
        db.set_today(None)
        self.conn.close()
        self.tmp.cleanup()

    def d(self, offset: int) -> str:
        from datetime import timedelta  # noqa: PLC0415

        return (db.today() + timedelta(days=offset)).isoformat()

    def test_prefix_stable_without_mutation_rebuilt_after_event_write(self):
        first = propose_stage.build_prefix(self.conn, self.cfg)
        second = propose_stage.build_prefix(self.conn, self.cfg)
        self.assertEqual(first, second)
        events.upsert(self.conn, {"title": "poker night", "date": self.d(2)})
        third = propose_stage.build_prefix(self.conn, self.cfg)
        self.assertNotEqual(first, third)
        self.assertIn("poker night", third)

    def test_packed_cost_defaults_to_one_wave_and_scales_prefix_writes(self):
        kw = dict(prefix_tokens=1000, suffix_tokens=0, output_tokens=0,
                  requests=30, max_parallel=2)
        single = llm.packed_cost("openai/gpt-5.6-luna", **kw)
        explicit = llm.packed_cost("openai/gpt-5.6-luna", **kw, waves=1)
        self.assertEqual(single["cache_misses"], explicit["cache_misses"])
        self.assertEqual(single["cache_misses"], 2)
        multi = llm.packed_cost("openai/gpt-5.6-luna", **kw, waves=4)
        self.assertEqual(multi["cache_misses"], 8)
        self.assertGreater(multi["prefix_now"], single["prefix_now"])

    def test_dry_run_quotes_the_waved_run_it_prices(self):
        self.cfg.max_parallel = 2
        self.cfg.pack_bundles = 1
        fakes = [bundle_stage.Bundle(entity=f"person:wave-{i:02d}",
                                     title=f"Wave {i}")
                 for i in range(30)]
        waves = dream_run._wave_count(self.cfg, "ondemand", len(fakes))
        self.assertGreater(waves, 1)
        real_packed_cost = llm.packed_cost
        seen: dict = {}
        quoted: dict = {}

        def recording(*args, **kwargs):
            seen.update(kwargs)
            quoted.update(real_packed_cost(*args, **kwargs))
            return dict(quoted)

        with mock.patch.object(bundle_stage, "build", return_value=fakes):
            with mock.patch.object(llm, "packed_cost", side_effect=recording):
                result = dream_run.dream(self.conn, self.cfg, mode="ondemand",
                                         dry_run=True)
        self.assertEqual(seen.get("waves"), waves)
        self.assertGreater(quoted.get("cache_misses", 0), 2)
        self.assertTrue(any(f"{waves} waves" in line for line in result.log),
                        f"dry-run log should name its {waves} waves: {result.log[:2]}")
        # The waved quote must cost more prefix writes than a single-wave pack.
        single = real_packed_cost(
            self.cfg.propose_model, prefix_tokens=1000, suffix_tokens=0,
            output_tokens=0, requests=30, max_parallel=2, waves=1)
        waved = real_packed_cost(
            self.cfg.propose_model, prefix_tokens=1000, suffix_tokens=0,
            output_tokens=0, requests=30, max_parallel=2, waves=waves)
        self.assertGreater(waved["cache_misses"], single["cache_misses"])


if __name__ == "__main__":
    unittest.main()
