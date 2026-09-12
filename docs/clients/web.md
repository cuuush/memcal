# Web UI

```bash
memcal ui
```

Listens on `http://127.0.0.1:8765` (loopback only). Eight tabs:

| Tab | Purpose |
|---|---|
| Gate | Post-gate volume — waiting, already read, everything; by conversation or flat |
| Chats | Per-conversation stats; muting keeps archive rows, stops model spend |
| Dream | Collect (free) → preview bundles → run (spends money); retry failed runs |
| Senders | Email gate table plus body backfill |
| Memory | Clickable brief; event detail with why/provenance and trace panels |
| Wiki | Pages with cited lines and encounters |
| Runs | Pass history with requests, bundles, and outcomes; filter and retry |
| Settings | Every `MEMCAL_*` setting with meaning, default, and file provenance |

The **Settings tab** is the full list of what memcal can be told. Saving writes
`~/.memcal/.env` — leaving hand-written lines alone — and applies to the running
process immediately. Clearing a field unsets the key and restores the built-in
default. Credentials report presence only, never values. Model fields open onto
priced provider models; executable fields offer resolved paths; calendar fields
offer calendars memcal has read.
