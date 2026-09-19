"""Treat conversations as first-class entities with names and judgements.

The archive key remains source-defined. Display names are derived from speakers, and
collisions are flagged rather than silently merged. Relevance produces candidates for
review; this module does not infer it from traffic alone.
"""

from __future__ import annotations

import json
import sqlite3

from . import db, identity, presentation

#: Below this a chat is not worth interrupting anyone about, whatever it scores.
REVIEW_MIN_ITEMS = 20

DECISIONS = ("read", "mute")


# ------------------------------------------------------------------ recording --

def record(conn: sqlite3.Connection, channel: str, thread: str | None, *,
           label: str | None = None, participants: list[str] | None = None,
           is_group: bool | None = None, platform_muted: bool | None = None,
           platform_note: str | None = None) -> None:
    """Note what a source knows about a conversation, without touching what we derived.

    Called once per message, so it must be cheap and must never clobber. A source that
    stops reporting participants on some messages (BlueBubbles omits them on the ones
    the user sent) would otherwise blank the roster it filled in a moment earlier.

    `platform_muted` is evidence and nothing more — see `_is_candidate`.
    """
    if not thread:
        return
    # Whitespace in a display name is invisible and lethal: "Crystal Harbor" and
    # "Crystal Harbor " read as one chat missing half its messages.
    label = " ".join((label or "").split()) or None
    roster = db.jdump(sorted({p for p in (participants or []) if p})) if participants else None
    conn.execute(
        """INSERT INTO threads(channel, thread, label, participants, is_group,
                               platform_muted, platform_note, updated_at)
           VALUES(?,?,?,coalesce(?,'[]'),?,coalesce(?,0),?,?)
           ON CONFLICT(channel, thread) DO UPDATE SET
             label        = coalesce(excluded.label, threads.label),
             participants = CASE WHEN ? IS NULL THEN threads.participants
                                 ELSE excluded.participants END,
             is_group     = max(threads.is_group, excluded.is_group),
             platform_muted = CASE WHEN ? IS NULL THEN threads.platform_muted
                                   ELSE excluded.platform_muted END,
             platform_note  = coalesce(excluded.platform_note, threads.platform_note),
             updated_at   = excluded.updated_at""",
        (channel, thread, label, roster, int(bool(is_group)),
         None if platform_muted is None else int(bool(platform_muted)), platform_note,
         db.now(), roster, None if platform_muted is None else 1),
    )
    if participants:
        record_members(conn, channel, thread, [(handle, None) for handle in participants])


def record_members(conn: sqlite3.Connection, channel: str, thread: str | None,
                   members: list[tuple[str, str | None]], *,
                   review_unknown: bool = False) -> int:
    """Record stable handles and every display name observed for a conversation.

    A GroupMe nickname is not an identity. It may be silly, group-specific, or changed
    tomorrow; the GroupMe user id is the durable node. Identity resolution remains the
    explicit `handles` lookup, so two accounts are joined only by Contacts, an exact
    known name, or `memcal who` rather than by fuzzy guessing.

    Returns how many handles it recorded, so a call that can only ever be a no-op is
    visible as one — GroupMe passed the `[]` off a payload whose memberships it had
    asked to omit.
    """
    if not thread:
        return 0
    recorded = 0
    stamp = db.now()
    for raw_handle, raw_name in members:
        handle = identity.normalize(raw_handle)
        name = " ".join((raw_name or "").split()) or None
        if not handle:
            continue
        conn.execute(
            """INSERT INTO thread_members(
                   channel, thread, handle, seen_name, first_seen, last_seen)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(channel, thread, handle) DO UPDATE SET
                 seen_name = coalesce(excluded.seen_name, thread_members.seen_name),
                 last_seen = excluded.last_seen""",
            (channel, thread, handle, name, stamp, stamp),
        )
        if name:
            conn.execute(
                """INSERT INTO thread_member_names(
                       channel, thread, handle, name, first_seen, last_seen, seen_count)
                   VALUES(?,?,?,?,?,?,1)
                   ON CONFLICT(channel, thread, handle, name) DO UPDATE SET
                     last_seen = excluded.last_seen,
                     seen_count = thread_member_names.seen_count + 1""",
                (channel, thread, handle, name, stamp, stamp),
            )
        recorded += 1
        person = identity.resolve(conn, handle)
        if not person and name:
            # `link_by_name` only, deliberately — **not** `adopt_seen_name`. The name on
            # a roster row is a *per-group nickname*: the same GroupMe id is "DJ Pickle"
            # in one chat, "Alexander" in another and "Alex" in a third, which
            # `TestConversationMembership` asserts on purpose. Adopting one of those as
            # the person's name picks whichever group was read first. An exact match
            # against somebody already in Contacts is still safe, and the adoption path
            # runs off `unresolved.seen_name`, which keeps the first name seen per handle
            # rather than the last group scanned.
            person = identity.link_by_name(conn, handle, name, source=f"{channel}:roster")
        if not person and review_unknown:
            identity.note_unresolved(conn, handle, channel, name)
    return recorded


