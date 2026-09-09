"""What the subject test would rescue, and what it would let in with it.

The gate skipped Headway's appointment reminders and Whatnot's delivery notices on an
address test. `gate.subject_is_event` is meant to rescue exactly those. Whether it does
is a question about ten years of real mail, not about the regex, so this runs it over
every archived email and prints both sides: what comes back, and what rides in with it.

    python3 tools/audit_subject_gate.py            # summary
    python3 tools/audit_subject_gate.py rescued    # every rescued sender
    python3 tools/audit_subject_gate.py noise 40   # a sample of the riskiest
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import config, db, gate                        # noqa: E402

mode = sys.argv[1] if len(sys.argv) > 1 else "summary"
count = int(sys.argv[2]) if len(sys.argv) > 2 else 25

cfg = config.load(None)
conn = db.open_db(cfg.db_path)

rows = conn.execute(
    """SELECT handle, gated, gate_reason, meta, ts FROM archive
        WHERE stream = 'email' AND handle IS NOT NULL""").fetchall()

rescued = defaultdict(list)      # skipped today, event subject → would now pass
agreed = 0                       # already passing, subject also says event
still_out = 0
by_reason = Counter()

for r in rows:
    subject = (db.jload(r["meta"], {}) or {}).get("subject") or ""
    hit = gate.subject_is_event(subject)
    if r["gated"]:
        agreed += bool(hit)
        continue
    if hit:
        rescued[r["handle"]].append((str(r["ts"])[:10], subject[:78]))
        by_reason[r["gate_reason"] or "?"] += 1
    else:
        still_out += 1

total_rescued = sum(len(v) for v in rescued.values())
skipped = sum(1 for r in rows if not r["gated"])

print(f"{len(rows):,} archived emails · {skipped:,} skipped by the gate today")
print(f"the subject test rescues {total_rescued:,} of them "
      f"({100 * total_rescued / max(1, skipped):.1f}%) across {len(rescued)} senders")
print(f"{still_out:,} stay out · {agreed:,} already-passing mails also read as events\n")

print("what they were skipped for:")
for reason, n in by_reason.most_common():
    print(f"  {reason:24} {n:>6,}")

WANTED = ("headway", "whatnot", "wrstbnd", "oe.target", "ridersalliance")
print("\nthe ones the user named:")
for handle, hits in sorted(rescued.items()):
    if any(w in handle for w in WANTED):
        print(f"  {handle[:58]:60} {len(hits):>4} rescued")
        for when, subject in hits[:3]:
            print(f"      {when}  {subject}")

ranked = sorted(rescued.items(), key=lambda kv: -len(kv[1]))
if mode == "rescued":
    print(f"\nevery rescued sender ({len(ranked)}):")
    for handle, hits in ranked:
        print(f"  {len(hits):>5}  {handle}")
elif mode == "noise":
    print(f"\nthe {count} biggest rescued senders — read these as the cost:")
    for handle, hits in ranked[:count]:
        print(f"\n  {handle}  ({len(hits)})")
        for when, subject in hits[:3]:
            print(f"      {when}  {subject}")
else:
    print(f"\ntop 15 rescued senders (`noise 40` to read their subjects):")
    for handle, hits in ranked[:15]:
        print(f"  {len(hits):>5}  {handle[:64]}")

conn.close()
