"""Stage 4 — apply (code).

Keyed diffs merge deterministically. Two bundles proposing the same memcal row
collide on the key and merge; two bundles touching one wiki page apply per-slot.
No model is involved here.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from datetime import timedelta

from .. import dates, db, events, identity, questions, series as series_mod, todos, trace, wiki
from ..config import Config
from .bundle import Bundle

# How far past its own source traffic a row is allowed to land. A year-old fraternity
# email saying "poker this Saturday" means a Saturday a year ago; nothing in it can
# schedule anything for this week. This is the structural half of the poker-address
# case — the model is not the thing standing between them and that answer.
MAX_LOOKAHEAD_DAYS = 45

# And how far *before* its own source traffic. The mirror of the rule above, and it was
# missing: `_horizon` returned an upper bound only, so a bundle whose newest line is
# today could still write a row into last year and nothing objected.
#
# Which is not hypothetical. The first live run of tools/benchmark_temporal.py put three
# rows — poker, an AWS event and a Smash Bros night — on 2025-08-07, 2025-08-04 and
# 2025-08-03, from traffic timestamped 2026-08-03, with `TODAY IS Monday 2026-08-03` in
# the prefix and `-- Mon 2026-08-03 --` above every line. The model simply wrote the
# previous year. A row a year in the past is invisible: it renders nowhere near the
# brief's window, so the failure looks like nothing was captured at all.
#
# Wider than the forward bound because looking back is legitimate — `observed` rows and
# a late-arriving "how was dinner" both point backwards — but a bundle cannot report
# something from before its own oldest line by any margin that matters.
MAX_LOOKBACK_DAYS = 120


# ------------------------------------------------------------------ evidence --
# Which lines a row was built from. The model cites them when it can (`cites` → an
# archive id per `L` tag), and when it cannot, this is the difference between narrow
# evidence and none at all — the alternative, attaching the whole conversation, produced
# a question about Spider-Man carrying 1,725 lines of Taco Bell orders as its receipt.

#: Words too common to prove two sentences are about the same thing.
_EVIDENCE_STOP = frozenset("""
    about after again all also and another any are because been before being both but
    can come could did does doing done down each even ever every from get gets going
    gonna good got great have having here how into its just know like little look made
    make many maybe more most much must need next now off only other our out over own
    really right said same see should since some still such take than that the their
    them then there these they thing think this those through time today too under
    until very want was way well went were what when where which while who will with
    would yeah yes you your
