# The four-day scenario — beat sheet

Benchmark ground truth for `tools/benchmark_temporal.py`. The suite is hand-written
to prevent model self-evaluation artifacts.

This file documents the native-source `core` suite. The benchmark also executes three
model-free probe suites from `tests/scenarios/probes.py`: hostile response schemas,
calendar and timezone boundary conditions, and multi-turn Hermes lifecycle sequences.

**Day 1 is Mon 2026-08-03. Day 2 is Tue 2026-08-04. Day 3 is Wed 2026-08-05. Day 4 is
Thu 2026-08-06.** Day 1 establishes records and Day 2 modifies them; Days 3 and 4 verify
temporal progression. Each day executes one extraction pass with fixed timestamps
via `db.set_today`.

Traffic is stored as neutral structured records without pre-rendered prompt syntax.
Runtime formatting is produced by `bundle.FORMATS[...]`, making wire formatting a
benchmark parameter (`--format`).

## Record schema

```json
{"day": 1, "time": "19:42", "stream": "groupme", "thread": "poker-crew",
 "sender": "Jordan Lee", "text": "...", "beat": "poker.established"}
```

`sender: "me"` marks outbound messages (`from_me`). Email records supply `address`,
`subject`, and optional `headers` instead of `sender`. `beat` links the record to a
specific benchmark challenge; filler records carry `beat: null` and must produce no output.

## Cast

| Entity | Handle | Role |
|---|---|---|
| Jordan Lee | +19175550001 | poker host |
| Alex Rivera | +19175550002 | poker and dinner attendee |
| Cameron Ortiz | +19175550003 | dinner and beer garden attendee |
| Harper | +19175550004 | partner; conversational filler |
| Riley Morgan | +19175550005 | sibling |
| Rowan Vale | +19175550006 | EZ-Pass task target, travelling |
| Alex Chen | +19175550007 | colleague; name collision with Alex Rivera |
| Skyler Reed | +19175550008 | brunch attendee |
| Devon Park | +19175550009 | brunch attendee |
| Bailey Stone | +19175550010 | family friend (non-parent disambiguation) |
| Mom | +19175550011 | mother |

Typed facts: Casey's page records North End and Comet. The shared-calendar page has
`our cal`, `shared cal`, and `u&me` aliases.

## Storage eligibility criteria

The benchmark enforces three tiers of storage eligibility:

| Tier | Example | Expected handling |
|---|---|---|
| **High consequence** — commitments involving financial deposits or obligations | Tattoo appointment with non-refundable deposit | Store record; preserve through subsequent sweep passes (beat 50) |
| **Direct personal invitation** — social commitments with attendance tracking | Partiful invitation to birthday event | Store record; track RSVP response status and preserve invitation URL (beats 45, 48) |
| **Broadcast marketing event** — public or vendor webinars without attendance obligation | Pet insurer webinar | Drop record; produce no state (beat 51) |

Broadcast vendor invitations contain structured dates, times, and URLs, but lack interpersonal commitments or attendance obligations.

## The challenges

Each challenge evaluates facts established on Day 1 and mutated on Day 2.
"Must" designates an assertion in the answer key; "must not" designates an anti-regression assertion.

### 1. Date move — `poker`
- **D1** `groupme:poker-crew` — Jordan proposes poker **Fri Aug 7, 8pm**.
  Alex Rivera and Cameron agree. Casey agrees.
- **D2** `imessage:jordan` — Jordan sends direct message moving event to **Sat Aug 8**, same time.
- **Must:** exactly one poker row. `date` = 2026-08-08. Row key remains unchanged (retains
  the Aug 7 suffix; entity keys are immutable). `event_history` records `date` transition
  `2026-08-07` → `2026-08-08`.

### 2. Time move — `dinner`
- **D1** `whatsapp:dinner-thu` — Alex Rivera, Cameron, Riley: dinner **Thu Aug 6, 7pm**.
- **D2** same thread — moved to **8:30pm**.
- **Must:** one row, `time` updated, `date` unchanged at 2026-08-06.

### 3. Location move — `poker`
- **D1** poker located at Jordan's residence.
- **D2** `imessage:jordan` — relocated to Alex's residence, **42 Example Street**.
- **Must:** the existing poker row `location` updates to 42 Example Street without retaining
  or concatenating previous values. §10 case 1.

### 4. Status advance — `climbing`
- **D1** `imessage:riley` — Casey: "might do the climbing gym wednesday".
- **D2** Riley: "you still on for climbing tomorrow?" → Casey: "yeah locked in".
- **Must:** one row on 2026-08-05, `status` progresses `mentioned` → `confirmed`. §10 case 2.

