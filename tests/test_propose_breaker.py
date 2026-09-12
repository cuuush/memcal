"""The propose circuit breaker: a broken run stops sending instead of burning.

Run 13 spent 3,376s over 76 requests producing nothing — every request waiting
out the capacity backoff and then failing, one after another. The run row
measured it (`requests`, `failed_calls`, `wait_seconds`); nothing acted on it.
These pin the control: consecutive propose failures open the circuit, further
requests are held back without sending, the spool stays unread, and the run
records an abort rather than a passing score.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memcal import archive, config, db, llm, settings  # noqa: E402
from memcal.config import Config  # noqa: E402
from memcal.dream import propose as propose_stage  # noqa: E402
from memcal.dream import run as dream_run  # noqa: E402


class _ScriptedClient:
    """Fails the first `failures` completions, then answers reviewed-but-empty.

    Bundle ids are read back out of the request suffix, so a success routes
    every bundle it was given and the pass marks them read — the healthy shape.
    `sent` counts every completion, `schemas` names each one, so a test can tell
    a held-back sweep from a sent one.
    """

    def __init__(self, failures: int):
        self.failures = failures
        self.sent = 0
        self.schemas: list[str] = []
        self.usage = llm.Usage()

    def complete(self, **kw):
        self.sent += 1
        self.schemas.append(str(kw.get("schema_name") or ""))
        if self.sent <= self.failures:
            exc = llm.LLMError("HTTP 429: too many requests (in-body 429)")
            exc.tally = llm.Tally(requests=3, waits=3, waited=12.0)
            raise exc
        ids = dict.fromkeys(
            re.findall(r"BUNDLE ID ([0-9a-f]{6})", str(kw.get("suffix") or "")))
        return llm.Reply(text="{}", data={"reviewed": list(ids), "diffs": []},
                         usage=llm.Usage(calls=1), model=str(kw.get("model") or ""),
                         generation_id=f"gen-cb-{self.sent}", finish_reason="stop")

    def map(self, jobs, worker, max_parallel=8, on_done=None):
        out = []
        for index, job in enumerate(jobs):
            value = llm._safe(worker, job)
            out.append(value)
            if on_done:
                on_done(index, value)
        return out


class BreakerBase(unittest.TestCase):
    PEOPLE = ("Ava", "Liam", "Mia", "Noah", "Emma", "Oliver", "Finn", "Zoe")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)
        db.set_today("2026-09-01")
        self.cfg.pack_bundles = 1

    def tearDown(self):
        db.set_today(None)
        self.conn.close()
        self.tmp.cleanup()

    def _spool(self, people) -> None:
        # `subject-event` is deliberately not a planning reason, so a healthy
        # empty answer does not earn these bundles a second-look re-send and
        # every request in these tests is exactly one completion.
        for index, person in enumerate(people):
            aid = archive.append(
                self.conn, stream="imessage", external_id=f"cb-{person}-{index}",
                ts=db.now(), text=f"dinner tomorrow at 8? asking {person}",
                thread=f"thread-{person}", person=person, from_me=False,
                gated=True, gate_reason="subject-event")
            self.assertIsNotNone(aid)
            archive.spool_add(self.conn, aid, f"person:{person}")
        self.conn.commit()

    def _dream(self, client):
        with mock.patch.object(dream_run.llm, "client_for", return_value=client):
            return dream_run.dream(self.conn, self.cfg, mode="nightly")

    def _spool_state(self) -> tuple[int, int]:
        unread = self.conn.execute(
            "SELECT COUNT(*) n FROM spool WHERE processed_at IS NULL").fetchone()["n"]
        read = self.conn.execute(
            "SELECT COUNT(*) n FROM spool WHERE processed_at IS NOT NULL").fetchone()["n"]
        return unread, read

    def _run_error(self):
        return self.conn.execute(
            "SELECT error FROM runs ORDER BY id DESC LIMIT 1").fetchone()["error"]


class TestABrokenRunStopsSending(BreakerBase):
    """Eight bundles, one request each, every request a 429 wall."""

    def test_all_failing_run_aborts_at_the_threshold_and_leaves_spool_unread(self):
        self._spool(self.PEOPLE)
        client = _ScriptedClient(failures=100)
        result = self._dream(client)
        # The default threshold is 3: three requests went out, the other five
        # were held back, and merge/sweep failed fast without sending.
        self.assertEqual(len([s for s in client.schemas if s == "memcal_diff"]), 3)
        self.assertEqual(client.sent, 3)
        text = "; ".join(result.errors)
        self.assertIn("circuit breaker opened", text)
        self.assertIn("held back 5 request(s)", text)
        # Nothing was read, nothing was written, and the run says so.
        self.assertEqual(self._spool_state(), (8, 0))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) n FROM events").fetchone()["n"], 0)
        self.assertTrue(result.errors)
        self.assertIsNotNone(self._run_error())
        self.assertIn("circuit breaker", self._run_error())


class TestABlipDoesNotKillAHealthyPass(BreakerBase):
    """Two failures, then health: the counter resets and the pass completes."""

    def test_two_failures_then_success_completes_without_opening(self):
        self._spool(self.PEOPLE)
        client = _ScriptedClient(failures=2)
        result = self._dream(client)
        text = "; ".join(result.errors)
        self.assertNotIn("circuit breaker", text)
        # The six bundles a model saw are read; the two it never saw stay queued.
        self.assertEqual(self._spool_state(), (2, 6))
        # Eight propose completions plus the sweep; nothing re-sent, nothing held.
        self.assertEqual(client.sent, 9)
        self.assertIn("memcal_sweep", client.schemas)


class TestABrokenColdStartStopsLaunchingWaves(BreakerBase):
    """Thirty bundles over four waves: the first wave trips it, the rest never launch."""

    def test_first_wave_failure_aborts_the_remaining_waves(self):
        people = [f"Wave{index:02d}" for index in range(30)]
        self._spool(people)
        client = _ScriptedClient(failures=100)
        with mock.patch.object(dream_run.llm, "client_for", return_value=client):
            result = dream_run.dream(self.conn, self.cfg, mode="ondemand")
        self.assertEqual(client.sent, 3)
        text = "; ".join(result.errors)
        self.assertIn("circuit breaker opened", text)
        self.assertIn("skipped 3 wave(s)", text)
        self.assertEqual(self._spool_state(), (30, 0))
        self.assertIn("circuit breaker", self._run_error())


class TestBreakerThresholdKnob(BreakerBase):
    def test_threshold_one_aborts_on_first_failure(self):
        self.cfg.propose_breaker = 1
        self._spool(self.PEOPLE[:4])
        client = _ScriptedClient(failures=100)
        result = self._dream(client)
        self.assertEqual(client.sent, 1)
        self.assertIn("circuit breaker opened", "; ".join(result.errors))
        self.assertEqual(self._spool_state(), (4, 0))

    def test_zero_disables_the_breaker(self):
        self.cfg.propose_breaker = 0
        self._spool(self.PEOPLE[:4])
        client = _ScriptedClient(failures=100)
        result = self._dream(client)
        # Disabled means every request is attempted — four propose calls plus
        # the sweep trying its luck — and nothing is held back.
        self.assertEqual(len([s for s in client.schemas if s == "memcal_diff"]), 4)
        self.assertNotIn("circuit breaker", "; ".join(result.errors))
        self.assertEqual(self._spool_state(), (4, 0))


class TestBreakerKnobPlumbing(unittest.TestCase):
    def test_default_is_on_at_three(self):
        self.assertEqual(Config(home=Path("/tmp/nope")).propose_breaker, 3)

    def test_setting_round_trips_through_save_and_load(self):
        self.assertIn("MEMCAL_PROPOSE_BREAKER", settings.BY_KEY)
        self.assertEqual(settings.BY_KEY["MEMCAL_PROPOSE_BREAKER"].attr,
                         "propose_breaker")
        saved = os.environ.pop("MEMCAL_PROPOSE_BREAKER", None)
        try:
            with tempfile.TemporaryDirectory() as root:
                home = Path(root)
                self.assertEqual(config.load(home).propose_breaker, 3)
                (home / ".env").write_text("MEMCAL_PROPOSE_BREAKER=7\n")
                self.assertEqual(config.load(home).propose_breaker, 7)
                cfg = Config(home=home)
                settings.save(cfg, {"MEMCAL_PROPOSE_BREAKER": "0"})
                self.assertEqual(cfg.propose_breaker, 0)
        finally:
            if saved is not None:
                os.environ["MEMCAL_PROPOSE_BREAKER"] = saved


class TestBreakerMechanics(unittest.TestCase):
    """The breaker itself, without a database: what counts, what resets."""

    def _breaker(self, threshold: int):
        inner = mock.Mock()
        inner.usage = llm.Usage()
        return dream_run._ProposeBreaker(inner, threshold=threshold), inner

    def _ok(self):
        return llm.Reply(text="{}", data={}, usage=llm.Usage(calls=1),
                         model="m", generation_id="gen-ok", finish_reason="stop")

    def test_success_resets_the_consecutive_count(self):
        breaker, inner = self._breaker(3)
        calls = {"n": 0}

        def complete(**kw):
            calls["n"] += 1
            if calls["n"] in (1, 2, 4, 5):
                raise llm.LLMError("boom")
            return self._ok()

        inner.complete.side_effect = complete
        for _ in range(2):
            with self.assertRaises(llm.LLMError):
                breaker.complete(model="m", prefix="p", suffix="s")
        breaker.complete(model="m", prefix="p", suffix="s")  # healthy: resets
        for _ in range(2):
            with self.assertRaises(llm.LLMError):
                breaker.complete(model="m", prefix="p", suffix="s")
        # Fail, fail, healthy, fail, fail is never three in a row.
        self.assertFalse(breaker.opened)
        self.assertEqual(breaker.consecutive, 2)

    def test_truncated_replies_do_not_count(self):
        breaker, inner = self._breaker(2)
        inner.complete.side_effect = propose_stage.Truncated("cut off at 1200")
        for _ in range(5):
            with self.assertRaises(propose_stage.Truncated):
                breaker.complete(model="m", prefix="p", suffix="s")
        self.assertFalse(breaker.opened)
        self.assertEqual(breaker.consecutive, 0)

    def test_an_open_breaker_fails_fast_as_an_llm_error(self):
        breaker, inner = self._breaker(2)
        inner.complete.side_effect = llm.LLMError("HTTP 503: unavailable")
        for _ in range(2):
            with self.assertRaises(llm.LLMError):
                breaker.complete(model="m", prefix="p", suffix="s")
        self.assertTrue(breaker.opened)
        with self.assertRaises(dream_run._CircuitOpen) as caught:
            breaker.complete(model="m", prefix="p", suffix="s")
        self.assertIsInstance(caught.exception, llm.LLMError)
        self.assertEqual(inner.complete.call_count, 2)
        self.assertEqual(breaker.rejected, 1)


if __name__ == "__main__":
    unittest.main()
