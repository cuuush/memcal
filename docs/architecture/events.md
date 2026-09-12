# Events & series

The event store is a private, optimistic model of what is going on — explicitly
not your real calendar. Rows are short, one line each, which is what keeps a week
readable inside a token budget.

## Kinds

The store holds the whole week as known, not just commitments:

| Kind | Meaning | Example |
|---|---|---|
| `commitment` | Something you are doing | Poker at Jordan's, Fri ~8pm |
| `availability` | Someone else's state | Alex free Monday night |
| `opportunity` | Something happening you might want | Volunteer member day, never replied to |
| `observed` | Something that already happened | For the backward window |

Availability is what makes introductions work: a brother visiting this week plus
Alex free Monday becomes "you should introduce them."

## Status and window

`mentioned` / `tentative` / `confirmed` / `declined` / `happened`. Optimistic
capture means most rows start at `mentioned` and never go further, and that is
fine. The brief shows 7 days forward and 3 back; everything else stays in the
database behind `memcal_list_month` and search.

The backward window is the reconciliation surface: Tuesday's optimistic dinner row
sits there Wednesday, the agent asks, you answer, the row resolves.

## Matching

A new mention matches an existing row deterministically first: same series, date
within ±10 days, overlapping participants. "The game moved to Sat" updates the
Friday row rather than creating a Saturday one. Only genuinely ambiguous
near-horizon cases get a model call — and only where being wrong actually costs
something.

Cancelling one booking and making another keeps the old row declined and the new
row confirmed; they never collapse into one. An older notice cannot undo a newer
confirmation.

## Series

Recurring things get a rule; occurrences stay ordinary rows linked to it. Two
poker games are two rows, both linked to the poker-night series, which holds the
durable stuff — whose house, the address, roughly monthly.

```bash
memcal series poker-night --every fortnightly --on friday --at 20:00
```

Moving or skipping one week without touching the rule is `memcal_move_once`
(agent tool).

"Where was poker last time" is a page read, not an archive search: series live
alongside the [wiki](wiki.md).

## Invitations with RSVPs

Partiful invitations arrive through calendar data. Memcal keeps their RSVP links
and distinguishes an unanswered invitation (opportunity, mentioned) from a
confirmed plan — disclosing the location confirms, withholding it does not.
