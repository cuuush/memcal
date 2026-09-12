"""The machine is an axis too, and so is the checkout.

The suite already covers failures that appear only on certain weekdays, before 19:00,
or at UTC-04:00. These are two more machine-dependent cases, and
they are the two `clock_sweep.py` cannot fake, because no environment variable moves
either one: what host the suite runs on, and how much history it was cloned with.

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import os
import plistlib
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
        # The bundle paths are macOS-only; pretend to be there so the routing,
        # build, and health tests exercise the macOS behavior off a Mac.
        self._macos = mock.patch.object(schedule, "_is_macos", return_value=True)
        self._macos.start()

    def tearDown(self):
        self._macos.stop()
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
        plist.unlink(missing_ok=True)
        if installed:
            plist.write_bytes(b"")
            # A plist with no script beside it is its own warning, which would answer
            # for launchd here and make the assertion below about the wrong thing.
            schedule.script_path(self.cfg).write_text(
                f'#!/bin/sh\nPY="{sys.executable}"\n', encoding="utf-8")
            schedule.stamp_path(self.cfg).touch()
        with mock.patch.object(schedule, "plist_path", return_value=plist), \
             mock.patch.object(schedule, "_launchctl", answer):
            return {f"{f.section}/{f.name}": f
                    for f in cli.doctor_findings(self.conn, self.cfg)}

    def test_status_answers_on_a_host_that_has_no_launchd(self):
        """`status` is a read, and `doctor` calls it. It raised instead."""
        state = schedule.status(self.cfg, runner=self._absent_launchctl)
        self.assertFalse(state["loaded"])

    def test_the_doctor_reports_the_launchd_it_was_given_and_not_the_machine(self):
        """Findings follow the injected launchd answer, not the host state."""
        loaded = self._findings(lambda *a, **kw: (0, ""))["Schedule/nightly"]
        gone = self._findings(
            lambda *a, **kw: (1, "Could not find service"))["Schedule/nightly"]
        self.assertNotIn("NOT LOADED", loaded.detail)
        self.assertEqual(loaded.status, cli.OK)
        self.assertIn("not loaded", gone.detail.lower())

    def test_the_doctor_reads_the_launch_agents_it_was_pointed_at(self):
        """`installed` reflects the pointed-at LaunchAgents directory."""
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
        self.assertEqual(found["Schedule/nightly"].status, cli.WARN)

    def _run_probe(self):
        """Run the permission probe against a fake launchd."""
        captured = {}

        def answer(*args, **_kw):
            if args[0] == "bootstrap":
                probed = plistlib.loads(Path(args[2]).read_bytes())
                captured["plist"] = probed
                argv = probed["ProgramArguments"]
                Path(argv[argv.index("--result") + 1]).write_text(
                    json.dumps({"ok": True, "message": "granted"}), encoding="utf-8")
            return (0, "")

        with mock.patch.object(schedule, "_launchctl", answer):
            ok, msg = schedule.calendar_permission_probe(self.cfg, timeout=1)
        return ok, msg, captured["plist"]

    def test_the_calendar_probe_runs_through_the_app_when_it_is_built(self):
        exe = schedule.app_executable(self.cfg)
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"")
        exe.chmod(0o755)
        ok, msg, plist = self._run_probe()
        argv = plist["ProgramArguments"]
        self.assertTrue(ok)
        self.assertEqual(argv[0], str(exe))
        self.assertEqual(argv[1:-1], self._probe_python_call()[:-1])
        self.assertEqual(Path(argv[-1]).name[:16], "ical-permission-")
        self.assertTrue(str(argv[-1]).endswith("-result.json"))
        self.assertEqual([schedule.APP_BUNDLE_ID],
                         plist["AssociatedBundleIdentifiers"])
        self.assertIn("memcal", msg)

    def test_the_probe_falls_back_to_python_without_the_app(self):
        ok, _msg, plist = self._run_probe()
        self.assertTrue(ok)
        self.assertEqual(plist["ProgramArguments"][:-1], self._probe_python_call()[:-1])
        self.assertTrue(str(plist["ProgramArguments"][-1]).endswith("-result.json"))
        self.assertNotIn("AssociatedBundleIdentifiers", plist)

    def test_the_probe_ignores_a_non_executable_launcher(self):
        exe = schedule.app_executable(self.cfg)
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"")
        ok, _msg, plist = self._run_probe()
        self.assertTrue(ok)
        self.assertEqual(plist["ProgramArguments"][:-1], self._probe_python_call()[:-1])
        self.assertTrue(str(plist["ProgramArguments"][-1]).endswith("-result.json"))
        self.assertNotIn("AssociatedBundleIdentifiers", plist)

    def _probe_python_call(self):
        return [schedule.pinned_python(self.cfg), "-m", "memcal", "ical", "probe",
                "--context", "nightly", "--result",
                str(self.cfg.home / "ical-permission-result.json")]

    def test_the_eventkit_request_runs_as_memcal_not_the_terminal(self):
        """EventKit calls run under the launchd agent identity."""
        captured = {}
        exe = schedule.app_executable(self.cfg)
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"")
        exe.chmod(0o755)

        def answer(*args, **_kw):
            if args[0] == "bootstrap":
                probed = plistlib.loads(Path(args[2]).read_bytes())
                captured["plist"] = probed
                argv = probed["ProgramArguments"]
                Path(argv[argv.index("--result") + 1]).write_text(
                    json.dumps({"ok": True, "message": "granted"}), encoding="utf-8")
            return (0, "")

        with mock.patch.object(schedule, "_launchctl", answer):
            ok, msg = schedule.eventkit_call(self.cfg, "request", timeout=1)
        argv = captured["plist"]["ProgramArguments"]
        self.assertTrue(ok)
        self.assertEqual(argv[0], str(exe))
        self.assertIn("--ek", argv)
        self.assertIn("request", argv)
        self.assertEqual([schedule.APP_BUNDLE_ID],
                         captured["plist"]["AssociatedBundleIdentifiers"])

    def test_the_doctor_reads_the_eventkit_stamp_not_the_terminal(self):
        """Doctor reads the setup stamp, not the terminal identity."""
        self.cfg.publish_calendar = "memcal"
        answer = lambda *_a, **_kw: (0, "")  # noqa: E731
        def refuse(*_a, **_kw):
            raise AssertionError("doctor reached EventKit; it must read the stamp")

        with mock.patch.object(ical, "_account_call", side_effect=refuse):
            failing = self._findings(answer)["Calendar/account"]
            self.assertEqual(failing.status, cli.FAIL)
            self.assertIn("ical setup", failing.fix)
            db.set_meta(self.conn, "ical.eventkit.verified", db.now())
            passing = self._findings(answer)["Calendar/account"]
            self.assertEqual(passing.status, cli.OK)

    def test_the_doctor_stops_trusting_a_stale_eventkit_stamp(self):
        """The stamp is a memory of a grant, not the grant: past its TTL it fails
        and names setup again instead of passing forever."""
        self.cfg.publish_calendar = "memcal"
        answer = lambda *_a, **_kw: (0, "")  # noqa: E731

        def refuse(*_a, **_kw):
            raise AssertionError("doctor reached EventKit; it must read the stamp")

        with mock.patch.object(ical, "_account_call", side_effect=refuse):
            db.set_meta(self.conn, "ical.eventkit.verified",
                        (db.now_dt() - timedelta(days=ical.EVENTKIT_VERIFIED_TTL_DAYS + 1)
                         ).isoformat(timespec="seconds"))
            stale = self._findings(answer)["Calendar/account"]
            self.assertEqual(stale.status, cli.FAIL)
            self.assertIn("ical setup", stale.fix)
            db.set_meta(self.conn, "ical.eventkit.verified",
                        (db.now_dt() - timedelta(days=ical.EVENTKIT_VERIFIED_TTL_DAYS - 1)
                         ).isoformat(timespec="seconds"))
            fresh = self._findings(answer)["Calendar/account"]
            self.assertEqual(fresh.status, cli.OK)

    def _run_ical_probe(self, ek):
        """Run `ical probe` against this test's config without touching macOS.

        The command owns the connection it was handed and closes it, so the test
        reopens afterwards.
        """
        args = argparse.Namespace(action="probe", ek=ek, context="nightly",
                                  result=None, home=str(self.cfg.home))
        with mock.patch.object(cli, "open_ctx", return_value=(self.cfg, self.conn)):
            rc = cli.cmd_ical(args)
        self.conn = db.open_db(self.cfg.db_path)
        return rc

    def test_probe_where_without_publishing_stamps_nothing(self):
        """`where` with publishing off answers without touching EventKit, so an ok
        answer is no verification and stamps none — previously it stamped one."""
        self.cfg.publish_calendar = ""

        def refuse(*_a, **_kw):
            raise AssertionError("probe reached EventKit with publishing off")

        with mock.patch.object(ical, "_account_call", side_effect=refuse):
            rc = self._run_ical_probe("where")
        self.assertEqual(rc, 0)
        self.assertIsNone(db.get_meta(self.conn, "ical.eventkit.verified"))

    def test_probe_where_with_publishing_still_stamps(self):
        """The setup path keeps its stamp: a real `where` check that passed."""
        self.cfg.publish_calendar = "memcal"
        with mock.patch.object(ical, "account_status",
                               return_value=(True, "'memcal' is in iCloud and syncs")):
            rc = self._run_ical_probe("where")
        self.assertEqual(rc, 0)
        self.assertIsNotNone(db.get_meta(self.conn, "ical.eventkit.verified"))

    def test_probe_request_still_stamps(self):
        """The consent step stamps too: a granted request is a real check."""
        self.cfg.publish_calendar = "memcal"
        with mock.patch.object(ical, "request_calendar_access",
                               return_value=(True, "access granted")):
            rc = self._run_ical_probe("request")
        self.assertEqual(rc, 0)
        self.assertIsNotNone(db.get_meta(self.conn, "ical.eventkit.verified"))

    def test_a_failed_eventkit_check_clears_the_stamp(self):
        """Revocation without a live doctor check: the next real probe that fails
        drops the stamp, so doctor fails instead of trusting it to its TTL."""
        self.cfg.publish_calendar = "memcal"
        db.set_meta(self.conn, "ical.eventkit.verified", db.now())
        with mock.patch.object(ical, "account_status",
                               return_value=(False, "no Calendar access yet")):
            rc = self._run_ical_probe("where")
        self.assertEqual(rc, 1)
        self.assertIsNone(db.get_meta(self.conn, "ical.eventkit.verified"))

    def test_a_failed_bare_probe_keeps_the_stamp(self):
        """The bare probe checks the Apple Events path, not EventKit: its failure
        says nothing about the stamp and must not clear it."""
        self.cfg.publish_calendar = "memcal"
        db.set_meta(self.conn, "ical.eventkit.verified", db.now())
        with mock.patch.object(ical, "permission_status",
                               return_value=(False, "no Apple Events access")):
            rc = self._run_ical_probe(None)
        self.assertEqual(rc, 1)
        self.assertIsNotNone(db.get_meta(self.conn, "ical.eventkit.verified"))

    def test_reminder_status_is_read_as_a_number(self):
        """The status check coerces like its siblings: a string "4" grants, and
        garbage denies instead of raising."""
        self.cfg.publish_reminders = "memcal"
        cases = [({"status": "4"}, 0), ({"status": 3}, 0),
                 ({"status": "bogus"}, 1), ({}, 1)]
        for payload, want in cases:
            args = argparse.Namespace(action="status", home=str(self.cfg.home))
            with mock.patch.object(cli, "open_ctx",
                                   return_value=(self.cfg, self.conn)), \
                 mock.patch.object(ical, "_reminder_call", return_value=payload):
                rc = cli.cmd_reminders(args)
            self.conn = db.open_db(self.cfg.db_path)
            self.assertEqual(rc, want, payload)

    def test_build_app_bundle_writes_a_memcal_identity_and_signs_it(self):
        calls = []

        def runner(args, **_kw):
            calls.append(args)
            if "-o" in args:
                out = Path(args[args.index("-o") + 1])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"launcher")
            return subprocess.CompletedProcess(args, 0, "", "")

        out = schedule.build_app_bundle(self.cfg, runner=runner)
        info = plistlib.loads(
            (schedule.app_path(self.cfg) / "Contents" / "Info.plist").read_bytes())
        self.assertEqual(info["CFBundleName"], "memcal")
        self.assertEqual(info["CFBundleIdentifier"], schedule.APP_BUNDLE_ID)
        self.assertTrue(info["NSAppleEventsUsageDescription"])
        # An agent that can still show consent UI: background-only broke `ical
        # setup` (TCC never displayed the dialog; the wait just expired).
        self.assertTrue(info.get("LSUIElement"))
        self.assertNotIn("LSBackgroundOnly", info)
        self.assertTrue(any(a[:2] == ["xcrun", "clang"] for a in calls), calls)
        self.assertTrue(any(a[0] == "codesign" for a in calls), calls)
        self.assertTrue(any("built" in line for line in out), out)
        self.assertTrue(schedule.app_executable(self.cfg).is_file())
        self.assertFalse(schedule.app_executable(self.cfg).with_name(
            schedule.app_executable(self.cfg).name + ".new").exists())

    def test_build_app_bundle_skips_when_current(self):
        def runner(args, **_kw):
            if "-o" in args:
                out = Path(args[args.index("-o") + 1])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"launcher")
            return subprocess.CompletedProcess(args, 0, "", "")

        schedule.build_app_bundle(self.cfg, runner=runner)
        exe = schedule.app_executable(self.cfg)
        exe.chmod(0o755)
        # Make the build unambiguously newer than every source without touching
        # the tracked source files themselves.
        src = max(
            schedule.LAUNCHER_SOURCE.stat().st_mtime,
            schedule.ICON_SOURCE.stat().st_mtime if schedule.ICON_SOURCE.is_file() else 0,
        )
        os.utime(exe, (src + 60, src + 60))
        calls = []

        def counting(args, **_kw):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, "", "")

        out = schedule.build_app_bundle(self.cfg, runner=counting)
        self.assertTrue(any("is current" in line for line in out), out)
        self.assertFalse(any(a[0] == "codesign" for a in calls), calls)

    def test_failed_rebuild_preserves_the_working_launcher(self):
        exe = schedule.app_executable(self.cfg)
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"working")
        exe.chmod(0o755)
        # Make the binary unambiguously older than the source to force a rebuild
        # attempt, without touching the tracked source file.
        src = schedule.LAUNCHER_SOURCE.stat().st_mtime
        os.utime(exe, (src - 60, src - 60))

        def failing(args, **_kw):
            if args[:2] == ["xcrun", "-f"]:
                return subprocess.CompletedProcess(args, 0, "", "")
            return subprocess.CompletedProcess(args, 1, "", "boom")

        out = schedule.build_app_bundle(self.cfg, runner=failing)
        self.assertEqual(b"working", exe.read_bytes())
        self.assertTrue(any("could not compile" in line for line in out), out)

    def test_force_rebuilds_even_when_current(self):
        def runner(args, **_kw):
            if "-o" in args:
                out = Path(args[args.index("-o") + 1])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"launcher")
            return subprocess.CompletedProcess(args, 0, "", "")

        schedule.build_app_bundle(self.cfg, runner=runner)
        exe = schedule.app_executable(self.cfg)
        src = max(
            schedule.LAUNCHER_SOURCE.stat().st_mtime,
            schedule.ICON_SOURCE.stat().st_mtime if schedule.ICON_SOURCE.is_file() else 0,
        )
        os.utime(exe, (src + 60, src + 60))
        calls = []

        def counting(args, **_kw):
            calls.append(args)
            if "-o" in args:
                out = Path(args[args.index("-o") + 1])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"launcher")
            return subprocess.CompletedProcess(args, 0, "", "")

        out = schedule.build_app_bundle(self.cfg, runner=counting, force=True)
        self.assertTrue(any("built" in line for line in out), out)
        self.assertTrue(any(a[0] == "codesign" for a in calls), calls)

    def test_bundle_health_names_the_broken_states(self):
        verdict, _ = schedule.bundle_health(self.cfg)
        self.assertEqual("absent", verdict)
        exe = schedule.app_executable(self.cfg)
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"working")
        verdict, detail = schedule.bundle_health(self.cfg)
        self.assertEqual("stale", verdict)
        self.assertIn("not executable", detail)
        exe.chmod(0o755)
        (schedule.app_path(self.cfg) / "Contents" / "Info.plist").parent.mkdir(
            parents=True, exist_ok=True)
        with (schedule.app_path(self.cfg) / "Contents" / "Info.plist").open("wb") as fh:
            plistlib.dump(schedule.render_info_plist(), fh)
        src = max(
            schedule.LAUNCHER_SOURCE.stat().st_mtime,
            schedule.ICON_SOURCE.stat().st_mtime if schedule.ICON_SOURCE.is_file() else 0,
        )
        os.utime(exe, (src + 60, src + 60))
        verdict, _ = schedule.bundle_health(self.cfg)
        self.assertEqual("ok", verdict)

    def test_plist_identity_health_matches_the_bundle(self):
        with mock.patch.object(schedule, "plist_path",
                               return_value=self.cfg.home / "missing.plist"):
            self.assertEqual("absent", schedule.plist_identity_health(self.cfg)[0])
        plist = self.cfg.home / "nightly.plist"
        with mock.patch.object(schedule, "plist_path", return_value=plist):
            with plist.open("wb") as fh:
                plistlib.dump(schedule.render_plist(self.cfg), fh)
            verdict, _ = schedule.plist_identity_health(self.cfg)
            self.assertEqual("ok", verdict)
            exe = schedule.app_executable(self.cfg)
            exe.parent.mkdir(parents=True, exist_ok=True)
            exe.write_bytes(b"")
            exe.chmod(0o755)
            verdict, detail = schedule.plist_identity_health(self.cfg)
            self.assertNotEqual("ok", verdict)
            self.assertIn("bundle", detail)

    def test_doctor_reports_a_stale_bundle(self):
        exe = schedule.app_executable(self.cfg)
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"working")
        exe.chmod(0o755)
        (schedule.app_path(self.cfg) / "Contents").mkdir(parents=True, exist_ok=True)
        with (schedule.app_path(self.cfg) / "Contents" / "Info.plist").open("wb") as fh:
            plistlib.dump(schedule.render_info_plist(), fh)
        src = schedule.LAUNCHER_SOURCE.stat().st_mtime
        os.utime(exe, (src - 60, src - 60))
        with mock.patch.object(schedule, "_launchctl", return_value=(1, "not loaded")):
            found = {f"{f.section}/{f.name}": f
                     for f in cli.doctor_findings(self.conn, self.cfg)}
        self.assertIn("Schedule/app bundle", found)
        self.assertNotEqual(cli.OK, found["Schedule/app bundle"].status)
        self.assertIn("--rebuild", found["Schedule/app bundle"].fix)

    def test_build_app_bundle_degrades_without_a_compiler(self):
        def runner(args, **_kw):
            return subprocess.CompletedProcess(args, 1, "", "not found")

        with mock.patch.object(schedule.shutil, "which", return_value=None):
            out = schedule.build_app_bundle(self.cfg, runner=runner)
        self.assertFalse(schedule.app_executable(self.cfg).exists())
        self.assertFalse(schedule.app_path(self.cfg).exists())
        self.assertTrue(any("no C compiler" in line for line in out), out)
        self.assertEqual(schedule.launch_through(self.cfg, ["x"]), ["x"])
        with mock.patch.object(schedule, "_launchctl", return_value=(1, "not loaded")):
            found = {f"{f.section}/{f.name}": f
                     for f in cli.doctor_findings(self.conn, self.cfg)}
        self.assertNotEqual(cli.FAIL, found["Schedule/app bundle"].status)

    def test_skip_current_clears_a_leftover_new_file(self):
        def runner(args, **_kw):
            if "-o" in args:
                out = Path(args[args.index("-o") + 1])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"launcher")
            return subprocess.CompletedProcess(args, 0, "", "")

        schedule.build_app_bundle(self.cfg, runner=runner)
        exe = schedule.app_executable(self.cfg)
        exe.chmod(0o755)
        src = max(
            schedule.LAUNCHER_SOURCE.stat().st_mtime,
            schedule.ICON_SOURCE.stat().st_mtime if schedule.ICON_SOURCE.is_file() else 0,
        )
        os.utime(exe, (src + 60, src + 60))
        exe.with_name(exe.name + ".new").write_bytes(b"orphan")
        out = schedule.build_app_bundle(self.cfg, runner=runner)
        self.assertTrue(any("is current" in line for line in out), out)
        self.assertFalse(exe.with_name(exe.name + ".new").exists())

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


class TestCalendarIdentityReexec(unittest.TestCase):
    """`memcal` re-runs itself through the app bundle so every context — manual CLI,
    the web server, the nightly job — reads Calendar under one memcal identity."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()
        self._macos = mock.patch.object(schedule, "_is_macos", return_value=True)
        self._macos.start()

    def tearDown(self):
        self._macos.stop()
        self.tmp.cleanup()

    def _args(self, cmd):
        return argparse.Namespace(cmd=cmd, home=str(self.cfg.home))

    def _build_fake_app(self):
        exe = schedule.app_executable(self.cfg)
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        exe.chmod(0o755)
        return exe

    def _capture_reexec(self, cmd, argv, *, env):
        calls = []
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch("os.execv", lambda *a: calls.append(a)):
            if "MEMCAL_APP" not in env:
                os.environ.pop("MEMCAL_APP", None)
            cli._maybe_reexec_under_app(self._args(cmd), argv)
        return calls

    def test_a_calendar_command_reexecs_through_the_app(self):
        exe = self._build_fake_app()
        calls = self._capture_reexec(
            "ingest", ["--home", str(self.cfg.home), "ingest", "all"], env={})
        self.assertEqual(len(calls), 1, "a calendar command did not hand off to the app")
        prog, new_argv = calls[0]
        self.assertEqual(prog, str(exe))
        self.assertEqual(new_argv[0], str(exe))
        # The app runs `<python> -m memcal <original args>`.
        self.assertEqual(new_argv[1:3], [sys.executable, "-m"])
        self.assertEqual(new_argv[-2:], ["ingest", "all"])

    def test_already_under_the_app_does_not_reexec(self):
        self._build_fake_app()
        calls = self._capture_reexec("ingest", ["ingest"], env={"MEMCAL_APP": "1"})
        self.assertEqual(calls, [], "re-exec looped instead of stopping under the app")

    def test_schedule_command_never_reexecs(self):
        # `schedule` builds and removes the bundle, so it must not depend on it.
        self._build_fake_app()
        calls = self._capture_reexec("schedule", ["schedule", "install"], env={})
        self.assertEqual(calls, [])

    def test_no_bundle_means_no_reexec(self):
        # Nothing built (or a non-macOS host): run in place, as before the bundle.
        calls = self._capture_reexec("ingest", ["ingest"], env={})
        self.assertEqual(calls, [])

    def test_off_macos_never_reexecs_even_with_a_bundle(self):
        self._build_fake_app()
        with mock.patch.object(schedule, "_is_macos", return_value=False):
            calls = self._capture_reexec("ingest", ["ingest", "all"], env={})
        self.assertEqual(calls, [])

    def test_off_macos_skips_the_bundle_build_and_health(self):
        with mock.patch.object(schedule, "_is_macos", return_value=False):
            out = schedule.build_app_bundle(self.cfg)
            self.assertTrue(any("macOS-only" in line for line in out), out)
            self.assertFalse(schedule.app_path(self.cfg).exists())
            self.assertEqual("ok", schedule.bundle_health(self.cfg)[0])
            self.assertEqual("ok", schedule.plist_identity_health(self.cfg)[0])
            self.assertEqual(["x"], schedule.launch_through(self.cfg, ["x"]))


