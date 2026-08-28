"""Focused HTTP trust-boundary checks for the local web controls."""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from memcal import web, web_server


class TestBrowserMutationsHaveARealSameOriginBoundary(unittest.TestCase):
    origin = "http://127.0.0.1:8765"
    token = "unpredictable-server-token"

    def headers(self, **extra):
        headers = {
            "Host": "127.0.0.1:8765",
            "Origin": self.origin,
            "Cookie": f"{web.CSRF_COOKIE}={self.token}",
        }
        headers.update(extra)
        return headers

    def test_the_page_that_received_this_servers_cookie_can_mutate(self):
        self.assertTrue(web._is_same_origin_mutation(
            self.headers(), origin=self.origin, csrf_token=self.token))

    def test_a_hostile_origin_or_host_cannot_spend_or_change_anything(self):
        hostile = (
            self.headers(Origin="https://attacker.example"),
            self.headers(Host="attacker.example"),
            self.headers(Host="localhost:8765"),
            self.headers(Cookie=""),
            self.headers(Cookie=f"{web.CSRF_COOKIE}=guessed"),
        )
        for headers in hostile:
            with self.subTest(headers=headers):
                self.assertFalse(web._is_same_origin_mutation(
                    headers, origin=self.origin, csrf_token=self.token))

    def test_the_csrf_cookie_is_per_server_and_not_a_prefix_match(self):
        headers = self.headers(Cookie=f"{web.CSRF_COOKIE}={self.token}-suffix")
        self.assertFalse(web._is_same_origin_mutation(
            headers, origin=self.origin, csrf_token=self.token))

    def test_handler_rejects_a_cross_site_post_before_opening_the_store(self):
        handler = object.__new__(web.Handler)
        handler.path = "/api/dream"
        handler.headers = self.headers(Origin="https://attacker.example")
        handler.origin = self.origin
        handler.csrf_token = self.token
        handler.rfile = io.BytesIO(b"{}")
        handler._send = mock.Mock()
        handler._conn = mock.Mock(side_effect=AssertionError("store must stay unopened"))

        handler.do_POST()

        handler._send.assert_called_once_with(
            {"error": "cross-site requests are not allowed"}, 403)

    def test_dns_rebound_host_cannot_read_private_apis(self):
        handler = object.__new__(web.Handler)
        handler.path = "/api/memory"
        handler.headers = {"Host": "attacker.example:8765"}
        handler.origin = self.origin
        handler._send = mock.Mock()
        handler._conn = mock.Mock(side_effect=AssertionError("store must stay unopened"))

        handler.do_GET()

        handler._send.assert_called_once_with({"error": "unexpected host"}, 403)


class TestPostBodiesAreJsonOnlyWhenTheyHaveBytes(unittest.TestCase):
    def test_the_existing_json_ui_content_type_is_accepted(self):
        self.assertEqual(web._read_json_body(
            {"Content-Length": "2", "Content-Type": "application/json; charset=utf-8"},
            io.BytesIO(b"{}")), {})

    def test_nonempty_form_or_unspecified_bodies_are_rejected_before_json_parsing(self):
        for content_type in ("", "text/plain", "application/x-www-form-urlencoded"):
            stream = mock.Mock()
            with self.subTest(content_type=content_type), self.assertRaises(
                    web.RequestBodyError) as caught:
                web._read_json_body(
                    {"Content-Length": "2", "Content-Type": content_type}, stream)
            self.assertEqual(caught.exception.status, 415)
            stream.read.assert_not_called()

    def test_an_empty_post_keeps_the_legacy_empty_object_behavior(self):
        self.assertEqual(web._read_json_body({}, io.BytesIO()), {})


class TestIPv6LoopbackIsActuallyServed(unittest.TestCase):
    def test_ipv6_server_uses_af_inet6_and_a_bracketed_browser_url(self):
        self.assertEqual(web.IPv6ThreadingHTTPServer.address_family, web.socket.AF_INET6)
        cfg = web.Config(home=web.Path("/tmp/memcal-web-security-test"))
        httpd = mock.Mock()
        httpd.serve_forever.side_effect = KeyboardInterrupt
        with mock.patch.object(web_server, "IPv6ThreadingHTTPServer", return_value=httpd) as server, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            web.serve(cfg, host="::1", open_browser=False)
        self.assertEqual(server.call_args.args[0], ("::1", 8765))
        self.assertIn("http://[::1]:8765", output.getvalue())
        httpd.server_close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
