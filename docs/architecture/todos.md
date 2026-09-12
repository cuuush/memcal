# To-dos

To-dos are explicit obligations with ages. Each carries how long it has been open,
rendered in the brief so stale items look stale.

```bash
memcal todo "Send the reservation deposit" --due friday
memcal todos
memcal done T7
```

## Wake conditions, not just dates

Some to-dos wake on the world rather than the calendar: ask about the toll-by-mail
thing once Rowan is back from Italy. When ingestion writes something that satisfies
the condition, the to-do surfaces:

```bash
memcal todo "Ask Rowan about the toll letter" --when "Rowan back from Italy" --who rowan
```

## Closure is conversational

How a to-do dies is a real problem. You return the borrowed item in person and no
stream ever reports it; left alone, the brief fills with zombies and stops being
trustworthy. So memcal never infers completion — the agent raises the question at
a plausible moment ("You were out in Lakeside yesterday — did you ever give Rowan
that EZ-Pass back?") and you confirm. `done` is explicit, or asked.

Due tasks can additionally publish to Apple Reminders — off by default, configured
separately. See [Publishing](../hosting/publishing.md).