### 5. Private decline against a public plan — `brunch`
- **D1** `groupme:brunch-sunday` — Skyler and Devon settle **brunch Sun Aug 9, 11am**.
  Casey is in.
- **D2** `imessage:skyler` — Casey privately: the user can't make it anymore, asks her not to
  say anything yet. **And** the group chat that same day still reads "see everyone
  sunday!".
- **Must:** the brunch row's status is `declined`. The row still exists — a decline is
  not a delete. The brief reads as *not going*, in plain language.
- **Must not:** the later, louder, public "see everyone sunday" overwrite the private
  decline back to confirmed. Recency alone is not authority.

### 6. Participant change — `dinner`
- **D2** `whatsapp:dinner-thu` — "cameron's out, riley's coming instead".
- **Must:** the Thursday dinner row's participants include Riley. Same row, no second
  row. (Whether Cameron is *removed* is not asserted — `upsert` merges participants by
  design and never drops one. Recorded here as a known limit, not a failure.)

### 7. Cross-platform change — `bbq`
- **D1** `groupme:block-party` — a neighbour's BBQ **Sat Aug 15, 2pm**.
- **D2** `email` — a Partiful update moves it to **4pm**.
- **Must:** one row. The email updates the row the GroupMe thread created. §10 case 12.

### 8. Cancellation — `movie`
- **D1** `imessage:riley` — movie **Tue Aug 11**.
- **D2** — cancelled, the theater is closed.
- **Must:** the row is not left sitting as a live commitment. `declined` or dropped.

### 9. Decoy: a confirmed row that must not move — `beer-garden`
- **D1** `groupme:beer-garden` — beer garden **Sat Aug 8, 3pm**, confirmed, with Cameron.
- **D2** `whatsapp:dinner-thu` — Cameron says, of nothing in particular, "we should just
  do the thing next weekend instead". No noun, no referent.
- **Must not:** the beer-garden row move off 2026-08-08. This is the exact bug behind
  `events.upsert`'s `confidence <= 1` guard — a passing "next weekend" in an unrelated
  thread re-dating a plan the people going had already settled.
- **Note:** poker also lands on Aug 8 after challenge 1. Two rows, one evening, one
  shared participant (Alex Rivera is in both). They must stay two rows.

### 10. Decoy: no walking backwards — `poker`
- **D2** `groupme:poker-crew` — someone late to the thread: "wait weren't we doing
  friday?"
- **Must not:** the poker row revert to Aug 7 or to `mentioned`.

### 11. One plan, three streams, one row — `dinner`
- **D2** the Thursday dinner is referenced in the WhatsApp group, in a DM from Alex
  Alvarez, and in a calendar-invite email — all the same evening.
- **Must:** one row. This is what the Merge stage exists for.

### 12. Wake condition — `ezpass`
- **D1** `agent` — Casey: "i need to give rowan back their ezpass when the user's back from
  italy".
- **D2** `imessage:rowan` — Rowan: "just landed, jet lagged as hell".
- **Must:** the to-do exists after day 1 and is **still open** after day 2, with a
  question raised about it. Closure is conversational, never inferred — invariant 6.

### 13. Slot update — `jordan`
- **D1** `imessage:jordan` — Jordan moved to **Eastwood**.
- **D2** — the lease fell through, it's **Riverton**.
- **Must:** Jordan's page slot for where the user lives reads Riverton, and `slot_history`
  records Eastwood → Riverton. The page holds what is true; the table holds what it
  used to say.
- **Was a known gap until 2026-07-28:** `set_slot` replaced without keeping the old
  value anywhere, so a corrected fact stopped ever having been the case and "the change
  was recorded" was not assertable. `slot_history` closes it.

### 14. The mom trap — `bailey`
- **D1** `imessage:bailey` — Bailey Stone, a family friend, mentions running into
  Casey's mother at the market.
- **D2** `imessage:mom` — their actual mother, about her birthday dinner.
- **Must not:** any page claim Bailey is their mother, or fill a family slot on her page
  from that message. Nobody said it. Inference becomes a question, never a fact.

### 15. The Comet trap — inference from context
- **D2** someone asks how Comet held up "after the trail ride".
- **Must not:** a slot appear saying Comet is a horse. Standing says Comet is their dog.
  This is a canonical example of a plausible completion nobody actually stated.

### 16. Junk, both days
Affection from Harper. An AWS newsletter announcing an event "tomorrow". A Chase
autopay notice. A retail "sale ends tomorrow". A 5-digit shortcode. A busy gaming
GroupMe where nothing is arranged.
- **Must:** zero memcal rows, zero to-dos, zero slots from any of it. §10 case 7.

### 17. Blank-page discipline
- **Must not:** a page exist for the shortcode, for any bulk sender, for a group chat
  name, or for an unresolved phone number.
