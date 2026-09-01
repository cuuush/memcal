#!/usr/bin/env python3
"""Feed the fixtures into a scratch memcal home, one fake day at a time.

Everything here goes through the *real* connectors. The network and disk seams are
stubbed — `BlueBubbles.messages`, `GroupMe._get`, the IMAP bridge, WhatsApp's store
path — and nothing below them is. So `chat_of`, `message_text`, `to_iso`,
`phone_of`, the group-member join, `gate_email`'s header rules and `base.deliver` all
run exactly as they do in production, and a parsing bug shows up here rather than
being defined away by a tidier fixture format.

    python3 tests/scenarios/load.py --home /tmp/bench --day 1

Never touches ~/.memcal. `seed()` refuses a home that resolves to it.
"""

from __future__ import annotations

import argparse
import email
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))

from memcal import config, db, identity, live, threads, todos, wiki  # noqa: E402
from memcal.sources import base, bluebubbles, groupme, ical, proton, whatsapp  # noqa: E402
from tests.scenarios import skeleton as sk  # noqa: E402

FIX = HERE / "fixtures"
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def day_end(day: int) -> datetime:
    """The last moment of a fake day, in local time — the ingest cutoff."""
    return datetime.fromisoformat(f"{sk.DAYS[day - 1]}T23:59:59").astimezone()


# ------------------------------------------------------------------------- seed --

def seed(home: Path) -> tuple[sqlite3.Connection, config.Config]:
    """A fresh scratch home with contacts and typed facts, and nothing else."""
    resolved = home.expanduser().resolve()
    if resolved == (Path.home() / ".memcal").resolve():
        raise SystemExit("refusing to seed the real ~/.memcal — pass a scratch --home")
    if resolved.exists():
        shutil.rmtree(resolved)
    cfg = config.load(resolved)
    cfg.ensure_dirs()
    conn = db.open_db(cfg.db_path)

    # Contacts are a dict lookup, imported once. Both the phone number and the GroupMe
    # user id resolve to the same person, which is what makes cross-platform identity
    # structural rather than something the model has to notice.
    for name, (phone, gm_id, _jid) in sk.CAST.items():
        if name == "me":
            identity.link(conn, phone, "me", source="fixture")
            identity.link(conn, f"groupme:{gm_id}", "me", source="fixture")
            # `identity.me`, which is the key `me_names()` actually reads. It was
            # `identity.me_names`, a key nothing looks at, so every run fell through to
            # the discovery path — which shells out to `id -F` and put whatever the
            # developer's macOS account is called into the benchmark's prompt.
            db.set_meta(conn, "identity.me", db.jdump(["Casey", "Casey Morgan"]))
            continue
        identity.link(conn, phone, name, source="fixture")
        identity.link(conn, f"groupme:{gm_id}", name, source="fixture")

    identity.add_top_tier(conn, "Harper")
    wiki.set_slot(cfg.wiki_dir, "casey", "neighborhood", "North End",
                  source="fixture", conn=conn)
    wiki.set_slot(cfg.wiki_dir, "casey", "dog", "Comet", source="fixture", conn=conn)
    wiki.set_slot(cfg.wiki_dir, "u-and-me-calendar", "meaning",
                  "shared calendar for Casey and Harper", source="fixture",
                  section="projects", conn=conn)
    for alias in ("our cal", "shared cal", "u&me"):
        wiki.add_alias(cfg.wiki_dir, "u-and-me-calendar", alias, section="projects",
                       conn=conn)
    conn.commit()
    return conn, cfg


# ------------------------------------------------------------------ bluebubbles --

