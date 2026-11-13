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
OPERATIONS = ("check", "ingest", "collect", "contacts", "calendar")


def in_helper() -> bool:
    # MEMCAL_APP alone can also mean a bundle started by a terminal. Only a
    # launchd-owned worker has the independent responsible-process identity.
    return (os.environ.get(WORKER_ENV) == "1"
            or os.environ.get("MEMCAL_HELPER_CONTEXT") == "1")


class BridgeError(RuntimeError):
    pass


def available(cfg) -> bool:
    return (schedule._is_macos() and not in_helper()
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
         force=False, conn=None, rounds=1, calendar=None, progress=None, background=False) -> dict:
    _idle(conn, cfg)
    request = {"id": uuid.uuid4().hex, "operation": operation, "source": source,
               "limit": limit, "collection_id": collection_id, "force": force,
               "rounds": rounds, "background": background}
    if calendar is not None:
        request["calendar"] = calendar
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
        extra = {}
        if progress is not None:
            last = None

            def poll():
                nonlocal last
                try:
                    value = (directory / "progress.json").read_text(encoding="utf-8")
                    if value != last:
                        event = json.loads(value)
                        progress(event.pop("note"), **event)
                        last = value
                except (OSError, ValueError, KeyError):
                    pass

            extra["on_poll"] = poll
        timeout = (calendar_timeout(calendar["kind"], calendar["arguments"]) + 20
                   if operation == "calendar" else
                   180 if operation in {"ingest", "collect", "contacts"} else 15)
        payload, identity = schedule.launchd_memcal_call(
            cfg, label="com.memcal.bridge", stem="bridge",
            memcal_argv=["bridge-worker", "--request", str(path)],
            timeout=timeout, work_dir=directory, **extra)
    if not isinstance(payload, dict) or payload.get("id") != request["id"]:
        raise BridgeError(f"Memcal app bridge did not return a valid result ({identity}); retry the command")
    if payload.get("error"):
        raise BridgeError(str(payload["error"]))
    result = payload.get("result")
    if not isinstance(result, dict):
        raise BridgeError("Memcal app bridge returned an invalid result")
    return result


def calendar_timeout(kind, arguments) -> int:
    if kind == "snapshot":
        return 180
    if kind == "probe":
        return 30
    if kind == "account" and arguments[0] == "migrate":
        return 300
    if kind == "account" and arguments[0] in {"request", "where"}:
        return 60
    if kind == "reminder" and arguments[0] == "request":
        return 90
    return 120 if kind in {"account", "reminder"} else 60


def _validate_calendar(value) -> None:
    if not isinstance(value, dict) or set(value) != {"kind", "arguments"}:
        raise BridgeError("Invalid Calendar bridge request")
    kind, args = value["kind"], value["arguments"]
    if not isinstance(kind, str) or not isinstance(args, list) or any(
            not isinstance(arg, str) or len(arg) > 262144 for arg in args):
        raise BridgeError("Invalid Calendar bridge arguments")
    if kind == "probe" and args == []:
        return
    if kind == "snapshot" and len(args) == 2:
        from datetime import date
        try:
            if date.fromisoformat(args[0]) < date.fromisoformat(args[1]):
                return
        except ValueError:
            pass
    if kind in {"publish", "retract"} and len(args) == 2 and args[0]:
        if kind == "retract" and args[1]:
            return
        if kind == "publish":
            try:
                if isinstance(json.loads(args[1]), dict):
                    return
            except ValueError:
                pass
    verbs = ({"request": 0, "where": 1, "ensure": 1, "migrate": 2,
              "repeat": 2, "unrepeat": 2, "exception": 2} if kind == "account" else
             {"request": 1, "status": 1, "put": 2, "drop": 2} if kind == "reminder" else {})
    if args and args[0] in verbs and len(args) == verbs[args[0]] + 1:
        if args[0] == "migrate" and args[-1] not in {"plan", "apply"}:
            raise BridgeError("Invalid Calendar migration mode")
        return
    raise BridgeError("Unsupported Calendar bridge operation or arguments")


def calendar_call(cfg, kind, arguments, *, progress=None):
    try:
        return call(cfg, "calendar", calendar={"kind": kind, "arguments": list(arguments)},
                    progress=progress)
    except BridgeError as exc:
        # Existing Calendar transports already translate OSError to their domain
        # errors. A bridge failure must never fall through to the host's osascript.
        raise OSError(str(exc)) from exc


