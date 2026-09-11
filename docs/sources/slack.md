# Slack

## What it reads

Slack DMs, group DMs, and channels (public and private) through the `slack_sdk`
`WebClient`. It needs a **user token** (`xoxp-…`), not a bot token — bot tokens cannot
see your DMs (`im:history` covers them).

What gets skipped, by design:

- Channel join/leave/pin/rename notices (any message whose `subtype` is not spoken
  text: `""`, `thread_broadcast`, `me_message`, `file_share`).
- App and workflow posts made by bots (`bot_id` set, no `user`) — they make no
  commitments.
- Your note-to-self DM (the `is_im` channel addressed to your own user id).
- Archived channels — the listing passes `exclude_archived=True`, so they never
  appear.

Markup is resolved on the way in: `<@U123>` becomes the display name, `<#C123|name>`
becomes `#name`, `<https://…|text>` keeps the text a human wrote, `<!here>` /
`<!channel>` become `@here` / `@channel`, and Slack's `&lt;` / `&gt;` / `&amp;`
escapes are unescaped. A message with no text falls back to a file summary
(`[image]`, `[video, attachment]`), and an empty body is passed over — it still
advances the watermark so it is not re-requested forever.

## Prerequisites

- [ ] A Slack workspace you are a member of.
- [ ] Permission to create an app and install it to the workspace. On locked-down
  workspaces this needs an admin to approve the install.
- [ ] Python 3.11+ with memcal installed.
- [ ] `slack_sdk` installed (see below).
- [ ] A `~/.memcal/.env` file you can edit (created by `memcal init` / `memcal setup`).

## Install

```bash
pip install "memcal[slack]"
```

This installs `slack_sdk>=3.27`. `pip install "memcal[chat]"` installs the Slack and
Telegram libraries together. The SDK is imported inside `connect()`, not at module
scope — a missing library disables this source with a message in `memcal sources`
instead of breaking the registry.

## Credential setup

Slack has no `memcal login` step — the token is the login.

1. Go to [https://api.slack.com/apps](https://api.slack.com/apps) and click
   **Create New App → From scratch**. Give it a name (e.g. `memcal`) and pick your
   workspace.
2. In the left sidebar open **OAuth & Permissions**. Scroll to **Scopes → User Token
   Scopes** (not Bot Token Scopes) and add each of these:

   | Scope | Why memcal needs it |
   |---|---|
   | `im:history` | Read your DMs — the core of this source |
   | `mpim:history` | Read group DMs |
   | `channels:history` | Read public channels you are in |
   | `groups:history` | Read private channels you are in |
   | `users:read` | Resolve user ids to display names once, instead of one lookup per message |
   | `channels:read` | Resolve channel ids to names for `#mentions` |

3. Scroll up and click **Install to Workspace** (or **Reinstall** if the app already
   exists), then **Allow**.
4. Copy the **User OAuth Token** — it starts with `xoxp-`. A token starting with
   `xoxb-` is the bot token and will not work for DMs.
5. Add it to `~/.memcal/.env`, one `key=value` per line:

   ```bash
   slack=xoxp-...
   ```

### Env reference

| Key | Required | Notes |
|---|---|---|
| `slack` / `SLACK_TOKEN` | yes | User OAuth token (`xoxp-…`). Either spelling works — lookup is case- and separator-insensitive (see below), so `SLACK_TOKEN=`, `slack=`, `Slack-Token=` all match. |
| `SLACK_USER_ID` | no — do not set | There is deliberately no such setting: `auth_test()` already returns your user id. A `slack` + `slack_user_id` pair trips the prefix match in `Config.secret`, so setting one can shadow the token lookup. |

The file is `~/.memcal/.env` (or `$MEMCAL_HOME/.env` when `MEMCAL_HOME` is set).
Memcal loads `.env` from three paths — `<checkout>/.env`, then `$HOME/.env`, then
`./.env` — with later files overriding earlier ones; secrets are then looked up in
that merged map first and `os.environ` second. `Config.secret` normalizes keys by
lowercasing and dropping every non-alphanumeric character, so `slack`, `SLACK`,
`SLACK_TOKEN`, and `slack-token` are the same key. There is also a one-directional
prefix match on the longest alias (minimum 5 characters), so a verbosely-named
variable still answers — but a short key never satisfies a long lookup.

## Login + verify

There is no interactive login. Verify with:

```bash
memcal sources
```

Expected success (one line per registered source, plus the plugin directory):

```text
ok slack        Slack DMs, group DMs and channels (user token, slack_sdk)
   connected as alice in acme
```

The detail line is `connected as {user}` plus ` in {team}` when the workspace name is
known. Expected failure shapes:

```text
-- slack        Slack DMs, group DMs and channels (user token, slack_sdk)
   no Slack token. Add `slack=xoxp-...` to memcal/.env — create an app at ...
```

```text
-- slack        Slack DMs, group DMs and channels (user token, slack_sdk)
   slack_sdk is not installed — `pip install slack_sdk`
```

```text
-- slack        Slack DMs, group DMs and channels (user token, slack_sdk)
   invalid_auth
```

The last line is the raw reason from `auth_test()` truncated to 80 characters
(`Slack rejected the token: invalid_auth` in full during ingest). A scope warning is
appended to an otherwise-ok line when the token's granted scopes (read from the
`x-oauth-scopes` response header) are missing the essentials:

