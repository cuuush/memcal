"""memcal CLI.

Every subcommand is usable on its own; the dream pass is one of them, not the point
of the thing.
"""

from __future__ import annotations

import argparse
import difflib
import getpass
import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, timedelta
from functools import wraps

from . import (archive, brief, calls, config, db, detail, events, identity, live,
               llm, schedule, series, todos, trace, web, wiki)
from .config import Config
from .dream import bundle as bundle_stage
from .dream import propose as propose_stage
from .dream.run import dream
from . import sources
from .sources import ical
from .llm import LLMError

WEEKDAYS = {"mon": 0, "tue": 1, "tues": 1, "wed": 2, "weds": 2, "thu": 3, "thur": 3,
            "thurs": 3, "fri": 4, "sat": 5, "sun": 6}


def parse_when(value: str | None) -> str:
    """'fri', 'tomorrow', '+3', '2026-08-01' — all resolve to an ISO date."""
    if not value:
        return db.today().isoformat()
    raw = value.strip().lower()
    today = db.today()
    if raw in ("today", "tod"):
        return today.isoformat()
    if raw in ("tomorrow", "tmr", "tmrw"):
        return (today + timedelta(days=1)).isoformat()
    if raw in ("yesterday",):
        return (today - timedelta(days=1)).isoformat()
    if raw.startswith(("+", "-")) and raw[1:].isdigit():
        return (today + timedelta(days=int(raw))).isoformat()
    key = raw[:5] if raw[:5] in WEEKDAYS else raw[:4] if raw[:4] in WEEKDAYS else raw[:3]
    if key in WEEKDAYS:
        ahead = (WEEKDAYS[key] - today.weekday()) % 7
        return (today + timedelta(days=ahead or 7)).isoformat()
    try:
        return db.parse_date(raw).isoformat()
    except ValueError:
        raise SystemExit(f"can't read date: {value}")


def open_ctx(args) -> tuple[Config, sqlite3.Connection]:
    cfg = config.load(getattr(args, "home", None))
    cfg.ensure_dirs()
    conn = db.open_db(cfg.db_path)
    _own_connection(args, conn)
    return cfg, conn


def _own_connection(args, conn: sqlite3.Connection) -> None:
    """Register a connection with the current CLI lifecycle owner, if any."""
    owned = getattr(args, "_owned_connections", None)
    if owned is None:
        owned = getattr(args, "_direct_connections", None)
    if owned is not None:
        owned.append(conn)


def _closes_direct_connections(func):
    """Close command-owned connections when a caller bypasses ``main``."""
    @wraps(func)
    def command(args):
        if getattr(args, "_owned_connections", None) is not None:
            return func(args)
        args._direct_connections = []
        try:
            return func(args)
        finally:
            for conn in reversed(args._direct_connections):
                conn.close()
            del args._direct_connections
    return command


# ------------------------------------------------------------------- handles --
#
# The brief is an index and a handle is what opens an entry in it. Every other surface
# has been able to follow one since `detail.open_handle` was written — the web UI, MCP,
# Hermes — and the CLI, which is the surface that *prints* the index, could not. Worse,
# the legend it printed said the handles
# "open with memcal_open", which is an MCP tool name and means nothing at a shell.
#
# So the CLI spoke a second vocabulary: `week --keys` printed `tutoring@2026-08-25` and
# `status`/`rm`/`done` demanded one. Two names for one row, and the one on screen was
# never the one the next command wanted. The index entries were fine, but there was no
# way to act on them.

#: What a handle looks like when a human types it. The brief prints `〔E286〕`; the
#: brackets are for a model reading prose and are a nuisance at a shell, so both spellings
#: resolve and every listing here prints the bare one.
HANDLE_RE = re.compile(r"^[〔\[]?\s*([ETQSetqs])\s*(\d+)\s*[〕\]]?$")

#: `brief.source_tag` without the brackets. Same handle, typable.
def handle(kind: str, row_id: int | None) -> str:
    prefix = {"event": "E", "todo": "T", "question": "Q", "standing": "S"}.get(kind)
    return f"{prefix}{row_id}" if prefix and row_id is not None else ""


def is_handle(token: str) -> bool:
    return bool(HANDLE_RE.match(str(token or "").strip()))


def resolve_handle(conn: sqlite3.Connection, token: str) -> dict | None:
    """`E286` → the row it names, through the same resolver every other surface uses.

    Returns `trace.resolve_source`'s dict, or None when this is not a handle at all —
    which is the caller's cue to fall back to a key or a title, so nothing that used to
    work stops working.
    """
    match = HANDLE_RE.match(str(token or "").strip())
    if not match:
        return None
    found = trace.resolve_source(conn, f"{match.group(1).upper()}{match.group(2)}")
    return None if found.get("error") else found


def find_event(conn: sqlite3.Connection, token: str) -> events.Event | None:
    """However the user refers to a row: `E286`, `tutoring@2026-08-25`, or its title.

    Handle first, because that is what is on screen. The title fallback is last and
    stays deliberately loose — it is how `memcal status poker confirmed` has always
    worked and there is no reason to take it away.
    """
    found = resolve_handle(conn, token)
    if found and found["kind"] == "event":
        return events.get(conn, found["ref"])
    row = events.get(conn, str(token or ""))
    if row:
        return row
    return events.find_match(conn, title=str(token or ""), on=db.today().isoformat())


def find_todo(conn: sqlite3.Connection, token: str):
    found = resolve_handle(conn, token)
    if found and found["kind"] == "todo":
        return todos.get(conn, found["ref"])
    return todos.get(conn, str(token or "")) or todos.find(conn, str(token or ""))


# ---------------------------------------------------------------------- json --

def emit_json(payload) -> int:
    """One shape for every `--json`, so a caller learns it once.

    Machine output is not a nicety here: the store is the useful half of memcal and the
    only ways to get at it were the web UI, an MCP client, or SQLite. A shell script had
    to parse a line designed to be read by a human — which is how `week --keys` came to
    exist, printing a second identifier in brackets so something could grep it.
    """
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2, default=str)
    sys.stdout.write("\n")
    return 0


def event_json(event) -> dict:
    return {"handle": handle("event", event.id), "key": event.key,
            "title": event.title, "date": event.date, "until": event.until,
            "time": event.time, "kind": event.kind, "status": event.status,
            "location": event.location, "participants": list(event.participants or []),
            "series": event.series, "note": event.note, "rsvp_url": event.rsvp_url,
            "join_url": event.join_url, "source": event.source, "origin": event.origin}


def todo_json(todo) -> dict:
    return {"handle": handle("todo", todo.id), "key": todo.key, "text": todo.text,
            "status": todo.status, "due": todo.due, "subject": todo.subject,
            "opened_at": getattr(todo, "opened_at", None)}


# ------------------------------------------------------------------ commands --

def cmd_init(args) -> int:
    cfg, conn = open_ctx(args)
    print(f"home      {cfg.home}")
    print(f"db        {cfg.db_path}")
    print(f"wiki      {cfg.wiki_dir}")
    print(f"brief     {cfg.brief_path}")
    linked, message = identity.import_contacts(conn)
    print(f"contacts  {message}")
    if not todos.standing(conn, "identity"):
        print("\nnothing in standing yet. Start with something like:")
        print('  memcal standing identity "Casey, North End. Dog: Comet."')
    brief.write(conn, cfg)
    print(f"\nwrote {cfg.brief_path}")
    return 0


def _write_env(path, values: dict[str, str]) -> None:
    """Update owned keys without flattening a person's hand-edited .env file."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    pending = dict(values)
    out = []
    for line in lines:
        stripped = line.strip()
        key = stripped.partition("=")[0].strip() if "=" in stripped else ""
        if key in pending:
            out.append(f"{key}={pending.pop(key)}")
        else:
            out.append(line)
    if pending and out and out[-1].strip():
        out.append("")
    out.extend(f"{key}={value}" for key, value in pending.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _provider_choice() -> str:
    choices = (("1", "openrouter", "OpenRouter API key"),
               ("2", "claude-code", "Claude Code programmatic mode"),
               ("3", "codex", "Codex programmatic mode"))
    print("LLM provider:")
    for number, _value, label in choices:
        print(f"  {number}. {label}")
    try:
        answer = input("Choose [1]: ").strip() or "1"
    except EOFError as exc:
        raise SystemExit("no interactive input; pass --provider") from exc
    by_input = {number: value for number, value, _label in choices}
    by_input.update({value: value for _number, value, _label in choices})
    if answer not in by_input:
        raise SystemExit("choose 1, 2, 3, openrouter, claude-code, or codex")
    return by_input[answer]


def cmd_setup(args) -> int:
    """Guided provider choice, persisted in memcal's existing .env boundary."""
    cfg = config.load(getattr(args, "home", None))
    cfg.ensure_dirs()
    guided = not args.provider
    provider = args.provider or _provider_choice()
    default_model = llm.PROVIDER_DEFAULT_MODELS[provider]
    model = args.model
    if guided and not model:
        try:
            model = input(f"Model [{default_model}]: ").strip() or default_model
        except EOFError:
            model = default_model
    model = model or default_model

    values = {
        "MEMCAL_LLM_PROVIDER": provider,
        "MEMCAL_PROPOSE_MODEL": model,
        "MEMCAL_SWEEP_MODEL": model,
        "MEMCAL_MATCH_MODEL": model,
    }
    if provider == "openrouter":
        key = args.api_key or cfg.api_key
        if not key and guided:
            key = getpass.getpass("OpenRouter API key: ").strip()
        if not key:
            print("error: OpenRouter needs --api-key or OPENROUTER_API_KEY", file=sys.stderr)
            return 1
        if args.api_key or not cfg.api_key:
            values["OPENROUTER_API_KEY"] = key
    _write_env(cfg.home / ".env", values)

    ready = config.load(cfg.home)
    ok, detail = llm.provider_status(ready)
    print(f"saved     {cfg.home / '.env'}")
    print(f"provider  {provider}")
    print(f"model     {model}")
    print(f"runtime   {detail}")
    if not ok:
        print("setup was saved, but the selected runtime is not available", file=sys.stderr)
        return 1
    return 0


def _run_openclaw(command: list[str]) -> tuple[bool, str]:
    try:
        proc = subprocess.run(command, text=True, capture_output=True, check=False)
    except FileNotFoundError:
        return False, "openclaw command not found"
    text = (proc.stdout or proc.stderr or "").strip()
    return proc.returncode == 0, text


