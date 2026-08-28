"""iMessage ingestion — reads ~/Library/Messages/chat.db read-only.

Requires Full Disk Access for whatever runs it; that failure is reported clearly
rather than raised as a stack trace. Handles resolve by dict lookup at ingest,
before anything expensive runs.
"""

from __future__ import annotations

import os
import plistlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import archive, db, gate, identity, threads
from . import base
from .spec import Source, SourceError
from . import register

CHAT_DB = Path("~/Library/Messages/chat.db").expanduser()
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

QUERY = """
SELECT m.ROWID              AS rowid,
       m.guid               AS guid,
       m.date               AS date,
       m.text               AS text,
       m.attributedBody     AS attributed,
       m.is_from_me         AS from_me,
       h.id                 AS handle,
       c.chat_identifier    AS chat,
       c.display_name       AS chat_name,
       (SELECT count(*) FROM chat_handle_join chj WHERE chj.chat_id = c.ROWID) AS members
FROM message m
LEFT JOIN handle h ON h.ROWID = m.handle_id
LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
LEFT JOIN chat c ON c.ROWID = cmj.chat_id
WHERE m.ROWID > ? AND m.date >= ?
ORDER BY m.ROWID
LIMIT ?
"""


@dataclass
class IngestReport:
    read: int = 0
    archived: int = 0
    passed: int = 0
    unresolved: int = 0
    last_rowid: int = 0
    floor: str | None = None
    error: str | None = None

    def summary(self) -> str:
        if self.error:
            return f"imessage: {self.error}"
        return (f"imessage: read {self.read}, archived {self.archived}, "
                f"gate passed {self.passed}, unresolved handles {self.unresolved}")


def apple_time(value) -> str:
    """chat.db stores nanoseconds since 2001 on modern macOS, seconds on old ones."""
    try:
        raw = int(value or 0)
    except (TypeError, ValueError):
        return db.now()
    seconds = raw / 1_000_000_000 if raw > 10**11 else raw
    return (APPLE_EPOCH + timedelta(seconds=seconds)).astimezone().isoformat(timespec="seconds")


def apple_ns(iso_ts: str) -> int:
    """An ISO timestamp as the nanoseconds-since-2001 `message.date` holds."""
    return int((db.parse_ts(iso_ts) - APPLE_EPOCH).total_seconds() * 1_000_000_000)


def resume_floor(conn: sqlite3.Connection) -> str | None:
    """Return the timestamp of the newest archived iMessage, regardless of which transport delivered it."""

    row = conn.execute(
        "SELECT max(ts) FROM archive WHERE stream = 'imessage'").fetchone()
    return (row[0] if row else None) or None


#: The body of an `attributedBody` sits immediately after the `NSString` class
#: declaration: the class name, a version byte, then `\x84\x01+` and the contents.
#: The version byte varies (`\x94` for a plain `NSString`, `\x95` when the object is
#: an `NSMutableString` that declares `NSString` as its superclass) and matching it
#: exactly is half of why the old scraper missed.
STRING_MARKER = re.compile(rb"NSString\x01.{0,2}\x84\x01\+", re.DOTALL)

#: An attachment's placeholder. A message that is only this has no text, and saying so
#: lets `deliver` drop it the way it drops any empty line.
OBJECT_REPLACEMENT = "\ufffc"


def _typedstream_length(blob: bytes, start: int) -> tuple[int, int]:
    """Read typedstream's variable-length byte count. Returns (length, next offset).

    A byte below 0x81 is the count itself; 0x81 introduces a two-byte little-endian
    count and 0x82 a four-byte one. Measured in **bytes, not characters** — an
    apostrophe costs three of them.
    """
    if start >= len(blob):
        return 0, start
    first = blob[start]
    if first < 0x81:
        return first, start + 1
    width = 2 if first == 0x81 else 4
    end = start + 1 + width
    if end > len(blob):
        return 0, len(blob)
    return int.from_bytes(blob[start + 1:end], "little"), end


def decode_attributed(blob) -> str:
    """Newer messages leave `text` NULL and put the body in a typedstream blob."""
    if not blob:
        return ""
    if isinstance(blob, str):
        return blob
    try:
        parsed = plistlib.loads(blob, fmt=plistlib.FMT_BINARY)
        if isinstance(parsed, dict):
            for key in ("NSString", "string"):
                if key in parsed:
                    return str(parsed[key])
    except Exception:
        pass

    match = STRING_MARKER.search(blob)
    if not match:
        return ""
    length, start = _typedstream_length(blob, match.end())
    if length <= 0:
        return ""
    body = blob[start:start + length].decode("utf-8", "replace")
    # An attachment-only message is one placeholder character and no words.
    return body.replace(OBJECT_REPLACEMENT, " ").strip()


#: enumerated.
DECODER_GENERATION = "2"
DECODER_KEY = "imessage.decoder_generation"


def repair_decoded_text(conn: sqlite3.Connection, src: sqlite3.Connection) -> int:
    """Re-derive archived bodies whenever the decoder changes."""
    if db.get_meta(conn, DECODER_KEY, "") == DECODER_GENERATION:
        return 0
    rows = conn.execute(
        "SELECT id, external_id, text FROM archive WHERE stream = 'imessage'").fetchall()
    fixed = 0
    for row in rows:
        try:
            found = src.execute(
                "SELECT attributedBody FROM message WHERE guid = ? AND text IS NULL",
                (row["external_id"],)).fetchone()
        except sqlite3.Error:
            return fixed          # a schema we cannot read; leave the marker unset
        if not found or not found["attributedBody"]:
            continue
        body = decode_attributed(found["attributedBody"]).replace("\ufffc", "").strip()
        if body == (row["text"] or ""):
            continue
        if body:
            conn.execute("UPDATE archive SET text = ? WHERE id = ?", (body, row["id"]))
        else:
            conn.execute(
                "UPDATE archive SET text = '', gated = 0, gate_reason = 'attachment-only'"
                "  WHERE id = ?", (row["id"],))
            conn.execute("DELETE FROM spool WHERE archive_id = ?", (row["id"],))
        fixed += 1
    db.set_meta(conn, DECODER_KEY, DECODER_GENERATION)
    conn.commit()
    return fixed


