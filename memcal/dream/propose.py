"""Stage 2 — propose (model, parallel, one call per bundle).

Each call sees its bundle plus current state. The shared prefix (instructions,
memcal window, open to-dos, identity, page titles) is byte-identical across every
call in a run, so it caches; the varying part — the bundle's own wiki pages and its
items — goes in the user turn.

The model never returns free-form memories. Only typed diffs against keys. That
single invariant is what makes deduplication automatic.
"""

from __future__ import annotations

import hashlib
import json as _json
import re
import sqlite3
from dataclasses import dataclass
from datetime import timedelta

from .. import brief, calls, db, events, identity, llm, questions, textclean, threads, todos, trace, wiki
from ..config import Config
from ..llm import CompletionClient, Reply
from . import affinity
from . import bundle as bundle_stage
from . import instructions
from . import stages as stage_plan_mod
from .bundle import Bundle

#: The v1 text, still importable under its old name so tools and tests that reach for
#: `propose.INSTRUCTIONS` keep working. `build_prefix` selects by version.
INSTRUCTIONS = instructions.V1


_STR = {"type": ["string", "null"]}

QUESTION_DIFF = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action", "key", "version", "text", "answer", "wake_condition", "cites"],
    "properties": {
        "action": {"type": "string",
                   "enum": ["ask", "keep", "amend", "resolve", "drop"]},
        "key": {**_STR, "description":
                "exact candidate key; null only when asking a new question"},
        "version": {**_STR, "description":
                    "exact candidate version; null only when asking a new question"},
        "text": {**_STR, "description":
                 "new question text for ask/amend; otherwise null"},
        "answer": {**_STR, "description":
                   "known answer for resolve; otherwise null"},
        "wake_condition": {**_STR, "description":
                           "what the open question is waiting for after amend; else null"},
        "cites": {"type": "array", "items": {"type": "string"},
                  "description": "supporting L-tags; required for state-changing actions"},
    },
}

# One bundle's diff. The batch schema below wraps a list of these.
BUNDLE_DIFF = {
    "type": "object",
    "additionalProperties": False,
    "required": ["entity", "events", "todos", "wiki", "questions"],
    "properties": {
        "entity": {"type": "string",
                   "description": "echo the BUNDLE header exactly, so diffs route back"},
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["key", "date", "until", "time", "kind", "subject", "title",
                             "location", "status", "participants", "series", "note"],
                "properties": {
                    "key": {**_STR, "description": "existing row key, or null if new"},
                    "date": {"type": "string", "description": "yyyy-mm-dd, the day it starts"},
                    "until": {**_STR, "description":
                              "yyyy-mm-dd last day, for anything spanning days; else null"},
                    "time": _STR,
                    "kind": {"type": "string",
                             "enum": ["commitment", "availability", "opportunity", "observed"]},
                    "subject": {"type": "string", "description": "'me' or the person it is about"},
                    "title": {"type": "string"},
                    "location": _STR,
                    "status": {"type": "string",
                               "enum": ["mentioned", "tentative", "confirmed", "declined", "happened"]},
                    "participants": {"type": "array", "items": {"type": "string"}},
                    "series": {**_STR, "description": "wiki slug of the recurring thing"},
                    "note": _STR,
                },
            },
        },
        "todos": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["op", "key", "text", "subject", "due", "wake_condition"],
                "properties": {
                    "op": {"type": "string", "enum": ["open", "close"]},
                    "key": _STR,
                    "text": {"type": "string"},
                    "subject": _STR,
                    "due": _STR,
                    "wake_condition": _STR,
                },
            },
        },
        "wiki": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["page", "section", "slot", "value", "question", "alias"],
                "properties": {
                    "page": {"type": "string", "description": "page slug, e.g. jordan"},
                    "section": {"type": "string",
                                "enum": ["people", "places", "projects", "preferences"]},
                    "slot": _STR,
                    "value": _STR,
                    "question": {**_STR, "description": "an open question to record on the page"},
                    "alias": {**_STR, "description":
                              "another name for this same person, e.g. their legal name"},
                },
            },
        },
        "questions": {"type": "array", "items": QUESTION_DIFF},
    },
}

# What a single request returns: one diff per bundle it was given.
DIFF_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["bundles"],
    "properties": {"bundles": {"type": "array", "items": BUNDLE_DIFF}},
}

# v2's diff is the same payload keyed differently: a six-character bundle id instead of
# an echoed header line. The id is exact, so routing needs no normalisation at all —
# `_route_key` exists entirely because "echo the BUNDLE line" produces a string that
# does not match the bundle it names.
#: `L<n>` tags for the bundle lines supporting a row. Missing citations fall back to the
#: full bundle so evidence is not lost.
_CITES = {
    "type": "array",
    "items": {"type": "string"},
    "description": "L-tags of the lines this came from, e.g. ['L7','L12']; [] if unsure",
}

EVENT_DIFF_V2 = {
    **BUNDLE_DIFF["properties"]["events"]["items"],
    "required": (BUNDLE_DIFF["properties"]["events"]["items"]["required"]
                 + ["instead_of", "cites"]),
    "properties": {
        **BUNDLE_DIFF["properties"]["events"]["items"]["properties"],
        # The one field that separates "this week we meet Wednesday instead" from "we
        # meet Wednesdays now". Both arrive as a Wednesday, and without somewhere to say
        # which one it is, the rule and the exception are the same write.
        "instead_of": {**_STR, "description":
                       "yyyy-mm-dd of the series date this replaces, for a one-off move"},
        "cites": _CITES,
    },
}

TODO_DIFF_V2 = {
    **BUNDLE_DIFF["properties"]["todos"]["items"],
    "required": (BUNDLE_DIFF["properties"]["todos"]["items"]["required"]
                 + ["event_key", "cites"]),
    "properties": {
        **BUNDLE_DIFF["properties"]["todos"]["items"]["properties"],
        "event_key": {
            **_STR,
            "description": "exact existing event key this obligation belongs to, else null",
        },
        "cites": _CITES,
    },
}

#: A change to the *schedule*, as against a change to one occasion.
#:
#: "Can we move to Tuesdays at 1pm going forward" is one sentence and, before this
#: existed, up to fifty rows: the store held occurrences and no rule, so a cadence had no
#: way to change and the only expressible reading of that email was "here is a Tuesday".
#: The next Monday then stayed exactly where it was, because nothing had said the Mondays
#: were over — nothing *could* say it.
#:
#: This stays inside invariant 2. It is a typed diff against a key, not a verb and not a
#: tool call: the model says what the rule now is, and `series.upsert` decides what that
#: means for the rows, which is deterministic store arithmetic rather than a model task.
#: `effective_on` is the whole reason it is safe — a change agreed today
#: for next month does not retro-date the occurrence that already happened this morning.
SERIES_DIFF_V2 = {
    "type": "object",
    "additionalProperties": False,
    "required": ["slug", "title", "cadence", "weekday", "day_of_month", "time",
                 "location", "join_url", "effective_on", "ended", "cites"],
    "properties": {
        "slug": {"type": "string",
                 "description": "the recurring thing's slug, e.g. 'tutoring'"},
        "title": _STR,
        "cadence": {"anyOf": [{"type": "string",
                               "enum": ["weekly", "fortnightly", "monthly"]},
                              {"type": "null"}],
                    "description": "null when the user says it repeats but not how often"},
        "weekday": {"anyOf": [{"type": "integer"}, {"type": "null"}],
                    "description": "0=Monday .. 6=Sunday, for weekly/fortnightly"},
        "day_of_month": {"anyOf": [{"type": "integer"}, {"type": "null"}],
                         "description": "1-31, for monthly"},
        "time": {**_STR, "description": "HH:MM the series meets at"},
        "location": _STR,
        "join_url": {**_STR, "description": "how you attend, if it is a link"},
        "effective_on": {**_STR, "description":
                         "yyyy-mm-dd the new schedule starts; null means already"},
        "ended": {"anyOf": [{"type": "boolean"}, {"type": "null"}],
                  "description": "true only if the user said the user has stopped going"},
        "cites": _CITES,
    },
}

