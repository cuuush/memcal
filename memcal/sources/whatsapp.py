"""WhatsApp — reads the macOS app's ChatStorage.sqlite read-only."""

from __future__ import annotations

import glob
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import db, identity, textclean, threads
from ..config import Config
from . import base, register
from .spec import Source, SourceError

# The group container is readable without Full Disk Access — unlike chat.db, WhatsApp
# does not sandbox it.
STORE_GLOBS = (
    "~/Library/Group Containers/*.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite",
    "~/Library/Group Containers/*.net.whatsapp.*.shared/ChatStorage.sqlite",
    "~/Library/Containers/net.whatsapp.WhatsApp/Data/Library/Application Support/"
    "WhatsApp/ChatStorage.sqlite",
)
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

# ZWAMESSAGE is one row per message; ZWACHATSESSION names the conversation; for group
# chats the individual sender hangs off ZWAGROUPMEMBER, because ZFROMJID is the group.
QUERY = """
SELECT m.Z_PK                      AS rowid,
       m.ZTEXT                     AS text,
       m.ZMESSAGEDATE              AS date,
       m.ZISFROMME                 AS from_me,
       m.ZFROMJID                  AS from_jid,
       m.ZMESSAGETYPE              AS kind,
       s.ZCONTACTJID               AS chat_jid,
       s.ZPARTNERNAME              AS chat_name,
       s.ZSESSIONTYPE              AS session_type,
       g.ZMEMBERJID                AS member_jid,
       g.ZCONTACTNAME              AS member_name
FROM ZWAMESSAGE m
LEFT JOIN ZWACHATSESSION s ON s.Z_PK = m.ZCHATSESSION
LEFT JOIN ZWAGROUPMEMBER g ON g.Z_PK = m.ZGROUPMEMBER
WHERE m.Z_PK > ?
ORDER BY m.Z_PK
LIMIT ?
"""

# ZMESSAGETYPE 0 is text. The rest are media, calls and system notices; they carry no
# text and would only add "[image]" noise to a bundle.
TEXT_MESSAGE = 0


def store_path(explicit: str | None = None) -> Path | None:
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.exists() else None
    for pattern in STORE_GLOBS:
        for hit in sorted(glob.glob(os.path.expanduser(pattern))):
            return Path(hit)
    return None


def to_iso(value) -> str:
    """ZMESSAGEDATE is seconds since 2001-01-01, sometimes as a float."""
    try:
        seconds = float(value or 0)
    except (TypeError, ValueError):
        return db.now()
    if seconds <= 0:
        return db.now()
    return (APPLE_EPOCH + timedelta(seconds=seconds)).astimezone().isoformat(timespec="seconds")


#: The JID domains whose local part really is a phone number. Everything else merely
#: looks like one — see `phone_of`.
PHONE_DOMAINS = frozenset({"s.whatsapp.net", "c.us"})

#: WhatsApp's Linked ID domain. A `@lid` is an opaque per-user identifier the app
#: hands out so group members do not expose their number to everyone in the group.
LID_DOMAIN = "lid"

#: Bot domains are opaque identifiers, not phone-number namespaces.
BOT_DOMAIN = "bot"

#: Short enough to admit every national numbering plan, long enough to reject `0@`,
#: which is WhatsApp's own account and was becoming the handle `+0`.
MIN_PHONE_DIGITS = 7


def domain_of(jid: str | None) -> str:
    return str(jid or "").rsplit("@", 1)[-1].lower() if "@" in str(jid or "") else ""


def phone_of(jid: str | None) -> str | None:
    """Normalize phone JIDs; reject opaque and bot domains."""
    if domain_of(jid) not in PHONE_DOMAINS:
        return None
    local = str(jid).split("@", 1)[0]
    if not local.isdigit() or len(local) < MIN_PHONE_DIGITS:
        return None            # a linked-device suffix, or `0@` — WhatsApp itself
    return identity.normalize("+" + local)


def handle_of(jid: str | None) -> str | None:
    """The handle for one sender, honest about which namespace it came out of.

    A `@lid` gets `whatsapp:lid:<id>`, which is an opaque id that says so. That is
    worth more than it sounds: it cannot be mistaken for a phone number by anything
    downstream, `identity.normalize` leaves it alone the way it leaves `groupme:123`
    alone, and the 91 people behind them show up in the name-this-person queue as
    unresolved instead of as wrong.
    """
    if not jid:
        return None
    phone = phone_of(jid)
    if phone:
        return phone
    domain = domain_of(jid)
    if domain in (LID_DOMAIN, BOT_DOMAIN):
        return identity.normalize(f"whatsapp:{domain}:{str(jid).split('@', 1)[0]}")
    return str(jid) or None


