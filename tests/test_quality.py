"""Regression checks for repository and process boundaries."""

from __future__ import annotations

import ast
import contextlib
import hashlib
import io
import json
import re
import sqlite3
import subprocess
import tempfile
import tokenize
import tomllib
import unittest
import urllib.error
from pathlib import Path, PurePosixPath
from unittest import mock

from memcal import cli, db, llm, trace, web, web_server
from memcal.sources import base
from tests.scenarios import skeleton


ROOT = Path(__file__).resolve().parent.parent


class TestTheWheelContainsTheWholeWebUI(unittest.TestCase):
    def test_every_static_file_is_declared_as_package_data(self):
        manifest = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        patterns = manifest["tool"]["setuptools"]["package-data"]["memcal"]
        static = [p.relative_to(ROOT / "memcal").as_posix()
                  for p in (ROOT / "memcal" / "static").iterdir() if p.is_file()]
        missing = [name for name in static
                   if not any(PurePosixPath(name).match(pattern) for pattern in patterns)]
        self.assertEqual(missing, [])


class TestTheWebServerStaysOnLoopback(unittest.TestCase):
    def test_loopback_spellings_are_the_only_allowed_hosts(self):
        for host in ("localhost", "127.0.0.1", "127.20.30.40", "::1"):
            with self.subTest(host=host):
                self.assertTrue(web._is_loopback_host(host))
        for host in ("", "0.0.0.0", "::", "192.168.1.20", "example.com"):
            with self.subTest(host=host):
                self.assertFalse(web._is_loopback_host(host))

    def test_remote_host_is_rejected_before_the_socket_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = web.Config(home=Path(tmp))
            with mock.patch.object(
                web_server, "ThreadingHTTPServer",
                side_effect=AssertionError("socket creation crossed the trust boundary"),
            ):
                with self.assertRaises(web.WebError):
                    web.serve(cfg, host="0.0.0.0", open_browser=False)


class TestTheWebServerBoundsRequestBodies(unittest.TestCase):
    def test_oversized_body_is_rejected_without_being_read(self):
        stream = mock.Mock()
        with self.assertRaises(web.RequestBodyError) as caught:
            web._read_json_body(
                {"Content-Length": str(web.MAX_REQUEST_BYTES + 1)}, stream)
        self.assertEqual(caught.exception.status, 413)
        stream.read.assert_not_called()

    def test_malformed_lengths_and_json_are_client_errors(self):
        cases = [
            ({"Content-Length": "many"}, io.BytesIO(), "content length"),
            ({"Content-Length": "-1"}, io.BytesIO(), "content length"),
            ({"Content-Length": "1", "Content-Type": "application/json"},
             io.BytesIO(b"{"), "json"),
        ]
        for headers, stream, message in cases:
            with self.subTest(headers=headers):
                with self.assertRaises(web.RequestBodyError) as caught:
                    web._read_json_body(headers, stream)
                self.assertEqual(caught.exception.status, 400)
                self.assertIn(message, str(caught.exception).lower())

    def test_an_empty_body_keeps_the_existing_empty_object_semantics(self):
        self.assertEqual(web._read_json_body({}, io.BytesIO()), {})


class TestHttpErrorsReleaseTheirResponses(unittest.TestCase):
    @staticmethod
    def _error(status: int = 401):
        body = io.BytesIO(json.dumps({"error": "nope"}).encode())
        error = urllib.error.HTTPError(
            "https://example.test/x", status, "nope", {}, body)
        return error, body

    def test_source_transport_closes_the_error_response(self):
        error, body = self._error()
        with mock.patch.object(base.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(base.HttpError):
                base.get_json("https://example.test/x")
        self.assertTrue(body.closed)

    def test_model_transport_closes_the_error_response(self):
        error, body = self._error()
        with mock.patch.object(llm.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(llm.LLMError):
                llm.OpenRouter("test-key")._post("/x", {})
        self.assertTrue(body.closed)

    def test_trace_transport_closes_the_error_response(self):
        error, body = self._error(404)
        with mock.patch.object(trace.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(trace.TraceError):
                trace.fetch("test-key", "generation")
        self.assertTrue(body.closed)


class TestModelRequestsKeepTheirAttributionHeaders(unittest.TestCase):
    def test_openrouter_receives_the_standard_referer_header(self):
        client = llm.OpenRouter("test-key", referer="https://example.test/memcal")
        self.assertEqual(
            client.headers["HTTP-Referer"], "https://example.test/memcal")
        self.assertNotIn("HTTeferer", client.headers)


class TestCliMainClosesEveryOpenedDatabase(unittest.TestCase):
    def test_successful_command_closes_its_connection(self):
        opened = []
        real_open = db.open_db

        def tracked_open(path):
            conn = real_open(path)
            opened.append(conn)
            return conn

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            cli.db, "open_db", side_effect=tracked_open,
        ), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["--home", tmp, "todos"]), 0)

        self.assertTrue(opened)
        for conn in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")


