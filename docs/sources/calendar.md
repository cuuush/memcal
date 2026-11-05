# Calendar

Created calendars and subscribed feeds from macOS Calendar, read as a periodic
snapshot. No secret — access is a macOS permission, requested explicitly:

```bash
memcal ical setup
memcal ical status
```

Events you create in personal calendars are retained as scheduling context for
matching and interpreting messages. They stay out of the brief and web event lists;
direct record detail remains available. Plans learned from messages and subscribed
calendar events appear in those memory views.

Partiful invitations are recognized through Apple Calendar data when you
subscribe to the Partiful calendar. Memcal keeps their RSVP links and
distinguishes an unanswered invitation (opportunity, mentioned) from a confirmed
plan. See [Events & series](../architecture/events.md).

Publishing back out — confirmed memcal events to a dedicated Apple Calendar, due
tasks to Apple Reminders — is a separate opt-in and off by default. See
[Publishing](../hosting/publishing.md).
