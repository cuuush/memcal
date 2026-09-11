"""Stage 3: merge proposals that several conversations made about the same key."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

from .. import db, events, llm, pending, trace
from ..config import Config
from ..llm import CompletionClient
from .bundle import Bundle

#: Two mentions of one event are rarely written on the same day — "next Sunday" said on
#: a Friday and "sunday after 6" said on a Monday describe one evening. Wider than this
#: and a weekly poker game starts absorbing next week's.
NEAR_DAYS = 4

#: How far apart two mentions may be for a shared guest list *alone* — no shared wording
#: at all — to say they are one event. Zero: the same day.
#:
#: Shared-guest matching is same-day only. A wider window merges unrelated plans by
#: friend-group membership; wording and place matches handle legitimate cross-day cases.
SAME_GUESTS_DAYS = 0

#: Words that carry no identity. "beer garden with quinn" and "beer garden with julian"
#: must overlap on "beer" and "garden", not on "with".
_NOISE = {
    "the", "a", "an", "and", "or", "with", "at", "in", "on", "for", "to", "of", "my",
    "our", "his", "her", "their", "me", "we", "us", "i", "is", "are", "be", "night",
    "day", "morning", "evening", "afternoon", "trip", "visit", "meetup", "hang", "out",
}

#: Words a source stamps on every title it exports. These name the channel, never the
#: occasion, so they may not be the evidence that two rows are one thing.
#:
#: Partiful appends "| Partiful" to every title, and the store holds fifteen of them:
#: "Jack's 30th | Partiful" on 08-22 and "Capture The Flag 2 - Trojan War | Partiful" on
#: 08-23 are a birthday and a field game a day apart whose only shared distinctive word
#: is `partiful`. A threshold of two hid that by accident; the field-poor threshold below
#: is one, so without this the tag alone would fabricate one party out of two.
#:
#: A literal list, and deriving it from the store was tried and is worse. Two rules were
#: measured against the real corpus:
#:
#:   *date spread and voice count* — what `affinity.ambient_tokens` uses. It needs a large
#:     population to mean anything; Merge sees one run's proposed rows. At that size the
#:     same rule once suppressed every word that was doing the linking (M31, M43). It also
#:     keys on how many voices say a word, so if every poker row traces to one friend's
#:     thread, `poker` reads as one voice scattered across the year — undoing M12.
#:
#:   *appears only in importer-written titles* — sound in principle and dangerous in
#:     practice, because it depends on a population that is empty exactly when it matters.
#:     Run against a store with no dream rows yet — a cold start, the case with the most
#:     duplicates — it suppressed `birthday`, `appointment`, `meeting`, `gym`, `improv`
#:     and `reese`, the last of which would stop the user's partner's name from ever
#:     clustering anything.
#:
#: Three words that need editing when a fourth exporter appears is the cheaper failure.
_PLATFORM = {"partiful", "eventbrite", "evite"}


def _tokens(title: str) -> set[str]:
    """Distinctive words, singularised. "Bier gardens" and "Beer garden at Bohemian
    Hall" are the same plan written by two people; the plural alone must not hide it."""
    out = set()
    for token in db.slugify(title or "").split("-"):
        if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
            token = token[:-1]
        if token and token not in _NOISE:
            out.add(token)
    return out


def _people(row: dict) -> set[str]:
    return {db.slugify(p) for p in (row.get("participants") or []) if isinstance(p, str)}


def _person_variant(a: dict, b: dict) -> bool:
    """Does one guest list use a bare given name for a fuller name in the other?"""
    left, right = _people(a), _people(b)
    for one in left:
        for other in right:
            short, long = sorted((one, other), key=len)
            if "-" not in short and long.startswith(short + "-"):
                return True
    return False


def _field_poor(row: dict) -> bool:
    """No guest list and no place — a date and a title are all this row has to match on.

    Propose fills these fields from whatever the conversation happened to say, so the
    rows that carry neither are not the unimportant ones; they are the ones written from
    a passing line ("beer hall saturday?") that names no venue and tags nobody. That is
    the same line most likely to be said twice in two threads, so field-poor and
    duplicated are the same population.
    """
    return not _people(row) and not str(row.get("location") or "").strip()


class Mention:
    """One proposed row, and the conversation that proposed it.

    The pairing is the point. A row on its own is an assertion; a row with its source
    is evidence, and only evidence can be weighed against other evidence.
    """

    __slots__ = ("row", "bundle", "diff", "existing")

    def __init__(self, row: dict, bundle: Bundle, diff: dict, existing: bool = False):
        self.row, self.bundle, self.diff = row, bundle, diff
        #: A row already in the store, pulled in to be compared against. Never collapsed.
        self.existing = existing

    @property
    def date(self) -> str:
        return str(self.row.get("date") or "")

    @property
    def origin(self) -> str:
        """Who is vouching for this, collapsed to one voice.

        An organisation is one source however many of its staff write to you. A movie
        night was referred to nine times and three of those were `hannah@`, `molly@` and
        `derrick@ridersalliance.org` — one organisation mailing a list, not three
        witnesses. Counting bundles instead of voices lets whoever sends the most email
        outvote a person, which is the wrong way round.
        """
        entity = self.bundle.entity
        if entity.startswith("thread:email:") and "@" in entity:
            return f"email:{entity.rsplit('@', 1)[-1].lower()}"
        return entity

    def describe(self) -> str:
        bits = [f"from {self.bundle.entity}", f"date {self.date or '?'}"]
        if self.row.get("key"):
            bits.append(f"key {self.row['key']}")
        for field in ("time", "until", "location", "status", "kind"):
            if self.row.get(field):
                bits.append(f"{field} {self.row[field]}")
        if self.row.get("participants"):
            bits.append("with " + ", ".join(map(str, self.row["participants"])))
        if self.row.get("note"):
            bits.append(f"note {self.row['note']}")
        return f"  - {self.row.get('title') or '(untitled)'}  [{'; '.join(bits)}]"


