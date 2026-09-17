"""The PACBench corpus: one module per incident family.

Each module exposes ``lives()`` returning the family's `Life` objects — the bet life and
its matched control. Ground truth lives in the rubric; message text here is hand-authored
for the first families and, at scale, rendered by a model from this same structured
spec (never the other way round — the model that writes surface text never authors the
key).
"""

from __future__ import annotations

from ..life import Life
from . import poker_move, weekend_plans


def all_lives() -> list[Life]:
    return [*poker_move.lives(), *weekend_plans.lives()]