```text
ok slack        Slack DMs, group DMs and channels (user token, slack_sdk)
   connected as alice in acme — missing scope(s): im:history, users:read; DMs will not be read
```

## First ingest

Start small, read the status lines, then collect everything:

```bash
memcal ingest slack --limit 20
```

A `PolledSource` ingest walks three phases — `connecting`, `listing`, `reading` —
and prints one summary per round plus notes:

```text
slack: read 20, archived 18, queued 15
  42 conversations, 30 with anything new, 12 dormant skipped on initial load
```

How to read it:

- `read` — items pulled from Slack this run (including passed-over notices).
- `archived` — rows appended to the local archive (deduplicated on
  `(stream, external_id)`; a replay is safe but wasteful).
- `queued` — lines that passed the gate and were spooled for the model pass.
  Suffixes appear when relevant: `N passed but older than 30d` (outside the spool
  horizon), `N skipped as muted`, `unresolved handles N`, and `[more waiting]`
  when the budget or round cap stopped the run before exhaustion.
- `42 conversations, 30 with anything new` — the listing found 42 readable
  conversations; 30 had no watermark yet-anew or a newer message than the watermark.
  Conversations whose listing already reports the watermarked newest id cost no
  request at all.
- `12 dormant skipped on initial load` — see below. Only appears when nonzero.

Then collect everything and run a pass as usual:

```bash
memcal ingest all
memcal dream --dry-run
memcal dream
```

## How watermarks, budget, and rounds work here

Slack is a `PolledSource`: it enumerates conversations, then reads each one forward
from its own per-conversation watermark (`slack.<channel_id>` → Slack `ts`).

- **Budget.** `--limit N` (default 1000) caps items per round; each conversation takes
  at most `min(page, remaining budget)` where `page` is 200. Conversations are read
  newest-first by listing recency, so an exhausted budget is spent on live
  conversation first. When the budget runs out mid-list, the report sets
  `report.more = True` and prints `[more waiting]`.
- **Rounds.** `memcal ingest` calls `catch_up`, which repeats rounds (default
  `--rounds 25`) while `report.more` is set and no error occurred. It stops early
  with `stopped after N rounds — the last one added nothing (rate limited, or
  genuinely done)` when a round archives nothing, and with
  `stopped at 25 rounds with more waiting — run again, or raise --rounds` when the
  cap is hit. Multiple rounds print `caught up over N rounds`.
- **Paging inside one conversation.** Slack returns `conversations.history`
  newest-first with `limit=200` per page. memcal pages up to 50 pages (≈10k
  messages) per conversation per round, sorts oldest-first, and hands back only the
  oldest `want` — returning just the first page would archive the newest 200 and
  strand older backlog behind the watermark forever. Past 10k, the next round
  continues from the advanced watermark.
- **Re-runs.** Idempotent. The watermark advances in platform order (`ts` as float),
  so a partial round cannot skip past messages; already-archived
  `(slack, <channel>:<ts>)` rows deduplicate. A second run with nothing new reads
  the listing, matches every `newest` against its watermark, and makes no history
  calls.
- **Dormant-chat skip.** On a first run (no watermark yet), a conversation silent
  longer than `initial_days = 30` is not read at all — dormant chats are common and
  each one costs a request to learn nothing. Unknown activity time means *read it*,
  not drop it. The skip only applies before the first watermark is stored; once a
  conversation has a watermark it is always checked.
- **Muted.** `is_muted` channels are still read and archived, recorded with the
  platform note `muted in Slack`, and their lines count as `skipped as muted` at
  spool time (depending on `MEMCAL_PLATFORM_MUTE`: `show` archives and displays
  without priority, `ask` enqueues for review, `mute` drops). Muting never deletes
  archive rows.

