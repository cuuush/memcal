#!/usr/bin/env python3
"""Copy a store for cold-start benchmarking without answer-leaking corrections."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import config  # noqa: E402

#: Model-written rows. Remove on replay to avoid scoring stored answers.
#: Keep `ical`/`partiful` rows as the cold-start baseline.
MODEL_WRITERS = ("dream:%", "live", "sweep")

#: Feed keys embed the minted date (title+date). Restore the `date` column from the key.
FEED_KEY_PREFIXES = ("ical-", "partiful-")

#: Derived tables. Contain only model-call output.
DERIVED_TABLES = ("todos", "questions", "standing", "event_history",
                  "provenance", "evidence", "slot_history", "generations", "runs")


def build(source: Path, dest: Path, *, keep_agent: bool = False) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    shutil.copy(source / "memcal.db", dest / "memcal.db")
    if (source / "wiki").is_dir():
        shutil.copytree(source / "wiki", dest / "wiki")

    conn = sqlite3.connect(dest / "memcal.db")
    conn.execute("UPDATE spool SET processed_at=NULL, run_id=NULL")
    if not keep_agent:
        conn.execute("DELETE FROM spool WHERE archive_id IN "
                     "(SELECT id FROM archive WHERE stream='agent')")
    restored = _restore_feed_rows(conn)
    for pattern in MODEL_WRITERS:
        conn.execute("DELETE FROM events WHERE written_by LIKE ?", (pattern,))
    for table in DERIVED_TABLES:
        conn.execute(f"DELETE FROM {table}")
    conn.commit()

    pending = conn.execute(
        "SELECT count(*) FROM spool WHERE processed_at IS NULL").fetchone()[0]
    agent = conn.execute(
        "SELECT count(*) FROM spool s JOIN archive a ON a.id = s.archive_id "
        "WHERE a.stream='agent'").fetchone()[0]
    kept = conn.execute("SELECT count(*) FROM events").fetchone()[0]
    conn.close()

    print(f"{dest}: {pending:,} pending · {kept} source-written events kept"
          + (f" · {restored} feed row(s) restored" if restored else "")
          + (f" · {agent} agent rows LEFT IN (leaky)" if agent else " · no agent rows"))
    print(f"  MEMCAL_HOME={dest} python3 -m memcal dream --mode ondemand --model <model>")


def _restore_feed_rows(conn: sqlite3.Connection) -> int:
    """Put calendar-fed rows back the way the calendar had them, instead of deleting them."""
    restored = 0
    for prefix in FEED_KEY_PREFIXES:
        rows = conn.execute(
            "SELECT id, key, date, source, origin FROM events WHERE key LIKE ?",
            (prefix + "%",)).fetchall()
        for row_id, key, date, source, origin in rows:
            _slug, _, minted = key.rpartition("@")
            if len(minted) != 10 or not minted[:4].isdigit():
                continue
            feed = origin or source or ""
            if not feed.startswith(("ical:", "partiful:")):
                # Fall back to the key prefix when origin was overwritten.
                feed = f"{prefix.rstrip('-')}:subscribed:restored"
            # Rewrite `written_by` so hand-corrected (`live`) feed rows are kept.
            conn.execute(
                "UPDATE events SET date=?, source=?, origin=?, written_by=? WHERE id=?",
                (minted, feed, feed, prefix.rstrip("-"), row_id))
            restored += date != minted or (source or "") != feed
    return restored


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dest", type=Path)
    ap.add_argument("--source", type=Path, default=None,
                    help="store to copy from (default: the configured MEMCAL_HOME)")
    ap.add_argument("--keep-agent", action="store_true",
                    help="leave the agent stream in — only to measure how much it leaks")
    args = ap.parse_args()
    source = args.source or config.load().home
    if not (source / "memcal.db").is_file():
        print(f"no store at {source}")
        return 1
    build(source, args.dest, keep_agent=args.keep_agent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
