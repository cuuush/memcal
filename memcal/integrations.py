"""Native harness assets shipped with the same version as the Python package."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import sys

from . import __version__, config


def asset_path(harness: str) -> Path:
    if harness not in ("hermes", "openclaw"):
        raise ValueError(f"unknown harness: {harness}")
    return Path(__file__).resolve().parent / "integration_assets" / harness


def prepare(harness: str, home: Path) -> Path:
    """Refresh managed assets, never a host's configuration or unrelated files.

    The stable destination survives replacement of the Python installation.
    Rerun setup/refresh after upgrading; the descriptor records the Python runtime
    that owns MemCal rather than assuming the host's python3 has it installed.
    """
    source = asset_path(harness)
    destination = home / "integrations" / harness
    if harness == "hermes":
        source = source / "memcal"
        destination = destination / "memcal"
    if any(path.is_symlink() for path in (home / "integrations", home / "integrations" / harness, destination)):
        raise RuntimeError("managed integration destinations must not be symlinks")
    manifest_path = destination / "memcal-managed.json"
    if manifest_path.is_symlink():
        raise RuntimeError("managed integration manifest must not be a symlink")
    if destination.exists():
        if not manifest_path.is_file():
            raise RuntimeError(f"unmanaged integration directory: {destination}")
        manifest = json.loads(manifest_path.read_text())
        files = manifest.get("files") if isinstance(manifest, dict) else None
        if not isinstance(files, dict) or not all(
                isinstance(name, str) and Path(name).name == name
                and name not in (".", "..") and isinstance(digest, str)
                for name, digest in files.items()):
            raise RuntimeError(f"invalid managed integration manifest: {manifest_path}")
        for name, digest in manifest["files"].items():
            path = destination / name
            if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise RuntimeError(f"modified managed integration file: {path}; preserved")
        incoming = {item.name for item in source.iterdir() if item.is_file()
                    and item.suffix in (".py", ".ts", ".json")}
        incoming.add("memcal-runtime.json")
        for name in incoming - manifest["files"].keys():
            if (destination / name).exists() or (destination / name).is_symlink():
                raise RuntimeError(f"unmanaged integration file: {destination / name}; preserved")
    destination.mkdir(parents=True, exist_ok=True)
    managed = []
    for item in source.iterdir():
        if item.is_file() and item.suffix in (".py", ".ts", ".json"):
            shutil.copyfile(item, destination / item.name)
            managed.append(item.name)
    (destination / "memcal-runtime.json").write_text(json.dumps({
        "version": __version__, "source": str(config.PROJECT_ROOT),
        "python": sys.executable, "home": str(home),
    }, indent=2) + "\n")
    managed.append("memcal-runtime.json")
    manifest_path.write_text(json.dumps({"version": __version__, "files": {
        name: hashlib.sha256((destination / name).read_bytes()).hexdigest()
        for name in managed
    }}, indent=2) + "\n")
    return destination


def refresh_existing(cfg) -> list[str]:
    """Refresh only our staged copies. Raise before replacing a modified file."""
    refreshed = []
    for harness in ("hermes", "openclaw"):
        destination = cfg.home / "integrations" / harness
        if harness == "hermes":
            destination /= "memcal"
        if (destination / "memcal-managed.json").is_file():
            prepare(harness, cfg.home)
            refreshed.append(harness)
    return refreshed
