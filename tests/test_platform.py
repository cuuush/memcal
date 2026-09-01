"""The machine is an axis too, and so is the checkout.

The suite already covers failures that appear only on certain weekdays, before 19:00,
or at UTC-04:00. These are two more machine-dependent cases, and
they are the two `clock_sweep.py` cannot fake, because no environment variable moves
either one: what host the suite runs on, and how much history it was cloned with.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import ast
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memcal import cli, db, events, schedule  # noqa: E402
from memcal.config import Config  # noqa: E402
from memcal.sources import ical  # noqa: E402


class _FakeSnapshotProcess:
    """An `osascript` that writes JSON to the sink and exits, like the real one."""

    def __init__(self, payload: str):
        self.payload = payload
        self.commands: list[list[str]] = []

    def __call__(self, command, stdout=None, stderr=None, text=True):
        self.commands.append(command)
        stdout.write(self.payload.encode("utf-8"))
        self.stderr = io.StringIO("")
        return self

    def wait(self, timeout=None):
        return 0

    def kill(self):  # pragma: no cover - only a timeout reaches this
        pass


class TestASuiteThatIsGreenOnlyOnAMac(unittest.TestCase):
    """Keep platform checks behind injectable seams so the suite runs off macOS."""

    #: Reads the platform question directly, on purpose. `ICalSource.check` is the
    #: source's *health declaration*, it takes no transport, and on a host with no
    #: `osascript` the honest answer really is "unavailable". Anything else added here is
    #: a claim that needs the same argument.
    PLATFORM_HONEST = {"check"}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)

    def tearDown(self):
        db.set_today(None)
        self.conn.close()
        self.tmp.cleanup()

    def day(self, offset: int) -> str:
        return (db.today() + timedelta(days=offset)).isoformat()

    # ----------------------------------------------------------- the seam --

    def _fake_run(self, payload: str = '{"uid": "u-1"}'):
        """A fake `osascript`, and the list of what it was actually asked to run."""
        commands: list[list[str]] = []

        def run(command, **_kw):
            commands.append(command)
            return type("Done", (), {"returncode": 0, "stdout": payload,
                                     "stderr": ""})()

        return run, commands

    def _publish(self, runner=None):
        """One confirmed row published, through whatever transport it is handed."""
        self.cfg.publish_calendar = "memcal"
        event, _ = events.upsert(self.conn, {
            "title": "Tutoring", "date": (db.today() + timedelta(days=1)).isoformat(),
            "kind": "commitment", "status": "confirmed"}, written_by="live")
        return ical.publish(self.conn, self.cfg, event,
                            runner=runner or self._fake_run()[0])

    def _sites(self) -> dict[str, list]:
        """Every entry point in `ical.py` that takes a transport, given a fake one.

        The value is what the fake was handed. Empty means the platform gate answered
        before the seam did, which is the bug this class is named for.
        """
        reached: dict[str, list] = {}

        def attempt(name, call, commands):
            try:
                call()
            except Exception as exc:  # noqa: BLE001 - the failure under test
                reached[name] = exc
            else:
                reached[name] = commands

        run, commands = self._fake_run('{"calendars": 1}')
        attempt("permission_status",
                lambda: ical.permission_status(runner=run), commands)

        snapshot = _FakeSnapshotProcess('{"events": [], "unreadable": []}')
        attempt("_calendar_snapshot",
                lambda: ical._calendar_snapshot(self.day(0), self.day(30),
                                                opener=snapshot),
                snapshot.commands)

        run, commands = self._fake_run('{"stored": true}')
        attempt("_reminder_call",
                lambda: ical._reminder_call("put", "x", runner=run), commands)

        run, commands = self._fake_run('{"status": 3}')
        attempt("_account_call",
                lambda: ical._account_call("where", "memcal", runner=run), commands)

        run, commands = self._fake_run(
            '{"uid": "u-1", "calendar": "memcal", "calendar_uid": "cal-1"}')
        attempt("publish", lambda: self._publish(runner=run), commands)
        return reached

    def test_an_injected_transport_answers_before_the_platform_does(self):
        """The bug, on a host that has no `osascript` — which is every host but this one.

        Nothing here touches Calendar.app on any machine: each site is handed a fake and
        the assertion is that the fake was the thing that ran.
        """
        with mock.patch.object(ical, "_have_osascript", return_value=False):
            reached = self._sites()
        self.assertEqual(len(reached), 5, "every gated entry point is exercised")
        for name, handed in reached.items():
            self.assertIsInstance(
                handed, list,
                f"{name} raised {handed!r} rather than using the transport it was given")
            self.assertTrue(handed, f"{name} never called the transport it was given")

    def test_the_same_calls_still_work_where_osascript_does_exist(self):
        """The decoy. A gate deleted rather than narrowed passes the test above too."""
        with mock.patch.object(ical, "_have_osascript", return_value=True):
            reached = self._sites()
        self.assertEqual(len(reached), 5)
        for name, handed in reached.items():
            self.assertIsInstance(handed, list, f"{name}: {handed!r}")

    def test_a_real_user_still_gets_a_sentence_rather_than_an_oserror(self):
        """The gate's actual job, which narrowing it must not cost.

        Asked with the real transport on a host with none, each of these says what is
        wrong in its own error type. Without the gate the user gets a bare
        `FileNotFoundError: 'osascript'` out of `subprocess`, which names nothing.

        `_unavailable` is forced rather than the real transport being handed over: the
        default `runner=subprocess.run` is bound at import, so a broken gate here would
        drive the real `osascript` — and `permission_status` is the call that opens the
        consent dialog.
        """
        expected = [
            (ical.SourceError,
             lambda: ical._calendar_snapshot(self.day(0), self.day(30),
                                             opener=_FakeSnapshotProcess("{}"))),
            (ical.ReminderError,
             lambda: ical._reminder_call("put", "x", runner=self._fake_run()[0])),
            (ical.CalendarAccountError,
             lambda: ical._account_call("where", "m", runner=self._fake_run()[0])),
            (ical.PublishError, self._publish),
        ]
        self.assertEqual(len(expected), 4)
        with mock.patch.object(ical, "_unavailable", return_value=True):
            passive, message = ical.permission_status(runner=self._fake_run()[0])
            self.assertFalse(passive)
            self.assertIn("osascript is unavailable", message)
            for error, call in expected:
                with self.assertRaises(error) as caught:
                    call()
                self.assertIn("osascript is unavailable", str(caught.exception))

    def test_the_gate_is_about_the_real_transport_and_nothing_else(self):
        with mock.patch.object(ical, "_have_osascript", return_value=False):
            self.assertTrue(ical._unavailable(subprocess.run))
            self.assertTrue(ical._unavailable(subprocess.Popen))
            self.assertFalse(ical._unavailable(lambda *_a, **_kw: None))
        with mock.patch.object(ical, "_have_osascript", return_value=True):
            self.assertFalse(ical._unavailable(subprocess.run))

    # ------------------------------------------------------------ launchd --

    def _absent_launchctl(self, *_a, **_kw):
        raise FileNotFoundError(2, "No such file or directory", "launchctl")

    def _findings(self, answer, *, installed: bool = True):
        """`doctor`, with launchd answering `answer` and its plists in this test's home."""
        plist = self.cfg.home / f"{schedule.LABEL}.plist"
        catchup = self.cfg.home / f"{schedule.CATCHUP_LABEL}.plist"
        for path in (plist, catchup):
            path.unlink(missing_ok=True)
            if installed:
                path.write_bytes(b"")
        with mock.patch.object(schedule, "plist_path", return_value=plist), \
             mock.patch.object(schedule, "catchup_plist_path", return_value=catchup), \
             mock.patch.object(schedule, "_launchctl", answer):
            return {f"{f.section}/{f.name}": f
                    for f in cli.doctor_findings(self.conn, self.cfg)}

    def test_status_answers_on_a_host_that_has_no_launchd(self):
        """`status` is a read, and `doctor` calls it. It raised instead."""
        state = schedule.status(self.cfg, runner=self._absent_launchctl)
        self.assertFalse(state["loaded"])
        self.assertFalse(state["catchup"]["loaded"])

    def test_the_doctor_reports_the_launchd_it_was_given_and_not_the_machine(self):
        """The half worth more than the red: nine findings were facts about their Mac.

        Same store, same config, two different launchd answers — the finding has to
        follow the answer. It followed `launchctl` on whatever machine was running it,
        and was green because *their* has both agents loaded.
        """
        loaded = self._findings(lambda *a, **kw: (0, ""))["Schedule/catch-up"]
        gone = self._findings(
            lambda *a, **kw: (1, "Could not find service"))["Schedule/catch-up"]
        self.assertEqual(loaded.status, cli.OK)
        self.assertEqual(gone.status, cli.WARN)

    def test_the_doctor_reads_the_launch_agents_it_was_pointed_at(self):
        """The other host dependency, and the quieter one.

        `installed` is `~/Library/LaunchAgents/com.memcal.nightly.plist` existing, so the
        doctor tests took the *installed* branch on their Mac and the *not installed*
        branch on CI. Nothing was red either way — the same nine tests were simply
        exercising different code depending on who ran them.
        """
        answer = lambda *_a, **_kw: (0, "")  # noqa: E731 - one launchd reply, twice
        here = self._findings(answer, installed=True)["Schedule/nightly"]
        away = self._findings(answer, installed=False)["Schedule/nightly"]
        self.assertIn("not installed", away.detail)
        self.assertNotIn("not installed", here.detail)

    def test_the_doctor_speaks_on_a_host_with_no_launchd_at_all(self):
        """It raised `FileNotFoundError` out of the diagnostic. The diagnostic is the
        last thing that should need the platform to be healthy before it will answer.

        The real `_launchctl` is used here, with only its transport replaced — the point
        is what that function does with an `OSError`, so stubbing it would test nothing.
        """
        real = schedule._launchctl
        found = self._findings(
            lambda *args, **_kw: real(*args, runner=self._absent_launchctl))
        self.assertIn("Schedule/nightly", found)
        self.assertEqual(found["Schedule/catch-up"].status, cli.WARN)

    # ------------------------------------------- what stops it coming back --

    def _ical_functions(self):
        """Every function in `ical.py`, with the transport parameter it accepts."""
        tree = ast.parse(Path(ical.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            names = [a.arg for a in args.args + args.kwonlyargs]
            transport = [n for n in names if n in ("runner", "opener")]
            yield node, (transport[0] if transport else "")

    def test_nothing_that_takes_a_transport_asks_the_platform_first(self):
        """The structural half, so the seventh site cannot be added by accident.

        A function that has been handed a transport has had the platform question
        answered for it. Asking `shutil.which` anyway is the bug, whatever it then does
        with the answer.
        """
        taking = [node for node, transport in self._ical_functions() if transport]
        self.assertGreaterEqual(len(taking), 10,
                                "the transport seam is what this reads; it is gone")
        offenders = []
        for node in taking:
            offenders += [f"{node.name}:{call.lineno}"
                          for call in ast.walk(node)
                          if isinstance(call, ast.Call)
                          and ast.unparse(call.func).endswith("shutil.which")]
        self.assertEqual(offenders, [],
                         "ask _unavailable(runner) — a caller who injected a transport "
                         "has already answered what shutil.which asks")

    def test_only_the_health_declaration_reads_the_platform_on_its_own(self):
        """`_have_osascript` is the platform question with nothing else attached, and
        the one place it belongs is the answer to "is this source reachable"."""
        callers = set()
        for node, _transport in self._ical_functions():
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and \
                        ast.unparse(call.func).endswith("_have_osascript"):
                    callers.add(node.name)
        self.assertTrue(callers, "nothing asks the platform question at all any more")
        self.assertEqual(callers - {"_unavailable"}, self.PLATFORM_HONEST,
                         "a new direct reader of the platform is a claim that this "
                         "function is unreachable off a Mac; argue it in the issue")

    def _test_sources(self):
        here = Path(__file__).resolve().parent
        return sorted(here.glob("test_*.py"))

    #: The two ways a test reaches launchd. `doctor_findings` is here because it is how
    #: nine of them did it — not by naming `schedule` at all.
    REACHES_LAUNCHD = ("schedule.status", "doctor_findings")

    def _reaching_launchd(self):
        """Every test that reaches launchd, and the fixture that might answer for it."""
        for path in self._test_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for cls in [c for c in ast.walk(tree) if isinstance(c, ast.ClassDef)]:
                fixture = [m for m in cls.body if isinstance(m, ast.FunctionDef)
                           and m.name in ("setUp", "setUpClass")]
                for fn in [m for m in cls.body if isinstance(m, ast.FunctionDef)]:
                    calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call)
                             and ast.unparse(c.func).endswith(self.REACHES_LAUNCHD)]
                    if calls:
                        yield path, cls, fn, calls[0].lineno, fixture

    def _answers_for_launchd(self, *nodes) -> bool:
        """An explicit runner, or launchd patched out. Nothing else counts."""
        for node in nodes:
            for call in [c for c in ast.walk(node) if isinstance(c, ast.Call)]:
                if any(kw.arg == "runner" for kw in call.keywords):
                    return True
                text = ast.unparse(call)
                if "patch" in ast.unparse(call.func) and any(
                        name in text for name in
                        ("_launchctl", '"status"', "'status'", "doctor_findings")):
                    return True
        return False

    def test_no_test_reaches_the_real_launchctl(self):
        """`TestNoTestCanReachTheRealCalendar` for the other outward call.

        `test_web.py:507` shelled out to a real `launchctl list`, and the test it was
        inside is called `test_status_reports_a_missing_install_rather_than_raising`.
        The nine `doctor` tests did it without naming `schedule` at all, which is why
        `doctor_findings` counts as reaching launchd here.
        """
        reaching = list(self._reaching_launchd())
        self.assertTrue(reaching, "nothing calls schedule.status; this checks nothing")
        offenders = [f"{path.name}:{line} {cls.name}.{fn.name}"
                     for path, cls, fn, line, fixture in reaching
                     if not self._answers_for_launchd(fn, *fixture)]
        self.assertEqual(offenders, [],
                         "pass runner= or patch schedule._launchctl; the default runner "
                         "shells out and the answer is this machine's, not the code's")

    def test_no_test_is_skipped_for_not_being_a_mac(self):
        """The fix this must never become, written down where it will be noticed.

        A skip makes the badge green by running 45 fewer checks. If a test genuinely
        cannot run off macOS, that is a case argued in the issue for that one test.
        """
        offenders = []
        for path in self._test_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = ast.unparse(node.func)
                if not (name.endswith("skipUnless") or name.endswith("skipIf")):
                    continue
                text = ast.unparse(node)
                if "sys.platform" in text or "platform.system" in text or \
                        "darwin" in text.lower():
                    offenders.append(f"{path.name}:{node.lineno} {text[:70]}")
        self.assertEqual(offenders, [],
                         "inject the transport instead; a platform skip is a score that "
                         "improved by deleting the checks that failed")


if __name__ == "__main__":
    unittest.main()
