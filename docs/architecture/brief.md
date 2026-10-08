# Brief

One rendered file, always in context. The brief is the whole interface — everything
else in this architecture exists to keep it accurate and short.

```bash
memcal brief
```

## Shape

```markdown
## Upcoming  (today is Thursday 8 October 2026)
〔E12〕 Fri Oct 9  "Dentist appointment", 2pm — confirmed
〔E18〕 Fri Oct 16  "Weekend visit" — maybe · with Rowan Vale

## Open
〔T7〕 Send the cabin deposit — due Friday

## Ask about
〔Q4〕 Is dinner with Jordan Saturday or Sunday?

## People and facts
Pages: jordan (address, birthday) · beacon-dental (phone)
```

The blocks: **Now** (due reminders), **Upcoming** (3 days back, 30 forward, with
its coverage stamp), **Later** (committed plans further out), **Regularly**
(recurring rules), **Open** (to-dos with ages), **Ask about** (unattached
uncertainty), and **People and facts** (the wiki title list, so the agent knows
which pages exist to open).

The date window rolls forward from today; it is not a calendar month. Its coverage
stamp includes full ISO dates and years. Custom `MEMCAL_DAYS_BACK` and
`MEMCAL_DAYS_FORWARD` values still apply. Upcoming and Later entries both show
participants, status, and time; location and notes remain behind the handle.

The brief suggests `memcal_list_month(month="2026-10")` for a full calendar month
and `memcal_list_days(when="2026-01-01", days=365)` for a full year (366 in a leap
year). These reads include visits already in progress at the start of the range.

The handles — `E12`, `T7`, `Q4` — open the full row, evidence, and history:

```bash
memcal E286        # bare handle works too
memcal_open        # the same read over MCP
```

## Budget

The whole brief has a hard token cap (`MEMCAL_BRIEF_TOKEN_CAP`, default 1500).
Junk in the archive costs nothing; junk in the brief costs on every single turn
forever, so rendering trims to fit and freshness metadata stays beside its event
when trimming happens. A general backlog notice survives truncation: when activity
is omitted, the brief says so.

## Coverage is qualified

The Upcoming block carries its coverage stamp — what period it is complete for — and
flagged items carry activity warnings beside the last-confirmed plan. Prepared
facts are the fast path when coverage supports them; for a flagged item, the
assistant inspects the indicated activity before claiming current details. If
collection or lookup failed, it states the last known plan and the limit instead
of presenting that plan as freshly verified. The old "snapshot is the answer"
wording is gone: the brief is the starting point, and it says where it ends.

Trimming keeps this honest. An event and its warning drop as one unit, and the
completeness stamp is only kept while every coverage hole it would otherwise hide
is still shown; if the budget drops a warning that stood in for a thread's
backlog, the stamp is removed and a compact `[coverage incomplete — unreviewed or
uncollected input not shown]` line takes its place. Backlog coverage is judged
against the events the brief actually renders — not date-range membership — so a
thread linked only to an unconfirmed opportunity or an event past the Later cap
still surfaces. See [Daytime freshness](freshness.md).

If trimming removes a known event, the completeness stamp is also removed. The
brief reports incomplete coverage and points to the calendar tools for full rows.
