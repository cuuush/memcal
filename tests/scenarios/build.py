#!/usr/bin/env python3
"""Build connector-native fixtures from the synthetic scenario."""

from __future__ import annotations

import email.utils
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))

from tests.scenarios import skeleton as sk  # noqa: E402

OUT = HERE / "fixtures"
TEXT = HERE / "text"
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)

#: Stable GroupMe group IDs across benchmark runs.
GM_IDS = {"poker crew": "101", "brunch sunday": "102", "beer garden": "103",
          "block party": "104", "smash bros": "105", "board game night": "106",
          "emoji noise": "107", "rave chat": "108"}
#: WhatsApp group JIDs.
WA_JIDS = {"dinner thu": "120363011111111111@g.us",
           "morgan family": "120363022222222222@g.us",
           "doggo park": "120363033333333333@g.us"}


def when(day: int, time: str) -> datetime:
    return datetime.fromisoformat(f"{sk.DAYS[day - 1]}T{time}:00").astimezone()


def handle_of(name: str) -> str:
    """Return contact handle (phone number or identifier)."""
    entry = sk.CAST.get(name)
    return entry[0] if entry else name


def gm_id_of(name: str) -> str:
    entry = sk.CAST.get(name)
    return entry[1] if entry else "0"


def wa_jid_of(name: str) -> str:
    entry = sk.CAST.get(name)
    return entry[2] if entry else f"{name}@s.whatsapp.net"


def load_text() -> dict:
    """Load all text lines keyed by skeleton ID."""
    out: dict = {}
    for path in sorted(TEXT.glob("*.json")):
        out.update(json.loads(path.read_text(encoding="utf-8")))
    return out


def expand() -> list[dict]:
    """Expand skeleton and text entries into a sorted list of message records."""
    text = load_text()
    rows: list[dict] = []
    missing: list[str] = []

    for rec in sk.SIGNAL:
        # Inline text allows defining regression records without generating text batches.
        body = rec.get("text") or text.get(rec["id"])
        if not body:
            missing.append(rec["id"])
            continue
        rows.append({**rec, "text": body})

    for batch in sk.FILLER:
        written = text.get(batch["id"])
        if not written:
            missing.append(batch["id"])
            continue
        for item in written:
            rows.append({"id": batch["id"], "day": batch["day"], "time": item["time"],
                         "src": batch["src"], "thread": batch["thread"],
                         "who": item["who"], "beat": None, "text": item["text"]})

    if missing:
        raise SystemExit(f"no text written for: {', '.join(missing)}")
    rows.sort(key=lambda r: (r["day"], r["time"]))
    return rows


# ------------------------------------------------------------------ bluebubbles --

