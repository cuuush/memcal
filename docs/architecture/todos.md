# To-dos

To-dos are explicit obligations with ages. Each carries how long it has been open,
rendered in the brief so stale items look stale.

```bash
memcal todo "Send the reservation deposit" --due friday
memcal todos
memcal done T7
```

## Wake conditions

Use `--when` to save a condition for a to-do.

```bash
memcal todo "Ask Rowan about the toll letter" --when "Rowan back from Italy" --who rowan
```

Set `MEMCAL_SEMANTIC_WAKES=1` or enable **Semantic wake checks** in Settings to
have Dream check these conditions. Dream can raise a follow-up question when
it finds a cited message sent after the to-do opened. The to-do stays open
until you confirm completion.

## Closure is conversational

How a to-do dies is a real problem. You return the borrowed item in person and no
channel ever reports it; left alone, the brief fills with zombies and stops being
trustworthy. So memcal never infers completion — the agent raises the question at
a plausible moment ("You were out in Lakeview yesterday — did you ever give Rowan
that EZ-Pass back?") and you confirm. `done` is explicit, or asked.

Due tasks can additionally publish to Apple Reminders — off by default, configured
separately. See [Publishing](../hosting/publishing.md).
