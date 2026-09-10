"""Telegram DMs, groups and channels via Telethon.

Bots can't see your DMs, same as Discord — but Telegram also publishes a user API and
expects clients to be built on it. Get an api_id/api_hash at https://my.telegram.org.

telethon.sync runs the client synchronously; memcal polls from cron and has no event
loop to hand Telethon. The import is in connect() so a missing library shows up in
check() instead of dropping the source from the registry.

First run needs an interactive login: `memcal login telegram`. The session file grants
full account access, so it lives under the memcal home, not the checkout.
"""

from __future__ import annotations

import sqlite3
from datetime import timezone

from .. import textclean
from ..config import Config
from . import base, register
from .polled import Conversation, Message, PolledSource, SourceError, link_me

#: Telethon pages internally; this bounds one conversation's share of a round.
PAGE = 200


def session_path(cfg: Config) -> str:
    """Session file location. It grants full account access, so keep it with the data."""
    return str(cfg.home / "telegram.session")


def _entity_name(entity) -> str:
    """Display name for a user, a group or a channel."""
    title = getattr(entity, "title", None)
    if title:
        return " ".join(str(title).split())
    parts = [getattr(entity, "first_name", None), getattr(entity, "last_name", None)]
    name = " ".join(" ".join(str(p).split()) for p in parts if p)
    if name:
        return name
    handle = getattr(entity, "username", None)
    return f"@{handle}" if handle else str(getattr(entity, "id", "") or "chat")


def message_text(message) -> str:
    """Message text, or a media summary when there's no text."""
    body = str(getattr(message, "message", "") or "").strip()
    if body:
        text = textclean.spoken_text(body)
        if text:
            return textclean.clean_message(text)
    media = getattr(message, "media", None)
    if media is None:
        return ""
    kind = type(media).__name__.replace("MessageMedia", "").lower() or "media"
    return f"[{kind}]"


@register
class TelegramSource(PolledSource):
    name = "telegram"
    description = "Telegram DMs, groups and channels (MTProto user API, Telethon)"
    secrets = ("TELEGRAM_API_ID", "TELEGRAM_API_HASH")
    order = 36
    page = PAGE
    noun = "chats"
    handle_prefix = "telegram"

    def __init__(self) -> None:
        self._me = ""
        self._client = None

    # ------------------------------------------------------------- connection --
    def _build(self, cfg: Config):
        api_id = cfg.secret("TELEGRAM_API_ID", "telegramapiid")
        api_hash = cfg.secret("TELEGRAM_API_HASH", "telegramapihash")
        if not api_id or not api_hash:
            raise SourceError(
                "no Telegram API credentials. Sign in at https://my.telegram.org, open "
                "'API development tools', and add `telegram_api_id=` and "
                "`telegram_api_hash=` to memcal/.env.")
        try:
            from telethon.sync import TelegramClient                  # noqa: PLC0415
        except ImportError as exc:
            raise SourceError(
                "telethon is not installed — `pip install telethon`") from exc
        try:
            return TelegramClient(session_path(cfg), int(api_id), str(api_hash))
        except (TypeError, ValueError) as exc:
            raise SourceError(f"TELEGRAM_API_ID must be a number: {exc}") from exc

    def connect(self, cfg: Config):
        client = self._build(cfg)
        client.connect()
        if not client.is_user_authorized():
            client.disconnect()
            raise SourceError(
                "Telegram is not logged in yet — run `memcal login telegram` once to "
                "sign in with your phone number.")
        self._client = client
        me = client.get_me()
        self._me = str(getattr(me, "id", "") or "")
        return client

    def identify(self, conn: sqlite3.Connection, client,
                 report: base.IngestReport) -> str:
        me = client.get_me()
        return link_me(conn, report, "telegram", self._me, name=_entity_name(me))

    def is_rate_limit(self, exc: Exception) -> bool:
        # Telethon raises FloodWaitError with the seconds Telegram wants back.
        return type(exc).__name__ == "FloodWaitError"

    # ------------------------------------------------------------------ shape --
    def conversations(self, client):
        for dialog in client.iter_dialogs():
            entity = dialog.entity
            chat_id = str(getattr(entity, "id", "") or "")
            if not chat_id:
                continue
            # A broadcast channel you merely subscribe to is a feed, not a conversation.
            if getattr(entity, "broadcast", False):
                continue
            newest = getattr(dialog.message, "id", None)
            when = getattr(dialog, "date", None)
            if when is not None and when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            yield Conversation(
                id=chat_id,
                name=_entity_name(entity),
                label=_entity_name(entity),
                is_group=bool(getattr(entity, "megagroup", False)
                              or getattr(entity, "participants_count", None)
                              or getattr(entity, "title", None)),
                newest=str(newest) if newest else "",
                recency=when.timestamp() if when else 0.0,
                muted=bool(getattr(dialog, "archived", False)),
                muted_note="archived in Telegram" if getattr(dialog, "archived", False)
                           else "",
                last_activity=when,
                raw=dialog,
            )

    def history(self, client, conversation: Conversation, since: str | None,
                limit: int) -> list:
        # min_id is exclusive and reverse=True walks forward from it, so a partial page
        # still leaves a watermark with nothing skipped past it.
        try:
            min_id = int(since) if since else 0
        except (TypeError, ValueError):
            min_id = 0
        return list(client.iter_messages(
            conversation.raw.entity, limit=min(limit, PAGE), min_id=min_id,
            reverse=True))

    def normalize(self, raw, conversation: Conversation) -> Message | None:
        message_id = getattr(raw, "id", None)
        if not message_id:
            return None                       # no id, no place in the ordering
        # A service message — someone joined, the title changed, a call happened.
        if getattr(raw, "action", None) is not None:
            return Message.passed_over(str(message_id), float(message_id))
        sender = getattr(raw, "sender", None)
        author = str(getattr(raw, "sender_id", "") or "")
        out = bool(getattr(raw, "out", False))
        when = getattr(raw, "date", None)
        if when is not None and when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        from .. import db
        return Message(
            external_id=f"{conversation.id}:{message_id}",
            ts=when.astimezone().isoformat(timespec="seconds") if when else db.now(),
            text=message_text(raw),
            order=float(message_id),
            author_id=author,
            author_name=_entity_name(sender) if sender is not None else None,
            from_me=out or bool(author and author == self._me),
            cursor=str(message_id),
            meta={"group": conversation.is_group},
        )

    # ------------------------------------------------------------------ setup --
    def setup(self, cfg: Config) -> tuple[bool, str]:
        """One-time login. Telethon prompts for phone number, code and password."""
        client = self._build(cfg)
        client.start()                      # prompts on stdin, writes the session file
        me = client.get_me()
        client.disconnect()
        return True, f"signed in as {_entity_name(me)}; session at {session_path(cfg)}"

    # ------------------------------------------------------------------ check --
    def check(self, cfg: Config) -> tuple[bool, str]:
        try:
            client = self._build(cfg)
        except SourceError as exc:
            return False, str(exc)[:120]
        try:
            client.connect()
            if not client.is_user_authorized():
                return False, "not logged in — run `memcal login telegram`"
            me = client.get_me()
            return True, f"connected as {_entity_name(me)}"
        except Exception as exc:
            return False, str(exc)[:80]
        finally:
            try:
                client.disconnect()
            except Exception:
                pass
