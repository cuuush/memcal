"""Proton Mail via the local Bridge (IMAP on 127.0.0.1:1143, STARTTLS).

Proton has no public mail API; Bridge is the supported way in, and it decrypts
locally, so nothing leaves the machine to read it.

Email is a sender problem, not a content problem: headers decide, and only what the
sender table lets through ever gets its body fetched. "AWS re:Invent night is
tomorrow" and "poker is tomorrow" are lexically identical, so nothing here reads the
subject line for temporal tokens.
"""

from __future__ import annotations

import email
import email.utils
import imaplib
import re
import socket
import sqlite3
import ssl
from datetime import timedelta
from email.header import decode_header, make_header
from typing import Callable

from .. import db, gate, identity, textclean, todos
from ..config import Config
from . import base
from .spec import Source, SourceError
from . import register

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 1143
# Folders worth reading. Sent is included because their own commitments live there.
# Sent is read first: who the user writes to is the strongest free signal for
# deciding what inbound mail deserves attention.
FOLDERS = ("Sent", "INBOX")
BODY_CHARS = 4000   # raw ceiling; textclean cuts this down further


class Bridge:
    def __init__(self, cfg: Config):
        self.host = cfg.secret("PROTON_BRIDGE_HOST", "protonhost") or DEFAULT_HOST
        self.port = int(cfg.secret("PROTON_BRIDGE_PORT", "protonport") or DEFAULT_PORT)
        self.user = cfg.secret("PROTON_BRIDGE_USER", "protonuser")
        self.password = cfg.secret("PROTON_BRIDGE_PASSWORD", "protonpassword")
        self.conn: imaplib.IMAP4 | None = None
        self.security = ""
        if not (self.user and self.password):
            raise base.HttpError(
                "no Proton Bridge credentials. Add PROTON_BRIDGE_USER and "
                "PROTON_BRIDGE_PASSWORD to memcal/.env (from Bridge → Mailbox details)."
            )

    def __enter__(self) -> "Bridge":
        context = ssl.create_default_context()
        # Bridge presents a self-signed certificate for localhost by design.
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        try:
            self.conn = imaplib.IMAP4_SSL(
                self.host, self.port, timeout=30, ssl_context=context)
            self.security = "SSL"
        except ssl.SSLError:
            self._connect_starttls(context)
        except (OSError, socket.error) as exc:
            raise base.HttpError(
                f"Proton Bridge is not accepting connections at {self.host}:{self.port}. "
                "Open Bridge and unlock it."
            ) from exc
        self._login()
        return self

    def _connect_starttls(self, context: ssl.SSLContext) -> None:
        try:
            self.conn = imaplib.IMAP4(self.host, self.port, timeout=30)
            self.conn.starttls(context)
            self.security = "STARTTLS"
        except (imaplib.IMAP4.error, OSError, socket.error) as exc:
            self._disconnect()
            raise base.HttpError(
                f"Proton Bridge answered at {self.host}:{self.port}, but secure IMAP "
                f"could not start ({exc}). Restart Bridge and try again."
            ) from exc

    def _login(self) -> None:
        try:
            self.conn.login(self.user, self.password)
        except imaplib.IMAP4.error as exc:
            self._disconnect()
            raise base.HttpError(
                "Proton Bridge is open, but rejected the saved mailbox credentials. "
                "Open Mailbox details and update "
                "PROTON_BRIDGE_USER and PROTON_BRIDGE_PASSWORD in memcal's .env with "
                "the IMAP credentials shown there."
            ) from exc
        except (OSError, socket.error) as exc:
            self._disconnect()
            raise base.HttpError(
                f"Proton Bridge closed the connection during login ({exc}). Restart "
                "Bridge and try again."
            ) from exc

    def __exit__(self, *_exc) -> None:
        self._disconnect()

    def _disconnect(self) -> None:
        if self.conn:
            try:
                self.conn.logout()
            except Exception:
                pass
            self.conn = None

    def folders(self) -> list[str]:
        typ, data = self.conn.list()
        if typ != "OK":
            return list(FOLDERS)
        names = []
        for raw in data:
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            match = re.search(r'"([^"]+)"\s*$', line) or re.search(r'(\S+)\s*$', line)
            if match:
                names.append(match.group(1))
        return names

    def select(self, folder: str) -> tuple[bool, str]:
        typ, data = self.conn.select(f'"{folder}"', readonly=True)
        if typ != "OK":
            return False, ""
        typ, response = self.conn.status(f'"{folder}"', "(UIDVALIDITY)")
        validity = ""
        if typ == "OK" and response:
            match = re.search(rb"UIDVALIDITY (\d+)", response[0])
            if match:
                validity = match.group(1).decode()
        return True, validity

    def uids_since(self, last_uid: int, since_days: int = 0) -> list[int]:
        """New UIDs, optionally floored at a date."""
        criteria = f"UID {last_uid + 1}:*"
        if since_days > 0:
            floor = (db.today() - timedelta(days=since_days)).strftime("%d-%b-%Y")
            criteria = f"({criteria} SINCE {floor})"
        typ, data = self.conn.uid("SEARCH", None, criteria)
        if typ != "OK" or not data or not data[0]:
            return []
        return [int(x) for x in data[0].split() if int(x) > last_uid]

    def headers(self, uid: int) -> email.message.Message | None:
        typ, data = self.conn.uid(
            "FETCH", str(uid),
            "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID LIST-ID "
            "LIST-UNSUBSCRIBE LIST-POST PRECEDENCE AUTO-SUBMITTED X-AUTOREPLY)])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            return None
        return email.message_from_bytes(data[0][1])

    def body(self, uid: int) -> str:
        typ, data = self.conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            return ""
        message = email.message_from_bytes(data[0][1])
        return extract_text(message)


def extract_text(message: email.message.Message) -> str:
    """First text/plain part, HTML stripped as a fallback. Only the first chunk."""
    parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                parts.append(_decode(part))
                break
        if not parts:
            for part in message.walk():
                if part.get_content_type() == "text/html":
                    parts.append(_strip_html(_decode(part)))
                    break
    else:
        raw = _decode(message)
        parts.append(_strip_html(raw) if message.get_content_type() == "text/html" else raw)
    text = "\n".join(parts)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:BODY_CHARS]


