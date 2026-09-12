"""The dream pass, end to end.

One watermark-driven program with three knobs — frequency, model, window. There is
no separate real-time tier: running it every 30 minutes with a cheap model is a cron
change, not a second codebase.
"""

from __future__ import annotations

import contextlib
import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta

from .. import (archive, brief, db, events, identity, llm, pending, textclean,
                threads, todos, wiki)
from ..config import Config
from ..llm import LLMError
from . import apply as apply_stage
from . import bundle as bundle_stage
from . import propose as propose_stage
from . import merge as merge_stage
from . import sweep as sweep_stage


@dataclass
class DreamResult:
    run_id: int
    bundles: int = 0
    items: int = 0
    diffs: int = 0
    log: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    sweep_actions: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    woken: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: Recoveries: work the pass did to route around a problem, having succeeded. A
    #: split-and-resend or a second look belongs here, a timeout or a refusal belongs in
    #: `errors`. Only `errors` reaches `runs.error`, which is the column `memcal doctor`
    #: reads to decide whether the last pass failed.
    notes: list[str] = field(default_factory=list)
    usage_summary: str = ""
    dry_run: bool = False
    nothing_new: bool = False

    def report(self) -> str:
        if self.nothing_new:
            return (f"dream run #{self.run_id} — nothing new since the last pass "
                    f"(ingest first, or use --redo to re-read what was already processed)")
        lines = [f"dream run #{self.run_id} — {self.bundles} bundles, {self.items} items, "
                 f"{self.diffs} writes"]
        lines += [f"  {line}" for line in self.log[:40]]
        if len(self.log) > 40:
            lines.append(f"  … {len(self.log) - 40} more")
        for label, values in (("merged", self.resolved),
                              ("sweep", self.sweep_actions), ("woke", self.woken),
                              ("asked", self.questions), ("note", self.notes),
                              ("errors", self.errors)):
            for value in values:
                lines.append(f"  {label}: {value}")
        if self.usage_summary:
            lines.append(f"  usage: {self.usage_summary}")
        return "\n".join(lines)