def entity_people(conn: sqlite3.Connection, entity: str) -> list[str]:
    """Canonical non-user people belonging to a bundle entity.

    Read the historical roster as well as message speakers. A new two-line group bundle
    may contain only the user's own line, while the archive already knows the other
    eleven members. Handles are resolved at read time so `memcal who` takes effect
    immediately across old memberships and across apps.
    """
    kind, _, rest = (entity or "").partition(":")
    if kind == "person":
        return [] if identity.is_me(conn, rest) else [rest]
    if kind != "thread":
        return []
    channel, _, thread = rest.partition(":")
    names: set[str] = set()
    for row in conn.execute(
        """SELECT handle FROM thread_members WHERE channel = ? AND thread = ?
           UNION
           SELECT handle FROM archive
            WHERE channel = ? AND thread = ? AND from_me = 0 AND handle IS NOT NULL""",
        (channel, thread, channel, thread),
    ):
        person = identity.resolve(conn, row["handle"])
        if person and not identity.is_me(conn, person):
            names.add(person)
    for row in conn.execute(
        """SELECT DISTINCT person FROM archive
            WHERE channel = ? AND thread = ? AND from_me = 0 AND person IS NOT NULL""",
        (channel, thread),
    ):
        if not identity.is_me(conn, row["person"]):
            names.add(row["person"])
    return sorted(names)


def names_for_handle(conn: sqlite3.Connection, handle: str, limit: int = 6) -> list[str]:
    """Recent per-thread names seen for one stable handle, for identity review."""
    return [row["name"] for row in conn.execute(
        """SELECT name, max(last_seen) AS latest FROM thread_member_names
            WHERE handle = ? GROUP BY name ORDER BY latest DESC LIMIT ?""",
        (identity.normalize(handle), limit),
    )]


