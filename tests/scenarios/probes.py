"""Adversarial benchmark suites that do not need a paid model call."""

from __future__ import annotations

import importlib.util
import email
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from memcal import archive, brief, db, events, gate, identity, llm, series, todos, trace, wiki
from memcal.config import Config
from memcal.dream import apply as apply_stage
from memcal.dream import bundle as bundle_stage
from memcal.dream import propose
from memcal.dream import sweep
from memcal.sources import base
from memcal.sources import proton


def result(check_id: str, challenge: str, ok: bool, note: str, *,
           frontier: bool = False, soft: bool = False) -> dict:
    return {
        "id": check_id,
        "challenge": challenge,
        "day": 0,
        "ok": bool(ok),
        "soft": soft,
        "frontier": frontier,
        "note": str(note)[:200],
    }


def _empty_diff(**values) -> dict:
    return {
        "events": [], "todos": [], "wiki": [], "standing": [], "questions": [],
        **values,
    }


def contract_checks() -> list[dict]:
    """Saved hostile model replies through the real JSON parser and v2 router."""
    bundles = [
        bundle_stage.Bundle(entity="person:Alpha", title="Alpha"),
        bundle_stage.Bundle(entity="person:Beta", title="Beta"),
        bundle_stage.Bundle(entity="thread:groupme:gamma", title="Gamma"),
    ]
    ids = [propose.bundle_id(item.entity) for item in bundles]
    checks: list[dict] = []

    raw = "```json\n" + json.dumps({"reviewed": ids, "diffs": []}) + "\n```"
    payload = llm._parse_json(raw)  # benchmark the same forgiving parser production uses
    errors: list[str] = []
    routed, _ = propose._route_v2(bundles, payload, errors)
    checks.append(result(
        "contract.fenced-empty", "response contract",
        len(routed) == 3 and not errors,
        f"routed={len(routed)} errors={errors}"))

    duplicate = {
        "reviewed": ids,
        "diffs": [
            {"bundle": ids[0], **_empty_diff(events=[{"title": "one"}])},
            {"bundle": ids[0], **_empty_diff(events=[{"title": "two"}])},
        ],
    }
    errors = []
    routed, _ = propose._route_v2(bundles, duplicate, errors)
    alpha = next((diff for bundle, diff in routed if bundle.entity == "person:Alpha"), {})
    checks.append(result(
        "contract.duplicate-id-merges", "response contract",
        [row.get("title") for row in alpha.get("events", [])] == ["one", "two"],
        f"alpha events={alpha.get('events', [])} errors={errors}"))

    errors = []
    routed, _ = propose._route_v2(
        bundles,
        {"reviewed": ids, "diffs": [
            {"bundle": "ffffff", **_empty_diff(events=[{"title": "wrong person"}])},
        ]},
        errors,
    )
    checks.append(result(
        "contract.unknown-never-guessed", "response contract",
        all(not diff["events"] for _bundle, diff in routed)
        and any("unknown" in error for error in errors),
        f"routed={[(b.entity, d['events']) for b, d in routed]} errors={errors}"))

    malformed = llm._parse_json('{"reviewed": ["abc123"], "diffs": [')
    checks.append(result(
        "contract.truncated-json-not-partial", "response contract",
        malformed is None, f"parsed={malformed!r}"))

    # Schema-capable endpoints prevent this. Prompt-only endpoints and old saved calls
    # can still return it, and the router currently makes an omitted `reviewed` list
    # indistinguishable from a valid request which reviewed nothing.
    errors = []
    routed, _ = propose._route_v2(bundles, {"diffs": []}, errors)
    checks.append(result(
        "contract.missing-reviewed-diagnosed", "response contract",
        bool(errors) and not routed,
        f"routed={len(routed)} errors={errors or '(none)'}",
        frontier=True))

    class TruncatingClient:
        def complete(self, **_kwargs):
            return llm.Reply(text="", data=None, finish_reason="length")

    cfg = Config(home=Path("/tmp/memcal-contract-probe"))
    try:
        propose.propose_group(TruncatingClient(), cfg, "prefix", [bundles[0]], suffix="x")
        raised = False
    except propose.Truncated:
        raised = True
    checks.append(result(
        "contract.length-never-marks-read", "response contract",
        raised, f"Truncated raised={raised}"))
    return checks


def _deliver(conn, cfg, *, external_id: str, ts: str, text: str, thread: str,
             person: str = "Tester", verdict: gate.Verdict | None = None) -> int | None:
    report = base.IngestReport.opened("imessage", cfg)
    return base.deliver(
        conn, report, stream="imessage", external_id=external_id, ts=ts, text=text,
        thread=thread, person=person, handle="+19175550999", from_me=False,
        verdict=verdict, is_group=True,
    )