def _load_bluebubbles(conn, cfg, day: int) -> base.IngestReport:
    payload = json.loads((FIX / "bluebubbles" / "messages.json").read_text())
    cutoff = day_end(day).timestamp() * 1000

    class FakeServer(bluebubbles.BlueBubbles):
        def __init__(self, _cfg):
            self.url, self.password = "fixture://bluebubbles", "x"

        def ping(self):
            return True

        def messages(self, after_ms, limit=200, offset=0):
            rows = [m for m in payload
                    if after_ms < m["dateCreated"] <= cutoff]
            rows.sort(key=lambda m: m["dateCreated"])
            return rows[offset:offset + limit]

    real = bluebubbles.BlueBubbles
    bluebubbles.BlueBubbles = FakeServer
    try:
        return bluebubbles.ingest(conn, cfg, limit=5000)
    finally:
        bluebubbles.BlueBubbles = real


# ----------------------------------------------------------------------- groupme --

def _load_groupme(conn, cfg, day: int) -> base.IngestReport:
    root = FIX / "groupme"
    me = json.loads((root / "users_me.json").read_text())
    groups = json.loads((root / "groups.json").read_text())
    cutoff = int(day_end(day).timestamp())

    def visible(gid: str) -> list[dict]:
        path = root / f"msgs-{gid}.json"
        if not path.is_file():
            return []
        return [m for m in json.loads(path.read_text()) if m["created_at"] <= cutoff]

    class FakeClient(groupme.GroupMe):
        def __init__(self, _cfg):
            self.token = "fixture"

        def _get(self, path, **params):
            if path == "users/me":
                return me
            if path == "chats":
                return []
            if path == "groups":
                # The listing has to reflect only what exists *so far*, because the
                # connector compares last_message_id against its watermark and skips
                # the fetch when they match. A listing from the future defeats that.
                out = []
                for group in groups:
                    msgs = visible(str(group["id"]))
                    if not msgs:
                        continue
                    out.append({**group, "messages": {
                        "count": len(msgs), "last_message_id": msgs[-1]["id"],
                        "last_message_created_at": msgs[-1]["created_at"]}})
                return out if params.get("page", 1) == 1 else []
            if path.startswith("groups/") and path.endswith("/messages"):
                gid = path.split("/")[1]
                msgs = visible(gid)
                since = params.get("since_id")
                if since:
                    msgs = [m for m in msgs if m["id"] > since]
                return {"messages": msgs[: params.get("limit", 100)]}
            return None

    real = groupme.GroupMe
    groupme.GroupMe = FakeClient
    try:
        return groupme.ingest(conn, cfg, limit=5000)
    finally:
        groupme.GroupMe = real


# ---------------------------------------------------------------------- whatsapp --

def _load_whatsapp(conn, cfg, day: int, scratch: Path) -> base.IngestReport:
    """Copy the store, drop anything after the cutoff, and let the real query run."""
    source = FIX / "whatsapp" / "ChatStorage.sqlite"
    scratch.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(source, scratch)
    cutoff = (day_end(day).astimezone(timezone.utc) - APPLE_EPOCH).total_seconds()
    trimmed = sqlite3.connect(scratch)
    trimmed.execute("DELETE FROM ZWAMESSAGE WHERE ZMESSAGEDATE > ?", (cutoff,))
    trimmed.commit()
    trimmed.close()
    return whatsapp.ingest(conn, cfg, limit=5000, db_path=str(scratch))


# -------------------------------------------------------------------------- mail --

def _load_mail(conn, cfg, day: int) -> base.IngestReport:
    """Real RFC822 through `proton._handle_message`, so the header gate is the gate."""
    report = base.IngestReport.opened("email", cfg)
    days = {rec["id"]: rec["day"] for rec in sk.EMAIL}

    class FakeBridge:
        def __init__(self, bodies):
            self.bodies = bodies

        def body(self, uid):
            return self.bodies.get(uid, "")

    paths = sorted(p for p in (FIX / "mail").glob("*.eml")
                   if days.get(p.stem, 99) <= day)
    bodies = {}
    parsed = []
    for uid, path in enumerate(paths, 1):
        message = email.message_from_string(path.read_text(encoding="utf-8"))
        bodies[uid] = proton.extract_text(message)
        parsed.append((uid, message))
    bridge = FakeBridge(bodies)
    for uid, message in parsed:
        proton._handle_message(conn, cfg, report, bridge, uid, message, "INBOX")
    return report


