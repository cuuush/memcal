"""Signal DMs and groups via signal-cli, linked as a device.

signal-cli links the same way Signal Desktop does — no token to leak, nothing leaves the
machine. It's a JVM program, not a pip install: `brew install signal-cli`, then
`signal-cli link -n memcal`. This module shells out to it and parses its JSON.

A StreamSource, not a PolledSource: Signal's server holds an undelivered-message queue
rather than per-chat history, so there's nothing to enumerate or page. See `receive` for
why that makes this the one source here that can't safely replay.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone

from .. import textclean
from ..config import Config
from . import base, register
from .polled import Conversation, Message, SourceError, StreamSource, link_me

#: How long `receive` waits for the server to go quiet before returning.
RECEIVE_TIMEOUT = 10
#: Ceiling on one drain, so a long silence cannot block a nightly pass indefinitely.
RUN_TIMEOUT = 180


class SignalCli:
    """Thin wrapper over the signal-cli binary."""

    def __init__(self, binary: str, account: str):
        self.binary = binary
        self.account = account
        self._groups: dict[str, str] = {}

    def _run(self, *args: str, timeout: int = 60) -> str:
        command = [self.binary, "--output=json"]
        if self.account:
            command += ["-a", self.account]
        command += list(args)
        try:
            done = subprocess.run(command, capture_output=True, text=True,
                                  timeout=timeout, check=False)
        except FileNotFoundError as exc:
            raise SourceError(f"signal-cli not found at {self.binary}") from exc
        except subprocess.TimeoutExpired as exc:
            raise SourceError(f"signal-cli timed out after {timeout}s") from exc
        if done.returncode != 0:
            detail = (done.stderr or done.stdout or "").strip().splitlines()
            raise SourceError(
                f"signal-cli failed: {detail[-1][:160] if detail else done.returncode}")
        return done.stdout

    def accounts(self) -> list[str]:
        try:
            raw = json.loads(self._run("listAccounts") or "[]")
        except ValueError:
            return []
        return [str(a.get("number") or "") for a in raw if isinstance(a, dict)]

    def groups(self) -> dict[str, str]:
        """Map group id to name, so group threads aren't named by a base64 blob."""
        if self._groups:
            return self._groups
        try:
            raw = json.loads(self._run("listGroups", "-d") or "[]")
        except (ValueError, SourceError):
            return self._groups
        for group in raw if isinstance(raw, list) else []:
            group_id = str(group.get("id") or "")
            if group_id:
                self._groups[group_id] = str(group.get("name") or "").strip() or group_id
        return self._groups

    def receive(self) -> list[dict]:
        """Drain the queue. One envelope per line of output.

        Destructive: this acks messages off the server queue, so a crash before they are
        archived loses them rather than replaying them. The ack happens inside
        signal-cli, so the watermark is a high-water mark for reporting, not a cursor
        that can rewind. There's no backfill either — Signal keeps no server-side
        archive, so history from before the link is simply unavailable.
        """
        raw = self._run("receive", "--timeout", str(RECEIVE_TIMEOUT),
                        "--ignore-attachments", timeout=RUN_TIMEOUT)
        out = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


def _text(body: str, attachments) -> str:
    words = str(body or "").strip()
    if words:
        cleaned = textclean.spoken_text(words)
        if cleaned:
            return textclean.clean_message(cleaned)
    kinds = sorted({str(a.get("contentType") or "attachment").split("/")[0]
                    for a in attachments or []})
    return f"[{', '.join(kinds)}]" if kinds else ""


def _iso(millis) -> str:
    from .. import db
    try:
        return (datetime.fromtimestamp(int(millis) / 1000, tz=timezone.utc)
                .astimezone().isoformat(timespec="seconds"))
    except (TypeError, ValueError, OverflowError, OSError):
        return db.now()


