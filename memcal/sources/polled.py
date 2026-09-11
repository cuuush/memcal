"""Base classes for message sources.

PolledSource: list conversations, read each one forward from its own watermark. Slack,
Telegram, GroupMe. This was the same loop written once per connector.

StreamSource: walk one ordered stream from one cursor. iMessage, WhatsApp, BlueBubbles,
Signal — none of these have a conversation list to enumerate.

Two classes, not one with a mode flag. Subclasses implement auth, naming, and what counts
as a real message; watermarks, paging, budget and rate-limit handling live here.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from typing import Any, Iterable

from .. import db, identity, threads
from ..config import Config
from . import base
from .spec import Source, SourceError


@dataclass
class Conversation:
    """A chat as the platform reports it."""

    #: Stable platform id. Used for the watermark key, so it must not change on rename.
    id: str
    #: Thread name as stored. Disambiguate here — threads key on this string, and a
    #: bare `#general` collides across workspaces.
    name: str
    #: What the platform calls it, when that differs from `name`.
    label: str | None = None
    is_group: bool = False
    #: Newest message id if the listing includes one; matching the watermark means
    #: nothing new, saving a request.
    newest: str = ""
    #: Ordering key for spending the budget on live conversation first. Higher is newer.
    recency: float = 0.0
    muted: bool = False
    muted_note: str = ""
    #: Last-spoke time, for the dormant-chat skip on a first run. None means unknown,
    #: which we read rather than drop.
    last_activity: Any = None
    #: The platform's own object, for hooks that need more than this carries.
    raw: Any = None


@dataclass
class Message:
    """A message, normalized enough for base.deliver()."""

    external_id: str
    ts: str
    text: str
    #: Ascending sort key within a conversation — snowflakes and rowids as ints,
    #: otherwise the timestamp. The watermark advances in this order, so getting it
    #: wrong drops messages rather than duplicating them.
    order: float = 0.0
    author_id: str = ""
    author_name: str | None = None
    from_me: bool = False
    meta: dict = field(default_factory=dict)
    #: What to store as the watermark. Defaults to `external_id`.
    cursor: str = ""
    #: Seen and deliberately not archived: a join notice, a bot post, a receipt. Still
    #: advances the watermark — otherwise a chat whose newest messages are all notices
    #: gets re-requested on every run forever.
    skip: bool = False

    def watermark(self) -> str:
        return self.cursor or self.external_id

    @classmethod
    def passed_over(cls, cursor: str, order: float) -> "Message":
        """A message we've seen and decided not to archive."""
        return cls(external_id=cursor, ts="", text="", order=order, cursor=cursor,
                   skip=True)


class _Budgeted:
    """Bookkeeping both shapes need: budget, progress phases, delivery."""

    #: Included in `memcal sources`; also the `stream` column and the watermark prefix.
    name: str = ""
    #: Largest page this platform will return in one request.
    page: int = 100
    #: Handle prefix for identity. `slack:U123`, `telegram:456`.
    handle_prefix: str = ""

    def handle_for(self, author_id: str) -> str | None:
        prefix = self.handle_prefix or self.name
        return f"{prefix}:{author_id}" if author_id else None

    def _phased(self, report: base.IngestReport, plan):
        progress = base.phased(report.progress, plan)
        report.progress = progress
        return progress

    def _deliver_one(self, conn: sqlite3.Connection, report: base.IngestReport,
                     message: Message, *, thread: str, is_group: bool,
                     tier: set[str]) -> None:
        handle = None if message.from_me else self.handle_for(message.author_id)
        meta = dict(message.meta)
        meta.setdefault("seen_name", message.author_name)
        if is_group:
            meta.setdefault("group", True)
        base.deliver(
            conn, report,
            stream=self.name,
            external_id=message.external_id,
            ts=message.ts,
            text=message.text,
            thread=thread,
            handle=handle,
            person="me" if message.from_me else None,
            from_me=message.from_me,
            is_group=is_group,
            top_tier=tier,
            meta=meta,
        )

    # ------------------------------------------------------------------- hooks --
    def connect(self, cfg: Config):
        """Build and return the platform client. Raise SourceError to fail cleanly."""
        raise NotImplementedError

    def identify(self, conn: sqlite3.Connection, client, report: base.IngestReport) -> str:
        """Link the owner's handle, return their platform id.

        Without it, your own messages look like someone else's.
        """
        return ""

    def is_rate_limit(self, exc: Exception) -> bool:
        """True if this error should stop the round rather than be logged and skipped."""
        return False


