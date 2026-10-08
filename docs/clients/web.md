# Web UI

```bash
memcal ui
```

Listens on `http://127.0.0.1:8765` (loopback only). Opens on Memory.

| Page | Purpose |
|---|---|
| Memory | Current brief; select a memory to inspect its sources |
| Wiki | People, places, and projects; edit facts and review evidence |
| Conversations | Review names and mute threads without removing their archive |
| Inbox | Waiting, read, and archived messages; search and filter by source |
| Dream | Collect messages, review input, and run Dream using model quota |
| History | Past runs, outcomes, costs, and detailed requests; retry incomplete runs |
| Email rules | Choose which senders are processed, archived, or ignored |
| Settings | Model, sources, credentials, preferences, and nightly automation |

Dream shows its actions and selection summary first. Open **Input preview** to
inspect context, requests, and conversations. History keeps token metrics in run
details. Settings groups expand individually; searching opens matching groups.
The color theme persists in your browser, and navigation adapts to narrow screens.

Memory shows the next 30 days by default, with participants and tentative status
on each plan. Details show facts first; history and diagnostics are expandable.
Wiki facts open their cited message IDs on demand, and an existing wiki page can
be edited as Markdown. Dream shows the live pass's stage and progress even when
it was started elsewhere. History includes **Saved dream log** and links successful
resumes to the earlier failed run.

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
