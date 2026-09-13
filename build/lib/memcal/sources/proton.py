"""Proton Mail via the local Bridge (IMAP on 127.0.0.1:1143, STARTTLS).

Proton has no public mail API; Bridge is the supported path and decrypts locally.

Headers decide priority, never inclusion. Every in-scope message has its body
fetched and archived before relevance is assigned.
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
BODY_CHARS = 4000   # what is fetched from the message itself
#: What is kept in the archive. Larger than the ~900 characters a bundle wants, because
#: the archive is the record and the bundle is a view of it: a booking reference below
#: the fold has to survive in the row even when it never reaches a prompt.
ARCHIVE_BODY_CHARS = 2500


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
            "(BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID "
            "IN-REPLY-TO REFERENCES LIST-ID "
            "LIST-UNSUBSCRIBE LIST-POST PRECEDENCE AUTO-SUBMITTED X-AUTOREPLY)])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            return None
        return email.message_from_bytes(data[0][1])

    def message(self, uid: int) -> email.message.Message | None:
        """The whole message, so its identity and its body come from one fetch."""
        typ, data = self.conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            return None
        return email.message_from_bytes(data[0][1])

    def body(self, uid: int) -> str:
        message = self.message(uid)
        return extract_text(message) if message is not None else ""

    def find(self, message_id: str) -> int | None:
        """The UID currently holding this `Message-ID`, if this folder still has it.

        A UID is only meaningful beside the UIDVALIDITY it was issued under. After a
        resync the same number addresses a different message, so a stored UID is a hint
        about where to look and never proof of what is there.
        """
        wanted = _bare_id(message_id)
        if not wanted:
            return None
        typ, data = self.conn.uid("SEARCH", None, "HEADER", "Message-ID", f"<{wanted}>")
        if typ != "OK" or not data or not data[0]:
            return None
        uids = [int(part) for part in data[0].split() if part.isdigit()]
        return uids[-1] if uids else None


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


#: `<a href="X">Y</a>` → `Y (X)`. Preserves link targets that text-only
#: stripping would drop.
#:
#: Only `http(s)`, and only when the label does not already contain the URL.
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
                # A first run has no watermark; floor the search at the spool
                # horizon and report it.
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
                    _handle_message(conn, cfg, report, bridge, uid, headers, target,
                                    uidvalidity=validity)
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


def _bare_id(raw: str) -> str:
    """`<abc@host>` → `abc@host`. Angle brackets are notation, not part of the id."""
    return (raw or "").strip().strip("<>").strip()


def archived_id(conn: sqlite3.Connection, message_id: str) -> str:
    """The `external_id` this message is already filed under, or the bare form.

    Mail archived before ids were normalised is keyed with the angle brackets the header
    carries. Writing the bare form for the same message would insert a second row for it
    on the next collection — the same mail twice, under two spellings, each with its own
    evidence links. So the stored spelling wins whenever one exists, and nothing already
    written is rewritten: `external_id` is what callers, traces and prior runs recorded,
    and normalising it in place would break every reference to it for the sake of tidiness.
    """
    bare = _bare_id(message_id)
    if not bare:
        return bare
    row = conn.execute(
        "SELECT external_id FROM archive WHERE stream = 'email' AND external_id IN (?,?)"
        " ORDER BY id LIMIT 1", (bare, f"<{bare}>")).fetchone()
    return str(row["external_id"]) if row else bare


def message_ids(headers: email.message.Message) -> tuple[str, str, list[str]]:
    """This message's id, the one it answers, and the chain above it.

    All three are preserved because the reply relationship is the only thing that
    establishes one conversation. The same sender is not: an advisor's mail about a
    trust and their mail about tax forms are two conversations, and reading them as one
    sender bucket is how a booking reference ends up filed under a year of receipts.
    """
    own = _bare_id(header_str(headers, "Message-ID"))
    parent = _bare_id(header_str(headers, "In-Reply-To"))
    chain = [_bare_id(part) for part in
             re.split(r"\s+", header_str(headers, "References") or "") if part.strip()]
    return own, parent, [ref for ref in chain if ref]


def conversation_key(conn, *, own: str, parent: str, chain: list[str],
                     fallback: str) -> str:
    """The root of this conversation, from real reply relationships where they exist.

    Walks to the earliest id already archived, so the third message of a thread joins
    the first two rather than starting a third conversation. Headers go missing
    constantly — forwarded mail, gateways, mailing lists that rewrite them — so a
    message with none of them keeps its own id and stands alone, which is right: it has
    said nothing to connect it to anything.
    """
    for candidate in [*chain, parent]:
        if not candidate:
            continue
        # Both spellings, for the same reason `archived_id` exists: an ancestor archived
        # before ids were normalised is filed with its angle brackets, and missing it
        # would start a second conversation halfway through the first one.
        row = conn.execute(
            "SELECT thread FROM archive WHERE stream = 'email'"
            " AND external_id IN (?,?) ORDER BY id LIMIT 1",
            (candidate, f"<{candidate}>")).fetchone()
        if row and row["thread"]:
            return str(row["thread"])
    # No archived ancestor. The chain's first entry is still the root the sender named.
    for candidate in [*chain, parent]:
        if candidate:
            return candidate
    return own or fallback


def _handle_message(conn, cfg, report, bridge, uid, headers, folder,
                    uidvalidity: str = "") -> None:
    subject = header_str(headers, "Subject") or "(no subject)"
    from_raw = header_str(headers, "From")
    _name, address = email.utils.parseaddr(from_raw)
    address = (address or "unknown").lower()
    own_id, parent_id, chain = message_ids(headers)
    message_id = archived_id(conn, own_id) if own_id else f"{folder}:{uid}"
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

    # Fetch bodies before settling relevance so automatic verdicts stay
    # reviewable. User blocks stop the fetch entirely: blocked bodies are
    # never decrypted into the archive.
    if verdict.excluded:
        # Record the exclusion distinctly; backfill repairs only unfetched rows.
        text, body_meta = subject, {"body_excluded": verdict.reason}
    else:
        text, body_meta = _with_body(bridge, uid, subject)
    if not verdict and todos.may_contain_event_proof(conn, subject) \
            and todos.matching_event_proofs(conn, text):
        verdict = gate.Verdict(True, "event-todo-proof")

    # A thread is a conversation, not a correspondent. Mail the user sent belongs with the
    # exchange it is part of; `counterpart` is what still carries "who this is with".
    counterpart = address
    if from_me:
        recipients = email.utils.getaddresses([headers.get("To") or ""])
        counterpart = next((a.lower() for _n, a in recipients if a and "@" in a), address)
    thread = conversation_key(conn, own=message_id, parent=parent_id, chain=chain,
                              fallback=counterpart)

    base.deliver(
        conn, report,
        stream="email",
        external_id=message_id,
        ts=ts,
        text=text,
        thread=thread,
        handle=None if from_me else address,
        person=None,
        from_me=from_me,
        counterpart=counterpart,
        meta={"folder": folder, "uid": uid, "uidvalidity": uidvalidity,
              "subject": subject, "from": from_raw,
              "message_id": message_id, "in_reply_to": parent_id,
              "references": chain, **body_meta},
        verdict=verdict,
    )


def _body_of(message: email.message.Message, subject: str) -> tuple[str, dict]:
    """Subject plus body for a message already in hand. See `_with_body` for the record."""
    try:
        return _shape_body(extract_text(message) or "", subject)
    except Exception as exc:                        # noqa: BLE001 — recorded, not fatal
        return subject, {"body_error": f"{type(exc).__name__}: {exc}"[:200],
                         "body_chars": 0}


def _with_body(bridge, uid, subject: str) -> tuple[str, dict]:
    """Subject plus body, with a record of truncation or fetch failure."""
    try:
        raw = bridge.body(uid) or ""
    except Exception as exc:                        # noqa: BLE001 — recorded, not fatal
        return subject, {"body_error": f"{type(exc).__name__}: {exc}"[:200],
                         "body_chars": 0}
    return _shape_body(raw, subject)


def _shape_body(raw: str, subject: str) -> tuple[str, dict]:
    """What is kept, and what was lost keeping it."""
    if not raw.strip():
        return subject, {"body_chars": 0}
    body = textclean.clean_email(raw, limit=ARCHIVE_BODY_CHARS)
    meta = {"body_chars": len(raw), "kept_chars": len(body)}
    if len(body) < len(raw) or len(raw) >= BODY_CHARS:
        # Named plainly so a reader knows there is more behind this row, and so
        # `proton.backfill_bodies` knows which rows have something left to recover.
        meta["body_truncated"] = True
    return (f"{subject}\n\n{body}" if body else subject), meta


# ------------------------------------------------------------------- backfill --

def recoverable(conn: sqlite3.Connection, *, limit: int = 0) -> list[sqlite3.Row]:
    """Archived mail whose body is missing or short, newest first.

    Two populations, and they are not the same. Rows written before mail bodies were
    fetched by default hold a subject and nothing else; rows whose fetch failed say so
    in `meta.body_error`. Both are recoverable from the mailbox and neither can be
    repaired by a schema migration — a subject-only row does not become evidence because
    a column was added, and pretending otherwise would put un-read mail into the model's
    input as though it had been read.
    """
    rows = [row for row in conn.execute(
        # `gate_reason` as well as the meta marker, because rows collected before the
        # marker existed carry no trace of it — and a sender the user has since blocked
        # must not have their mail opened by a repair pass that only reads what was
        # written down at the time.
        "SELECT * FROM archive WHERE stream = 'email'"
        " AND coalesce(gate_reason, '') NOT LIKE 'blocked:%' ORDER BY ts DESC")
        if _needs_body(db.jload(row["meta"], {}))]
    return rows[:limit] if limit else rows


def _needs_body(meta: dict) -> bool:
    # A sender the user blocked. Deliberately withheld, so it is not a gap to repair —
    # backfilling it would walk straight around the exclusion `_handle_message` honours.
    if meta.get("body_excluded"):
        return False
    if meta.get("body_error"):
        return True
    # Written before bodies were fetched by default: no record either way.
    return "body_chars" not in meta and bool(meta.get("uid"))


def backfill_bodies(conn: sqlite3.Connection, cfg: Config, *, limit: int = 200,
                    dry_run: bool = True,
                    progress: Callable[[str], None] | None = None) -> dict:
    """Re-fetch the bodies of mail archived without one. Resumable, and dry by default.

    Updates the existing archive row in place, matched on its `Message-ID`, so running
    this twice cannot produce a second copy of anybody's mail. It stops at `limit` and
    leaves the rest for the next run; nothing here deletes, moves or marks anything in
    the mailbox.
    """
    pending = recoverable(conn, limit=limit)
    report = {"candidates": len(recoverable(conn)), "attempted": len(pending),
              "updated": 0, "failed": 0, "unidentified": 0, "relocated": 0,
              "dry_run": bool(dry_run), "errors": []}
    if dry_run or not pending:
        return report
    with Bridge(cfg) as bridge:
        current, validity = "", ""
        for row in pending:
            meta = db.jload(row["meta"], {})
            folder = str(meta.get("folder") or "INBOX")
            if folder != current:
                # Into locals first. Assigning `validity` from a *failed* select wiped
                # the UIDVALIDITY of the folder still selected, and `current` was left
                # pointing at it — so every later row in that folder took the
                # already-selected path with an empty validity, skipped the identity
                # check in `_locate`, and had its own `uidvalidity` overwritten with the
                # blank. One unreadable folder quietly destroyed the evidence the next
                # run depends on.
                ok, found_validity = bridge.select(folder)
                if not ok:
                    report["errors"].append(f"could not select {folder}")
                    continue
                current, validity = folder, found_validity
            if progress:
                progress(f"{row['ts'][:10]} {meta.get('subject', '')[:40]}")
            found = _locate(bridge, row, meta, validity)
            if found is None:
                # The one outcome that must never be a write. A UID is meaningful only
                # beside the UIDVALIDITY it was issued under, and after a resync the same
                # number addresses somebody else's mail — so a body fetched without
                # proving identity is a body attached to the wrong message. Leave the row
                # exactly as it was and say the message could not be found.
                report["unidentified"] += 1
                report["errors"].append(
                    f"could not identify {str(meta.get('subject') or row['external_id'])[:60]}")
                continue
            uid, message = found
            if uid != meta.get("uid"):
                report["relocated"] += 1
            text, body_meta = _body_of(message, str(meta.get("subject") or ""))
            stored = db.jdump({**meta, **body_meta, "uid": uid,
                               "uidvalidity": validity,
                               "body_error": body_meta.get("body_error")})
            if body_meta.get("body_error"):
                # Failed, and only failed. It used to also count as updated and to write
                # `text` back as the bare subject, so a run where every fetch broke
                # reported itself fully updated and the counts did not sum to
                # `attempted`. The meta is still worth keeping — it carries the error and
                # any relocated UID — but the row learned nothing, so its text stands.
                report["failed"] += 1
                report["errors"].append(str(body_meta["body_error"])[:120])
                conn.execute("UPDATE archive SET meta = ? WHERE id = ?",
                             (stored, row["id"]))
                continue
            conn.execute("UPDATE archive SET text = ?, meta = ? WHERE id = ?",
                         (text, stored, row["id"]))
            report["updated"] += 1
        conn.commit()
    return report


def _locate(bridge: "Bridge", row, meta: dict,
            validity: str) -> tuple[int, email.message.Message] | None:
    """Find this archive row's message in the mailbox, or return None.

    The stored UID is a hint. It is trusted only when the folder still reports the
    UIDVALIDITY it was issued under *and* the message sitting there carries the same
    `Message-ID`; otherwise the message is searched for by that id and, if found, its new
    UID is adopted. Two independent checks because either one alone has been wrong: a
    server may keep UIDVALIDITY across a rebuild, and a moved message keeps its id while
    losing its number.
    """
    wanted = _bare_id(str(row["external_id"] or "")) or _bare_id(str(meta.get("message_id") or ""))
    stored_validity = str(meta.get("uidvalidity") or "")
    uid = meta.get("uid")
    if uid and (not stored_validity or not validity or stored_validity == validity):
        message = _peek(bridge, int(uid))
        if message is not None and (
                not wanted or _bare_id(header_str(message, "Message-ID")) == wanted):
            return int(uid), message
    if not wanted:
        return None
    try:
        moved = bridge.find(wanted)
    except Exception:                               # noqa: BLE001 — reported as unfound
        return None
    if moved is None:
        return None
    message = _peek(bridge, moved)
    if message is None or _bare_id(header_str(message, "Message-ID")) != wanted:
        return None
    return moved, message


def _peek(bridge: "Bridge", uid: int) -> email.message.Message | None:
    try:
        return bridge.message(uid)
    except Exception:                               # noqa: BLE001 — reported as unfound
        return None


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