def cmd_openclaw(args) -> int:
    """Install or inspect the native prompt hook plus memcal's stdio MCP server."""
    cfg = config.load(getattr(args, "home", None))
    plugin = config.PROJECT_ROOT / "integrations" / "openclaw"
    if args.action == "status":
        checks = (("plugin", ["openclaw", "plugins", "inspect", "memcal", "--runtime", "--json"]),
                  ("mcp", ["openclaw", "mcp", "show", "memcal", "--json"]))
        failed = False
        for label, command in checks:
            ok, text = _run_openclaw(command)
            print(f"{label:<8} {'ok' if ok else 'missing'}"
                  + (f" — {' '.join(text.split())[:180]}" if text else ""))
            failed = failed or not ok
        return 1 if failed else 0

    if not args.yes:
        try:
            answer = input("Link the memcal plugin and MCP server into OpenClaw? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("nothing changed")
            return 1

    mcp = json.dumps({
        "command": sys.executable,
        "args": ["-m", "memcal.mcp_server"],
        "cwd": str(config.PROJECT_ROOT),
        "env": {"MEMCAL_HOME": str(cfg.home)},
    }, separators=(",", ":"))
    commands = (
        ["openclaw", "plugins", "install", "--link", str(plugin)],
        ["openclaw", "plugins", "enable", "memcal"],
        ["openclaw", "mcp", "set", "memcal", mcp],
    )
    for command in commands:
        ok, text = _run_openclaw(command)
        if not ok:
            print(f"error: {' '.join(command[:3])}: {text}", file=sys.stderr)
            return 1
    print("OpenClaw now has fresh memcal context and the memcal MCP tools.")
    print("Restart the OpenClaw gateway before the next agent turn.")
    return 0


def cmd_brief(args) -> int:
    cfg, conn = open_ctx(args)
    # The CLI spelling of the legend: at a shell the verb is `memcal open E258`, not the
    # MCP tool name. `--write` still writes the agent spelling, because the file on disk
    # is read by agents and not by this terminal.
    text = brief.render(conn, cfg, surface="cli")
    if args.write:
        brief.write(conn, cfg)
    sys.stdout.write(text)
    if args.tokens:
        print(f"\n[~{brief.approx_tokens(text)} tokens / cap {cfg.brief_token_cap}]",
              file=sys.stderr)
    return 0


@_closes_direct_connections
def cmd_open(args) -> int:
    """Follow a handle. The other half of printing an index.

    `detail.open_handle` has assembled this for the web UI, MCP and Hermes since it was
    written, and the CLI — the surface that prints the brief — had no route to it. So
    `memcal` answered with fourteen 〔E#〕 handles and no way to open one.
    """
    cfg, conn = open_ctx(args)
    text = detail.open_handle(conn, cfg, args.ref)
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 1 if text.startswith("(not a memcal handle") or text.startswith("no row") else 0


@_closes_direct_connections
def cmd_week(args) -> int:
    cfg, conn = open_ctx(args)
    rows = events.window(conn, args.back if args.back is not None else cfg.days_back,
                         args.forward if args.forward is not None else cfg.days_forward)
    if args.json:
        return emit_json([event_json(ev) for ev in rows])
    if not rows:
        print("(nothing known)")
        return 0
    for ev in rows:
        # The handle leads, because it is what the next command wants. `--keys` still
        # appends the store key for anyone who wants the durable identifier.
        line = f"{handle('event', ev.id):>5}  {ev.one_line()}"
        print(f"{line}   [{ev.key}]" if args.keys else line)
    _open_hint(rows)
    return 0


def _open_hint(rows) -> None:
    """Say what to do with the handles, once, on a listing that has any.

    An index whose entries cannot be followed is the complaint; an index that can be
    followed and does not say so is the same complaint one step later.
    """
    if rows and sys.stdout.isatty():
        first = handle("event", getattr(rows[0], "id", None)) or "E1"
        print(f"\n  memcal open {first}   — everything known about a row", file=sys.stderr)


def cmd_month(args) -> int:
    """Separate from the user calendar. It's the memory cal — it's different."""
    cfg, conn = open_ctx(args)
    ref = db.parse_date(args.month + "-01") if args.month else db.today().replace(day=1)
    nxt = (ref.replace(day=28) + timedelta(days=4)).replace(day=1)
    rows = events.between(conn, ref.isoformat(), (nxt - timedelta(days=1)).isoformat())
    if args.json:
        return emit_json([event_json(ev) for ev in rows])
    print(f"# memcal {ref.strftime('%B %Y')} ({len(rows)} rows)")
    for ev in rows:
        print(f"{handle('event', ev.id):>5}  {ev.one_line()}")
    return 0


def cmd_add(args) -> int:
    cfg, conn = open_ctx(args)
    event, verb = events.upsert(
        conn,
        {
            "title": args.title,
            "date": parse_when(args.date),
            "time": args.time,
            "kind": args.kind,
            "subject": args.subject,
            "location": args.where,
            "status": args.status,
            "participants": args.who or [],
            "series": args.series,
        },
        written_by="cli",
    )
    print(f"{verb}: {handle('event', event.id)}  {event.one_line()}  [{event.key}]")
    brief.write(conn, cfg)
    return 0


def cmd_status(args) -> int:
    cfg, conn = open_ctx(args)
    event = find_event(conn, args.key)
    if not event:
        print(f"no row matching {args.key!r}. "
              f"Handles look like E286 and `memcal week` prints them.")
        return 1
    updated, verb = events.upsert(conn, {"key": event.key, "date": event.date,
                                         "title": event.title, "status": args.status},
                                  written_by="cli", match=False)
    print(f"{verb}: {updated.one_line()}")
    brief.write(conn, cfg)
    return 0


def cmd_rm(args) -> int:
    cfg, conn = open_ctx(args)
    event = find_event(conn, args.key)
    ok = bool(event) and events.delete(conn, event.key)
    print(f"deleted {event.key}" if ok else f"no row matching {args.key!r}")
    brief.write(conn, cfg)
    return 0 if ok else 1


def cmd_todo(args) -> int:
    cfg, conn = open_ctx(args)
    # Through `live` when a reminder is wanted, because that is the layer that resolves
    # the hour, publishes to the phone and records the live write. `todos.open_todo` is
    # the plain path and stays the plain path.
    if args.remind is not None:
        try:
            todo, verb = live.open_todo(conn, cfg, args.text, due=args.due,
                                        remind=args.remind, wake_condition=args.when,
                                        event=args.event)
        except live.LiveError as exc:
            print(exc)
            return 1
        print(f"{verb}: {handle('todo', todo.id)}  {todo.one_line()}  [{todo.key}]")
        print(f"  reminding you {todo.remind_at}")
        return 0
    linked = live.find_event(conn, args.event) if args.event else None
    todo, verb = todos.open_todo(conn, args.text, subject=args.who, due=args.due,
                                 wake_condition=args.when,
                                 event_id=linked.id if linked else None,
                                 written_by="cli")
    print(f"{verb}: {handle('todo', todo.id)}  {todo.one_line()}  [{todo.key}]")
    brief.write(conn, cfg)
    return 0


def cmd_series(args) -> int:
    """Read or move a recurring thing's schedule. The rule, not one occurrence."""
    cfg, conn = open_ctx(args)
    if not args.which:
        rows = conn.execute("SELECT slug FROM series ORDER BY status, slug").fetchall()
        if not rows:
            print("(no recurring schedules)")
        for row in rows:
            rule = series.get(conn, row["slug"])
            nxt = series.next_on(conn, rule)
            print(f"- {rule.title} [{rule.slug}] — {rule.phrase}"
                  + (f"; next {nxt.isoformat()}" if nxt else "")
                  + (f" · join: {rule.join_url}" if rule.join_url else ""))
        return 0
    try:
        rule, log = live.set_schedule(
            conn, cfg, args.which, cadence=args.every, weekday=args.on,
            day_of_month=args.day, time=args.at, location=args.where,
            join_url=args.join, starting=args.starting, ended=args.ended)
    except live.LiveError as exc:
        print(f"{exc}")
        return 1
    print(f"{rule.title}: {rule.phrase}, from {rule.effective_on}")
    for line in log:
        print(f"  {line}")
    return 0


def cmd_todos(args) -> int:
    _cfg, conn = open_ctx(args)
    items = todos.open_items(conn)
    if args.json:
        return emit_json([todo_json(t) for t in items])
    if not items:
        print("(nothing open)")
    for todo in items:
        line = f"{handle('todo', todo.id):>5}  {todo.one_line()}"
        print(f"{line}   [{todo.key}]" if args.keys else line)
    _open_hint([])
    return 0


def cmd_done(args) -> int:
    cfg, conn = open_ctx(args)
    todo = find_todo(conn, args.what)
    if not todo:
        print(f"no open to-do matching {args.what!r}. "
              f"Handles look like T7 and `memcal todos` prints them.")
        return 1
    todos.close(conn, todo.key)
    print(f"closed: {todo.text}")
    brief.write(conn, cfg)
    return 0


def cmd_ask(args) -> int:
    cfg, conn = open_ctx(args)
    todos.ask(conn, args.text, written_by="cli")
    print(f"asked: {args.text}")
    brief.write(conn, cfg)
    return 0


@_closes_direct_connections
def cmd_answer(args) -> int:
    cfg, conn = open_ctx(args)
    # `todos.resolve` matches on words, which is right for prose. A handle is not
    # words, so translate it to the row's own text first rather than teaching a second
    # lookup — one vocabulary at the edge, one matcher underneath.
    needle = args.question
    found = resolve_handle(conn, needle)
    if found and found["kind"] in ("question", "todo"):
        needle = found["label"] or needle
    ok, kind = todos.resolve(conn, needle, args.answer)
    print(f"resolved ({kind})" if ok else "nothing open matches that")
    brief.write(conn, cfg)
    return 0 if ok else 1


def cmd_standing(args) -> int:
    cfg, conn = open_ctx(args)
    if not args.value:
        rows = todos.standing(conn, args.kind if args.kind != "all" else None)
        for row in rows:
            print(f"{handle('standing', row['id']):>5}  {row['kind']:11} "
                  f"{row['scope']:9} {row['value']}   [{row['key']}]")
        return 0
    key, verb = todos.set_standing(conn, args.kind, args.value,
                                   scope="permanent" if args.permanent else "session")
    print(f"{verb}: {args.kind} = {args.value}  [{key}]")
    brief.write(conn, cfg)
    return 0


@_closes_direct_connections
def cmd_forget(args) -> int:
    cfg, conn = open_ctx(args)
    found = resolve_handle(conn, args.key)
    key = found["ref"] if found and found["kind"] == "standing" else args.key
    ok = todos.forget_standing(conn, key)
    print("forgotten" if ok else "no such standing key")
    brief.write(conn, cfg)
    return 0 if ok else 1


def cmd_page(args) -> int:
    cfg, conn = open_ctx(args)
    if args.slot and args.value:
        page = wiki.set_slot(cfg.wiki_dir, args.slug, args.slot, args.value,
                             source="cli", section=args.section, conn=conn)
        print(f"{page.path}: {args.slot} = {args.value}")
        return 0
    page = wiki.read(cfg.wiki_dir, args.slug)
    if not page:
        print(f"no page for {args.slug}. Pages: {', '.join(wiki.list_pages(cfg.wiki_dir)) or '(none)'}")
        return 1
    sys.stdout.write(page.render())
    return 0


def cmd_pages(args) -> int:
    cfg, _conn = open_ctx(args)
    pages = wiki.list_pages(cfg.wiki_dir)
    print("\n".join(pages) if pages else "(no pages yet — they're created lazily)")
    return 0


def cmd_alias(args) -> int:
    cfg, _conn = open_ctx(args)
    if not args.name:
        page = wiki.read(cfg.wiki_dir, args.slug)
        if not page:
            print(f"no page for {args.slug}")
            return 1
        print("\n".join(page.aliases) if page.aliases
              else f"{page.slug} has no other names on file")
        return 0
    try:
        page = wiki.add_alias(cfg.wiki_dir, args.slug, args.name)
    except ValueError as exc:
        print(exc)
        return 1
    print(f"{page.path}: also known as {', '.join(page.aliases)}")
    return 0


def cmd_merge(args) -> int:
    cfg, _conn = open_ctx(args)
    keep, drop = wiki.read(cfg.wiki_dir, args.keep), wiki.read(cfg.wiki_dir, args.drop)
    if keep is None or drop is None:
        print(f"nothing to do: {args.keep}={keep is not None}, {args.drop}={drop is not None}")
        return 1
    if not args.apply:
        print(f"would keep {keep.slug} and fold in {drop.slug}\n")
        print(keep.render())
        print("-" * 40)
        print(drop.render())
        print("\ndry run — re-run with --apply")
        return 0
    page = wiki.merge(cfg.wiki_dir, args.keep, args.drop)
    sys.stdout.write(page.render())
    return 0


def cmd_search(args) -> int:
    _cfg, conn = open_ctx(args)
    rows = archive.search(conn, args.query, limit=args.limit)
    if args.json:
        return emit_json([
            {"id": r["id"], "stream": r["stream"], "ts": r["ts"],
             "thread": r["thread"], "handle": r["handle"], "person": r["person"],
             "from_me": bool(r["from_me"]), "gated": bool(r["gated"]),
             "gate_reason": r["gate_reason"], "text": r["text"]} for r in rows])
    if not rows:
        print("(nothing)")
    for row in rows:
        who = "me" if row["from_me"] else (row["person"] or row["handle"] or "?")
        print(f"{str(row['ts'])[:16]}  {row['stream']:8} {who:14} {row['text'][:120]}")
    return 0


@_closes_direct_connections
def cmd_who(args) -> int:
    """Clear the unresolved queue: adopt what the platform already said, list the rest.

    The queue had 247 rows and 47 of them carried 25+ messages each, which is a queue
    nobody was ever going to work through by hand. Most of that was not a naming problem
    at all — GroupMe had been telling us the display name the whole time and nothing took
    it. What is left after `--adopt` is the genuinely unanswerable part, and that is small
    enough to sit down with.
    """
    _cfg, conn = open_ctx(args)
    if args.handle and args.person:
        identity.link(conn, args.handle, args.person, source="cli")
        print(f"{identity.normalize(args.handle)} → {args.person}")
        return 0

    if args.adopt:
        dropped = identity.forget_non_people(conn)
        taken = identity.adopt_platform_names(conn)
        for handle, person in taken:
            print(f"  {handle:20} → {person}")
        print(f"\nadopted {len(taken)}"
              + (f", dropped {dropped} non-person handle(s)" if dropped else ""))

    # Two spellings of one name that Contacts cannot join. Shown here rather than
    # asked, because the user has already dismissed being asked it — see `candidate_lines`.
    pairs = identity.candidate_lines(conn)
    if pairs:
        print(f"\n# possibly one person, on two platforms ({len(pairs)})")
        print("  memcal who <handle> <name> to link one; nothing merges on its own")
        for line in pairs:
            print(line)

    rows = identity.unresolved(conn, limit=args.limit)
    if not rows:
        if not pairs:
            print("(no unresolved handles)")
        return 0
    # Named by *what memcal can see*, because the two halves need different things from
    # them. An id whose platform gave a name is one keystroke; an id with no name anywhere
    # — a WhatsApp LID, which is not a phone number and matches no contact by design — can
    # only ever be answered by the person who knows whose 472 messages those are.
    nameless = [r for r in rows if not (r["seen_name"] or "").strip()]
    named = [r for r in rows if (r["seen_name"] or "").strip()]
    for title, group in (("nobody can name these but you", nameless),
                         ("the platform offers a name", named)):
        if not group:
            continue
        print(f"\n# {title} ({len(group)})")
        for row in group:
            where = ", ".join(identity.where_seen(conn, row["handle"]))
            # The guess is printed as a guess and never applied. It matches on first
            # name, so on this very queue it offered "joe coleman" for Joe Navarro.
            hint = (row["seen_name"] or "").strip() \
                or (f"?{identity.guess_person(conn, row)}"
                    if identity.guess_person(conn, row) else "")
            print(f"  {row['handle']:20} x{row['count']:<5} {hint:20} "
                  f"{where[:24]:26}{_one_line(row['sample'] or '', 46)}")
    print("\n  memcal who <handle> \"<name>\"   name one"
          "\n  memcal who --adopt              take every name the platform already gave"
          "\n  a `?name` is a first-name guess and is never applied on its own")
    return 0


def cmd_senders(args) -> int:
    _cfg, conn = open_ctx(args)
    if args.address and args.decision:
        # From the CLI it is them typing, so it is a judgement, not the gate's own
        # bookkeeping — which is what stops a subject line from reopening it later.
        identity.set_sender(conn, args.address, args.decision, reason="cli", source="you")
        print(f"{args.address} → {args.decision}  (permanent; the subject test will not "
              f"reopen it)" if args.decision != "process" else f"{args.address} → process")
        return 0
    rows = identity.senders(conn, args.decision if args.address is None else None)
    if not rows:
        print("(sender table empty)")
    for row in rows:
        who = row["source"] or "auto"
        mark = "" if who == "auto" else f"  [{who}]"
        print(f"{row['decision']:8} x{row['count']:<5} {row['address']:40} "
              f"{row['reason'] or ''}{mark}")
    return 0


def cmd_block(args) -> int:
    """"I don't care about this." The same verb the agent and the web UI use."""
    cfg, conn = open_ctx(args)
    payload = {"by": "agent" if args.agent else "you", "reason": args.reason}
    if args.target and "@" in args.target and "/" not in args.target:
        payload["address"] = args.target
    elif args.target and "/" in args.target:
        stream, _, thread = args.target.partition("/")
        payload["stream"], payload["thread"] = stream, thread
    elif args.event:
        payload["event_id"] = args.event
    else:
        print("give an address, a stream/thread, or --event <id>")
        return 2
    out = web.block(conn, cfg, payload)
    if out.get("error"):
        print(out["error"])
        return 1
    retired = out.get("retired") or 0
    print(f"blocked {out['blocked']}"
          + (f" — {retired} queued item(s) retired" if retired else ""))
    return 0


def cmd_top(args) -> int:
    _cfg, conn = open_ctx(args)
    if args.person:
        if args.remove:
            identity.remove_top_tier(conn, args.person)
            print(f"removed {args.person}")
        else:
            identity.add_top_tier(conn, args.person)
            print(f"top tier: {args.person}")
        return 0
    tier = identity.top_tier(conn)
    print("\n".join(sorted(tier)) if tier else "(nobody in the top tier)")
    return 0


def _behind_and_reachable(conn: sqlite3.Connection, cfg: Config, candidates: list):
    """Sources that are behind *and* answer the phone right now."""
    behind = {stream for stream, _age in archive.stale_streams(conn, cfg=cfg)}
    out = []
    for source in candidates:
        if source.name not in behind:
            continue
        ok, _why = source.check(cfg)
        if ok:
            out.append(source)
    return out


@_closes_direct_connections
def cmd_ingest(args) -> int:
    cfg, conn = open_ctx(args)
    if args.stream == "all":
        chosen = [s for s in sources.all_sources(cfg) if s.in_all]
    else:
        source = sources.get(args.stream, cfg)
        if not source:
            known = ", ".join(sources.names(cfg))
            print(f"unknown source {args.stream!r}. Available: {known}")
            return 1
        chosen = [source]

    if args.stale:
        chosen = _behind_and_reachable(conn, cfg, chosen)
        if not chosen:
            print("nothing stale that is reachable right now")
            return 0
        print(f"catching up: {', '.join(s.name for s in chosen)}")

    # Resolve before ingesting, not after: a handle named today is a person on every
    # row read in the next minute, and an unnamed one is a bundle keyed on a phone
    # number that never joins up with anything.
    linked, message = identity.refresh_contacts(conn)
    if linked:
        print(f"contacts: {message}")

    failed = []
    collection_id = archive.open_collection(conn, mode="cli")
    for source in chosen:
        report = sources.catch_up(source, conn, cfg, limit=args.limit,
                                  rounds=args.rounds, collection_id=collection_id)
        print(report.summary())
        if report.error:
            failed.append(source.name)
    archive.close_collection(conn, collection_id)
    brief.write(conn, cfg)
    # Any failure is a failure, including in `ingest all`. This used to be
    # `failed and len(chosen) == 1`, so the one caller that runs unattended — the 3am
    # launchd job, which always passes `all` — could not fail. The Proton Bridge was
    # unreachable for nine consecutive nights, every one of them exited 0, `nightly.log`
    # ended each with "done", and the only thing that ever said otherwise was a line in
    # the brief addressed to a reader who was asleep.
    if failed:
        print(f"\n{len(failed)} source(s) failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


def cmd_sources(args) -> int:
    """What can feed memcal right now, and what each one still needs."""
    cfg, _conn = open_ctx(args)
    rows = sources.all_sources(cfg)
    if args.json:
        out = []
        for source in rows:
            ok, message = source.check(cfg)
            out.append({"name": source.name, "usable": ok, "detail": message,
                        "description": source.description, "in_all": source.in_all,
                        "health": getattr(source, "health", "stream")})
        return emit_json({"sources": out, "load_errors": list(sources.load_errors()),
                          "plugin_dir": str(cfg.plugin_dir)})
    if not rows:
        print("(no sources registered)")
    for source in rows:
        ok, message = source.check(cfg)
        flag = "ok" if ok else "--"
        tag = "" if source.in_all else "  (not in `ingest all`)"
        print(f"{flag} {source.name:12} {source.description}{tag}")
        print(f"   {message}")
    for problem in sources.load_errors():
        print(f"!! {problem}")
    print(f"\nplugin directory: {cfg.plugin_dir}")
    return 0


def cmd_reminders(args) -> int:
    """Request or report macOS Reminders permission.

    Separate from `ical setup` on purpose: Reminders is a different EventKit entity type
    with its own authorization and its own consent dialog, and granting Calendar grants
    nothing here. A publish that discovers that at 3am has already lost the reminder.
    """
    cfg, conn = open_ctx(args)
    name = (cfg.publish_reminders or "").strip()
    try:
        if args.action == "setup":
            if not args.yes:
                print("This opens a macOS consent dialog for Reminders access.")
                print("Re-run with --yes to request it.")
                return 0
            ok, message = ical.request_reminders_access()
        else:
            payload = ical._reminder_call("status", name or "")
            ok = payload.get("status") == ical.EK_FULL_ACCESS
            message = ("Reminders access granted" if ok else
                       f"Reminders access not granted (status {payload.get('status')}) — "
                       "run `memcal reminders setup --yes`")
    except ical.ReminderError as exc:
        ok, message = False, str(exc)
    finally:
        conn.close()
    print(("ok " if ok else "-- ") + message)
    if ok and not name:
        print("   publishing is off: set MEMCAL_PUBLISH_REMINDERS in ~/.memcal/.env")
    return 0 if ok else 1


def cmd_ical(args) -> int:
    """Request or report macOS Calendar permission for the real requester."""
    cfg, conn = open_ctx(args)
    if args.action == "probe":
        ok, message = ical.permission_status()
        if ok:
            db.set_meta(conn, f"ical.permission.{args.context}", db.now())
        if args.result:
            from pathlib import Path
            Path(args.result).write_text(
                json.dumps({"ok": ok, "message": message}), encoding="utf-8"
            )
        else:
            print(("ok " if ok else "-- ") + message)
        conn.close()
        return 0 if ok else 1

    if args.action == "account":
        ok, message = ical.account_status(cfg)
        print(("ok " if ok else "-- ") + message)
        conn.close()
        return 0 if ok else 1

    if args.action == "migrate":
        # memcal's calendar was created through Calendar.app's scripting interface, which
        # can only make local calendars, so it never left this Mac. This copies it into
        # iCloud and re-points memcal's own records at the copies.
        if not (cfg.publish_calendar or "").strip():
            print("publishing is off (publish_calendar is empty); nothing to migrate.")
            conn.close()
            return 0
        for line in ical.migrate_to_icloud(conn, cfg, dry_run=not args.yes):
            print(line)
        if not args.yes:
            print("\nThis was a dry run. Re-run with `memcal ical migrate --yes` to move "
                  "the events and delete the local calendar.")
        conn.close()
        return 0

    installed = schedule.status(cfg)["installed"]
    if args.action == "status":
        interactive = db.get_meta(conn, "ical.permission.interactive")
        nightly = db.get_meta(conn, "ical.permission.nightly")
        print("Calendar permission status (passive; no macOS dialogs):")
        print(f"  interactive  last verified {interactive}" if interactive else
              "  interactive  not verified")
        if installed:
            print(f"  nightly      last verified {nightly}" if nightly else
                  "  nightly      not verified")
        else:
            print("  nightly      schedule not installed")
        print("Run `memcal ical setup` for a live check.")
        conn.close()
        return 0 if (nightly if installed else interactive) else 1

    target = "nightly launchd job" if installed else "current interactive launcher"
    print(
        f"Calendar permission setup will now make one live request from the {target}.\n"
        "macOS may show up to two consent dialogs: Calendar data and Automation.\n"
        "No events are imported by this check."
    )
    if not args.yes:
        if not sys.stdin.isatty():
            print("Stopped before requesting access. Re-run with `memcal ical setup --yes`.")
            conn.close()
            return 2
        answer = input("Continue and let macOS request Calendar access? [y/N] ")
        if answer.strip().casefold() not in ("y", "yes"):
            print("Stopped before requesting access.")
            conn.close()
            return 2

    if installed:
        conn.close()
        ok, message = schedule.calendar_permission_probe(cfg)
        print(("ok " if ok else "-- ") + message)
    else:
        ok, message = ical.permission_status()
        if ok:
            db.set_meta(conn, "ical.permission.interactive", db.now())
        conn.close()
        print(("ok " if ok else "-- ") + f"interactive requester: {message}")
        print(
            "The nightly schedule is not installed. After `memcal schedule install`, "
            "run this setup command again for its launchd requester."
        )
    if (cfg.publish_calendar or "").strip():
        # A second, separate macOS permission. Reading the calendar goes through Apple
        # Events; *naming the account* to create memcal's calendar in goes through
        # EventKit, and granting one grants nothing of the other. Only asked for when the
        # store actually publishes — invariant 11.
        granted, message = ical.request_calendar_access()
        print(("ok " if granted else "-- ") + message)
        if granted:
            where_ok, where = ical.account_status(cfg)
            print(("ok " if where_ok else "-- ") + where)
        ok = ok and granted
    return 0 if ok else 1


def cmd_gatecheck(args) -> int:
    """Read the gate's output before connecting a model to it."""
    _cfg, conn = open_ctx(args)
    rows = archive.recent(conn, limit=args.limit, stream=args.stream)
    passed = 0
    for row in rows:
        mark = "PASS" if row["gated"] else "  · "
        passed += bool(row["gated"])
        who = "me" if row["from_me"] else (row["person"] or row["handle"] or "?")
        print(f"{mark} {row['gate_reason'] or '':16} {who:14} {(row['text'] or '')[:90]}")
    print(f"\n{passed}/{len(rows)} passed the gate")
    return 0


def _dream_waits(event: str, data: dict) -> None:
    """Say so while the pass is waiting."""
    if event == "stage" and data.get("state") == "waiting":
        print(f"  {data.get('stage') or 'model'}: {data.get('note') or ''}", flush=True)


@_closes_direct_connections
def cmd_dream(args) -> int:
    """One pass, or as many as it takes to drain the queue.

    A pass reads the newest `items_per_entity` lines of every waiting conversation, which
    is the right shape for a nightly run and the wrong shape for a first load: thirty days
    imported in one go leaves several passes' worth of backlog, and reading only the last
    two days of their partner's thread is exactly the failure the round-robin fixed at the
    other end. `--rounds` keeps going until nothing is left, printing what each pass cost.
    """
    cfg, conn = open_ctx(args)
    failed = False
    for round_no in range(1, max(1, args.rounds) + 1):
        result = dream(conn, cfg, mode=args.mode, model=args.model, limit=args.limit,
                       dry_run=args.dry_run, skip_sweep=args.no_sweep,
                       redo=args.redo if round_no == 1 else None,
                       progress=_dream_waits)
        if args.rounds > 1:
            print(f"\n=== round {round_no} of {args.rounds} ===")
        print(result.report())
        failed = failed or bool(result.errors)
        left = conn.execute(
            "SELECT count(*) n FROM spool WHERE processed_at IS NULL").fetchone()["n"]
        if result.nothing_new or not left or args.dry_run:
            if args.rounds > 1:
                print(f"\nqueue is drained after {round_no} pass(es)"
                      if not left else f"\nnothing more this pass — {left} still waiting")
            break
        if round_no < args.rounds:
            print(f"  {left} item(s) still waiting — going again")
    return 1 if failed else 0


def cmd_remember(args) -> int:
    """The live path: the user's sitting right there, so this writes now."""
    cfg, conn = open_ctx(args)
    try:
        counts, log = live.remember(conn, cfg, args.text, speaker=args.speaker)
    except LLMError as exc:
        print(f"error: {exc}")
        return 1
    if not log:
        print("nothing to write (correct answer most of the time)")
    for line in log:
        print(line)
    return 0


def cmd_note(args) -> int:
    """A stated fact needs no extractor. Straight onto the page."""
    cfg, conn = open_ctx(args)
    ok, message = live.note(conn, cfg, args.page, args.slot, args.value,
                            section=args.section, source="cli")
    print(message if ok else f"error: {message}")
    return 0 if ok else 1


def cmd_trace(args) -> int:
    """Read back what was actually sent and what came back.

    Current calls are stored locally for every provider. OpenRouter remains the fallback
    for older rows created before local call logging existed.
    """
    cfg, conn = open_ctx(args)
    rows = trace.find(conn, args.what or "")
    if not rows:
        print("no calls recorded yet — run `memcal dream` first")
        return 1

    if not args.what:
        print(f"{'run':>4}  {'when':16}  {'stage':8} {'cost':>8}  what")
        for row in rows:
            print(f"{row['run_id'] or '-':>4}  {str(row['created_at'])[:16]}  "
                  f"{row['stage']:8} ${row['cost_usd']:7.4f}  {row['label'] or ''}")
        print(f"\n`memcal trace <run>` or `memcal trace {rows[0]['generation_id']}`")
        return 0

    for row in rows[: args.limit]:
        print("=" * 78)
        print(f"{row['generation_id']}  run {row['run_id']}  {row['stage']}  {row['label']}")
        print("=" * 78)
        local = calls.load(cfg.home, row["generation_id"], row["run_id"])
        if local:
            for label, field in (("SYSTEM / SHARED CONTEXT", "prefix"),
                                 ("USER / BUNDLE", "suffix"),
                                 ("REASONING", "reasoning"),
                                 ("REPLY", "completion")):
                value = str(local.get(field) or "").strip()
                if value:
                    print(f"\n--- {label} ---\n{value}")
            print()
            continue
        try:
            content = trace.fetch(cfg.api_key, row["generation_id"])
        except trace.TraceError as exc:
            print(f"could not read it back: {exc}")
            continue
        print(trace.render(content, trace.stats(cfg.api_key, row["generation_id"])))
        print()
    return 0


def cmd_review(args) -> int:
    """The human review surface (§8). What it wrote, what it wants to know, what it can't resolve."""
    cfg, conn = open_ctx(args)
    run = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if run:
        print(f"# last run  #{run['id']}  {str(run['started_at'])[:16]}  {run['mode']}"
              f"  {run['bundles']} bundles → {run['diffs']} writes  ${run['cost_usd']:.4f}")
        if run["error"]:
            print(f"  error: {run['error'][:160]}")

    recent = events.between(conn, (db.today() - timedelta(days=1)).isoformat(),
                            (db.today() + timedelta(days=cfg.days_forward)).isoformat())
    fresh = [e for e in recent if str(e.written_by).startswith("dream")]
    if fresh:
        print(f"\n# written by the last passes ({len(fresh)})")
        for ev in fresh[:args.limit]:
            print(f"  {handle('event', ev.id):>5}  {ev.one_line()}")

    questions = todos.open_questions(conn, limit=args.limit)
    if questions:
        print(f"\n# it wants to know ({len(questions)})")
        for q in questions:
            print(f"  {handle('question', q['id']):>5}  {q['text']}")
        print("  answer with:  memcal answer Q35 \"...\"   (or words from the question)")

    stale = [t for t in todos.open_items(conn) if "week" in t.age or "month" in t.age]
    if stale:
        print(f"\n# going stale ({len(stale)})")
        for todo in stale[:args.limit]:
            print(f"  {handle('todo', todo.id):>5}  {todo.one_line()}")

    unknown = identity.unresolved(conn, limit=args.limit)
    if unknown:
        print(f"\n# unresolved handles ({len(identity.unresolved(conn, limit=10000))})")
        for row in unknown[:5]:
            guess = identity.guess_person(conn, row) or ""
            print(f"  {row['handle']:22} x{row['count']:<4} {guess}")
        print("  link with:  memcal who <handle> <person>")

    curious = []
    for slug in wiki.list_pages(cfg.wiki_dir):
        page = wiki.read(cfg.wiki_dir, slug)
        if page and page.questions:
            curious.append((slug, len(page.questions)))
    if curious:
        total = sum(n for _s, n in curious)
        print(f"\n# wiki pages with open questions ({total} across {len(curious)} pages)")
        for slug, count in sorted(curious, key=lambda x: -x[1])[:8]:
            print(f"  {slug:24} {count} open")
    return 0


def cmd_stats(args) -> int:
    """Instrument before optimizing. Post-gate volume is the entire cost story."""
    cfg, conn = open_ctx(args)
    since = (db.today() - timedelta(days=args.days)).isoformat()
    print(f"# archive volume, last {args.days} days")
    for row in archive.counts_by_stream(conn, since):
        gated = row["gated"] or 0
        pct = (100 * gated / row["n"]) if row["n"] else 0
        print(f"{row['stream']:10} {row['n']:6} items  {gated:5} gated ({pct:4.1f}%)  "
              f"~{(row['chars'] or 0)//4:7} tokens raw")
    pending = len(archive.spool_pending(conn, limit=100000))
    print(f"\nspool pending: {pending}")
    rows = conn.execute(
        "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (args.runs,)
    ).fetchall()
    if rows:
        print("\n# recent dream runs")
        for row in rows:
            # What the run spent that no completion accounts for, printed only when
            # there is some: a pass that reported "0 writes, $0.0000" and had made 76
            # requests over 56 minutes read here as a pass that did nothing. NULL is a
            # run older than the columns and prints nothing rather than a zero.
            spent = ""
            if (row["requests"] or 0) or (row["failed_calls"] or 0):
                spent = f"  {row['requests']} req"
                if row["failed_calls"]:
                    spent += f", {row['failed_calls']} failed"
                if (row["wait_seconds"] or 0) >= 1:
                    spent += f", {row['wait_seconds']:.0f}s waiting"
            print(f"#{row['id']:<4} {str(row['started_at'])[:16]} {row['mode']:8} "
                  f"{row['bundles']:3} bundles {row['diffs']:3} writes  "
                  f"{row['prompt_tokens']:6} in ({row['cached_tokens']} cached) "
                  f"{row['completion_tokens']:5} out  ${row['cost_usd']:.4f}" + spent
                  + (f"  ERR {row['error'][:40]}" if row["error"] else ""))
    return 0


def cmd_web(args) -> int:
    """The gate is the entire cost story and it runs with no model, so the only way to
    know whether it is right is to read what it did."""
    cfg, conn = open_ctx(args)
    conn.close()
    try:
        web.serve(cfg, host=args.host, port=args.port, open_browser=not args.no_open)
    except web.WebError as exc:
        print(exc)
        return 1
    return 0


def cmd_schedule(args) -> int:
    cfg, conn = open_ctx(args)
    conn.close()
    if args.action == "install":
        for line in schedule.install(cfg, hour=args.hour, minute=args.minute):
            print(line)
        return 0
    if args.action == "uninstall":
        for line in schedule.uninstall(cfg):
            print(line)
        return 0
    if args.action == "run":
        return schedule.run_now(cfg)

    st = schedule.status(cfg)
    if not st["installed"]:
        print("nightly    not installed — `memcal schedule install`")
        return 1
    when = f"{st['at'][0]:02d}:{st['at'][1]:02d}" if st["at"] else "?"
    print(f"nightly    {'loaded' if st['loaded'] else 'INSTALLED BUT NOT LOADED'} · daily at {when}")
    print(f"next       {st['next']}")
    catch = st.get("catchup") or {}
    hours = ", ".join(f"{h:02d}:00" for h in catch.get("at", []))
    print(f"catch-up   {'loaded' if catch.get('loaded') else 'not installed'}"
          f" · stale sources at {hours}")
    if st["warning"]:
        print(f"WARNING    {st['warning']}")
    print(f"script     {st['script']}")
    print(f"plist      {st['plist']}")
    if st["last_exit"] is not None:
        print(f"last exit  {st['last_exit']}")
    if st["tail"]:
        print(f"\n# tail of {st['log']}")
        for line in st["tail"].splitlines():
            print(f"  {line}")
    elif st["log"] is None:
        print("log        (nothing yet — it has not run)")
    return 0 if st["loaded"] else 1


def cmd_models(args) -> int:
    cfg, _conn = open_ctx(args)
    if cfg.llm_provider != "openrouter":
        default = llm.PROVIDER_DEFAULT_MODELS[cfg.llm_provider]
        print(f"provider  {cfg.llm_provider}")
        print(f"default   {default}")
        print(f"in use    propose={cfg.propose_model} sweep={cfg.sweep_model} "
              f"match={cfg.match_model}")
        print("\nAny model accepted by that CLI can be selected with `memcal setup --model`. ")
        return 0
    try:
        client = llm.OpenRouter(cfg.api_key)
        rows = client.list_models(args.filter)
    except LLMError as exc:
        print(f"error: {exc}")
        return 1
    for model in sorted(rows, key=lambda m: m["id"])[: args.limit]:
        pricing = model.get("pricing", {})
        print(f"{model['id']:45} ctx {model.get('context_length', 0):>8}  "
              f"in ${float(pricing.get('prompt', 0))*1e6:.2f}/M  "
              f"out ${float(pricing.get('completion', 0))*1e6:.2f}/M")
    print(f"\nin use: propose={cfg.propose_model} sweep={cfg.sweep_model}")
    return 0


# ------------------------------------------------------------------- doctor --
#
# `doctor` printed twenty-six aligned lines mixing facts with verdicts: `wiki pages 14`
# next to `source email -- 9 days behind`, both in the same column, one of them a
# problem and nothing saying so. You had to know what the numbers should be to read it,
# which is exactly backwards — the point of a doctor is to tell somebody who does *not*.
#
# So a check is a value now, not a `print`. It has a section, a verdict, and — the part
# that was missing entirely — the command that fixes it. The renderer groups them, hides
# the healthy detail behind `--verbose`, and ends with a count rather than an exit code
# nobody sees.

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"

#: Not set up, and nothing depends on it. Distinct from `OK` because it is not working,
#: and from `WARN` because there is nothing to do — BlueBubbles being down is fine, since
#: `imessage` falls back to the local chat.db and reads more than BlueBubbles would.
#: Without it an optional-and-off source printed in the default view beside the one real
#: problem, which is how a diagnosis becomes a list again.
SKIP = "skip"


@dataclass
class Finding:
    """One thing doctor looked at.

    `fix` is the whole reason this is a dataclass. Every previous version knew what was
    wrong and told you in the same breath as twenty-five things that were fine, and left
    working out what to type as an exercise. A finding that cannot name its own remedy is
    a finding that should be `INFO`.
    """
    section: str
    name: str
    status: str
    detail: str = ""
    fix: str = ""

    @property
    def bad(self) -> bool:
        return self.status in (WARN, FAIL)


MARKS = {OK: "✓", WARN: "!", FAIL: "✗", INFO: " ", SKIP: "·"}


def _ago(stamp) -> str:
    """`age_phrase` plus "ago", except when the phrase is already an adverb.

    It said "today ago" and "1 day ago" side by side. Small, and it is the sort of thing
    that makes a reader stop trusting the rest of the line.
    """
    phrase = db.age_phrase(stamp)
    return phrase if phrase in ("today", "just now", "never") else f"{phrase} ago"


def _one_line(message: str, width: int = 88) -> str:
    """A connector's complaint, on one line, cut at a space rather than mid-token.

    `message[:58]` produced `URLError contacting http://localhost:1234/…: <urlopen err`,
    which stops exactly where the reason starts.
    """
    text = " ".join(str(message or "").split())
    if len(text) <= width:
        return text
    cut = text.rfind(" ", 0, width)
    return text[:cut if cut > width // 2 else width].rstrip(" ,;:") + "…"

#: Order the sections are printed in — outside-in, which is the order things break in:
#: the store exists, something feeds it, something reads it, something schedules that,
#: and only then is there anything to publish.
SECTIONS = ("Store", "Sources", "Extraction", "Schedule", "Calendar")

#: How much traffic an unnamed handle has to carry before it is worth naming. Below this
#: a handle is a wrong number or a one-off delivery notification, and there are hundreds.
NOISY_HANDLE = 25


def doctor_findings(conn: sqlite3.Connection, cfg: Config, *,
                    home_was_missing: bool = False,
                    brief_was_missing: bool = False) -> list[Finding]:
    """Everything doctor knows, as data. `cmd_doctor` only renders it.

    Split out so `--json` is the same answer rather than a second one, and so a check can
    be tested without capturing stdout — the old version could only be tested by reading
    its printout, so it never was.
    """
    out: list[Finding] = []
    add = lambda *a, **k: out.append(Finding(*a, **k))  # noqa: E731

    # -- Store ---------------------------------------------------------------
    if home_was_missing:
        add("Store", "home", INFO, f"{cfg.home} — created just now, with db, wiki and brief")
    else:
        add("Store", "home", OK, str(cfg.home))
    if brief_was_missing and not home_was_missing:
        add("Store", "brief.md", WARN, "was missing and has been written",
            fix="nothing to do; it is regenerated by every write")

    # `quick_check` rather than `integrity_check`: same answer for anything that matters
    # here and it does not walk every index on a 20k-row archive.
    try:
        verdict = conn.execute("PRAGMA quick_check(1)").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        verdict = str(exc)
    size = cfg.db_path.stat().st_size / 1e6 if cfg.db_path.exists() else 0
    if verdict == "ok":
        add("Store", "database", OK, f"{size:.1f} MB, integrity ok")
    else:
        add("Store", "database", FAIL, f"integrity: {verdict}",
            fix=f"restore one of the backups beside {cfg.db_path}")

    counts = {name: conn.execute(f"SELECT count(*) AS n FROM {name}").fetchone()["n"]
              for name in ("events", "archive", "handles")}
    add("Store", "contents", INFO,
        f"{counts['events']} events · {len(todos.open_items(conn))} open to-dos · "
        f"{len(wiki.list_pages(cfg.wiki_dir))} wiki pages · {counts['archive']} archived lines")

    # Counted by how much traffic each one carries, not by how many rows exist. A phone
    # number seen once is noise and there are always hundreds; a number seen fifty times
    # with no name attached is a person whose every row is filed under a numeral, and
    # bundling by entity cannot join any of them up. Thresholding on the raw total made
    # this warn permanently, which is the same as not warning.
    loud = [row for row in identity.unresolved(conn, limit=10000)
            if (row["count"] or 0) >= NOISY_HANDLE]
    total = len(identity.unresolved(conn, limit=10000))
    if loud:
        top = ", ".join(f"{r['handle']} (x{r['count']})" for r in loud[:3])
        add("Store", "unresolved handles", WARN,
            f"{len(loud)} of {total} carry {NOISY_HANDLE}+ messages and have no name: {top}",
            fix="memcal who             # name them; every row they touch joins up")
    else:
        add("Store", "unresolved handles", OK,
            f"{total}, none carrying {NOISY_HANDLE}+ messages")

    # -- Sources -------------------------------------------------------------
    behind = dict(archive.stale_streams(conn, cfg=cfg))
    fresh = {row["stream"]: row for row in archive.freshness(conn, cfg)}
    for source in sources.all_sources(cfg):
        usable, message = source.check(cfg)
        seen = fresh.get(source.name)
        age = db.age_phrase(seen["newest"]) if seen and seen["newest"] else "never"
        detail = f"{(seen or {}).get('n', 0)} items, last seen {age}"
        if source.name in behind and usable:  # noqa: SIM114 — three distinct verdicts
            # The one that cost nine days: reachable *now*, behind because the only
            # scheduled attempt is at an hour its dependency is not up.
            add("Sources", source.name, FAIL, f"{_ago(seen['newest'])} behind — but reachable right now",
                fix="memcal ingest --stale   # and the 12:00/19:00 catch-up job does this for you")
        elif source.name in behind:
            add("Sources", source.name, FAIL, f"{age} behind — {message[:70]}",
                fix=f"fix the connector above, then `memcal ingest {source.name}`")
        elif not usable:
            status = WARN if source.in_all else SKIP
            add("Sources", source.name, status, _one_line(message),
                fix=f"memcal sources         # what {source.name} still needs"
                    if status == WARN else "")
        else:
            add("Sources", source.name, OK, detail)

    # The pass-level verdict this could not give until `close_collection` rolled the
    # per-source errors up. Nine nightly collections failed to read email and every one
    # of them recorded three cheerful counts and no error at all.
    last = conn.execute(
        "SELECT * FROM collections ORDER BY id DESC LIMIT 1").fetchone()
    if last and last["error"]:
        add("Sources", "last collection", FAIL,
            f"{_ago(last['started_at'])}: {_one_line(str(last['error']))}",
            fix="memcal ingest --stale")
    elif last:
        add("Sources", "last collection", OK,
            f"{_ago(last['started_at'])}, {last['read']} read, "
            f"{last['passed']} passed the gate")

    # -- Extraction ----------------------------------------------------------
    provider_ok, provider_detail = llm.provider_status(cfg)
    add("Extraction", "provider", OK if provider_ok else FAIL,
        f"{cfg.llm_provider} — {provider_detail}",
        fix="" if provider_ok else "memcal setup")
    add("Extraction", "models", INFO,
        f"propose={cfg.propose_model} sweep={cfg.sweep_model}")

    last_run = conn.execute(
        "SELECT * FROM runs WHERE mode <> 'dry-run' ORDER BY id DESC LIMIT 1").fetchone()
    if not last_run:
        add("Extraction", "last dream", FAIL, "never run",
            fix="memcal dream --dry-run   # price it first, then `memcal dream`")
    else:
        started = str(last_run["started_at"])
        # Two days, not one. A job that ran at 03:00 is legitimately "1 day ago" for most
        # of the next day, and a check that calls that broken is a check people learn to
        # ignore — which is how the actually-broken one stayed unnoticed for five days.
        days = (db.today() - db.parse_date(started[:10])).days
        add("Extraction", "last dream", FAIL if days > 2 else OK,
            f"{_ago(started)}, {last_run['diffs']} writes",
            fix="memcal dream" if days > 2 else "")
        if last_run["error"]:
            add("Extraction", "last dream error", WARN, str(last_run["error"])[:90],
                fix="memcal trace            # the prompt, reasoning and reply")

    pending = len(archive.spool_pending(conn, limit=100000))
    add("Extraction", "spool", WARN if pending > 500 else OK,
        f"{pending} line(s) waiting to be read",
        fix="memcal dream" if pending > 500 else "")

    text = brief.render(conn, cfg)
    tokens, cap = brief.approx_tokens(text), cfg.brief_token_cap
    # At the cap the brief is being *trimmed*, which drops the oldest blocks silently.
    add("Extraction", "brief", WARN if tokens >= cap else OK,
        f"~{tokens} tokens of {cap}"
        + (" — at the cap, so blocks are being trimmed out" if tokens >= cap else ""),
        fix="raise brief_token_cap in .env, or close some to-dos" if tokens >= cap else "")
    add("Extraction", "shared prefix", INFO,
        f"~{brief.approx_tokens(propose_stage.build_prefix(conn, cfg))} tokens, cached per run")

    # -- Schedule ------------------------------------------------------------
    state = schedule.status(cfg)
    if not state["installed"]:
        add("Schedule", "nightly", WARN, "not installed",
            fix="memcal schedule install")
    else:
        when = f"{state['at'][0]:02d}:{state['at'][1]:02d}" if state["at"] else "?"
        add("Schedule", "nightly", WARN if state["warning"] else OK,
            state["warning"] or f"{state['label']} daily at {when}, next {state['next']}",
            fix="memcal schedule install" if state["warning"] else "")
    catch = state.get("catchup") or {}
    add("Schedule", "catch-up", OK if catch.get("loaded") else WARN,
        (f"{catch.get('label')} at "
         + ", ".join(f"{h:02d}:00" for h in catch.get("at", []))) if catch.get("loaded")
        else "not installed — a source unreachable at 03:00 waits a whole day",
        fix="" if catch.get("loaded") else "memcal schedule install")

    kind = "nightly" if state["installed"] else "interactive"
    verified = db.get_meta(conn, f"ical.permission.{kind}")
    add("Schedule", "calendar permission", OK if verified else WARN,
        f"{kind} last verified {verified}" if verified else f"{kind} not verified",
        fix="" if verified else "memcal ical setup       # opens macOS consent dialogs")

    # -- Calendar ------------------------------------------------------------
    name = (cfg.publish_calendar or "").strip()
    if not name:
        add("Calendar", "publishing", INFO,
            "off — memcal writes nothing outside itself (invariant 11)")
    else:
        account_ok, account = ical.account_status(cfg)
        add("Calendar", "account", OK if account_ok else FAIL, account[:90],
            fix="" if account_ok else "memcal ical migrate --yes")
        # Rows that should be on the real calendar and are not. This is the check that
        # would have caught the tutoring series publishing without its join link — not by
        # noticing the link, but by noticing the row's published state no longer matches.
        due = [events.Event.from_row(row) for row in conn.execute(
            "SELECT * FROM events WHERE status = 'confirmed' AND date >= ?",
            (db.today().isoformat(),))]
        unpublished = [e for e in due if ical.publishable(e) and not conn.execute(
            "SELECT 1 FROM calendar_items WHERE event_key = ? AND published = 1",
            (e.key,)).fetchone() and not e.series]
        add("Calendar", "published", WARN if unpublished else OK,
            f"{len(unpublished)} confirmed commitment(s) not on '{name}'"
            if unpublished else f"everything confirmed is on '{name}'",
            fix="memcal dream            # publishing runs at the end of a pass"
                if unpublished else "")
    return out


@_closes_direct_connections
def cmd_doctor(args) -> int:
    # open_ctx() creates the home and database as a side effect. Notice the state
    # before opening it so a first `memcal doctor` does not claim the store was
    # already healthy when it was the command that initialized it.
    cfg = config.load(getattr(args, "home", None))
    home_was_missing = not cfg.home.exists()
    brief_was_missing = not cfg.brief_path.exists()
    cfg.ensure_dirs()
    conn = db.open_db(cfg.db_path)
    # Doctor opens directly to inspect pre-initialization state, so register it here.
    _own_connection(args, conn)

    # Doctor stays passive about Contacts and permissions, unlike `memcal init`, but
    # a home it creates must still be usable by the agent. Previously it created the
    # schema and wiki directories while leaving the defining artifact, brief.md, out.
    if brief_was_missing:
        brief.write(conn, cfg)

    found = doctor_findings(conn, cfg, home_was_missing=home_was_missing,
                            brief_was_missing=brief_was_missing)
    if getattr(args, "json", False):
        emit_json([{"section": f.section, "name": f.name, "status": f.status,
                    "detail": f.detail, "fix": f.fix} for f in found])
        return 1 if any(f.bad for f in found) else 0

    problems = [f for f in found if f.bad]
    width = max((len(f.name) for f in found), default=10) + 2
    for section in SECTIONS:
        rows = [f for f in found if f.section == section]
        if not rows:
            continue
        # Quiet by default. Twenty-six lines of "everything is fine" is how the two lines
        # that were not fine went unread for nine days; `--verbose` is for when you want
        # the inventory rather than the diagnosis.
        shown = rows if getattr(args, "verbose", False) else [
            f for f in rows if f.bad or f.status == INFO]
        bad_here = sum(1 for f in rows if f.bad)
        headline = f"{bad_here} problem{'s' if bad_here != 1 else ''}" if bad_here \
            else f"ok ({len(rows)} checks)"
        print(f"\n{section:<14}{headline}")
        for finding in shown:
            print(f"  {MARKS[finding.status]} {finding.name:<{width}}{finding.detail}")
            if finding.fix:
                print(f"    {'':<{width}}→ {finding.fix}")

    if not problems:
        print(f"\nAll {len(found)} checks pass. Nothing to do.")
        return 0
    print(f"\n{len(problems)} problem(s):")
    for finding in problems:
        print(f"  {finding.section.lower()}/{finding.name}: {finding.detail}")
        if finding.fix:
            print(f"      {finding.fix}")
    return 1


def cmd_help(args) -> int:
    """`memcal help <command>`, because that is what people type.

    argparse's own answer to `memcal help week` is "'help' is not a command", and
    "`memcal week --help`" is the kind of thing you know only once you no longer need it.
    """
    parser = build_parser()
    if not args.topic:
        parser.print_help()
        return 0
    child = parser.memcal_subparsers.choices.get(args.topic)
    if not child:
        near = difflib.get_close_matches(args.topic, list(parser.memcal_commands), n=3)
        print(f"memcal: no command called {args.topic!r}."
              + (f" Did you mean: {', '.join(near)}?" if near else ""), file=sys.stderr)
        return 2
    child.print_help()
    return 0


#: One line each, generated from the parser so it cannot drift from what exists. Both
#: reference CLIs ship completion and it is the difference between forty commands being
#: a wall and being a menu — you stop needing to remember any of them.
COMPLETION = {
    "zsh": """\
#compdef memcal
# memcal completion — add to your fpath, or:  memcal completion zsh > ~/.zsh/_memcal
_memcal() {{
  local -a cmds
  cmds=({commands})
  _arguments '1: :->cmd' '*: :->rest'
  case $state in
    cmd) _describe 'command' cmds ;;
  esac
}}
compdef _memcal memcal
""",
    "bash": """\
# memcal completion — source it, or:  memcal completion bash > /etc/bash_completion.d/memcal
_memcal() {{
  local cur="${{COMP_WORDS[COMP_CWORD]}}"
  if [ "$COMP_CWORD" -eq 1 ]; then
    COMPREPLY=( $(compgen -W "{plain}" -- "$cur") )
  fi
}}
complete -F _memcal memcal
""",
}


def cmd_completion(args) -> int:
    parser = build_parser()
    names = [n for n in sorted(parser.memcal_commands) if n not in HIDDEN_COMMANDS]
    helps = getattr(parser, "memcal_help_text", {})
    described = " ".join(
        f"'{n}:{helps.get(n, '').replace(chr(39), '')}'" for n in names)
    print(COMPLETION[args.shell].format(commands=described, plain=" ".join(names)))
    return 0


# -------------------------------------------------------------------- parser --

# Forty-odd subcommands in one flat alphabetical list is how `memcal` with no arguments
# came to answer with a wall of names and an argparse error. Grouping decides order and
# headers only — the help text itself is still whatever add_parser(help=...) says, read
# back out of the parser below. A count is not written down here on purpose: the last one
# said "thirty-seven" for three commands longer than it was true.
COMMAND_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Read it", ("brief", "open", "week", "month", "todos", "search", "ui")),
    ("Write to it", ("remember", "add", "todo", "done", "ask", "answer", "note",
                     "series", "status", "rm")),
    ("Wiki and standing", ("page", "pages", "alias", "merge", "standing", "forget", "who")),
    ("Feed it", ("ingest", "sources", "dream", "review", "schedule")),
    ("The gate", ("gatecheck", "senders", "top", "block")),
    ("Set up and check", ("setup", "init", "openclaw", "doctor", "stats", "trace",
                          "models", "ical", "reminders", "completion")),
)

#: Commands that work and are not listed. Two names for one thing is the shape of a
#: confusing CLI — `ui` and `web` were both in the visible list, doing the same job,
#: with `web` labelled "the same server, under its original name" — so the older name
#: keeps working and stops being a choice anybody has to make.
HIDDEN_COMMANDS = frozenset({"web"})

#: The top of `--help`. A list of forty verbs answers "what is there" and never answers
#: "what do I type", and the second question is the one somebody opening a terminal
#: actually has. Every line here is a real command against a real store.
USAGE_EXAMPLE = """\
start here:

  memcal brief               today and the week around it — every line starts with a handle
  memcal E286                open that handle: the address, the link, the messages it came from
  memcal week                the same window, one row per line
  memcal todos               what is open

  memcal add "Dinner with Sam" fri --time 19:00 --where Lilia
  memcal status E286 confirmed
  memcal done T7

  memcal ingest all          pull every source into the archive
  memcal dream               read what is new and write what it means
  memcal doctor              is any of this working
"""


def _install_grouped_help(p: argparse.ArgumentParser, sub) -> None:
    """Swap argparse's flat command list for the same commands, grouped.

    argparse has no concept of subcommand groups, so the grouped listing goes in the
    epilog and the flat one is dropped. Reading the help strings back out of the
    parser keeps add_parser(help=...) the single place they live; a command nobody
    put in a group still shows up, under "Other", rather than vanishing from --help.
    """
    listing = getattr(sub, "_choices_actions", None)
    if not listing:  # a future argparse without it: keep the flat list over none
        return
    helps = {a.dest: (a.help or "") for a in listing}
    # Stashed before the listing is cleared below. `completion` needs the same strings
    # and reading them off a cleared list is how it shipped describing nothing.
    p.memcal_help_text = helps
    groups = list(COMMAND_GROUPS)
    grouped = {name for _, names in groups for name in names}
    ungrouped = tuple(n for n in sub.choices
                      if n not in grouped and n not in HIDDEN_COMMANDS)
    if ungrouped:
        groups.append(("Other", ungrouped))
    pad = max((len(n) for n in helps), default=0) + 2
    lines = [USAGE_EXAMPLE, "commands:"]
    for title, names in groups:
        lines.append("")
        lines.append(f"  {title}")
        lines.extend(f"    {n:<{pad}}{helps[n]}" for n in names if n in helps)
    lines.append("")
    lines.append("  `memcal help <command>` or `memcal <command> --help` for one command.")
    p.epilog = "\n".join(lines) + "\n"
    p.formatter_class = argparse.RawDescriptionHelpFormatter
    listing.clear()  # the epilog is the listing now; leaving both prints it twice


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="memcal", description="a calendar and a to-do list that live in context")
    p.add_argument("--home", help="memcal home directory (default ~/.memcal)")
    # No subcommand prints the help. It printed the *brief* until 2026-08-20, on the
    # reasoning that the brief is the one file the whole system exists to keep accurate
    # — true, and the wrong thing to do with the only keystroke somebody types before
    # they know anything. `memcal` answered a question nobody had asked yet with two
    # hundred lines, and the answer to "what is this and what do I type" was three
    # commands away. `memcal brief` is still the brief, and `memcal E286` still opens
    # a row, so nothing that knew what it wanted lost anything.
    p.set_defaults(func=cmd_help, topic=None)
    sub = p.add_subparsers(dest="cmd", required=False, metavar="<command>")

    s = sub.add_parser("help", help="help for one command")
    s.add_argument("topic", nargs="?", help="the command to explain")
    s.set_defaults(func=cmd_help)

    s = sub.add_parser("completion", help="print a shell completion script (zsh, bash)")
    s.add_argument("shell", choices=sorted(COMPLETION),
                   help="which shell to write a completion script for")
    s.set_defaults(func=cmd_completion)

    s = sub.add_parser("init", help="set up the db, import contacts, write the brief")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("setup", help="interactively choose the LLM provider and model")
    s.add_argument("--provider", choices=tuple(llm.PROVIDER_DEFAULT_MODELS),
                   help="skip the provider prompt")
    s.add_argument("--model", help="provider-native model id")
    s.add_argument("--api-key", help="OpenRouter only; saved with mode 0600")
    s.set_defaults(func=cmd_setup)

    s = sub.add_parser("openclaw", help="install or inspect the OpenClaw integration")
    s.add_argument("action", nargs="?", default="status", choices=["status", "setup"],
                   help="status, or setup to link the plugin and register MCP")
    s.add_argument("--yes", action="store_true", help="setup without confirmation")
    s.set_defaults(func=cmd_openclaw)

    s = sub.add_parser("open", help="everything known about one handle (E286, T7, Q12)")
    s.add_argument("ref", help="a handle from the brief, `week` or `todos`")
    s.set_defaults(func=cmd_open)

    s = sub.add_parser("brief", help="print brief.md")
    s.add_argument("--write", action="store_true", help="also write it to disk")
    s.add_argument("--tokens", action="store_true", help="report the token count")
    s.set_defaults(func=cmd_brief)

    s = sub.add_parser("week", help="the memcal window")
    s.add_argument("--back", type=int,
                   help="days of history to include (default: days_back)")
    s.add_argument("--forward", type=int,
                   help="days ahead to include (default: days_forward)")
    s.add_argument("--keys", action="store_true",
                   help="also print the store key beside each handle")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_week)

    s = sub.add_parser("month", help="everything for a month (separate from the user calendar)")
    s.add_argument("month", nargs="?", help="yyyy-mm")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_month)

    s = sub.add_parser("add", help="add a memcal row")
    s.add_argument("title", help="what the thing is called")
    s.add_argument("date", nargs="?", help="fri | tomorrow | +3 | 2026-08-01")
    s.add_argument("--time", help="24h clock, e.g. 19:00")
    s.add_argument("--kind", default="commitment",
                   choices=["commitment", "availability", "opportunity", "observed"],
                   help="commitment = you are expected; opportunity = you could")
    s.add_argument("--subject", default="me", help="whose row this is (default: me)")
    s.add_argument("--where",
                   help="the place. A join link goes in the row's join_url, not here")
    s.add_argument("--status", default="mentioned",
                   choices=["mentioned", "tentative", "confirmed", "declined", "happened"],
                   help="how settled it is")
    s.add_argument("--who", nargs="*", help="people coming, space separated")
    s.add_argument("--series", help="slug of the recurring rule this is an occurrence of")
    s.set_defaults(func=cmd_add)

    s = sub.add_parser("status", help="move a row's status")
    s.add_argument("key", help="a handle (E286), a store key, or the title")
    s.add_argument("status", choices=["mentioned", "tentative", "confirmed", "declined", "happened"],
                   help="the status to move it to")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("rm", help="delete a memcal row")
    s.add_argument("key", help="a handle (E286), a store key, or the title")
    s.set_defaults(func=cmd_rm)

    s = sub.add_parser("todo", help="open a to-do")
    s.add_argument("text", help="what needs doing")
    s.add_argument("--who", help="whose to-do it is (default: me)")
    s.add_argument("--due", help="a date: fri | tomorrow | +3 | 2026-08-01")
    s.add_argument("--when", help="wake condition, e.g. 'Rowan is back from Italy'")
    s.add_argument("--event", help="link to an event by key or distinctive words")
    s.add_argument("--remind", nargs="?", const=True, default=None,
                   metavar="WHEN",
                   help="remind me: bare for a sensible hour (09:00 the day before), "
                        "or an explicit ISO datetime")
    s.set_defaults(func=cmd_todo)

    s = sub.add_parser("series", help="read or move a recurring thing's schedule")
    s.add_argument("which", nargs="?", help="the recurring thing, e.g. 'tutoring'")
    s.add_argument("--every", choices=list(series.CADENCES), help="how often")
    s.add_argument("--on", help="day of the week, e.g. tuesday")
    s.add_argument("--day", type=int, help="day of the month, for --every monthly")
    s.add_argument("--at", help="HH:MM")
    s.add_argument("--where", help="where it happens, every time")
    s.add_argument("--join", help="the link you attend through")
    s.add_argument("--starting", help="the day the new schedule begins; "
                                      "omit for 'from now on'")
    s.add_argument("--ended", action="store_true",
                   help="the user has stopped going. Retires the rule; deletes nothing")
    s.set_defaults(func=cmd_series)

    s = sub.add_parser("todos", help="list open to-dos")
    s.add_argument("--keys", action="store_true",
                   help="also print the store key beside each handle")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_todos)

    s = sub.add_parser("done", help="close a to-do (conversational, never inferred)")
    s.add_argument("what", help="a handle (T7), a store key, or words from the to-do")
    s.set_defaults(func=cmd_done)

    s = sub.add_parser("ask", help="add a question to the Ask about block")
    s.add_argument("text", help="the question, as you would ask it out loud")
    s.set_defaults(func=cmd_ask)

    s = sub.add_parser("answer", help="answer an open question")
    s.add_argument("question", help="a handle (Q35), or words from the open question")
    s.add_argument("answer", help="the answer")
    s.set_defaults(func=cmd_answer)

    s = sub.add_parser("standing", help="read or write the standing block")
    s.add_argument("kind", nargs="?", default="all",
                   choices=["all", "identity", "preference", "alias"],
                   help="which slot, or 'all' to list them")
    s.add_argument("value", nargs="?", help="the value to store; omit to read")
    s.add_argument("--permanent", action="store_true", help="keep it beyond this session")
    s.set_defaults(func=cmd_standing)

    s = sub.add_parser("forget", help="drop a standing entry")
    s.add_argument("key", help="a handle (S2) or the key, both printed by `memcal standing`")
    s.set_defaults(func=cmd_forget)

    s = sub.add_parser("page", help="read a wiki page, or fill a slot")
    s.add_argument("slug", help="the page name, as printed by `memcal pages`")
    s.add_argument("slot", nargs="?",
                   help="a named fact on the page; omit to read the whole page")
    s.add_argument("value", nargs="?", help="what to put in the slot")
    s.add_argument("--section", default="people",
                   choices=["people", "places", "projects", "preferences"],
                   help="which directory the page lives in")
    s.set_defaults(func=cmd_page)

    s = sub.add_parser("pages", help="list wiki pages")
    s.set_defaults(func=cmd_pages)

    s = sub.add_parser("alias", help="another name for someone who already has a page")
    s.add_argument("slug", help="the page that already exists")
    s.add_argument("name", nargs="?", help="omit to list the names already on file")
    s.set_defaults(func=cmd_alias)

    s = sub.add_parser("merge", help="fold one wiki page into another (same person)")
    s.add_argument("keep", help="the page that survives")
    s.add_argument("drop", help="the page folded into it")
    s.add_argument("--apply", action="store_true", help="without this, only shows both pages")
    s.set_defaults(func=cmd_merge)

    s = sub.add_parser("search", help="full-text search the archive")
    s.add_argument("query", help="full-text query over every archived line")
    s.add_argument("--limit", type=int, default=20, help="how many lines to print")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("who", help="resolve unknown handles")
    s.add_argument("handle", nargs="?",
                   help="a phone number or address; omit to list what is unresolved")
    s.add_argument("person", nargs="?", help="the name it belongs to")
    s.add_argument("--limit", type=int, default=30,
                   help="how many unresolved handles to print")
    s.add_argument("--adopt", action="store_true",
                   help="link every id whose own platform already told us the name")
    s.set_defaults(func=cmd_who)

    s = sub.add_parser("senders", help="the email gate table")
    s.add_argument("address", nargs="?", help="an email address; omit to list the table")
    s.add_argument("decision", nargs="?", choices=["ignore", "archive", "process"],
                   help="ignore = never again; archive = keep, no model; process = pass the gate")
    s.set_defaults(func=cmd_senders)

    s = sub.add_parser("block", help="never spend a model call on this sender or chat again")
    s.add_argument("target", nargs="?",
                   help="an email address, or stream/thread (e.g. groupme/Dev Chat)")
    s.add_argument("--event", type=int, help="block whatever produced this event id")
    s.add_argument("--reason", default="", help="why, kept on the record")
    s.add_argument("--agent", action="store_true",
                   help="record the agent as the decider rather than you")
    s.set_defaults(func=cmd_block)

    s = sub.add_parser("top", help="top-tier senders always pass the gate")
    s.add_argument("person", nargs="?", help="a name; omit to list the top tier")
    s.add_argument("--remove", action="store_true",
                   help="take them out of the top tier instead")
    s.set_defaults(func=cmd_top)

    s = sub.add_parser("ingest", help="pull a source into the archive")
    s.add_argument("stream", nargs="?", default="all", help="source name, or 'all'")
    s.add_argument("--stale", action="store_true",
                   help="only sources that are behind and reachable right now")
    s.add_argument("--limit", type=int, default=1000, help="items per round")
    s.add_argument("--rounds", type=int, default=sources.DEFAULT_ROUNDS,
                   help="how many rounds to spend catching a stale source up")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("sources", help="list sources and whether each is usable")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_sources)

    s = sub.add_parser("ical", help="check/request macOS Calendar access")
    s.add_argument("action", nargs="?", default="status",
                   choices=["status", "setup", "probe", "account", "migrate"],
                   help="status, or setup to request permission")
    s.add_argument("--result", help=argparse.SUPPRESS)
    s.add_argument("--context", choices=["interactive", "nightly"],
                   default="interactive", help=argparse.SUPPRESS)
    s.add_argument("--yes", action="store_true",
                   help="setup: perform the permission request. "
                        "migrate: actually move the events, rather than say what would move")
    s.set_defaults(func=cmd_ical)

    s = sub.add_parser("reminders", help="check/request macOS Reminders access")
    s.add_argument("action", nargs="?", default="status", choices=["status", "setup"],
                   help="status, or setup to request permission")
    s.add_argument("--yes", action="store_true",
                   help="setup: perform the permission request (opens a macOS dialog)")
    s.set_defaults(func=cmd_reminders)

    s = sub.add_parser("gatecheck", help="what the gate is passing and rejecting")
    s.add_argument("--stream", help="only this source")
    s.add_argument("--limit", type=int, default=40,
                   help="how many recent decisions to show")
    s.set_defaults(func=cmd_gatecheck)

    s = sub.add_parser("dream", help="run the dream pass over everything spooled")
    s.add_argument("--mode", default="ondemand", choices=["nightly", "ondemand", "realtime"],
                   help="nightly = frontier model, whole window; ondemand = cheaper")
    s.add_argument("--model", help="override the propose model")
    s.add_argument("--limit", type=int, default=0,
                   help="items to read this pass (default: item_budget from config)")
    s.add_argument("--rounds", type=int, default=1, metavar="N",
                   help="keep passing until the queue is empty, at most N times — "
                        "what a first load needs, since one pass reads the newest "
                        "lines of each conversation and a 30-day import is several deep")
    s.add_argument("--dry-run", action="store_true", help="bundle and price it, call nothing")
    s.add_argument("--redo", nargs="?", const="all", metavar="SINCE",
                   help="re-read already-processed items ('all', or an ISO date)")
    s.add_argument("--no-sweep", action="store_true",
                   help="skip the final cheap pass over the result")
    s.set_defaults(func=cmd_dream)

    s = sub.add_parser("remember", help="tell memcal something directly (writes immediately)")
    s.add_argument("text", help="what to remember, in prose. Costs a model call")
    s.add_argument("--speaker", default="me", help="who said it (default: me)")
    s.set_defaults(func=cmd_remember)

    s = sub.add_parser("note", help="write one durable fact onto a wiki page (no model)")
    s.add_argument("page", help="who or what, e.g. 'Quinn Brooks'")
    s.add_argument("slot", help="the label, e.g. 'likes'")
    s.add_argument("value", help="the bare answer, e.g. 'Pokemon'")
    s.add_argument("--section", choices=wiki.SECTIONS,
                   help="which directory the page lives in")
    s.set_defaults(func=cmd_note)

    s = sub.add_parser("trace", help="the prompt, reasoning and reply of a real model call")
    s.add_argument("what", nargs="?",
                   help="a run number, a gen-... id, or part of a bundle name; omit to list")
    s.add_argument("--limit", type=int, default=3, help="how many calls to print")
    s.set_defaults(func=cmd_trace)

    s = sub.add_parser("review", help="what it wrote, what it wants to know, what it can't resolve")
    s.add_argument("--limit", type=int, default=12, help="how many recent writes to show")
    s.set_defaults(func=cmd_review)

    s = sub.add_parser("stats", help="post-gate volume and run history")
    s.add_argument("--days", type=int, default=14,
                   help="how far back to count post-gate volume")
    s.add_argument("--runs", type=int, default=5, help="how many dream runs to list")
    s.set_defaults(func=cmd_stats)

    # Two names for one server. `web` is what it was called when it only showed the
    # gate; `ui` is what people reach for now that collect, preview and dream live there
    # too. Both still work — the muscle memory is real — but only `ui` is *listed*: a
    # help page that offers a reader two spellings of one command has asked them to make
    # a choice that does not exist, and "the same server, under its original name" was a
    # line in the visible list explaining why it was not worth reading.
    for name, blurb in (("ui", "open the web UI — collect, preview a dream, run it"),
                        ("web", "alias for `ui`, kept for muscle memory")):
        s = sub.add_parser(name, help=blurb)
        s.add_argument("--port", type=int, default=8765, help="port to serve on")
        s.add_argument("--host", default="127.0.0.1",
                       help="loopback address: localhost, 127.0.0.1, or ::1")
        s.add_argument("--no-open", action="store_true", help="don't open a browser")
        s.set_defaults(func=cmd_web)

    s = sub.add_parser("schedule", help="run the dream pass every night (launchd)")
    s.add_argument("action", nargs="?", default="status",
                   choices=["status", "install", "uninstall", "run"],
                   help="status, install, uninstall, or run it now")
    s.add_argument("--hour", type=int, default=schedule.DEFAULT_HOUR,
                   help="hour of the nightly pass")
    s.add_argument("--minute", type=int, default=schedule.DEFAULT_MINUTE,
                   help="minute of the nightly pass")
    s.set_defaults(func=cmd_schedule)

    s = sub.add_parser("models", help="show models for the configured LLM provider")
    s.add_argument("filter", nargs="?", default="anthropic",
                   help="substring to match against model ids")
    s.add_argument("--limit", type=int, default=40, help="how many to print")
    s.set_defaults(func=cmd_models)

    s = sub.add_parser("doctor", help="find what is wrong, and what to type about it")
    s.add_argument("--verbose", "-v", action="store_true",
                   help="also print the checks that passed")
    s.add_argument("--json", action="store_true", help="machine-readable output")
    s.set_defaults(func=cmd_doctor)
    p.memcal_commands = tuple(sub.choices)  # what main() checks a typo against
    p.memcal_subparsers = sub               # what `help` and `completion` read back out
    _install_grouped_help(p, sub)
    return p


