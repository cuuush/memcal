"""Stage — semantic wake evaluation (post-apply, batched, fail-closed).

A waiter is a to-do with a `wake_condition`, e.g. "Give Rowan back their EZ-Pass"
waiting on "Rowan is back from Italy". It sleeps until new traffic shows the
condition came true.

Code nominates permissive `(todo, bundle)` candidates per bundle — words split
across unrelated bundles never jointly satisfy anything — and one structured
model call decides which conditions the candidate lines actually entail. The
model may wake a to-do; it may never close one. The existing follow-up question
remains the user's decision.

Fail closed: unavailable, malformed, or truncated output leaves every waiter
asleep. The call or failure is recorded through the normal trace path.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from .. import calls, db, todos, trace
from ..config import Config
from ..llm import CompletionClient

#: Arbitration model: heuristics nominate, the model judges — the same role the
#: merge stage gives `match_model` for ambiguous associations.
WAKE_MODEL_ATTR = "match_model"

WAKES_INSTRUCTIONS = """\
You decide whether new traffic shows a waiter's wake condition came true.

Each WAITER names a to-do and the condition it sleeps on, followed by the
candidate lines from one conversation that plausibly bear on it. Decide per
waiter whether those lines entail the condition is now true:

  satisfied   the lines state the condition came true — arrival, return,
              delivery, decision — even when paraphrased ("landed at JFK"
              for "back from Italy" when the context supports the return).
  not satisfied  negation ("is not back", "never went"), delay or future
              ("extended the trip another week", "lands next Tuesday"),
              mere discussion ("flights are expensive"), or anything said
              before the waiter opened.

