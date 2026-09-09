"""Background collection and dream jobs for the local UI."""

from __future__ import annotations

import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import archive, db, identity, threads
from .config import Config
from . import web_memory

class _Job:
    """One long-running thing, and enough about it to draw honestly while it runs."""

    def __init__(self, kind: str):
        self.kind = kind
        self.lines: list[str] = []
        self.done = False
        self.error: str | None = None
        self.result: dict = {}
        # Collecting from five sources takes minutes and a spinner says nothing about
        # which one it is stuck on. `steps` is the whole plan up front, so the bar is
        # honest from the first frame instead of guessing at a denominator.
        self.steps: list[dict] = []
        self.version = 0
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)

    def _bump(self) -> None:
        """Called holding the lock. Wakes every streaming reader."""
        self.version += 1
        self.changed.notify_all()

    def say(self, line: str) -> None:
        with self.lock:
            self.lines.append(line)
            self._bump()

    def plan(self, names: list[str]) -> None:
        with self.lock:
            self.steps = [_blank_step(name) for name in names]
            self._bump()

    def step(self, name: str, state: str = "", note: str = "", *,
             done: int | None = None, total: int | None = None,
             phase: str = "") -> None:
        """Update one step: its state, its counted progress, and what it is doing.

        Every field is optional and only what is passed is changed, because the callers
        are interleaved — a source reports `done`/`total` from inside its fetch loop
        while the runner reports `state` around it, and either clobbering the other is
        how a finished source goes back to reading.
        """
        with self.lock:
            for entry in self.steps:
                if entry["name"] == name:
                    break
            else:
                entry = _blank_step(name)
                self.steps.append(entry)
            if state:
                if state == "running" and entry["state"] != "running":
                    entry["since"] = time.monotonic()
                entry["state"] = state
            if note:
                entry["note"] = note
            if phase:
                entry["phase"] = phase
            if total is not None:
                entry["total"] = int(total)
            if done is not None:
                entry["done"] = int(done)
            if entry["state"] in ("done", "skipped", "failed"):
                # A finished step is a full bar. Its last counted position is usually
                # short of its own total — the total was what it *found*, and the tail
                # of it was deduplicated or gated away — and a bar stuck at 96% next to
                # the word "done" reads as something having gone wrong.
                entry["phase"] = entry["phase"] if entry["state"] != "done" else "done"
                entry["done"] = entry["total"] = max(1, entry["total"] or 1)
            self._bump()

    def snapshot(self) -> dict:
        with self.lock:
            return self._snapshot()

    def _snapshot(self) -> dict:
        """Called holding the lock."""
        finished = sum(1 for s in self.steps
                       if s["state"] in ("done", "skipped", "failed"))
        now = time.monotonic()
        steps = []
        for entry in self.steps:
            step = dict(entry)
            # How long this step has been running, which is a fact, and the only thing
            # there is to say about a step that cannot count itself — one model call has
            # no halfway. The page draws it; see `webui.html`, `.pbar.elapsed`.
            step["running_for"] = (round(now - step.pop("since"), 1)
                                   if step["state"] == "running" else 0.0)
            steps.append(step)
        return {"kind": self.kind, "lines": list(self.lines),
                "done": self.done, "error": self.error, "result": self.result,
                "steps": steps,
                "finished": finished, "total": len(self.steps),
                "version": self.version}

    def wait_for_change(self, since: int, timeout: float = 25.0) -> dict:
        """Block until this job moves, then return it. For the streaming endpoint.

        The timeout is not a poll: it exists so a connection through a proxy that would
        drop a silent socket gets a frame anyway, and so a finished job cannot hold a
        thread forever.
        """
        with self.lock:
            if self.version <= since and not self.done:
                self.changed.wait(timeout)
            return self._snapshot()


def _blank_step(name: str) -> dict:
    # `total` 0 means "no denominator yet", which the page draws as an indeterminate
    # bar rather than as 0%. Inventing a denominator to have one is how a progress bar
    # starts lying — half the sources genuinely cannot know their total until they have
    # asked, and a bar that crawls to 90% and waits is worse than one that admits it.
    return {"name": name, "state": "waiting", "note": "", "phase": "",
            "done": 0, "total": 0, "since": 0.0}


def _reporter(job: _Job, name: str):
    """The progress callback handed to one source.

    Kept compatible with the plain `progress("some text")` a third-party plugin was
    written against — `done`, `total` and `phase` are keyword-only and every one of them
    is optional. A source that says nothing but a sentence still drives the note; one
    that counts drives the bar as well.
    """
    def report(note: str = "", *, done: int | None = None, total: int | None = None,
               phase: str = "") -> None:
        job.step(name, note=note, done=done, total=total,
                 phase=phase or _phase_from(note))
    return report


