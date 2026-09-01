#!/usr/bin/env python3
"""Run the isolated synthetic temporal benchmark across deterministic and model layers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memcal import archive, brief, db                                     # noqa: E402
from memcal import llm as _llm                                            # noqa: E402
from memcal.dream import bundle as bundle_stage                           # noqa: E402
from memcal.dream import run as dream_run                                 # noqa: E402
from tests.scenarios import expect, integration, load, probes             # noqa: E402
from tests.scenarios import skeleton as sk                                # noqa: E402

OUT = ROOT / "tools" / "bench_output" / "temporal"
GREEN, RED, YELLOW, DIM, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
LAYER_ALIASES = {"replay": "integration", "live": "model"}
STATUS_HEARTBEAT_SECONDS = 15.0


def _elapsed(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


class BenchmarkStatus:
    """Flushed model progress plus a heartbeat while one CLI call is outstanding."""

    def __init__(self, enabled: bool, label: str, *,
                 every: float = STATUS_HEARTBEAT_SECONDS, stream=None):
        self.enabled = enabled
        self.label = label
        self.every = every
        self.stream = stream or sys.stderr
        self.started = time.monotonic()
        self.last_event = self.started
        self.current = label
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not self.enabled:
            return
        self.update(self.label)
        self._thread = threading.Thread(
            target=self._heartbeat, name="memcal-benchmark-status", daemon=True)
        self._thread.start()

    def close(self) -> None:
        if not self.enabled:
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=min(1.0, self.every + 0.1))

    def update(self, message: str) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        with self._lock:
            self.current = " ".join(str(message).split())
            self.last_event = now
            current = self.current
        print(f"[bench {_elapsed(now - self.started)}] {current}",
              file=self.stream, flush=True)

    def progress(self, day: int, event: str, data: dict) -> None:
        """Translate dream's structured progress events into stable terminal text."""
        prefix = f"day {day}/{len(sk.DAYS)}"
        if event == "stage":
            stage = str(data.get("stage") or "model")
            state = str(data.get("state") or "running")
            note = str(data.get("note") or "")
            self.update(f"{prefix} · {stage} {state}" + (f" · {note}" if note else ""))
            return
        if event == "propose_wave":
            kind = str(data.get("kind") or "main")
            self.update(
                f"{prefix} · propose dispatch · {data.get('requests', 0)} request(s) · "
                f"{data.get('bundles', 0)} bundle(s) · {kind}")
            return
        if event == "propose_request":
            outcome = "finished" if data.get("ok") else "failed"
            self.update(
                f"{prefix} · propose {data.get('done', 0)}/{data.get('total', 0)} "
                f"bundles · request {data.get('index', '?')} {outcome}")

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.every):
            now = time.monotonic()
            with self._lock:
                current = self.current
                quiet = now - self.last_event
            print(
                f"[bench {_elapsed(now - self.started)}] still running · {current} · "
                f"{int(quiet)}s since the last model event",
                file=self.stream, flush=True)


def canonical_layer(layer: str) -> str:
    """Canonical public layer name; old names remain accepted CLI aliases."""
    return LAYER_ALIASES.get(layer, layer)


def _pin(day: int) -> None:
    db.set_today(sk.DAYS[day - 1])


def _selected(check, case: str | None) -> bool:
    if not case:
        return True
    needle = case.casefold()
    return needle in check.id.casefold() or needle in check.challenge.casefold()


def grade(ctx, day: int, *, case: str | None = None) -> list[dict]:
    """Run every check filed under this day. A check that raises is a failure."""
    out = []
    for check in expect.CHECKS:
        if check.day != day or not _selected(check, case):
            continue
        try:
            ok, note = check.fn(ctx)
        except Exception as exc:                      # a check must never take the run down
            ok, note = False, f"check raised: {type(exc).__name__}: {exc}"
        out.append({"id": check.id, "challenge": check.challenge, "day": day,
                    "ok": bool(ok), "soft": check.soft,
                    "frontier": check.frontier, "note": str(note)[:200]})
    return out


