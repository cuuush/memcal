#!/usr/bin/env python3
"""Run the sweep stage alone, against the state as it stands.

The sweep after a full-backlog dream truncated at its old flat 1500-token ceiling and
so never reported the duplicates it exists to find. The ceiling now scales, but the
rows it should have caught are already written — this re-runs just that stage, without
re-reading traffic or re-proposing anything.

    python3 tools/resweep.py             # show what it would drop, change nothing
    python3 tools/resweep.py --apply
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import config, db, llm  # noqa: E402
from memcal.dream import sweep as sweep_stage  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--home")
    args = ap.parse_args()

    cfg = config.load(args.home)
    conn = db.open_db(cfg.db_path)
    client = llm.OpenRouter(cfg.api_key)

    snapshot = sweep_stage.state_snapshot(conn, cfg, [])
    ceiling = sweep_stage.sweep_ceiling(snapshot)
    print(f"snapshot {len(snapshot)} chars, ceiling {ceiling} tokens, "
          f"model {cfg.sweep_model}\n")

    if args.apply:
        result, actions = sweep_stage.sweep(client, conn, cfg, [])
        print("\n".join(actions) or "(no actions)")
        conn.close()
        return 0

    # Dry run: same call, but nothing is written back.
    reply = client.complete(
        model=cfg.sweep_model,
        prefix=sweep_stage.SWEEP_INSTRUCTIONS,
        suffix=snapshot + "\n\nReview this state.",
        schema=sweep_stage.SWEEP_SCHEMA,
        schema_name="memcal_sweep",
        max_tokens=ceiling,
    )
    print(f"truncated={reply.truncated} out={reply.usage.completion_tokens} "
          f"cost=${reply.usage.cost:.4f}\n")
    print(json.dumps(reply.data, indent=2) if reply.data else reply.text[:2000])
    print("\ndry run — re-run with --apply to act on this")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
