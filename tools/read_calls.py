#!/usr/bin/env python3
"""Read the on-disk call store of a scratch (or real) memcal home."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

FIELDS = ("reasoning", "completion", "prefix", "suffix", "parsed")


def load(home: Path) -> list[dict]:
    out = []
    for path in sorted((home / "calls").glob("run-*/*.json")):
        blob = json.loads(path.read_text(encoding="utf-8"))
        blob["_path"] = path
        blob["_run"] = path.parent.name
        out.append(blob)
    out.sort(key=lambda b: (b["_run"], b.get("at", "")))
    for n, blob in enumerate(out, 1):
        blob["_n"] = n
    return out


def index(calls: list[dict]) -> None:
    for blob in calls:
        usage = blob.get("usage") or {}
        print(f"[{blob['_n']:2}] {blob['_run']}  {blob.get('stage'):8} "
              f"{usage.get('completion_tokens', 0):5} out / {blob.get('max_tokens')} cap  "
              f"{'TRUNCATED' if blob.get('truncated') else blob.get('finish_reason', '')}")
        for b in blob.get("bundles") or []:
            print(f"        {b.get('id')}  {b.get('entity')}  ({b.get('lines')} lines)")
        if blob.get("unrouted"):
            print(f"        UNROUTED: {[b.get('entity') for b in blob['unrouted']]}")


def grep(calls: list[dict], pattern: str, field: str, window: int) -> None:
    rx = re.compile(pattern, re.I)
    for blob in calls:
        for name in ([field] if field != "any" else FIELDS):
            text = blob.get(name)
            if not isinstance(text, str):
                text = json.dumps(text, ensure_ascii=False) if text else ""
            for match in rx.finditer(text):
                lo = max(0, match.start() - window)
                hi = min(len(text), match.end() + window)
                print(f"\n=== call {blob['_n']} ({blob['_run']}, {name}) "
                      f"@{match.start()} ===")
                print(text[lo:hi].strip())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--call", type=int, default=None)
    ap.add_argument("--show", default="reasoning", choices=FIELDS)
    ap.add_argument("--grep", default=None)
    ap.add_argument("--field", default="reasoning",
                    choices=(*FIELDS, "any"))
    ap.add_argument("--window", type=int, default=700)
    args = ap.parse_args()

    calls = load(Path(args.home).expanduser())
    if args.grep:
        grep(calls, args.grep, args.field, args.window)
    elif args.call:
        blob = next(b for b in calls if b["_n"] == args.call)
        value = blob.get(args.show)
        print(value if isinstance(value, str)
              else json.dumps(value, indent=2, ensure_ascii=False))
    else:
        index(calls)


if __name__ == "__main__":
    main()