def measure(ctx, day: int) -> list[dict]:
    out = []
    for item in expect.MEASURES:
        if item.day != day:
            continue
        try:
            value = item.fn(ctx)
        except Exception as exc:
            value = f"raised: {exc}"
        out.append({"id": item.id, "label": item.label, "value": str(value)})
    return out


def _table_state(conn, table: str, *, omit: set[str] | None = None) -> list[dict]:
    omit = omit or set()
    rows = []
    for row in conn.execute(f"SELECT * FROM {table}"):
        rows.append({key: row[key] for key in row.keys() if key not in omit})
    return sorted(rows, key=lambda row: json.dumps(row, sort_keys=True, default=str))


def semantic_state(conn, cfg, *, audit: bool = False) -> dict:
    """Canonical product state for redo comparisons.

    Wall-clock columns and surrogate ids are deliberately omitted. A redo may happen
    seconds later and still be semantically identical; duplicate history, evidence or
    provenance rows remain visible because the normalized row occurs twice.
    """
    if audit:
        return {
            "provenance": _table_state(
                conn, "provenance", omit={"id", "at", "run_id", "generation_id"}),
            "evidence": _table_state(conn, "evidence", omit={
                "id", "attached_at", "run_id", "generation_id"}),
        }
    wiki_files = {}
    if cfg.wiki_dir.exists():
        for path in sorted(cfg.wiki_dir.rglob("*.md")):
            wiki_files[str(path.relative_to(cfg.wiki_dir))] = path.read_text()
    return {
        "events": _table_state(conn, "events", omit={"id", "created_at", "updated_at"}),
        "event_history": _table_state(
            conn, "event_history", omit={"id", "event_id", "changed_at"}),
        "todos": _table_state(
            conn, "todos", omit={"id", "opened_at", "updated_at", "closed_at", "woke_at"}),
        "questions": _table_state(
            conn, "questions", omit={"id", "asked_at", "answered_at"}),
        "standing": _table_state(conn, "standing", omit={"id", "created_at", "updated_at"}),
        "slot_history": _table_state(conn, "slot_history", omit={"id", "changed_at"}),
        "wiki": wiki_files,
    }


def _state_note(before: dict, after: dict) -> str:
    changed = [name for name in sorted(set(before) | set(after))
               if before.get(name) != after.get(name)]

    def digest(value):
        return hashlib.sha1(
            json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:8]

    return ("identical" if not changed else
            f"changed {changed}; {digest(before)} -> {digest(after)}")


def _redo_checks(layer: str, conn, cfg, args, progress=None) -> tuple[list[dict], list[str]]:
    """Actually re-read day 2 and grade both product and audit idempotence."""
    if args.case and "18" not in args.case.casefold() and "idempot" not in args.case.casefold():
        return [], []
    before = semantic_state(conn, cfg)
    before_audit = semantic_state(conn, cfg, audit=True)
    errors: list[str] = []
    if layer == "integration":
        archive.spool_reset(conn, since=sk.DAY2)
        note = integration.apply_day(conn, cfg, 2)
    else:
        result = dream_run.dream(
            conn, cfg, mode="nightly", redo=sk.DAY2, skip_sweep=args.skip_sweep,
            progress=progress)
        note = result.report()
        errors.extend(f"redo: {error}" for error in result.errors)
    after = semantic_state(conn, cfg)
    after_audit = semantic_state(conn, cfg, audit=True)
    return [
        {"id": "redo.product-state", "challenge": "18 idempotence", "day": 2,
         "ok": before == after, "soft": False, "frontier": False,
         "note": f"{_state_note(before, after)}; {note.splitlines()[0]}"},
        {"id": "redo.audit-state", "challenge": "18 idempotence", "day": 2,
         "ok": before_audit == after_audit, "soft": False, "frontier": True,
         "note": _state_note(before_audit, after_audit)},
    ], errors