## Rate limits

The SDK client is built with `RateLimitErrorRetryHandler(max_retry_count=3)` and
`ConnectionErrorRetryHandler(max_retry_count=2)` — Slack's `Retry-After` is honoured
inside the SDK, there is no hand-rolled backoff. An error whose response status is
429 (`is_rate_limit`) stops the round rather than being logged-and-skipped:

```text
slack: read 312, archived 300, queued 210  [more waiting]
  rate limited by slack — stopping this round
```

What you see is the normal summary with `[more waiting]` plus that note. The
watermarks for conversations already read are committed, so the next run (or the
next round, if retries inside the SDK absorbed it) picks up where this one stopped.
Per-conversation errors that are *not* rate limits — no access, removed mid-run —
are normal at Slack scale and appear as `  <conversation>: <error…>` notes while
the round continues.

## Troubleshooting

1. **`slack: no Slack token. Add \`slack=xoxp-...\` to memcal/.env …`** — no key
   matched `SLACK_TOKEN`/`slack` in the merged `.env` or environment. Check the file
   path (`~/.memcal/.env`, or `$MEMCAL_HOME/.env`), one `key=value` per line, no
   quotes needed. Run `memcal sources` first — it shows the same message.
2. **`slack: Slack rejected the token: invalid_auth`** (or `token_revoked`,
   `account_inactive`) — the token is wrong, revoked, or for a deleted app. Reinstall
   the app at [https://api.slack.com/apps](https://api.slack.com/apps) and copy the
   User OAuth Token again. `check()` truncates this to 80 characters in
   `memcal sources`.
3. **`slack: slack_sdk is not installed — \`pip install slack_sdk\``** — install the
   extra: `pip install "memcal[slack]"`. The import happens in `connect()` so this
   shows as a per-source message, not a crash.
4. **`… — missing scope(s): im:history, users:read; DMs will not be read`** — the
   token was issued before all scopes were added, or bot scopes were used instead of
   user scopes. Add the missing scopes under **OAuth & Permissions → User Token
   Scopes** and **Reinstall** the app; OAuth tokens are frozen at install time, so
   editing scopes without reinstalling changes nothing.
5. **A channel never appears.** Archived channels are excluded by the listing;
   unarchive it in Slack. The note-to-self DM and pure-bot channels are skipped by
   design. A channel you were removed from surfaces as a per-conversation note
   (`<name>: channel_not_found` or similar) and the round continues.
6. **Bot/app messages missing from a channel.** `bot_id`-only posts and non-spoken
   subtypes (joins, pins, renames) are passed over deliberately — they advance the
   watermark but are never archived. If a human's message is missing, check the
   watermark didn't advance past it during a partial round (re-runs resume forward;
   watermarks never rewind).
7. **`rate limited by slack — stopping this round` on every run.** The SDK retried
   3× per request and Slack still said 429. Wait a few minutes and run
   `memcal ingest slack` again — watermarks were committed, so nothing re-reads.
   If it persists, lower the per-round cost with `--limit` (e.g. `--limit 200`).

Never paste the token when asking for help — send the output of `memcal sources`
(which prints only the workspace/user, never the secret) plus the summary lines.

## FAQ

**User token or bot token?**
User token (`xoxp-…`). Bot tokens (`xoxb-…`) cannot see your DMs, and DMs are the
point. The source docstring and the missing-token error both say this explicitly.

**Why do I have to Reinstall after adding scopes?**
Slack freezes a token's scopes at install time. Adding `im:history` to the app
definition does nothing to already-issued tokens until you click Reinstall and copy
the new User OAuth Token.

**Will a re-run duplicate everything?**
No. Archive rows deduplicate on `(stream, external_id)` = `(slack,
<channel>:<ts>)`, and per-conversation watermarks mean unchanged conversations cost
zero history calls on the next run.

**Does memcal read my whole Slack history on day one?**
Almost: every non-dormant conversation (active within 30 days) is read from the
oldest available message, up to ~10k messages per conversation per round, across up
to 25 rounds by default. Dormant chats are skipped until they speak again. Old lines
are still archived and searchable; only the last 30 days (`spool_horizon_days`) are
queued for the model.

**Why is a muted channel still being read?**
Muting controls priority and presentation, not collection. Muted lines are archived
and searchable but counted as `skipped as muted` instead of queued (under the
default `platform_mute=show`).

## Safety

A Slack user token with these scopes sees **all of your DMs**, including private
channels and group DMs. Treat `~/.memcal/.env` as sensitive: keep it out of version
control and protect it with normal filesystem access controls.
