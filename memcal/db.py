"""SQLite connection and schema management."""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta, timezone, time as dt_time
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")


#: Allow concurrent source collectors to wait for SQLite's single writer.
BUSY_TIMEOUT_MS = 30_000


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Keep the web UI readable during ingestion.
    with contextlib.suppress(sqlite3.Error):        # a read-only or network FS may refuse
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


# Existing databases need explicit ALTERs; schema.sql only creates new tables.
ADDED_COLUMNS = (
    ("events", "until", "TEXT"),
    # Reminder delivery is separate from the obligation's due date.
    ("todos", "remind_at", "TEXT"),
    ("todos", "reminded_at", "TEXT"),
    ("todos", "reminder_uid", "TEXT"),
    ("todos", "event_id", "INTEGER REFERENCES events(id) ON DELETE SET NULL"),
    ("threads", "platform_muted", "INTEGER NOT NULL DEFAULT 0"),
    ("threads", "platform_note", "TEXT"),
    # Store the actual ceiling so saturation remains measurable after formula changes.
    ("generations", "max_tokens", "INTEGER NOT NULL DEFAULT 0"),
    # Who made a sender or thread policy decision.
    ("senders", "source", "TEXT NOT NULL DEFAULT 'auto'"),
    ("threads", "decided_by", "TEXT NOT NULL DEFAULT 'auto'"),
    # `origin` is immutable; `source` may change on later writes.
    ("events", "origin", "TEXT"),
    ("todos", "origin", "TEXT"),
    # Queue views group lines by ingest pass.
    ("archive", "collection_id", "INTEGER"),
    # Addressee distinguishes conversation from instructions sent to an agent.
    ("archive", "addressed_to", "TEXT NOT NULL DEFAULT 'person'"),
    # What Calendar.app said about an event last time it was read, so an unchanged one
    # costs nothing, and whether memcal is the thing that put it there.
    ("calendar_items", "revision", "TEXT"),
    ("calendar_items", "published", "INTEGER NOT NULL DEFAULT 0"),
    ("calendar_items", "published_state", "TEXT"),
    # The row this one happens inside, and where you reply to an invitation. Both
    # nullable, both added after the first release, so both have to be here or every
    # store built before today reads as corrupt.
    ("events", "part_of", "INTEGER REFERENCES events(id) ON DELETE SET NULL"),
    ("events", "rsvp_url", "TEXT"),
    ("events", "join_url", "TEXT"),
    # The scheduled day an occurrence stands in for, so one week moved to Wednesday is
    # an exception to a Tuesday rule rather than evidence the rule is now Wednesday.
    ("events", "instead_of", "TEXT"),
    # Nullable metrics distinguish old, unmeasured runs from measured zeroes.
    ("runs", "requests", "INTEGER"),
    ("runs", "failed_calls", "INTEGER"),
    ("runs", "wait_seconds", "REAL"),
    ("generations", "requests", "INTEGER"),
    # Lets questions expire with an explicitly named day even without an event link.
    ("questions", "about_date", "TEXT"),
    # Question review is optimistic: a model disposition applies only to the exact
    # version it saw, and a deferred question is not ordinary stale prompt litter.
    ("questions", "wake_condition", "TEXT"),
    ("questions", "updated_at", "TEXT"),
    # Expected old wiki bytes for conflict-safe outbox recovery. NULL means the target
    # did not exist when the snapshot was staged.
    ("wiki_pending_writes", "expected_hash", "TEXT"),
)


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    added = []
    for table, column, decl in ADDED_COLUMNS:
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            added.append((table, column))
    # Old rows have no better origin record than their current source.
    for table, column in added:
        if column == "origin":
            conn.execute(f"UPDATE {table} SET origin = source WHERE origin IS NULL")
        elif table == "questions" and column == "updated_at":
            conn.execute("UPDATE questions SET updated_at = created_at"
                         " WHERE updated_at IS NULL")
    _drop_empty_legacy_tables(conn)
    _resync_archive_fts(conn)
    conn.commit()


#: Bumped when something makes the existing full-text index wrong. `1` is the arrival of
#: the `archive_au` trigger: every `UPDATE archive SET person` before it left the index
#: holding the old tokens.
FTS_GENERATION = "1"


def _resync_archive_fts(conn: sqlite3.Connection) -> bool:
    """Rebuild the archive index once after an index-affecting schema change."""
    if get_meta(conn, "archive_fts.generation", "") == FTS_GENERATION:
        return False
    conn.execute("INSERT INTO archive_fts(archive_fts) VALUES('rebuild')")
    set_meta(conn, "archive_fts.generation", FTS_GENERATION)
    return True


#: Tables from removed features.
LEGACY_TABLES = ("visits", "places", "location_samples")


def _drop_empty_legacy_tables(conn: sqlite3.Connection) -> None:
    """Drop legacy tables only when they contain no user data."""
    for table in LEGACY_TABLES:
        try:
            row = conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()
        except sqlite3.OperationalError:
            continue                       # never existed here; nothing to do
        if row and row["n"] == 0:
            conn.execute(f"DROP TABLE {table}")


def open_db(db_path: Path) -> sqlite3.Connection:
    conn = connect(db_path)
    migrate(conn)
    return conn


# -------------------------------------------------------------------- clock --
#
# Centralize the clock because dates control write precedence, matching, spool expiry,
# and brief windows. Tests and benchmarks pin it with `MEMCAL_TODAY` or `set_today()`;
# unset means the real clock.

_FAKE_TODAY: date | None = None

#: The time of day, when the pin carried one. Separate from `_FAKE_TODAY` because
#: pinning a date and pinning a moment are different requests and most callers only
#: want the first — see `now_dt`.
_FAKE_CLOCK: dt_time | None = None