def _decode(part: email.message.Message) -> str:
    try:
        payload = part.get_payload(decode=True) or b""
        charset = part.get_content_charset() or "utf-8"
        return payload.decode(charset, "replace")
    except Exception:
        return ""


#: `<a href="X">Y</a>` → `Y (X)`. An anchor keeps the URL in an attribute and the words
#: in the body, so a stripper that keeps text keeps "Tutoring Meeting Room Link" and
#: throws away the meeting room. That is what happened to a tutoring appointment: the
#: reschedule mail carried the link, the calendar entry said "Online", and between
#: the two of them the store ended up with no way to attend.
#:
#: Only `http(s)`, and only when the label does not already contain the URL — otherwise
#: every plain-text link in a mail gets printed twice.
_ANCHOR = re.compile(
    r"""(?is)<a\b[^>]*\bhref\s*=\s*["']?(https?://[^"'\s>]+)["']?[^>]*>(.*?)</a>""")


def _keep_href(match: re.Match) -> str:
    url, label = match.group(1), re.sub(r"<[^>]+>", " ", match.group(2))
    label = " ".join(label.split())
    if not label or url in label:
        return f" {url} "
    return f" {label} ({url}) "


def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html)
    text = _ANCHOR.sub(_keep_href, text)
    text = re.sub(r"(?i)<br\s*/?>|</p>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'"))
    return re.sub(r"[ \t]{2,}", " ", text)


def header_str(message: email.message.Message, name: str) -> str:
    raw = message.get(name)
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


def ingest(conn: sqlite3.Connection, cfg: Config, *, limit: int = 300,
           folders: tuple[str, ...] = FOLDERS,
           progress: Callable[[str], None] | None = None) -> base.IngestReport:
    report = base.IngestReport.opened("email", cfg)
    progress = base.adapt_progress(progress)
    report.progress = progress
    try:
        with Bridge(cfg) as bridge:
            available = set(bridge.folders())
            for folder in folders:
                target = folder if folder in available else _match_folder(folder, available)
                if not target:
                    report.notes.append(f"no {folder} folder on the bridge")
                    continue
                ok, validity = bridge.select(target)
                if not ok:
                    report.notes.append(f"could not select {target}")
                    continue
                key = f"proton.{target}"
                stored = base.watermark(conn, key, "")
                last_validity, _, last_uid_raw = stored.partition(":")
                last_uid = int(last_uid_raw or 0)
                if validity and last_validity and validity != last_validity:
                    report.notes.append(f"{target}: UIDVALIDITY changed, resyncing")
                    last_uid = 0

                if progress:
                    progress(f"{target}: checking for new messages…", phase="checking")
                # A first run of this folder has no watermark, so the search would be
                # the whole mailbox. Floor it at the horizon the spool would apply
                # anyway, and say so in the report rather than doing it silently —
                # `IngestReport.too_old` exists because a backfill that quietly drops
                # most of what it read is the shape of bug that hides for weeks.
                first_run = last_uid == 0
                window = int(getattr(cfg, "email_backfill_days", 0) or
                             report.horizon_days) if first_run else 0
                if first_run and window:
                    report.notes.append(
                        f"{target}: first run — starting {window} days back")
                pending = bridge.uids_since(last_uid, since_days=window)
                uids = pending[:limit]
                if len(pending) > len(uids):
                    report.more = True
                if not uids:
                    if progress:
                        progress(f"{target}: up to date", phase="up to date")
                    continue
                if progress:
                    progress(f"{target}: 0/{len(uids)} this round · {len(pending)} waiting",
                             done=0, total=len(uids), phase="reading mail")
                highest = last_uid
                for index, uid in enumerate(uids, 1):
                    if progress:
                        progress(
                            f"{target}: {index}/{len(uids)} this round · "
                            f"{len(pending) - index + 1} waiting",
                            done=index, total=len(uids), phase="reading mail",
                        )
                    headers = bridge.headers(uid)
                    if headers is None:
                        continue
                    highest = max(highest, uid)
                    _handle_message(conn, cfg, report, bridge, uid, headers, target)
                base.set_watermark(conn, key, f"{validity}:{highest}")
                conn.commit()
                if progress:
                    progress(f"{target}: {len(uids)}/{len(uids)} this round",
                             done=len(uids), total=len(uids))
    except base.HttpError as exc:
        report.error = str(exc)
    return report


