# Corpus

A small set of five-channel, seven-day lives with real user-turn operations,
older background evidence, hidden expectations, and failure-inclusive scoring.

## Observations, not answers

Scenarios ship as versioned, serializable lives. Adapters receive only public
observations: stable source id, source timestamp, arrival timestamp, channel,
sender/thread metadata, and original content. Hidden grading data — expected
atomic facts, accepted alternatives, supporting observation ids, forbidden stale
claims — never reaches the system under test.

Model time is explicit throughout: when evidence was written, when it arrived,
when collection exposes it, and when the described fact applies. Expectations
follow scheduled exposure, never omniscience. A question asked before an update
is exposed cannot expect the update; ambiguous or conflicting evidence may
warrant an uncertain answer.

## Genuinely interleaved lives

Five streams — email, iMessage, WhatsApp, GroupMe, assistant conversations —
with distinct senders, threads, language, and metadata. Each life carries
recurring people and ongoing commitments, ordinary low-value traffic, and
developments that only resolve by combining two or more channels: year-old email
with a plausible old address, this week's group-chat planning, a later venue
change. Broad requests with no obvious search string ("Anything fun this
weekend?", "What should I sort out before my trip?") sit beside direct,
complete-list, historical, cross-source, and unanswerable questions.

The motivating case: email books poker Friday at 7 at Sam's; the organizer moves
it in GroupMe to Saturday at 8 at Jordan's; a participant repeats the old plan on
WhatsApp; the organizer confirms in iMessage; you tell the assistant you're still
going and bringing chips. Morning questions must recover Saturday, 8, Jordan's,
attendance, and the chips — while keeping Friday as history. The answer follows
the meaning and authority of the evidence, not newest-wins or channel priority.

## Controls and roles

Matched controls change one meaning at a time: "moved" → "booked another," the
decisive update removed, cancellation → non-attendance. Consolidated-source and
distributed-source versions of selected histories test joining partial evidence
versus reconciling conflicting complete statements.

Three corpus roles guard against overfitting: **development** cases for debugging,
a small **calibration** set for reviewing grader and wiring, and **held-out**
lives for measurement — split by story family, so a renamed poker night is never
new evidence. Once a held-out life's details tune code or prompts, it is marked
exposed and replaced.
