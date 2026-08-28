"""memcal as a Hermes memory provider."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

DEFAULT_MEMCAL_HOME = Path.home() / ".memcal"
# Where the memcal package lives, so we can import it without installing it.
DEFAULT_MEMCAL_SRC = Path.home() / "code" / "memcal"


_loaded_mtime = 0.0


def _source_mtime(src: str) -> float:
    """Newest mtime across the memcal package. A few stat calls, once per tool call."""
    try:
        return max(p.stat().st_mtime for p in Path(src, "memcal").glob("**/*.py"))
    except (OSError, ValueError):
        return 0.0


def _drop_cached_modules() -> None:
    """Forget every imported memcal module so the next import reads from disk."""
    for name in [n for n in sys.modules
                 if n == "memcal" or n.startswith("memcal.")]:
        sys.modules.pop(name, None)


def _load_memcal():
    """Import memcal from wherever it lives, re-reading it when the source has changed."""
    global _loaded_mtime
    src = os.environ.get("MEMCAL_SRC") or str(DEFAULT_MEMCAL_SRC)
    if src and src not in sys.path and Path(src).is_dir():
        sys.path.insert(0, src)
    newest = _source_mtime(src)
    if newest > _loaded_mtime:
        if _loaded_mtime:
            logger.info("memcal source changed, reloading")
        _drop_cached_modules()
        _loaded_mtime = newest
    try:
        import memcal  # noqa: F401
        return memcal
    except ImportError:
        return None


OPEN_PAGE = {
    "name": "memcal_open_page",
    "description": (
        "Read the wiki page for a person, place, project, or preference — including "
        "the user's own page, which is where durable facts about them live. Returns "
        "the facts as fields, each with the source that stated it and when, plus past "
        "encounters and the original messages behind every fact.\n"
        "The brief's `Pages:` line names, in parentheses, the facts each page holds. "
        "When one of those names is what the question is about, open that page — "
        "'what is Jordan into', and equally 'where is my most recent resume' against "
        "a page holding a current resume. Read that line before going to the "
        "filesystem or anywhere else for something durable about the user."),
    "parameters": {
        "type": "object",
        "properties": {"slug": {"type": "string", "description": "page slug, e.g. jordan"}},
        "required": ["slug"],
    },
}

LIST_DAYS = {
    "name": "memcal_list_days",
    "description": (
        "What memcal has on one day or a short stretch, for days OUTSIDE the window "
        "already in your context. The brief above is complete for the days it covers — "
        "'what's my weekend looking like' and 'what about the rest of my week' are "
        "answered by reading it, and calling this for a day it already lists returns "
        "the same rows a second time.\n"
        "Use it when the user asks about a date past that window: 'am I free the 15th', "
        "'anything the weekend after next'. Takes the words they used: 'saturday', "
        "'next tuesday', 'this weekend', or yyyy-mm-dd. Prefer it over "
        "memcal_list_month when the question is about a particular day — a month of "
        "rows reads as a busy calendar when the day itself may be empty."),
    "parameters": {
        "type": "object",
        "properties": {
            "when": {"type": "string",
                     "description": "'saturday', 'tomorrow', 'this weekend', or yyyy-mm-dd"},
            "days": {"type": "integer", "description": "how many days from there; default 1"},
        },
    },
}

LIST_MONTH = {
    "name": "memcal_list_month",
    "description": (
        "Everything memcal knows for a month. This is the memory calendar — optimistic "
        "and private, separate from the user's real calendar. Use it when the question "
        "reaches past the week already in the brief, and is not about one particular "
        "day — for a single day or a weekend use memcal_list_days."),
    "parameters": {
        "type": "object",
        "properties": {"month": {"type": "string", "description": "yyyy-mm; omit for this month"}},
    },
}

SEARCH_ARCHIVE = {
    "name": "memcal_search_archive",
    "description": (
        "Search every message, email, and note ever ingested. Use when you need the "
        "exact wording of something, something older than the wiki knows, or to go and "
        "check a claim the user is questioning.\n"
        "The filters are what make digging quick: `person` for one side of a "
        "conversation ('what did Quinn say about it'), `stream` for a channel "
        "('was it in an email'), and `since`/`until` for a window ('that week'). "
        "`query` may be left empty when the filters are the search — person plus a date "
        "range returns everything they said in it. Every result carries a `line_id` you "
        "can hand to memcal_conversation to read around it."),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "words to look for; may be empty"},
            "person": {"type": "string", "description": "who said it, or who it is with"},
            "stream": {"type": "string",
                       "description": "imessage | groupme | whatsapp | email | ical | agent"},
            "since": {"type": "string", "description": "yyyy-mm-dd"},
            "until": {"type": "string", "description": "yyyy-mm-dd"},
            "limit": {"type": "integer", "description": "default 20"},
        },
    },
}

OPEN = {
    "name": "memcal_open",
    "description": (
        "Open one line of the memcal brief and get everything memcal knows about it. "
        "The brief is an index — it names what is happening and who is there, and holds "
        "the rest here.\n"
        "Reach for this whenever the answer needs a detail the line does not carry: the "
        "street address, the invite or join link, whether it repeats and when the next "
        "one is, what the invitation actually said, who to ask about it. It also "
        "returns the messages the row came from and every change it has been through, "
        "so 'is this right?' and 'where did this come from?' are one call.\n"
        "Brief lines carry a short handle such as E258, T2, Q12 or S4; give it that. "
        "Events, to-dos and questions all work; S handles remain readable for older data."),
    "parameters": {
        "type": "object",
        "properties": {
            "ref": {"type": "string", "description": "brief handle, e.g. E258"}
        },
        "required": ["ref"],
    },
}

OPEN_SOURCE = {
    "name": "memcal_open_source",
    "description": (
        "Open the original messages or email behind a line in the memcal brief, and see "
        "how well it is actually backed up. Brief lines end in a short source handle "
        "such as E46, T3, Q5, or S1.\n"
        "This is the first thing to reach for whenever the user asks where something "
        "came from, why it is on their calendar, whether it is right, or what the "
        "invitation actually said. Lines marked as evidence are what the row was built "
        "from; the rest is nearby conversation. If it comes back saying there is no "
        "line-level citation, say so — that row is a summary of a conversation, not a "
        "quote from it, and it may simply be wrong.\n"
        "For the wider conversation around it, follow up with memcal_conversation."),
    "parameters": {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "brief source handle, e.g. E46"}
        },
        "required": ["source"],
    },
}

CONVERSATION = {
    "name": "memcal_conversation",
    "description": (
        "Read a stretch of the conversation a memory came out of, in order, as it was "
        "actually said. Use after memcal_open_source when the cited lines are not "
        "enough — the user asks 'what were they talking about', 'who else was in on "
        "it', 'what did they decide' — or to check a row against what was really "
        "said.\n"
        "Give it the id of a line (every line from memcal_open_source, "
        "memcal_conversation and memcal_search_archive carries one) and it returns the "
        "messages around that moment. Or give a stream and thread for the tail of a "
        "conversation."),
    "parameters": {
        "type": "object",
        "properties": {
            "line_id": {"type": "integer",
                        "description": "id of a line to centre on, from any memcal result"},
            "stream": {"type": "string",
                       "description": "imessage | groupme | whatsapp | email | ical | agent"},
            "thread": {"type": "string", "description": "the conversation id or name"},
            "before": {"type": "integer", "description": "lines before; default 12"},
            "after": {"type": "integer", "description": "lines after; default 12"},
        },
    },
}

# The write tools are typed and run as plain code — no model, no extraction step, no
# waiting. There used to be one `memcal_remember(text)` that sent the sentence to a
# model and hoped the right diff fell out. It is the wrong shape here: you are already
# a model, you already know which field you mean, and re-deriving it downstream is
# where "not going to the Meowser vet visit" turned into a free-text note with the
# status left untouched. Say what changed and it changes.

#: Everything a row can hold, shared by add and update so the two read the same.
_WHEN = {"type": "string",
         "description": "'saturday', 'tomorrow', 'this weekend', or yyyy-mm-dd"}
_STATUS = {"type": "string",
           "enum": ["mentioned", "tentative", "confirmed", "declined", "happened"],
           "description": "declined = not going. confirmed = locked in. happened = done."}
_KIND = {"type": "string", "enum": ["commitment", "availability", "opportunity", "observed"],
         "description": "commitment = they are doing it; availability = someone else's "
                        "state ('Alex free Monday'); opportunity = something they could "
                        "go to but have not committed to"}

ADD_EVENT = {
    "name": "memcal_add",
    "description": (
        "Put a new thing on memcal, right now. Use the moment the user mentions a plan — "
        "memcal is optimistic and private, so 'I might get dinner Tuesday' belongs on it "
        "at status 'mentioned'. If the thing is already on the calendar, use memcal_update "
        "instead of adding it twice."),
    "parameters": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "short, as a person would say it"},
            "when": _WHEN,
            "time": {"type": "string", "description": "'18:00' or 'after 6'; omit if unsaid"},
            "location": {"type": "string"},
            "participants": {"type": "array", "items": {"type": "string"},
                             "description": "full names where known, not the user"},
            "status": _STATUS,
            "kind": _KIND,
            "subject": {"type": "string",
                        "description": "whose thing this is; 'me' unless it is someone "
                                       "else's state or plan"},
            "until": {"type": "string", "description": "last day, for things spanning days"},
            "join_url": {"type": "string",
                         "description": "the link you attend through, for anything online. \"Online\" is a location; a Zoom link is this"},
            "series": {"type": "string", "description": "the recurring thing this is one of, as a slug ('tutoring'). Sets it where repetition alone could not prove it — a new instance then starts with what the series already knows"},
        },
        "required": ["title", "when"],
    },
}

UPDATE_EVENT = {
    "name": "memcal_update",
    "description": (
        "Change a row that already exists — this is how corrections land. Pass the exact "
        "E# handle returned by memcal_list_days, memcal_list_month, or memcal_open when "
        "you have it; otherwise name the row by distinctive words ('BondVet', 'Rowan "
        "meetup').\n"
        "  not going / cancelled / don't care  -> status 'declined'\n"
        "  yes I'm going / locked in           -> status 'confirmed'\n"
        "  it moved to Sunday                  -> when 'sunday'\n"
        "  X is coming too                     -> add_participants ['X']\n"
        "  the trip is the 15th to the 23rd    -> when '2026-08-15', until '2026-08-23'\n"
        "  there's no location for that         -> location '' (an empty string removes)\n"
        "Passing an empty string is how a detail gets *removed* rather than replaced, and "
        "it is the only way — omitting a field means 'leave it alone'. location, note, "
        "time, until, join_url, rsvp_url and series can be emptied; title, date and "
        "status cannot, because a row without them is broken rather than simpler.\n"
        "A span that is wrong at the end is `until`, not a sentence in `note`: a note "
        "reads fine and nothing can act on it, and the row drops off the brief on the "
        "day `until` says it ends.\n"
        "Returns the row as it now stands, so there is no need to look it up afterwards."),
    "parameters": {
        "type": "object",
        "properties": {
            "which": {"type": "string",
                      "description": "the row's E# handle when available, otherwise distinctive words"},
            "status": _STATUS,
            "when": _WHEN,
            "until": {"type": "string", "description": "last day, for things spanning days"},
            "time": {"type": "string"},
            "location": {"type": "string"},
            "title": {"type": "string", "description": "only to correct a wrong one"},
            "kind": _KIND,
            "subject": {"type": "string",
                        "description": "whose thing this is; only to correct a row "
                                       "filed under the wrong person"},
            "add_participants": {"type": "array", "items": {"type": "string"}},
            "note": {"type": "string", "description": "a detail with nowhere else to go"},
            "join_url": {"type": "string",
                         "description": "the link you attend through, for anything online. \"Online\" is a location; a Zoom link is this"},
            "series": {"type": "string", "description": "the recurring thing this is one of, as a slug ('tutoring'). Sets it where repetition alone could not prove it — a new instance then starts with what the series already knows"},
        },
        "required": ["which"],
    },
}

SET_SCHEDULE = {
    "name": "memcal_schedule",
    "description": (
        "The recurring thing itself moved — 'tutoring is Tuesdays at 1 now, starting next "
        "week'. This is the *rule*, not one date. memcal supersedes the old cadence, "
        "writes the next occurrence, and withdraws the ones it had projected on the old "
        "day; the link and the place live on the series, so every future occurrence "
        "carries them.\n"
        "  moved to Tuesdays at 1 going forward -> weekday 'tuesday', time '13:00'\n"
        "  every other Thursday now             -> cadence 'fortnightly', weekday 'thursday'\n"
        "  I've stopped going                   -> ended true\n"
        "Only one week changing is memcal_move_once, and a one-off event moving is "
        "memcal_update. `ended` is only ever what the user said — a single cancellation "
        "is not the end of a series."),
    "parameters": {
        "type": "object",
        "properties": {
            "which": {"type": "string",
                      "description": "the recurring thing, as a name or slug ('tutoring')"},
            "cadence": {"type": "string", "enum": ["weekly", "fortnightly", "monthly"]},
            "weekday": {"type": "string", "description": "'tuesday', for weekly/fortnightly"},
            "day_of_month": {"type": "integer", "description": "1-31, for monthly"},
            "time": {"type": "string", "description": "HH:MM"},
            "location": {"type": "string"},
            "join_url": {"type": "string",
                         "description": "the link you attend through. Held on the series, "
                                        "so every future occurrence has it"},
            "starting": {"type": "string",
                         "description": "the day the new schedule begins; omit for 'from "
                                        "now on'. Never retro-dates what already happened"},
            "ended": {"type": "boolean", "description": "only when they say they stopped"},
        },
        "required": ["which"],
    },
}

MOVE_ONCE = {
    "name": "memcal_move_once",
    "description": (
        "One week of a recurring thing moved, and only that week — 'I can't do Tuesday, "
        "we're doing Wednesday at noon just this once'. The schedule is untouched, so "
        "the week after is still on its normal day, and memcal will not put the "
        "original day back. A week skipped outright rather than moved is cancelled "
        "true. If the schedule itself changed, that is memcal_schedule."),
    "parameters": {
        "type": "object",
        "properties": {
            "which": {"type": "string",
                      "description": "the row's E# handle when available, otherwise distinctive words"},
            "to": {"type": "string", "description": "'wednesday' or yyyy-mm-dd"},
            "time": {"type": "string"},
            "cancelled": {"type": "boolean",
                          "description": "the week is skipped, not moved"},
        },
        "required": ["which", "to"],
    },
}

MERGE_EVENTS = {
    "name": "memcal_merge",
    "description": (
        "Two rows are the same thing — 'the Rowan meetup IS the beer garden'. Folds one "
        "into the other: details pool, guest lists combine, the duplicate goes away. "
        "Only when the user says they are one event; two similar plans on nearby days "
        "are usually two plans."),
    "parameters": {
        "type": "object",
        "properties": {
            "keep": {"type": "string",
                     "description": "the surviving row's E# handle or distinctive words — prefer the more specific one"},
            "drop": {"type": "string",
                     "description": "the duplicate row's E# handle or distinctive words"},
        },
        "required": ["keep", "drop"],
    },
}

DROP_EVENT = {
    "name": "memcal_drop",
    "description": (
        "Delete a row that should never have been there — a newsletter that became an "
        "event, something about a person the user does not know. NOT for things they are "
        "simply not attending: that is memcal_update with status 'declined', which keeps "
        "the row so it stops being suggested."),
    "parameters": {
        "type": "object",
        "properties": {"which": {"type": "string",
                                  "description": "the row's E# handle or distinctive words"}},
        "required": ["which"],
    },
}

ADD_TODO = {
    "name": "memcal_todo",
    "description": (
        "Something the user said they would do. Short imperative, their words. Use "
        "wake_condition when it should wait on something ('Rowan is back from Italy') "
        "rather than on a date. Use event to link an obligation to an existing occasion; "
        "it then expires when that event is over."),
    "parameters": {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "e.g. 'Return Rowan's EZ-Pass'"},
            "due": _WHEN,
            "wake_condition": {"type": "string",
                               "description": "what has to become true before asking"},
            "event": {"type": "string",
                      "description": "event E# handle, key, or distinctive words naming it"},
            "remind": {
                "type": "string",
                "description": (
                    "Set when the user wants poking about this rather than just a record of "
                    "it. 'yes' picks the hour — 09:00 the day before whatever it is "
                    "anchored to — or give an ISO datetime to say exactly when. Needs a "
                    "due date or an event to count back from. It reaches their phone and "
                    "their Telegram, so only when the user asked to be reminded."),
            },
        },
        "required": ["text"],
    },
}

ALIAS = {
    "name": "memcal_alias",
    "description": (
        "Two names, one person: a legal name behind a nickname, a surname arriving late. "
        "Points the other name at the page that already exists so it never gets a second "
        "one. Only on evidence the user gave you — merging two real people is not "
        "recoverable, and two friends sharing a first name is the opposite situation."),
    "parameters": {
        "type": "object",
        "properties": {
            "page": {"type": "string", "description": "the page that already exists, e.g. 'robbie'"},
            "name": {"type": "string", "description": "the other name, e.g. 'Robin West'"},
        },
        "required": ["page", "name"],
    },
}


NOTE = {
    "name": "memcal_note",
    "description": (
        "Record a durable fact about a person, place, or project on their wiki page — "
        "a role, a relationship, an interest, where they live. Use this when the user "
        "states something that will still be true next month, so it lands as a labelled "
        "fact instead of a sentence the nightly pass has to re-derive.\n"
        "One fact per call: page='quinn delgado', slot='likes', value='Pokemon'. "
        "The slot is the label; the value is the bare answer, not a sentence. "
        "Only what the user actually said — if you are inferring it, ask instead."),
    "parameters": {
        "type": "object",
        "properties": {
            "page": {"type": "string",
                     "description": "who or what this is about, e.g. 'Quinn Brooks'"},
            "slot": {"type": "string",
                     "description": "the label, e.g. 'likes', 'dungeon master for', 'works at'"},
            "value": {"type": "string", "description": "the bare answer, e.g. 'Pokemon'"},
            "section": {"type": "string", "enum": ["people", "places", "projects", "preferences"],
                        "description": "omit unless it is obviously not a person"},
        },
        "required": ["page", "slot", "value"],
    },
}


ANSWER = {
    "name": "memcal_answer",
    "description": (
        "Resolve anything the brief is holding open — an 'Ask about' question OR an "
        "'Open' to-do — when the user tells you the answer or says it's done. This is "
        "how those loops close; nothing closes by inference."),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string",
                         "description": "distinctive words from the question or to-do"},
            "answer": {"type": "string", "description": "what the user said"},
        },
        "required": ["question", "answer"],
    },
}

def _sourced(rows) -> list[dict]:
    """Calendar rows with the handle that opens their source.

    The brief prints `〔E117〕` on every line and a lookup printed none, so a row the
    agent found by asking was the one row it could not then check. Same handle, same
    meaning, whichever way the row was reached.
    """
    from memcal import brief                                       # noqa: PLC0415
    return [{"row": row.one_line(),
             "source": brief.source_tag("event", row.id).strip("〔〕")}
            for row in rows]


def _w_add(live, conn, cfg, args):
    event, verb = live.add_event(
        conn, cfg, title=args.get("title", ""), when=args.get("when", ""),
        time=args.get("time"), location=args.get("location"),
        participants=args.get("participants") or [], status=args.get("status"),
        kind=args.get("kind"), subject=args.get("subject"), until=args.get("until"),
        join_url=args.get("join_url"), series=args.get("series"))
    return {verb: event.one_line()}, [("event", event.key, verb)]


def _w_update(live, conn, cfg, args):
    event, changed = live.update_event(
        conn, cfg, args.get("which", ""), status=args.get("status"), when=args.get("when"),
        until=args.get("until"), time=args.get("time"), location=args.get("location"),
        title=args.get("title"), kind=args.get("kind"), subject=args.get("subject"),
        note=args.get("note"), join_url=args.get("join_url"),
        series=args.get("series"),
        add_participants=args.get("add_participants") or [])
    # Saying what did *not* change is the part that stops the retry loop: an agent told
    # only "written" re-sent the same correction three times, harder each time.
    return ({"row": event.one_line(),
             "changed": changed or "nothing — it already said that"},
            [("event", event.key, "updated")] if changed else [])


def _w_schedule(live, conn, cfg, args):
    rule, log = live.set_schedule(
        conn, cfg, args.get("which", ""), cadence=args.get("cadence"),
        weekday=args.get("weekday"), day_of_month=args.get("day_of_month"),
        time=args.get("time"), location=args.get("location"),
        join_url=args.get("join_url"), starting=args.get("starting"),
        ended=bool(args.get("ended")))
    return ({"schedule": f"{rule.title}: {rule.phrase}", "from": rule.effective_on,
             "rows": log or "nothing to re-date"},
            [("series", rule.slug, "scheduled")])


def _w_move_once(live, conn, cfg, args):
    event, replaced = live.move_one_occurrence(
        conn, cfg, args.get("which", ""), to=args.get("to", ""),
        time=args.get("time"), cancelled=bool(args.get("cancelled")))
    return ({"row": event.one_line(),
             "this week only": f"stands in for the {replaced} session; "
                               f"the schedule is unchanged"},
            [("event", event.key, "moved-once")])


def _w_merge(live, conn, cfg, args):
    event = live.merge_events(conn, cfg, args.get("keep", ""), args.get("drop", ""))
    return {"merged": event.one_line()}, [("event", event.key, "merged")]


def _w_drop(live, conn, cfg, args):
    return {"deleted": live.drop_event(conn, cfg, args.get("which", ""))}, []


def _w_todo(live, conn, cfg, args):
    # "yes"/"true" means *you pick the hour*; anything else is taken as an explicit time
    # and handed straight through. A model that writes "tomorrow morning" gets an error
    # naming what it needs rather than a reminder at a time nobody chose.
    raw = (args.get("remind") or "").strip()
    remind = True if raw.lower() in ("yes", "true", "1", "y") else (raw or None)
    todo, verb = live.open_todo(conn, cfg, args.get("text", ""), due=args.get("due"),
                                remind=remind,
                                wake_condition=args.get("wake_condition"),
                                event=args.get("event"))
    out = {verb: todo.text}
    if todo.remind_at:
        out["reminding"] = todo.remind_at
    return out, [("todo", todo.key, verb)]


def _w_note(live, conn, cfg, args):
    ok, message = live.note(conn, cfg, args.get("page", ""), args.get("slot", ""),
                            args.get("value", ""), section=args.get("section"))
    if not ok:
        return {"error": message}, []
    from memcal import db
    ref = f"{db.slugify(args.get('page', ''))}.{str(args.get('slot', '')).strip().lower()}"
    return {"written": message}, [("wiki", ref, "slot")]


def _w_alias(live, conn, cfg, args):
    from memcal import wiki
    page = wiki.add_alias(cfg.wiki_dir, args.get("page", ""), args.get("name", ""))
    ref = f"{page.slug}:alias:{args.get('name', '')}"
    return ({"page": page.slug, "also known as": page.aliases},
            [("wiki", ref, "alias")])


#: tool name -> handler. Every one of these is plain code: no model call, no waiting.
_WRITE_TOOLS = {
    "memcal_add": _w_add,
    "memcal_update": _w_update,
    "memcal_schedule": _w_schedule,
    "memcal_move_once": _w_move_once,
    "memcal_merge": _w_merge,
    "memcal_drop": _w_drop,
    "memcal_todo": _w_todo,
    "memcal_note": _w_note,
    "memcal_alias": _w_alias,
}


class MemcalMemoryProvider(MemoryProvider):
    """A calendar and a to-do list that live in context."""

    def __init__(self) -> None:
        self._home = Path(os.environ.get("MEMCAL_HOME") or DEFAULT_MEMCAL_HOME)
        self._memcal = None
        self._cfg = None
        self._session_id = ""
        self._agent_context = "primary"
        self._lock = threading.Lock()
        self._turn_archive_id: int | None = None
        self._turn_number = 0
        self._archived_turns: dict[tuple[str, str], int] = {}

    @property
    def name(self) -> str:
        return "memcal"

    # ------------------------------------------------------------- lifecycle --
    def is_available(self) -> bool:
        """No network check — just whether memcal is importable and initialized."""
        if _load_memcal() is None:
            return False
        return (self._home / "memcal.db").exists()

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._agent_context = kwargs.get("agent_context", "primary")
        self._memcal = _load_memcal()
        if self._memcal is None:
            logger.warning("memcal not importable; set MEMCAL_SRC to its checkout")
            return
        from memcal import config
        self._cfg = config.load(self._home)
        self._cfg.ensure_dirs()

    def shutdown(self) -> None:
        return

    # ----------------------------------------------------------------- inject --
    def system_prompt_block(self) -> str:
        """Stable instructions only; the changing brief is supplied by `prefetch`.

        Hermes builds this block once for prompt caching. Putting brief.md here made a
        multi-day conversation keep the version from its first turn, and compaction
        could drop the only copy. `prefetch()` is the per-turn, replaceable surface.
        """
        return (
            "# Memcal\n\n"
            "Memcal supplies a fresh snapshot of the user's week on every turn. Treat "
            "the newest `MEMCAL SNAPSHOT` as authoritative; if an older snapshot remains "
            "in long conversation history, the newest one supersedes it completely. "
            "Rows are things that were mentioned, not necessarily commitments.\n\n"
            "The snapshot is the answer, not a summary of one. It is complete for the "
            "days it covers, so 'what's my weekend looking like', 'what am I doing "
            "Thursday' and 'what's the rest of my week' are answered by reading it — "
            "reaching for a tool first adds a round trip and returns these same rows. "
            "Look things up for dates past its window, or for depth it does not carry. "
            "Every memory line has a source handle; open it when the wording is too "
            "short or the user asks what it means.\n\n"
            "When something here is settled in conversation, write it back the moment "
            "they say it, with the tool that names what changed:\n"
            "  not going, cancelled, don't care  -> memcal_update, status 'declined'\n"
            "  yes I'm going / it moved to Sunday -> memcal_update, status or when\n"
            "  a new plan they just mentioned     -> memcal_add\n"
            "  those two rows are one thing       -> memcal_merge\n"
            "  that was never real                -> memcal_drop\n"
            "  something they'll do               -> memcal_todo\n"
            "  a question or to-do resolved       -> memcal_answer\n"
            "  a durable fact about someone       -> memcal_note\n"
            "These run as code and return the row as it now stands: there is nothing to "
            "verify afterwards. Nothing closes by inference — if you are guessing, ask.\n\n"
            "macOS Calendar events are imported into this snapshot directly, without "
            "model extraction. Memcal writes change private memory only; they do not "
            "change Calendar.app. When the user asks to create, move, edit, or delete "
            "an event in their actual calendar, use Hermes's built-in iCal capability. "
            "Do not also add a duplicate memcal row: the next iCal ingest will bring "
            "the Calendar.app event into the snapshot.\n\n"
            "A wiki page may contain facts stated by the user or by their contacts. "
            "Never infer a fact, but do not throw away a plainly stated address, "
            "relationship, or favorite just because it arrived in a text."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Fresh brief plus material wiki pages named in this turn, with no model call."""
        self._refresh()
        if not self._cfg or self._agent_context != "primary":
            return ""
        try:
            from memcal import brief, db, wiki
            conn = db.open_db(self._cfg.db_path)
            try:
                snapshot = brief.render(conn, self._cfg).strip()
                pages = wiki.mentioned_pages(self._cfg.wiki_dir, query, limit=3)
                page_blocks = []
                for page in pages:
                    profile = wiki.profile(conn, self._cfg.wiki_dir, page.slug) or {}
                    encounters = profile.get("encounters") or {}
                    extra = ""
                    if encounters.get("count"):
                        activities = ", ".join(
                            f"{row['activity']} ×{row['count']}"
                            for row in encounters.get("by_activity", [])[:4])
                        extra = f"\nPast encounters: {encounters['count']}"
                        if activities:
                            extra += f" ({activities})"
                    page_blocks.append((profile.get("page") or page.render()).strip() + extra)
                stamp = db.now()
            finally:
                conn.close()
        except Exception as exc:
            logger.debug("memcal prefetch failed: %s", exc)
            return ""
        out = [f"MEMCAL SNAPSHOT {stamp}\n\n{snapshot}"]
        if page_blocks:
            out.append("WIKI PAGES MENTIONED THIS TURN\n\n" + "\n\n---\n\n".join(page_blocks))
        return "\n\n".join(out)

    # -------------------------------------------------------------- sync_all --
    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """Archive the clean user turn before any injected memory or assistant output.

        This is the self-reference boundary. Hermes calls it with the original user
        message, then performs memory prefetch. Assistant replies, summaries, injected
        snapshots and tool results never cross it.
        """
        self._turn_number = turn_number
        self._turn_archive_id = None
        if self._agent_context != "primary" or not (message or "").strip():
            return
        digest = hashlib.sha1(message.strip().encode("utf-8")).hexdigest()
        self._turn_archive_id = self._archive_user(
            message, self._session_id, external_id=f"hermes:{self._session_id}:turn:{turn_number}")
        if self._turn_archive_id is not None:
            marker = (self._session_id, digest)
            self._archived_turns[marker] = self._archived_turns.get(marker, 0) + 1

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", messages: Optional[List[Dict[str, Any]]] = None) -> None:
        """Compatibility fallback for Hermes builds without `on_turn_start`."""
        self._refresh()
        if self._agent_context != "primary" or not (user_content or "").strip():
            return
        sid = session_id or self._session_id
        digest = hashlib.sha1(user_content.strip().encode("utf-8")).hexdigest()
        marker = (sid, digest)
        already = self._archived_turns.get(marker, 0)
        if already:
            if already == 1:
                self._archived_turns.pop(marker, None)
            else:
                self._archived_turns[marker] = already - 1
            return
        self._turn_archive_id = self._archive_user(
            user_content, sid,
            external_id=f"hermes:{sid}:sync:{digest[:12]}:{time.time_ns()}")

    def _archive_user(self, text: str, session_id: str,
                      *, external_id: str) -> int | None:
        try:
            with self._lock:
                from memcal import archive, db, gate
                conn = db.open_db(self._cfg.db_path)
                stamp = db.now()
                # The user is talking to a machine, and that is the whole difference
                # between "I will do this" and "you do this". Without it the gate calls
                # every instruction the user issues an `own-commitment` and the nightly files
                # their delegated work as their own — #57.
                verdict = gate.gate_message(text, from_me=True, addressed_to="machine")
                archive_id = archive.append(
                    conn, stream="agent", external_id=external_id,
                    ts=stamp, text=text.strip()[:4000], thread=f"hermes:{session_id}",
                    person="me", from_me=True, addressed_to="machine",
                    meta={"session": session_id, "origin": "hermes-user"},
                    gated=bool(verdict), gate_reason=verdict.reason,
                )
                if archive_id and verdict:
                    archive.spool_add(conn, archive_id, "person:me")
                conn.commit()
                conn.close()
                return archive_id
        except Exception as exc:
            logger.debug("memcal spool failed: %s", exc)
            return None

    def on_session_switch(self, new_session_id: str, **kwargs) -> None:
        self._session_id = new_session_id
        self._turn_archive_id = None
        self._turn_number = 0
        self._archived_turns.clear()

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """A free episode boundary: re-render the brief so the next session opens fresh."""
        if self._agent_context != "primary" or not self._cfg:
            return
        try:
            with self._lock:
                from memcal import brief, db
                conn = db.open_db(self._cfg.db_path)
                brief.write(conn, self._cfg)
                conn.close()
        except Exception as exc:
            logger.debug("memcal brief render failed: %s", exc)

    # ------------------------------------------------------------------ tools --
    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        # Reading, then writing. §8 named one write tool, `remember`, taking a sentence;
        # what shipped sent that sentence to a model to be turned back into fields the
        # caller already had. These are the fields. One verb each, no extraction step,
        # nothing to wait for — and the set is small enough that choosing between them
        # is obvious from the user's own words.
        return [OPEN, OPEN_PAGE, OPEN_SOURCE, CONVERSATION, LIST_DAYS, LIST_MONTH,
                SEARCH_ARCHIVE,
                ADD_EVENT, UPDATE_EVENT, SET_SCHEDULE, MOVE_ONCE,
                MERGE_EVENTS, DROP_EVENT, ADD_TODO,
                ANSWER, NOTE, ALIAS]

    def _refresh(self) -> None:
        """Pick up an edited memcal before serving a call, not after the next restart.

        `self._cfg` came from the module that was loaded at start-up; once that module
        is dropped, the stale Config would still work by duck typing but would miss any
        new field, so it is rebuilt whenever the source turns over.
        """
        before = _loaded_mtime
        if _load_memcal() is None or _loaded_mtime == before:
            return
        from memcal import config
        self._cfg = config.load(self._home)
        self._cfg.ensure_dirs()

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        self._refresh()
        if not self._cfg:
            return json.dumps({"error": "memcal is not initialized"})
        try:
            from memcal import db
            conn = db.open_db(self._cfg.db_path)
            try:
                return self._dispatch(conn, tool_name, args)
            finally:
                conn.close()
        except Exception as exc:
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"})

    def _dispatch(self, conn, tool_name: str, args: Dict[str, Any]) -> str:
        from datetime import timedelta

        from memcal import archive, db, events, wiki

        if tool_name == "memcal_open_page":
            profile = wiki.profile(conn, self._cfg.wiki_dir, args.get("slug", ""))
            if not profile:
                return json.dumps({"error": "no such page",
                                   "pages_that_exist": wiki.list_pages(self._cfg.wiki_dir)})
            return json.dumps(profile)

        if tool_name == "memcal_open":
            # Both surfaces or neither. `memcal_source` and `memcal_open_source` are
            # the same tool under two names because one of them was added here and not
            # there; the cross-surface contract test exists to catch that drift.
            from memcal import detail
            return detail.open_handle(conn, self._cfg, args.get("ref", ""))

        if tool_name == "memcal_open_source":
            from memcal import trace
            return json.dumps(trace.resolve_source(conn, args.get("source", "")))

        if tool_name == "memcal_conversation":
            from memcal import trace
            stream = args.get("stream", "")
            thread = args.get("thread", "")
            around = ""
            if args.get("line_id"):
                anchor = trace.line(conn, int(args["line_id"]))
                if not anchor:
                    return json.dumps({"error": f"no line {args['line_id']}"})
                stream, thread, around = anchor["stream"], anchor["thread"], anchor["ts"]
            if not (stream and thread):
                return json.dumps({"error": "give a line_id, or a stream and thread"})
            lines = trace.conversation(
                conn, stream=stream, thread=thread, around=around,
                before=int(args.get("before") or 12), after=int(args.get("after") or 12))
            return json.dumps({"stream": stream, "thread": thread,
                               "conversation": lines})

        if tool_name == "memcal_list_days":
            start, span = db.parse_when(args.get("when", ""))
            span = max(1, int(args.get("days") or span))
            end = start + timedelta(days=span - 1)
            rows = events.between(conn, start.isoformat(), end.isoformat())
            # The resolved date goes back with the answer: "saturday" is a guess about
            # which Saturday, and the reply is what lets the user catch a wrong one.
            return json.dumps({
                "from": start.isoformat(), "to": end.isoformat(),
                "weekday": start.strftime("%A"),
                "rows": _sourced(rows),
                "free": not rows,
            })

        if tool_name == "memcal_list_month":
            month = args.get("month")
            start = db.parse_date(month + "-01") if month else db.today().replace(day=1)
            end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            rows = events.between(conn, start.isoformat(), end.isoformat())
            return json.dumps({"month": start.strftime("%Y-%m"), "rows": _sourced(rows)})

        if tool_name == "memcal_search_archive":
            rows = archive.search_filtered(
                conn, args.get("query", ""), limit=int(args.get("limit") or 20),
                person=args.get("person", ""), stream=args.get("stream", ""),
                since=args.get("since", ""), until=args.get("until", ""))
            # The "agent" stream is this conversation's own notes written back through
            # memcal_remember — frequently the assistant's paraphrase of what the user
            # said, stored under from_me. Unlabelled, a later search returns it looking
            # exactly like an independent message and the model cites its own summary
            # as corroboration for the thing the summary came from.
            return json.dumps({"results": [
                {"line_id": r["id"], "when": str(r["ts"])[:16],
                 "who": "me" if r["from_me"] else (r["person"] or r["handle"] or "?"),
                 "stream": r["stream"], "thread": r["thread"] or "",
                 "text": (r["text"] or "")[:400],
                 **({"note": "written by you earlier this session — not independent "
                             "evidence, do not cite it as confirmation"}
                    if r["stream"] == "agent" else {})}
                for r in rows]})

        if tool_name == "memcal_answer":
            from memcal import brief, todos
            ok, kind = todos.resolve(conn, args.get("question", ""), args.get("answer", ""))
            if ok:
                brief.write(conn, self._cfg)
                if kind == "already":
                    # Not a failure, and worth saying plainly — the agent usually got
                    # here by writing the same fact twice in one turn.
                    return json.dumps({"recorded": True,
                                       "note": "already settled by an earlier call"})
                return json.dumps({"recorded": True, "closed": kind})
            return json.dumps({
                "error": "nothing open matches that",
                "open_questions": [q["text"] for q in todos.open_questions(conn, 5)],
                "open_todos": [t.text for t in todos.open_items(conn)][:5],
            })

        if tool_name in _WRITE_TOOLS:
            from memcal import live, trace
            with self._lock:
                try:
                    payload, refs = _WRITE_TOOLS[tool_name](live, conn, self._cfg, args)
                    for kind, ref, verb in refs:
                        trace.stamp(
                            conn, kind=kind, ref=ref, verb=verb,
                            entity=f"thread:agent:hermes:{self._session_id}",
                            stage="live", archive_ids=(
                                [self._turn_archive_id] if self._turn_archive_id else []))
                    conn.commit()
                    return json.dumps(payload)
                except live.LiveError as exc:
                    # A refusal is an answer: it says what to do next rather than
                    # leaving the agent to rephrase the same call and hope.
                    return json.dumps({"error": str(exc), **exc.detail})
                except ValueError as exc:
                    return json.dumps({"error": str(exc)})

        return json.dumps({"error": f"unknown tool {tool_name}"})

    # ----------------------------------------------------------------- config --
    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "memcal_home", "description": "memcal data directory",
             "required": False, "default": str(DEFAULT_MEMCAL_HOME), "env_var": "MEMCAL_HOME"},
            {"key": "memcal_src", "description": "path to the memcal checkout (for import)",
             "required": False, "default": str(DEFAULT_MEMCAL_SRC), "env_var": "MEMCAL_SRC"},
        ]

    def backup_paths(self) -> List[str]:
        return [str(self._home)]


def register(ctx) -> None:
    ctx.register_memory_provider(MemcalMemoryProvider())


def register_cli(subparser) -> None:
    """`hermes memcal dream|review|who` — §8's CLI, reachable from inside Hermes."""
    subparser.add_argument("command", nargs="?", default="review",
                           choices=["dream", "review", "who", "brief", "sources", "ingest"])
    subparser.add_argument("rest", nargs="*")

    def run(args) -> int:
        src = os.environ.get("MEMCAL_SRC") or str(DEFAULT_MEMCAL_SRC)
        cmd = [sys.executable, "-m", "memcal", args.command, *(args.rest or [])]
        return subprocess.call(cmd, cwd=src)

    return run
