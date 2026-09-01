"""Seed a memcal home with acceptance test cases as synthetic stream traffic.

Provides deterministic synthetic data for end-to-end verification of dream
and brief operations without external message sources.

    python3 tests/fixture_acceptance.py --home /tmp/memcal-accept
    MEMCAL_HOME=/tmp/memcal-accept python3 -m memcal dream
    MEMCAL_HOME=/tmp/memcal-accept python3 -m memcal brief
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memcal import archive, brief, config, db, gate, identity, todos, wiki  # noqa: E402

# (days_ago, stream, thread, person, from_me, text)
TRAFFIC = [
    # 1. Stale email superseded by recent messages.
    (365, "email", "frat-listserv", "College Club Listserv", False,
     "Brothers — poker night this Saturday at the chapter house, 21 Waverly Pl. Bring cash."),
    # Recent message takes precedence.
    (0, "imessage", "jordan-sms", "Jordan", False,
     "poker friday at my buddy alex's place, 42 Example Street, we're starting around 8"),
    (0, "imessage", "jordan-sms", "Jordan", False, "you in?"),
    (0, "imessage", "jordan-sms", "me", True, "yeah i'm in, see you friday"),

    # 2. Mentioned-to-confirmed state progression across a thread.
    (6, "imessage", "cameron-sms", "Cameron", False,
     "thinking of doing a poker game in two weeks if you're around"),
    (0, "imessage", "cameron-sms", "Cameron", False, "you still going to be around for that game?"),
    (0, "imessage", "cameron-sms", "me", True, "yeah should be"),

    # 3. Unanswered inbound invitation.
    (2, "email", "hudson-farm", "Hudson Valley Farm Sanctuary", False,
     "Volunteer day Saturday Aug 1 — goat care and barn cleaning, 9am-2pm, Hudson Valley. "
     "Reply to claim a spot."),

    # 5. Third-party availability window.
    (0, "imessage", "alex-sms", "Alex", False, "i'm free monday night if you wanna do something"),

    # 6. Lateral connection: visiting contact.
    (0, "imessage", "kev-sms", "Riley", False,
     "gonna be in the city wednesday through sunday, staying with you if that's cool"),

    # 7. Non-actionable noise rejection.
    (0, "imessage", "harper-sms", "Harper", False, "i love you"),
    (0, "imessage", "harper-sms", "Harper", False, "miss you, text me when you're home"),
    (0, "imessage", "cameron-sms", "Cameron", False, "honestly man you need a real job"),
    (0, "agent", "conversation", "me", True,
     "you can use bash to save files, just use the write tool next time"),

    # 8. Debounce: short non-informative messages.
    (0, "imessage", "alex-sms", "Alex", False, "hey"),
    (0, "imessage", "alex-sms", "Alex", False, "lol"),

    # 9. Attribute extraction.
    (1, "imessage", "kev-sms", "Riley", False,
     "took the kids to the aquarium, i could watch the otters all day, favorite animal by far"),

    # 11. Wake condition: pending dependency.
    (30, "imessage", "rowan-sms", "me", True,
     "i need to give rowan back their ezpass when the user's back from italy"),
    (0, "imessage", "rowan-sms", "Rowan", False, "just landed back from italy, jet lagged as hell"),

    # 13. Calendar vocabulary alias.
    (0, "agent", "conversation", "me", True,
     "put dinner with harper on our cal for thursday at 7"),

    # 14. Disambiguation across duplicate names.
    (0, "imessage", "worksms", "Alex Chen", False,
     "can we push the standup to thursday? i'm out tuesday"),
]

# Bulk email senders filtered out before calendar extraction.
BULK_EMAIL = [
    ("news@amazonses.com", "AWS re:Invent night is tomorrow — join us at 7pm",
     {"List-Unsubscribe": "<mailto:unsub@amazonses.com>", "Precedence": "bulk"}),
    ("hello@kindred.com", "You're invited: Kindred member mixer this Thursday",
     {"List-ID": "<members.kindred.com>"}),
    ("billing@squarespace.com", "Your domain example.org renews on Aug 14", {}),
]


def seed(home: Path) -> None:
    cfg = config.load(home)
    cfg.ensure_dirs()
    conn = db.open_db(cfg.db_path)

    identity.set_me(conn, "Casey", "Casey Morgan")
    wiki.set_slot(cfg.wiki_dir, "casey", "neighborhood", "North End",
                  source="fixture", conn=conn)
    wiki.set_slot(cfg.wiki_dir, "casey", "dog", "Comet", source="fixture", conn=conn)
    wiki.set_slot(cfg.wiki_dir, "u-and-me-calendar", "meaning",
                  "shared calendar for Casey and Harper", source="fixture",
                  section="projects", conn=conn)
    for alias in ("our cal", "shared cal", "u&me"):
        wiki.add_alias(cfg.wiki_dir, "u-and-me-calendar", alias, section="projects",
                       conn=conn)
    identity.add_top_tier(conn, "Harper")
    for person, handle in (("Jordan", "+19175550001"), ("Alex", "+19175550002"),
                           ("Cameron", "+19175550003"), ("Harper", "+19175550004"),
                           ("Riley", "+19175550005"), ("Rowan", "+19175550006"),
                           ("Alex Chen", "+19175550007")):
        identity.link(conn, handle, person, source="fixture")

    tier = identity.top_tier(conn)
    spooled = 0
    for days_ago, stream, thread, person, from_me, text in TRAFFIC:
        ts = (db.today() - timedelta(days=days_ago)).isoformat() + "T18:00:00"
        verdict = gate.gate_message(text, person=person, from_me=from_me, top_tier=tier)
        archive_id = archive.append(
            conn, stream=stream, external_id=f"fixture:{stream}:{thread}:{text[:24]}",
            ts=ts, text=text, thread=thread, person=None if from_me else person,
            from_me=from_me, gated=bool(verdict), gate_reason=verdict.reason,
        )
        if archive_id and verdict:
            archive.spool_add(conn, archive_id, gate.bundle_entity(
                person if not from_me else _counterpart(thread), thread, stream))
            spooled += 1

    for address, subject, headers in BULK_EMAIL:
        verdict = gate.gate_email(conn, address=address, subject=subject, headers=headers)
        archive_id = archive.append(
            conn, stream="email", external_id=f"fixture:email:{address}", ts=db.now(),
            text=subject, thread=address, handle=address, person=None,
            gated=bool(verdict), gate_reason=verdict.reason,
        )
        if archive_id and verdict:
            archive.spool_add(conn, archive_id, gate.bundle_entity(None, address, "email"))
            spooled += 1
    conn.commit()

    passed = conn.execute("SELECT count(*) AS n FROM archive WHERE gated = 1").fetchone()["n"]
    total = conn.execute("SELECT count(*) AS n FROM archive").fetchone()["n"]
    print(f"seeded {home}")
    print(f"archived {total}, gate passed {passed}, spooled {spooled}")
    for row in conn.execute(
            "SELECT gate_reason, count(*) AS n FROM archive GROUP BY gate_reason ORDER BY n DESC"):
        print(f"  {row['gate_reason']:22} {row['n']}")
    brief.write(conn, cfg)


def _counterpart(thread: str) -> str | None:
    """Return thread counterpart entity for bundling outgoing messages."""
    return {"jordan-sms": "Jordan", "cameron-sms": "Cameron", "alex-sms": "Alex",
            "kev-sms": "Riley", "rowan-sms": "Rowan", "harper-sms": "Harper",
            "worksms": "Alex Chen"}.get(thread)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    args = parser.parse_args()
    seed(Path(args.home).expanduser())
