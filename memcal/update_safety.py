"""Non-destructive update snapshots and store maintenance coordination.

Recovery writes to a new directory, never over the live store. Backups contain
private data and must be treated like the original store.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import uuid

LOCK = 'maintenance.lock'
BYPASS = 'MEMCAL_UPDATE_MAINTENANCE_HOME'


class MaintenanceBusy(RuntimeError):
    pass


def acquire(home: Path, *, exclusive: bool = False):
    import fcntl
    home = home.resolve()
    home.mkdir(parents=True, exist_ok=True)
    fd = os.open(home / LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise MaintenanceBusy('store is in use or being updated; stop MemCal UI, agent and background writers, then retry') from exc
    return fd


def release(fd):
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.contextmanager
def maintenance(home: Path):
    from . import config, lock
    cfg = config.load(home)
    with lock.held(cfg), lock.held(cfg, name='dream.lock'):
        fd = acquire(home, exclusive=True)
        try:
            yield
        finally:
            release(fd)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def snapshot(home: Path, *, version: str, installation: dict) -> Path:
    home = home.resolve()
    parent = home / 'backups'
    if parent.is_symlink():
        raise ValueError('backup directory must not be a symlink')
    parent.mkdir(mode=0o700, exist_ok=True)
    parent.chmod(0o700)
    target = parent / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8])
    target.mkdir(mode=0o700)
    data = target / 'store'
    data.mkdir(mode=0o700)
    try:
        # The maintenance and collector/Dream locks keep cooperating writers out.
        # SQLite's backup API includes committed WAL data without copying live WAL.
        for item in home.iterdir():
            if item.name == 'backups' or item.name.endswith(('.lock', '-wal', '-shm')):
                continue
            if item.is_symlink():
                raise ValueError(f'refusing to snapshot symlink: {item.name}')
            dest = data / item.name
            if item.is_dir():
                for child in item.rglob('*'):
                    if child.is_symlink():
                        raise ValueError(f'refusing to snapshot symlink: {child.relative_to(home)}')
                shutil.copytree(item, dest)
            elif item.suffix == '.db':
                source = sqlite3.connect(item.as_uri() + '?mode=ro', uri=True)
                output = sqlite3.connect(dest)
                try:
                    source.backup(output)
                    if output.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise ValueError(f'invalid SQLite backup: {item.name}')
                finally:
                    output.close()
                    source.close()
            else:
                shutil.copy2(item, dest)
        for path in data.rglob('*'):
            path.chmod(0o700 if path.is_dir() else 0o600)
        manifest = {'format': 1, 'version': version, 'home': str(home),
                    'installation': installation, 'status': 'backup-complete',
                    'files': {str(p.relative_to(data)): _digest(p)
                              for p in data.rglob('*') if p.is_file()}}
        (target / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
        (target / 'manifest.json').chmod(0o600)
        return target
    except BaseException:
        # Do not erase partial evidence; clearly mark it unusable.
        (target / 'INCOMPLETE').touch(mode=0o600)
        raise


def restore(backup: Path, destination: Path) -> dict:
    backup = backup.resolve()
    destination = destination.expanduser().absolute()
    if destination.exists():
        raise ValueError('recovery destination must not exist; the live store is never overwritten')
    if (backup / 'INCOMPLETE').exists():
        raise ValueError('snapshot is incomplete')
    manifest = json.loads((backup / 'manifest.json').read_text())
    if manifest.get('format') != 1 or manifest.get('status') != 'backup-complete':
        raise ValueError('unsupported or incomplete snapshot')
    source = backup / 'store'
    if not source.is_dir() or source.is_symlink():
        raise ValueError('snapshot store is missing or unsafe')
    if any(p.is_symlink() for p in source.rglob('*')):
        raise ValueError('snapshot contains symlinks')
    files = {str(p.relative_to(source)): p for p in source.rglob('*') if p.is_file()}
    expected = manifest.get('files')
    if not isinstance(expected, dict) or set(files) != set(expected):
        raise ValueError('snapshot file inventory does not match manifest')
    for name, path in files.items():
        if _digest(path) != expected[name]:
            raise ValueError(f'snapshot checksum mismatch: {name}')
    shutil.copytree(source, destination)
    destination.chmod(0o700)
    for path in destination.rglob('*'):
        path.chmod(0o700 if path.is_dir() else 0o600)
    return manifest