- **Must:** every page that exists hold a fact, alias, real question, or recurring
  series history. A page is created by its content, never as a contact-card placeholder.

### 18. Idempotence
- Re-running day 2's pass with `--redo` must not duplicate any row, to-do or page.

## Agent tool calls — the other half of the day

Challenges 1–18 are all traffic: a message arrives, the gate sees it, it waits in the
spool, and a model reads it hours later. That is most of memcal and none of what the user
actually touches. When the user tells the assistant something, the assistant does not queue a
note for tonight — it calls a typed tool (`memcal_add`, `memcal_todo`, …), the store
changes immediately, and the nightly pass meets a calendar that already knows.

`skeleton.ACTIONS` holds those calls. They name functions in `memcal.live`, which is
what `mcp_server` dispatches to, so no model is involved in any of them and they run
in `load.ingest_day` before that day's dream — the order it happens in real life.

### 19. The agent settles a plan — `movie`
- **D1 20:20** `imessage:riley` — Riley proposes the movie **Tue Aug 11**.
- **D1 20:21** `agent` — Casey tells the assistant the user's going and to put it on the
  calendar. `memcal_add` writes the row `confirmed`, `written_by` = `live`.
- **D1 20:22** `imessage:riley` — the user answers Riley, a minute later.
- **Must (D1):** exactly one movie row. Still `confirmed` after the pass reads the
  very conversation that produced it, and still recorded at `live` authority — a pass
  that agrees with a row must not quietly demote it.
- **Must (D2):** the row is still one row, and Riley's cancellation still lands
  (challenge 8). Precedence exists to stop a cheap pass re-reading *old* traffic, not
  to stop it reading the news. This is the beat that found the opposite: every row the
  user ever dictated was frozen against every later pass from the next midnight on.

### 20. One sentence, two tools — `show`
- **D1 17:45** `imessage:cameron` — Cameron has a spare ticket to a Bowery Ballroom show
  on **Fri Aug 7**.
- **D1 17:52** `agent` — Casey settles it in one sentence: yes, 8pm, and remind them to
  venmo Cameron. Two tool calls: `memcal_add` and `memcal_todo`.
- **D1 17:55** `imessage:cameron` — the user tells Cameron the user's in.
- **D2 12:10** `imessage:cameron` — Cameron, the next day: "doors at 8 friday, meet you
  there". Same plan, same day, same time. Nothing here is news.
- **Must:** one row and one to-do after day 1. After day 2, still one row, still
  Friday, still `confirmed`, still 8pm, and still one to-do.
- **Must not:** day 2's echo become a second row. A plan the agent wrote is the case
  where a duplicate is most likely and least visible — the pass has never seen the row
  before, because no pass wrote it.

### 21. The agent closes a to-do — `venmo`
- **D2 18:22** `agent` — "sent cameron the money for the ticket, that's done".
- **D2 18:23** `memcal_todo` with `done` — the to-do is closed.
- **Must:** exactly one venmo to-do after day 2, and it is not open.
- **Must not:** the pass, reading the same sentence, re-open it or open a second one
  beside it. It is forbidden from closing a to-do on inference (invariant 6) and this
  is the beat that says what happens when the user states it outright instead. The
  verb did not exist until this challenge asked for it — `memcal_answer` needed an
  open question to hang off, so an agent told "paid them" had nothing to call.

### 22. An emoji can belong to the plan — `reaction-context`
- **D1** Jose sends only `👀`; six minutes later the group starts scheduling board
  game night.
- **Must:** the emoji remain recorded as `trivial` in the immutable gate audit, but be
  pulled into the same bundle once the substantive scheduling line arrives.

### 23. A terse event keeps its association and source — `aspca-context`
- **D1** a Doggo Park WhatsApp group says the ASPCA mobile clinic is at their run,
  Wednesday 10–3 at the 129th Street entrance.
- **Must:** one clinic row with the Doggo Park association/location, and the row must
  open directly onto that original line.

### 24. Wiki pages earn their file with useful facts — `wiki-facts`
- **D1** Quinn plainly states their favorite theater and favorite Pokémon set.
- **Must:** both facts land on one Quinn page. No placeholder page is needed first.

### 25. One-situation permission is not standing memory — `transient-permission`
- **D1** Casey says Quinn can borrow the car Friday to drive Katie to Medieval Times.
- **Must not:** “Quinn may borrow my car” become a permanent preference stripped of
  the trip that made it true.

### 26. Questions must stand on their own — `standalone-question`
- **D2** Mom asks, “When am I coming over again?”
- **Must:** the Ask block identify Mom as the speaker. The quoted sentence alone is
  unusable once the thread has disappeared.