def refresh_members(conn: sqlite3.Connection) -> int:
    """Backfill durable rosters and name history from existing thread/archive metadata.

    New connectors write memberships as they ingest. Existing stores already contain
    most of the same evidence in `threads.participants`, archive handles, and
    `meta.seen_name`; rebuilding that graph must not require replaying every source.
    This is idempotent and runs alongside the other derived thread refresh.
    """
    memberships: dict[tuple[str, str, str], dict] = {}

    def observe(channel: str, thread: str, raw_handle: str, name: str | None,
                first: str, last: str) -> None:
        handle = identity.normalize(raw_handle)
        if not channel or not thread or not handle:
            return
        key = (channel, thread, handle)
        item = memberships.setdefault(
            key, {"first": first, "last": last, "latest_name": None, "names": {}})
        item["first"] = min(item["first"], first)
        if last >= item["last"]:
            item["last"] = last
            if name:
                item["latest_name"] = name
        if name:
            seen = item["names"].setdefault(name, [first, last, 0])
            seen[0] = min(seen[0], first)
            seen[1] = max(seen[1], last)
            seen[2] += 1

    for row in conn.execute(
            "SELECT channel, thread, participants, updated_at FROM threads"):
        for handle in db.jload(row["participants"], []):
            observe(row["channel"], row["thread"], handle, None,
                    row["updated_at"], row["updated_at"])

    for row in conn.execute(
        """SELECT channel, thread, handle, meta, ts FROM archive
            WHERE thread IS NOT NULL AND thread != ''
              AND handle IS NOT NULL AND handle != '' ORDER BY ts"""
    ):
        try:
            meta = json.loads(row["meta"] or "{}")
        except (TypeError, ValueError):
            meta = {}
        raw_name = meta.get("seen_name") if isinstance(meta, dict) else None
        name = " ".join(str(raw_name or "").split()) or None
        observe(row["channel"], row["thread"], row["handle"], name,
                str(row["ts"]), str(row["ts"]))

    for (channel, thread, handle), item in memberships.items():
        conn.execute(
            """INSERT INTO thread_members(
                   channel, thread, handle, seen_name, first_seen, last_seen)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(channel, thread, handle) DO UPDATE SET
                 seen_name = coalesce(excluded.seen_name, thread_members.seen_name),
                 first_seen = min(thread_members.first_seen, excluded.first_seen),
                 last_seen = max(thread_members.last_seen, excluded.last_seen)""",
            (channel, thread, handle, item["latest_name"],
             item["first"], item["last"]),
        )
        for name, (first, last, count) in item["names"].items():
            conn.execute(
                """INSERT INTO thread_member_names(
                       channel, thread, handle, name, first_seen, last_seen, seen_count)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(channel, thread, handle, name) DO UPDATE SET
                     first_seen = min(thread_member_names.first_seen, excluded.first_seen),
                     last_seen = max(thread_member_names.last_seen, excluded.last_seen),
                     seen_count = max(thread_member_names.seen_count, excluded.seen_count)""",
                (channel, thread, handle, name, first, last, count),
            )
    return len(memberships)


#: meta key holding the watermark refresh() last fully recomputed at.
REFRESH_WATERMARK_KEY = "threads.refresh_watermark"


def _refresh_watermark(conn: sqlite3.Connection) -> str | None:
    """Fingerprint of everything refresh() reads, from indexes alone.

    The web UI calls refresh() on every tab open, and the full recompute walks
    every archive row in Python — near a second on a large store, paid on every
    click even when nothing arrived since the last one. The recompute reads the
    archive (speakers, timestamps, handles) and the handles table (a dream guess
    renames a thread with no new mail), so those two fingerprints are the whole
    of "did anything refresh could notice change". Both come off index tips, and
    a store too old to have a meta table answers with an error and gets the full
    pass, exactly as today.
    """
    try:
        archive_mark = conn.execute(
            "SELECT count(*), max(id) FROM archive").fetchone()
        handles_mark = conn.execute(
            "SELECT count(*), max(updated_at) FROM handles").fetchone()
    except sqlite3.Error:
        return None
    return (f"{archive_mark[0]}/{archive_mark[1]}"
            f"/{handles_mark[0]}/{handles_mark[1]}")


