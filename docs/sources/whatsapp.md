# WhatsApp

Groups and direct messages from the macOS WhatsApp database
(`ChatStorage.sqlite`). No connector secret is needed. Install the desktop app and sign in there first.

```bash
memcal ingest whatsapp --limit 20
```

To point at a non-default database location, set the override in
`~/.memcal/.env`:

```bash
WHATSAPP_DB=/path/to/ChatStorage.sqlite
```

Incremental and idempotent on re-runs with its own stored cursor.
Verify with `memcal sources`, then collect and dream as usual (`memcal ingest
all`, `memcal dream --dry-run`, `memcal dream`).

## Opening the desktop app for collection

In Settings → Collection, enable **Open WhatsApp for dream**. This also applies to
explicit collection and the nightly source pull. The app opens hidden and in the
background. Periodic `ingest --due` checks read the database without launching the
app by default; doctor and source diagnostics never open it. Configuration:

```dotenv
MEMCAL_WHATSAPP_AUTOSTART=1
MEMCAL_WHATSAPP_AUTOSTART_BACKGROUND=0
MEMCAL_WHATSAPP_CLOSE_AFTER=1
MEMCAL_WHATSAPP_WAIT_SECONDS=30
```

Memcal waits up to the configured limit for a readable database, then collects
normally. It quits WhatsApp after collection only when it opened the app itself;
an already-running app stays open. Set `MEMCAL_WHATSAPP_CLOSE_AFTER=0` to leave a newly opened app
running. The wait is a maximum startup allowance, not a fixed delay after collection.

Only enable **Open WhatsApp during background checks** if you want periodic checks
to launch it too. The two startup settings are independent of closing the Memcal
web UI: launchd keeps scheduled work running until its job is disabled.

Protected source reads, WhatsApp startup, and cleanup run through the Memcal app
bridge. Enable Full Disk Access for `~/.memcal/memcal.app`; your terminal or agent
keeps its existing permissions. Opening WhatsApp cannot bypass the app's grant.
Run `memcal sources` and `memcal doctor` to verify the bridge and source readiness.

Use `memcal permissions setup` or **Settings → Full Disk Access** in the web UI for
[guided approval and rechecking](../hosting/permissions.md). The helper opens the
system pane and reveals the right app; macOS keeps approval in your hands.