""".split())

#: Most lines to attach when nothing was cited. Enough to read the exchange, few enough
#: that opening it is still an answer rather than a scroll.
MAX_DERIVED_EVIDENCE = 12

#: Lines kept per claim. One is the proof; the second is usually the reply that settled
#: it, and a plan is rarely made in a single message.
PER_CLAIM = 2

#: Capitalised words that name nothing — the calendar vocabulary every question is full
#: of, plus the words a question opens with. Left in, "Sunday" and "Aug" would count as
#: things a conversation must mention, and "When" would match almost any conversation
#: there is, which is how the check quietly passed everything.
_NOT_A_PROPER_NOUN = frozenset(
    [*dates.WEEKDAYS, *dates.MONTHS,
     "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
     "today", "tonight", "tomorrow", "yesterday", "morning", "afternoon", "evening",
     "night", "weekend", "week", "month",
     "when", "what", "where", "which", "who", "whose", "why", "how", "will", "would",
     "are", "did", "does", "should", "could", "can", "has", "have", "any", "and",
     "the", "there", "this", "that", "your", "you"])

#: The context `_standalone_question` prefixes onto a question so it survives its
#: conversation disappearing: "Quinn asked: ", "In Doggo Park 142: ", "From Mom's
#: messages: ". Every rule below is about the question itself, so it comes off first —
#: a question stored with a prefix and one written a second ago must be judged the same,
#: or a rule silently stops applying to everything already in the store.
_ATTRIBUTION_RE = re.compile(
    r"^(?:in\s|from\s)?[^:]{1,60}?(?:\sasked|'s\smessages)?:\s+", re.IGNORECASE)


def _asked_itself(text: str) -> str:
    """A question without the speaker context code put in front of it."""
    body = " ".join((text or "").split()).strip()
    stripped = _ATTRIBUTION_RE.sub("", body, count=1).strip()
    return stripped or body


def _content_words(text: str) -> set[str]:
    """The words in a sentence that could identify what it is about.

    A hyphenated name is indexed both ways — "Spider-Man" yields `spiderman` and
    `spider` — because nobody types a name the same way twice, and the row and the line
    that produced it are exactly the two places that disagree. Matching them literally,
    "Spider-Man" found nothing in a thread that said "Spiderman" and "Spider man", so the
    question kept 1,593 lines of Taco Bell instead of the two messages it came from.
    """
    out: set[str] = set()
    for raw in re.findall(r"[a-z0-9'’-]+", (text or "").lower()):
        for candidate in (re.sub(r"[^a-z0-9]+", "", raw),
                          *re.split(r"[^a-z0-9]+", raw)):
            if len(candidate) >= 4 and candidate not in _EVIDENCE_STOP:
                out.add(candidate)
    return out


def _best_lines(bundle: Bundle, claim: str) -> list[tuple[str, int]]:
    """`(ts, archive id)` for the lines in this bundle that mention `claim`, best first.

    Deterministic and deliberately dumb: shared distinctive words. It is not trying to
    be right about causation, only to be *about the same subject* — which is the whole
    of what someone clicking "why is this here" is asking, and infinitely more than a
    thousand-line thread attached wholesale.
    """
    wanted = _content_words(claim)
    if not wanted:
        return []
    scored = []
    for row in bundle.items:
        if "id" not in row.keys() or not row["id"]:
            continue
        # The speaker counts as part of the line. Most guests are never named in any
        # message — they are on the row because they turned up in the conversation and
        # said they were coming, and "Quinn Brooks: I'm in" names Quinn Brooks.
        speaker = row["person"] if "person" in row.keys() else ""
        overlap = len(wanted & _content_words(f"{row['text']} {speaker or ''}"))
        if overlap:
            scored.append((-overlap, str(row["ts"]), int(row["id"])))
    scored.sort()
    return [(ts, archive_id) for _score, ts, archive_id in scored]


def _supporting_lines(bundle: Bundle, claims,
                      limit: int = MAX_DERIVED_EVIDENCE) -> list[int]:
    """Archive ids covering everything this row asserts, in the order it was said.

    A row claims several things at once — an occasion, a place, and each person on it —
    and matching the title alone left a guest with no line naming them anywhere in their
    own row's evidence, which is exactly the shape of a fabricated attendee. So every
    claim gets its own best line first, and only then does the leftover budget go to
    second-best ones: a row with five guests still proves all five before it spends
    anything on repeating itself about the title.
    """
    if isinstance(claims, str):
        claims = [claims]
    ranked = [_best_lines(bundle, claim) for claim in claims if claim]
    picked: dict[int, str] = {}
    for depth in range(PER_CLAIM):
        for lines in ranked:
            if len(picked) >= limit:
                break
            if depth < len(lines):
                ts, archive_id = lines[depth]
                picked.setdefault(archive_id, ts)
    return [archive_id for archive_id, _ts
            in sorted(picked.items(), key=lambda pair: (pair[1], pair[0]))]


def _proper_nouns(text: str) -> set[str]:
    """Names a sentence claims, minus the ones every sentence has.

    The first word is skipped: every sentence capitalises it, and "When" is not a thing
    a conversation has to have mentioned.

    A possessive is the same name. "Devon's" squashes to `devons`, which is not a
    substring of `devonpark`, so "Where is Devon's housewarming?" asked against a bundle
    labelled *Devon Park* claimed a name nothing mentioned and was thrown away as
    invented. This rule only ever refuses to write something, so it has to fail
    permissively — see `_squash`, which is loose in the same direction and for the same
    reason.
    """
    words = re.findall(r"\b[A-Z][\w'-]{2,}\b", (text or "").strip())
    return {re.sub(r"['’]s$", "", word.lower()) for word in words[1:]
            if word.lower() not in _NOT_A_PROPER_NOUN}


def _talks_about_nothing_here(bundle: Bundle, text: str) -> bool:
    """Does this question name things, none of which the conversation ever mentions?"""
    named = _proper_nouns(_asked_itself(text))
    if not named:
        return False              # nothing claimed, nothing to check
    haystack = _squash(" ".join([*(str(row["text"] or "") for row in bundle.items),
                                 *(str(row["person"] or "") for row in bundle.items),
                                 bundle.label or "", *(bundle.people or [])]))
    return not any(_squash(name) in haystack for name in named)


def _squash(text: str) -> str:
    """Letters and digits only, for matching a name against how someone typed it.

    Deliberately loose in the permissive direction. This rule only ever refuses to write
    something, so a false match costs a question that should have been asked anyway,
    while a false miss deletes a real one. It nearly did: "Spider-Man" against a thread
    that said "Spiderman" and "Spider man" matched neither under a word-boundary regex,
    and the question — which was real, and came from "Do we have ticket for Spiderman?"
    — would have been thrown away as invented.
    """
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


# ----------------------------------------------------- a question with a date --

#: "Will you attend …", "are you going to …" — a question about turning up somewhere.
_ATTENDANCE_RE = re.compile(
    r"^(?:will|would|are|do|did|is)\s+you\w*\s*"
    r"(?:be\s+)?(?:want\s+to\s+|planning\s+to\s+|going\s+to\s+)?"
    r"(?:attend(?:ing)?|go(?:ing)?|come|coming|be\s+at|make\s+it\s+to|join)\b"
    r"(?:\s+to|\s+at|\s+for)?\s+",
    re.IGNORECASE)

#: Where a title stops being the name of a thing and starts being a description of it.
_TITLE_TAIL_RE = re.compile(r"\s+(?:about|regarding|concerning|discussing)\s+",
                            re.IGNORECASE)
MAX_DERIVED_TITLE = 60


def _dated_occasion(text: str, bundle: Bundle) -> dict | None:
    """A question that is really a calendar row, turned into one."""
    body = _asked_itself(text).rstrip("?").strip()
    match = _ATTENDANCE_RE.match(body)
    if not match:
        return None
    said_on = max((db.parse_ts(str(row["ts"])) for row in bundle.items
                   if row["ts"]), default=None)
    when = next((resolved for phrase in dates.claims(body)
                 if (resolved := dates.resolve(phrase, said_on))), None)
    if not when:
        return None                      # no day named: it is a real question after all
    # Everything after the attendance opener, minus the date clause it ends on — the
    # date is a field now, and a title carrying one stops matching the moment it moves.
    rest = body[match.end():].strip()
    found = dates.PHRASE_RE.search(rest)
    if found:
        rest = re.sub(r"[\s,]*\b(?:on|at|in|this|next|for)\s*$", "", rest[:found.start()],
                      flags=re.IGNORECASE)
    rest = re.sub(r"^(?:the|a|an)\s+", "", rest, flags=re.IGNORECASE).strip(" ,.")
    if not rest:
        return None
    # "…meeting about the dog-run issues" is a name and then a description of it. The
    # name is the title, because that is what a later mention is matched against.
    title, detail = rest, ""
    parts = _TITLE_TAIL_RE.split(rest, maxsplit=1)
    if len(parts) == 2 and len(rest) > MAX_DERIVED_TITLE and parts[0].strip():
        title, detail = parts[0].strip(), parts[1].strip()
    return {"title": title[:MAX_DERIVED_TITLE].strip(" ,."),
            "date": when,
            "kind": "opportunity",
            "status": "mentioned",
            "note": detail or None,
            # Whether the day was *named* rather than worked out. "September 25, 2026" is
            # absolute; "Saturday" is relative to whoever said it. `_occasion_horizon`
            # needs the difference — see there for what it costs to get it wrong.
            "absolute": bool(dates.MONTH_OR_ISO_RE.search(body))}


def _occasion_horizon(bundle: Bundle, occasion: dict) -> tuple:
    """The date bounds for a row converted out of a question."""
    earliest, latest = _horizon(bundle)
    if not occasion.get("absolute"):
        return earliest, latest
    try:
        named = db.parse_date(occasion["date"])
    except (ValueError, TypeError, KeyError):
        return earliest, latest
    return min(earliest, named), max(latest, named)


def apply_diffs(conn: sqlite3.Connection, cfg: Config, proposals,
                *, written_by: str, run_id: int | None = None,
                stage: str = "propose") -> tuple[Counter, list[str]]:
    """Commit typed rows, their audit trail, and staged wiki pages as one unit."""
    # Archive and call records may have been written immediately before application.
    # They are inputs to this unit, not part of its typed-row/audit outcome.
    if conn.in_transaction:
        conn.commit()
    wiki.recover(conn, cfg.wiki_dir)
    conn.execute("BEGIN")
    try:
        result = _apply_diffs(conn, cfg, proposals, written_by=written_by,
                              run_id=run_id, stage=stage)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    wiki.recover(conn, cfg.wiki_dir)
    return result


def _apply_diffs(conn: sqlite3.Connection, cfg: Config, proposals,
                 *, written_by: str, run_id: int | None = None,
                 stage: str = "propose") -> tuple[Counter, list[str]]:
    """Merge every bundle's diff. Keyed, so collisions merge instead of duplicating.

    A proposal is `(bundle, diff)` or `(bundle, diff, generation_id)`. With the id, every
    row written here also gets a provenance line pointing at the exact model call, which
    is what turns "where did this question come from?" into a lookup.
    """
    counts, log = Counter(), []
    # Two bundles can propose the same slot in one run — the model sees the same fact
    # from two threads. The key collision is what makes that a no-op rather than a
    # double write, so it is tracked here rather than left to the wiki layer.
    seen_slots: set[tuple[str, str]] = set()
    for proposal in proposals:
        bundle, diff = proposal[0], proposal[1]
        generation_id = proposal[2] if len(proposal) > 2 else None
        source = bundle.entity
        archive_ids = [int(row["id"]) for row in bundle.items
                       if "id" in row.keys() and row["id"]]
        horizon = _horizon(bundle)
        newest = _newest(bundle)

        def note(kind: str, outcome, bucket: str, cites=None, about=(), generation=None) -> None:
            """One outcome: count it, log it, and record which call produced it.

            `cites` is the archive ids the model pointed at, already resolved from this
            bundle's `L` tags by `propose._resolve_cites`. When it names any, the
            evidence is those rows. When it names none, the lines that *talk about* this
            row are found here instead, and only a row nothing in the conversation
            mentions falls all the way back to the whole bundle. Narrowing is the entire
            point — bundle-wide evidence answered "somewhere in this conversation",
            which for a 1,599-line thread is not an answer.
            """
            if not outcome:
                return
            verb, label = outcome[0], outcome[1]
            counts[f"{bucket}{verb}"] += 1
            log.append(f"{verb:9} {label}")
            ref = outcome[2] if len(outcome) > 2 else ""
            if ref and "rejected" not in verb:
                cited = [i for i in (cites or ()) if isinstance(i, int)]
                derived = _supporting_lines(bundle, about) if (about and not cited) else []
                trace.stamp(conn, kind=kind, ref=ref, verb=verb, entity=source,
                            stage=stage, run_id=run_id,
                            generation_id=(generation or generation_id),
                            archive_ids=(cited or derived or archive_ids), strict=True)

        # Before the events, deliberately. A cadence change and the first occurrence
        # under it arrive in the same diff out of the same email, and `_inherit_from_
        # series` reads the rule — so the rule has to exist by the time the occurrence
        # is written, or the row it was supposed to furnish arrives blank and the fix
        # inheritance works only from the *second* week onward.
        for row in diff.get("series") or []:
            note("series", _apply_series(conn, cfg, row, source=source,
                                         written_by=written_by, evidence_ts=newest,
                                         commit=False),
                 "series:", cites=row.get("cite_ids") if isinstance(row, dict) else None,
                 about=_claims(row, "title"),
                 generation=row.get("_generation_id") if isinstance(row, dict) else None)
        for row in diff.get("events") or []:
            cited = row.get("cite_ids") if isinstance(row, dict) else None
            note("event", _apply_event(conn, row, source=source, written_by=written_by,
                                       horizon=_horizon(bundle, cited),
                                       evidence_ts=newest,
                                       join_url=_join_link(bundle, cited),
                                       named_only_by_thread=_named_only_by_thread(bundle, row),
                                       commit=False),
                 "event:", cites=cited, about=_claims(row, "title"),
                 generation=row.get("_generation_id") if isinstance(row, dict) else None)
        for row in diff.get("todos") or []:
            note("todo", _apply_todo(conn, row, source=source, written_by=written_by,
                                     auto_remind=cfg.remind_deadlines, commit=False),
                 "todo:", cites=row.get("cite_ids") if isinstance(row, dict) else None,
                 about=_claims(row, "text"),
                 generation=row.get("_generation_id") if isinstance(row, dict) else None)
        for row in diff.get("wiki") or []:
            # A wiki row is three independent claims, so it can produce three outcomes.
            for outcome in _apply_wiki(conn, cfg, row, source=source, seen=seen_slots,
                                       commit=False):
                note("wiki", outcome, "wiki:",
                     generation=(row.get("_generation_id")
                                 if isinstance(row, dict) else None))
        for row in diff.get("standing") or []:
            note("standing", _apply_standing(conn, row, written_by=written_by,
                                             commit=False), "standing:",
                 generation=row.get("_generation_id") if isinstance(row, dict) else None)
        for question in diff.get("questions") or []:
            # Legacy deterministic fixtures still use strings. Model contracts use the
            # typed shape below; fixture compatibility does not restore an inference path.
            if isinstance(question, str):
                action, question_text, cited = "ask", question.strip(), None
            elif isinstance(question, dict):
                action = str(question.get("action") or "").strip().lower()
                question_text = str(question.get("text") or "").strip()
                cited = question.get("cite_ids")
            else:
                continue
            if action in {"amend", "resolve", "drop"} and not cited:
                counts["question:rejected-uncited"] += 1
                log.append(f"{'rejected':9} uncited question {action}: "
                           f"{question.get('key') or question_text}")
                continue
            if action in {"keep", "amend", "resolve", "drop"}:
                outcome = questions.apply_action(
                    conn, question, written_by=written_by, commit=False)
                if not outcome:
                    counts["question:rejected-invalid"] += 1
                    continue
                verb, label, ref = outcome
                counts[f"question:{verb}"] += 1
                log.append(f"{verb:9} {label}")
                trace.stamp(
                    conn, kind="question", ref=ref, verb=verb, entity=source,
                    stage=stage, run_id=run_id,
                    generation_id=(question.get("_generation_id") or generation_id),
                    archive_ids=(cited or []), strict=True)
                continue
            if action != "ask" or not question_text:
                counts["question:rejected-invalid"] += 1
                continue
            if isinstance(question, dict) and not cited:
                counts["question:rejected-uncited"] += 1
                log.append(f"{'rejected':9} uncited new question: {question_text}")
                continue
            # A question that names a thing and the day it happens is a calendar row
            # wearing a question mark. Written as a row it answers date lookups, ages
            # out on its own, and stops needing them to answer it for the store to know
            # what it was already told.
            occasion = _dated_occasion(question_text, bundle)
            if occasion:
                note("event",
                     _apply_event(conn, occasion, source=source, written_by=written_by,
                                  horizon=_occasion_horizon(bundle, occasion),
                                  evidence_ts=newest, commit=False),
                     "event:", about=_claims(occasion, "title"))
                continue
            if _talks_about_nothing_here(bundle, question_text):
                counts["question:rejected-unsupported"] += 1
                log.append(f"{'rejected':9} nothing in {bundle.label} mentions "
                           f"that: {question_text}")
                continue
            text = _standalone_question(bundle, question_text)
            # The bundle's newest line, not now: "on Sunday" means the Sunday near
            # whoever said it, and a nightly pass reads traffic that is already a day
            # or more old. `_dated_occasion` above anchors on the same moment.
            key = todos.ask(conn, text, said_on=db.parse_ts(newest) if newest else None,
                            written_by=written_by, commit=False)
            counts["question"] += 1
            log.append(f"{'ask':9} {text}")
            trace.stamp(conn, kind="question", ref=key, verb="asked", entity=source,
                        stage=stage, run_id=run_id, generation_id=generation_id,
                        archive_ids=(cited or _supporting_lines(bundle, question_text)
                                     or archive_ids),
                        strict=True)
    return counts, log


def _claims(row, field: str) -> list[str]:
    """Everything a row asserts, each as its own claim to find a line for.

    Kept apart rather than joined into one string: "Poker at Jose's with Quinn Brooks"
    scored as one claim ranks the line that says "poker" above the line that says
    "Quinn is in", and the second is the only evidence that guest exists.
    """
    if not isinstance(row, dict):
        return []
    out = [str(row.get(field) or "")]
    out += [str(p) for p in (row.get("participants") or []) if isinstance(p, str)]
    out += [str(row.get(name) or "") for name in ("location", "note", "wake_condition")]
    # The day is a claim like any other, and the line that settled it — "poker
    # thursday" — is the single most useful thing to have kept when someone later asks
    # where a date came from. `bench_audit --audit dates` re-derives the date from
    # exactly these lines, so leaving them out makes a correct row unverifiable.
    out.append(_day_words(row.get("date")))
    return [claim for claim in out if claim.strip()]


def _day_words(value) -> str:
    """"2026-08-07" → "thursday august", the words a message would have used."""
    try:
        when = db.parse_date(str(value))
    except (ValueError, TypeError):
        return ""
    return f"{dates.WEEKDAYS[when.weekday()]} {dates.MONTHS[when.month - 1]}"


def _horizon(bundle: Bundle, cites=None) -> tuple:
    """The window this row's own evidence can plausibly reach, (earliest, latest).

    Anchored on the lines the row actually cites when it names any, and on the whole
    bundle otherwise. The difference is the point of citing at all: a month of a
    conversation spans weeks, so a bundle-wide window admits almost any date, while the
    two messages that settled a plan pin it to a couple of days either side. A "Saturday"
    said in one of those two messages belongs to *that* week, and this is what makes a
    date derived from the wrong week detectable instead of merely wrong.
    """
    rows = bundle.items
    if cites:
        wanted = {int(i) for i in cites if isinstance(i, int)}
        cited = [r for r in bundle.items
                 if "id" in r.keys() and int(r["id"]) in wanted]
        rows = cited or bundle.items
    if not rows:
        today = db.today()
        return (today - timedelta(days=MAX_LOOKBACK_DAYS),
                today + timedelta(days=MAX_LOOKAHEAD_DAYS))
    stamps = [db.parse_ts(row["ts"]).date() for row in rows]
    return (min(stamps) - timedelta(days=MAX_LOOKBACK_DAYS),
            max(stamps) + timedelta(days=MAX_LOOKAHEAD_DAYS))


def _newest(bundle: Bundle) -> str | None:
    """When the last thing in this bundle was said — the age of its evidence.

    `events.upsert` weighs it against the row's last write, which is how a nightly pass
    is allowed to revise what the agent settled yesterday without being allowed to
    re-litigate it from traffic older than the decision.
    """
    stamps = [str(row["ts"]) for row in bundle.items if row["ts"]]
    return max(stamps) if stamps else None


def _clean(value):
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


#: An identifier that is not a name: a phone number, a `stream:id` token, an address.
#: Anything matching is unusable in text a person reads.
_NOT_A_NAME = re.compile(r"^\+?\d[\d\s().-]*$|^[a-z]+:[\w.-]+$|@|^\d+$", re.IGNORECASE)


def _readable(candidate: str) -> str:
    """A speaker's name, or "" when what we have is an identifier rather than a name.

    Falling back from `person` to `handle` put raw identifiers into text the user reads:
    GroupMe handles are literally `groupme:128934125` and iMessage handles are phone
    numbers, so the brief carried "From groupme:128934125's messages: What date is the
    PSK board-game gathering" and "+137933361279215 asked: …".

    An unattributed question is worse than an unattributed one is bad — but a question
    attributed to a phone number is *also* unattributed, and it reads as a system
    leaking its internals. Better to say nothing than to say `groupme:128934125`.
    """
    name = " ".join((candidate or "").split()).strip()
    if not name or _NOT_A_NAME.match(name):
        return ""
    return name


def _standalone_question(bundle: Bundle, proposed: str) -> str:
    """Make a question understandable after its conversation has disappeared.

    The model once copied a mother's exact line, "When am I coming over again?", into
    the brief. It was a fair question and a useless memory: neither the user nor the
    agent could tell who "I" was. The bundle already knows the speaker, so attach that
    context in code instead of asking every model to remember a prose convention.
    """
    text = " ".join((proposed or "").split()).strip()
    if not text:
        return text
    candidates = []
    for row in bundle.items:
        if row["from_me"]:
            continue
        body = " ".join(str(row["text"] or "").split()).strip()
        if not body:
            continue
        # Shared words are what make this line the one the question came out of; the
        # question mark only breaks ties between lines that already qualify. Scored the
        # other way round, the highest-scoring line in a bundle with no relevant line at
        # all is whoever last typed "?" — which is how "Morgan asked:" came to be
        # attached to a question about a film they never mentioned. An attribution is a
        # claim about who said something, and inventing one is worse than omitting it.
        overlap = len(set(re.findall(r"[a-z0-9']{3,}", text.lower()))
                      & set(re.findall(r"[a-z0-9']{3,}", body.lower())))
        if not overlap:
            continue
        candidates.append((overlap + (2 if "?" in body else 0), row))
    row = max(candidates, key=lambda pair: pair[0])[1] if candidates else None
    speaker = _readable(str(row["person"] or "") if row is not None else "")
    if not speaker and row is not None:
        speaker = _readable(str(row["handle"] or ""))
    if not speaker and bundle.entity.startswith("person:"):
        speaker = _readable(bundle.entity.split(":", 1)[1])
    # Already descriptive: do not turn "Should I ask Mom when she's coming?" into
    # "Mom asked: Should I ask Mom…".
    speaker_named = any(
        len(part) >= 3 and re.search(rf"\b{re.escape(part)}\b", text, re.IGNORECASE)
        for part in re.findall(r"[\w'-]+", speaker))
    if speaker and not speaker_named:
        # Preserve the distinction between a literal question and a question memcal
        # inferred it should ask. "Rowan is back — did you return the pass?" came from
        # their arrival message; claiming "Rowan asked" would fabricate speech.
        if row is not None and "?" in str(row["text"] or ""):
            return f"{speaker} asked: {text}"
        return f"From {speaker}'s messages: {text}"
    label = _readable(bundle.label)
    if not speaker and label and label.casefold() not in text.casefold():
        return f"In {label}: {text}"
    return text


def resolve_subject(conn: sqlite3.Connection, proposed: str | None,
                    participants: list[str]) -> str:
    """Resolve a row's subject from bundle context."""
    name = _clean(proposed)
    if not name or identity.is_me(conn, name):
        return "me"
    if identity.resolve(conn, name):
        return name
    if any(name.casefold() == p.casefold() for p in participants):
        return name
    known = {row["person"].casefold() for row in conn.execute(
        "SELECT DISTINCT person FROM handles WHERE person IS NOT NULL")}
    known |= {row["subject"].casefold() for row in conn.execute(
        "SELECT DISTINCT subject FROM events WHERE subject IS NOT NULL")}
    known.discard("me")
    return name if name.casefold() in known else "me"