def run_layer(layer: str, home: Path, args) -> dict:
    """Seed, then for each fake day: pin the clock, ingest, pass, grade."""
    layer = canonical_layer(layer)
    conn, cfg = load.seed(home)
    if args.model:
        cfg.propose_model = args.model
    cfg.bundle_format = args.format
    if args.prompt_version:
        cfg.prompt_version = args.prompt_version
    if args.effort:
        cfg.reasoning_effort = args.effort
    if args.pack:
        cfg.pack_bundles = args.pack
    if args.stages is not None:
        cfg.propose_stages = args.stages

    trial = int(getattr(args, "_trial", 1))
    trials = int(getattr(args, "_trials", 1))
    trial_text = f" · trial {trial}/{trials}" if trials > 1 else ""
    status = BenchmarkStatus(
        layer == "model" and not args.dry_run,
        f"core · provider {cfg.llm_provider} · model {cfg.propose_model}{trial_text}")
    status.start()

    results: list[dict] = []
    measures: list[dict] = []
    log: list[str] = []
    started = time.time()

    for day in range(1, len(sk.DAYS) + 1):
        _pin(day)
        print(f"\n{DIM}--- fake day {day} — {db.today()} ---{OFF}")
        status.update(f"day {day}/{len(sk.DAYS)} · ingesting fixture traffic")
        load.ingest_day(conn, cfg, day, quiet=True)

        if layer == "integration":
            note = integration.apply_day(conn, cfg, day)
        else:
            status.update(f"day {day}/{len(sk.DAYS)} · starting dream pass")
            result = dream_run.dream(conn, cfg, mode="nightly",
                                     dry_run=args.dry_run, skip_sweep=args.skip_sweep,
                                     progress=lambda event, data, current=day:
                                     status.progress(current, event, data))
            note = result.report()
            if result.errors:
                log.extend(f"day {day}: {e}" for e in result.errors)
        if args.dry_run:
            print(note)
            # Pricing day 2 against the cumulative day-1 backlog quotes a pass that
            # production would never make: the real day-1 pass consumes those lines.
            # Advance only the scratch queue so the second quote prices day 2's delta.
            pending_ids = [row["id"] for row in conn.execute(
                "SELECT id FROM spool WHERE processed_at IS NULL")]
            archive.spool_mark(conn, pending_ids, result.run_id)
            continue
        print(f"  {note.splitlines()[0] if note else '(nothing)'}")
        status.update(f"day {day}/{len(sk.DAYS)} · grading resulting state")
        brief.write(conn, cfg)

        ctx = expect.Ctx(conn, cfg)
        day_results = grade(ctx, day, case=args.case)
        results.extend(day_results)
        measures.extend(measure(ctx, day))
        hard = [r for r in day_results if not r["soft"]]
        if hard:
            print(f"  day {day}: {sum(1 for r in hard if r['ok'])}/{len(hard)} checks")
        else:
            print(f"  day {day}: no matching checks; full traffic still ran")

        # Idempotence is "re-read the day you just read". It has to happen *here*,
        # while day 2 is the latest thing the store knows, and not after day 4: on a
        # store that has moved on two days, replaying day 2's diffs is a test of
        # walking backwards, which is challenge 10 and a different question.
        if day == 2 and not args.dry_run:
            status.update(f"day {day}/{len(sk.DAYS)} · replaying for idempotence")
            redo_results, redo_errors = _redo_checks(
                layer, conn, cfg, args,
                progress=lambda event, data, current=day:
                status.progress(current, event, data))
            results.extend(redo_results)
            log.extend(redo_errors)

    if args.dry_run and layer == "model":
        _pin(2)
        redo_price = dream_run.dream(
            conn, cfg, mode="nightly", dry_run=True, redo=sk.DAY2,
            skip_sweep=args.skip_sweep)
        print(f"\n{DIM}--- fake day 2 redo — idempotence price ---{OFF}")
        print(redo_price.report())

    _pin(len(sk.DAYS))
    ctx = expect.Ctx(conn, cfg)
    brief_text = ctx.brief()
    # What the two passes actually cost, off the `runs` rows rather than off a formula —
    # the whole point of making packing an axis is comparing a real bill to a real score.
    row = conn.execute(
        "SELECT count(*) AS runs, coalesce(sum(prompt_tokens),0) AS input,"
        " coalesce(sum(completion_tokens),0) AS output,"
        " coalesce(sum(cached_tokens),0) AS cached,"
        " coalesce(sum(cost_usd),0) AS cost FROM runs").fetchone()
    calls_made = conn.execute(
        "SELECT count(*) AS n FROM generations").fetchone()["n"]
    usage = {"calls": calls_made, "input": row["input"], "output": row["output"],
             "cached": row["cached"], "cost": round(row["cost"] or 0.0, 5)}
    status.update(f"complete · {calls_made} model call(s) · {time.time() - started:.1f}s")
    status.close()
    conn.close()
    # A run that lost bundles is not a low score, it is a *void* one. Unread traffic
    # grades identically to traffic the model read and understood nothing of, so the
    # number looks like a measurement and is not one: gpt-5.6-sol came back 152/192
    # having never been shown four of day 3's bundles. Say so loudly, and keep it out
    # of the history — a void run in the trend line is worse than a missing one.
    lost = [line for line in log if "left queued" in line or "429" in line]
    return {"layer": layer, "results": results, "measures": measures,
            "void": lost,
            "brief": brief_text, "errors": log,
            "seconds": round(time.time() - started, 1),
            "suite": "core",
            "model": cfg.propose_model if layer == "model" else "none",
            "format": cfg.bundle_format, "prompt": cfg.prompt_version,
            "effort": (cfg.reasoning_effort
                       or (_llm.endpoint(cfg.propose_model).reasoning_effort or "-")),
            "stages": cfg.propose_stages or "-",
            "pack": cfg.pack_bundles, "usage": usage}


