"""Collection job recovery and slow-source progress."""

from __future__ import annotations

import email
import io
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from memcal import archive, db, web_jobs
from memcal.config import Config
from memcal.sources import base, groupme, ical, proton
from memcal.sources.spec import SourceError


class TestActiveJobRecovery(unittest.TestCase):
    def tearDown(self):
        with web_jobs._JOB_LOCK:
            web_jobs._JOBS.clear()

    def test_active_job_can_be_found_after_the_page_loses_its_id(self):
        job = web_jobs._Job("gather")
        job.plan(["email"])
        job.step("email", "running", "INBOX: 25/100")
        with web_jobs._JOB_LOCK:
            web_jobs._JOBS["gather-7"] = job

        status = web_jobs.job_status(kind="gather")

        self.assertEqual(status["job"], "gather-7")
        self.assertEqual(status["steps"][0]["note"], "INBOX: 25/100")

    def test_completed_job_is_not_advertised_as_active(self):
        job = web_jobs._Job("gather")
        job.done = True
        with web_jobs._JOB_LOCK:
            web_jobs._JOBS["gather-8"] = job

        self.assertEqual(web_jobs.job_status(kind="gather"), {"job": None})


class TestFinishedWebJobsAreEvicted(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.old_retention = web_jobs.COMPLETED_JOB_RETENTION
        with web_jobs._JOB_LOCK:
            web_jobs._JOBS.clear()
            web_jobs._NEXT_JOB_ID = 1
        web_jobs.COMPLETED_JOB_RETENTION = 3

    def tearDown(self):
        web_jobs.COMPLETED_JOB_RETENTION = self.old_retention
        with web_jobs._JOB_LOCK:
            web_jobs._JOBS.clear()
            web_jobs._NEXT_JOB_ID = 1
        self.tmp.cleanup()

    def _finish(self, kind="gather"):
        work_returned = threading.Event()

        def work(_conn, _cfg, _job):
            work_returned.set()

        job_id = web_jobs.start_job(kind, work, self.cfg)["job"]
        self.assertTrue(work_returned.wait(1))
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with web_jobs._JOB_LOCK:
                job = web_jobs._JOBS.get(job_id)
                if job is not None and job.done:
                    return job_id
            time.sleep(0.001)
        self.fail(f"{job_id} did not finish")

    def test_only_the_newest_completed_snapshots_are_kept(self):
        job_ids = [self._finish() for _ in range(5)]

        with web_jobs._JOB_LOCK:
            self.assertEqual(list(web_jobs._JOBS), job_ids[-3:])

    def test_an_active_job_is_never_evicted(self):
        with web_jobs._JOB_LOCK:
            for number in range(5):
                job = web_jobs._Job("gather")
                job.done = True
                web_jobs._JOBS[f"gather-{number}"] = job
            active = web_jobs._Job("dream")
            web_jobs._JOBS["dream-active"] = active
            web_jobs._trim_completed_jobs()

            self.assertIs(web_jobs._JOBS["dream-active"], active)
            self.assertEqual(
                [job_id for job_id, job in web_jobs._JOBS.items() if job.done],
                ["gather-2", "gather-3", "gather-4"],
            )

    def test_job_ids_do_not_repeat_after_eviction(self):
        web_jobs.COMPLETED_JOB_RETENTION = 1
        self.assertEqual(
            [self._finish() for _ in range(3)],
            ["gather-1", "gather-2", "gather-3"],
        )


class TestEmailProgress(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_folder_and_message_counts_are_reported(self):
        messages = {
            uid: email.message_from_string(
                "From: friend@example.com\n"
                f"Subject: poker tomorrow {uid}\n"
                "Date: Thu, 30 Jul 2026 12:00:00 -0400\n"
                f"Message-ID: <message-{uid}>\n\n"
            )
            for uid in range(1, 31)
        }

        class Bridge:
            def __init__(self, _cfg):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                pass

            def folders(self):
                return ["INBOX"]

            def select(self, _folder):
                return True, "1"

            def uids_since(self, _last_uid, since_days=0):
                # A first run floors the IMAP search at a date rather than walking the
                # whole mailbox. This fake ignores the floor and returns everything —
                # the window is covered separately; what this test is about is progress.
                self.asked_since = since_days
                return list(messages)

            def headers(self, uid):
                return messages[uid]

            def body(self, _uid):
                return "See you there."

        updates: list[str] = []
        with mock.patch.object(proton, "Bridge", Bridge):
            proton.ingest(
                self.conn, self.cfg, limit=100, folders=("INBOX",),
                progress=updates.append,
            )

        self.assertIn("INBOX: checking for new messages…", updates)
        self.assertIn("INBOX: 25/30 this round · 6 waiting", updates)
        self.assertEqual(updates[-1], "INBOX: 30/30 this round")


class TestProtonBridgeConnectionFailures(unittest.TestCase):
    def _context(self):
        temporary_home = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_home.cleanup)
        home = Path(temporary_home.name)
        cfg = Config(home=home, env={
            "PROTON_BRIDGE_USER": "mailbox-user",
            "PROTON_BRIDGE_PASSWORD": "old-mailbox-password",
        })
        conn = db.connect(home / "memcal.db")
        db.migrate(conn)
        self.addCleanup(conn.close)
        return conn, cfg

    def test_login_rejection_names_the_bridge_credentials_to_replace(self):
        closed = []
        authentication_error = proton.imaplib.IMAP4.error

        class IMAP:
            error = authentication_error

            def __init__(self, *_args, **_kwargs):
                pass

            def starttls(self, _context):
                pass

            def login(self, _user, _password):
                raise authentication_error("authentication failed")

            def logout(self):
                closed.append(True)

        conn, cfg = self._context()

        with mock.patch.object(proton.imaplib, "IMAP4_SSL",
                               side_effect=proton.ssl.SSLError), \
             mock.patch.object(proton.imaplib, "IMAP4", IMAP):
            report = proton.ingest(conn, cfg)

        self.assertIn("Bridge is open", report.error or "")
        self.assertIn("rejected the saved mailbox credentials", report.error or "")
        self.assertIn("PROTON_BRIDGE_PASSWORD", report.error or "")
        self.assertNotIn("not accepting connections", report.error or "")
        self.assertEqual(closed, [True])

    def test_a_closed_bridge_says_to_open_it(self):
        conn, cfg = self._context()
        with mock.patch.object(
                proton.imaplib, "IMAP4_SSL", side_effect=ConnectionRefusedError):
            report = proton.ingest(conn, cfg)

        self.assertIn("not accepting connections", report.error or "")
        self.assertIn("Open Bridge and unlock it", report.error or "")
        self.assertNotIn("mailbox credentials", report.error or "")

    def test_implicit_ssl_is_used_when_the_bridge_accepts_it(self):
        connected = []

        class IMAPSSL:
            def __init__(self, *_args, **_kwargs):
                connected.append("SSL")

            def login(self, _user, _password):
                pass

            def logout(self):
                pass

        _conn, cfg = self._context()
        with mock.patch.object(proton.imaplib, "IMAP4_SSL", IMAPSSL):
            with proton.Bridge(cfg) as bridge:
                self.assertEqual(bridge.security, "SSL")
        self.assertEqual(connected, ["SSL"])

    def test_starttls_is_used_when_implicit_ssl_is_rejected(self):
        connected = []

        class IMAP:
            def __init__(self, *_args, **_kwargs):
                connected.append("STARTTLS")

            def starttls(self, _context):
                pass

            def login(self, _user, _password):
                pass

            def logout(self):
                pass

        _conn, cfg = self._context()
        with mock.patch.object(proton.imaplib, "IMAP4_SSL",
                               side_effect=proton.ssl.SSLError), \
             mock.patch.object(proton.imaplib, "IMAP4", IMAP):
            with proton.Bridge(cfg) as bridge:
                self.assertEqual(bridge.security, "STARTTLS")
        self.assertEqual(connected, ["STARTTLS"])


class TestTheFirstEmailRunIsBounded(unittest.TestCase):
    """Proton had no date floor while every other source had one.

    A first load searched `UID 1:*` across every folder and fetched **27,799 messages
    back to 2012** — one IMAP round trip each for headers, a second full-body fetch for
    everything that passed, all decrypted by the Bridge, and then all but the last
    thirty days discarded by the spool horizon on arrival. Hours of work to archive a
    decade of mail no pass will ever read.
    """

    def setUp(self):
        self.conn = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        db.migrate(self.conn)
        self.cfg = Config(home=Path(tempfile.mkdtemp()))
        self.addCleanup(self.conn.close)

    def _run(self, *, watermark=None):
        asked = {}

        class Bridge:
            def __init__(self, _cfg):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                pass

            def folders(self):
                return ["INBOX"]

            def select(self, _folder):
                return True, "1"

            def uids_since(self, last_uid, since_days=0):
                asked["last_uid"], asked["since_days"] = last_uid, since_days
                return []

        if watermark is not None:
            base.set_watermark(self.conn, "proton.INBOX", watermark)
        with mock.patch.object(proton, "Bridge", Bridge):
            report = proton.ingest(self.conn, self.cfg, limit=10, folders=("INBOX",))
        return asked, report

    def test_a_first_run_asks_the_server_for_a_bounded_window(self):
        asked, report = self._run()
        self.assertEqual(asked["last_uid"], 0)
        self.assertGreater(asked["since_days"], 0,
                           "an unbounded first search is the whole defect")
        self.assertTrue(any("first run" in note for note in report.notes),
                        "a silently narrowed backfill is the shape of bug that hides")

    def test_a_later_run_is_watermark_driven_and_unbounded(self):
        """Once there is a watermark the floor would only re-hide mail that arrived
        while the machine was off, so it applies to the first run and nothing else."""
        asked, _ = self._run(watermark="1:4200")
        self.assertEqual(asked["last_uid"], 4200)
        self.assertEqual(asked["since_days"], 0)



class TestASourceThatRanIsDistinguishableFromOneThatDidNot(unittest.TestCase):
    """"Was the Proton Bridge open during collection?" had no answer.

    Freshness was measured off the newest *message*, so a source that has not run for a
    month reads as healthy if its last message happens to be recent, and one that runs
    fine through a quiet week reads as broken. The two failures are opposite and the
    signal could not tell them apart.
    """

    def setUp(self):
        self.conn = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        db.migrate(self.conn)
        self.cfg = Config(home=Path(tempfile.mkdtemp()))
        self.addCleanup(self.conn.close)

    def _source(self, name, *, blow_up=False):
        from memcal.sources.spec import Source, SourceError      # noqa: PLC0415

        class Fake(Source):
            def fetch(_self, conn, cfg, report, limit):
                if blow_up:
                    raise SourceError("cannot reach Proton Bridge — is it running?")
        fake = Fake()
        fake.name = name
        return fake

    def test_a_clean_run_records_that_it_happened(self):
        report = self._source("email").run(self.conn, self.cfg)
        self.assertFalse(report.error)
        self.assertTrue(db.get_meta(self.conn, "source.email.last_success"))

    def test_a_failed_run_records_nothing_so_the_stream_goes_stale(self):
        """This is the Bridge case. A closed Bridge must not look like a quiet inbox."""
        report = self._source("email", blow_up=True).run(self.conn, self.cfg)
        self.assertIn("Bridge", report.error)
        self.assertIsNone(db.get_meta(self.conn, "source.email.last_success"))



class TestACollectionIsRecorded(unittest.TestCase):
    """"Waiting for the next dream" could only ever show what was queued.

    A skipped item never enters the spool — it exists solely as `archive.gated = 0` with
    nothing tying it to the pass that skipped it — so "what will the next dream ignore,
    and why" was unanswerable. Per-source counts lived in a job object that died with
    the process, and the only durable trace of an ingest was a scattering of watermarks.
    """

    def setUp(self):
        self.conn = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        db.migrate(self.conn)
        self.cfg = Config(home=Path(tempfile.mkdtemp()))
        self.addCleanup(self.conn.close)

    def _mail_source(self):
        from memcal.sources.spec import Source                   # noqa: PLC0415

        class Mail(Source):
            name = "email"

            def fetch(_self, conn, cfg, report, limit):
                base.deliver(conn, report, stream="email", external_id="1", ts=db.now(),
                             text="Your weekly newsletter", thread="news@bulk.example",
                             handle="news@bulk.example",
                             meta={"list-id": "<news.bulk.example>"})
                base.deliver(conn, report, stream="email", external_id="2", ts=db.now(),
                             text="dinner thursday 7pm?", thread="sam@example.com",
                             handle="sam@example.com")
        return Mail()

    def test_it_counts_what_is_waiting_and_what_was_skipped(self):
        cid = archive.open_collection(self.conn, mode="cli")
        self._mail_source().run(self.conn, self.cfg, collection_id=cid)
        archive.close_collection(self.conn, cid)

        row = archive.collections(self.conn)[0]
        self.assertEqual(row["waiting"], 1)
        self.assertEqual(row["skipped"], 1, "the half that had no home before")
        self.assertEqual(row["archived"], 2)

    def test_every_skipped_row_can_name_the_pass_that_skipped_it(self):
        cid = archive.open_collection(self.conn, mode="cli")
        self._mail_source().run(self.conn, self.cfg, collection_id=cid)
        skipped = self.conn.execute(
            "SELECT text, gate_reason FROM archive WHERE collection_id = ? AND gated = 0",
            (cid,)).fetchall()
        self.assertEqual(len(skipped), 1)
        self.assertTrue(skipped[0]["gate_reason"], "a skip without a reason is not useful")

    def test_a_failing_source_is_recorded_with_its_error(self):
        """The case with nothing else to show for itself, and the one worth recording."""
        from memcal.sources.spec import Source, SourceError       # noqa: PLC0415

        class Broken(Source):
            name = "email"

            def fetch(_self, conn, cfg, report, limit):
                raise SourceError("cannot reach Proton Bridge — is it running?")

        cid = archive.open_collection(self.conn, mode="cli")
        Broken().run(self.conn, self.cfg, collection_id=cid)
        archive.close_collection(self.conn, cid)
        sources = archive.collections(self.conn)[0]["sources"]
        self.assertEqual(len(sources), 1)
        self.assertIn("Bridge", sources[0]["error"])


class TestOneBarPerSourceInsteadOfOneBarForAll(unittest.TestCase):
    """A single bar over "sources finished" described none of what was happening.

    It sat at 1/6 for the ninety seconds Proton took and then moved three steps at once,
    and it could not say which source was slow — which was the whole question, because
    the sources now run at the same time. Each step carries its own fraction, its own
    phase, and a state that only its own runner sets.
    """

    def tearDown(self):
        with web_jobs._JOB_LOCK:
            web_jobs._JOBS.clear()

    def test_a_step_reports_its_own_fraction(self):
        job = web_jobs._Job("gather")
        job.plan(["email", "ical"])
        job.step("email", "running", "INBOX: 12/30", done=12, total=30,
                 phase="reading mail")
        email_step = job.snapshot()["steps"][0]
        self.assertEqual((email_step["done"], email_step["total"]), (12, 30))
        self.assertEqual(email_step["phase"], "reading mail")

    def test_a_source_that_cannot_count_reports_no_denominator(self):
        """`total` 0 means "draw motion, not a percentage" — never a fake 0%."""
        job = web_jobs._Job("gather")
        job.plan(["imessage"])
        job.step("imessage", "running", phase="reading")
        self.assertEqual(job.snapshot()["steps"][0]["total"], 0)

    def test_counted_progress_does_not_clobber_state_or_the_other_way(self):
        """The two callers interleave: the source counts from inside its fetch loop
        while the runner sets state around it. Either overwriting the other is how a
        finished source goes back to reading."""
        job = web_jobs._Job("gather")
        job.plan(["email"])
        job.step("email", "running", phase="reading mail")
        job.step("email", done=5, total=10)            # the source, mid-loop
        step = job.snapshot()["steps"][0]
        self.assertEqual(step["state"], "running")
        self.assertEqual(step["phase"], "reading mail")

    def test_a_finished_step_is_a_full_bar(self):
        """Its last count is short of its own total — the tail was deduplicated away —
        and a bar stuck at 96% beside the word "done" reads as a failure."""
        job = web_jobs._Job("gather")
        job.plan(["email"])
        job.step("email", "running", done=28, total=30)
        job.step("email", "done", "8 new, 4 queued")
        step = job.snapshot()["steps"][0]
        self.assertEqual(step["done"], step["total"])

    def test_every_change_bumps_the_version_a_stream_waits_on(self):
        job = web_jobs._Job("gather")
        first = job.snapshot()["version"]
        job.step("email", "running", phase="reading mail")
        self.assertGreater(job.snapshot()["version"], first)

    def test_waiting_returns_immediately_when_something_already_moved(self):
        job = web_jobs._Job("gather")
        job.plan(["email"])
        job.step("email", "running")
        self.assertEqual(job.wait_for_change(0, timeout=0.05)["steps"][0]["state"],
                         "running")


class TestAPlainProgressCallbackStillWorks(unittest.TestCase):
    """Sources report counts now; plenty of callers only ever wanted a sentence.

    `progress=updates.append` is a real caller — it is how the email progress test reads
    what a fetch reported — and handing `updates.append` a keyword argument raises
    `TypeError: list.append() takes no keyword arguments` from inside the ingest loop.
    One adapter, at the boundary, rather than every source learning to ask.
    """

    def test_a_one_argument_callback_never_sees_the_extra_fields(self):
        seen: list[str] = []
        report = base.adapt_progress(seen.append)
        report("INBOX: 12/30", done=12, total=30, phase="reading mail")
        self.assertEqual(seen, ["INBOX: 12/30"])

    def test_a_callback_that_wants_the_counts_gets_them(self):
        seen: list[tuple] = []

        def rich(note="", *, done=None, total=None, phase=""):
            seen.append((note, done, total, phase))

        base.adapt_progress(rich)("x", done=1, total=2, phase="reading")
        self.assertEqual(seen, [("x", 1, 2, "reading")])

    def test_a_callback_taking_kwargs_is_passed_through_untouched(self):
        def anything(note="", **kw):
            return note

        self.assertIs(base.adapt_progress(anything), anything)

    def test_nothing_stays_nothing(self):
        self.assertIsNone(base.adapt_progress(None))


class TestTheICalBarWasAStripeForTheWholeSlowPart(unittest.TestCase):
    """The Calendar read is one opaque subprocess and it is where the seconds go, so the
    lane animated for the entire wait and then counted only the fast filing pass at the
    end. The JXA now says how far through it is, on stderr, while it runs."""

    def test_a_tick_is_read_as_a_phase_and_a_fraction(self):
        self.assertEqual(ical._tick("@@memcal reading 40 115\n"), ("reading", 40, 115))

    def test_anything_else_calendar_says_is_not_a_tick(self):
        for line in ("execution error: Not authorized (-1743)", "@@memcal reading x y",
                     "@@memcal reading 3", ""):
            self.assertIsNone(ical._tick(line), line)

    def test_the_ticks_arrive_while_the_read_is_still_running(self):
        seen: list[tuple] = []

        def progress(note="", *, done=0, total=0, phase=""):
            seen.append((phase, done, total))

        snapshot = ical._calendar_snapshot(
            "2026-01-01", "2026-06-01", progress=progress,
            opener=_FakeCalendar(
                ticks=["@@memcal checking 1 2", "@@memcal checking 2 2",
                       "@@memcal reading 0 9", "@@memcal reading 5 9",
                       "@@memcal reading 9 9"],
                payload='{"events": [{"uid": "a"}], "unreadable": []}'))
        self.assertEqual(snapshot.items, [{"uid": "a"}])
        self.assertTrue(snapshot.complete)
        self.assertEqual(seen[0], ("checking", 1, 2))
        self.assertEqual(seen[-1], ("reading", 9, 9))

    def test_a_calendar_failure_still_explains_itself(self):
        """stderr carries both the ticks and the error. Filtering the one must not eat
        the other — the message is the whole of what the user is shown."""
        with self.assertRaises(SourceError) as caught:
            ical._calendar_snapshot(
                "2026-01-01", "2026-06-01",
                opener=_FakeCalendar(ticks=["@@memcal checking 1 1",
                                            "execution error: Not authorized (-1743)"],
                                     payload="", code=1))
        self.assertIn("Not authorized", str(caught.exception))

    def test_a_calendar_that_never_answers_is_not_waited_on_for_ever(self):
        stuck = _FakeCalendar(ticks=[], payload="", hangs=True)
        with self.assertRaises(SourceError) as caught:
            ical._calendar_snapshot("2026-01-01", "2026-06-01", opener=stuck)
        self.assertIn("did not answer", str(caught.exception))
        self.assertTrue(stuck.killed, "a process that timed out has to be killed")


class TestOneBarOverPhasesThatCountDifferentThings(unittest.TestCase):
    """Three phases, three denominators, one lane. Reported per phase the bar sweeps to
    full and resets twice, which reads as the work having restarted."""

    PLAN = (("checking", 5), ("reading", 80), ("filing", 15))

    def _collect(self):
        seen: list[int] = []
        bar = base.phased(
            lambda note="", *, done=0, total=0, phase="": seen.append(done), self.PLAN)
        return bar, seen

    def test_each_phase_owns_its_share_of_the_bar(self):
        bar, seen = self._collect()
        bar("", done=1, total=1, phase="checking")
        bar("", done=1, total=2, phase="reading")
        bar("", done=1, total=1, phase="filing")
        self.assertEqual(seen, [50, 450, 1000])

    def test_it_never_moves_backwards(self):
        """A retry, or a phase that reports a smaller fraction than the one before it,
        is not the work undoing itself."""
        bar, seen = self._collect()
        bar("", done=9, total=10, phase="reading")
        bar("", done=1, total=10, phase="reading")
        self.assertEqual(seen, [770, 770])

    def test_a_phase_with_no_denominator_holds_where_it_starts(self):
        bar, seen = self._collect()
        bar("asking Calendar.app for the window", phase="checking")
        self.assertEqual(seen, [0])

    def test_no_callback_means_no_wrapper(self):
        self.assertIsNone(base.phased(None, self.PLAN))

    def test_a_denominator_announced_once_is_counted_against_afterwards(self):
        """GroupMe says `0/17 groups` and every tick after that is a bare `done` — the
        same convention `_Job.step` already honours. Forgetting it between ticks resets
        each one to the start of its phase, which is the reset this exists to remove."""
        bar, seen = self._collect()
        bar("", done=0, total=4, phase="reading")
        bar("", done=2, phase="reading")
        bar("", done=4, phase="reading")
        self.assertEqual(seen, [50, 450, 850])


class TestGroupMeFilledTheSameBarThreeTimes(unittest.TestCase):
    """Groups, rosters and DMs each counted their own list against their own total, so
    one fetch swept the lane end to end three times over. Three full bars read as three
    runs — the phase label was the only thing saying otherwise."""

    def test_the_phases_cover_the_bar_exactly_once(self):
        self.assertEqual(sum(weight for _name, weight in groupme.INGEST_PHASES), 100)

    def test_the_phases_are_the_ones_the_run_actually_reports(self):
        source = Path(groupme.__file__).read_text()
        self.assertTrue(groupme.INGEST_PHASES, "no phases to check")
        for name, _weight in groupme.INGEST_PHASES:
            self.assertIn(f'phase="{name}"', source,
                          f"{name} is weighted but never reported")

    def test_one_run_crosses_the_bar_once_and_only_forwards(self):
        seen: list[int] = []
        bar = base.phased(
            lambda note="", *, done=0, total=0, phase="": seen.append(done),
            groupme.INGEST_PHASES)
        bar("authenticating", phase="connecting")
        bar("0/2 groups with new messages", done=0, total=2, phase="reading groups")
        bar("1/2 groups", done=1, phase="reading groups")
        bar("2/2 groups", done=2, phase="reading groups")
        bar("checking group rosters", done=0, total=1, phase="rosters")
        bar("rosters current", done=1, total=1, phase="rosters")
        bar("0/2 DMs with new messages", done=0, total=2, phase="reading DMs")
        bar("2/2 DMs", done=2, phase="reading DMs")
        self.assertEqual(seen, sorted(seen), "the bar restarted or went backwards")
        self.assertEqual(seen.count(base.PHASE_SCALE), 1, "it filled more than once")
        self.assertEqual(seen[-1], base.PHASE_SCALE)

    def test_a_whole_ical_run_is_one_bar_that_only_grows(self):
        """End to end: asking Calendar.app, reading the events out of it and filing them
        are three phases of one lane, and the page draws `done/total` for every frame."""
        conn = db.connect(Path(tempfile.mkdtemp()) / "t.db")
        db.migrate(conn)
        cfg = Config(home=Path(tempfile.mkdtemp()))
        seen: list[tuple[int, int]] = []

        def progress(note="", *, done=0, total=0, phase=""):
            seen.append((done, total))

        def snapshot(_start, _end, *, progress=None, **_kw):
            for phase, done, total in (("checking", 1, 2), ("checking", 2, 2),
                                       ("reading", 0, 4), ("reading", 4, 4)):
                progress("", done=done, total=total, phase=phase)
            return ical.Snapshot(items=[
                {"calendar_name": "Personal", "calendar_uid": "cal-1", "writable": True,
                 "uid": f"uid-{n}", "title": f"Thing {n}",
                 "start": f"2026-08-1{n}T19:00:00Z", "end": f"2026-08-1{n}T20:00:00Z",
                 "all_day": False, "location": "", "description": "", "url": ""}
                for n in range(4)
            ])

        with mock.patch.object(ical, "_calendar_snapshot", snapshot):
            report = ical.ICalSource().run(conn, cfg, limit=100, progress=progress)
        conn.close()

        self.assertIsNone(report.error)
        self.assertTrue(seen, "the lane got no frames at all")
        self.assertEqual([done for done, _t in seen],
                         sorted(done for done, _t in seen),
                         "a bar that goes backwards reads as the work restarting")
        self.assertTrue(all(total == base.PHASE_SCALE for _d, total in seen),
                        "every frame must carry a denominator or the page animates")
        self.assertEqual(seen[-1][0], base.PHASE_SCALE)


class _FakeCalendar:
    """An osascript that ticks on stderr and returns JSON on stdout, like the real one."""

    def __init__(self, ticks, payload, code=0, hangs=False):
        self.ticks, self.payload, self.code, self.hangs = ticks, payload, code, hangs
        self.killed = False

    def __call__(self, _command, stdout=None, stderr=None, text=True):
        stdout.write(self.payload.encode("utf-8"))
        self.stderr = io.StringIO("".join(line + "\n" for line in self.ticks))
        return self

    def wait(self, timeout=None):
        if self.hangs:
            raise subprocess.TimeoutExpired("osascript", timeout)
        return self.code

    def kill(self):
        self.killed = True


if __name__ == "__main__":
    unittest.main()
