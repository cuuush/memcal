"""Grade an assistant's free-text answer against a checkpoint's rubric.

The design pushes almost all of grading out of a model's hands. Most slots — dates,
times, statuses, yes/no, named entities — are matched deterministically here, with no
model call at all. Only a TEXT slot whose value could not be matched deterministically
falls through to the semantic judge, and the judge answers exactly one blind binary
question: *does this answer assert this specific claim?* It never sees the corpus, the
score, or the other slots.

Stage 1 ships the deterministic layer and a **judge seam**: pass ``judge=`` a callable
``(answer, claim) -> bool`` to resolve the residual. With no judge, a TEXT slot is
graded on deterministic containment alone and any genuinely paraphrased claim it misses
is reported as MISSED rather than silently credited — a conservative default that never
invents a pass. Wiring the seam to a calibrated model is Stage 1's grading sub-task and
the point where human-agreement gating attaches.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Callable

from .rubric import Checkpoint, Kind, Norm, Slot, Verdict

Judge = Callable[[str, str], bool]

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_ABBREV = {d[:3]: d for d in _WEEKDAYS}
_ABSTAIN_CUES = (
    "not sure", "cannot tell", "can't tell", "don't know", "do not know", "unclear",
    "couldn't find", "could not find", "no evidence", "ambiguous", "not enough",
    "do you mean", "which ", "can't say", "cannot say", "unsure", "hasn't confirmed",
    "haven't confirmed", "not confirmed", "want me to ask", "should i ask", "to be confirmed",
    "not settled", "let me know which",
)
_AFFIRM = ("yes", "yep", "yeah", "still on", "still going", "confirmed", "you are", "you're")
_NEGATE = ("no", "not going", "cancelled", "canceled", "called off", "you are not", "won't")
#: Clauses about the past. A superseded value inside one is history, not a current claim,
#: so retaining "it used to be Friday" must not be scored as asserting Friday now — the
#: history-keeping this benchmark rewards would otherwise read as an error.
_HISTORY_CUES = ("used to", "use to", "was ", "were ", "before", "originally", "previously",
                 "no longer", "moved from", "changed from", "formerly", "had been", "old ")


def _norm_text(s: str) -> str:
    return re.sub(r"[^\w\s:]", " ", s.lower())


def _time_forms(value: str) -> set[str]:
    """Surface forms of a clock value: '8pm' -> {8pm, 8 pm, 8:00 pm, 20:00, eight...}."""
    v = value.strip().lower()
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", v)
    forms = {v}
    if not m:
        return forms
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = m.group(3)
    if ampm is None and hour <= 12:
        ampm = "am" if hour < 8 else "pm"      # a bare social hour is evening by default
    h24 = hour % 12 + (12 if ampm == "pm" else 0)
    for hh in {hour, hour % 12 or 12}:
        forms |= {f"{hh}{ampm}", f"{hh} {ampm}", f"{hh}:{minute:02d}{ampm}",
                  f"{hh}:{minute:02d} {ampm}"}
    forms.add(f"{h24:02d}:{minute:02d}")
    return {f for f in forms if f}


def _date_forms(value: str) -> set[str]:
    """Surface forms of a date value: a weekday name, or an ISO date rendered a few ways."""
    v = value.strip().lower()
    if v in _WEEKDAYS:
        return {v, v[:3]}
    if v in _ABBREV:
        return {v, _ABBREV[v]}
    try:
        d = date.fromisoformat(value)
    except ValueError:
        return {v}
    wd = _WEEKDAYS[d.weekday()]
    return {value, wd, wd[:3], d.strftime("%b %d").lower(), d.strftime("%B %d").lower(),
            d.strftime("%-m/%-d") if hasattr(d, "strftime") else value}


def _entity_forms(value: str) -> set[str]:
    v = _norm_text(value).strip()
    forms = {v}
    if v.endswith("s"):
        forms.add(v[:-1])                       # jordan's -> jordan
    forms.add(v.replace("'", ""))
    return {f.strip() for f in forms if f.strip()}


def _asserts(answer: str, value: str, norm: Norm, judge: Judge | None) -> bool:
    """Does `answer` assert `value` under this norm's matching rule?"""
    hay = _norm_text(answer)
    if norm is Norm.TIME:
        return any(f in hay for f in _time_forms(value))
    if norm is Norm.DATE:
        return any(re.search(rf"\b{re.escape(f)}\b", hay) for f in _date_forms(value))
    if norm is Norm.YESNO:
        cues = _AFFIRM if value.strip().lower() in ("yes", "true", "1") else _NEGATE
        return any(re.search(rf"\b{re.escape(c)}\b", hay) for c in cues)
    if norm is Norm.ENTITY:
        return any(f in hay for f in _entity_forms(value))
    if norm is Norm.STATUS:
        return _norm_text(value).strip() in hay
    # TEXT: deterministic containment first, judge only on the residual.
    if _norm_text(value).strip() in hay:
        return True
    return bool(judge and judge(answer, value))


