"""Public observations: exactly what every memory system receives, and nothing more.

An `Observation` holds only public fields. There is no hidden data to strip before an
adapter sees one, because the object cannot carry an expectation in the first place —
ground truth lives in `rubric.Checkpoint`, a separate object that never leaves the
grader. That separation is structural, not a convention, so "the answer key leaked into
the corpus" is a class of bug the format makes unrepresentable.

Four clocks are kept distinct on purpose:

- ``written_at``   — when the sender authored the content.
- ``arrived_at``   — when it landed in the source (a late confirmation arrives after it
  was written).
- ``collected_at`` — when the harness is scheduled to expose it to the systems. An
  expectation follows this clock: before collection, no system can be expected to know
  the update; a failed collection after its scheduled moment is a real failure, not an
  excuse to weaken the key.

Channel and thread are provenance, never a tenant boundary — every channel resolves to
the same person's memory. `sender` keeps the source-provided identity; an email from
someone else is never quietly re-attributed to the owner.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

CHANNELS = ("email", "imessage", "whatsapp", "groupme", "agent")


@dataclass(frozen=True)
class Observation:
    """One immutable record delivered at a collection boundary.

    Frozen so a variant (reorder, batch) that changes *arrival* can never reach back and
    mutate the shared source record — the bug the collision harness still carries
    (`tests/scenarios/collision.py`, sparse variant) and one this format refuses to
    inherit.
    """

    source_id: str                 # stable and unique within a life
    channel: str                   # one of CHANNELS
    written_at: str                # ISO-8601; when the content was authored
    arrived_at: str                # ISO-8601; when the source received it
    collected_at: str              # ISO-8601; when the harness exposes it
    sender: str                    # source-provided identity, not normalized to the owner
    thread: str                    # provenance
    text: str
    from_owner: bool = False
    #: Structured payload only when it genuinely exists in the source (e.g. an iCal row),
    #: delivered identically to every adapter. Never a hidden entity id or inferred link.
    structured: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.channel not in CHANNELS:
            raise ValueError(f"unknown channel {self.channel!r}; expected one of {CHANNELS}")

    def public(self) -> dict:
        """The full record an adapter may see. Every field here is public by construction."""
        return asdict(self)


def delivery_order(observations: list[Observation]) -> list[Observation]:
    """Observations in the order the systems learn them: by collection time, with a
    stable tiebreak on source id so a batch delivered at one instant is reproducible."""
    return sorted(observations, key=lambda o: (o.collected_at, o.source_id))


def chronology_ok(observations: list[Observation]) -> list[str]:
    """Return the ids whose clocks are inconsistent — authored after they arrived, or
    collected before they arrived. A corpus check, not a grader call."""
    bad: list[str] = []
    for o in observations:
        if o.arrived_at < o.written_at or o.collected_at < o.arrived_at:
            bad.append(o.source_id)
    return bad
