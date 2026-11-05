# Scheduling

One background item, `memcal`, appears in System Settings → Login Items &
Extensions. The installer builds a small local app wrapper so macOS attributes
Calendar access to memcal instead of Python; if that build is unavailable, the
named script remains the fallback. launchd starts it at 03:00, at login after
power-on, and every five minutes by default while the machine is awake. The
check interval follows `MEMCAL_COLLECT_INTERVAL_MINUTES`. A calendar trigger
missed during sleep runs on wake.

```bash
memcal schedule install    # daily + catch-up launchd job
memcal schedule status     # loaded/at/next/last-pass/outcome
memcal schedule            # when the pass last ran, whether one is owed
memcal schedule run        # run now regardless
memcal schedule due        # just the answer
memcal disable             # stop scheduled work until explicitly enabled
memcal enable              # resume scheduled work
```

`memcal disable` stops the scheduled job and disables it across logins and restarts.
Both nightly Dream and periodic source checks stop. The installed app, its permissions,
and your data remain intact; manual commands and the web UI still work. Updating or
reinstalling the schedule preserves the disabled state. Resume with `memcal enable`.
Closing the terminal or web UI alone does not disable launchd scheduling.

Each run asks whether the day's pass is owed:

- **Owed** — collect from every source, then run the extraction pass. A laptop
  asleep at 03:00 runs one catch-up pass when it wakes. After being powered off,
  it checks at the next login. Several missed nights need one catch-up pass.
- **Not owed** — check sources due for another read and collect new records from
  reachable sources. This does not run Dream or make model calls. WhatsApp's database
  is read without opening its desktop app unless background startup is explicitly
  enabled separately.

A pass remains owed until both collection and dream succeed. A failure or
shutdown during the pass leaves a pending marker, so the next check or login
tries again. A successful pass clears it and prevents another extraction pass
for the same night. `memcal schedule status` reports when a retry is owed.

`memcal update` regenerates an installed job script and reloads launchd unless
scheduling is disabled. To repair
it separately, run `memcal schedule install` from your normal installation. An
existing pending pass remains owed through update or reinstall. Reinstall acknowledges
the previous exit warning while waiting for a fresh invocation. Missing scripts,
unloaded jobs and fresh startup failures still report immediately; a new failed
invocation restores the warning. Collection runs once before nightly extraction,
which uses `--no-ingest` to avoid pulling the same sources again.

Daytime collection between passes is model-free; dream stays nightly. See
[Daytime freshness](../architecture/freshness.md) and [Dream](../architecture/dream.md).
