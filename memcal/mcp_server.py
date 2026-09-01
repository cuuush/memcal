"""A small MCP server over stdio — the portable half of §8.

One resident brain, many disposable mouths. The brief is exposed as a resource so a
harness can inject it with one read and zero latency; the navigation tools are the
"hmm, let me try to remember" path.

    python3 -m memcal.mcp_server
"""

from __future__ import annotations

import json
import sys
import traceback
from datetime import timedelta

from . import archive, brief, config, db, detail, events, live, series, todos, trace, wiki

PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "memcal_brief",
        "description": ("The always-in-context block: this week's memcal, open to-dos, "
                        "questions to ask, and identity aliases. Read this first; most "
                        "questions about the user's life are answerable from it alone."),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "memcal_open",
        "description": (
            "Open one line of the brief and get everything memcal knows about it. The "
            "brief is an index: it names what is happening and who is there, and holds "
            "the rest here. Use this whenever the answer needs a detail the line does "
            "not carry — the street address, the invite or join link, whether it "
            "repeats and when the next one is, what the invitation said, who to ask. "
            "Also returns the messages it came from and every change it has been "
            "through, so 'is this right?' and 'where did this come from?' are the same "
            "one call. Takes the handle the brief prints in brackets, such as E258, "
            "T2, or Q12. Existing legacy S handles remain readable."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string",
                        "description": "a brief handle, e.g. E258 or T2"},
            },
            "required": ["ref"], "additionalProperties": False,
        },
    },
    {
        "name": "memcal_open_page",
        "description": (
            "Read a wiki page about a person, place, project, or preference — "
            "including the user's own page, which is where durable facts about them "
            "live. Returns the page's facts as fields, each with the source that "
            "stated it and when, plus how often they and this person have actually "
            "met, and the original messages behind every fact.\n"
            "The brief's `Pages:` line already names, in parentheses, the facts each "
            "page holds. When one of those names is what the question is about, this "
            "is the call — 'what should I get Jordan for their birthday', and equally "
            "'where is my most recent resume' against a page holding a current "
            "resume. Check that line before searching anywhere else for something "
            "durable about the user."),
        "inputSchema": {
            "type": "object",
            "properties": {"slug": {"type": "string", "description": "page slug, e.g. jordan"}},
            "required": ["slug"], "additionalProperties": False,
        },
    },
    {
        "name": "memcal_list_days",
        "description": ("What memcal has on one day or a short stretch of days — "
                        "'am I free Saturday', 'what's on this week', 'anything Friday "
                        "night'. Takes plain words: 'saturday', 'tomorrow', 'this "
                        "weekend', 'next tuesday', or a yyyy-mm-dd date. Prefer this "
                        "over memcal_list_month for any question about a specific day; "
                        "a month of rows buries the one day that was asked about. Each "
                        "row ends in an E# handle; pass that exact handle to "
                        "memcal_update, memcal_move_once, memcal_merge, or memcal_drop."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "when": {"type": "string",
                         "description": "'saturday', 'tomorrow', 'this weekend', or yyyy-mm-dd"},
                "days": {"type": "integer",
                         "description": "how many days from there; default 1"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "memcal_list_month",
        "description": ("Everything memcal knows for a whole month. This is the memory "
                        "view, including direct observations imported from macOS "
                        "Calendar and plans learned elsewhere. Reading it never changes "
                        "Calendar.app. For a single day or a weekend use "
                        "memcal_list_days instead."),
        "inputSchema": {
            "type": "object",
            "properties": {"month": {"type": "string", "description": "yyyy-mm; omit for this month"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "memcal_search_archive",
        "description": (
            "Search every raw message, email, and note ever ingested. The filters are "
            "what make digging quick: `person` for one side of a conversation, `stream` "
            "for a channel, `since`/`until` for a window. `query` may be empty when the "
            "filters are the search. Every result carries a `line_id` for "
            "memcal_conversation."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "person": {"type": "string"},
                "stream": {"type": "string"},
                "since": {"type": "string", "description": "yyyy-mm-dd"},
                "until": {"type": "string", "description": "yyyy-mm-dd"},
                "limit": {"type": "integer", "description": "default 20"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "memcal_source",
        "description": (
            "Show the original messages a calendar row, to-do or question came from — "
            "the actual email or texts, in full, not a search result — and how well the "
            "row is backed up. Use this when the user asks why something is on the "
            "calendar, what an invitation said, or for a detail the one-line summary "
            "dropped. Takes the row's stable key, or the short handle the brief prints "
            "in brackets, e.g. 'E46' or 'riders-alliance-event@2026-07-30'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string",
                        "description": "the row's stable key, or a brief handle like E46"},
                "kind": {"type": "string", "enum": ["event", "todo", "question", "wiki"],
                         "description": "default 'event'"},
            },
            "required": ["ref"], "additionalProperties": False,
        },
    },
    {
        "name": "memcal_conversation",
        "description": (
            "Read the conversation around a message, in order, as it was said. Follows "
            "memcal_source when the cited lines are not enough — 'what were they talking "
            "about', 'who else was in on it'. Give a `line_id` from any memcal result, "
            "or a stream and thread."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "line_id": {"type": "integer"},
                "stream": {"type": "string"},
                "thread": {"type": "string"},
                "before": {"type": "integer", "description": "default 12"},
                "after": {"type": "integer", "description": "default 12"},
            },
            "additionalProperties": False,
        },
    },
    # The write half. One verb each, all plain code — the caller is a model that
    # already knows which field it means, so there is nothing left to extract.
    {
        "name": "memcal_add",
        "description": ("Put a new thing in memcal's private memory now. memcal is "
                        "optimistic and private: "
                        "'I might get dinner Tuesday' belongs on it at status 'mentioned'. "
                        "This does not create an event in Calendar.app; use the host's "
                        "calendar capability for that. If it is already here, use "
                        "memcal_update."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "when": {"type": "string",
                         "description": "'saturday', 'tomorrow', 'this weekend', or yyyy-mm-dd"},
                "time": {"type": "string"},
                "location": {"type": "string"},
                "participants": {"type": "array", "items": {"type": "string"}},
                "status": {"type": "string", "enum": list(events.STATUSES)},
                "kind": {"type": "string", "enum": list(events.KINDS)},
                "subject": {"type": "string", "description": "whose thing this is; 'me' usually"},
                "until": {"type": "string", "description": "last day, for multi-day things"},
                "join_url": {"type": "string",
                             "description": "the link you attend through, for anything "
                                            "online. Not the same as where it is"},
                "series": {"type": "string", "description": "the recurring thing this is one of, as a slug ('tutoring'). Sets it where repetition alone could not prove it — a new instance then starts with what the series already knows"},
            },
            "required": ["title", "when"], "additionalProperties": False,
        },
    },
    {
        "name": "memcal_update",
        "description": ("Change a row that already exists — this is how corrections land. "
                        "Pass the exact E# handle returned by a brief, list, or open "
                        "read when you have it; otherwise use distinctive words from the "
                        "row. 'not going' is status "
                        "'declined'; 'it moved to Sunday' is when='sunday'; a span that "
                        "was wrong at either end is when= and until= together. To "
                        "**remove** a wrong detail rather than replace it, pass an empty "
                        "string: location='' drops the place. Works for location, note, "
                        "time, until, join_url, rsvp_url and series. Returns "
                        "the row as it now stands, so nothing needs looking up afterwards."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "which": {"type": "string", "description": "the row's E# handle when available, otherwise distinctive words"},
                "status": {"type": "string", "enum": list(events.STATUSES)},
                "when": {"type": "string"},
                "until": {"type": "string", "description": "last day, for multi-day things"},
                "time": {"type": "string"},
                "location": {"type": "string"},
                "title": {"type": "string"},
                "kind": {"type": "string", "enum": list(events.KINDS)},
                "subject": {"type": "string",
                            "description": "whose thing this is; only to correct a "
                                           "row filed under the wrong person"},
                "add_participants": {"type": "array", "items": {"type": "string"}},
                "join_url": {"type": "string",
                             "description": "the link you attend through, for anything "
                                            "online. Not the same as where it is"},
                "series": {"type": "string", "description": "the recurring thing this is one of, as a slug ('tutoring'). Sets it where repetition alone could not prove it — a new instance then starts with what the series already knows"},
                "note": {"type": "string"},
            },
            "required": ["which"], "additionalProperties": False,
        },
    },
    {
        "name": "memcal_schedule",
        "description": ("The recurring thing itself moved — 'tutoring is Tuesdays at 1 "
                        "now, starting next week'. This is the *rule*, not one date: it "
                        "supersedes the old cadence, writes the next occurrence, and "
                        "withdraws the ones memcal had projected under the old one. Use "
                        "memcal_move_once when only a single week changes."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "which": {"type": "string",
                          "description": "the recurring thing, as a name or slug ('tutoring')"},
                "cadence": {"type": "string", "enum": list(series.CADENCES)},
                "weekday": {"type": "string",
                            "description": "'tuesday', for weekly/fortnightly"},
                "day_of_month": {"type": "integer", "description": "1-31, for monthly"},
                "time": {"type": "string", "description": "HH:MM"},
                "location": {"type": "string"},
                "join_url": {"type": "string",
                             "description": "the link you attend through. Held on the "
                                            "series, so every future occurrence has it"},
                "starting": {"type": "string",
                             "description": "the day the new schedule begins; omit for "
                                            "'from now on'. Never retro-dates what has "
                                            "already happened"},
                "ended": {"type": "boolean",
                          "description": "only when the user says they have stopped "
                                         "going. Never inferred from one cancellation"},
            },
            "required": ["which"], "additionalProperties": False,
        },
    },
    {
        "name": "memcal_move_once",
        "description": ("One week of a recurring thing moved, and only that week — 'I "
                        "can't do Tuesday, we're doing Wednesday at noon just this "
                        "once'. The schedule is untouched, so the week after is still "
                        "on its normal day. If the schedule itself changed, that is "
                        "memcal_schedule."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "which": {"type": "string",
                          "description": "the row's E# handle when available, otherwise distinctive words"},
                "to": {"type": "string", "description": "'wednesday' or yyyy-mm-dd"},
                "time": {"type": "string"},
                "cancelled": {"type": "boolean",
                              "description": "true when the week is skipped outright "
                                             "rather than moved"},
            },
            "required": ["which", "to"], "additionalProperties": False,
        },
    },
    {
        "name": "memcal_merge",
        "description": ("Two rows are the same thing — 'the Rowan meetup IS the beer garden'. "
                        "Details pool, guest lists combine, the duplicate goes away."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keep": {"type": "string",
                         "description": "the surviving row's E# handle or distinctive words"},
                "drop": {"type": "string",
                         "description": "the duplicate row's E# handle or distinctive words"},
            },
            "required": ["keep", "drop"], "additionalProperties": False,
        },
    },
    {
        "name": "memcal_drop",
        "description": ("Delete a row that should never have existed — a newsletter that "
                        "became an event. Not for things the user simply is not attending: "
                        "that is memcal_update with status 'declined'."),
        "inputSchema": {
            "type": "object",
            "properties": {"which": {"type": "string",
                                      "description": "the row's E# handle or distinctive words"}},
            "required": ["which"], "additionalProperties": False,
        },
    },
    {
        "name": "memcal_todo",
        "description": ("Something the user said they would do. Short imperative, their "
                        "words. wake_condition makes it wait on an event rather than a "
                        "date. event links an obligation to an existing occasion so it "
                        "expires with that event. done=true closes one they say they have "
                        "finished — any distinctive words from it will find it."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "due": {"type": "string"},
                "wake_condition": {"type": "string"},
                "event": {"type": "string",
                          "description": "event E# handle, key, or distinctive words naming it"},
                "done": {"type": "boolean"},
            },
            "required": ["text"], "additionalProperties": False,
        },
    },
    {
        "name": "memcal_note",
        "description": ("One durable fact on a wiki page: page='quinn delgado', "
                        "slot='likes', value='Pokemon'. The slot is the label, the value "
                        "is the bare answer. Only what the user actually said."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "page": {"type": "string"}, "slot": {"type": "string"},
                "value": {"type": "string"},
                "section": {"type": "string", "enum": list(wiki.SECTIONS)},
            },
            "required": ["page", "slot", "value"], "additionalProperties": False,
        },
    },
    {
        "name": "memcal_alias",
        "description": ("Two names, one person — a legal name behind a nickname. Points the "
                        "other name at the page that already exists so it never gets a "
                        "second one. Only on evidence the user gave you."),
        "inputSchema": {
            "type": "object",
            "properties": {"page": {"type": "string"}, "name": {"type": "string"}},
            "required": ["page", "name"], "additionalProperties": False,
        },
    },
    {
        "name": "memcal_answer",
        "description": ("Record the user's answer to one of the 'Ask about' questions, so it "
                        "stops being asked. To-dos close conversationally — this is how."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "any distinctive words from it"},
                "answer": {"type": "string"},
            },
            "required": ["question", "answer"], "additionalProperties": False,
        },
    },
]


