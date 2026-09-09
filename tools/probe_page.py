#!/usr/bin/env python3
"""Read-only probe: what `memcal_open_page` actually returns for one slug.

A lab instrument. It opens the live store read-only and prints the profile payload
byte-for-byte plus its size, so "the page read is too bare / too noisy" can be measured
instead of asserted.

    python3 tools/probe_page.py casey-morgan
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import config, db, wiki  # noqa: E402


def main() -> int:
    slug = sys.argv[1] if len(sys.argv) > 1 else "casey-morgan"
    cfg = config.load()
    conn = db.open_db(cfg.db_path)
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
