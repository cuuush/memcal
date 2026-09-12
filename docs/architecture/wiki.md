# Wiki

One page per entity — people, places, projects, preferences. Prose plus named
slots, each line with a timestamp and source. Pages track their own open slots,
which give the agent standing curiosity ("Jordan's birthday, unknown — worth
asking when natural").

```bash
memcal pages
memcal page jordan
memcal page travel "preferred flight time" "morning" --section preferences
memcal note jordan birthday "May 3"
memcal alias jordan "Jord"
memcal merge --apply jordan-smith jordan   # fold one page into another
```

## Lazy, not prefilled

Pages are created when there is finally something to put on one — never from the
contact list. Three hundred contacts, most of whom do not matter, stay out until
they earn a page. Reading a page is an explicit tool call (`memcal_open_page`),
and the brief's `Pages:` line exists so the agent can decide to open one.

## Slots, not blobs

Durable facts belong on wiki pages; open obligations belong in to-dos. A slot fill
is a small keyed write with provenance, applied per-slot when two bundles touch
one page. Older installations may still contain legacy standing rows: their `S`
handles remain readable for recovery, but new standing writes are rejected.

## Hand-editable

Pages are Markdown files, readable in any editor. The dream pass reads pages from
disk every run and never caches its own last output — your edits are the state,
not a suggestion.
