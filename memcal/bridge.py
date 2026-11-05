"""A private local request/reply bridge to the Full Disk Access Memcal app.

launchd starts a fresh worker under the existing app identity. Requests select
fixed operations, never arbitrary commands, SQL, or source paths. Ingestion stays
inside that app and returns only its report to the caller.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import uuid
from pathlib import Path

from . import db, schedule

WORKER_ENV = "MEMCAL_BRIDGE_WORKER"
SOURCES = ("imessage", "whatsapp")
OPERATIONS = ("check", "ingest", "collect", "contacts")


class BridgeError(RuntimeError):
    pass


def available(cfg) -> bool:
    return (schedule._is_macos() and os.environ.get(WORKER_ENV) != "1"
            and os.environ.get("MEMCAL_HELPER_CONTEXT") != "1"
            and schedule.app_executable(cfg).is_file()
            and os.access(schedule.app_executable(cfg), os.X_OK))


def _idle(conn, cfg) -> None:
    if conn is not None and conn.in_transaction:
        raise BridgeError("Finish the current store transaction before calling the app bridge")
    if conn is not None:
        path = conn.execute("PRAGMA database_list").fetchone()[2]
        if not path or Path(path).resolve() != cfg.db_path.resolve():
            raise BridgeError("App bridge and caller must use the same Memcal store")


def call(cfg, operation: str, *, source=None, limit=1000, collection_id=None,
         force=False, conn=None, rounds=1) -> dict:
    _idle(conn, cfg)
    request = {"id": uuid.uuid4().hex, "operation": operation, "source": source,
               "limit": limit, "collection_id": collection_id, "force": force, "rounds": rounds}
    _validate(request)
    if not available(cfg):
        raise BridgeError("Memcal app bridge is unavailable; run memcal permissions setup")
    cfg.ensure_dirs()
    with tempfile.TemporaryDirectory(prefix="bridge-", dir=cfg.home) as temporary:
        directory = Path(temporary)
        directory.chmod(0o700)
        path = directory / "request.json"
        with path.open("x", encoding="utf-8") as file:
            path.chmod(0o600)
            json.dump(request, file)
        payload, identity = schedule.launchd_memcal_call(
            cfg, label="com.memcal.bridge", stem="bridge",
            memcal_argv=["bridge-worker", "--request", str(path)],
            timeout=180 if operation in {"ingest", "collect", "contacts"} else 15,
            work_dir=directory)
    if not isinstance(payload, dict) or payload.get("id") != request["id"]:
        raise BridgeError(f"Memcal app bridge did not return a valid result ({identity}); retry the command")
    if payload.get("error"):
        raise BridgeError(str(payload["error"]))
    result = payload.get("result")
    if not isinstance(result, dict):
        raise BridgeError("Memcal app bridge returned an invalid result")
    return result


def check(cfg, source) -> tuple[bool, str]:
    try:
        result = call(cfg, "check", source=source)
        if type(result.get("ok")) is not bool:
            raise BridgeError("Memcal app bridge returned an invalid access check")
        return result["ok"], str(result.get("detail") or "no detail") + " (via memcal.app bridge)"
    except BridgeError as exc:
        return False, str(exc)


def _report(result):
    from .sources.base import IngestReport
    fields = {key: value for key, value in result.items()
              if key in IngestReport.__dataclass_fields__ and key != "progress"}
    fields["unknown_handles"] = set(fields.get("unknown_handles") or [])
    report = IngestReport(**fields)
    report.notes.append("read protected source through memcal.app bridge")
    return report


def ingest(conn, cfg, report, limit) -> None:
    result = call(cfg, "ingest", source=report.channel, limit=limit,
                  collection_id=report.collection_id, conn=conn)
    report.absorb(_report(result))


def collect(conn, cfg, source, *, limit, rounds, collection_id):
    from .sources.base import IngestReport
    from . import archive
    try:
        result = call(cfg, "collect", source=source, limit=limit, rounds=max(1, rounds),
                      collection_id=collection_id, conn=conn)
        report = _report(result)
    except BridgeError as exc:
        report = IngestReport(channel=source, error=str(exc), collection_id=collection_id)
    archive.record_source(conn, collection_id, report)
    conn.commit()
    return report


def _validate(request) -> None:
    if not isinstance(request, dict) or set(request) != {
            "id", "operation", "source", "limit", "collection_id", "force", "rounds"}:
        raise BridgeError("Invalid bridge request")
    if not isinstance(request["id"], str) or len(request["id"]) != 32:
        raise BridgeError("Invalid bridge request ID")
    if request["operation"] not in OPERATIONS:
        raise BridgeError("Unsupported app bridge operation")
    if request["operation"] != "contacts" and request["source"] not in SOURCES:
        raise BridgeError("Unsupported app bridge source")
    if type(request["limit"]) is not int or not 1 <= request["limit"] <= 100_000:
        raise BridgeError("Bridge limit must be between 1 and 100000")
    if request["collection_id"] is not None and (
            type(request["collection_id"]) is not int or request["collection_id"] < 1):
        raise BridgeError("Invalid bridge collection")
    if type(request["rounds"]) is not int or not 1 <= request["rounds"] <= 1000:
        raise BridgeError("Bridge rounds must be between 1 and 1000")
    if type(request["force"]) is not bool:
        raise BridgeError("Invalid bridge force flag")


def _request_file(cfg, path: Path, result: Path) -> dict:
    directory = path.parent
    if (directory.parent.resolve() != cfg.home.resolve() or not directory.name.startswith("bridge-")
            or directory.is_symlink() or path.is_symlink() or result.is_symlink()
            or result.parent != directory):
        raise BridgeError("Bridge files must be in a private store request directory")
    for candidate in (directory, path):
        info = candidate.stat()
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise BridgeError("Bridge files must be owned by this user and private")
    if not path.is_file() or path.stat().st_size > 4096:
        raise BridgeError("Invalid bridge request file")
    request = json.loads(path.read_text(encoding="utf-8"))
    _validate(request)
    return request


def worker(cfg, request_path, result_path) -> int:
    """Execute a bounded operation under the already-authorized app identity."""
    if os.environ.get("MEMCAL_APP") != "1":
        raise BridgeError("The bridge worker must run under memcal.app")
    path, result = Path(request_path), Path(result_path)
    request = _request_file(cfg, path, result)
    previous_worker = os.environ.get(WORKER_ENV)
    os.environ[WORKER_ENV] = "1"
    response = {"id": request["id"]}
    try:
        from .sources import imessage, whatsapp
        if request["operation"] == "check":
            source = imessage.IMessageSource() if request["source"] == "imessage" else whatsapp.WhatsAppSource()
            ok, detail = source.check(cfg)
            response["result"] = {"ok": ok, "detail": detail}
        else:
            from . import identity
            if not cfg.db_path.is_file():
                raise BridgeError("Initialize the Memcal store before ingesting through the app bridge")
            conn = db.connect(cfg.db_path)
            try:
                if request["operation"] == "contacts":
                    count, detail = identity.refresh_contacts(conn, force=request["force"])
                    response["result"] = {"count": count, "detail": detail}
                else:
                    from contextlib import nullcontext
                    from . import sources
                    notes = []
                    session = (whatsapp.desktop_session(cfg, report=notes.append)
                               if request["source"] == "whatsapp" else nullcontext())
                    with session:
                        if request["operation"] == "collect":
                            source = imessage.IMessageSource() if request["source"] == "imessage" else whatsapp.WhatsAppSource()
                            report = sources.catch_up(source, conn, cfg, limit=request["limit"],
                                                      rounds=request["rounds"], collection_id=request["collection_id"])
                        elif request["source"] == "imessage":
                            report = imessage.ingest(conn, limit=request["limit"],
                                                     collection_id=request["collection_id"])
                        else:
                            report = whatsapp.ingest(conn, cfg, limit=request["limit"],
                                                     collection_id=request["collection_id"])
                    report.notes.extend(notes)
                    response["result"] = {key: value for key, value in vars(report).items()
                                          if key != "progress"}
                    response["result"]["unknown_handles"] = sorted(report.unknown_handles)
                conn.commit()
            finally:
                conn.close()
    except Exception as exc:
        response["error"] = f"{type(exc).__name__}: {exc}"
    if previous_worker is None:
        os.environ.pop(WORKER_ENV, None)
    else:
        os.environ[WORKER_ENV] = previous_worker
    temporary = result.with_suffix(".tmp")
    with temporary.open("x", encoding="utf-8") as file:
        temporary.chmod(0o600)
        json.dump(response, file)
    temporary.replace(result)
    return 1 if response.get("error") else 0
