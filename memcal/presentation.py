"""Small shared vocabulary for state and change labels across user surfaces."""

from __future__ import annotations

import json


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


#: A name dream invented for an otherwise-nameless sender is shown as a guess until a
#: real source confirms it. Marked, never hidden — the reader sees both the guess and
#: that it is one.
GUESS_PREFIX = "maybe: "  # Legacy display text; never written onto an identity.
INFERRED_NAME = " (inferred from messages)"
CALENDAR_INFERRED_NAME = " (name inferred)"


def raw_name(name: str) -> str:
    """Recover an identity from display text, including older saved annotations."""
    text = (name or "").strip()
    if text.startswith(GUESS_PREFIX):
        return text[len(GUESS_PREFIX):]
    if text.endswith(INFERRED_NAME):
        value = text[:-len(INFERRED_NAME)]
        try:
            decoded = json.loads(value)
        except ValueError:
            return value
        return decoded if isinstance(decoded, str) else value
    if text.endswith(CALENDAR_INFERRED_NAME):
        return text[:-len(CALENDAR_INFERRED_NAME)]
    return text


def as_guess(name: str, *, audience: str = "agent") -> str:
    """Annotate the inferred name, independently of the plan's acceptance status."""
    text = raw_name(name)
    if not text:
        return ""
    if audience == "calendar":
        return text + CALENDAR_INFERRED_NAME
    return json.dumps(text, ensure_ascii=False) + INFERRED_NAME


#: Agent turns are user-authored context, not independent evidence.
SELF_WRITTEN_STREAM = "agent"
SELF_WRITTEN_NOTE = ("written by you earlier this session — not independent evidence, "
                     "do not cite it as confirmation")


def self_written(channel: str | None) -> bool:
    """Is this line the user's own earlier turn rather than something somebody sent?"""
    return str(channel or "") == SELF_WRITTEN_STREAM


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
