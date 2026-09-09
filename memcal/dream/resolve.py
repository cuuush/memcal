"""Compatibility imports for the former name of the Merge stage."""

from .merge import Mention, SCHEMA, cluster, explain, merge_all, resolve_all, same_event

__all__ = [
    "Mention", "SCHEMA", "cluster", "explain", "merge_all", "resolve_all", "same_event",
]