def _named_only_by_thread(bundle, row) -> bool:
    """Is a group chat's *name* the only place this event's title appears?"""
    if not bundle or not isinstance(row, dict):
        return False
    # Only a conversation's *name*. A `person:` bundle is labelled with a name identity
    # resolution worked out, and "Avery visits" out of a bundle about Avery is an
    # ordinary row, not a title talking to itself.
    if not str(bundle.entity or "").startswith("thread:"):
        return False
    # Not `_proper_nouns`: that skips the first word, because every *sentence*
    # capitalises its first word and "When" is not a claim. A title is not a sentence
    # and its first word is usually the whole name — "Chili's", "Elements", "Poker".
    words = [_squash(word) for word in re.findall(r"[\w'-]{3,}", str(row.get("title") or ""))
             if word.lower() not in _TITLE_NOISE]
    words = [word for word in words if len(word) >= 3]
    if not words:
        return False
    spoken = _squash(" ".join(str(item["text"] or "") for item in bundle.items))
    if any(word in spoken for word in words):
        return False              # somebody actually said it; this is ordinary evidence
    # *Every* word, not any. One shared word is not "named only by the thread" — it is
    # a coincidence between an occasion noun and a chat title, which is the same
    # mistake `todos.GENERIC` was extended to stop. A WhatsApp group called "dinner
    # thu" held a confirmed ramen plan with a time and three guests, and this
    # downgraded it to an opportunity on the word `dinner` while `ramen` — the part
    # that says what the row actually is — appears nowhere in the chat's name.
    #
    # The case this exists for is unaffected: a chat called "We are going to chilis"
    # yielding a row titled "Chili's" has every word of the title in the label, and is
    # still weakened to something the user has not committed to.
    return all(word in _squash(bundle.label or "") for word in words)


