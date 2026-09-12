# GroupMe

Groups and direct messages through API v3 with a personal access token.

```bash
GROUPME_ACCESS_TOKEN=…
```

Add the token to `~/.memcal/.env`, verify with `memcal sources`, then:

```bash
memcal ingest groupme --limit 20
```

Per-conversation watermarks, newest-first history paging, and the shared
`--limit` budget and `--rounds` catch-up. Stable
opaque user ids identify people (display names are ignored); unknown ids land in
the [identity queue](../architecture/identity.md) that `memcal who` clears.
Re-runs are safe and idempotent.
