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
memcal series poker-night --every fortnightly --on friday
memcal alias jordan "Jord"
memcal merge --apply jordan-smith jordan
memcal rm E999          # never-real rows only — not for declined plans
```

## MCP

| Tool | Corrects |
|---|---|
| `memcal_update` | Status, date/time, location, title, kind, add/remove participants; `""` clears only location, note, time, until, join_url, rsvp_url, series |
| `memcal_merge` | Two rows are one thing |
| `memcal_drop` | Delete a never-real row (not for `declined`) |
| `memcal_schedule` | The rule moved, not one date |
| `memcal_move_once` | One week moved or skipped only |
| `memcal_todo` | With `done=true`, closes |
| `memcal_answer` | Answers a question, stops asking |
| `memcal_alias` | Binds another name to a page |

## Precedence

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

When the brief flags a plan with new activity, `memcal activity <handle>` (MCP
`memcal_activity`) reads the messages behind it — paginated, changing nothing.
Cite their ids in a correction to apply the change, or, when the stored plan still
stands, acknowledge them with `memcal reviewed <handle> --source-ids "…"` (MCP
`memcal_reviewed`) so only those lines stop raising the hint. With no handle,
`memcal activity` lists unlinked backlog traffic. See
[Daytime freshness](../architecture/freshness.md).
