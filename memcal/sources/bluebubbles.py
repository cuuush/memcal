"""BlueBubbles source — iMessage ingestion via the BlueBubbles REST API.

Resolves group chats and participants, bypasses macOS chat.db file locking, and supports
remote execution. Requires the server password in configuration (`BLUEBUBBLES_PASSWORD`).
Default URL is http://localhost:1234.
"""

from __future__ import annotations

import sqlite3
import urllib.parse
from datetime import datetime, timezone

from .. import db, identity, textclean, threads
from ..config import Config
from . import base
from .spec import Source, SourceError
from . import register

DEFAULT_URL = "http://localhost:1234"
PAGE_SIZE = 200


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
    text = (message.get("text") or "").replace("￼", "").strip()
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
                stream="imessage",
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

