# Troubleshooting

Cross-source checklist. Each source page lists the exact error strings its code
produces ([Slack](sources/slack.md), [Telegram](sources/telegram.md),
[Signal](sources/signal.md)); this page covers the failures shared by all of them.

1. **Start with `memcal sources`, then `memcal doctor`.** The first shows per-source
   usability and the exact gap; the second diagnoses store, extraction, schedule,
   and calendar with what to type next.
2. **Check which store you are talking to.** `MEMCAL_HOME` selects the home;
   `~/.memcal/.env` vs `$MEMCAL_HOME/.env` mismatches explain "logged in here,
   unknown there" (notably Telegram sessions). `.env` files merge checkout →
   store → working directory, later winning.
3. **Never paste secrets when asking for help.** `memcal sources` prints users and
   workspaces, never tokens. Session files and API hashes stay off the screen.
4. **Small sip first.** `memcal ingest <name> --limit 20`, read the
   `read / archived / queued` line plus its notes (`[more waiting]`, `older than
   30d`, `skipped as muted`), then `memcal ingest all` and
   `memcal dream --dry-run` before the real pass.
5. **Rate-limited rounds are safe to retry.** Slack (`Retry-After` honored, 429
   stops the round), Telegram (`FloodWait` stops it), Signal (180s ceiling) —
   watermarks commit per round, so the next run resumes forward. Lower `--limit`
   and space out first loads.
6. **Nothing archived, ever.** For Signal: receipts and typing notices are
   skipped, pre-link history never existed, and a crash between receive and
   commit loses acked messages. For polled sources: check dormant-chat skips on
   first run, muted handling, and whether the gate passed anything
   (`memcal gatecheck`).
7. **Watermarks never rewind.** A partial round cannot skip past messages, but a
   re-run resumes forward — nothing replays. If traffic looks missing, search the
   archive before assuming collection failed.
8. **Dream failed or partial?** The Runs tab (or `memcal dream --retry RUN`)
   requeues readable lines under the current provider and model. Lines beyond the
   model horizon stay read.
9. **Stale brief?** `memcal schedule status` shows the last pass and whether one
   is owed; `memcal brief` re-renders from current state. In an agent session, a
   freshness-unavailable warning means prefetch failed — say so, do not trust an
   old snapshot.
10. **Still stuck?** `memcal trace` (exact model call), `memcal review` (last
    pass outcomes), `memcal stats` (volume history), and `calls/` (full audit)
    give, in that order, the complete picture.
