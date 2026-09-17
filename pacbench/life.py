"""A `Life`: one person's week (or a slice of it) plus the questions asked of it.

`bet` names the architectural claim the family exercises; `role` is its corpus split
(`development` for debugging, `calibration` for reviewing the grader, `held-out` for the
comparative measurement). A `control_of` life removes exactly the one element the bet
depends on, so a win on the bet family paired with parity on its control is a capability
claim rather than a corpus authored to favour one product.
"""

from __future__ import annotations

from dataclasses import dataclass

from .observation import Observation
from .rubric import Checkpoint


@dataclass(frozen=True)
class Life:
    id: str
    bet: str
    role: str
    observations: tuple[Observation, ...]
    checkpoints: tuple[Checkpoint, ...]
    control_of: str | None = None
