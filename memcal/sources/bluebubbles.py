"""BlueBubbles source — iMessage ingestion via the BlueBubbles REST API.

Resolves group chats and participants, bypasses macOS chat.db file locking, and supports
remote execution. Requires the server password in configuration (`BLUEBUBBLES_PASSWORD`).
Default URL is http://localhost:1234.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone

from .. import db, identity, textclean, threads
from ..config import Config
from . import base
from .spec import Source, SourceError
from . import register

DEFAULT_URL = "http://localhost:1234"
PAGE_SIZE = 200

#: Hosts we can launch the app for. A server on another machine we cannot open.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})

#: How long to wait for a freshly-opened server to bind its port, and the gap between
#: pings while waiting. BlueBubbles takes a few seconds from launch to answering.
_WAKE_TIMEOUT = 20.0
_WAKE_POLL = 1.0


def _is_local(url: str) -> bool:
    return (urllib.parse.urlparse(url).hostname or "").lower() in _LOCAL_HOSTS


def is_local(cfg: Config, url: str) -> bool:
    """Whether the BlueBubbles server counts as running on this Mac — the only case
    where opening the app can help. `bluebubbles_location` decides: `local`/`remote`
    force the answer, `auto` (the default) reads it off the URL's host."""
    location = str(getattr(cfg, "bluebubbles_location", "auto") or "auto").lower()
    if location == "remote":
        return False
    if location == "local":
        return True
    return _is_local(url)


