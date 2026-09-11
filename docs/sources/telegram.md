# Telegram

## What it reads

Telegram DMs, groups, and channels through the MTProto user API via Telethon. Bots
cannot see your DMs, so memcal signs in as your user account instead.

What gets skipped, by design:

- Broadcast channels you merely subscribe to (a feed, not a conversation).
- Service messages — joins, title changes, calls (`action` set on the message). They
  advance the watermark via `passed_over` but are never archived.
- Messages with no text and no media resolve to empty and are not archived.

Text goes through the same spoken-text cleaning as other sources; a media-only
message is archived as a kind summary (`[photo]`, `[video]`, `[document]`…).

## Prerequisites

- [ ] A Telegram account (phone number that can receive a login code).
- [ ] An `api_id` / `api_hash` pair from Telegram's API development tools.
- [ ] Python 3.11+ with memcal installed.
- [ ] `telethon` installed (see below).
- [ ] An interactive terminal for the one-time login (phone number → code → optional
  2FA password). Later runs reuse the session file non-interactively.

## Install

```bash
pip install "memcal[telegram]"
```

This installs `telethon>=1.36`. `pip install "memcal[chat]"` installs the Telegram
and Slack libraries together. Telethon is imported inside `connect()` (synchronous
`telethon.sync` client — memcal polls from cron and has no event loop to hand it),
so a missing library disables this source with a message in `memcal sources`
instead of breaking the registry.

## Credential setup