class TestConsentRequestsNeverHangSilently(unittest.TestCase):
    """`ical setup` once blocked for two silent minutes on a dialog macOS never
    showed: the JXA spun 120s, Python waited 150s, and nothing was printed while
    either waited. The waits are bounded now, the wait is announced by the caller,
    and an unanswered request says how to grant by hand."""

    def _request(self, payload: str):
        seen = {}

        def run(command, **kw):
            seen.update(kw)
            return type("Done", (), {"returncode": 0, "stdout": payload,
                                     "stderr": ""})()

        with mock.patch.object(ical, "_have_osascript", return_value=True):
            return ical.request_calendar_access(runner=run), seen

    def test_the_eventkit_wait_is_bounded(self):
        (_, _), seen = self._request('{"granted": true, "answered": true, "status": 4}')
        self.assertLessEqual(seen.get("timeout", 999), 60)

    def test_full_access_on_either_sdk_era_counts(self):
        for status in (3, 4):
            (ok, message), _ = self._request(
                '{"granted": true, "answered": true, "status": %d}' % status)
            self.assertTrue(ok, message)

    def test_add_only_is_not_full_access(self):
        """`granted` alone lies: Add-Only grants yet reads nothing. Setup must say
        which switch to flip instead of reporting success."""
        (ok, message), _ = self._request(
            '{"granted": true, "answered": true, "status": 5}')
        self.assertFalse(ok)
        self.assertIn("Full Access", message)

    def test_an_unanswered_dialog_names_the_manual_grant(self):
        (ok, message), _ = self._request('{"granted": false, "answered": false}')
        self.assertFalse(ok)
        self.assertIn("memcal.app", message)
        self.assertIn("ical setup", message)

    def test_a_denied_dialog_names_memcal_not_the_terminal(self):
        (ok, message), _ = self._request('{"granted": false, "answered": true}')
        self.assertFalse(ok)
        self.assertIn("memcal", message)
        self.assertNotIn("terminal", message.lower())

    def test_the_jxa_spins_are_bounded(self):
        for script in (ical.ACCOUNT_JXA, ical.REMINDERS_JXA):
            self.assertNotIn("dateWithTimeIntervalSinceNow(120)", script)
            self.assertNotIn("dateWithTimeIntervalSinceNow(60)", script)