# --home takes a value, and that value is not a command guess.
_VALUE_FLAGS = ("--home",)


def _unknown_command(argv: list[str], choices) -> tuple[str, list[str]] | None:
    """The first bare word, if it isn't a command — with anything it looks like.

    argparse's own answer to a typo is to reprint every choice there is, which is the
    same wall in a worse mood. Naming the near miss is the whole value here.
    """
    skip = False
    for tok in argv:
        if skip:
            skip = False
            continue
        if tok in _VALUE_FLAGS:
            skip = True
            continue
        if tok.startswith("-"):
            continue
        if tok in choices:
            return None
        return tok, difflib.get_close_matches(tok, list(choices), n=3, cutoff=0.6)
    return None


def _expand_bare_handle(argv: list[str], choices) -> list[str]:
    """`memcal E286` means `memcal open E286`.

    You type `memcal brief`, you get the week, and every line of it starts with a handle.
    Making that handle a command in its own right is the shortest path from reading to
    acting, and it is unambiguous: no memcal command is spelled like `E286`, and the
    check is exact rather than a prefix, so a future `export` command is unaffected.
    """
    if argv and argv[0] not in choices and is_handle(argv[0]):
        return ["open", *argv]
    return argv


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    argv = _expand_bare_handle(argv, parser.memcal_commands)
    miss = _unknown_command(argv, parser.memcal_commands)
    if miss:
        name, near = miss
        print(f"memcal: '{name}' is not a command.", file=sys.stderr)
        if near:
            print(f"did you mean: {', '.join(near)}", file=sys.stderr)
        else:
            print("try: memcal --help", file=sys.stderr)
        return 2
    args = parser.parse_args(argv)
    args._owned_connections = []
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except LLMError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        for conn in reversed(args._owned_connections):
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