Each candidate block is a separate conversation. Use lines from one block only
for each decision. Cite the supporting line tags (e.g. "L2") for every satisfied waiter;
an empty cite list means not satisfied. You may wake a waiter; you may not
close, drop, or amend anything. Unsure means not satisfied.\
"""

WAKES_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decisions"],
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["todo_key", "satisfied", "cites"],
                "properties": {
                    "todo_key": {"type": "string"},
                    "satisfied": {"type": "boolean"},
                    "cites": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}

@dataclass
class WakeCandidate:
    todo: todos.Todo
    bundle: object


def find_candidates(conn: sqlite3.Connection, bundles,
                    *, since: str | None = None) -> list[WakeCandidate]:
    """Permissive `(todo, bundle)` pairs. No database write."""
    return [WakeCandidate(todo=todo, bundle=bundle)
            for todo, bundle in todos.find_wake_candidates(conn, bundles, since=since)]


def _render_candidates(candidates: list[WakeCandidate]) -> tuple[
        str, dict[str, dict[str, tuple[int, int]]]]:
    """Render fresh lines and index each tag by waiter, bundle, and archive ID."""
    parts: list[str] = []
    citations: dict[str, dict[str, tuple[int, int]]] = {}
    by_todo: dict[str, list[WakeCandidate]] = {}
    for candidate in candidates:
        by_todo.setdefault(candidate.todo.key, []).append(candidate)
    for key in sorted(by_todo):
        todo = by_todo[key][0].todo
        try:
            opened = db.parse_ts(todo.opened_at, strict=True)
        except ValueError:
            continue
        blocks = []
        lines = {}
        for index, candidate in enumerate(by_todo[key]):
            bundle = candidate.bundle
            block = []
            for row in bundle.items:
                try:
                    said = db.parse_ts(row["ts"], strict=True)
                except ValueError:
                    continue
                body = " ".join(str(row["text"] or "").splitlines()).strip()
                if body and said > opened:
                    tag = f"L{len(lines) + 1}"
                    lines[tag] = (index, int(row["id"]))
                    block.append(f"    {tag} {body}")
            if block:
                blocks.append(f"  candidate from BUNDLE {bundle.entity} ({bundle.label}):")
                blocks.extend(block)
        if lines:
            citations[key] = lines
            parts.append(f"WAITER {todo.key} | {todo.text}")
            parts.append(f"  waiting on: {todo.wake_condition}")
            parts.extend(blocks)
    return "\n".join(parts), citations


def _ceiling(suffix: str) -> int:
    return min(8000, 1200 + len(suffix) // 4)


def _label(candidates: list[WakeCandidate]) -> str:
    keys = sorted({c.todo.key for c in candidates})
    shown = ", ".join(keys[:4])
    return shown + (f" +{len(keys) - 4} more" if len(keys) > 4 else "")


def _bundle_refs(candidates: list[WakeCandidate]) -> list[dict]:
    seen: dict[str, dict] = {}
    for candidate in candidates:
        bundle = candidate.bundle
        entity = str(getattr(bundle, "entity", ""))
        if entity and entity not in seen:
            seen[entity] = {"entity": entity,
                            "label": str(getattr(bundle, "label", entity) or entity)}
    return list(seen.values())


def _resolve_cites(lines: dict[str, tuple[int, int]], cites: list[str]) -> list[int]:
    if any(cite.strip().upper() not in lines for cite in cites):
        return []
    resolved = [lines[cite.strip().upper()] for cite in cites]
    if len({index for index, _ in resolved}) != 1:
        return []
    return list(dict.fromkeys(archive_id for _, archive_id in resolved))


def _valid_decision(item) -> tuple[str, bool, list] | None:
    if not isinstance(item, dict):
        return None
    key = item.get("todo_key")
    satisfied = item.get("satisfied")
    cites = item.get("cites")
    if not isinstance(key, str) or not key or not isinstance(satisfied, bool):
        return None
    if not isinstance(cites, list) or not all(isinstance(c, str) for c in cites):
        return None
    return key, satisfied, cites


def evaluate(client: CompletionClient, conn: sqlite3.Connection, cfg: Config,
             bundles, *, since: str | None = None,
             run_id: int | None = None) -> tuple[list[todos.Todo], list[str]]:
    """One batched entailment call over this run's candidates.

    Returns `(woken, problems)`. Fail closed: no candidates means no model call,
    and any unavailable, malformed, or truncated output leaves every waiter
    asleep with the failure recorded through the normal trace path.
    """
    if not getattr(cfg, "semantic_wakes", False):
        return [], []
    candidates = find_candidates(conn, bundles, since=since)
    if not candidates:
        return [], []
    suffix, citations = _render_candidates(candidates)
    if not citations:
        return [], []
    ceiling = _ceiling(suffix)
    model = str(getattr(cfg, WAKE_MODEL_ATTR, "") or "")
    label = _label(candidates)
    try:
        reply = client.complete(
            model=model, prefix=WAKES_INSTRUCTIONS, suffix=suffix + "\n\nDecide.",
            schema=WAKES_SCHEMA, schema_name="memcal_wakes", max_tokens=ceiling,
            reasoning_effort=cfg.reasoning_effort or None,
        )
        trace.record(conn, run_id=run_id, stage="wakes", label=label, reply=reply,
                     max_tokens=ceiling, home=cfg.home,
                     prefix=WAKES_INSTRUCTIONS, suffix=suffix + "\n\nDecide.",
                     bundles=_bundle_refs(candidates))
    except Exception as exc:
        tally = getattr(exc, "tally", None)
        calls.save_failure(cfg.home, run_id=run_id, stage="wakes", label=label,
                           error=f"{type(exc).__name__}: {exc}"[:500],
                           model=model, prefix=WAKES_INSTRUCTIONS,
                           suffix=suffix + "\n\nDecide.", max_tokens=ceiling,
                           bundles=_bundle_refs(candidates),
                           requests=getattr(tally, "requests", 0),
                           waited=getattr(tally, "waited", 0.0))
        conn.commit()
        return [], [f"wakes: {type(exc).__name__}: {exc}"[:300]]
    if getattr(reply, "truncated", False):
        conn.commit()
        return [], [f"wakes reply cut off at the {ceiling}-token ceiling — "
                    f"nothing from it was applied"]
    data = getattr(reply, "data", None)
    if not isinstance(data, dict) or not isinstance(data.get("decisions"), list):
        conn.commit()
        return [], ["wakes: malformed reply — expected {decisions: [...]}; "
                    "nothing from it was applied"]
    generation_id = str(getattr(reply, "generation_id", "") or "")
    woken: list[todos.Todo] = []
    problems: list[str] = []
    for item in data["decisions"]:
        parsed = _valid_decision(item)
        if parsed is None:
            problems.append("wakes: ignored a malformed decision; "
                            "that waiter stays asleep")
            continue
        key, satisfied, cites = parsed
        if not satisfied:
            continue
        if not cites:
            problems.append(f"wakes: {key} claims satisfied with no cites; "
                            f"it stays asleep")
            continue
        if key not in citations:
            continue
        archive_ids = _resolve_cites(citations[key], cites)
        if not archive_ids:
            problems.append(f"wakes: {key} cites must name lines from one candidate bundle")
            continue
        todo = todos.get(conn, key)
        if todo is None or todo.status != "open" or not todo.wake_condition:
            continue
        if todo.woke_at:
            continue
        if since and str(todo.opened_at or "") >= str(since):
            continue
        todos.mark_woken(conn, todo)
        trace.stamp(conn, kind="todo", ref=todo.key, verb="woke",
                    entity=f"wakes:{len(archive_ids)} line(s)", stage="wakes",
                    run_id=run_id, generation_id=generation_id or None,
                    archive_ids=archive_ids)
        woken.append(todo)
    conn.commit()
    return woken, problems


def maybe_wake(conn: sqlite3.Connection, cfg: Config, bundles, *,
               since: str | None = None, run_id: int | None = None,
               client: CompletionClient | None = None) -> tuple[list[todos.Todo], list[str]]:
    """Post-apply hook: entailment-gated wakes, or nothing when disabled.

    The client comes through `llm.client_for(cfg)`; tests inject a fake.
    """
    if not getattr(cfg, "semantic_wakes", False):
        return [], []
    if client is None:
        from .. import llm                                          # noqa: PLC0415
        client = llm.client_for(cfg)
    return evaluate(client, conn, cfg, bundles, since=since, run_id=run_id)