def same_event(a: Mention, b: Mention, cfg: Config | None = None,
               links: set[frozenset] | None = None) -> bool:
    """Could these two be one event?"""
    if links and a.row.get("key") and b.row.get("key"):
        if frozenset((str(a.row["key"]), str(b.row["key"]))) in links:
            return True
    left_subject = str(a.row.get("subject") or "me")
    right_subject = str(b.row.get("subject") or "me")
    subjects_differ = left_subject.casefold() != right_subject.casefold()
    try:
        apart = abs(db.parse_date(a.date).toordinal() - db.parse_date(b.date).toordinal())
    except ValueError:
        return False
    if apart > NEAR_DAYS:
        return False
    if {a.row.get("status"), b.row.get("status")} == {"happened", "confirmed"}:
        return False

    shared_title = _tokens(a.row.get("title", "")) & _tokens(b.row.get("title", ""))
    shared_title -= _PLATFORM        # the exporter's name is not the occasion's name
    shared_people = _people(a.row) & _people(b.row)
    same_place = bool(a.row.get("location")) and db.slugify(str(a.row["location"])) == \
        db.slugify(str(b.row.get("location") or ""))

    # "Quinn is coming over" can describe the same user commitment as "work on the
    # song with Quinn". One proposer made the guest the subject and another made the
    # user the subject; rejecting different subjects before looking at the roster meant
    # Merge never got to arbitrate them. Keep this exact-day and reciprocal: arbitrary
    # rows about two different people remain incomparable.
    reciprocal_subject = False
    if (subjects_differ and apart == 0
            and "me" in {left_subject.casefold(), right_subject.casefold()}):
        other = right_subject if left_subject.casefold() == "me" else left_subject
        reciprocal_subject = (
            db.slugify(other) in shared_people
            and a.row.get("kind") == b.row.get("kind") == "commitment"
        )
    if subjects_differ and not reciprocal_subject:
        return False
    if reciprocal_subject:
        return True

    # Two of the same people, on the same evening, is stronger evidence than any
    # wording — and it is the only thing that survives the wording changing. The live
    # case that motivated this stage was "Bier gardens with Quinn and Jamie" against
    # "Beer garden at Bohemian Hall": not one word in common, the same two guests, the
    # same evening. Requiring a shared word first is what let it through as two rows.
    #
    # The date bound is not decoration. Unbounded, this rule read "the same two people,
    # within four days" — which is the ordinary shape of a friend group, not of one
    # event. Three separate outings in one week with the same two people otherwise
    # collapse into a single cluster on the shared guests alone. That cluster disagrees
    # about both date and title, so it costs a model call every run, and when the call
    # fails `_merge_locally` keeps the earliest fragment and drops the rest.
    if len(shared_people) >= 2 and apart <= SAME_GUESTS_DAYS:
        return True

    # An exact clock time on an identical date is the strongest single agreement two
    # importers can reach, and `same_event` did not read `time` at all. Rows carrying a
    # place and a person are not field-poor, so they faced the full two-word title bar —
    # which the same place written two ways, or the same person written with and without
    # a qualification, actively works against. An identical date and non-null time lowers
    # the bar to one point of contact, the trade `same_event_poor_tokens` already makes
    # for rows with no fields at all. Not the time alone: something must still be shared.
    # Two things cannot occupy one clock time, so the cluster is worth its call even when
    # the call refuses it.
    clock = str(a.row.get("time") or "")
    if apart == 0 and clock and clock == str(b.row.get("time") or ""):
        if shared_people or same_place or (shared_title and _person_variant(a.row, b.row)):
            return True

    if not shared_title:
        return False

    # One sender describing one day. Clustering is refusable; the duplicate is not.
    if apart == 0 and a.origin == b.origin:
        return True
    # One distinctive word is only suggestive — "poker" and "lunch" both being with
    # Quinn on Tuesday is two plans, not one. A word plus a person, a word plus a
    # place, or two words, is enough.
    if shared_people or same_place:
        return True

    # …unless either row has neither a person nor a place to offer, in which case asking
    # for a second word rejects precisely the pairs this stage exists to catch.
    # `beer-hall` against `beer-garden` on 2026-08-02 shares `{beer}`; `poker` against
    # `poker-game` on 2026-08-01 shares `{poker}`. Both stayed two rows for one evening
    # on step-3.7-flash and on gpt-5.6-luna alike, because a size-1 overlap never became
    # a cluster and so no model was ever asked the question (M11, M12). Two models
    # failing identically is what says the threshold, not the model, is the constraint.
    #
    # A cluster costs one call and can still be refused; the duplicate it prevents is
    # permanent. The date bound above is what keeps the loosened threshold safe — the
    # weekly poker game is four days past NEAR_DAYS before this line is ever reached, and
    # the platform tags are already out of `shared_title`.
    thresholds = cfg if cfg is not None else Config
    need = (thresholds.same_event_poor_tokens
            if _field_poor(a.row) or _field_poor(b.row)
            else thresholds.same_event_tokens)
    return len(shared_title) >= need


def corroboration(group: list[Mention]) -> int:
    """Count independent origins, not repeated mentions from one origin."""
    return len({m.origin for m in group})


