"""To-dos, questions, and standing facts.

A to-do dies conversationally. Nothing here closes an item by inference — the
system's job is to raise the question at a plausible moment.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, time, timedelta
from dataclasses import dataclass

from . import dates, db

STANDING_KINDS = ("identity", "preference", "alias")

_TICKET_THING_RE = re.compile(
    r"\b(?:ticket|tickets|pass|passes|seat|seats|reservation|booking)\b",
    re.IGNORECASE,
)
_ACQUIRED_RE = re.compile(
    r"\b(?:got|bought|purchased|booked|confirmed|confirmation|receipt|"
    r"order(?:ed)?|issued|are ours|is ours)\b",
    re.IGNORECASE,
)
_EVENT_TERM_STOP = {
    "the", "and", "with", "movie", "film", "show", "event", "night",
    "festival", "tickets", "ticket",
}


@dataclass
class Todo:
    key: str
    text: str
    status: str = "open"
    event_id: int | None = None
    event_key: str | None = None
    event_title: str | None = None
    event_date: str | None = None
    subject: str | None = None
    due: str | None = None
    remind_at: str | None = None
    reminded_at: str | None = None
    reminder_uid: str | None = None
    wake_condition: str | None = None
    woke_at: str | None = None
    source: str | None = None
    opened_at: str = ""
    id: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Todo":
        columns = set(row.keys())
        return cls(
            id=row["id"], key=row["key"], text=row["text"], status=row["status"],
            event_id=row["event_id"] if "event_id" in columns else None,
            event_key=row["event_key"] if "event_key" in columns else None,
            event_title=row["event_title"] if "event_title" in columns else None,
            event_date=row["event_date"] if "event_date" in columns else None,
            subject=row["subject"], due=row["due"],
            remind_at=row["remind_at"] if "remind_at" in columns else None,
            reminded_at=row["reminded_at"] if "reminded_at" in columns else None,
            reminder_uid=row["reminder_uid"] if "reminder_uid" in columns else None,
            wake_condition=row["wake_condition"],
            woke_at=row["woke_at"], source=row["source"], opened_at=row["opened_at"],
        )

    @property
    def age(self) -> str:
        return db.age_phrase(self.opened_at)

    def one_line(self) -> str:
        # A bare "(today)" is the age, but every reader takes it for a deadline — the
        # user did, and so did the agent, which then told them a to-do with no due date
        # at all was due today. Say which one this is. Nothing here has a deadline
        # unless `due` is set, and that renders separately below.
        age = "opened today" if self.age == "today" else f"opened {self.age} ago"
        line = f"{self.text} ({age})"
        if self.event_title:
            target = self.event_title
            if self.event_date:
                target += " on " + db.parse_date(self.event_date).strftime("%a %b %-d")
            line += f" — for {target}"
        if self.wake_condition and not self.woke_at:
            line += f" — waiting: {self.wake_condition}"
        elif self.woke_at:
            line += " — ready"
        elif self.due:
            line += f" — due {self.due}"
        return line


def open_todo(
    conn: sqlite3.Connection,
    text: str,
    *,
    key: str | None = None,
    subject: str | None = None,
    due: str | None = None,
    remind_at: str | None = None,
    wake_condition: str | None = None,
    event_id: int | None = None,
    source: str | None = None,
    written_by: str = "cli",
    auto_remind: bool = True,
    commit: bool = True,
) -> tuple[Todo, str]:
    key = key or f"todo:{db.slugify(text, 64)}"
    stamp = db.now()
    if remind_at is None and auto_remind:
        remind_at = _deadline_reminder(conn, due=due, event_id=event_id)
    existing = conn.execute("SELECT * FROM todos WHERE key = ?", (key,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE todos SET text = ?, event_id = coalesce(?, event_id),"
            " subject = coalesce(?, subject), due = coalesce(?, due),"
            " remind_at = coalesce(?, remind_at),"
            " wake_condition = coalesce(?, wake_condition), updated_at = ? WHERE key = ?",
            (text, event_id, subject, due, remind_at, wake_condition, stamp, key),
        )
        if commit:
            conn.commit()
        return get(conn, key), "updated"  # type: ignore[return-value]
    conn.execute(
        """INSERT INTO todos(key, text, status, event_id, subject, due, remind_at,
                             wake_condition, source, written_by, opened_at, updated_at)
           VALUES(?,?,'open',?,?,?,?,?,?,?,?,?)""",
        (key, text, event_id, subject, due, remind_at, wake_condition, source, written_by,
         stamp, stamp),
    )
    if commit:
        conn.commit()
    return get(conn, key), "opened"  # type: ignore[return-value]


def _deadline_reminder(conn: sqlite3.Connection, *, due: str | None,
                       event_id: int | None) -> str | None:
    """Return a reminder for a dated commitment that involves another person."""
    if not event_id:
        return None
    row = conn.execute(
        "SELECT date, kind, participants FROM events WHERE id = ?", (event_id,)
    ).fetchone()
    if row is None or (row["kind"] or "") != "commitment":
        return None
    from . import identity                    # circular at module scope
    others = [name for name in db.jload(row["participants"], []) or []
              if name and not identity.is_me(conn, str(name))]
    if not others:
        return None
    # The obligation's own due date wins when it has one — a reservation may have to be
    # made days before the table, and the deadline is the booking, not the dinner.
    return remind_when(due or row["date"])


#: Reminders stay within waking hours.
WAKING_HOURS = (8, 21)
#: Default reminder hour when a full day remains.
REMINDER_HOUR = 9
#: Default lead time for reminders.
REMINDER_LEAD_DAYS = 1


def remind_when(anchor: str | None, *, now: datetime | None = None) -> str | None:
    """When to poke them about something happening on `anchor`."""
    if not anchor:
        return None
    now = now or db.now_dt()
    try:
        day = db.parse_date(anchor[:10])
    except ValueError:
        return None
    target = datetime.combine(day - timedelta(days=REMINDER_LEAD_DAYS),
                              time(REMINDER_HOUR, 0), tzinfo=now.tzinfo)
    if target <= now:
        soon = (now + timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
        target = soon + timedelta(hours=1) if soon <= now + timedelta(hours=1) else soon
    low, high = WAKING_HOURS
    # 21:00 remains usable; only later targets move to the next 08:00 slot.
    if target > datetime.combine(target.date(), time(high, 0), tzinfo=target.tzinfo):
        target = datetime.combine(target.date() + timedelta(days=1), time(low, 0),
                                  tzinfo=target.tzinfo)
    elif target.hour < low:
        target = target.replace(hour=low, minute=0)
    # Never schedule after the anchor day. Once the anchor has passed, return no reminder
    # rather than producing an immediate reminder for a stale obligation.
    latest = datetime.combine(day, time(high, 0), tzinfo=now.tzinfo)
    if latest <= now:
        return None
    if target > latest:
        target = min(latest, now + timedelta(minutes=30))
    return target.isoformat(timespec="seconds")


#: How long after poking the agent about a reminder before poking it again. The agent
#: is the judge — it may decide a reminder is not worth saying anything about, and emit
#: `[SILENT]` — so a poke is *not* proof the user was told, and consuming the reminder on the
#: first poke would silently drop the ones the agent chose to sit on. It comes back, at
#: a cadence that is a nudge rather than a nag. The thing that actually ends it is the
#: to-do closing, which happens conversationally and never by inference (invariant 6).
REMINDER_SNOOZE_HOURS = 4


def due_reminders(conn: sqlite3.Connection, *, now: str | None = None,
                  snooze: bool = True) -> list[Todo]:
    """Open to-dos whose reminder has come due."""
    stamp = now or db.now()
    sql = ["""SELECT t.*, e.key AS event_key, e.title AS event_title, e.date AS event_date
                FROM todos t LEFT JOIN events e ON e.id = t.event_id
               WHERE t.status = 'open' AND t.remind_at IS NOT NULL
                 AND t.remind_at <= ?"""]
    params: list = [stamp]
    if snooze:
        cutoff = (db.parse_ts(stamp) - timedelta(hours=REMINDER_SNOOZE_HOURS)).isoformat()
        sql.append("AND (t.reminded_at IS NULL OR t.reminded_at < ?)")
        params.append(cutoff)
    sql.append("ORDER BY t.remind_at")
    rows = conn.execute(" ".join(sql), params).fetchall()
    return [Todo.from_row(row) for row in rows]


def mark_reminded(conn: sqlite3.Connection, key: str) -> None:
    """Record that the agent was poked about this — not that the user was told.

    Those are different events now, and only the first one is observable from here.
    """
    stamp = db.now()
    conn.execute("UPDATE todos SET reminded_at = ?, updated_at = ? WHERE key = ?",
                 (stamp, stamp, key))
    conn.commit()


def get(conn: sqlite3.Connection, key: str) -> Todo | None:
    row = conn.execute(
        """SELECT t.*, e.key AS event_key, e.title AS event_title, e.date AS event_date
             FROM todos t LEFT JOIN events e ON e.id = t.event_id
            WHERE t.key = ?""", (key,)).fetchone()
    return Todo.from_row(row) if row else None


def find(conn: sqlite3.Connection, needle: str) -> Todo | None:
    """Match a to-do by key, or by a substring of its text. CLI convenience."""
    exact = get(conn, needle)
    if exact:
        return exact
    row = conn.execute(
        """SELECT t.*, e.key AS event_key, e.title AS event_title, e.date AS event_date
             FROM todos t LEFT JOIN events e ON e.id = t.event_id
            WHERE t.status = 'open' AND (t.key LIKE ? OR lower(t.text) LIKE ?)
              AND (e.id IS NULL OR (coalesce(nullif(e.until,''), e.date) >= ?
                                    AND e.status NOT IN ('declined','happened')))
            ORDER BY t.opened_at LIMIT 1""",
        (f"%{needle}%", f"%{needle.lower()}%", db.today().isoformat()),
    ).fetchone()
    return Todo.from_row(row) if row else None


def close(conn: sqlite3.Connection, key: str, status: str = "closed", *,
          commit: bool = True) -> bool:
    cur = conn.execute(
        "UPDATE todos SET status = ?, closed_at = ?, updated_at = ? WHERE key = ? AND status = 'open'",
        (status, db.now(), db.now(), key),
    )
    if commit:
        conn.commit()
    return cur.rowcount > 0


def open_items(conn: sqlite3.Connection) -> list[Todo]:
    rows = conn.execute(
        """SELECT t.*, e.key AS event_key, e.title AS event_title, e.date AS event_date
             FROM todos t LEFT JOIN events e ON e.id = t.event_id
            WHERE t.status = 'open'
              AND (e.id IS NULL OR (coalesce(nullif(e.until,''), e.date) >= ?
                                    AND e.status NOT IN ('declined','happened')))
            ORDER BY t.opened_at""", (db.today().isoformat(),)).fetchall()
    return [Todo.from_row(r) for r in rows]


def expire_event_links(conn: sqlite3.Connection) -> int:
    """Drop linked obligations once their occasion is no longer actionable."""
    cur = conn.execute(
        """UPDATE todos SET status = 'dropped', closed_at = ?, updated_at = ?
            WHERE status = 'open' AND event_id IN (
                SELECT id FROM events
                 WHERE coalesce(nullif(until,''), date) < ?
                    OR status IN ('declined','happened')
            )""",
        (db.now(), db.now(), db.today().isoformat()),
    )
    conn.commit()
    return cur.rowcount


def may_contain_event_proof(conn: sqlite3.Connection, text: str) -> bool:
    """Is a filtered email worth fetching because a linked obligation is waiting?

    This is intentionally only a body-fetch decision. It spends no model call, and the
    full message is still required to name the event before it may enter the spool.
    """
    if not _ACQUIRED_RE.search(text or ""):
        return False
    return any(_TICKET_THING_RE.search(todo.text) for todo in open_items(conn)
               if todo.event_id is not None)


def matching_event_proofs(conn: sqlite3.Connection, text: str) -> list[Todo]:
    """Linked ticket obligations explicitly supported by this source text."""
    body = (text or "").casefold()
    if not (_TICKET_THING_RE.search(body) and _ACQUIRED_RE.search(body)):
        return []
    matched: list[Todo] = []
    for todo in open_items(conn):
        if not todo.event_id or not todo.event_title or not _TICKET_THING_RE.search(todo.text):
            continue
        terms = [
            word for word in re.findall(r"[a-z0-9]+", todo.event_title.casefold())
            if len(word) >= 3 and word not in _EVENT_TERM_STOP
        ]
        needed = min(2, len(set(terms)))
        if needed and sum(1 for word in set(terms) if word in body) >= needed:
            matched.append(todo)
    return matched


def check_wakes(conn: sqlite3.Connection, text_blob: str,
                *, since: str | None = None) -> list[Todo]:
    """Surface to-dos whose wake condition looks satisfied by new traffic."""
    woken: list[Todo] = []
    blob = (text_blob or "").lower()
    if not blob:
        return woken
    stop = {"the", "is", "are", "back", "from", "when", "once", "about", "and", "to", "a", "an", "of", "has"}
    for todo in open_items(conn):
        if not todo.wake_condition or todo.woke_at:
            continue
        if since and str(todo.opened_at or "") >= str(since):
            continue
        words = [w for w in re.findall(r"[a-z']{3,}", todo.wake_condition.lower()) if w not in stop]
        if not words:
            continue
        if sum(1 for w in words if w in blob) >= max(1, len(words) // 2):
            conn.execute("UPDATE todos SET woke_at = ?, updated_at = ? WHERE id = ?",
                         (db.now(), db.now(), todo.id))
            todo.woke_at = db.now()
            woken.append(todo)
    conn.commit()
    return woken


# ------------------------------------------------------------------ questions --

STOPWORDS = {"the", "a", "an", "is", "was", "did", "do", "does", "you", "your", "i",
             "my", "me", "of", "in", "on", "at", "to", "for", "and", "or", "that",
             "this", "it", "with", "who", "what", "which", "when", "where", "how"}


def _keywords(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']{3,}", (text or "").lower())
            if w not in STOPWORDS}


_NUMBER_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
                 "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10"}


def _topic_words(text: str) -> set[str]:
    """Loose topic words for matching a stored question to a later exchange."""
    out = set()
    for word in _keywords(text):
        word = _NUMBER_WORDS.get(word, word)
        if word.startswith("play"):
            word = "play"
        out.add(word)
    # Keep short numerals, which `_keywords` intentionally drops.
    out.update(re.findall(r"\b\d{1,2}\b", text or ""))
    return out


def answer_from_exchange(conn: sqlite3.Connection, entity: str, rows,
                         *, commit: bool = True) -> list[dict]:
    """Answer matching open questions when a direct question receives a reply."""
    open_rows = conn.execute(
        """SELECT DISTINCT q.* FROM questions q
             JOIN provenance p ON p.kind = 'question' AND p.ref = q.key
            WHERE q.status = 'open' AND p.entity = ?""", (entity,)
    ).fetchall()
    if not open_rows:
        return []

    exchanges = []
    ordered = sorted(rows, key=lambda row: (str(row["ts"]), int(row["id"] or 0)))
    for index, sent in enumerate(ordered):
        if not sent["from_me"] or sent["stream"] == "agent":
            continue
        reply = next((row for row in ordered[index + 1:]
                      if not row["from_me"] and row["stream"] == sent["stream"]
                      and row["thread"] == sent["thread"]), None)
        if reply is not None and timedelta(0) <= (
                db.parse_ts(reply["ts"]) - db.parse_ts(sent["ts"])) <= timedelta(days=2):
            exchanges.append((sent, reply, _topic_words(sent["text"])))

    settled = []
    for question in open_rows:
        wanted = _topic_words(question["text"])
        match = next(((sent, reply) for sent, reply, topic in exchanges
                      if len(wanted & topic) >= 2
                      and db.parse_ts(sent["ts"]) >= db.parse_ts(question["created_at"])),
                     None)
        if not match:
            continue
        sent, reply = match
        answer_text = " ".join(str(reply["text"] or "").split())
        conn.execute(
            "UPDATE questions SET status = 'answered', answer = ?, answered_at = ? WHERE id = ?",
            (answer_text, db.now(), question["id"]),
        )
        settled.append({"key": question["key"], "text": question["text"],
                        "answer": answer_text,
                        "archive_ids": [int(sent["id"]), int(reply["id"])]})
    if commit and settled:
        conn.commit()
    return settled


def answer_from_recent_exchanges(conn: sqlite3.Connection, *, days: int = 14,
                                 commit: bool = True) -> list[dict]:
    """Settle open questions from recent archived replies, even if already processed."""
    since = (db.today() - timedelta(days=days)).isoformat()
    entities = [row["entity"] for row in conn.execute(
        """SELECT DISTINCT p.entity FROM provenance p JOIN questions q
               ON p.kind = 'question' AND p.ref = q.key
            WHERE q.status = 'open' AND p.entity IS NOT NULL""")]
    settled = []
    for entity in entities:
        rows = conn.execute(
            """SELECT * FROM (
                   SELECT a.* FROM spool s JOIN archive a ON a.id = s.archive_id
                    WHERE s.entity = ? AND a.ts >= ?
                    ORDER BY a.ts DESC, a.id DESC LIMIT 400
               ) ORDER BY ts, id""", (entity, since)).fetchall()
        for answer in answer_from_exchange(conn, entity, rows, commit=False):
            answer["entity"] = entity
            settled.append(answer)
    if commit and settled:
        conn.commit()
    return settled


# Questions are for the user, about the user's life. A question about memcal's own
# clock or bookkeeping is the system doubting itself out loud, and once stored it sits
# in the brief forever inviting the same confusion that created it.
SELF_REFERENTIAL = re.compile(
    r"\b(?:my (?:date|clock|calendar|records?|sense|understanding|copy|notes?)\b"
    r"|should i (?:shift|update|change|correct) my"
    r"|my (?:sense|understanding) of (?:today|the date|time)"
    r"|is my (?:date|time|clock)"
    r"|(?:shift|adjust) my sense"
    # "Which Casey should confirm the Frozen Far campaign name?" — asking the user to
    # ratify a note we just took is not curiosity about their life, it is us checking
    # our own homework out loud.
    r"|(?:who|which \w+) (?:should|can|could|needs? to) (?:confirm|verify|check)"
    r"|should (?:i|we) (?:record|store|log|note|keep)"
    r"|(?:confirm|verify|double.?check) (?:the |my |this )?(?:note|entry|record|spelling|name)\b"
    r"|did i (?:get|record|note) (?:that|this) right"
    # "The event key 'beer-garden@2026-08-01' has a date suffix of 2026-08-01, but the
    # row's date field says 2026-08-02 — which is correct?" A key is minted once and
    # never changes, so every row that moved date looks like this. The sweep asked it
    # twice in two consecutive runs; only memcal could act on the answer.
    r"|\b(?:event |todo |row )?key\b[^?]*\b(?:suffix|date field|disagree|does not match)"
    r"|\bsuffix\b[^?]*\bdate\b)",
    re.IGNORECASE,
)


def is_self_referential(text: str) -> bool:
    return bool(SELF_REFERENTIAL.search(text or ""))


# Questions the user read and dismissed, by shape rather than by topic. Each one was true,
# unknown, and worthless: nothing the user could answer would change anything the user does. A
# list of five where one matters is worse than a list of one.
NOT_WORTH_ASKING = re.compile(
    # Asking permission to track. "Is finding a lot something you want tracked?"
    r"\b(?:something|anything) you want (?:tracked|me to track|recorded|logged)"
    r"|\bwant me to (?:track|keep track of|add|remember) (?:that|this|it)\b"
    r"|\bshould i (?:track|keep|add) (?:that|this|it)\b"
    # Disambiguating two contacts — memcal's own homework, not their life.
    r"|\bis \"?\w+\"? the same (?:person|one) as\b"
    r"|\bare \"?\w+\"? and \"?\w+\"? the same (?:person|one)\b"
    # Settled past with no action left in it.
    r"|\bwere you (?:fronting|covering|paying for)\b"
    r"|\bdid (?:that|this|it) (?:ever )?(?:get|end up) (?:sorted|resolved|settled)\b"
    # Biography.
    r"|\b(?:is|was) \w+ a (?:fraternity|sorority|club|society) you (?:were|used to be) in\b"
    r"|\bwhere did you (?:go to|attend) (?:school|college|university)\b"
    r"|\b(?:billing|mailing|card) address\b"
    # Asking them to deduplicate the calendar. "Were the three August 7 Elements
    # entries the same event...?" — the sweep's own instructions already forbid this
    # ("duplicate ... -> drop the worse-worded one"), and the prompt rule alone was not
    # enough. The user cannot answer it any better than the row can, and being asked reads as
    # the system handing them its own job.
    r"|\b(?:are|were) (?:the |these |those )?(?:\w+ |\d+ )*"
    r"(?:entries|rows|events|listings|records|copies) .{0,40}\bthe same\b"
    r"|\b(?:is|are) (?:this|that|these|those) a? ?duplicate",
    re.IGNORECASE,
)


def is_worth_asking(text: str) -> bool:
    """Would the answer change anything the user does next? That is the whole bar.

    **This reads the wording and nothing else, which is why it is not the whole gate.**
    Every branch of `NOT_WORTH_ASKING` above was added after somebody read a bad
    question and noticed a phrase, and a list grown that way can only ever catch
    questions that are badly *worded*. "Did Dad's birthday happen on Sunday? who with?"
    is a perfectly good sentence. See `admissible`.
    """
    text = (text or "").strip()
    return bool(text) and not is_self_referential(text) and not NOT_WORTH_ASKING.search(text)


#: Two places are the same place when either name contains the other, so "Elements" and
#: "Elements, Blakeslee PA" do not read as a conflict. Deliberately generous: the cost of
#: calling two places one place is a question that gets asked, and the cost of the
#: reverse is a question that does not.
def _same_place(one: str | None, other: str | None) -> bool:
    a = " ".join((one or "").casefold().split())
    b = " ".join((other or "").casefold().split())
    return bool(a) and bool(b) and (a in b or b in a)


def occupied_elsewhere(conn: sqlite3.Connection, event) -> object | None:
    """A settled commitment that covers this row's day, somewhere else."""
    from . import events as events_mod
    if not (event.location or "").strip():
        return None
    rows = conn.execute(
        """SELECT * FROM events
             WHERE id != ?
               AND date <= ?
               AND COALESCE(until, date) >= ?
               AND status IN ('confirmed', 'happened')
               AND kind IN ('commitment', 'observed')
               AND location IS NOT NULL AND trim(location) != ''
             ORDER BY COALESCE(until, date) DESC, id""",
        (event.id, event.date, event.date),
    ).fetchall()
    for row in rows:
        other = events_mod.Event.from_row(row)
        if event.part_of == other.id or other.part_of == event.id:
            continue
        if _same_place(event.location, other.location):
            continue
        return other
    return None