def boundary_checks(home: Path) -> list[dict]:
    """Calendar edges, stale evidence and long-gap context on an isolated store."""
    cfg = Config(home=home)
    cfg.ensure_dirs()
    conn = db.open_db(cfg.db_path)
    identity.link(conn, "+19175550999", "Tester", source="benchmark")
    checks: list[dict] = []
    try:
        db.set_today(date(2027, 3, 13))  # Saturday before US daylight saving starts.

        # A source timestamp with an explicit offset must render in the user's local
        # timezone. At present parse_ts preserves +00:00, so this exposes the gap.
        _deliver(
            conn, cfg, external_id="tz-1", ts="2027-03-14T07:30:00+00:00",
            text="Flight check-in opens today at 3:30 AM local time.",
            thread="timezone", verdict=gate.Verdict(True, "temporal"))

        # The old address is outside add_thread_context's ±120 minute window. The reply
        # eleven days later is intentionally too terse to stand alone.
        _deliver(
            conn, cfg, external_id="late-1", ts="2027-03-02T10:00:00-05:00",
            text="Robbie's new address is 14 Example Avenue.",
            thread="late reply", verdict=gate.Verdict(False, "no-signal"))
        _deliver(
            conn, cfg, external_id="late-2", ts="2027-03-13T10:00:00-05:00",
            text="Are we still meeting there tomorrow at 8?",
            thread="late reply", verdict=gate.Verdict(True, "question"))

        bundles = bundle_stage.build(conn, limit=100, per_entity=100)
        timezone_bundle = next(b for b in bundles if b.entity.endswith(":timezone"))
        late_bundle = next(b for b in bundles if b.entity.endswith(":late reply"))
        rendered_tz = timezone_bundle.render()
        rendered_late = late_bundle.render()
        checks.append(result(
            "boundary.utc-renders-local", "timezone boundary",
            "03:30" in rendered_tz and "07:30" not in rendered_tz,
            rendered_tz.replace("\n", " | "), frontier=True))
        checks.append(result(
            "boundary.late-reply-has-referent", "long-gap context",
            "14 Example" in rendered_late,
            rendered_late.replace("\n", " | "), frontier=True))
        checks.append(result(
            "boundary.new-vs-context-labelled", "long-gap context",
            "[new]" in rendered_late.casefold() and "[context]" in rendered_late.casefold(),
            rendered_late.replace("\n", " | "), frontier=True))

        # A source that no longer exists must stop reporting itself stale, and a source
        # that does exist must keep doing it. `freshness()` built its list of streams
        # from records a source leaves behind — archive rows and
        # `source.<stream>.last_success` — and both outlive deletion, so removing a
        # source could never remove its alarm. Find My was deleted on 2026-08-02 and its
        # meta key kept telling the agent "this week may be incomplete" for nine days.
        # The decoy is the half that matters: silencing the whole line would pass a
        # check that only asked about findmy.
        db.set_meta(conn, "source.findmy.last_success", "2027-03-01T09:00:00-05:00")
        db.set_meta(conn, "source.whatsapp.last_success", "2027-03-01T09:00:00-05:00")
        stale_brief = brief.render(conn, cfg, ref=date(2027, 3, 13))
        stale_line = next((line for line in stale_brief.splitlines()
                           if line.startswith("[STALE")), "no stale line")
        checks.append(result(
            "brief.no-unregistered-stream", "deleted source",
            "findmy" not in stale_brief.casefold(), stale_line))
        checks.append(result(
            "brief.registered-stream-still-warns", "deleted source",
            "whatsapp" in stale_brief.casefold(), stale_line))

        start, _ = db.parse_when("tomorrow", ref=date(2026, 12, 31))
        next_friday, _ = db.parse_when("next friday", ref=date(2026, 12, 31))
        checks.append(result(
            "boundary.new-year-tomorrow", "calendar boundary",
            start.isoformat() == "2027-01-01", f"resolved={start}"))
        checks.append(result(
            "boundary.next-weekday-over-year", "calendar boundary",
            next_friday.isoformat() == "2027-01-08", f"resolved={next_friday}"))

        # A miniature two-day pass straddling New Year's: the source says "tomorrow",
        # then the next day changes only the time. This is the same establish/update
        # shape as the main corpus, run through apply with real archive evidence.
        db.set_today(date(2026, 12, 31))
        first_id = _deliver(
            conn, cfg, external_id="newyear-1",
            ts="2026-12-31T23:58:00-05:00",
            text="New Year's brunch is tomorrow at noon at my place.",
            thread="new year", verdict=gate.Verdict(True, "temporal"))
        first_row = conn.execute("SELECT * FROM archive WHERE id = ?", (first_id,)).fetchone()
        first_bundle = bundle_stage.Bundle(
            entity="thread:imessage:new year", title="New year",
            items=[first_row], spool_ids=[])
        apply_stage.apply_diffs(
            conn, cfg, [(first_bundle, _empty_diff(events=[{
                "title": "New Year's brunch", "date": "2027-01-01", "time": "12:00",
                "location": "Tester's place", "participants": ["Tester"],
                "status": "confirmed", "subject": "me",
            }]))], written_by="dream:integration-day1")
        db.set_today(date(2027, 1, 1))
        second_id = _deliver(
            conn, cfg, external_id="newyear-2",
            ts="2027-01-01T09:00:00-05:00",
            text="Can we make New Year's brunch 1 instead of noon?",
            thread="new year", verdict=gate.Verdict(True, "question"))
        second_row = conn.execute("SELECT * FROM archive WHERE id = ?", (second_id,)).fetchone()
        second_bundle = bundle_stage.Bundle(
            entity="thread:imessage:new year", title="New year",
            items=[second_row], spool_ids=[])
        apply_stage.apply_diffs(
            conn, cfg, [(second_bundle, _empty_diff(events=[{
                "title": "New Year's brunch", "date": "2027-01-01", "time": "13:00",
                "location": "Tester's place", "participants": ["Tester"],
                "status": "confirmed", "subject": "me",
            }]))], written_by="dream:integration-day2")
        new_year_rows = [
            events.Event.from_row(row) for row in conn.execute(
                "SELECT * FROM events WHERE title LIKE '%brunch%'")]
        history = (events.history(conn, new_year_rows[0].id)
                   if len(new_year_rows) == 1 else [])
        sources = (trace.source_rows(conn, "event", new_year_rows[0].key)
                   if len(new_year_rows) == 1 else [])
        checks.append(result(
            "boundary.cross-year-update", "calendar boundary",
            len(new_year_rows) == 1 and new_year_rows[0].date == "2027-01-01"
            and new_year_rows[0].time == "13:00"
            and any(row["field"] == "time" for row in history),
            f"rows={[(r.date, r.time) for r in new_year_rows]}; "
            f"history={[(r['field'], r['old_value'], r['new_value']) for r in history]}"))
        evidence = [row["text"] for row in sources if row["evidence"]]
        checks.append(result(
            "boundary.cross-year-sources", "calendar boundary",
            len(evidence) == 2 and any("tomorrow at noon" in text for text in evidence)
            and any("1 instead of noon" in text for text in evidence),
            f"evidence={evidence}"))

        source = late_bundle
        counts, _ = apply_stage.apply_diffs(
            conn, cfg, [(source, _empty_diff(events=[{
                "title": "Far future conference", "date": "2027-05-20",
                "kind": "opportunity", "status": "mentioned", "subject": "me",
            }]))],
            written_by="dream:benchmark")
        far = conn.execute(
            "SELECT count(*) AS n FROM events WHERE title = 'Far future conference'"
        ).fetchone()["n"]
        checks.append(result(
            "boundary.horizon-refuses-future", "evidence horizon",
            far == 0 and counts["event:rejected-stale"] == 1,
            f"rows={far} counts={dict(counts)}"))

        # A span crossing the year boundary remains visible in its middle.
        db.set_today(date(2026, 12, 31))
        event, _ = events.upsert(conn, {
            "title": "New Year cabin trip", "date": "2027-01-01",
            "until": "2027-01-03", "status": "confirmed"})
        db.set_today(date(2027, 1, 2))
        visible = {row.key for row in events.window(conn, 0, 0)}
        checks.append(result(
            "boundary.span-visible-midway", "calendar boundary",
            event.key in visible, f"visible={sorted(visible)}"))

        # Older evidence arriving late must not overwrite a decision made yesterday.
        db.set_today(date(2027, 3, 13))
        current, _ = events.upsert(conn, {
            "title": "Saturday game", "date": "2027-03-20",
            "location": "77 Oak Ave", "participants": ["Tester"],
            "status": "confirmed"}, written_by="live")
        conn.execute(
            "UPDATE events SET updated_at = ? WHERE id = ?",
            ("2027-03-12T12:00:00-05:00", current.id))
        conn.commit()
        stale_id = _deliver(
            conn, cfg, external_id="stale-1", ts="2027-03-01T09:00:00-05:00",
            text="Saturday game will be at 12 Elm St.", thread="stale evidence",
            verdict=gate.Verdict(True, "temporal"))
        stale_row = conn.execute("SELECT * FROM archive WHERE id = ?", (stale_id,)).fetchone()
        stale_bundle = bundle_stage.Bundle(
            entity="person:Tester", title="Tester", items=[stale_row], spool_ids=[])
        apply_stage.apply_diffs(
            conn, cfg, [(stale_bundle, _empty_diff(events=[{
                "title": "Saturday game", "date": "2027-03-20",
                "location": "12 Elm St", "participants": ["Tester"],
                "status": "confirmed", "subject": "me",
            }]))],
            written_by="dream:realtime")
        current = events.get_by_id(conn, current.id)
        checks.append(result(
            "boundary.old-evidence-cannot-overwrite", "evidence ordering",
            current.location == "77 Oak Ave", f"location={current.location!r}"))

        # A payment source may settle a matching obligation, but the transfer itself is
        # not a calendar event and the closed work must disappear from the brief.
        todo, _ = todos.open_todo(
            conn, "Pay Quinn $10 for their server", written_by="live")
        report = base.IngestReport.opened("venmo", cfg)
        payment_id = base.deliver(
            conn, report, stream="venmo", external_id="venmo-1",
            ts="2027-03-13T15:00:00-05:00",
            text="You paid Quinn Brooks $10 for their server.",
            thread="quinn", person="Quinn Brooks",
            verdict=gate.Verdict(True, "transaction"))
        payment_row = conn.execute(
            "SELECT * FROM archive WHERE id = ?", (payment_id,)).fetchone()
        payment_bundle = bundle_stage.Bundle(
            entity="person:Quinn Brooks", title="Quinn Brooks",
            items=[payment_row], spool_ids=[])
        apply_stage.apply_diffs(
            conn, cfg, [(payment_bundle, _empty_diff(
                events=[{
                    "title": "Paid Quinn $10", "date": "2027-03-13",
                    "kind": "observed", "status": "happened", "subject": "me",
                    "participants": ["Quinn Brooks"],
                }],
                todos=[{"op": "close", "key": todo.key, "text": todo.text}],
            ))], written_by="dream:integration")
        settled = todos.get(conn, todo.key)
        payment_events = conn.execute(
            "SELECT count(*) AS n FROM events WHERE title LIKE 'Paid Quinn%'"
        ).fetchone()["n"]
        payment_brief = brief.render(conn, cfg)
        payment_sources = trace.source_rows(conn, "todo", todo.key)
        payment_evidence = [row["text"] for row in payment_sources if row["evidence"]]
        checks.append(result(
            "transaction.closes-matching-todo", "transaction lifecycle",
            bool(settled and settled.status == "closed"),
            f"status={settled.status if settled else '(missing)'}"))
        checks.append(result(
            "transaction.no-calendar-ledger", "transaction lifecycle",
            payment_events == 0, f"payment events={payment_events}"))
        checks.append(result(
            "transaction.resolved-disappears", "transaction lifecycle",
            "Pay Quinn" not in payment_brief and "$10" not in payment_brief,
            f"brief contains payment={'Pay Quinn' in payment_brief or '$10' in payment_brief}"))
        checks.append(result(
            "transaction.source-survives", "transaction lifecycle",
            payment_evidence == ["You paid Quinn Brooks $10 for their server."],
            f"evidence={payment_evidence}"))

        # This is an explicit frontier question: the store currently unions people and
        # cannot represent "Cameron is out" as removal.
        roster, _ = events.upsert(conn, {
            "title": "Roster dinner", "date": "2027-03-21",
            "participants": ["Cameron", "Riley"], "status": "confirmed"})
        roster, _ = events.upsert(conn, {
            "key": roster.key, "title": roster.title, "date": roster.date,
            "participants": ["Riley"]})
        checks.append(result(
            "boundary.participant-removal", "participant removal",
            "Cameron" not in roster.participants,
            f"participants={roster.participants}", frontier=True))

        # Two unrelated conversations with the same organization must not become one
        # giant sender bucket; a reply should still join its own root.
        class Mailbox:
            bodies = {
                1: "Can we meet Tuesday at 10 about the trust?",
                2: "Tuesday at 10 works.",
                3: "Your 2026 tax forms are attached.",
            }

            def body(self, uid):
                return self.bodies[uid]

        def message(message_id, subject, when, *, reply_to=""):
            headers = [
                f"Message-ID: <{message_id}>",
                f"Date: {when}",
                "From: Advisor <advisor@example.com>",
                "To: Casey <casey@example.com>",
                f"Subject: {subject}",
            ]
            if reply_to:
                headers.extend([
                    f"In-Reply-To: <{reply_to}>",
                    f"References: <{reply_to}>",
                ])
            return email.message_from_string("\n".join(headers) + "\n\nbody")

        report = base.IngestReport.opened("email", cfg)
        mailbox = Mailbox()
        proton._handle_message(
            conn, cfg, report, mailbox, 1,
            message("trust-1@example.com", "Trust meeting Tuesday",
                    "Sat, 13 Mar 2027 10:00:00 -0500"),
            "INBOX")
        proton._handle_message(
            conn, cfg, report, mailbox, 2,
            message("trust-2@example.com", "Re: Trust meeting Tuesday",
                    "Sat, 13 Mar 2027 10:05:00 -0500",
                    reply_to="trust-1@example.com"),
            "INBOX")
        proton._handle_message(
            conn, cfg, report, mailbox, 3,
            message("tax-1@example.com", "Your 2026 tax forms",
                    "Sat, 13 Mar 2027 10:10:00 -0500"),
            "INBOX")
        mail_rows = conn.execute(
            "SELECT text, thread FROM archive WHERE handle = 'advisor@example.com'"
            " ORDER BY id").fetchall()
        threads = [row["thread"] for row in mail_rows]
        checks.append(result(
            "boundary.email-thread-identity", "email thread identity",
            len(threads) == 3 and threads[0] == threads[1] and threads[2] != threads[0],
            f"threads={threads}", frontier=True))

        # A calendar row carries no participants — `ical` writes none — so
        # `find_match`'s tier 1 (overlapping participants *and* a similar title) can
        # never fire for one. Only tier 2, an exact title slug, can join it. So a model
        # that writes a *better* title than the calendar's gets a duplicate: gpt-5.6-terra
        # wrote "Jack's 30th birthday" beside the feed's "Jack's 30th" in all three
        # trials, and one duplicate row cost five separate checks. Writing a richer
        # title should not be punished.
        events.upsert(conn, {
            "title": "Jack's 30th", "date": "2027-04-10", "kind": "opportunity",
            "status": "mentioned", "source": "ical:subscribed:Partiful"},
            written_by="ical")
        events.upsert(conn, {
            "title": "Jack's 30th birthday", "date": "2027-04-10",
            "kind": "commitment", "status": "confirmed", "participants": ["Tester"]},
            written_by="dream:benchmark")
        birthday = conn.execute(
            "SELECT count(*) AS n FROM events WHERE title LIKE \"%30th%\"").fetchone()["n"]
        checks.append(result(
            "boundary.calendar-row-joins-a-richer-title", "participant-less matching",
            birthday == 1, f"{birthday} rows for one party", frontier=True))

        # The same shape with no calendar anywhere near it, which is where the fix above
        # does not reach. Beat 7: a group chat writes the row, and the next day a
        # Partiful *email* moves it to 4pm. Neither is a feed row, neither carries the
        # other's guests, and the slugs differ — so nothing joins them. Both models
        # produce it: 'BBQ' beside "Devon's Block Party BBQ" on 2026-08-15, in three of
        # six trials, costing `bbq.one-row` and `bbq.moved` each time.
        events.upsert(conn, {
            "title": "BBQ", "date": "2027-04-17", "time": "14:00",
            "kind": "commitment", "status": "mentioned"}, written_by="dream:nightly")
        events.upsert(conn, {
            "title": "Devon's Block Party BBQ", "date": "2027-04-17", "time": "16:00",
            "kind": "commitment", "status": "confirmed"}, written_by="dream:nightly")
        bbq = [events.Event.from_row(r) for r in conn.execute(
            "SELECT * FROM events WHERE date = '2027-04-17' ORDER BY id")]
        checks.append(result(
            "boundary.two-conversation-rows-are-one-occasion", "participant-less matching",
            len(bbq) == 1 and bbq[0].time == "16:00",
            "; ".join(f"{e.title!r} at {e.time}" for e in bbq) or "no rows",
            frontier=True))

        # The other direction, which is the one `ical` takes: the conversation gets
        # there first with the longer name, and the feed arrives afterwards with the
        # short one. `_calendar_stub` reads the *existing* row, so this side was never
        # covered — and it is the ordinary order of events for a party somebody
        # mentions before the invitation lands.
        events.upsert(conn, {
            "title": "Rae's housewarming party", "date": "2027-04-24",
            "kind": "commitment", "status": "confirmed", "participants": ["Rae"]},
            written_by="dream:nightly")
        events.upsert(conn, {
            "title": "Rae's housewarming", "date": "2027-04-24", "kind": "opportunity",
            "status": "mentioned", "source": "ical:subscribed:Partiful"},
            written_by="ical")
        warming = conn.execute(
            "SELECT count(*) AS n FROM events WHERE title LIKE '%housewarming%'"
        ).fetchone()["n"]
        checks.append(result(
            "boundary.a-feed-row-joins-a-richer-conversation-row",
            "participant-less matching",
            warming == 1, f"{warming} rows for one housewarming", frontier=True))

        # And the refusal that keeps the two above safe. Two rows on one day that a
        # shorter title could equally name are not one row and not a choice — SQLite's
        # row order would decide it. Leaving a duplicate is recoverable; renaming a
        # correct row is not.
        for title in ("Superman movie", "Movie with Riley"):
            events.upsert(conn, {"title": title, "date": "2027-05-01",
                                 "status": "confirmed"}, written_by="dream:nightly")
        events.upsert(conn, {"title": "Movie", "date": "2027-05-01",
                             "status": "confirmed"}, written_by="dream:nightly")
        ambiguous = [r["title"] for r in conn.execute(
            "SELECT title FROM events WHERE date = '2027-05-01' ORDER BY id")]
        checks.append(result(
            "boundary.an-ambiguous-title-joins-nothing", "participant-less matching",
            ambiguous == ["Superman movie", "Movie with Riley", "Movie"],
            f"rows = {ambiguous}"))

        # 02:30 never occurs in New York on 2027-03-14. Memcal has no timezone field or
        # invalid-local-time representation yet, so accepting it silently is a gap.
        dst, _ = events.upsert(conn, {
            "title": "DST pickup", "date": "2027-03-14", "time": "02:30",
            "status": "confirmed"})
        checks.append(result(
            "boundary.nonexistent-local-time", "timezone boundary",
            dst.time != "02:30", f"stored time={dst.time!r}", frontier=True))

        # -- A pass in which one source failed is a failed pass.
        #
        # Nine consecutive nightly runs could not reach the Proton Bridge. Each wrote
        # the reason into `collection_sources`, each closed `collections` with a clean
        # error column and three cheerful counts, `ingest all` exited 0 because its
        # failure branch was `failed and len(chosen) == 1`, and `nightly.log` ended
        # every one of them with "done". Email was nine days stale before anybody asked
        # a question that needed it. The detail was never missing; the *summary*
        # disagreed with it, which is the only reading a cron wrapper ever gets.
        broken = archive.open_collection(conn, mode="benchmark")
        archive.record_source(conn, broken, base.IngestReport(
            stream="email", error="cannot reach Proton Bridge at 127.0.0.1:1143"))
        archive.record_source(conn, broken, base.IngestReport(stream="imessage", read=8))
        archive.close_collection(conn, broken)
        rolled = conn.execute("SELECT error FROM collections WHERE id = ?",
                              (broken,)).fetchone()["error"]
        checks.append(result(
            "collect.a-failed-source-fails-the-pass", "a pass that reported success",
            bool(rolled) and "email" in rolled, f"collections.error = {rolled!r}"))

        # The decoy. If every pass carries an error the column says nothing, and the
        # next person to read it learns to ignore it — which is where this started.
        clean = archive.open_collection(conn, mode="benchmark")
        archive.record_source(conn, clean, base.IngestReport(stream="imessage", read=3))
        archive.close_collection(conn, clean)
        quiet = conn.execute("SELECT error FROM collections WHERE id = ?",
                             (clean,)).fetchone()["error"]
        checks.append(result(
            "collect.a-clean-pass-stays-clean", "a pass that reported success",
            not quiet, f"collections.error = {quiet!r}"))

        # -- A numeral is not a name.
        #
        # 247 handles unresolved, 47 of them carrying 25+ messages each. Bundling is by
        # entity, so every one was a person whose rows filed under a number and joined up
        # with nothing — and GroupMe had been supplying the display name the whole time.
        # Only `link_by_name`'s exact-Contacts match ever consumed it, so a roster of
        # people the user had never saved stayed anonymous for ever.
        identity.link(conn, "+19175559001", "Joe Coleman", source="contacts")
        identity.note_unresolved(conn, "groupme:6014661", "groupme",
                                 seen_name="Joe Navarro")
        identity.note_unresolved(conn, "groupme:system", "groupme", seen_name="GroupMe")
        adopted = dict(identity.adopt_platform_names(conn))
        checks.append(result(
            "identity.a-platform-name-is-taken-verbatim", "a numeral is not a name",
            identity.resolve(conn, "groupme:6014661") == "Joe Navarro",
            f"groupme:6014661 -> {identity.resolve(conn, 'groupme:6014661')!r}"))

        # The decoy, and the reason this is not done with `guess_person`. That matches on
        # *first name* and is documented as a prompt pre-fill; used as a resolver on the
        # live queue it wanted Joe Navarro → joe coleman, Steven Tan → Steven Whitlock,
        # Jack Bartley → jack kirkland — seven wrong merges out of fifteen, which is beat
        # 7's collision exactly. Leaving a duplicate is recoverable; renaming a correct
        # row is not.
        checks.append(result(
            "identity.a-first-name-guess-is-never-applied", "a numeral is not a name",
            "Joe Coleman" not in adopted.values(),
            f"adopted {sorted(adopted.values())}"))

        # And "name this" has no answer for a platform's own announcement channel.
        checks.append(result(
            "identity.an-announcement-channel-is-not-a-person", "a numeral is not a name",
            identity.resolve(conn, "groupme:system") is None
            and not any(r["handle"] == "groupme:system"
                        for r in identity.unresolved(conn, limit=1000)),
            "groupme:system is neither linked nor queued"))
    finally:
        conn.close()
        db.set_today(None)
    return checks


