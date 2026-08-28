"""Gate, archive, and sender projections for the local UI."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from . import archive, db, gate, identity, threads
from .config import Config

PREVIEW_CHARS = 240


def _meta(row: sqlite3.Row) -> dict:
    return db.jload(row["meta"] if "meta" in row.keys() else None, {}) or {}


def counterparts(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> dict[tuple, str]:
    """Who each thread is *with*, for the whole page in one query.

    Half their traffic is their own, and "me" in the sender column says nothing you could
    skim — "me → Jordan" is the line that tells you whether a row is worth reading.
    Resolved the same way bundling resolves it, so what this shows is what the dream
    pass will actually group on.
    """
    keys = {(r["stream"], r["thread"]) for r in rows if r["thread"]}
    if not keys:
        return {}
    threads = sorted({t for _s, t in keys})
    marks = ",".join("?" * len(threads))
    speakers: dict[tuple, set[str]] = {}
    for row in conn.execute(
        f"""SELECT DISTINCT stream, thread, person FROM archive
             WHERE thread IN ({marks}) AND from_me = 0
               AND person IS NOT NULL AND person != 'me'""", threads):
        speakers.setdefault((row["stream"], row["thread"]), set()).add(row["person"])

    out = {}
    for stream, thread in keys:
        who = speakers.get((stream, thread), set())
        if len(who) == 1:
            out[(stream, thread)] = next(iter(who))
        elif len(who) > 1:
            out[(stream, thread)] = f"group of {len(who)}"
        elif stream == "email":
            out[(stream, thread)] = thread        # the address is the counterpart
    return out


def _who(row: sqlite3.Row, with_whom: dict[tuple, str] | None = None) -> str:
    other = (with_whom or {}).get((row["stream"], row["thread"]))
    if row["from_me"]:
        return f"me → {other}" if other else "me"
    return row["person"] or row["handle"] or "?"


def _queue_state(row: sqlite3.Row) -> str:
    """What will actually happen to this item, which is not the same as what the gate said.

    An item the gate passed still goes nowhere if it landed outside the spool horizon,
    and an item the gate skipped can be queued by hand from here. The gate's verdict is
    a record of a decision; this is the consequence.
    """
    if (row["gate_reason"] or "") == "calendar-structured":
        # iCal has already supplied the fields an event needs. These rows are applied
        # directly in code, not rejected by the gate and not sent to a model.
        return "structured"
    if (row["gate_reason"] or "") == "live":
        # The live path writes as it goes and never queues anything — the user is sitting
        # right there. Reading that as "the pass never got to it" would be a bug report
        # about the one thing working as designed.
        return "live"
    if row["spool_id"] is None:
        return "dropped" if row["gated"] else "skipped"
    if not row["processed_at"]:
        return "queued"
    # `processed_at` means two different things: a run consumed this, or it was retired
    # from the queue without ever being read — by hand here, or by the horizon sweep at
    # the top of every pass. Only `run_id` separates them, and reporting a retirement as
    # "read" would overstate what the model has actually seen by the whole backlog.
    return "read" if row["run_id"] is not None else "retired"


def _item(row: sqlite3.Row, with_whom: dict[tuple, str] | None = None) -> dict:
    meta = _meta(row)
    text = row["text"] or ""
    subject = meta.get("subject")
    # For mail the subject *is* the skimmable part, and it is also the whole of the
    # stored text whenever the gate said no — the body is never fetched for a skip.
    body = text
    if subject and body.startswith(subject):
        body = body[len(subject):].strip()
    return {
        "id": row["id"],
        "ts": str(row["ts"]),
        "stream": row["stream"],
        # The conversation this line is in — what "don't care" acts on when there is no
        # address to blame, which is every chat stream.
        "thread": row["thread"] or "",
        "who": _who(row, with_whom),
        "address": None if row["from_me"] else (row["handle"] or None),
        "from_me": bool(row["from_me"]),
        "subject": subject,
        "preview": body[:PREVIEW_CHARS],
        "truncated": len(body) > PREVIEW_CHARS,
        "gated": bool(row["gated"]),
        "reason": row["gate_reason"] or "",
        "state": _queue_state(row),
        "entity": row["entity"],
    }


ITEM_SELECT = """
    SELECT a.id, a.stream, a.ts, a.thread, a.handle, a.person, a.from_me, a.text,
           a.meta, a.gated, a.gate_reason,
           s.id AS spool_id, s.processed_at, s.run_id, s.entity
      FROM archive a LEFT JOIN spool s ON s.archive_id = a.id
