"""Dream failures retain their stage, call evidence, and actionable error text."""

import argparse
import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from tests._support import Base
from memcal import archive, calls, cli, db, llm, questions, web_jobs, web_memory
from memcal.dream import diagnostics, propose, retry, run
from memcal.dream.bundle import Bundle


class TestExactQuestionKeyRecovery(Base):
    def review(self):
        return {"abc": {"candidates": [questions.Candidate(
            key="q:movie", text="When is movie night?", version="v1",
            wake_condition=None, likely_lines=())], "overflow": 0}}

    def test_missing_prefix_is_repaired_only_for_exact_candidate_and_version(self):
        action = {"action": "keep", "key": "movie", "version": "v1"}
        payload = {"diffs": [{"bundle": "abc", "questions": [action]}]}
        missing, errors = propose._question_gaps(payload, self.review())
        self.assertEqual((missing, errors), ({}, []))
        self.assertEqual(action["key"], "q:movie")
        self.assertIn("exact candidate and version match", payload["_coverage_notes"][0])

    def test_unknown_and_stale_keys_remain_rejected(self):
        for key, version in [("other", "v1"), ("movie", "stale")]:
            payload = {"diffs": [{"bundle": "abc", "questions": [
                {"action": "keep", "key": key, "version": version}]}]}
            missing, errors = propose._question_gaps(payload, self.review())
            self.assertIn("abc", missing)
            self.assertIn("unknown question key", errors[0])

    def test_failed_repair_keeps_the_paid_initial_reply(self):
        reply = llm.Reply(text='{"reviewed": ["abc"], "diffs": []}',
                          data={"reviewed": ["abc"], "diffs": []}, usage=llm.Usage(),
                          model="test", generation_id="initial")
        client = mock.Mock()
        client.complete.side_effect = [reply, llm.LLMError("repair endpoint unavailable")]
        with self.assertRaises(llm.LLMError) as raised:
            propose.propose_group(client, self.cfg, "prefix", [Bundle(entity="person:Quinn", items=[])],
                                  suffix="source", reviews=self.review())
        self.assertEqual(raised.exception.turns[0].reply.generation_id, "initial")

    def test_recovered_key_passes_review_without_another_call_and_replays_safely(self):
        from memcal import todos
        key = todos.ask(self.conn, "When is movie night?")
        row = self.conn.execute("SELECT * FROM questions WHERE key = ?", (key,)).fetchone()
        version = row["updated_at"] or row["created_at"]
        candidate = questions.Candidate(key=key, text=row["text"], version=version,
                                        wake_condition=None, likely_lines=())
        reviews = {"abc": {"candidates": [candidate], "overflow": 0}}
        action = {"action": "keep", "key": key.removeprefix("q:"), "version": version}
        payload = {"reviewed": ["abc"], "diffs": [{"bundle": "abc", "questions": [action]}]}
        client = mock.Mock()
        client.complete.return_value = llm.Reply(text=json.dumps(payload), data=payload,
            usage=llm.Usage(), model="test", generation_id="recovered")
        _, result, _ = propose.propose_group(client, self.cfg, "prefix",
            [Bundle(entity="person:Quinn", items=[])], suffix="source", reviews=reviews)
        self.assertEqual(client.complete.call_count, 1)
        self.assertFalse(result.get("_coverage_errors"))
        for _ in range(2):
            questions.apply_action(self.conn, action, written_by="dream", commit=True)
        saved = self.conn.execute("SELECT * FROM questions WHERE key = ?", (key,)).fetchone()
        self.assertEqual(saved["status"], "open")
        self.assertEqual(self.conn.execute("SELECT count(*) FROM questions").fetchone()[0], 1)

    def test_truncated_repair_is_rejected_with_both_paid_turns(self):
        initial = llm.Reply(text="{}", data={"reviewed": ["abc"], "diffs": []},
                            model="test", generation_id="initial")
        repair = llm.Reply(text="{}", data={"diffs": []}, model="test",
                           generation_id="repair", finish_reason="length")
        client = mock.Mock()
        client.complete.side_effect = [initial, repair]
        with self.assertRaises(propose.Truncated) as raised:
            propose.propose_group(client, self.cfg, "prefix", [Bundle(entity="person:Quinn", items=[])],
                                  suffix="source", reviews=self.review())
        self.assertIn("question-repair", str(raised.exception))
        self.assertEqual([t.reply.generation_id for t in raised.exception.turns], ["initial", "repair"])


class TestDurableDreamErrors(Base):
    def spool(self):
        aid = archive.append(self.conn, channel="imessage", external_id="diagnostics",
                             ts=db.now(), text="movie Friday at 8", thread="quinn",
                             person="Quinn", gated=True)
        archive.spool_add(self.conn, aid, "person:Quinn")
        self.conn.commit()

    def test_crash_is_printed_and_saved_with_traceback_for_web_and_cli(self):
        self.spool()
        job = web_jobs._Job("dream")
        stderr = io.StringIO()
        with mock.patch.object(run.llm, "client_for", side_effect=RuntimeError("fixture disk failure")), \
                redirect_stderr(stderr), self.assertRaises(RuntimeError):
            web_jobs.dream_work(self.conn, self.cfg, job)
        row = self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(row["finished_at"])
        self.assertIn("fixture disk failure", stderr.getvalue())
        entries = diagnostics.read(self.cfg.home, row["id"])
        crash = next(e for e in entries if e.get("traceback"))
        self.assertIn("RuntimeError: fixture disk failure", crash["traceback"])
        self.assertTrue(any("fixture disk failure" in line for line in job.lines))
        detail = web_memory.run_detail(self.conn, self.cfg, row["id"])
        self.assertEqual(detail["diagnostics"], entries)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli.cmd_trace(argparse.Namespace(
                home=str(self.cfg.home), what=str(row["id"]), limit=20)), 0)
        self.assertIn("fixture disk failure", output.getvalue())

    def test_logging_failure_is_printed_even_if_disk_cannot_save_it(self):
        stderr = io.StringIO()
        journal = diagnostics.Journal(self.cfg.home)
        with mock.patch("memcal.dream.diagnostics.os.open", side_effect=OSError("disk full")), \
                redirect_stderr(stderr), self.assertRaises(OSError):
            journal.record("error", {"error": "original problem"}, 99)
        self.assertIn("dream logging failed", stderr.getvalue())
        self.assertIn("disk full", stderr.getvalue())

    def test_call_logging_failure_is_not_silent(self):
        reply = llm.Reply(text="{}", data={}, usage=llm.Usage(), model="test", generation_id="fixture")
        stderr = io.StringIO()
        with mock.patch.object(calls, "_atomic_write", side_effect=OSError("disk full")), \
                redirect_stderr(stderr):
            self.assertIsNone(calls.save(self.cfg.home, reply=reply, stage="propose", run_id=1))
        self.assertIn("call logging failed", stderr.getvalue())
        self.assertIn("disk full", stderr.getvalue())

    def test_validation_errors_with_writes_are_partial_not_ok(self):
        row = {"mode": "web", "finished_at": "finished", "error": "question coverage: abc unknown key",
               "diffs": 26, "bundles": 89, "failed_calls": 0}
        self.assertEqual(retry.outcome(row), retry.PARTIAL)
        self.assertEqual(diagnostics.error_stage(row["error"]), "propose")


if __name__ == "__main__":
    unittest.main()