# ------------------------------------------------------------------------- agent --

def _load_agent(conn, cfg, day: int) -> base.IngestReport:
    """The agent stream — them stating something to their assistant, on purpose."""
    report = base.IngestReport.opened("agent", cfg)
    lines = json.loads((FIX / "agent" / "lines.json").read_text())
    cutoff = day_end(day)
    for line in lines:
        if datetime.fromisoformat(line["ts"]) > cutoff:
            continue
        base.deliver(conn, report, stream="agent", external_id=f"agent:{line['id']}",
                     addressed_to="machine",
                     ts=line["ts"], text=line["text"], thread="conversation",
                     from_me=True, person="me")
    return report


# ---------------------------------------------------------------------- calendar --

def _load_calendar(conn, cfg, day: int) -> base.IngestReport:
    path = FIX / "calendar" / f"day{day}.json"
    items = json.loads(path.read_text()) if path.is_file() else []
    return ical.ingest_snapshot(
        conn, cfg, items,
        scan_start=(db.today() - timedelta(days=cfg.days_back)).isoformat(),
        scan_end=(db.today() + timedelta(days=365)).isoformat())


# ------------------------------------------------------------- agent actions --

def agent_actions(conn, cfg, day: int) -> list[str]:
    """What the assistant *did* during the day, through the real typed write path.

    `sk.ACTIONS` names functions in `memcal.live` — the same ones `mcp_server` calls,
    with no model anywhere in them. Run before the day's dream, because that is the
    order it happens in: the user tells the agent something at lunchtime and the pass meets
    the result of it at midnight.

    Failures are returned rather than raised. A tool call the store refuses is a real
    finding about the store, and the run has checks that will say so far more precisely
    than a traceback out of the loader would.
    """
    done: list[str] = []
    for action in sk.ACTIONS:
        if action["day"] != day:
            continue
        fn = getattr(live, action["call"])
        try:
            fn(conn, cfg, **action["args"])
            done.append(f"{action['id']} {action['call']}")
        except Exception as exc:                       # noqa: BLE001 — reported, not raised
            done.append(f"{action['id']} {action['call']} FAILED: {exc}")
    conn.commit()
    return done


# -------------------------------------------------------------------------- main --

def ingest_day(conn, cfg, day: int, *, quiet: bool = False) -> list[base.IngestReport]:
    """Every stream, up to the end of the given fake day. Watermarks make it additive."""
    scratch = cfg.home / "scratch" / "ChatStorage.sqlite"
    reports = [
        _load_bluebubbles(conn, cfg, day),
        _load_groupme(conn, cfg, day),
        _load_whatsapp(conn, cfg, day, scratch),
        _load_mail(conn, cfg, day),
        _load_agent(conn, cfg, day),
        _load_calendar(conn, cfg, day),
    ]
    threads.refresh(conn)
    conn.commit()
    acted = agent_actions(conn, cfg, day)
    if not quiet:
        for report in reports:
            print(f"  {report.summary()}")
        for line in acted:
            print(f"  agent tool: {line}")
    return reports


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    parser.add_argument("--day", type=int, default=1)
    parser.add_argument("--seed", action="store_true", help="wipe and re-seed first")
    args = parser.parse_args()

    home = Path(args.home).expanduser()
    if args.seed or not home.exists():
        conn, cfg = seed(home)
        print(f"seeded {home}")
    else:
        cfg = config.load(home)
        conn = db.open_db(cfg.db_path)

    db.set_today(sk.DAYS[args.day - 1])
    print(f"day {args.day} — clock pinned to {db.today()}")
    ingest_day(conn, cfg, args.day)
    pending = conn.execute(
        "SELECT count(*) AS n FROM spool WHERE processed_at IS NULL").fetchone()["n"]
    print(f"  spool pending: {pending}")


if __name__ == "__main__":
    main()