BUNDLE_DIFF_V2 = {
    **BUNDLE_DIFF,
    "required": (["bundle"] + [k for k in BUNDLE_DIFF["required"] if k != "entity"]
                 + ["series"]),
    "properties": {
        **{k: v for k, v in BUNDLE_DIFF["properties"].items() if k != "entity"},
        "events": {"type": "array", "items": EVENT_DIFF_V2},
        "todos": {"type": "array", "items": TODO_DIFF_V2},
        "series": {"type": "array", "items": SERIES_DIFF_V2,
                   "description": "only when the schedule itself changed, not one date"},
        "bundle": {"type": "string",
                   "description": "the six-character id from this bundle's header"},
    },
}

# The contract that makes "I read it and it says nothing" cheap to express. Under v1
# that answer costs a whole empty skeleton per bundle, and a model that decides six
# bundles in a row say nothing tends to collapse them into `{"bundles": []}` — which
# reads as a call that never happened. `reviewed` separates the two for good.
DIFF_SCHEMA_V2 = {
    "type": "object",
    "additionalProperties": False,
    "required": ["reviewed", "diffs"],
    "properties": {
        "reviewed": {
            "type": "array", "items": {"type": "string"},
            "description": "every bundle id in this request, read or not",
        },
        "diffs": {"type": "array", "items": BUNDLE_DIFF_V2,
                  "description": "only the bundles that changed something"},
    },
}


def schema_for(cfg: Config) -> dict:
    return DIFF_SCHEMA_V2 if prompt_version(cfg) == "v2" else DIFF_SCHEMA


def prompt_version(cfg: Config) -> str:
    return getattr(cfg, "prompt_version", instructions.DEFAULT)


def stage_plan(cfg: Config) -> list[stage_plan_mod.Stage]:
    """Which staged passes to run over each request. Empty means the single call.

    v1 is excluded deliberately and not as an oversight: it is kept byte-identical as
    the baseline v2 is measured against, and it has no bundle ids, so a later stage
    could not name the bundle it was amending without the routing normaliser v2 exists
    to delete.
    """
    if prompt_version(cfg) != "v2":
        return []
    return stage_plan_mod.parse(getattr(cfg, "propose_stages", ""))


def stage_schema(stage: stage_plan_mod.Stage, *, first: bool) -> dict:
    """The response shape for one staged turn: only the arrays that stage owns.

    Restricting the schema is half of what a stage *is*. Asking for to-dos while the
    schema still admits `events` invites a model to re-send the calendar it already
    wrote — costing output tokens on every turn to restate an answer that is already
    on the wire, and putting two versions of one row in one request for `_route_v2` to
    merge.

    `reviewed` rides on the first turn only. It is proof the bundles were read, and
    they are read once however many times they are asked about.
    """
    entry = {
        "type": "object",
        "additionalProperties": False,
        "required": ["bundle", *stage.fields],
        "properties": {
            "bundle": BUNDLE_DIFF_V2["properties"]["bundle"],
            **{field: BUNDLE_DIFF_V2["properties"][field] for field in stage.fields},
        },
    }
    diffs = {"type": "array", "items": entry,
             "description": "only the bundles with something to add in this pass"}
    if not first:
        return {"type": "object", "additionalProperties": False,
                "required": ["diffs"], "properties": {"diffs": diffs}}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["reviewed", "diffs"],
        "properties": {"reviewed": DIFF_SCHEMA_V2["properties"]["reviewed"],
                       "diffs": diffs},
    }

EMPTY_DIFF = {"events": [], "todos": [], "wiki": [], "questions": [], "series": []}


ACTIVE_DAYS = 90
MAX_PEOPLE_LISTED = 60


def known_people(conn: sqlite3.Connection, cfg: Config) -> tuple[list[str], dict[str, list[str]]]:
    """Return known people and ambiguous first names for the prompt prefix."""
    since = (db.today() - timedelta(days=ACTIVE_DAYS)).isoformat()
    active = {row["person"] for row in conn.execute(
        "SELECT DISTINCT person FROM archive WHERE person IS NOT NULL AND ts >= ?", (since,))}
    for ev in events.window(conn, cfg.days_back, cfg.days_forward):
        active.update(ev.participants)
        if ev.subject and ev.subject != "me":
            active.add(ev.subject)
    page_names = {slug.replace("-", " ").title() for slug in wiki.list_pages(cfg.wiki_dir)}
    everyone = [row["person"] for row in conn.execute(
        "SELECT DISTINCT person FROM handles ORDER BY person")]
    active.update(name for name in everyone if name in page_names)
    # Streams stamp the user's own messages `person = "me"`, so the user kept showing
    # up in the list of other people to write pages about.
    active = {name for name in active if name and not identity.is_me(conn, name)}

    listed = sorted(active)[:MAX_PEOPLE_LISTED]
    listed_firsts = {name.split()[0].lower() for name in listed}
    by_first: dict[str, list[str]] = {}
    for person in everyone:
        # The user is never ambiguous with themselves. Their own duplicate contact
        # cards used to land here, which made the system ask "was that you, or a
        # different Casey?" about its own owner.
        if not person or identity.is_me(conn, person):
            continue
        by_first.setdefault(person.split()[0].lower(), []).append(person)

    ambiguous = {
        names[0].split()[0]: sorted(names)
        for first, names in sorted(by_first.items())
        if len(set(names)) > 1 and first in listed_firsts
    }
    return listed, ambiguous


#: Days of named calendar handed to the model, from yesterday forward. Long enough to
#: cover "next Saturday" and a date two weeks out, short enough to stay one paragraph.
STRIP_DAYS = 17


def _calendar_strip(today) -> str:
    """Every weekday name in reach, with its date. §2.5: make it a lookup.

    Naming today and leaving the rest as arithmetic is asking a model to count, and it
    miscounts. "beer garden saturday? like 3", said on Monday the 3rd, landed on Sunday
    the 9th in two runs out of four — a whole plan on the wrong day, from a sentence
    with no ambiguity in it at all. This costs sixty tokens, in the half of the prompt
    that is identical across every call in a pass, and deletes the entire class.
    """
    days = [today + timedelta(days=n) for n in range(-1, STRIP_DAYS)]
    # There was a `.replace(" ", " ")` here — both sides U+0020, verified
    # byte-wise, so it replaced a space with itself and had never done anything.
    # Deleted rather than repaired: the obvious guess is that a non-breaking space
    # was meant, but nothing records that, this string is prompt bytes, and
    # swapping a token the model has seen a trillion times for one it has not is a
    # change that would need measuring rather than assuming. Removing something
    # that provably does nothing needs neither.
    named = " · ".join(d.strftime("%a %-d %b")
                       + (" (TODAY)" if d == today else "") for d in days)
    return ("\nTHE DAYS BY NAME — resolve every weekday and bare date against this list, "
            "never by counting:\n  " + named
            + f"\n  (year is {today.year} unless a message says otherwise)")