def _join_link(bundle, cites=None) -> str | None:
    """The conferencing URL in this bundle's own lines, found in code."""
    from ..sources.ical import join_link
    if not bundle:
        return None
    wanted = {int(i) for i in (cites or ()) if isinstance(i, int)}
    ordered = [item for item in bundle.items if item["id"] in wanted] if wanted else []
    ordered += [item for item in bundle.items if item["id"] not in wanted]
    return join_link(*[str(item["text"] or "") for item in ordered])


def _lift_join_link(location: str | None) -> tuple[str, str | None] | None:
    """`(url, what is left of the location)`, or None if there is no link in it."""
    from ..sources.ical import join_link
    url = join_link(location)
    if not url:
        return None
    rest = " ".join(str(location).replace(url, "").strip(" ;,·|-").split()) or None
    return url, rest


#: Words a title can be made of that say nothing about what it is.
_TITLE_NOISE = frozenset({
    "the", "and", "for", "with", "his", "her", "our", "their", "this", "that",
    "night", "day", "plan", "plans", "event", "meet", "meeting", "hang",
})


def _apply_event(conn: sqlite3.Connection, row: dict, *, source: str, written_by: str,
                 horizon=None, evidence_ts: str | None = None,
                 join_url: str | None = None,
                 named_only_by_thread: bool = False, commit: bool = True):
    if not isinstance(row, dict):
        return None
    title = _clean(row.get("title"))
    date_value = _clean(row.get("date"))
    if not title or not date_value:
        return None
    try:
        on = db.parse_date(date_value)
    except ValueError:
        return None
    if horizon:
        earliest, latest = horizon
        if on > latest:
            return ("rejected-stale",
                    f"rejected {date_value} {title} (source traffic ends {latest})")
        if on < earliest:
            return ("rejected-stale",
                    f"rejected {date_value} {title} (source traffic starts {earliest})")
    participants = [p for p in (row.get("participants") or []) if isinstance(p, str)]
    participants = [p for p in participants if not identity.is_me(conn, p)]
    subject = resolve_subject(conn, row.get("subject"), participants)

    # A settled transfer is searchable history, not something happening in the user's
    # life. It can support or close a payment to-do, but rendering "Paid Quinn $10"
    # beside dinner and poker makes the brief an activity ledger. Only reject completed
    # observations; an upcoming bill or an unresolved obligation still has work left.
    if _is_settled_transaction(title, row):
        return ("rejected-transaction", f"archive-only settled transaction: {title}")

    # An inverted span is dropped by `events.upsert`, which is where every writer
    # passes — this used to be the only place that checked, and the typed writers went
    # straight past it. Still cleaned here so the diff that goes into the log is the
    # diff that lands.
    until = _clean(row.get("until"))
    if until:
        try:
            until = until if db.parse_date(until) >= on else None
        except ValueError:
            until = None

    # A key is a name this store minted, not a field the model fills in. One that
    # matches nothing is not an update to anything — it is an invention, and honouring
    # it inserts a row under a permanent identifier nobody can guess. The same run that
    # returned titles as subjects also returned `"key": "alpha"` and `"key": "beta"`,
    # and both went in as keys and stayed. Dropping it costs nothing: `find_match` is
    # what reunites a keyless diff with its row, and it is better at it than a guess.
    key = _clean(row.get("key"))
    if key and events.get(conn, key) is None:
        key = None

    kind = _clean(row.get("kind")) or "commitment"
    status = _clean(row.get("status")) or "mentioned"
    location = _clean(row.get("location"))
    # A model handed a join link and no field for it puts the link in `location`, which
    # is what the schema tells it to do with a fact that has nowhere to go — the same
    # move that put a date range in `note` when `until` was unreachable. All three
    # model trials on this corpus overwrote "Online" with a Zoom URL. Code takes it
    # back out, using the matcher the calendar connector uses, so the rule lives once.
    from_location = _lift_join_link(location)
    if from_location:
        location, join_url = from_location[1], join_url or from_location[0]
    if named_only_by_thread:
        # Nothing anyone said supports this; the conversation's name does. Keep the
        # topic at the weakest claim it can carry and drop a location the model
        # invented by repeating the title ("Chili's" at "Chili's").
        kind, status = "opportunity", "mentioned"
        if location and _squash(location) == _squash(title):
            location = None

    fields = {
        "key": key,
        "date": date_value,
        "until": until,
        "time": _clean(row.get("time")),
        "kind": kind,
        "subject": subject,
        "title": title,
        "location": location,
        "status": status,
        "participants": participants,
        "series": _clean(row.get("series")),
        "note": _clean(row.get("note")),
        "join_url": join_url,
        "source": source,
    }
    fields["instead_of"] = _stands_in_for(conn, fields, _clean(row.get("instead_of")))
    fields = {k: v for k, v in fields.items() if v is not None}
    event, verb = events.upsert(conn, fields, written_by=written_by,
                                evidence_ts=evidence_ts, commit=commit)
    if verb == "unchanged":
        return None
    return verb, f"{event.date} {event.title} [{event.key}]", event.key