def cluster(mentions: list[Mention], cfg: Config | None = None,
            links: set[frozenset] | None = None) -> list[list[Mention]]:
    """Group mentions into events. Union-find over `same_event`, so a chain of pairwise
    matches lands in one cluster even when the ends of the chain do not resemble each
    other — which is exactly what a plan looks like as it is refined across threads."""
    parent = list(range(len(mentions)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(mentions)):
        for j in range(i + 1, len(mentions)):
            if find(i) != find(j) and same_event(
                    mentions[i], mentions[j], cfg, links):
                parent[find(i)] = find(j)

    groups: dict[int, list[Mention]] = {}
    for index, mention in enumerate(mentions):
        groups.setdefault(find(index), []).append(mention)
    return list(groups.values())


def _with_observations(groups: list[list[Mention]], observations: list[Mention],
                       *, include: bool = True) \
        -> list[list[Mention]]:
    """Show every plausible target together; ranking must not decide a cancellation."""
    for observation in observations:
        words = _tokens(str(observation.row.get("title") or observation.row.get("note") or ""))
        selected = []
        for index, group in enumerate(groups):
            overlap = max((len(words & _tokens(str(m.row.get("title") or "")))
                           for m in group), default=0)
            same_origin = any(m.origin == observation.origin for m in group)
            if overlap or same_origin:
                selected.append(index)
        if selected:
            combined = [m for index in selected for m in groups[index]]
            if include:
                combined.append(observation)
            first = selected[0]
            groups = [combined if index == first else group
                      for index, group in enumerate(groups)
                      if index == first or index not in selected]
        elif include:
            groups.append([observation])
    return groups


INSTRUCTIONS = """\
Several conversations described what may be the same event. Each was read on its own, so
each proposal only knows its own thread — one of them may have been guessing about a
detail another one states outright.

Decide what is actually true. Return one row only when the evidence describes one
occasion. A cancelled old booking followed by a newly booked date is two occasions:
answer same_event false so the declined old row and confirmed new row both survive.

WEIGH THE SOURCE, NOT THE COUNT
Three fragments repeating a guess do not outvote one that was there. Prefer:
  - a detail stated by the people arranging it over one mentioned in passing elsewhere
  - a specific date ("sunday after 6") over one derived from a vague phrase ("next
    weekend", "sometime soon"), whichever is more common
  - a correction over what it corrected — later beats earlier when they conflict
The timestamped source lines behind each fragment are given. A plan discussed in its own thread is
better evidence about that plan than a reference to it inside a thread about something
else entirely.

WHEN YOU CANNOT TELL, SAY SO
Answer `"unresolved"` rather than choosing. Two rows the user can see and merge later
is a recoverable mistake; one row assembled out of two real plans is not.

POOL WHAT DOES NOT CONFLICT
Guest lists especially: each thread knows the people in it, and none knows everyone.
Union the participants unless a fragment says someone is not coming. Take the most
specific title, location and time available from any fragment.

IF THEY ARE NOT THE SAME EVENT
Say so with same_event false, and every proposal is kept as its own row. Two poker
nights a week apart are two poker nights. Only merge what is genuinely one occasion.
The schema still requires the other fields, so fill them from any one fragment and put
the reason in `why`. Do not stitch two answers into one field: "Go running; Go to a bar"
is not a title and "confirmed; mentioned" is not a status.
Use observation_targets and pending_targets to identify
which dated proposal each cancellation concerns, including when same_event is false.
Leave those arrays empty when the evidence cannot identify a target. SOURCE ids support
fields through citations; a stored summary or the time it was written is not new evidence."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    # Every key in `properties`, because strict json_schema requires it — a field that is
    # merely optional is expressed as a nullable type, never by omission from this list.
    # It was the five required keys, which the previous endpoint accepted and OpenAI
    # rejects outright: `Missing 'until'`, HTTP 400, on every conflicted cluster in a
    # run. The stage did not fall over visibly. It fell back to `_merge_locally` for the
    # whole pass, which keeps the earliest-dated fragment and drops the rest — so the one
    # stage built to arbitrate dates was silently answering "the earlier one" every time.
    # Every other schema in the package already lists all its keys; this was the outlier.
    "required": ["same_event", "date", "until", "time", "title", "location",
                 "kind", "status", "participants", "note", "citations", "links",
                 "pending_targets", "observation_targets", "why"],
    "properties": {
        # Three answers, not two. "I cannot tell from this" is a real state and it has
        # to be sayable: with only true/false the honest answer had nowhere to go and
        # came back as a merge, which is the one outcome that cannot be undone by
        # looking at the calendar.
        "same_event": {"type": ["boolean", "string"],
                       "enum": [True, False, "unresolved"],
                       "description": "false if these are genuinely different occasions;"
                                      " \"unresolved\" if what you were shown cannot"
                                      " settle it. Never guess: two rows can be merged"
                                      " later, one wrong row cannot be taken apart"},
        "date": {"type": "string", "description": "yyyy-mm-dd"},
        "until": {"type": ["string", "null"]},
        "time": {"type": ["string", "null"]},
        "title": {"type": "string"},
        "location": {"type": ["string", "null"]},
        "kind": {"type": ["string", "null"]},
        "status": {"type": ["string", "null"]},
        "participants": {"type": "array", "items": {"type": "string"}},
        "note": {"type": ["string", "null"]},
        "citations": {
            "type": "array",
            "description": "source ids supporting each field selected for the merged row",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["field", "source_ids"],
                "properties": {
                    "field": {"type": "string", "enum": [
                        "date", "until", "time", "title", "location", "kind",
                        "status", "participants", "note"]},
                    "source_ids": {"type": "array", "items": {"type": "integer"}},
                },
            },
        },
        "links": {
            "type": "array",
            "description": "validated relationships to existing event keys",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["kind", "key"],
                "properties": {
                    "kind": {"type": "string", "enum": ["same_as", "replaces", "related"]},
                    "key": {"type": "string"},
                },
            },
        },
        "pending_targets": {
            "type": "array",
            "description": "undated observation ids and the 1-based proposal they target",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["pending_id", "proposal"],
                "properties": {
                    "pending_id": {"type": "integer"},
                    "proposal": {"type": "integer"},
                },
            },
        },
        "observation_targets": {
            "type": "array",
            "description": "fresh undated proposal and the dated proposal it describes",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["observation", "target"],
                "properties": {
                    "observation": {"type": "integer"},
                    "target": {"type": "integer"},
                },
            },
        },
        "why": {"type": "string",
                "description": "one sentence: which fragment settled the date, and why"},
    },
}


def _cites(group: list[Mention]) -> list[int]:
    """Every archive row any fragment pointed at."""
    out: list[int] = []
    for mention in group:
        for archive_id in mention.row.get("cite_ids") or ():
            if isinstance(archive_id, int) and archive_id not in out:
                out.append(archive_id)
    return out


def _links(group: list[Mention]) -> list[dict]:
    """Validated relationship claims from every fragment."""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for mention in group:
        for item in mention.row.get("links") or ():
            if not isinstance(item, dict):
                continue
            pair = (str(item.get("kind") or ""), str(item.get("key") or ""))
            if pair[0] not in events.LINK_KINDS or not pair[1] or pair in seen:
                continue
            seen.add(pair)
            out.append({"kind": pair[0], "key": pair[1]})
    return out


def _field_cites(group: list[Mention], merged: dict) -> dict[str, list[int]]:
    """Carry citations only from fragments agreeing with the selected field value."""
    out: dict[str, list[int]] = {}
    for field in ("date", "until", "time", "title", "location", "kind", "status",
                  "participants", "note"):
        selected = merged.get(field)
        ids: list[int] = []
        for mention in group:
            value = mention.row.get(field)
            agrees = value == selected
            if field == "participants" and isinstance(selected, list):
                agrees = bool(value) and set(value) <= set(selected)
            if not agrees:
                continue
            claimed = mention.row.get("field_cite_ids")
            values = (claimed.get(field) or ()) if isinstance(claimed, dict) \
                else (mention.row.get("cite_ids") or ())
            for archive_id in values:
                if isinstance(archive_id, int) and archive_id not in ids:
                    ids.append(archive_id)
        if ids:
            out[field] = ids
    return out


def _evidence_times(group: list[Mention], conn: sqlite3.Connection | None) -> dict[str, str]:
    ids = set(_cites(group))
    for mention in group:
        for row in mention.bundle.items:
            if "id" in row.keys() and row["id"]:
                ids.add(int(row["id"]))
    if conn is None or not ids:
        return {str(row["id"]): str(row["ts"])
                for mention in group for row in mention.bundle.items
                if "id" in row.keys() and row["id"] and row["ts"]}
    marks = ",".join("?" for _ in ids)
    rows = conn.execute(f"SELECT id, ts FROM archive WHERE id IN ({marks})", list(ids))
    return {str(row["id"]): str(row["ts"]) for row in rows}


def _guest_list(names) -> list[str]:
    """One person, one entry, keeping the fullest name anyone used."""
    seen: dict[str, str] = {}
    for name in names:
        if not isinstance(name, str) or not name.strip():
            continue
        name = name.strip()
        key = db.slugify(name)
        if key not in seen or len(name) > len(seen[key]):
            seen[key] = name

    out: dict[str, str] = {}
    for key, name in seen.items():
        if "-" not in key:                     # a bare single-word name
            fuller = [k for k in seen if k != key and k.startswith(key + "-")]
            if len(fuller) == 1:
                continue                       # it is the short form of that one person
        out[key] = name
    return sorted(out.values())


def _merge_locally(group: list[Mention]) -> dict:
    """Merge agreeing fragments without a model call."""
    base = dict(min(group, key=lambda m: m.date).row)
    for mention in group:
        if mention.existing and mention.row.get("key"):
            base["key"] = mention.row["key"]
            break
    # One person, one entry. A union of raw strings put Quinn in twice — once as
    # "Quinn Brooks" and once as "Quinn" — because each thread names people the way
    # that thread names them, and a guest list only ever grows. Folding on the slug and
    # keeping the fullest spelling gives the calendar the name a reader wants and the
    # store a single person.
    base["participants"] = _guest_list(
        person for mention in group for person in (mention.row.get("participants") or []))
    for field in ("time", "until", "location", "note"):
        values = [mention.row.get(field) for mention in group if mention.row.get(field)]
        if values:
            # Agreeing vague + specific fragments are not a conflict. Preserve the
            # richest wording: "Alex's place" must not erase "42 Example Street, Alex's
            # place" merely because its bundle happened to be first.
            base[field] = max(values, key=lambda value: (
                len(_tokens(str(value))), len(str(value))))
    statuses = [m.row.get("status") for m in group if m.row.get("status")]
    if statuses and "declined" not in statuses:
        rank = {"mentioned": 0, "tentative": 1, "confirmed": 2, "happened": 3}
        base["status"] = max(statuses, key=lambda value: rank.get(value, 0))
    base["cite_ids"] = _cites(group)
    return base


def _conflicted(group: list[Mention]) -> bool:
    """Does this cluster need a model at all? Only if the fragments disagree about
    something a union cannot settle. Agreeing fragments are the common case and must
    not cost a call."""
    if len({m.date for m in group}) > 1 or len(
            {db.slugify(str(m.row.get("title") or "")) for m in group}) > 1:
        return True
    for field in ("time", "until", "location"):
        values = [str(m.row.get(field)) for m in group if m.row.get(field)]
        token_sets = [_tokens(value) for value in values]
        # One wording containing the other's distinctive words is added specificity,
        # not disagreement: "Alex's place" / "42 Example Street, Alex's place".
        if len(values) > 1 and not all(
                a <= b or b <= a
                for index, a in enumerate(token_sets)
                for b in token_sets[index + 1:]):
            return True
    statuses = {m.row.get("status") for m in group if m.row.get("status")}
    if "declined" in statuses and len(statuses) > 1:
        return True
    return False


#: Base output allowance for one Merge judgement, before the endpoint's own boost.
MERGE_TOKENS = 1200
#: Choosing between existing actions is a smaller answer than merging rows.
QUESTION_MERGE_TOKENS = 800
#: Room for the answer once the model has stopped thinking. A merged row is a handful
#: of short fields; the thinking is the part that varies by model.
MERGE_ANSWER_TOKENS = 600


def _ceiling(cfg: Config, base: int = MERGE_TOKENS) -> int:
    """Allow for the endpoint's configured reasoning budget."""
    spec = llm.endpoint(cfg.match_model)
    return min(8000, max(int(base * spec.ceiling_boost),
                         spec.think_tokens + MERGE_ANSWER_TOKENS))


class _StoredBundle:
    """Stands in for the conversation a stored row came from."""

    __slots__ = ("entity", "items")

    def __init__(self, entity: str, items=()):
        self.entity, self.items = entity, list(items)


def _source_lines(conn: sqlite3.Connection | None, mention: Mention) -> list[str]:
    """Timestamped source evidence, or labeled stored-state history."""
    if not mention.existing:
        wanted = set(mention.row.get("cite_ids") or ())
        rows = [row for row in mention.bundle.items
                if not wanted or ("id" in row.keys() and row["id"] in wanted)]
        rows = rows[-6:]
        return [f"SOURCE {row['id']} at {row['ts']} — "
                f"{'me' if row['from_me'] else (row['person'] or 'they')}: {row['text']}"
                for row in rows if "id" in row.keys() and row["id"]]
    if conn is None or not mention.row.get("key"):
        return ["STORED STATE — no source evidence attached"]
    key = str(mention.row["key"])
    direct = conn.execute(
        """SELECT DISTINCT a.id, a.ts, a.person, a.from_me, a.text
             FROM evidence e JOIN archive a ON a.id = e.archive_id
            WHERE e.kind = 'event' AND e.ref = ? ORDER BY a.ts DESC LIMIT 6""", (key,)
    ).fetchall()
    lines = [f"SOURCE {row['id']} at {row['ts']} — "
             f"{'me' if row['from_me'] else (row['person'] or 'they')}: {row['text']}"
             for row in reversed(direct)]
    event = events.get(conn, key)
    if event is not None:
        stamp = conn.execute("SELECT updated_at FROM events WHERE id = ?", (event.id,)).fetchone()
        lines.append(f"STORED STATE at {stamp['updated_at']} — {event.status} on {event.date}")
        for row in events.history(conn, event.id)[-4:]:
            lines.append(f"STORED HISTORY at {row['changed_at']} — {row['field']}: "
                         f"{row['old_value']} -> {row['new_value']}")
    return lines


def _pending_candidates(conn: sqlite3.Connection | None,
                        group: list[Mention]) -> list[sqlite3.Row]:
    if conn is None:
        return []
    wanted = set().union(*(_tokens(str(m.row.get("title") or "")) for m in group))
    dates = {m.date for m in group if m.date}
    places = {db.slugify(str(m.row.get("location") or "")) for m in group
              if m.row.get("location")}
    out: list[sqlite3.Row] = []
    pending_rows = conn.execute(
        "SELECT * FROM pending_changes WHERE status = 'open' ORDER BY id DESC LIMIT 20")
    for row in pending_rows:
        overlap = wanted & _tokens(
            f"{row['subject_title'] or ''} {row['observation']} "
            f"{row['subject_location'] or ''}")
        same_date = bool(row["subject_date"] and row["subject_date"] in dates)
        same_place = bool(row["subject_location"] and
                          db.slugify(str(row["subject_location"])) in places)
        if not (overlap or same_date or same_place):
            continue
        out.append(row)
        if len(out) == 6:
            break
    return out


def _pending_lines(conn: sqlite3.Connection | None, group: list[Mention]) -> list[str]:
    """Undated observations nominated by shared event wording."""
    lines = []
    for row in _pending_candidates(conn, group):
        details = [str(row[name]) for name in
                   ("subject_title", "subject_date", "subject_time", "subject_location")
                   if row[name]]
        suffix = f" [{'; '.join(details)}]" if details else ""
        lines.append(f"PENDING {row['id']} at {row['observed_at'] or row['created_at']} "
                     f"from {row['entity'] or '?'} — {row['observation']}{suffix}")
    return lines


def _merge_suffix(conn: sqlite3.Connection | None, group: list[Mention]) -> str:
    lines = ["PROPOSALS"]
    for index, mention in enumerate(group, 1):
        lines.append(f"PROPOSAL {index}")
        lines.append(mention.describe())
        lines.extend("    " + line for line in _source_lines(conn, mention))
    pending = _pending_lines(conn, group)
    if pending:
        lines.append("\nUNDATED OBSERVATIONS — context only; decide which target they name")
        lines.extend(pending)
    lines.append("\nOne row, or same_event false to preserve distinct targets.")
    return "\n".join(lines)


def _answer_citations(conn: sqlite3.Connection | None, group: list[Mention], answer: dict,
                      merged: dict) -> dict[str, list[int]]:
    if not isinstance(answer.get("citations"), list):
        return _field_cites(group, merged)
    available: set[int] = set()
    for mention in group:
        for line in _source_lines(conn, mention):
            if line.startswith("SOURCE "):
                try:
                    available.add(int(line.split()[1]))
                except (ValueError, IndexError):
                    continue
    out: dict[str, list[int]] = {}
    for item in answer["citations"]:
        if not isinstance(item, dict):
            continue
        field = str(item.get("field") or "")
        if field not in merged:
            continue
        ids = [value for value in (item.get("source_ids") or [])
               if isinstance(value, int) and value in available]
        if ids:
            out[field] = list(dict.fromkeys(ids))
    return out


def _merged_links(conn: sqlite3.Connection | None, group: list[Mention],
                  answer: dict) -> list[dict]:
    out = _links(group)
    seen = {(item["kind"], item["key"]) for item in out}
    for item in answer.get("links") or ():
        if not isinstance(item, dict):
            continue
        kind, key = str(item.get("kind") or ""), str(item.get("key") or "")
        if (kind not in events.LINK_KINDS or not key or (kind, key) in seen
                or (conn is not None and events.get(conn, key) is None)):
            continue
        out.append({"kind": kind, "key": key})
        seen.add((kind, key))
    return out


def _apply_pending_targets(conn: sqlite3.Connection | None, group: list[Mention],
                           answer: dict, *, merged: dict | None = None) -> None:
    if conn is None:
        return
    allowed = {int(row["id"]) for row in _pending_candidates(conn, group)}
    for item in answer.get("pending_targets") or ():
        if not isinstance(item, dict) or not isinstance(item.get("pending_id"), int):
            continue
        pending_id, proposal = item["pending_id"], item.get("proposal")
        if pending_id not in allowed or not isinstance(proposal, int) \
                or not 1 <= proposal <= len(group):
            continue
        mention = group[proposal - 1]
        if not mention.date:
            continue
        if mention.existing and mention.row.get("key"):
            writable = next((m for m in group if not m.existing), None)
            if writable is not None:
                writable.diff.setdefault("_pending_targets", []).append({
                    "id": pending_id, "key": str(mention.row["key"])})
            continue
        target = merged if merged is not None else mention.row
        ids = target.setdefault("_pending_ids", [])
        if pending_id not in ids:
            ids.append(pending_id)


def _apply_observation_targets(group: list[Mention], answer: dict,
                               *, merged: dict | None = None) -> None:
    for item in answer.get("observation_targets") or ():
        if not isinstance(item, dict):
            continue
        observation, target = item.get("observation"), item.get("target")
        if not isinstance(observation, int) or not isinstance(target, int) \
                or not 1 <= observation <= len(group) or not 1 <= target <= len(group):
            continue
        source, destination = group[observation - 1], group[target - 1]
        if source.row.get("date") or source.row.get("status") != "declined" \
                or not destination.row.get("date"):
            continue
        if destination.row.get("key"):
            source.row["_target_key"] = destination.row["key"]
            continue
        claimed = source.row.get("field_cite_ids")
        cited = (claimed.get("status") or []) if isinstance(claimed, dict) \
            else (source.row.get("cite_ids") or [])
        stamps = [str(item["ts"]) for item in source.bundle.items
                  if item["id"] in cited and item["ts"]]
        if not stamps:
            continue
        row = merged if merged is not None else destination.row
        row.setdefault("_resolved_observations", []).append({
            "observation": source.row.get("title") or source.row.get("note"),
            "observed_at": max(stamps),
            "entity": source.bundle.entity,
        })
        if merged is None:
            source.diff["events"] = [item for item in source.diff.get("events") or []
                                     if item is not source.row]


def _mark_distinct_lifecycle(group: list[Mention]) -> None:
    dates = {m.date for m in group if m.date}
    statuses = {m.row.get("status") for m in group}
    if len(dates) < 2 or not {"declined", "confirmed"} <= statuses:
        return
    for mention in group:
        if not mention.existing and mention.row.get("status") == "declined" and mention.date:
            mention.row["_allow_declined_insert"] = True


def _stored_near(conn, mentions: list[Mention], *, include_claimed: bool = False) \
        -> list[Mention]:
    """Rows already in the store within reach of any proposal, as comparable mentions."""
    dates = sorted({m.date for m in mentions if m.date})
    if not dates and not include_claimed:
        return []
    try:
        if include_claimed:
            lo, hi = db.window_bounds(events.AMENDABLE_DAYS_BACK,
                                      events.AMENDABLE_DAYS_FORWARD)
        else:
            lo = (db.parse_date(dates[0]) - timedelta(days=NEAR_DAYS)).isoformat()
            hi = (db.parse_date(dates[-1]) + timedelta(days=NEAR_DAYS)).isoformat()
    except ValueError:
        return []
    # A row apply will reach on its own is apply's, under the evidence rules. Only the
    # ones nothing resolves to are worth a cluster.
    claimed: set[str] = set()
    if not include_claimed:
        claimed = {str(m.row.get("key")) for m in mentions if m.row.get("key")}
        for m in mentions:
            hit = events.find_match(
                conn, title=str(m.row.get("title") or ""), on=m.date,
                series=m.row.get("series"),
                participants=[p for p in (m.row.get("participants") or [])
                              if isinstance(p, str)],
                subject=str(m.row.get("subject") or "me"))
            if hit is not None:
                claimed.add(hit.key)
    out: list[Mention] = []
    for event in events.between(conn, lo, hi):
        if event.key in claimed:
            continue
        row = {"key": event.key, "date": event.date, "time": event.time,
               "title": event.title, "location": event.location, "kind": event.kind,
               "subject": event.subject, "status": event.status, "series": event.series,
               "note": event.note, "participants": list(event.participants or [])}
        evidence = conn.execute(
            """SELECT DISTINCT a.* FROM evidence e JOIN archive a ON a.id = e.archive_id
                WHERE e.kind = 'event' AND e.ref = ? ORDER BY a.ts DESC LIMIT 6""",
            (event.key,)).fetchall()
        out.append(Mention({k: v for k, v in row.items() if v not in (None, "")},
                           _StoredBundle(event.source or f"event:{event.key}", evidence),
                           {"events": []}, existing=True))
    return out


def merge_all(client: CompletionClient, cfg: Config, proposals: list,
              *, conn: sqlite3.Connection | None = None,
              run_id: int | None = None) -> tuple[list, list[str]]:
    """Collapse cross-bundle duplicate events and question actions.

    `proposals` is propose's own output, `[(bundle, diff, generation_id), ...]`, and the
    same shape comes back: the surviving row is left on one source bundle and the other
    copies are removed. A question conflict the model cannot settle defers every bundle
    involved, so a linked event or to-do cannot be applied without its question action.
    """
    mentions: list[Mention] = []
    observations: list[Mention] = []
    for bundle, diff, _gen in proposals:
        for row in (diff.get("events") or []):
            if not isinstance(row, dict):
                continue
            mention = Mention(row, bundle, diff)
            if row.get("date") and row.get("title"):
                mentions.append(mention)
            elif not row.get("date") and row.get("status") == "declined":
                observations.append(mention)

    links = events.linked_pairs(conn) if conn is not None else set()
    pending_open = bool(pending.open_items(conn)) if conn is not None else False
    stored = (_stored_near(conn, mentions,
                           include_claimed=bool(observations or pending_open))
              if conn is not None else [])
    log: list[str] = []
    pool = mentions + stored
    groups = cluster(pool, cfg, links) if pool else []
    groups = _with_observations(groups, observations)
    if conn is not None:
        contexts = [Mention({"title": row["subject_title"] or row["observation"]},
                            Bundle(entity=row["entity"] or "pending"), {})
                    for row in pending.open_items(conn)]
        groups = _with_observations(groups, contexts, include=False)
    for group in groups:
        has_pending = bool(_pending_candidates(conn, group))
        if (len(group) < 2 and not has_pending) or not any(not m.existing for m in group):
            continue
        sources = ", ".join(sorted({m.bundle.entity for m in group}))
        voices = corroboration(group)
        if not _conflicted(group) and not has_pending:
            merged = _merge_locally(group)
            merged["links"] = _links(group)
            merged["field_cite_ids"] = _field_cites(group, merged)
            merged["_evidence_times"] = _evidence_times(group, conn)
            _corroborate(merged, voices)
            _collapse(group, merged)
            log.append(f"merged {len(group)} mentions of {merged['title']!r} "
                       f"from {voices} source(s) ({sources})")
            continue

        suffix = _merge_suffix(conn, group)
        ceiling = _ceiling(cfg)
        try:
            reply = client.complete(
                model=cfg.match_model, prefix=INSTRUCTIONS, suffix=suffix,
                schema=SCHEMA, schema_name="memcal_merge", max_tokens=ceiling)
            if conn is not None:
                trace.record(conn, run_id=run_id, stage="merge",
                             label=sources[:120], reply=reply, max_tokens=ceiling,
                             home=cfg.home, prefix=INSTRUCTIONS, suffix=suffix)
            answer = reply.data if isinstance(reply.data, dict) else {}
        except Exception as exc:                       # a failed merge must not lose rows
            # And must not silently make one. These mentions *disagree* — that is why a
            # model was asked at all — so a timeout is a question with no answer, not a
            # yes. Merging on the failure path meant an unreachable provider produced
            # confident merges of rows nothing had ever compared, and the only trace was
            # a line in a log nobody reads. Two rows the user can see and correct is the
            # recoverable failure; one row built out of two plans is not.
            log.append(f"merge failed for {sources}: {exc} — kept {len(group)} rows "
                       f"separate, unresolved")
            continue

        if reply.truncated:
            log.append(f"kept {len(group)} rows separate ({sources}): reply cut off")
            continue
        if answer.get("same_event") is False:
            _apply_pending_targets(conn, group, answer)
            _apply_observation_targets(group, answer)
            _mark_distinct_lifecycle(group)
            log.append(f"kept {len(group)} separate rows ({sources}): not the same event")
            continue
        if str(answer.get("same_event") or "").lower() == "unresolved":
            log.append(f"kept {len(group)} rows separate ({sources}): model could not tell")
            continue
        if not answer.get("date") or not answer.get("title"):
            why = (f"reply cut off at the {ceiling}-token ceiling" if reply.truncated
                   else "model returned no date or title")
            log.append(f"kept {len(group)} rows separate ({sources}): {why}")
            continue
        if answer.get("same_event") is not True:
            log.append(f"kept {len(group)} rows separate ({sources}): no merge decision")
            continue

        merged = {k: v for k, v in answer.items()
                  if k not in ("same_event", "why", "citations", "pending_targets",
                               "observation_targets")
                  and v not in (None, "")}
        # The model answers with fields, not with provenance. The citations belong to
        # the fragments it was shown, so they are carried across here rather than lost
        # to a stage whose entire purpose is combining evidence.
        merged["cite_ids"] = _cites(group)
        merged["links"] = _merged_links(conn, group, answer)
        merged["field_cite_ids"] = _answer_citations(conn, group, answer, merged)
        merged["cite_ids"] = list(dict.fromkeys([
            *merged["cite_ids"],
            *(archive_id for ids in merged["field_cite_ids"].values() for archive_id in ids)]))
        merged["_evidence_times"] = _evidence_times(group, conn)
        _apply_pending_targets(conn, group, answer, merged=merged)
        _apply_observation_targets(group, answer, merged=merged)
        _corroborate(merged, voices)
        # The key travels with the merged row so apply amends the row that already
        # exists rather than minting a second one beside it.
        for mention in group:
            if mention.row.get("key"):
                merged.setdefault("key", mention.row["key"])
                break
        _collapse(group, merged)
        why = str(answer.get("why") or "").strip()
        log.append(f"merged {len(group)} mentions of {merged['title']!r} ({sources})"
                   + (f" — {why}" if why else ""))
    proposals, question_log = _merge_question_actions(
        client, cfg, proposals, conn=conn, run_id=run_id)
    log.extend(question_log)
    return proposals, log


QUESTION_CHOICE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["choice", "why"],
    "properties": {
        "choice": {"type": ["integer", "null"],
                   "description": "1-based action to keep; null when evidence cannot decide"},
        "why": {"type": "string",
                "description": "one factual sentence grounded in the quoted source lines"},
    },
}


