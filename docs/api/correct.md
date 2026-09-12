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
