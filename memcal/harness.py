"""Shared boundary for agent harnesses that do not implement Hermes' provider ABC.

The OpenClaw plugin invokes this module as a short-lived process. Keeping personal
state access in Python means the TypeScript adapter never learns the database schema,
gate policy, wiki layout, or brief rendering rules.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time

from . import archive, brief, config, db, gate, wiki
from .config import Config


GUIDANCE = """\
# Memcal

The MEMCAL SNAPSHOT below is regenerated for this turn. It supersedes older snapshots
in conversation history. Rows were mentioned; only rows marked confirmed are settled.
For questions inside the snapshot window, read the snapshot before calling a tool. Open
a source handle when its compact wording is insufficient, and use the memcal MCP tools
for dates outside the window, source evidence, or typed writes.

Write settled conversational changes immediately with the tool that names the change:
memcal_add, memcal_update, memcal_merge, memcal_drop, memcal_todo, memcal_answer,
memcal_note, or memcal_alias. These tools update private memcal state directly and do
not need a second model pass. Never infer that a to-do is complete; ask when unsure.

Memcal does not mutate the user's real calendar. Use the harness's calendar capability
for an explicit request to create, move, or delete a real calendar event; do not also
create a duplicate private row because the next calendar ingest will import it.
"""


def context(cfg: Config, query: str) -> str:
    """Fresh brief plus material wiki pages explicitly named in this turn."""
    cfg.ensure_dirs()
    conn = db.open_db(cfg.db_path)
    try:
        snapshot = brief.render(conn, cfg).strip()
        page_blocks = []
        for page in wiki.mentioned_pages(cfg.wiki_dir, query or "", limit=3):
            profile = wiki.profile(conn, cfg.wiki_dir, page.slug) or {}
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
    blocks = [GUIDANCE.strip(), f"MEMCAL SNAPSHOT {stamp}\n\n{snapshot}"]
    if page_blocks:
        blocks.append("WIKI PAGES MENTIONED THIS TURN\n\n" +
                      "\n\n---\n\n".join(page_blocks))
    return "\n\n".join(blocks)


def archive_user_turn(cfg: Config, text: str, *, harness: str, session_id: str,
                      message_id: str = "", sender: str = "me") -> int | None:
    """Archive exactly one inbound user turn and queue it when the gate admits it."""
    text = (text or "").strip()
    if not text:
        return None
    cfg.ensure_dirs()
    stamp = db.now()
    if not message_id:
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
        message_id = f"{digest}:{time.time_ns()}"
    external_id = f"{harness}:{session_id}:{message_id}"
    conn = db.open_db(cfg.db_path)
    try:
        verdict = gate.gate_message(text, from_me=True, addressed_to="machine")
        archive_id = archive.append(
            conn, stream="agent", external_id=external_id, ts=stamp,
            text=text[:4000], thread=f"{harness}:{session_id}", person=sender,
            from_me=True, addressed_to="machine",
            meta={"session": session_id, "origin": f"{harness}-user"},
            gated=bool(verdict), gate_reason=verdict.reason,
        )
        if archive_id and verdict:
            archive.spool_add(conn, archive_id, "person:me")
        conn.commit()
        return archive_id
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m memcal.harness")
    parser.add_argument("action", choices=("context", "archive"))
    parser.add_argument("--home")
    args = parser.parse_args(argv)
    try:
        payload = json.load(sys.stdin)
        cfg = config.load(args.home)
        if args.action == "context":
            sys.stdout.write(context(cfg, str(payload.get("query") or "")))
        else:
            row_id = archive_user_turn(
                cfg, str(payload.get("text") or ""), harness="openclaw",
                session_id=str(payload.get("session_id") or "default"),
                message_id=str(payload.get("message_id") or ""),
                sender=str(payload.get("sender") or "me"),
            )
            json.dump({"archived": row_id}, sys.stdout)
            sys.stdout.write("\n")
        return 0
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
