"""Temporary compatibility support for retiring legacy stores."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import db


STANDING_DESTINATIONS = ("wiki", "identity", "config", "discarded")


@dataclass(frozen=True)
class StandingRedirect:
    old_id: int
    old_key: str
    old_kind: str
    old_value: str
    old_scope: str
    old_written_by: str
    old_created_at: str
    destination_kind: str
    destination_ref: str | None
    retired_at: str

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "StandingRedirect":
        return cls(**{name: row[name] for name in cls.__dataclass_fields__})

    @property
    def destination(self) -> str:
        if self.destination_kind == "discarded":
            return "discarded"
        return f"{self.destination_kind}:{self.destination_ref}"


def standing_redirect(conn: sqlite3.Connection, key_or_id: str | int
                      ) -> StandingRedirect | None:
    """Find a retired standing row by its old key or numeric S-handle id."""
    if isinstance(key_or_id, int) or str(key_or_id).isdigit():
        row = conn.execute(
            "SELECT * FROM standing_redirects WHERE old_id = ?", (int(key_or_id),)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM standing_redirects WHERE old_key = ?", (str(key_or_id),)
        ).fetchone()
    return StandingRedirect.from_row(row) if row else None


def retire_standing(
    conn: sqlite3.Connection,
    key: str,
    *,
    destination_kind: str,
    destination_ref: str | None = None,
    commit: bool = True,
) -> tuple[StandingRedirect, str]:
    """Retire one standing row after its typed destination has been written.

    This function owns only the compatibility move: snapshot the old row, preserve its
    S handle, and remove it from the active legacy table. Callers write and validate a
    wiki, identity, or config destination first, in the same larger workflow. A discard
    is explicit and carries no destination reference.

    Repeating the same decision is safe. Reusing an old key with a different decision is
    an error rather than silently changing where a historical handle points.
    """
    kind = str(destination_kind or "").strip().lower()
    ref = str(destination_ref).strip() if destination_ref is not None else None
    if kind not in STANDING_DESTINATIONS:
        raise ValueError(f"destination_kind must be one of {STANDING_DESTINATIONS}")
    if kind == "discarded" and ref is not None:
        raise ValueError("a discarded standing row cannot have a destination_ref")
    if kind != "discarded" and not ref:
        raise ValueError(f"{kind} retirement requires a destination_ref")

    known = standing_redirect(conn, key)
    if known is not None:
        if (known.destination_kind, known.destination_ref) != (kind, ref):
            raise ValueError(
                f"{key} already retires to {known.destination}; refusing {kind}:{ref}"
            )
        # A transaction interrupted after the redirect insert but before deletion can
        # only arise when a caller owns commit=False. Finish that same decision safely.
        conn.execute("DELETE FROM standing WHERE key = ?", (key,))
        if commit:
            conn.commit()
        return known, "unchanged"

    row = conn.execute("SELECT * FROM standing WHERE key = ?", (key,)).fetchone()
    if row is None:
        raise KeyError(f"no standing row {key!r}")
    conn.execute(
        """INSERT INTO standing_redirects(
               old_id, old_key, old_kind, old_value, old_scope, old_written_by,
               old_created_at, destination_kind, destination_ref, retired_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (row["id"], row["key"], row["kind"], row["value"], row["scope"],
         row["written_by"], row["created_at"], kind, ref, db.now()),
    )
    conn.execute("DELETE FROM standing WHERE id = ?", (row["id"],))
    if commit:
        conn.commit()
    redirect = standing_redirect(conn, key)
    if redirect is None:  # the insert above is the invariant; make corruption loud
        raise sqlite3.IntegrityError(f"standing redirect for {key!r} was not recorded")
    return redirect, "retired"
