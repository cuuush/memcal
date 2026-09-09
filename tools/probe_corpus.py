#!/usr/bin/env python3
"""What is actually in a store, for the specific cases the benchmark wants to score.

Writes nothing. Exists so benchmark checks are grounded in real rows — a check invented
from memory tests the memory, not the system.

    python3 tools/probe_corpus.py /tmp/bench-affinity
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

#: (heading, sql). Each is a case the benchmark either scores today or should.
PROBES = [
    ("events by source", """
        SELECT source, count(*) n FROM events GROUP BY source ORDER BY n DESC LIMIT 15
    """),
    ("partiful-titled events (M30: the platform tag that linked two unrelated ones)", """
        SELECT key, date, title, source FROM events
        WHERE lower(title) LIKE '%partiful%' OR lower(source) LIKE '%partiful%'
        ORDER BY date
    """),
    ("ticket-shaped traffic (must NOT merge: an order confirmation is not an invitation)", """
        SELECT id, ts, stream, handle, substr(replace(text, char(10), ' '), 1, 90) t
        FROM archive
        WHERE lower(text) LIKE '%axs%' OR lower(text) LIKE '%your tickets%'
           OR lower(text) LIKE '%order confirmation%'
        ORDER BY ts DESC LIMIT 12
    """),
    ("poker traffic", """
        SELECT id, ts, stream, handle, person,
               substr(replace(text, char(10), ' '), 1, 90) t
        FROM archive WHERE lower(text) LIKE '%poker%' ORDER BY ts
    """),
    ("beer traffic", """
        SELECT id, ts, stream, handle, person,
               substr(replace(text, char(10), ' '), 1, 90) t
        FROM archive WHERE lower(text) LIKE '%beer%' OR lower(text) LIKE '%bohemian%'
        ORDER BY ts
    """),
    ("riders alliance traffic (the nine mentions)", """
        SELECT id, ts, stream, handle, person,
               substr(replace(text, char(10), ' '), 1, 90) t
        FROM archive WHERE lower(text) LIKE '%riders alliance%'
           OR lower(text) LIKE '%nitehawk%' OR lower(text) LIKE '%mccollum%'
        ORDER BY ts
    """),
    ("nadia / venmo (M21: the receipt that should close the to-do)", """
        SELECT id, ts, stream, handle,
               substr(replace(text, char(10), ' '), 1, 100) t
        FROM archive WHERE lower(text) LIKE '%nadia%' ORDER BY ts
    """),
    ("ical rows (M5: authority)", """
        SELECT key, date, time, title, source FROM events
        WHERE key LIKE 'ical-%' ORDER BY date LIMIT 12
    """),
    ("streams and volume", """
        SELECT stream, count(*) n, min(ts) oldest, max(ts) newest
        FROM archive GROUP BY stream ORDER BY n DESC
    """),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("home", type=Path)
    ap.add_argument("--only", default="", help="substring match on the heading")
    args = ap.parse_args()

    conn = sqlite3.connect(args.home / "memcal.db")
    conn.row_factory = sqlite3.Row
    for heading, sql in PROBES:
        if args.only and args.only.lower() not in heading.lower():
            continue
        print(f"\n=== {heading}")
        try:
            rows = conn.execute(sql).fetchall()
        except sqlite3.Error as exc:
            print(f"    !! {exc}")
            continue
        if not rows:
            print("    (nothing)")
        for row in rows:
            print("    " + " · ".join(str(v) for v in tuple(row)))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