def build_prefix(conn: sqlite3.Connection, cfg: Config) -> str:
    """The shared, cacheable half. Identical for every bundle in a run."""
    today = db.today()
    mine = identity.me_names(conn)
    who = (f"\nTHE USER IS {' / '.join(mine)}. Anything they say in the first person, and "
           f"any mention of them by name, is subject \"me\" — never a contact, never a "
           f"question about which one they are.\n") if mine else ""
    parts = [instructions.get(prompt_version(cfg)),
             f"\nTODAY IS {today.strftime('%A %Y-%m-%d')}.", who,
             _calendar_strip(today), "CURRENT MEMCAL"]
    rows = events.window(conn, cfg.days_back, cfg.days_forward)
    if rows:
        for ev in rows:
            parts.append(f"  {ev.key} | {ev.date} | {ev.kind}/{ev.status} | {ev.title}"
                         + (f" | {ev.time}" if ev.time else "")
                         + (f" | {ev.location}" if ev.location else "")
                         + (f" | {ev.note}" if ev.note else "")
                         + (f" | with {', '.join(ev.participants)}" if ev.participants else ""))
    else:
        parts.append("  (empty)")

    parts.append("\nOPEN TO-DOS")
    items = todos.open_items(conn)
    parts += [
        f"  {t.key} | {t.text} ({t.age})"
        + (f" | FOR EVENT {t.event_key}: {t.event_title} on {t.event_date}"
           if t.event_key else "")
        for t in items
    ] or ["  (none)"]

    active, ambiguous = known_people(conn, cfg)
    if active:
        parts.append("\nPEOPLE KNOWN: " + ", ".join(active))
    if ambiguous:
        parts.append("AMBIGUOUS FIRST NAMES — a bare one of these names does not resolve to "
                     "anyone. The full names beside it are each fine to use when the message "
                     "gives you one:")
        for first, candidates in ambiguous.items():
            parts.append(f"  {first} → {', '.join(candidates)}")

    pages = wiki.list_pages(cfg.wiki_dir)
    parts.append("\nWIKI PAGES: " + (", ".join(pages) if pages else "(none yet)"))
    # A model whose endpoint cannot take a json_schema has to be told the shape, or it
    # invents a plausible neighbouring one and every diff is dropped at routing. Under
    # staging the shape is different on every pass, so it goes on each ask instead —
    # printing the whole single-pass schema here would contradict all of them.
    if llm.endpoint(cfg.propose_model).json_mode != "schema" and not stage_plan(cfg):
        parts.append("\nRESPOND WITH ONLY VALID JSON — no markdown fences, no prose before"
                     "\nor after — matching exactly this JSON Schema:\n"
                     + _json.dumps(schema_for(cfg)))
    return "\n".join(parts)


# Packing. Completion clients have no batch endpoint, so "submit as one batch" means putting
# several bundles in one request rather than firing one request each. Bundles are
# independent and keyed, so packing changes nothing about correctness — collisions
# still merge deterministically at apply time — and it turns 80 calls into 8.
#
# It is not free, though, and the cost is on the *output* side. `max_tokens` scales with
# the bundle count, thinking is spent out of that same allowance, and nothing asks for a
# separate thinking budget — so a packed request can reason until the ceiling and return
# JSON that stops mid-object. Ten bundles in one call is 10,200 tokens for the model to
# think in and then fail to write a diff. Both numbers are on Config so the trade can
# actually be measured instead of argued about; `benchmark_temporal.py --pack N`
# exercises the same corpus at different packing levels.
PACK_TOKENS = 12_000     # fallback for callers with no Config to hand
PACK_BUNDLES = 6


def _fmt(cfg: Config) -> str:
    """Which wire format the bundles go out in. See `bundle.FORMATS`."""
    return getattr(cfg, "bundle_format", None) or bundle_stage.DEFAULT_FORMAT


def pack(cfg: Config, bundles: list[Bundle],
         conn: sqlite3.Connection | None = None) -> list[list[Bundle]]:
    """Group bundles into requests, largest first so one big bundle rides alone.

    With `pack_strategy=affinity`, conversations that look like they are about the same
    occasion are grouped first and everything else falls through to the size-ordered
    packing below. The affinity pass is pure code over the store — see `affinity.py` for
    why a wrong answer there is cheap.
    """
    max_bundles = max(1, getattr(cfg, "pack_bundles", PACK_BUNDLES))
    max_tokens = max(1000, getattr(cfg, "pack_tokens", PACK_TOKENS))
    groups: list[list[Bundle]] = []
    if getattr(cfg, "pack_strategy", "size") == "affinity":
        groups, bundles = affinity.group(
            bundles, max_bundles=max_bundles, max_tokens=max_tokens,
            cost=lambda b: textclean.estimate_tokens(build_bundle_block(cfg, b, conn)),
            near_days=getattr(cfg, "affinity_near_days", 3))
    current: list[Bundle] = []
    current_tokens = 0
    for bundle in sorted(bundles, key=lambda b: -len(b.render(_fmt(cfg)))):
        cost = textclean.estimate_tokens(build_bundle_block(cfg, bundle, conn))
        too_big = current and (current_tokens + cost > max_tokens
                               or len(current) >= max_bundles)
        if too_big:
            groups.append(current)
            current, current_tokens = [], 0
        current.append(bundle)
        current_tokens += cost
    if current:
        groups.append(current)
    return groups


def output_ceiling(group: list[Bundle]) -> int:
    """Size `max_tokens` from both bundle and message counts."""
    lines = sum(len(getattr(b, "items", ()) or ()) for b in group)
    return min(16000, 1200 + 900 * len(group) + 8 * lines)


def model_ceiling(cfg: Config, group: list[Bundle]) -> int:
    """`output_ceiling` adjusted for how much of the allowance the model thinks away.

    Measured, not guessed: on the same 23 requests, grok-4.5 emitted a median 671
    characters of reasoning and never truncated, while step-3.7-flash emitted 35k and
    truncated twice at the unboosted ceiling. The boost is per-model for that reason —
    handing every model the larger number would just pay for headroom Grok never uses.
    """
    spec = llm.endpoint(cfg.propose_model)
    # Per bundle, not per request: see the note on `Endpoint.think_tokens`. A group of
    # four tiny bundles is four judgements however little text it carries.
    floor = spec.think_tokens * max(1, len(group))
    return min(32000, max(int(output_ceiling(group) * spec.ceiling_boost), floor))


QUESTION_REPAIR_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["diffs"],
    "properties": {
        "diffs": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["bundle", "questions"],
                "properties": {
                    "bundle": {"type": "string"},
                    "questions": {"type": "array", "items": QUESTION_DIFF},
                },
            },
        },
    },
}


def _question_entries(payload: dict, bundle: str) -> list[dict]:
    for entry in payload.get("diffs") or []:
        if isinstance(entry, dict) and str(entry.get("bundle") or "").lower() == bundle:
            questions = entry.setdefault("questions", [])
            return questions if isinstance(questions, list) else []
    entry = {"bundle": bundle, **{field: [] for field in EMPTY_DIFF}}
    payload.setdefault("diffs", []).append(entry)
    return entry["questions"]


def _question_gaps(payload: dict, reviews: dict[str, dict]) -> tuple[dict[str, list], list[str]]:
    """Reject malformed dispositions and return the candidates that still need review."""
    missing: dict[str, list] = {}
    errors = []
    for bid, review in reviews.items():
        if review["overflow"]:
            errors.append(f"{bid} question review overflowed by {review['overflow']} item(s)")
            continue
        expected = {candidate.key: candidate for candidate in review["candidates"]}
        if not expected:
            continue
        actions = _question_entries(payload, bid)
        kept = []
        by_key: dict[str, list[dict]] = {}
        for action in actions:
            if not isinstance(action, dict):
                continue
            key = str(action.get("key") or "")
            if not key and action.get("action") == "ask":
                kept.append(action)
                continue
            if key not in expected:
                errors.append(f"{bid} rejected unknown question key {key!r}")
                continue
            if action.get("action") not in {"keep", "amend", "resolve", "drop"}:
                errors.append(f"{bid} rejected invalid disposition for {key!r}")
                continue
            by_key.setdefault(key, []).append(action)
        for key, rows in by_key.items():
            candidate = expected[key]
            if len(rows) != 1:
                errors.append(f"{bid} rejected duplicate question key {key!r}")
                continue
            row = rows[0]
            if str(row.get("version") or "") != candidate.version:
                errors.append(f"{bid} rejected stale question version for {key!r}")
                continue
            kept.append(row)
        actions[:] = kept
        covered = {str(row.get("key") or "") for row in kept}
        gap = [candidate for key, candidate in expected.items() if key not in covered]
        if gap:
            missing[bid] = gap
    return missing, errors