def push_names(src: sqlite3.Connection) -> dict[str, str]:
    """JID to the name its owner chose, from WhatsApp's own profile cache.

    This is the table that makes `@lid` recoverable rather than merely honest. The
    group-member row has no usable name — `ZCONTACTNAME` is empty for every row in the
    store and `ZFIRSTNAME` holds a protobuf blob, not a first name — so without this
    join a linked id has nothing to be called. With it, 76 of the 120 people who have
    ever spoken in a group get one.

    Missing on an older store, which is not an error: no names, same as today.
    """
    try:
        rows = src.execute(
            "SELECT ZJID AS jid, ZPUSHNAME AS name FROM ZWAPROFILEPUSHNAME"
            " WHERE ZPUSHNAME IS NOT NULL AND ZPUSHNAME <> ''").fetchall()
    except sqlite3.Error:
        return {}
    return {str(row["jid"]): str(row["name"]) for row in rows if row["jid"]}


#: Every table that stores a handle. A rename that misses one leaves the store holding
#: both spellings, which is the bug being repaired wearing a different hat.
HANDLE_TABLES = ("archive", "handles", "unresolved", "thread_members",
                 "thread_member_names")


#: The domains whose all-digit local part an older `phone_of` minted into a phone
#: number. Both were found the same way and neither is a phone namespace.
MINTED_DOMAINS = (LID_DOMAIN, BOT_DOMAIN)


def opaque_jids(src: sqlite3.Connection) -> dict[str, str]:
    """Every opaque JID this store mentions, as `local part -> domain`.

    A member row, a chat session and a message each carry a JID and none of them is a
    superset of the others — a DM partner never appears in ZWAGROUPMEMBER, and a member
    who has left still owns their old messages.
    """
    found: dict[str, str] = {}
    for table, column in (("ZWAGROUPMEMBER", "ZMEMBERJID"),
                          ("ZWACHATSESSION", "ZCONTACTJID"),
                          ("ZWAMESSAGE", "ZFROMJID")):
        for domain in MINTED_DOMAINS:
            try:
                rows = src.execute(
                    f"SELECT DISTINCT {column} AS jid FROM {table}"
                    f" WHERE {column} LIKE '%@{domain}'").fetchall()
            except sqlite3.Error:
                continue
            for row in rows:
                local = str(row["jid"] or "").split("@", 1)[0]
                if local:
                    found[local] = domain
    return found


def repair_minted_handles(conn: sqlite3.Connection, src: sqlite3.Connection) -> int:
    """Rewrite the fake phone numbers an older build minted out of opaque JIDs."""
    opaque = opaque_jids(src)
    if not opaque:
        return 0
    renames = {identity.normalize("+" + local): f"whatsapp:{domain}:{local}"
               for local, domain in opaque.items()}
    moved = 0
    for old, new in renames.items():
        touched = 0
        for table in HANDLE_TABLES:
            # OR REPLACE: the new spelling may already be present from a later run of
            # the fixed code, and the two rows are the same person either way.
            cur = conn.execute(
                f"UPDATE OR REPLACE {table} SET handle = ? WHERE handle = ?", (new, old))
            touched += cur.rowcount or 0
        moved += touched
    if moved:
        conn.commit()
    return moved


def adopt_push_names(conn: sqlite3.Connection, src: sqlite3.Connection) -> int:
    """Give the handles already in the store the names WhatsApp has since learned."""
    named = 0
    for jid, name in push_names(src).items():
        handle = handle_of(jid)
        if not handle or identity.resolve(conn, handle):
            continue
        seen = conn.execute(
            "SELECT 1 FROM archive WHERE stream = 'whatsapp' AND handle = ? LIMIT 1",
            (handle,)).fetchone()
        if not seen:
            continue
        person = (identity.link_by_name(conn, handle, name, source="whatsapp")
                  or identity.adopt_seen_name(conn, handle, name,
                                              source="whatsapp:profile"))
        if not person:
            continue
        conn.execute(
            "UPDATE archive SET person = ?"
            "  WHERE stream = 'whatsapp' AND handle = ? AND from_me = 0"
            "    AND coalesce(person, '') != ?", (person, handle, person))
        named += 1
    if named:
        conn.commit()
    return named