def calendar_runner(cfg, runner):
    """Preserve injected transports; route real Apple Events through launchd."""
    import subprocess
    from .sources.ical import _REAL_TRANSPORT
    if runner is not _REAL_TRANSPORT[0] or not schedule._is_macos() or in_helper():
        return runner
    if cfg is None:
        from .config import load
        cfg = load()

    def run(argv, **_kwargs):
        from .sources import ical
        scripts = {ical.PROBE_JXA: "probe", ical.ACCOUNT_JXA: "account",
                   ical.REMINDERS_JXA: "reminder", ical.PUBLISH_JXA: "publish",
                   ical.RETRACT_JXA: "retract"}
        if (len(argv) < 5 or argv[:4] != ["osascript", "-l", "JavaScript", "-e"]
                or argv[4] not in scripts):
            raise OSError("Unsupported Calendar bridge script")
        result = calendar_call(cfg, scripts[argv[4]], argv[5:])
        return subprocess.CompletedProcess(argv[:4], result["returncode"],
                                           result["stdout"], result["stderr"])
    return run


def _calendar_worker(request, directory):
    import subprocess
    from .sources import ical
    kind, args = request["kind"], request["arguments"]
    if kind == "snapshot":
        def progress(note, **fields):
            temporary = directory / "progress-next.json"
            with temporary.open("w", encoding="utf-8") as file:
                temporary.chmod(0o600)
                json.dump({"note": note, **fields}, file)
            temporary.replace(directory / "progress.json")
        snapshot = ical._calendar_snapshot(*args, progress=progress)
        return {"items": snapshot.items, "unreadable": list(snapshot.unreadable)}
    script = {"probe": ical.PROBE_JXA, "account": ical.ACCOUNT_JXA,
              "reminder": ical.REMINDERS_JXA, "publish": ical.PUBLISH_JXA,
              "retract": ical.RETRACT_JXA}[kind]
    done = subprocess.run(["osascript", "-l", "JavaScript", "-e", script, *args],
                          capture_output=True, text=True, timeout=calendar_timeout(kind, args))
    return {"returncode": done.returncode, "stdout": done.stdout, "stderr": done.stderr}


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


def collect(conn, cfg, source, *, limit, rounds, collection_id, background=False):
    from .sources.base import IngestReport
    from . import archive
    try:
        result = call(cfg, "collect", source=source, limit=limit, rounds=max(1, rounds),
                      collection_id=collection_id, conn=conn, background=background)
        report = _report(result)
    except BridgeError as exc:
        report = IngestReport(channel=source, error=str(exc), collection_id=collection_id)
    archive.record_source(conn, collection_id, report)
    conn.commit()
    return report


def _validate(request) -> None:
    fields = {"id", "operation", "source", "limit", "collection_id", "force", "rounds", "background"}
    if isinstance(request, dict) and request.get("operation") == "calendar":
        fields.add("calendar")
    if not isinstance(request, dict) or set(request) != fields:
        raise BridgeError("Invalid bridge request")
    if not isinstance(request["id"], str) or len(request["id"]) != 32:
        raise BridgeError("Invalid bridge request ID")
    if request["operation"] not in OPERATIONS:
        raise BridgeError("Unsupported app bridge operation")
    if request["operation"] not in {"contacts", "calendar"} and request["source"] not in SOURCES:
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
    if type(request["background"]) is not bool:
        raise BridgeError("Invalid bridge background flag")
    if request["operation"] == "calendar":
        _validate_calendar(request["calendar"])


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
    if not path.is_file() or path.stat().st_size > 1048576:
        raise BridgeError("Invalid bridge request file")
    request = json.loads(path.read_text(encoding="utf-8"))
    _validate(request)
    if request["operation"] != "calendar" and path.stat().st_size > 4096:
        raise BridgeError("Invalid bridge request file")
    return request


def worker(cfg, request_path, result_path) -> int:
    """Execute a bounded operation under the already-authorized app identity."""
    if (os.environ.get("MEMCAL_APP") != "1"
            or os.environ.get("MEMCAL_HELPER_CONTEXT") != "1"):
        raise BridgeError("The bridge worker must run under launchd's memcal.app")
    path, result = Path(request_path), Path(result_path)
    request = _request_file(cfg, path, result)
    previous_worker = os.environ.get(WORKER_ENV)
    os.environ[WORKER_ENV] = "1"
    response = {"id": request["id"]}
    try:
        from .sources import imessage, whatsapp
        if request["operation"] == "calendar":
            response["result"] = _calendar_worker(request["calendar"], path.parent)
        elif request["operation"] == "check":
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
                    session = (whatsapp.desktop_session(cfg, report=notes.append,
                                                       background=request["background"])
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