def admissible(conn: sqlite3.Connection, text: str, *,
               about_event: int | None = None) -> tuple[bool, str]:
    """Return whether the store supports asking this question, with a refusal reason."""
    from . import events as events_mod
    if not is_worth_asking(text):
        return False, "wording"
    if about_event is not None:
        row = events_mod.get_by_id(conn, about_event)
        if row is not None:
            clash = occupied_elsewhere(conn, row)
            if clash is not None:
                return False, (f"the user was at {clash.title} ({clash.location}) that day, "
                               f"not {row.location or 'this'}")
    return True, ""


# A question nobody has engaged with in this long was never going to be answered.
# §12 left this open for rows; the same argument applies to questions, and more so —
# a stale question sits in the brief taking a slot from a live one. "Logan Hayes sent
# you $100 and $122 — were you fronting?" was three days old and long settled.
QUESTION_TTL_DAYS = 10

# A question anchored to a particular evening dies with that evening. "The Lootbox
# chat was lining up an 8/8:30 game session tonight — are you playing?" is a fair
# question for about six hours and litter afterwards.
TONIGHT = re.compile(r"\b(?:tonight|today|this (?:evening|afternoon|morning)|"
                     r"in an hour|right now)\b", re.IGNORECASE)
TONIGHT_TTL_DAYS = 2


