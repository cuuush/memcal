"""Regression checks for CLI resource ownership."""

from __future__ import annotations

import argparse
import contextlib
import io
import sqlite3
import tempfile
import unittest
from unittest import mock

from memcal import cli, db


class TestDoctorClosesItsDatabase(unittest.TestCase):
    def _run(self, extra_args, findings):
        opened = []
        real_open = db.open_db

        def tracked_open(path):
            conn = real_open(path)
            opened.append(conn)
            return conn

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            cli.db, "open_db", side_effect=tracked_open,
        ), mock.patch.object(cli, "doctor_findings", return_value=findings), \
                contextlib.redirect_stdout(io.StringIO()):
            result = cli.main(["--home", tmp, "doctor", *extra_args])

        self.assertTrue(opened)
        for conn in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")
        return result

    def test_success_return_closes_connection(self):
        self.assertEqual(self._run([], []), 0)

    def test_json_error_return_closes_connection(self):
        finding = cli.Finding("Store", "database", cli.FAIL, "broken")
        self.assertEqual(self._run(["--json"], [finding]), 1)

    def test_initialization_error_still_closes_connection(self):
        opened = []
        real_open = db.open_db

        def tracked_open(path):
            conn = real_open(path)
            opened.append(conn)
            return conn

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            cli.db, "open_db", side_effect=tracked_open,
        ), mock.patch.object(
            cli.brief, "write", side_effect=RuntimeError("render failed"),
        ), self.assertRaisesRegex(RuntimeError, "render failed"):
            cli.main(["--home", tmp, "doctor"])

        self.assertTrue(opened)
        for conn in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")


class TestDirectCommandsCloseTheirDatabase(unittest.TestCase):
    """Command functions have callers outside argparse, too.

    Python 3.14 reports a ResourceWarning when those callers leave SQLite to finalizers.
    The three commands below are the direct-call paths that did so; the assertion uses
    SQLite itself rather than relying on a version-specific warning message.
    """

    def _run(self, runner):
        opened = []
        real_open = db.open_db

        def tracked_open(path):
            conn = real_open(path)
            opened.append(conn)
            return conn

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            cli.db, "open_db", side_effect=tracked_open,
        ), contextlib.redirect_stdout(io.StringIO()):
            result = runner(tmp)

        self.assertTrue(opened)
        for conn in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")
        return result

    def test_who_closes_when_called_directly(self):
        self.assertEqual(0, self._run(lambda tmp: cli.cmd_who(
            argparse.Namespace(home=tmp, handle=None, person=None, limit=10, adopt=False))))

    def test_ingest_closes_on_its_early_unknown_source_return(self):
        def runner(tmp):
            args = argparse.Namespace(home=tmp, stream="missing", stale=False,
                                      limit=10, rounds=1)
            with mock.patch.object(cli.sources, "get", return_value=None), \
                    mock.patch.object(cli.sources, "names", return_value=[]):
                return cli.cmd_ingest(args)

        self.assertEqual(1, self._run(runner))

    def test_doctor_closes_when_called_directly(self):
        def runner(tmp):
            args = argparse.Namespace(home=tmp, verbose=False, json=False)
            with mock.patch.object(cli.brief, "write"), \
                    mock.patch.object(cli, "doctor_findings", return_value=[]):
                return cli.cmd_doctor(args)

        self.assertEqual(0, self._run(runner))


if __name__ == "__main__":
    unittest.main()