1. Sign in at [https://my.telegram.org](https://my.telegram.org) with your phone
   number (confirm the code Telegram sends you in the app).
2. Open **API development tools**. If you have no app yet, fill in **App title** and
   **Short name** (anything, e.g. `memcal` / `memcal`) and click **Create
   application**. Note the `api_id` (a number, e.g. `123456`) and `api_hash` (a hex
   string, e.g. `abcdef…`).
3. Add both to `~/.memcal/.env`, one `key=value` per line:

   ```bash
   telegram_api_id=123456
   telegram_api_hash=abcdef...
   ```

### Env reference

| Key | Required | Notes |
|---|---|---|
| `telegram_api_id` / `TELEGRAM_API_ID` | yes | Numeric. Either spelling works — lookup is case- and separator-insensitive, so `Telegram_Api_Id=` also matches. A non-numeric value fails with `TELEGRAM_API_ID must be a number: …`. |
| `telegram_api_hash` / `TELEGRAM_API_HASH` | yes | Hex string from the same app page. |

The file is `~/.memcal/.env` (or `$MEMCAL_HOME/.env` when `MEMCAL_HOME` is set).
Memcal loads `.env` from three paths — `<checkout>/.env`, then `$HOME/.env`, then
`./.env` — with later files overriding earlier ones; secrets are then looked up in
that merged map first and `os.environ` second. `Config.secret` normalizes keys by
lowercasing and dropping every non-alphanumeric character.

The session file is separate from `.env`: `~/.memcal/telegram.session`
(`cfg.home / "telegram.session"`). It grants full account access — keep `~/.memcal`
private.

## Login + verify

The first run needs one interactive login — Telethon prompts for your phone number,
the code Telegram sends you, and your two-factor password if you have one:

```bash
memcal login telegram
```

Expected success:

```text
telegram: signed in as Alice Liddell; session at ~/.memcal/telegram.session
```

(`signed in as {display name}; session at {session_path}` — the name is your
account's title/first+last name, `@username` fallback, or numeric id.)

Then verify non-interactively:

```bash
memcal sources
```

```text
ok telegram      Telegram DMs, groups and channels (MTProto user API, Telethon)
   connected as Alice Liddell
```

Expected failure shapes:

```text
-- telegram      Telegram DMs, groups and channels (MTProto user API, Telethon)
   no Telegram API credentials. Sign in at https://my.telegram.org, open 'API development tools', ...
```

```text
-- telegram      Telegram DMs, groups and channels (MTProto user API, Telethon)
   telethon is not installed — `pip install telethon`
```

```text
-- telegram      Telegram DMs, groups and channels (MTProto user API, Telethon)
   not logged in — run `memcal login telegram`
```

The last one means the session file is missing or unauthorized; during ingest the
same state raises `Telegram is not logged in yet — run \`memcal login telegram\`
once to sign in with your phone number.`

## First ingest

```bash
memcal ingest telegram --limit 20
```

```text
telegram: read 20, archived 19, queued 12
  58 chats, 21 with anything new, 9 dormant skipped on initial load
```

The noun here is `chats`, not `conversations`. `read / archived / queued` mean the
same as everywhere (see [Slack](slack.md#first-ingest)): pulled, stored
(deduplicated on `(telegram, <peer>:<message-id>)`), and spooled for the model.
Old-but-passing lines report as `N passed but older than 30d`; muted lines as
`N skipped as muted`; `[more waiting]` means budget or rounds stopped the run early.

Then collect everything and run a pass as usual:

```bash
memcal ingest all
memcal dream --dry-run
memcal dream
```

## How watermarks, budget, and rounds work here

Telegram is a `PolledSource`: it enumerates dialogs, then reads each one forward
from its own per-chat watermark (`telegram.<peer>:<id>` → message id, e.g.
`telegram.user:123456`).

- **Chat identity.** The watermark key includes the peer type (`user:`, `channel:`,
  `chat:`), so a user and a channel sharing a numeric id stay apart.
- **Newest-id shortcut.** The dialog listing already carries the newest message id.
  When it equals the stored watermark, the chat is skipped with no request — a quiet
  re-run costs one dialogs listing, not one call per chat.
- **History.** `iter_messages(entity, limit=min(limit, PAGE), min_id=<watermark>,
  reverse=True)` with `PAGE = 200`: forward from the exclusive watermark, so a
  partial page still leaves a watermark with nothing skipped past it. Ordering key
  is the numeric message id.
- **Budget.** `--limit N` (default 1000) caps items per round; each chat takes at
  most `min(200, remaining budget)`. Chats are read newest-first by dialog date, so
  an exhausted budget lands on live conversation. Exhaustion sets `report.more` and
  prints `[more waiting]`.
- **Rounds.** Shared `catch_up` loop: up to `--rounds 25` while `more` is set,
  `stopped after N rounds — the last one added nothing …` on a stalled round,
  `stopped at 25 rounds with more waiting — run again, or raise --rounds` at the
  cap, `caught up over N rounds` on multi-round success.
- **Re-runs.** Idempotent: archive dedupe on `(stream, external_id)` plus
  per-chat `min_id` watermarks. Nothing-new re-runs make no message calls.
- **Dormant-chat skip.** First run only: a chat whose last dialog date is older than
  `initial_days = 30` is not read at all (`N dormant skipped on initial load`).
  Unknown dates are read, not dropped.
- **Muted/archived.** Telegram `archived` dialogs are ingested but recorded muted
  with the platform note `archived in Telegram`. Group detection: megagroup flag,
  participant count, or a title. Spool-time handling follows `MEMCAL_PLATFORM_MUTE`
  (`show` default).
- **Connection hygiene.** Ingest disconnects the Telethon client in a `finally`
  after every `fetch` — one leaked socket per run otherwise. `check()` opens and
  closes its own connection.

## Rate limits

Telethon raises `FloodWaitError` (carrying the seconds Telegram wants back) when the
account hits Telegram's limits. `is_rate_limit` matches exactly that class name and
stops the round:

```text
telegram: read 410, archived 400, queued 260  [more waiting]
  rate limited by telegram — stopping this round
```

Memcal backs off by stopping — it does not retry through the limit. Watermarks for
chats already read are committed, and the remaining chats wait for the next round or
run. Non-rate-limit per-chat errors are logged as `  <chat>: <error…>` notes and the
round continues.

## Troubleshooting

1. **`telegram: no Telegram API credentials. Sign in at https://my.telegram.org …`**
   — neither `telegram_api_id` nor `telegram_api_hash` matched. Check the file path
   (`~/.memcal/.env` vs `$MEMCAL_HOME/.env`), spelling, and that each is on its own
   `key=value` line. `memcal sources` shows the same message truncated to 120 chars.
2. **`telegram: TELEGRAM_API_ID must be a number: …`** — the id has letters, quotes,
   or trailing whitespace. It must be the bare integer from the app page (e.g.
   `123456`), not the app title.
3. **`telegram: telethon is not installed — \`pip install telethon\``** — install the
   extra: `pip install "memcal[telegram]"`.
4. **`telegram: Telegram is not logged in yet — run \`memcal login telegram\` …`**
   (`not logged in — run \`memcal login telegram\`` in `memcal sources`) — the
   session file is absent or unauthorized. Run `memcal login telegram` interactively
   and complete phone → code → 2FA. If login succeeds but ingest still says this,
   `MEMCAL_HOME` differs between the two invocations and they are using different
   session files — compare `memcal login telegram` output path with your ingest env.
5. **`telegram: FloodWaitError …` / `rate limited by telegram — stopping this round`
   on every run** — the account is rate-limited. Stop ingesting for a while (Telegram
   backs off in minutes-to-hours depending on severity); watermarks are committed so
   nothing re-reads. Repeated large first-loads with high `--limit` trigger this —
   use smaller `--limit` values (e.g. `--limit 200`) spaced apart.
6. **A channel I follow never appears.** Subscribed broadcast channels are skipped by
   design (feeds, not conversations). Only groups, megagroups, and DMs are listed.
7. **Other Telethon errors are truncated.** `check()` cuts messages to 80 characters
   and per-chat notes to 80 characters. For the full text, run
   `memcal ingest telegram --limit 20` and read the `telegram: …` error line, which
   carries the untruncated `SourceError` (or `Type: detail` for unexpected
   exceptions). Anything Telethon raises that is not `FloodWaitError` is treated as
   a per-chat skip or a clean run-level failure — never a silent drop.

When reporting a problem, paste `memcal sources` output and the ingest summary
lines. Never paste `telegram_api_hash` or the session file — `memcal sources`
prints neither.

## FAQ

**Why a user API instead of a bot?**
Bots cannot see your DMs. Telegram publishes a user API and expects clients to be
built on it, so memcal signs in as you (same reason as the Slack user token).

**What happens if I delete the session file?**
The account is simply logged out: `check()` reports `not logged in` and ingest
raises the login error. Nothing archived is lost (watermarks live in the database,
not the session). Re-run `memcal login telegram` to sign in again.

**Will re-running ingest duplicate everything?**
No — archive rows deduplicate on `(telegram, <peer>:<message-id>)`, and each chat
resumes from its `min_id` watermark.

**Does the first ingest read years of history?**
It reads every non-dormant, non-broadcast chat from the oldest available message,
bounded by `--limit` per round and `--rounds 25` by default. Dormant chats (silent
> 30 days) are skipped until they speak again. Lines older than 30 days
(`spool_horizon_days`) are archived and searchable but reported as
`passed but older than 30d` instead of queued for the model.

## Safety

The session file at `~/.memcal/telegram.session` grants **full access to your Telegram
account** — anyone holding it is you. Keep `~/.memcal` private (mode 0700 directory
is a good idea). If Telegram rate-limits collection, memcal stops the round
(`FloodWait`) and continues on the next run rather than retrying through the limit.
