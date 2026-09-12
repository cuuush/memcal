#!/usr/bin/env python3
"""Probe model reasoning cost and strict-schema compliance."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import config, db, llm, textclean  # noqa: E402
from memcal.dream import bundle as bundle_mod, propose  # noqa: E402
from memcal.llm import OpenRouter  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="openai/gpt-5.6-luna")
    ap.add_argument("--counts", default="1,2,4",
                    help="bundle counts per request to probe, comma separated")
    ap.add_argument("--effort", default=None, help="override reasoning_effort")
    ap.add_argument("--limit", type=int, default=400,
                    help="spool rows to draw bundles from")
    ap.add_argument("--largest", action="store_true",
                    help="probe the biggest bundles instead of the smallest")
    args = ap.parse_args()

    cfg = config.load()
    conn = db.connect(cfg.db_path)
    client = OpenRouter(cfg.api_key)

    bundles = bundle_mod.build(conn, limit=args.limit)
    if not bundles:
        # A drained spool has nothing to render.
        print("no pending bundles — run `memcal ingest all` first, or use --limit higher")
        return 1

    bundles = sorted(bundles, key=lambda b: len(b.render("v1")))
    if args.largest:
        # Probe both size extremes. Small bundles alone understate the ceiling.
        bundles = bundles[::-1]
    prefix = propose.build_prefix(conn, cfg)

    print(f"model   {args.model}")
    print(f"spec    {llm.endpoint(args.model)}")
    print(f"pool    {len(bundles)} bundles pending\n")
    print(f"{'bundles':>7}  {'lines':>6}  {'in':>7}  {'out':>7}  {'think~':>7}  "
          f"{'ceiling':>7}  {'finish':>9}  think/bundle")
    print("-" * 84)

    per_bundle: list[float] = []
    ratios: list[float] = []
    for count in [int(c) for c in args.counts.split(",") if c.strip()]:
        group = bundles[:count]
        if len(group) < count:
            print(f"{count:>7}  (only {len(bundles)} bundles available, skipped)")
            continue
        suffix = propose.build_suffix(cfg, group, conn)
        try:
            reply = client.complete(
                model=args.model, prefix=prefix, suffix=suffix,
                schema=propose.schema_for(cfg), schema_name="memcal_diff",
                # Use a generous ceiling to measure unconstrained spend.
                max_tokens=32_000,
                capture_reasoning=True,
                reasoning_effort=args.effort,
            )
        except Exception as exc:                       # a probe must report, not raise
            print(f"{count:>7}  FAILED: {type(exc).__name__}: {str(exc)[:120]}")
            continue

        # Estimate reasoning text with the packer estimator for consistency.
        # Prefer the API token breakdown; fall back to the estimate when absent.
        think = reply.usage.reasoning_tokens
        measured = bool(think)
        if not measured and reply.reasoning:
            think = textclean.estimate_tokens(reply.reasoning)
        allowed = propose.output_ceiling(group)
        shaped = isinstance(reply.data, dict)
        per_bundle.append(think / count)
        ratios.append(reply.usage.completion_tokens / max(1, allowed))
        print(f"{count:>7}  {sum(len(b.items) for b in group):>6}  "
              f"{reply.usage.prompt_tokens:>7}  {reply.usage.completion_tokens:>7}  "
              f"{think:>7}{'' if measured else '~'}  {allowed:>7}  "
              f"{reply.finish_reason:>9}  {think / count:>7.0f}"
              f"{'' if shaped else '   [UNSHAPED — schema not honoured]'}")

    if per_bundle:
        # Size think_tokens to the worst case, not the mean.
        think = int(round(max(per_bundle) / 100.0) * 100)
        # Floor the suggested boost at 1.0.
        boost = max(1.0, round(max(ratios) * 1.5, 1)) if ratios else 1.0
        print("\nsuggested ENDPOINTS values:")
        print(f"  think_tokens  = {think:_}   (worst per-bundle reasoning, rounded up)")
        print(f"  ceiling_boost = {boost}   (worst spend vs output_ceiling, +50% margin)")
        print("\nNote: max_tokens is a ceiling, not a charge. Headroom is nearly free;")
        print("truncating costs the whole request. Round up when in doubt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
