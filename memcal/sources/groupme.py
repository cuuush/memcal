"""GroupMe source — ingestion via GroupMe API v3 personal access tokens.

Ingests group threads and direct messages. Gating evaluates temporal tokens and content
relevance across all active conversations regardless of user participation frequency.
"""

from __future__ import annotations

import random
import sqlite3
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from .. import db, identity, textclean, threads
from ..config import Config
from . import base
from .spec import Source, SourceError
from . import register

API = "https://api.groupme.com/v3"
PAGE = 100

# Concurrency limit for parallel fetch requests.
FETCH_WORKERS = 4

# Shared exponential jitter retry parameters for HTTP 420/429 responses.
RATE_LIMIT_RETRIES = 4
RATE_LIMIT_BASE_SECONDS = 1.0
RATE_LIMIT_MAX_SECONDS = 60.0
RATE_LIMIT_MAX_RETRY_AFTER = 300.0

# Maximum age in days for unwatermarked groups to be included during initial synchronization.
INITIAL_GROUP_DAYS = 30

#: Ingestion phases and relative progress weights for the phased progress bar.
INGEST_PHASES = (("connecting", 5), ("reading groups", 50),
                 ("rosters", 10), ("reading DMs", 35))


class GroupMe:
    def __init__(self, cfg: Config):
        self.token = cfg.secret("GROUPME_ACCESS_TOKEN", "groupme", "groupmetoken")
        if not self.token:
            raise base.HttpError(
                "no GroupMe token. Add `groupme=<token>` to memcal/.env — get one at "
                "https://dev.groupme.com/ → Access Token."
            )
        self._rate_lock = threading.Lock()
        self._rate_until = 0.0
        self._retry_lock = threading.Lock()

    def _get(self, path: str, **params):
        params["token"] = self.token
        url = f"{API}/{path.lstrip('/')}?{urllib.parse.urlencode(params)}"
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                if attempt:
                    # Serialize retry attempts after a rate limit cooldown to prevent worker stampedes.
                    with self._retry_lock:
                        self._wait_for_rate_limit()
                        data = base.get_json(url)
                else:
                    self._wait_for_rate_limit()
                    data = base.get_json(url)
                return (data or {}).get("response")
            except base.HttpError as exc:
                if not _is_rate_limit(exc) or attempt >= RATE_LIMIT_RETRIES:
                    raise
                ceiling = min(
                    RATE_LIMIT_MAX_SECONDS,
                    RATE_LIMIT_BASE_SECONDS * (2 ** attempt),
                )
                # Equal jitter decorrelates retry timing across concurrent workers.
                delay = max(
                    _retry_after(exc),
                    ceiling / 2 + random.uniform(0.0, ceiling / 2),
                )
                self._extend_rate_limit(delay)
        raise AssertionError("unreachable")

    def _wait_for_rate_limit(self) -> None:
        """Wait until the latest cooldown observed by any worker has elapsed."""
        while True:
            with self._rate_lock:
                remaining = self._rate_until - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(remaining)

    def _extend_rate_limit(self, seconds: float) -> None:
        until = time.monotonic() + max(0.0, seconds)
        with self._rate_lock:
            self._rate_until = max(self._rate_until, until)

    def me(self) -> dict:
        return self._get("users/me") or {}

    def groups(self) -> list[dict]:
        out: list[dict] = []
        page = 1
        while True:
            batch = self._get("groups", page=page, per_page=PAGE, omit="memberships") or []
            out.extend(batch)
            if len(batch) < PAGE:
                break
            page += 1
        return out

    def group(self, group_id: str) -> dict:
        """Fetch group details including member roster and display names."""
        return self._get(f"groups/{group_id}") or {}

    def group_messages(self, group_id: str, since_id: str | None, limit: int = PAGE) -> list[dict]:
        params = {"limit": min(limit, PAGE)}
        if since_id:
            params["since_id"] = since_id
        try:
            payload = self._get(f"groups/{group_id}/messages", **params)
        except base.HttpError as exc:
            if "HTTP 304" in str(exc) or "HTTP 404" in str(exc):
                return []
            raise
        return (payload or {}).get("messages") or []

    def chats(self) -> list[dict]:
        return self._get("chats", page=1, per_page=PAGE) or []

    def direct_messages(self, other_user_id: str, since_id: str | None) -> list[dict]:
        params = {"other_user_id": other_user_id}
        if since_id:
            params["since_id"] = since_id
        try:
            payload = self._get("direct_messages", **params)
        except base.HttpError as exc:
            if "HTTP 304" in str(exc):
                return []
            raise
        return (payload or {}).get("direct_messages") or []


