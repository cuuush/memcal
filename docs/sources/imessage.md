# iMessage

iMessage can be read two ways. Pick one with `MEMCAL_IMESSAGE_BACKEND`:

- **`bluebubbles`** (default): read through your [BlueBubbles](https://bluebubbles.app)
  server — groups, participants, attachments, and clean message text, and the server
  can run on another Mac. Set the password with `bluebubbles=` and, for a non-default
  address, the URL with `bluebubblesurl=` in the store's `.env`.
- **`chatdb`**: read `~/Library/Messages/chat.db` on this Mac directly. No server, no
  password; needs Full Disk Access for whatever runs the pass. `attributedBody` message
  bodies are decoded with the [`pytypedstream`](https://pypi.org/project/pytypedstream/)
  library.

Incremental on re-runs with its own stored cursor, deduplicated into the archive like
everything else. Verify with `memcal sources`, then `memcal ingest imessage --limit 20`.

Inbound handles resolve through Contacts at ingest, so phone numbers arrive already
attached to people. See [Identity](../architecture/identity.md).

## Choosing and tuning the backend

| Setting | Values | Default | What it does |
| --- | --- | --- | --- |
| `MEMCAL_IMESSAGE_BACKEND` | `bluebubbles`, `chatdb` | `bluebubbles` | Which transport to read iMessage through. `chatdb` never contacts BlueBubbles. |
| `MEMCAL_IMESSAGE_FALLBACK` | on/off | on | With the `bluebubbles` backend, read the local `chat.db` when the server is unreachable. Off makes an unreachable server a hard failure instead. |
| `MEMCAL_BLUEBUBBLES_LOCATION` | `auto`, `local`, `remote` | `auto` | Where the server runs, which decides whether opening the app could help. `auto` infers it from the URL (localhost is local); `local` forces "this Mac"; `remote` forces "another machine". |
| `MEMCAL_BLUEBUBBLES_AUTOSTART` | on/off | on | Open BlueBubbles when a local server is down (see below). |

Connection details live in the store's `.env`, not as `MEMCAL_*` settings:

- `bluebubbles=<password>` — the server password (required for the `bluebubbles` backend).
- `bluebubblesurl=<url>` — the server address. Defaults to `http://localhost:1234`. Point
  it at another machine for a remote server, e.g. `http://mymac.local:1234`.

## Opening BlueBubbles automatically

The BlueBubbles server only answers while its app is running. When the nightly (or
scheduled) pass finds a **local** server down, it opens the app for you — hidden and in
the background (`open -g -j`), so no window appears and it is never brought to
fullscreen — waits a few seconds for it to answer, then reads through it. If it does not
come up in time, the pass falls back to `chat.db` (unless the fallback is off).

This only ever happens from the nightly/scheduled pass, never from a plain
`memcal ingest`, and only when **all** of these hold:

- `MEMCAL_IMESSAGE_BACKEND` is `bluebubbles`,
- `MEMCAL_BLUEBUBBLES_AUTOSTART` is on,
- a `bluebubbles=` password is configured,
- the server is local — `MEMCAL_BLUEBUBBLES_LOCATION` is `local`, or `auto` with a
  localhost URL — so a **remote** server is never launched,
- the BlueBubbles app is installed on this Mac,
- the server is not already answering.

To stop it opening the app, set `MEMCAL_BLUEBUBBLES_AUTOSTART=0`; to keep it from ever
touching a machine that only hosts the server remotely, set
`MEMCAL_BLUEBUBBLES_LOCATION=remote`.

## Recipes

Local server, open it when it is down (the default):

```
MEMCAL_IMESSAGE_BACKEND=bluebubbles
# bluebubbles=<password> in .env; URL defaults to http://localhost:1234
```

Remote server on another Mac, never launch anything locally, fail rather than read a
local database:

```
MEMCAL_IMESSAGE_BACKEND=bluebubbles
MEMCAL_BLUEBUBBLES_LOCATION=remote
MEMCAL_IMESSAGE_FALLBACK=0
# bluebubblesurl=http://mymac.local:1234 and bluebubbles=<password> in .env
```

Skip BlueBubbles entirely and read the local database:

```
MEMCAL_IMESSAGE_BACKEND=chatdb
```
