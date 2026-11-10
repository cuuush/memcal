#!/usr/bin/env python3
"""Print the raw wiki profile data and its size from an existing store.

    python3 tools/probe_page.py casey-morgan
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import config, db, wiki  # noqa: E402


def main() -> int:
    slug = sys.argv[1] if len(sys.argv) > 1 else "casey-morgan"
    cfg = config.load()
    try:
        conn = db.connect(cfg.db_path, read_only=True)
    except sqlite3.OperationalError as exc:
        print(f"Cannot open store {cfg.db_path}: {exc}", file=sys.stderr)
        return 1
    try:
        payload = wiki.profile(conn, cfg.wiki_dir, slug)
    finally:
        conn.close()
    if payload is None:
        print(f"no page {slug!r}; pages: {wiki.list_pages(cfg.wiki_dir)}")
        return 1
    text = json.dumps(payload, indent=2, default=str)
    print(text)
    print(f"\n--- {len(text)} chars, ~{len(text) // 4} tokens ---")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
