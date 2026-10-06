"""Dispatch updates before importing application dependencies."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    offset = 0
    if raw[:1] == ["--home"]:
        offset = 2
    elif raw and raw[0].startswith("--home="):
        offset = 1
    if raw[offset:offset + 1] == ["update"]:
        from .update import main as update_main
        return update_main(raw)
    from .cli import main as cli_main
    return cli_main(argv)