def _split_pin(value: str | date | datetime) -> tuple[date, dt_time | None]:
    """Split a test clock pin into its day and optional time."""
    if isinstance(value, datetime):
        return value.date(), value.timetz() if value.tzinfo else value.time()
    if isinstance(value, date):
        return value, None
    text = str(value).strip()
    day = parse_date(text)
    rest = text[10:].lstrip("T").strip()
    if not rest:
        return day, None
    try:
        return day, dt_time.fromisoformat(rest)
    except ValueError:
        return day, None


def set_today(value: str | date | datetime | None) -> None:
    """Pin the clock, or pass None to hand it back to the real one.

    Accepts a bare day or a day and a time; see `_split_pin`.
    """
    global _FAKE_TODAY, _FAKE_CLOCK
    if not value:
        _FAKE_TODAY, _FAKE_CLOCK = None, None
        return
    _FAKE_TODAY, _FAKE_CLOCK = _split_pin(value)


def _env_pin() -> tuple[date | None, dt_time | None]:
    raw = os.environ.get("MEMCAL_TODAY")
    if not raw:
        return None, None
    try:
        return _split_pin(raw)
    except ValueError:
        return None, None


def _env_today() -> date | None:
    return _env_pin()[0]


def today() -> date:
    return _FAKE_TODAY or _env_today() or date.today()


# ------------------------------------------------------------------ helpers --

def now() -> str:
    return now_dt().isoformat(timespec="seconds")


def now_dt() -> datetime:
    """Return now, honoring the optional test day and time pins."""
    stamp = datetime.now().astimezone()
    day = _FAKE_TODAY or _env_today()
    clock = _FAKE_CLOCK if _FAKE_TODAY else _env_pin()[1]
    if not day:
        return stamp
    stamp = stamp.replace(year=day.year, month=day.month, day=day.day)
    if clock is None:
        return stamp
    return stamp.replace(hour=clock.hour, minute=clock.minute,
                         second=clock.second, microsecond=0)


def parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def parse_when(phrase: str, *, ref: date | None = None) -> tuple[date, int]:
    """Resolve a simple human date phrase to ``(start, day_count)``."""
    ref = ref or today()
    text = " ".join((phrase or "").split()).lower().strip()
    if not text:
        return ref, 1
    try:
        return parse_date(text), 1
    except ValueError:
        pass
    if text in ("today", "tonight"):
        return ref, 1
    if text == "tomorrow":
        return ref + timedelta(days=1), 1
    if text == "yesterday":
        return ref - timedelta(days=1), 1
    if "weekend" in text:
        ahead = (5 - ref.weekday()) % 7
        if "next" in text and ahead < 7 - ref.weekday():
            ahead += 7
        return ref + timedelta(days=ahead), 2
    if text in ("this week", "week"):
        return ref, 7
    if text == "next week":
        return ref + timedelta(days=(7 - ref.weekday()) % 7 or 7), 7
    for index, name in enumerate(WEEKDAYS):
        if name in text or name[:3] == text[:3]:
            ahead = (index - ref.weekday()) % 7
            if "next" in text and ahead < 7 - ref.weekday():
                ahead += 7          # still inside this week, so "next" means the one after
            return ref + timedelta(days=ahead), 1
    return ref, 1


def parse_ts(value: str) -> datetime:
    """Parse a stored timestamp. Naive values are assumed local, so comparisons
    between fixture data and real ingest never raise."""
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        stamp = now_dt()
    if stamp.tzinfo is None:
        stamp = stamp.astimezone()
    return stamp


def utc_stamp(value: str | date | datetime) -> str:
    """Return an instant as UTC milliseconds, matching JavaScript ``toISOString``."""
    if isinstance(value, datetime):
        stamp = value if value.tzinfo else value.astimezone()
    elif isinstance(value, date):
        stamp = datetime.combine(value, dt_time(0, 0)).astimezone()
    else:
        text = str(value).strip()
        if not text:
            return ""
        try:
            stamp = datetime.fromisoformat(text)
        except ValueError:
            # Deliberately not `parse_ts`, which answers `now()` here so that a
            # comparison never raises. That is the right trade for reading a timestamp
            # and the wrong one for writing an instant: it would invent a moment nothing
            # observed, and since `ical._identity` hashes this very string, a value that
            # did not round-trip would quietly re-key its own row. Hand back what
            # arrived and let it stay visibly wrong.
            return text
        if stamp.tzinfo is None:
            stamp = stamp.astimezone()
    return (stamp.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"))


def days_between(a: str | date, b: str | date) -> int:
    return (parse_date(a) - parse_date(b)).days


def age_phrase(iso_ts: str, ref: datetime | None = None) -> str:
    """'6 weeks', '5 days', 'today' — the age rendered next to a to-do."""
    ref = ref or now_dt()
    try:
        then = datetime.fromisoformat(iso_ts)
    except ValueError:
        return "?"
    if then.tzinfo is None:
        then = then.astimezone()
    days = (ref - then).days
    if days <= 0:
        return "today"
    if days == 1:
        return "1 day"
    if days < 14:
        return f"{days} days"
    if days < 60:
        return f"{days // 7} weeks"
    return f"{days // 30} months"


def slugify(text: str, max_len: int = 48) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (text or "untitled")[:max_len].strip("-")


def jload(raw: str | None, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def jdump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def window_bounds(days_back: int, days_forward: int, ref: date | None = None) -> tuple[str, str]:
    ref = ref or today()
    return (
        (ref - timedelta(days=days_back)).isoformat(),
        (ref + timedelta(days=days_forward)).isoformat(),
    )
