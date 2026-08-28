#!/usr/bin/env python3
"""Print due reminders for Hermes; --mark records a poke and snoozes them."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import config, db, todos                                # noqa: E402


def _how_far(target: str, today) -> str:
    """'today', 'tomorrow', 'in 3 days', '2 days ago' — relative to now.

    `db.days_between(a, b)` is `a - b`, so passing (today, target) counts *backwards*:
    a positive number means the target has already gone by.
    """
    behind = db.days_between(today.isoformat(), target)
    if behind == 0:
        return "today"
    if behind == 1:
        return "yesterday"
    if behind == -1:
        return "tomorrow"
    return f"{behind} days ago" if behind > 0 else f"in {-behind} days"


def line_for(todo, today) -> str:
    """One reminder, with the facts needed to judge it and nothing else.

    Notably *how late it is* and *what it is attached to*: "three days overdue" and "the
    thing is tomorrow" deserve different messages, and only one of them deserves a
    message at all on a busy afternoon.
    """
    bits = [f'"{todo.text}"']
    if todo.event_title:
        when = ""
        if todo.event_date:
            when = (f", which is {db.parse_date(todo.event_date).strftime('%a %-d %b')}"
                    f" — {_how_far(todo.event_date, today)}")
        bits.append(f"for {todo.event_title}{when}")
    if todo.due:
        bits.append(f"due {_how_far(todo.due, today)} ({todo.due})")
    if todo.reminded_at:
        bits.append(f"you were last poked about this at {todo.reminded_at[:16]}"
                    " and chose to say nothing")
    return "· " + "; ".join(bits)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mark", action="store_true",
                        help="record the poke, so it snoozes rather than repeating")
    parser.add_argument("--home", help="a memcal home other than the default")
    args = parser.parse_args()

    cfg = config.load(args.home)
    conn = db.connect(cfg.db_path)
    today = db.today()
    due = todos.due_reminders(conn)
    if not due:
        # Hermes' wake gate: a last stdout line of `{"wakeAgent": false}` skips the
        # agent run entirely — no model call, no delivery, nothing to suppress
        # (`cron/scheduler.py:_parse_wake_gate`). On a quiet day that is the whole job.
        #
        # This is the same shape as memcal's own first invariant, one layer out: the
        # cheap deterministic thing decides what the model is even shown, and a model
        # is never asked a question that code can answer. Leaving stdout empty also
        # produced no message, but by waking a model to tell it there was nothing to
        # say — paying for a judgement whose answer was already known here.
        print(json.dumps({"wakeAgent": False}))
        return 0

    print(f"memcal has {len(due)} reminder(s) that have come due, as of "
          f"{db.now()[:16]}:")
    for todo in due:
        print(line_for(todo, today))
        if args.mark:
            todos.mark_reminded(conn, todo.key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
