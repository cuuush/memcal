# Monitoring

## Doctor first

```bash
memcal doctor
```

Reports across Store, Sources, Extraction, Schedule, and Calendar — what is
wrong, and what to type about it.

## Runs and volume

- The web UI's **Runs tab** shows every pass: ok, partial, failed, running, or
  priced only — with requests, bundles, and per-run detail. Retry from here or
  with `memcal dream --retry RUN`.
- `memcal stats` reports per-stream totals over recent days plus recent run history.
- `memcal gatecheck` shows what the gate is passing and rejecting right now.
- Ingest health per source lives at `/api/collections` in the web API (the Runs
  tab reads `/api/runs`).

## Traces and review

- `memcal trace` — the prompt, reasoning, and reply of a real model call.
- `memcal review` — what the last pass wrote, what it wants to know, what it
  could not resolve.
- `calls/` keeps every prompt, reply, usage record, and trace on disk for audit.

## Schedule

`memcal schedule status` shows loaded state, next run, last pass, and exit —
plus whether a pass is currently owed. See [Scheduling](scheduling.md).
