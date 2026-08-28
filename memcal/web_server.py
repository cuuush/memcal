"""Loopback HTTP server for the local UI."""

from __future__ import annotations

import errno
import ipaddress
import json
import secrets
import socket
import sqlite3
import subprocess
import threading
import webbrowser
from functools import partial
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import archive, db, threads, trace, wiki
from .config import Config
from . import web_queue, web_memory, web_dream, web_jobs

PAGE = Path(__file__).with_name("webui.html")
STATIC_DIR = Path(__file__).with_name("static")
STATIC_TYPES = {".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8"}
MAX_REQUEST_BYTES = 64 * 1024
CSRF_COOKIE = "memcal_csrf"

class RequestBodyError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _read_json_body(headers, stream) -> dict:
    raw_length = headers.get("Content-Length") or "0"
    try:
        length = int(raw_length)
    except (TypeError, ValueError) as exc:
        raise RequestBodyError("bad content length") from exc
    if length < 0:
        raise RequestBodyError("bad content length")
    if length > MAX_REQUEST_BYTES:
        raise RequestBodyError(
            f"request body exceeds {MAX_REQUEST_BYTES} bytes", status=413)
    if length and headers.get("Content-Type", "").split(";", 1)[0].strip().casefold() \
            != "application/json":
        raise RequestBodyError("request body must be application/json", status=415)
    try:
        payload = json.loads(stream.read(length) or b"{}")
    except (TypeError, ValueError, UnicodeDecodeError) as exc:
        raise RequestBodyError("bad json") from exc
    if not isinstance(payload, dict):
        raise RequestBodyError("json body must be an object")
    return payload


def _is_loopback_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _origin_for(host: str, port: int) -> str:
    """The one browser origin allowed to mutate this particular server."""
    canonical_host = host.casefold()
    if canonical_host != "localhost":
        canonical_host = ipaddress.ip_address(canonical_host).compressed
    authority = (f"[{canonical_host}]:{port}" if ":" in canonical_host
                 else f"{canonical_host}:{port}")
    return f"http://{authority}"


def _csrf_cookie(headers) -> str:
    """Read one cookie without treating a malformed Cookie header as an error."""
    try:
        jar = SimpleCookie(headers.get("Cookie", ""))
    except CookieError:
        return ""
    morsel = jar.get(CSRF_COOKIE)
    return morsel.value if morsel else ""


def _has_expected_host(headers, origin: str) -> bool:
    """Reject DNS rebinding before either private reads or mutations run."""
    expected = origin.removeprefix("http://")
    return headers.get("Host", "").casefold() == expected.casefold()


def _is_same_origin_mutation(headers, *, origin: str, csrf_token: str) -> bool:
    """A browser POST needs its exact host, origin, and this server's fresh cookie.

    Loopback is reachable from every web page on the machine.  `Origin` rejects a
    cross-site form/fetch before an action runs; the unpredictable, SameSite cookie
    means a hostile page cannot make a matching request merely by knowing the port.
    """
    if not _has_expected_host(headers, origin):
        return False
    if headers.get("Origin", "") != origin:
        return False
    supplied = _csrf_cookie(headers)
    return bool(supplied) and secrets.compare_digest(supplied, csrf_token)


class IPv6ThreadingHTTPServer(ThreadingHTTPServer):
    """The stdlib server defaults to AF_INET, even when given an IPv6 address."""
    address_family = socket.AF_INET6


def frontend_source() -> str:
    """Everything the browser is served: the shell plus every split-out module.

    Tests that string-match against page content (e.g. "does the why panel say
    'Original source'") read this instead of `PAGE` alone, since that content
    now lives in memcal/static/*.js rather than inline in webui.html.
    """
    return PAGE.read_text() + "".join(
        p.read_text() for p in sorted(STATIC_DIR.glob("*")))