"""


# What counts as one conversation. Group on the thread where there is one, and on the
# sender otherwise. Mail has no threads worth the name, so for email the address *is*
# the conversation. The rollup groups by this and the row expansion filters by it, so
# it lives in one place — the two drifting apart is a group that opens onto nothing.
GROUP_KEY = ("CASE WHEN a.stream = 'email' THEN coalesce(a.handle, a.thread, '?')"
             " ELSE coalesce(a.thread, a.handle, '?') END")


# What "waiting" means, in SQL. The spool is the queue; `processed_at IS NULL` is the
# whole of "no pass has taken this yet". Kept beside the other filters because the
# distinction it draws — what the next dream will read, versus everything that ever
# passed the gate — is the one the page is now organised around.
QUEUE_CLAUSES = {
    "queued": "s.id IS NOT NULL AND s.processed_at IS NULL",
    # A pass consumed it. Not `processed_at IS NOT NULL`, which also covers everything
    # retired from the queue unread — on this store that is the difference between a
    # hundred-odd items and seven thousand.
    "read": "s.run_id IS NOT NULL",
    "retired": "s.processed_at IS NOT NULL AND s.run_id IS NULL",
    "never": "s.id IS NULL",
}


def _filters(*, stream: str = "", verdict: str = "", reason: str = "",
             q: str = "", days: int = 0, group: str = "",
             queue: str = "") -> tuple[str, list]:
    # Old builds archived a daily iCal health marker. It remains immutable and
    # searchable, but it is operational bookkeeping rather than something the gate
    # observed, so it does not belong in this feed.
    where, args = [
        "NOT (a.stream = 'ical' AND a.external_id LIKE 'snapshot:%')"
    ], []
    if queue in QUEUE_CLAUSES:
        where.append(QUEUE_CLAUSES[queue])
    if stream:
        where.append("a.stream = ?")
        args.append(stream)
    # An exact key, not a search. A group chat's key is its identifier — an opaque GUID
    # for an unnamed one — and no message contains it, so matching it as text finds
    # nothing at all.
    if group:
        where.append(f"{GROUP_KEY} = ?")
        args.append(group)
    if verdict == "passed":
        where.append("a.gated = 1")
    elif verdict == "skipped":
        where.append("a.gated = 0 AND a.gate_reason != 'calendar-structured'")
    elif verdict == "structured":
        where.append("a.gate_reason = 'calendar-structured'")
    if reason:
        where.append("a.gate_reason = ?")
        args.append(reason)
    if days:
        where.append("a.ts >= ?")
        args.append((db.today() - timedelta(days=days)).isoformat())
    if q:
        where.append("(lower(a.text) LIKE ? OR lower(coalesce(a.handle,'')) LIKE ?"
                     " OR lower(coalesce(a.person,'')) LIKE ?)")
        args += [f"%{q.lower()}%"] * 3
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    return clause, args


#: Counting and faceting need the same spool join the feed has, or a queue filter is a
#: syntax error against a bare `archive`. One constant so the three cannot drift.
ARCHIVE_JOIN = " FROM archive a LEFT JOIN spool s ON s.archive_id = a.id"


def items(conn: sqlite3.Connection, *, stream: str = "", verdict: str = "",
          reason: str = "", q: str = "", days: int = 0, group: str = "",
          queue: str = "", limit: int = 100, offset: int = 0) -> dict:
    """The feed: what the gate saw, what it decided, and where the item ended up."""
    clause, args = _filters(stream=stream, verdict=verdict, reason=reason, q=q,
                            days=days, group=group, queue=queue)
    rows = conn.execute(
        ITEM_SELECT + clause + " ORDER BY a.ts DESC LIMIT ? OFFSET ?",
        args + [limit, offset],
    ).fetchall()
    total = conn.execute(
        "SELECT count(*) AS n" + ARCHIVE_JOIN + clause, args
    ).fetchone()["n"]

    # Facets ignore the reason filter, so the chips stay put when one is clicked.
    fclause, fargs = _filters(stream=stream, verdict=verdict, q=q, days=days,
                              group=group, queue=queue)
    facets = conn.execute(
        "SELECT a.gate_reason AS reason, a.gated, count(*) AS n" + ARCHIVE_JOIN
        + fclause + " GROUP BY 1, 2 ORDER BY n DESC", fargs
    ).fetchall()
    with_whom = counterparts(conn, rows)
    return {
        "items": [_item(r, with_whom) for r in rows],
        "total": total,
        "offset": offset,
        "reasons": [{"reason": r["reason"] or "(none)", "passed": bool(r["gated"]),
                     "structured": r["reason"] == "calendar-structured",
                     "n": r["n"]} for r in facets],
    }


def groups(conn: sqlite3.Connection, *, stream: str = "", verdict: str = "",
           reason: str = "", q: str = "", days: int = 0, queue: str = "",
           limit: int = 200) -> dict:
    """The same feed, rolled up by who it came from.

    A flat list of five thousand lines is not a view of what was collected, it is the
    raw material for one — you cannot see that 381 of them are the dog park until they
    are next to each other. Same filters as the feed, so a reason chip narrows both.
    """
    clause, args = _filters(stream=stream, verdict=verdict, reason=reason, q=q,
                            days=days, queue=queue)
    rows = conn.execute(
        f"""SELECT a.stream, {GROUP_KEY} AS key,
                  count(*) AS n, sum(a.gated) AS gated, sum(a.from_me) AS mine,
                  sum(a.gate_reason = 'calendar-structured') AS structured,
                  sum(length(a.text)) AS chars, max(a.ts) AS last_ts, min(a.ts) AS first_ts,
                  sum(CASE WHEN s.id IS NOT NULL AND s.processed_at IS NULL
                           THEN 1 ELSE 0 END) AS queued
             FROM archive a LEFT JOIN spool s ON s.archive_id = a.id""" + clause +
        " GROUP BY 1, 2 ORDER BY n DESC LIMIT ?", args + [limit]).fetchall()

    names = threads.titles(conn)
    hushed = threads.muted(conn)
    out = []
    for row in rows:
        key = (row["stream"], row["key"])
        out.append({
            "stream": row["stream"],
            "key": row["key"],
            "title": names.get(key, row["key"]),
            "n": row["n"],
            "gated": row["gated"] or 0,
            "structured": row["structured"] or 0,
            "mine": row["mine"] or 0,
            "queued": row["queued"] or 0,
            "tokens": (row["chars"] or 0) // 4,
            "span": f"{str(row['first_ts'])[:10]} → {str(row['last_ts'])[:10]}",
            "last": str(row["last_ts"] or "")[:10],
            "muted": key in hushed,
        })
    return {"groups": out, "total": len(out)}


def conversations(conn: sqlite3.Connection, cfg: Config, *, stream: str = "",
                  q: str = "") -> dict:
    """Every chat, plus the ones worth asking about. Refreshes the derived numbers first."""
    threads.refresh(conn)
    policy = cfg.platform_mute
    threads.apply_platform_mutes(conn, policy)
    everything = threads.rows(conn, stream=stream, q=q, policy=policy)
    return {"threads": everything,
            "review": threads.review(conn, policy=policy),
            "min_items": threads.REVIEW_MIN_ITEMS,
            "platform_mute": policy,
            # The measurement behind the default, so the setting explains itself rather
            # than being a knob with an opinion attached.
            "platform_muted_count": sum(1 for t in everything if t["platform_muted"]),
            "platform_muted_with_mutuals": sum(
                1 for t in everything if t["platform_muted"] and t["mutuals"])}


def item_detail(conn: sqlite3.Connection, archive_id: int) -> dict:
    row = conn.execute(ITEM_SELECT + " WHERE a.id = ?", (archive_id,)).fetchone()
    if not row:
        return {"error": "no such item"}
    out = _item(row, counterparts(conn, [row]))
    out["text"] = row["text"] or ""
    out["meta"] = _meta(row)
    out["thread"] = row["thread"]
    return out


# ------------------------------------------------------------------ senders --

def senders(conn: sqlite3.Connection, *, q: str = "", decision: str = "",
            limit: int = 200) -> list[dict]:
    """The email gate table, joined to what it actually did to the mailbox.

    A row is only worth reading next to its consequences — 46 denials that saved a
    token each look identical to 46 denials that lost a party invitation until the
    subject lines are sitting beside them.
    """
    args: list = []
    clause = ""
    if q:
        clause = " AND lower(a.handle) LIKE ?"
        args.append(f"%{q.lower()}%")
    rows = conn.execute(
        """SELECT a.handle AS address, count(*) AS n, sum(a.gated) AS passed,
                  sum(CASE WHEN s.run_id IS NOT NULL THEN 1 ELSE 0 END) AS seen,
                  max(a.ts) AS last_seen
             FROM archive a LEFT JOIN spool s ON s.archive_id = a.id
            WHERE a.stream = 'email' AND a.from_me = 0 AND a.handle IS NOT NULL"""
        + clause +
        " GROUP BY 1 ORDER BY n DESC LIMIT ?", args + [limit],
    ).fetchall()

    # The newest subject per sender, in one pass rather than one query per row.
    latest = {r["handle"]: (db.jload(r["meta"], {}) or {}).get("subject", "")
              for r in conn.execute(
                  """SELECT handle, meta FROM (
                       SELECT handle, meta,
                              row_number() OVER (PARTITION BY handle ORDER BY ts DESC) AS rn
                         FROM archive
                        WHERE stream = 'email' AND from_me = 0 AND handle IS NOT NULL
                     ) WHERE rn = 1""")}

    table = {r["address"]: r for r in conn.execute("SELECT * FROM senders")}
    out = []
    for row in rows:
        address = row["address"]
        known = table.get(address)
        current = known["decision"] if known else None
        if decision and current != decision:
            continue
        # Would today's gate still say this? The table is consulted before every other
        # signal, so a decision made by an older build is never revisited on its own —
        # `travel@m.livekindred.com` keeps passing because it passed once, back when
        # the sending-subdomain rule did not exist yet.
        stale = bool(current == "process" and gate.is_automated(address))
        out.append({
            "address": address,
            "decision": current or "(unseen)",
            "reason": (known["reason"] if known else "") or "",
            "n": row["n"],
            "passed": row["passed"] or 0,
            # Passing the gate is not the same as being read: most of what passes is
            # retired by the horizon sweep before any pass gets to it.
            "seen": row["seen"] or 0,
            "last_seen": str(row["last_seen"] or "")[:10],
            "subject": latest.get(address, ""),
            "disagrees": stale,
            # Who decided, and so whether the subject test may still rescue a line from
            # this sender. `auto` is the gate's own guess and is reopened by a subject
            # that reports an event; anything else is a judgement and is not.
            "source": (known["source"] if known else None) or "auto",
            "blocked": bool(known and known["decision"] in ("archive", "ignore")
                            and (known["source"] or "auto") != "auto"),
        })
    return out


def set_sender(conn: sqlite3.Connection, cfg: Config, address: str, decision: str,
               *, backfill: bool = False, source: str = "you",
               reason: str | None = None) -> dict:
    """Flip one sender, and optionally act on the mail already sitting in the archive."""
    if decision not in ("ignore", "archive", "process"):
        return {"error": f"unknown decision: {decision}"}
    if source not in identity.SENDER_SOURCES:
        return {"error": f"unknown source: {source}"}
    identity.set_sender(conn, address, decision, reason or source, source=source)

    queued = retired = 0
    if decision == "process" and backfill:
        queued = _queue_sender(conn, cfg, address)
    elif decision != "process":
        cur = conn.execute(
            """UPDATE spool SET processed_at = ?
                WHERE processed_at IS NULL AND archive_id IN
                  (SELECT id FROM archive WHERE stream='email' AND handle = ?)""",
            (db.now(), address),
        )
        retired = cur.rowcount
    conn.commit()
    return {"address": address, "decision": decision, "queued": queued, "retired": retired}


def block(conn: sqlite3.Connection, cfg: Config, payload: dict) -> dict:
    """"I don't care about this" — one verb, whatever produced the thing."""
    by = payload.get("by") or "you"
    if by not in ("you", "agent"):
        return {"error": f"unknown decider: {by}"}
    why = (payload.get("reason") or "").strip() or None

    address = (payload.get("address") or "").strip().lower()
    stream = (payload.get("stream") or "").strip()
    thread = (payload.get("thread") or "").strip()

    # An event id is the handle the agent is most likely to have: it just proposed the
    # row, and what it knows is which one the user objected to. `provenance` is what makes
    # that answerable — it records the bundle every written row came out of.
    if payload.get("event_id") and not (address or thread):
        row = conn.execute(
            """SELECT p.entity FROM events e
                 JOIN provenance p ON p.kind = 'event' AND p.ref = e.key
                WHERE e.id = ? AND p.entity IS NOT NULL
                ORDER BY p.id DESC LIMIT 1""", (int(payload["event_id"]),)).fetchone()
        if not row:
            return {"error": "no record of which conversation that event came from"}
        kind, _, rest = str(row["entity"]).partition(":")
        if kind == "thread":
            stream, _, thread = rest.partition(":")
            # For mail the thread key *is* the address, and blocking a sender is a
            # stronger, more useful statement than muting one address's thread.
            if stream == "email":
                address, thread = thread.lower(), ""
        else:
            # A person bundle. Muting a friend is not what "I don't care about this" ever
            # means — it means the thing, and there is no sender to blame.
            return {"error": f"that event came from {rest}, a person — "
                             f"block a sender or a chat, not someone you know"}

    if address:
        out = set_sender(conn, cfg, address, "ignore", source=by,
                         reason=why or f"{by}: don't care")
        out["blocked"] = address
        return out
    if stream and thread:
        out = threads.decide(conn, stream, thread, "mute",
                             reason=why or f"{by}: don't care", by=by)
        out["blocked"] = f"{stream}/{thread}"
        return out
    return {"error": "nothing to block — give an address, a stream+thread, or an event_id"}


def _queue_sender(conn: sqlite3.Connection, cfg: Config, address: str) -> int:
    cutoff = (db.today() - timedelta(days=cfg.spool_horizon_days)).isoformat()
    rows = conn.execute(
        """SELECT a.id, a.person, a.thread FROM archive a
            LEFT JOIN spool s ON s.archive_id = a.id
           WHERE a.stream = 'email' AND a.handle = ? AND s.id IS NULL
             AND substr(a.ts, 1, 10) >= ?""",
        (address, cutoff),
    ).fetchall()
    for row in rows:
        archive.spool_add(conn, row["id"], gate.entity_for(
            person=row["person"], thread=row["thread"], stream="email", is_group=False))
    return len(rows)


def queue_item(conn: sqlite3.Connection, cfg: Config, archive_id: int, action: str) -> dict:
    """Queue one item for the next pass, or retire it from the queue.

    The archive row is never rewritten. What the gate decided stays on the record —
    that record is the whole point of this page — and the queue carries the override.
    """
    row = conn.execute(
        "SELECT id, stream, ts, person, thread, meta FROM archive WHERE id = ?", (archive_id,)
    ).fetchone()
    if not row:
        return {"error": "no such item"}
    if action == "queue":
        if not archive.within_horizon(str(row["ts"]), cfg.spool_horizon_days):
            return {"error": f"older than the {cfg.spool_horizon_days}-day spool horizon"}
        archive.spool_add(conn, row["id"], gate.entity_for(
            person=row["person"], thread=row["thread"], stream=row["stream"],
            is_group=bool((db.jload(row["meta"], {}) or {}).get("group"))))
        # Clear the run too, or a re-queued item reads as already-read the moment it
        # is queued.
        conn.execute("UPDATE spool SET processed_at = NULL, run_id = NULL"
                     " WHERE archive_id = ?", (row["id"],))
    elif action == "skip":
        conn.execute(
            "UPDATE spool SET processed_at = ? WHERE archive_id = ? AND processed_at IS NULL",
            (db.now(), row["id"]),
        )
    else:
        return {"error": f"unknown action: {action}"}
    conn.commit()
    return item_detail(conn, archive_id)


# ----------------------------------------------------------------- overview --
