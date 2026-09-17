"""Bet: recency is decided at write time.

A year-old email carries a plausible poker address (Sam's, 12 Ash St, Fridays at 7).
This week the group re-plans there, then the organizer moves it to Saturday 8pm at
Jordan's, and a participant later repeats the old plan. Asked "where is poker?", the
current answer is Jordan's / Saturday / 8, while the original address stays recoverable
as history.

A retrieve-at-read stack is expected to let the year-old email or the stale repeat win,
because relevance drowns out recency. The matched control (`poker-unchanged`) removes the
move: poker stays Friday at Sam's, both systems should answer it, and the write-time
advantage does not apply.
"""

from __future__ import annotations

from ..life import Life
from ..observation import Observation
from ..rubric import Checkpoint, Kind, Norm, Slot

_ID = "poker-move"
_CTL = "poker-unchanged"


def _t(day: int, clock: str) -> str:
    # Monday 2026-09-07 is day 0; the whole family lives in one fictional week.
    from datetime import date, timedelta
    d = date(2026, 9, 7) + timedelta(days=day)
    return f"{d.isoformat()}T{clock}:00"


def _year_ago(clock: str) -> str:
    return f"2025-09-06T{clock}:00"


# ------------------------------------------------------------------- the move --

def _bet() -> Life:
    obs = (
        Observation(
            source_id=f"{_ID}/email-oldaddr", channel="email",
            written_at=_year_ago("19:00"), arrived_at=_year_ago("19:00"),
            collected_at=_t(0, "08:00"), sender="Sam Delgado", thread="poker",
            text=("Good season everyone. Same as always — poker's at my place, "
                  "12 Ash Street, Fridays at 7."),
        ),
        Observation(
            source_id=f"{_ID}/gm-replan", channel="groupme",
            written_at=_t(1, "10:00"), arrived_at=_t(1, "10:00"),
            collected_at=_t(1, "10:00"), sender="Sam Delgado", thread="poker-crew",
            text="Poker this week? Friday 7 at mine like usual.",
        ),
        Observation(
            source_id=f"{_ID}/gm-move", channel="groupme",
            written_at=_t(2, "14:00"), arrived_at=_t(2, "14:00"),
            collected_at=_t(2, "14:00"), sender="Jordan Vance", thread="poker-crew",
            text=("Change of plan — moving poker to Saturday 8pm at my place, "
                  "44 Birch Ave. Can't do Friday."),
        ),
        Observation(
            source_id=f"{_ID}/im-confirm", channel="imessage",
            written_at=_t(2, "15:30"), arrived_at=_t(2, "15:30"),
            collected_at=_t(2, "15:30"), sender="Sam Delgado", thread="Sam Delgado",
            text="works for me, Jordan's Saturday at 8. see you there",
        ),
        # A stale repeat that must NOT override the move — the newest message does not win
        # by being newest; the meaning of the evidence does.
        Observation(
            source_id=f"{_ID}/wa-stale", channel="whatsapp",
            written_at=_t(3, "09:00"), arrived_at=_t(3, "09:00"),
            collected_at=_t(3, "09:00"), sender="Priya Nair", thread="poker-crew",
            text="poker friday at Sam's right? bringing snacks",
        ),
    )

    where = Checkpoint(
        id=f"{_ID}/where", at=_t(3, "12:00"),
        question="Where is poker, and when?",
        mode="complete-plan",
        required_fields=("loc", "day", "time"),
        slots=(
            Slot(id="loc", kind=Kind.CURRENT, norm=Norm.ENTITY, expected="Jordan's",
                 alternatives=("44 Birch Ave", "44 Birch"),
                 forbidden=("Sam's", "12 Ash", "12 Ash Street"),
                 supported_by=(f"{_ID}/gm-move", f"{_ID}/im-confirm")),
            Slot(id="day", kind=Kind.CURRENT, norm=Norm.DATE, expected="Saturday",
                 forbidden=("Friday",),
                 supported_by=(f"{_ID}/gm-move", f"{_ID}/im-confirm")),
            Slot(id="time", kind=Kind.CURRENT, norm=Norm.TIME, expected="8pm",
                 forbidden=("7pm",),
                 supported_by=(f"{_ID}/gm-move", f"{_ID}/im-confirm")),
        ),
    )
    historical = Checkpoint(
        id=f"{_ID}/old-address", at=_t(3, "12:05"),
        question="What was the original poker address before it moved?",
        slots=(
            Slot(id="old-loc", kind=Kind.HISTORICAL, norm=Norm.ENTITY,
                 expected="12 Ash", alternatives=("Sam's", "12 Ash Street"),
                 supported_by=(f"{_ID}/email-oldaddr",)),
        ),
    )
    return Life(id=_ID, bet="write-time-recency", role="development",
                observations=obs, checkpoints=(where, historical))


# --------------------------------------------------- the control: no move at all --

def _control() -> Life:
    obs = (
        Observation(
            source_id=f"{_CTL}/email-oldaddr", channel="email",
            written_at=_year_ago("19:00"), arrived_at=_year_ago("19:00"),
            collected_at=_t(0, "08:00"), sender="Sam Delgado", thread="poker",
            text=("Good season everyone. Same as always — poker's at my place, "
                  "12 Ash Street, Fridays at 7."),
        ),
        Observation(
            source_id=f"{_CTL}/gm-replan", channel="groupme",
            written_at=_t(1, "10:00"), arrived_at=_t(1, "10:00"),
            collected_at=_t(1, "10:00"), sender="Sam Delgado", thread="poker-crew",
            text="Poker this week? Friday 7 at mine like usual.",
        ),
        Observation(
            source_id=f"{_CTL}/gm-confirm", channel="groupme",
            written_at=_t(2, "14:00"), arrived_at=_t(2, "14:00"),
            collected_at=_t(2, "14:00"), sender="Jordan Vance", thread="poker-crew",
            text="in. Friday 7 at Sam's, see everyone there.",
        ),
    )
    where = Checkpoint(
        id=f"{_CTL}/where", at=_t(3, "12:00"),
        question="Where is poker, and when?",
        mode="complete-plan",
        required_fields=("loc", "day", "time"),
        slots=(
            Slot(id="loc", kind=Kind.CURRENT, norm=Norm.ENTITY, expected="Sam's",
                 alternatives=("12 Ash", "12 Ash Street"),
                 forbidden=("Jordan's", "44 Birch"),
                 supported_by=(f"{_CTL}/gm-replan", f"{_CTL}/gm-confirm")),
            Slot(id="day", kind=Kind.CURRENT, norm=Norm.DATE, expected="Friday",
                 forbidden=("Saturday",),
                 supported_by=(f"{_CTL}/gm-confirm",)),
            Slot(id="time", kind=Kind.CURRENT, norm=Norm.TIME, expected="7pm",
                 forbidden=("8pm",),
                 supported_by=(f"{_CTL}/gm-confirm",)),
        ),
    )
    return Life(id=_CTL, bet="write-time-recency", role="development",
                observations=obs, checkpoints=(where,), control_of=_ID)


def lives() -> list[Life]:
    return [_bet(), _control()]