#: When a source reports prose rather than a phase, name what it is doing from the
#: prose. Two words at most: this is rendered in a fixed column beside the bar, and it
#: is meant to be *glanceable* — the sentence is already in the note underneath.
_PHASES = (
    ("up to date", "up to date"), ("checking", "checking"), ("waiting", "waiting"),
    ("calendar", "reading"), ("member", "resolving"), ("roster", "resolving"),
    ("resolv", "resolving"), ("chat", "reading chats"), ("group", "reading groups"),
    ("dm", "reading DMs"), ("messag", "reading"), ("read", "reading"),
    ("fetch", "fetching"), ("gat", "gating"),
)


def _phase_from(note: str) -> str:
    text = (note or "").lower()
    for needle, phase in _PHASES:
        if needle in text:
            return phase
    return ""


_JOBS: dict[str, _Job] = {}
_JOB_LOCK = threading.Lock()
_NEXT_JOB_ID = 1
COMPLETED_JOB_RETENTION = 20


def _trim_completed_jobs() -> None:
    """Keep the newest completed snapshots. Called with ``_JOB_LOCK`` held."""
    completed = [job_id for job_id, job in _JOBS.items() if job.done]
    for job_id in completed[:-COMPLETED_JOB_RETENTION]:
        del _JOBS[job_id]


def start_job(kind: str, work, cfg: Config) -> dict:
    global _NEXT_JOB_ID
    with _JOB_LOCK:
        if any(not j.done for j in _JOBS.values()):
            return {"error": "something is already running — wait for it to finish"}
        job = _Job(kind)
        job_id = f"{kind}-{_NEXT_JOB_ID}"
        _NEXT_JOB_ID += 1
        _JOBS[job_id] = job

    def run() -> None:
        conn = db.open_db(cfg.db_path)
        try:
            job.result = work(conn, cfg, job) or {}
        except Exception as exc:
            job.error = f"{type(exc).__name__}: {exc}"
            job.say(f"failed: {job.error}")
        finally:
            conn.close()
            # Under the lock, and bumped — like every other mutator on `_Job`. Set bare,
            # it changed the flag without waking anybody, so `wait_for_change` returned
            # only on its 25-second timeout and the button stayed disabled and the bar
            # stayed mid-flight for 25s after the work had finished. The error path was
            # worse: `job.error` is set silently, so a failed job also looked busy.
            with job.lock:
                job.done = True
                job._bump()
            with _JOB_LOCK:
                _trim_completed_jobs()

    threading.Thread(target=run, daemon=True).start()
    return {"job": job_id}


def find_job(job_id: str = "", kind: str = "") -> tuple[str, "_Job | None"]:
    with _JOB_LOCK:
        if job_id:
            return job_id, _JOBS.get(job_id)
        found = [(candidate_id, candidate)
                 for candidate_id, candidate in reversed(list(_JOBS.items()))
                 if not candidate.done and (not kind or candidate.kind == kind)]
    return found[0] if found else ("", None)


def job_status(job_id: str = "", kind: str = "") -> dict:
    """Return one job, or the active job of a kind so a reloaded page can rejoin it."""
    with _JOB_LOCK:
        if job_id:
            job = _JOBS.get(job_id)
        else:
            found = [
                (candidate_id, candidate)
                for candidate_id, candidate in reversed(list(_JOBS.items()))
                if not candidate.done and (not kind or candidate.kind == kind)
            ]
            job_id, job = found[0] if found else ("", None)
    if not job:
        return {"job": None} if not job_id else {"error": "no such job"}
    return {"job": job_id, **job.snapshot()}


#: How many sources read at once. They are almost all waiting on somebody else's
#: server, so this is bounded by politeness rather than by CPU: GroupMe answers a fast
#: loop with 429s and each source is already internally parallel.
COLLECT_WORKERS = 4