def _defer_incomplete_question_bundles(payload: dict, bids: set[str]) -> None:
    if not bids:
        return
    payload["reviewed"] = [bid for bid in (payload.get("reviewed") or [])
                           if str(bid).lower() not in bids]
    payload["diffs"] = [entry for entry in (payload.get("diffs") or [])
                        if not (isinstance(entry, dict)
                                and str(entry.get("bundle") or "").lower() in bids)]


def _tag_generation(payload: dict, generation_id: str) -> None:
    """Keep row-level provenance when several calls contribute to one merged payload."""
    if not generation_id:
        return
    for entry in payload.get("diffs") or []:
        if not isinstance(entry, dict):
            continue
        for field in EMPTY_DIFF:
            for row in entry.get(field) or []:
                if isinstance(row, dict):
                    row["_generation_id"] = generation_id


def _repair_question_coverage(client: CompletionClient, cfg: Config, prefix: str,
                              body: str, group: list[Bundle], payload: dict,
                              reply: Reply, reviews: dict[str, dict]
                              ) -> tuple[dict, Turn | None]:
    """One bounded continuation for omitted candidate keys; never repeat the proposal."""
    if prompt_version(cfg) != "v2" or not reviews:
        return payload, None
    payload["_question_coverage_checked"] = True
    missing, errors = _question_gaps(payload, reviews)
    overflow = {bid for bid, review in reviews.items() if review["overflow"]}
    if not missing:
        _defer_incomplete_question_bundles(payload, overflow)
        payload.setdefault("_coverage_errors", []).extend(errors)
        return payload, None

    lines = ["You omitted question dispositions. Review only the entries below against "
             "the same source bundles. Return only `diffs` with those question actions; "
             "do not repeat events, to-dos, wiki rows, or already-reviewed questions."]
    for bid, candidates in missing.items():
        lines.append(f"BUNDLE {bid}")
        lines.extend(f"  {c.key} | version {c.version} | {c.text}" for c in candidates)
    ask = "\n".join(lines)
    said = (reply.text or "").strip() or _json.dumps(payload, ensure_ascii=False)
    repair = client.complete(
        model=cfg.propose_model, prefix=prefix, suffix=body,
        schema=QUESTION_REPAIR_SCHEMA, schema_name="memcal_question_repair",
        max_tokens=min(6000, 700 + 500 * sum(map(len, missing.values()))),
        reasoning_effort=cfg.reasoning_effort or None,
        turns=[{"role": "assistant", "content": said},
               {"role": "user", "content": ask}],
    )
    repair_payload = repair.data if isinstance(repair.data, dict) else {}
    for entry in repair_payload.get("diffs") or []:
        if not isinstance(entry, dict):
            continue
        bid = str(entry.get("bundle") or "").lower()
        if bid not in missing:
            errors.append(f"question repair rejected unknown bundle {bid!r}")
            continue
        actions = entry.get("questions") or []
        if not isinstance(actions, list):
            errors.append(f"question repair rejected malformed actions for {bid!r}")
            continue
        generation_id = (getattr(repair, "generation_id", "") or "").strip()
        for action in actions:
            if isinstance(action, dict) and generation_id:
                action["_generation_id"] = generation_id
        _question_entries(payload, bid).extend(actions)
    still_missing, repair_errors = _question_gaps(payload, reviews)
    errors.extend(repair_errors)
    incomplete = set(still_missing) | overflow
    if incomplete:
        for bid in sorted(incomplete):
            keys = ", ".join(candidate.key for candidate in still_missing.get(bid, []))
            errors.append(f"{bid} incomplete question review"
                          + (f": {keys}" if keys else ""))
        _defer_incomplete_question_bundles(payload, incomplete)
    payload.setdefault("_coverage_errors", []).extend(errors)
    return payload, Turn("question-repair", repair, repair_payload)


def build_bundle_block(cfg: Config, bundle: Bundle,
                       conn: sqlite3.Connection | None = None) -> str:
    """One bundle as the model sees it: what it may be amending, its pages, its items."""
    context = []
    head = None
    v2 = prompt_version(cfg) == "v2"
    if v2:
        # The id goes first, so the thing the model has to echo is the first thing it
        # reads about the bundle and is six characters long. v1 asked it to echo a
        # header line carrying a colon-separated key and a parenthesised display name,
        # then spent a normaliser trying to match what came back.
        #
        # And it is the *only* name the block gives the bundle. The renderer's own
        # `BUNDLE <entity>` line used to survive underneath it, so the model was shown
        # two names for one bundle and told to echo "the id"; it echoed the entity, and
        # a diff holding the run's only to-do was dropped as unroutable.
        head = f"BUNDLE ID {bundle_id(bundle.entity)}   ({bundle.label})"
    if conn is not None:
        open_rows = build_open_rows(conn, bundle)
        if open_rows:
            context.append(open_rows)
        review = question_manifest(conn, bundle)
        if review["candidates"] or review["overflow"]:
            context.append(render_question_manifest(review))
    page_people = [p for p in bundle.people
                   if p != "me" or bundle.entity == "person:me"]
    missing = [p for p in page_people
               if p != "me" and not wiki.exists(cfg.wiki_dir, db.slugify(p))]
    if missing:
        context.append("NO PAGE YET FOR: " + ", ".join(missing)
                       + " — open one only if this bundle contains a durable fact about them.")
    pages = wiki.context_for(cfg.wiki_dir, [db.slugify(p) for p in page_people],
                             max_chars=2500)
    if pages:
        context.append("PAGES FOR THIS BUNDLE\n" + pages)

    # Keep the one routing id immediately attached to the traffic. Context used to sit
    # between them, which caused a model to borrow the *next* bundle's id; repeating the
    # id below that context fixed routing but made every small bundle look duplicated.
    # Traffic first and supporting context second preserves the association with one id.
    traffic = bundle.render(_fmt(cfg), head if v2 else None)
    return "\n".join([traffic, *context])


def question_manifest(conn: sqlite3.Connection, bundle: Bundle) -> dict:
    """The narrow question-review contract for one exact conversation entity."""
    candidates, overflow = questions.candidates(
        conn, [bundle.entity, *bundle.merged], bundle.items)
    return {"bundle": bundle_id(bundle.entity), "entity": bundle.entity,
            "candidates": candidates, "overflow": overflow}


def render_question_manifest(review: dict) -> str:
    """Put nominated questions beside the source traffic without calling hints evidence."""
    lines = [
        "OPEN QUESTIONS ASSOCIATED WITH THIS CONVERSATION.",
        "Return one disposition for every key: keep is normal when nothing changed;",
        "amend keeps it open, resolve records a known answer, and drop retires it.",
        "Copy the exact version. Cite source lines only for ask/amend/resolve/drop.",
    ]
    for candidate in review["candidates"]:
        waiting = (f" | waiting: {candidate.wake_condition}"
                   if candidate.wake_condition else "")
        lines.append(f"  {candidate.key} | version {candidate.version} | "
                     f"{candidate.text}{waiting}")
        if candidate.likely_lines:
            refs = "-".join(f"L{line}" for line in candidate.likely_lines)
            lines.append(f"    MEMCAL HINT (not source evidence): {refs} may affect "
                         f"{candidate.key}. Decide; do not assume it changed.")
    if review["overflow"]:
        lines.append(f"  REVIEW OVERFLOW: {review['overflow']} associated question(s) were "
                     "not presented. This bundle must remain queued.")
    return "\n".join(lines)


