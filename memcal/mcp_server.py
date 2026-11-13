"""A small MCP server over stdio.

The brief is exposed as a resource for single-read injection; the navigation
tools support follow-up detail lookups.

    python3 -m memcal.mcp_server
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from datetime import timedelta

from . import __version__
from . import (archive, brief, config, db, detail, events, harness, live, presentation,
               series, todos, trace, whois, wiki)

PROTOCOL_VERSION = "2025-06-18"

TOOLS = [
    {
        "name": "memcal_brief",
        "description": ("The always-in-context block: the next 30 days by default, open to-dos, "
                        "questions to ask, and identity aliases. Read this first; open "
                        "relevant handles or pages with memcal_open for details and history."),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "memcal_open",
        "description": (
            'Open a brief handle or wiki page for details and history. Includes labeled citations, nearby context, and capped pending messages. Use memcal_activity only to continue omitted messages. Accepts E#, T#, Q#, page names, aliases, or me.'),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string",
                        "description": "a brief handle (E258, T2), page name, alias, or me"},
            },
            "required": ["ref"], "additionalProperties": False,
        },
    },
    {
        "name": "memcal_search_wiki",
        "description": (
            'Find remembered facts when the page is unknown. Searches names, aliases, fact labels, values, and prose. Use before searching original messages.'),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "name, alias, fact label, value, or prose to find"},
                "limit": {"type": "integer", "description": "max pages; default 10"},
            },
            "required": ["query"], "additionalProperties": False,
        },
    },
    {
        "name": "memcal_list_days",
        "description": ('List a day or date range beyond the brief, using plain date words or YYYY-MM-DD and days. For a year, use YYYY-01-01 with days=365 (366 in a leap year). Weekly overviews within the brief need no lookup.'),
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
        "description": ('List a whole calendar month, YYYY-MM. For one day or a short range use memcal_list_days. Weekly overviews within the brief need no lookup.'),
        "inputSchema": {
            "type": "object",
            "properties": {"month": {"type": "string", "description": "yyyy-mm; omit for this month"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "memcal_search_archive",
        "description": (
            'Search original messages when records and wiki facts do not answer. Filter by person, channel, or date; query may be empty with filters. Results include line_id for reading surrounding conversation.'),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "person": {"type": "string"},
                "channel": {"type": "string"},
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
            "Read full original messages or email when memcal_open's details and linked "
            "evidence are not enough. Open the relevant record with memcal_open first; "
            "use this for a fuller quotation or verification against the source. "
            "Takes the row's stable key, or the short handle the brief prints "
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
            "or a channel and thread."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "line_id": {"type": "integer"},
                "channel": {"type": "string"},
                "thread": {"type": "string"},
                "before": {"type": "integer", "description": "default 12"},
                "after": {"type": "integer", "description": "default 12"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "memcal_activity",
        "description": (
            'Continue pending messages omitted by memcal_open, using its cursor. Messages are nominated for this entry, not every message in the thread. Reading does not review them; cite line ids in a correction or memcal_reviewed.'),
        "inputSchema": {
            "type": "object",
            "properties": {
                "handle": {"type": "string",
                           "description": "a brief handle, e.g. E46"},
                "cursor": {"type": "integer",
                           "description": "an archive id to read after (for paging)"},
                "limit": {"type": "integer", "description": "default 20"},
            },
            "required": ["handle"], "additionalProperties": False,
        },
    },
    # The write half. One verb each, all plain code — the caller is a model that
    # already knows which field it means, so there is nothing left to extract.
    {
        "name": "memcal_add",
        "description": ('Add one private memory event. Use memcal_schedule for recurring events, including several weekdays; do not add each weekday separately. Tentative plans are allowed. Use memcal_update for an existing event.'),
        "inputSchema": {
            "type": "object",
            "properties": {
                              "note": {"type": "string", "description": "Other event details in plain text"},
                "title": {"type": "string"},
                "when": {"type": "string",
                         "description": "'saturday', 'tomorrow', 'this weekend', or yyyy-mm-dd"},
                "time": {"type": "string"},
                "location": {"type": "string", "description": "Place, address, or video-call URL; omit if unknown"},
                "participants": {"type": "array", "items": {"type": "string"}},
                "status": {"type": "string", "enum": list(events.STATUSES)},
                "kind": {"type": "string", "enum": list(events.KINDS)},
                "subject": {"type": "string", "description": "whose thing this is; 'me' usually"},
                "until": {"type": "string", "description": "last day, for multi-day things"},
                "series": {"type": "string", "description": "the recurring thing this is one of, as a slug ('tutoring'). Sets it where repetition alone could not prove it — a new instance then starts with what the series already knows"},
                "field_sources": {
                    "type": "object",
                    "description": "map of field name → archive line ids supporting that field "
                                   "(newest timestamp wins per field). Prefer this over flat "
                                   "source_ids when fields have different supporting messages.",
                    "additionalProperties": {"type": "array", "items": {"type": "integer"}},
                },
                "context_source_ids": {
                    "type": "array", "items": {"type": "integer"},
                    "description": "background archive ids used to interpret the request; "
                                   "no field authority. Only valid with field_sources.",
                },
                "source_ids": {"type": "array", "items": {"type": "integer"},
                               "description": "archive [ids] supporting this creation (flat). "
                                              "Do not combine with field_sources."},
            },
            "required": ["title", "when"], "additionalProperties": False,
        },
    },
    {
        "name": "memcal_update",
        "description": ('Update one event by its E# handle or distinctive title. Omitted fields stay unchanged; an empty string clears an optional detail. Use status declined for cancellation or non-attendance. Use memcal_schedule to change a series. Returns the updated record; no verification read needed.'),
        "inputSchema": {
            "type": "object",
            "properties": {
                "which": {"type": "string", "description": "the row's E# handle when available, otherwise distinctive words"},
                "status": {"type": "string", "enum": list(events.STATUSES)},
                "when": {"type": "string"},
                "until": {"type": "string", "description": "last day, for multi-day things"},
                "time": {"type": "string"},
                "location": {"type": "string", "description": "Place, address, or video-call URL; omit if unknown"},
                "title": {"type": "string"},
                "kind": {"type": "string", "enum": list(events.KINDS)},
                "subject": {"type": "string",
                            "description": "whose thing this is; only to correct a "
                                           "row filed under the wrong person"},
                "add_participants": {"type": "array", "items": {"type": "string"}},
                "remove_participants": {"type": "array", "items": {"type": "string"}},
                "series": {"type": "string", "description": "the recurring thing this is one of, as a slug ('tutoring'). Sets it where repetition alone could not prove it — a new instance then starts with what the series already knows"},
                "note": {"type": "string"},
                "field_sources": {
                    "type": "object",
                    "description": "map of field name → archive line ids supporting that field "
                                   "(newest timestamp wins per field). Prefer this over flat "
                                   "source_ids when fields have different supporting messages.",
                    "additionalProperties": {"type": "array", "items": {"type": "integer"}},
                },
                "context_source_ids": {
                    "type": "array", "items": {"type": "integer"},
                    "description": "background archive ids used to interpret the request; "
                                   "no field authority. Only valid with field_sources.",
                },
                "source_ids": {"type": "array", "items": {"type": "integer"},
                               "description": "archive [ids] from memcal_open or memcal_activity this change is based on "
                                              "(flat oldest-line rule). Do not combine with field_sources."},
            },
            "required": ["which"], "additionalProperties": False,
        },
    },
    {
        "name": "memcal_schedule",
        "description": ('Create or change ONE recurring series. weekdays lists all days: Monday/Wednesday/Friday is one weekly rule. interval repeats every N days/weeks/months. starting dates a change while preserving the earlier schedule. status paused/active/ended pauses, resumes, or stops it. Use memcal_move_once for one occurrence.'),
        "inputSchema": {
            "type": "object",
            "properties": {
                "which": {"type": "string",
                          "description": "the recurring thing, as a name or slug ('tutoring')"},
                "cadence": {"type": "string", "enum": list(series.PUBLIC_CADENCES)},
                "weekdays": {"type": "array", "items": {"type": "string"}, "description": "All repeating days, e.g. [monday, wednesday, friday]; one series"},
                "interval": {"type": "integer", "minimum": 1, "maximum": 366, "description": "Repeat every N days/weeks/months; default 1"},
                "status": {"type": "string", "enum": ["active", "paused", "ended"], "description": "Pause, resume (active), or stop the series, from starting"},
                "weekday": {"type": "string",
                            "description": "'tuesday', for a single weekly day; prefer weekdays"},
                "day_of_month": {"type": "integer", "description": "1-31, for monthly"},
                "time": {"type": "string", "description": "HH:MM"},
                "location": {"type": "string", "description": "Place, address, or video-call URL; omit if unknown"},
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
        "description": ('Move or cancel one occurrence without changing the series. Use an event E# handle, or the series name plus on for a date not yet materialized. Give to for a move; cancelled=true skips it without requiring to.'),
        "inputSchema": {
            "type": "object",
            "properties": {
                              "on": {"type": "string", "description": "Original occurrence date when which names the series"},
                "which": {"type": "string",
                          "description": "the row's E# handle when available, otherwise distinctive words"},
                "to": {"type": "string", "description": "'wednesday' or yyyy-mm-dd"},
                "time": {"type": "string"},
                "cancelled": {"type": "boolean",
                              "description": "true when the week is skipped outright "
                                             "rather than moved"},
            },
            "required": ["which"], "additionalProperties": False,
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
        "description": ("Remember or correct one fact. page='me' stores a fact about "
                        "the user; updating the same fact replaces its value while "
                        "retaining history. One durable fact per call: page='quinn delgado', "
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
        "name": "memcal_name",
        "description": ('Confirm or correct an inferred sender name after the user supplies confirmation or a correction. Do not ask solely because a name is inferred; only an explicitly uncertain identity relevant to the answer warrants a question.'),
        "inputSchema": {
            "type": "object",
            "properties": {
                "guess": {"type": "string",
                          "description": "the guessed name as shown, e.g. 'Tire shop'"},
                "correct": {"type": "string",
                            "description": "the real name, only if the guess was wrong"},
            },
            "required": ["guess"], "additionalProperties": False,
        },
    },
    {
        "name": "memcal_answer",
        "description": ("Record what the user said about one of the 'Ask about' questions, "
                        "or that an 'Open' to-do is done, so it stops being asked. "
                        "Resolves questions and to-dos alike; repeating something "
                        "already settled still counts as recorded."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "any distinctive words from it"},
                "answer": {"type": "string"},
            },
            "required": ["question", "answer"], "additionalProperties": False,
        },
    },
    {
        "name": "memcal_reviewed",
        "description": ("Mark activity lines as reviewed with no change to the row — the "
                        "stored plan still stands after reading them. Only the cited "
                        "lines stop raising hints. Cite the [ids] memcal_open or "
                        "memcal_activity returned."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "handle": {"type": "string",
                           "description": "a brief handle, e.g. E46"},
                "source_ids": {"type": "array", "items": {"type": "integer"},
                               "description": "archive line ids this review covered"},
            },
            "required": ["handle", "source_ids"], "additionalProperties": False,
        },
    },
    {
        "name": "memcal_refresh",
        "description": ("Re-render the current brief on demand — the same snapshot "
                        "prefetch injects, without model work. For turns where "
                        "injection may have been skipped or source state changed "
                        "mid-turn."),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
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
        # `evidence` marks lines the fact was built from; other lines are context.
        quotes = [row for row in rows if row["evidence"]][:2]
        # Pre-citation rows carry whole-bundle evidence; quoting from them
        # would misattribute, so direct those to `memcal_source` instead.
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


def _render_search_hits(hits: list[dict], query: str, limit: int) -> str:
    """Bounded wiki-fact matches as text: page refs, facts, and provenance.

    Facts lead; provenance follows per hit. Bounded: `len(hits) == limit`
    means truncation is possible.
    """
    lines = [f"Found {len(hits)} page(s) for {query!r}:"]
    for hit in hits:
        matched = ", ".join(hit.get("matched") or [])
        lines.append(f"\n- {hit['slug']} ({hit.get('section', '')})"
                     + (f" [matched: {matched}]" if matched else ""))
        facts = hit.get("facts") or {}
        for slot, info in facts.items():
            lines.append(f"  - **{slot}**: {info.get('value', '')}")
        for key, info in (hit.get("context") or {}).items():
            lines.append(f"  - {key}: {info.get('value', '')}")
        prov = hit.get("provenance") or {}
        if prov:
            bits = []
            for slot, meta in prov.items():
                src = (meta or {}).get("source") or "?"
                ts = str((meta or {}).get("ts") or "")[:10]
                bits.append(f"{slot} ({src}{', ' + ts if ts else ''})")
            lines.append(f"  provenance: {'; '.join(bits)}")
        lines.append(f"  open with memcal_open ref='{hit['slug']}'")
    if len(hits) == limit:
        lines.append("(bounded: more may exist; narrow the query or raise limit)")
    return "\n".join(lines) + "\n"


#: Every tool that writes. `Server.call` and `Server._write` both route on
#: this set, so advertised tools and handlers cannot diverge.
WRITE_TOOLS = frozenset({
    "memcal_add", "memcal_update", "memcal_schedule", "memcal_move_once",
    "memcal_merge", "memcal_drop", "memcal_todo", "memcal_note", "memcal_alias",
    "memcal_reviewed", "memcal_name",
})


class Server:
    #: Class defaults so a surface bound by hand — a test store, an embedded caller —
    #: still answers `origin()` rather than raising on an attribute `__init__` set.
    harness = "openclaw"
    session = ""

    def __init__(self):
        self.cfg = config.load()
        self.cfg.ensure_dirs()
        self.conn = db.open_db(self.cfg.db_path)
        # Which conversation this process is serving, when the surrounding harness says
        # so. Set by the client that also archives the user's turns — see
        # `harness.archive_user_turn`, which files them under `<harness>:<session>`.
        self.harness = os.environ.get("MEMCAL_HARNESS") or "openclaw"
        self.session = os.environ.get("MEMCAL_SESSION") or ""

    def origin(self) -> live.Origin:
        """The turn behind the tool call now being served, when it can be known.

        A stdio MCP server is handed a tool call and nothing about why. When the client
        tells it which session it is, the session's own most recent archived turn is a
        sound answer and is scoped so an overlapping session cannot borrow it. When it
        does not, this records that the operation had no available originating turn
        rather than attaching the newest sentence in the store to it.
        """
        if not self.session:
            return live.Origin.of("mcp", note="mcp client supplied no session id")
        return live.Origin.of(
            "mcp",
            harness.recent_turn(self.conn, harness=self.harness,
                                session_id=self.session),
            session=self.session)

    def _close_todo_for_answer(self, which: str) -> bool:
        """Close the to-do `memcal_answer` resolved, with a provenance stamp.

        `live.close_todo` raises when nothing matches; here that is not an error —
        it just means the needle was a question, not a to-do — so it maps to False
        and `resolve` reports "no matching open question".
        """
        try:
            live.close_todo(self.conn, self.cfg, which, origin=self.origin())
            return True
        except live.LiveError:
            return False

    @staticmethod
    def _cited_ids(args: dict) -> list[int]:
        """Archive ids the caller claims to have read. Validated at the write."""
        raw = args.get("source_ids") or []
        if isinstance(raw, str):
            raw = raw.replace(",", " ").split()
        try:
            return [int(part) for part in raw]
        except (TypeError, ValueError):
            raise ValueError("source_ids must be archive line ids, e.g. [12, 13]")

    def _cited(self, args: dict) -> live.Origin:
        """This turn's origin plus the activity lines the caller actually opened."""
        base = self.origin()
        extra = self._cited_ids(args)
        return live.Origin.of(
            base.surface, base.archive_ids, cited=extra, session=base.session,
            note=base.note if base.sourced or not extra
            else "cited activity lines", op_id=base.op_id)

    # ------------------------------------------------------------------ tools --
    def call(self, name: str, args: dict) -> str:
        if name == "memcal_brief":
            return brief.render(self.conn, self.cfg)

        if name == "memcal_refresh":
            return brief.render(self.conn, self.cfg)

        if name == "memcal_activity":
            from . import activity as activity_mod
            try:
                kind, ref = activity_mod.resolve_handle(
                    self.conn, str(args.get("handle", "")))
            except LookupError as exc:
                return str(exc)
            try:
                cursor = int(args.get("cursor") or 0)
            except (TypeError, ValueError):
                return "cursor must be an archive id number"
            try:
                limit = max(1, min(50, int(args.get("limit") or 20)))
            except (TypeError, ValueError):
                return "limit must be a number"
            page = activity_mod.read(self.conn, kind, ref, cursor=cursor,
                                     limit=limit, relevant_only=True)
            weak = activity_mod.associations(self.conn, kind, ref)["weak"]
            return activity_mod.format_read(
                page, weak, label=str(args.get("handle", "")).strip())

        if name == "memcal_open_page":
            return self.call("memcal_open", {"ref": args.get("slug", "")})

        if name == "memcal_open":
            raw = str(args.get("ref", "") or "")
            if detail.parse_handle(raw):
                return detail.open_handle(self.conn, self.cfg, raw)
            try:
                self_target = wiki.resolve_self_page(
                    self.conn, self.cfg.wiki_dir, raw)
            except wiki.SelfAmbiguous as exc:
                cands = ", ".join(exc.candidates)
                return (f"Ambiguous self page for {raw!r}: {cands}. "
                        f"Open one explicitly, e.g. memcal_open with "
                        f"ref='{exc.candidates[0]}'. Nothing was created or "
                        f"merged; writes need an explicit page.")
            slug = (self_target if self_target is not None
                    else wiki.canonical(self.cfg.wiki_dir, raw))
            profile = (wiki.profile(self.conn, self.cfg.wiki_dir, slug)
                       if slug else None)
            if not profile:
                known = ", ".join(wiki.list_pages(self.cfg.wiki_dir)) or "(none yet)"
                near = wiki.near_pages(self.cfg.wiki_dir, raw)
                hint = (f" Did you mean: {', '.join(near)}? "
                        f"Open one explicitly." if near else "")
                if self_target == "me":
                    return (f"No self page yet for {raw!r}. Pages that exist: "
                            f"{known}.{hint} Reads do not create pages; the first "
                            f"memcal_note with page='me' will create it. Try "
                            f"memcal_search_wiki with a name, alias, fact label or "
                            f"value, or memcal_search_archive for original statements.")
                return (f"No page for {raw!r}. Pages that exist: {known}.{hint} "
                        f"Try memcal_search_wiki with a name, alias, fact label or "
                        f"value; or memcal_search_archive for original statements.")
            return _render_page(profile)

        if name == "memcal_search_wiki":
            query = str(args.get("query", "") or "")
            try:
                limit = int(args.get("limit") or 10)
            except (TypeError, ValueError):
                limit = 10
            limit = max(1, min(limit, 20))
            if not query.strip():
                return ("Give a query to search the wiki — a name, alias, fact "
                        "label, value, or prose. An empty query matches nothing.")
            hits = wiki.search_facts(self.cfg.wiki_dir, query, limit)
            if not hits:
                return presentation.wiki_search_miss(query)
            return _render_search_hits(hits, query, limit)

        if name == "memcal_list_days":
            start, span = db.parse_when(args.get("when", ""))
            span = max(1, int(args.get("days") or span))
            end = start + timedelta(days=span - 1)
            series.materialize_between(self.conn, start, end)
            rows = events.overlapping(self.conn, start.isoformat(), end.isoformat())
            label = (start.strftime("%A %b %-d, %Y") if span == 1
                     else f"{start.isoformat()} – {end.isoformat()}")
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
            series.materialize_between(self.conn, start, end)
            rows = events.overlapping(self.conn, start.isoformat(), end.isoformat())
            head = f"memcal {start.strftime('%B %Y')} ({len(rows)} rows)"
            return ("\n".join([head] + [f"{r.one_line()}  {brief.source_tag('event', r.id)}"
                                           for r in rows]) if rows else head + "\n(nothing)")

        if name == "memcal_search_archive":
            rows = archive.search_filtered(
                self.conn, args.get("query", ""), limit=int(args.get("limit") or 20),
                person=args.get("person", ""), channel=args.get("channel", ""),
                since=args.get("since", ""), until=args.get("until", ""))
            if not rows:
                return "(nothing found)"
            out = []
            for row in rows:
                who = "me" if row["from_me"] else (row["person"] or row["handle"] or "?")
                own = ("  <- " + presentation.SELF_WRITTEN_NOTE
                       if presentation.self_written(row["channel"]) else "")
                out.append(f"[{row['id']}] {str(row['ts'])[:16]}  {row['channel']}/{who}: "
                           f"{row['text'][:300]}{own}")
            return ("(the number in brackets is a line_id — pass it to "
                    "memcal_conversation to read around it)\n" + "\n".join(out))

        if name == "memcal_conversation":
            channel, thread = args.get("channel", ""), args.get("thread", "")
            around = ""
            if args.get("line_id"):
                anchor = trace.line(self.conn, int(args["line_id"]))
                if not anchor:
                    return f"no line {args['line_id']}"
                channel, thread, around = anchor["channel"], anchor["thread"], anchor["ts"]
            if not (channel and thread):
                return "give a line_id, or a channel and thread"
            lines = trace.conversation(
                self.conn, channel=channel, thread=thread, around=around,
                before=int(args.get("before") or 12), after=int(args.get("after") or 12))
            if not lines:
                return "(nothing in that conversation)"
            return "\n".join(f"[{line['id']}] {line['ts']}  {line['who']}: {line['text']}"
                             for line in lines)

        if name == "memcal_source":
            # Agents need the full invitation body; search truncates hits.
            ref, kind = args.get("ref", ""), args.get("kind") or "event"
            # Accept the brief's `〔E119〕` spelling directly.
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
                # `evidence` marks lines the row was built from; the rest is
                # neighbouring context for readability.
                mark = "*" if row.get("evidence") else " "
                # Untruncated: the dropped detail is the reason to prefer this
                # over `memcal_search_archive`.
                if row.get("source_heading"):
                    out.append(f"--- {row['source_heading']} ---")
                own = ("\n(" + presentation.SELF_WRITTEN_NOTE + ")"
                       if presentation.self_written(row["channel"]) else "")
                out.append(f"{mark} [{row['id']}] {row['channel']} · {row['ts'][:16]} · "
                           f"{row['who']}{own}\n{row['text']}")
            return ("(* = a line this row was built from; others are nearby context. "
                    "[n] is a line_id for memcal_conversation)\n\n" + "\n\n".join(out))

        if name in WRITE_TOOLS:
            try:
                return self._write(name, args)
            except (live.LiveError, ValueError) as exc:
                # Named `fields` to avoid shadowing the imported `detail` module.
                fields = getattr(exc, "detail", {}) or {}
                extra = "".join(f"\n  {k}: {v}" for k, v in fields.items())
                return f"{exc}{extra}"

        if name == "memcal_answer":
            # Same verb as the Hermes surface and the CLI: a conversational close
            # resolves a question or a to-do, and an already-settled repeat counts.
            # Answering a question stamps nothing (like its Hermes twin), but closing
            # a to-do goes through `live.close_todo` so the closure carries the same
            # origin/action record a `memcal_todo done` would — never a silent close.
            ok, _kind = todos.resolve(
                self.conn, args.get("question", ""), args.get("answer", ""),
                close_todo=self._close_todo_for_answer)
            brief.write(self.conn, self.cfg)
            return "recorded" if ok else "no matching open question"

        raise ValueError(f"unknown tool {name}")

    def _write(self, name: str, args: dict) -> str:
        """The typed writes. Each returns the resulting row, so the caller never has to
        read the calendar back to find out whether its edit landed."""
        if name not in WRITE_TOOLS:
            # Reject unknown tools explicitly.
            raise ValueError(f"unknown tool {name}")
        conn, cfg = self.conn, self.cfg
        if name == "memcal_add":
            origin = self.origin()
            if args.get("source_ids"):
                origin = self._cited(args)
            outcome = live.add_event(
                conn, cfg, origin=origin,
                title=args.get("title", ""), when=args.get("when", ""),
                time=args.get("time"), location=args.get("location"),
                participants=args.get("participants") or [], status=args.get("status"),
                kind=args.get("kind"), subject=args.get("subject"), until=args.get("until"),
                note=args.get('note'), join_url=args.get("join_url"), series=args.get("series"),
                field_sources=args.get("field_sources"),
                context_source_ids=args.get("context_source_ids"))
            event, verb = outcome.event, outcome.verb
            if outcome.fields:
                detail = "\n".join(outcome.summary_lines())
                return f"{verb}: {event.one_line()}\n{detail}"
            return f"{verb}: {event.one_line()}"
        if name == "memcal_update":
            origin = (self._cited(args) if args.get("source_ids")
                      and not args.get("field_sources") else self.origin())
            if args.get("field_sources") and args.get("source_ids"):
                raise live.LiveError(
                    "pass field_sources or source_ids, not both — a flat citation set "
                    "cannot be combined with per-field attribution")
            outcome = live.update_event(
                conn, cfg, args.get("which", ""), origin=origin,
                status=args.get("status"),
                when=args.get("when"), until=args.get("until"), time=args.get("time"),
                location=args.get("location"), title=args.get("title"),
                kind=args.get("kind"), subject=args.get("subject"),
                note=args.get("note"), join_url=args.get("join_url"),
                series=args.get("series"),
                add_participants=args.get("add_participants") or [],
                remove_participants=args.get("remove_participants") or [],
                field_sources=args.get("field_sources"),
                context_source_ids=args.get("context_source_ids"))
            event = outcome.event
            if outcome.fields and (outcome.rejected or args.get("field_sources")):
                detail = "\n".join(outcome.summary_lines())
                return f"{event.one_line()}\n{detail}"
            changed = outcome.changed_lines
            if changed:
                return event.one_line() + "\nchanged: " + "; ".join(changed)
            if outcome.rejected:
                detail = "\n".join(outcome.summary_lines())
                return f"{event.one_line()}\n{detail}"
            return event.one_line() + "\nchanged: nothing — it already said that"
        if name == "memcal_schedule":
            rule, log = live.set_schedule(
                conn, cfg, args.get("which", ""), origin=self.origin(),
                cadence=args.get("cadence"),
                weekday=args.get("weekday"), day_of_month=args.get("day_of_month"),
                weekdays=args.get('weekdays'), interval=args.get('interval'), status=args.get('status'),
                time=args.get("time"), location=args.get("location"),
                join_url=args.get("join_url"), starting=args.get("starting"),
                ended=bool(args.get("ended")))
            return (f"{rule.title}: {rule.phrase}, from {rule.effective_on}"
                    + ("\n" + "\n".join(log) if log else ""))
        if name == "memcal_move_once":
            event, replaced = live.move_one_occurrence(
                conn, cfg, args.get("which", ""), origin=self.origin(),
                to=args.get("to", ""), on=args.get('on'),
                time=args.get("time"), cancelled=bool(args.get("cancelled")))
            return (f"{event.one_line()}\nthis week only — the {replaced} session; "
                    f"the schedule is unchanged")
        if name == "memcal_merge":
            return "merged: " + live.merge_events(
                conn, cfg, args.get("keep", ""), args.get("drop", ""),
                origin=self.origin()).one_line()
        if name == "memcal_drop":
            return "deleted: " + live.drop_event(conn, cfg, args.get("which", ""),
                                                 origin=self.origin())
        if name == "memcal_todo":
            if args.get("done"):
                return ("closed: " + live.close_todo(
                    conn, cfg, args.get('text', ''), origin=self.origin()).text)
            todo, verb = live.open_todo(conn, cfg, args.get("text", ""),
                                        origin=self.origin(), due=args.get("due"),
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
        if name == "memcal_reviewed":
            return live.reviewed(conn, cfg, args.get("handle", ""),
                                 origin=self._cited(args))
        if name == "memcal_name":
            return whois.settle_guess(conn, args.get("guess", ""), args.get("correct"))
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
                "serverInfo": {"name": "memcal", "version": __version__},
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
                "description": "Upcoming month by default, open to-dos, questions, and identity aliases.",
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


def _reexec_under_app() -> None:
    """Keep the CLI-compatible bundle wrapper. Calendar tools still use the launchd
    app bridge: direct re-exec retains the host's responsible-process identity.
    No-op when already under the bundle or when none is built."""
    if os.environ.get("MEMCAL_APP") == "1":
        return
    from . import schedule  # noqa: PLC0415 - avoid importing launchd code at module load
    if not schedule._is_macos():
        return
    exe = schedule.app_executable(config.load())
    if not (exe.is_file() and os.access(exe, os.X_OK)):
        return
    os.environ["MEMCAL_APP"] = "1"
    try:
        os.execv(str(exe), [str(exe), sys.executable, "-m", "memcal.mcp_server"])
    except OSError:
        os.environ.pop("MEMCAL_APP", None)


def main() -> int:
    _reexec_under_app()
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
