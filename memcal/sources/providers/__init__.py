"""Provider-specific policy layered on generic transports."""

from __future__ import annotations

import sqlite3
from typing import Protocol, runtime_checkable

from . import partiful


@runtime_checkable
class Policy(Protocol):
    """What a provider has to answer for the transport to defer to it."""

    #: Stored in `calendar_items.provider`. Also the key `reconcile_missing` filters on,
    #: so it is durable: changing it orphans every row the provider has ever filed.
    name: str

    def claims(self, item: dict, cfg=None) -> bool:
        """Is this feed item ours? Read the item, never the store."""

    def fields(self, item: dict, common: dict) -> dict:
        """The transport's normalized fields, plus whatever the platform implies.

        Returns a new dict. Anything derived here is passed to `events.upsert` as
        `inferred`, so it may create a value and may never restate one over another
        writer's judgement.
        """

    def describe(self, item: dict, fields: dict) -> list[str]:
        """Extra clauses for the archived line — what a model reads about this row.

        Must describe only what the *feed* said. This is the line that once asserted
        "Partiful RSVP yes" over an invitation nobody had answered, so the falsehood was
        in the archive as well as on the row.
        """

    def reconcile_missing(self, conn: sqlite3.Connection, *, seen: set[str],
                          seen_uids: set[str] | None, scan_start: str, scan_end: str,
                          report) -> None:
        """What a disappearance from this feed means. Runs after a complete snapshot."""


#: Order matters only in that the first policy to claim an item wins. Keep the most
#: specific first; a policy that claims broadly belongs last.
REGISTRY: tuple[Policy, ...] = (partiful.POLICY,)


def claiming(item: dict, cfg=None) -> Policy | None:
    """Which policy owns this feed item, or None for a plain calendar row."""
    for policy in REGISTRY:
        if policy.claims(item, cfg):
            return policy
    return None


def by_name(name: str) -> Policy | None:
    """The policy a stored `calendar_items.provider` refers to, if it still exists.

    Returns None for a provider that has been removed from the registry rather than
    raising: its rows are still in the store and still have to be readable. A policy
    that is gone simply stops having opinions about them: a deleted component must not
    keep asserting itself, and must not take its records with it.
    """
    for policy in REGISTRY:
        if policy.name == name:
            return policy
    return None
