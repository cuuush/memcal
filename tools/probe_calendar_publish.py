#!/usr/bin/env python3
"""Does the Calendar.app round trip actually work, on this Mac, right now?"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import config, db, events                              # noqa: E402
from memcal.sources import ical                                    # noqa: E402

# `app.calendars.whose({name: ...})` silently matches nothing here, and
# `delete every event` reports success while deleting none. Walking the list and
# deleting by index, backwards, is the form that actually works on this macOS.
REMOVE_JXA = r"""
function run(argv) {
  const app = Application("Calendar");
  let calendar = null;
  for (const candidate of app.calendars()) {
    if (String(candidate.name()) === argv[0]) { calendar = candidate; break; }
  }
  if (!calendar) return JSON.stringify({removed: 0});
  const events = calendar.events();
  let removed = 0;
  for (let i = events.length - 1; i >= 0; i--) { app.delete(events[i]); removed++; }
  return JSON.stringify({removed: removed});
}
"""


def cleanup(name: str) -> int:
    done = subprocess.run(["osascript", "-l", "JavaScript", "-e", REMOVE_JXA, name],
                          capture_output=True, text=True, timeout=120)
    try:
        return int(json.loads(done.stdout or "{}").get("removed", 0))
    except ValueError:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--calendar", default="memcal probe")
    ap.add_argument("--keep", action="store_true", help="do not delete what it wrote")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as home:
        cfg = config.load(home)
        cfg.publish_calendar = args.calendar
        conn = db.open_db(cfg.db_path)
        when = (db.today() + timedelta(days=6)).isoformat()
        event, _verb = events.upsert(conn, {
            "title": "memcal probe dinner", "date": when, "time": "19:30",
            "location": "Somewhere", "status": "confirmed", "kind": "commitment",
            "participants": ["A Friend"]}, written_by="live")

        print(f"publishing {event.title!r} on {when} to calendar {args.calendar!r}…")
        log = ical.publish_pending(conn, cfg)
        for line in log:
            print("  " + line)
        if not log or "could not publish" in log[0]:
            return 1

        row = conn.execute(
            "SELECT * FROM calendar_items WHERE published = 1").fetchone()
        print(f"  recorded uid {row['event_uid']} in {row['calendar_name']!r}")

        print("publishing again with nothing changed…")
        again = ical.publish_pending(conn, cfg)
        print(f"  {len(again)} write(s) — expected 0")

        print("moving the row, then publishing again…")
        events.upsert(conn, {"key": event.key, "date": when, "time": "20:30"},
                      written_by="live", match=False)
        moved = ical.publish_pending(conn, cfg)
        for line in moved:
            print("  " + line)

        # The half that matters: the next scan must not read it back in as news.
        time.sleep(1)
        print("re-reading Calendar.app…")
        start = time.time()
        snapshot = ical._calendar_snapshot(
            (db.today() - timedelta(days=ical.LOOKBACK_DAYS)).isoformat(),
            (db.today() + timedelta(days=ical.NEAR_LOOKAHEAD_DAYS)).isoformat())
        elapsed = time.time() - start
        items = snapshot.items
        mine = [i for i in items if i.get("calendar_name") == args.calendar]
        print(f"  {len(items)} events in {elapsed:.1f}s; {len(mine)} in the probe calendar")
        if snapshot.unreadable:
            print("  partial read — calendars that failed: "
                  + ", ".join(name or "(unnamed)" for name in snapshot.unreadable))

        before = conn.execute("SELECT count(*) n FROM events").fetchone()["n"]
        report = ical.ingest_snapshot(
            conn, cfg, items, scan_start="", scan_end="",
            unreadable=snapshot.unreadable)
        after = conn.execute("SELECT count(*) n FROM events").fetchone()["n"]
        archived = conn.execute(
            "SELECT count(*) n FROM archive WHERE stream='ical'").fetchone()["n"]
        print(f"  events {before} -> {after}; ical archive rows {archived}")
        for note in report.notes:
            print("  note: " + note)
        ours = [n for n in report.notes if "memcal published" in n]
        print("  " + ("OK: memcal's own event was not read back in" if ours
                      else "PROBLEM: nothing said the published event was skipped"))
        conn.close()

    if not args.keep:
        print(f"cleanup: removed {cleanup(args.calendar)} event(s) from "
              f"{args.calendar!r} (delete the empty calendar by hand if you want it gone)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