def day_it_is_about(text: str, said_on) -> str | None:
    """The last day a question's own words commit to, or None."""
    resolved = [day for phrase in dates.claims(text or "")
                if (day := dates.resolve(phrase, said_on))]
    return max(resolved) if resolved else None


#: The askers whose questions are about the past on purpose. One constant, because the
#: two things it decides are one fact: `_subject_has_passed` must not expire them, and
#: they must not carry a day at all. `dates.resolve` only ever answers *forward* — "Did
#: Play Half-Life 2 happen on Friday?", asked on the Saturday about the Friday before,
#: resolves to the Friday after — so the only date derivable for one of these is wrong
#: by construction, and a wrong date nothing reads today is one something reads later.
ASKS_ABOUT_THE_PAST = ("reconcile",)


def backfill_about_date(conn: sqlite3.Connection) -> int:
    """Read a day out of the questions already stored, against the day each was written."""
    found = 0
    for row in conn.execute(
            "SELECT id, text, created_at FROM questions"
            "  WHERE status = 'open' AND about_date IS NULL"
            f"   AND written_by NOT IN ({','.join('?' * len(ASKS_ABOUT_THE_PAST))})",
            ASKS_ABOUT_THE_PAST).fetchall():
        day = day_it_is_about(row["text"], db.parse_ts(str(row["created_at"])))
        if day:
            conn.execute("UPDATE questions SET about_date = ? WHERE id = ?",
                         (day, row["id"]))
            found += 1
    return found


