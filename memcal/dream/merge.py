"""Stage 3: merge proposals that several conversations made about the same key."""

from __future__ import annotations

import json
import sqlite3

from .. import db, trace
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

    __slots__ = ("row", "bundle", "diff")

    def __init__(self, row: dict, bundle: Bundle, diff: dict):
        self.row, self.bundle, self.diff = row, bundle, diff

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
        for field in ("time", "until", "location", "status", "kind"):
            if self.row.get(field):
                bits.append(f"{field} {self.row[field]}")
        if self.row.get("participants"):
            bits.append("with " + ", ".join(map(str, self.row["participants"])))
        if self.row.get("note"):
            bits.append(f"note {self.row['note']}")
        return f"  - {self.row.get('title') or '(untitled)'}  [{'; '.join(bits)}]"


def same_event(a: Mention, b: Mention, cfg: Config | None = None) -> bool:
    """Could these two be one event?"""
    if (a.row.get("subject") or "me") != (b.row.get("subject") or "me"):
        return False
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

    # Two of the same people, on the same evening, is stronger evidence than any
    # wording — and it is the only thing that survives the wording changing. The live
    # case that motivated this stage was "Bier gardens with Quinn and Jamie" against
    # "Beer garden at Bohemian Hall": not one word in common, the same two guests, the
    # same evening. Requiring a shared word first is what let it through as two rows.
    #
    # The date bound is not decoration. Unbounded, this rule read "the same two people,
    # within four days" — which is the ordinary shape of a friend group, not of one
    # event. A benchmark week where the same crew had ramen on Thursday, poker on
    # Friday and a beer garden on Saturday collapsed all three into a single cluster on
    # nothing but Alex and Cameron being in all of them. That cluster then disagrees about
    # both date and title, so it costs a model call every run, and when the call fails
    # `_merge_locally` keeps the earliest fragment and silently drops the other two.
    if len(shared_people) >= 2 and apart <= SAME_GUESTS_DAYS:
        return True
    if not shared_title:
        return False
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


def cluster(mentions: list[Mention], cfg: Config | None = None) -> list[list[Mention]]:
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
            if find(i) != find(j) and same_event(mentions[i], mentions[j], cfg):
                parent[find(i)] = find(j)

    groups: dict[int, list[Mention]] = {}
    for index, mention in enumerate(mentions):
        groups.setdefault(find(index), []).append(mention)
    return list(groups.values())


INSTRUCTIONS = """\
Several conversations described what may be the same event. Each was read on its own, so
each proposal only knows its own thread — one of them may have been guessing about a
detail another one states outright.

Decide what is actually true, and return one row.

WEIGH THE SOURCE, NOT THE COUNT
Three fragments repeating a guess do not outvote one that was there. Prefer:
  - a detail stated by the people arranging it over one mentioned in passing elsewhere
  - a specific date ("sunday after 6") over one derived from a vague phrase ("next
    weekend", "sometime soon"), whichever is more common
  - a correction over what it corrected — later beats earlier when they conflict
The bundle each fragment came from is given. A plan discussed in its own thread is
better evidence about that plan than a reference to it inside a thread about something
else entirely.

POOL WHAT DOES NOT CONFLICT
Guest lists especially: each thread knows the people in it, and none knows everyone.
Union the participants unless a fragment says someone is not coming. Take the most
specific title, location and time available from any fragment.

IF THEY ARE NOT THE SAME EVENT
Say so with same_event false, and every proposal is kept as its own row. Two poker
nights a week apart are two poker nights. Only merge what is genuinely one occasion."""

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
                 "kind", "status", "participants", "note", "why"],
    "properties": {
        "same_event": {"type": "boolean",
                       "description": "false if these are genuinely different occasions"},
        "date": {"type": "string", "description": "yyyy-mm-dd"},
        "until": {"type": ["string", "null"]},
        "time": {"type": ["string", "null"]},
        "title": {"type": "string"},
        "location": {"type": ["string", "null"]},
        "kind": {"type": ["string", "null"]},
        "status": {"type": ["string", "null"]},
        "participants": {"type": "array", "items": {"type": "string"}},
        "note": {"type": ["string", "null"]},
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
    """The no-model answer: keep the earliest-dated fragment's row, union the people.

    Used when the fragments do not actually disagree about anything that matters, and
    as the fallback when the call fails — a merged row built from what they agree on is
    strictly better than emitting all of them and calling it a day.
    """
    base = dict(min(group, key=lambda m: m.date).row)
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
    for bundle, diff, _gen in proposals:
        for row in (diff.get("events") or []):
            if isinstance(row, dict) and row.get("date") and row.get("title"):
                mentions.append(Mention(row, bundle, diff))

    log: list[str] = []
    for group in cluster(mentions, cfg) if len(mentions) >= 2 else []:
        if len(group) < 2:
            continue
        sources = ", ".join(sorted({m.bundle.entity for m in group}))
        voices = corroboration(group)
        if not _conflicted(group):
            merged = _merge_locally(group)
            _corroborate(merged, voices)
            _collapse(group, merged)
            log.append(f"merged {len(group)} mentions of {merged['title']!r} "
                       f"from {voices} source(s) ({sources})")
            continue

        suffix = ("PROPOSALS\n" + "\n".join(m.describe() for m in group)
                  + "\n\nOne row, or same_event false.")
        try:
            reply = client.complete(
                model=cfg.propose_model, prefix=INSTRUCTIONS, suffix=suffix,
                schema=SCHEMA, schema_name="memcal_merge", max_tokens=1200)
            if conn is not None:
                trace.record(conn, run_id=run_id, stage="merge",
                             label=sources[:120], reply=reply, max_tokens=1200,
                             home=cfg.home, prefix=INSTRUCTIONS, suffix=suffix)
            answer = reply.data if isinstance(reply.data, dict) else {}
        except Exception as exc:                       # a failed merge must not lose rows
            log.append(f"merge failed for {sources}: {exc} — merged on agreement only")
            merged = _merge_locally(group)
            _corroborate(merged, voices)
            _collapse(group, merged)
            continue

        if answer.get("same_event") is False:
            log.append(f"kept {len(group)} separate rows ({sources}): not the same event")
            continue
        if not answer.get("date") or not answer.get("title"):
            _collapse(group, _merge_locally(group))
            continue

        merged = {k: v for k, v in answer.items()
                  if k not in ("same_event", "why") and v not in (None, "")}
        # The model answers with fields, not with provenance. The citations belong to
        # the fragments it was shown, so they are carried across here rather than lost
        # to a stage whose entire purpose is combining evidence.
        merged["cite_ids"] = _cites(group)
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
        try:
            reply = client.complete(
                model=cfg.propose_model,
                prefix="Merge conflicting actions for one existing open question.",
                suffix=suffix, schema=QUESTION_CHOICE_SCHEMA,
                schema_name="memcal_question_merge", max_tokens=800)
            if conn is not None:
                trace.record(conn, run_id=run_id, stage="merge", label=key,
                             reply=reply, max_tokens=800, home=cfg.home,
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
    keeper = min(group, key=lambda m: m.date)
    for mention in group:
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
