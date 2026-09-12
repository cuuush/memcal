# Publishing

The private event store is not your real calendar. That separation keeps an agent
from creating, moving, or deleting live calendar data just because it inferred a
plan from conversation. Publishing opts in per destination, and both are off by
default.

## Apple Calendar

```bash
# ~/.memcal/.env
MEMCAL_PUBLISH_CALENDAR=memcal

memcal ical setup
```

Publishes confirmed memcal events to a dedicated Apple Calendar.

## Apple Reminders

```bash
# ~/.memcal/.env
MEMCAL_PUBLISH_REMINDERS=memcal

memcal reminders setup --yes
```

Publishes due tasks to a Reminders list.

Both destinations are editable from the web UI's Settings tab under *Writing
back out*, and like every outward-write setting they read this store's `.env` plus the environment — never the checkout or
working-directory files. Publishing is idempotent: changes update the matching item,
withdrawn rows retract it, and disabled publishing performs no external writes.
Calendar and Reminders require separate macOS permissions.