def _subject_has_passed(conn: sqlite3.Connection) -> list[tuple[int, str, str]]:
    """Return actionable questions whose linked or inferred date has passed."""
    today = db.today().isoformat()
    exempt = ",".join("?" * len(ASKS_ABOUT_THE_PAST))
    out: list[tuple[int, str, str]] = []
    for row in conn.execute(
        f"""SELECT q.id, q.key, q.about_date, e.id AS event_id, e.title, e.status,
                   coalesce(nullif(e.until,''), e.date) AS event_ends
              FROM questions q LEFT JOIN events e ON e.id = q.about_event
             WHERE q.status = 'open' AND q.written_by NOT IN ({exempt})
               AND CASE WHEN e.id IS NOT NULL
                        THEN (coalesce(nullif(e.until,''), e.date) < ?
                              OR e.status IN ('declined', 'happened'))
                        ELSE (q.about_date IS NOT NULL AND q.about_date < ?)
                   END""",
            (*ASKS_ABOUT_THE_PAST, today, today)):
        if row["event_id"] is None:
            why = f"the day it asks about ({row['about_date']}) has passed"
        elif row["status"] == "declined":
            why = f"{row['title']} was declined"
        else:
            why = f"{row['title']} was over on {row['event_ends']}"
        out.append((row["id"], row["key"], why))
    return out