def _stands_in_for(conn: sqlite3.Connection, fields: dict, claimed: str | None) -> str | None:
    """Which scheduled day, if any, this occurrence is standing in for."""
    slug = fields.get("series")
    if not slug:
        return None
    rule = series_mod.get(conn, slug)
    if rule is None or not rule.projectable:
        return None
    on = db.parse_date(fields["date"])
    landing = series_mod.occurrences(None, on - timedelta(days=21),
                                     on + timedelta(days=21), series=rule)
    if not landing:
        return None
    if claimed:
        try:
            wanted = db.parse_date(claimed)
        except (ValueError, TypeError):
            wanted = None
        if wanted in landing:
            return wanted.isoformat()
    if on in landing:
        return None                 # it is the scheduled day, not a stand-in for one
    reach = (14 if rule.cadence == "fortnightly" else 7) // 2
    near = [d for d in landing if abs((d - on).days) <= reach
            and not conn.execute("SELECT 1 FROM events WHERE series = ? AND date = ?",
                                 (rule.slug, d.isoformat())).fetchone()]
    return near[0].isoformat() if len(near) == 1 else None


_TRANSACTION_RE = re.compile(
    r"(?:^|\b)(?:paid|sent|venmo(?:ed)?|zelle(?:d)?|cashapp(?:ed)?|"
    r"payment (?:to|from)|received \$?|refunded|reimbursed)\b|"
    r"\$\s?\d+(?:\.\d{2})?\b",
    re.IGNORECASE,
)


