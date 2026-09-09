"""What the gate kept and what it threw away, read back out of the archive.

The gate is the cost governor and it runs with no model, so the only way to know
whether it is right is to read what it did. This prints both sides per stream, with
samples, and then the two questions that actually matter:

  * of the mail it dropped, how much looks like it was from a person?
  * of the chat it dropped, how much was an answer to something it kept?

    python3 tools/audit_gate.py
    python3 tools/audit_gate.py email        # one stream, more samples
"""

import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import config, db, gate                       # noqa: E402

only = sys.argv[1] if len(sys.argv) > 1 else ""
cfg = config.load(None)
conn = db.open_db(cfg.db_path)
q = lambda sql, *a: conn.execute(sql, a).fetchall()        # noqa: E731

print("=" * 78)
print("WHY EACH STREAM'S TRAFFIC PASSED OR FAILED")
print("=" * 78)
for row in q("SELECT stream, count(*) n, sum(gated) g FROM archive GROUP BY 1 ORDER BY 2 DESC"):
    if only and row["stream"] != only:
        continue
    share = 100 * (row["g"] or 0) / row["n"]
    print(f"\n{row['stream']}: {row['n']} items, {row['g']} picked up ({share:.0f}%)")
    for r in q("""SELECT gate_reason reason, gated, count(*) n FROM archive
                   WHERE stream = ? GROUP BY 1,2 ORDER BY n DESC LIMIT 14""", row["stream"]):
        mark = "keep" if r["gated"] else "drop"
        print(f"    {mark}  {r['n']:6}  {r['reason']}")

# --------------------------------------------------------------------- email --
if not only or only == "email":
    print("\n" + "=" * 78)
    print("EMAIL: THE DROPPED SENDERS, RANKED — is any of this a person?")
    print("=" * 78)
    rows = q("""SELECT a.handle addr, count(*) n, max(a.ts) last,
                       max(a.gate_reason) why, s.decision
                  FROM archive a LEFT JOIN senders s ON s.address = a.handle
                 WHERE a.stream='email' AND a.from_me=0 AND a.gated=0
                 GROUP BY 1 ORDER BY n DESC LIMIT 40""")
    for r in rows:
        auto = "automated" if gate.is_automated(r["addr"] or "") else "REPLYABLE"
        print(f"  {r['n']:5}  {auto:9}  {(r['decision'] or '?'):8}  {str(r['addr'])[:52]}")

    print("\n  -- dropped senders whose address does NOT look automated:")
    human = [r for r in rows if not gate.is_automated(r["addr"] or "")]
    for r in human:
        subj = q("""SELECT json_extract(meta,'$.subject') s FROM archive
                     WHERE stream='email' AND handle=? ORDER BY ts DESC LIMIT 1""",
                 r["addr"])
        print(f"  {r['n']:5}  {str(r['addr'])[:44]:44}  {r['why']}")
        print(f"         last subject: {str(subj[0]['s'] if subj else '')[:70]}")
    if not human:
        print("  (none — every dropped sender is a machine)")

    print("\n" + "=" * 78)
    print("EMAIL: WHAT PASSED — is any of this junk?")
    print("=" * 78)
    for r in q("""SELECT a.handle addr, count(*) n, max(a.gate_reason) why
                    FROM archive a WHERE a.stream='email' AND a.from_me=0 AND a.gated=1
                   GROUP BY 1 ORDER BY n DESC LIMIT 30"""):
        auto = "AUTOMATED" if gate.is_automated(r["addr"] or "") else "replyable"
        print(f"  {r['n']:5}  {auto:9}  {str(r['addr'])[:46]:46}  {r['why']}")

# ---------------------------------------------------------------- chat drops --
print("\n" + "=" * 78)
print("CHAT: WHAT THE GATE DROPPED, AND WHETHER IT WAS AN ANSWER")
print("=" * 78)
# The case the user raised: "beer garden?" is a proposal, and "yeah i'm down" a day later is
# the only thing that settles it. A dropped line sitting next to a kept one is a line
# whose meaning was in the pair.
for stream in ("groupme", "whatsapp", "imessage"):
    if only and stream != only:
        continue
    dropped = q("""SELECT count(*) n FROM archive WHERE stream=? AND gated=0""", stream)[0]["n"]
    if not dropped:
        continue
    near = q("""SELECT count(*) n FROM archive d
                 WHERE d.stream=? AND d.gated=0 AND EXISTS (
                   SELECT 1 FROM archive k
                    WHERE k.stream=d.stream AND k.thread=d.thread AND k.gated=1
                      AND abs(julianday(k.ts) - julianday(d.ts)) <= 0.0833)""", stream)[0]["n"]
    day = q("""SELECT count(*) n FROM archive d
                WHERE d.stream=? AND d.gated=0 AND EXISTS (
                  SELECT 1 FROM archive k
                   WHERE k.stream=d.stream AND k.thread=d.thread AND k.gated=1
                     AND abs(julianday(k.ts) - julianday(d.ts)) <= 1.5)""", stream)[0]["n"]
    print(f"\n{stream}: {dropped} dropped · {near} within 2h of a kept line "
          f"({100*near/dropped:.0f}%) · {day} within 36h ({100*day/dropped:.0f}%)")
    print("  samples of dropped lines that sit beside a kept one:")
    for r in q("""SELECT d.text, d.ts, d.thread FROM archive d
                   WHERE d.stream=? AND d.gated=0 AND EXISTS (
                     SELECT 1 FROM archive k
                      WHERE k.stream=d.stream AND k.thread=d.thread AND k.gated=1
                        AND abs(julianday(k.ts) - julianday(d.ts)) <= 0.0833)
                   ORDER BY random() LIMIT 12""", stream):
        print(f"    {str(r['text'])[:74]!r}")

# ----------------------------------------------------------- reply shorthand --
print("\n" + "=" * 78)
print("THE SHORTHAND PROBLEM: how much dropped chat is a bare yes or no?")
print("=" * 78)
AGREE = re.compile(r"^\W*(y|ya|yea|yeah|yep|yup|yes|sure|ok|okay|k|word|bet|down|"
                   r"i'?m down|im down|sounds good|works|deal|for sure|fs|100|"
                   r"no|nah|nope|cant|can'?t|maybe|idk)\W*$", re.I)
counts = Counter()
for r in q("""SELECT stream, text FROM archive WHERE gated=0 AND stream != 'email'"""):
    if AGREE.match((r["text"] or "").strip()):
        counts[r["stream"]] += 1
for stream, n in counts.most_common():
    total = q("SELECT count(*) n FROM archive WHERE gated=0 AND stream=?", stream)[0]["n"]
    print(f"  {stream:9} {n:5} of {total} dropped lines are a bare yes/no ({100*n/total:.0f}%)")
if not counts:
    print("  (none)")