#: What a question is asking for, by how it opens — the same reading
#: `_ASKS_THE_DAY` does, widened to the other two fields a row can answer. Only the
#: opening: a "when" mid-sentence is usually a clause ("let me know when you land").
#: The optional lead is the attribution the prompt asks for, "Mom asked: …".
_ASKS_FOR_FIELD = (
    (re.compile(r"^(?:[^:?]{0,40}:\s*)?(?:what|which) (?:date|day)\b", re.I), "date"),
    (re.compile(r"^(?:[^:?]{0,40}:\s*)?when\b(?=\s+(?:are|is|do|does|will|did|would))",
                re.I), "date"),
    (re.compile(r"^(?:[^:?]{0,40}:\s*)?(?:what|which) time\b", re.I), "time"),
    (re.compile(r"^(?:[^:?]{0,40}:\s*)?where\b(?=\s+(?:are|is|will|does|do))", re.I),
     "location"),
)


def _redundant_with_linked_event(conn: sqlite3.Connection) -> list[tuple[int, str, str]]:
    """Questions the row beside them now answers."""
    out: list[tuple[int, str, str]] = []
    for row in conn.execute(
            """SELECT q.id, q.key, q.text, e.date, e.time, e.location, e.status
                 FROM questions q JOIN events e ON e.id = q.about_event
                WHERE q.status = 'open' AND e.status NOT IN ('mentioned', 'declined')"""):
        field = next((name for rx, name in _ASKS_FOR_FIELD
                      if rx.match(str(row["text"] or ""))), None)
        if field and str(row[field] or "").strip():
            out.append((row["id"], row["key"], f"the row already says {field}"))
    return out