def refresh(conn: sqlite3.Connection) -> int:
    """Recompute every thread's shape from the archive. Cheap, and safe to re-run.

    Derived rather than counted at ingest because the interesting numbers only become
    true in hindsight: a chat the user has never posted in is a chat the user has never posted in
    *yet*, and one message from them changes the answer for the whole thread.

    Skips the rescan when the watermark says nothing it reads has moved. A skipped
    pass also skips the commit, so a read-only page stops taking the writer lock
    on every open.
    """
    mark = _refresh_watermark(conn)
    if mark is not None:
        try:
            seen = db.get_meta(conn, REFRESH_WATERMARK_KEY)
        except sqlite3.Error:
            seen = None
        if seen == mark:
            try:
                return conn.execute("SELECT count(*) FROM threads").fetchone()[0]
            except sqlite3.Error:
                return 0
    refresh_members(conn)
    rows = conn.execute(
        """SELECT channel, thread,
                  sum(from_me) AS mine,
                  sum(1 - from_me) AS theirs,
                  min(ts) AS first_ts, max(ts) AS last_ts
             FROM archive WHERE thread IS NOT NULL AND thread != ''
            GROUP BY 1, 2"""
    ).fetchall()
    speakers = _speakers(conn)
    mutual_keys = _mutual_keys(conn)
    stamp = db.now()

    for row in rows:
        who = speakers.get((row["channel"], row["thread"]), [])
        named = [w for w in who if w["person"]]
        mutuals = sum(1 for w in who if (w["person"] or w["handle"]) in mutual_keys)
        conn.execute(
            """INSERT INTO threads(channel, thread, members, is_group, mine, theirs,
                                   known, mutuals, first_ts, last_ts, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(channel, thread) DO UPDATE SET
                 members  = max(threads.members, excluded.members),
                 is_group = max(threads.is_group, excluded.is_group),
                 mine = excluded.mine, theirs = excluded.theirs,
                 known = excluded.known, mutuals = excluded.mutuals,
                 first_ts = excluded.first_ts, last_ts = excluded.last_ts,
                 updated_at = excluded.updated_at""",
             (row["channel"], row["thread"], len(who) + 1, int(len(who) > 1),
              row["mine"] or 0, row["theirs"] or 0, len(named), mutuals,
              row["first_ts"], row["last_ts"], stamp),
         )
    conn.commit()
    if mark is not None:
        try:
            db.set_meta(conn, REFRESH_WATERMARK_KEY, mark)
        except sqlite3.Error:
            pass
    return len(rows)


def _speakers(conn: sqlite3.Connection) -> dict[tuple, list[dict]]:
    """Everyone who has spoken into each thread besides the user, busiest first."""
    out: dict[tuple, list[dict]] = {}
    for row in conn.execute(
        """SELECT channel, thread, person, handle, count(*) AS n FROM archive
            WHERE from_me = 0 AND thread IS NOT NULL AND thread != ''
              AND (person IS NULL OR person != 'me')
            GROUP BY 1, 2, 3, 4 ORDER BY n DESC"""
    ):
        seat = out.setdefault((row["channel"], row["thread"]), [])
        key = row["person"] or row["handle"]
        if not key or any((s["person"] or s["handle"]) == key for s in seat):
            continue
        seat.append({"person": row["person"], "handle": row["handle"], "n": row["n"]})
    return out


def _mutual_keys(conn: sqlite3.Connection) -> set[str]:
    """Everyone the user has ever been in a conversation *with*, across all streams."""
    return {row["k"] for row in conn.execute(
        """SELECT DISTINCT coalesce(a.person, a.handle) AS k FROM archive a
             JOIN (SELECT channel, thread FROM archive
                    WHERE from_me = 1 AND thread IS NOT NULL GROUP BY 1, 2) m
               ON m.channel = a.channel AND m.thread = a.thread
            WHERE a.from_me = 0 AND coalesce(a.person, a.handle) IS NOT NULL
              AND coalesce(a.person, a.handle) != 'me'""") if row["k"]}


# --------------------------------------------------------------------- naming --

def _join(names: list[str], extra: int) -> str:
    """"Me, Quinn, and Jamie" — and "Me, Quinn, and 4 others" past that."""
    if extra > 0:
        names = names + [f"{extra} other{'s' if extra > 1 else ''}"]
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


MAX_NAMED = 3


def titles(conn: sqlite3.Connection) -> dict[tuple, str]:
    """A readable name for every thread, in one pass. `{(channel, thread): name}`."""
    speakers = _speakers(conn)
    rows = {(r["channel"], r["thread"]): r
            for r in conn.execute("SELECT channel, thread, label, is_group, mine FROM threads")}
    out = {}
    for key in set(speakers) | set(rows):
        row = rows.get(key)
        label = (row["label"] if row else None) or key[1]
        who = speakers.get(key, [])
        named = [w["person"] for w in who if w["person"]]
        group = bool(row["is_group"]) if row else len(who) > 1

        # One-to-one first, and the person wins over the label. A DM's label is the raw
        # handle — naming the thread "+15551234567" when the archive already knows it is
        # Rowan is the whole failure this function exists to fix, in miniature.
        if not group:
            out[key] = named[0] if named else (who[0]["handle"] if who else label)
            continue
        if not _opaque(label):
            out[key] = label
            continue
        if not named:
            out[key] = label
            continue
        mine = (row["mine"] if row else 0) or 0
        front = (["Me"] if mine else []) + named[:MAX_NAMED]
        out[key] = _join(front, max(0, len(named) - MAX_NAMED))
    return out