### 27. Durable permission survives the transient-permission guard
- **D2** Casey explicitly says Quinn can borrow their car anytime in the future.
- **Must:** this becomes a named fact on Quinn's wiki page.
- **Counterweight:** challenge 25's Medieval Times permission remains one-situation
  context and must not become a durable fact. A regex that rejects every “can borrow” sentence
  fails one side or the other; model decision quality remains a separate benchmark concern.

### 28. An unrelated emoji remains unrelated
- **D1** Jose sends the same `👀` in a quiet group with no topic around it.
- **Must not:** this reaction enter the spool.
- **Counterweight:** challenge 22's `👀`, six minutes before scheduling begins, does.

### 29. Provenance is relevant, not merely resolvable
- Wiki favorite, EZ-Pass to-do, Mom question, poker update and ticket confirmation each
  have an exact expected source.
- **Must:** every derived object opens onto the line that supports it.
- **Frontier:** an updated row should show the evidence for its *current* state first;
  chronological evidence currently puts the obsolete original plan first.
- **Frontier:** computed wiki facts need a derivation source too. The poker series page's
  `where` slot is generated from sourced events but currently has no traversable link.

### 30. Encounter history is a projection
- **D1** Casey states three past real-life encounters with Quinn.
- **Must:** Quinn's profile reports exactly three. The upcoming board-game plan is not
  past, and moving an existing poker row must never count as another encounter.

### 31. Resolved work disappears
- **D2** the Cameron payment to-do is closed.
- **Must:** it remains auditable in the store but no longer appears in the brief.

### 32. Closed-world output inventory
- **Must:** every event, to-do, question, wiki page, and row in the legacy standing table
  belongs to a declared beat. Targeted “no paid event” regexes are insufficient—a
  hallucinated “Transfer to Quinn” title would evade one while still being unwanted.
- **Frontier:** one wake should produce one question. The current deterministic integration
  produces both the model's EZ-Pass question and the code-generated wake question.

### 33. A ticket confirmation is a calendar source
- **D1** an Alamo email confirms Dune, Wednesday August 12 at 7:30.
- **Must:** the event lands and opens to the confirmation.
- **Current frontier gap:** the transactional email is blocked as `auto-submitted`
  because the subject classifier does not recognize this ticket wording.

### 34. Relationships link both people
- **D1** Quinn says Katie is their sister and everyone calls her Kat.
- **Must:** Quinn links to Katie, Katie links to Quinn, Kat resolves as an alias,
  both pages are material, and the relationship opens to Quinn's exact line.

### 35. Generic group event, without roster-as-attendance
- **D1** a six-person rave chat gets “there's a party tomorrow” with the distinctive
  name Neon Garden. The sender and another room member explicitly cannot attend.
- **Must:** Neon Garden is still stored as an opportunity on Tuesday, with no attendees.
  The room roster is retrieval context, never an attendance assertion.
- **D2** the original sender privately says Neon Garden moved to Wednesday.
- **Must:** the DM finds and moves the same row, records the date history, and does not
  add the sender or the rest of the room as attendees.

### 36. Existing event enriched by its group discussion
- **D1** a live Elements Music Festival row already exists with only its Friday start.
  The rave chat names the same festival, says it runs through Sunday at Cedar Falls, and
  explicitly says Casey and Alex—not the whole room—are going.
- **Must:** the existing row is enriched rather than duplicated; `until` is Sunday,
  location is Cedar Falls, and Alex is the only stored participant.

### 37. Event-linked ticket lifecycle
- **D1** Spider-Man, Fantastic Four and Superman are confirmed movies, each with an
  open, event-linked “make sure we have tickets” to-do.
- **D2 / direct proof** Morgan says the user got the Spider-Man tickets and gives the theater,
  showtime and seats.
- **Must:** Dream closes only that ticket to-do and writes the details onto the existing
  Spider-Man event. The details remain in the brief after the to-do disappears.
- **D2 / filtered receipt** an automated AMC email has the generic subject
  “AMC confirmation #84721”; its body explicitly confirms Fantastic Four and includes
  theater, showtime, auditorium, seats and confirmation number.
- **Must:** the linked obligation earns a body fetch despite the sender gate. The exact
  event match queues the receipt, closes the Fantastic Four to-do, and enriches the
  existing event.
- **Counterexample / wrong movie:** the same sender confirms Batman Returns, for which
  there is no tracked event or linked obligation.
- **Must not:** that body enter the spool, create Batman, or close another movie’s task.
- **Counterexample / marketing:** a mailing list says “Superman tickets are on sale.”
- **Must not:** it enter the spool or close the Superman task. Available is not acquired.
- Model-sensitive extraction checks begin as frontier checks, including ignoring the
  wrong receipt after thread-context reinjection. Integration states the intended
  contract; model failures remain visible gaps rather than hidden xfails.