def expire_questions(conn: sqlite3.Connection, days: int = QUESTION_TTL_DAYS) -> int:
    """Drop open questions that are no longer worth a reply.

    Three ways that happens: nobody engaged with it for long enough, its occasion has
    passed, or the store can now answer it itself.
    """
    from . import trace                                             # noqa: PLC0415

    stale = (db.today() - timedelta(days=days)).isoformat()
    tonight = (db.today() - timedelta(days=TONIGHT_TTL_DAYS)).isoformat()
    doomed = [row["id"] for row in conn.execute(
        "SELECT id, text, created_at FROM questions WHERE status = 'open'")
        if row["created_at"] < stale
        or (row["created_at"] < tonight and TONIGHT.search(row["text"] or ""))]
    for question_id, key, why in (_subject_has_passed(conn)
                                  + _redundant_with_linked_event(conn)):
        doomed.append(question_id)
        # A question that disappears without a record of why is indistinguishable from
        # one that was never asked.
        trace.stamp(conn, kind="question", ref=key, verb=f"dropped — {why}",
                    entity="code:expire", stage="code")
    doomed = list(dict.fromkeys(doomed))
    if not doomed:
        return 0
    conn.executemany("UPDATE questions SET status = 'dropped' WHERE id = ?",
                     [(qid,) for qid in doomed])
    conn.commit()
    return len(doomed)


def _record_refusal(conn: sqlite3.Connection, text: str, why: str) -> None:
    """Say out loud that a question was refused, and why.

    A gate that drops things silently is the failure it was built to prevent wearing a
    different hat: "nothing to ask" and "I refused to ask" look identical from outside,
    and the second one is a decision somebody may disagree with. The wording refusals
    are not recorded — those are a static list anyone can read — only the ones decided
    against the store, which are the ones that could be wrong on this row today.
    """
    if why == "wording":
        return
    from . import trace
    trace.stamp(conn, kind="question", ref=f"refused:{db.slugify(text, 48)}",
                verb="refused", entity=why, stage="code")


def ask(conn: sqlite3.Connection, text: str, *, key: str | None = None,
        about_event: int | None = None, about_todo: int | None = None,
        said_on=None, written_by: str = "cli", commit: bool = True) -> str:
    # The single choke point. Every asker goes through here — `apply`, both of `sweep`'s
    # paths, the wake in `run`, and the CLI — so the gate belongs here and not in any of
    # them, and no future caller can be written that forgets it. `about_date` is derived
    # here for the same reason: a caller that has to remember to fill a column is a
    # column that is empty on the paths nobody thought about.
    #
    # `said_on` is the moment the words were said, which is what a weekday is relative
    # to. `apply` passes its bundle's newest line; everything else is writing now about
    # now, so now is the honest anchor.
    ok, why = admissible(conn, text, about_event=about_event)
    if not ok:
        _record_refusal(conn, text, why)
        return ""            # a question the user would not answer is worse than no question
    # One correction in a real session produced five questions, two of them the same
    # question in different words ("Which Casey runs..." / "Which Casey is the DM...").
    # The brief has a hard cap, so a near-duplicate costs a real one its place.
    wanted = _keywords(text)
    if wanted:
        for existing in open_questions(conn, limit=50):
            have = _keywords(existing["text"])
            if have and len(wanted & have) / len(wanted | have) >= 0.6:
                return existing["key"]
    if about_todo is None:
        about_todo = _todo_it_is_about(conn, wanted)
    key = key or f"q:{db.slugify(text, 64)}"
    conn.execute(
        "INSERT INTO questions(key, text, about_event, about_todo, about_date,"
        "                      written_by, created_at)"
        " VALUES(?,?,?,?,?,?,?) ON CONFLICT(key) DO NOTHING",
        (key, text, about_event, about_todo,
         None if written_by in ASKS_ABOUT_THE_PAST
         else day_it_is_about(text, said_on or db.now_dt()),
         written_by, db.now()),
    )
    if commit:
        conn.commit()
    return key


# Words that carry no subject. Every to-do is about planning something on some day, so
# sharing "plan" or "trip" says nothing — and at a low threshold it said everything:
# "Plan Coney Island trip with Avery" collected nine questions, among them "Is the
# Disney/Kissimmee trip August 2027?", on the strength of the word "trip".
GENERIC = {"plan", "plans", "planning", "trip", "get", "got", "go", "going", "need",
           "needs", "want", "put", "day", "days", "date", "dates", "time", "times",
           "week", "weekend", "back", "thing", "things", "stuff", "ask", "tell", "send",
           "make", "take", "actually", "still", "next", "last", "one", "new", "know",
           # Occasion nouns. Every one of these names the *shape* of a gathering and
           # none of them names its subject, so sharing one says only "these are both
           # things on a calendar". A board-game night and a tutor's appointment
           # both filed themselves under "Alumni meeting" on the word `meeting`, and a
           # parking question under "Hang out with Quinn" on the word `out`.
           "meeting", "out", "event", "night", "party", "session", "call", "hang",
           "dinner", "lunch", "birthday"}

# Most of a to-do's distinctive words have to appear for a question to be about it.
# Set high on purpose: the useful link is a question that is *evidence the to-do may be
# done*, not any question mentioning the same person. A wrong link is worse than none,
# because it hides a real question under an unrelated item.
ABOUT_THRESHOLD = 0.5


def _subject_words(text: str) -> set[str]:
    return _keywords(text) - GENERIC


def _todo_it_is_about(conn: sqlite3.Connection, wanted: set[str]) -> int | None:
    """Find the open to-do this question is evidence about.

    memcal held the to-do "Venmo Emery for the artwork" and, separately, the question
    "did the $50 Venmo to Emery on Jul 7 cover it?" — the receipt and the obligation,
    sitting in two different blocks of the brief as strangers. Nothing was wrong with
    either row. What was missing was the line between them, and it is free: both are
    already in the database, and this is set arithmetic, not a model call.
    """
    wanted = wanted - GENERIC
    if not wanted:
        return None
    best, best_score = None, 0.0
    for todo in open_items(conn):
        have = _subject_words(todo.text)
        if not have:
            continue
        # Containment on the to-do's side, not Jaccard: the question is usually much
        # longer because it carries the evidence, and Jaccard punishes exactly that.
        score = len(wanted & have) / len(have)
        if score > best_score:
            best, best_score = todo, score
    return best.id if best is not None and best_score >= ABOUT_THRESHOLD else None


