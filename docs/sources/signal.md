# Signal

## What it reads

Signal DMs and groups through `signal-cli`, linked the same way Signal Desktop links:
as another device on your account. There are two hard constraints that follow from
how Signal works:

- **No server-side history.** Signal keeps only an undelivered-message queue. Only
  messages arriving *after* the link can be read — history from before the link is
  simply unavailable, and there is nothing to backfill.
- **The receive queue is destructive.** Draining it acknowledges messages off the
  server. A crash between receiving and archiving **loses those messages** — they
  will not replay on the next run.

Signal is therefore a `StreamSource`, not a `PolledSource`: one ordered drain from
one cursor, no conversation list to enumerate, no paging. Receipts and typing
notices (anything that is neither a `dataMessage` nor a `syncMessage.sentMessage`)
are skipped. A message you sent from another device arrives as a sync message and
is recorded `from_me`. Group threads are named via `listGroups`; unknown groups
fall back to `Signal group <id-prefix>`. DMs are named for the other person
(`sourceName` or the phone number).

## Prerequisites

- [ ] A Signal account with the phone app available (linking scans a QR code / confirms
  from the phone).
- [ ] Java runtime for `signal-cli` (it is a JVM program, not a pip package).
- [ ] `signal-cli` on `PATH` (see below).
- [ ] Python 3.11+ with memcal installed. No `memcal[…]` extra is needed for Signal.
- [ ] Ingest promptly after linking — anything in the queue is acked on drain.

## Install

```bash
brew install signal-cli
```

Any install that puts a working `signal-cli` binary on `PATH` is fine. To use a
binary that is not on `PATH`, set the override in `~/.memcal/.env`:

```bash
signal_cli=/path/to/signal-cli
```

## Credential setup

Link `signal-cli` as a device. On the phone: Signal → Settings → Linked devices →
**Link New Device**, then scan the QR code this prints:

```bash
signal-cli link -n memcal
```

There is no `memcal login signal` step — the link is the login. (`memcal login
signal` answers `<name> needs no interactive login` for sources without an
interactive `setup()`; Signal is one of them.)

If `signal-cli` holds several accounts, choose one in `~/.memcal/.env`:

```bash
signal_account=+15551234567
```

### Env reference

| Key | Required | Notes |
|---|---|---|
| `signal_account` / `SIGNAL_ACCOUNT` | only with several linked accounts | Phone number of the account to drain, e.g. `+15551234567`. With exactly one linked account it is auto-selected; with several, ingest refuses until you set this. |
| `signal_cli` / `SIGNAL_CLI` | no | Override path to the `signal-cli` binary. Default: `signal-cli` resolved from `PATH` via `shutil.which`. |

The file is `~/.memcal/.env` (or `$MEMCAL_HOME/.env` when `MEMCAL_HOME` is set).
Lookup is case- and separator-insensitive (`signal_account`, `SIGNAL_ACCOUNT`,
`signal-account` all match); secrets are read from the merged `.env` (checkout,
home, cwd — later wins) first, then `os.environ`.

## Login + verify

Verify with:

```bash
memcal sources
```

Expected success:

```text
ok signal        Signal DMs and groups (signal-cli, linked device)
   linked as +15551234567, 4 group(s)
```

(`linked as {number}, {n} group(s)` — the group count comes from `listGroups`.)

Expected failure shapes:

```text
-- signal        Signal DMs and groups (signal-cli, linked device)
   signal-cli not found — `brew install signal-cli`, then `signal-cli link -n memcal` ...
```

```text
-- signal        Signal DMs and groups (signal-cli, linked device)
   no linked account — run `signal-cli link -n memcal`
```

## First ingest

Ingest **promptly after linking** — the queue starts accumulating immediately:

```bash
memcal ingest signal --limit 20
```

```text
signal: read 3, archived 3, queued 2
```

Two Signal-specific notes on this output:

- `--limit` does **not** slice the drain. `receive()` acks everything off the server
  queue, so stopping early would drop already-acked messages with no replay — the
  source ignores the limit on purpose and processes the whole drain. `limit` only
  feeds `report.more`: when the drain size reaches the budget, the report is marked
  `[more waiting]` so `catch_up` schedules another round.
- There is no `N chats, M with anything new` or dormant-skipped line: a
  `StreamSource` has no conversation listing, so there is nothing to enumerate.

Then collect everything and run a pass as usual:

```bash
memcal ingest all
memcal dream --dry-run
memcal dream
```

## How watermarks, budget, and rounds work here

Signal is a `StreamSource`: one stream, one cursor key (`signal.received`).

- **The cursor is a high-water mark, not a rewind cursor.** The ack happens inside
  `signal-cli` during `receive()`. The stored watermark (latest send timestamp) is
  for reporting only — it cannot replay anything. `since` is ignored on every drain
  because the server queue *is* the cursor.
