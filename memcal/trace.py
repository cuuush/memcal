"""Index model calls and fetch legacy content from OpenRouter.

`calls.py` stores current prompts and responses locally. This module keeps generation
metadata in SQLite and provides the fallback for calls made before local logging.

    memcal trace              # recent calls
    memcal trace <id|run>     # the prompt, the reasoning, the reply
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.parse
import urllib.request

from . import calls, dates, db

API = "https://openrouter.ai/api/v1"


def record(conn: sqlite3.Connection, *, run_id: int | None, stage: str, label: str,
           reply, max_tokens: int = 0, home=None, prefix: str = "", suffix: str = "",
           bundles: list[dict] | None = None) -> None:
    """Remember where OpenRouter filed this call, and write the call itself to disk.

    The row is the index; the file is the content. Pass `home` and every caller gets
    an offline trace for free — see `calls.py` for why that stopped being optional.
    """
    generation_id = (getattr(reply, "generation_id", "") or "").strip()
    if not generation_id:
        return
    if home is not None:
        calls.save(home, reply=reply, stage=stage, run_id=run_id, label=label,
                   model=getattr(reply, "model", ""), prefix=prefix, suffix=suffix,
                   max_tokens=max_tokens, bundles=bundles)
    usage = getattr(reply, "usage", None)
    try:
        conn.execute(
            """INSERT INTO generations(run_id, generation_id, stage, label, model,
                                       prompt_tokens, completion_tokens, cost_usd,
                                       max_tokens, requests, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(generation_id) DO NOTHING""",
            (run_id, generation_id, stage, label[:200], getattr(reply, "model", ""),
             getattr(usage, "prompt_tokens", 0), getattr(usage, "completion_tokens", 0),
             getattr(usage, "cost", 0.0), int(max_tokens or 0),
             int(getattr(reply, "requests", 1) or 1), db.now()),
        )
        conn.commit()
    except sqlite3.Error:
        pass


def stamp(conn: sqlite3.Connection, *, kind: str, ref: str, verb: str | None = None,
          entity: str | None = None, stage: str = "propose", run_id: int | None = None,
          generation_id: str | None = None,
          archive_ids: list[int] | tuple[int, ...] | None = None,
          strict: bool = False) -> None:
    """Remember which call wrote one row and which original lines it was reading.

    `provenance` points to the model call; `evidence` points to the archive, which is the
    source a person or agent usually wants when they click a brief line. Callers that
    promise an audited write pass ``strict=True`` so neither can be lost silently.
    """
    if not ref:
        return
    try:
        conn.execute(
            """INSERT INTO provenance(kind, ref, verb, entity, stage, run_id,
                                      generation_id, at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (kind, ref, verb, entity, stage, run_id, generation_id or None, db.now()),
        )
        for archive_id in dict.fromkeys(int(i) for i in (archive_ids or []) if i):
            conn.execute(
                """INSERT OR IGNORE INTO evidence(
                       kind, ref, archive_id, entity, run_id, generation_id, attached_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (kind, ref, archive_id, entity, run_id, generation_id or None, db.now()),
            )
    except sqlite3.Error:
        if strict:
            raise


def history(conn: sqlite3.Connection, kind: str, ref: str) -> list[sqlite3.Row]:
    """Every call that has ever written to one row, newest first, with its token counts."""
    return conn.execute(
        """SELECT p.*, g.model, g.prompt_tokens, g.completion_tokens, g.cost_usd, g.label
             FROM provenance p LEFT JOIN generations g
                    ON g.generation_id = p.generation_id
            WHERE p.kind = ? AND p.ref = ? ORDER BY p.id DESC""",
        (kind, ref),
    ).fetchall()


def timeline(conn: sqlite3.Connection, kind: str, ref: str,
             changes: list[dict] | None = None) -> list[dict]:
    """One entry per *write*, with the field edits and the lines that justified it."""
    stamps = list(conn.execute(
        """SELECT p.*, g.model, g.cost_usd FROM provenance p
             LEFT JOIN generations g ON g.generation_id = p.generation_id
            WHERE p.kind = ? AND p.ref = ? ORDER BY p.at, p.id""", (kind, ref)))
    cited: dict[str, list[dict]] = {}
    for row in conn.execute(
        """SELECT e.generation_id, a.* FROM evidence e JOIN archive a ON a.id = e.archive_id
            WHERE e.kind = ? AND e.ref = ? ORDER BY a.ts""", (kind, ref)):
        cited.setdefault(str(row["generation_id"] or ""), []).append(_line(row))

    entries: list[dict] = []
    for stamp in stamps:
        gen = str(stamp["generation_id"] or "")
        entries.append({
            "at": str(stamp["at"])[:19],
            "verb": stamp["verb"] or "written",
            "stage": stamp["stage"] or "code",
            "entity": stamp["entity"] or "",
            "run": stamp["run_id"],
            "gen": gen,
            "model": (stamp["model"] or "").split("/")[-1],
            "changes": [],
            # A call's own citations; a structured write shares the un-attributed pool.
            "cites": cited.get(gen, []) if gen else cited.get("", []),
        })

    for change in changes or []:
        at = str(change.get("at") or "")[:19]
        for entry in entries:
            if entry["at"] == at:
                entry["changes"].append(change)
                break
        else:
            entries.append({
                "at": at, "verb": "updated", "stage": change.get("by") or "direct",
                "entity": "", "run": None, "gen": "", "model": "",
                "changes": [change], "cites": [],
                # Nothing stamped this. A hand repair against SQLite is the honest
                # reading, and saying so beats rendering it as an ordinary write.
                "unstamped": True,
            })
    entries.sort(key=lambda entry: entry["at"])
    return entries


def wrote(conn: sqlite3.Connection, generation_id: str) -> list[sqlite3.Row]:
    """Everything one call put in the store — the other direction of the same link."""
    return conn.execute(
        "SELECT * FROM provenance WHERE generation_id = ? ORDER BY id",
        (generation_id,),
    ).fetchall()


#: Streams whose `thread` is an item id rather than a conversation. Do not add adjacent
#: context for these streams; neighboring rows are unrelated items.
UNTHREADED_STREAMS = frozenset({"ical"})


def source_rows(conn: sqlite3.Connection, kind: str, ref: str,
                *, context: int = 2, limit: int = 40) -> list[dict]:
    """Original archive lines behind one derived row, with a little thread context.

    New writes use the explicit `evidence` link. Older writes predate that table, so
    fall back to the spool rows consumed by the same run and entity. That fallback is
    intentionally conservative: it returns the source bundle, never a guessed global
    text search that could make an unrelated line look like corroboration.
    """
    linked = conn.execute(
        """SELECT DISTINCT a.* FROM evidence e
             JOIN archive a ON a.id = e.archive_id
            WHERE e.kind = ? AND e.ref = ?
            ORDER BY a.ts LIMIT ?""", (kind, ref, limit)
    ).fetchall()
    if not linked:
        history_rows = conn.execute(
            """SELECT run_id, entity FROM provenance
                WHERE kind = ? AND ref = ? AND run_id IS NOT NULL AND entity IS NOT NULL
                ORDER BY id DESC""", (kind, ref)
        ).fetchall()
        seen: set[int] = set()
        recovered = []
        for stamp in history_rows:
            rows = conn.execute(
                """SELECT a.* FROM spool s JOIN archive a ON a.id = s.archive_id
                    WHERE s.run_id = ? AND s.entity = ? ORDER BY a.ts LIMIT ?""",
                (stamp["run_id"], stamp["entity"], limit),
            ).fetchall()
            for row in rows:
                if row["id"] not in seen:
                    seen.add(row["id"])
                    recovered.append(row)
        linked = recovered[:limit]
    if not linked:
        return []

    ids = {row["id"] for row in linked}
    expanded: dict[int, sqlite3.Row] = {row["id"]: row for row in linked}
    if context:
        for row in linked:
            if not row["thread"] or row["stream"] in UNTHREADED_STREAMS:
                continue
            before = conn.execute(
                """SELECT * FROM archive
                    WHERE stream = ? AND thread = ? AND
                          (ts < ? OR (ts = ? AND id < ?))
                    ORDER BY ts DESC, id DESC LIMIT ?""",
                (row["stream"], row["thread"], row["ts"], row["ts"], row["id"],
                 context),
            ).fetchall()
            after = conn.execute(
                """SELECT * FROM archive
                    WHERE stream = ? AND thread = ? AND
                          (ts > ? OR (ts = ? AND id > ?))
                    ORDER BY ts, id LIMIT ?""",
                (row["stream"], row["thread"], row["ts"], row["ts"], row["id"],
                 context),
            ).fetchall()
            neighbours = [*reversed(before), *after]
            for neighbour in neighbours:
                expanded.setdefault(neighbour["id"], neighbour)

    rows = [{
        "id": row["id"],
        "ts": str(row["ts"]),
        "stream": row["stream"],
        "thread": row["thread"] or "",
        "who": "me" if row["from_me"] else (row["person"] or row["handle"] or "?"),
        "text": row["text"] or "",
        "evidence": row["id"] in ids,
    } for row in sorted(expanded.values(), key=lambda r: (str(r["ts"]), r["id"]))[:limit]]
    return _mark_source_shifts(conn, rows)


def _mark_source_shifts(conn: sqlite3.Connection, rows: list[dict]) -> list[dict]:
    """Mark each change of conversation or day in a source timeline."""
    names = titles(conn)
    previous = None
    for row in rows:
        stamp = db.parse_ts(row["ts"])
        key = (row["stream"], row["thread"])
        changed_conversation = previous is None or key != previous[0]
        changed_day = previous is not None and stamp.date() != previous[1].date()
        if changed_conversation or changed_day:
            where = names.get(key, "") or row["thread"]
            if row["stream"] == "agent" and row["thread"].startswith("hermes:"):
                channel = "Hermes chat"
            elif row["stream"] == "imessage":
                channel = f"iMessage with {where}" if where else "iMessage"
            else:
                channel = f"{row['stream']} · {where}" if where else row["stream"]
            gap = ""
            if previous is not None:
                days = (stamp.date() - previous[1].date()).days
                if days:
                    gap = f" · {days} day{'s' if days != 1 else ''} later"
            when = f"{stamp:%a %b} {stamp.day}, {stamp:%Y %H:%M}"
            row["source_heading"] = f"{when} · {channel}{gap}"
        previous = (key, stamp)
    return rows


def citations(conn: sqlite3.Connection, kind: str, ref: str) -> dict:
    """How well a row is backed up, in the few numbers worth saying out loud.

    Every surface that shows a memory wants the same one-line answer — "3 lines, from
    Lootbox Addicts Support Group, 31 July" — and none of them wants to fetch and count
    the lines to get it. `narrow` is the part that matters most: it separates a row
    pointing at the two messages that made it from one pointing at a whole conversation
    because nothing could be narrowed, and the second is the shape that let a question
    about a film nobody mentioned look thoroughly evidenced.
    """
    rows = conn.execute(
        """SELECT a.stream, a.thread, a.ts FROM evidence e JOIN archive a ON a.id = e.archive_id
            WHERE e.kind = ? AND e.ref = ?""", (kind, ref)).fetchall()
    spooled = conn.execute(
        """SELECT count(DISTINCT s.id) AS n FROM provenance p
             JOIN spool s ON s.run_id = p.run_id AND s.entity = p.entity
            WHERE p.kind = ? AND p.ref = ?""", (kind, ref)).fetchone()["n"]
    names = titles(conn) if rows else {}
    where = sorted({names.get((r["stream"], r["thread"]), r["thread"] or r["stream"])
                    for r in rows})
    stamps = sorted(str(r["ts"]) for r in rows)
    return {
        "lines": len(rows),
        "conversations": where,
        "first": stamps[0][:16] if stamps else "",
        "last": stamps[-1][:16] if stamps else "",
        # The same two stamps with the weekday said rather than implied. Both forms,
        # because the ISO one is what a caller sorts and compares on and the readable one
        # is what stops a model doing the arithmetic itself — asked where a to-do came
        # from, one read `2026-08-10T14:52` and answered "Sunday, Aug 10". Monday.
        "first_said": dates.said_on(stamps[0]) if stamps else "",
        "last_said": dates.said_on(stamps[-1]) if stamps else "",
        # A handful of lines out of a conversation is a citation. The whole conversation
        # is the fallback wearing a citation's clothes.
        "narrow": bool(rows) and (spooled == 0 or len(rows) < spooled),
    }


def titles(conn: sqlite3.Connection) -> dict[tuple, str]:
    """Thread names, imported late to keep `threads` out of this module's import cycle."""
    from . import threads                                          # noqa: PLC0415
    return threads.titles(conn)


def conversation(conn: sqlite3.Connection, *, stream: str, thread: str,
                 around: str = "", before: int = 12, after: int = 12,
                 limit: int = 60) -> list[dict]:
    """The exchange around one moment, as it was actually said.

    `source_rows` gives two lines either side, which is enough to read a citation and
    not enough to answer "what were they talking about". This is the rest of it: the
    conversation, in order, centred on the line that was cited.
    """
    if around:
        earlier = conn.execute(
            """SELECT * FROM archive WHERE stream = ? AND thread = ? AND ts <= ?
                ORDER BY ts DESC, id DESC LIMIT ?""",
            (stream, thread, around, before)).fetchall()
        later = conn.execute(
            """SELECT * FROM archive WHERE stream = ? AND thread = ? AND ts > ?
                ORDER BY ts, id LIMIT ?""", (stream, thread, around, after)).fetchall()
        rows = [*reversed(earlier), *later]
    else:
        rows = list(reversed(conn.execute(
            """SELECT * FROM archive WHERE stream = ? AND thread = ?
                ORDER BY ts DESC, id DESC LIMIT ?""", (stream, thread, limit)).fetchall()))
    return [_line(row) for row in rows[:limit]]


def _line(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "ts": str(row["ts"])[:16],
        "stream": row["stream"],
        "thread": row["thread"] or "",
        "who": "me" if row["from_me"] else (row["person"] or row["handle"] or "?"),
        "text": row["text"] or "",
    }


def line(conn: sqlite3.Connection, archive_id: int) -> dict:
    """One archived message by id, with enough about it to go and read the rest."""
    row = conn.execute("SELECT * FROM archive WHERE id = ?", (archive_id,)).fetchone()
    return _line(row) if row else {}


def resolve_source(conn: sqlite3.Connection, token: str) -> dict:
    """Resolve a brief handle such as ``E46`` to the row and its archive evidence."""
    text = (token or "").strip().upper().strip("〔〕")
    if len(text) < 2 or not text[1:].isdigit():
        return {"error": f"invalid source handle {token!r}"}
    row_id = int(text[1:])
    spec = {
        "E": ("event", "events", "key", "title"),
        "T": ("todo", "todos", "key", "text"),
        "Q": ("question", "questions", "key", "text"),
        "S": ("standing", "standing", "key", "value"),
    }.get(text[0])
    if not spec:
        return {"error": f"invalid source handle {token!r}"}
    kind, table, key_col, label_col = spec
    row = conn.execute(
        f"SELECT * FROM {table} WHERE id = ?", (row_id,)
    ).fetchone()
    if not row:
        return {"error": f"no row for {text}"}
    ref = str(row[key_col])
    cited = citations(conn, kind, ref)
    out = {
        "source": text,
        "kind": kind,
        "ref": ref,
        "label": str(row[label_col] or ""),
        "citations": cited,
        "evidence": source_rows(conn, kind, ref),
    }
    if not cited["narrow"]:
        # Said plainly, because the alternative is a reader assuming that forty lines of
        # a group chat are forty pieces of evidence. They are the conversation this came
        # out of; no line in it was ever pointed at.
        out["caveat"] = ("no line-level citation — these are the conversation this row "
                         "came out of, not the lines it was built from")
    return out


def recent(conn: sqlite3.Connection, limit: int = 20, run_id: int | None = None
           ) -> list[sqlite3.Row]:
    if run_id is not None:
        return conn.execute(
            "SELECT * FROM generations WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
    return conn.execute(
        "SELECT * FROM generations ORDER BY id DESC LIMIT ?", (limit,)).fetchall()


def find(conn: sqlite3.Connection, needle: str) -> list[sqlite3.Row]:
    """A generation id, a run number, or part of a label. Whatever they typed."""
    needle = (needle or "").strip()
    if not needle:
        return recent(conn)
    if needle.startswith("gen-"):
        return conn.execute(
            "SELECT * FROM generations WHERE generation_id = ?", (needle,)).fetchall()
    if needle.isdigit():
        return recent(conn, run_id=int(needle))
    return conn.execute(
        "SELECT * FROM generations WHERE label LIKE ? ORDER BY id DESC LIMIT 10",
        (f"%{needle}%",)).fetchall()


class TraceError(RuntimeError):
    pass


def fetch(api_key: str, generation_id: str) -> dict:
    """The stored prompt, reasoning and completion for one call."""
    if not api_key:
        raise TraceError("no OpenRouter API key")
    req = urllib.request.Request(
        f"{API}/generation/content?id={urllib.parse.quote(generation_id)}",
        headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            if exc.code == 404:
                raise TraceError(
                    "OpenRouter has no stored content for this call. Input & Output Logging "
                    "only keeps generations made after it was switched on — see "
                    "https://openrouter.ai/settings/privacy") from exc
            detail = exc.read().decode("utf-8", "replace")[:200]
            raise TraceError(f"HTTP {exc.code}: {detail}") from exc
        finally:
            exc.close()
    except urllib.error.URLError as exc:
        raise TraceError(str(exc)) from exc
    return payload.get("data") or {}


def stats(api_key: str, generation_id: str) -> dict:
    """Cost, latency, provider and native token counts for one call."""
    req = urllib.request.Request(
        f"{API}/generation?id={urllib.parse.quote(generation_id)}",
        headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return (json.loads(resp.read().decode()) or {}).get("data") or {}
    except (urllib.error.URLError, ValueError):
        return {}


def render(content: dict, stats_row: dict | None = None) -> str:
    """One call, laid out to be read."""
    lines: list[str] = []
    if stats_row:
        lines.append(
            f"provider {stats_row.get('provider_name')}  model {stats_row.get('model')}  "
            f"${stats_row.get('total_cost', 0):.4f}  {stats_row.get('latency')}ms")
        lines.append(
            f"tokens: {stats_row.get('native_tokens_prompt')} in "
            f"({stats_row.get('native_tokens_cached')} cached) · "
            f"{stats_row.get('native_tokens_completion')} out · "
            f"{stats_row.get('native_tokens_reasoning')} reasoning")
        lines.append("")

    for message in (content.get("input") or {}).get("messages") or []:
        body = message.get("content")
        if isinstance(body, list):
            body = "\n".join(part.get("text") or "" for part in body
                             if isinstance(part, dict))
        lines.append(f"───── {str(message.get('role', '?')).upper()} " + "─" * 40)
        lines.append(str(body or "").strip())
        lines.append("")

    output = content.get("output") or {}
    if output.get("reasoning"):
        lines.append("───── REASONING " + "─" * 40)
        lines.append(str(output["reasoning"]).strip())
        lines.append("")
    lines.append("───── COMPLETION " + "─" * 39)
    completion = output.get("completion") or ""
    try:
        completion = json.dumps(json.loads(completion), indent=2, ensure_ascii=False)
    except (ValueError, TypeError):
        pass
    lines.append(str(completion).strip())
    return "\n".join(lines)