#: How far a question may reach for the row it is about. Backwards barely at all — a
#: question about something that already happened is expired, not linked — and forwards
#: as far as a row can sit in the store.
EVENT_LINK_BACK_DAYS = 3
EVENT_LINK_FORWARD_DAYS = 120

#: How much of a row's *title* the question has to repeat before they are the same
#: subject. A row's identity is its name: matching on participants alone linked "what
#: date and showtime are planned with Quinn, Priya and Katie" to whichever unrelated
#: row those three also stood on, and a wrong link buries a real question under a row
#: nobody was asking about.
TITLE_MATCH = 0.5

#: ...and a ratio alone is not enough, because half of a two-word title is one word.
#: Every wrong link on the live store scored exactly 0.50 on exactly one shared word.
#: So a proportion *and* a floor: two distinctive words in common, or a single word
#: that names only this row in the whole store.
MIN_SHARED_TITLE_WORDS = 2


def _title_word_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many rows' titles each distinctive word appears in.

    "elements" is in both "Elements festival" and "Breakfast at Elements", so it does
    not identify either on its own; "aspca" is in one row and identifies it outright.
    That difference is what makes a one-word match sometimes safe and usually not, and
    it is free to compute over a store this size.
    """
    counts: dict[str, int] = {}
    for row in conn.execute("SELECT title FROM events"):
        for word in _subject_words(row["title"] or ""):
            counts[word] = counts.get(word, 0) + 1
    return counts


def _event_it_is_about(conn: sqlite3.Connection, wanted: set[str]):
    """The calendar row this question is asking about, or None.

    "When are you and Priya leaving for the Montana trip with Morgan?" and the
    Montana trip were two rows in two blocks of the brief with nothing between them —
    so the question read as though memcal had no idea when the trip was, while the row
    two inches above it said 15–23 August. Both facts were already in the database; what
    was missing is set arithmetic.
    """
    from . import events                                            # noqa: PLC0415

    wanted = wanted - GENERIC
    if not wanted:
        return None
    counts = _title_word_counts(conn)
    best, best_score = None, 0
    for event in events.window(conn, EVENT_LINK_BACK_DAYS, EVENT_LINK_FORWARD_DAYS):
        if event.status == "declined":
            continue
        people = set()
        for person in [event.subject, *event.participants]:
            people |= _subject_words(person or "")
        # The people on a row are not what the row *is*. "Gym with Morgan" has "reese"
        # in its title, so half its words matched every question that mentioned them —
        # Spider-Man, Orlando, a camping trip and a tutor's appointment all filed
        # themselves under a gym session. What identifies a row is the part of its name
        # that is not a guest list.
        title_words = _subject_words(event.title) - people
        if not title_words:
            continue
        shared = title_words & wanted
        if len(shared) / len(title_words) < TITLE_MATCH:
            continue
        # The floor, on top of the proportion. One word in common is a coincidence at
        # this corpus size unless that word names this row and nothing else.
        if len(shared) < MIN_SHARED_TITLE_WORDS and not all(
                counts.get(word, 0) <= 1 for word in shared):
            continue
        have = title_words | people
        score = len(wanted & have)
        if score > best_score:
            best, best_score = event, score
    return best


def relink_questions(conn: sqlite3.Connection) -> int:
    """Recompute open-question links and inferred dates without clearing unmatched links."""
    linked = 0
    for row in conn.execute(
        "SELECT id, text, about_todo, about_event FROM questions WHERE status = 'open'"
    ).fetchall():
        wanted = _keywords(row["text"])
        todo_id = _todo_it_is_about(conn, wanted)
        if todo_id:
            if row["about_todo"] != todo_id:
                conn.execute(
                    "UPDATE questions SET about_todo = ?, about_event = NULL"
                    " WHERE id = ?", (todo_id, row["id"]))
                linked += 1
            continue
        event = _event_it_is_about(conn, wanted)
        if event is not None and row["about_event"] != event.id:
            conn.execute(
                "UPDATE questions SET about_event = ?, about_todo = NULL WHERE id = ?",
                (event.id, row["id"]))
            linked += 1
    linked += _sharpen_linked_questions(conn)
    backfill_about_date(conn)
    conn.commit()
    return linked


#: A question that opens by asking *which day*. Only the opening, and only these forms:
#: "when" buried mid-sentence is usually part of a clause ("let me know when you land")
#: rather than the thing being asked.
_ASKS_THE_DAY = re.compile(
    r"^(?:(?P<lead>[^:]{1,40}:\s*)?)(?P<when>when)\b(?=\s+(?:are|is|do|does|will|did|would))",
    re.IGNORECASE,
)


def _sharpen_linked_questions(conn: sqlite3.Connection) -> int:
    """Ask for what is missing, not for what the row beside it already says."""
    from . import trace                                             # noqa: PLC0415

    changed = 0
    rows = conn.execute(
        """SELECT q.id, q.key, q.text, e.date, e.time, e.status
             FROM questions q JOIN events e ON e.id = q.about_event
            WHERE q.status = 'open'""").fetchall()
    for row in rows:
        if row["time"] or row["status"] == "mentioned":
            continue
        match = _ASKS_THE_DAY.match(row["text"] or "")
        if not match:
            continue
        start, end = match.span("when")
        text = row["text"][:start] + "What time" + row["text"][end:]
        conn.execute("UPDATE questions SET text = ? WHERE id = ?", (text, row["id"]))
        # A question whose words changed without a record of why is indistinguishable
        # from a model having written it that way.
        trace.stamp(conn, kind="question", ref=row["key"], verb="sharpened",
                    entity="code:relink", stage="code")
        changed += 1
    return changed


def questions_by_todo(conn: sqlite3.Connection) -> dict[int, list[sqlite3.Row]]:
    """Open questions grouped by the open to-do they are evidence about."""
    grouped: dict[int, list[sqlite3.Row]] = {}
    rows = conn.execute(
        "SELECT q.* FROM questions q JOIN todos t ON t.id = q.about_todo"
        " WHERE q.status = 'open' AND t.status = 'open' ORDER BY q.created_at"
    ).fetchall()
    for row in rows:
        grouped.setdefault(row["about_todo"], []).append(row)
    return grouped


def questions_by_event(conn: sqlite3.Connection) -> dict[int, list[sqlite3.Row]]:
    """Open questions grouped by the calendar row they are about.

    A declined row keeps its questions to itself: "what time are you leaving" under a
    trip the user has called off is noise with a date on it.
    """
    grouped: dict[int, list[sqlite3.Row]] = {}
    rows = conn.execute(
        "SELECT q.* FROM questions q JOIN events e ON e.id = q.about_event"
        " WHERE q.status = 'open' AND e.status != 'declined' ORDER BY q.created_at"
    ).fetchall()
    for row in rows:
        grouped.setdefault(row["about_event"], []).append(row)
    return grouped


def open_questions(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM questions WHERE status = 'open' ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()


def resolve(conn: sqlite3.Connection, needle: str, answer_text: str) -> tuple[bool, str]:
    """Resolve anything the brief is holding open — a question or a to-do.

    An agent reading the brief sees one list of unresolved things and reasonably
    reaches for one verb. Making the caller first work out which table an item lives
    in is our bookkeeping leaking into their reasoning; a real session tried to
    "answer" a to-do and got told no such question exists.

    Returns (resolved, what_kind).
    """
    if answer(conn, needle, answer_text):
        return True, "question"

    todo = find(conn, needle)
    if todo:
        # Closing is still a conversational act — the user said so, we are recording it.
        close(conn, todo.key)
        return True, "todo"

    # Nothing open matched — but an agent often writes the same fact twice in one turn
    # (once as a memory, once as an answer), and the first write closes the item. In a
    # real session the second call came back "nothing open matches that", which reads
    # as a failure and prompted a retry against something that was already correct.
    if _already_settled(conn, needle):
        return True, "already"
    return False, ""


def _already_settled(conn: sqlite3.Connection, needle: str) -> bool:
    """Did a closed to-do or an answered question already cover this?"""
    wanted = _subject_words(needle)
    if not wanted:
        return False
    rows = conn.execute(
        "SELECT text FROM todos WHERE status != 'open'"
        " UNION ALL SELECT text FROM questions WHERE status != 'open'"
    ).fetchall()
    for row in rows:
        have = _subject_words(row["text"])
        if have and len(wanted & have) / len(have) >= ABOUT_THRESHOLD:
            return True
    return False


def answer(conn: sqlite3.Connection, key_or_text: str, answer_text: str) -> bool:
    row = conn.execute(
        "SELECT * FROM questions WHERE status = 'open' AND (key = ? OR key LIKE ? OR lower(text) LIKE ?)"
        " ORDER BY created_at LIMIT 1",
        (key_or_text, f"%{key_or_text}%", f"%{key_or_text.lower()}%"),
    ).fetchone()
    if not row:
        # Nobody quotes a question back verbatim. An agent that just read "Which Casey
        # is the DM of the Crystal Harbor campaign?" will say "Which Casey runs the
        # Crystal Harbor campaign" — same question, no common substring. Match on the
        # words that carry meaning instead.
        wanted = _keywords(key_or_text)
        if wanted:
            best, best_score = None, 0.0
            for candidate in open_questions(conn, limit=50):
                have = _keywords(candidate["text"])
                if not have:
                    continue
                score = len(wanted & have) / len(wanted | have)
                if score > best_score:
                    best, best_score = candidate, score
            if best is not None and best_score >= 0.34:
                row = best
    if not row:
        return False
    conn.execute(
        "UPDATE questions SET status = 'answered', answer = ?, answered_at = ? WHERE id = ?",
        (answer_text, db.now(), row["id"]),
    )
    conn.commit()
    return True


def drop_question(conn: sqlite3.Connection, key: str) -> None:
    conn.execute("UPDATE questions SET status = 'dropped' WHERE key = ?", (key,))
    conn.commit()


# ------------------------------------------------------------------- standing --

def set_standing(conn: sqlite3.Connection, kind: str, value: str, *, key: str | None = None,
                 scope: str = "permanent", written_by: str = "cli",
                 commit: bool = True) -> tuple[str, str]:
    """Preferences are session-scoped by default; repetition promotes them."""
    if kind not in STANDING_KINDS:
        raise ValueError(f"kind must be one of {STANDING_KINDS}")
    key = key or f"{kind}:{db.slugify(value, 48)}"
    stamp = db.now()
    row = conn.execute("SELECT * FROM standing WHERE key = ?", (key,)).fetchone()
    if row:
        hits = row["hits"] + 1
        new_scope = "permanent" if (hits >= 2 or scope == "permanent") else row["scope"]
        conn.execute(
            "UPDATE standing SET value = ?, hits = ?, scope = ?, updated_at = ? WHERE key = ?",
            (value, hits, new_scope, stamp, key),
        )
        if commit:
            conn.commit()
        return key, ("promoted" if new_scope != row["scope"] else "updated")
    conn.execute(
        "INSERT INTO standing(key, kind, value, scope, written_by, created_at, updated_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (key, kind, value, scope, written_by, stamp, stamp),
    )
    if commit:
        conn.commit()
    return key, "added"


def standing(conn: sqlite3.Connection, kind: str | None = None, permanent_only: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM standing WHERE 1=1"
    args: list[object] = []
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    if permanent_only:
        sql += " AND scope = 'permanent'"
    sql += " ORDER BY kind, updated_at DESC"
    return conn.execute(sql, args).fetchall()


def forget_standing(conn: sqlite3.Connection, key: str) -> bool:
    cur = conn.execute("DELETE FROM standing WHERE key = ?", (key,))
    conn.commit()
    return cur.rowcount > 0
