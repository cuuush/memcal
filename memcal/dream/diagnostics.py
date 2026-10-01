"""Durable stage events and crash details for CLI, web, and scheduled dreams."""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from pathlib import Path

from .. import calls, db


def path_for(home: Path, run_id: int) -> Path:
    return calls.shard(home, run_id) / "dream.jsonl"


def read(home: Path, run_id: int) -> list[dict]:
    path = path_for(home, run_id)
    if not path.exists():
        return []
    rows = []
    try:
        for line in path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                rows.append({"event": "logging-error", "error": "Unreadable diagnostic entry"})
    except OSError as exc:
        rows.append({"event": "logging-error", "error": f"Cannot read {path}: {exc}"})
    return rows


def error_stage(error: str) -> str:
    if (error.startswith("question coverage:") or "bundle(s) left queued" in error
            or (error[:1].isdigit() and "bundle(s) [" in error)
            or error.startswith("circuit breaker opened")):
        return "propose"
    for stage in ("prepare", "propose", "merge", "apply", "wakes", "sweep", "render"):
        if error.startswith(stage + ":") or error.startswith(stage + " ("):
            return stage
    return ""


class Journal:
    def __init__(self, home: Path):
        self.home = home
        self.path = home / "calls" / "live" / f"dream-{uuid.uuid4().hex}.jsonl"
        self.stage = "prepare"
        self.lock = threading.Lock()

    def record(self, event: str, data: dict, run_id: int | None = None) -> None:
        with self.lock:
            if event == "stage" and data.get("state") == "running":
                self.stage = data.get("stage") or self.stage
            try:
                if run_id:
                    target = path_for(self.home, run_id)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if self.path != target and self.path.exists():
                        self.path.replace(target)
                    self.path = target
                self.path.parent.mkdir(parents=True, exist_ok=True)
                row = {"at": db.now(), "event": event, "stage": self.stage, **data}
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a") as stream:
                    stream.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
            except OSError as exc:
                print(f"dream logging failed: cannot save {self.path}: {type(exc).__name__}: {exc}",
                      file=sys.stderr)
                raise
