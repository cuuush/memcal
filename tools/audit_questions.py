#!/usr/bin/env python3
"""What would today's apply stage do with the questions already in the store?"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import config, db, todos, trace                       # noqa: E402
from memcal.dream import apply as apply_stage                     # noqa: E402
from memcal.dream.bundle import Bundle                            # noqa: E402


def bundle_for(conn, key: str) -> Bundle:
    """The lines this question was written from, as the bundle the stage would see.

    `provenance.entity` names the bundle; the archive rows behind it are what the model
    actually read. Both are already recorded, so this needs no re-run and no model.
    """
    entity = ""
    stamp = conn.execute(
        "SELECT entity FROM provenance WHERE kind='question' AND ref=?"
        " AND entity IS NOT NULL ORDER BY id DESC LIMIT 1", (key,)).fetchone()
    if stamp:
        entity = stamp["entity"] or ""
    rows = conn.execute(
        """SELECT a.* FROM evidence e JOIN archive a ON a.id = e.archive_id
            WHERE e.kind = 'question' AND e.ref = ? ORDER BY a.ts""", (key,)).fetchall()
    return Bundle(entity=entity or "unknown", items=list(rows))


def verdict_for(row, bundle: Bundle) -> str:
    """What today's apply stage would make of one stored question.

    `written_by` is read before the evidence count because the backward window stamps
    its questions with no `archive_ids` on purpose — it reads state, not the archive,
    so its evidence is the row on `about_event` and there is nothing to cite. Judging
    those by the general rule reports a healthy row as unjudgeable, forever.
    """
    if not bundle.items:
        if row["written_by"] in todos.ASKS_ABOUT_THE_PAST and row["about_event"]:
            return (f"expected: {row['written_by']} cites state, not the archive"
                    f" — evidence is event {row['about_event']}")
        return "no evidence recorded — cannot judge"
    occasion = apply_stage._dated_occasion(row["text"], bundle)
    if occasion:
        return (f"becomes an event: {occasion['date']} {occasion['title']!r}"
                + (f" (note: {occasion['note']})" if occasion["note"] else ""))
    if apply_stage._talks_about_nothing_here(bundle, row["text"]):
        named = ", ".join(sorted(apply_stage._proper_nouns(row["text"])))
        return f"unsupported: nothing in {bundle.label} mentions {named}"
    return "stands"


#: A month name or a numeric day, which `dates.resolve` reads in whichever direction
#: the text points. Anything else that resolves came from a bare weekday, and those it
#: only ever answers *forward*.
SPELLED_OUT = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|\b\d{1,2}/\d{1,2}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b|\btoday\b|\btomorrow\b|\byesterday\b",
    re.IGNORECASE)


def date_coverage(conn) -> dict:
    """How many open questions carry a resolvable day, and by which signal.

    Covers the linked row, `about_date` read from question text, and the TTL fallback.
    """
    out = {"open": 0, "linked": 0, "dated": 0, "undated": 0, "weekday_only": [],
           "exempt": 0}
    for row in conn.execute(
            "SELECT id, text, written_by, about_event, about_date, created_at"
            "  FROM questions WHERE status = 'open' ORDER BY id").fetchall():
        out["open"] += 1
        if row["written_by"] in todos.ASKS_ABOUT_THE_PAST:
            out["exempt"] += 1          # asks about the past on purpose, carries no day
            continue
        if row["about_event"]:
            out["linked"] += 1
            continue
        if row["about_date"]:
            out["dated"] += 1
            if not SPELLED_OUT.search(row["text"] or ""):
                # `dates.resolve` answers forward. A past weekday resolves to the next
                # occurrence and extends the open window.
                out["weekday_only"].append((row["id"], row["about_date"]))
        else:
            out["undated"] += 1
    return out


def report_coverage(cover: dict) -> list[str]:
    unlinked = cover["dated"] + cover["undated"]
    share = f"{cover['dated'] / unlinked:.0%}" if unlinked else "n/a"
    lines = [
        "date coverage of open questions",
        f"  {cover['linked']:>3}  a linked row decides",
        f"  {cover['dated']:>3}  unlinked, day read out of the text ({share} of unlinked)",
        f"  {cover['undated']:>3}  unlinked, no day — falls back to the "
        f"{todos.QUESTION_TTL_DAYS}-day TTL",
        f"  {cover['exempt']:>3}  asks about the past on purpose, carries no day",
    ]
    if unlinked and not cover["dated"]:
        lines.append("  ALARM: no unlinked question carries a day. The propose model has "
                     "stopped writing dates into question text and expiry is back to the "
                     "flat TTL.")
    if cover["weekday_only"]:
        named = ", ".join(f"Q{qid} -> {day}" for qid, day in cover["weekday_only"])
        lines.append(f"  note: {len(cover['weekday_only'])} dated by a bare weekday, "
                     f"which only ever resolves forward: {named}")
    return lines


def main() -> int:
    home = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else None
    cfg = config.load(home)
    conn = db.open_db(cfg.db_path)
    rows = conn.execute(
        "SELECT id, key, text, written_by, about_event FROM questions"
        " WHERE status='open' ORDER BY id").fetchall()
    verdicts: dict[str, list[str]] = {}
    for row in rows:
        bundle = bundle_for(conn, row["key"])
        verdict = verdict_for(row, bundle)
        print(f"Q{row['id']:<3} {verdict}")
        print(f"     {row['text']}")
        print(f"     lines {len(bundle.items)} · {bundle.entity}")
        verdicts.setdefault(verdict.split(":")[0], []).append(f"Q{row['id']}")
    print()
    for name, ids in sorted(verdicts.items()):
        print(f"{len(ids):>3}  {name}  ({', '.join(ids)})")
    print()
    print("\n".join(report_coverage(date_coverage(conn))))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
