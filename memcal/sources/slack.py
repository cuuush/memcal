"""Slack DMs, group DMs and channels via slack_sdk.

Needs a user token (xoxp-), not a bot token — bot tokens can't see your DMs. The
im:history scope covers them.

slack_sdk is imported in connect(), not at module scope: memcal has no hard dependencies,
so a missing library should show up in check() rather than drop the source from the
registry with a swallowed ImportError.

No SLACK_USER_ID setting on purpose. auth_test() already returns the user id, and a
`slack` + `slack_user_id` pair trips the prefix match in Config.secret.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
import uuid

from .. import textclean
from ..config import Config
from . import base, register
from .polled import Conversation, Message, PolledSource, SourceError, link_me

#: `conversations.list` and `users.list` cap here.
LIST_PAGE = 200
#: `conversations.history` caps at 1000, but 200 keeps a round responsive.
PAGE = 200

#: Everything that can hold a human conversation, including DMs and group DMs.
CHANNEL_TYPES = "public_channel,private_channel,mpim,im"

#: The user scopes memcal reads with. History scopes see messages; users:read and
#: channels:read turn author and channel ids into names. No write scopes on purpose.
SLACK_USER_SCOPES = ("im:history", "mpim:history", "channels:history",
                     "groups:history", "users:read", "channels:read")


def provision_manifest() -> dict:
    """Minimal CLI manifest: a token-only app, no bot, no listeners."""
    return {
        "_metadata": {"major_version": 1, "minor_version": 1},
        "display_information": {
            "name": "memcal",
            "description": "Personal memory ingest: reads my DMs and channels for memcal",
        },
        "oauth_config": {"scopes": {"user": list(SLACK_USER_SCOPES)}},
        "settings": {
            "org_deploy_enabled": False,
            "socket_mode_enabled": False,
            "token_rotation_enabled": False,
        },
    }


def _cli_error(output: str) -> str:
    """Last meaningful lines of CLI output, without the generic help footer."""
    filler = ("learn more about this command", "suggestion",
              "check ", "for error logs", "try running the command")
    lines = [line.strip() for line in (output or "").strip().splitlines()
             if line.strip() and not any(f in line.lower() for f in filler)]
    # The prompt echo ("the prompt that would have been shown...") names the
    # missing answer, which is the useful half — keep it, drop the preamble.
    return " — ".join(lines[-2:])[:300] if lines else "unknown error"


def _slack_teams(binary: str) -> list[str]:
    """Workspace ids the Slack CLI is logged into, from `slack auth list`."""
    try:
        done = subprocess.run([binary, "auth", "list"], capture_output=True,
                              text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if done.returncode != 0:
        return []
    return re.findall(r"Team ID:\s+([A-Z0-9]+)", done.stdout)


def scaffold_cli_project(directory: str) -> None:
    """A manifest-only CLI project: no Bolt code to download or install."""
    from pathlib import Path
    root = Path(directory)
    (root / "manifest.json").write_text(json.dumps(provision_manifest(), indent=2))
    dot = root / ".slack"
    dot.mkdir(exist_ok=True)
    (dot / "config.json").write_text(json.dumps(
        {"manifest": {"source": "local"}, "project_id": str(uuid.uuid4())}))
    # `get-manifest` prints manifest.json directly — no Bolt SDK to install.
    # (The templates point get-hooks at slack_cli_hooks, which needs its package;
    # a token-only app has no code, so there is nothing to hook into.)
    # The CLI appends a `--source=DIR` argument; sh swallows it, cat ignores it.
    (dot / "hooks.json").write_text(json.dumps(
        {"hooks": {"get-manifest": "sh -c 'cat manifest.json'"}}))

#: Subtypes that are still a person speaking. Everything else — joins, leaves, pins,
#: channel renames — is a notice with no author making a commitment.
SPOKEN_SUBTYPES = frozenset({"", "thread_broadcast", "me_message", "file_share"})

#: `<@U123>` and `<@U123|name>`.
MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|([^>]*))?>")
#: `<#C123|name>` and `<#C123>`.
CHANNEL_LINK = re.compile(r"<#([CD][A-Z0-9]+)(?:\|([^>]*))?>")
#: `<https://example.com|text>` — keep the text a human wrote, drop the target.
LINK = re.compile(r"<(https?://[^>|]+)(?:\|([^>]*))?>")
#: `<!here>`, `<!channel>`, `<!subteam^S123|@team>`.
BROADCAST = re.compile(r"<!(here|channel|everyone)(?:\|[^>]*)?>")
SUBTEAM = re.compile(r"<!subteam\^[A-Z0-9]+(?:\|@?([^>]*))?>")


def message_text(raw: dict, users: dict[str, str], channels: dict[str, str]) -> str:
    """Resolve Slack's id markup. Falls back to a file summary when there's no text."""
    body = str(raw.get("text") or "").strip()
    if body:
        body = MENTION.sub(
            lambda m: f"@{m.group(2) or users.get(m.group(1), 'someone')}", body)
        body = CHANNEL_LINK.sub(
            lambda m: f"#{m.group(2) or channels.get(m.group(1), 'channel')}", body)
        body = LINK.sub(lambda m: m.group(2) or m.group(1), body)
        body = BROADCAST.sub(lambda m: f"@{m.group(1)}", body)
        body = SUBTEAM.sub(lambda m: f"@{m.group(1) or 'team'}", body)
        # Slack escapes these three and only these three.
        body = body.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
        text = textclean.spoken_text(body)
        if text:
            return textclean.clean_message(text)
    kinds = sorted({str(f.get("mimetype") or "file").split("/")[0]
                    for f in raw.get("files") or []})
    kinds += ["attachment"] * bool(raw.get("attachments"))
    return f"[{', '.join(kinds)}]" if kinds else ""


def to_iso(ts: str) -> str:
    """Slack ts is `1712345678.000200`: epoch seconds plus a per-channel tiebreaker."""
    from datetime import datetime, timezone
    try:
        seconds = float(ts)
    except (TypeError, ValueError):
        from .. import db
        return db.now()
    return (datetime.fromtimestamp(seconds, tz=timezone.utc)
            .astimezone().isoformat(timespec="seconds"))


@register
class SlackSource(PolledSource):
    name = "slack"
    description = "Slack DMs, group DMs and channels (user token, slack_sdk)"
    secrets = ("SLACK_TOKEN",)
    order = 35
    page = PAGE
    noun = "conversations"
    handle_prefix = "slack"

    def __init__(self) -> None:
        self._users: dict[str, str] = {}
        self._channels: dict[str, str] = {}
        self._me = ""
        self._team = ""

    # ------------------------------------------------------------- connection --
    def _client(self, cfg: Config):
        token = cfg.secret("SLACK_TOKEN", "slack")
        if not token:
            raise SourceError("no Slack token — run `memcal login slack`")
        return self._client_for_token(token)

    def _client_for_token(self, token: str):
        try:
            from slack_sdk import WebClient                       # noqa: PLC0415
            from slack_sdk.http_retry.builtin_handlers import (   # noqa: PLC0415
                ConnectionErrorRetryHandler, RateLimitErrorRetryHandler)
        except ImportError as exc:
            raise SourceError(
                "slack_sdk is not installed — `pip install slack_sdk`") from exc
        # The SDK honours Slack's Retry-After, so there's no backoff to hand-roll here.
        return WebClient(token=token, retry_handlers=[
            RateLimitErrorRetryHandler(max_retry_count=3),
            ConnectionErrorRetryHandler(max_retry_count=2),
        ])

    def validate_token(self, token: str) -> tuple[bool, str]:
        """Does this token work, and who does Slack say it belongs to."""
        try:
            who = _body(self._client_for_token(token).auth_test())
        except Exception as exc:
            return False, f"Slack rejected the token: {_reason(exc)}"[:120]
        name = str(who.get("user") or who.get("user_id") or "you")
        team = f" in {who['team']}" if who.get("team") else ""
        return True, f"connected as {name}{team}"

    def connect(self, cfg: Config):
        client = self._client(cfg)
        try:
            who = _body(client.auth_test())
        except Exception as exc:
            raise SourceError(f"Slack rejected the token: {_reason(exc)}") from exc
        self._me = str(who.get("user_id") or "")
        self._team = str(who.get("team") or "")
        self._users = self._load_users(client)
        return client

    def identify(self, conn: sqlite3.Connection, client,
                 report: base.IngestReport) -> str:
        return link_me(conn, report, "slack", self._me,
                       name=self._users.get(self._me) or "me")

    def is_rate_limit(self, exc: Exception) -> bool:
        response = getattr(exc, "response", None)
        return getattr(response, "status_code", None) == 429

    # ------------------------------------------------------------------ shape --
    def _load_users(self, client) -> dict[str, str]:
        """Fetch every display name once, instead of a users.info per message author."""
        names: dict[str, str] = {}
        cursor = None
        while True:
            page = client.users_list(limit=LIST_PAGE, cursor=cursor)
            for user in _items(page, "members"):
                if not isinstance(user, dict):
                    continue
                profile = user.get("profile") or {}
                label = (profile.get("display_name") or profile.get("real_name")
                         or user.get("real_name") or user.get("name") or "")
                if user.get("id"):
                    names[str(user["id"])] = " ".join(str(label).split())
            cursor = _next_cursor(page)
            if not cursor:
                return names

    def conversations(self, client):
        cursor = None
        while True:
            page = client.conversations_list(
                types=CHANNEL_TYPES, exclude_archived=True, limit=LIST_PAGE,
                cursor=cursor)
            for raw in _items(page, "channels"):
                if not isinstance(raw, dict):
                    continue
                found = self._conversation(raw)
                if found is not None:
                    self._channels[found.id] = found.label or found.name
                    yield found
            cursor = _next_cursor(page)
            if not cursor:
                return

    def _conversation(self, raw: dict) -> Conversation | None:
        channel_id = str(raw.get("id") or "")
        if not channel_id:
            return None
        is_dm = bool(raw.get("is_im"))
        is_group_dm = bool(raw.get("is_mpim"))
        if is_dm:
            other = str(raw.get("user") or "")
            if other == self._me:
                return None                    # the note-to-self DM, not a conversation
            label = self._users.get(other) or other
            name = label
        else:
            label = str(raw.get("name") or channel_id)
            # A bare `#general` collides across workspaces; threads are keyed by name.
            name = f"{self._team} #{label}".strip() if self._team else f"#{label}"
        # The listing carries no newest-message id, so `newest` stays empty and every
        # conversation costs one history call; `latest` just orders them.
        recency = _float(raw.get("updated"), 0.0) / 1000.0 or _float(
            (raw.get("latest") or {}).get("ts"), 0.0)
        return Conversation(
            id=channel_id,
            name=name,
            label=label,
            is_group=not is_dm,
            recency=recency,
            muted=bool(raw.get("is_muted")),
            muted_note="muted in Slack" if raw.get("is_muted") else "",
            last_activity=_moment(recency),
            raw=raw,
        )

    def history(self, client, conversation: Conversation, since: str | None,
                limit: int) -> list:
        # Slack returns newest-first. `polled.py` sorts and advances the watermark
        # to the max, so returning just the first page would archive the newest
        # 200 and skip older backlog forever. Page to the end, then hand back
        # the oldest `want` — the next round picks up where this one stopped.
        want = min(limit, PAGE)
        collected: list = []
        cursor = None
        for _ in range(50):  # 10k messages; beyond that the next round continues
            page = client.conversations_history(
                channel=conversation.id, oldest=since, limit=PAGE,
                inclusive=False, cursor=cursor)
            collected.extend(_items(page, "messages"))
            cursor = _next_cursor(page)
            if not cursor:
                break
        collected.sort(key=lambda m: _float(m.get("ts"), 0.0)
                       if isinstance(m, dict) else 0.0)
        return collected[:want] if len(collected) > want else collected

    def normalize(self, raw: dict, conversation: Conversation) -> Message | None:
        ts = str(raw.get("ts") or "")
        if not ts:
            return None                       # no id, no place in the ordering
        seen = Message.passed_over(ts, _float(ts, 0.0))
        if str(raw.get("subtype") or "") not in SPOKEN_SUBTYPES:
            return seen                       # a join, a pin, a channel rename
        # Slack apps and workflows post as bots; they make no commitments.
        if raw.get("bot_id") and not raw.get("user"):
            return seen
        author = str(raw.get("user") or "")
        return Message(
            external_id=f"{conversation.id}:{ts}",
            ts=to_iso(ts),
            text=message_text(raw, self._users, self._channels),
            order=_float(ts, 0.0),
            author_id=author,
            author_name=self._users.get(author),
            from_me=bool(author and author == self._me),
            cursor=ts,
            meta={"reactions": sum(int(r.get("count") or 0)
                                   for r in raw.get("reactions") or []),
                  "replies": int(raw.get("reply_count") or 0)},
        )

    # ------------------------------------------------------------------ check --
    def check(self, cfg: Config) -> tuple[bool, str]:
        try:
            client = self._client(cfg)
        except SourceError as exc:
            return False, str(exc)[:120]
        try:
            raw_who = client.auth_test()
            who = _body(raw_who)
        except Exception as exc:
            return False, _reason(exc)[:80]
        scopes = _granted(raw_who)
        line = f"connected as {who.get('user') or who.get('user_id')}"
        if who.get("team"):
            line += f" in {who['team']}"
        missing = [s for s in ("im:history", "users:read") if scopes and s not in scopes]
        if missing:
            line += f" — missing scope(s): {', '.join(missing)}; DMs will not be read"
        return True, line

    # ------------------------------------------------------------------ setup --
    def setup(self, cfg: Config) -> tuple[bool, str]:
        """Get a Slack user token: provision the app, paste the token, save it.

        A saved token that still validates ends here with no new app — re-running
        this never litters the workspace with duplicates. Otherwise the Slack CLI
        creates and installs the app (Slack issues the token itself only to a
        human, so pasting it here is the one manual step), the token is checked
        with `auth_test` before anything is written, and an existing value is
        only overwritten on confirmation.
        """
        from .polled import ask, confirm_overwrite, save_credential
        existing = (cfg.secret("SLACK_TOKEN", "slack") or "").strip()
        if existing:
            try:
                ok, line = self.validate_token(existing)
            except SourceError as exc:
                return False, str(exc)[:120]
            if ok:
                return True, f"already set up — {line}"
            print(f"Saved token no longer works ({line}) — let's replace it.")
        binary = shutil.which("slack")
        if not binary:
            print("Without the Slack CLI this is the by-hand path: create an app at "
                  "https://api.slack.com/apps with the User Token Scopes "
                  f"{', '.join(SLACK_USER_SCOPES)}, install it, copy the User OAuth "
                  "Token (xoxp-...). Or install the CLI and re-run: `curl -fsSL "
                  "https://downloads.slack-edge.com/slack-cli/install.sh | bash`, "
                  "`slack login`.")
        else:
            print("Creating a Slack app called `memcal` with read-only user scopes "
                  f"({', '.join(SLACK_USER_SCOPES)}) and installing it to your workspace.")
            self._provision(binary)
        for _ in range(3):
            token = ask("Paste the User OAuth Token (xoxp-...): ")
            if not token:
                return False, "no token given — re-run `memcal login slack` when ready"
            if not token.startswith("xoxp-"):
                print("That doesn't look like a user token — it starts with `xoxp-`, "
                      "not `xoxb-` (the bot token can't see your DMs).")
                continue
            try:
                ok, line = self.validate_token(token)
            except SourceError as exc:
                return False, str(exc)[:120]
            if not ok:
                print(f"{line} — try again.")
                continue
            if not confirm_overwrite(cfg, "SLACK_TOKEN"):
                return False, "kept the existing token"
            save_credential(cfg, "SLACK_TOKEN", token)
            return True, f"saved — {line}"
        return False, "three bad pastes — re-run `memcal login slack` when ready"

    def _provision(self, binary: str) -> None:
        """Create and install the app through the Slack CLI (manifest-only project)."""
        from .polled import ask
        directory = tempfile.mkdtemp(prefix="memcal-slack-")
        scaffold_cli_project(directory)
        teams = _slack_teams(binary)
        if not teams:
            print("Slack CLI isn't logged into any workspace — run `slack login` "
                  "first. Falling through to the token paste anyway.")
            return
        team = teams[0]
        if len(teams) > 1:
            print("Logged into several workspaces:")
            for index, candidate in enumerate(teams, start=1):
                print(f"  {index}. {candidate}")
            choice = ask("Install to which workspace? [1]: ")
            if choice.isdigit() and 1 <= int(choice) <= len(teams):
                team = teams[int(choice) - 1]
        try:
            # The trailing newline answers "Create a new app"; -w preselects the
            # team, which is what lets this run without an interactive terminal.
            done = subprocess.run(
                [binary, "install", "--environment", "local", "-w", team],
                input="\n", capture_output=True, text=True, timeout=120,
                cwd=directory, check=False)
        except subprocess.TimeoutExpired:
            print("Slack CLI timed out — pasting a hand-provisioned token still works.")
            return
        if done.returncode != 0:
            print(f"Slack CLI install failed ({_cli_error(done.stderr or done.stdout)}) — "
                  "pasting a hand-provisioned token still works.")
            return
        match = re.search(r"App\s+ID:\s+([A-Z0-9]+)", done.stdout)
        if match:
            print(f"App installed — the token is at "
                  f"https://api.slack.com/apps/{match.group(1)}/oauth")


def _body(page) -> dict:
    """Response body as a dict, whether the SDK gave a dict or a SlackResponse."""
    data = getattr(page, "data", None)
    if isinstance(data, dict):
        return data
    if isinstance(page, dict):
        return page
    try:
        return dict(page)
    except Exception:
        return {}


def _items(page, key: str) -> list:
    body = _body(page)
    value = body.get(key) or []
    return list(value) if isinstance(value, list) else []


def _next_cursor(page) -> str:
    meta = _body(page).get("response_metadata") or {}
    if isinstance(meta, dict):
        return str(meta.get("next_cursor") or "")
    return ""


def _granted(response) -> set[str]:
    """Scopes this token actually holds, per the response headers."""
    headers = getattr(response, "headers", None) or {}
    raw = headers.get("x-oauth-scopes") or headers.get("X-OAuth-Scopes") or ""
    return {s.strip() for s in str(raw).split(",") if s.strip()}


def _reason(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            return str(response.get("error") or exc)
        except Exception:
            pass
    return str(exc)


def _float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _moment(seconds: float):
    from datetime import datetime, timezone
    if not seconds:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
