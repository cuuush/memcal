# Freshness gap note — issue #81

Gaps between current daytime freshness behaviour and
`docs/architecture/freshness.md` plus Chris product rules. MVP on branch
`freshness-correctness-81` closes the brief-facing ones below; Field Evidence
live-write and #80 unified-open stay out of scope.

## Product rules (target)

1. **Hint must be relevant linked subchannel only** — citations / evidence
   associations for that plan, not whole-stream activity.
2. **iCal: calendar row's own feed churn is NOT a hint** — UNTHREADED `ical`
   family revisions currently ride `activity.pending` ← `evidence_families` and
   can flag every ical-backed event on collect. Calendar self-feed churn ≠ hint.
3. **Hint ≠ apply** — the brief only flags text; the assistant (or nightly
   dream) judges and applies.
4. **Never leak raw phone / Proton IDs in the brief** — resolve via
   `threads.label` / whois display names when richer, else `threads.title()`;
   if the label still looks like a phone or long opaque id, use
   `"unknown number"` or a short truncated human title.
5. **Gut / remove the UNREVIEWED footer** — `_backlog_lines` /
   `unlinked_backlog` render in `brief.py` currently dumps
   `[UNREVIEWED: stream/thread …]` (PII). Not the designed signal for MVP.
6. **Dream sender titles = out of MVP.**

## Observed bugs (pre-fix)

- `_activity_hint` prints `stream/thread` raw (`chat/+1555…`, Proton hex, etc.).
- `strong_total` can be an entire never-reviewed thread (2351-style) when
  `reviewed_lines` is empty — the count dominates the brief.
- iCal-only pending still produces a brief activity hint on the event line.

## Frozen hint copy (Integrations must mirror)

Canonical format string (also `brief.ACTIVITY_HINT_FORMAT`):

```text
  ↳ New activity: {where} — {count} message(s){extra} since this plan was reviewed. It may have changed; open with memcal_activity(handle={handle}) before giving current details.
```

- `{where}` is `stream/<safe-label>` (never raw phone / long opaque id).
- `{count}` is capped for display (`99+` past the cap); conversational pending
  only (no UNTHREADED / ical families).
- `{extra}` is ` (+N more)` when more strong conversational links exist than
  the sampled rows, else empty.
- `{handle}` is the brief source tag without brackets (e.g. `E42`).
- Hint ≠ apply: this line never invents a replacement date, address, or status.

## MVP scope on this branch

| Area | Change |
|------|--------|
| A | Brief hints ignore UNTHREADED (`ical`) pending; keep chat/email/etc. |
| B | Label-safe `_activity_hint` (label/whois/title); cap counts; require a review mark |
| C | Stop emitting `[UNREVIEWED: stream/thread…]`; non-PII coverage stub only |
| D | Regression tests for ical-only, safe chat label, no UNREVIEWED PII |
| E | CHANGELOG Unreleased bullets |

## Remaining limitations (not this MVP)

- Dream sender titles still unresolved.
- `activity.pending` / `memcal_activity` may still surface ical family revisions
  for the activity reader — only the **brief hint** suppresses them.
- Weak associations are unchanged (brief already uses `strong_only=True`).
- Whois enrichment still depends on contacts having been resolved; until then
  safe fallbacks are `"unknown number"` / short title.
- Unlinked backlog no longer names the thread; broad “anything fun?” coverage
  is a single non-identifying `[coverage incomplete …]` notice.