def is_group(row) -> bool:
    """Session type 1 is a group; a group JID also ends @g.us. Belt and braces —
    older stores set one and not the other."""
    if str(row["chat_jid"] or "").endswith("@g.us"):
        return True
    try:
        return int(row["session_type"] or 0) == 1
    except (TypeError, ValueError):
        return False


def ingest(conn: sqlite3.Connection, cfg: Config, *, limit: int = 2000,
           db_path: str | None = None) -> base.IngestReport:
    report = base.IngestReport.opened("whatsapp", cfg)
    path = store_path(db_path or cfg.secret("WHATSAPP_DB", "whatsappdb"))
    if not path:
        report.error = ("no WhatsApp store found. Install WhatsApp from the Mac App Store "
                        "and sign in; its history lands in ~/Library/Group Containers/")
        return report

    try:
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        report.error = f"could not open {path.name}: {exc}"
        return report

    repaired = repair_minted_handles(conn, src)
    if repaired:
        report.notes.append(
            f"rewrote {repaired} rows whose opaque id was stored as a phone number")
    adopted = adopt_push_names(conn, src)
    if adopted:
        report.notes.append(f"named {adopted} handle(s) from WhatsApp's profile cache")

    watermark = int(base.watermark(conn, "whatsapp.rowid", "0") or 0)
    tier = identity.top_tier(conn)
    pushed = push_names(src)
    highest = watermark
    try:
        rows = src.execute(QUERY, (watermark, limit)).fetchall()
    except sqlite3.Error as exc:
        src.close()
        report.error = f"unexpected WhatsApp schema ({exc}) — the app may have updated"
        return report

    for row in rows:
        highest = max(highest, int(row["rowid"]))
        if int(row["kind"] or 0) != TEXT_MESSAGE:
            continue
        text = textclean.clean_message((row["text"] or "").strip())
        if not text:
            continue
        from_me = bool(row["from_me"])
        group = is_group(row)
        # In a group the sender is the group member; in a DM it is the chat itself.
        sender_jid = row["member_jid"] if group else (row["from_jid"] or row["chat_jid"])
        handle = handle_of(sender_jid)
        thread = row["chat_name"] or row["chat_jid"] or "whatsapp"
        counterpart = None if group else handle_of(row["chat_jid"])
        # In a DM the session's partner name is the address-book name, which is the one
        # with a chance of matching Contacts; in a group there is no per-member name at
        # all and the profile cache is the only source. `identity` decides whether
        # either is believable — see `name_shaped`, which exists because this field
        # sometimes holds a formatted phone number instead.
        seen_name = pushed.get(str(sender_jid or "")) or row["member_name"]
        if not group:
            seen_name = row["chat_name"] or seen_name
        threads.record(conn, "whatsapp", thread, label=row["chat_name"], is_group=group)

        base.deliver(
            conn, report,
            stream="whatsapp",
            external_id=f"wa:{row['rowid']}",
            ts=to_iso(row["date"]),
            text=text,
            thread=thread,
            handle=None if from_me else handle,
            from_me=from_me,
            is_group=group,
            top_tier=tier,
            counterpart=counterpart,
            meta={"seen_name": seen_name, "group": group},
        )
    src.close()

    conn.commit()
    if highest > watermark:
        base.set_watermark(conn, "whatsapp.rowid", highest)
    if len(rows) >= limit:
        report.more = True
    return report


@register
class WhatsAppSource(Source):
    name = "whatsapp"
    description = "WhatsApp from the macOS app's local store (groups and DMs)"
    secrets = ()
    order = 20

    def fetch(self, conn, cfg, report, limit):
        report.absorb(ingest(conn, cfg, limit=limit))

    def check(self, cfg):
        path = store_path(cfg.secret("WHATSAPP_DB", "whatsappdb"))
        if not path:
            return False, ("WhatsApp desktop not installed or never signed in — "
                           "no ChatStorage.sqlite in ~/Library/Group Containers/")
        try:
            src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            n = src.execute("SELECT count(*) FROM ZWAMESSAGE").fetchone()[0]
            src.close()
        except sqlite3.Error as exc:
            return False, f"found {path.name} but could not read it: {exc}"
        return True, f"{n} messages in {path.parent.name}"