def _is_settled_transaction(title: str, row: dict) -> bool:
    status = (_clean(row.get("status")) or "").lower()
    kind = (_clean(row.get("kind")) or "").lower()
    note = _clean(row.get("note")) or ""
    completed = status == "happened" or kind == "observed"
    return completed and bool(_TRANSACTION_RE.search(f"{title} {note}"))


def _apply_todo(conn: sqlite3.Connection, row: dict, *, source: str, written_by: str,
                auto_remind: bool = True, commit: bool = True):
    if not isinstance(row, dict):
        return None
    text = _clean(row.get("text"))
    op = (_clean(row.get("op")) or "open").lower()
    key = _clean(row.get("key"))
    if op == "close":
        target = todos.get(conn, key) if key else (todos.find(conn, text) if text else None)
        if target and todos.close(conn, target.key, commit=commit):
            return "closed", target.text, target.key
        return None
    if not text:
        return None
    event_key = _clean(row.get("event_key"))
    event = events.get(conn, event_key) if event_key else None
    todo, verb = todos.open_todo(
        conn, text, key=key, subject=_clean(row.get("subject")), due=_clean(row.get("due")),
        wake_condition=_clean(row.get("wake_condition")), source=source, written_by=written_by,
        event_id=event.id if event else None, auto_remind=auto_remind, commit=commit,
    )
    if verb == "updated":
        return None
    return "opened", todo.text, todo.key