def clock_checks(home: Path) -> list[dict]:
    """What only elapsed time can show: what retires, what survives, what re-links."""
    cfg = Config(home=home)
    cfg.ensure_dirs()
    conn = db.open_db(cfg.db_path)
    checks: list[dict] = []

    def advance(day: str) -> None:
        """One day passing, as the nightly pass sees it. `run.py:362-376`."""
        db.set_today(date.fromisoformat(day))
        events.mark_past_happened(conn)
        todos.relink_questions(conn)
        todos.expire_questions(conn)
        todos.expire_event_links(conn)          # `brief.render` does this on every read
        sweep.reconcile_backward_window(conn, cfg)
        conn.commit()

    def question(key: str) -> sqlite3.Row | None:
        return conn.execute("SELECT * FROM questions WHERE key = ?", (key,)).fetchone()

    try:
        db.set_today(date(2026, 8, 3))

        # -- the cast. Every title here is two words long, because that is the shape
        #    the linker gets wrong: `TITLE_MATCH = 0.5` against a two-word name is
        #    satisfied by *one* shared word, and "meeting", "out" and "night" name a
        #    shape rather than a subject.
        gym, _ = events.upsert(conn, {
            "title": "Gym", "date": "2026-08-05", "kind": "commitment",
            "status": "mentioned", "subject": "me"}, written_by="dream:day1")
        tutoring, _ = events.upsert(conn, {
            "title": "Tutoring", "date": "2026-08-11", "kind": "commitment",
            "status": "confirmed", "subject": "me"}, written_by="dream:day1")
        events.upsert(conn, {
            "title": "Alumni meeting", "date": "2026-08-11", "kind": "commitment",
            "status": "confirmed", "subject": "me"}, written_by="dream:day1")
        events.upsert(conn, {
            "title": "Hang out with Quinn Brooks", "date": "2026-08-09",
            "kind": "commitment", "status": "confirmed", "subject": "me",
            "participants": ["Quinn Brooks"]}, written_by="dream:day1")
        board_game, _ = events.upsert(conn, {
            "title": "Board game night at Jose's", "date": "2026-08-08",
            "kind": "commitment", "status": "confirmed", "subject": "me"},
            written_by="dream:day1")
        lapsed, _ = events.upsert(conn, {
            "title": "Standup", "date": "2026-08-04", "kind": "commitment",
            "status": "mentioned", "subject": "me"}, written_by="dream:day1")
        # A row that is wrongly declined on day 1 and put right on day 3. It is not a
        # candidate while it is declined, so a question about it lands on the nearest
        # row that is — and `relink_questions` only ever considers questions linked to
        # nothing, so the wrong link is never revisited once made.
        festival, _ = events.upsert(conn, {
            "title": "Elements festival", "date": "2026-08-07", "until": "2026-08-09",
            "kind": "commitment", "status": "declined", "subject": "me"},
            written_by="ical")
        events.upsert(conn, {
            "title": "Breakfast at Elements", "date": "2026-08-08",
            "kind": "commitment", "status": "confirmed", "subject": "me"},
            written_by="dream:day1")

        # -- an obligation with no occasion attached. r1's counter-case: this is the
        #    thing that must *not* retire when a day passes, and the reason a question
        #    and a to-do cannot share one expiry rule.
        standing_todo, _ = todos.open_todo(
            conn, "Return Rowan's EZ-Pass", written_by="live")
        # ...and one that is only about the gym session, which must go with it.
        linked_todo, _ = todos.open_todo(
            conn, "Pack gym shoes", event_id=gym.id, written_by="live")

        # -- the questions asked on day 1. Two of them share exactly one generic word
        #    with a row they have nothing to do with, and one legitimately names its
        #    row — the positive control, without which "never link anything" passes.
        todos.ask(conn, "Are you going to the gym on Wednesday?",
                  key="q:gym", about_event=gym.id, written_by="dream:day1")
        todos.ask(conn, "When is the tutoring appointment this month?",
                  key="q:tutoring", written_by="dream:day1")
        todos.ask(conn, "Is the PSK meeting still happening on Saturday?",
                  key="q:psk", written_by="dream:day1")
        todos.ask(conn, "Did you ever sort out the parking permit?",
                  key="q:permit", written_by="dream:day1")
        todos.ask(conn, "Which day is the board game night at Jose's?",
                  key="q:boardgame", written_by="dream:day1")
        todos.ask(conn, "Which days is the Elements festival running?",
                  key="q:festival", written_by="dream:day1")
        conn.commit()

        advance("2026-08-04")
        advance("2026-08-05")

        def linked_title(key: str) -> str | None:
            row = question(key)
            if not row or not row["about_event"]:
                return None
            found = conn.execute("SELECT title FROM events WHERE id = ?",
                                 (row["about_event"],)).fetchone()
            return found["title"] if found else None

        # A question is only ever about a subject. "meeting", "out", "night" and
        # "party" name a shape, never a subject, and a two-word title matching on one
        # of them is a coincidence at this corpus size. On the live store that filed a
        # board-game night *and* a tutoring appointment under one "Alumni meeting", and
        # a parking question under "Hang out with Quinn" on the word `out`.
        checks.append(result(
            "clock.generic-word-is-not-a-subject", "question linking",
            linked_title("q:psk") is None,
            f"PSK question linked to {linked_title('q:psk') or '(nothing)'}"))
        checks.append(result(
            "clock.out-is-not-a-subject", "question linking",
            linked_title("q:permit") is None,
            f"parking question linked to {linked_title('q:permit') or '(nothing)'}"))
        # The counterweight: a question that does name its row must still find it, or
        # "link nothing to anything" would score full marks.
        checks.append(result(
            "clock.a-named-row-is-still-found", "question linking",
            question("q:boardgame")["about_event"] == board_game.id,
            f"board-game question linked to "
            f"{linked_title('q:boardgame') or '(nothing)'}"))

        # The row's day has passed and nobody said a word about it. `expire_questions`
        # is age-only on `created_at`, so this question sits open for ten days beside a
        # row that is over — while `expire_event_links` has implemented exactly the
        # date-based rule for to-dos since it was written.
        advance("2026-08-06")
        gone = question("q:gym")
        checks.append(result(
            "clock.question-dies-with-its-subject", "elapsed time",
            gone["status"] == "dropped", f"status={gone['status']}"))
        checks.append(result(
            "clock.row-outlives-its-question", "elapsed time",
            events.get_by_id(conn, gym.id) is not None,
            "the row is still there" if events.get_by_id(conn, gym.id)
            else "the row went with the question"))
        checks.append(result(
            "clock.linked-obligation-retires", "elapsed time",
            todos.get(conn, linked_todo.key).status == "dropped",
            f"status={todos.get(conn, linked_todo.key).status}"))
        checks.append(result(
            "clock.obligation-is-not-a-question", "elapsed time",
            todos.get(conn, standing_todo.key).status == "open",
            f"status={todos.get(conn, standing_todo.key).status}"))

        # A confirmed row whose day passed is an observation; an unresolved one is the
        # reconciler's business and must be left for it.
        checks.append(result(
            "clock.past-commitment-becomes-observation", "elapsed time",
            events.get_by_id(conn, tutoring.id).status == "confirmed"
            and events.get_by_id(conn, lapsed.id).status == "mentioned",
            f"tutoring={events.get_by_id(conn, tutoring.id).status} "
            f"standup={events.get_by_id(conn, lapsed.id).status}"))
        asked = question(f"q:resolve:{lapsed.key}")
        checks.append(result(
            "clock.past-mention-becomes-a-question", "elapsed time",
            asked is not None and asked["status"] == "open",
            f"reconcile question={asked['text'] if asked else '(none)'}"))
        # `reconcile_backward_window` calls `todos.ask` with no `trace.stamp`, so its
        # questions carry zero provenance and render "no source" in the web UI. Every
        # other question path stamps. Two live questions had literally nothing.
        stamped = conn.execute(
            "SELECT count(*) AS n FROM provenance WHERE kind = 'question' AND ref = ?",
            (f"q:resolve:{lapsed.key}",)).fetchone()["n"]
        checks.append(result(
            "clock.reconcile-question-has-provenance", "elapsed time",
            stamped > 0, f"{stamped} provenance row(s)"))

        # The link the live store actually holds, written directly because that is the
        # state that has to be *repaired*: the question was filed on the breakfast *at*
        # the festival while the festival row itself was wrongly `declined`, and a
        # declined row is not a candidate. Not making the bad link again is necessary
        # and not sufficient — the store is full of links made under conditions that
        # have since changed, and nothing ever looks at one twice.
        breakfast = conn.execute(
            "SELECT id FROM events WHERE title = 'Breakfast at Elements'").fetchone()
        conn.execute("UPDATE questions SET about_event = ? WHERE key = 'q:festival'",
                     (breakfast["id"],))
        conn.commit()
        checks.append(result(
            "clock.stale-link-was-made", "question linking",
            linked_title("q:festival") == "Breakfast at Elements",
            f"festival question starts on {linked_title('q:festival') or '(nothing)'}"))
        # The user puts the row right themselves, which is a `live` write — the correction has
        # to outrank the `ical` scan that declined it, or nothing moves at all.
        events.upsert(conn, {
            "key": festival.key, "title": "Elements festival", "date": "2026-08-07",
            "until": "2026-08-09", "status": "confirmed"}, written_by="live")
        checks.append(result(
            "clock.a-correction-outranks-the-scan", "question linking",
            events.get_by_id(conn, festival.id).status == "confirmed",
            f"festival status={events.get_by_id(conn, festival.id).status}"))
        advance("2026-08-06")
        checks.append(result(
            "clock.a-stale-link-is-re-scored", "question linking",
            linked_title("q:festival") == "Elements festival",
            f"festival question now on {linked_title('q:festival') or '(nothing)'}"))

        # ...and it must not re-ask on every subsequent pass.
        advance("2026-08-07")
        again = conn.execute(
            "SELECT count(*) AS n FROM questions WHERE text LIKE 'Did Standup happen%'"
        ).fetchone()["n"]
        checks.append(result(
            "clock.reconcile-does-not-nag", "elapsed time",
            again == 1, f"{again} question(s) about the same lapsed row"))

        # The store learned the answer on day 4. Nothing re-reads a question after the
        # row it is about is enriched, so "When is the tutoring appointment" stays open
        # directly beneath a row that says Tuesday 11 August.
        events.upsert(conn, {
            "key": tutoring.key, "title": "Tutoring", "date": "2026-08-11",
            "time": "16:00", "location": "Riverton", "status": "confirmed"},
            written_by="dream:day4")
        advance("2026-08-08")
        settled = question("q:tutoring")
        checks.append(result(
            "clock.answered-question-is-dropped", "elapsed time",
            settled["status"] == "dropped", f"status={settled['status']}"))

        # -- "Did Dad's birthday happen on Sunday? who with?"
        #
        # The user was at a festival in Cedar Falls, Vermont that weekend, and the store
        # knew: a four-day span the user was confirmed inside, at a named place two hundred
        # miles from the restaurant. The reconciler asked anyway, because nothing
        # between composing a question and storing it has ever read the store — the
        # only bar was `is_worth_asking`, a regex over the *wording*, and there is
        # nothing wrong with the sentence. What is wrong is a fact.
        #
        # Three decoys, and they are the point. A question about a day the user was free must
        # survive; a question about something at the *same* place must survive, because
        # "the user is busy" is not the claim; and a settled row is nobody's business either
        # way. Silencing the reconciler would pass the first check alone.
        events.upsert(conn, {
            "title": "Elements weekend", "date": "2026-08-14", "until": "2026-08-17",
            "kind": "commitment", "status": "confirmed", "subject": "me",
            "location": "Blakeslee, PA"}, written_by="ical")
        away, _ = events.upsert(conn, {
            "title": "Dad's birthday steak dinner", "date": "2026-08-16",
            "kind": "commitment", "status": "mentioned", "subject": "me",
            "location": "Peddler's Village"}, written_by="dream:day1")
        same_place, _ = events.upsert(conn, {
            "title": "Sunrise set", "date": "2026-08-16", "kind": "commitment",
            "status": "mentioned", "subject": "me",
            "location": "Blakeslee, PA"}, written_by="dream:day1")
        free_day, _ = events.upsert(conn, {
            "title": "Dentist cleaning", "date": "2026-08-18", "kind": "commitment",
            "status": "mentioned", "subject": "me",
            "location": "Midtown"}, written_by="dream:day1")
        # `days_back` is 3, so this has to land where the reconciler can still see the
        # 16th — the first draft advanced four days and every check passed vacuously
        # with nothing asked at all.
        advance("2026-08-19")
        checks.append(result(
            "clock.no-question-about-a-day-the user-was-elsewhere", "question admissibility",
            question(f"q:resolve:{away.key}") is None,
            f"asked: {(question(f'q:resolve:{away.key}') or {'text': '(nothing)'})['text']}"))
        checks.append(result(
            "clock.same-place-is-not-a-conflict", "question admissibility",
            question(f"q:resolve:{same_place.key}") is not None,
            f"asked: {(question(f'q:resolve:{same_place.key}') or {'text': '(nothing)'})['text']}"))
        checks.append(result(
            "clock.a-free-day-is-still-asked-about", "question admissibility",
            question(f"q:resolve:{free_day.key}") is not None,
            f"asked: {(question(f'q:resolve:{free_day.key}') or {'text': '(nothing)'})['text']}"))
        refused = conn.execute(
            "SELECT count(*) AS n FROM provenance"
            " WHERE kind = 'question' AND verb = 'refused'").fetchone()["n"]
        checks.append(result(
            "clock.a-refused-question-is-visible", "question admissibility",
            refused > 0, f"{refused} refusal(s) recorded"))

        # -- "if there is a series that has a URL and it needs to be temporarily moved
        #    to another date. It should carry the same qualities."
        #
        # A weekly appointment knows where it is and how to join it. Move one occurrence
        # and the store had a row that knew neither: `ensure_series` derives the page
        # *from* its instances and nothing derives back, so the series could describe
        # what happened and could not furnish what came next. The page literally holds
        # `where: Online` while the next instance holds nothing.
        for day in ("2026-07-14", "2026-07-21"):
            events.upsert(conn, {
                "key": f"ical-voice@{day}", "title": "Voice lesson", "date": day,
                "time": "10:00", "kind": "commitment", "status": "happened",
                "location": "Online",
                "join_url": "https://us02web.zoom.example/j/8842119"},
                written_by="ical", match=False)
        wiki.link_series(conn, cfg.wiki_dir)
        moved, _ = events.upsert(conn, {
            "title": "Voice lesson", "date": "2026-08-26", "time": "12:00",
            "kind": "commitment", "status": "mentioned"}, written_by="dream:web")
        checks.append(result(
            "series.moved-instance-joins-it", "series carries its qualities",
            moved.series == "voice-lesson", f"series={moved.series!r}"))
        checks.append(result(
            "series.moved-instance-carries-the-link", "series carries its qualities",
            moved.join_url == "https://us02web.zoom.example/j/8842119",
            f"join_url={moved.join_url!r}"))
        checks.append(result(
            "series.moved-instance-carries-the-place", "series carries its qualities",
            moved.location == "Online", f"location={moved.location!r}"))
        # The decoys, and they are most of the value. "Temporarily moved" means the day
        # is the one thing that must *not* come along, and a series may never lend a
        # judgement: a new occurrence is not confirmed because the last one happened.
        checks.append(result(
            "series.moved-instance-keeps-its-own-day", "series carries its qualities",
            moved.date == "2026-08-26" and moved.time == "12:00",
            f"{moved.date} {moved.time}"))
        checks.append(result(
            "series.status-is-never-inherited", "series carries its qualities",
            moved.status == "mentioned", f"status={moved.status}"))
        stated, _ = events.upsert(conn, {
            "title": "Voice lesson", "date": "2026-09-02", "time": "10:00",
            "kind": "commitment", "status": "mentioned",
            "location": "14 Example Avenue"}, written_by="dream:web")
        checks.append(result(
            "series.a-stated-place-wins", "series carries its qualities",
            stated.location == "14 Example Avenue", f"location={stated.location!r}"))
        # One prior row is not a series. `link_series` needs two distinct dates before
        # it will open a page, and inheritance may not be looser than membership.
        events.upsert(conn, {
            "key": "ical-haircut@2026-07-15", "title": "Haircut", "date": "2026-07-15",
            "kind": "commitment", "status": "happened", "location": "Fade Room"},
            written_by="ical", match=False)
        lone, _ = events.upsert(conn, {
            "title": "Haircut", "date": "2026-09-09", "kind": "commitment",
            "status": "mentioned"}, written_by="dream:web")
        checks.append(result(
            "series.one-occasion-is-not-a-series", "series carries its qualities",
            lone.location is None, f"location={lone.location!r}"))
    finally:
        conn.close()
        db.set_today(None)
    return checks


