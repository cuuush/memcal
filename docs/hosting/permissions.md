# Full Disk Access and the app bridge

On macOS, `memcal.app` owns access to local Messages, WhatsApp, and Contacts data.
CLI commands and the web UI send protected source work to this app over a private
local bridge. You can keep using your preferred terminal or agent without granting
it Full Disk Access.

```bash
memcal permissions status       # verify the app bridge
memcal permissions setup        # prepare the app and guide macOS approval
memcal ingest imessage          # protected reads run inside memcal.app
memcal ingest whatsapp
```

The same helper appears in **Settings → Full Disk Access** in the web UI. It reveals
`~/.memcal/memcal.app` and opens the system pane when approval is needed. You enable
that app and complete any macOS confirmation yourself. Full Disk Access covers
other apps' protected data as well as the sources enabled in Memcal. Calendar and
Automation consent are separate.

## How the bridge works

Each request starts a fresh worker through launchd under the existing Memcal app
identity. It shares the app identity used by the nightly job. Already-running
scheduled workers read directly, while interactive callers delegate source checks,
Messages/WhatsApp collection, and Contacts refresh to the app.

The protocol allows only those operations. It accepts no arbitrary shell command,
SQL, or source path. Request and response files live in a per-call directory readable
only by the current user and are removed afterward. Source ingestion and its cursors
stay in the app process; callers receive collection reports and continue using the
Memcal store. WhatsApp startup, readiness waits, and cleanup stay inside the app for
the complete collection rather than reopening it for every page.

A failed or timed-out bridge request reports an error; it does not ask for broader
terminal permissions. The next request starts a new worker. Archive identities and
stored cursors make retries safe.

## Setup and rechecking

```bash
memcal permissions setup --target memcal
memcal permissions setup --no-open --json
```

Explicit setup prepares a missing app bundle without installing a nightly job.
Approve `memcal.app` in System Settings → Privacy & Security → Full Disk Access,
then run `memcal permissions status` or use **Recheck source access** in Settings.
The next request starts a fresh app worker. A working grant is reused.

Status checks do not ingest messages or open/migrate the Memcal database. The web
helper also works if that database cannot open. Ingestion requests require an
initialized store and preserve collection ownership, message timestamps, and cursors.
On Linux the macOS bridge is inactive and source transports keep their existing path.

Apple's [Full Disk Access announcement](https://developer.apple.com/news/?id=p6zjojqw)
describes additional controls requiring explicit user action. Memcal keeps approval
in the system interface and verifies actual source reads through its app afterward.
