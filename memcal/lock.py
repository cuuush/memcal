"""One collector per store.

Manual (`memcal ingest`), web (Gather), and scheduled collection all mutate the
same per-source cursors and outcome rows. An in-process mutex cannot see the
other two, and a PID file cannot tell a dead owner from a slow one, so this is
an advisory file lock on `flock`: the kernel owns it, a second process gets a
clean refusal instead of a guess, and process exit releases it with the file
descriptor — no cleanup pass, no timeout that can delete a live owner's lock.

The lock covers one collection only (cursors plus outcome rows). It is never
held across a model call: dream runs as a separate step after collection
releases it.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from .config import Config

#: Inside the store so separate stores never block one another. Never deleted:
#: removing the file while another process holds it would orphan their lock.
LOCK_NAME = "collect.lock"


class Busy(RuntimeError):
    """Another process holds this store's collection lock right now."""


def lock_path(cfg: Config) -> Path:
    return cfg.home / LOCK_NAME


def _lock(fd: int) -> None:
    """Exclusive, non-blocking. Raises `Busy` instead of waiting."""
    try:
        import fcntl
    except ImportError:
        fcntl = None  # type: ignore
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise Busy("another collection is already running on this store") from exc
        return
    try:
        import msvcrt
    except ImportError:
        msvcrt = None  # type: ignore
    if msvcrt is not None:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise Busy("another collection is already running on this store") from exc
        return
    raise Busy("no file-lock primitive on this platform; refusing rather than racing")


def _unlock(fd: int) -> None:
    try:
        import fcntl
    except ImportError:
        fcntl = None  # type: ignore
    if fcntl is not None:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        return
    try:
        import msvcrt
    except ImportError:
        msvcrt = None  # type: ignore
    if msvcrt is not None:
        with contextlib.suppress(OSError):
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


@contextlib.contextmanager
def held(cfg: Config):
    """Hold this store's collection lock, or raise `Busy`.

    The descriptor stays open for the whole `with` block; closing it releases
    the lock even if the process dies first.
    """
    cfg.home.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path(cfg)), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _lock(fd)
    except BaseException:
        os.close(fd)
        raise
    try:
        yield
    finally:
        try:
            _unlock(fd)
        finally:
            os.close(fd)