def collect_work(conn: sqlite3.Connection, cfg: Config, job: _Job) -> dict:
    """Every source that can run, run to exhaustion — the spool for the next dream."""
    from . import sources

    wanted = [s for s in sources.all_sources(cfg) if s.in_all]
    job.plan(["contacts"] + [s.name for s in wanted] + ["chats"])
    # The job object dies with the process; this outlives it, and is what the queue view
    # groups by so "what will the next dream skip?" has something to group *on*.
    collection_id = archive.open_collection(conn, mode="web")

    # Contacts first and alone: every source resolves handles through the table this
    # writes, and a source that starts before it is finished files known people as
    # strangers.
    job.step("contacts", "running", phase="linking")
    linked, message = identity.refresh_contacts(conn)
    if linked:
        job.say(f"contacts: {message}")
    job.step("contacts", "done", f"{linked} linked" if linked else "fresh")

    def collect_one(source) -> dict:
        ok, reason = source.check(cfg)
        if not ok:
            job.say(f"skip {source.name}: {reason}")
            job.step(source.name, "skipped", reason, phase="skipped")
            return {"stream": source.name, "skipped": reason}
        job.step(source.name, "running", phase="starting")
        job.say(f"{source.name}: reading…")
        own = db.open_db(cfg.db_path)
        try:
            report = sources.catch_up(
                source, own, cfg, collection_id=collection_id,
                progress=_reporter(job, source.name),
            )
        finally:
            own.close()
        job.say(report.summary())
        for note in report.notes:
            job.say(f"  {source.name}: {note}")
        job.step(source.name, "failed" if report.error else "done",
                 report.error or f"{report.archived} new, {report.passed} queued")
        return {"stream": source.name, "read": report.read,
                "archived": report.archived, "passed": report.passed,
                "muted": report.muted, "error": report.error}

    reports = []
    if wanted:
        with ThreadPoolExecutor(max_workers=min(COLLECT_WORKERS, len(wanted))) as pool:
            futures = [(source, pool.submit(collect_one, source)) for source in wanted]
            for source, future in futures:
                try:
                    reports.append(future.result())
                except Exception as exc:              # one bad plugin, not a dead run
                    message = f"{type(exc).__name__}: {exc}"
                    job.step(source.name, "failed", message)
                    job.say(f"{source.name}: {message}")
                    reports.append({"stream": source.name, "error": message})

    archive.close_collection(conn, collection_id)

    # Conversations last: their shape is derived from everything just archived, and the
    # ask-me queue is computed from it.
    job.step("chats", "running", phase="grouping")
    seen = threads.refresh(conn)
    asks = len(threads.review(conn))
    job.step("chats", "done", f"{seen} chats, {asks} to review" if asks else f"{seen} chats")
    if asks:
        job.say(f"{asks} group chat(s) worth a decision — see the Chats tab")

    pending = conn.execute(
        "SELECT count(*) n FROM spool WHERE processed_at IS NULL").fetchone()["n"]
    job.say(f"done — {pending} items waiting for the next dream")
    return {"sources": reports, "pending": pending, "review": asks}


def _read_so_far(data: dict) -> str:
    """`propose`'s note. The same two numbers the bar is drawn from, in words."""
    return f"{data.get('done', 0)}/{data.get('total', 0)} bundles read"


def dream_work(conn: sqlite3.Connection, cfg: Config, job: _Job) -> dict:
    """The real pass. Whatever it writes is what the Memory tab will show next."""
    from .dream.run import dream as run_dream

    job.plan(["prepare", "propose", "merge", "apply", "sweep", "render"])
    request_count = 0

    def progress(event: str, data: dict) -> None:
        nonlocal request_count
        if event == "stage":
            stage = data.get("stage", "")
            state = data.get("state", "")
            note = data.get("note", "")
            job.step(stage, state, note)
            if state in ("running", "failed"):
                job.say(f"{stage}: {note}")
            return
        if event == "propose_wave":
            kind = data.get("kind", "main")
            opening = "propose" if kind == "main" else f"propose ({kind})"
            job.say(f"{opening}: {data.get('requests', 0)} request(s) in flight"
                    f" · {data.get('bundles', 0)} bundle(s)")
            # A re-send is work the plan did not have in it, so the denominator grew and
            # the lane should say so now rather than at the next reply, minutes later.
            job.step("propose", "running", _read_so_far(data),
                     done=data.get("done"), total=data.get("total"))
            return
        if event == "propose_request":
            request_count += 1
            state = "done" if data.get("ok") else "failed"
            # Bundles read, not requests finished: the request count has no denominator
            # — packing and re-sends both change it mid-pass — and a bar needs one.
            job.step("propose", "running",
                     f"{_read_so_far(data)} · {request_count} request(s)",
                     done=data.get("done"), total=data.get("total"))
            label = data.get("label") or "request"
            tail = f" — {data.get('error')}" if data.get("error") else ""
            job.say(f"  request {request_count} {state}: {label}{tail}")

    job.say(f"dreaming with {cfg.propose_model}…")
    result = run_dream(conn, cfg, mode="web", progress=progress)
    for line in result.report().splitlines():
        job.say(line)
    writes = []
    for row in conn.execute(
        "SELECT kind, ref, verb FROM provenance WHERE run_id = ? ORDER BY id",
        (result.run_id,),
    ):
        writes.append({
            "kind": row["kind"], "ref": row["ref"], "verb": row["verb"] or "",
            "label": web_memory._needle(conn, row["kind"], row["ref"]) or row["ref"],
        })
    return {
        "run_id": result.run_id, "bundles": result.bundles, "items": result.items,
        "diffs": result.diffs, "wrote": result.log,
        "writes": writes,
        "questions": result.questions, "woken": result.woken,
        "sweep": result.sweep_actions, "errors": result.errors,
        "usage": result.usage_summary, "nothing_new": result.nothing_new,
    }


# ------------------------------------------------------------------- server --