class Handler(BaseHTTPRequestHandler):
    server_version = "memcal"

    def __init__(self, *args, cfg: Config, origin: str, csrf_token: str, **kwargs):
        self.cfg = cfg
        self.origin = origin
        self.csrf_token = csrf_token
        super().__init__(*args, **kwargs)

    # One connection per request. SQLite objects belong to the thread that made them,
    # and this server is threaded so the browser can fetch four panels at once.
    def _conn(self) -> sqlite3.Connection:
        return db.open_db(self.cfg.db_path)

    def log_message(self, fmt, *args):   # quiet; the terminal is the user's
        pass

    def _send(self, payload, status: int = 200, ctype: str = "application/json") -> None:
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # It is HttpOnly because JavaScript never needs the secret: a same-origin
        # request carries it automatically.  SameSite and the Origin check each cover
        # a browser behaviour the other deliberately does not promise to cover alone.
        self.send_header(
            "Set-Cookie",
            f"{CSRF_COOKIE}={self.csrf_token}; HttpOnly; Path=/; SameSite=Strict",
        )
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, name: str) -> None:
        ctype = STATIC_TYPES.get(Path(name).suffix)
        path = (STATIC_DIR / name).resolve()
        if not ctype or STATIC_DIR.resolve() not in path.parents or not path.is_file():
            return self._send({"error": "not found"}, 404)
        self._send(path.read_bytes(), ctype=ctype)

    def _stream_job(self, query: dict) -> None:
        """Server-sent events for one job: a frame the moment anything changes."""
        job_id, job = web_jobs.find_job(query.get("id", ""), query.get("kind", ""))
        if not job:
            return self._send({"job": None})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        version = -1
        try:
            while True:
                snapshot = job.wait_for_change(version)
                version = snapshot["version"]
                payload = json.dumps({"job": job_id, **snapshot})
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
                if snapshot["done"]:
                    return
        except (BrokenPipeError, ConnectionResetError):
            return               # the page navigated away; nothing to clean up

    def do_GET(self) -> None:
        if not _has_expected_host(self.headers, self.origin):
            return self._send({"error": "unexpected host"}, 403)
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        if url.path in ("/", "/index.html"):
            return self._send(PAGE.read_bytes(), ctype="text/html; charset=utf-8")
        if url.path.startswith("/static/"):
            return self._send_static(url.path.removeprefix("/static/"))
        if url.path == "/api/job/stream":
            return self._stream_job(query)
        if not url.path.startswith("/api/"):
            return self._send({"error": "not found"}, 404)

        conn = self._conn()
        try:
            self._send(self._api_get(url.path, query, conn))
        except Exception as exc:                       # a diagnostic page that 500s
            self._send({"error": f"{type(exc).__name__}: {exc}"}, 500)
        finally:
            conn.close()

    def _api_get(self, path: str, query: dict, conn: sqlite3.Connection):
        if path == "/api/overview":
            return web_memory.overview(conn, self.cfg, days=int(query.get("days", 14)))
        if path == "/api/items":
            return web_queue.items(conn,
                         stream=query.get("stream", ""),
                         verdict=query.get("verdict", ""),
                         reason=query.get("reason", ""),
                         q=query.get("q", ""),
                         days=int(query.get("days", 0) or 0),
                         group=query.get("group", ""),
                         queue=query.get("queue", ""),
                         limit=min(int(query.get("limit", 100)), 500),
                         offset=int(query.get("offset", 0)))
        if path == "/api/item":
            return web_queue.item_detail(conn, int(query["id"]))
        if path == "/api/groups":
            return web_queue.groups(conn,
                          stream=query.get("stream", ""),
                          verdict=query.get("verdict", ""),
                          reason=query.get("reason", ""),
                          q=query.get("q", ""),
                          days=int(query.get("days", 0) or 0),
                          queue=query.get("queue", ""),
                          limit=min(int(query.get("limit", 200)), 1000))
        if path == "/api/chats":
            return web_queue.conversations(conn, self.cfg, stream=query.get("stream", ""),
                                 q=query.get("q", ""))
        if path == "/api/senders":
            return {"senders": web_queue.senders(conn, q=query.get("q", ""),
                                       decision=query.get("decision", ""),
                                       limit=min(int(query.get("limit", 200)), 1000))}
        if path == "/api/events":
            # One facet per request, matching how it is asked: a pill for a person, a
            # place or a series. `exclude` drops the row the pill was clicked on.
            return web_memory.event_list(conn, self.cfg,
                              person=query.get("person", ""),
                              location=query.get("location", ""),
                              series=query.get("series", ""),
                              exclude=query.get("exclude", ""),
                              limit=min(int(query.get("limit", 200)), 500))
        if path == "/api/memory":
            return web_memory.memory(conn, self.cfg)
        if path == "/api/runs":
            return {"runs": web_memory.runs(conn, limit=int(query.get("limit", 30)))}
        if path == "/api/collections":
            # What each ingest pass brought in, and — the part that had no home before —
            # what it *skipped*. A skipped item never enters the spool, so the queue view
            # could only ever show what was waiting, never what the next dream will
            # ignore and why.
            return {"collections": archive.collections(
                conn, limit=int(query.get("limit", 20)))}
        if path == "/api/run":
            return web_memory.run_detail(conn, self.cfg, int(query["id"]))
        if path == "/api/dream_preview":
            # No ceiling of its own. This used to default to 500 *and* clamp to 500, so
            # the page showed a pass reading 500 lines while the run it was previewing
            # read `item_budget` of them — the preview's one job is to be what the run
            # will do. Dealt round-robin across 106 conversations, 500 also meant the
            # biggest conversation in the archive previewed as eight lines.
            return web_dream.dream_preview(conn, self.cfg,
                                 limit=int(query.get("limit", 0) or 0))
        if path == "/api/job":
            return web_jobs.job_status(query.get("id", ""), query.get("kind", ""))
        if path == "/api/why":
            return web_memory.why(conn, query.get("kind", ""), query.get("ref", ""), cfg=self.cfg)
        if path == "/api/conversation":
            # A cited line with no way to read what surrounded it answers "where did
            # this come from" with a fragment. `trace.conversation` already existed for
            # the agent's tool; the page had no route to it.
            return {"lines": trace.conversation(
                conn, stream=query.get("stream", ""), thread=query.get("thread", ""),
                around=query.get("around", ""))}
        if path == "/api/wiki_pages":
            return web_memory.wiki_pages(conn, self.cfg, q=query.get("q", ""))
        if path == "/api/wiki":
            profile = wiki.profile(conn, self.cfg.wiki_dir, query.get("slug", ""))
            return profile or {"error": "no such wiki page"}
        if path == "/api/trace":
            return web_memory.trace_call(conn, self.cfg, query.get("gen", ""))
        if path == "/api/generations":
            return {"calls": [
                {"gen": r["generation_id"], "stage": r["stage"], "label": r["label"],
                 "model": (r["model"] or "").split("/")[-1], "run": r["run_id"],
                 "at": str(r["created_at"])[:16], "prompt": r["prompt_tokens"],
                 "completion": r["completion_tokens"], "cost": round(r["cost_usd"], 4)}
                for r in trace.recent(conn, limit=int(query.get("limit", 40)),
                                      run_id=int(query["run"]) if query.get("run") else None)]}
        return {"error": "not found"}

    def do_POST(self) -> None:
        url = urlparse(self.path)
        if not _is_same_origin_mutation(
                self.headers, origin=self.origin, csrf_token=self.csrf_token):
            return self._send({"error": "cross-site requests are not allowed"}, 403)
        try:
            payload = _read_json_body(self.headers, self.rfile)
        except RequestBodyError as exc:
            return self._send({"error": str(exc)}, exc.status)

        conn = self._conn()
        try:
            if url.path == "/api/sender":
                out = web_queue.set_sender(conn, self.cfg, payload["address"], payload["decision"],
                                 backfill=bool(payload.get("backfill")),
                                 source=payload.get("source", "you"),
                                 reason=payload.get("reason"))
            elif url.path == "/api/queue":
                out = web_queue.queue_item(conn, self.cfg, int(payload["id"]), payload["action"])
            elif url.path == "/api/chat":
                out = threads.decide(conn, payload["stream"], payload["thread"],
                                     payload["decision"],
                                     reason=payload.get("reason") or "you",
                                     by=payload.get("by", "you"))
            # One verb for "I don't care about this", whichever kind of thing it is.
            # The caller has a calendar row or a bundle in front of it, not a schema.
            elif url.path == "/api/block":
                out = web_queue.block(conn, self.cfg, payload)
            # "/api/collect" is on every ad blocker's list — uBlock and EasyPrivacy both
            # match it as an analytics beacon — so the browser cancelled the request
            # before it left the page and the button did nothing but log
            # ERR_BLOCKED_BY_CLIENT. Nothing on our side was wrong, which is why it was
            # confusing. The old path is kept working for anything that still calls it.
            elif url.path in ("/api/gather", "/api/collect"):
                out = web_jobs.start_job("gather", web_jobs.collect_work, self.cfg)
            elif url.path == "/api/dream":
                # The one endpoint here that spends money, so it is never implicit —
                # it fires because someone pressed the button on the preview.
                out = web_jobs.start_job("dream", web_jobs.dream_work, self.cfg)
            else:
                return self._send({"error": "not found"}, 404)
            self._send(out)
        except Exception as exc:
            self._send({"error": f"{type(exc).__name__}: {exc}"}, 500)
        finally:
            conn.close()