def _amendable_groups(conn: sqlite3.Connection, bundle: Bundle
                      ) -> tuple[list[events.Event], list[events.Event]]:
    """The event graph for one bundle, shared by context and silent-pass recovery."""
    graph_entities = [bundle.entity, *bundle.merged]
    people = sorted(
        set(bundle.people)
        | {
            person
            for entity in graph_entities
            for person in threads.entity_people(conn, entity)
        }
    )
    body = "\n".join(str(row["text"] or "") for row in bundle.items)
    return events.amendable_groups(
        conn, people=people, entity=bundle.entity, entities=bundle.merged, text=body)


def build_open_rows(conn: sqlite3.Connection, bundle: Bundle) -> str:
    """What is already on the calendar that this conversation could be about."""
    graph_entities = [bundle.entity, *bundle.merged]
    same, related = _amendable_groups(conn, bundle)
    if not same and not related:
        return ""
    here = {r["id"] for r in bundle.items if "id" in r.keys()}
    lines = ["ALREADY ON THE CALENDAR, AND POSSIBLY WHAT THIS CONVERSATION IS ABOUT.",
             "A line that moves, cancels, confirms or adds detail to one of these is an",
             "update: return it with that exact key, not as a new row."]

    def append_event(event, *, from_here: bool) -> None:
        day = db.parse_date(event.date).strftime("%a %Y-%m-%d")
        bits = [event.key, day, f"{event.kind}/{event.status}", event.title]
        if event.until:
            bits.insert(2, f"until {event.until}")
        if event.location:
            bits.append(event.location)
        if event.participants:
            bits.append("with " + ", ".join(event.participants))
        lines.append("  " + " | ".join(bits))
        if from_here:
            said = []
            for graph_entity in graph_entities:
                said = events.written_from(
                    conn, event.key, exclude=here, entity=graph_entity)
                if said:
                    break
        else:
            said = []
        if from_here and said:
            lines.append("    ←from here, on " + str(said[0]["ts"])[:10] + ": "
                         + " / ".join(
                             f"{'me' if r['from_me'] else (r['person'] or 'they')}: "
                             f"{str(r['text'] or '')[:70]}"
                             for r in reversed(said)))

    if same:
        lines.append("EVENTS CREATED OR UPDATED FROM THIS CONVERSATION")
        for event in same:
            append_event(event, from_here=True)
    if related:
        lines.append("OTHER POSSIBLY RELATED EVENTS INVOLVING PEOPLE HERE")
        for event in related:
            append_event(event, from_here=False)
    return "\n".join(lines)


def build_suffix(cfg: Config, group: list[Bundle],
                 conn: sqlite3.Connection | None = None) -> str:
    """The varying half of one request: every bundle in this group."""
    blocks = [build_bundle_block(cfg, bundle, conn) for bundle in group]
    if prompt_version(cfg) == "v2":
        # Paired with their labels, not as a bare list of hex. Six opaque six-character
        # ids in one request is six chances to transpose two of them, and a transposed
        # id is not a small error: one request came back with every conclusion correct
        # and two of them filed under the same wrong bundle.
        ids = "\n".join(f"  {bundle_id(b.entity)}  {b.label}" for b in group)
        header = (f"{len(group)} bundle(s) follow:\n{ids}\n"
                  f"List all {len(group)} of those ids in `reviewed`. Add a `diffs` entry "
                  f"only for the ones that change something — most requests change "
                  f"nothing, and an empty `diffs` with a full `reviewed` is the right "
                  f"answer then. Copy each `bundle` id from the header of the bundle it "
                  f"came from and check it against the list above before you write it; a "
                  f"diff under the wrong id is filed against the wrong conversation. "
                  f"Bundles are unrelated; do not carry a conclusion from one into "
                  f"another.")
    else:
        header = (f"{len(group)} bundle(s) follow. Return one diff object per bundle, "
                  f"echoing its BUNDLE line as `entity`. Bundles are unrelated to each "
                  f"other — do not carry a conclusion from one into another. Most will be "
                  f"empty; that is right.")
    return header + "\n\n" + "\n\n----------------\n\n".join(blocks)


#: Gate verdicts that mean "this line looked like a plan", rather than "this line came
#: from someone worth reading". `all-of:imessage` passes thousands of texts with no
#: opinion on any of them; `invitation` passes ninety and says something about each.
PLANNING_REASONS = frozenset({
    "invitation", "temporal", "commitment-verb", "own-commitment", "directive",
})
#: `subject-event` is omitted because a subject-line regex is weaker than message-level
#: planning language and produces false-positive retries for commercial mail.


def worth_a_second_look(bundle: Bundle, answered: set[str],
                        conn: sqlite3.Connection | None = None) -> bool:
    """Did the gate call this a plan while the model said nothing at all about it?"""
    if not bundle.items or bundle_id(bundle.entity) in answered:
        return False
    loud = sum(1 for row in bundle.items
               if (row["gate_reason"] or "") in PLANNING_REASONS)
    if loud and loud >= len(bundle.items) / 2:
        return True
    if conn is None:
        return False
    same, _related = _amendable_groups(conn, bundle)
    return bool(same)


class Truncated(RuntimeError):
    """The model ran out of output budget mid-JSON. Nothing in the reply is usable.

    It carries the turns that produced it, because **this is the most expensive failure
    there is**: the request ran to its entire output ceiling and is billed in full. It
    was raised before `_absorb` could record it, so the one call that spent the most left
    no file and no `generations` row — the exact opposite of what
    "read the trace before blaming the prompt" needs. Under staging it carries the turns
    that *succeeded* as well, which were paid for and thrown away with it.
    """

    def __init__(self, message: str, *, turns: "list[Turn] | tuple" = ()):
        super().__init__(message)
        self.turns = list(turns)


@dataclass
class Turn:
    """One model call inside a request. A single-pass request has exactly one.

    Kept separate rather than merged into a single payload because the generation id is
    per *call* and provenance is per *row*: with four stages, the event, the to-do and
    the question about one conversation were each decided by a different call, and
    "which call wrote this row" is only answerable if they stay apart until apply.
    """

    stage: str
    reply: Reply
    payload: dict


def _truncated(cfg: Config, group: list[Bundle], ceiling: int, stage: str,
               turns: "list[Turn] | tuple" = ()) -> Truncated:
    where = f" on the {stage} pass" if stage else ""
    return Truncated(
        f"reply cut off{where} at the {ceiling}-token ceiling with {len(group)} "
        f"bundle(s) in the request — thinking is spent from the same allowance. Lower "
        f"pack_bundles (currently {getattr(cfg, 'pack_bundles', PACK_BUNDLES)}) or "
        f"raise the ceiling.", turns=turns)