@register
class SignalSource(StreamSource):
    name = "signal"
    description = "Signal DMs and groups (signal-cli, linked device)"
    secrets = ("SIGNAL_ACCOUNT",)
    order = 34
    cursor_key = "received"
    handle_prefix = "signal"

    def __init__(self) -> None:
        self._me = ""
        self._groups: dict[str, str] = {}

    # ------------------------------------------------------------- connection --
    def _cli(self, cfg: Config) -> SignalCli:
        binary = cfg.secret("SIGNAL_CLI", "signalcli") or shutil.which("signal-cli")
        if not binary:
            raise SourceError(
                "signal-cli not found — `brew install signal-cli`, then "
                "`signal-cli link -n memcal` and scan the QR code from your phone.")
        account = (cfg.secret("SIGNAL_ACCOUNT", "signalaccount") or "").strip()
        return SignalCli(binary, account)

    def connect(self, cfg: Config) -> SignalCli:
        client = self._cli(cfg)
        if not client.account:
            linked = client.accounts()
            if not linked:
                raise SourceError(
                    "signal-cli has no linked account — run `memcal login signal` "
                    "and scan the QR code from Signal on your phone.")
            if len(linked) > 1:
                raise SourceError(
                    f"signal-cli has several accounts ({', '.join(linked)}) — set "
                    f"`signal_account=` in memcal/.env to choose one.")
            client.account = linked[0]
        self._me = client.account
        self._groups = client.groups()
        return client

    def identify(self, conn: sqlite3.Connection, client,
                 report: base.IngestReport) -> str:
        return link_me(conn, report, "signal", self._me, name="me")

    # ------------------------------------------------------------------ setup --
    def setup(self, cfg: Config) -> tuple[bool, str]:
        """Link memcal as a Signal device: prints the QR code, you scan it."""
        from .polled import ask, credential_is_set, save_credential
        client = self._cli(cfg)  # raises SourceError with install help if missing
        print("Linking memcal to Signal as a new device (like Signal Desktop) — "
              "nothing leaves your machine.")
        print("Scan the QR code below from Signal on your phone: "
              "Settings → Linked devices → +")
        try:
            # No capture: the QR code must render live in your terminal.
            done = subprocess.run([client.binary, "link", "-n", "memcal"],
                                  check=False)
        except KeyboardInterrupt:
            return False, "cancelled — re-run `memcal login signal` when ready"
        if done.returncode != 0:
            return False, "linking failed or was cancelled — re-run `memcal login signal`"
        linked = client.accounts()
        if len(linked) > 1 and not credential_is_set(cfg, "SIGNAL_ACCOUNT"):
            print("Several Signal accounts are linked:")
            for index, number in enumerate(linked, start=1):
                print(f"  {index}. {number}")
            choice = ask("Use which one for memcal? [1]: ")
            if choice.isdigit() and 1 <= int(choice) <= len(linked):
                save_credential(cfg, "SIGNAL_ACCOUNT", linked[int(choice) - 1])
                return True, (f"device linked, using {linked[int(choice) - 1]} — "
                               "`memcal sources` should now show it")
            save_credential(cfg, "SIGNAL_ACCOUNT", linked[0])
        return True, "device linked — `memcal sources` should now show your number"

    # ------------------------------------------------------------------ shape --
    def stream(self, client: SignalCli, since: str | None, limit: int):
        """One drain. `since` is ignored: the server queue is the cursor.

        `limit` is ignored on purpose: `receive()` acks everything off the server
        queue, so slicing here would drop already-acked messages with no replay.
        Process the whole drain; `StreamSource.fetch` still sets `report.more`
        when the drain hits the budget.
        """
        return client.receive()

    def normalize(self, raw: dict):
        envelope = raw.get("envelope") or {}
        sync = ((envelope.get("syncMessage") or {}).get("sentMessage")) or {}
        data = envelope.get("dataMessage") or {}
        # A sync message is something you sent from another device; a data message is
        # something someone sent you. Anything else is a receipt or a typing notice.
        body = sync or data
        if not body:
            return None
        from_me = bool(sync)

        group_id = str((body.get("groupInfo") or {}).get("groupId") or "")
        if group_id:
            conversation = Conversation(
                id=f"group:{group_id}",
                name=self._groups.get(group_id) or f"Signal group {group_id[:8]}",
                is_group=True,
            )
        else:
            # Name a DM for the other person — sender if received, destination if sent.
            other = str(sync.get("destination") or envelope.get("sourceNumber")
                        or envelope.get("source") or "")
            if not other:
                return None
            conversation = Conversation(
                id=f"dm:{other}",
                name=str(envelope.get("sourceName") or "").strip() or other,
                is_group=False,
            )

        stamp = body.get("timestamp") or envelope.get("timestamp")
        author = "" if from_me else str(envelope.get("sourceNumber")
                                        or envelope.get("source") or "")
        message = Message(
            # No message id in Signal; the send timestamp is also its dedupe key.
            external_id=f"{conversation.id}:{stamp}",
            ts=_iso(stamp),
            text=_text(body.get("message"), body.get("attachments")),
            order=float(stamp or 0),
            author_id=author,
            author_name=str(envelope.get("sourceName") or "").strip() or None,
            from_me=from_me,
            cursor=str(stamp or ""),
            meta={"group": conversation.is_group},
        )
        return conversation, message

    # ------------------------------------------------------------------ check --
    def check(self, cfg: Config) -> tuple[bool, str]:
        try:
            client = self._cli(cfg)
        except SourceError as exc:
            return False, str(exc)[:120]
        try:
            linked = client.accounts()
        except SourceError as exc:
            return False, str(exc)[:80]
        if not linked:
            return False, "no linked account — run `memcal login signal`"
        chosen = client.account or linked[0]
        groups = len(client.groups())
        return True, f"linked as {chosen}, {groups} group(s)"
