# Email

Mail arrives over IMAP, including via Proton Mail Bridge (`127.0.0.1:1143`).
Authenticate with the Bridge credentials in `~/.memcal/.env`:

```bash
PROTON_BRIDGE_USER=…
PROTON_BRIDGE_PASSWORD=…
```

## Priority, not exclusion

Every message inside your folders and time range gets its body fetched and
archived before anything decides relevance. Automatic relevance sets a
*priority*: list mail, retailers, and no-reply addresses queue low — read after
everything else in bounded batches, searchable the whole time. The only thing
that keeps a sender out entirely is you saying so (`memcal senders <address>
ignore`, "I don't care about this" to the agent, or the block button in the web
queue). A blocked sender's body is never fetched or stored.

To reject a scam sender and retire its queued mail:

```bash
memcal block scammer@example.com --reason "Impersonation scam"
memcal block --event 123 --reason "Impersonation scam"
```

An email event is resolved through its cited sender address, even when its display
name looks official. If the evidence has multiple senders or mixes email and chat,
give an explicit address. Blocking does not delete existing events or change your
mailbox's spam rules. Automatic low priority still permits Dream to read a message;
it is not a phishing quarantine. Display names and event-like subjects prove neither
authenticity nor acceptance of an appointment.

```bash
memcal mail                        # queued by priority, and what has no body
memcal mail --backfill             # what a recovery run would cover — reports only
memcal mail --backfill --apply     # actually re-fetch, updating rows in place
```

The backfill exists because rows archived before bodies were kept hold a subject
line and nothing else — and no migration can turn that into evidence. It is
resumable, stops at `--limit`, and never deletes, moves, or marks anything in
your mailbox.

Mail threads by `Message-ID` / `In-Reply-To` / `References`, so a reply joins the
thread it answers and two unrelated messages from one sender stay two
conversations. First reach is bounded by `MEMCAL_EMAIL_BACKFILL_DAYS`, then by
watermark. See [Gate](../architecture/gate.md) for the sender table.
