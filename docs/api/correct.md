# Correct

Corrections write immediately and deterministically — no model, no inference.
Change the typed row, record the old value in history, update the next brief.
A daytime correction and the nightly dream preserve the same event identity and
evidence: re-reading the statement that moved an event never duplicates it.

## CLI

```bash
memcal status E286 confirmed
memcal todo "…" / memcal done T7
memcal answer Q4 "Saturday — Jordan confirmed by text"
memcal series poker-night --every weekly --interval 2 --on friday
memcal series practice --every weekly --weekdays monday wednesday friday --at 17:00
memcal series practice --state paused --starting 2032-03-08
memcal series practice --state active --starting 2032-03-22
memcal alias jordan "Jord"
memcal merge --apply jordan-smith jordan
memcal rm E999          # never-real rows only — not for declined plans
```

## MCP

| Tool | Corrects |
|---|---|
| `memcal_update` | Status, date/time, location (including a video URL), title, kind, participants, and notes; `""` clears optional details |
| `memcal_merge` | Two rows are one thing |
| `memcal_drop` | Delete a never-real row (not for `declined`) |
| `memcal_schedule` | One recurring rule: multiple weekdays, interval, dated changes, pause/resume/end |
| `memcal_move_once` | Move or skip one occurrence, by event handle or series name plus `on` |
| `memcal_todo` | With `done=true`, closes |
| `memcal_answer` | Answers a question, stops asking |
| `memcal_alias` | Binds another name to a page |

## Recurring events

Use one `memcal_schedule` call for multiple weekly days, not one `memcal_add`
per weekday. For example, `which="practice"`, `cadence="weekly"`,
`weekdays=["monday", "wednesday", "friday"]`, and `time="17:00"` create one
rule. `interval=3` repeats those days every three weeks. `location` accepts a
place, address, or video-call URL; `note` on an event holds other freeform details.

`starting` dates a schedule change without changing earlier occurrences. Use
`status="paused"` to pause, `status="active"` to resume, or `status="ended"`
to stop from that date. A pause and a later resume can be scheduled in advance.
For one cancellation, call `memcal_move_once(which="practice", on="2032-03-05",
cancelled=true)`; no replacement date is needed. To postpone that occurrence,
give `to` instead. The surrounding schedule stays intact.

When Calendar publishing is enabled, the rule is a native recurring event.
Dated changes use bounded recurring segments; a single cancellation or move uses
an exception to one occurrence. Earlier calendar history is preserved. Publishing
remains disabled by default.

## Evidence precedence

Per field, on when evidence was *said*: a message written before your correction
cannot revise the field you corrected, while genuinely newer evidence still can.
An event update can add or remove named participants explicitly. Memcal never
infers a to-do is complete — it waits for an explicit completion or asks.

A correction may cite the source lines it is based on. Prefer
`field_sources` on `memcal_add` / `memcal_update` (and the Hermes twins) when
fields have different supporting messages: a map of field name → archive line
ids. Each field uses the *newest* supporting timestamp. Optional
`context_source_ids` are background only (no authority) and are valid only with
`field_sources`. Do not combine flat `source_ids` with `field_sources`.

Flat `source_ids` on `memcal_update` (and `--source-ids` on `memcal reviewed`)
remain for single-authority corrections: the evidence time is the *oldest*
cited line, so a stale line cannot ride a newer one. A `source_id` that names
no real row, or a line with no readable timestamp, is refused rather than
treated as now. Values a row was created holding are protected by their
founding evidence time, so the first edit — however old its source — cannot
erase them.

Mapped writes return per-field outcomes (`applied` / `unchanged` / `rejected`,
plus `evidence_advanced` when a newer same-value cite advances evidence). An
all-rejected request never reads as a silent no-op.

## New activity

When the brief flags a plan with new activity, `memcal open <handle>` (MCP
`memcal_open`) returns the stored row with the new messages already included —
one call, changing nothing. `memcal activity <handle>` (MCP `memcal_activity`)
pages further only when that preview is truncated. Cite their ids in a correction
to apply the change, or, when the stored plan still stands, acknowledge them with
`memcal reviewed <handle> --source-ids "…"` (MCP `memcal_reviewed`) so only those
lines stop raising the hint. With no handle, `memcal activity` lists unlinked
backlog traffic. See [Daytime freshness](../architecture/freshness.md).
