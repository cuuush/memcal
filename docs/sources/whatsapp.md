# WhatsApp

Groups and direct messages from the macOS WhatsApp database
(`ChatStorage.sqlite`). No secret and no login — it reads the local app data.

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