def _question_signature(row: dict) -> tuple:
    return tuple(row.get(field) for field in
                 ("action", "key", "version", "text", "answer", "wake_condition"))


def _question_evidence(conn: sqlite3.Connection | None, row: dict) -> list[str]:
    if conn is None:
        return []
    ids = [int(value) for value in (row.get("cite_ids") or []) if value]
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    found = conn.execute(
        f"SELECT id, person, from_me, text FROM archive WHERE id IN ({marks}) ORDER BY ts, id",
        ids,
    ).fetchall()
    return [f"source {item['id']} — "
            f"{'me' if item['from_me'] else (item['person'] or 'they')}: {item['text']}"
            for item in found]


def _merge_question_actions(client: CompletionClient, cfg: Config, proposals: list,
                            *, conn: sqlite3.Connection | None,
                            run_id: int | None) -> tuple[list, list[str]]:
    """Bring same-key question actions together; evidence, never overlap, decides."""
    by_key: dict[str, list[tuple]] = {}
    for proposal in proposals:
        bundle, diff = proposal[0], proposal[1]
        for row in diff.get("questions") or []:
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "")
            if key and row.get("action") != "ask":
                by_key.setdefault(key, []).append((proposal, bundle, diff, row))

    log: list[str] = []
    blocked: set[int] = set()
    for key, group in by_key.items():
        if len(group) < 2:
            continue
        signatures = {_question_signature(row) for _p, _b, _d, row in group}
        if len(signatures) == 1:
            chosen = group[0][3]
            chosen["cite_ids"] = list(dict.fromkeys(
                archive_id for _p, _b, _d, row in group
                for archive_id in (row.get("cite_ids") or [])))
            for _proposal, _bundle, diff, row in group[1:]:
                diff["questions"].remove(row)
            log.append(f"merged {len(group)} matching question actions for {key}")
            continue

        lines = [f"QUESTION {key}",
                 "Choose the action best supported by the quoted source evidence. "
                 "Temporary delay is amend, a known answer is resolve, explicit refusal "
                 "is drop, and unrelated traffic is keep."]
        for index, (_proposal, bundle, _diff, row) in enumerate(group, 1):
            fields = {field: row.get(field) for field in
                      ("action", "version", "text", "answer", "wake_condition")}
            lines.append(f"ACTION {index} from {bundle.entity}: {json.dumps(fields)}")
            lines.extend("  " + evidence for evidence in _question_evidence(conn, row))
        suffix = "\n".join(lines)
        ceiling = _ceiling(cfg, QUESTION_MERGE_TOKENS)
        try:
            reply = client.complete(
                model=cfg.match_model,
                prefix="Merge conflicting actions for one existing open question.",
                suffix=suffix, schema=QUESTION_CHOICE_SCHEMA,
                schema_name="memcal_question_merge", max_tokens=ceiling)
            if conn is not None:
                trace.record(conn, run_id=run_id, stage="merge", label=key,
                             reply=reply, max_tokens=ceiling, home=cfg.home,
                             prefix="Merge conflicting question actions.", suffix=suffix)
            choice = (reply.data or {}).get("choice") if isinstance(reply.data, dict) else None
        except Exception as exc:
            choice = None
            log.append(f"question merge failed for {key}: {exc}")
        if not isinstance(choice, int) or not 1 <= choice <= len(group):
            blocked.update(id(proposal) for proposal, _b, _d, _r in group)
            log.append(f"deferred {len(group)} bundle(s): question conflict for {key}")
            continue
        selected = group[choice - 1][3]
        for _proposal, _bundle, diff, row in group:
            if row is not selected:
                diff["questions"].remove(row)
        why = str((reply.data or {}).get("why") or "").strip()
        log.append(f"merged question {key} as {selected.get('action')}"
                   + (f" — {why}" if why else ""))
    if blocked:
        proposals = [proposal for proposal in proposals if id(proposal) not in blocked]
    return proposals, log


