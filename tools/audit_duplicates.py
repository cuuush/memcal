#!/usr/bin/env python3
"""Audit calendar identity drift, dangling publishes, and false declines."""
from __future__ import annotations

import argparse
import collections
import os
import pathlib
import sqlite3
import sys


def connect(home: pathlib.Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{home / 'memcal.db'}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def identity_drift(conn: sqlite3.Connection) -> list[dict]:
    """Find Apple UIDs associated with multiple identities."""
    by_uid: dict[str, list[sqlite3.Row]] = collections.defaultdict(list)
    for row in conn.execute(
        "SELECT identity, calendar_uid, calendar_name, event_uid, event_key,"
        "       starts_at, active, published, last_seen_at"
        "  FROM calendar_items WHERE event_uid != ''"
    ):
        by_uid[row["event_uid"]].append(row)

    out = []
    for uid, rows in sorted(by_uid.items()):
        if len({r["identity"] for r in rows}) < 2:
            continue
        keys = {r["event_key"] for r in rows}
        events = {}
        for key in keys:
            got = conn.execute(
                "SELECT id, title, date, kind, status, written_by FROM events WHERE key = ?",
                (key,),
            ).fetchone()
            if got:
                events[key] = got
            out.append({"uid": uid, "items": rows, "events": events})
    return out


def dangling_publish(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Find published records referencing non-existent event keys."""
    return list(
        conn.execute(
            "SELECT c.* FROM calendar_items c"
            "  WHERE c.published = 1"
            "    AND NOT EXISTS (SELECT 1 FROM events e WHERE e.key = c.event_key)"
        )
    )


def key_date_skew(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Find live rows whose keys disagree with their current event date."""
    return list(
        conn.execute(
            "SELECT id, key, date, title, written_by FROM events"
            "  WHERE key GLOB '*@[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"
            "    AND substr(key, -10) != date"
        )
    )


def false_declines(conn: sqlite3.Connection, drift: list[dict]) -> list[dict]:
    """Find declined rows whose Apple UID remains active on the calendar."""
    out = []
    for group in drift:
        live = [r for r in group["items"] if r["active"]]
        if not live:
            continue
        for key, event in group["events"].items():
            if event["status"] != "declined":
                continue
            stale = [r for r in group["items"] if r["event_key"] == key and not r["active"]]
            if stale:
                out.append({"event": event, "still_present_as": [r["event_key"] for r in live]})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--home", default=os.environ.get("MEMCAL_HOME", "~/.memcal"))
    args = ap.parse_args()
    home = pathlib.Path(args.home).expanduser()
    conn = connect(home)

    total = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    by_writer = dict(conn.execute(
        "SELECT written_by, count(*) FROM events GROUP BY written_by").fetchall())
    print(f"{total} events   {by_writer}\n")

    print("=" * 78)
    print("CALENDAR IDENTITY DRIFT — one Apple UID under two identities")
    print("=" * 78)
    drift = identity_drift(conn)
    dup_events = 0
    for group in drift:
        live_rows = [k for k, e in group["events"].items()]
        if len(live_rows) > 1:
            dup_events += len(live_rows) - 1
        print(f"\nApple UID {group['uid'][:36]}")
        for r in group["items"]:
            flag = "active" if r["active"] else "STALE "
            print(f"   {flag} cal_uid={r['calendar_uid']!r:<12} name={r['calendar_name']:<12}"
                  f" key={r['event_key']}")
        for key, e in group["events"].items():
            print(f"     -> E{e['id']:<4} {e['kind']:<11} {e['status']:<10} {e['title'][:44]}")
    print(f"\n{len(drift)} Apple event(s) split across identities"
          f" -> {dup_events} duplicate memcal row(s)")

    print("\n" + "=" * 78)
    print("DANGLING PUBLISH RECORDS — key embeds the date, the date moved")
    print("=" * 78)
    for r in dangling_publish(conn):
        print(f"  {r['event_key']:<44} published, no such row"
              f"   (calendar copy starts {r['starts_at']})")

    print("\n" + "=" * 78)
    print("KEY/DATE SKEW — live rows whose key disagrees with their own date")
    print("=" * 78)
    for r in key_date_skew(conn):
        print(f"  E{r['id']:<4} key={r['key']:<40} date={r['date']}  ({r['written_by']})")

    print("\n" + "=" * 78)
    print("FALSE DECLINES — 'you are not going' about an event still on the calendar")
    print("=" * 78)
    for bad in false_declines(conn, drift):
        e = bad["event"]
        print(f"  E{e['id']:<4} {e['title'][:50]:<52} declined")
        for key in bad["still_present_as"]:
            print(f"        still present as {key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