def report(run: dict) -> str:
    """Grouped by challenge, because a challenge is the unit anyone cares about."""
    use = run.get("usage") or {}
    lines = [f"\n{'=' * 72}"]
    if run.get("void"):
        lines.append(
            f"{RED}VOID RUN — bundles went unread, so this is not a score{OFF}\n"
            + "\n".join(f"  {line}" for line in run["void"][:4])
            + f"\n  {DIM}Unread traffic grades the same as traffic the model read and "
              f"understood nothing of.{OFF}")
    lines += [f"suite {run.get('suite', 'core')}  ·  layer {run['layer']}"
             f"  ·  model {run['model']}  ·  format {run['format']}"
             f"  ·  prompt {run['prompt']}  ·  effort {run.get('effort', '-')}"
             f"  ·  stages {run.get('stages', '-')}"
             f"  ·  pack {run.get('pack')}"
             f"  ·  {run['seconds']}s",
             (f"{use.get('calls', 0)} calls  ·  {use.get('input', 0)} in "
              f"({use.get('cached', 0)} cached)  ·  {use.get('output', 0)} out  ·  "
              f"${use.get('cost', 0)}") if use.get("calls") else "",
             "=" * 72]
    by_challenge: dict[str, list] = {}
    for row in run["results"]:
        by_challenge.setdefault(row["challenge"], []).append(row)

    hard_pass = hard_total = 0
    for challenge, rows in by_challenge.items():
        hard = [r for r in rows if not r["soft"]]
        ok = sum(1 for r in hard if r["ok"])
        hard_pass += ok
        hard_total += len(hard)
        failed = [r for r in hard if not r["ok"]]
        if not failed:
            mark = GREEN + "PASS" + OFF
        elif all(r.get("frontier") for r in failed):
            mark = YELLOW + "GAP " + OFF
        else:
            mark = RED + "FAIL" + OFF
        lines.append(f"\n{mark}  {challenge}   ({ok}/{len(hard)})")
        for row in rows:
            if row["ok"]:
                glyph = f"{GREEN}  ok  {OFF}"
            elif row["soft"]:
                glyph = f"{YELLOW} soft {OFF}"
            elif row.get("frontier"):
                glyph = f"{YELLOW} gap  {OFF}"
            else:
                glyph = f"{RED} FAIL {OFF}"
            lines.append(f"  {glyph} {row['id']:28} {row['note']}")

    soft_fail = [r for r in run["results"] if r["soft"] and not r["ok"]]
    frontier_fail = [r for r in run["results"]
                     if r.get("frontier") and not r["ok"]]
    challenge_total = len(by_challenge)
    challenge_pass = sum(
        1 for rows in by_challenge.values()
        if all(row["ok"] for row in rows if not row["soft"]))
    lines.append(f"\n{'-' * 72}")
    lines.append(f"{hard_pass}/{hard_total} hard checks, "
                 f"{challenge_pass}/{challenge_total} challenges fully green, "
                 f"{len(frontier_fail)} open frontier gaps, "
                 f"{len(soft_fail)} soft failures")
    if run["measures"]:
        lines.append("\nmeasured (not graded):")
        for item in run["measures"]:
            lines.append(f"  {item['label']:32} {item['value']}")
    if run["errors"]:
        lines.append("\nerrors:")
        lines.extend(f"  {e}" for e in run["errors"])
    return "\n".join(lines)


