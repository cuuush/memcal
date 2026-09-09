"""Dream previews and cost projections for the local UI."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from . import archive, db, llm, textclean, threads, wiki
from .config import Config
from .dream import bundle as bundle_stage
from .dream import propose as propose_stage

def dream_preview(conn: sqlite3.Connection, cfg: Config, *, limit: int = 0) -> dict:
    """Exactly what the next pass would see. Claims nothing — safe to call at will."""
    threads.refresh(conn)
    bundles = bundle_stage.build(conn, limit=limit or cfg.item_budget,
                                 per_entity=cfg.items_per_entity)
    prefix = propose_stage.build_prefix(conn, cfg)
    prefix_tok = textclean.estimate_tokens(prefix)
    groups = propose_stage.pack(cfg, bundles, conn) if bundles else []
    cards = {b.entity: _bundle_card(cfg, b, conn) for b in bundles}

    pending = conn.execute(
        "SELECT count(*) n FROM spool WHERE processed_at IS NULL").fetchone()["n"]
    last = conn.execute(
        """SELECT started_at, model, mode FROM runs
           WHERE mode NOT IN ('dry-run') AND diffs IS NOT NULL
           ORDER BY id DESC LIMIT 1""").fetchone()
    cutoff = (db.today() - timedelta(days=archive.SPOOL_HORIZON_DAYS)).isoformat()
    stale = conn.execute(
        """SELECT count(*) n FROM spool s JOIN archive a ON a.id = s.archive_id
           WHERE s.processed_at IS NULL AND substr(a.ts, 1, 10) < ?""", (cutoff,)
    ).fetchone()["n"]

    taken = sum(len(b.spool_ids) for b in bundles)
    return {
        "model": cfg.propose_model,
        "prefix": {"text": prefix, "tokens": prefix_tok,
                   "cache_min": llm.CACHE_MIN.get(cfg.propose_model, 1024)},
        "bundles": [cards[b.entity] for b in bundles],
        "requests": [_request_card(cfg, g, i, prefix_tok, cards, conn)
                     for i, g in enumerate(groups, 1)],
        "spool": {
            "pending": pending,
            "taken": taken,
            # The two ways something can be waiting and not read, kept apart because they
            # mean different things. `left_behind` is the tail of a conversation that *is*
            # being read — their partner's 1,509 messages, capped at 60. `unreached` is a
            # conversation that got no share at all, which under round-robin selection
            # only happens when the budget is smaller than the number of conversations.
            # Together they are the honest answer to "5,520 waiting, why 1,600 shown?".
            "left_behind": sum(b.waiting for b in bundles),
            "unreached": max(0, pending - taken - stale - sum(b.waiting for b in bundles)),
            "will_retire": stale, "retire_before": cutoff,
            "horizon_days": archive.SPOOL_HORIZON_DAYS,
            "item_budget": limit or cfg.item_budget,
            "per_entity": cfg.items_per_entity,
            "entities": len(bundles),
        },
        "last_dream": {"at": str(last["started_at"])[:16] if last else None,
                       "model": (last["model"] or "").split("/")[-1] if last else None},
        "max_parallel": cfg.max_parallel,
        "pack": {"bundles": cfg.pack_bundles, "tokens": cfg.pack_tokens},
        "cost": _cost_estimate(cfg, prefix_tok, groups, conn),
        "budget": _budget_history(conn, cfg),
    }


#: One definition, in the module that names bundles for the model. The UI and the
#: propose stage must agree on it or a link from a memory to its bundle points nowhere.
_bundle_id = propose_stage.bundle_id


def _bundle_card(cfg: Config, b, conn: sqlite3.Connection | None = None) -> dict:
    """One bundle, plus the things that are hard to see in a raw log."""
    items = [{
        "who": ("me" if r["from_me"] else (r["person"] or r["handle"] or "unknown")),
        "mine": bool(r["from_me"]),
        "at": str(r["ts"])[:16].replace("T", " "),
        "stream": r["stream"],
        "thread": r["thread"] or "",
        # A GroupMe line from a thirty-person chat and a GroupMe DM look identical in a
        # log. They are not the same evidence, and reading a group as a DM is how "yo
        # ravers" ends up filed as something a friend said to them directly.
        "group": bool((db.jload(r["meta"], {}) or {}).get("group")),
        "text": r["text"] or "",
        # Neighbours the gate rejected, pulled back in by add_thread_context for
        # readability. They arrive from a plain archive SELECT, so they are the rows
        # with no spool_id — the gated ones carry one. (Comparing ids would be wrong:
        # spool_ids are spool row ids, not archive ids.)
        "context": "spool_id" not in r.keys(),
    } for r in b.items]
    mine = sum(1 for i in items if i["mine"])
    # Which conversations this bundle is actually made of. A person bundle joins every
    # stream they appear on — that is the point of it — but with 21 lines from four
    # places under one name, "parker shaw · 3 lines" told them nothing about where they
    # came from, and one of them turned out to be a group chat.
    convos: dict[tuple, dict] = {}
    for i in items:
        key = (i["stream"], i["thread"], i["group"])
        seat = convos.setdefault(key, {"stream": i["stream"], "thread": i["thread"],
                                       "group": i["group"], "n": 0})
        seat["n"] += 1
    return {
        "entity": b.entity,
        # A short, stable handle so a bundle can be named out loud and looked up again.
        "id": _bundle_id(b.entity),
        "label": b.label,
        # How many more of this conversation's items are queued but will not be read this
        # pass, and which other chat ids were folded in because they are the same room.
        "waiting": b.waiting,
        "merged": b.merged,
        "kind": b.entity.split(":", 1)[0],
        "people": b.people,
        "streams": sorted({i["stream"] for i in items}),
        "conversations": sorted(convos.values(), key=lambda c: -c["n"]),
        "group": all(i["group"] for i in items) if items else False,
        "items": items,
        "count": len(items),
        "mine": mine,
        # A Hermes bundle is every word the user typed and none of the replies, because
        # sync_turn deliberately spools only their side. On the page that reads as a
        # monologue with the answers missing, so say so rather than letting it puzzle.
        "monologue": len(items) > 1 and mine == len(items),
        "missing_pages": [p for p in b.people
                          if p != "me" and not wiki.exists(cfg.wiki_dir, db.slugify(p))],
        "text": propose_stage.build_bundle_block(cfg, b, conn),
        "tokens": textclean.estimate_tokens(
            propose_stage.build_bundle_block(cfg, b, conn)),
        "span": (f"{str(b.items[0]['ts'])[:10]} → {str(b.items[-1]['ts'])[:10]}"
                 if b.items else ""),
    }


def _request_card(cfg: Config, group: list, index: int, prefix_tok: int,
                  cards: dict[str, dict] | None = None,
                  conn: sqlite3.Connection | None = None) -> dict:
    """One HTTP call, with the bundles riding in it and the output ceiling it will get."""
    suffix_tok = textclean.estimate_tokens(
        propose_stage.build_suffix(cfg, group, conn))
    riders = []
    for b in group:
        card = (cards or {}).get(b.entity) or {}
        riders.append({
            "id": card.get("id") or _bundle_id(b.entity),
            "entity": b.entity,
            "label": card.get("label") or b.label,
            "count": card.get("count") or len(b.items),
            "tokens": card.get("tokens") or 0,
        })
    return {
        "index": index,
        "entities": [b.entity for b in group],
        "riders": sorted(riders, key=lambda r: -r["tokens"]),
        "bundles": len(group),
        # Lines, not just bundles: the output ceiling is set from both, and a request
        # carrying one 1,500-line conversation is not the same request as one carrying
        # six short ones however alike the bundle count makes them look.
        "items": sum(len(b.items) for b in group),
        "suffix_tokens": suffix_tok,
        "input_tokens": prefix_tok + suffix_tok,
        "max_tokens": propose_stage.model_ceiling(cfg, group),
    }


def _budget_history(conn: sqlite3.Connection, cfg: Config | None = None) -> dict:
    """How often past propose calls ended exactly on their ceiling."""
    rows = conn.execute(
        """SELECT completion_tokens o, max_tokens cap FROM generations
           WHERE stage = 'propose' AND completion_tokens > 0""").fetchall()
    hit = [r["o"] for r in rows if r["cap"] and r["o"] >= r["cap"]]
    unknown = sum(1 for r in rows if not r["cap"])
    return {"calls": len(rows), "saturated": len(hit), "at": sorted(set(hit)),
            "unrecorded": unknown}


def _cost_estimate(cfg: Config, prefix_tok: int, groups: list,
                   conn: sqlite3.Connection | None = None) -> dict:
    model = cfg.propose_model
    n = len(groups)
    suffix = sum(textclean.estimate_tokens(propose_stage.build_suffix(cfg, g, conn))
                 for g in groups)
    out_cap = sum(propose_stage.model_ceiling(cfg, g) for g in groups)
    return llm.packed_cost(
        model, prefix_tokens=prefix_tok, suffix_tokens=suffix,
        output_tokens=out_cap, requests=n, max_parallel=cfg.max_parallel)


# --------------------------------------------------------------------- jobs --
# Collect and dream both outlast a request, so they run on a thread and the page polls.
# One at a time: two passes over the same spool would each claim half the traffic.