def _wave_count(cfg: Config, mode: str, bundles: int) -> int:
    """How many passes to split this run into. One, unless it is a first load.

    A nightly pass is six bundles against a store that already knows last night's, so
    the snapshot every call shares is nearly current and waves would buy nothing but
    latency. A first load is a hundred-plus bundles against an empty store, where that
    same snapshot is empty for all of them — the one situation where reading in stages
    changes the answer rather than the wall clock.
    """
    configured = int(getattr(cfg, "cold_start_waves", 4) or 1)
    if configured <= 1 or mode == "nightly" or bundles < 24:
        return 1
    return max(1, min(configured, bundles // 6))


class _ProposeBar:
    """How far through propose the pass is, in bundles read out of bundles planned."""

    def __init__(self, planned: int):
        self.planned = planned
        self.read = 0

    def see(self, event: str, data: dict) -> dict:
        """One event from propose, with the pass-wide fraction added to it."""
        if event == "propose_wave" and data.get("kind", "main") != "main":
            self.planned += int(data.get("bundles") or 0)
        elif event == "propose_request" and data.get("ok"):
            self.read += int(data.get("bundles") or 0)
        return {**data, "done": min(self.read, self.planned), "total": self.planned}


def _split(items: list, parts: int) -> list[list]:
    """Contiguous chunks, preserving order — the ordering is the point of splitting."""
    if parts <= 1:
        return [items]
    size = -(-len(items) // parts)                     # ceiling, so nothing is stranded
    return [items[i:i + size] for i in range(0, len(items), size)]


def dream(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    mode: str = "nightly",
    model: str | None = None,
    limit: int = 0,
    dry_run: bool = False,
    skip_sweep: bool = False,
    redo: str | None = None,
    progress=None,
) -> DreamResult:
    """Run one pass and record uncaught failures on its run row."""
    opened: list[int] = []
    try:
        return _dream(conn, cfg, opened=opened, mode=mode, model=model, limit=limit,
                      dry_run=dry_run, skip_sweep=skip_sweep, redo=redo,
                      progress=progress)
    except BaseException as exc:
        if opened:
            with contextlib.suppress(sqlite3.Error):
                conn.rollback()
            _finish(conn, opened[0], DreamResult(run_id=opened[0]),
                    error=f"{type(exc).__name__}: {exc}"[:500])
        raise


def _dream(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    opened: list[int],
    mode: str = "nightly",
    model: str | None = None,
    limit: int = 0,
    dry_run: bool = False,
    skip_sweep: bool = False,
    redo: str | None = None,
    progress=None,
) -> DreamResult:
    def emit(stage: str, state: str, note: str = "", **detail) -> None:
        if progress:
            progress("stage", {"stage": stage, "state": state, "note": note, **detail})

    emit("prepare", "running", "retiring stale items and building bundles")
    if model:
        cfg.propose_model = model
    if redo:
        # Un-claim already-processed items so a better model can re-read them. Writes
        # still merge on keys, so a redo corrects rather than duplicates.
        reset = archive.spool_reset(conn, since=redo if redo != "all" else None)
        print(f"redo: released {reset} previously-processed items")

    # A backfill can queue years of mail in one go. Retiring it here rather than at
    # ingest time means an archive spooled by an older build heals itself on the next
    # pass instead of charging for a decade of newsletters.
    cutoff = (db.today() - timedelta(days=archive.SPOOL_HORIZON_DAYS)).isoformat()
    retired = archive.spool_retire(conn, cutoff)
    if retired:
        print(f"retired {retired} spooled items older than {cutoff} (still in the archive)")
    rekeyed = archive.spool_rekey_groups(conn)
    if rekeyed:
        print(f"re-filed {rekeyed} group message(s) that were keyed under one speaker")
    forgotten = identity.forget_bulk_unresolved(conn) + identity.forget_non_people(conn)
    # Anything the platform already named. Free, no model call, and it is the difference
    # between a person's rows bundling together and each one filing under a numeral.
    identity.adopt_platform_names(conn)
    identity.collapse_split_spellings(conn)
    if forgotten:
        print(f"dropped {forgotten} bulk address(es) from the unresolved-handles queue")
    # Conversations first: their names go on the bundles, and a chat the user has muted must
    # drop out of the queue before anything is priced or packed.
    threads.refresh(conn)
    took = threads.apply_platform_mutes(conn, cfg.platform_mute)
    if took:
        print(f"muted {took} chat(s) the platform already had muted"
              f" (platform_mute={cfg.platform_mute})")
    hushed = _drop_muted(conn)
    if hushed:
        print(f"skipped {hushed} item(s) from muted chats (still in the archive)")

    # Put the next occurrence of every standing schedule into the state *before* the
    # conversations that may move or cancel it are bundled. Doing this only after the
    # model ran made a cancelled weekly session look like a brand-new appointment.
    from .. import series as series_mod                              # noqa: PLC0415
    prepared_series = series_mod.roll_forward(conn)

    bundles = bundle_stage.build(conn, limit=limit or cfg.item_budget,
                                 per_entity=cfg.items_per_entity)
    cur = conn.execute(
        "INSERT INTO runs(started_at, mode, model, bundles, items) VALUES(?,?,?,?,?)",
        # A priced-but-not-run pass is recorded, because knowing what a run *would* have
        # cost is the point of it — but it is not a pass, and filed under its own mode it
        # stops reading as one that found nothing.
        (db.now(), "dry-run" if dry_run else mode, cfg.propose_model, len(bundles),
         sum(len(b.items) for b in bundles)),
    )
    run_id = int(cur.lastrowid)
    opened.append(run_id)
    conn.commit()
    for abandoned in _mark_abandoned_runs(conn, run_id):
        print(f"run {abandoned} was left open by a pass that never finished; "
              f"marked abandoned")
    result = DreamResult(run_id=run_id, bundles=len(bundles),
                         items=sum(len(b.items) for b in bundles), dry_run=dry_run)
    result.log.extend(prepared_series)
    emit("prepare", "done", f"{len(bundles)} bundles · {result.items} lines",
         run_id=run_id)

    if not bundles:
        result.nothing_new = True
        _finish(conn, run_id, result)
        brief.write(conn, cfg)
        emit("render", "done", "nothing new; brief refreshed")
        return result

    if dry_run:
        emit("price", "running", "packing requests")
        prefix = propose_stage.build_prefix(conn, cfg)
        groups = propose_stage.pack(cfg, bundles, conn)
        prefix_tokens = textclean.estimate_tokens(prefix)
        # The live path splits cold starts into waves (_wave_count) and rebuilds
        # the prefix once per wave, so each wave pays its own cache writes. Price
        # the run that will actually happen, not a single-wave packing of it.
        waves = _wave_count(cfg, mode, len(bundles))
        # Whether the shared prefix is actually cached is a property of the endpoint,
        # not of the packing. Saying "cached" for a model that has no prompt cache
        # under-reports the bill by the prefix times every request, which on this
        # backlog is most of the input.
        cached = cfg.propose_model not in llm.NO_PROMPT_CACHE
        wave_note = f" in {waves} waves" if waves > 1 else ""
        result.log.append(
            f"{len(bundles)} bundles pack into {len(groups)} request(s){wave_note}; "
            f"shared prefix ~{prefix_tokens} tokens, "
            + ("cached within each wave" if cached and waves > 1
               else "cached across all of them" if cached
               else f"re-sent with each ({cfg.propose_model} has no prompt cache)"))
        total = prefix_tokens * len(groups)
        suffix_total = 0
        for index, group in enumerate(groups, 1):
            size = textclean.estimate_tokens(
                propose_stage.build_suffix(cfg, group, conn))
            suffix_total += size
            total += size
            names = ", ".join(b.label for b in group[:3])
            more = f" +{len(group) - 3} more" if len(group) > 3 else ""
            result.log.append(f"request {index}: {len(group)} bundles, ~{size} tokens  "
                              f"({names}{more})")
        result.log.append(f"~{total} input tokens total, at most {cfg.max_parallel} in flight")
        # Staging multiplies turns, not requests, and the turns share a conversation.
        # Priced conservatively: the bundles are charged again on every turn, because
        # whether a provider's automatic prefix cache covers the *user* message is a
        # property of the endpoint and only the explicit system-block marker is
        # guaranteed. A quote that comes in over is the wrong direction to be wrong in.
        turns = max(1, len(propose_stage.stage_plan(cfg)))
        if turns > 1:
            result.log.append(
                f"{turns} staged turns per request ({cfg.propose_stages}) — "
                f"{len(groups) * turns} model calls over {len(groups)} conversations; "
                f"priced as if the bundles are re-sent each turn")
        estimate = llm.packed_cost(
            cfg.propose_model, prefix_tokens=prefix_tokens,
            suffix_tokens=suffix_total * turns,
            output_tokens=sum(propose_stage.model_ceiling(cfg, group)
                              for group in groups) * turns,
            requests=len(groups) * turns, max_parallel=cfg.max_parallel,
            waves=waves)
        if estimate["priced"]:
            result.log.append(
                f"~${estimate['input']:.4f} input; up to "
                f"${estimate['output_ceiling']:.4f} output at every request ceiling")
        elif str(getattr(cfg, "llm_provider", "")) in llm.PROVIDER_COMMANDS:
            # A CLI backend bills against a subscription, not per token, so there is no
            # per-token price to be missing and saying "add it to llm.PRICES" would be
            # advice for the wrong kind of account. The size still matters — capacity is
            # the scarce thing there — so the token counts above stand and this only
            # names what they do not convert into.
            result.log.append(
                f"no per-token price: {cfg.llm_provider} bills against its subscription, "
                f"so the tokens above are the size of the run and not a bill")
        else:
            # Silence here is the one thing a dry run must never do. `rates()` returns
            # None for a model with no `llm.PRICES` entry and the quote simply vanished
            # — so "price it before you spend" stopped working for exactly the models
            # worth pricing, the new expensive ones. Say so instead.
            result.log.append(
                f"NO PRICE ON FILE for {cfg.propose_model} — this run is unpriced, not "
                f"free. Add it to llm.PRICES (and FLEX_PRICES if its endpoint asks for "
                f"the flex tier) before spending against it")
        _finish(conn, run_id, result)
        emit("price", "done", result.log[-1] if result.log else "priced")
        return result

    try:
        client = llm.client_for(
            cfg, on_retry=lambda note: emit("model", "waiting", note))
    except LLMError as exc:
        result.errors.append(str(exc))
        _finish(conn, run_id, result, error=str(exc))
        emit("propose", "failed", str(exc))
        return result

    # 2. propose — N independent calls sharing one cached prefix. Each reads one
    #    conversation and reports only what that conversation states.
    #
    # Cold starts run in waves ordered by usefulness, so later waves can amend
    # rows earlier waves wrote.
    waves = _wave_count(cfg, mode, len(bundles))
    if waves > 1:
        bundles = bundle_stage.cold_start_order(conn, bundles)
        emit("propose", "running",
             f"reading {len(bundles)} bundles in {waves} waves, most useful first")
    else:
        emit("propose", "running", f"reading {len(bundles)} bundles")

    bar = _ProposeBar(len(bundles))

    def track(event: str, data: dict) -> None:
        """Pass propose's own events through, with the pass-wide fraction added."""
        enriched = bar.see(event, data)
        if progress:
            progress(event, enriched)

    proposals: list = []
    errors: list[str] = []
    notes: list[str] = []
    # Conversations actually read; tracked separately since wave mode applies
    # and discards proposals as it goes.
    read_entities: set[str] = set()
    for index, batch in enumerate(_split(bundles, waves), start=1):
        if waves > 1:
            emit("propose", "running",
                 f"wave {index} of {waves} · {len(batch)} bundles", wave=index)
        got, problems, recovered = propose_stage.propose_all(
            client, conn, cfg, batch, run_id=run_id, progress=track)
        errors.extend(problems)
        notes.extend(recovered)
        read_entities.update(b.entity for b, _d, _g in got)
        if waves == 1:
            proposals.extend(got)
            continue
        # Resolve and write this wave before reading the next, so the next wave's
        # prefix contains these rows and can amend them by key instead of duplicating
        # them. This is the whole reason to run in waves rather than all at once.
        try:
            got, wave_log = merge_stage.merge_all(
                client, cfg, got, conn=conn, run_id=run_id)
            result.resolved.extend(wave_log)
        except LLMError as exc:
            errors.append(f"merge (wave {index}): {exc}")
        counts, log = apply_stage.apply_diffs(
            conn, cfg, got, written_by=f"dream:{mode}", run_id=run_id, stage="propose")
        result.log.extend(log)
        result.diffs += sum(v for k, v in counts.items() if "rejected" not in k)
        # A wave can write the row an earlier wave's cancellation was waiting for.
        if pending.open_items(conn):
            result.log.extend(pending.retry(conn))
        emit("propose", "running", f"wave {index} wrote {len(log)} row(s)", wave=index)

    result.errors.extend(errors)
    result.notes.extend(notes)
    emit("propose", "done" if (proposals or result.diffs) else "failed",
         f"{len(bundles)} bundles reviewed · {len(errors)} issue(s)")

    # 3. merge — the only stage seeing every proposal at once. In wave mode
    # these accumulate rather than assign, since per-wave passes already ran.
    try:
        emit("merge", "running", "joining proposals across conversations")
        proposals, merge_log = merge_stage.merge_all(
            client, cfg, proposals, conn=conn, run_id=run_id)
        result.resolved.extend(merge_log)
        emit("merge", "done", f"{len(result.resolved)} decision(s)")
    except LLMError as exc:
        result.errors.append(f"merge: {exc}")
        emit("merge", "failed", str(exc))

    # 4. apply — deterministic merge on keys
    before_apply = db.now()
    emit("apply", "running", "merging typed diffs")
    counts, log = apply_stage.apply_diffs(conn, cfg, proposals, written_by=f"dream:{mode}",
                                          run_id=run_id, stage="propose")
    result.log.extend(log)
    result.diffs += sum(v for k, v in counts.items() if "rejected" not in k)
    emit("apply", "done", f"{result.diffs} write(s)")

    # Wake conditions are checked against ingested traffic, excluding the
    # traffic that opened the to-do (`before_apply`).
    for todo in todos.check_wakes(conn, bundle_stage.all_text(bundles),
                                  since=before_apply):
        result.woken.append(todo.text)
        todos.ask(conn, f"{todo.text} — {todo.wake_condition} now looks true. Still open?",
                  key=f"q:wake:{todo.key}", about_todo=todo.id, written_by="dream")

    # 5. sweep — one cheap call over the resulting state
    if not skip_sweep:
        try:
            # What it is reviewing, not just that it is reviewing. The stage is a single
            # model call with no fraction to report, so the size of the thing it was
            # handed is the only advance warning of whether this is four seconds or forty.
            emit("sweep", "running",
                 f"reviewing {len(result.log)} write(s) against the whole store")
            _result, actions = sweep_stage.sweep(client, conn, cfg, result.log, run_id=run_id)
            result.sweep_actions = actions
            emit("sweep", "done", f"{len(actions)} action(s)")
        except LLMError as exc:
            result.errors.append(f"sweep: {exc}")
            emit("sweep", "failed", str(exc))
    else:
        emit("sweep", "skipped", "disabled")

    events.mark_past_happened(conn)
    # An observation still waiting for a target. Retried here rather than at the moment
    # it arrived, because the evidence that places it is usually the traffic of the
    # following days — and the bundle that produced it was marked read at the time, so
    # nothing is re-read to get here.
    result.log.extend(pending.retry(conn, ask=lambda text, key: todos.ask(
        conn, text, key=key, written_by="dream")))
    # Nest a row inside the one it happens within, so a weekend with things in it does
    # not read as three unrelated plans on the same days.
    events.link_contained(conn)
    # Anything that reached `confirmed` goes onto the real calendar. After the sweep,
    # because the sweep can still drop a row, and publishing one it is about to delete
    # puts an event on their phone that memcal no longer believes in.
    from ..sources import ical                                      # noqa: PLC0415
    # The rules first, then the rows. A published rule puts its own occurrences on the
    # calendar, and `publish_pending` skips a row a rule already covers — so doing this
    # the other way round writes the Tuesday itself and then writes it again.
    result.log.extend(series_mod.roll_forward(conn))
    result.log.extend(ical.publish_schedules(conn, cfg))
    result.log.extend(ical.publish_pending(conn, cfg))
    # Put each question next to the to-do it is evidence about. Free, and the thing
    # that was missing when the Venmo receipt and "Venmo Emery" sat in two blocks.
    todos.relink_questions(conn)
    # A question nobody engaged with was never going to be answered, and it holds a
    # slot in the brief against one that would have been.
    stale = todos.expire_questions(conn)
    if stale:
        result.log.append(f"expired   {stale} question(s) nobody engaged with")
    result.questions = sweep_stage.reconcile_backward_window(conn, cfg)

    # Only what was actually read. A request that failed — a timeout, a refusal, a reply
    # cut off at its output ceiling — contributes no proposals, and marking its bundles
    # anyway retired the traffic without anyone having looked at it. The queue is the
    # only record that it had not been read, so losing that loses it for good.
    read = read_entities | {b.entity for b, _diff, _gen in proposals}
    unread = [b for b in bundles if b.entity not in read]
    archive.spool_mark(conn, [sid for b in bundles if b.entity in read
                              for sid in b.spool_ids], run_id)
    if unread:
        result.errors.append(
            f"{len(unread)} bundle(s) left queued — no diff came back for them "
            f"({', '.join(b.label for b in unread[:4])}"
            f"{'…' if len(unread) > 4 else ''})")
    if read:
        bundle_stage.set_watermark(conn, max(str(r["ts"]) for b in bundles
                                            if b.entity in read for r in b.items))

    # 6. render. A page is a fact container, not a contact-card placeholder. Earlier
    # builds opened an empty file for everyone standing on an event, then preserved it
    # precisely because they were on the event; the result was dozens of blank pages
    # charged to every prompt forever. Slot, alias, body and recurring-series writes
    # create their own pages at the moment there is something to put on them.
    emit("render", "running", "pruning empty pages and writing the brief")
    gone = wiki.prune_empty(cfg.wiki_dir)
    if gone:
        result.log.append(f"pages     pruned {len(gone)} empty ({', '.join(gone[:8])}…)")
    for series in wiki.link_series(conn, cfg.wiki_dir):
        result.log.append(f"series    {series}")
    retired = wiki.retire_obsolete_series(conn, cfg.wiki_dir)
    if retired:
        result.log.append(f"series    retired stale pages: {', '.join(retired[:8])}")
    brief.write(conn, cfg)
    result.usage_summary = client.usage.summary()
    _finish(conn, run_id, result, usage=client.usage)
    emit("render", "done", "brief is current")
    return result


def _drop_muted(conn: sqlite3.Connection) -> int:
    """Retire anything queued from a chat the user has muted, before it is priced.

    Muting has to act on the backlog as well as the future, or the decision reads as
    having done nothing: the dev chat the user just silenced still shows up in the next pass
    with ninety-six lines in it.
    """
    cur = conn.execute(
        """UPDATE spool SET processed_at = ?
            WHERE processed_at IS NULL AND archive_id IN
              (SELECT a.id FROM archive a JOIN threads t
                 ON t.stream = a.stream AND t.thread = a.thread
                WHERE t.decision = 'mute')""", (db.now(),))
    conn.commit()
    return cur.rowcount


def _mark_abandoned_runs(conn: sqlite3.Connection, current: int) -> list[int]:
    """Mark older unfinished run rows as abandoned."""
    rows = conn.execute(
        "SELECT id FROM runs WHERE id < ? AND finished_at IS NULL AND error IS NULL",
        (current,)).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE runs SET error = ? WHERE id = ?",
            ("abandoned — the pass never wrote a finish", row["id"]))
    if rows:
        conn.commit()
    return [row["id"] for row in rows]


def _finish(conn: sqlite3.Connection, run_id: int, result: DreamResult,
            *, usage=None, error: str | None = None) -> None:
    conn.execute(
        """UPDATE runs SET finished_at = ?, diffs = ?, prompt_tokens = ?, completion_tokens = ?,
                           cached_tokens = ?, cost_usd = ?, requests = ?,
                           failed_calls = ?, wait_seconds = ?, error = ? WHERE id = ?""",
        (db.now(), result.diffs,
         getattr(usage, "prompt_tokens", 0), getattr(usage, "completion_tokens", 0),
         getattr(usage, "cached_tokens", 0), getattr(usage, "cost", 0.0),
         # What the pass spent that no completion accounts for. Run 13 made ~76 requests
         # over 56 minutes and wrote zeroes into every other column on this row, so the
         # only honest reading of it was that nothing had happened.
         getattr(usage, "requests", 0), getattr(usage, "failed", 0),
         round(getattr(usage, "waited", 0.0), 1),
         error or ("; ".join(result.errors) if result.errors else None), run_id),
    )
    conn.commit()