class WebError(Exception):
    """The server could not start, with a way forward rather than an errno."""


def _who_has(port: int) -> str:
    """What is already listening, named rather than left as a mystery.

    "Address already in use" is a true sentence that leaves you with nothing to do. The
    port was held by an unrelated `wsserver.py` that had been running since the previous
    day, and from the message alone that is indistinguishable from memcal being broken —
    which is how it was reported.
    """
    try:
        found = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                               capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = [line.split() for line in (found.stdout or "").splitlines()[1:] if line]
    return f"{lines[0][0]} (pid {lines[0][1]})" if lines and len(lines[0]) > 1 else ""


def serve(cfg: Config, *, host: str = "127.0.0.1", port: int = 8765,
          open_browser: bool = True) -> None:
    if not _is_loopback_host(host):
        raise WebError(
            "memcal web has no authentication and only binds to loopback addresses. "
            "Use 127.0.0.1 or ::1 and an SSH tunnel for remote access."
        )
    origin = _origin_for(host, port)
    csrf_token = secrets.token_urlsafe(32)
    server_class = IPv6ThreadingHTTPServer if ":" in host else ThreadingHTTPServer
    try:
        httpd = server_class(
            (host, port),
            partial(Handler, cfg=cfg, origin=origin, csrf_token=csrf_token),
        )
    except OSError as exc:
        if exc.errno not in (errno.EADDRINUSE, errno.EACCES):
            raise
        holder = _who_has(port)
        raise WebError(
            f"port {port} is already taken{' by ' + holder if holder else ''}. "
            f"Run `memcal web --port {port + 1}`, or stop whatever is on {port}."
        ) from exc
    url = origin
    print(f"memcal web  {url}   (ctrl-c to stop)", flush=True)
    print(f"  db {cfg.db_path}", flush=True)
    if open_browser:
        threading.Timer(0.4, webbrowser.open, (url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