def title(conn: sqlite3.Connection, channel: str, thread: str) -> str:
    return titles(conn).get((channel, thread), thread or channel)


def is_guessed_thread(conn: sqlite3.Connection, channel: str, thread: str) -> bool:
    """True when a 1:1 conversation's name is an unconfirmed dream guess.

    Only a lone sender: a guess names one handle, so a thread with several
    speakers is never shown as a guess even if one of them carries one.
    Shared with brief._is_guessed_thread so the web UI and the brief agree.
    """
    if not thread:
        return False
    rows = conn.execute(
        "SELECT DISTINCT handle FROM archive WHERE channel = ?"
        " AND coalesce(thread, '') = ? AND from_me = 0"
        " AND handle IS NOT NULL AND handle != ''", (channel, thread)).fetchall()
    return len(rows) == 1 and identity.is_guessed(conn, rows[0]["handle"])


def guessed_threads(conn: sqlite3.Connection,
                    keys: set[tuple] | None = None) -> set[tuple]:
    """Which (channel, thread) pairs are 1:1 guessed names, in two queries.

    `rows()` and the gate rollup render hundreds of threads; per-thread
    queries would be N+1. Read the guessed handles once, read the distinct
    speakers per thread once, join in Python.
    """
    try:
        guessed_handles = {
            identity.normalize(r["handle"]) for r in conn.execute(
                "SELECT handle FROM handles WHERE source LIKE '%:dream-guess'")}
    except sqlite3.Error:
        return set()
    if not guessed_handles:
        return set()
    if keys is not None and not keys:
        return set()
    if keys is not None:
        chans = sorted({c for c, _ in keys})
        marks = ",".join("?" * len(chans))
        qrows = conn.execute(
            f"""SELECT DISTINCT channel, thread, handle FROM archive
                 WHERE from_me = 0 AND handle IS NOT NULL AND handle != ''
                   AND channel IN ({marks})""", chans).fetchall()
    else:
        qrows = conn.execute(
            """SELECT DISTINCT channel, thread, handle FROM archive
                 WHERE from_me = 0 AND handle IS NOT NULL AND handle != ''""").fetchall()
    per_thread: dict[tuple, set[str]] = {}
    for r in qrows:
        if not r["thread"]:
            continue
        key = (r["channel"], r["thread"])
        if keys is not None and key not in keys:
            continue
        per_thread.setdefault(key, set()).add(identity.normalize(r["handle"]))
    return {k for k, v in per_thread.items()
            if len(v) == 1 and next(iter(v)) in guessed_handles}


def guessed_persons(conn: sqlite3.Connection) -> set[str]:
    """Person names currently backed only by a dream guess, for display prefixing."""
    try:
        return {r["person"] for r in conn.execute(
            "SELECT person FROM handles WHERE source LIKE '%:dream-guess'"
            " AND person IS NOT NULL AND person != ''")}
    except sqlite3.Error:
        return set()


# ------------------------------------------------------------------ merging --
# iMessage splits a conversation in two more often than you would think. When one
# person's phone stops playing along, the same group chat exists twice — once over
# iMessage/RCS and once over SMS — with the same name and the same people, and the two
# chat rows have different identifiers. Their "Crystal Harbor" is exactly this: one row
# carrying SMS and RCS, another carrying RCS, differing by a trailing space in the name.
#
# Nothing downstream can recover from that on its own. Two bundles for one conversation
# means the model reads half of a plan twice and never sees the other half, and it is
# the half that got sent over the other service.

#: Rosters this alike are the same room. A subset always merges (one side may only have
#: a handful of messages so far); otherwise they have to genuinely overlap.
ROSTER_OVERLAP = 0.5