- **One drain per round.** `receive` runs
  `signal-cli receive --timeout 10 --ignore-attachments`: up to 10s waiting for the
  server to go quiet (`RECEIVE_TIMEOUT = 10`), with a 180s ceiling (`RUN_TIMEOUT`)
  so a long silence cannot block a nightly pass indefinitely. `--ignore-attachments`
  skips attachment downloads; messages with attachments still archive their text
  (or a `[kind]` fallback).
- **Dedupe key.** There is no Signal message id — the send timestamp is also the
  dedupe key: `external_id` is `<dm:number>:<ts>` or `<group:id>:<ts>`.
- **Re-runs.** A re-run with an empty queue drains nothing and archives nothing.
  Because the ack is destructive, a re-run can never recover messages lost to a
  crash between `receive` and commit — unlike `PolledSource` re-runs, which simply
  resume from watermarks.
- **No dormant-chat skip, no muted handling.** Both are `PolledSource` concepts
  (conversation listing + `initial_days = 30`). Signal conversations carry no mute
  flag; group-vs-DM is the only shape recorded.
- **Rounds.** The shared `catch_up` loop still applies: if a drain hits the budget
  (`len(items) >= want`), `more` is set and up to `--rounds 25` further drains run.

## Rate limits

Signal has no documented per-method rate limiting in this path, and the source
defines no `is_rate_limit` — every `StreamSource` default returns `False`. What you
can hit instead are the two timeouts, surfaced as clean run-level errors:

```text
signal: signal-cli timed out after 180s
```

The watermark and everything drained before the failure are committed per round, so
the next run continues from the high-water mark. Per-drain `signal-cli` failures
raise `signal-cli failed: <last stderr/stdout line, ≤160 chars>` and fail the round
cleanly without taking down `ingest all` for other sources.

## Troubleshooting

1. **`signal: signal-cli not found — \`brew install signal-cli\`, then
   \`signal-cli link -n memcal\` …`** (check view; ingest raises
   `signal-cli not found at {binary}`) — no binary on `PATH` and no `signal_cli`
   override. Install it, or set `signal_cli=/path/to/signal-cli` in
   `~/.memcal/.env`.
2. **`signal: signal-cli has no linked account — run \`signal-cli link -n memcal\`
   …`** (`no linked account — run \`signal-cli link -n memcal\`` in
   `memcal sources`) — `listAccounts` came back empty. Link from the phone again.
3. **`signal: signal-cli has several accounts (…, …) — set \`signal_account=\` in
   memcal/.env …`** — disambiguation refusal. Add the wanted number as
   `signal_account=+15551234567` (any case/separator spelling works).
4. **`signal: signal-cli failed: <last line>`** — `signal-cli` exited nonzero; the
   message is the last line of its stderr/stdout (≤160 chars). Re-run the same
   subcommand by hand (`signal-cli -a +15551234567 receive --timeout 10
   --ignore-attachments`, `listAccounts`, `listGroups -d`) to see the full error —
   common causes are a locked/conflicting `signal-cli` data directory or a Java
   failure.
5. **`signal: signal-cli timed out after 180s`** — the drain hung (usually network).
   Nothing drained is lost that wasn't already acked; just run
   `memcal ingest signal` again.
6. **Ingest works but archives nothing, ever.** Almost certainly receipts/typing
   notices only, or an empty queue — both normalize to `None` and are skipped
   silently. Also: messages sent *before* the link never existed server-side for
   this device. There is no backfill to fix that; only post-link traffic arrives.
7. **Group names are `Signal group a1b2c3d4`.** `listGroups -d` returned nothing for
   that id (sync lag after linking is the usual cause). Names resolve on later runs
   once the group list populates; archived rows keep whatever name was current and
   threads key on the stable `group:<id>`, so a rename never splits history.

When reporting a problem, paste `memcal sources` output, the ingest summary line,
and the matching hand-run `signal-cli` output. Never paste anything that could
receive messages as you — account numbers are fine to redact to `+1555…`.

## FAQ

**Why can't I backfill old Signal history?**
Signal keeps no server-side archive — only the undelivered queue. A newly linked
device sees messages from the link moment forward. This is a Signal property, not a
memcal limitation.

**Does `--limit` do anything for Signal?**
Only as the `more`-budget: the drain itself is always whole (slicing it would drop
acked messages). If a drain reaches the budget, `catch_up` runs another round.

**Can two memcal homes share one linked device?**
Don't. Both would drain the same destructive queue and each would see a random
subset. One home per linked account; pick it with `signal_account` if the machine
links several numbers.

**Is `memcal login signal` a thing?**
No. Linking (`signal-cli link -n memcal` + phone scan) is the login, and `memcal
sources` is the verification. There is no interactive `setup()` on this source.

## Safety

The Signal receive queue is **destructive**: draining it acknowledges messages off the
server, so a crash between receiving and archiving **loses those messages** — they
will not replay on the next run. Ingest promptly after linking, and treat the linked
device the way you would treat Signal Desktop: someone with the machine reads your
Signal.