def propose_group(client: CompletionClient, cfg: Config, prefix: str,
                  group: list[Bundle],
                  conn: sqlite3.Connection | None = None,
                  suffix: str | None = None,
                  reviews: dict[str, dict] | None = None,
                  ) -> tuple[list[Bundle], dict, list[Turn]]:
    """One request. `suffix` is prebuilt by the caller when this runs off-thread.

    It has to be: building it reads the calendar, and a sqlite connection belongs to the
    thread that opened it. Passing the text instead of the connection is what keeps the
    fan-out safe.

    Returns the merged payload — what the request said in total, which is what the
    second-look heuristic reads — alongside the individual turns that produced it.
    """
    body = suffix if suffix is not None else build_suffix(cfg, group, conn)
    plan = stage_plan(cfg)
    if plan:
        return _propose_staged(client, cfg, prefix, group, body, plan, reviews or {})
    ceiling = model_ceiling(cfg, group)
    reply = client.complete(
        model=cfg.propose_model,
        prefix=prefix,
        suffix=body,
        schema=schema_for(cfg),
        schema_name="memcal_diff",
        max_tokens=ceiling,
        reasoning_effort=cfg.reasoning_effort or None,
    )
    # A truncated reply is a failed call, and it has to be raised rather than returned.
    # Returned, it parses as zero diffs, the pass reports "nothing new", and — worst of
    # it — every bundle in the group gets marked read. The traffic is gone from the queue
    # without ever having been looked at, and nothing anywhere says so.
    payload = reply.data if isinstance(reply.data, dict) else {}
    if reply.truncated:
        raise _truncated(cfg, group, ceiling, "", [Turn("", reply, payload)])
    payload, repair = _repair_question_coverage(
        client, cfg, prefix, body, group, payload, reply, reviews or {})
    turns = [Turn("", reply, payload)]
    if repair:
        turns.append(repair)
    return group, payload, turns


def _propose_staged(client: CompletionClient, cfg: Config, prefix: str, group: list[Bundle],
                    body: str, plan: list[stage_plan_mod.Stage], reviews: dict[str, dict]
                    ) -> tuple[list[Bundle], dict, list[Turn]]:
    """The same bundles, asked about one rubric at a time, in one conversation."""
    #: The exchange so far, in wire order, starting after the first user turn:
    #: [assistant, user, assistant, user, …]. The first stage's ask rides on the suffix
    #: instead, because two user messages in a row is not a conversation.
    turns: list[dict] = []
    opening = ""
    done: list[Turn] = []
    merged: dict = {"reviewed": [], "diffs": []}
    ceiling = model_ceiling(cfg, group)
    for index, stage in enumerate(plan):
        first = index == 0
        ask = stage_plan_mod.ask_for(plan, index)
        schema = stage_schema(stage, first=first)
        # A model whose endpoint cannot honour a json_schema is carrying the shape on the
        # prompt, and the shape now changes with every pass — so it belongs on the ask,
        # not in the shared prefix, which `build_prefix` leaves out for staged runs.
        if llm.endpoint(cfg.propose_model).json_mode != "schema":
            ask += ("\n\nRespond with only valid JSON — no markdown fences, no prose "
                    "before or after — matching exactly this JSON Schema:\n"
                    + _json.dumps(schema))
        if first:
            opening = body + "\n\n" + ask
        reply = client.complete(
            model=cfg.propose_model,
            prefix=prefix,
            suffix=opening,
            schema=schema,
            schema_name=f"memcal_diff_{stage.name}",
            max_tokens=ceiling,
            reasoning_effort=cfg.reasoning_effort or None,
            turns=None if first else [*turns, {"role": "user", "content": ask}],
        )
        payload = reply.data if isinstance(reply.data, dict) else {}
        if reply.truncated:
            # The turns already answered were paid for. They are unusable — the request
            # fails whole, for the reason in this function's docstring — but they are
            # still the record of what the model said before it ran out of room.
            raise _truncated(cfg, group, ceiling, stage.name,
                             [*done, Turn(stage.name, reply, payload)])
        done.append(Turn(stage.name, reply, payload))
        # What the model actually said, not a re-serialisation of what we parsed: the
        # next turn should see its own words. `text` is empty only when a provider
        # returns structured output with no content field, and then the parse is all
        # there is to echo back.
        said = (reply.text or "").strip() or _json.dumps(payload, ensure_ascii=False)
        if not first:
            turns.append({"role": "user", "content": ask})
        turns.append({"role": "assistant", "content": said})
        _tag_generation(payload, (getattr(reply, "generation_id", "") or "").strip())
        if first and isinstance(payload.get("reviewed"), list):
            merged["reviewed"] = payload["reviewed"]
        merged["diffs"].extend(d for d in (payload.get("diffs") or [])
                               if isinstance(d, dict))
    if done:
        merged, repair = _repair_question_coverage(
            client, cfg, prefix, opening, group, merged, done[-1].reply, reviews)
        if repair:
            done.append(repair)
    return group, merged, done


