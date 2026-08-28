"""Stage 1 — bundle (code).

Group by entity or thread, across all streams. Jordan's text, the GroupMe line where
Jordan spoke, and Jordan's email land in one bundle. Splitting by source would
separate the things that must be joined; grouping by subject makes cross-platform
deduplication structural.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta

from .. import archive, db, threads

# Read the backlog by default; `pack()` enforces the request token budget.
MAX_ITEMS_PER_BUNDLE = 2_000
SPOOL_LIMIT = 20_000
CONTEXT_MINUTES = 120      # how far around a gated line to look for its neighbours
MAX_CONTEXT_PER_BUNDLE = 8


@dataclass
class Bundle:
    entity: str
    items: list[sqlite3.Row] = field(default_factory=list)
    spool_ids: list[int] = field(default_factory=list)
    #: A readable name for the conversation, filled in by `build`. The entity key is not
    #: one: an iMessage group chat with no display name keys as
    #: `thread:imessage:9858b62c161544bca4342589e0344bbe`, and that string was what the
    #: model and the UI both got told the bundle was called.
    title: str = ""
    #: How many pending items this entity has in total, when more were waiting than the
    #: per-entity share allowed. 0 means the bundle is everything there is.
    waiting: int = 0
    #: Other spool entities folded into this one — the same conversation arriving under
    #: two chat ids because iMessage split it across SMS and iMessage.
    merged: list[str] = field(default_factory=list)
    #: Readable names for each conversation in this bundle, so a multi-conversation
    #: bundle labels its lines "Crystal Harbor" rather than a chat guid.
    convo_titles: dict = field(default_factory=dict)

    @property
    def label(self) -> str:
        return self.title or self.entity.split(":", 1)[-1]

    @property
    def people(self) -> list[str]:
        names = {row["person"] for row in self.items if row["person"]}
        if self.entity.startswith("person:"):
            names.add(self.entity.split(":", 1)[1])
        return sorted(names)

    def cite(self, tags) -> list[int]:
        """Line tags the model quoted (`L7`) → the archive ids they name."""
        out: list[int] = []
        for tag in tags or ():
            index = None
            if isinstance(tag, int):
                index = tag
            elif isinstance(tag, str):
                digits = tag.strip().lstrip("Ll#").strip()
                if digits.isdigit():
                    index = int(digits)
            # 1-based, matching what the renderer prints.
            if index is not None and 1 <= index <= len(self.items):
                archive_id = int(self.items[index - 1]["id"])
                if archive_id not in out:
                    out.append(archive_id)
        return out

    def render(self, fmt: str | None = None, head: str | None = None) -> str:
        """The bundle as the model receives it, in one of the named formats below."""
        return FORMATS[fmt or DEFAULT_FORMAT](self, head)

    def _shape(self) -> str:
        """One line saying what this bundle is and what it is not.

        The `waiting` half matters more than it looks: without it a bundle that holds the
        newest 44 of 339 lines reads as the whole relationship, and a three-week silence
        that is really a truncation becomes evidence of a three-week silence.
        """
        convos = {(r["stream"], r["thread"]) for r in self.items}
        streams = sorted({r["stream"] for r in self.items})
        if len(convos) == 1 and streams == ["email"]:
            # Email identity is often unresolved on first load, so its bundle is keyed
            # by thread rather than person. That says nothing about group membership:
            # "thread:email:…" is an email thread, not a "group chat on email".
            shape = "email thread"
        else:
            kind = ("one-to-one"
                    if self.entity.startswith("person:") and len(convos) == 1
                    else f"{len(convos)} conversations"
                    if len(convos) > 1
                    else "group chat")
            shape = f"{kind} on {'+'.join(streams)}"
        span = ""
        if self.items:
            lo, hi = str(self.items[0]["ts"])[:10], str(self.items[-1]["ts"])[:10]
            # A one-day bundle states this again in the mandatory day heading directly
            # below. The range adds information; the single date only duplicates it.
            span = f" · {lo} to {hi}" if lo != hi else ""
        unit = "line" if len(self.items) == 1 else "lines"
        counted = (f"newest {len(self.items)} of {len(self.items) + self.waiting} {unit}"
                   if self.waiting else f"{len(self.items)} {unit}")
        return f"{shape} · {counted}{span}"


# ------------------------------------------------------------------ formats --
#
# What a bundle looks like on the wire. Separated from the Bundle itself so the corpus
# a benchmark feeds in stays neutral — structured records of who said what, where and
# when — and the question "does the model do better without the stream tag on every
# line" is answered by re-rendering the same data, not by regenerating it.
#
# Every format must keep two things, because code depends on them rather than taste:
# whatever `head` it is handed, alone on the first line (v1 routing echoes the default
# one; v2 supplies its own id and any second name for the bundle is a routing failure
# waiting to happen), and one line per message.


def _addressed_to(row) -> str:
    """`person` unless the row says otherwise. Tolerates a row assembled by hand."""
    try:
        return str(row["addressed_to"] or "person")
    except (IndexError, KeyError, TypeError):
        return "person"


def _render_v1(bundle: "Bundle", head: str | None = None) -> str:
    """Render the original readable bundle format with dated conversation gaps."""
    # The entity line is what the model echoes back to route its diff under v1, so it
    # stays exactly as it is. The name goes beside it — an opaque chat id tells the
    # model as little as it told us, and "Me, Quinn, and Jamie" is the difference
    # between reading a group chat as a group chat and reading it as a stranger.
    if head is None:
        head = f"BUNDLE {bundle.entity}"
        if bundle.title and bundle.title != bundle.entity.split(":", 1)[-1]:
            head += f"   ({bundle.title})"
    lines = ([head] if head else []) + [bundle._shape()]

    single = len({(r["stream"], r["thread"]) for r in bundle.items}) == 1
    day = None
    previous = None
    for index, row in enumerate(bundle.items, start=1):
        when = db.parse_ts(str(row["ts"]))
        # Consecutive days are clear from headings; mark only longer gaps.
        if previous is not None and (when - previous).total_seconds() > 48 * 3600:
            quiet = round((when - previous).total_seconds() / 86400)
            lines.append(f"  … {quiet} day{'s' if quiet != 1 else ''} "
                         f"with nothing said …")
        previous = when
        if when.date() != day:
            day = when.date()
            lines.append(f"-- {day.strftime('%a %Y-%m-%d')} --")

        who = "me" if row["from_me"] else (row["person"] or row["handle"] or "unknown")
        # Distinguish a user statement from an instruction addressed to the agent.
        if who == "me" and _addressed_to(row) == "machine":
            who = "me → assistant"
        # The stream stays on every line — the instructions lean on it to identify the
        # agent stream. The thread only earns its place when the bundle holds more
        # than one, which is exactly when it says something.
        where = row["stream"] if single else (
            f"{row['stream']}/{bundle.convo_titles.get((row['stream'], row['thread']), row['thread'] or '?')}")
        body = str(row["text"] or "").strip()
        first, *rest = body.split("\n")
        # Line tags are compact citations resolved to archive ids by `Bundle.cite`.
        lines.append(f"  L{index} {when.strftime('%H:%M')} ({where}) {who}: {first}")
        # A message with a newline in it would otherwise look like a second message
        # with no speaker.
        lines.extend(f"      {part.strip()}" for part in rest if part.strip())
    return "\n".join(lines)


#: Gate verdicts that indicate planning content. `subject-event` is omitted because
#: subject-line matches are not comparable with message-level verdicts.
_PLANNING_REASONS = frozenset({
    "temporal", "invitation", "commitment-verb", "own-commitment", "directive",
    "availability", "question",
})


def importance(conn: sqlite3.Connection, bundle: "Bundle") -> float:
    """Return a cold-start ordering score using signals comparable across streams."""
    if not bundle.items:
        return 0.0
    score = 0.0
    if any(r["from_me"] for r in bundle.items):
        score += 45.0                          # the user talks back: a relationship, not a feed
    if bundle.entity.startswith("person:"):
        score += 30.0                          # a resolved human, not an opaque thread
        name = bundle.entity.split(":", 1)[1]
        if conn.execute("SELECT 1 FROM top_tier WHERE person = ?", (name,)).fetchone():
            score += 60.0
    planning = sum(1 for r in bundle.items
                   if (r["gate_reason"] or "") in _PLANNING_REASONS)
    # Capped low on purpose: this is corroborating evidence, never the deciding vote,
    # because its availability depends on which stream the line arrived by.
    score += min(20.0, planning * 2.0)
    subject_only = sum(1 for r in bundle.items if r["gate_reason"] == "subject-event")
    score += min(8.0, subject_only * 0.5)
    try:
        newest = max(db.parse_ts(str(r["ts"])).date() for r in bundle.items)
        score += max(0.0, 25.0 - (db.today() - newest).days)
    except ValueError:
        pass
    return score


def cold_start_order(conn: sqlite3.Connection, bundles: list["Bundle"]) -> list["Bundle"]:
    """Most worth reading first. Stable, so a re-run reads in the same order."""
    return sorted(bundles, key=lambda b: (-importance(conn, b), b.entity))


def _render_v2_quiet_stream(bundle: "Bundle", head: str | None = None) -> str:
    """v1 with the per-line stream tag dropped wherever the header already said it.

    The hypothesis worth measuring: on a single-stream bundle `(imessage)` is on every
    line and carries nothing the shape line did not already say, so it is pure repeated
    tokens between the time and the speaker.

    The exception is not negotiable. The `agent` stream is them talking to their assistant,
    which the instructions call the most reliable source in the system — that tag is
    required for attribution and stays on the line even when it is the only stream present.
    """
    text = _render_v1(bundle, head)
    streams = {r["stream"] for r in bundle.items}
    if len({(r["stream"], r["thread"]) for r in bundle.items}) != 1:
        return text            # multi-conversation: the tag is disambiguating, keep it
    if streams == {"agent"} or "agent" in streams:
        return text
    only = next(iter(streams))
    return text.replace(f" ({only}) ", " ")


#: name -> renderer. Add one, measure it, keep it or delete it.
FORMATS = {
    "v1": _render_v1,
    "v2-quiet-stream": _render_v2_quiet_stream,
}
DEFAULT_FORMAT = "v1"


def build(conn: sqlite3.Connection, limit: int = SPOOL_LIMIT,
          per_entity: int = MAX_ITEMS_PER_BUNDLE) -> list[Bundle]:
    pending = archive.spool_pending(conn, limit=limit, per_entity=per_entity)
    # One conversation, one bundle — even when the platform gave it two chat ids.
    alias = threads.aliases(conn)
    grouped: dict[str, Bundle] = {}
    for row in pending:
        key = threads.fold_entity(row["entity"], alias)
        bundle = grouped.setdefault(key, Bundle(entity=key))
        if key != row["entity"] and row["entity"] not in bundle.merged:
            bundle.merged.append(row["entity"])
        # The spool id goes on the bundle only when the line goes into it. These two used
        # to be separate: every pending row was marked read, but only the first 60 per
        # bundle were rendered, so raising `items_per_entity` — the knob the config offers
        # for exactly this — silently retired the surplus unread. A line is marked read
        # when a model has seen it, and not before.
        if per_entity <= 0 or len(bundle.items) < per_entity:
            bundle.items.append(row)
            bundle.spool_ids.append(row["spool_id"])
    bundles = [b for b in grouped.values() if b.items]

    total: dict[str, int] = {}
    for row in archive.spool_shape(conn):
        key = threads.fold_entity(row["entity"], alias)
        total[key] = total.get(key, 0) + row["n"]
    names = threads.titles(conn)
    for bundle in bundles:
        bundle.waiting = max(0, total.get(bundle.entity, 0) - len(bundle.spool_ids))
        bundle.title = _title_for(bundle, names)
        # Items arrive newest-first per entity, so a merged bundle interleaves two
        # conversations out of order until this puts them back on one timeline.
        bundle.items.sort(key=lambda r: str(r["ts"]))
        add_thread_context(conn, bundle)
        bundle.convo_titles = {
            (r["stream"], r["thread"]): names.get((r["stream"], r["thread"]),
                                                  r["thread"] or r["stream"])
            for r in bundle.items}
    return bundles


def _title_for(bundle: Bundle, names: dict[tuple, str]) -> str:
    """What to call this bundle. A person bundle is already named; a thread is not."""
    kind, _, rest = bundle.entity.partition(":")
    if kind == "person":
        return rest
    if kind == "thread":
        stream, _, thread = rest.partition(":")
        return names.get((stream, thread), thread)
    return rest


def add_thread_context(conn: sqlite3.Connection, bundle: Bundle) -> None:
    """Pull in the neighbours of each gated line, even though they failed the gate.

    "u going tomorrow" passes; the "yeah" that answers it does not, and on its own it
    never would. The association only exists if both are in the same context, so the
    gate governs what we *look* at and this restores what it takes to read it.
    """
    seen = {row["id"] for row in bundle.items}
    anchors = [(row["stream"], row["thread"], db.parse_ts(row["ts"]))
               for row in bundle.items if row["thread"]]
    if not anchors:
        bundle.items.sort(key=lambda r: str(r["ts"]))
        return

    extra: list[sqlite3.Row] = []
    window = timedelta(minutes=CONTEXT_MINUTES)
    for stream, thread in {(s, t) for s, t, _ in anchors}:
        times = [ts for s, t, ts in anchors if (s, t) == (stream, thread)]
        # Bound the scan by date prefix: format-agnostic, unlike SQLite date math on
        # timestamps that may or may not carry a T and an offset.
        lo = (min(times) - window).date().isoformat()
        hi = (max(times) + window).date().isoformat()
        rows = conn.execute(
            """SELECT a.* FROM archive a
               WHERE a.stream = ? AND a.thread = ? AND substr(a.ts, 1, 10) BETWEEN ? AND ?
                 AND a.id NOT IN (SELECT archive_id FROM spool)
               ORDER BY a.ts""",
            (stream, thread, lo, hi),
        ).fetchall()
        for neighbour in rows:
            if neighbour["id"] in seen or len(extra) >= MAX_CONTEXT_PER_BUNDLE:
                continue
            when = db.parse_ts(neighbour["ts"])
            if any(abs(when - anchor) <= window for anchor in times):
                seen.add(neighbour["id"])
                extra.append(neighbour)
    if extra:
        bundle.items = sorted(bundle.items + extra, key=lambda r: str(r["ts"]))
    else:
        bundle.items.sort(key=lambda r: str(r["ts"]))


def all_text(bundles: list[Bundle]) -> str:
    """Every gated line in one blob — what wake conditions get checked against."""
    return "\n".join(row["text"] for b in bundles for row in b.items)


def stats(bundles: list[Bundle]) -> str:
    items = sum(len(b.items) for b in bundles)
    chars = sum(len(r["text"] or "") for b in bundles for r in b.items)
    return f"{len(bundles)} bundles · {items} items · ~{chars // 4} tokens of traffic"


def watermark(conn: sqlite3.Connection) -> str | None:
    return db.get_meta(conn, "dream.watermark")


def set_watermark(conn: sqlite3.Connection, value: str) -> None:
    db.set_meta(conn, "dream.watermark", value)
