"""Stage 5 — post-write cleanup.

Inspects the resulting state and diff log for duplicates, contradictions, and invalid rows.
Operates on summarized post-run output rather than raw input traffic.
"""

from __future__ import annotations

import sqlite3

from .. import db, events, todos, trace
from ..config import Config
from ..llm import CompletionClient

SWEEP_INSTRUCTIONS = """\
You are reviewing the state of a personal memory system right after a batch of writes.
You are not adding anything new. You are looking for damage:

  duplicate    two rows that are plainly the same event -> drop the worse-worded one
  junk         a row or fact that should never have been stored (banter, affection,
               newsletter events, facts about software, other people's opinions)
  contradiction  two rows that cannot both be true -> keep neither silently; ask

For a contradiction you cannot resolve from what you see, do not guess and do not
delete. Raise a question instead.

KEYS ARE OPAQUE
A row's key is an identifier, not data. It is minted once from the title and the date
the row was first seen, and it never changes afterwards — so a row that moved to a new
date keeps its original suffix, and `beer-garden@2026-08-01` dated 2026-08-02 is a row
that moved, working exactly as intended. The date field is the truth; the key is a
name. Never raise a question about a key disagreeing with a date.

QUESTIONS ARE FOR THE USER
Every question you raise is shown to a person and costs them a reply. Ask only about
their life — never about this system's bookkeeping, formatting, or internal
consistency. If the only person who could act on the answer is the system itself, it
is not a question.

Be conservative. Deleting a real row is worse than leaving a slightly awkward one.
Return empty arrays when the state looks fine, which is the common case.
"""

SWEEP_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["drop_events", "drop_todos", "questions"],
    "properties": {
        "drop_events": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "reason"],
                "properties": {
                    "key": {"type": "string"},
                    "reason": {"type": "string", "enum": ["duplicate", "junk"]},
                },
            },
        },
        "drop_todos": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "reason"],
                "properties": {
                    "key": {"type": "string"},
                    "reason": {"type": "string", "enum": ["duplicate", "junk"]},
                },
            },
        },
        "questions": {"type": "array", "items": {"type": "string"}},
    },
}


def state_snapshot(conn: sqlite3.Connection, cfg: Config, diff_log: list[str]) -> str:
    parts = ["MEMCAL AFTER THIS RUN"]
    for ev in events.window(conn, cfg.days_back, cfg.days_forward):
        parts.append(f"  {ev.key} | {ev.date} | {ev.kind}/{ev.status} | {ev.title}"
                     + (f" | {ev.location}" if ev.location else "")
                     + (f" | with {', '.join(ev.participants)}" if ev.participants else ""))
    parts.append("\nOPEN TO-DOS")
    parts += [f"  {t.key} | {t.text}" for t in todos.open_items(conn)] or ["  (none)"]
    # Include currently open questions to prevent duplicate questions across runs.
    parts.append("\nQUESTIONS ALREADY OPEN — do not ask any of these again")
    parts += [f"  {r['text']}" for r in todos.open_questions(conn, limit=25)] \
        or ["  (none)"]
    parts.append("\nWRITES IN THIS RUN")
    parts += [f"  {line}" for line in diff_log[:80]] or ["  (none)"]
    return "\n".join(parts)


def sweep_ceiling(snapshot: str, model: str = "") -> int:
    """Calculate maximum output tokens for sweep completion.

    Scales token allowance proportionally to snapshot size and accounts for model-specific
    reasoning token budgets (`think_tokens` and `ceiling_boost`).
    """
    from .. import llm
    spec = llm.endpoint(model)
    base = min(8000, 1500 + len(snapshot) // 8)
    # Ensure sufficient token headroom for reasoning over the full state snapshot.
    return min(32000, max(int(base * spec.ceiling_boost), spec.think_tokens))


def sweep(client: CompletionClient, conn: sqlite3.Connection, cfg: Config,
          diff_log: list[str], *, run_id: int | None = None) -> tuple[dict, list[str]]:
    """Execute the sweep stage, returning (result_dict, actions_taken)."""
    snapshot = state_snapshot(conn, cfg, diff_log)
    ceiling = sweep_ceiling(snapshot, cfg.sweep_model)
    reply = client.complete(
        model=cfg.sweep_model,
        prefix=SWEEP_INSTRUCTIONS,
        suffix=snapshot + "\n\nReview this state.",
        schema=SWEEP_SCHEMA,
        schema_name="memcal_sweep",
        max_tokens=ceiling,
        reasoning_effort=cfg.reasoning_effort or None,
    )
    trace.record(conn, run_id=run_id, stage="sweep", label="state review", reply=reply,
                 max_tokens=ceiling, home=cfg.home, prefix=SWEEP_INSTRUCTIONS,
                 suffix=snapshot + "\n\nReview this state.")
    result = reply.data if isinstance(reply.data, dict) else {}
    actions: list[str] = []
    if reply.truncated:
        # Record truncation warning when the response reaches the token ceiling.
        actions.append(f"sweep reply cut off at the {ceiling}-token ceiling — "
                       f"state was not fully reviewed, duplicates may remain")

    for row in result.get("drop_events") or []:
        key = (row or {}).get("key")
        if key and events.delete(conn, key):
            actions.append(f"dropped event {key} ({row.get('reason')})")
    for row in result.get("drop_todos") or []:
        key = (row or {}).get("key")
        if key and todos.close(conn, key, status="dropped"):
            actions.append(f"dropped todo {key} ({row.get('reason')})")
    for question in result.get("questions") or []:
        if isinstance(question, str) and question.strip():
            key = todos.ask(conn, question.strip(), written_by="sweep")
            # Record trace provenance stamp for sweep-generated questions.
            trace.stamp(conn, kind="question", ref=key, verb="asked",
                        entity="sweep:state-review", stage="sweep", run_id=run_id)
            actions.append(f"asked: {question.strip()}")
    conn.commit()
    return result, actions


def reconcile_backward_window(conn: sqlite3.Connection, cfg: Config) -> list[str]:
    """Reconcile past unconfirmed events deterministically in code.

    Generates clarifying questions for past events retaining 'mentioned' status.
    """
    raised: list[str] = []
    lo, _ = db.window_bounds(cfg.days_back, 0)
    today = db.today().isoformat()
    for ev in events.between(conn, lo, today):
        if ev.date >= today or ev.kind in ("availability", "opportunity", "observed"):
            continue
        if ev.status != "mentioned":
            continue
        when = db.parse_date(ev.date).strftime("%A")
        text = f"Did {ev.title} happen on {when}?"
        if not ev.participants:
            text += " who with?"
        key = todos.ask(conn, text, key=f"q:resolve:{ev.key}", about_event=ev.id,
                        written_by="reconcile")
        # Stamp provenance linking the reconciliation question to the target event.
        trace.stamp(conn, kind="question", ref=key, verb="asked",
                    entity=f"event:{ev.key}", stage="code")
        raised.append(text)
    conn.commit()
    return raised
