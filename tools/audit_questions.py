#!/usr/bin/env python3
"""What would today's apply stage do with the questions already in the store?"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import config, db, trace                              # noqa: E402
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


def main() -> int:
    home = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else None
    cfg = config.load(home)
    conn = db.open_db(cfg.db_path)
    rows = conn.execute(
        "SELECT id, key, text FROM questions WHERE status='open' ORDER BY id").fetchall()
    verdicts: dict[str, list[str]] = {}
    for row in rows:
        bundle = bundle_for(conn, row["key"])
        occasion = apply_stage._dated_occasion(row["text"], bundle) if bundle.items else None
        if occasion:
            verdict = (f"becomes an event: {occasion['date']} {occasion['title']!r}"
                       + (f" (note: {occasion['note']})" if occasion["note"] else ""))
        elif bundle.items and apply_stage._talks_about_nothing_here(bundle, row["text"]):
            named = ", ".join(sorted(apply_stage._proper_nouns(row["text"])))
            verdict = f"unsupported: nothing in {bundle.label} mentions {named}"
        elif not bundle.items:
            verdict = "no evidence recorded — cannot judge"
        else:
            verdict = "stands"
        print(f"Q{row['id']:<3} {verdict}")
        print(f"     {row['text']}")
        print(f"     lines {len(bundle.items)} · {bundle.entity}")
        verdicts.setdefault(verdict.split(":")[0], []).append(f"Q{row['id']}")
    print()
    for name, ids in sorted(verdicts.items()):
        print(f"{len(ids):>3}  {name}  ({', '.join(ids)})")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