def _slot_asserts_expected(answer: str, slot: Slot, judge: Judge | None) -> bool:
    values = (slot.expected, *slot.alternatives)
    return any(_asserts(answer, v, slot.norm, judge) for v in values)


def _abstains(answer: str) -> bool:
    hay = answer.lower()
    return any(cue in hay for cue in _ABSTAIN_CUES)


def _current_text(answer: str) -> str:
    """The answer with clauses that are clearly about the past dropped, so a forbidden
    value survives here only when it is asserted as the *current* state."""
    parts = re.split(r"[.;\n]|,? but | though | although | however |,? not ",
                     answer, flags=re.IGNORECASE)
    return " ".join(p for p in parts
                    if not any(cue in p.lower() for cue in _HISTORY_CUES))


@dataclass
class GradedSlot:
    id: str
    verdict: str
    reason: str


@dataclass
class Grade:
    checkpoint: str
    slots: list[GradedSlot] = field(default_factory=list)
    correct: int = 0
    missed: int = 0
    misrepresented: int = 0
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    complete_plan: bool | None = None
    abstention: float | None = None
    #: open-ended only: supported options surfaced, and forbidden options wrongly suggested.
    options_found: int | None = None
    options_total: int | None = None
    suggested_forbidden: int = 0

    def to_dict(self) -> dict:
        return {**asdict(self)}


def _verdict_for(answer: str, slot: Slot, judge: Judge | None) -> tuple[str, str]:
    current = _current_text(answer)
    forbidden_hit = next(
        (f for f in slot.forbidden if _asserts(current, f, slot.norm, judge)), None)
    expected = _slot_asserts_expected(answer, slot, judge)
    if slot.kind is Kind.ABSTAIN:
        # Abstaining while naming the candidates ("Saturday or Sunday — not confirmed") is
        # the right answer, so an abstention outranks the mention of a forbidden value.
        # Only a definite pick with no hedge is a confabulation.
        if _abstains(answer):
            return Verdict.CORRECT, "abstained on insufficient evidence"
        if forbidden_hit:
            return Verdict.MISREPRESENTED, f"confabulated {forbidden_hit!r}"
        return Verdict.MISSED, "neither abstained nor answered"
    # A superseded value asserted as current loses the slot even if the right one also
    # appears — asserting both the new and old time is not a correct answer.
    if forbidden_hit:
        return Verdict.MISREPRESENTED, f"asserted stale/forbidden {forbidden_hit!r}"
    if expected:
        return Verdict.CORRECT, f"asserted {slot.expected!r}"
    return Verdict.MISSED, f"no assertion of {slot.expected!r}"


def grade(checkpoint: Checkpoint, answer: str, *, judge: Judge | None = None) -> Grade:
    """Grade one answer. Deterministic given the same inputs and judge."""
    scored = checkpoint.slots or checkpoint.supported
    result = Grade(checkpoint=checkpoint.id)
    answerable = [s for s in scored if s.required]
    for slot in scored:
        verdict, reason = _verdict_for(answer, slot, judge)
        result.slots.append(GradedSlot(slot.id, verdict, reason))
        if not slot.required:
            continue
        if verdict == Verdict.CORRECT:
            result.correct += 1
        elif verdict == Verdict.MISSED:
            result.missed += 1
        else:
            result.misrepresented += 1

    if checkpoint.mode == "open-ended":
        # Recall over the supported options the evidence contains; precision against the
        # forbidden ones (expired, declined, wrong person). A forbidden suggestion is a
        # false positive only — it is not a missed supported option, so it is not double
        # counted as a false negative the way a wrong replacement of a required slot is.
        current = _current_text(answer)
        for opt in checkpoint.forbidden_options:
            values = (opt.expected, *opt.alternatives)
            if any(_asserts(current, v, opt.norm, judge) for v in values):
                result.slots.append(
                    GradedSlot(opt.id, Verdict.MISREPRESENTED, "suggested a forbidden option"))
                result.suggested_forbidden += 1
        found, total = result.correct, len(answerable)
        result.options_found, result.options_total = found, total
        fp = result.suggested_forbidden
        result.recall = found / total if total else 1.0
        result.precision = found / (found + fp) if found + fp else 1.0
    else:
        tp, fn, fp = result.correct, result.missed + result.misrepresented, result.misrepresented
        result.precision = tp / (tp + fp) if tp + fp else (1.0 if not answerable else 0.0)
        result.recall = tp / (tp + fn) if tp + fn else (1.0 if not answerable else 0.0)
    result.f1 = (2 * result.precision * result.recall / (result.precision + result.recall)
                 if result.precision + result.recall else 0.0)

    if checkpoint.mode == "complete-plan":
        by_id = {gs.id: gs.verdict for gs in result.slots}
        result.complete_plan = all(
            by_id.get(fid) == Verdict.CORRECT for fid in checkpoint.required_fields)
    abstain = [gs for gs, s in zip(result.slots, scored) if s.kind is Kind.ABSTAIN]
    if abstain:
        result.abstention = sum(gs.verdict == Verdict.CORRECT for gs in abstain) / len(abstain)
    return result