## Days 3 and 4 — whose job is elapsed time

**Day 3 is Wed 2026-08-05. Day 4 is Thu 2026-08-06.** They carry very little new
traffic on purpose. Their job is that *time passed*: a question asked on day 1 can be
answered by day-3 evidence, a plan's day can arrive and go by with nobody saying
anything, and a calendar can be re-scanned. None of that is expressible in a two-day
story, and it is where the reported defects were.

### 41. A question is answered by later evidence
- **D1** `imessage:devon` — Devon mentions their housewarming on Sat the 15th and says
  the user will send the address. The row lands with no location, and memcal asks where it is.
- **D3** Devon sends the address.
- **Must:** the row carries the address, and the question is **not open** — it is the
  row directly above it that answers it. Whether it reads `answered` or `dropped` is
  not asserted; that it still stands there open is the defect.
- **Must not:** a second housewarming row appear, or the question move onto another row.

### 42. A day passes and nobody says anything
- **D1/D2** Neon Garden is an opportunity nobody has committed to, moved to Wed the 5th.
  memcal asks whether the user is going.
- **D3** the 5th. **D4** nothing was ever said.
- **Must:** the question retires. **The row does not** — it is what the backward window
  reconciles against, and deleting the subject to silence the question is the wrong
  half. This is the most common thing that happens to a plan and nothing tested it.

### 43. An obligation is not a question — the counter-case
- **D1** the EZ-Pass to-do (challenge 12), which is attached to no occasion.
- **D4** still open, three days later, with Rowan home since day 2.
- **Must:** it is a **to-do that survives**. A to-do linked to an occasion retires with
  it (`expire_event_links`); one with no occasion has no day to die with. This beat is
  the one that proves the split, and without it "expire things that got old" passes.

### 44. A recurring appointment's owner moves it
- **D1** `imessage:nadia` — the physio clinic confirms Wed the 12th at 5, on a series.
- **D3** the clinic moves it to Wed the 19th, same time.
- **Must:** one row, `date` = 2026-08-19, `event_history` holds 08-12 → 08-19, the time
  is unchanged, and the **series survives** so the next one still matches.
- This is the good case — e125 followed a tutor's schedule change without being
  told — and it is here to stay working.

### 45. A calendar rescan must not undo judgement
- **D1** the Partiful calendar carries "Jack's 30th" on the 22nd with **no location**,
  so the RSVP inference reads it as `opportunity`/`mentioned`. `imessage:reese` that
  evening settles it: the user is going.
- **D3** the calendar is **renamed**, which changes every item's revision and forces a
  full re-derivation, and the location is still absent.
- **Must:** `kind`/`status`/participants survive the rescan. The connector learned
  nothing new — "does this feed row have a location" is an inference, and re-running an
  inference is not news.
- **Must not:** the rename produce a second row for any calendar item. `calendar_uid`
  in these fixtures is deliberately **not** derived from `calendar_name`: every fixture
  computed `f"calendar-{name.lower()}"`, so a rename was not expressible, and that is
  precisely why the benchmark missed the most expensive bug in the project.

### 46. An obligation stated obliquely, inside unrelated traffic
- **D3** in the middle of a busy game-night thread about nothing, the user says the user still owes
  Devon a deposit before Saturday.
- **Must:** a to-do exists for it. Nothing else in the corpus states an obligation
  anywhere but plainly and in a quiet thread, so every extraction change scores as
  noise — this is the beat the extraction work has been asking for.
- **Frontier.** The first cold start on the real corpus wrote 134 events and **zero**
  to-dos; this is that failure, made reproducible and free.

### 47. Containment — a thing that happens inside another thing
- **D1** the Elements festival runs Fri 7 – Sun 9 August.
- **D3** the rave chat settles breakfast at Elements on the Saturday morning.
- **Must:** two rows, not one, and the breakfast is nested under the festival — the
  brief renders it with `↳`, the same shape a question about a row gets.
- **Must not:** the two be *merged*. They share one word and one guest, which is
  `find_match`'s weakest tier, and absorbing the breakfast renamed the festival to it
  and lost the whole weekend. A weak match against a row that **spans** the target date
  is containment, never identity.
- **Must not:** the festival lose its `until`.
- Three live rows were all called "Elements" and read as three unrelated plans on one
  weekend. `part_of` is a column of its own because `series` is read by
  `find_match_scored` and would merge exactly what this needs kept apart.

### 48. An invitation is a fact about how to act
- **D1** the Partiful calendar carries "Capture The Flag" on the 23rd. Nobody ever
  says anything about it.
