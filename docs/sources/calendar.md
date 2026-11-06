# Calendar

Created calendars and subscribed feeds from macOS Calendar, read as a periodic
snapshot. No secret — access is a macOS permission, requested explicitly:

```bash
memcal ical setup
memcal ical status
```

Calendar reads, publishing, recurring events, and Reminders use the local bridge
to `memcal.app`, including when `memcal dream` runs from a terminal. Consent belongs
to Memcal, shared with nightly work. Your terminal does not need permission to
control Calendar. If macOS names the terminal in a pending prompt, decline it and
run `memcal ical setup` after updating.

`memcal ical status` reports the app's last saved Automation and EventKit checks
without opening a consent dialog. The same Automation check applies before and
after installing, disabling, or enabling the schedule. A failed live Automation
check clears its saved success; EventKit verification stays separate. Older
nightly check records remain usable, but an older terminal-only check does not
verify the app. Use `memcal ical setup` for a fresh check.

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

Setup announces each live permission phase and its timeout. A repeat run reuses an
existing EventKit authorization decision. After accepting a dialog, authorization
status can complete the check even if macOS does not deliver the JXA callback.
Temporary consent workers return their result without opening or migrating the
Memcal database, so an open store transaction cannot hold up a completed dialog.
If setup times out, the error names the requester and manual settings steps; do not
reset all privacy permissions. Calendar data and Automation are separate grants.
For Automation, enable **memcal → Calendar** in System Settings → Privacy & Security
→ Automation. The bridge never retries an unsuccessful request from the terminal.
