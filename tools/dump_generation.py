#!/usr/bin/env python3
"""Backfill the on-disk call store from OpenRouter, for runs made before it existed."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import calls, config, db, trace  # noqa: E402
from memcal.dream import propose  # noqa: E402
from memcal.llm import _parse_json  # noqa: E402

BUNDLE_RE = re.compile(r"^BUNDLE (.+)$", re.MULTILINE)


class _Reply:
    """Enough of `llm.Reply` for `calls.save`, rebuilt from a stored generation."""

    def __init__(self, blob: dict, stats: dict, row) -> None:
        output = blob.get("output") or {}
        self.text = str(output.get("completion") or "")
        self.data = _parse_json(self.text)
        self.reasoning = str(output.get("reasoning") or "")
        self.generation_id = row["generation_id"]
        self.model = row["model"] or ""
        self.finish_reason = str(stats.get("finish_reason")
                                 or stats.get("native_finish_reason") or "")
        self.usage = type("U", (), {
            "prompt_tokens": stats.get("native_tokens_prompt") or row["prompt_tokens"],
            "completion_tokens": (stats.get("native_tokens_completion")
                                  or row["completion_tokens"]),
            "cached_tokens": stats.get("native_tokens_cached") or 0,
            "cost": row["cost_usd"] or 0.0,
        })()

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"


def _messages(blob: dict) -> tuple[str, str]:
    """(prefix, suffix) — the system turn and the user turn, as they were sent."""
    system, user = [], []
    for message in (blob.get("input") or {}).get("messages") or []:
        body = message.get("content")
        if isinstance(body, list):
            body = "\n".join(p.get("text") or "" for p in body if isinstance(p, dict))
        (system if message.get("role") == "system" else user).append(str(body or ""))
    return "\n".join(system), "\n".join(user)


def _bundles_from(suffix: str) -> list[dict]:
    """Every bundle in the request, read back off its own header.

    The `generations.label` column holds only the first four entities and a "+N more",
    which is exactly why run 5's error message could name four of six. The prompt has
    all of them.
    """
    out = []
    for raw in BUNDLE_RE.findall(suffix):
        head = raw.strip()
        label = ""
        match = re.search(r"\s*\(([^()]*)\)\s*$", head)
        if match:
            label, head = match.group(1), head[:match.start()].strip()
        out.append({"id": propose.bundle_id(head), "entity": head,
                    "label": label or head.split(":", 1)[-1], "lines": 0})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*", help="generation ids; omit to use --run/--all")
    ap.add_argument("--run", type=int, help="every generation in this run")
    ap.add_argument("--all", action="store_true", help="every generation on record")
    ap.add_argument("--force", action="store_true", help="re-fetch calls already saved")
    ap.add_argument("--home")
    args = ap.parse_args()

    cfg = config.load(args.home)
    conn = db.open_db(cfg.db_path)

    if args.all:
        rows = conn.execute("SELECT * FROM generations ORDER BY id").fetchall()
    elif args.run:
        rows = conn.execute("SELECT * FROM generations WHERE run_id = ? ORDER BY id",
                            (args.run,)).fetchall()
    else:
        rows = [r for gid in args.ids for r in conn.execute(
            "SELECT * FROM generations WHERE generation_id = ?", (gid,)).fetchall()]

    print(f"{len(rows)} generation(s)")
    saved = skipped = failed = 0
    for row in rows:
        gid, run_id = row["generation_id"], row["run_id"]
        if not args.force and calls.find(cfg.home, gid, run_id):
            skipped += 1
            continue
        try:
            content = trace.fetch(cfg.api_key, gid)
        except trace.TraceError as exc:
            print(f"  !! {gid}: {exc}")
            failed += 1
            continue
        stats = trace.stats(cfg.api_key, gid)
        prefix, suffix = _messages(content)
        reply = _Reply(content, stats, row)
        bundles = _bundles_from(suffix) if row["stage"] in ("propose", "live") else []

        calls.save(cfg.home, reply=reply, stage=row["stage"], run_id=run_id,
                   label=row["label"] or "", model=row["model"] or "",
                   prefix=prefix, suffix=suffix, max_tokens=row["max_tokens"],
                   bundles=bundles, extra={"backfilled": True})

        if bundles:
            payload = reply.data if isinstance(reply.data, dict) else {}
            returned = payload.get("bundles")
            echoed = [str((d or {}).get("entity") or "")
                      for d in (returned or []) if isinstance(d, dict)]
            keys = {propose._route_key(b["entity"]): b for b in bundles}
            landed = {}
            for index, name in enumerate(echoed):
                hit = keys.get(propose._route_key(name))
                if hit is None and len(echoed) == len(bundles):
                    hit = bundles[index]          # the positional fallback, as it ran
                if hit:
                    landed[hit["id"]] = hit
            calls.annotate(cfg.home, gid, run_id, echoed=echoed,
                           routed=list(landed.values()),
                           unrouted=[b for b in bundles if b["id"] not in landed])
        saved += 1
        state = "" if bundles else "  (no bundles — not a propose call)"
        print(f"  {gid}  {str(row['label'])[:44]:44}{state}")

    print(f"\nsaved {saved} · already had {skipped} · failed {failed}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