def propose_all(client: CompletionClient, conn: sqlite3.Connection, cfg: Config,
                bundles: list[Bundle], *,
                run_id: int | None = None,
                progress=None) -> tuple[list[tuple[Bundle, dict]], list[str]]:
    prefix = build_prefix(conn, cfg)
    groups = pack(cfg, bundles, conn)

    good: list[tuple[Bundle, dict]] = []
    errors: list[str] = []
    # A stage plan that covers only some of the diff is a legitimate experiment and an
    # easy way to stop recording a whole category of memory without noticing. Saying so
    # once per run is the difference between the two.
    plan = stage_plan(cfg)
    if plan:
        missing = stage_plan_mod.uncovered(plan)
        if missing:
            errors.append(f"propose_stages={cfg.propose_stages!r} asks for no "
                          f"{', '.join(missing)} — nothing this run can write one")
    #: Every request and what came back, so a bundle the gate was confident about and
    #: the model said nothing about can be found afterwards.
    seen: list[tuple] = []

    def wave(batch: list[list[Bundle]], kind: str = "main") -> list[list[Bundle]]:
        """Send these groups. Returns the ones that truncated and are worth splitting.

        The suffixes are built here, on this thread, before anything fans out: a sqlite
        connection belongs to the thread that made it, and `build_suffix` reads the
        calendar.

        `kind` says whether this wave was in the plan. Only the caller can draw an honest
        bar, and only if it is told the difference between the bundles it already expected
        and a re-send that appeared halfway through.
        """
        reviews = {}
        for group in batch:
            for bundle in group:
                review = question_manifest(conn, bundle)
                if review["candidates"] or review["overflow"]:
                    reviews[bundle_id(bundle.entity)] = review
        suffixes = [build_suffix(cfg, g, conn) for g in batch]
        if progress:
            progress("propose_wave", {"requests": len(batch), "kind": kind,
                                      "bundles": sum(len(group) for group in batch)})

        def finished(index, outcome) -> None:
            if not progress:
                return
            group = batch[index]
            progress("propose_request", {
                "index": index + 1,
                "bundles": len(group),
                "label": ", ".join(b.label for b in group[:2]),
                "ok": not isinstance(outcome, Exception),
                "error": str(outcome) if isinstance(outcome, Exception) else "",
            })

        jobs = [(group, suffix, {bundle_id(bundle.entity): reviews[bundle_id(bundle.entity)]
                                for bundle in group if bundle_id(bundle.entity) in reviews})
                for group, suffix in zip(batch, suffixes)]
        send = lambda pair: propose_group(  # noqa: E731
            client, cfg, prefix, pair[0], suffix=pair[1], reviews=pair[2])

        # Warm the cache before widening. Every request in a wave carries the same
        # `prefix`, so a wave launched all at once has every one of its first
        # `max_parallel` requests racing an empty cache and paying to write it — on this
        # store, 8 of 27 wrote a ~5,800-token prefix that the other 19 then read for a
        # tenth of the price. Sending one request first and fanning out behind it turns
        # those 8 writes into 1, which is why `packed_cost` prices a wave as
        # `misses = min(requests, max_parallel)` and not as 1.
        #
        # Only worth it when there is a cache to warm and enough requests to amortise the
        # serialised first call: with no cache (the pinned open-weight endpoints) this is
        # pure added latency, so those keep the flat fan-out.
        results: list = []
        if (len(jobs) > cfg.max_parallel
                and cfg.propose_model not in llm.NO_PROMPT_CACHE):
            first = client.map(jobs[:1], send, 1, on_done=finished)
            rest = client.map(jobs[1:], send, cfg.max_parallel,
                              on_done=lambda i, out: finished(i + 1, out))
            results = [*first, *rest]
        else:
            results = client.map(jobs, send, cfg.max_parallel, on_done=finished)
        again: list[list[Bundle]] = []
        for group, suffix, outcome in zip(batch, suffixes, results):
            seen.append((group, suffix, outcome))
            if isinstance(outcome, Exception):
                # Before anything decides what to do about it. A request that failed is
                # the one worth reading afterwards, and it used to leave nothing behind
                # but a sentence in `runs.error`.
                _record_failure(conn, cfg, prefix, group, suffix, outcome,
                                run_id=run_id)
            if isinstance(outcome, Truncated) and len(group) > 1:
                again.append(group)
                continue
            if isinstance(outcome, Exception):
                errors.append(f"{len(group)} bundle(s) [{group[0].entity}, …]: {outcome}")
                continue
            _absorb(conn, cfg, prefix, group, suffix, outcome, good, errors,
                    run_id=run_id)
        return again

    # A truncated packed request is not a failed conversation, it is a failed *batch* —
    # and it takes every bundle in it down at once. One run lost Jordan, Riley and Skyler
    # to a single reply that thought past its ceiling, and seven checks failed for it.
    #
    # Splitting and resending is the obvious recovery and nothing was doing it: the
    # bundles were simply left queued with an error string. It costs extra calls only in
    # the case that was previously a total loss, and it turns the packing question from
    # "how much correctness will you trade for cheaper input" into a tuning knob.
    def doubted(group_results) -> list[list[Bundle]]:
        """Bundles the gate was confident about, and the model said nothing about."""
        again: list[list[Bundle]] = []
        for group, _suffix, outcome in group_results:
            if isinstance(outcome, Exception):
                continue
            _g, payload, _reply = outcome
            spoke = {str(d.get("bundle") or "") for d in (payload.get("diffs") or [])}
            again += [[b] for b in group if worth_a_second_look(b, spoke, conn)]
        return again

    seen.clear()
    stranded = wave(groups)
    while stranded:
        halves = [half for group in stranded
                  for half in (group[:len(group) // 2], group[len(group) // 2:]) if half]
        errors.append(f"split {len(stranded)} truncated request(s) into {len(halves)} "
                      f"and re-sent")
        stranded = wave(halves, "split")

    # Second look at the bundles the gate was sure about and the model was silent on.
    # Deliberately after the truncation retries, and deliberately once: this is a hedge
    # against a call that under-thought, not a way to argue with a model that read
    # something and correctly found nothing in it.
    doubtful = doubted(list(seen))
    if doubtful:
        errors.append(f"re-asked {len(doubtful)} bundle(s) the gate or event graph "
                      f"flagged as plans but the model passed over "
                      f"({', '.join(g[0].label for g in doubtful[:3])}"
                      f"{'…' if len(doubtful) > 3 else ''})")
        seen.clear()
        wave(doubtful, "second-look")
    return good, errors


def _record_failure(conn, cfg: Config, prefix: str, group: list[Bundle], suffix: str,
                    outcome: Exception, *, run_id: int | None) -> None:
    """A request that produced no usable diff, written down anyway."""
    label = ", ".join(b.entity for b in group[:4])
    if len(group) > 4:
        label += f" +{len(group) - 4} more"
    ceiling = model_ceiling(cfg, group)
    refs = [bundle_ref(b) for b in group]
    turns = list(getattr(outcome, "turns", ()) or ())
    for turn in turns:
        stage = f"propose:{turn.stage}" if turn.stage else "propose"
        trace.record(conn, run_id=run_id, stage=stage,
                     label=f"[{turn.stage}] {label}" if turn.stage else label,
                     reply=turn.reply, max_tokens=ceiling, home=cfg.home,
                     prefix=prefix, suffix=suffix, bundles=refs)
        gen = (getattr(turn.reply, "generation_id", "") or "").strip()
        calls.annotate(cfg.home, gen, run_id, echoed=[], routed=[], unrouted=refs,
                       failed=str(outcome))
    if turns:
        return
    # `llm.complete` hangs its `Tally` on the exception on the way out, because that is
    # the only way an attempt count survives a raise.
    tally = getattr(outcome, "tally", None)
    calls.save_failure(
        cfg.home, run_id=run_id, stage="propose", label=label, error=str(outcome),
        model=cfg.propose_model, prefix=prefix, suffix=suffix, max_tokens=ceiling,
        bundles=refs, requests=getattr(tally, "requests", 0),
        waited=getattr(tally, "waited", 0.0))


def _absorb(conn, cfg: Config, prefix: str, group: list[Bundle], suffix: str, outcome,
            good: list, errors: list[str], *, run_id: int | None) -> None:
    """Record one successful request and route its diffs back to their bundles.

    A staged request is several calls over one conversation, and each is recorded and
    routed on its own. Nothing downstream is told about staging: `apply_diffs` merges on
    keys, so four proposals naming one bundle collide exactly the way two bundles
    proposing one slot already do — and every row keeps the generation id of the call
    that actually decided it, which one merged payload could not have preserved.
    """
    _group, merged, turns = outcome
    label = ", ".join(b.entity for b in group[:4])
    if len(group) > 4:
        label += f" +{len(group) - 4} more"
    ceiling = model_ceiling(cfg, group)
    coverage_checked = bool(merged.pop("_question_coverage_checked", False))
    coverage_errors = list(merged.pop("_coverage_errors", []) or [])
    if coverage_errors:
        errors.extend(f"question coverage: {error}" for error in coverage_errors)
    recorded: list[tuple[Turn, str]] = []
    for turn in turns:
        # The call itself is on disk, so `memcal trace` never has to ask its provider.
        stage = f"propose:{turn.stage}" if turn.stage else "propose"
        trace.record(conn, run_id=run_id, stage=stage,
                     label=f"[{turn.stage}] {label}" if turn.stage else label,
                     reply=turn.reply, max_tokens=ceiling, home=cfg.home,
                     prefix=prefix, suffix=suffix,
                     bundles=[bundle_ref(b) for b in group])
        # Carry the id forward: apply writes the provenance line, and it is the only
        # thing that can answer "which call wrote this row" once the run is over.
        gen = (getattr(turn.reply, "generation_id", "") or "").strip()
        recorded.append((turn, gen))
        if coverage_checked:
            calls.annotate(cfg.home, gen, run_id, echoed=[], routed=[],
                           unrouted=[bundle_ref(b) for b in group])
            continue
        if prompt_version(cfg) == "v2":
            routed, echoed = _route_v2(group, turn.payload, errors)
        else:
            routed = _route(group, turn.payload, errors)
            echoed = [str((d or {}).get("entity") or "")
                      for d in (turn.payload.get("bundles") or []) if isinstance(d, dict)]
        # What the model came back about against what it was given. This is the
        # difference between "read it and had nothing to say" and "never came back about
        # it", and until it was written down the only trace of it was four names in an
        # error string. Run 5 lost six bundles here and took a day to explain.
        landed = {b.entity for b, _ in routed}
        calls.annotate(
            cfg.home, gen, run_id, echoed=echoed,
            routed=[bundle_ref(b) for b, _ in routed],
            unrouted=[bundle_ref(b) for b in group if b.entity not in landed])
        for bundle, diff in routed:
            _resolve_cites(bundle, diff)
        good.extend((bundle, diff, gen) for bundle, diff in routed)
    if coverage_checked:
        routed, echoed = _route_v2(group, merged, errors)
        for bundle, diff in routed:
            _resolve_cites(bundle, diff)
        # Main diffs came from the first call. A repaired question action carries its
        # own generation id so apply can attribute that one write to the continuation.
        gen = recorded[0][1] if recorded else ""
        routed_by_generation: dict[str, set[str]] = {}
        for bundle, diff in routed:
            for field in EMPTY_DIFF:
                for row in diff.get(field) or []:
                    if isinstance(row, dict) and row.get("_generation_id"):
                        routed_by_generation.setdefault(
                            str(row["_generation_id"]), set()).add(bundle.entity)
        all_landed = {bundle.entity for bundle, _diff in routed}
        for index, (turn, turn_gen) in enumerate(recorded):
            landed = all_landed if index == 0 else routed_by_generation.get(turn_gen, set())
            turn_echoed = [str(entry.get("bundle") or "")
                           for entry in (turn.payload.get("diffs") or [])
                           if isinstance(entry, dict)]
            calls.annotate(
                cfg.home, turn_gen, run_id, echoed=turn_echoed,
                routed=[bundle_ref(bundle) for bundle, _diff in routed
                        if bundle.entity in landed],
                unrouted=[bundle_ref(bundle) for bundle in group
                          if bundle.entity not in landed])
        good.extend((bundle, diff, gen) for bundle, diff in routed)


def _resolve_cites(bundle: Bundle, diff: dict) -> None:
    """Turn this bundle's `L` tags into archive ids, here, while the bundle is known."""
    for key in ("events", "todos", "questions"):
        for row in diff.get(key) or []:
            if isinstance(row, dict) and "cites" in row:
                row["cite_ids"] = bundle.cite(row.pop("cites"))


def bundle_ref(bundle: Bundle) -> dict:
    """How a bundle is named everywhere outside its own module.

    `bundle_id` rather than the entity string, because the entity is long, contains
    colons and display names, and is the thing that kept failing to match. Six hex
    characters of the entity's hash is stable across runs, safe in a URL, and short
    enough to say out loud — which is what "which bundle was that?" actually needs.
    """
    return {"id": bundle_id(bundle.entity), "entity": bundle.entity,
            "label": bundle.label, "lines": len(bundle.items)}


def bundle_id(entity: str) -> str:
    """A short stable name for a bundle, derived from its key. Same bundle, same six
    characters, run after run — so a bundle can be linked to, searched for and named."""
    return hashlib.sha1((entity or "").encode("utf-8")).hexdigest()[:6]


def _route_v2(group: list[Bundle], payload: dict,
              errors: list[str]) -> tuple[list[tuple[Bundle, dict]], list[str]]:
    """Route by bundle id, and take `reviewed` as the proof a bundle was read."""
    by_id = {bundle_id(b.entity): b for b in group}
    by_entity = {_route_key(b.entity): bundle_id(b.entity) for b in group}
    labels: dict[str, list[str]] = {}
    for b in group:
        labels.setdefault(_route_key(b.label), []).append(bundle_id(b.entity))
    by_entity.update({name: ids[0] for name, ids in labels.items()
                      if len(ids) == 1 and name not in by_entity})
    reviewed = payload.get("reviewed")
    reviewed = [str(x).strip().lower() for x in reviewed] if isinstance(reviewed, list) else []
    reviewed = [r if r in by_id else by_entity.get(_route_key(r), r) for r in reviewed]
    returned = payload.get("diffs")
    returned = returned if isinstance(returned, list) else []

    diffs: dict[str, dict] = {}
    for entry in returned:
        if not isinstance(entry, dict):
            continue
        bid = str(entry.get("bundle") or "").strip().lower()
        bid = bid if bid in by_id else by_entity.get(_route_key(bid), bid)
        if bid not in by_id:
            errors.append(f"dropped a diff for unknown bundle id {bid!r}")
            continue
        for field in EMPTY_DIFF:
            entry.setdefault(field, [])
        # Two entries naming one bundle merge. They used to overwrite, and the cost of
        # that was invisible: a six-bundle request came back with the climbing confirm
        # and the movie cancellation both correct, both under the wrong id, and both
        # under the *same* wrong id — so the second silently replaced the first and two
        # checks failed as though the model had never noticed either.
        #
        # Merging is the same rule apply already runs on: everything is keyed, so a
        # duplicate row collides and merges instead of doubling.
        if bid in diffs:
            for field in EMPTY_DIFF:
                diffs[bid][field] = list(diffs[bid][field]) + list(entry[field])
        else:
            diffs[bid] = entry

    # A diff is itself proof the bundle was read, so a model that writes one and forgets
    # to list the id has still plainly read it. Only the other direction is unsafe.
    handled = [bid for bid in by_id if bid in reviewed or bid in diffs]
    unknown = [bid for bid in reviewed if bid not in by_id]
    if unknown:
        errors.append(f"reviewed named {len(unknown)} id(s) not in this request: "
                      f"{', '.join(unknown[:4])}")
    return ([(by_id[bid], diffs.get(bid) or dict(EMPTY_DIFF)) for bid in handled],
            reviewed)


def _route_key(entity: str) -> str:
    """Normalize an entity label so both sides of the match agree."""
    text = (entity or "").strip()
    if text.upper().startswith("BUNDLE "):
        text = text[7:].strip()
    text = re.sub(r"\s*\([^()]*\)\s*$", "", text)
    return " ".join(text.split()).casefold()


def _route(group: list[Bundle], payload: dict, errors: list[str]) -> list[tuple[Bundle, dict]]:
    """Match each returned diff back to the bundle it belongs to.

    By entity first. A diff naming a bundle that was not in this request is dropped
    rather than guessed at — misrouting a diff writes a memory onto the wrong person.
    """
    by_entity = {_route_key(bundle.entity): bundle for bundle in group}
    returned = payload.get("bundles") if isinstance(payload, dict) else None
    if not isinstance(returned, list):
        return []

    routed: list[tuple[Bundle, dict]] = []
    for index, diff in enumerate(returned):
        if not isinstance(diff, dict):
            continue
        entity = (diff.get("entity") or "").strip()
        bundle = by_entity.get(_route_key(entity))
        if bundle is None and len(returned) == len(group):
            bundle = group[index]        # positional fallback, only when counts agree
        if bundle is None:
            errors.append(f"dropped a diff for unknown bundle {entity!r}")
            continue
        for field in EMPTY_DIFF:
            diff.setdefault(field, [])
        routed.append((bundle, diff))
    return routed


def approx_prefix_tokens(prefix: str) -> int:
    return brief.approx_tokens(prefix)


def propose_one(client: CompletionClient, cfg: Config, prefix: str, bundle: Bundle,
                conn: sqlite3.Connection | None = None):
    """The live path: one thing the user just said, written now.

    The connection matters most here. The user is talking to the agent about something that is
    already on the calendar more often than not — "actually make it sunday" is the whole
    shape of a live turn — and without the amendable rows this writes a second row
    instead of moving the first."""
    group, payload, turns = propose_group(client, cfg, prefix, [bundle], conn)
    if prompt_version(cfg) == "v2":
        routed, _echoed = _route_v2(group, payload, [])
    else:
        routed = _route(group, payload, [])
    # The merged payload is the right thing to route here even under staging: one bundle,
    # and the caller wants everything the model said about it in one diff.
    #
    # **Every** turn comes back, not just the last one. With `propose_stages` on this is
    # N calls, and returning one reply meant N−1 of them reached neither disk nor
    # `generations` — the live path spending four calls and recording one.
    diff = routed[0][1] if routed else dict(EMPTY_DIFF)
    return bundle, diff, turns