def _open_app() -> bool:
    """Open BlueBubbles.app hidden, in the background. True when the launch was accepted.

    `-g` keeps it from taking foreground and `-j` launches it hidden, so a nightly wake
    never throws a window (let alone a fullscreen one) in the user's face — the server
    binds its port and stays out of the way. macOS only, and only when the app is
    installed: `open` exits non-zero when the platform or the app is missing — exactly
    the "cannot" case, so a failure here is the signal to fall back to chat.db.
    """
    if sys.platform != "darwin":
        return False
    # Never throw a GUI app at someone mid-test-run. A test that exercises the real
    # dream/ingest pipeline against a store that has a BlueBubbles password would
    # otherwise launch the app on the developer's screen; the transport tests that
    # mean to check launching patch `_open_app` and never reach this.
    if "unittest" in sys.modules or "pytest" in sys.modules:
        return False
    opener = shutil.which("open")
    if not opener:
        return False
    try:
        proc = subprocess.run([opener, "-g", "-j", "-a", "BlueBubbles"],
                              capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _alive(client: "BlueBubbles") -> bool:
    """Whether the server answers, treating a refused connection as "not up" rather
    than an error — the server being down is the normal case we are here to fix."""
    try:
        return client.ping()
    except base.HttpError:
        return False


def wake_server(client: "BlueBubbles", cfg: Config, notes: list[str], *,
                timeout: float = _WAKE_TIMEOUT, poll: float = _WAKE_POLL,
                opener=None, sleep=None) -> bool:
    """Open the local BlueBubbles app and wait for it to answer, if we can.

    Returns True once a ping succeeds. Only tries a launch for a server this Mac is
    hosting (`bluebubbles_location`) whose app is installed; a remote or absent server
    is left untouched so the caller can fall back to chat.db. Appends progress to `notes`.
    """
    opener = opener or _open_app          # resolved here so patching the module works
    sleep = sleep or time.sleep
    if not is_local(cfg, client.url):
        return False
    if not opener():
        return False
    notes.append(f"BlueBubbles at {client.url} was down — opened the app")
    attempts = max(1, int(timeout / poll)) if poll > 0 else 1
    for attempt in range(attempts):
        if _alive(client):
            notes.append("BlueBubbles came up")
            return True
        if attempt < attempts - 1:
            sleep(poll)
    notes.append("BlueBubbles did not come up in time — falling back")
    return False


def ensure_server(cfg: Config, *, opener=None, sleep=None) -> list[str]:
    """Bring the local BlueBubbles server up before a pass reads iMessage, if we can.

    The launch belongs to the nightly/scheduled pass, not to the transport: `ingest()`
    never starts a GUI app, so a plain `memcal ingest`, a test, or any other caller has
    no surprise window thrown at it. Dream calls this once before pulling sources.

    A no-op unless every condition holds: the iMessage backend is BlueBubbles, autostart
    is on, a password is configured (so BlueBubbles is really in use), the server is on
    this Mac, the app is installed, and it is not already answering. Returns notes.
    """
    notes: list[str] = []
    if str(getattr(cfg, "imessage_backend", "bluebubbles")).lower() != "bluebubbles":
        return notes          # reading chat.db directly; nothing to wake
    if not getattr(cfg, "bluebubbles_autostart", True):
        return notes
    try:
        client = BlueBubbles(cfg)
    except base.HttpError:
        return notes          # no password: BlueBubbles is not in use, nothing to wake
    if _alive(client):
        return notes
    wake_server(client, cfg, notes, opener=opener, sleep=sleep)
    return notes


class BlueBubbles:
    def __init__(self, cfg: Config):
        self.password = cfg.secret("BLUEBUBBLES_PASSWORD", "bluebubbles", "bluebubblespassword")
        self.url = (cfg.secret("BLUEBUBBLES_URL", "bluebubblesurl") or DEFAULT_URL).rstrip("/")
        if not self.password:
            raise base.HttpError(
                "no BlueBubbles password. Add a line to memcal/.env — `bluebubbles=<password>` "
                "is enough — using the password from the BlueBubbles server app."
            )

    def _path(self, path: str, **params) -> str:
        params["password"] = self.password
        return f"{self.url}/api/v1/{path.lstrip('/')}?{urllib.parse.urlencode(params)}"

    def ping(self) -> bool:
        data = base.get_json(self._path("ping"))
        return bool(data) and data.get("status") == 200

    def server_info(self) -> dict:
        return (base.get_json(self._path("server/info")) or {}).get("data") or {}

    def messages(self, after_ms: int, limit: int = PAGE_SIZE, offset: int = 0) -> list[dict]:
        """Query messages in ascending chronological order for watermark tracking."""
        payload = {
            "limit": limit,
            "offset": offset,
            "with": ["chat", "chat.participants", "handle", "attachment"],
            "sort": "ASC",
            "after": int(after_ms),
        }
        data = base.post_json(self._path("message/query"), payload)
        return (data or {}).get("data") or []


def to_iso(ms) -> str:
    try:
        value = int(ms or 0)
    except (TypeError, ValueError):
        return db.now()
    if value <= 0:
        return db.now()
    if value > 10**12:  # milliseconds
        value = value / 1000
    return datetime.fromtimestamp(value, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def message_text(message: dict) -> str:
    text = textclean.spoken_text(message.get("text") or "")
    if text:
        return textclean.clean_message(text)
    attachments = message.get("attachments") or []
    if attachments:
        kinds = {(a.get("mimeType") or "file").split("/")[0] for a in attachments}
        return f"[sent {len(attachments)} {'/'.join(sorted(kinds))} attachment(s)]"
    return ""


def chat_of(message: dict) -> tuple[str, bool, list[str], str]:
    """Return a tuple of (thread_key, is_group, participant_handles, display_name).

    Differentiates thread identifiers from display names, allowing thread labels
    to be derived from participant rosters when display names are absent.
    """
    chats = message.get("chats") or []
    chat = chats[0] if chats else {}
    participants = [p.get("address") for p in (chat.get("participants") or []) if p.get("address")]
    name = " ".join((chat.get("displayName") or "").split())
    key = (name or chat.get("chatIdentifier")
           or (message.get("handle") or {}).get("address") or "unknown")
    is_group = len(participants) > 2 or bool(name)
    return key, is_group, participants, name


def ingest(conn: sqlite3.Connection, cfg: Config, *, limit: int = 1000) -> base.IngestReport:
    report = base.IngestReport.opened("imessage", cfg)
    try:
        client = BlueBubbles(cfg)
        if not client.ping():
            report.error = f"server at {client.url} did not answer ping"
            return report
    except base.HttpError as exc:
        # The transport never launches the app itself — starting BlueBubbles is the
        # dream pass's job (`ensure_server`), so a plain ingest just falls back here.
        report.error = str(exc)
        return report

    after = int(base.watermark(conn, "bluebubbles.after", "0") or 0)
    if after == 0:
        # Initial run: default watermark to 30 days prior.
        after = int((db.today().toordinal() - 30 - datetime(1970, 1, 1).date().toordinal())
                    * 86400 * 1000)
        report.notes.append("first run — starting 30 days back")

    tier = identity.top_tier(conn)
    newest = after
    fetched = 0
    offset = 0
    while fetched < limit:
        try:
            page = client.messages(after, limit=min(PAGE_SIZE, limit - fetched), offset=offset)
        except base.HttpError as exc:
            report.error = str(exc)
            break
        if not page:
            break
        for message in page:
            fetched += 1
            text = message_text(message)
            if not text:
                continue
            thread, is_group, participants, display = chat_of(message)
            threads.record(conn, "imessage", thread, label=display or None,
                           participants=participants or None, is_group=is_group)
            # `handle` is present but null on outgoing messages; guard against explicit None values.
            handle_obj = message.get("handle") or {}
            handle = handle_obj.get("address")
            from_me = bool(message.get("isFromMe"))
            created = message.get("dateCreated") or message.get("dateDelivered")
            newest = max(newest, int(created or 0))
            base.deliver(
                conn, report,
                channel="imessage",
                external_id=message.get("guid") or f"bb:{message.get('originalROWID')}",
                ts=to_iso(created),
                text=text,
                thread=thread,
                handle=None if from_me else handle,
                from_me=from_me,
                is_group=is_group,
                top_tier=tier,
                counterpart=None if is_group else (handle or thread),
                meta={"source": "bluebubbles", "group": is_group,
                      "service": handle_obj.get("service")},
            )
        if len(page) < PAGE_SIZE:
            break
        offset += len(page)
    else:
        # Reached fetch limit before source exhaustion; mark more records available.
        report.more = True

    conn.commit()
    if newest > after:
        base.set_watermark(conn, "bluebubbles.after", newest + 1)
    return report


@register
class BlueBubblesSource(Source):
    name = "bluebubbles"
    description = "iMessage via the BlueBubbles server (groups, participants, attachments)"
    secrets = ("BLUEBUBBLES_PASSWORD",)
    in_all = False          # The imessage source runs bluebubbles first and falls back to chat.db.
    order = 10

    def fetch(self, conn, cfg, report, limit):
        result = ingest(conn, cfg, limit=limit)
        _absorb(report, result)

    def check(self, cfg):
        try:
            client = BlueBubbles(cfg)
        except base.HttpError as exc:
            return False, str(exc).split(".")[0]
        try:
            return (True, f"server up at {client.url}") if client.ping() else (False, "no ping")
        except base.HttpError as exc:
            # Return full exception string without truncation to preserve diagnostic detail.
            return False, str(exc)


def _absorb(report, other) -> None:
    """Merge result report into the caller report."""
    report.absorb(other)

