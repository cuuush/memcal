# Wiki

One page per entity — people, places, projects. Prose plus named
slots, each line with a timestamp and source. Pages track their own open slots,
which give the agent standing curiosity ("Jordan's birthday, unknown — worth
asking when natural").

```bash
memcal pages
memcal page jordan
memcal note me "preferred flight time" "morning"
memcal note jordan birthday "May 3"
memcal alias jordan "Jord"
memcal merge --apply jordan-smith jordan   # fold one page into another
```

## Lazy, not prefilled

Pages are created when there is finally something to put on one — never from the
contact list. Three hundred contacts, most of whom do not matter, stay out until
they earn a page. Reading a page is an explicit tool call (`memcal_open`),
and the brief's `Pages:` line exists so the agent can decide to open one.

`memcal_open me` resolves the user's existing page, including a page under their
full name. Reads do not create a page; an explicit note or evidence-backed dream
write can create the first one. Multiple plausible self pages are reported as
ambiguous. The brief's **About you** line exposes stored self facts or a pointer
to their page. When the page is unknown, `memcal_search_wiki` searches names,
aliases, fact labels, values, and prose before an archive lookup.

Legacy `preferences/` pages remain readable and migrate into `people/`; new writes
use only people, places, or projects. Personal preferences belong on the user's page.

## Slots, not blobs

Durable facts belong on wiki pages; open obligations belong in to-dos. A slot fill
is a small keyed write with provenance, applied per-slot when two bundles touch
one page. Older installations may still contain legacy standing rows: their `S`
handles remain readable for recovery, but new standing writes are rejected.

## Hand-editable

Pages are Markdown files, readable in any editor. The dream pass reads pages from
disk every run and never caches its own last output — your edits are the state,
not a suggestion. The web Wiki page can edit Markdown directly. Facts show their
cited message IDs and source history, with evidence expanded on demand.
