"""Persists model calls locally to JSON files sharded by run.

Each file records prompt prefix and suffix, response completion, reasoning,
routing metadata, usage statistics, and bundle identifiers. Failed requests
are recorded alongside successful ones.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path

from . import db

#: Upper character bound on stored prompt and response text fields.
MAX_FIELD_CHARS = 4_000_000


def root(home: Path) -> Path:
    return Path(home) / "calls"


def shard(home: Path, run_id: int | None) -> Path:
    return root(home) / (f"run-{run_id:04d}" if run_id else "live")


def path_for(home: Path, generation_id: str, run_id: int | None = None) -> Path:
    return shard(home, run_id) / f"{generation_id}.json"


def find(home: Path, generation_id: str, run_id: int | None = None) -> Path | None:
    """Locates the path to a saved generation file, searching across shards if run_id is omitted."""
    if not generation_id:
        return None
    if run_id is not None:
        direct = path_for(home, generation_id, run_id)
        if direct.is_file():
            return direct
    for candidate in root(home).glob(f"*/{generation_id}.json"):
        return candidate
    return None


def _clip(text: str | None) -> str:
    text = str(text or "")
    return text if len(text) <= MAX_FIELD_CHARS else text[:MAX_FIELD_CHARS] + "\n…[clipped]"


def save(home: Path, *, reply, stage: str, run_id: int | None = None,
         label: str = "", model: str = "", prefix: str = "", suffix: str = "",
         max_tokens: int = 0, bundles: list[dict] | None = None,
         extra: dict | None = None) -> Path | None:
    """Saves a completed model call record to disk."""
    generation_id = (getattr(reply, "generation_id", "") or "").strip()
    if not generation_id:
        return None
    usage = getattr(reply, "usage", None)
    payload = {
        "generation_id": generation_id,
        "run_id": run_id,
        "stage": stage,
        "label": label,
        "model": model or getattr(reply, "model", ""),
        "at": db.now(),
        "max_tokens": int(max_tokens or 0),
        "finish_reason": getattr(reply, "finish_reason", ""),
        "truncated": bool(getattr(reply, "truncated", False)),
        # Records the serving capacity tier reported by the provider.
        "service_tier": getattr(reply, "service_tier", ""),
        "usage": {
            "prompt_tokens": getattr(usage, "prompt_tokens", 0),
            "completion_tokens": getattr(usage, "completion_tokens", 0),
            "cached_tokens": getattr(usage, "cached_tokens", 0),
            "cost": getattr(usage, "cost", 0.0),
        },
        # Request retry count and total wait time.
        "requests": int(getattr(reply, "requests", 1) or 1),
        "waited": float(getattr(reply, "waited", 0.0) or 0.0),
        "bundles": bundles or [],
        "prefix": _clip(prefix),
        "suffix": _clip(suffix),
        "reasoning": _clip(getattr(reply, "reasoning", "")),
        "completion": _clip(getattr(reply, "text", "")),
        "parsed": getattr(reply, "data", None),
        **(extra or {}),
    }
    target = path_for(home, generation_id, run_id)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, json.dumps(payload, indent=2, ensure_ascii=False,
                                         default=str))
    except OSError:
        return None
    return target


def save_failure(home: Path, *, run_id: int | None, stage: str, label: str,
                 error: str, model: str = "", prefix: str = "", suffix: str = "",
                 max_tokens: int = 0, bundles: list[dict] | None = None,
                 requests: int = 0, waited: float = 0.0) -> Path | None:
    """Saves a failed model request to disk keyed by payload hash.

    Stored as a JSON file in the shard directory without inserting a row into `generations`.
    """
    refs = bundles or []
    seed = "|".join([stage, label, *(str(b.get("entity") or "") for b in refs)])
    name = "fail-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
    payload = {
        "generation_id": "",
        "failed": True,
        "error": _clip(error),
        "run_id": run_id,
        "stage": stage,
        "label": label,
        "model": model,
        "at": db.now(),
        "max_tokens": int(max_tokens or 0),
        "requests": int(requests or 0),
        "waited": float(waited or 0.0),
        "bundles": refs,
        "prefix": _clip(prefix),
        "suffix": _clip(suffix),
    }
    target = shard(home, run_id) / f"{name}.json"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, json.dumps(payload, indent=2, ensure_ascii=False,
                                         default=str))
    except OSError:
        return None
    return target


def failures_for_run(home: Path, run_id: int | None) -> list[dict]:
    """Returns all recorded failed requests for a run, ordered chronologically."""
    out = []
    for path in sorted(shard(home, run_id).glob("fail-*.json")):
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda blob: str(blob.get("at") or ""))


def _atomic_write(target: Path, text: str) -> None:
    """Writes content to a temporary file and atomically replaces the target path."""
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load(home: Path, generation_id: str, run_id: int | None = None) -> dict | None:
    """Loads a saved call JSON record by generation ID, or None if not found."""
    path = find(home, generation_id, run_id)
    if not path:
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def annotate(home: Path, generation_id: str, run_id: int | None = None, **fields) -> None:
    """Updates an existing call record with additional metadata fields."""
    path = find(home, generation_id, run_id)
    if not path:
        return
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
        blob.update(fields)
        _atomic_write(path, json.dumps(blob, indent=2, ensure_ascii=False, default=str))
    except (OSError, ValueError):
        pass


def for_run(conn: sqlite3.Connection, home: Path, run_id: int) -> list[dict]:
    """Returns all calls for a run ordered by generation ID, merged with disk records and provenance."""
    out = []
    rows = conn.execute(
        "SELECT * FROM generations WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
    for n, row in enumerate(rows, 1):
        gid = row["generation_id"]
        blob = load(home, gid, run_id) or {}
        out.append({
            "gen": gid,
            # 1-based call index within the run.
            "n": n,
            "stage": row["stage"],
            "label": row["label"] or "",
            "model": row["model"] or blob.get("model") or "",
            "at": str(row["created_at"])[:19],
            "prompt_tokens": row["prompt_tokens"],
            "completion_tokens": row["completion_tokens"],
            "cost": round(row["cost_usd"] or 0, 5),
            "max_tokens": row["max_tokens"],
            # HTTP request count, or None if not recorded.
            "requests": row["requests"] if "requests" in row.keys() else None,
            "saved": bool(blob),
            "finish_reason": blob.get("finish_reason", ""),
            "truncated": blob.get("truncated", False),
            "bundles": blob.get("bundles") or [],
            "routed": blob.get("routed"),
            "unrouted": blob.get("unrouted"),
            "echoed": blob.get("echoed"),
            "wrote": [{"kind": p["kind"], "ref": p["ref"], "verb": p["verb"]}
                      for p in conn.execute(
                          "SELECT * FROM provenance WHERE generation_id = ? ORDER BY id",
                          (gid,)).fetchall()],
        })
    return out


def ordinal(conn: sqlite3.Connection, generation_id: str) -> int:
    """Returns the 1-based sequence index of a call within its run based on `generations.id` order."""
    row = conn.execute(
        """SELECT (SELECT count(*) FROM generations x
                    WHERE x.run_id = g.run_id AND x.id <= g.id) AS n
             FROM generations g WHERE g.generation_id = ? AND g.run_id IS NOT NULL""",
        (generation_id,)).fetchone()
    return int(row["n"]) if row else 0


def prune(home: Path, keep_runs: int = 60) -> int:
    """Prunes oldest run shard directories to keep total run shards within keep_runs."""
    shards = sorted((p for p in root(home).glob("run-*") if p.is_dir()),
                    key=lambda p: p.name)
    gone = 0
    for old in shards[:-keep_runs] if len(shards) > keep_runs else []:
        for f in old.glob("*.json"):
            f.unlink(missing_ok=True)
            gone += 1
        old.rmdir()
    return gone
