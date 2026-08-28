"""What is actually in the store right now — archive, spool, bundles, unresolved.

A read-only snapshot for answering "we grabbed 5,000 things, why are there only
eleven bundles?". Nothing here writes.

    python3 tools/state.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import config, db                             # noqa: E402

cfg = config.load(None)
conn = db.open_db(cfg.db_path)
q = lambda sql, *a: conn.execute(sql, a).fetchall()       # noqa: E731

print(f"db: {cfg.db_path}")
print("archive:", q("SELECT count(*) c FROM archive")[0]["c"],
      "gated:", q("SELECT count(*) c FROM archive WHERE gated")[0]["c"])
print("by stream:", [(r["stream"], r["n"], r["gated"])
                     for r in q("SELECT stream, count(*) n, sum(gated) gated FROM archive"
                                " GROUP BY 1 ORDER BY 2 DESC")])
print("spool:", q("SELECT count(*) c FROM spool")[0]["c"],
      "pending:", q("SELECT count(*) c FROM spool WHERE processed_at IS NULL")[0]["c"],
      "entities:", q("SELECT count(DISTINCT entity) c FROM spool"
                     " WHERE processed_at IS NULL")[0]["c"])

print("\n--- pending entities (all)")
for r in q("""SELECT s.entity, count(*) n, min(a.ts) lo, max(a.ts) hi
                FROM spool s JOIN archive a ON a.id = s.archive_id
               WHERE s.processed_at IS NULL GROUP BY 1 ORDER BY 2 DESC"""):
    print(f"  {r['n']:5}  {str(r['lo'])[:10]} -> {str(r['hi'])[:10]}  {r['entity'][:70]}")

print("\n--- gated-but-never-spooled, by stream")
for r in q("""SELECT stream, count(*) n, min(ts) lo, max(ts) hi FROM archive a
               WHERE gated AND id NOT IN (SELECT archive_id FROM spool)
               GROUP BY 1 ORDER BY 2 DESC"""):
    print(f"  {r['n']:5}  {str(r['lo'])[:10]} -> {str(r['hi'])[:10]}  {r['stream']}")

print("\n--- threads seen, by stream (top 30)")
for r in q("""SELECT stream, thread, count(*) n, sum(gated) g, max(ts) hi
                FROM archive GROUP BY 1,2 ORDER BY n DESC LIMIT 30"""):
    print(f"  {r['n']:5} gated={r['g'] or 0:5}  {str(r['hi'])[:10]}  "
          f"{r['stream']}/{str(r['thread'])[:50]}")

print("\nunresolved:", q("SELECT count(*) c FROM unresolved")[0]["c"],
      [(r["stream"], r["n"]) for r in q("SELECT stream, count(*) n FROM unresolved"
                                        " GROUP BY 1 ORDER BY 2 DESC")])
print("runs:", [(r["id"], r["mode"], str(r["started_at"])[:16])
                for r in q("SELECT * FROM runs ORDER BY id DESC LIMIT 5")])