def _one_sided(conn: sqlite3.Connection) -> set[tuple]:
    """Email threads with one speaker the user never answered.

    Email alone: there `threads.label` is a per-message subject, so a sender that mails
    a new subject each time splits into one conversation per notice. Every other channel
    labels the conversation, not the message.
    """
    replied = {(r["channel"], r["thread"]) for r in conn.execute(
        "SELECT DISTINCT channel, thread FROM archive WHERE from_me = 1"
        "  AND thread IS NOT NULL AND thread != ''")}
    speakers = _speakers(conn)
    return {key for key, seat in speakers.items()
            if key[0] == "email" and len(seat) == 1 and key not in replied}


def aliases(conn: sqlite3.Connection) -> dict[tuple, str]:
    """`{(channel, thread): canonical thread}` for conversations that are really one.

    Merges on the name where there is a real one, and on the roster where the name is
    just an id — the same split happens to unnamed group chats, where both halves are
    opaque guids and the people in them are the only thing that matches.

    A shared name is not enough on its own: two unrelated chats can both be called
    "Family". The rosters have to agree as well, so a real name collision stays two
    conversations and gets flagged instead.
    """
    speakers = _speakers(conn)
    one_sided = _one_sided(conn)
    labels = {(r["channel"], r["thread"]): (r["label"] or "") for r in conn.execute(
        "SELECT channel, thread, label FROM threads")}
    counts = {(r["channel"], r["thread"]): r["n"] for r in conn.execute(
        """SELECT channel, thread, count(*) AS n FROM archive
            WHERE thread IS NOT NULL AND thread != '' GROUP BY 1, 2""")}

    buckets: dict[tuple, list[tuple]] = {}
    for key in counts:
        channel, thread = key
        label = labels.get(key) or thread
        roster = frozenset(s["person"] or s["handle"] for s in speakers.get(key, []))
        if _opaque(label) or key in one_sided:
            # No usable name on either side; the people are all there is to match on,
            # and an empty roster matches nothing rather than everything.
            if not roster:
                continue
            bucket = (channel, "roster", roster)
        else:
            bucket = (channel, "label", " ".join(label.split()).casefold())
        buckets.setdefault(bucket, []).append(key)

    out: dict[tuple, str] = {}
    for members in buckets.values():
        if len(members) < 2:
            continue
        # Busiest wins, so the canonical key is the one most rows already point at.
        # Sorted by thread as well, or the choice flips between runs on a tie.
        members.sort(key=lambda k: (-counts.get(k, 0), k[1]))
        head = members[0]
        head_roster = frozenset(s["person"] or s["handle"]
                                for s in speakers.get(head, []))
        for other in members[1:]:
            roster = frozenset(s["person"] or s["handle"] for s in speakers.get(other, []))
            if _compatible(head_roster, roster):
                out[other] = head[1]
    return out


def _compatible(a: frozenset, b: frozenset) -> bool:
    if not a or not b:
        return True                       # one side has barely spoken yet
    if a <= b or b <= a:
        return True
    return len(a & b) / len(a | b) >= ROSTER_OVERLAP


def fold_entity(entity: str, alias: dict[tuple, str]) -> str:
    """Rewrite a `thread:<channel>:<thread>` bundle key onto its canonical thread."""
    kind, _, rest = (entity or "").partition(":")
    if kind != "thread":
        return entity
    channel, _, thread = rest.partition(":")
    canonical = alias.get((channel, thread))
    return f"thread:{channel}:{canonical}" if canonical else entity


def _opaque(label: str) -> bool:
    """Is this "name" actually an internal id? `chat9842975…`, a bare uuid, a number.

    Sources hand back `displayName or chatIdentifier`, so an unnamed group arrives
    already wearing its id as a name and nothing downstream can tell the difference.
    """
    text = (label or "").strip()
    if not text:
        return True
    if text.startswith("chat") and text[4:].isdigit():
        return True
    stripped = text.replace("-", "")
    if len(stripped) >= 24 and all(c in "0123456789abcdefABCDEF" for c in stripped):
        return True
    return text.isdigit() and len(text) > 6


#: Public name for the same test. `brief.attribution` needs it to decide whether a
#: conversation can be named out loud in a sentence the user reads, and reaching into a
#: private helper from another module is how one of these two quietly becomes two.
is_opaque = _opaque


