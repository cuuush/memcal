"""Resolve date phrases against the timestamp of the line that said them.

Unrecognized phrases return ``None``: callers ask instead of guessing. The same resolver
supports forward extraction and auditing dates already written to the store.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from . import db

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
MONTHS = ("january", "february", "march", "april", "may", "june", "july",
          "august", "september", "october", "november", "december")

#: Common abbreviations. Some overlap ordinary words; false positives remain candidates
#: and are handled permissively by grouping and date auditing.
_ABBREV = {
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "weds": 2, "thu": 3, "thur": 3,
    "thurs": 3, "fri": 4, "sat": 5, "sun": 6,
}

#: Month abbreviations, and the one rule that makes them safe: they count only with a
#: day number behind them. `may` is a modal verb, `mar` and `sep` are names and word
#: fragments, and unlike `weds` a bare one is not a claim about any day — "we can go in
#: Sept" commits to nothing. `Sept 25` does.
_MONTH_ABBREV = ("sept", "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep",
                 "oct", "nov", "dec")

_MONTH_WORD = "|".join(sorted(set(MONTHS) | set(_MONTH_ABBREV), key=len, reverse=True))
_ABBREV_WORD = "|".join(sorted(_MONTH_ABBREV, key=len, reverse=True))

#: A day number, ordinal suffix and all. `22nd` used to end the match one character early
#: and the whole month phrase failed with it — see `resolve`.
_DAY = r"(\d{1,2})(?:st|nd|rd|th)?"
#: A year, when the text bothers to say one. Optional, and authoritative when present.
_YEAR = r"(?:,?\s*(\d{4}))?"
_ISO_PATTERN = r"\d{4}-\d{2}-\d{2}"

_ORDINAL_RE = re.compile(r"\b(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b", re.IGNORECASE)
_ISO_RE = re.compile(rf"\b{_ISO_PATTERN}\b")
_MONTH_DAY_RE = re.compile(rf"\b({_MONTH_WORD})\w*\.?\s+{_DAY}{_YEAR}\b", re.IGNORECASE)
#: "the 25th of September", "25 December". The same date with the halves swapped, which
#: is common enough in their corpus to be worth reading and was previously answered by the
#: bare-ordinal branch — with the month thrown away.
_DAY_MONTH_RE = re.compile(rf"\b{_DAY}\s+(?:of\s+)?({_MONTH_WORD})\w*\.?{_YEAR}\b",
                           re.IGNORECASE)
#: A month name with a number beside it that neither pattern above could read — most
#: often a month and a year with no day in it. It is not a date, and it must not be
#: allowed to fall through to a branch that answers using the number alone.
_MONTH_BESIDE_NUMBER_RE = re.compile(
    rf"\b(?:{_MONTH_WORD})\w*\.?[\s,]*\d|\d(?:st|nd|rd|th)?[\s,]*(?:of\s+)?(?:{_MONTH_WORD})\b",
    re.IGNORECASE)

#: A date the text *states* rather than one resolved against when it was said. A month
#: with a day, or an ISO date: both mean the same day whoever reads them and whenever.
#: A weekday does not — "Saturday" is only a date if you know the week.
MONTH_OR_ISO_RE = re.compile(
    rf"\b(?:{_MONTH_WORD})\w*\.?\s+\d{{1,2}}\b|\b{_ISO_PATTERN}\b", re.IGNORECASE)

#: Any wording that commits to a day. Used to find the phrases worth resolving in free
#: text, so an audit can ask "does this row's date agree with its own evidence?".
#:
#: The ISO clause is built from the same pattern `resolve` reads, because the two had
#: drifted: `resolve` handled `2026-09-25` and this never offered it one, so 370 archive
#: lines carried a date nothing could see.
PHRASE_RE = re.compile(
    r"\b(?:" + "|".join(WEEKDAYS) + r"|" + "|".join(MONTHS)
    + r"|" + "|".join(sorted(_ABBREV, key=len, reverse=True))
    + r"|today|tonight|tomorrow|yesterday|next week|this weekend|next weekend)\b"
    + rf"|\b{_ISO_PATTERN}\b"
    + rf"|\b(?:{_ABBREV_WORD})\w*\.?\s+\d{{1,2}}\b",
    re.IGNORECASE)


def _month_number(word: str) -> int | None:
    """`september`, `Sept`, `sep.` → 9. The first three letters decide it, uniquely."""
    stem = (word or "").lower()[:3]
    return next((i for i, name in enumerate(MONTHS, start=1)
                 if name.startswith(stem)), None) if len(stem) == 3 else None


def resolve(phrase: str, said_on) -> str | None:
    """A date phrase plus the moment it was said → an ISO date, or None.

    `said_on` may be a `datetime` or a `date`; anything else, or an unparseable phrase,
    returns None.
    """
    text = (phrase or "").strip().lower()
    if not text or said_on is None:
        return None
    anchor = said_on.date() if hasattr(said_on, "date") else said_on

    iso = _ISO_RE.search(text)
    if iso:
        try:
            return db.parse_date(iso.group(0)).isoformat()
        except ValueError:
            return None

    if "today" in text or "tonight" in text:
        return anchor.isoformat()
    if "tomorrow" in text:
        return (anchor + timedelta(days=1)).isoformat()
    if "yesterday" in text:
        return (anchor - timedelta(days=1)).isoformat()

    # "September 25" and "the 25th of September" are the same date with the halves
    # swapped; whichever comes first in the phrase is the one being talked about.
    named = [(m, order) for m, order in
             ((_MONTH_DAY_RE.search(text), "month first"),
              (_DAY_MONTH_RE.search(text), "day first")) if m]
    if named:
        match, order = min(named, key=lambda pair: pair[0].start())
        first, second, stated_year = match.group(1), match.group(2), match.group(3)
        month_word, day_text = (first, second) if order == "month first" else (second, first)
        month, day = _month_number(month_word), int(day_text)
        if month is None:
            return None
        # A year somebody wrote down is evidence, and the arithmetic below is inference.
        # Overriding the first with the second is invariant 5 in the one module written
        # never to do that: "March 3, 2028" said in 2026 answered 2027, and "July 4,
        # 2027" said this August answered a day that had already gone by. Whether a date
        # this far out is worth *storing* is `apply`'s call and it has bounds for it.
        if stated_year:
            try:
                return date(int(stated_year), month, day).isoformat()
            except ValueError:
                return None
        for year in (anchor.year, anchor.year + 1):
            try:
                candidate = date(year, month, day)
            except ValueError:
                return None
            # A bare month and day with no year means the next one, not one in the past.
            if (candidate - anchor).days >= -180:
                return candidate.isoformat()
        return None

    index = next((i for i, name in enumerate(WEEKDAYS) if name in text), None)
    if index is None:
        # Longest first, so "thurs" is not read as "thu" and "weds" not as "wed".
        for short in sorted(_ABBREV, key=len, reverse=True):
            if re.search(rf"\b{short}\b", text):
                index = _ABBREV[short]
                break
    if index is not None:
        ahead = (index - anchor.weekday()) % 7
        # "Saturday" said on a Saturday means today; every other weekday means the next
        # one. "next saturday" skips a week — which is what people mean often enough to
        # honour, and ambiguous enough that being wrong here is a known cost.
        if ahead == 0 and "next" in text:
            ahead = 7
        elif ahead and "next" in text and ahead < 7:
            ahead += 7
        return (anchor + timedelta(days=ahead)).isoformat()

    # The bare ordinal below reads a day number and picks the next month containing it —
    # right when nobody named a month, and catastrophic when somebody did. "party on
    # January 22nd" reached it because `_MONTH_DAY_RE` ended `(\d{1,2})\b` and that
    # boundary cannot match with `nd` behind it; the month branch failed, this one
    # answered, and the answer was August 22nd. Both halves are fixed — the pattern reads
    # the suffix now — but the fall-through is the defect and it is closed here too: a
    # number beside a month name is that month's day or it is nothing, and nothing is the
    # cheap failure this module is built around.
    if _MONTH_BESIDE_NUMBER_RE.search(text):
        return None

    ordinal = _ORDINAL_RE.search(text)
    if ordinal:
        day = int(ordinal.group(1))
        if not 1 <= day <= 31:
            return None
        for offset in (0, 1):
            month = anchor.month + offset
            year = anchor.year + (month - 1) // 12
            month = (month - 1) % 12 + 1
            try:
                candidate = date(year, month, day)
            except ValueError:
                continue
            if candidate >= anchor:
                return candidate.isoformat()
        return None
    return None


def weekday_of(iso: str) -> str:
    """`2026-08-01` → `saturday`. For saying which day a row actually landed on."""
    try:
        return WEEKDAYS[db.parse_date(iso).weekday()]
    except (ValueError, TypeError):
        return "?"


def said_on(ts: str) -> str:
    """`2026-08-10T14:52` → `Mon 10 Aug 14:52`."""
    raw = str(ts or "")
    try:
        # Not `db.parse_ts`, which answers an unreadable stamp with *now* so that
        # comparisons never raise. That is the right trade where two timestamps are being
        # ordered and exactly the wrong one here: it would render today's weekday beside
        # somebody's message and read as a fact.
        stamp = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return raw
    return stamp.strftime("%a %-d %b %H:%M")


#: Context kept either side of a matched phrase. Both sides affect resolution and for
#: different reasons: the qualifier that makes "saturday" into "next saturday" sits to the
#: left, and the day number that makes "July" into "July 30" sits to the right. Taking
#: only the left — as this did at first — meant no month-and-day date in the corpus
#: resolved at all: "Join us Thursday, July 30 at Nitehawk" was truncated to "…, July"
#: and fell through to the weekday branch, landing on the wrong day entirely.
#:
#: Eight to the right was enough for the day and not for the year behind it:
#: `September 25, 2026` arrived as `September 25, 202`, and a stated year that never
#: reaches `resolve` cannot be honoured by it. Fourteen carries `25th, 2026`.
LEFT_CONTEXT, RIGHT_CONTEXT = 12, 14


def phrase_at(text: str, match: "re.Match") -> str:
    """One matched phrase plus the context needed to read it correctly."""
    return text[max(0, match.start() - LEFT_CONTEXT):match.end() + RIGHT_CONTEXT].strip()


def claims(text: str) -> list[str]:
    """Every day-committing phrase in a piece of text, in the order they appear."""
    return [phrase_at(text or "", m) for m in PHRASE_RE.finditer(text or "")]