def probe_run(name: str, home: Path, case: str | None = None) -> dict:
    started = time.time()
    # `main` always passes a named child of the benchmark root, never the root itself.
    # Reusing --home should reproduce a fresh run rather than accumulate probe rows.
    if home.exists():
        shutil.rmtree(home)
    if name == "contract":
        rows = probes.contract_checks()
    elif name == "boundaries":
        rows = probes.boundary_checks(home)
    elif name == "hermes":
        rows = probes.hermes_checks(home)
    elif name == "clock":
        rows = probes.clock_checks(home)
    elif name == "schedule":
        rows = probes.schedule_checks(home)
    elif name == "collection":
        rows = probes.collection_checks(home)
    else:
        raise ValueError(f"unknown probe suite {name}")
    if case:
        needle = case.casefold()
        rows = [row for row in rows
                if needle in row["id"].casefold()
                or needle in row["challenge"].casefold()]
    return {
        "suite": name, "layer": "probe", "results": rows, "measures": [],
        "brief": "", "errors": [], "seconds": round(time.time() - started, 1),
        "model": "none", "format": "-", "prompt": "-", "effort": "-",
        "pack": 0, "usage": {},
    }


def repeat_report(runs: list[dict]) -> str:
    """Per-check reliability, not one lucky model score."""
    if len(runs) < 2:
        return ""
    by_id: dict[str, list[dict]] = {}
    for run in runs:
        for row in run["results"]:
            if not row["soft"]:
                by_id.setdefault(row["id"], []).append(row)
    scores = [
        sum(1 for row in run["results"] if not row["soft"] and row["ok"])
        / max(1, sum(1 for row in run["results"] if not row["soft"]))
        for run in runs
    ]
    costs = [float((run.get("usage") or {}).get("cost") or 0) for run in runs]
    lines = [
        f"\n{'=' * 72}",
        f"model reliability across {len(runs)} independent trials",
        f"score range {min(scores):.1%}–{max(scores):.1%}"
        f" · mean {sum(scores) / len(scores):.1%}"
        f" · total cost ${sum(costs):.4f}",
        "=" * 72,
    ]
    unstable = []
    always_green = always_red = 0
    for check_id, rows in by_id.items():
        passed = sum(1 for row in rows if row["ok"])
        if passed == len(rows):
            always_green += 1
        elif passed == 0:
            always_red += 1
        else:
            unstable.append((passed / len(rows), check_id, rows[-1]["note"]))
    # How much of that headline was ever in play. A score dominated by checks the model
    # never moves is a bad summary of a model: comparing gpt-5.6-luna against
    # gpt-5.6-terra, 127 of 178 checks were green for both and the headline separated
    # them by two points — while on the 39 checks either model actually moved, the gap
    # was ten, and twenty-six once one memcal-side duplicate was set aside. Read this
    # line before the percentage.
    settled = always_green + always_red
    lines.append(
        f"  {settled} of {len(by_id)} checks never varied across these trials "
        f"({always_green} green, {always_red} red) — the headline is "
        f"{settled / max(1, len(by_id)):.0%} pre-decided. {len(unstable)} were in play.")
    if not unstable:
        lines.append("  every graded check passed every trial")
    else:
        lines.append("")
        for rate, check_id, note in sorted(unstable):
            lines.append(
                f"  {rate:>5.0%}  {check_id:32} {note[:100]}")
    return "\n".join(lines)


