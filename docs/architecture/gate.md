# Gate

The gate is the cost governor. Everything is archived; the gate decides what the
dream pass even looks at. It is deterministic code — no model, ever — so "hey"
costs nothing and a newsletter never reaches an expensive call.

## Messages

A message passes on any of: a temporal token (weekday, "tonight," "tomorrow," a
time, a date), a question mark, a first-person commitment verb, a top-tier
sender, an availability state, an invitation, a wiki attribute, a known contact
in a 1:1 thread, a full-stream source (iMessage), or your own commitment or
directive. Most chatter fails everything and is archived for search at zero model
cost.

```bash
memcal gatecheck --stream groupme --limit 40   # what is passing and rejecting
memcal top alice                               # alice always passes the gate
```

## Email is a sender problem

"AWS re:Invent night is tomorrow" and "poker is tomorrow" are lexically identical.
Nothing in the text separates them, so the gate keys on the **sender** using free
signals: `List-Unsubscribe`, `List-ID`, `Precedence: bulk`, provider category
labels, and whether the address has been seen before.

An unknown sender gets one decision — ignore, archive-only, or process — and after
that it is a table lookup forever:

```bash
memcal senders                       # the email gate table
memcal senders vendor@example.com ignore
memcal mail                          # what is queued, by priority
```

Automatic relevance sets a *priority*, never a verdict. Low-priority mail is read
after everything else in bounded batches and stays searchable the whole time. The
only thing that keeps a sender out entirely is an explicit ignore — and a blocked
sender's body is never fetched or stored.

## Mutes and blocks

Muting shapes priority and presentation, never collection. `memcal block` (sender
or stream/thread) means never spending a model call there again; the archive rows
remain. Platform mutes (a muted Slack channel, an archived Telegram dialog) are
recorded with the platform's own note and handled per `MEMCAL_PLATFORM_MUTE`.

## What the gate does not do

New-activity detection never depends solely on passing the keyword gate:
"Actually, use my new place" can matter in a known event thread even though it
carries no temporal token. Thread association for [freshness](freshness.md) uses
evidence links and bounded candidate retrieval, not the gate — the gate governs
model cost, not relevance.