def _is_rate_limit(exc: base.HttpError) -> bool:
    cause = exc.__cause__
    code = getattr(cause, "code", None)
    return code in (420, 429) or any(f"HTTP {value}" in str(exc) for value in (420, 429))


def _retry_after(exc: base.HttpError) -> float:
    """Extract cooldown duration in seconds from standard Retry-After headers."""
    cause = exc.__cause__
    headers = getattr(cause, "headers", None)
    raw = headers.get("Retry-After") if headers else None
    if not raw:
        return 0.0
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        try:
            requested = parsedate_to_datetime(str(raw))
            if requested.tzinfo is None:
                requested = requested.replace(tzinfo=timezone.utc)
            seconds = requested.timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            return 0.0
    return min(RATE_LIMIT_MAX_RETRY_AFTER, max(0.0, seconds))


def to_iso(epoch_seconds) -> str:
    try:
        value = int(epoch_seconds or 0)
    except (TypeError, ValueError):
        return db.now()
    if value <= 0:
        return db.now()
    return datetime.fromtimestamp(value, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


def last_message_id(container: dict, field: str = "messages") -> str | None:
    """Extract the newest message ID reported in group or chat listing payloads.

    Avoids individual conversation message queries when the watermark matches the newest ID.
    """
    summary = container.get(field)
    if not isinstance(summary, dict):
        return None
    value = summary.get("last_message_id") if field == "messages" else summary.get("id")
    return str(value) if value else None


def _last_activity(group: dict) -> int:
    """Extract latest message creation timestamp for ordering ingestion priority."""
    summary = group.get("messages") if isinstance(group.get("messages"), dict) else {}
    for value in (summary.get("last_message_created_at"), group.get("updated_at")):
        try:
            if value:
                return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def _active_for_initial_load(group: dict) -> bool:
    """Return True if an unwatermarked group has activity within INITIAL_GROUP_DAYS."""
    cutoff = int((db.now_dt() - timedelta(days=INITIAL_GROUP_DAYS)).timestamp())
    return _last_activity(group) >= cutoff


def _chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _muted(muted_until) -> bool:
    """Return True if the GroupMe mute expiration timestamp is in the future.

    Recorded as platform metadata; mute enforcement occurs according to configuration policy.
    """
    try:
        return float(muted_until or 0) > datetime.now(tz=timezone.utc).timestamp()
    except (TypeError, ValueError):
        return False


def group_members(group: dict) -> list[tuple[str, str | None]]:
    """Return a list of (handle, nickname) tuples for group members."""
    out = []
    for member in group.get("members") or []:
        user_id = str(member.get("user_id") or member.get("id") or "")
        if user_id:
            out.append((f"groupme:{user_id}",
                        member.get("nickname") or member.get("name")))
    return out


def _included_group(conn: sqlite3.Connection, thread: str) -> bool:
    """Return True if the thread contains gated archive rows and is not muted in threads table."""
    if threads.is_muted(conn, "groupme", thread):
        return False
    return conn.execute(
        """SELECT 1 FROM archive
            WHERE stream = 'groupme' AND thread = ? AND gated = 1 LIMIT 1""",
        (thread,),
    ).fetchone() is not None


def _profiles_are_current(conn: sqlite3.Connection, group: dict, thread: str) -> bool:
    group_id = str(group.get("id") or "")
    if not group_id:
        return True
    row = conn.execute(
        """SELECT fetched_at FROM groupme_group_profile_sync
            WHERE group_id = ?""",
        (group_id,),
    ).fetchone()
    if not row:
        return False
    # Refresh roster only when new handles appear in gated traffic after the last snapshot timestamp.
    unknown = conn.execute(
        """SELECT 1 FROM archive a
           LEFT JOIN groupme_profiles p
             ON p.user_id = substr(a.handle, length('groupme:') + 1)
           WHERE a.stream = 'groupme' AND a.thread = ? AND a.gated = 1
             AND a.from_me = 0 AND a.ts > ? AND a.handle LIKE 'groupme:%'
             AND p.user_id IS NULL
           LIMIT 1""",
        (thread, row["fetched_at"]),
    ).fetchone()
    return unknown is None


def _cache_group_profiles(conn: sqlite3.Connection, summary: dict, detail: dict,
                          *, my_id: str) -> int:
    """Cache group member profiles and update derived person identities on archived messages."""
    group_id = str(summary.get("id") or detail.get("id") or "")
    thread = detail.get("name") or summary.get("name") or group_id
    stamp = db.now()
    members = detail.get("members") or []
    roster = group_members(detail)
    if roster:
        threads.record_members(conn, "groupme", thread, roster, review_unknown=False)

    cached = 0
    for member in members:
        user_id = str(member.get("user_id") or "")
        account_name = " ".join(str(member.get("name") or "").split())
        if not user_id or not account_name:
            continue
        conn.execute(
            """INSERT INTO groupme_profiles(user_id, name, updated_at)
               VALUES(?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET
                 name = excluded.name, updated_at = excluded.updated_at""",
            (user_id, account_name, stamp),
        )
        cached += 1
        if user_id == my_id:
            continue

        handle = identity.normalize(f"groupme:{user_id}")
        # Resolve canonical person via link_by_name or adopt_seen_name to preserve existing mappings.
        canonical = (identity.link_by_name(conn, handle, account_name, source="groupme")
                     or identity.adopt_seen_name(conn, handle, account_name,
                                                 source="groupme:profile"))
        if not canonical:
            continue
        # Update derived person column on archived records without altering immutable message fields.
        conn.execute(
            """UPDATE archive SET person = ?
                WHERE stream = 'groupme' AND handle = ? AND from_me = 0
                  AND coalesce(person, '') != ?""",
            (canonical, handle, canonical),
        )

    if group_id:
        conn.execute(
            """INSERT INTO groupme_group_profile_sync(
                   group_id, last_message_id, fetched_at)
               VALUES(?,?,?)
               ON CONFLICT(group_id) DO UPDATE SET
                 last_message_id = excluded.last_message_id,
                 fetched_at = excluded.fetched_at""",
            (group_id, last_message_id(summary), stamp),
        )
    return cached


def _refresh_profiles(conn: sqlite3.Connection, client: GroupMe, groups: list[dict],
                      report: base.IngestReport, *, my_id: str) -> bool:
    """Resolve rosters only for included groups whose cached snapshot is stale."""
    wanted = [
        group for group in groups
        if (
            _included_group(
                conn, group.get("name") or str(group.get("id"))
            )
            and not _profiles_are_current(
                conn, group, group.get("name") or str(group.get("id"))
            )
        )
    ]
    if not wanted:
        return False
    results, limited = _fetch(
        report,
        [(group, None) for group in wanted],
        lambda pair: client.group(str(pair[0].get("id"))),
        lambda pair: f"group profile {pair[0].get('name')}",
    )
    profiles = 0
    for (group, _unused), detail in results:
        profiles += _cache_group_profiles(conn, group, detail, my_id=my_id)
    conn.commit()
    report.notes.append(
        f"resolved {profiles} member profiles in {len(results)} gated groups"
    )
    return limited


def message_text(message: dict) -> str:
    text = textclean.clean_message((message.get("text") or "").strip())
    attachments = message.get("attachments") or []
    if not text and attachments:
        kinds = sorted({a.get("type", "attachment") for a in attachments})
        return f"[{', '.join(kinds)}]"
    return text


def ingest(conn: sqlite3.Connection, cfg: Config, *, limit: int = 500,
           include_dms: bool = True, progress=None) -> base.IngestReport:
    report = base.IngestReport.opened("groupme", cfg)
    progress = base.phased(base.adapt_progress(progress), INGEST_PHASES)
    report.progress = progress
    if progress:
        progress("authenticating", phase="connecting")
    try:
        client = GroupMe(cfg)
        me = client.me()
    except base.HttpError as exc:
        report.error = str(exc)
        return report

    my_id = str(me.get("id") or "")
    if my_id:
        identity.link(conn, f"groupme:{my_id}", me.get("name") or "me", source="groupme")
    tier = identity.top_tier(conn)
    # Bounded by message count limit per round and watermarks per conversation.
    budget = limit if limit and limit > 0 else 10 ** 9

    try:
        groups = client.groups()
    except base.HttpError as exc:
        report.error = str(exc)
        return report
    # Record group metadata, roster members, and platform mute status from the initial groups listing.
    pending = []
    initial_skipped = 0
    for group in groups:
        group_id = str(group.get("id"))
        name = group.get("name") or group_id
        members = group_members(group)
        # Evaluate whether unix timestamp in muted_until exceeds current time.
        hushed = _muted(group.get("muted_until"))
        threads.record(conn, "groupme", name, label=group.get("name"),
                       participants=[handle for handle, _seen in members] or None,
                       is_group=True,
                       platform_muted=hushed,
                       platform_note="muted in GroupMe" if hushed else "")
        threads.record_members(conn, "groupme", name, members, review_unknown=True)
        since = base.watermark(conn, f"groupme.group.{group_id}", "") or None
        newest = last_message_id(group)
        if newest and since and newest == since:
            continue
        if not since and not _active_for_initial_load(group):
            initial_skipped += 1
            continue
        pending.append((group, since))
    conn.commit()
    # Sort pending conversations by last activity timestamp in descending order.
    pending.sort(key=lambda pair: _last_activity(pair[0]), reverse=True)
    note = f"{len(groups)} groups, {len(pending)} with anything new"
    if initial_skipped:
        note += f", {initial_skipped} dormant groups skipped on initial load"
    report.notes.append(note)
    if progress:
        progress(f"0/{len(pending)} groups with new messages",
                 done=0, total=len(pending), phase="reading groups")

    stopped = False
    read_groups = 0
    for chunk in _chunks(pending, FETCH_WORKERS):
        if budget <= 0:
            report.more = True
            break
        want = min(PAGE, budget)
        results, stopped = _fetch(
            report, chunk,
            lambda pair: client.group_messages(str(pair[0].get("id")), pair[1], limit=want),
            lambda pair: f"group {pair[0].get('name')}")
        read_groups += len(chunk)
        if progress:
            progress(f"{read_groups}/{len(pending)} groups",
             done=read_groups, phase="reading groups")
        for (group, since), messages in results:
            if len(messages) >= want:
                report.more = True      # this group alone filled the page
            # Process messages in ascending chronological order so the watermark advances accurately.
            messages = sorted(messages, key=lambda m: int(m.get("created_at") or 0))
            newest = since
            name = group.get("name") or str(group.get("id"))
            for message in messages:
                budget -= 1
                _deliver(conn, report, message, thread=name,
                         my_id=my_id, tier=tier, is_group=True)
                newest = message.get("id") or newest
            if newest and newest != since:
                base.set_watermark(conn, f"groupme.group.{str(group.get('id'))}", newest)
        conn.commit()
        if stopped:
            break

    # Refresh rosters for groups with gated messages following delivery.
    if not stopped:
        # Report start and completion for uncounted roster resolution phase.
        if progress:
            progress("checking group rosters", done=0, total=1, phase="rosters")
        stopped = _refresh_profiles(conn, client, groups, report, my_id=my_id)
        if progress:
            progress("rosters current", done=1, total=1, phase="rosters")

    if include_dms and budget > 0 and not stopped:
        try:
            chats = client.chats()
        except base.HttpError as exc:
            report.notes.append(f"DMs unavailable: {str(exc)[:100]}")
            chats = []

        waiting = []
        for chat in chats:
            other = chat.get("other_user") or {}
            other_id = str(other.get("id") or "")
            if not other_id:
                continue
            if other.get("name"):
                handle = f"groupme:{other_id}"
                if not identity.resolve(conn, handle):
                    identity.note_unresolved(conn, handle, "groupme", other["name"],
                                             (chat.get("last_message") or {}).get("text"))
            threads.record(conn, "groupme", other.get("name") or other_id,
                           label=other.get("name"), is_group=False)
            since = base.watermark(conn, f"groupme.dm.{other_id}", "") or None
            newest = last_message_id(chat, "last_message")
            if newest and since and newest == since:
                continue
            waiting.append((chat, since))
        conn.commit()
        report.notes.append(f"{len(chats)} DMs, {len(waiting)} with anything new")
        if progress:
            progress(f"0/{len(waiting)} DMs with new messages",
                     done=0, total=len(waiting), phase="reading DMs")

        read_dms = 0
        for chunk in _chunks(waiting, FETCH_WORKERS):
            if budget <= 0:
                report.more = True
                break
            results, stopped = _fetch(
                report, chunk,
                lambda pair: client.direct_messages(
                    str((pair[0].get("other_user") or {}).get("id")), pair[1]),
                lambda pair: f"DM with {(pair[0].get('other_user') or {}).get('name')}")
            read_dms += len(chunk)
            if progress:
                progress(f"{read_dms}/{len(waiting)} DMs", done=read_dms,
                         phase="reading DMs")
            for (chat, since), messages in results:
                other = chat.get("other_user") or {}
                other_id = str(other.get("id") or "")
                messages = sorted(messages, key=lambda m: int(m.get("created_at") or 0))
                newest = since
                name = other.get("name") or other_id
                for message in messages:
                    budget -= 1
                    _deliver(conn, report, message, thread=name,
                             my_id=my_id, tier=tier, is_group=False)
                    newest = message.get("id") or newest
                if newest and newest != since:
                    base.set_watermark(conn, f"groupme.dm.{other_id}", newest)
            conn.commit()
            if stopped:
                break

    return report


def _fetch(report, chunk: list, call, describe) -> tuple[list, bool]:
    """Execute read-only fetch tasks concurrently via thread pool. Returns (results, hit_rate_limit).

    HTTP requests run across worker threads; database operations remain on the calling thread.
    """
    results, limited = [], False
    with ThreadPoolExecutor(max_workers=min(FETCH_WORKERS, len(chunk))) as pool:
        futures = [(item, pool.submit(call, item)) for item in chunk]
        for item, future in futures:
            try:
                results.append((item, future.result()))
            except base.HttpError as exc:
                if _is_rate_limit(exc):
                    # Stop batch on rate limit to prevent redundant errors and mark ingest incomplete.
                    limited = True
                else:
                    report.notes.append(f"{describe(item)}: {str(exc)[:80]}")
    if limited:
        report.notes.append("rate limited by GroupMe — stopping this round")
        report.more = True
    return results, limited


def _deliver(conn, report, message: dict, *, thread: str, my_id: str, tier: set[str],
             is_group: bool) -> None:
    text = message_text(message)
    if not text or message.get("system"):
        return
    user_id = str(message.get("user_id") or "")
    from_me = bool(my_id and user_id == my_id)
    handle = f"groupme:{user_id}" if user_id else None
    person = identity.resolve(conn, handle) if handle else None
    base.deliver(
        conn, report,
        stream="groupme",
        external_id=str(message.get("id")),
        ts=to_iso(message.get("created_at")),
        text=text,
        thread=thread,
        handle=None if from_me else handle,
        person="me" if from_me else person,
        from_me=from_me,
        is_group=is_group,
        top_tier=tier,
        meta={"seen_name": message.get("name"), "group": is_group,
              "likes": len(message.get("favorited_by") or [])},
    )


@register
class GroupMeSource(Source):
    name = "groupme"
    description = "GroupMe groups and DMs (API v3 personal access token)"
    secrets = ("GROUPME_ACCESS_TOKEN",)
    order = 30

    def fetch(self, conn, cfg, report, limit):
        from .bluebubbles import _absorb
        _absorb(report, ingest(conn, cfg, limit=limit, progress=report.progress))

    def check(self, cfg):
        if not cfg.secret("GROUPME_ACCESS_TOKEN", "groupme"):
            return False, "no token — add `groupme=<token>` to .env"
        try:
            me = GroupMe(cfg).me()
            return True, f"authenticated as {me.get('name', '?')}"
        except base.HttpError as exc:
            return False, str(exc)[:80]