class TestTrackedFixturesContainOnlyApprovedSyntheticIdentities(unittest.TestCase):
    """Shareable fixtures use one reviewed cast; no private identifier reaches tracked text."""

    FROZEN = frozenset()
    PRIVATE_DIGESTS = frozenset({
        "00dfd39c5df1c3ba20f49cfdb8d336fa2ae804db8021cb355138910eb7e8d8b7",
        "ee61600e7bcfba96ea015358ae470d6e87d883b13b86fba13c5a28eced4b915e",
        "b1526dd4f61b0e8fabe93245669fea73c654efee07778b6b8bfb4b71fa492973",
        "f2992e62ed8ea01eee5c61648e1fbfe4e47b6a2f92f7ea953e99c8ca5baa2e6b",
        "31939deaf302489bcb6285e041909762d8234673db9e57e2862a4d67db374a38",
        "cd94ff4b286e88e339799eb64d4c86d381878043e4de8647bc18ebc1f7fe32c2",
        "2c865d3df6ebedd58ff666155ee115fc5549aeaa1fec3c5d2c1fb77445516945",
        "780e6a69d79bdc1ada3a6ca239bb4ce91310b781a57507187fd10d3c98bd8059",
        "adb44eed3736d3277bab68e561bcc90237fba7df5eecd19a9321da91421dbc20",
        "3572dd8f072ad9a488df0f7e2bd86047d8a105b81e00547b66b4e08f8490b5d2",
        "2fb10b6a0209f3c6985c9dc1be5466b18e056af9e429d4e99339990cf0b6b70d",
        "84942891b145ad6057db9f5bf9277e6af902b900eafa6ba823389e17560d28e9",
        "5a92f61066f597caa0e5d24a571244bf60e8da38b150fc6af9f01a84ec9c3b4f",
    })
    PHONE_LIKE = re.compile(
        r"(?<!\d)(?:\+?1[-. ()]*)?(?:[2-9]\d{2}[-. ()]*)[2-9]\d{2}"
        r"[-. ]*\d{4}(?!\d)"
    )
    APPROVED_CAST = {
        "me": ("+19178889999", "88000", "19178889999@s.whatsapp.net"),
        "Jordan Lee": ("+19175550001", "88001", "19175550001@s.whatsapp.net"),
        "Alex Rivera": ("+19175550002", "88002", "19175550002@s.whatsapp.net"),
        "Cameron Ortiz": ("+19175550003", "88003", "19175550003@s.whatsapp.net"),
        "Harper": ("+19175550004", "88004", "19175550004@s.whatsapp.net"),
        "Riley Morgan": ("+19175550005", "88005", "19175550005@s.whatsapp.net"),
        "Rowan Vale": ("+19175550006", "88006", "19175550006@s.whatsapp.net"),
        "Alex Chen": ("+19175550007", "88007", "19175550007@s.whatsapp.net"),
        "Skyler Reed": ("+19175550008", "88008", "19175550008@s.whatsapp.net"),
        "Devon Park": ("+19175550009", "88009", "19175550009@s.whatsapp.net"),
        "Bailey Stone": ("+19175550010", "88010", "19175550010@s.whatsapp.net"),
        "Mom": ("+19175550011", "88011", "19175550011@s.whatsapp.net"),
        "Jose": ("+19175550012", "88012", "19175550012@s.whatsapp.net"),
        "Quinn Brooks": ("+19175550013", "88013", "19175550013@s.whatsapp.net"),
        "Rae": ("+19175550014", "88014", "19175550014@s.whatsapp.net"),
        "Morgan": ("+19175550015", "88015", "19175550015@s.whatsapp.net"),
        "Nadia Okoro": ("+19175550016", "88016", "19175550016@s.whatsapp.net"),
        "Sasha Kim": ("+19175550017", "88017", "19175550017@s.whatsapp.net"),
        "Unnameable Neighbour": (
            "+261516951601296", "88018", "261516951601296@lid"),
    }
    APPROVED_PERSON_MAIL = {
        ("Alex Rivera", "alex.rivera@example.com"),
        ("Bailey Stone", "bailey@example.com"),
        ("Morgan Hale", "morgan@harbortutoring.example"),
    }

    @staticmethod
    def _digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def test_tracked_text_has_no_private_identifiers_or_local_home_paths(self):
        names = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z"], check=True,
            capture_output=True).stdout.decode().split("\0")
        findings = []
        for name in filter(None, names):
            if name in self.FROZEN:
                continue
            path = ROOT / name
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if re.search(r"/(?:Users|home)/[^/\s]+/", text):
                findings.append(f"{name}:local-home-path")
            words = re.findall(r"[a-z0-9]+", text.casefold())
            for width in (1, 2, 3):
                for start in range(len(words) - width + 1):
                    digest = self._digest(" ".join(words[start:start + width]))
                    if digest in self.PRIVATE_DIGESTS:
                        findings.append(f"{name}:private-identifier:{digest[:12]}")
        self.assertEqual(sorted(set(findings)), [])

    def test_comments_and_docstrings_contain_no_phone_numbers(self):
        names = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z"], check=True,
            capture_output=True).stdout.decode().split("\0")
        findings = []
        line_comments = {".sql": "--", ".js": "//", ".sh": "#",
                         ".toml": "#", ".yml": "#", ".yaml": "#"}
        for name in filter(None, names):
            path = ROOT / name
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if path.suffix == ".py":
                tokens = tokenize.generate_tokens(io.StringIO(text).readline)
                for token in tokens:
                    if token.type == tokenize.COMMENT and self.PHONE_LIKE.search(token.string):
                        findings.append(f"{name}:{token.start[0]}:comment")
                tree = ast.parse(text)
                for node in ast.walk(tree):
                    if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                             ast.AsyncFunctionDef)):
                        continue
                    doc = ast.get_docstring(node, clean=False)
                    if doc and self.PHONE_LIKE.search(doc):
                        findings.append(f"{name}:{getattr(node, 'lineno', 1)}:docstring")
                continue
            marker = line_comments.get(path.suffix)
            if marker:
                for line_number, line in enumerate(text.splitlines(), 1):
                    comment = line.partition(marker)[2]
                    if comment and self.PHONE_LIKE.search(comment):
                        findings.append(f"{name}:{line_number}:comment")
        self.assertEqual(findings, [])

    def test_the_canonical_native_fixture_identities_are_reviewed(self):
        self.assertEqual(skeleton.CAST, self.APPROVED_CAST)
        person_mail = {(row["name"], row["addr"]) for row in skeleton.EMAIL
                       if row["kind"] == "person"}
        self.assertEqual(person_mail, self.APPROVED_PERSON_MAIL)

        fixture = ROOT / "tests" / "scenarios" / "fixtures"
        approved_names = (set(self.APPROVED_CAST) - {"me"}) | {"Casey", "GroupMe"}
        approved_phones = {row[0] for row in self.APPROVED_CAST.values()} | {"262966"}
        approved_gm_ids = {row[1] for row in self.APPROVED_CAST.values()} | {"0", "system"}
        approved_jids = {row[2] for row in self.APPROVED_CAST.values()}

        blue = json.loads((fixture / "bluebubbles" / "messages.json").read_text())
        for row in blue:
            addresses = [p["address"] for p in row["chats"][0]["participants"]]
            addresses.append(row["chats"][0]["chatIdentifier"])
            if row["handle"]:
                addresses.append(row["handle"]["address"])
            self.assertTrue(set(addresses) <= approved_phones)

        groupme = fixture / "groupme"
        payloads = [json.loads((groupme / "groups.json").read_text())]
        payloads += [json.loads(path.read_text())
                     for path in sorted(groupme.glob("msgs-*.json"))]
        for payload in payloads:
            for row in payload:
                if "members" in row:
                    self.assertTrue({m["nickname"] for m in row["members"]}
                                    <= approved_names)
                    self.assertTrue({m["user_id"] for m in row["members"]}
                                    <= approved_gm_ids)
                else:
                    self.assertIn(row["name"], approved_names)
                    self.assertIn(row["user_id"], approved_gm_ids)

        conn = sqlite3.connect(fixture / "whatsapp" / "ChatStorage.sqlite")
        self.addCleanup(conn.close)
        names = {row[0] for row in conn.execute(
            "SELECT ZCONTACTNAME FROM ZWAGROUPMEMBER WHERE ZCONTACTNAME IS NOT NULL")}
        jids = {row[0] for row in conn.execute(
            "SELECT ZMEMBERJID FROM ZWAGROUPMEMBER")}
        self.assertTrue(names <= approved_names)
        self.assertTrue(jids <= approved_jids)


class TestDocstringsStayFocused(unittest.TestCase):
    def test_tracked_docstrings_stay_under_the_review_limits(self):
        names = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "*.py", "-z"], check=True,
            capture_output=True).stdout.decode().split("\0")
        findings = []
        for name in filter(None, names):
            path = ROOT / name
            if not path.is_file():
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                         ast.AsyncFunctionDef)):
                    continue
                doc = ast.get_docstring(node, clean=False)
                if not doc:
                    continue
                words = len(doc.split())
                lines = len(doc.splitlines())
                if words > 100 or lines > 12:
                    findings.append(
                        f"{name}:{getattr(node, 'lineno', 1)}:{words} words:{lines} lines")
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
