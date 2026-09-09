#!/usr/bin/env python3
"""Re-derive `tests/token_calibration.json` from local saved call records.

Every saved generation carries the provider's own `usage.prompt_tokens` for the
request text. This walks those records and writes one row per call holding the
character-class counts of that text and the token count it really cost — counts
only, never a character of the message itself, so the result is safe to commit
from a private home directory into a public repository.

    python3 tools/token_calibration.py            # report only
    python3 tools/token_calibration.py --write    # refresh the fixture
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import textclean  # noqa: E402

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "token_calibration.json"
FIELDS = ("letters", "spaces", "digits", "marks", "wide", "tokens")


def rows(calls_dir: Path) -> list[dict[str, int]]:
    """One row per saved call: class counts, the estimate, and the real token cost."""
    out = []
    for shard in sorted(calls_dir.glob("run-*/gen-*.json")):
        try:
            record = json.loads(shard.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        real = (record.get("usage") or {}).get("prompt_tokens")
        text = (record.get("prefix") or "") + (record.get("suffix") or "")
        if not real or not text:
            continue
        row = textclean.character_classes(text)
        row["tokens"] = int(real)
        row["estimate"] = textclean.estimate_tokens(text)
        out.append(row)
    return out


def report(data: list[dict[str, int]]) -> None:
    if not data:
        print("no saved calls carry usage.prompt_tokens")
        return
    est = sum(row["estimate"] for row in data)
    real = sum(row["tokens"] for row in data)
    ratios = sorted(row["estimate"] / row["tokens"] for row in data)
    under = sum(1 for row in data if row["estimate"] < row["tokens"])
    print(f"calls        {len(data)}")
    print(f"estimate     {est:,}")
    print(f"real         {real:,}")
    print(f"ratio        {est / real:.3f}")
    print(f"worst / best {ratios[0]:.3f} / {ratios[-1]:.3f}")
    print(f"under-counts {under} / {len(data)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--calls", type=Path, default=Path.home() / ".memcal" / "calls",
                    help="directory of saved run-*/gen-*.json records")
    ap.add_argument("--write", action="store_true", help="rewrite the committed fixture")
    args = ap.parse_args()

    if not args.calls.is_dir():
        print(f"no call records under {args.calls}", file=sys.stderr)
        return 1
    data = rows(args.calls)
    report(data)
    if args.write and data:
        # FIELDS only: the estimate is recomputed by the test, and nothing else in a
        # record is safe to commit.
        keep = [{field: int(row[field]) for field in FIELDS} for row in data]
        FIXTURE.write_text(json.dumps(keep, indent=1) + "\n", encoding="utf-8")
        print(f"wrote {len(keep)} row(s) to {FIXTURE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