def _render_page(profile: dict) -> str:
    """One wiki page as text: what it says, how often they meet, who said each fact.

    The page markdown leads because it is what the user wrote and what the user would read. What
    follows is the two things the markdown genuinely cannot carry — an encounter count
    that is computed from events rather than stored, and the exact line behind each
    slot, which the rendering compresses into a source name inside an HTML comment.
    """
    out = [profile["page"].strip()]

    seen = profile.get("encounters") or {}
    if seen.get("count"):
        activities = ", ".join(f"{row['activity']} ×{row['count']}"
                               for row in seen.get("by_activity", [])[:4])
        line = f"Past encounters: {seen['count']}"
        out.append(f"{line} ({activities})" if activities else line)

    stated = []
    narrow = profile.get("narrow") or {}
    for slot, rows in (profile.get("sources") or {}).items():
        # `evidence` is the distinction worth keeping: these are the lines the fact was
        # built from, not merely lines near it. Anything else is a `memcal_source` call.
        quotes = [row for row in rows if row["evidence"]][:2]
        # Except where the row predates line-level citation, in which case `source_rows`
        # recovers the whole spool bundle and marks every line of it as evidence.
        # Quoting the first two under "Stated by" is not a smaller answer, it is a wrong
        # one: `casey-morgan.education` attributed "computer science" to "Hey
        # rutgers is having a hiring freeze" and "Yooooo how's it going". `narrow` is
        # what tells the two apart, and `memcal_source` already says so out loud.
        if not narrow.get(slot, True):
            stated.append(f"- **{slot}** — no line-level citation; "
                          f"memcal_source(ref='{profile['slug']}.{slot.lower()}', "
                          f"kind='wiki') has the conversation it came out of")
            continue
        for row in quotes:
            stated.append(f"- **{slot}** — {row['who']}, {str(row['ts'])[:10]}: "
                          f"{row['text'].strip()[:200]}")
    if stated:
        out.append("## Stated by\n\n" + "\n".join(stated))
    return "\n\n".join(out) + "\n"