- **Must:** it reads **"not replied"**, not "could go". Those are different facts:
  "could go" is memcal's guess about something a friend mentioned and there is nothing
  to do but ask them, while an invitation has a button — and the link can be forwarded
  to your brother, which is the whole point. The brief names the link.
- **D3** it disappears from the feed.
- **Must:** it is declined. A disappearance is an *observation*, unlike the RSVP
  inference, so it keeps its full authority (challenge 45 is the other half of this).
- **Must:** the declined invitation **stays visible**, reading "not going", with its
  link. A birthday you have said no to is exactly the one you still want to open and
  send a message through, and it was vanishing from the brief entirely.
- Publishing is untouched: `ical.publishable` already refuses anything whose origin is
  a subscribed feed, so a declined invite is visible in memcal and never on the phone.

### 49. An obligation accepted in a work DM
- **D1** `imessage:alex-chen` — a standup that already happened, then "i need you to
  look at this jira ticket when you get a sec", the ticket id, and **"got it, will
  check it out"**.
- **Must:** a to-do exists for it. The user was asked to do something and said the user would.
- **Must not:** a calendar row appear — nothing here has a day on it.
- These six lines were **filler** until 2026-08-05: the skeleton asked the writer for
  "no plans that involve them personally" and got a *task* instead of a plan, which
  nobody noticed. gpt-5.6-terra then extracted it and the answer key scored that as an
  invented row. The traffic is unchanged; what changed is that it is graded, and the
  score before and after this beat is not comparable.
- It is also a second oblique-obligation case (beat 46 is the other) in genuinely noisy
  traffic, which is the category the extraction work is about.

### 50. Stakes — forgetting this one costs something
- **D1** `imessage:sasha-kim` — the tattoo artist confirms Tue Aug 18 at 2pm, notes the
  **$100 deposit is non-refundable** and that she needs 48 hours to move it. The user confirms.
- **Must:** a confirmed commitment on 2026-08-18, and it must still be there after every
  later pass — the sweep may not drop it as junk.
- This is the clearest case memcal exists for: the user does not forget it because forgetting
  it costs money and lets somebody down.

### 57. An explicit appointment amendment changes the existing row
- **D1** establishes the synthetic appointment in beat 50.
- **D2** `imessage:sasha-kim` — the provider explicitly says the appointment is
  **moved** to Thu Aug 20 at 4:15pm, using ordinary conversational wording.
- **Must:** exactly one appointment row, now on 2026-08-20 at 16:15. Its prior date and
  time are each in history; an explicit correction must not leave the old time behind.
- **Must not:** retain a second row on Aug 18, or treat the later conversation as a
  fresh appointment just because the later message contains a replacement date and time.

### 51. A company's event is not their plan
- **D1** `email` — their pet insurer, addressed to them by name: "You're invited: Wellness
  Wednesday with Dr. Ramirez, Aug 12", a time, and a join link.
- **Must not:** any row, to-do or question come out of it.
- Harder than the AWS newsletter in beat 16, which announces itself as bulk. This one is
  transactional, personal in tone, and has every surface feature of a real invitation.
  What separates it is that nobody notices if the user does not attend and nothing is owed.

### 52. A link is how you attend, and it is not a place
- **D1** `email` — their tutor reschedules, in HTML, signing off with
  `<a href="…zoom…">Tutoring Meeting Room Link</a>`. The link is in the attribute; the
  words are in the body.
- **D1** `calendar` — the same appointment is already on their calendar: Wed 12 Aug, 12:00,
  location **"Online"**, with the Zoom URL in the event's `description`.
- **D3** the calendar is renamed, so every revision changes and the whole snapshot is
  re-derived.
- **Must:** the row carry the URL in `join_url`, and the brief show it — including from
  `## Later`, since on day 1 the appointment is nine days out and that is exactly when a
  reader needs to know it is a link rather than a room.
- **Must not:** `location` be replaced by the link. "Online" is what their calendar
  says and it is worth keeping; a fix that overwrites it has moved the problem.
- **Must not:** the rename lose the link, or the Petly webinar in beat 51 —
  which also carries a join link — become a row on the strength of having one.
- **D1** `calendar` — and the half with no link in it at all: "Bloodwork", Thu 6 Aug,
  description "Suite 300, ring buzzer 4. Bring your insurance card."
- **Must:** that survive too, as `note`, and reach the brief. The connector was not
  dropping *links*; it was dropping the description, and a buzzer number is lost exactly
  as completely — no matcher would ever have rescued it.
- **Must not:** memcal's own published description ("Added by memcal. With …") be read
  back as though somebody had told us something. That is the row manufacturing its own
  evidence, which has happened here before.
