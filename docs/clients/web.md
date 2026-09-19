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
the models memcal can price for the provider you have chosen — named the way
that provider names them, with their rates — plus any this store has already
run. Picking a provider re-asks before you save, so the list follows the choice
you are making; one control sets the propose, sweep, and merge models together.
Model fields reject names known to belong to another provider, and changing
providers resets such fields to the new default. Executable fields offer
resolved paths; calendar fields offer calendars memcal has read. The tab also
shows every source with an on/off toggle (disabled sources are skipped by
Collect, `ingest all`, due checks, and the nightly pull), the credentials each
source needs, the `.env` files behind every value, and what the nightly job is
doing.
