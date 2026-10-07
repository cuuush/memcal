"""Resume a dream pass that died partway, reusing its spent propose calls.

A pass that crashes before marking the spool read loses its propose outputs
from memory, but each call's completion and parsed payload survive on disk
under calls/run-NNNN. Resume matches those saved requests against rebuilt
bundles by entity set plus byte-identical suffix, routes them through the same
`_route` + `_resolve_cites` the live path runs, and proposes only the rest.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memcal import archive, calls, cli, db, llm  # noqa: E402
from memcal.config import Config  # noqa: E402
from memcal.dream import bundle as bundle_stage  # noqa: E402
from memcal.dream import propose as propose_stage  # noqa: E402
from memcal.dream import retry as retry_stage  # noqa: E402
from memcal.dream import run as dream_run  # noqa: E402
from memcal.dream import diagnostics  # noqa: E402


class _AnswerEmpty:
    """Every request succeeds with no diffs; every bundle routes as read."""

    def __init__(self):
        self.sent = 0
        self.schemas: list[str] = []
        self.usage = llm.Usage()
        self.tag = f"{id(self) % 100000}"

    def complete(self, **kw):
        self.sent += 1
        self.schemas.append(str(kw.get("schema_name") or ""))
        ids = dict.fromkeys(
            re.findall(r"BUNDLE ID ([0-9a-f]{6})", str(kw.get("suffix") or "")))
        return llm.Reply(text="{}", data={"reviewed": list(ids), "diffs": []},
                         usage=llm.Usage(calls=1), model=str(kw.get("model") or ""),
                         generation_id=f"gen-resume-{self.tag}-{self.sent}",
                         finish_reason="stop")

    def map(self, jobs, worker, max_parallel=8, on_done=None):
        out = []
        for index, job in enumerate(jobs):
            value = llm._safe(worker, job)
            out.append(value)
            if on_done:
                on_done(index, value)
        return out

    def propose_calls(self) -> int:
        return sum(1 for s in self.schemas if s == "memcal_diff")


class _RefuseAll(_AnswerEmpty):
    """Fails loudly if any model call is attempted."""

    def complete(self, **kw):
        raise AssertionError(f"model called for {kw.get('schema_name')}")


class ResumeBase(unittest.TestCase):
    PEOPLE = ("Ava", "Liam", "Mia")

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
        # empty answer earns no second-look re-send: one request, one completion.
        for index, person in enumerate(people):
            aid = archive.append(
                self.conn, channel="imessage", external_id=f"rs-{person}-{index}",
                ts=db.now(), text=f"dinner tomorrow at 8? asking {person}",
                thread=f"thread-{person}", person=person, from_me=False,
                gated=True, gate_reason="subject-event")
            self.assertIsNotNone(aid)
            archive.spool_add(self.conn, aid, f"person:{person}")
        self.conn.commit()

    def _bundles(self):
        return bundle_stage.build(self.conn, limit=100, per_entity=50)

    def _open_run(self, bundles, error=None) -> int:
        cur = self.conn.execute(
            "INSERT INTO runs(started_at, mode, model, bundles, items)"
            " VALUES(?,?,?,?,?)",
            (db.now(), "ondemand", "test-model", len(bundles),
             sum(len(b.items) for b in bundles)))
        run_id = int(cur.lastrowid)
        if error is not None:
            self.conn.execute("UPDATE runs SET finished_at = ?, error = ? WHERE id = ?",
                              (db.now(), error, run_id))
        self.conn.commit()
        return run_id

    def _failed_run_with_calls(self, people=PEOPLE):
        """A run shaped like a crash before apply: proposes saved, nothing written."""
        self._spool(people)
        bundles = self._bundles()
        run_id = self._open_run(bundles, error="OperationalError: boom")
        client = _AnswerEmpty()
        got, errors, _notes = propose_stage.propose_all(
            client, self.conn, self.cfg, bundles, run_id=run_id)
        self.assertEqual([], errors)
        self.assertTrue(got)
        return run_id, bundles, got, client


class TestResumeOffer(ResumeBase):
    def test_failed_run_offers_its_spent_calls(self):
        run_id, _bundles, _got, client = self._failed_run_with_calls()
        offer = retry_stage.resume_source(self.conn, self.cfg.home)
        self.assertIsNotNone(offer)
        self.assertEqual(run_id, offer["run_id"])
        self.assertEqual(client.sent, offer["calls"])
        self.assertEqual(len(self.PEOPLE), offer["read"])
        self.assertEqual(100, offer["read_pct"])
        self.assertEqual(0, offer["wrote"])
        self.assertEqual("propose", offer["stage"])
        self.assertIn("boom", offer["error"])
        self.assertTrue(offer["index"])

    def test_clean_run_offers_nothing(self):
        self._spool(self.PEOPLE)
        self._open_run(self._bundles())
        self.assertIsNone(retry_stage.resume_source(self.conn, self.cfg.home))

    def test_drained_queue_offers_nothing(self):
        run_id, _bundles, _got, _client = self._failed_run_with_calls()
        self.conn.execute("UPDATE spool SET processed_at = ?, run_id = ?",
                          (db.now(), run_id))
        self.conn.commit()
        self.assertIsNone(retry_stage.resume_source(self.conn, self.cfg.home))

    def test_applied_calls_are_not_offered(self):
        run_id, _bundles, got, _client = self._failed_run_with_calls()
        gen = got[0][2]
        self.conn.execute(
            "INSERT INTO provenance(kind, ref, verb, entity, stage, run_id,"
            " generation_id, at) VALUES(?,?,?,?,?,?,?,?)",
            ("event", "k", "inserted", "person:Ava", "propose", run_id, gen, db.now()))
        self.conn.commit()
        offer = retry_stage.resume_source(self.conn, self.cfg.home)
        self.assertIsNotNone(offer)
        gens = {t["generation_id"] for turns in offer["index"].values() for t in turns}
        self.assertNotIn(gen, gens)


class TestReplaySavesCalls(ResumeBase):
    def _norm(self, got):
        return sorted((b.entity, json.dumps(d, sort_keys=True, default=str), g)
                      for b, d, g in got)

    def test_replay_spends_no_model_call(self):
        run_id, _bundles, got, _client = self._failed_run_with_calls()
        new_id = self._open_run(self._bundles())
        index = propose_stage.load_replay(self.conn, self.cfg.home, run_id)
        self.assertTrue(index)
        strict = _RefuseAll()
        got2, errors2, notes2 = propose_stage.propose_all(
            strict, self.conn, self.cfg, self._bundles(),
            run_id=new_id, replay=index)
        self.assertEqual(0, strict.sent)
        self.assertEqual([], errors2)
        self.assertEqual(self._norm(got), self._norm(got2))
        self.assertTrue(any("replayed" in n for n in notes2))

    def test_changed_bundle_proposes_fresh(self):
        run_id, _bundles, _got, _client = self._failed_run_with_calls()
        self._open_run(self._bundles())
        aid = archive.append(
            self.conn, channel="imessage", external_id="rs-Ava-new",
            ts=db.now(), text="actually make it 9, asking Ava",
            thread="thread-Ava", person="Ava", from_me=False,
            gated=True, gate_reason="subject-event")
        archive.spool_add(self.conn, aid, "person:Ava")
        self.conn.commit()
        index = propose_stage.load_replay(self.conn, self.cfg.home, run_id)
        counting = _AnswerEmpty()
        got2, _errors, _notes = propose_stage.propose_all(
            counting, self.conn, self.cfg, self._bundles(),
            run_id=self._open_run(self._bundles()), replay=index)
        fresh = [s for s in counting.schemas if s == "memcal_diff"]
        self.assertEqual(1, len(fresh))
        self.assertEqual(1, counting.sent)
        entities = {b.entity for b, _d, _g in got2}
        self.assertEqual({f"person:{p}" for p in self.PEOPLE}, entities)

    def test_multiturn_requests_repropose(self):
        self._spool(("Ava",))
        run_id = self._open_run(self._bundles())
        refs = [{"id": "a1b2c3", "entity": "person:Ava", "label": "Ava", "lines": 1}]

        def _save(gen, label):
            reply = SimpleNamespace(
                generation_id=gen, text="{}", data={"reviewed": ["a1b2c3"], "diffs": []},
                model="m", finish_reason="stop", truncated=False, reasoning="",
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                      cached_tokens=0, reasoning_tokens=0, cost=0.0),
                requests=1, waited=0.0)
            calls.save(self.cfg.home, reply=reply, stage="propose", run_id=run_id,
                       label=label, prefix="p", suffix="s", bundles=refs)

        _save("gen-turn-1", "person:Ava")
        _save("gen-turn-2", "person:Ava")
        _save("gen-other", "other")
        index = propose_stage.load_replay(self.conn, self.cfg.home, run_id)
        gens = {t["generation_id"] for turns in index.values() for t in turns}
        self.assertNotIn("gen-turn-1", gens)
        self.assertNotIn("gen-turn-2", gens)

    def test_resume_chain_follows_manifests(self):
        self._spool(("Ava",))
        first = self._open_run(self._bundles())
        reply = SimpleNamespace(
            generation_id="gen-chain-1", text="{}",
            data={"reviewed": ["a1b2c3"], "diffs": []},
            model="m", finish_reason="stop", truncated=False, reasoning="",
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1,
                                  cached_tokens=0, reasoning_tokens=0, cost=0.0),
            requests=1, waited=0.0)
        calls.save(self.cfg.home, reply=reply, stage="propose", run_id=first,
                   label="person:Ava", prefix="p", suffix="s",
                   bundles=[{"id": "a1b2c3", "entity": "person:Ava",
                             "label": "Ava", "lines": 1}])
        second = self._open_run(self._bundles())
        calls.note_replay(self.cfg.home, second,
                          [{"generation_id": "gen-chain-1", "source_run": first}])
        index = propose_stage.load_replay(self.conn, self.cfg.home, second)
        gens = {t["generation_id"] for turns in index.values() for t in turns}
        self.assertIn("gen-chain-1", gens)


class TestPackedResumeReusesUnchangedBundles(ResumeBase):
    """One new line must not re-read the whole packed request.

    Packing sorts by size, so a single new line reorders and regroups every
    request after it. Whole-group byte-matching then replays nothing — the
    pass re-spends what it already bought. The per-bundle fallback absorbs
    each unchanged bundle out of its saved request, and only the bundle that
    actually changed proposes fresh.
    """

    def setUp(self):
        super().setUp()
        self.cfg.pack_bundles = 6

    def _suffixes(self, client):
        return list(client.seen_suffixes)

    def test_one_new_line_only_reproposes_that_bundle(self):
        run_id, _bundles, got, _client = self._failed_run_with_calls()
        first_gen = {b.entity: g for b, _d, g in got}
        aid = archive.append(
            self.conn, channel="imessage", external_id="rs-Ava-new",
            ts=db.now(), text="actually make it 9, asking Ava",
            thread="thread-Ava", person="Ava", from_me=False,
            gated=True, gate_reason="subject-event")
        archive.spool_add(self.conn, aid, "person:Ava")
        self.conn.commit()
        index = propose_stage.load_replay(self.conn, self.cfg.home, run_id)
        counting = _AnswerEmpty()
        counting.seen_suffixes = []
        real_complete = counting.complete

        def _capture(**kw):
            counting.seen_suffixes.append(str(kw.get("suffix") or ""))
            return real_complete(**kw)

        counting.complete = _capture
        got2, errors2, notes2 = propose_stage.propose_all(
            counting, self.conn, self.cfg, self._bundles(),
            run_id=self._open_run(self._bundles()), replay=index)
        self.assertEqual([], errors2)
        # Only Ava changed, so only one fresh request goes out — and it
        # carries Ava alone, not the regrouped neighbours.
        self.assertEqual(1, counting.sent)
        fresh = self._suffixes(counting)[0]
        self.assertIn("asking Ava", fresh)
        self.assertNotIn("asking Liam", fresh)
        self.assertNotIn("asking Mia", fresh)
        second_gen = {b.entity: g for b, _d, g in got2}
        self.assertEqual(set(first_gen), set(second_gen))
        self.assertNotEqual(first_gen["person:Ava"], second_gen["person:Ava"])
        self.assertEqual(first_gen["person:Liam"], second_gen["person:Liam"])
        self.assertEqual(first_gen["person:Mia"], second_gen["person:Mia"])
        self.assertTrue(any("replayed" in n for n in notes2))

    def test_new_person_leaves_old_bundles_replayed(self):
        run_id, _bundles, got, _client = self._failed_run_with_calls()
        first_gen = {b.entity: g for b, _d, g in got}
        aid = archive.append(
            self.conn, channel="imessage", external_id="rs-Noah-0",
            ts=db.now(), text="dinner tomorrow at 8? asking Noah",
            thread="thread-Noah", person="Noah", from_me=False,
            gated=True, gate_reason="subject-event")
        archive.spool_add(self.conn, aid, "person:Noah")
        self.conn.commit()
        index = propose_stage.load_replay(self.conn, self.cfg.home, run_id)
        counting = _AnswerEmpty()
        counting.seen_suffixes = []
        real_complete = counting.complete

        def _capture(**kw):
            counting.seen_suffixes.append(str(kw.get("suffix") or ""))
            return real_complete(**kw)

        counting.complete = _capture
        got2, errors2, _notes = propose_stage.propose_all(
            counting, self.conn, self.cfg, self._bundles(),
            run_id=self._open_run(self._bundles()), replay=index)
        self.assertEqual([], errors2)
        self.assertEqual(1, counting.sent)
        fresh = self._suffixes(counting)[0]
        self.assertIn("asking Noah", fresh)
        self.assertNotIn("asking Liam", fresh)
        second_gen = {b.entity: g for b, _d, g in got2}
        for person in self.PEOPLE:
            self.assertEqual(first_gen[f"person:{person}"],
                             second_gen[f"person:{person}"])

    def test_untouched_packed_run_still_replays_all(self):
        run_id, _bundles, _got, _client = self._failed_run_with_calls()
        new_id = self._open_run(self._bundles())
        index = propose_stage.load_replay(self.conn, self.cfg.home, run_id)
        strict = _RefuseAll()
        got2, errors2, notes2 = propose_stage.propose_all(
            strict, self.conn, self.cfg, self._bundles(),
            run_id=new_id, replay=index)
        self.assertEqual(0, strict.sent)
        self.assertEqual([], errors2)
        self.assertTrue(any("replayed" in n for n in notes2))
        self.assertEqual(3, len(got2))

    def test_resume_note_counts_each_saved_call_once(self):
        # One packed call supplies Liam and Mia. The note must say one call
        # covering two bundles — not two calls covering three (the packed
        # request's full entity list, Ava included).
        run_id, _bundles, _got, _client = self._failed_run_with_calls()
        aid = archive.append(
            self.conn, channel="imessage", external_id="rs-Ava-new",
            ts=db.now(), text="actually make it 9, asking Ava",
            thread="thread-Ava", person="Ava", from_me=False,
            gated=True, gate_reason="subject-event")
        archive.spool_add(self.conn, aid, "person:Ava")
        self.conn.commit()
        index = propose_stage.load_replay(self.conn, self.cfg.home, run_id)
        counting = _AnswerEmpty()
        _got2, errors2, notes2 = propose_stage.propose_all(
            counting, self.conn, self.cfg, self._bundles(),
            run_id=self._open_run(self._bundles()), replay=index)
        self.assertEqual([], errors2)
        note = next(n for n in notes2 if "replayed" in n)
        self.assertEqual(
            f"replayed 1 propose call(s) from run(s) {run_id}"
            f" covering 2 bundle(s) \u2014 no model call", note)

    def test_second_look_still_fires_after_resume(self):
        # Liam's line looks like a plan, so the failed run asked about him
        # twice: the packed main call plus a second-look singleton. Drop the
        # singleton's file to simulate a crash before the second look. On
        # resume the main silence replays, but the hedge must still fire
        # fresh — absorbing the same silence a second time would preserve
        # exactly what the hedge exists to question, and record Liam twice
        # under one call.
        reasons = {"Ava": "subject-event", "Liam": "invitation",
                   "Mia": "subject-event"}
        for index, (person, reason) in enumerate(reasons.items()):
            aid = archive.append(
                self.conn, channel="imessage", external_id=f"rs2-{person}",
                ts=db.now(), text=f"dinner tomorrow at 8? asking {person}",
                thread=f"thread-{person}", person=person, from_me=False,
                gated=True, gate_reason=reason)
            self.assertIsNotNone(aid)
            archive.spool_add(self.conn, aid, f"person:{person}")
        self.conn.commit()
        bundles = self._bundles()
        run_id = self._open_run(bundles, error="OperationalError: boom")
        client = _AnswerEmpty()
        got, errors, _notes = propose_stage.propose_all(
            client, self.conn, self.cfg, bundles, run_id=run_id)
        self.assertEqual([], errors)
        self.assertEqual(2, client.sent)
        dropped = 0
        for path in sorted(calls.shard(self.cfg.home, run_id).glob("*.json")):
            if path.name == "replay.json":
                continue
            blob = json.loads(path.read_text(encoding="utf-8"))
            if len(blob.get("bundles") or []) == 1:
                path.unlink()
                dropped += 1
        self.assertEqual(1, dropped)
        index = propose_stage.load_replay(self.conn, self.cfg.home, run_id)
        self.assertEqual(1, sum(len(turns) for turns in index.values()))
        counting = _AnswerEmpty()
        got2, errors2, _notes2 = propose_stage.propose_all(
            counting, self.conn, self.cfg, self._bundles(),
            run_id=self._open_run(self._bundles()), replay=index)
        self.assertEqual([], errors2)
        self.assertEqual(1, counting.sent)
        liam_gens = [g for b, _d, g in got2 if b.entity == "person:Liam"]
        # Ava appears only in the main packed call, so her generation id names it.
        main_gen = next(g for b, _d, g in got if b.entity == "person:Ava")
        self.assertIn(main_gen, liam_gens)
        self.assertEqual(2, len(liam_gens))
        self.assertEqual(2, len(set(liam_gens)))


class TestResumePrompt(ResumeBase):
    def test_yes_means_resume_no_means_restart(self):
        self.assertTrue(self._ask(True, ""))
        self.assertTrue(self._ask(True, "y"))
        self.assertTrue(self._ask(True, "yes"))
        self.assertFalse(self._ask(True, "n"))
        self.assertFalse(self._ask(True, "no"))
        self.assertTrue(self._ask(False, ""))

    def _ask(self, tty, answer):
        import builtins

        stdin = SimpleNamespace(isatty=lambda: tty)
        with mock.patch.object(cli.sys, "stdin", stdin), \
                mock.patch.object(builtins, "input", return_value=answer):
            return cli._ask_resume()

    def test_cmd_dream_passes_replay_on_yes(self):
        run_id, _bundles, _got, _client = self._failed_run_with_calls()
        args = argparse.Namespace(home=str(self.cfg.home), dry_run=False, retry=None,
                                  redo=None, no_ingest=True, mode="ondemand", model=None,
                                  limit=0, rounds=1, no_sweep=True)
        seen = {}

        class _Result:
            errors = []
            nothing_new = True

            def report(self):
                return "ok"

        def _dream(conn, cfg, **kw):
            seen.update(kw)
            return _Result()

        import builtins

        tty = SimpleNamespace(isatty=lambda: True)
        out = io.StringIO()
        with mock.patch.object(cli, "dream", _dream), \
                mock.patch.object(cli.sys, "stdin", tty), \
                mock.patch.object(builtins, "input", return_value=""), \
                contextlib.redirect_stdout(out):
            self.assertEqual(0, cli.cmd_dream(args))
        self.assertIsNotNone(seen.get("replay"))
        entities = {e for ents in seen["replay"] for e in ents}
        self.assertEqual({f"person:{p}" for p in self.PEOPLE}, entities)
        self.assertIn(f"resuming run {run_id}", out.getvalue())

    def test_cmd_dream_restarts_on_no(self):
        self._failed_run_with_calls()
        args = argparse.Namespace(home=str(self.cfg.home), dry_run=False, retry=None,
                                  redo=None, no_ingest=True, mode="ondemand", model=None,
                                  limit=0, rounds=1, no_sweep=True)
        seen = {}

        class _Result:
            errors = []
            nothing_new = True

            def report(self):
                return "ok"

        def _dream(conn, cfg, **kw):
            seen.update(kw)
            return _Result()

        import builtins

        tty = SimpleNamespace(isatty=lambda: True)
        with mock.patch.object(cli, "dream", _dream), \
                mock.patch.object(cli.sys, "stdin", tty), \
                mock.patch.object(builtins, "input", return_value="no"), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, cli.cmd_dream(args))
        self.assertIsNone(seen.get("replay"))


class TestCodex503QueueRecovery(ResumeBase):
    def test_failed_bundle_stays_queued_and_clean_empty_retry_drains_it(self):
        self._spool(self.PEOPLE[:2])
        client = llm.Codex("codex", cwd=self.cfg.home)
        count = 0

        def execute(args, prompt):
            nonlocal count
            count += 1
            if count == 2:
                events = [{"type": "turn.failed", "error": {"message": "503 exhausted"}}]
                return subprocess.CompletedProcess([], 1, json.dumps(events[0]), "")
            ids = list(dict.fromkeys(re.findall(r"BUNDLE ID ([0-9a-f]{6})", prompt)))
            payload = {"reviewed": ids, "diffs": []}
            if count == 1:
                payload["diffs"] = [{"bundle": ids[0], "todos": [
                    {"text": "Book dinner", "key": "book-dinner"}]}]
            events = [
                {"type": "error", "message": "Reconnecting... 2/5 (unexpected status 503)"},
                {"type": "item.completed", "item": {
                    "type": "agent_message", "text": json.dumps(payload)}},
                {"type": "turn.completed", "usage": {}},
            ]
            return subprocess.CompletedProcess([], 0, "\n".join(map(json.dumps, events)), "")

        with mock.patch.object(client, "_run", side_effect=execute), \
                mock.patch.object(dream_run.llm, "client_for", return_value=client):
            first = dream_run.dream(self.conn, self.cfg, skip_sweep=True)
        row = self.conn.execute("SELECT * FROM runs WHERE id = ?", (first.run_id,)).fetchone()
        self.assertEqual(row["diffs"], 1)
        self.assertEqual(row["failed_calls"], 1)
        self.assertEqual(retry_stage.outcome(row), retry_stage.PARTIAL)
        self.assertEqual(diagnostics.read(self.cfg.home, first.run_id)[-1]["state"], "partial")
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM spool WHERE processed_at IS NULL").fetchone()[0], 1)

        second_client = llm.Codex("codex", cwd=self.cfg.home)
        with mock.patch.object(second_client, "_run", side_effect=execute), \
                mock.patch.object(dream_run.llm, "client_for", return_value=second_client):
            second = dream_run.dream(self.conn, self.cfg, skip_sweep=True)
        self.assertEqual(second.bundles, 1)
        self.assertEqual(second.diffs, 0)
        self.assertFalse(second.errors)
        self.assertEqual(self.conn.execute(
            "SELECT count(*) FROM spool WHERE processed_at IS NULL").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM todos").fetchone()[0], 1)
        self.assertEqual(diagnostics.read(self.cfg.home, second.run_id)[-1]["state"], "done")


class TestResumeEndToEnd(ResumeBase):
    def test_crash_then_resume_writes_once(self):
        self._spool(self.PEOPLE)
        first = _AnswerEmpty()
        with mock.patch.object(dream_run.llm, "client_for", return_value=first), \
                mock.patch.object(dream_run.apply_stage, "apply_diffs",
                                  side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                dream_run.dream(self.conn, self.cfg, mode="ondemand", skip_sweep=True)
        crashed = self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIn("boom", crashed["error"])
        self.assertEqual(0, self.conn.execute(
            "SELECT COUNT(*) n FROM provenance").fetchone()["n"])

        second = _AnswerEmpty()
        with mock.patch.object(dream_run.llm, "client_for", return_value=second):
            result = dream_run.dream(
                self.conn, self.cfg, mode="ondemand", skip_sweep=True,
                replay=propose_stage.load_replay(
                    self.conn, self.cfg.home, crashed["id"]))
        self.assertFalse(result.errors)
        self.assertEqual(0, second.propose_calls())
        self.assertEqual(0, self.conn.execute(
            "SELECT COUNT(*) n FROM spool WHERE processed_at IS NULL").fetchone()["n"])
        finished = self.conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNone(finished["error"])


if __name__ == "__main__":
    unittest.main()