def ingest(conn: sqlite3.Connection, *, limit: int = 2000, db_path: Path | None = None,
           since_rowid: int | None = None, backfill: bool = False) -> IngestReport:
    """Read new lines out of chat.db.

    Incrementally by default: the read is floored at the newest line the stream already
    has, so this resumes where the *stream* is rather than where this reader last was.
    `backfill=True` drops that floor to walk history forward from `imessage.rowid`, which
    is the only way to fill a gap older than the floor — it keeps its own watermark so a
    backfill in progress never drags the live position backwards.
    """
    report = IngestReport()
    path = db_path or CHAT_DB
    if not path.exists():
        report.error = f"{path} not found"
        return report

    key = "imessage.backfill_rowid" if backfill else "imessage.rowid"
    watermark = since_rowid
    if watermark is None:
        watermark = int(db.get_meta(conn, key, "0") or 0)
    floor = resume_floor(conn) if not backfill else None
    report.floor = floor
    # 0 is "since the Apple epoch", i.e. no bound at all — a cold start, where walking
    # from the beginning is the correct behaviour and the spool horizon below is what
    # stops a first import from charging for years of texts.
    floor_ns = apple_ns(floor) if floor else 0

    try:
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
        mended = repair_decoded_text(conn, src)
        if mended:
            report.notes.append(
                f"re-decoded {mended} line(s) the old body scraper had mangled")
        rows = src.execute(QUERY, (watermark, floor_ns, limit)).fetchall()
    except sqlite3.OperationalError as exc:
        report.error = (f"cannot read {path} ({exc}). Grant Full Disk Access to your terminal "
                        f"in System Settings → Privacy & Security.")
        return report

    tier = identity.top_tier(conn)
    for row in rows:
        report.read += 1
        report.last_rowid = max(report.last_rowid, int(row["rowid"]))
        text = (row["text"] or "").strip() or decode_attributed(row["attributed"])
        text = text.replace("￼", "").strip()
        if not text:
            continue

        handle = identity.normalize(row["handle"] or "")
        person = identity.resolve(conn, handle) if handle else None
        from_me = bool(row["from_me"])
        if handle and not person and not from_me:
            identity.note_unresolved(conn, handle, "imessage", None, text)
            report.unresolved += 1

        is_group = (row["members"] or 0) > 2
        thread = row["chat_name"] or row["chat"] or handle or "unknown"
        threads.record(conn, "imessage", thread, label=row["chat_name"], is_group=is_group)
        if handle and not from_me:
            threads.record_members(conn, "imessage", thread, [(handle, None)])
        ts = apple_time(row["date"])
        verdict = gate.gate_message(text, person=person, from_me=from_me, top_tier=tier,
                                    stream="imessage", is_group=is_group)
        # In a group chat, a line only matters if the user is in the conversation at all.
        archive_id = archive.append(
            conn,
            stream="imessage",
            external_id=row["guid"] or f"rowid:{row['rowid']}",
            ts=ts,
            text=text,
            thread=thread,
            handle=handle or None,
            person=person,
            from_me=from_me,
            meta={"group": is_group, "rowid": int(row["rowid"])},
            gated=bool(verdict),
            gate_reason=verdict.reason,
        )
        if archive_id is None:
            continue
        report.archived += 1
        if verdict and threads.is_muted(conn, "imessage", thread):
            continue
        if verdict and archive.within_horizon(ts):
            # The horizon belongs here too. Without it this path spooled whatever
            # chat.db handed back — a first import walks from rowid 0, so a fresh
            # install queued years of texts and charged for them on the next pass.
            entity = gate.entity_for(person=person, thread=thread, stream="imessage",
                                     is_group=is_group)
            archive.spool_add(conn, archive_id, entity)
            report.passed += 1

    conn.commit()
    src.close()
    if report.last_rowid:
        db.set_meta(conn, key, str(report.last_rowid))
    return report


def available() -> bool:
    return CHAT_DB.exists() and os.access(CHAT_DB, os.R_OK)


@register
class IMessageSource(Source):
    """Prefers the BlueBubbles server; falls back to reading chat.db directly."""

    name = "imessage"
    description = "iMessage (BlueBubbles if running, else the local chat.db)"
    order = 20

    def fetch(self, conn, cfg, report, limit):
        from .bluebubbles import _absorb
        from .bluebubbles import ingest as bb_ingest
        try:
            result = bb_ingest(conn, cfg, limit=limit)
            if not result.error:
                _absorb(report, result)
                return
            reason = result.error
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
        local = ingest(conn, limit=limit)
        if local.error:
            raise SourceError(f"{local.error} (bluebubbles also unavailable: {reason[:80]})")
        report.read += local.read
        report.archived += local.archived
        report.passed += local.passed
        report.notes.append(f"read chat.db directly — bluebubbles unavailable: {reason[:80]}")

    def check(self, cfg):
        from .bluebubbles import BlueBubbles
        try:
            if BlueBubbles(cfg).ping():
                return True, "via bluebubbles"
        except Exception:
            pass
        return (True, "via local chat.db") if available() else (False, "no chat.db access")