def build_bluebubbles(rows: list[dict]) -> int:
    """Build BlueBubbles message fixtures matching `message/query` payload format.

    Direct messages have an empty `displayName` and use `chatIdentifier` as the address.
    """
    out = []
    for row in rows:
        if row["src"] != "bb":
            continue
        other = row["thread"]
        address = handle_of(other)
        from_me = row["who"] == "me"
        stamp = when(row["day"], row["time"])
        out.append({
            "originalROWID": len(out) + 1,
            "guid": f"p:0/{uuid.uuid5(uuid.NAMESPACE_URL, row['id'] + row['time'])}".upper(),
            "text": row["text"],
            "dateCreated": int(stamp.timestamp() * 1000),
            "dateDelivered": int(stamp.timestamp() * 1000),
            "isFromMe": from_me,
            # Outgoing messages set handle to None per API response format.
            "handle": None if from_me else {"address": address, "service": "iMessage",
                                            "uncanonicalizedId": None},
            "chats": [{
                "chatIdentifier": address,
                "displayName": "",          # direct messages omit display name
                "participants": [{"address": address},
                                 {"address": handle_of("me")}],
            }],
            "attachments": [],
        })
    path = OUT / "bluebubbles"
    path.mkdir(parents=True, exist_ok=True)
    (path / "messages.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return len(out)


# ----------------------------------------------------------------------- groupme --

def build_groupme(rows: list[dict]) -> int:
    """Build GroupMe fixture files with unix epoch second timestamps."""
    path = OUT / "groupme"
    path.mkdir(parents=True, exist_ok=True)
    (path / "users_me.json").write_text(
        json.dumps({"id": gm_id_of("me"), "name": "Casey"}, indent=1), encoding="utf-8")

    by_group: dict[str, list] = {}
    for row in rows:
        if row["src"] != "gm":
            continue
        gid = GM_IDS[row["thread"]]
        stamp = when(row["day"], row["time"])
        msgs = by_group.setdefault(gid, [])
        msgs.append({
            # Monotonic string ID matching GroupMe pagination sort order.
            "id": f"{int(stamp.timestamp())}{len(msgs):04d}",
            "created_at": int(stamp.timestamp()),
            # System announcements use user_id="system" and system=True.
            "user_id": ("system" if row["who"] == sk.SYSTEM_SENDER
                        else gm_id_of(row["who"]) if row["who"] != "me"
                        else gm_id_of("me")),
            "name": (sk.SYSTEM_SENDER if row["who"] == sk.SYSTEM_SENDER
                     else row["who"] if row["who"] != "me" else "Casey"),
            "text": row["text"],
            "system": row["who"] == sk.SYSTEM_SENDER,
            "favorited_by": [],
            "attachments": [],
            "group_id": gid,
            "source_guid": f"fixture-{row['id']}-{len(msgs)}",
        })

    groups = []
    for name, gid in GM_IDS.items():
        msgs = by_group.get(gid, [])
        members = sk.GROUPS.get(("gm", name), [])
        groups.append({
            "id": gid,
            "name": name,
            "type": "private",
            "updated_at": max((m["created_at"] for m in msgs), default=0),
            "muted_until": None,
            "members": [{"user_id": gm_id_of(p) if p != "me" else gm_id_of("me"),
                         "nickname": p if p != "me" else "Casey"} for p in members],
            "messages": {"count": len(msgs),
                         "last_message_id": msgs[-1]["id"] if msgs else None,
                         "last_message_created_at": msgs[-1]["created_at"] if msgs else 0},
        })
        (path / f"msgs-{gid}.json").write_text(json.dumps(msgs, indent=1), encoding="utf-8")
    (path / "groups.json").write_text(json.dumps(groups, indent=1), encoding="utf-8")
    (path / "chats.json").write_text("[]", encoding="utf-8")   # Direct messages omitted in this corpus
    return sum(len(v) for v in by_group.values())


# ---------------------------------------------------------------------- whatsapp --

WA_SCHEMA = """
CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, ZCONTACTJID TEXT,
                             ZPARTNERNAME TEXT, ZSESSIONTYPE INTEGER);
CREATE TABLE ZWAGROUPMEMBER  (Z_PK INTEGER PRIMARY KEY, ZMEMBERJID TEXT,
                              ZCONTACTNAME TEXT);
CREATE TABLE ZWAMESSAGE      (Z_PK INTEGER PRIMARY KEY, ZTEXT TEXT, ZMESSAGEDATE REAL,
                              ZISFROMME INTEGER, ZFROMJID TEXT, ZMESSAGETYPE INTEGER,
                              ZCHATSESSION INTEGER, ZGROUPMEMBER INTEGER);
"""


def build_whatsapp(rows: list[dict]) -> int:
    """Build WhatsApp SQLite database fixture.

    Uses Apple epoch (2001-01-01) timestamps, session type 1 for group chats, and
    sender linkage through ZWAGROUPMEMBER.
    """
    path = OUT / "whatsapp"
    path.mkdir(parents=True, exist_ok=True)
    store = path / "ChatStorage.sqlite"
    store.unlink(missing_ok=True)
    conn = sqlite3.connect(store)
    conn.executescript(WA_SCHEMA)

    sessions: dict[str, int] = {}
    members: dict[str, int] = {}
    count = 0
    for row in rows:
        if row["src"] != "wa":
            continue
        thread = row["thread"]
        jid = WA_JIDS[thread]
        if thread not in sessions:
            sessions[thread] = len(sessions) + 1
            conn.execute(
                "INSERT INTO ZWACHATSESSION(Z_PK, ZCONTACTJID, ZPARTNERNAME, ZSESSIONTYPE)"
                " VALUES(?,?,?,?)", (sessions[thread], jid, thread, 1))
        member_pk = None
        if row["who"] != "me":
            if row["who"] not in members:
                members[row["who"]] = len(members) + 1
                conn.execute(
                    "INSERT INTO ZWAGROUPMEMBER(Z_PK, ZMEMBERJID, ZCONTACTNAME)"
                    " VALUES(?,?,?)",
                    (members[row["who"]], wa_jid_of(row["who"]),
                     # LID contacts do not supply contact display names.
                     None if row["who"] in sk.NAMELESS else row["who"]))
            member_pk = members[row["who"]]
        count += 1
        stamp = when(row["day"], row["time"]).astimezone(timezone.utc)
        conn.execute(
            """INSERT INTO ZWAMESSAGE(Z_PK, ZTEXT, ZMESSAGEDATE, ZISFROMME, ZFROMJID,
                                      ZMESSAGETYPE, ZCHATSESSION, ZGROUPMEMBER)
               VALUES(?,?,?,?,?,?,?,?)""",
            (count, row["text"], (stamp - APPLE_EPOCH).total_seconds(),
             1 if row["who"] == "me" else 0, jid, 0, sessions[thread], member_pk))
    conn.commit()
    conn.close()
    return count


# -------------------------------------------------------------------------- mail --

#: Email header templates for gating classification.
HEADERS = {
    "bulk": {
        "List-Unsubscribe": "<mailto:unsubscribe@{host}>, <https://{host}/unsubscribe>",
        "List-ID": "<campaigns.{host}>",
        "Precedence": "bulk",
        "X-Campaign-ID": "c-8827194",
        "Auto-Submitted": "auto-generated",
    },
    "transactional": {
        "Auto-Submitted": "auto-generated",
        "X-Auto-Response-Suppress": "All",
    },
    "person": {},
}


def build_mail(rows: list[dict]) -> int:
    path = OUT / "mail"
    path.mkdir(parents=True, exist_ok=True)
    for old in path.glob("*.eml"):
        old.unlink()

    bodies = json.loads((TEXT / "emails.json").read_text(encoding="utf-8"))
    written = 0
    for rec in sk.EMAIL:
        body = rec.get("text") or bodies.get(rec["id"])
        if not body:
            raise SystemExit(f"no body written for {rec['id']}")
        stamp = when(rec["day"], rec["time"])
        host = rec["addr"].split("@", 1)[1]
        lines = [
            f"Return-Path: <bounce-{rec['id']}@{host}>",
            f"Message-ID: <{rec['id']}.{int(stamp.timestamp())}@{host}>",
            f"Date: {email.utils.format_datetime(stamp)}",
            f"From: {rec['name']} <{rec['addr']}>",
            "To: Casey <casey@example.com>",
            f"Subject: {rec['subject']}",
            "MIME-Version: 1.0",
            # HTML mail fixtures test link target extraction from href attributes.
            'Content-Type: text/html; charset="utf-8"' if rec.get("html")
            else 'Content-Type: text/plain; charset="utf-8"',
            "Content-Transfer-Encoding: 8bit",
        ]
        for key, value in HEADERS[rec["kind"]].items():
            lines.append(f"{key}: {value.format(host=host)}")
        lines.append("")
        lines.append(body)
        (path / f"{rec['id']}.eml").write_text("\n".join(lines), encoding="utf-8")
        written += 1
    return written


# ------------------------------------------------------------------------- agent --

def build_agent(rows: list[dict]) -> int:
    path = OUT / "agent"
    path.mkdir(parents=True, exist_ok=True)
    out = [{"ts": when(r["day"], r["time"]).isoformat(timespec="seconds"),
            "text": r["text"], "id": r["id"]}
           for r in rows if r["src"] == "agent"]
    (path / "lines.json").write_text(json.dumps(out, indent=1), encoding="utf-8")
    return len(out)


# ---------------------------------------------------------------------- calendar --

def _calendar_name(spec: dict, day: int) -> str:
    """Return calendar display name for the specified day."""
    name = spec["calendar"]
    return name[day] if isinstance(name, dict) else name


def calendar_items(day: int) -> list[dict]:
    """Generate calendar snapshot records for ingestion.

    `calendar_uid` tracks stable calendar identity across calendar name modifications.
    """
    out = []
    for spec in sk.CALENDAR:
        if day not in spec["days"]:
            continue
        all_day = bool(spec.get("all_day"))
        if all_day:
            start = datetime.fromisoformat(f"{spec['date']}T00:00:00").astimezone()
            end = start + timedelta(days=1)
        else:
            start = datetime.fromisoformat(
                f"{spec['date']}T{spec['time']}:00").astimezone()
            end = start + timedelta(hours=spec.get("hours", 1))
        out.append({
            "calendar_name": _calendar_name(spec, day),
            "calendar_uid": spec["calendar_key"],
            "writable": bool(spec.get("writable")),
            "uid": spec["uid"],
            "title": spec["title"],
            "start": start.isoformat(),
            "end": end.isoformat(),
            "all_day": all_day,
            "location": spec.get("location", ""),
            "description": spec.get("description", ""),
            "url": spec.get("url", ""),
        })
    return out


def build_calendar() -> int:
    path = OUT / "calendar"
    path.mkdir(parents=True, exist_ok=True)
    for old in path.glob("day*.json"):
        old.unlink()
    total = 0
    for day in range(1, len(sk.DAYS) + 1):
        items = calendar_items(day)
        (path / f"day{day}.json").write_text(
            json.dumps(items, indent=1), encoding="utf-8")
        total += len(items)
    return total


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = expand()
    counts = {
        "bluebubbles": build_bluebubbles(rows),
        "groupme": build_groupme(rows),
        "whatsapp": build_whatsapp(rows),
        "mail": build_mail(rows),
        "agent": build_agent(rows),
        "calendar": build_calendar(),
    }
    total = sum(counts.values())
    print(f"built {total} items into {OUT}")
    for name, n in counts.items():
        print(f"  {name:12} {n}")
    beats = sorted({r["beat"] for r in rows if r["beat"]})
    filler = sum(1 for r in rows if not r["beat"])
    print(f"  {len(beats)} beats, {filler} filler lines "
          f"({filler * 100 // max(1, len(rows))}% of traffic)")


if __name__ == "__main__":
    main()
