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
        # A store whose spool is drained has nothing pending to render. Reading already
        # processed rows would need a different query; say so rather than probe on air.
        print("no pending bundles — run `memcal ingest all` first, or use --limit higher")
        return 1

    bundles = sorted(bundles, key=lambda b: len(b.render("v1")))
    if args.largest:
        # The tiny-bundle case is what truncated in production (four cheap bundles, four
        # separate judgements, an allowance sized for how little text they carried), but
        # it is not the ceiling. The biggest conversation in the store is, so probe both
        # ends or the numbers describe only the easy half.
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
                # Deliberately generous: we are measuring what it spends when nothing
                # stops it, not what it does under a ceiling.
                max_tokens=32_000,
                capture_reasoning=True,
                reasoning_effort=args.effort,
            )
        except Exception as exc:                       # a probe must report, not raise
            print(f"{count:>7}  FAILED: {type(exc).__name__}: {str(exc)[:120]}")
            continue

        # Reasoning comes back as text, not a token count, so it is estimated the same
        # way the packer estimates everything else — being consistent with `pack()`
        # matters more here than being exactly right, since these numbers feed the
        # ceiling `pack()` computes.
        # From the API, never from len(reply.reasoning): OpenAI shows a summary and keeps
        # the rest encrypted, so the visible text undercounts by an unknown amount. Fall
        # back to the estimate only for providers that report no breakdown at all, and
        # say which one is being used so the numbers can be trusted or discounted.
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
        # think_tokens is a floor, so take the worst case rather than the mean: sizing it
        # to the average guarantees the expensive half of requests truncates.
        think = int(round(max(per_bundle) / 100.0) * 100)
        # A boost below 1.0 means the existing ceiling was never the binding constraint,
        # so 1.0 is the floor to suggest — the ceiling is free headroom, and shrinking it
        # below what the formula already gives buys nothing and risks truncation.
        boost = max(1.0, round(max(ratios) * 1.5, 1)) if ratios else 1.0
        print("\nsuggested ENDPOINTS values:")
        print(f"  think_tokens  = {think:_}   (worst per-bundle reasoning, rounded up)")
        print(f"  ceiling_boost = {boost}   (worst spend vs output_ceiling, +50% margin)")
        print("\nNote: max_tokens is a ceiling, not a charge. Headroom is nearly free;")
        print("truncating costs the whole request. Round up when in doubt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