# ------------------------------------------------------------------ decisions --

def muted(conn: sqlite3.Connection) -> set[tuple]:
    return {(r["channel"], r["thread"]) for r in conn.execute(
        "SELECT channel, thread FROM threads WHERE decision = 'mute'")}


def is_muted(conn: sqlite3.Connection, channel: str, thread: str | None) -> bool:
    if not thread:
        return False
    row = conn.execute(
        "SELECT decision FROM threads WHERE channel = ? AND thread = ?", (channel, thread)
    ).fetchone()
    return bool(row and row["decision"] == "mute")


def decide(conn: sqlite3.Connection, channel: str, thread: str, decision: str,
           *, reason: str = "you", by: str = "you") -> dict:
    """Mute a chat or confirm it is worth reading. Muting also clears what is queued.

    Nothing is deleted — the messages stay in the archive and stay searchable. Muting
    says only that no model call will ever be spent on this conversation.

    `by` records who decided — `you` from the UI or CLI, `agent` when the model acts on
    something the user said in passing ("the dev chat's zoom calls, I don't care"). Both are
    permanent; the column exists so the page can say which, and so a future automatic
    guess can be told apart from a judgement.
    """
    if decision not in DECISIONS:
        return {"error": f"decision must be one of {', '.join(DECISIONS)}"}
    if by not in ("you", "agent", "auto"):
        return {"error": f"unknown decider: {by}"}
    conn.execute(
        """INSERT INTO threads(channel, thread, decision, reason, updated_at, decided_by)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(channel, thread) DO UPDATE SET decision = excluded.decision,
             reason = excluded.reason, updated_at = excluded.updated_at,
             decided_by = excluded.decided_by""",
        (channel, thread, decision, reason, db.now(), by),
    )
    retired = 0
    if decision == "mute":
        retired = conn.execute(
            """UPDATE spool SET processed_at = ?
                WHERE processed_at IS NULL AND archive_id IN
                  (SELECT id FROM archive WHERE channel = ? AND thread = ?)""",
            (db.now(), channel, thread),
        ).rowcount
    conn.commit()
    return {"channel": channel, "thread": thread, "decision": decision, "retired": retired}


# -------------------------------------------------------------------- reading --

#: What a platform's own mute is worth. Deliberately not a boolean: "the user muted it" and
#: "the user does not care about it" are different claims, and on their GroupMe they disagree
#: fifteen times out of sixteen.
PLATFORM_MUTE_POLICIES = ("show", "ask", "mute")


def rows(conn: sqlite3.Connection, *, channel: str = "", q: str = "",
         limit: int = 300, policy: str = "show") -> list[dict]:
    """Every conversation, busiest first, with its name and its numbers attached."""
    where, args = ["(t.theirs + t.mine) > 0"], []
    if channel:
        where.append("t.channel = ?")
        args.append(channel)
    if q:
        where.append("(lower(t.thread) LIKE ? OR lower(coalesce(t.label,'')) LIKE ?)")
        args += [f"%{q.lower()}%"] * 2
    found = conn.execute(
        f"""SELECT t.*, (SELECT count(*) FROM spool s JOIN archive a ON a.id = s.archive_id
                          WHERE s.processed_at IS NULL AND a.channel = t.channel
                            AND a.thread = t.thread) AS queued
              FROM threads t WHERE {' AND '.join(where)}
             ORDER BY (t.mine + t.theirs) DESC LIMIT ?""", args + [limit]).fetchall()
    names = titles(conn)
    roster = _speakers(conn)
    guessed = guessed_threads(conn, {(r["channel"], r["thread"]) for r in found})
    persons_guessed = guessed_persons(conn)
    cards = [_card(row, names, roster, policy, guessed, persons_guessed) for row in found]
    # Two conversations with one name is the failure that looks like one conversation
    # with missing messages. Say which ones, and let the roster tell them apart. Scoped
    # per channel: the same friend on iMessage and on WhatsApp is one person, not a clash.
    # Compare on the unprefixed name so a guessed "maybe: X" still collides with a
    # confirmed "X" — they read as the same name at a glance.
    seen: dict[tuple, int] = {}
    for card in cards:
        raw = card["title"]
        if raw.startswith(presentation.GUESS_PREFIX):
            raw = raw[len(presentation.GUESS_PREFIX):]
        key = (card["channel"], raw)
        seen[key] = seen.get(key, 0) + 1
    for card in cards:
        raw = card["title"]
        if raw.startswith(presentation.GUESS_PREFIX):
            raw = raw[len(presentation.GUESS_PREFIX):]
        card["collision"] = seen.get((card["channel"], raw), 0) > 1
    return cards