def schedule_checks(home: Path) -> list[dict]:
    """Check cadence changes, one-off exceptions, and inherited series fields."""
    cfg = Config(home=home)
    cfg.ensure_dirs()
    conn = db.open_db(cfg.db_path)
    checks: list[dict] = []
    ZOOM = "https://us02web.zoom.example/j/8842119"
    try:
        db.set_today(date(2026, 8, 10))

        # -- Mondays at 10, off their calendar, which is where a standing appointment
        #    actually lives. Two dates, so it is a series by `link_series`'s own rule.
        for day in ("2026-08-03", "2026-08-10"):
            events.upsert(conn, {
                "key": f"ical-tutoring@{day}", "title": "Tutoring", "date": day,
                "time": "10:00", "kind": "commitment", "status": "happened",
                "location": "Online"}, written_by="ical", match=False)
        wiki.link_series(conn, cfg.wiki_dir)
        # The Monday after the change, already on the calendar because a tutor does
        # not edit their phone. This is the row that must survive and be asked about.
        stray, _ = events.upsert(conn, {
            "key": "ical-tutoring@2026-08-17", "title": "Tutoring", "date": "2026-08-17",
            "time": "10:00", "kind": "commitment", "status": "mentioned",
            "series": "tutoring", "location": "Online"},
            written_by="ical", match=False)

        # -- "can we move to tuesday at 1pm going forward". One rule, one write.
        rule, verb = series.upsert(conn, {
            "slug": "tutoring", "title": "Tutoring", "cadence": "weekly", "weekday": 1,
            "time": "13:00", "location": "Online", "join_url": ZOOM,
            "effective_on": "2026-08-11", "source": "email:reese"},
            written_by="dream:day1")
        checks.append(result(
            "schedule.cadence-has-somewhere-to-live", "a schedule moves",
            verb == "inserted" and rule.weekday == 1 and rule.time == "13:00",
            f"{verb} weekday={rule.weekday} time={rule.time!r}"))

        # -- "this next tuesday i cant make that time... so we propose wednesday at
        #    noon". One occurrence, contradicting its own rule, on purpose.
        moved, _ = events.upsert(conn, {
            "title": "Tutoring", "date": "2026-08-12", "time": "12:00",
            "kind": "commitment", "status": "confirmed", "series": "tutoring",
            "instead_of": "2026-08-11"}, written_by="dream:day1")
        checks.append(result(
            "schedule.the-exception-keeps-its-own-day", "a schedule moves",
            moved.date == "2026-08-12" and moved.time == "12:00",
            f"{moved.date} {moved.time}"))
        checks.append(result(
            "schedule.the-exception-names-what-it-replaces", "a schedule moves",
            moved.instead_of == "2026-08-11", f"instead_of={moved.instead_of!r}"))
        # "for the zoom link to persist across the two new copies" — and note that no
        # past instance was ever on a Tuesday or carried this link. Scavenging the
        # instances, which is all inheritance could do before, finds nothing here.
        checks.append(result(
            "schedule.the-exception-carries-the-link", "a schedule moves",
            moved.join_url == ZOOM, f"join_url={moved.join_url!r}"))

        series.roll_forward(conn, slug="tutoring")

        # -- "and then next week, a series continuing forever still at tuesday at 1".
        #
        # The *rule* is what says forever, and it is asked rather than materialised: one
        # row for the next occurrence is the policy, the exception on the 12th is that
        # row while it is still ahead of them, and fifty Tuesdays in `events` would put
        # fifty rows in front of every matcher in the store to say what one rule says.
        after = series.next_on(conn, series.get(conn, "tutoring"), after="2026-08-12")
        checks.append(result(
            "schedule.next-week-is-the-new-cadence", "a schedule moves",
            after is not None and after.isoformat() == "2026-08-18",
            f"the one after the exception is {after}"))
        checks.append(result(
            "schedule.the-rule-reaches-the-brief", "a schedule moves",
            "Tuesdays" in brief.render(conn, cfg)
            and "this once on Wed 12 Aug" in brief.render(conn, cfg),
            [line for line in brief.render(conn, cfg).splitlines()
             if "Tutoring" in line] or "(tutoring is nowhere in the brief)"))

        # -- and once the excepted week has passed, the Tuesday is a row, with the link
        #    the rule holds — no past instance was ever on a Tuesday or carried one.
        db.set_today(date(2026, 8, 13))
        series.roll_forward(conn, slug="tutoring")
        rolled = conn.execute(
            "SELECT date, time, join_url FROM events"
            "  WHERE series = 'tutoring' AND date = '2026-08-18'").fetchone()
        checks.append(result(
            "schedule.the-projection-carries-the-link", "a schedule moves",
            rolled is not None and rolled["join_url"] == ZOOM
            and rolled["time"] == "13:00",
            f"18th: {dict(rolled) if rolled else '(no row)'}"))

        # -- and it may never invent one *earlier* than what a source already said.
        #
        # This is the regression the physio beat caught: told a slot is weekly on
        # Wednesdays and that the appointment is Wednesday the 12th, `roll_forward` also
        # wrote a Wednesday the 5th, because the 5th is the first Wednesday the rule
        # lands on. A projection may answer "when is the next one" and may never
        # contradict a source that said.
        series.upsert(conn, {"slug": "pilates", "title": "Pilates", "cadence": "weekly",
                             "weekday": 2, "time": "17:00",
                             "effective_on": "2026-08-13"}, written_by="cli")
        events.upsert(conn, {
            "key": "pilates@2026-08-26", "title": "Pilates", "date": "2026-08-26",
            "time": "17:00", "series": "pilates", "kind": "commitment"},
            written_by="dream:day1", match=False)
        series.roll_forward(conn, slug="pilates")
        booked = [r["date"] for r in conn.execute(
            "SELECT date FROM events WHERE series = 'pilates' ORDER BY date")]
        checks.append(result(
            "schedule.a-rule-does-not-invent-an-earlier-week", "a schedule moves",
            booked == ["2026-08-26"], "pilates rows: " + ", ".join(booked)))

        # -- "if theres no time, we should say so. time tbd/time unknown". A standing
        #    appointment always happens *at* a time, so a rule without one has a hole in
        #    it, and a blank renders as nothing at all — indistinguishable from an
        #    all-day thing that never had one.
        series.upsert(conn, {"slug": "sauna", "title": "Sauna", "cadence": "weekly",
                             "weekday": 4, "effective_on": "2026-08-14"},
                      written_by="cli")
        series.roll_forward(conn, slug="sauna")
        checks.append(result(
            "schedule.an-unknown-time-says-so", "a schedule moves",
            "time TBD" in series.get(conn, "sauna").phrase,
            f"phrase = {series.get(conn, 'sauna').phrase!r}"))
        asked = conn.execute(
            "SELECT text FROM questions WHERE key = 'q:series-time:sauna'").fetchone()
        checks.append(result(
            "schedule.an-unknown-time-is-asked-about", "a schedule moves",
            asked is not None, f"asked: {asked['text'] if asked else '(nothing)'}"))
        # And the counterweight: a rule that *has* a time must not be nagged about it.
        quiet = conn.execute(
            "SELECT 1 FROM questions WHERE key = 'q:series-time:tutoring'").fetchone()
        checks.append(result(
            "schedule.a-known-time-is-not-asked-about", "a schedule moves",
            quiet is None, "asked about a time it already knows" if quiet else "quiet"))
        # And once the user answers, the row memcal already wrote has to catch up. Setting the
        # time on the live `tutoring` rule left its own already-projected Tuesday blank,
        # so the brief said "Tuesdays at 13:00" one line above a row with no time on it.
        series.upsert(conn, {"slug": "sauna", "time": "19:00"}, written_by="live")
        series.roll_forward(conn, slug="sauna")
        sauna = conn.execute(
            "SELECT date, time FROM events WHERE series = 'sauna' ORDER BY date"
        ).fetchone()
        checks.append(result(
            "schedule.a-projection-follows-its-rule", "a schedule moves",
            sauna is not None and sauna["time"] == "19:00",
            f"sauna row: {dict(sauna) if sauna else '(none)'}"))
        db.set_today(date(2026, 8, 10))

        # -- the decoys. Each is a way this feature destroys something true.
        on_the_11th = conn.execute(
            "SELECT count(*) AS n FROM events"
            "  WHERE series = 'tutoring' AND date = '2026-08-11'").fetchone()["n"]
        checks.append(result(
            "schedule.the-excepted-day-is-not-recreated", "a schedule moves",
            not on_the_11th,
            "a Tuesday the 11th was written back in" if on_the_11th
            else "the 11th stayed excepted"))
        past = events.get(conn, "ical-tutoring@2026-08-03")
        checks.append(result(
            "schedule.the-past-is-not-retro-dated", "a schedule moves",
            past is not None and past.date == "2026-08-03" and past.time == "10:00",
            f"{past.date if past else '(gone)'} {past.time if past else ''}"))
        # A Monday that memcal projected is memcal's to withdraw. A Monday that came off
        # the calendar is an observation, and no cadence change entitles anyone to
        # delete an observation — invariant 5, so it becomes a question instead.
        survived = events.get_by_id(conn, stray.id)
        checks.append(result(
            "schedule.an-observed-leftover-is-not-deleted", "a schedule moves",
            survived is not None, "still there" if survived else "silently deleted"))
        checks.append(result(
            "schedule.an-observed-leftover-is-asked-about", "a schedule moves",
            any(row["date"] == "2026-08-17"
                for row in series.stale_occurrences(conn, "tutoring")),
            "stale: " + (", ".join(r["date"] for r in
                                   series.stale_occurrences(conn, "tutoring")) or "(none)")))

        # -- and the one memcal *did* project, once the rule moves again. This is "the
        #    original monday series to go away" in the case where it is memcal's to say.
        series.upsert(conn, {"slug": "tutoring", "weekday": 3, "time": "13:00",
                             "effective_on": "2026-08-19"}, written_by="dream:day3")
        series.roll_forward(conn, slug="tutoring")
        left = [r["date"] for r in conn.execute(
            "SELECT date FROM events WHERE series = 'tutoring' AND written_by = 'series'"
            " ORDER BY date")]
        checks.append(result(
            "schedule.a-projection-is-withdrawn-when-the-rule-moves", "a schedule moves",
            "2026-08-18" not in left and "2026-08-20" in left,
            "projected: " + (", ".join(left) or "(none)")))
        checks.append(result(
            "schedule.the-old-rule-is-still-readable", "a schedule moves",
            any(r["field"] == "weekday" and r["old_value"] == "1"
                for r in conn.execute(
                    "SELECT * FROM series_history WHERE slug = 'tutoring'")),
            "history: " + ", ".join(
                f"{r['field']} {r['old_value']}->{r['new_value']}" for r in conn.execute(
                    "SELECT * FROM series_history WHERE slug = 'tutoring' ORDER BY id"))))

        # -- ending is a thing the user says, and it never deletes anything.
        series.end(conn, "tutoring", on="2026-09-01", written_by="live")
        ended = series.get(conn, "tutoring")
        checks.append(result(
            "schedule.ending-retires-rather-than-deletes", "a schedule moves",
            ended is not None and ended.status == "ended"
            and events.get(conn, "ical-tutoring@2026-08-03") is not None,
            f"status={ended.status if ended else '(gone)'}, "
            f"past rows {'kept' if events.get(conn, 'ical-tutoring@2026-08-03') else 'lost'}"))
        checks.append(result(
            "schedule.an-ended-series-stops-projecting", "a schedule moves",
            series.roll_forward(conn, slug="tutoring") == [],
            "still projecting" if series.roll_forward(conn, slug="tutoring")
            else "stopped"))
    finally:
        conn.close()
        db.set_today(None)
    return checks


