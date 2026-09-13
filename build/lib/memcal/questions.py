"""Open-question review candidates, versioned actions, and change history."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from . import db

MAX_CANDIDATES = 20
_STOPWORDS = {
    "the", "a", "an", "is", "was", "did", "do", "does", "you", "your", "i",
    "my", "me", "of", "in", "on", "at", "to", "for", "and", "or", "that",
    "this", "it", "with", "who", "what", "which", "when", "where", "how",
}
_NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
}


@dataclass(frozen=True)
class Candidate:
    """One open question nominated for a bundle; no interpretation is made here."""

    key: str
    text: str
    version: str
    wake_condition: str | None
    likely_lines: tuple[int, ...] = ()


def _topic_words(text: str) -> set[str]:
    words = {
        word for word in re.findall(r"[a-z0-9']{3,}", (text or "").lower())
        if word not in _STOPWORDS
    }
    out = set()
    for word in words:
        word = _NUMBER_WORDS.get(word, word)
        out.add("play" if word.startswith("play") else word)
    out.update(re.findall(r"\b\d{1,2}\b", text or ""))
    return out


def candidates(conn: sqlite3.Connection, entities, rows, *, limit: int = MAX_CANDIDATES
               ) -> tuple[list[Candidate], int]:
    """Nominate questions from exact bundle provenance; lexical overlap is only a hint."""
    entities = [str(entity) for entity in dict.fromkeys(entities) if entity]
    if not entities:
        return [], 0
    marks = ",".join("?" for _ in entities)
    found = conn.execute(
        f"""SELECT DISTINCT q.* FROM questions q
              JOIN provenance p ON p.kind = 'question' AND p.ref = q.key
             WHERE q.status = 'open' AND p.entity IN ({marks})
             ORDER BY coalesce(nullif(q.updated_at,''), q.created_at) DESC, q.id DESC""",
        entities,
    ).fetchall()
    overflow = max(0, len(found) - limit)
    nominated = []
    for question in found[:limit]:
        wanted = _topic_words(question["text"])
        scored = []
        for index, row in enumerate(rows, 1):
            overlap = len(wanted & _topic_words(str(row["text"] or "")))
            if overlap:
                scored.append((-overlap, index))
        nominated.append(Candidate(
            key=str(question["key"]), text=str(question["text"]),
            version=str(question["updated_at"] or question["created_at"]),
            wake_condition=(str(question["wake_condition"])
                            if question["wake_condition"] else None),
            likely_lines=tuple(index for _score, index in sorted(scored)[:2]),
        ))
    return nominated, overflow


def record_history(conn: sqlite3.Connection, question_id: int, field: str,
                   old, new, written_by: str) -> None:
    if old == new:
        return
    conn.execute(
        "INSERT INTO question_history(question_id, field, old_value, new_value,"
        " changed_at, written_by) VALUES(?,?,?,?,?,?)",
        (question_id, field, old, new, db.now(), written_by),
    )


def apply_action(conn: sqlite3.Connection, row: dict, *, written_by: str,
                 commit: bool = True) -> tuple[str, str, str] | None:
    """Apply one typed disposition to the exact open-question version reviewed."""
    if not isinstance(row, dict):
        return None
    action = str(row.get("action") or "").strip().lower()
    key = str(row.get("key") or "").strip()
    if action not in {"keep", "amend", "resolve", "drop"} or not key:
        return None
    question = conn.execute(
        "SELECT * FROM questions WHERE key = ? AND status = 'open'", (key,)
    ).fetchone()
    if not question:
        return "rejected-stale", key, key
    current_version = str(question["updated_at"] or question["created_at"])
    if str(row.get("version") or "") != current_version:
        return "rejected-stale", question["text"], key
    if action == "keep":
        return "kept", question["text"], key

    now = db.now()
    if action == "amend":
        text = " ".join(str(row.get("text") or "").split()).strip()
        if not text:
            return "rejected-invalid", question["text"], key
        wake = row.get("wake_condition")
        wake = " ".join(str(wake).split()).strip() if wake is not None else None
        wake = wake or None
        record_history(conn, question["id"], "text", question["text"], text, written_by)
        record_history(conn, question["id"], "wake_condition",
                       question["wake_condition"], wake, written_by)
        if text == question["text"] and wake == question["wake_condition"]:
            return "unchanged", question["text"], key
        conn.execute(
            "UPDATE questions SET text = ?, wake_condition = ?, updated_at = ? WHERE id = ?",
            (text, wake, now, question["id"]),
        )
        outcome = ("amended", text, key)
    elif action == "resolve":
        answer = " ".join(str(row.get("answer") or "").split()).strip()
        if not answer:
            return "rejected-invalid", question["text"], key
        record_history(conn, question["id"], "status", "open", "answered", written_by)
        record_history(conn, question["id"], "answer", question["answer"], answer, written_by)
        conn.execute(
            "UPDATE questions SET status = 'answered', answer = ?, answered_at = ?,"
            " updated_at = ? WHERE id = ?", (answer, now, now, question["id"]),
        )
        outcome = ("resolved", f"{question['text']} — {answer}", key)
    else:
        record_history(conn, question["id"], "status", "open", "dropped", written_by)
        conn.execute(
            "UPDATE questions SET status = 'dropped', updated_at = ? WHERE id = ?",
            (now, question["id"]),
        )
        outcome = ("dropped", question["text"], key)
    if commit:
        conn.commit()
    return outcome