def combined_report(runs: list[dict]) -> str:
    """One headline across selected suites, without hiding each suite's findings."""
    if len(runs) < 2:
        return ""
    hard = [row for run in runs for row in run["results"] if not row["soft"]]
    challenges = {
        (run.get("suite", "core"), row["challenge"])
        for run in runs for row in run["results"]
    }
    green = {
        key for key in challenges
        if all(row["ok"] for run in runs for row in run["results"]
               if (run.get("suite", "core"), row["challenge"]) == key
               and not row["soft"])
    }
    gaps = [row for row in hard if row.get("frontier") and not row["ok"]]
    failures = [row for row in hard if not row["ok"] and not row.get("frontier")]
    return "\n".join([
        f"\n{'=' * 72}",
        "combined selected-suite baseline",
        f"{sum(1 for row in hard if row['ok'])}/{len(hard)} hard checks"
        f" · {len(green)}/{len(challenges)} challenges fully green"
        f" · {len(gaps)} frontier gaps · {len(failures)} established failures",
        "=" * 72,
    ])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", default=None,
                        help="parent for per-suite scratch homes (default: /tmp). "
                             "Never ~/.memcal")
    parser.add_argument(
        "--layer",
        choices=("integration", "model", "both", "replay", "live"),
        default="integration",
        help="integration = deterministic pipeline; model = actual LLM "
             "(replay/live remain deprecated aliases)")
    parser.add_argument("--model", default=None, help="override the propose model")
    parser.add_argument("--format", default="v1",
                        choices=sorted(bundle_stage.FORMATS), help="bundle wire format")
    parser.add_argument("--prompt-version", default=None, choices=("v1", "v2"))
    parser.add_argument("--effort", default=None, choices=("low", "medium", "high"),
                        help="override reasoning effort for every stage. Default is "
                             "whatever the model's llm.ENDPOINTS entry says, which was "
                             "measured against the live API. There is no 'auto' level")
    parser.add_argument("--stages", default=None, metavar="LIST",
                        help="cfg.propose_stages: 'on' for all four, or a comma-separated "
                             "order (calendar,todos,pages,questions). '' is the single "
                             "call. Costs one model turn per stage")
    parser.add_argument("--pack", type=int, default=None, metavar="N",
                        help="bundles per request (cfg.pack_bundles, default 6). "
                             "1 is the unpacked extreme")
    parser.add_argument("--dry-run", action="store_true", help="price the model run only")
    parser.add_argument("--skip-sweep", action="store_true")
    parser.add_argument(
        "--suite", default="all",
        help="comma-separated: core,contract,boundaries,clock,schedule,collection,hermes (default: all)")
    parser.add_argument("--case", default=None,
                        help="grade one check/challenge substring; traffic remains intact")
    parser.add_argument("--repeat", type=int, default=1, metavar="N",
                        help="run each model suite N independent times and report pass rates")
    parser.add_argument("--rebuild", action="store_true",
                        help="regenerate fixtures from the skeleton first")
    args = parser.parse_args()

    if args.rebuild:
        from tests.scenarios import build
        build.main()

    original_layer = args.layer
    args.layer = canonical_layer(args.layer)
    if original_layer != args.layer:
        print(f"{YELLOW}note:{OFF} --layer {original_layer} is deprecated; "
              f"use --layer {args.layer}")
    if args.dry_run and args.layer == "integration":
        args.layer = "model"
    if args.repeat < 1:
        parser.error("--repeat must be at least 1")

    # A model with no `llm.ENDPOINTS` entry runs on `Endpoint()` defaults: **no provider
    # pin**, no reasoning effort, `think_tokens = 0`. Every one of those quietly changes
    # what is being measured, and the first is the dangerous one — OpenRouter picks a
    # provider, and providers of identical weights do not agree on the one capability
    # the output contract rests on. Amazon Bedrock serves both gpt-5.6 models with
    # `response_format` absent, so an unpinned request can land there, drop the schema,
    # and produce exactly the run-5 failure: six bundles reasoned through correctly and
    # answered `{"bundles": []}`. That reads as "the model is worse" and is a routing
    # accident. Comparing models is the whole point of `--model`, so it has to refuse
    # rather than warn.
    if args.model and args.model not in _llm.ENDPOINTS:
        parser.error(
            f"{args.model} has no llm.ENDPOINTS entry, so it would run unpinned: "
            f"OpenRouter would choose the provider, and they disagree about "
            f"response_format. Add an entry (provider, json_mode, reasoning_effort, "
            f"ceiling_boost, think_tokens) before measuring it — the numbers are "
            f"properties of a deployment and cannot be read off a capability list. "
            f"Known: {', '.join(sorted(_llm.ENDPOINTS))}")

    valid_suites = ("core", "contract", "boundaries", "clock", "schedule", "collection",
                    "hermes")
    requested = [part.strip().lower() for part in args.suite.split(",") if part.strip()]
    suites = list(valid_suites) if "all" in requested else requested
    unknown = [name for name in suites if name not in valid_suites]
    if unknown:
        parser.error(f"unknown suite(s): {', '.join(unknown)}")

    base_home = Path(args.home).expanduser() if args.home else (
        Path("/tmp") / f"memcal-temporal-{int(time.time())}")
    OUT.mkdir(parents=True, exist_ok=True)

    layers = ("integration", "model") if args.layer == "both" else (args.layer,)
    core_runs: list[dict] = []
    probe_runs: list[dict] = []
    if "core" in suites:
        for layer in layers:
            trials = args.repeat if layer == "model" and not args.dry_run else 1
            model_trials: list[dict] = []
            for trial in range(1, trials + 1):
                args._trial, args._trials = trial, trials
                home = base_home / f"core-{layer}-{trial}"
                run = run_layer(layer, home, args)
                run["trial"] = trial
                if not args.dry_run:
                    print(report(run))
                    core_runs.append(run)
                    if layer == "model":
                        model_trials.append(run)
                    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                    tag = (f"core-{layer}-{args.format}"
                           + (f"-pack{args.pack}" if args.pack else "")
                           + (f"-trial{trial}" if trials > 1 else ""))
                    (OUT / f"{tag}-{stamp}.json").write_text(json.dumps(run, indent=1))
                    (OUT / f"{tag}-{stamp}.brief.md").write_text(run["brief"])
                    if run.get("void"):
                        print(f"{RED}trial produced no measurement — "
                              f"bundles went unread{OFF}")
            if model_trials:
                summary = repeat_report(model_trials)
                if summary:
                    print(summary)

    if not args.dry_run:
        for name in suites:
            if name == "core":
                continue
            run = probe_run(name, base_home / f"{name}-probe", args.case)
            print(report(run))
            probe_runs.append(run)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            (OUT / f"{name}-probe-{stamp}.json").write_text(json.dumps(run, indent=1))

    integration_run = next(
        (run for run in core_runs if run["layer"] == "integration"), None)
    model_runs = [run for run in core_runs if run["layer"] == "model"]
    if integration_run and model_runs:
        for index, model_run in enumerate(model_runs, 1):
            heading = "attribution" + (
                f" — model trial {index}" if len(model_runs) > 1 else "")
            print(f"\n{'=' * 72}\n{heading}\n{'=' * 72}")
            by_id = {row["id"]: row for row in integration_run["results"]}
            for row in model_run["results"]:
                was = by_id.get(row["id"])
                if not was or row["ok"]:
                    continue
                if was["ok"]:
                    print(f"  {YELLOW}model{OFF}   {row['id']:28} "
                          "passes integration, fails model")
                else:
                    print(f"  {RED}code{OFF}    {row['id']:28} fails both")
    representative_core = integration_run or (model_runs[0] if model_runs else None)
    combined = combined_report(
        ([representative_core] if representative_core else []) + probe_runs)
    if combined:
        print(combined)
    db.set_today(None)


if __name__ == "__main__":
    main()
