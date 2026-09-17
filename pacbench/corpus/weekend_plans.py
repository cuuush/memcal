"""Bet: implicit questions answer from prepared context, not a query.

"Anything fun this weekend?" has no search string that finds every invitation, offer, and
free friend at once — the answer has to already be sitting in context. Across the week the
evidence holds three genuine weekend opportunities (a venue member day, dinner with a free
friend, family visiting) plus two traps a query might dredge up: an offer that has already
expired and a plan the owner already declined. A good answer surfaces the live options and
suggests neither trap.

A retrieve-at-read stack has nothing to query on and either misses opportunities or serves
an expired/declined one. The matched control (`dentist-friday`) is a pointed question with
an obvious search term, where retrieval is entirely adequate and the advantage disappears.

Also carries an abstention probe: dinner with Dana is genuinely un-dayed, so the correct
move is to ask, not to guess a day.
"""

from __future__ import annotations

from datetime import date, timedelta

from ..life import Life
from ..observation import Observation
from ..rubric import Checkpoint, Kind, Norm, Slot

_ID = "weekend-plans"
_CTL = "dentist-friday"


def _t(day: int, clock: str) -> str:
    d = date(2026, 9, 7) + timedelta(days=day)          # Monday is day 0
    return f"{d.isoformat()}T{clock}:00"


def _bet() -> Life:
    obs = (
        Observation(
            source_id=f"{_ID}/email-memberday", channel="email",
            written_at=_t(2, "08:00"), arrived_at=_t(2, "08:00"),
            collected_at=_t(2, "08:00"), sender="The Foundry", thread="foundry-news",
            text="Members' Day this Saturday at the Foundry — free entry and a talk at noon.",
        ),
        Observation(
            source_id=f"{_ID}/gm-dinner", channel="groupme",
            written_at=_t(3, "12:30"), arrived_at=_t(3, "12:30"),
            collected_at=_t(3, "12:30"), sender="Dana Whitfield", thread="friends",
            text="free for dinner this weekend! Saturday or Sunday, I'll confirm which.",
        ),
        Observation(
            source_id=f"{_ID}/im-family", channel="imessage",
            written_at=_t(3, "18:00"), arrived_at=_t(3, "18:00"),
            collected_at=_t(3, "18:00"), sender="Mom", thread="Mom",
            text="Family's coming into town Sunday, dinner at ours if you're around.",
        ),
        # Trap 1: an offer that has already expired by the weekend.
        Observation(
            source_id=f"{_ID}/wa-gala", channel="whatsapp",
            written_at=_t(1, "09:00"), arrived_at=_t(1, "09:00"),
            collected_at=_t(1, "09:00"), sender="City Museum", thread="museum",
            text="Museum gala tickets — flash sale ends Wednesday, don't miss it!",
        ),
        # Trap 2: something the owner already declined.
        Observation(
            source_id=f"{_ID}/im-5k", channel="imessage",
            written_at=_t(2, "20:00"), arrived_at=_t(2, "20:00"),
            collected_at=_t(2, "20:00"), sender="me", thread="Reese Cole", from_owner=True,
            text="can't make the charity 5k Sunday, told Reese no.",
        ),
        # Distractor: last month's fun, not this weekend.
        Observation(
            source_id=f"{_ID}/gm-oldpoker", channel="groupme",
            written_at=_t(0, "10:00"), arrived_at=_t(0, "10:00"),
            collected_at=_t(0, "10:00"), sender="Sam Delgado", thread="poker-crew",
            text="poker last month was a blast, we should run it back sometime",
        ),
    )

    fun = Checkpoint(
        id=f"{_ID}/anything-fun", at=_t(4, "19:00"),
        question="Anything fun I can do this weekend?",
        mode="open-ended",
        supported=(
            Slot(id="opt-foundry", kind=Kind.CURRENT, norm=Norm.TEXT, expected="Foundry",
                 alternatives=("members' day", "members day", "member day"),
                 supported_by=(f"{_ID}/email-memberday",)),
            Slot(id="opt-dinner", kind=Kind.CURRENT, norm=Norm.TEXT, expected="dinner",
                 alternatives=("Dana",), supported_by=(f"{_ID}/gm-dinner",)),
            Slot(id="opt-family", kind=Kind.CURRENT, norm=Norm.TEXT, expected="family",
                 alternatives=("Mom", "your mother"), supported_by=(f"{_ID}/im-family",)),
        ),
        forbidden_options=(
            Slot(id="trap-gala", kind=Kind.CURRENT, norm=Norm.TEXT, expected="gala",
                 alternatives=("museum",)),
            Slot(id="trap-5k", kind=Kind.CURRENT, norm=Norm.TEXT, expected="5k",
                 alternatives=("charity run",)),
        ),
    )
    dinner_day = Checkpoint(
        id=f"{_ID}/dinner-day", at=_t(4, "19:05"),
        question="What day is dinner with Dana?",
        mode="unanswerable",
        slots=(
            Slot(id="dana-day", kind=Kind.ABSTAIN, norm=Norm.DATE, expected="",
                 forbidden=("Saturday", "Sunday"), supported_by=(f"{_ID}/gm-dinner",)),
        ),
    )
    return Life(id=_ID, bet="implicit-context", role="development",
                observations=obs, checkpoints=(fun, dinner_day))


def _control() -> Life:
    obs = (
        Observation(
            source_id=f"{_CTL}/email-dentist", channel="email",
            written_at=_t(1, "09:00"), arrived_at=_t(1, "09:00"),
            collected_at=_t(1, "09:00"), sender="Bright Smile Dental", thread="dentist",
            text="Reminder: your dental cleaning is Friday at 2:30pm. Reply to reschedule.",
        ),
    )
    when = Checkpoint(
        id=f"{_CTL}/dentist-when", at=_t(4, "08:00"),
        question="What time is my dentist appointment on Friday?",
        slots=(
            Slot(id="time", kind=Kind.CURRENT, norm=Norm.TIME, expected="2:30pm",
                 alternatives=("2:30",), supported_by=(f"{_CTL}/email-dentist",)),
        ),
    )
    return Life(id=_CTL, bet="implicit-context", role="development",
                observations=obs, checkpoints=(when,), control_of=_ID)


def lives() -> list[Life]:
    return [_bet(), _control()]
