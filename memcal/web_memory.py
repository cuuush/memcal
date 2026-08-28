"""Typed-memory and provenance projections for the local UI."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import timedelta

from . import archive, brief, calls, db, detail, identity, todos, trace, wiki
from .config import Config
from .dream import propose as propose_stage


def overview(conn: sqlite3.Connection, cfg: Config, days: int = 14) -> dict:
    since = (db.today() - timedelta(days=days)).isoformat()
    behind = dict(archive.stale_streams(conn, cfg=cfg))
    fresh = {r["stream"]: r for r in archive.freshness(conn, cfg)}
    streams = []
    rows = conn.execute(
        """SELECT stream, count(*) AS n, sum(gated) AS gated,
                  sum(gate_reason = 'calendar-structured') AS structured,
                  sum(length(text)) AS chars
             FROM archive
            WHERE ts >= ? AND NOT (stream = 'ical' AND external_id LIKE 'snapshot:%')
            GROUP BY stream ORDER BY n DESC""",
        (since,),
    ).fetchall()
    for row in rows:
        seen = fresh.get(row["stream"])
        streams.append({
            "stream": row["stream"],
            "n": row["n"],
            "gated": row["gated"] or 0,
            "structured": row["structured"] or 0,
            "tokens": (row["chars"] or 0) // 4,
            "last_seen": db.age_phrase(seen["newest"]) if seen else "?",
            "stale": behind.get(row["stream"]),
        })

    # The last pass that actually ran. A dry run is recorded but wrote nothing by
    # design, and showing it here reads as a pass that found nothing.
    run = conn.execute(
        "SELECT * FROM runs WHERE mode != 'dry-run' ORDER BY id DESC LIMIT 1").fetchone()
    spend = conn.execute(
        "SELECT sum(cost_usd) AS c FROM runs WHERE started_at >= ?", (since,)
    ).fetchone()["c"] or 0.0
    text = brief.render(conn, cfg)
    return {
        "days": days,
        "streams": streams,
        # Counted directly. `len(spool_pending(...))` used to stand in for this and now
        # would not: pending is capped per conversation, so it reports what a pass will
        # read, which is a different and smaller number than what is waiting.
        "pending": conn.execute(
            "SELECT count(*) n FROM spool WHERE processed_at IS NULL").fetchone()["n"],
        "events": conn.execute("SELECT count(*) AS n FROM events").fetchone()["n"],
        "todos": len(todos.open_items(conn)),
        "questions": len(todos.open_questions(conn, limit=1000)),
        "pages": len(wiki.list_pages(cfg.wiki_dir)),
        "unresolved": len(identity.unresolved(conn, limit=100000)),
        "brief_tokens": brief.approx_tokens(text),
        "brief_cap": cfg.brief_token_cap,
        "prefix_tokens": brief.approx_tokens(propose_stage.build_prefix(conn, cfg)),
        "spend": round(spend, 4),
        "horizon": cfg.spool_horizon_days,
        "propose_model": cfg.propose_model,
        "last_run": None if not run else {
            "id": run["id"], "at": str(run["started_at"])[:16], "mode": run["mode"],
            "bundles": run["bundles"], "diffs": run["diffs"],
            "cost": round(run["cost_usd"], 4), "error": run["error"],
        },
    }


def memory(conn: sqlite3.Connection, cfg: Config) -> dict:
    """Exactly what the agent sees, with a target behind each source handle."""
    text = brief.render(conn, cfg)
    lines = brief.structured(text)
    targets = {}
    last_run = conn.execute(
        """SELECT id FROM runs
            WHERE mode != 'dry-run' AND finished_at IS NOT NULL
            ORDER BY id DESC LIMIT 1""").fetchone()
    for token in {token for line in lines for token in line["sources"]}:
        parsed = brief.parse_source(token)
        if not parsed:
            continue
        kind, row_id = parsed
        row = conn.execute(
            f"SELECT key FROM {_TABLE[kind]} WHERE id = ?", (row_id,)).fetchone()
        if not row:
            continue
        change = ""
        if last_run:
            verbs = {stamp["verb"] for stamp in conn.execute(
                """SELECT verb FROM provenance
                    WHERE run_id = ? AND kind = ? AND ref = ?""",
                (last_run["id"], kind, row["key"]),
            )}
            if verbs:
                change = ("new" if verbs & {"inserted", "opened", "asked", "added"}
                          else "edited")
        targets[token] = {
            "kind": kind, "ref": row["key"],
            "last_dream_change": change,
            # Counted here rather than by fetching every line: the page shows a chip per
            # brief line, and "how much is behind this" is the question it answers.
            # `resolve_source` would pull the lines themselves — forty rows a token,
            # thirty tokens, to render a number.
            "citations": trace.citations(conn, kind, row["key"]),
        }
    return {
        "brief": text,
        "lines": lines,
        "targets": targets,
    }


def wiki_pages(conn: sqlite3.Connection, cfg: Config, *, q: str = "") -> dict:
    """Every wiki page as a browsable index — what memcal knows a page for, not its name."""
    needle = q.casefold().strip()
    pages = []
    for slug in wiki.list_pages(cfg.wiki_dir):
        page = wiki.read(cfg.wiki_dir, slug)
        if not page:
            continue
        title = page.title or slug
        if needle and needle not in title.casefold() and needle not in slug.casefold() \
                and not any(needle in a.casefold() for a in page.aliases):
            continue
        pages.append({
            "slug": page.slug, "title": title, "section": page.section,
            "facts": len(page.slots), "questions": len(page.questions),
            "aliases": list(page.aliases),
            # The slot names, so a card can say what the page is *for* — the same list
            # the brief's index publishes — without opening it.
            "answers": list(page.slots),
        })
    pages.sort(key=lambda p: (p["section"], p["title"].casefold()))
    return {"pages": pages, "total": len(pages)}


#: Where each brief handle's row lives. One place, so the page and `trace` cannot
#: disagree about what `Q12` means.
_TABLE = {"event": "events", "todo": "todos", "question": "questions",
          "standing": "standing"}


_TITLE_STOP_WORDS = {
    "the", "a", "an", "at", "with", "on", "in", "to", "for", "and", "or", "of",
    "from", "by", "my", "your",
}


def _title_terms(title: str) -> list[str]:
    """Words worth emphasizing when the title reappears in evidence or reasoning."""
    words = re.findall(r"[\w'$-]+", title or "", flags=re.UNICODE)
    return list(dict.fromkeys(
        word for word in words
        if len(word) >= 3 and word.casefold() not in _TITLE_STOP_WORDS
    ))


def _event_summary(row: sqlite3.Row) -> dict:
    return {
        "key": row["key"], "title": row["title"], "date": row["date"],
        "until": row["until"] or "", "time": row["time"] or "",
        "location": row["location"] or "", "status": row["status"],
        "kind": row["kind"],
    }


def _wiki_link(cfg: Config, name: str, *, role: str) -> dict | None:
    slug = wiki.canonical(cfg.wiki_dir, db.slugify(name))
    page = wiki.read(cfg.wiki_dir, slug)
    if not page:
        return None
    return {"slug": page.slug, "title": page.title or name, "section": page.section,
            "role": role}


#: The three facets a row can be shared by, and the only ones `/api/events` filters on.
FACETS = ("person", "location", "series")

#: {(db path, wiki dir): (watermark, index)}. Built lazily, reused until the watermark
#: below says the events table or the alias map moved.
_FACET_INDEX: dict[tuple, tuple] = {}
_FACET_LOCK = threading.Lock()


def _facet_watermark(conn: sqlite3.Connection, cfg: Config) -> tuple:
    """Cheap proof nothing the facet index reads has changed.

    Ids are never reused, so count + newest id + newest write between them catch an
    insert, an update and a delete. The alias map is in here because canonicalising a
    name is what decides which person bucket a row lands in — folding two spellings
    together has to invalidate buckets built from the old map, and the user hand-edits the
    wiki in Obsidian while the server is running.
    """
    row = conn.execute(
        "SELECT count(*) AS n, max(id) AS top, max(updated_at) AS at FROM events"
    ).fetchone()
    return (row["n"], row["top"], row["at"],
            tuple(sorted(wiki.alias_map(cfg.wiki_dir).items())))


def _facet_index(conn: sqlite3.Connection,
                 cfg: Config) -> dict[str, dict[str, list[dict]]]:
    """Build and cache exact event indexes by person, location, and series."""
    key = (str(cfg.db_path), str(cfg.wiki_dir))
    mark = _facet_watermark(conn, cfg)
    with _FACET_LOCK:
        cached = _FACET_INDEX.get(key)
        if cached and cached[0] == mark:
            return cached[1]

    index: dict[str, dict[str, list[dict]]] = {facet: {} for facet in FACETS}
    slugs: dict[str, str] = {}
    for row in conn.execute("SELECT * FROM events ORDER BY date DESC, id DESC"):
        summary = _event_summary(row)
        seen = set()
        for name in [row["subject"], *db.jload(row["participants"], [])]:
            name = str(name or "").strip()
            if not name:
                continue
            if name not in slugs:
                slugs[name] = wiki.canonical(cfg.wiki_dir, db.slugify(name))
            if slugs[name] not in seen:
                seen.add(slugs[name])
                index["person"].setdefault(slugs[name], []).append(summary)
        for facet in ("location", "series"):
            # Free text for locations, a slug for series; normalized exact equality
            # either way, never a substring.
            value = str(row[facet] or "").casefold().strip()
            if value:
                index[facet].setdefault(value, []).append(summary)
    # Built outside the lock on purpose. Two threads racing here do the same work twice
    # and agree on the answer; holding it across the scan would serialize the four
    # panels the page fetches at once.
    with _FACET_LOCK:
        _FACET_INDEX[key] = (mark, index)
    return index


def _facet_key(cfg: Config, facet: str, value: str) -> str:
    """The bucket a facet value belongs in. Must match how `_facet_index` files rows."""
    if facet == "person":
        return wiki.canonical(cfg.wiki_dir, db.slugify(value))
    return value.casefold().strip()


def _related_events(conn: sqlite3.Connection, cfg: Config, *, facet: str, value: str,
                    exclude: str = "") -> list[dict]:
    """Every row sharing one explicit event facet, newest first.

    `exclude` is the key of the row the caller is already looking at — "other entries
    involving Quinn" should not open with the event the pill was clicked on.
    """
    if facet not in FACETS or not value.strip():
        return []
    rows = _facet_index(conn, cfg)[facet].get(_facet_key(cfg, facet, value), [])
    return [row for row in rows if row["key"] != exclude]


def event_list(conn: sqlite3.Connection, cfg: Config, *, person: str = "",
               location: str = "", series: str = "", exclude: str = "",
               limit: int = 200) -> dict:
    """The calendar, or the slice of it sharing one facet with a row the user is reading.

    One facet at a time on purpose: the caller is a pill naming a person, a place or a
    series, and an intersection is a query the page has no way to express or undo.
    """
    asked = {facet: value for facet, value in
             (("person", person), ("location", location), ("series", series)) if value}
    if len(asked) > 1:
        return {"error": "one facet at a time: person, location or series"}
    if not asked:
        total = conn.execute("SELECT count(*) AS n FROM events").fetchone()["n"]
        rows = conn.execute(
            "SELECT * FROM events ORDER BY date DESC, id DESC LIMIT ?", (limit,))
        return {"events": [_event_summary(row) for row in rows], "total": total,
                "facet": "", "value": ""}
    facet, value = next(iter(asked.items()))
    found = _related_events(conn, cfg, facet=facet, value=value, exclude=exclude)
    return {"events": found[:limit], "total": len(found), "facet": facet,
            "value": value}


def _event_detail(conn: sqlite3.Connection, cfg: Config, ref: str) -> dict:
    """The panel's payload, assembled by `detail` so the agent sees the same thing.

    This function used to *be* the assembler, and it was reachable only by clicking in
    a browser — the same asymmetry `mcp_server` records about raw sources, one level
    up. `memcal_open` needed all of it, and a second copy would have been two answers
    to one question.
    """
    return detail.event_record(conn, cfg, ref)


def why(conn: sqlite3.Connection, kind: str, ref: str,
        cfg: Config | None = None) -> dict:
    """Which model calls have written to one row, newest first."""
    needle = _needle(conn, kind, ref)
    source = trace.source_rows(conn, kind, ref)
    source_needles = [row["text"] for row in source if row.get("evidence")]
    out = []
    direct = []
    for row in trace.history(conn, kind, ref):
        if not row["generation_id"]:
            direct.append({
                "verb": row["verb"] or "", "entity": row["entity"] or "",
                "stage": row["stage"] or "", "at": str(row["at"])[:16],
            })
            continue
        entity = row["entity"] or ""
        saved = calls.load(cfg.home, row["generation_id"] or "", row["run_id"]) if cfg else None
        excerpts = {}
        if saved:
            needles = [needle, *source_needles]
            excerpts = {
                "reasoning": _excerpts(str(saved.get("reasoning") or ""), needles),
                "answer": _excerpts(_pretty_json(saved.get("completion") or ""), needles),
            }
        out.append({
            "gen": row["generation_id"] or "",
            "verb": row["verb"], "entity": entity, "stage": row["stage"],
            "bundle": propose_stage.bundle_id(entity) if entity else "",
            "run": row["run_id"],
            # Which call within that run, so the panel can name it — and so the user can name
            # it back at me. "run 5 call 12" is a thing you can say; the generation id
            # is not, and "run #5" alone means digging through 24 requests.
            "call": calls.ordinal(conn, row["generation_id"] or ""),
            "at": str(row["at"])[:16],
            "model": (row["model"] or "").split("/")[-1],
            "prompt": row["prompt_tokens"] or 0,
            "completion": row["completion_tokens"] or 0,
            "cost": round(row["cost_usd"] or 0, 4),
            "bundles": row["label"] or "",
            "excerpts": excerpts,
        })
    return {"kind": kind, "ref": ref, "calls": out, "direct": direct,
            "needle": needle, "highlight_terms": _title_terms(needle),
            "source": source, "citations": trace.citations(conn, kind, ref),
            "detail": (_event_detail(conn, cfg, ref)
                       if kind == "event" and cfg is not None else {})}


def _excerpts(text: str, needles: list[str], *, radius: int = 240,
              limit: int = 4) -> list[str]:
    """Small context windows around relevant mentions, merged when they overlap."""
    if not text:
        return []
    lower = text.casefold()
    terms = []
    for raw in needles:
        raw = " ".join((raw or "").split()).strip()
        if len(raw) >= 3:
            terms.append(raw)
        # Full source lines rarely repeat verbatim in reasoning. Distinctive words make
        # "I only care about the relevant mentions" work even when the model paraphrased.
        terms.extend(word for word in re.findall(r"[A-Za-z0-9'$-]{5,}", raw)
                     if word.casefold() not in {"about", "would", "there", "their",
                                                "which", "should", "could"})
    spans = []
    for term in dict.fromkeys(terms):
        start = 0
        target = term.casefold()
        while len(spans) < limit * 3:
            at = lower.find(target, start)
            if at < 0:
                break
            spans.append((max(0, at - radius), min(len(text), at + len(term) + radius)))
            start = at + len(term)
    if not spans:
        return []
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1] + 40:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    out = []
    for start, end in merged[:limit]:
        chunk = text[start:end].strip()
        out.append(("… " if start else "") + chunk + (" …" if end < len(text) else ""))
    return out


def _needle(conn: sqlite3.Connection, kind: str, ref: str) -> str:
    """The most distinctive text this row holds, for the trace panel to search on.

    The row's key is no use — it is minted locally and never appears in the model's
    reply. The title is: an event written as `{"title": "Pay Parker for car camping
    pass"}` is findable by its own words and by nothing else.
    """
    try:
        if kind == "event":
            row = conn.execute("SELECT title FROM events WHERE key = ?", (ref,)).fetchone()
        elif kind == "todo":
            row = conn.execute("SELECT text FROM todos WHERE key = ?", (ref,)).fetchone()
        elif kind in ("question", "standing"):
            table = "questions" if kind == "question" else "standing"
            column = "text" if kind == "question" else "value"
            row = conn.execute(
                f"SELECT {column} AS text FROM {table} WHERE key = ?", (ref,)).fetchone()
        elif kind == "wiki":
            # `slug.slot` — the slot name is what appears in the diff.
            return ref.split(".", 1)[-1]
        else:
            return ""
    except sqlite3.Error:
        return ""
    return str(row[0] or "")[:80] if row else ""


def trace_call(conn: sqlite3.Connection, cfg: Config, generation_id: str) -> dict:
    """One model call, laid out to be read: what went in, what it thought, what it wrote.

    Served from disk (`calls.py`) whenever the call was made after the on-disk store
    existed, which is instant and works offline. Older calls fall back to OpenRouter,
    which is the only place their prompt and reasoning were ever kept.
    """
    row = conn.execute("SELECT * FROM generations WHERE generation_id = ?",
                       (generation_id,)).fetchone()
    local = {
        "gen": generation_id,
        "stage": row["stage"] if row else "",
        "label": row["label"] if row else "",
        "model": row["model"] if row else "",
        "run": row["run_id"] if row else None,
        "call": calls.ordinal(conn, generation_id),
        "at": str(row["created_at"])[:16] if row else "",
        "prompt_tokens": row["prompt_tokens"] if row else 0,
        "completion_tokens": row["completion_tokens"] if row else 0,
        "cost": round(row["cost_usd"], 4) if row else 0,
        "max_tokens": row["max_tokens"] if row else 0,
        # The other direction: everything this one call put in the store.
        "wrote": [{"kind": p["kind"], "ref": p["ref"], "verb": p["verb"],
                   "entity": p["entity"] or ""}
                  for p in trace.wrote(conn, generation_id)],
    }

    saved = calls.load(cfg.home, generation_id, local["run"])
    if saved:
        completion = _pretty_json(saved.get("completion") or "")
        return {
            **local,
            "source": "disk",
            # Kept apart rather than concatenated: the prefix is byte-identical across
            # every call in a run and is the half nobody needs to reread, while the
            # suffix is the only part that is about this bundle.
            "messages": [{"role": "system", "text": saved.get("prefix") or ""},
                         {"role": "user", "text": saved.get("suffix") or ""}],
            "reasoning": str(saved.get("reasoning") or ""),
            "completion": completion,
            "bundles": saved.get("bundles") or [],
            "routed": saved.get("routed"),
            "unrouted": saved.get("unrouted"),
            "echoed": saved.get("echoed"),
            "contract": _contract(saved),
            "native": {
                "prompt": (saved.get("usage") or {}).get("prompt_tokens"),
                "cached": (saved.get("usage") or {}).get("cached_tokens"),
                "completion": (saved.get("usage") or {}).get("completion_tokens"),
                "reasoning": None,
                "finish": saved.get("finish_reason"),
                "latency": None,
                "provider": None,
            },
        }

    try:
        content = trace.fetch(cfg.api_key, generation_id)
    except trace.TraceError as exc:
        return {**local, "source": "none", "error": str(exc)}

    messages = []
    for message in (content.get("input") or {}).get("messages") or []:
        body = message.get("content")
        if isinstance(body, list):
            body = "\n".join(part.get("text") or "" for part in body
                             if isinstance(part, dict))
        messages.append({"role": str(message.get("role", "?")),
                         "text": str(body or "").strip()})
    output = content.get("output") or {}
    stats = trace.stats(cfg.api_key, generation_id)
    return {
        **local,
        "source": "openrouter",
        "messages": messages,
        "reasoning": str(output.get("reasoning") or ""),
        "completion": _pretty_json(output.get("completion") or ""),
        "native": {
            "prompt": stats.get("native_tokens_prompt"),
            "cached": stats.get("native_tokens_cached"),
            "completion": stats.get("native_tokens_completion"),
            "reasoning": stats.get("native_tokens_reasoning"),
            "finish": stats.get("finish_reason") or stats.get("native_finish_reason"),
            "latency": stats.get("latency"),
            "provider": stats.get("provider_name"),
        },
    }


def _contract(saved: dict) -> str:
    """Which output contract this call ran under — read off the reply, not assumed."""
    parsed = saved.get("parsed")
    if isinstance(parsed, dict) and "reviewed" in parsed:
        return "v2"
    return "v1"


def _pretty_json(text: str) -> str:
    """Indent a JSON completion so a diff can be read and searched line by line.

    Not cosmetic: the "jump to the lines that wrote this" search matches on line
    content, and a whole diff on one line has nothing to jump to.
    """
    try:
        return json.dumps(json.loads(text), indent=2, ensure_ascii=False)
    except (ValueError, TypeError):
        return str(text)


def runs(conn: sqlite3.Connection, limit: int = 30) -> list[dict]:
    rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [{
        "id": r["id"], "at": str(r["started_at"])[:16], "mode": r["mode"],
        "model": (r["model"] or "").split("/")[-1], "bundles": r["bundles"],
        "items": r["items"], "diffs": r["diffs"],
        "prompt": r["prompt_tokens"], "cached": r["cached_tokens"],
        "completion": r["completion_tokens"], "cost": round(r["cost_usd"], 4),
        "error": r["error"],
        "calls": conn.execute(
            "SELECT count(*) n FROM generations WHERE run_id = ?", (r["id"],)
        ).fetchone()["n"],
    } for r in rows]


def run_detail(conn: sqlite3.Connection, cfg: Config, run_id: int) -> dict:
    """One pass, opened up: every request, every bundle in it, what came back.

    The runs table was a ledger — twelve numbers and an error string — and the error
    string was where the interesting thing always was. "6 bundle(s) left queued" names
    four bundles out of six and cannot say what happened to them, because what happened
    is in a reply nobody kept. Now the reply is on disk, so this can join the three
    things that answer the question: what went out, what came back, and what landed.
    """
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        return {"error": f"no run #{run_id}"}

    every = calls.for_run(conn, cfg.home, run_id)
    # A bundle can appear in one request only, so this is a partition, not a join —
    # but a bundle that never routed appears in `unrouted` and nowhere else, and that
    # is exactly the bundle someone came here to find.
    seats: dict[str, dict] = {}
    for call in every:
        for ref in call.get("bundles") or []:
            seat = seats.setdefault(ref["id"], {**ref, "gen": call["gen"],
                                                "stage": call["stage"], "wrote": [],
                                                "outcome": "no reply"})
            seat["gen"] = call["gen"]
        landed = {r["id"] for r in (call.get("routed") or [])}
        for ref in call.get("bundles") or []:
            if ref["id"] in seats:
                seats[ref["id"]]["outcome"] = "read" if ref["id"] in landed else "no diff"
        # Provenance records the entity a write came out of, which is how a write finds
        # its way back to a bundle rather than merely to the request that carried it.
        for wrote in call.get("wrote") or []:
            for ref in call.get("bundles") or []:
                if wrote.get("entity") and wrote["entity"] == ref["entity"]:
                    seats[ref["id"]]["wrote"].append(wrote)

    writes = conn.execute(
        """SELECT p.*, g.label FROM provenance p
             LEFT JOIN generations g ON g.generation_id = p.generation_id
            WHERE p.run_id = ? ORDER BY p.id""", (run_id,)).fetchall()
    return {
        "run": {
            "id": row["id"], "at": str(row["started_at"])[:19],
            "finished": str(row["finished_at"] or "")[:19],
            "mode": row["mode"], "model": row["model"] or "",
            "bundles": row["bundles"], "items": row["items"], "diffs": row["diffs"],
            "prompt": row["prompt_tokens"], "cached": row["cached_tokens"],
            "completion": row["completion_tokens"],
            "cost": round(row["cost_usd"] or 0, 5), "error": row["error"] or "",
            # The three numbers that separate "this pass did nothing" from "this pass
            # was refused for an hour". Both used to render identically. `null` for a run
            # that predates them, which the page draws as nothing rather than as zero.
            "requests": row["requests"],
            "failed_calls": row["failed_calls"],
            "wait_seconds": (round(row["wait_seconds"], 1)
                             if row["wait_seconds"] is not None else None),
        },
        "calls": every,
        # Requests that produced no reply, so there is no `generations` row and they
        # cannot appear above. This is where run 3's six connection resets went.
        "failures": [{"stage": f.get("stage") or "", "label": f.get("label") or "",
                      "error": str(f.get("error") or "")[:400],
                      "at": str(f.get("at") or "")[:19],
                      "requests": f.get("requests") or 0,
                      "waited": round(float(f.get("waited") or 0), 1),
                      "bundles": f.get("bundles") or []}
                     for f in calls.failures_for_run(cfg.home, run_id)],
        "seats": sorted(seats.values(), key=lambda s: (s["outcome"] == "read", -s["lines"])),
        "writes": [{"kind": w["kind"], "ref": w["ref"], "verb": w["verb"],
                    "entity": w["entity"] or "", "stage": w["stage"] or "",
                    "gen": w["generation_id"] or "",
                    "bundle": propose_stage.bundle_id(w["entity"]) if w["entity"] else ""}
                   for w in writes],
    }


# -------------------------------------------------------------------- dream --
# Reading a dream pass back out of OpenRouter's logs is how the truncation bug got
# found, and it should not have taken that. Everything the pass would send is
# derivable before spending a token, so this serves it: the bundles as the model
# will receive them, grouped into the requests they will actually travel in.
