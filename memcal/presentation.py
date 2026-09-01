"""Small shared vocabulary for state and change labels across user surfaces."""

from __future__ import annotations


CHANGE_LABELS = {"new": "New", "edited": "Updated"}

STAGE_LABELS = {
    "propose": "Read",
    "merge": "Merge",
    "resolve": "Merge",          # old recorded runs
    "apply": "Apply",
    "sweep": "Cleanup",
    "live": "Agent",
    "ical": "Calendar",
    "code": "Automatic",
}


def change_label(change: str | None) -> str:
    return CHANGE_LABELS.get(str(change or "").lower(), "")


def stage_label(stage: str | None) -> str:
    value = str(stage or "").strip()
    base, separator, detail = value.partition(":")
    label = STAGE_LABELS.get(base, base.replace("_", " ").title() or "Automatic")
    return f"{label} · {detail.replace('_', ' ').title()}" if separator else label


def question_state(row) -> str:
    status = str(row["status"] or "")
    if status == "answered":
        return "Answered"
    if status == "dropped":
        return "Closed"
    waiting = str(row["wake_condition"] or "").strip()
    return f"Waiting — {waiting}" if waiting else "Waiting for a reply"


def question_line(row) -> str:
    text = str(row["text"] or "").strip()
    waiting = str(row["wake_condition"] or "").strip()
    return f"{text} — waiting: {waiting}" if waiting else text