# A slot value should be a bare answer, not a sentence. Longer than this and the model
# has written an observation into a field that is meant to hold a fact.
MAX_SLOT_VALUE = 120


def resolve_section(conn, cfg: Config, slug: str, proposed: str | None) -> str:
    """Where a page belongs is knowable, so don't leave it to the model.

    It once filed a person's favourite animal under `preferences/`, which is meant for
    the user's own preferences. An existing page always wins; otherwise a slug we can
    recognise as a person goes to `people/`.
    """
    existing = wiki.read(cfg.wiki_dir, slug)
    if existing:
        return existing.section
    known = {db.slugify(row["person"]) for row in
             conn.execute("SELECT DISTINCT person FROM handles WHERE person IS NOT NULL")}
    known |= {db.slugify(row["subject"]) for row in
              conn.execute("SELECT DISTINCT subject FROM events WHERE subject != 'me'")}
    if slug in known:
        return "people"
    proposed = (proposed or "").strip().lower()
    return proposed if proposed in wiki.SECTIONS else "people"


# The diff schema's own field names, which a model under load will happily emit as
# *values* — one returned `{"slot": "location", "value": "Eastwood",
# "question": "standing:[]", "alias": "questions"}`, having simply carried on writing
# the keys it was about to need. An alias is the one field where believing that is
# permanent: it merges two identities and nothing later undoes it.
_SCHEMA_WORDS = frozenset((
    "page", "section", "slot", "value", "question", "questions", "alias", "aliases",
    "events", "todos", "wiki", "standing", "bundle", "entity", "reviewed", "diffs",
    "null", "none", "true", "false",
))


def _is_a_name(text: str) -> bool:
    """Could this be what somebody is called? Cheap, and only ever used to say no."""
    text = (text or "").strip()
    if not text or len(text) > 60 or text.casefold() in _SCHEMA_WORDS:
        return False
    if any(ch in text for ch in "{}[]:\"\\"):
        return False           # punctuation no name has, and every JSON fragment does
    return bool(re.search(r"[A-Za-z]", text)) and len(text.split()) <= 5


