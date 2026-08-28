"""Derive affinity groups without a model call.

Fragments are split by date phrase, matched by distinctive words and participants, and
packed within configured size limits. Grouping influences prompt co-occurrence rather
than database mutation.

Dates are resolved deterministically against the source line timestamp.

Key design constraints:
- Multiple occasions per line: generates one fragment per date phrase with a local context window.
- Platform tag suppression: `ambient_tokens` filters single-voice tokens spread across multiple dates.
- Bounded transitivity: agglomerative grouping enforces pack budgets rather than unbounded partitioning.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from itertools import combinations

from .. import dates, db

#: Gate verdicts indicating planning activity. `subject-event` is omitted to avoid noise from commercial subjects.
PLANNING = frozenset({"temporal", "invitation", "commitment-verb", "own-commitment",
                      "directive", "availability", "question"})

#: Common conversational and temporal stop words excluded from topic matching.
_NOISE = frozenset({
    "the", "and", "with", "for", "you", "your", "our", "was", "were", "are", "have",
    "has", "had", "this", "that", "then", "than", "there", "their", "them", "they",
    "night", "day", "morning", "evening", "afternoon", "tonight", "tomorrow", "today",
    "trip", "visit", "meetup", "hang", "out", "event", "thing", "plans", "party", "next",
    "last", "some", "any", "going", "get", "see", "want", "like", "just", "know", "can",
    "will", "would", "could", "should", "about", "back", "come", "coming", "make",
    "time", "week", "weekend", "yeah", "lol", "haha", "gonna", "wanna", "let", "who",
    "what", "when", "where", "how", "why", "not", "but", "all", "one", "also", "still",
})

_WORD_RE = re.compile(r"[a-z0-9']+")

#: Character window on either side of a date phrase used for token extraction.
WINDOW = 90

#: Minimum conversation count required before applying document-frequency token suppression.
MIN_CORPUS_FOR_DF = 12

#: Minimum absolute conversation count for document-frequency suppression ceiling.
MIN_DF_CEILING = 4


@dataclass(frozen=True)
class Fragment:
    """One reference to something dated, found in one line by code alone."""

    entity: str
    archive_id: int
    when: str | None
    tokens: frozenset[str]
    who: frozenset[str]
    origin: str

    @property
    def dated(self) -> bool:
        return self.when is not None


def _tokens(text: str) -> frozenset[str]:
    out = set()
    for token in _WORD_RE.findall((text or "").lower()):
        token = token.strip("'")
        if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        if len(token) > 2 and token not in _NOISE and not token.isdigit():
            out.add(token)
    return frozenset(out)


def _origin(row, entity: str) -> str:
    """Who is vouching, collapsed to one voice.

    An organisation is one source however many of its staff mail you. Counting raw
    mentions instead would let the loudest sender in the mailbox out-vote a person, and
    it is also what decides whether a word is a platform tag or a real subject.
    """
    stream = str(row["stream"] or "")
    handle = str(row["handle"] or "")
    if stream == "email" and "@" in handle:
        return f"email:{handle.rsplit('@', 1)[-1].lower()}"
    if handle:
        return f"{stream}:{handle}".lower()
    # User-authored rows often have no handle. Use the thread as the fallback voice so
    # unrelated conversations are not collapsed into one speaker.
    return f"{stream}:{entity}".lower()


def fragments(bundle) -> list[Fragment]:
    """Every dated reference in one bundle, one per date phrase rather than per line."""
    out: list[Fragment] = []
    for row in bundle.items:
        text = str(row["text"] or "")[:2000]
        if not text:
            continue
        try:
            said_on = db.parse_ts(str(row["ts"]))
        except ValueError:
            said_on = None
        who = frozenset(filter(None, {str(row["person"] or "").lower()}))
        seen_phrase = False
        for match in dates.PHRASE_RE.finditer(text):
            when = dates.resolve(dates.phrase_at(text, match), said_on)
            if not when:
                continue
            seen_phrase = True
            window = text[max(0, match.start() - WINDOW):match.end() + WINDOW]
            out.append(Fragment(bundle.entity, int(row["id"]), when,
                                _tokens(window), who, _origin(row, bundle.entity)))
        if not seen_phrase and str(row["gate_reason"] or "") in PLANNING:
            # Plan-shaped but with no day named. Carries no date to agree on, so it can
            # only link on wording, and `related` demands more of it for that reason.
            out.append(Fragment(bundle.entity, int(row["id"]), None,
                                _tokens(text[:400]), who, _origin(row, bundle.entity)))
    return out


def ambient_tokens(frags: list[Fragment], spread_days: int = 6) -> frozenset[str]:
    """Words that name a channel rather than an occasion, derived rather than listed.

    An occasion's name clusters into a few days because that is what an occasion is; a
    platform's name is scattered across the year. But spread alone is not enough — over a
    real corpus it suppressed every word that was doing the linking. What separates them
    is *who says it*: a platform tag is stamped by one exporter and nobody else ever
    types it, while an occasion is named by everyone going.
    """
    days: dict[str, list[int]] = {}
    voices: dict[str, set[str]] = {}
    for frag in frags:
        if not frag.when:
            continue
        try:
            ordinal = db.parse_date(frag.when).toordinal()
        except ValueError:
            continue
        for token in frag.tokens:
            days.setdefault(token, []).append(ordinal)
            voices.setdefault(token, set()).add(frag.origin)
    return frozenset(
        token for token, seen in days.items()
        if len(seen) > 2 and max(seen) - min(seen) > spread_days
        and len(voices.get(token, ())) < 2)


def common_tokens(by_entity: dict[str, list[Fragment]],
                  max_share: float = 0.08) -> frozenset[str]:
    """Words too widespread to identify anything, measured across conversations."""
    # Document frequency is meaningless on a handful of conversations: at a floor of two,
    # a three-bundle corpus suppresses any word all three mention, which is *the* word
    # they are about. That is not a synthetic worry — a nightly pass is 13-15 bundles, so
    # the pass this runs on 364 days a year sits right in the range where a low floor
    # deletes the signal. Below the minimum, skip it entirely and let `ambient_tokens`
    # carry the suppression on its own.
    if len(by_entity) < MIN_CORPUS_FOR_DF:
        return frozenset()
    seen: dict[str, set[str]] = {}
    for entity, frags in by_entity.items():
        for frag in frags:
            for token in frag.tokens:
                seen.setdefault(token, set()).add(entity)
    ceiling = max(MIN_DF_CEILING, int(len(by_entity) * max_share))
    return frozenset(t for t, entities in seen.items() if len(entities) > ceiling)


def related(a: Fragment, b: Fragment, ambient: frozenset[str], near_days: int) -> bool:
    """Could these two be references to one occasion?

    Only subject matter counts. Two fragments are not linked because they arrived by the
    same channel, from the same kind of sender, or because both concern tickets — an
    order confirmation for a festival and an invitation to a cinema are both of those and
    are not the same evening.
    """
    if a.entity == b.entity:
        return False
    shared = (a.tokens & b.tokens) - ambient
    if a.dated and b.dated:
        try:
            apart = abs(db.parse_date(a.when).toordinal() - db.parse_date(b.when).toordinal())
        except ValueError:
            return False
        if apart > near_days:
            return False
        # An agreeing date is most of the evidence, so one distinctive word is enough —
        # which is the case the row-level threshold gets wrong in the other direction,
        # rejecting `beer-hall`/`beer-garden` for sharing only `{beer}`.
        return bool(shared) or len(a.who & b.who) >= 2
    # Nothing agrees on a day, so wording has to carry it alone and has to be specific:
    # one shared word with no agreeing date is how "movie" links a cinema trip to a film
    # somebody watched at home.
    return len(shared) >= 2


def score_pairs(bundles: list, near_days: int = 3) -> dict[tuple[str, str], int]:
    """How strongly two conversations look like one occasion."""
    by_entity: dict[str, list[Fragment]] = {}
    for bundle in bundles:
        found = fragments(bundle)
        if found:
            by_entity[bundle.entity] = found
    ambient = (ambient_tokens([f for group in by_entity.values() for f in group])
               | common_tokens(by_entity))
    scores: dict[tuple[str, str], int] = {}
    for left, right in combinations(sorted(by_entity), 2):
        subjects: set[tuple[str, str]] = set()
        for a in by_entity[left]:
            for b in by_entity[right]:
                if not related(a, b, ambient, near_days):
                    continue
                for token in (a.tokens & b.tokens) - ambient:
                    subjects.add((a.when or b.when or "?", token))
        if subjects:
            scores[(left, right)] = len(subjects)
            _SUBJECTS[(left, right)] = subjects
    return scores


#: The `(day, word)` facts behind the most recent `score_pairs`, kept so a grouping can
#: be *read* rather than trusted. A number saying two conversations are related is not
#: checkable by a human; "2026-08-02 · garden, bohemian" is.
_SUBJECTS: dict[tuple[str, str], set[tuple[str, str]]] = {}


def subjects_for(left: str, right: str) -> set[tuple[str, str]]:
    return _SUBJECTS.get((left, right)) or _SUBJECTS.get((right, left)) or set()


def group(bundles: list, *, max_bundles: int, max_tokens: int, cost,
          near_days: int = 3) -> tuple[list[list], list]:
    """Bundles arranged into requests, the most-related first."""
    index = {b.entity: b for b in bundles}
    costs = {b.entity: cost(b) for b in bundles}
    scores = score_pairs(bundles, near_days)
    neighbours: dict[str, dict[str, int]] = {}
    for (left, right), hits in scores.items():
        neighbours.setdefault(left, {})[right] = hits
        neighbours.setdefault(right, {})[left] = hits

    groups: list[list] = []
    placed: set[str] = set()
    for (left, right), _hits in sorted(scores.items(), key=lambda kv: -kv[1]):
        if left in placed or right in placed:
            continue
        members = [left, right]
        placed.update(members)
        total = costs[left] + costs[right]
        # Accrete whatever is most related to anything already in the group, strongest
        # first, until a budget stops it.
        while len(members) < max_bundles:
            candidates = {
                other: hits
                for member in members
                for other, hits in neighbours.get(member, {}).items()
                if other not in placed and total + costs[other] <= max_tokens
            }
            if not candidates:
                break
            best = max(candidates, key=lambda k: candidates[k])
            members.append(best)
            placed.add(best)
            total += costs[best]
        groups.append([index[e] for e in members])

    leftovers = [b for b in bundles if b.entity not in placed]
    return groups, leftovers


def describe(bundles: list, near_days: int = 3, limit: int = 20) -> list[str]:
    """Readable pairs, strongest first. For looking at before trusting it."""
    scores = score_pairs(bundles, near_days)
    titles = {b.entity: (b.title or b.entity) for b in bundles}
    out = []
    for (a, b), hits in sorted(scores.items(), key=lambda kv: -kv[1])[:limit]:
        subjects = sorted(subjects_for(a, b))[:4]
        why = ", ".join(f"{day} {word}" for day, word in subjects)
        out.append(f"{hits:>3}  {titles.get(a, a)[:28]:<28} ~ {titles.get(b, b)[:28]:<28} {why}")
    return out
