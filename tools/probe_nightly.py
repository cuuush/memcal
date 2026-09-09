#!/usr/bin/env python3
"""What a *nightly* pass looks like, as against a cold start."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import config, db, textclean  # noqa: E402
from memcal.dream import affinity, bundle as bundle_mod, propose  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("home", type=Path)
    ap.add_argument("--day", required=True, help="ISO date the pass runs for")
    ap.add_argument("--days", type=int, default=1, help="window width")
    args = ap.parse_args()

    cfg = config.load(args.home)
    conn = db.connect(cfg.db_path)
    start = db.parse_date(args.day)
    end = start.fromordinal(start.toordinal() + args.days)

    # Everything outside the window is treated as already read, which is exactly what a
    # nightly pass sees: yesterday's traffic queued, the rest consumed by earlier passes.
    conn.execute("UPDATE spool SET processed_at = NULL")
    conn.execute(
        "UPDATE spool SET processed_at = ? WHERE archive_id IN "
        "(SELECT id FROM archive WHERE substr(ts,1,10) < ? OR substr(ts,1,10) >= ?)",
        (db.now(), start.isoformat(), end.isoformat()))

    bundles = bundle_mod.build(conn, limit=cfg.item_budget,
                               per_entity=cfg.items_per_entity)
    if not bundles:
        print(f"nothing queued for {args.day}")
        return 0
    costs = sorted((textclean.estimate_tokens(propose.build_bundle_block(cfg, b, conn)), b)
                   for b in bundles)
    total = sum(c for c, _ in costs)
    print(f"{args.day} +{args.days}d · {len(bundles)} bundles · "
          f"{sum(len(b.items) for b in bundles)} lines · {total:,} tokens")
    print(f"  median bundle {costs[len(costs) // 2][0]:,} tok · "
          f"largest {costs[-1][0]:,} ({costs[-1][1].title or costs[-1][1].entity}) · "
          f"budget {cfg.pack_tokens:,}")
    over = [b for c, b in costs if c > cfg.pack_tokens]
    print(f"  {len(over)} bundle(s) exceed the pack budget alone"
          + (": " + ", ".join((b.title or b.entity)[:24] for b in over[:4]) if over else ""))

    for strategy in ("size", "affinity"):
        cfg.pack_strategy = strategy
        groups = propose.pack(cfg, list(bundles), conn)
        multi = [g for g in groups if len(g) > 1]
        print(f"\n  {strategy:9} {len(groups)} request(s), {len(multi)} with company")
        if strategy == "affinity":
            for line in affinity.describe(bundles, cfg.affinity_near_days, limit=8):
                print("      " + line)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