def _apply_wiki(conn, cfg: Config, row: dict, *, source: str, seen: set | None = None,
                commit: bool = True):
    """Every field on this row that carries something, not the first one that does.

    Returns a list of outcomes, because a wiki row is three independent claims — a
    slot, an alias, a question — and it used to be a chain of early returns keyed on
    whichever came first. So a row carrying a good `location: Eastwood` alongside a
    garbage `alias` wrote the garbage and dropped the fact, silently: Jordan's address
    was never recorded on day one, and the failure surfaced a day later as a *history*
    check complaining there was nothing to record.
    """
    if not isinstance(row, dict):
        return []
    page = _clean(row.get("page"))
    if not page:
        return []
    slug = db.slugify(page)
    section = resolve_section(conn, cfg, slug, _clean(row.get("section")))
    slot, value = _clean(row.get("slot")), _clean(row.get("value"))
    question = _clean(row.get("question"))
    alias = _clean(row.get("alias"))
    out = []

    if slot and value:
        if len(value) > MAX_SLOT_VALUE:
            out.append(("rejected-verbose",
                        f"{slug}.{slot} value too long ({len(value)} chars)"))
        else:
            marker = (slug, slot.lower())
            if seen is None or marker not in seen:
                if seen is not None:
                    seen.add(marker)
                wiki.set_slot(cfg.wiki_dir, slug, slot, value, source=source,
                              section=section, conn=conn, commit=commit)
                out.append(("slot", f"{slug}.{slot} = {value}",
                            f"{slug}.{slot.lower()}"))

    if alias and not _is_a_name(alias):
        out.append(("rejected-alias", f"{slug}: {alias!r} is not a name"))
    elif alias:
        # An alias that would swallow a page holding its own facts is a merge, and a
        # merge is the user's call — `add_alias` refuses it, and the refusal becomes a
        # question on the page rather than a silent no-op.
        try:
            wiki.add_alias(cfg.wiki_dir, slug, alias, section=section, conn=conn,
                           commit=commit)
            out.append(("alias", f"{slug} is also {alias}",
                        f"{slug}:alias:{db.slugify(alias)}"))
        except ValueError:
            wiki.add_question(cfg.wiki_dir, slug, f"{slug}: same person as {alias}?",
                              section=section, conn=conn, commit=commit)
            out.append(("question", f"{slug}: same person as {alias}?", slug))

    if question and _is_a_question(question):
        wiki.add_question(cfg.wiki_dir, slug, question, section=section, conn=conn,
                          commit=commit)
        out.append(("question", f"{slug}: {question}", slug))
    return out


def _is_a_question(text: str) -> bool:
    """Same guard, one field over. `"question": "standing:[]"` is not a question."""
    text = (text or "").strip()
    return bool(text) and text.casefold() not in _SCHEMA_WORDS and not any(
        ch in text for ch in "{}[]\\")


def _apply_series(conn: sqlite3.Connection, cfg: Config, row: dict, *, source: str,
                  written_by: str, evidence_ts: str | None = None,
                  commit: bool = True):
    """Apply a series-rule change and regenerate its projected occurrences."""
    if not isinstance(row, dict):
        return None
    slug = db.slugify(_clean(row.get("slug")) or _clean(row.get("title")) or "")
    if not slug:
        return None
    known = series_mod.get(conn, slug)
    if row.get("ended") is True:
        if known is None or known.status == "ended":
            return None
        series_mod.end(conn, slug, written_by=written_by, commit=commit)
        return "ended", f"series: {known.title} has stopped", slug

    fields = {
        "slug": slug,
        "title": _clean(row.get("title")) or (known.title if known else
                                              slug.replace("-", " ").title()),
        "cadence": _clean(row.get("cadence")),
        "weekday": row.get("weekday"),
        "day_of_month": row.get("day_of_month"),
        "time": _clean(row.get("time")),
        "location": _clean(row.get("location")),
        "join_url": _clean(row.get("join_url")),
        "source": source,
    }
    if not fields["cadence"] and known is None:
        return None
    # A schedule announced today for a fortnight's time starts in a fortnight. Absent a
    # date the change is already in force, which is what "from now on" means; what it
    # must never do is default to something earlier than the traffic that said it, or
    # the rule retro-dates occurrences that already happened under the old one.
    when = _clean(row.get("effective_on"))
    if when:
        try:
            fields["effective_on"] = db.parse_date(when).isoformat()
        except (ValueError, TypeError):
            when = None
    if not when:
        fields["effective_on"] = max(
            db.today().isoformat(),
            (evidence_ts or "")[:10] or db.today().isoformat(),
        )
    # `effective_on` dates the *schedule*, so only a schedule change may move it. A diff
    # that restates the Zoom link is not a reason to declare the cadence newly in force,
    # and letting it write one walks the anchor of a fortnightly rule forward a week at a
    # time until "every other Tuesday" means whichever Tuesday was mentioned last.
    if known is not None and not _schedule_moved(known, fields):
        fields.pop("effective_on", None)

    rule, verb = series_mod.upsert(conn, fields, written_by=written_by, commit=commit)
    if verb == "unchanged":
        return None
    # Deliberately *not* rolled forward here. This runs before the events in its own
    # diff, so that a new occurrence can inherit from the rule the same email declared —
    # which means the store does not yet know when the next one is, and projecting now
    # writes the first day the rule lands on rather than the day somebody named. Told a
    # physio slot was weekly on Wednesdays and that the appointment was Wednesday the
    # 12th, that put a Wednesday the 5th in the store. `run.py` rolls every rule forward
    # once the pass has finished writing, which is the only point at which "what does the
    # store already know" has its final answer.
    when_said = (f" from {rule.effective_on}" if rule.effective_on else "")
    return verb, f"series: {rule.title} {rule.phrase}{when_said}", slug


def _schedule_moved(known, fields: dict) -> bool:
    """Whether this diff changes *when* the thing meets, as against what it knows."""
    return any(fields.get(name) is not None
               and str(fields[name]) != str(getattr(known, name, None) or "")
               for name in ("cadence", "weekday", "day_of_month", "time"))


def _apply_standing(conn: sqlite3.Connection, row: dict, *, written_by: str,
                    commit: bool = True):
    if not isinstance(row, dict):
        return None
    kind, value = _clean(row.get("kind")), _clean(row.get("value"))
    if kind not in todos.STANDING_KINDS or not value:
        return None
    # Old providers and hand-built payloads can still send a field excluded by the
    # current schema. Keep that compatibility boundary read-only: accepting it here
    # would let the retired store keep growing behind the schema's back.
    return ("rejected-legacy", f"standing is read-only: {kind}: {value}")
