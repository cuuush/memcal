# Brief

One rendered file, always in context. The brief is the whole interface — everything
else in this architecture exists to keep it accurate and short.

```bash
memcal brief
```

## Shape

```markdown
## This week
〔E12〕 Fri Aug 7  Dentist appointment, 2pm — confirmed · Beacon Dental
〔E18〕 Sat Aug 8  Community garden member day — opportunity

## Open
〔T7〕 Send the cabin deposit — due Friday

## Ask about
〔Q4〕 Is dinner with Jordan Saturday or Sunday?

## People and facts
Pages: jordan (address, birthday) · beacon-dental (phone)
```

The blocks: **Now** (due reminders), **This week** (3 days back, 7 forward, with
its coverage stamp), **Later** (committed plans further out), **Regularly**
(recurring rules), **Open** (to-dos with ages), **Ask about** (unattached
uncertainty), and **People and facts** (the wiki title list, so the agent knows
which pages exist to open).

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

The week block carries its coverage stamp — what period it is complete for — and
flagged items carry activity warnings beside the last-confirmed plan. Prepared
facts are the fast path when coverage supports them; for a flagged item, the
assistant inspects the indicated activity before claiming current details. If
collection or lookup failed, it states the last known plan and the limit instead
of presenting that plan as freshly verified. The old "snapshot is the answer"
wording is gone: the brief is the starting point, and it says where it ends. See
[Daytime freshness](freshness.md).