def _load_hermes_provider():
    hermes = Path.home() / ".hermes" / "hermes-agent"
    plugin = Path(__file__).resolve().parents[2] / "integrations" / "hermes" / "memcal"
    if not hermes.is_dir() or not plugin.is_dir():
        return None
    if str(hermes) not in sys.path:
        sys.path.insert(0, str(hermes))
    spec = importlib.util.spec_from_file_location(
        "_memcal_benchmark_hermes", plugin / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["_memcal_benchmark_hermes"] = module
    spec.loader.exec_module(module)
    return module.MemcalMemoryProvider


def hermes_checks(home: Path) -> list[dict]:
    """A three-turn session with stale snapshots, compaction and a session switch."""
    provider_cls = _load_hermes_provider()
    if provider_cls is None:
        return [result(
            "hermes.available", "Hermes lifecycle", False,
            "Hermes checkout is not installed; lifecycle suite skipped", soft=True)]

    old_home = os.environ.get("MEMCAL_HOME")
    old_src = os.environ.get("MEMCAL_SRC")
    os.environ["MEMCAL_HOME"] = str(home)
    os.environ["MEMCAL_SRC"] = str(Path(__file__).resolve().parents[2])
    cfg = Config(home=home)
    cfg.ensure_dirs()
    db.open_db(cfg.db_path).close()
    checks: list[dict] = []
    try:
        provider = provider_cls()
        provider.initialize("session-a", agent_context="primary")
        conn = db.open_db(cfg.db_path)
        todos.set_standing(
            conn, "identity", "Casey lives in North End.", key="identity:home")
        wiki.set_slot(cfg.wiki_dir, "quinn-brooks", "favorite movie theater",
                      "Alamo Drafthouse", source="benchmark", conn=conn)
        wiki.add_alias(cfg.wiki_dir, "quinn-brooks", "Q")
        conn.close()

        first = provider.prefetch("what is coming up?", session_id="session-a")
        provider.on_turn_start(1, "What am I doing Saturday?")
        provider.sync_turn(
            "What am I doing Saturday?",
            first + "\nYou have poker at Robbie's house.",
            messages=[{"role": "system", "content": first}])

        conn = db.open_db(cfg.db_path)
        todos.set_standing(
            conn, "identity", "Casey now lives in Harbor Point.", key="identity:home")
        conn.close()
        second = provider.prefetch("What theater does Q like?", session_id="session-a")
        checks.append(result(
            "hermes.latest-snapshot-wins", "Hermes lifecycle",
            "Harbor Point" in second and "North End" not in second,
            f"Harbor Point={'Harbor Point' in second}; old={'North End' in second}"))
        checks.append(result(
            "hermes.nickname-injects-page", "Hermes lifecycle",
            "Alamo Drafthouse" in second and "WIKI PAGES MENTIONED" in second,
            f"wiki={'Alamo Drafthouse' in second}"))

        provider.on_turn_start(2, "Poker at Robbie's next Saturday; yes, I'm going.")
        added = json.loads(provider.handle_tool_call(
            "memcal_add", {
                "title": "Poker at Robbie's", "when": "next saturday",
                "status": "confirmed", "participants": ["Robbie"],
            }))
        provider.sync_turn(
            "Poker at Robbie's next Saturday; yes, I'm going.",
            "Added it.\n" + first,
            messages=[{"role": "system", "content": first}])

        provider.on_session_switch("session-b")
        provider.on_turn_start(1, "What am I doing Saturday?")
        conn = db.open_db(cfg.db_path)
        archived = conn.execute(
            "SELECT text, meta FROM archive WHERE stream = 'agent' ORDER BY id"
        ).fetchall()
        event = conn.execute(
            "SELECT * FROM events WHERE title = ? ORDER BY id DESC LIMIT 1",
            ("Poker at Robbie's",)).fetchone()
        source = trace.source_rows(conn, "event", event["key"]) if event else []
        conn.close()
        texts = [row["text"] for row in archived]
        checks.append(result(
            "hermes.only-user-crosses-boundary", "Hermes lifecycle",
            not any("MEMCAL SNAPSHOT" in text or "You have poker" in text
                    or text == "Added it." for text in texts),
            f"archived={texts}"))
        checks.append(result(
            "hermes.turn-sync-deduplicates", "Hermes lifecycle",
            texts.count("What am I doing Saturday?") == 2
            and texts.count("Poker at Robbie's next Saturday; yes, I'm going.") == 1,
            f"archived={texts}"))
        checks.append(result(
            "hermes.session-switch-is-distinct", "Hermes lifecycle",
            texts.count("What am I doing Saturday?") == 2,
            f"copies={texts.count('What am I doing Saturday?')}"))
        evidence = [row["text"] for row in source if row["evidence"]]
        checks.append(result(
            "hermes.live-write-source", "Hermes lifecycle",
            evidence == ["Poker at Robbie's next Saturday; yes, I'm going."],
            f"tool={added} evidence={evidence}"))
    finally:
        if old_home is None:
            os.environ.pop("MEMCAL_HOME", None)
        else:
            os.environ["MEMCAL_HOME"] = old_home
        if old_src is None:
            os.environ.pop("MEMCAL_SRC", None)
        else:
            os.environ["MEMCAL_SRC"] = old_src
    return checks


# --------------------------------------------------------------- collection --

APPLE_EPOCH_UNIX = 978307200


def _apple_ns(when: datetime) -> int:
    """A chat.db date, in the nanoseconds-since-2001 that modern macOS writes."""
    return int((when.timestamp() - APPLE_EPOCH_UNIX) * 1_000_000_000)


def _fake_chat_db(path: Path, *, rows: list[tuple[int, datetime, str, str]]) -> None:
    """The four tables `imessage.QUERY` joins, and nothing else.

    Built rather than fixtured because the shape that matters is the *spread* of
    ROWIDs against dates: the bug is a reader that walks ROWIDs forward from an
    ancient position, which no small fixture with contiguous ids can express.
    """
    src = sqlite3.connect(path)
    src.executescript("""
        CREATE TABLE message (ROWID INTEGER PRIMARY KEY, guid TEXT, date INTEGER,
                              text TEXT, attributedBody BLOB, is_from_me INTEGER,
                              handle_id INTEGER);
        CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
        CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT,
                           display_name TEXT);
        CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
        CREATE TABLE chat_handle_join (chat_id INTEGER, handle_id INTEGER);
    """)
    src.execute("INSERT INTO handle(ROWID, id) VALUES (1, '+19175550004')")
    src.execute("INSERT INTO chat(ROWID, chat_identifier, display_name)"
                " VALUES (1, '+19175550004', NULL)")
    src.execute("INSERT INTO chat_handle_join(chat_id, handle_id) VALUES (1, 1)")
    for rowid, when, guid, text in rows:
        src.execute(
            "INSERT INTO message(ROWID, guid, date, text, attributedBody, is_from_me,"
            " handle_id) VALUES (?,?,?,?,NULL,0,1)",
            (rowid, guid, _apple_ns(when), text))
        src.execute("INSERT INTO chat_message_join(chat_id, message_id) VALUES (1, ?)",
                    (rowid,))
    src.commit()
    src.close()


def collection_checks(home: Path) -> list[dict]:
    """Check transport fallback watermarks and stream freshness semantics."""
    from memcal.sources import imessage

    cfg = Config(home=home)
    cfg.ensure_dirs()
    conn = db.open_db(cfg.db_path)
    checks: list[dict] = []
    chat_db = home / "chat.db"
    try:
        db.set_today(date(2026, 8, 13))
        old = datetime(2025, 4, 12, 13, 15, tzinfo=timezone.utc).astimezone()
        recent = datetime(2026, 8, 12, 12, 19, tzinfo=timezone.utc).astimezone()
        # A long-lived chat.db: ancient rows at low ROWIDs, today's at high ones, and
        # nothing in between the reader could mistake for progress.
        rows = [(i, old, f"old-{i}", f"an April 2025 line, number {i}")
                for i in range(1, 3016)]
        rows += [(864373, recent, "recent-1",
                  "Can you find a place we can get dinner tomorrow? "
                  "I'm meeting my friend at pier park at 730!")]
        _fake_chat_db(chat_db, rows=rows)

        # -- The primary already delivered through the 5th, which is what "the stream is
        #    here" means regardless of which transport said it.
        archive.append(conn, stream="imessage", external_id="via-bluebubbles",
                       ts="2026-08-05T11:33:50-04:00", text="the last line BlueBubbles got",
                       thread="+19175550004", handle="+19175550004", person="Harper",
                       from_me=False, meta={}, gated=True, gate_reason="top-tier")
        # -- and the fallback's own watermark is sixteen months behind it.
        db.set_meta(conn, "imessage.rowid", "0")
        conn.commit()

        imessage.ingest(conn, limit=1000, db_path=chat_db)
        newest = conn.execute(
            "SELECT max(ts) FROM archive WHERE stream='imessage'").fetchone()[0] or ""
        checks.append(result(
            "collection.fallback-resumes-in-time", "a fallback resumes where the primary stopped",
            newest[:10] >= "2026-08-12",
            f"newest imessage row after the fallback ran: {newest!r}"))
        got_dinner = conn.execute(
            "SELECT count(*) FROM archive WHERE stream='imessage'"
            " AND text LIKE '%pier park%'").fetchone()[0]
        checks.append(result(
            "collection.the-reported-line-arrives", "a fallback resumes where the primary stopped",
            got_dinner == 1, f"lines matching the report: {got_dinner}"))

        # -- Decoy: a genuine cold start has no floor to resume from and must still walk
        #    from the beginning. A fix that always jumps to the newest row silently
        #    destroys the first import, which is the one that matters most.
        cold_home = home / "cold"
        cold_cfg = Config(home=cold_home)
        cold_cfg.ensure_dirs()
        cold = db.open_db(cold_cfg.db_path)
        imessage.ingest(cold, limit=50, db_path=chat_db)
        oldest = cold.execute(
            "SELECT min(ts) FROM archive WHERE stream='imessage'").fetchone()[0] or ""
        checks.append(result(
            "collection.a-cold-start-still-starts-cold", "a fallback resumes where the primary stopped",
            oldest[:4] == "2025", f"oldest row on a cold start: {oldest!r}"))
        cold.close()

        # -- The health signal, on its own store so the fix above cannot make the data
        #    fresh underneath it. The collector ran five minutes ago and the newest line
        #    it has is eight days old; only one of those is a claim about *now*.
        health_home = home / "health"
        health_cfg = Config(home=health_home)
        health_cfg.ensure_dirs()
        health = db.open_db(health_cfg.db_path)
        archive.append(health, stream="imessage", external_id="eight-days-ago",
                       ts="2026-08-05T11:33:50-04:00", text="the last line that ever arrived",
                       thread="+19175550004", handle="+19175550004", person="Harper",
                       from_me=False, meta={}, gated=True, gate_reason="top-tier")
        db.set_meta(health, "source.imessage.last_success", "2026-08-13T10:23:31-04:00")
        health.commit()
        stale = dict(archive.stale_streams(health, cfg=health_cfg))
        checks.append(result(
            "brief.stale-stream-named", "a heartbeat is not freshness",
            "imessage" in stale,
            f"stale_streams says {stale!r}; last_success is today and the newest "
            f"imessage row is eight days old"))

        # -- Decoy: iCal is healthy when nothing changed. A fix that simply deletes the
        #    marker override reports every quiet snapshot source as broken, which is the
        #    requirement the marker was added for in the first place.
        archive.append(health, stream="ical", external_id="an-old-scan",
                       ts="2026-08-01T09:00:00-04:00", text="a calendar item, unchanged since",
                       thread="calendar", handle=None, person=None, from_me=False,
                       meta={}, gated=False, gate_reason=None)
        db.set_meta(health, "source.ical.last_success", "2026-08-13T10:23:31-04:00")
        health.commit()
        quiet = dict(archive.stale_streams(health, cfg=health_cfg))
        checks.append(result(
            "brief.a-quiet-snapshot-is-not-stale", "a heartbeat is not freshness",
            "ical" not in quiet,
            f"stale_streams says {quiet!r}; ical last read today and changed nothing"))
        health.close()
    finally:
        db.set_today(None)
        conn.close()
    return checks