class PolledSource(_Budgeted, Source):
    """Enumerate conversations, read each forward from its own watermark.

    Subclasses implement conversations(), history() and normalize().
    """

    #: On a first run, a conversation silent longer than this is not read at all.
    #: Dormant chats are common and each one costs a request to learn nothing.
    initial_days: int = 30
    #: What `memcal ingest` calls these in progress lines.
    noun: str = "conversations"
    phases = (("connecting", 5), ("listing", 20), ("reading", 75))

    # ------------------------------------------------------------------- hooks --
    def conversations(self, client) -> Iterable[Conversation]:
        """Conversations worth reading. Drop anything that can't hold a conversation."""
        raise NotImplementedError

    def history(self, client, conversation: Conversation, since: str | None,
                limit: int) -> list:
        """Up to `limit` messages after `since`, in whatever order the platform likes."""
        raise NotImplementedError

    def normalize(self, raw, conversation: Conversation) -> Message | None:
        """A platform message as a Message, or None if it can't be parsed.

        For messages that parse but shouldn't be archived — system notices, bot posts,
        empty bodies — return Message.passed_over(cursor, order). That advances the
        watermark past them. None leaves the item behind the watermark, so use it only
        for things this source genuinely can't read; a later fix can still pick them up.
        """
        raise NotImplementedError

    # -------------------------------------------------------------------- loop --
    def fetch(self, conn: sqlite3.Connection, cfg: Config, report: base.IngestReport,
              limit: int) -> None:
        progress = self._phased(report, self.phases)
        if progress:
            progress("authenticating", phase="connecting")
        client = self.connect(cfg)
        my_id = self.identify(conn, client, report)
        tier = identity.top_tier(conn)
        budget = limit if limit and limit > 0 else 10 ** 9

        found = list(self.conversations(client))
        if progress:
            progress(f"0/{len(found)} {self.noun}", done=0, total=len(found),
                     phase="listing")

        pending: list[tuple[Conversation, str | None]] = []
        dormant = 0
        for index, conversation in enumerate(found, start=1):
            if not conversation.id:
                continue
            threads.record(conn, self.name, conversation.name,
                           label=conversation.label or conversation.name,
                           is_group=conversation.is_group,
                           platform_muted=conversation.muted or None,
                           platform_note=conversation.muted_note or None)
            since = base.watermark(conn, f"{self.name}.{conversation.id}", "") or None
            # Listing already gave the newest id — nothing new, no request needed.
            if conversation.newest and since and conversation.newest == since:
                continue
            if not since and not self._active_enough(conversation):
                dormant += 1
                continue
            pending.append((conversation, since))
            if progress:
                progress(f"{index}/{len(found)} {self.noun}", done=index, phase="listing")
        conn.commit()

        # Newest first, so an exhausted budget is spent on live conversation.
        pending.sort(key=lambda pair: pair[0].recency, reverse=True)
        note = f"{len(found)} {self.noun}, {len(pending)} with anything new"
        if dormant:
            note += f", {dormant} dormant skipped on initial load"
        report.notes.append(note)
        if progress:
            progress(f"0/{len(pending)} {self.noun}", done=0, total=len(pending),
                     phase="reading")

        for index, (conversation, since) in enumerate(pending, start=1):
            if budget <= 0:
                report.more = True
                break
            want = min(self.page, budget)
            try:
                raw_messages = self.history(client, conversation, since, want)
            except Exception as exc:
                if self.is_rate_limit(exc):
                    report.notes.append(
                        f"rate limited by {self.name} — stopping this round")
                    report.more = True
                    break
                # An unreadable chat (no access, removed mid-run) is normal; skip it.
                report.notes.append(f"{conversation.name}: {str(exc)[:80]}")
                continue
            finally:
                if progress:
                    progress(f"{index}/{len(pending)} {self.noun}", done=index,
                             phase="reading")
            if len(raw_messages) >= want:
                report.more = True

            parsed = [self.normalize(raw, conversation) for raw in raw_messages]
            # Sort by platform order so a partial round can't skip past the watermark.
            ordered = sorted((m for m in parsed if m is not None), key=lambda m: m.order)
            newest = since
            for message in ordered:
                budget -= 1
                if not message.skip and message.text.strip():
                    self._deliver_one(conn, report, message,
                                      thread=conversation.name,
                                      is_group=conversation.is_group, tier=tier)
                newest = message.watermark() or newest
            if newest and newest != since:
                base.set_watermark(conn, f"{self.name}.{conversation.id}", newest)
            conn.commit()

    def _active_enough(self, conversation: Conversation) -> bool:
        """Did this chat speak inside the initial-load window?"""
        moment = conversation.last_activity
        if moment is None:
            # Unknown means read it. An empty chat has no newest id and never gets here.
            return True
        cutoff = db.now_dt().astimezone(timezone.utc) - timedelta(days=self.initial_days)
        try:
            return moment >= cutoff
        except TypeError:
            return True


