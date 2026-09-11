# Chat sources

Memcal reads the chats you already live in and archives new messages into its local,
append-only store. Start with one source, verify it with a small ingest, then add the
rest.

| Source | Page | Credential |
|---|---|---|
| Slack | [Setup](slack.md) | User OAuth token (`xoxp-…`) in `~/.memcal/.env` |
| Telegram | [Setup](telegram.md) | `telegram_api_id` / `telegram_api_hash` plus one interactive login |
| Signal | [Setup](signal.md) | `signal-cli link -n memcal`, scanned from your phone |

Credentials live in `~/.memcal/.env` (one `key=value` per line). `memcal sources`
reports what each source still needs; `memcal doctor` checks the whole setup.

## Comparison

| | Slack | Telegram | Signal |
|---|---|---|---|
| Auth type | User OAuth token (`xoxp-…`), no interactive login | `api_id`/`api_hash` + one interactive `memcal login telegram` (phone → code → 2FA), session file after | Linked device via `signal-cli link -n memcal` (QR scan on the phone), no `memcal login` step |
| Library | `slack_sdk>=3.27` (`pip install "memcal[slack]"`) | `telethon>=1.36` (`pip install "memcal[telegram]"`); `pip install "memcal[chat]"` gets both | No pip package — `signal-cli` JVM binary (`brew install signal-cli`), optional `signal_cli=` path override |
| Incremental model | `PolledSource`: per-conversation watermarks (`slack.<channel_id>`), newest-first history paging | `PolledSource`: per-chat watermarks (`telegram.<peer>:<id>`), newest-id shortcut skips quiet chats with no request | `StreamSource`: single destructive drain, one high-water cursor (`signal.received`) that cannot rewind |
| History availability | Full channel history, oldest-first per round (up to ~10k messages/conversation/round); pre-first-run dormant chats (> 30 days silent) skipped until they speak | Full dialog history via `min_id` forward walk; broadcast channels excluded; dormant chats (> 30 days) skipped on first run | **None before the link.** Only the undelivered queue from the link moment forward; no backfill exists |
| Rate limits | SDK retries (3× rate-limit, 2× connection) honoring `Retry-After`; 429 stops the round with `rate limited by slack — stopping this round`, watermarks committed | `FloodWaitError` stops the round with `rate limited by telegram — stopping this round`; memcal never retries through the limit | No API rate-limit handling; the bounds are `receive --timeout 10` and a 180s run ceiling, surfaced as `signal-cli timed out after 180s` |
| Risks | Token sees all DMs incl. private channels — guard `.env`; re-runs are safe and idempotent | Session file is full account access — guard `~/.memcal`; re-runs are safe and idempotent | **Destructive queue**: a crash between receive and commit loses messages permanently; `--limit` does not slice a drain; two homes must never share one link |

All three share the ingest economics: `--limit` (default 1000 items/round),
`--rounds` (default 25) via `catch_up`, `read / archived / queued` summaries with
`[more waiting]`, `N passed but older than 30d` spool-horizon notes, and
`N skipped as muted` for platform-muted threads (muting shapes priority and
presentation, never collection).

## Which order to set up

1. **Slack first** if you have it. No interactive login, fastest verify loop
   (`memcal sources` → `memcal ingest slack --limit 20`), and the richest history.
2. **Telegram second.** Needs the `my.telegram.org` app page and one interactive
   login; session reuse makes every later run non-interactive. Good history depth
   once logged in.
3. **Signal last, and promptly.** No history exists before the link, and the queue
   starts accumulating at link time — link, verify (`linked as …`), and ingest in
   one sitting so nothing waits acked-but-unarchived.

After each source: `memcal ingest <name> --limit 20`, read the summary, then
`memcal ingest all` and `memcal dream --dry-run` before adding the next one. If a
source misbehaves, its page's Troubleshooting section lists the exact error strings
the code produces; [Troubleshooting](../troubleshooting.md) covers the cross-source
checklist.