# Compatibility for callers and tests that still use the old implementation name.
# Old callers can migrate without changing the stored behavior in one release.
resolve_all = merge_all


def _corroborate(row: dict, voices: int) -> None:
    """Record how many independent voices settled this row, on the row itself."""
    if voices < 2:
        return
    if not row.get("status") or row.get("status") == "mentioned":
        row["status"] = "tentative"
    # The docstring above is right and the code used to disagree with it: having argued
    # that corroboration belongs in `status` because that is what the brief and every
    # downstream filter read, it then *also* appended "2 sources mention this" to the
    # note. The count is memcal's own bookkeeping, and a note is the one field written
    # for the user to read — the user saw "Beer garden · Bohemian Hall, Astoria · 2 sources
    # mention this" on their calendar and called the line silly, which it is. The status
    # nudge carries the same fact where something can act on it.


def _collapse(group: list[Mention], merged: dict) -> None:
    """Put the merged row on the earliest-dated mention; drop the rest.

    Earliest rather than best-worded: the first conversation to mention a plan is the
    one whose bundle the reader will look in for it.
    """
    writable = [m for m in group if not m.existing]
    if not writable:
        return
    keeper = min(writable, key=lambda m: m.date)
    for mention in group:
        if mention.existing:
            continue
        rows = mention.diff.get("events") or []
        if mention is keeper:
            for index, row in enumerate(rows):
                if row is mention.row:
                    rows[index] = merged
                    break
        else:
            mention.diff["events"] = [r for r in rows if r is not mention.row]


def explain(proposals: list, cfg: Config | None = None) -> str:
    """What resolution would do, without spending anything. For tools and tests."""
    mentions = [Mention(row, bundle, diff)
                for bundle, diff, _gen in proposals
                for row in (diff.get("events") or [])
                if isinstance(row, dict) and row.get("date") and row.get("title")]
    lines = []
    for group in cluster(mentions, cfg):
        if len(group) < 2:
            continue
        lines.append(f"cluster ({'conflicted' if _conflicted(group) else 'agrees'}):")
        lines += [m.describe() for m in group]
    return "\n".join(lines) or "(no cross-bundle duplicates)"