def _card(row: sqlite3.Row, names: dict[tuple, str],
          roster: dict[tuple, list[dict]] | None = None, policy: str = "show",
          guessed: set[tuple] | None = None,
          persons_guessed: set[str] | None = None) -> dict:
    total = (row["mine"] or 0) + (row["theirs"] or 0)
    who = (roster or {}).get((row["channel"], row["thread"]), [])
    raw_title = names.get((row["channel"], row["thread"]), row["thread"])
    is_guessed = (row["channel"], row["thread"]) in (guessed or set())
    title = presentation.as_guess(raw_title) if is_guessed else raw_title
    speakers = []
    for w in who[:6]:
        name = w["person"] or w["handle"]
        if w["person"] and persons_guessed and w["person"] in persons_guessed:
            name = presentation.as_guess(name)
        speakers.append(name)
    return {
        # The roster is what tells two same-named chats apart, so it travels with the card.
        "speakers": speakers,
        "more_speakers": max(0, len(who) - 6),
        "channel": row["channel"],
        "thread": row["thread"],
        "title": title,
        "guessed": is_guessed,
        "label": row["label"] or "",
        # Shown next to the title because two chats can share a name and this is the
        # only thing that tells them apart at a glance.
        "opaque": _opaque(row["label"] or "") ,
        "group": bool(row["is_group"]),
        "n": total,
        "mine": row["mine"] or 0,
        "share": round(100 * (row["mine"] or 0) / total) if total else 0,
        "known": row["known"] or 0,
        "mutuals": row["mutuals"] or 0,
        "members": row["members"] or 0,
        "queued": row["queued"] or 0,
        "last": str(row["last_ts"] or "")[:10],
        "decision": row["decision"] or "",
        "reason": row["reason"] or "",
        # Evidence, labelled as the platform's own word so it never reads as memcal's
        # decision. The two are called the same thing and mean different things.
        "platform_muted": bool(row["platform_muted"]),
        "platform_note": row["platform_note"] or "",
        "candidate": _is_candidate(row, policy),
    }


def _is_candidate(row: sqlite3.Row, policy: str = "show") -> bool:
    """A chat worth asking about: busy, the user has never spoken, and the user knows nobody in it."""
    if row["decision"]:
        return False
    total = (row["mine"] or 0) + (row["theirs"] or 0)
    if total < REVIEW_MIN_ITEMS or not row["is_group"] or row["mine"]:
        return False
    if policy == "ask" and row["platform_muted"]:
        return True
    return not row["mutuals"]


def review(conn: sqlite3.Connection, limit: int = 25, policy: str = "show") -> list[dict]:
    """The ask-me queue: noisiest unjudged chats the user is not part of."""
    return sorted((c for c in rows(conn, limit=1000, policy=policy) if c["candidate"]),
                  key=lambda c: -c["n"])[:limit]


def apply_platform_mutes(conn: sqlite3.Connection, policy: str = "show") -> int:
    """Under `platform_mute=mute`, take the platform's word for it.

    Off by default and it should stay off unless their habits are the other way round —
    but if they are, this is one line rather than sixty clicks.
    """
    if policy != "mute":
        return 0
    doomed = conn.execute(
        """SELECT channel, thread FROM threads
            WHERE platform_muted = 1 AND decision IS NULL""").fetchall()
    for row in doomed:
        decide(conn, row["channel"], row["thread"], "mute",
               reason="muted on the platform", by="auto")
    return len(doomed)
