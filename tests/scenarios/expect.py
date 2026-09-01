"""Verification rules and metrics for scenario evaluation.

Defines checks and measures asserted against stored state (events, todos, questions,
wiki pages, brief output, and evidence provenance) across simulated scenario days:

  Check     Binary pass/fail assertion on stored state after a specific scenario day.
  Measure   Informational quantitative metric tracking counts and rates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from memcal import brief as brief_mod
from memcal import db, detail, events, todos, trace, wiki


# --------------------------------------------------------------------- context --

class Ctx:
    """Evaluation context providing state inspection and regex-based lookups."""

    def __init__(self, conn, cfg):
        self.conn = conn
        self.cfg = cfg

    # -- events --
    def rows(self, pattern: str, *, on: str | None = None,
             include_declined: bool = True) -> list[events.Event]:
        rx = re.compile(pattern, re.I)
        out = []
        for row in self.conn.execute("SELECT * FROM events ORDER BY date"):
            event = events.Event.from_row(row)
            if not rx.search(event.title or ""):
                continue
            if on and event.date != on:
                continue
            if not include_declined and event.status == "declined":
                continue
            out.append(event)
        return out

    def one(self, pattern: str, **kw) -> events.Event | None:
        found = self.rows(pattern, **kw)
        return found[0] if len(found) == 1 else None

    def history(self, event: events.Event, field_name: str) -> list[tuple[str, str]]:
        return [(r["old_value"], r["new_value"]) for r in self.conn.execute(
            "SELECT * FROM event_history WHERE event_id = ? AND field = ?"
            " ORDER BY id", (event.id, field_name))]

    # -- todos / questions --
    def todos(self, pattern: str) -> list:
        rx = re.compile(pattern, re.I)
        return [t for t in (todos.Todo.from_row(r) for r in self.conn.execute(
            "SELECT * FROM todos")) if rx.search(t.text or "")]

    def questions(self, pattern: str, status: str | None = "open") -> list:
        """Return questions matching pattern, filtered by status or all if status is None."""
        rx = re.compile(pattern, re.I)
        sql, args = "SELECT * FROM questions", ()
        if status:
            sql, args = sql + " WHERE status = ?", (status,)
        return [r for r in self.conn.execute(sql, args)
                if rx.search(str(r["text"] or ""))]

    def question(self, pattern: str):
        """Return a single matching question row across all statuses, or None."""
        found = self.questions(pattern, status=None)
        return found[0] if len(found) == 1 else None

    def standing(self, pattern: str) -> list:
        rx = re.compile(pattern, re.I)
        return [r for r in self.conn.execute("SELECT * FROM standing")
                if rx.search(str(r["value"] or ""))]

    # -- wiki --
    def pages(self) -> list[str]:
        return wiki.list_pages(self.cfg.wiki_dir)

    def page(self, slug: str):
        return wiki.read(self.cfg.wiki_dir, slug)

    def slot_text(self, slug: str) -> str:
        page = self.page(slug)
        if not page:
            return ""
        return " ".join(f"{k} {v.get('value', '')}" for k, v in page.slots.items())

    # -- the gate --
    def spooled(self, pattern: str) -> list:
        """Return spooled archive rows matching pattern."""
        rx = re.compile(pattern, re.I)
        return [r for r in self.conn.execute(
            "SELECT a.* FROM archive a JOIN spool s ON s.archive_id = a.id")
            if rx.search(str(r["text"] or ""))]

    def gate_reason(self, pattern: str) -> str:
        rx = re.compile(pattern, re.I)
        for row in self.conn.execute("SELECT * FROM archive"):
            if rx.search(str(row["text"] or "")):
                return f"{'passed' if row['gated'] else 'blocked'}:{row['gate_reason']}"
        return "(not archived)"

    # -- brief --
    def brief(self) -> str:
        return brief_mod.render(self.conn, self.cfg)


@dataclass
class Check:
    id: str
    challenge: str
    day: int
    fn: Callable[[Ctx], tuple[bool, str]]
    #: Soft check indicating design preference rather than strict defect.
    soft: bool = False
    #: Frontier check tracking aspirational capabilities or known gaps.
    frontier: bool = False


@dataclass
class Measure:
    id: str
    label: str
    day: int
    fn: Callable[[Ctx], str]


# ------------------------------------------------------------------- shorthands --

def _found(rows, what: str) -> tuple[bool, str]:
    if len(rows) == 1:
        return True, f"{what}: {rows[0].date} {rows[0].title!r} [{rows[0].status}]"
    if not rows:
        return False, f"{what}: no row found"
    return False, (f"{what}: {len(rows)} rows, expected 1 — "
                   + "; ".join(f"{r.date} {r.title!r}" for r in rows[:4]))


def one_row(pattern, **kw):
    def run(ctx):
        return _found(ctx.rows(pattern, **kw), f"/{pattern}/")
    return run


def row_on(pattern, date, **kw):
    def run(ctx):
        rows = ctx.rows(pattern, on=date, **kw)
        ok, note = _found(rows, f"/{pattern}/")
        if not ok:
            return ok, note
        got = rows[0].date
        return got == date, f"date {got}, wanted {date}"
    return run


def field_is(pattern, name, wanted, **kw):
    def run(ctx):
        rows = ctx.rows(pattern, **kw)
        ok, note = _found(rows, f"/{pattern}/")
        if not ok:
            return ok, note
        got = getattr(rows[0], name, None)
        if isinstance(wanted, str) and wanted.startswith("~"):
            hit = bool(re.search(wanted[1:], str(got or ""), re.I))
            return hit, f"{name} = {got!r}, wanted ~{wanted[1:]}"
        return got == wanted, f"{name} = {got!r}, wanted {wanted!r}"
    return run


def publishes_location(pattern, wanted, **kw):
    """What memcal would put in the calendar entry's `location`, for this row.

    Graded on the composed value rather than on the column, because the column was never
    the bug: `location` and `join_url` were both correct in the store and the calendar
    entry their phone shows was built from one of them. The check runs `publish_location`
    itself, so it costs no Apple Event and no model call and never touches a real
    calendar — invariant 11 means no test may ask for one.
    """
    from memcal.sources import ical

    def run(ctx):
        rows = ctx.rows(pattern, **kw)
        ok, note = _found(rows, f"/{pattern}/")
        if not ok:
            return ok, note
        got = ical.publish_location(rows[0].location, rows[0].join_url)
        if isinstance(wanted, str) and wanted.startswith("~"):
            hit = bool(re.search(wanted[1:], got, re.I))
            return hit, f"published location = {got!r}, wanted ~{wanted[1:]}"
        return got == wanted, f"published location = {got!r}, wanted {wanted!r}"
    return run


def changed(pattern, name, frm, to, **kw):
    """The change is recorded, not merely the end state. Recency resolved at write."""
    def run(ctx):
        rows = ctx.rows(pattern, **kw)
        ok, note = _found(rows, f"/{pattern}/")
        if not ok:
            return ok, note
        moves = ctx.history(rows[0], name)
        for old, new in moves:
            if (frm is None or frm in str(old)) and to in str(new):
                return True, f"{name}: {old} -> {new}"
        return False, (f"{name} never recorded {frm} -> {to}; history = "
                       + (", ".join(f"{o}->{n}" for o, n in moves) or "empty"))
    return run


def no_row(pattern, **kw):
    def run(ctx):
        rows = ctx.rows(pattern, **kw)
        return (not rows,
                "none, correct" if not rows
                else f"{len(rows)} unwanted: " + "; ".join(
                    f"{r.date} {r.title!r}" for r in rows[:4]))
    return run


def count_rows(pattern, n, **kw):
    def run(ctx):
        rows = ctx.rows(pattern, **kw)
        return len(rows) == n, (f"{len(rows)} rows, wanted {n}: "
                                + "; ".join(f"{r.date} {r.title!r}" for r in rows[:5]))
    return run


def no_page_matching(pattern):
    def run(ctx):
        rx = re.compile(pattern, re.I)
        hits = [s for s in ctx.pages() if rx.search(s)]
        return not hits, "none, correct" if not hits else f"unwanted pages: {hits}"
    return run


def slot_says(slug, pattern):
    def run(ctx):
        text = ctx.slot_text(slug)
        return (bool(re.search(pattern, text, re.I)),
                f"{slug} slots: {text[:120] or '(none)'}")
    return run


def slot_changed(slug, frm, to):
    """The wiki's answer to `changed()`. Deliberately slot-name agnostic: the model
    picks the field name, and what is being asserted is that the old value was kept
    somewhere, not what it was filed under."""
    def run(ctx):
        moves = [(r["slot"], r["old_value"], r["new_value"])
                 for r in wiki.slot_history(ctx.conn, slug)]
        for slot, old, new in moves:
            if re.search(frm, str(old or ""), re.I) and re.search(to, str(new or ""), re.I):
                return True, f"{slot}: {old} -> {new}"
        return False, (f"{slug} never recorded {frm} -> {to}; history = "
                       + (", ".join(f"{s}: {o}->{n}" for s, o, n in moves) or "empty"))
    return run


def slot_never_says(slug, pattern):
    def run(ctx):
        page = ctx.page(slug)
        if not page:
            return True, f"{slug}: no page at all, correct"
        text = ctx.slot_text(slug) + " " + (page.body or "")
        hit = re.search(pattern, text, re.I)
        return not hit, ("clean" if not hit
                         else f"{slug} claims {hit.group(0)!r} in: {text[:140]}")
    return run


def brief_lacks(pattern):
    def run(ctx):
        text = ctx.brief()
        hit = re.search(pattern, text, re.I)
        return not hit, "clean" if not hit else f"brief contains {hit.group(0)!r}"
    return run


def brief_has(pattern):
    def run(ctx):
        text = ctx.brief()
        return (bool(re.search(pattern, text, re.I)),
                f"brief {'contains' if re.search(pattern, text, re.I) else 'is missing'}"
                f" /{pattern}/")
    return run


def opens_with(row_pattern, detail_pattern):
    """The brief names the row, and its handle opens to the detail.

    The check the index/detail split actually needs, and strictly stronger than the
    `brief_has` it replaces. Asserting a street address appears *on the brief line* was
    only ever one way of asserting it was reachable, and now it is the wrong one: the
    line is an index entry and the address is behind the handle. This form fails if the
    row vanishes, if it loses its handle, if the handle stops resolving, **or** if the
    detail is missing — where the old one could see only the last.
    """
    def run(ctx):
        text = ctx.brief()
        rows = [line for line in text.splitlines()
                if re.search(row_pattern, line, re.I)]
        if not rows:
            return False, f"brief never names /{row_pattern}/"
        found = brief_mod.SOURCE_RE.search(rows[0])
        if not found:
            return False, f"/{row_pattern}/ is on the brief with no handle to open"
        opened = detail.open_handle(ctx.conn, ctx.cfg, found.group(1))
        ok = bool(re.search(detail_pattern, opened, re.I | re.S))
        return ok, (f"{found.group(1)} opens"
                    if ok else
                    f"{found.group(1)} opens without /{detail_pattern}/")
    return run


def every_handle_opens(ctx):
    """No dead ends. Every handle the brief prints has to resolve to a record.

    Cheap, and it earns its place immediately: writing the legend that tells a reader
    what handles are for, with `〔E12〕` as the example, put two unresolvable handles in
    every brief — indistinguishable from real ones to `SOURCE_RE`, to the web UI's
    chips, and to any agent that tried to follow one.
    """
    text = ctx.brief()
    broken = []
    for token in brief_mod.SOURCE_RE.findall(text):
        opened = detail.open_handle(ctx.conn, ctx.cfg, token)
        if opened.lstrip().startswith("(") or "no row" in opened[:80].lower():
            broken.append(token)
    return not broken, ("every handle opens" if not broken
                        else f"dead handles: {', '.join(broken)}")


def every_row_has_a_handle(ctx):
    """A brief line a reader cannot open is a fact they cannot check or correct.

    Skips headers, the bracketed notes the brief writes about itself, and the People and
    facts block, which is a wiki index rather than one row.
    """
    missing = []
    for line in ctx.brief().splitlines():
        stripped = line.strip()
        if (not stripped or stripped.startswith(("#", "[", "Pages:"))
                or stripped.startswith("Casey")):
            continue
        if not brief_mod.SOURCE_RE.search(line):
            missing.append(stripped[:48])
    return not missing, ("every row opens" if not missing
                         else f"unopenable: {missing}")


def pages_name_what_they_hold(ctx):
    line = next((l for l in ctx.brief().splitlines() if l.startswith("Pages: ")), "")
    if not line:
        return False, "the brief has no Pages line"
    silent = []
    for slug in ctx.pages():
        page = ctx.page(slug)
        if not page or not page.slots:
            continue
        if not any(slot.lower() in line.lower() for slot in page.slots):
            silent.append(f"{slug}({', '.join(page.slots)})")
    return not silent, ("every page names its facts" if not silent
                        else f"named but not described: {'; '.join(silent)}")


def source_says(event_pattern, source_pattern, **kw):
    """The row can lead straight back to the relevant original line."""
    def run(ctx):
        row = ctx.one(event_pattern, **kw)
        if not row:
            return False, f"no /{event_pattern}/ row"
        sources = trace.source_rows(ctx.conn, "event", row.key)
        matching = [r for r in sources if r["evidence"]
                    and re.search(source_pattern, r["text"], re.I)]
        return bool(matching), (
            f"{len(matching)} matching source line(s); "
            f"evidence = {[r['text'][:80] for r in sources if r['evidence']]}")
    return run


def todo_source_says(todo_pattern, source_pattern):
    def run(ctx):
        found = ctx.todos(todo_pattern)
        if len(found) != 1:
            return False, f"{len(found)} to-dos match /{todo_pattern}/"
        rows = trace.source_rows(ctx.conn, "todo", found[0].key)
        evidence = [r["text"] for r in rows if r["evidence"]]
        return (any(re.search(source_pattern, text, re.I) for text in evidence),
                f"evidence = {evidence[:4]}")
    return run


def question_source_says(question_pattern, source_pattern):
    def run(ctx):
        found = ctx.questions(question_pattern)
        if len(found) != 1:
            return False, f"{len(found)} open questions match /{question_pattern}/"
        rows = trace.source_rows(ctx.conn, "question", found[0]["key"])
        evidence = [r["text"] for r in rows if r["evidence"]]
        return (any(re.search(source_pattern, text, re.I) for text in evidence),
                f"evidence = {evidence[:4]}")
    return run


def wiki_source_says(slug, slot_pattern, source_pattern):
    def run(ctx):
        profile = wiki.profile(ctx.conn, ctx.cfg.wiki_dir, slug) or {}
        sources = profile.get("sources") or {}
        matching = []
        for slot, rows in sources.items():
            if re.search(slot_pattern, slot, re.I):
                matching.extend(r["text"] for r in rows if r["evidence"])
        return (any(re.search(source_pattern, text, re.I) for text in matching),
                f"{slug}/{slot_pattern} evidence = {matching[:4]}")
    return run


def event_current_source_first(event_pattern, current_pattern, **kw):
    """The first evidence line should explain the row as it stands now."""
    def run(ctx):
        event = ctx.one(event_pattern, **kw)
        if not event:
            return False, f"no /{event_pattern}/ row"
        evidence = [r["text"] for r in trace.source_rows(
            ctx.conn, "event", event.key) if r["evidence"]]
        return (bool(evidence and re.search(current_pattern, evidence[0], re.I)),
                f"first evidence = {evidence[0] if evidence else '(none)'}")
    return run


def only_expected_events(specs):
    """No silently invented row, even when its title evades a targeted no-row regex."""
    def run(ctx):
        rows = [events.Event.from_row(r) for r in ctx.conn.execute(
            "SELECT * FROM events ORDER BY date, id")]
        unknown = []
        for event in rows:
            if not any(event.date == on and re.search(pattern, event.title, re.I)
                       for pattern, on in specs):
                unknown.append(f"{event.date} {event.title!r}")
        return (not unknown,
                "all rows belong to a declared beat" if not unknown
                else f"unexpected rows: {unknown[:8]}")
    return run


def only_expected_text_rows(fetch, patterns, label):
    def run(ctx):
        values = [str(value) for value in fetch(ctx)]
        unknown = [value for value in values
                   if not any(re.search(pattern, value, re.I) for pattern in patterns)]
        return (not unknown,
                f"all {label} belong to a declared beat" if not unknown
                else f"unexpected {label}: {unknown[:8]}")
    return run


def only_expected_pages(slugs):
    def run(ctx):
        actual = set(ctx.pages())
        extra = sorted(actual - set(slugs))
        return (not extra,
                f"pages={sorted(actual)}" if not extra else f"unexpected pages={extra}")
    return run


def only_expected_wiki_values(allowed):
    def run(ctx):
        unknown = []
        for slug in ctx.pages():
            page = ctx.page(slug)
            patterns = allowed.get(slug, [])
            for slot, data in (page.slots if page else {}).items():
                value = str(data.get("value") or "")
                if not any(re.search(pattern, value, re.I) for pattern in patterns):
                    unknown.append(f"{slug}.{slot}={value!r}")
        return (not unknown,
                "all wiki values belong to a declared beat" if not unknown
                else f"unexpected wiki values: {unknown[:8]}")
    return run


def only_expected_aliases(allowed):
    def run(ctx):
        unknown = []
        for slug in ctx.pages():
            page = ctx.page(slug)
            patterns = allowed.get(slug, [])
            for alias in page.aliases if page else []:
                if not any(re.fullmatch(pattern, alias, re.I) for pattern in patterns):
                    unknown.append(f"{slug}={alias!r}")
        return (not unknown,
                "all aliases belong to a declared beat" if not unknown
                else f"unexpected aliases: {unknown[:8]}")
    return run


def brief_sources_open(ctx):
    """Every product row has a handle, and every handle resolves to its stored row."""
    text = ctx.brief()
    data_lines = []
    for line in text.splitlines():
        if not line or line.startswith(("## ", "[", "(")) or line.startswith("Pages:"):
            continue
        data_lines.append(line)
    missing = [line for line in data_lines if not brief_mod.SOURCE_RE.search(line)]
    tokens = brief_mod.SOURCE_RE.findall(text)
    broken = [token for token in tokens
              if trace.resolve_source(ctx.conn, token).get("error")]
    ok = bool(tokens) and not missing and not broken
    return ok, f"{len(tokens)} handles; missing={missing[:2]}; broken={broken[:2]}"


def every_wiki_fact_has_source(ctx):
    missing = []
    for slug in ctx.pages():
        page = ctx.page(slug)
        profile = wiki.profile(ctx.conn, ctx.cfg.wiki_dir, slug) or {}
        sources = profile.get("sources") or {}
        for slot in (page.slots if page else {}):
            if not any(row["evidence"] for row in sources.get(slot, [])):
                missing.append(f"{slug}.{slot}")
        for alias in page.aliases if page else []:
            rows = trace.source_rows(
                ctx.conn, "wiki", f"{slug}:alias:{db.slugify(alias)}")
            if not any(row["evidence"] for row in rows):
                missing.append(f"{slug}:alias:{alias}")
    return not missing, f"missing source for {missing[:8]}"


def gate_corpus_stats(ctx) -> dict:
    """How much labelled signal/noise reached the queue.

    This is measured rather than graded because a signal acknowledgement often belongs
    in thread context without deserving its own spool row. The trend still catches a
    gate edit that doubles spend while the final calendar happens to remain correct.
    """
    from tests.scenarios import build, skeleton

    stream_names = {"bb": "imessage", "gm": "groupme", "wa": "whatsapp",
                    "agent": "agent"}
    truth = []
    for record in build.expand():
        truth.append((
            stream_names.get(record["src"], record["src"]),
            record["thread"], record["text"], bool(record.get("beat"))))
    for record in skeleton.EMAIL:
        truth.append(("email", record["addr"], record["subject"], bool(record.get("beat"))))

    signal = queued_signal = noise = queued_noise = matched = 0
    for stream, thread, text, is_signal in truth:
        row = ctx.conn.execute(
            """SELECT a.id, s.id AS spool_id FROM archive a
                 LEFT JOIN spool s ON s.archive_id = a.id
                WHERE a.stream = ? AND a.thread = ? AND a.text = ?
                ORDER BY a.id LIMIT 1""", (stream, thread, text)
        ).fetchone()
        if not row:
            continue
        matched += 1
        if is_signal:
            signal += 1
            queued_signal += bool(row["spool_id"])
        else:
            noise += 1
            queued_noise += bool(row["spool_id"])
    return {
        "matched": matched, "signal": signal, "queued_signal": queued_signal,
        "noise": noise, "queued_noise": queued_noise,
    }


# ------------------------------------------------------------------------ dates --

def todo_count(pattern, n):
    def run(ctx):
        found = ctx.todos(pattern)
        return len(found) == n, (f"{len(found)} to-do(s), wanted {n}: "
                                 + "; ".join(f"{t.text!r}[{t.status}]" for t in found[:4]))
    return run


#: The answer key's vocabulary for where a question is in its life, mapped onto the
#: column. `absent` is the fourth state and has no row at all — the question was never
#: worth asking, which is a different claim from having asked and retired it.
_QUESTION_STATES = {"asked": "open", "answered": "answered", "dropped": "dropped"}


def question_status(pattern, expected):
    """asked / answered / dropped / absent.

    Every check in this file used to end at "is it still open", so a question the store
    had since answered scored identically to one that was never raised. Eleven live
    defects were about what happens *after* a question is asked and not one of them was
    expressible here.
    """
    def run(ctx):
        rows = ctx.questions(pattern, status=None)
        seen = "; ".join(f"{str(r['text'])[:48]!r}[{r['status']}]" for r in rows[:4])
        if expected == "absent":
            return not rows, "absent, correct" if not rows else f"still stored: {seen}"
        if len(rows) != 1:
            return False, f"{len(rows)} questions match /{pattern}/: {seen or '(none)'}"
        want = _QUESTION_STATES.get(expected, expected)
        got = rows[0]["status"]
        return got == want, f"status = {got!r}, wanted {want!r} ({expected})"
    return run


def question_links_to(pattern, event_pattern):
    """The *right* row, not any row. `event_pattern=None` asserts it links to none.

    Asserting only that a link exists would have graded the live store green: a
    board-game night and a tutoring appointment were both filed under "Alumni meeting"
    on the shared word `meeting`, and "hang out with Quinn" onto another row on the
    word `out`. A wrong link is worse than no link, because it hides a real question
    underneath a row nobody was asking about.
    """
    def run(ctx):
        rows = ctx.questions(pattern, status=None)
        if len(rows) != 1:
            return False, f"{len(rows)} questions match /{pattern}/"
        linked = rows[0]["about_event"]
        row = ctx.conn.execute(
            "SELECT * FROM events WHERE id = ?", (linked,)).fetchone() if linked else None
        title = row["title"] if row else None
        if event_pattern is None:
            return linked is None, ("linked to nothing, correct" if linked is None
                                    else f"linked to {title!r}")
        return (bool(title and re.search(event_pattern, title, re.I)),
                f"linked to {title!r}, wanted ~{event_pattern}")
    return run


#: What a question is asking for, by the way it opens. Only the opening: a `when` in
#: the middle of a sentence is usually a clause ("let me know when you land") rather
#: than the thing being asked. The optional lead is the attribution the prompt asks
#: for — "Mom asked: …".
_ASKS_FOR = [
    (re.compile(r"^(?:[^:?]{0,40}:\s*)?(?:what|which) (?:date|day)\b", re.I), "date"),
    (re.compile(r"^(?:[^:?]{0,40}:\s*)?when\b(?=\s+(?:are|is|do|does|will|did|would))",
                re.I), "date"),
    (re.compile(r"^(?:[^:?]{0,40}:\s*)?(?:what|which) time\b", re.I), "time"),
    (re.compile(r"^(?:[^:?]{0,40}:\s*)?where\b(?=\s+(?:are|is|will|does|do))", re.I),
     "location"),
]


def _rows_a_question_is_about(ctx, row) -> list:
    """The row it is linked to, plus any row whose whole distinctive name it repeats.

    Deliberately stricter and more local than `todos._event_it_is_about`: requiring
    *every* title word keeps this check from moving when the linker's thresholds do,
    so a linker fix is graded by `question_links_to` and this stays a claim about
    redundancy alone.
    """
    from memcal import todos as todos_mod

    out = []
    if row["about_event"]:
        found = ctx.conn.execute(
            "SELECT * FROM events WHERE id = ?", (row["about_event"],)).fetchone()
        if found:
            out.append(found)
    asked = todos_mod._keywords(str(row["text"] or ""))
    for event in ctx.conn.execute("SELECT * FROM events"):
        if any(event["id"] == seen["id"] for seen in out):
            continue
        name = todos_mod._subject_words(event["title"] or "")
        if name and name <= asked:
            out.append(event)
    return out


def no_question_answered_by_a_row(ctx) -> tuple[bool, str]:
    """Closed-world: nothing open asks for a field the calendar already holds.

    The hand-listed version of this knew about one shape — "Who is Aaron?" when Aaron
    has a page. The live store's failures were all the same shape and none of them were
    that one: a question sat open beside the very row that had since answered it,
    because nothing re-read a question after its row was enriched. A question memcal
    can answer itself costs a slot in the brief that a real one needed.
    """
    bad = []
    for row in ctx.conn.execute(
            "SELECT key, text, about_event FROM questions WHERE status = 'open'"):
        text = str(row["text"] or "")
        field = next((name for rx, name in _ASKS_FOR if rx.match(text)), None)
        if not field:
            continue
        for event in _rows_a_question_is_about(ctx, row):
            if event["status"] in ("declined", "happened"):
                continue
            if str(event[field] or "").strip():
                bad.append(f"{row['key']} asks the {field}; "
                           f"{event['title']!r} says {event[field]!r}")
                break
    return not bad, "; ".join(bad[:3]) if bad else "no question a row already answers"


def todo_linked(pattern, event_pattern, *, status="open"):
    def run(ctx):
        found = ctx.todos(pattern)
        event = ctx.one(event_pattern)
        ok = (len(found) == 1 and event is not None
              and found[0].event_id == event.id and found[0].status == status)
        return ok, (
            f"to-dos={[(t.text, t.status, t.event_id) for t in found]}; "
            f"event={event.id if event else None}; wanted status={status}")
    return run


def written_by_at_least(pattern, floor, **kw):
    """Nobody cheaper than `floor` was the last to touch this row."""
    def run(ctx):
        rows = ctx.rows(pattern, **kw)
        ok, note = _found(rows, f"/{pattern}/")
        if not ok:
            return ok, note
        from memcal import events as ev
        got = rows[0].written_by
        return (ev.precedence(got) >= ev.precedence(floor),
                f"written_by = {got!r} ({ev.precedence(got)}), wanted at least "
                f"{floor!r} ({ev.precedence(floor)})")
    return run


FRI = "2026-08-07"
SAT = "2026-08-08"
SUN = "2026-08-09"
THU = "2026-08-06"
WED = "2026-08-05"
SUN_9 = "2026-08-09"
MON_10 = "2026-08-10"
TUE_11 = "2026-08-11"
TUE = "2026-08-04"
TUE_NEXT = "2026-08-11"
SAT_15 = "2026-08-15"
WED_12 = "2026-08-12"
THU_20 = "2026-08-20"


# ------------------------------------------------------------------------ CHECKS --

# ------------------------------------------------------ fit to read, checkable --
# Written after a fortnight of reading the real brief. Each encodes a complaint about
# output that was *correct* and still not usable.

#: A token that is not a name. GroupMe handles are literally `groupme:128934125` and
#: iMessage handles are phone numbers, and both reached the brief: "From
#: groupme:128934125's messages: What date is the PSK gathering", "+137933361279215
#: asked: …". A question attributed to a phone number is still unattributed and
#: additionally leaks the system's internals.
_IDENTIFIER_RE = re.compile(r"\b(?:[a-z]+:[\w.-]{4,}|\+\d{7,})", re.IGNORECASE)

#: Memcal narrating its own process inside the user's calendar: "2 sources mention
#: this", "Casey corrected the dates". The prompt's "the user is not this system's
#: proofreader" rule exists only under questions, so notes collect it instead.
_BOOKKEEPING_RE = re.compile(
    r"(\b\d+ sources?\b|sources? mention|mentioned by \d+|corrected the date|"
    r"per the model|according to (?:the )?(?:store|memcal|record))", re.IGNORECASE)

#: The benchmark's user. The user should be addressed, never described — "What time are you
#: meeting Jordan?" and not "What time is Casey meeting Jordan?", which reads like a
#: case file about them rather than their own calendar.
_USER_NAME_RE = re.compile(r"\bcasey('s)?\b", re.IGNORECASE)


def _prose(ctx) -> list[tuple[str, str, str]]:
    """Every piece of text written for the user to read: (kind, ref, text)."""
    out = []
    for row in ctx.conn.execute("SELECT key, text FROM questions"):
        out.append(("question", row["key"], str(row["text"] or "")))
    for row in ctx.conn.execute("SELECT key, text FROM todos"):
        out.append(("todo", row["key"], str(row["text"] or "")))
    for row in ctx.conn.execute(
            "SELECT key, note FROM events WHERE note IS NOT NULL AND note <> ''"):
        out.append(("note", row["key"], str(row["note"])))
    return out


def no_identifiers_in_prose(ctx) -> tuple[bool, str]:
    bad = [f"{kind} {ref}: {m.group(0)}" for kind, ref, text in _prose(ctx)
           for m in [_IDENTIFIER_RE.search(text)] if m]
    return not bad, "; ".join(bad[:3]) if bad else f"{len(_prose(ctx))} clean"


def addresses_the_user_directly(ctx) -> tuple[bool, str]:
    bad = [f"{kind} {ref}: \"{text[:56]}\"" for kind, ref, text in _prose(ctx)
           if _USER_NAME_RE.search(text)]
    return not bad, "; ".join(bad[:2]) if bad else "no third-person references"


def no_bookkeeping_in_notes(ctx) -> tuple[bool, str]:
    bad = [f"{ref}: \"{text[:48]}\"" for kind, ref, text in _prose(ctx)
           if kind == "note" and _BOOKKEEPING_RE.search(text)]
    return not bad, "; ".join(bad[:2]) if bad else "notes carry no bookkeeping"


def no_question_the_store_can_answer(ctx) -> tuple[bool, str]:
    """"Who is Aaron?" when Aaron has a page, fifteen messages and is in the chat title.

    The prompt already forbids asking "anything you could work out from what is already
    here"; nothing checked it. A question list is only read while it stays short, so one
    self-answerable entry costs attention on the real ones.
    """
    known = {slug.split("-")[0].lower() for slug in wiki.page_slugs(ctx.cfg)} \
        if hasattr(wiki, "page_slugs") else {
            p.stem.split("-")[0].lower()
            for p in ctx.cfg.wiki_dir.glob("*/*.md")}
    bad = []
    for row in ctx.conn.execute("SELECT key, text FROM questions WHERE status='open'"):
        m = re.match(r"\s*who (?:is|are) ([A-Z][\w'-]+)", str(row["text"] or ""), re.I)
        if m and m.group(1).lower() in known:
            bad.append(f"{row['key']} asks who {m.group(1)} is, who has a page")
    return not bad, "; ".join(bad[:2]) if bad else "no self-answerable questions"


#: Counts above this limit indicate bundle-wide rather than line-level evidence.
MAX_EVIDENCE_LINES = 40


def evidence_stays_line_level(ctx) -> tuple[bool, str]:
    rows = ctx.conn.execute(
        "SELECT ref, count(*) n FROM evidence WHERE kind='event' "
        "GROUP BY ref ORDER BY n DESC LIMIT 1").fetchall()
    if not rows:
        return False, "no evidence attached to any event"
    worst = rows[0]
    return worst["n"] <= MAX_EVIDENCE_LINES, f"worst: {worst['ref']} cites {worst['n']}"


def guests_appear_in_evidence(ctx) -> tuple[bool, str]:
    """A guest nobody's cited message names was not read out of the evidence.

    A fabricated attendee is invisible by inspection: the row reads perfectly. Two live
    rows listed the same person, and neither cited a single line naming them.
    """
    bad, checkable = [], 0
    for row in ctx.conn.execute("SELECT key, participants FROM events"):
        people = db.jload(row["participants"], []) or []
        if not people:
            continue
        cited = ctx.conn.execute(
            "SELECT a.text, a.person FROM evidence v JOIN archive a ON a.id=v.archive_id"
            " WHERE v.kind='event' AND v.ref=?", (row["key"],)).fetchall()
        if not cited:
            continue
        blob = " ".join(f"{r['text']} {r['person'] or ''}" for r in cited).lower()
        missing = [p for p in people
                   if isinstance(p, str) and p.split()[0].lower() not in blob]
        checkable += 1
        if missing:
            bad.append(f"{row['key']}: {', '.join(missing)}")
    if not checkable:
        return False, "no row has both guests and cited evidence to check them against"
    return not bad, ("; ".join(bad[:3]) if bad
                     else f"all {checkable} rows' guests are named in evidence")


def dates_follow_from_evidence(ctx) -> tuple[bool, str]:
    """Re-derive each row's date from the phrases in the lines it cited.

    Code has each message's timestamp and cannot miscount, so this is checkable. On the
    live store it disagreed for 7 of 20 checkable rows on one model and 4 of 10 on
    another — the same rate on two models with different code paths.
    """
    from memcal import dates as dates_mod
    bad, checkable = [], 0
    for row in ctx.conn.execute("SELECT key, date FROM events"):
        cited = ctx.conn.execute(
            "SELECT a.ts, a.text FROM evidence v JOIN archive a ON a.id=v.archive_id"
            " WHERE v.kind='event' AND v.ref=?", (row["key"],)).fetchall()
        candidates, saw = set(), False
        for line in cited:
            try:
                said = db.parse_ts(str(line["ts"]))
            except ValueError:
                continue
            for phrase in dates_mod.claims(str(line["text"])[:2000]):
                saw = True
                got = dates_mod.resolve(phrase, said)
                if got:
                    candidates.add(got)
        if saw and candidates:
            checkable += 1
            if str(row["date"]) not in candidates:
                bad.append(f"{row['key']} says {row['date']}, evidence says "
                           f"{sorted(candidates)[:2]}")
    # Nothing to check is not a pass. A run that wrote no rows, or wrote them with no
    # evidence, would otherwise collect this check for free — green at exactly the
    # moment the system has failed hardest. `evidence.line-level` has always refused
    # this; its two siblings were quietly handing it out.
    if not checkable:
        return False, "no row's date could be checked against its own evidence"
    return not bad, ("; ".join(bad[:3]) if bad
                     else f"all {checkable} checkable dates follow from their evidence")


def far_rows_are_named(ctx) -> tuple[bool, str]:
    """A row past the brief's window must still be mentioned somewhere in it.

    Two rows the system captured correctly were reported as extraction failures purely
    because the week block stopped before them. Saying "look up anything outside that"
    asks the reader to know what they are missing before they can ask for it.
    """
    text = brief_mod.render(ctx.conn, ctx.cfg)
    far = [row for row in (events.Event.from_row(r) for r in ctx.conn.execute(
        "SELECT * FROM events WHERE date > ? ORDER BY date",
        ((db.today() + __import__("datetime").timedelta(
            days=ctx.cfg.days_forward)).isoformat(),)))
        # Only rows the brief would show at all. A subscribed holiday feed row is
        # `opportunity` + `mentioned` and is excluded on purpose — asserting it must be
        # named would make this check demand the opposite of `brief._committed`.
        if brief_mod._committed(row)]
    if not far:
        return True, "nothing beyond the window"
    unseen = [e.title for e in far[:6]
              if e.title and e.title.split()[0].lower() not in text.lower()]
    return not unseen, ("not named in the brief: " + ", ".join(unseen[:3])
                        if unseen else f"{len(far)} far row(s) named")


CHECKS: list[Check] = [
    # -- day 1: the facts have to land before anything can be said about changing them.
    Check("d1.poker", "1 date move", 1, row_on(r"poker", FRI)),
    Check("d1.dinner", "2 time move", 1, row_on(r"ramen", THU)),
    Check("d1.climbing", "4 status advance", 1, field_is(r"climb", "status", "mentioned")),
    Check("d1.brunch", "5 private decline", 1, row_on(r"brunch", SUN)),
    Check("d1.beergarden", "9 must not move", 1, row_on(r"beer garden", SAT)),
    Check("d1.bbq", "7 cross-platform", 1, row_on(r"bbq|block party|barbecue", SAT_15)),
    Check("d1.ezpass", "12 wake condition", 1,
          lambda c: (bool(c.todos(r"ez.?pass")), f"todos: {[t.text for t in c.todos('.')]}")),
    # -- 56. The user handed the claim to the assistant. The assistant filed it; the user owes
    #    nothing afterwards. `d1.ezpass` and `show.d1-todo` are the decoys and they are
    #    in this same bundle: same grammar, same stream, and the doer is them.
    Check("d1.delegated-no-todo", "56 work handed off", 1,
          lambda c: (lambda seen, made: (
              seen and not made,
              f"archived={bool(seen)}; todos={[t.text for t in made]}"))(
                  c.gate_reason(r"vet insurance claim") != "(not archived)",
                  c.todos(r"insurance claim|vet insurance|receipts"))),
    # The gate is not what saves us here and the corpus should not pretend it is:
    # COMMIT_RE is a first-person detector, so a bare imperative handed to an assistant
    # is `no-signal` and arrives as a *neighbour* of the gated agent line seven minutes
    # earlier. That is the real path archive 20080 took. What has to hold is that the
    # model, having been shown it, does not turn it into work the user owes.
    Check("d1.delegated-archived", "56 work handed off", 1,
          lambda c: (c.gate_reason(r"vet insurance claim") == "blocked:no-signal",
                     f"gate said {c.gate_reason(r'vet insurance claim')!r}")),
    Check("d1.junk-aws", "16 junk", 1, no_row(r"aws|summit|networking")),
    Check("d1.junk-chase", "16 junk", 1, no_row(r"chase|autopay|automatic payment")),
    Check("d1.board-game", "22 emoji context", 1, row_on(r"board game", SAT)),
    Check("d1.eyes-read", "22 emoji context", 1,
          lambda c: (bool(c.spooled(r"👀")),
                     f"emoji: {c.gate_reason(r'👀')}; spooled={len(c.spooled(r'👀'))}")),
    Check("d1.aspca", "23 source context", 1, row_on(r"aspca|mobile clinic", WED)),
    Check("d1.aspca-location", "23 source context", 1,
          field_is(r"aspca|mobile clinic", "location", "~doggo|129th")),
    Check("d1.quinn-theater", "24 useful wiki", 1,
          slot_says("quinn-brooks", r"alamo drafthouse")),
    Check("d1.quinn-pokemon", "24 useful wiki", 1,
          slot_says("quinn-brooks", r"team rocket")),
    Check("d1.car-not-standing", "25 transient context", 1,
          lambda c: (not c.standing(r"quinn.*(?:borrow|use).*car"),
                     f"standing: {[r['value'] for r in c.standing(r'quinn|car')]}")),
    Check("d1.reaction-without-context", "28 reaction counterexample", 1,
          lambda c: (len(c.spooled(r"^👀$")) == 1,
                     f"eyes spooled = "
                     f"{[(r['thread'], r['gate_reason']) for r in c.spooled(r'^👀$')]}")),
    Check("d1.ticket", "33 confirmation source", 1,
          row_on(r"dune|drafthouse", WED_12), frontier=True),
    Check("d1.ticket-time", "33 confirmation source", 1,
          field_is(r"dune|drafthouse", "time", "~^(19:30|7:30 ?pm)$"),
          frontier=True),
    Check("d1.wiki-source", "29 provenance completeness", 1,
          wiki_source_says("quinn-brooks", r"favorite movie theater",
                           r"Alamo Drafthouse is my favorite")),
    Check("d1.todo-source", "29 provenance completeness", 1,
          todo_source_says(r"ez.?pass", r"Rowan.*EZ.?Pass")),
    Check("d1.encounters", "30 encounter projection", 1,
          lambda c: (lambda p: (
              bool(p) and p["encounters"]["count"] == 3,
              f"encounters = {(p or {}).get('encounters', {}).get('count', 0)}; "
              f"activities = {(p or {}).get('encounters', {}).get('by_activity', [])}"))(
                  wiki.profile(c.conn, c.cfg.wiki_dir, "quinn-brooks"))),
    Check("d1.relationship-forward", "34 linked relationships", 1,
          slot_says("quinn-brooks", r"sister Katie")),
    Check("d1.relationship-reciprocal", "34 linked relationships", 1,
          slot_says("katie", r"brother Quinn Brooks")),
    Check("d1.relationship-alias", "34 linked relationships", 1,
          lambda c: (lambda p: (
              bool(p and "Kat" in p.aliases),
              f"aliases = {p.aliases if p else '(no page)'}"))(c.page("katie"))),
    Check("d1.relationship-source", "34 linked relationships", 1,
          wiki_source_says("katie", r"brother", r"Katie is my sister")),
    Check("d1.generic-party-created", "35 generic group event", 1,
          row_on(r"neon garden", TUE)),
    Check("d1.generic-party-no-roster", "35 generic group event", 1,
          lambda c: (lambda e: (
              bool(e) and not e.participants,
              f"participants = {e.participants if e else '(no row)'}"))(
                  c.one(r"neon garden"))),
    Check("d1.elements-one-row", "36 enrich existing group event", 1,
          count_rows(r"elements", 1)),
    Check("d1.elements-range", "36 enrich existing group event", 1,
          field_is(r"elements", "until", "2026-08-09")),
    Check("d1.elements-location", "36 enrich existing group event", 1,
          field_is(r"elements", "location", "~cedar falls")),
    Check("d1.elements-attendees-not-roster", "36 enrich existing group event", 1,
          lambda c: (lambda e: (
              bool(e) and e.participants == ["Alex Rivera"],
              f"participants = {e.participants if e else '(no row)'}"))(
                  c.one(r"elements"))),
    Check("d1.elements-source", "36 enrich existing group event", 1,
          source_says(r"elements", r"Friday August 7 through Sunday August 9")),
    Check("d1.tattoo", "50 stakes", 1, row_on(r"tattoo|sasha", "2026-08-18")),
    Check("d1.tattoo-confirmed", "50 stakes", 1,
          field_is(r"tattoo|sasha", "status", "confirmed")),
    Check("d1.tattoo-stakes-kept", "50 stakes", 1,
          field_is(r"tattoo|sasha", "note", "~deposit")),
    Check("d1.no-vendor-event", "51 a company's event", 1,
          no_row(r"wellness wednesday|webinar|petly|ramirez")),
    Check("d1.work-todo", "49 obligation in a work DM", 1,
          todo_count(r"jira|sec.?2847", 1)),
    Check("d1.work-no-row", "49 obligation in a work DM", 1,
          no_row(r"jira|sec.?2847|standup")),
    Check("d1.ticket-spider-linked", "37 ticket lifecycle", 1,
          todo_linked(r"Spider-Man tickets", r"Spider-Man movie")),
    Check("d1.ticket-fantastic-linked", "37 ticket lifecycle", 1,
          todo_linked(r"Fantastic Four tickets", r"Fantastic Four movie")),
    Check("d1.ticket-superman-linked", "37 ticket lifecycle", 1,
          todo_linked(r"Superman tickets", r"Superman movie")),

    # -- 1. date move
    # `^poker at` here was the most expensive pattern in the key. It asked a
    # model-written title to *start* with two particular words, so gpt-5.6-terra's third
    # trial — which stored "Poker, 8pm, 42 Example Street, with Jordan, Alex and
    # Cameron", every asserted field correct — lost nine checks for calling it "Poker".
    # The day is what separates this row from June's poker night, so the day is what
    # the lookup scopes on.
    Check("poker.moved", "1 date move", 2, row_on(r"poker", SAT)),
    Check("poker.one-row", "1 date move", 2, count_rows(r"poker", 1, on=SAT)),
    Check("poker.history", "1 date move", 2,
          changed(r"poker", "date", FRI, SAT, on=SAT)),
    Check("poker.key-stable", "1 date move", 2,
          lambda c: (lambda e: (bool(e) and FRI in (e.key or ""),
                                f"key = {e.key if e else '(no row)'}"))(
                                    c.one(r"poker", on=SAT)),
          soft=True),

    # -- 2. time move
    Check("dinner.time", "2 time move", 2,
          field_is(r"ramen", "time", "~^(20:30|8:30 ?pm)$")),
    Check("dinner.date-held", "2 time move", 2, row_on(r"ramen", THU)),
    # A confirmed plan with a time and three guests, downgraded to an opportunity
    # because the WhatsApp group is called "dinner thu" and the title contains the
    # word `dinner`. Nothing asserted `kind` on this row, so the misfire had been
    # invisible since the guard was written — and after the `plain_state` fix it
    # rendered "confirmed" anyway, which hid it a second time.
    Check("dinner.stays-a-commitment", "2 time move", 2,
          field_is(r"ramen", "kind", "commitment")),

    # -- 3. location move
    # "~412" here was a typo for the corpus's "42 Example Street" and had been red since
    # the first commit — `412` appears nowhere in the fixtures, the beat sheet or the
    # hand-written diff `integration.py` feeds in, and the three other checks on this
    # same row (`poker.source-*`, `poker.evidence`) all assert "42 Example Street".
    # A permanently red check is worse than a missing one: it trains everyone reading
    # the summary to discount the failure count, which is the number that would show a
    # real regression.
    Check("poker.location", "3 location move", 2,
          field_is(r"poker", "location", "~42 Example", on=SAT)),
    Check("poker.location-replaced", "3 location move", 2,
          lambda c: (lambda e: (bool(e) and not re.search(r"jordan", str(e.location or ""), re.I),
                                f"location = {e.location if e else '(no row)'}"))(
                                    c.one(r"poker", on=SAT))),

    # -- 4. status advance
    Check("climbing.confirmed", "4 status advance", 2,
          field_is(r"climb", "status", "confirmed")),
    Check("climbing.date", "4 status advance", 2, row_on(r"climb", WED)),

    # -- 5. private decline beats the later public message
    Check("brunch.declined", "5 private decline", 2,
          field_is(r"brunch", "status", "declined")),
    Check("brunch.survives", "5 private decline", 2, count_rows(r"brunch", 1)),
    Check("brunch.reads-plainly", "5 private decline", 2,
          brief_lacks(r"\bdeclined\b"), soft=True),

    # -- 6. participants
    Check("dinner.riley", "6 participants", 2,
          lambda c: (lambda e: (bool(e) and any("riley" in p.lower() for p in e.participants),
                                f"participants = {e.participants if e else '(no row)'}"))(
              c.one(r"ramen"))),
    Check("neon.moved-from-dm", "35 generic group event", 2,
          row_on(r"neon garden", WED)),
    Check("neon.one-row", "35 generic group event", 2,
          count_rows(r"neon garden", 1)),
    Check("neon.move-history", "35 generic group event", 2,
          changed(r"neon garden", "date", TUE, WED)),
    Check("neon.still-no-roster", "35 generic group event", 2,
          lambda c: (lambda e: (
              bool(e) and not e.participants,
              f"participants = {e.participants if e else '(no row)'}"))(
                  c.one(r"neon garden"))),
    Check("neon.sources-include-dm", "35 generic group event", 2,
          source_says(r"neon garden", r"moved from tonight to tomorrow")),

    # -- 7. cross-platform: an email moves a row a group chat created
    Check("bbq.moved", "7 cross-platform", 2,
          field_is(r"bbq|block party|barbecue", "time", "~^(16:00|4 ?pm)$")),
    Check("bbq.one-row", "7 cross-platform", 2, count_rows(r"bbq|block party|barbecue", 1)),

    # -- 19. The agent settled it during the day, before any pass had read a word of
    #    it. The row is `live`, confirmed, and already there when the day-1 pass reads
    #    the very conversation that produced it. It must recognise it and leave it be.
    Check("movie.agent-row", "19 agent settles a plan", 1,
          count_rows(r"^(?!.*superman).*\bmovie\b", 1, on=TUE_NEXT)),
    Check("movie.agent-confirmed", "19 agent settles a plan", 1,
          field_is(r"^(?!.*superman).*\bmovie\b", "status", "confirmed", on=TUE_NEXT)),
    Check("movie.agent-kept", "19 agent settles a plan", 1,
          written_by_at_least(r"^(?!.*superman).*\bmovie\b", "live", on=TUE_NEXT)),
    # ...and still be able to cancel it the next day. Precedence is meant to stop a
    # cheap pass re-reading old traffic, not to stop it reading the news.
    Check("movie.one-row", "19 agent settles a plan", 2,
          count_rows(r"^(?!.*superman).*\bmovie\b", 1, on=TUE_NEXT)),

    # -- 20. One row and one to-do out of one sentence to the assistant — and a day 2
    #    where the same plan arrives again from the friend it was made with, saying
    #    nothing new. Everything about day 2 here is a duplicate waiting to happen.
    Check("show.agent-row", "20 one sentence, two tools", 1,
          row_on(r"bowery|the show", FRI)),
    Check("show.d1-todo", "20 one sentence, two tools", 1, todo_count(r"venmo", 1)),
    Check("show.one-row", "20 one sentence, two tools", 2, count_rows(r"bowery|the show", 1)),
    Check("show.held", "20 one sentence, two tools", 2, row_on(r"bowery|the show", FRI)),
    Check("show.still-confirmed", "20 one sentence, two tools", 2,
          field_is(r"bowery|the show", "status", "confirmed")),
    Check("show.time-held", "20 one sentence, two tools", 2,
          field_is(r"bowery|the show", "time", "~^(20:00|8 ?pm)$")),

    # -- 21. The user told it the ticket was paid for, so the to-do is closed. The pass reads
    #    the same sentence and must not re-open it, nor open a second one beside it.
    Check("venmo.closed", "21 agent closes a to-do", 2,
          lambda c: (lambda t: (bool(t) and all(x.status != "open" for x in t),
                                f"venmo to-dos: {[(x.text, x.status) for x in t]}"))(
              c.todos(r"venmo"))),
    Check("venmo.one-todo", "21 agent closes a to-do", 2, todo_count(r"venmo", 1)),
    Check("venmo.no-event", "21 agent closes a to-do", 2,
          no_row(r"paid|sent .*money|venmo")),
    Check("venmo.not-in-brief", "31 resolved work disappears", 2,
          brief_lacks(r"venmo|sent Cameron.*money")),

    # -- 23/26. A terse row or pronoun-heavy question is only useful if it carries the
    #    conversation that made it meaningful.
    Check("aspca.source", "23 source context", 2,
          source_says(r"aspca|mobile clinic", r"doggo park.*129th")),
    Check("mom.question-stands-alone", "26 standalone questions", 2,
          question_status(r"mom asked(?::| when).*coming over", "asked")),
    # A question about a person is not a question about whichever row that person also
    # stands on. This is the corpus's guard on the linker: it is green today and goes
    # red the moment a title-overlap threshold is loosened.
    Check("mom.question-not-on-a-row", "26 standalone questions", 2,
          question_links_to(r"mom asked(?::| when).*coming over", None)),
    Check("quinn.durable-car-policy", "27 durable permission", 2,
          lambda c: (bool(re.search(r"car permission.*borrow.*car.*anytime",
                                    c.slot_text("quinn-brooks"), re.I)),
                     f"quinn-brooks slots: {c.slot_text('quinn-brooks')}"),
          frontier=True),
    Check("mom.question-source", "29 provenance completeness", 2,
          question_source_says(r"coming over", r"When am I coming over again")),
    Check("ticket.source", "33 confirmation source", 2,
          source_says(r"dune|drafthouse", r"ticket is confirmed.*Dune.*August 12"),
          frontier=True),
    Check("poker.sources-include-update", "29 provenance completeness", 2,
          source_says(r"poker", r"42 Example Street", on=SAT)),
    Check("poker.current-source-first", "29 provenance completeness", 2,
          event_current_source_first(r"poker", r"42 Example Street", on=SAT),
          frontier=True),
    Check("wiki.every-fact-has-source", "29 provenance completeness", 2,
          every_wiki_fact_has_source, frontier=True),

    # -- 8. cancellation
    Check("movie.not-live", "8 cancellation", 2,
          lambda c: (lambda rows: (all(r.status in ("declined", "happened") for r in rows)
                                   if rows else True,
                                   f"movie rows: {[(r.date, r.status) for r in rows] or 'none'}"))(
              c.rows(r"^(?!.*superman).*\bmovie\b|theater|cinema", on=TUE_NEXT))),

    # -- 9. the decoy that must not move a settled plan
    Check("beergarden.held", "9 must not move", 2, row_on(r"beer garden", SAT)),
    Check("beergarden.one-row", "9 must not move", 2, count_rows(r"beer garden", 1)),
    Check("beergarden.not-next-week", "9 must not move", 2,
          no_row(r"beer garden", on="2026-08-15")),
    Check("saturday.two-rows", "9 must not move", 2,
          lambda c: (lambda rows: (len(rows) >= 2,
                                   f"{len(rows)} rows on {SAT}: "
                                   + "; ".join(r.title for r in rows)))(
              [events.Event.from_row(r) for r in c.conn.execute(
                  "SELECT * FROM events WHERE date = ?", (SAT,))])),

    # -- 10. no walking backwards
    Check("poker.no-friday", "10 no walk back", 2, no_row(r"poker", on=FRI)),

    # -- 11. one plan, three streams
    Check("dinner.one-row", "11 dedupe", 2, count_rows(r"ramen", 1)),

    # -- 12. wake condition raises a question and closes nothing
    Check("ezpass.still-open", "12 wake condition", 2,
          lambda c: (lambda t: (bool(t) and t[0].status == "open",
                                f"todo: {t[0].text!r} [{t[0].status}]" if t else "no todo"))(
              c.todos(r"ez.?pass"))),
    Check("ezpass.asked", "12 wake condition", 2,
          lambda c: (bool(c.questions(r"ez.?pass|rowan")),
                     f"questions: {[str(q['text'])[:60] for q in c.questions('.')]}")),

    # -- 13. slot replaced
    Check("jordan.riverton", "13 slot update", 2, slot_says("jordan-lee", r"riverton")),
    Check("jordan.not-eastwood", "13 slot update", 2,
          slot_never_says("jordan-lee", r"eastwood")),
    # The page holds what is true; `slot_history` holds what it used to say. Events have
    # had this since the beginning and slots had nothing, so a corrected fact simply
    # stopped ever having been the case.
    Check("jordan.change-recorded", "13 slot update", 2,
          slot_changed("jordan-lee", r"eastwood", r"riverton")),

    # -- 14. the mom trap
    Check("bailey.not-mom", "14 mom trap", 2,
          slot_never_says("bailey-hall", r"\b(mom|mother)\b")),
    Check("mom.not-bailey", "14 mom trap", 2, slot_never_says("mom", r"bailey")),

    # -- 15. the Comet trap
    Check("comet.not-a-horse", "15 comet trap", 2, no_row(r"trail ride")),
    Check("comet.no-species-claim", "15 comet trap", 2,
          lambda c: (lambda hits: (not hits, f"pages claiming a horse: {hits}" if hits
                                   else "clean"))(
                  [s for s in c.pages()
                   if re.search(r"horse|pony|equestrian", c.slot_text(s) or "", re.I)])),

    # -- 37. Explicit proof settles an event-linked prerequisite and moves the useful
    #    details onto the event. Positive model judgements start as frontier checks;
    #    the gate counterexamples are deterministic product requirements.
    Check("tickets.spider-closed", "37 ticket lifecycle", 2,
          todo_linked(r"Spider-Man tickets", r"Spider-Man movie", status="closed"),
          frontier=True),
    Check("tickets.spider-time", "37 ticket lifecycle", 2,
          field_is(r"Spider-Man movie", "time", "~^(19:40|7:40 ?pm)$"),
          frontier=True),
    Check("tickets.spider-theater", "37 ticket lifecycle", 2,
          field_is(r"Spider-Man movie", "location", "~AMC Lincoln Square"),
          frontier=True),
    Check("tickets.spider-seats", "37 ticket lifecycle", 2,
          field_is(r"Spider-Man movie", "note", "~H8.*H9"),
          frontier=True),
    Check("tickets.spider-source", "37 ticket lifecycle", 2,
          source_says(r"Spider-Man movie", r"got our Spider-Man tickets"),
          frontier=True),
    Check("tickets.fantastic-closed", "37 ticket lifecycle", 2,
          todo_linked(r"Fantastic Four tickets", r"Fantastic Four movie", status="closed"),
          frontier=True),
    Check("tickets.fantastic-time", "37 ticket lifecycle", 2,
          field_is(r"Fantastic Four movie", "time", "~^(18:20|6:20 ?pm)$"),
          frontier=True),
    Check("tickets.fantastic-theater", "37 ticket lifecycle", 2,
          field_is(r"Fantastic Four movie", "location", "~AMC Empire 25"),
          frontier=True),
    Check("tickets.fantastic-details", "37 ticket lifecycle", 2,
          field_is(r"Fantastic Four movie", "note", "~Auditorium 7.*J10.*J11.*84721"),
          frontier=True),
    Check("tickets.fantastic-source", "37 ticket lifecycle", 2,
          source_says(r"Fantastic Four movie", r"tickets are confirmed.*Fantastic Four"),
          frontier=True),
    Check("tickets.details-in-brief", "37 ticket lifecycle", 2,
          opens_with(r"Spider-Man", r"7:40.*AMC Lincoln Square.*H8.*H9"),
          frontier=True),
    Check("gate.amc-proof-arrives", "37 ticket lifecycle", 2,
          lambda c: (bool(c.spooled(r"tickets are confirmed for Fantastic Four")),
                     f"receipt: {c.gate_reason(r'Fantastic Four')}")),
    Check("gate.amc-wrong-stays-out", "37 ticket lifecycle", 2,
          lambda c: (not c.spooled(r"Batman Returns"),
                     f"wrong receipt: {c.gate_reason(r'Batman Returns')}")),
    Check("gate.amc-marketing-stays-out", "37 ticket lifecycle", 2,
          lambda c: (not c.spooled(r"Superman tickets are on sale"),
                     f"marketing: {c.gate_reason(r'Superman tickets are on sale')}")),
    Check("tickets.superman-still-open", "37 ticket lifecycle", 2,
          todo_linked(r"Superman tickets", r"Superman movie", status="open")),
    Check("tickets.no-batman-row", "37 ticket lifecycle", 2,
          no_row(r"Batman Returns"), frontier=True),

    # -- 16. junk, after both days
    Check("junk.no-aws", "16 junk", 2, no_row(r"aws|summit|networking night")),
    Check("junk.no-sale", "16 junk", 2, no_row(r"uniqlo|sale|final hours|discount")),
    Check("junk.no-amazon", "16 junk", 2, no_row(r"package|delivery|amazon")),
    Check("junk.no-affection", "16 junk", 2,
          lambda c: (lambda hits: (not hits, f"affection stored: {hits}" if hits else "clean"))(
              [s for s in c.pages()
               if re.search(r"loves? (casey|them|me)|i love you", c.slot_text(s) or "", re.I)])),
    Check("junk.no-opinion", "16 junk", 2,
          slot_never_says("alex-chen", r"should (be )?look|better job|career")),
    # Logistics that resolve inside the conversation. "what do you wanna watch tonight"
    # / "you pick" and "thinking of making pasta tonight" / "sounds good" are two people
    # at home settling an evening — no time, no place, nobody else expecting them. Found
    # over-captured while comparing packing levels; the corpus had always tested it and
    # nothing had ever asserted it, so "Movie night with Harper" and "Pasta dinner" had
    # been quietly landing on the calendar the whole time.
    # 50/51, after both days. The one with stakes is still a commitment; the vendor's
    # does not arrive late through some other door.
    Check("tattoo.survives", "50 stakes", 2, count_rows(r"tattoo|sasha", 1)),
    Check("tattoo.still-confirmed", "50 stakes", 2,
          field_is(r"tattoo|sasha", "status", "confirmed")),
    # 57. A complete later replacement must amend the original, including its time.
    Check("appointment.amendment-date", "57 explicit appointment amendment", 2,
          row_on(r"tattoo|sasha", THU_20)),
    Check("appointment.amendment-time", "57 explicit appointment amendment", 2,
          field_is(r"tattoo|sasha", "time", "16:15")),
    Check("appointment.amendment-one-row", "57 explicit appointment amendment", 2,
          count_rows(r"tattoo|sasha", 1)),
    Check("appointment.amendment-date-history", "57 explicit appointment amendment", 2,
          changed(r"tattoo|sasha", "date", "2026-08-18", THU_20)),
    Check("appointment.amendment-time-history", "57 explicit appointment amendment", 2,
          changed(r"tattoo|sasha", "time", "14:00", "16:15")),
    Check("vendor.nothing-at-all", "51 a company's event", 2,
          no_row(r"wellness wednesday|webinar|petly|ramirez")),
    Check("vendor.no-todo", "51 a company's event", 2,
          todo_count(r"webinar|petly|wellness", 0)),
    Check("junk.no-home-logistics", "16 junk", 2,
          no_row(r"pasta|what to watch|movie night|groceries|milk|dinner at home")),

    # -- 17. blank-page discipline
    Check("pages.no-shortcode", "17 wiki hygiene", 2, no_page_matching(r"262966|^\d{5,6}$")),
    Check("pages.no-bulk-sender", "17 wiki hygiene", 2,
          no_page_matching(r"amazonses|chase|uniqlo|squarespace|partiful|no-?reply")),
    Check("pages.no-group-name", "17 wiki hygiene", 2,
          no_page_matching(r"^(poker-crew|smash-bros|brunch-sunday|beer-garden|"
                           r"block-party|dinner-thu|morgan-family)$")),
    Check("pages.no-raw-handle", "17 wiki hygiene", 2, no_page_matching(r"^\+?1?\d{10,}$")),
    Check("pages.no-self", "17 wiki hygiene", 2, no_page_matching(r"^(me|casey)$")),
    Check("pages.none-empty", "17 wiki hygiene", 2,
          lambda c: (lambda empty: (
              not empty, "all pages hold facts/aliases/history" if not empty
              else f"empty pages: {empty}"))(
                  [slug for slug in c.pages()
                   if not (c.page(slug) and wiki.is_material(c.page(slug)))])),

    # -- the gate, as distinct from what the model did with what got through. Both of
    #    these are findings from the first run of this benchmark, not settled bugs:
    #    `gate_email` checks the subject *before* the bulk-header tests on purpose, so
    #    "Reminder:" rescues a Headway appointment from `noreply@` and lets an AWS
    #    mailing list through on the identical word. Recorded here so the tradeoff has a
    #    number attached the next time it is argued about.
    Check("gate.partiful-arrives", "gate", 2,
          lambda c: (bool(c.spooled(r"block party bbq")),
                     f"partiful update: {c.gate_reason(r'Block Party BBQ')}")),
    Check("gate.bulk-stays-out", "gate", 2,
          lambda c: (not c.spooled(r"AWS Summit"),
                     f"aws newsletter: {c.gate_reason(r'AWS Summit')}"), soft=True),

    # -- the brief is the product surface. A right row that never reaches it is a loss,
    #    and schema words that reach it become speech.
    Check("brief.has-poker", "brief", 2, brief_has(r"poker")),
    Check("brief.no-jargon", "brief", 2,
          brief_lacks(r"\b(commitment|availability|opportunity|observed|written_by|"
                      r"mentioned|upsert|bundle|slug)\b"), soft=True),
    Check("brief.no-keys", "brief", 2, brief_lacks(r"@20\d\d-\d\d-\d\d")),
    Check("brief.sources-open", "brief", 2, brief_sources_open),
    Check("state.only-declared-events", "32 closed-world inventory", 2,
          only_expected_events([
              (r"poker", SAT),
              (r"ramen|dinner", THU),
              (r"climb", WED),
              (r"brunch", SUN),
              (r"beer garden", SAT),
              (r"bbq|block party|barbecue", SAT_15),
              (r"board game", SAT),
              (r"aspca|mobile clinic", WED),
              (r"dog park", SAT),          # 54, proposed by an id nothing can name
              (r"movie", TUE_NEXT),
              (r"bowery|the show", FRI),
              (r"poker", "2026-06-05"),
              (r"dinner", "2026-06-20"),
              (r"board game", "2026-07-18"),
              (r"dune|drafthouse", WED_12),
              (r"neon garden", WED),
              (r"elements", FRI),
              (r"Spider-Man movie", MON_10),
              (r"Fantastic Four movie", SUN_9),
              (r"Superman movie", TUE_11),
              (r"housewarming", SAT_15),
              (r"physio", "2026-08-12"),
              (r"jack's 30th", "2026-08-22"),
              (r"dentist", "2026-08-20"),
              (r"statehood", "2026-08-21"),
              (r"capture the flag", "2026-08-23"),
              (r"tattoo|sasha", THU_20),
              (r"tutoring", WED_12),
              (r"bloodwork", "2026-08-06"),
              (r"mount aldon", MON_10),
          ])),
    Check("state.only-declared-todos", "32 closed-world inventory", 2,
          only_expected_text_rows(
              lambda c: [todo.text for todo in c.todos(".")],
              [r"venmo Cameron.*ticket", r"Rowan.*EZ.?Pass",
               r"Spider-Man tickets", r"Fantastic Four tickets", r"Superman tickets",
               r"Jira|SEC.?2847"],
              "to-dos")),
    Check("state.only-declared-questions", "32 closed-world inventory", 2,
          only_expected_text_rows(
              lambda c: [row["text"] for row in c.questions(".")],
              [r"Rowan|EZ.?Pass", r"Mom.*coming over",
               r"Neon Garden", r"housewarming"], "questions")),
    Check("state.only-declared-standing", "32 closed-world inventory", 2,
          only_expected_text_rows(
              lambda c: [row["value"] for row in c.standing(".")],
              [], "standing rows")),
    Check("state.only-declared-pages", "32 closed-world inventory", 2,
          only_expected_pages({
              "casey", "jordan-lee", "katie", "poker-night", "quinn-brooks",
              "u-and-me-calendar",
          })),
    Check("state.only-declared-wiki-values", "32 closed-world inventory", 2,
          only_expected_wiki_values({
              "casey": [r"North End", r"Comet"],
              "jordan-lee": [r"Riverton"],
              "katie": [r"Quinn Brooks"],
              "poker-night": [r"42 Example Street"],
              "quinn-brooks": [r"Alamo Drafthouse", r"Team Rocket", r"Katie",
                                r"borrow.*car.*anytime"],
              "u-and-me-calendar": [r"shared calendar.*Casey.*Harper"],
          })),
    Check("state.only-declared-aliases", "32 closed-world inventory", 2,
          only_expected_aliases({"katie": [r"Kat"]})),
    Check("state.one-question-per-topic", "32 closed-world inventory", 2,
          lambda c: (len(c.questions(r"Rowan|EZ.?Pass")) == 1,
                     f"EZ-Pass questions="
                     f"{[row['text'] for row in c.questions(r'Rowan|EZ.?Pass')]}"),
          frontier=True),

    # -- 33. Whether the output is fit to be read by the person it is written for.
    #
    # Every check above asks whether the calendar is *correct*. These ask whether it is
    # *usable*, which turned out to be a different question: a fortnight of reading the
    # real brief produced five complaints, and two of them were about rows that were
    # entirely correct and simply unreadable or unreachable. None of this needs new
    # traffic — they hold over whatever the run produced, which is what makes them cheap
    # to keep and hard to satisfy by accident.
    Check("voice.no-identifiers", "38 fit to read", 2, no_identifiers_in_prose),
    Check("voice.second-person", "38 fit to read", 2, addresses_the_user_directly),
    Check("voice.no-bookkeeping", "38 fit to read", 2, no_bookkeeping_in_notes),
    Check("state.no-self-answerable-question", "38 fit to read", 2,
          no_question_the_store_can_answer, frontier=True),
    # The same complaint against the calendar rather than the wiki, and stated as a
    # rule instead of one hand-listed shape.
    Check("state.no-question-a-row-answers", "38 fit to read", 2,
          no_question_answered_by_a_row),

    # -- 34. Whether a row can be checked by the person reading it.
    Check("evidence.line-level", "39 checkable", 2, evidence_stays_line_level),
    Check("evidence.guests-are-named", "39 checkable", 2, guests_appear_in_evidence,
          frontier=True),
    Check("evidence.dates-are-supported", "39 checkable", 2, dates_follow_from_evidence,
          frontier=True),

    # -- 35. Whether what was captured is actually shown.
    Check("brief.later-is-visible", "40 reachable", 2, far_rows_are_named),

    # ---------------------------------------------------------------- days 3 & 4 --
    # Almost no new traffic. What is being graded is what the *store* does while
    # nobody is saying anything, which two days cannot express at all.

    # -- 41. A question asked on day 1, answered by day-3 evidence.
    Check("house.address-arrives", "41 answered by later evidence", 3,
          field_is(r"housewarming", "location", "~55 Linden")),
    Check("house.one-row", "41 answered by later evidence", 3,
          count_rows(r"housewarming", 1)),
    Check("house.question-retires", "41 answered by later evidence", 3,
          question_status(r"housewarming", "dropped")),
    Check("house.question-was-on-the-right-row", "41 answered by later evidence", 3,
          question_links_to(r"housewarming", r"housewarming")),

    # -- 44. The good case, and it is here so that it stays working: a recurring
    #    appointment's owner moves it and the row follows without being told.
    Check("physio.moved", "44 owner moves a recurring row", 3,
          row_on(r"physio", "2026-08-19")),
    Check("physio.one-row", "44 owner moves a recurring row", 3,
          count_rows(r"physio", 1)),
    Check("physio.history", "44 owner moves a recurring row", 3,
          changed(r"physio", "date", "2026-08-12", "2026-08-19")),
    Check("physio.time-held", "44 owner moves a recurring row", 3,
          field_is(r"physio", "time", "~^(17:00|5 ?pm)$")),
    # The slug is written by a model, so this is a regex and not a string compare —
    # the model layer picked `weekly-physio`, which keeps the series perfectly well.
    # What is being asserted is that the row still belongs to a series after moving,
    # not what anyone decided to call it.
    Check("physio.series-survives", "44 owner moves a recurring row", 3,
          lambda c: (lambda e: (
              bool(e) and bool(re.search(r"physio", e.series or "", re.I)),
              f"series = {e.series if e else '(no row)'}"))(c.one(r"physio"))),

    # -- 45. The rescan. Red until the connector stops treating a re-derivation as
    #    news; the rename is what forces every revision to change and the whole
    #    snapshot to be re-derived.
    Check("jack30.kind-survives-rescan", "45 rescan must not undo judgement", 3,
          field_is(r"jack's 30th", "kind", "commitment")),
    Check("jack30.status-survives-rescan", "45 rescan must not undo judgement", 3,
          field_is(r"jack's 30th", "status", "confirmed")),
    Check("jack30.one-row", "45 rescan must not undo judgement", 3,
          count_rows(r"jack's 30th", 1)),
    Check("calendar.rename-makes-no-duplicate", "45 rescan must not undo judgement", 3,
          count_rows(r"dentist", 1)),

    # -- 52. A link is how you attend, and it is not a place.
    #
    #    "the new entry for Wednesday just said online as the location. But the
    #    information was there. It was in my email." Both halves were true and neither
    #    could reach a row: `location` answers *where*, `rsvp_url` answers *how you
    #    reply*, and nothing answered *how you attend*, so a Zoom link had no field to
    #    land in from either source. The two arms are graded apart on purpose — the
    #    calendar arm is deterministic and the email arm is the model's.
    Check("tutoring.row", "52 a link is not a place", 1,
          row_on(r"tutoring", WED_12)),
    Check("tutoring.join-url", "52 a link is not a place", 1,
          field_is(r"tutoring", "join_url", r"~zoom\.example/j/8842119")),
    #    The decoy that keeps the fix honest: "Online" is what their calendar says
    #    and it is worth keeping. A link that overwrites the location has moved the
    #    problem rather than solved it.
    Check("tutoring.location-kept", "52 a link is not a place", 1,
          field_is(r"tutoring", "location", r"~online")),
    #    A stored link that nothing renders is still unavailable to the user.
    Check("tutoring.link-reaches-the-brief", "52 a link is not a place", 1,
          brief_has(r"zoom\.example/j/8842119")),
    #    The href lives in an attribute and the label lives in the body, so a stripper
    #    that keeps text keeps "Tutoring Meeting Room Link" and loses the appointment.
    Check("tutoring.email-keeps-the-link", "52 a link is not a place", 1,
          lambda c: (bool(c.spooled(r"zoom\.example/j/8842119")),
                     "the archived mail still has the URL"
                     if c.spooled(r"zoom\.example/j/8842119")
                     else "the mail kept the label and lost the href")),
    #    A rescan may not re-derive it back out again.
    Check("tutoring.join-url-survives-rescan", "52 a link is not a place", 3,
          field_is(r"tutoring", "join_url", r"~zoom\.example/j/8842119")),
    #    The return trip, which nothing graded for nine days. `join_link`
    #    lifts the URL out of `location` on the way in; until `publish_location` there
    #    was no way to put it back, so the entry memcal wrote to their real calendar said
    #    "Online" and the Join button on the 12:58 notification was empty. Reading a
    #    convention you will not write is the shape, and only an outbound check sees it.
    Check("tutoring.join-url-is-published-to-location", "52 a link is not a place", 1,
          publishes_location(r"tutoring", r"~zoom\.example/j/8842119")),
    #    The decoy, again, on the way out: composing must not eat the place. "Online"
    #    is their calendar's word and a publish that replaces it with a URL has moved the
    #    problem outdoors instead of solving it.
    Check("tutoring.published-location-keeps-the-place",
          "52 a link is not a place", 1,
          publishes_location(r"tutoring", r"~^online;")),
    #    The half with no URL in it, which is the general form: the connector was not
    #    dropping *links*, it was dropping the description. A buzzer number is lost
    #    exactly as completely and no matcher would ever have rescued it.
    Check("bloodwork.detail-kept", "52 a link is not a place", 1,
          field_is(r"bloodwork", "note", r"~buzzer 4")),
    # Was `brief_has(r"buzzer 4")`. The buzzer code is detail and lives behind the
    # handle now; what this report was about is that it was *unreachable*, and being on
    # the line was one way to be reachable rather than the requirement.
    Check("bloodwork.detail-reaches-the-brief", "52 a link is not a place", 1,
          opens_with(r"bloodwork", r"Suite 300|buzzer 4")),

    # -- 54. The group has a member nobody can name.
    #
    #    A WhatsApp LID: fifteen digits, no push name, matching no contact, and nothing
    #    in the corpus or on the machine can ever resolve it. The plan is real and the
    #    proposer is unnameable, and both halves have to survive — an over-eager filter
    #    that throws the line away has swapped one failure for a worse one.
    Check("nameless.plan-survives-its-proposer", "54 a member nobody can name", 1,
          row_on(r"park", SAT)),
    #    The decoy that keeps the fix honest. A numeral is not a person: it must not
    #    reach `participants`, and the *named* participant must still be on the row, so
    #    "drop the whole line" cannot pass this.
    Check("nameless.the-numeral-is-not-a-person", "54 a member nobody can name", 1,
          lambda c: (lambda rows: (
              bool(rows)
              and not any("261516951601296" in str(p) for p in rows[0].participants),
              f"participants = {rows[0].participants if rows else 'no row'}"))(
                  c.rows(r"park", on=SAT))),
    Check("nameless.no-page-for-a-number", "54 a member nobody can name", 1,
          no_page_matching(r"\d{12,}")),
    #    An unanswerable question is worse than no question: nobody on earth can say who
    #    `+261516951601296` is, so asking occupies the Ask block for ever and teaches them
    #    the block is noise. That is `todos.admissible`'s rung, not the prompt's.
    Check("nameless.no-unanswerable-question", "54 a member nobody can name", 1,
          lambda c: (not c.questions(r"\d{12,}"),
                     "no question about a bare id"
                     if not c.questions(r"\d{12,}")
                     else f"asked: {c.questions(r'.d{12,}')[0]['text'][:60]}")),

    # -- 55. The platform is not a person, and sometimes it is quoting one.
    #
    #    Both lines arrive on `groupme:system` under the display name "GroupMe". Nothing
    #    may be created for it — `groupme:system` sat at the top of the live
    #    name-this-person queue with 218 sightings behind it, which is an unanswerable
    #    question in the one position that guarantees it is read first.
    Check("platform.no-row-for-the-app", "55 the platform is not a person", 2,
          no_row(r"groupme")),
    Check("platform.no-page-for-the-app", "55 the platform is not a person", 2,
          no_page_matching(r"^groupme$")),
    Check("platform.not-in-the-name-queue", "55 the platform is not a person", 2,
          lambda c: (lambda hits: (
              not hits, "not queued" if not hits else f"queued: {hits}"))(
                  [r["handle"] for r in c.conn.execute("SELECT handle FROM unresolved")
                   if "system" in str(r["handle"]).lower()])),
    #    And the decoy that stops the fix being "ignore the system channel": the notice
    #    *contains* the message. Riley Morgan is the author, GroupMe is only the speaker,
    #    and a filter that drops the handle drops the plan with it.
    #    **Frontier, and it is a live defect rather than a hypothetical.**
    #    `groupme._deliver` returns early on `message.get("system")`, so the whole
    #    channel is dropped at ingest — including the notices that *are* the message.
    #    The live store has `DELIA edited to: "This is where we are planning to…"` and
    #    `Casey Morgan edited to: "No, Morgan is sitting th…"`, both destroyed on the
    #    way in and in no store. The structural fix is not a regex over the wording: the
    #    API sends `event: {type, data}` on every system message and the connector's
    #    field list has never named it. Read `event.type`
    #    and drop the bookkeeping kinds, keep the ones carrying a person's words.
    Check("platform.the-quoted-plan-still-lands", "55 the platform is not a person", 2,
          row_on(r"smash", SUN), frontier=True),
    #    Frontier for the same reason: there is no row yet to attribute wrongly.
    Check("platform.the-author-is-not-the-app", "55 the platform is not a person", 2,
          lambda c: (lambda rows: (
              bool(rows) and not any("groupme" in str(p).lower()
                                     for p in [rows[0].subject, *rows[0].participants]),
              f"subject={rows[0].subject!r} participants={rows[0].participants}"
              if rows else "no row"))(c.rows(r"smash", on=SUN)),
          frontier=True),

    # -- 46. The obligation nobody announced, buried in a thread about a video game.
    #    Integration states the contract; a red *model* result here is the extraction
    #    workstream and is meant to read loudly rather than as a known gap.
    Check("oblique.todo-exists", "46 oblique obligation", 3,
          todo_count(r"deposit", 1)),

    # -- 42. The day arrived, and went, and nobody ever said anything.
    Check("neon.question-retires", "42 a day passes in silence", 4,
          question_status(r"neon garden", "dropped")),
    Check("neon.row-survives", "42 a day passes in silence", 4,
          count_rows(r"neon garden", 1)),

    # -- 43. ...and the counter-case, which is the one that proves the split. An
    #    obligation attached to no occasion has no day to die with.
    Check("ezpass.survives-four-days", "43 an obligation is not a question", 4,
          lambda c: (lambda t: (len(t) == 1 and t[0].status == "open",
                                f"to-dos: {[(x.text, x.status) for x in t]}"))(
              c.todos(r"ez.?pass"))),

    # -- 47. A row that happens inside another row's span, and says so in its name.
    Check("elements.breakfast-exists", "47 containment", 3,
          row_on(r"breakfast", SAT)),
    Check("elements.breakfast-nested", "47 containment", 3,
          lambda c: (lambda child, parent: (
              bool(child and parent) and child.part_of == parent.id,
              f"part_of = {child.part_of if child else None}, "
              f"festival id = {parent.id if parent else None}"))(
                  c.one(r"breakfast", on=SAT), c.one(r"elements music festival"))),
    Check("elements.breakfast-not-merged", "47 containment", 3,
          count_rows(r"elements", 2)),
    Check("elements.nested-in-brief", "47 containment", 3,
          brief_has(r"↳ .*Breakfast")),
    # `series` would have merged these two rows outright, which is why `part_of` is a
    # column of its own and never consulted by matching.
    Check("elements.festival-keeps-its-span", "47 containment", 3,
          field_is(r"elements music festival", "until", SUN)),

    # -- 48. An invitation is a fact about how to act on it.
    Check("invite.rsvp-url-kept", "48 an invitation is actionable", 2,
          field_is(r"jack's 30th", "rsvp_url", "~partiful\\.com/e/jacks30")),
    Check("invite.unanswered-reads-not-replied", "48 an invitation is actionable", 2,
          lambda c: (lambda e: (bool(e) and e.plain_state() == "not replied",
                                f"reads {e.plain_state()!r}" if e else "(no row)"))(
              c.one(r"capture the flag"))),
    Check("invite.brief-names-the-link", "48 an invitation is actionable", 2,
          brief_has(r"invite: partiful\.com/e/ctf")),
    # It vanished from the feed, which is an observation and not a re-derivation, so it
    # keeps its authority to decline the row.
    Check("invite.disappearance-declines", "48 an invitation is actionable", 3,
          field_is(r"capture the flag", "status", "declined")),
    Check("invite.declined-stays-visible", "48 an invitation is actionable", 3,
          brief_has(r"Capture The Flag")),
    Check("invite.declined-keeps-its-link", "48 an invitation is actionable", 3,
          brief_has(r"Capture The Flag.*invite: partiful\.com/e/ctf")),
    Check("invite.declined-reads-not-going", "48 an invitation is actionable", 3,
          lambda c: (lambda e: (bool(e) and e.plain_state() == "not going",
                                f"reads {e.plain_state()!r}" if e else "(no row)"))(
              c.one(r"capture the flag"))),

    # -- The index/detail contract. The brief stopped carrying the address, the note
    #    and the logistics on 2026-08-13 and started carrying a handle that opens them.
    #    That trade is only sound if the second half is reliable, so it is graded: three
    #    of the checks above were rewritten from "the brief line says X" to "the brief
    #    names the row and its handle opens to X", and these hold the property itself.
    Check("brief.every-handle-opens", "index and detail", 2, every_handle_opens),
    Check("brief.every-handle-opens-at-the-end", "index and detail", 4,
          every_handle_opens),
    Check("brief.every-row-can-be-opened", "index and detail", 2,
          every_row_has_a_handle),
    # The wiki is indexed by the same contract and was the half nobody graded: the
    # brief named the pages and never what was on them, so nothing in context could
    # tell the page holding the answer from the ones that did not.
    Check("brief.pages-name-what-they-hold", "index and detail", 2,
          pages_name_what_they_hold),
    Check("brief.pages-still-describe-themselves-at-the-end", "index and detail", 4,
          pages_name_what_they_hold),
    # The other direction, and the one that catches a thinning that went too far: what
    # the line no longer says has to be somewhere a reader can still get it.
    Check("detail.has-the-address-the-line-dropped", "index and detail", 2,
          opens_with(r"poker", r"42 Example Street|Example St")),
    Check("detail.opens-a-todo", "index and detail", 2,
          opens_with(r"EZ.?Pass", r"EZ.?Pass")),

    # -- 53. A withheld field is not a value. Beat 48 reaches "not replied" with the
    #    location genuinely empty; this reaches it with the platform's placeholder
    #    sentence sitting in the field, which is the only spelling production sends.
    Check("invite.withheld-location-reads-not-replied", "53 a withheld field", 2,
          lambda c: (lambda e: (bool(e) and e.plain_state() == "not replied",
                                f"reads {e.plain_state()!r}" if e else "(no row)"))(
              c.one(r"mount aldon"))),
    # The placeholder is a status message, not a venue. It was rendering in the brief
    # where the address goes, one clause away from a note claiming the user had RSVP'd.
    Check("invite.withheld-location-is-not-stored", "53 a withheld field", 2,
          lambda c: (lambda e: (bool(e) and not (e.location or ""),
                                f"location={e.location!r}" if e else "(no row)"))(
              c.one(r"mount aldon"))),
    # The RSVP inference overwrote `note`, so the invitation's own words were destroyed
    # at ingest on every Partiful row that had a location.
    Check("invite.withheld-keeps-its-description", "53 a withheld field", 2,
          field_is(r"mount aldon", "note", "~Nadia")),
    # Withheld is not evidence of anything. The user attended their own ceremony without ever
    # replying, so this may never become a decline — only a disappearance may.
    Check("invite.withheld-is-not-declined", "53 a withheld field", 2,
          lambda c: (lambda e: (bool(e) and e.status != "declined",
                                f"status={e.status!r}" if e else "(no row)"))(
              c.one(r"mount aldon"))),
    # The reported symptom itself, which is a *line* and not a row: "location available
    # once rsvp, but it says partiful rsvp yes... i mean, lots going on in there!!!"
    # Both halves of that contradiction have to be gone from what a reader is handed.
    # Asserted together with the row's presence on purpose: `brief_lacks` alone is
    # vacuously green on a store that extracted nothing, which is the one way a check
    # about a *missing* string can pass by the whole beat having failed.
    Check("invite.withheld-line-is-not-contradictory", "53 a withheld field", 2,
          lambda c: (lambda t: (bool(re.search(r"Mount Aldon", t, re.I))
                                and not re.search(r"RSVP'd|RSVP yes", t, re.I),
                                "clean" if re.search(r"Mount Aldon", t, re.I)
                                else "(row never reached the brief)"))(c.brief())),
    Check("invite.withheld-line-reads-not-replied", "53 a withheld field", 2,
          brief_has(r"Mount Aldon.*not replied")),
    # As in beat 45, the rename changes every revision and the whole snapshot is
    # re-derived from the same withheld location.
    Check("invite.withheld-survives-rescan", "53 a withheld field", 3,
          lambda c: (lambda e: (bool(e) and e.plain_state() == "not replied"
                                and not (e.location or ""),
                                f"reads {e.plain_state()!r} at {e.location!r}"
                                if e else "(no row)"))(c.one(r"mount aldon"))),

    # -- 32, at the end. The closed-world inventory is the only check that catches a
    #    row nobody asked for — a targeted `no_row` regex cannot, because a
    #    hallucinated title evades one by definition. It stopped at day 2 while the
    #    corpus ran to day 4, so two whole days of writes had no invention guard at
    #    all. This is the same claim over the *final* state.
    Check("state.only-declared-events-at-the-end", "32 closed-world inventory", 4,
          only_expected_events([
              (r"poker", "2026-06-05"),
              (r"dinner", "2026-06-20"),
              (r"board game", "2026-07-18"),
              (r"aspca|mobile clinic", WED),
              (r"dog park", SAT),          # 54, proposed by an id nothing can name
              (r"climb", WED),
              (r"neon garden", WED),
              (r"ramen|dinner", THU),
              (r"elements music festival", FRI),
              (r"bowery|the show", FRI),
              (r"beer garden", SAT),
              (r"board game", SAT),
              (r"breakfast", SAT),
              (r"poker", SAT),
              (r"brunch", SUN),
              (r"Fantastic Four movie", SUN_9),
              (r"Spider-Man movie", MON_10),
              (r"movie", TUE_11),
              (r"Superman movie", TUE_11),
              (r"bbq|block party|barbecue", SAT_15),
              (r"housewarming", SAT_15),
              (r"physio", "2026-08-19"),
              (r"dentist", "2026-08-20"),
              (r"statehood", "2026-08-21"),
              (r"jack's 30th", "2026-08-22"),
              (r"capture the flag", "2026-08-23"),
              (r"tattoo|sasha", THU_20),
              (r"tutoring", WED_12),
              (r"bloodwork", "2026-08-06"),
              (r"mount aldon", MON_10),
          ])),
    Check("state.only-declared-todos-at-the-end", "32 closed-world inventory", 4,
          only_expected_text_rows(
              lambda c: [todo.text for todo in c.todos(".")],
              [r"venmo Cameron.*ticket", r"Rowan.*EZ.?Pass",
               r"Spider-Man tickets", r"Fantastic Four tickets", r"Superman tickets",
               r"Devon the deposit", r"Jira|SEC.?2847"],
              "to-dos")),
    Check("state.only-declared-questions-at-the-end", "32 closed-world inventory", 4,
          only_expected_text_rows(
              lambda c: [row["text"] for row in c.questions(".", status=None)],
              [r"Rowan|EZ.?Pass", r"Mom.*coming over", r"Neon Garden",
               r"housewarming"], "questions")),

    # -- 45, a day later. Nothing duplicated overnight either.
    Check("calendar.no-duplicate-after-rename", "45 rescan must not undo judgement", 4,
          count_rows(r"dentist", 1)),
    Check("jack30.still-one-row", "45 rescan must not undo judgement", 4,
          count_rows(r"jack's 30th", 1)),
    Check("feed.row-exists", "45 rescan must not undo judgement", 4,
          one_row(r"statehood")),
    # The row `brief._committed` exists to exclude. Asserted beside the row's own
    # existence, so it can never pass by the row having quietly vanished.
    Check("feed.stays-out-of-the-brief", "45 rescan must not undo judgement", 4,
          brief_lacks(r"statehood")),
]


# ---------------------------------------------------------------------- MEASURES --

MEASURES: list[Measure] = [
    Measure("m.rows", "memcal rows", 2,
            lambda c: str(c.conn.execute(
                "SELECT count(*) AS n FROM events").fetchone()["n"])),
    Measure("m.pages", "wiki pages", 2, lambda c: str(len(c.pages()))),
    Measure("m.blank-pages", "pages holding no fact", 2,
            lambda c: (lambda pages: (
                f"{sum(1 for p in pages if not (c.page(p) and (c.page(p).slots or (c.page(p).body or '').strip())))}"
                f" of {len(pages)}"))(c.pages())),
    Measure("m.questions", "open questions", 2,
            lambda c: str(len(c.questions(".")))),
    Measure("m.todos", "open to-dos", 2,
            lambda c: str(len([t for t in c.todos(".") if t.status == "open"]))),
    Measure("m.archive", "archived items", 2,
            lambda c: str(c.conn.execute(
                "SELECT count(*) AS n FROM archive").fetchone()["n"])),
    Measure("m.read", "lines a model actually read", 2,
            lambda c: str(c.conn.execute(
                "SELECT count(*) AS n FROM spool WHERE run_id IS NOT NULL").fetchone()["n"])),
    Measure("m.gate-signal", "labelled signal queued", 2,
            lambda c: (lambda s: f"{s['queued_signal']} of {s['signal']}")(
                gate_corpus_stats(c))),
    Measure("m.gate-noise", "labelled noise queued", 2,
            lambda c: (lambda s: f"{s['queued_noise']} of {s['noise']}")(
                gate_corpus_stats(c))),
]