def _is_me(conn, cfg, address: str, folder: str) -> bool:
    """Learn the user's own alias addresses instead of assuming one login address."""
    login = (cfg.secret("PROTON_BRIDGE_USER", "protonuser") or "").lower()
    known = set(filter(None, (db.get_meta(conn, "email.my_addresses", "") or "").split(",")))
    if address == login or address in known:
        return True
    if folder.lower().endswith("sent"):
        known.add(address)
        db.set_meta(conn, "email.my_addresses", ",".join(sorted(known)))
        return True
    return False


def _record_correspondents(conn, headers) -> None:
    """Anyone the user emails is someone the user talks to — promote them past the sender gate."""
    for field in ("To", "Cc"):
        raw = headers.get(field)
        if not raw:
            continue
        for _name, address in email.utils.getaddresses([raw]):
            address = (address or "").strip().lower()
            if not address or "@" not in address:
                continue
            if gate.AUTOMATED_RE.search(address):
                continue
            if identity.sender_decision(conn, address) is None:
                identity.set_sender(conn, address, "process", "i-emailed-them")


def _match_folder(wanted: str, available: set[str]) -> str | None:
    for name in available:
        if name.lower().endswith(wanted.lower()):
            return name
    return None


def _handle_message(conn, cfg, report, bridge, uid, headers, folder) -> None:
    subject = header_str(headers, "Subject") or "(no subject)"
    from_raw = header_str(headers, "From")
    _name, address = email.utils.parseaddr(from_raw)
    address = (address or "unknown").lower()
    message_id = header_str(headers, "Message-ID") or f"{folder}:{uid}"
    date_header = headers.get("Date")
    try:
        ts = email.utils.parsedate_to_datetime(date_header).astimezone().isoformat(timespec="seconds")
    except Exception:
        ts = db.now()

    # Anything sent from the Sent folder is mine regardless of which alias sent it —
    # casey.owner@example.com and @icloud.com are the same person as the bridge login.
    from_me = _is_me(conn, cfg, address, folder)

    if from_me:
        # Their own mail is a commitment surface, not a sender-gate problem. It is also
        # where we learn who the user actually corresponds with.
        _record_correspondents(conn, headers)
        verdict = gate.gate_message(subject, from_me=True)
    else:
        header_map = {k: v for k, v in headers.items()}
        verdict = gate.gate_email(conn, address=address, subject=subject, headers=header_map)

    text = subject
    # A sender decision controls routine mail, but an open event-linked obligation may
    # make one otherwise filtered receipt newly relevant. Fetching a candidate body is
    # still free of model cost; it enters the spool only if it explicitly names both
    # the occasion and acquired tickets.
    watch_candidate = not verdict and todos.may_contain_event_proof(conn, subject)
    if verdict or watch_candidate:
        body = textclean.clean_email(bridge.body(uid))
        if body:
            text = f"{subject}\n\n{body}"
    if watch_candidate and todos.matching_event_proofs(conn, text):
        verdict = gate.Verdict(True, "event-todo-proof")

    # A thread is the other party, never me: mail I sent belongs in the recipient's
    # bundle, or it forms a bundle of my own address talking to itself.
    counterpart = address
    if from_me:
        recipients = email.utils.getaddresses([headers.get("To") or ""])
        counterpart = next((a.lower() for _n, a in recipients if a and "@" in a), address)

    base.deliver(
        conn, report,
        stream="email",
        external_id=message_id,
        ts=ts,
        text=text,
        thread=counterpart,
        handle=None if from_me else address,
        person=None,
        from_me=from_me,
        counterpart=counterpart,
        meta={"folder": folder, "uid": uid, "subject": subject, "from": from_raw},
        verdict=verdict,
    )


@register
class ProtonSource(Source):
    name = "email"
    description = "Proton Mail via the local Bridge (IMAP, STARTTLS)"
    secrets = ("PROTON_BRIDGE_USER", "PROTON_BRIDGE_PASSWORD")
    order = 40

    def fetch(self, conn, cfg, report, limit):
        from .bluebubbles import _absorb
        _absorb(report, ingest(conn, cfg, limit=limit, progress=report.progress))

    def check(self, cfg):
        ok, message = Source.check(self, cfg)
        if not ok:
            return ok, message
        try:
            with Bridge(cfg) as bridge:
                return True, f"bridge up over {bridge.security}, {len(bridge.folders())} folders"
        except base.HttpError as exc:
            return False, str(exc)[:90]