- **Must:** the entry memcal *publishes back* carry the link in its `location` —
  `Online; https://…` — because the Join button on the notification is built from
  `location` and not from the notes, and the notification at 12:58 is the only moment
  any of this matters. The lift had an inbound half and no outbound one for nine days;
  the row the user reported was the weekly one, so both publish paths count.
- **Must not:** composing eat the place. `~^online;` is the decoy — a publish that
  replaces "Online" with the URL has moved the problem outdoors rather than fixed it.
- `location` answers *where*, `rsvp_url` answers *how you reply*, and neither answers
  *how you attend*. Both connectors were reading the link and then dropping it:
  `ical._normalized` narrowed the calendar item to five fields, and `proton._strip_html`
  kept anchor text and threw away the href. A missing column looks exactly like this from
  the outside.

### 54. The group has a member nobody can name

- **D1** `whatsapp` — in the doggo park chat, a participant whose only identifier is
  `+261516951601296` proposes a Saturday morning meet-up at the park. The number is a
  **WhatsApp LID**: 15 digits, not a phone number, matching no contact and carrying no
  push name, so nothing in the corpus or on the machine can ever name them.
- **D1** `whatsapp` — Rae, who *is* named, replies agreeing to the time.
- **Must:** the meet-up become a row, on Saturday, with Rae on it. A plan is a plan
  whoever proposed it, and the point of the archive is that the traffic is not lost
  because the roster was.
- **Must not:** the numeral appear as a person — no `+261516951601296` in
  `participants`, no wiki page for it, no `Q` asking who they are. A question nobody on
  earth can answer is worse than no question: it occupies the Ask block for ever and
  teaches them the block is noise. `todos.admissible` is the rung it belongs at.
- **Must not:** the row be dropped for having an unnameable proposer, and the *named*
  participant must survive — an over-eager filter that throws the whole line away has
  swapped one failure for a worse one.
- 247 handles in the live store had no name and 47 carried 25+ messages each; fourteen
  of those were LIDs and are the only ones a human has to answer. Everything else was
  the platform's own display name going unused, which is `identity.adopt_seen_name`.

### 55. The platform is not a person, and sometimes it is quoting one

- **D1** `groupme` — in the smash bros chat, GroupMe itself posts `A message was
  deleted.` The sender is `groupme:system` with the display name **GroupMe**.
- **D2** `groupme` — GroupMe posts again, and this time the notice *contains the
  message*: `Riley Morgan edited to: "actually let's make smash 7pm Saturday"`. The
  platform is the speaker and a person is the author.
- **Must not:** anything be created for "GroupMe" — no person, no page, no row, no
  question, and no handle waiting to be named. `groupme:system` sat at the top of the
  live name-this-person queue with 218 sightings behind it, which is an unanswerable
  question in the one position that guarantees it is read first.
- **Must:** the edit's *content* still reach a row. The smash bros plan moves to 7pm
  Saturday. This is the decoy that stops the fix being "ignore the system channel":
  the noise and the answer arrive on the same handle, and a filter that drops the
  handle drops the plan with it.
- **Must not:** the edit be attributed to GroupMe. Riley Morgan is named inside the
  text and is already in the roster.
- Distinct from beat 51's marketing: that is a real sender with nothing worth keeping.
  This is a **non-sender** whose traffic sometimes carries the thing that matters.
- **The second half is a frontier gap, and a live one.** `groupme._deliver` returns early
  on `message.get("system")`, so the whole channel is dropped at ingest and the edit
  notice never reaches the archive. The live store has `DELIA edited to: "This is where
  we are planning to…"` and `Casey Morgan edited to: "No, Morgan is sitting th…"`, both
  destroyed on the way in and recoverable from nothing. The fix is not a regex over the
  wording: GroupMe sends `event: {type, data}` on every system message and the
  connector's field list has never named it. Read `event.type`, drop the bookkeeping
  kinds, keep the ones quoting a person.

### 56. Work the user handed off is not work the user owes

- **D1** `agent` — the user tells their assistant to go and file Comet's vet insurance claim
  itself: the receipts are in their email, handle it, say when it is done. Imperative,
  `from_me`, and thick with commitment verbs.
- **Must not:** a to-do be opened. The doer is the assistant, the work happens inside
  that session, and nothing is owed by them afterwards. The live store had four of these
  in six open to-dos — *"Apply for 5 jobs"* was filed at 03:00 the morning after the
  assistant had already applied, and it can never close, because to-dos close
  conversationally (invariant 6) and the user is never the one who reports it done.
- **Must:** the line still reach the model. Recall is not in question here; the agent
  stream is the highest-signal stream there is.
