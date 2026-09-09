#!/usr/bin/env python3
"""What `affinity` thinks is related, printed."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import config, db  # noqa: E402
from memcal.dream import affinity, bundle as bundle_mod  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("home", type=Path)
    ap.add_argument("--near-days", type=int, default=3)
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--grep", default="", help="only pairs mentioning this text")
    ap.add_argument("--groups", action="store_true", help="show the packed groups too")
    args = ap.parse_args()

    cfg = config.load(args.home)
    conn = db.connect(cfg.db_path)
    bundles = bundle_mod.build(conn, limit=cfg.item_budget,
                               per_entity=cfg.items_per_entity)
    frags = [f for b in bundles for f in affinity.fragments(b)]
    dated = sum(1 for f in frags if f.dated)
    print(f"{len(bundles)} bundles · {sum(len(b.items) for b in bundles):,} lines · "
          f"{len(frags)} fragments ({dated} dated)")

    ambient = affinity.ambient_tokens(frags)
    print(f"\nambient (suppressed as platform/channel words): "
          f"{', '.join(sorted(ambient)[:24]) or 'none'}")

    print(f"\n--- related pairs, strongest first")
    lines = affinity.describe(bundles, args.near_days, limit=10_000)
    if args.grep:
        lines = [ln for ln in lines if args.grep.lower() in ln.lower()]
    for line in lines[:args.limit]:
        print("  " + line)
    if len(lines) > args.limit:
        print(f"  … {len(lines) - args.limit} more")

    if args.groups:
        from memcal.dream import propose
        groups, leftovers = affinity.group(
            bundles, max_bundles=cfg.pack_bundles, max_tokens=cfg.pack_tokens,
            cost=lambda b: len(b.render(cfg.bundle_format)) // 4,
            near_days=args.near_days)
        print(f"\n--- {len(groups)} affinity group(s), {len(leftovers)} ungrouped")
        for members in groups:
            print("  · " + " + ".join((b.title or b.entity)[:30] for b in members))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