class TestAppBundleIconWiring(unittest.TestCase):
    """The handle-grid art ships as the bundle icon and web favicon."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(home=Path(self.tmp.name))
        self.cfg.ensure_dirs()

    def tearDown(self):
        self.tmp.cleanup()

    def test_info_plist_names_the_icon(self):
        info = schedule.render_info_plist()
        self.assertEqual(schedule.APP_ICON_NAME, info["CFBundleIconFile"])
        self.assertEqual(schedule.APP_ICON_NAME, info["CFBundleIconName"])

    def test_icon_source_is_checked_in(self):
        self.assertTrue(schedule.ICON_SOURCE.is_file(), schedule.ICON_SOURCE)
        self.assertTrue((ROOT / "memcal" / "static" / "icon.png").is_file())

    def test_build_writes_the_icon_when_tools_succeed(self):
        def runner(args, **_kw):
            if args[0] == "sips":
                Path(args[-1]).write_bytes(b"cell")
                return subprocess.CompletedProcess(args, 0, "", "")
            if args[0] == "iconutil":
                out = Path(args[args.index("-o") + 1])
                out.write_bytes(b"icns")
                return subprocess.CompletedProcess(args, 0, "", "")
            if "-o" in args:
                out = Path(args[args.index("-o") + 1])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"launcher")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(schedule, "_is_macos", return_value=True):
            out = schedule.build_app_bundle(self.cfg, runner=runner)
        self.assertTrue(schedule.app_icon_path(self.cfg).is_file())
        self.assertTrue(any("AppIcon.icns" in line for line in out), out)

    def test_build_stays_iconless_but_green_when_tools_fail(self):
        def runner(args, **_kw):
            if args[0] in ("sips", "iconutil"):
                return subprocess.CompletedProcess(args, 1, "", "no such tool")
            if "-o" in args:
                out = Path(args[args.index("-o") + 1])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"launcher")
            return subprocess.CompletedProcess(args, 0, "", "")

        with mock.patch.object(schedule, "_is_macos", return_value=True):
            out = schedule.build_app_bundle(self.cfg, runner=runner)
        self.assertTrue(schedule.app_executable(self.cfg).is_file())
        self.assertTrue(any("built" in line for line in out), out)

    def test_newer_icon_source_marks_the_bundle_stale(self):
        exe = schedule.app_executable(self.cfg)
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"launcher")
        exe.chmod(0o755)
        (schedule.app_path(self.cfg) / "Contents" / "Info.plist").parent.mkdir(
            parents=True, exist_ok=True)
        with (schedule.app_path(self.cfg) / "Contents" / "Info.plist").open("wb") as fh:
            plistlib.dump(schedule.render_info_plist(), fh)
        launcher_mtime = schedule.LAUNCHER_SOURCE.stat().st_mtime
        os.utime(exe, (launcher_mtime - 60, launcher_mtime - 60))
        # Older than the launcher alone is stale regardless of the icon.
        self.assertFalse(schedule._bundle_is_current(self.cfg))
        # A newer icon source must trip staleness on its own, without touching
        # the tracked icon file: stand in a controlled icon newer than the exe.
        with tempfile.TemporaryDirectory() as tmp:
            icon = Path(tmp) / "icon.png"
            icon.write_bytes(b"icon")
            os.utime(icon, (launcher_mtime + 120, launcher_mtime + 120))
            with mock.patch.object(schedule, "ICON_SOURCE", icon):
                os.utime(exe, (launcher_mtime + 60, launcher_mtime + 60))
                self.assertFalse(schedule._bundle_is_current(self.cfg))
                os.utime(exe, (launcher_mtime + 180, launcher_mtime + 180))
                self.assertTrue(schedule._bundle_is_current(self.cfg))


if __name__ == "__main__":
    unittest.main()