#: Every tool that writes. `Server.call` routes on this and `Server._write` refuses
#: anything outside it, so the two cannot drift apart — and they had:
#: `memcal_schedule` and `memcal_move_once` were advertised in `TOOLS`, had working
#: handlers in `_write`, and were missing from the hand-written tuple `call` matched on,
#: so both raised "unknown tool". Invariant 12 — a repeating thing is stored as its rule
#: — had no expression at all on this surface, and the fallback an agent reaches for is
#: `memcal_update` on one occurrence, which is the exact failure `live.set_schedule`
#: exists to prevent.
WRITE_TOOLS = frozenset({
    "memcal_add", "memcal_update", "memcal_schedule", "memcal_move_once",
    "memcal_merge", "memcal_drop", "memcal_todo", "memcal_note", "memcal_alias",
})


class Server:
    def __init__(self):
        self.cfg = config.load()
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)

    # ------------------------------------------------------------------ tools --
    def call(self, name: str, args: dict) -> str:
        if name == "memcal_brief":
            return brief.render(self.conn, self.cfg)

        if name == "memcal_open":
            # No `kind` argument, unlike `memcal_source`. The handle already says which
            # table it is, and every parameter a caller can get wrong is a parameter a
            # caller does get wrong.
            return detail.open_handle(self.conn, self.cfg, str(args.get("ref", "")))

        if name == "memcal_open_page":
            # Both surfaces or neither, the same rule `memcal_open_source` is under.
            # This one returned `page.render()` and nothing else while the Hermes
            # surface returned the whole profile, so the same tool name answered the
            # same question two different ways and the poorer one was the portable
            # half. Encounters and the line that stated each fact are the difference.
            profile = wiki.profile(self.conn, self.cfg.wiki_dir, args.get("slug", ""))
            if not profile:
                known = ", ".join(wiki.list_pages(self.cfg.wiki_dir)) or "(none yet)"
                return f"No page for {args.get('slug')!r}. Pages that exist: {known}"
            return _render_page(profile)

        if name == "memcal_list_days":
            start, span = db.parse_when(args.get("when", ""))
            span = max(1, int(args.get("days") or span))
            end = start + timedelta(days=span - 1)
            rows = events.between(self.conn, start.isoformat(), end.isoformat())
            label = (start.strftime("%A %b %-d") if span == 1
                     else f"{start.strftime('%a %b %-d')} – {end.strftime('%a %b %-d')}")
            if not rows:
                # "(nothing)" reads as a failed lookup. The answer to "am I free
                # Saturday" is the whole point of the tool, so say it plainly.
                return f"memcal {label}: nothing on the calendar — free as far as memcal knows."
            head = f"memcal {label} ({len(rows)} row{'s' if len(rows) != 1 else ''})"
            return "\n".join([head] + [f"{r.one_line()}  {brief.source_tag('event', r.id)}"
                                           for r in rows])

        if name == "memcal_list_month":
            month = args.get("month")
            start = db.parse_date(month + "-01") if month else db.today().replace(day=1)
            end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
            rows = events.between(self.conn, start.isoformat(), end.isoformat())
            head = f"memcal {start.strftime('%B %Y')} ({len(rows)} rows)"
            return ("\n".join([head] + [f"{r.one_line()}  {brief.source_tag('event', r.id)}"
                                           for r in rows]) if rows else head + "\n(nothing)")

        if name == "memcal_search_archive":
            rows = archive.search_filtered(
                self.conn, args.get("query", ""), limit=int(args.get("limit") or 20),
                person=args.get("person", ""), stream=args.get("stream", ""),
                since=args.get("since", ""), until=args.get("until", ""))
            if not rows:
                return "(nothing found)"
            out = []
            for row in rows:
                who = "me" if row["from_me"] else (row["person"] or row["handle"] or "?")
                out.append(f"[{row['id']}] {str(row['ts'])[:16]}  {row['stream']}/{who}: "
                           f"{row['text'][:300]}")
            return ("(the number in brackets is a line_id — pass it to "
                    "memcal_conversation to read around it)\n" + "\n".join(out))

        if name == "memcal_conversation":
            stream, thread = args.get("stream", ""), args.get("thread", "")
            around = ""
            if args.get("line_id"):
                anchor = trace.line(self.conn, int(args["line_id"]))
                if not anchor:
                    return f"no line {args['line_id']}"
                stream, thread, around = anchor["stream"], anchor["thread"], anchor["ts"]
            if not (stream and thread):
                return "give a line_id, or a stream and thread"
            lines = trace.conversation(
                self.conn, stream=stream, thread=thread, around=around,
                before=int(args.get("before") or 12), after=int(args.get("after") or 12))
            if not lines:
                return "(nothing in that conversation)"
            return "\n".join(f"[{line['id']}] {line['ts']}  {line['who']}: {line['text']}"
                             for line in lines)

        if name == "memcal_source":
            # The asymmetry this closes: the web UI has had an "Original source" panel
            # for a while, and an agent reading `〔E119〕` in the brief had no way at all
            # to reach the email behind it — `memcal_search_archive` is full-text only
            # and truncates every hit to 300 characters. "What did the invitation say?"
            # was answerable by a person clicking and not by the agent being asked.
            ref, kind = args.get("ref", ""), args.get("kind") or "event"
            # The brief prints `〔E119〕` and nothing else, so that is what a caller has
            # in hand. Refusing it and asking for a key the agent has never been shown
            # is our schema leaking into their reasoning.
            handle = brief.parse_source(str(ref).strip("〔〕"))
            if handle:
                resolved = trace.resolve_source(self.conn, str(ref).strip("〔〕"))
                if resolved.get("error"):
                    return resolved["error"]
                kind, ref = resolved["kind"], resolved["ref"]
            cited = trace.citations(self.conn, kind, ref)
            rows = trace.source_rows(self.conn, kind=kind, ref=ref)
            if not rows:
                return ("(no source recorded for that row — it may have been added "
                        "directly rather than read out of a message)")
            out = []
            if not cited["narrow"]:
                out.append("(!) no line-level citation: what follows is the conversation "
                           "this row came out of, not the lines it was built from")
            for row in rows:
                # `evidence` marks the lines the row was actually built from; the rest
                # is neighbouring thread context, which is what makes a two-word "yeah"
                # readable. Marking them is cheaper than dropping them and lets the
                # caller weigh what it quotes.
                mark = "*" if row.get("evidence") else " "
                # Untruncated on purpose. The whole reason to come here rather than to
                # search is that the detail the summary dropped is somewhere in a body
                # that `memcal_search_archive` cuts off at 300 characters.
                if row.get("source_heading"):
                    out.append(f"--- {row['source_heading']} ---")
                out.append(f"{mark} [{row['id']}] {row['stream']} · {row['ts'][:16]} · "
                           f"{row['who']}\n{row['text']}")
            return ("(* = a line this row was built from; others are nearby context. "
                    "[n] is a line_id for memcal_conversation)\n\n" + "\n\n".join(out))

        if name in WRITE_TOOLS:
            try:
                return self._write(name, args)
            except (live.LiveError, ValueError) as exc:
                # Named `fields`, not `detail`. `detail` is the module this file imports
                # and `memcal_open` calls — and binding it here made it a local for the
                # whole of `call`, so `detail.open_handle` at the top raised
                # `UnboundLocalError` on every single invocation. The flagship "the brief
                # is an index, open the handle" tool was dead on this surface, and the
                # outer handler turned the traceback into a returned string, so it looked
                # like a tool that answers unhelpfully rather than one that never ran.
                fields = getattr(exc, "detail", {}) or {}
                extra = "".join(f"\n  {k}: {v}" for k, v in fields.items())
                return f"{exc}{extra}"

        if name == "memcal_answer":
            ok = todos.answer(self.conn, args.get("question", ""), args.get("answer", ""))
            brief.write(self.conn, self.cfg)
            return "recorded" if ok else "no matching open question"

        raise ValueError(f"unknown tool {name}")

    def _write(self, name: str, args: dict) -> str:
        """The typed writes. Each returns the resulting row, so the caller never has to
        read the calendar back to find out whether its edit landed."""
        if name not in WRITE_TOOLS:
            # This used to fall through to the alias branch at the bottom, so any name
            # that reached here and matched nothing quietly wrote an alias.
            raise ValueError(f"unknown tool {name}")
        conn, cfg = self.conn, self.cfg
        if name == "memcal_add":
            event, verb = live.add_event(
                conn, cfg, title=args.get("title", ""), when=args.get("when", ""),
                time=args.get("time"), location=args.get("location"),
                participants=args.get("participants") or [], status=args.get("status"),
                kind=args.get("kind"), subject=args.get("subject"), until=args.get("until"),
                join_url=args.get("join_url"), series=args.get("series"))
            return f"{verb}: {event.one_line()}"
        if name == "memcal_update":
            event, changed = live.update_event(
                conn, cfg, args.get("which", ""), status=args.get("status"),
                when=args.get("when"), until=args.get("until"), time=args.get("time"),
                location=args.get("location"), title=args.get("title"),
                kind=args.get("kind"), subject=args.get("subject"),
                note=args.get("note"), join_url=args.get("join_url"),
                series=args.get("series"),
                add_participants=args.get("add_participants") or [])
            return (event.one_line() + "\nchanged: "
                    + ("; ".join(changed) if changed else "nothing — it already said that"))
        if name == "memcal_schedule":
            rule, log = live.set_schedule(
                conn, cfg, args.get("which", ""), cadence=args.get("cadence"),
                weekday=args.get("weekday"), day_of_month=args.get("day_of_month"),
                time=args.get("time"), location=args.get("location"),
                join_url=args.get("join_url"), starting=args.get("starting"),
                ended=bool(args.get("ended")))
            return (f"{rule.title}: {rule.phrase}, from {rule.effective_on}"
                    + ("\n" + "\n".join(log) if log else ""))
        if name == "memcal_move_once":
            event, replaced = live.move_one_occurrence(
                conn, cfg, args.get("which", ""), to=args.get("to", ""),
                time=args.get("time"), cancelled=bool(args.get("cancelled")))
            return (f"{event.one_line()}\nthis week only — the {replaced} session; "
                    f"the schedule is unchanged")
        if name == "memcal_merge":
            return "merged: " + live.merge_events(
                conn, cfg, args.get("keep", ""), args.get("drop", "")).one_line()
        if name == "memcal_drop":
            return "deleted: " + live.drop_event(conn, cfg, args.get("which", ""))
        if name == "memcal_todo":
            if args.get("done"):
                return f"closed: {live.close_todo(conn, cfg, args.get('text', '')).text}"
            todo, verb = live.open_todo(conn, cfg, args.get("text", ""), due=args.get("due"),
                                        wake_condition=args.get("wake_condition"),
                                        event=args.get("event"))
            return f"{verb}: {todo.text}"
        if name == "memcal_note":
            ok, message = live.note(conn, cfg, args.get("page", ""), args.get("slot", ""),
                                    args.get("value", ""), section=args.get("section"))
            return message if ok else f"not written: {message}"
        if name == "memcal_alias":
            page = wiki.add_alias(cfg.wiki_dir, args.get("page", ""), args.get("name", ""))
            return f"{page.slug} is also known as {', '.join(page.aliases)}"
        raise ValueError(f"unroutable write tool {name}")

    # --------------------------------------------------------------- protocol --
    def handle(self, request: dict) -> dict | None:
        method = request.get("method")
        request_id = request.get("id")
        params = request.get("params") or {}

        if method == "initialize":
            return _ok(request_id, {
                "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {}, "resources": {}},
                "serverInfo": {"name": "memcal", "version": "0.1.0"},
            })
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return _ok(request_id, {})
        if method == "tools/list":
            return _ok(request_id, {"tools": TOOLS})
        if method == "resources/list":
            return _ok(request_id, {"resources": [{
                "uri": "memcal://brief",
                "name": "memcal brief",
                "description": "This week, open to-dos, questions, and identity aliases.",
                "mimeType": "text/markdown",
            }]})
        if method == "resources/read":
            text = brief.render(self.conn, self.cfg)
            return _ok(request_id, {"contents": [{
                "uri": params.get("uri", "memcal://brief"),
                "mimeType": "text/markdown",
                "text": text,
            }]})
        if method == "tools/call":
            name = params.get("name", "")
            try:
                text = self.call(name, params.get("arguments") or {})
                return _ok(request_id, {"content": [{"type": "text", "text": text}]})
            except Exception as exc:
                return _ok(request_id, {
                    "content": [{"type": "text", "text": f"{type(exc).__name__}: {exc}"}],
                    "isError": True,
                })
        if request_id is None:
            return None
        return {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32601, "message": f"method not found: {method}"}}


def _ok(request_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main() -> int:
    server = Server()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            continue
        try:
            response = server.handle(request)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            response = {"jsonrpc": "2.0", "id": request.get("id"),
                        "error": {"code": -32603, "message": "internal error"}}
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
