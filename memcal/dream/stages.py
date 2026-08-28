"""Split propose fields into ordered passes within one model conversation."""

from __future__ import annotations

from dataclasses import dataclass

#: All array names in a bundle diff, used to verify stage coverage.
ALL_FIELDS = ("events", "todos", "wiki", "standing", "questions")


@dataclass(frozen=True)
class Stage:
    """A single pass over staged bundles targeting a subset of diff fields.

    `ask` is appended as a user turn after traffic in the prompt context.
    """

    name: str
    fields: tuple[str, ...]
    ask: str


CALENDAR = Stage("calendar", ("events",), """\
NOW, PASS {n} OF {total} — THE CALENDAR ONLY.
List every bundle id above in `reviewed`, whether or not it earned a diff. Then return
`events` for the bundles where something is happening on a day, exactly by the calendar
rules you were given. To-dos, pages and questions are asked for in later passes: leave
them out, and do not skip a row because it is not the whole story.""")


TODOS = Stage("todos", ("todos",), """\
NOW, PASS {n} OF {total} — TO-DOS ONLY.
Same bundles. Did the user say the user would do something? Apply the to-do rules you were given.
`event_key` may name a row you wrote in the pass above as well as one already on the
calendar — use the exact key, never an invented one. Return `todos` only; the rows above
stand as written.""")


PAGES = Stage("pages", ("wiki", "standing"), """\
NOW, PASS {n} OF {total} — PAGES AND STANDING ONLY.
Same bundles. What durable, entity-level facts does this traffic establish? Apply the
page, slot, alias and standing rules you were given. Return `wiki` and `standing` only.""")


QUESTIONS = Stage("questions", ("questions",), """\
NOW, PASS {n} OF {total} — QUESTIONS ONLY.
Same bundles, with everything you wrote above now in front of you. Apply the rules you
were given for when to ask. A row above is not an answer: the row records that something
is happening, the question records what is still unresolved about it. What the rows
settle is the wording — you can now name a plan the way the calendar names it. Return
`questions` only.""")


#: Lookup table keyed by stage name for `propose_stages`.
STAGES: dict[str, Stage] = {s.name: s for s in (CALENDAR, TODOS, PAGES, QUESTIONS)}

#: Default stage sequence for `propose_stages=on`. Order is sequential by dependency.
DEFAULT_ORDER: tuple[str, ...] = ("calendar", "todos", "pages", "questions")


class UnknownStage(ValueError):
    """Raised when an unrecognized stage name is provided in `propose_stages`."""


def parse(spec: str | None) -> list[Stage]:
    """Parse `propose_stages` into an ordered list of Stage objects.

    Returns an empty list for single-pass mode. Unrecognized stage names raise
    UnknownStage. Partial stage configurations are allowed; see `uncovered()`.
    """
    text = (spec or "").strip()
    if not text or text.lower() in ("0", "no", "false", "off"):
        return []
    names = ["on" if n == "on" else n for n in
             (part.strip().lower() for part in text.split(",")) if n]
    if names == ["on"] or names == ["all"]:
        names = list(DEFAULT_ORDER)
    unknown = [n for n in names if n not in STAGES]
    if unknown:
        raise UnknownStage(
            f"unknown propose stage(s): {', '.join(unknown)}. "
            f"Known: {', '.join(DEFAULT_ORDER)} (or 'on' for all four, in that order).")
    # Deduplicate stage names while preserving order.
    seen: set[str] = set()
    return [STAGES[n] for n in names if not (n in seen or seen.add(n))]


def uncovered(plan: list[Stage]) -> list[str]:
    """Return diff field names not covered by any stage in the plan."""
    covered = {field for stage in plan for field in stage.fields}
    return [field for field in ALL_FIELDS if field not in covered]


def ask_for(plan: list[Stage], index: int) -> str:
    """Format the prompt turn for the stage at index in plan."""
    return plan[index].ask.format(n=index + 1, total=len(plan))