class StreamSource(_Budgeted, Source):
    """Walk one ordered stream forward from one cursor.

    For platforms with no conversation list: a local database ordered by rowid, or a
    receive queue. Each message names the conversation it belongs to.
    """

    #: Single watermark key suffix. One stream, one cursor.
    cursor_key: str = "cursor"
    phases = (("connecting", 10), ("reading", 90))

    # ------------------------------------------------------------------- hooks --
    def stream(self, client, since: str | None, limit: int) -> Iterable:
        """Up to `limit` items after `since`, oldest first."""
        raise NotImplementedError

    def normalize(self, raw) -> tuple[Conversation, Message] | None:
        """An item as (conversation, message), or None to skip it."""
        raise NotImplementedError

    # -------------------------------------------------------------------- loop --
    def fetch(self, conn: sqlite3.Connection, cfg: Config, report: base.IngestReport,
              limit: int) -> None:
        progress = self._phased(report, self.phases)
        if progress:
            progress("connecting", phase="connecting")
        client = self.connect(cfg)
        self.identify(conn, client, report)
        tier = identity.top_tier(conn)
        want = limit if limit and limit > 0 else 10 ** 9

        key = f"{self.name}.{self.cursor_key}"
        since = base.watermark(conn, key, "") or None
        items = list(self.stream(client, since, want))
        if len(items) >= want:
            report.more = True
        if progress:
            progress(f"0/{len(items)} messages", done=0, total=len(items), phase="reading")

        seen: set[str] = set()
        newest = since
        for index, raw in enumerate(items, start=1):
            parsed = self.normalize(raw)
            if progress:
                progress(f"{index}/{len(items)} messages", done=index, phase="reading")
            if parsed is None:
                continue
            conversation, message = parsed
            if conversation.id not in seen:
                seen.add(conversation.id)
                threads.record(conn, self.name, conversation.name,
                               label=conversation.label or conversation.name,
                               is_group=conversation.is_group,
                               platform_muted=conversation.muted or None,
                               platform_note=conversation.muted_note or None)
            if message.text.strip():
                self._deliver_one(conn, report, message, thread=conversation.name,
                                  is_group=conversation.is_group, tier=tier)
            newest = message.watermark() or newest
        if newest and newest != since:
            base.set_watermark(conn, key, newest)
        conn.commit()


def link_me(conn: sqlite3.Connection, report: base.IngestReport, stream: str,
            my_id: str, *, name: str = "me", setting: str = "") -> str:
    """Link the owner's handle, or note why their messages will look wrong."""
    my_id = str(my_id or "").strip()
    if my_id:
        identity.link(conn, f"{stream}:{my_id}", name or "me", source=stream)
    elif setting:
        report.notes.append(
            f"{setting} is unset — your own messages will read as another "
            f"participant's until it is set")
    return my_id


def save_credential(cfg: Config, key: str, value: str) -> None:
    """Store a credential in the store's own `.env` and this process.

    Uses the same writer as Settings, so hand edits survive and the file stays
    0600. Values are never printed — callers show at most "already set".
    """
    from .. import settings
    settings.write_env(cfg.home / settings.STORE_ENV, {key: value})
    cfg.env[key] = value


def credential_is_set(cfg: Config, key: str) -> bool:
    """Is there already a value, without revealing it."""
    return bool(cfg.secret(key, key.lower()))


def ask(question: str) -> str:
    """One stdin line, empty when there is no terminal to ask in."""
    try:
        return input(question).strip()
    except EOFError:
        return ""


def confirm_overwrite(cfg: Config, key: str) -> bool:
    """True when a credential may be (re)written — asks if one is already set."""
    if not credential_is_set(cfg, key):
        return True
    answer = ask(f"`{key.lower()}` is already set — overwrite it? [y/N]: ")
    return answer.lower() in ("y", "yes")


__all__ = ["Conversation", "Message", "PolledSource", "StreamSource", "SourceError",
           "link_me", "save_credential", "credential_is_set", "ask",
           "confirm_overwrite"]
