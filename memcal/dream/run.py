"""The dream pass, end to end.

One watermark-driven program with three knobs — frequency, model, window. There is
no separate real-time tier: running it every 30 minutes with a cheap model is a cron
change, not a second codebase.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta

from .. import archive, brief, db, events, identity, llm, textclean, threads, todos, wiki
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
                              ("asked", self.questions), ("errors", self.errors)):
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
    conn.commit()
    result = DreamResult(run_id=run_id, bundles=len(bundles),
                         items=sum(len(b.items) for b in bundles), dry_run=dry_run)
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
        # Whether the shared prefix is actually cached is a property of the endpoint,
        # not of the packing. Saying "cached" for a model that has no prompt cache
        # under-reports the bill by the prefix times every request, which on this
        # backlog is most of the input.
        cached = cfg.propose_model not in llm.NO_PROMPT_CACHE
        result.log.append(
            f"{len(bundles)} bundles pack into {len(groups)} request(s); "
            f"shared prefix ~{prefix_tokens} tokens, "
            + ("cached across all of them" if cached
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
            requests=len(groups) * turns, max_parallel=cfg.max_parallel)
        if estimate["priced"]:
            result.log.append(
                f"~${estimate['input']:.4f} input; up to "
                f"${estimate['output_ceiling']:.4f} output at every request ceiling")
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
    #    conversation and says only what that conversation says.
    #
    # On a cold start that is not enough. A first load is 127 bundles against an empty
    # store, every call shares one snapshot taken before any of them ran, and no call can
    # see what any other found — so a plan settled in one thread and referred to in
    # another arrives twice with nothing to join it. Splitting the pass into waves and
    # rebuilding the state between them lets the later waves amend rows the earlier ones
    # wrote, which is what a nightly pass gets for free by running after yesterday's.
    #
    # The order is not arbitrary either: `cold_start_order` reads the people the user actually
    # talks to first, so the first rows on screen are plans rather than receipts, and a
    # pass that dies part-way loses the least valuable half rather than a random one.
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
    # Which conversations were actually read. Tracked separately from `proposals`
    # because a wave applies and discards its own proposals as it goes — leaving the
    # list empty at the end, which the spool-marking below reads as "nothing was read"
    # and re-queues the entire backlog for ever.
    read_entities: set[str] = set()
    for index, batch in enumerate(_split(bundles, waves), start=1):
        if waves > 1:
            emit("propose", "running",
                 f"wave {index} of {waves} · {len(batch)} bundles", wave=index)
        got, problems = propose_stage.propose_all(
            client, conn, cfg, batch, run_id=run_id, progress=track)
        errors.extend(problems)
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
        emit("propose", "running", f"wave {index} wrote {len(log)} row(s)", wave=index)

    result.errors.extend(errors)
    emit("propose", "done" if (proposals or result.diffs) else "failed",
         f"{len(bundles)} bundles reviewed · {len(errors)} issue(s)")

    # 3. merge — the only stage that sees every proposal at once, so it is the only
    #    one that can tell one event mentioned in four threads from four events.
    #    Deterministic clustering; a model is called only where fragments disagree.
    # In wave mode both of these already ran per wave and `proposals` is empty, so these
    # are no-ops — but they must *accumulate* rather than assign, or the last empty pass
    # would erase everything the waves recorded.
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

    # Wake conditions are satisfied by ingestion, not by the model — and never by the
    # very traffic that opened the to-do, which is what `before_apply` rules out.
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
    from .. import series as series_mod                              # noqa: PLC0415
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