- **The gate is not the lever, and this beat is built to prove it.** `COMMIT_RE` is a
  *first-person* detector — "i need to", "remind me", "let's" — so a bare imperative
  handed to an assistant never fired `own-commitment` in the first place. Archive 20080
  was `no-signal`, and it reached the model as a *neighbour* of the gated agent line
  seven minutes before it (`bundle.add_thread_context`). `dl01` takes that same path on
  purpose. Where the gate *does* fire — 72 agent lines carrying first-person wording —
  the verdict is now `directive` rather than `own-commitment`, so nothing downstream
  reads an obligation off the reason string; that half is pinned by
  `TestACommandToAMachineIsNotACommitmentHeMade` rather than here.
- **The decoys are in the same bundle, and they are the whole point.** `ez01` (beat 12)
  and `sh02` (beat 20) are also them addressing their assistant in the imperative, and both
  *must* still open a to-do — because the doer is them. "Remind me to venmo Cameron" is
  their obligation stated to a machine; "file the claim" is the machine's. A fix that
  reads as "agent stream, imperative, no to-do" takes beats 12 and 20 down with it.
- Nothing in the text separates the two. **The addressee does**, and until `#57` the
  archive had no column for it: `from_me` is a fact about authorship, and on every other
  stream there is a person on the other end, so the two travelled together. `me: cancel
  the booking` and `me → assistant: cancel the booking` are the same words and opposite
  obligations.

### 53. A withheld field is not a value
- **D1** `calendar` — the Partiful feed carries "Mount Aldon Stage Reading" on the 16th,
  and its location is the literal sentence **"Location available once RSVP'd"**. That is
  not a place. It is the platform saying, in the field itself, that it will not tell us
  what the field holds until the user replies.
- **Must:** it read **"not replied"** — `opportunity` / `mentioned`, exactly like beat 48,
  which reaches the same state with the field genuinely *empty*. The two spellings of "no
  location" must produce one answer.
- **Must not:** `location` hold that sentence. A status message in the venue field is a
  second bug underneath the first, and it is what the brief was rendering.
- **Must not:** it be **declined**, ever. The user attended their own domestic-partnership
  ceremony without RSVPing: a withheld location says nothing about attendance in either
  direction, so the only honest reading is that the feed is not telling us. Beat 48's
  disappearance is the one thing that still declines, and it is an observation.
- **D1** the same item carries a real description — "Doors 6:30. The reading is upstairs;
  ask for Nadia at the desk."
- **Must:** that survive as `note`. The RSVP inference used to *overwrite* it, so 17 of
  18 live Partiful rows held the string "Partiful RSVP yes" where the invitation's own
  words should have been — destroyed at ingest, and not in the archive either, because
  the archived line is built from the note after it was overwritten.
- **D3** the calendar is renamed, so every revision changes and the snapshot is
  re-derived from the same withheld location, as in beat 45.
- **Must not:** any of the above flip on the rescan.
- Every calendar fixture in this repo modelled "has not RSVP'd" as `location=""`, a shape
  the real feed has **never sent** — the same reason beat 45 exists, one field over. A
  source that encodes absence as an in-band value defeats a presence test, and the
  inference then fires hardest exactly where the source said it had nothing.

### 38. Fit to read
- No new traffic. These hold over whatever the run produced, which is what makes them
  cheap to keep and hard to satisfy by accident.
- **Must not:** a question, to-do or note contain a raw handle (`groupme:128934125`,
  `+1917…`), describe the user in the third person, or narrate memcal's own bookkeeping
  ("2 sources mention this").
- **Must not:** an open question ask something the store already answers — neither
  "Who is Aaron?" when Aaron has a page, nor "when is X?" while the row it is about
  carries the date.

### 39. Checkable
- **Must:** every row's evidence stay at line granularity rather than citing a full bundle.
- **Must:** every stored guest be named in a line the row actually cites, and every
  row's date follow from a phrase in its own evidence. A fabricated attendee or an
  unsupported date is invisible by inspection: the row reads perfectly.

### 40. Reachable
- **Must:** a row past the brief's forward window still be named somewhere in the brief.
  Two rows captured correctly were reported as extraction failures purely because the
  week block stopped before them, and "look up anything outside that" asks the reader to
  know what they are missing before they can ask for it.

## Frontier checks

`frontier=True` is not an xfail. A frontier check expresses the product behavior we
want even when the code cannot do it yet. It stays red, remains in the hard-check score,
and turns green automatically when the system earns it. `soft=True` remains reserved
for product-policy measurements where either answer can be defensible.

## Filler

Roughly two thirds of the corpus is traffic that means nothing: logistics that resolve
themselves, links, reactions, dead threads, a busy group chat about a game. Filler
exists because a gate and a bundler that only ever see signal are not being tested,
and because the ratio is what the cost story rests on.

Filler may contain temporal words. It may not contain a plan involving Casey on a
specific date — that would make the answer key wrong.
