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
background; doctor and source diagnostics never open it. Configuration:

```dotenv
MEMCAL_WHATSAPP_AUTOSTART=1
MEMCAL_WHATSAPP_CLOSE_AFTER=1
MEMCAL_WHATSAPP_WAIT_SECONDS=30
```

Memcal waits up to the configured limit for a readable database, then collects
normally. It quits WhatsApp after collection only when it opened the app itself;
an already-running app stays open. Set `MEMCAL_WHATSAPP_CLOSE_AFTER=0` to leave a newly opened app
running. The wait is a maximum startup allowance, not a fixed delay after collection.

An unreadable database may still need macOS permission: System Settings → Privacy &
Security → Full Disk Access → add and enable `~/.memcal/memcal.app`, then quit and
reopen Memcal. If running without the wrapper, grant access to the terminal or agent
app that runs Python. Opening WhatsApp cannot bypass this permission. Run
`memcal sources` and `memcal doctor` to verify the connector and scheduled requester.

macOS can retain the terminal/agent as the responsible requester even when a CLI
command passes through `memcal.app`. If doctor's scheduled probe passes but the
interactive source check fails, the scheduled grant is already working: enable Full
Disk Access for the terminal/agent running that command, then quit and reopen that
app. Doctor states this distinction explicitly; reinstalling the schedule cannot
change the interactive host's privacy grant.
