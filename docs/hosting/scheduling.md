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
```

Each run asks whether the day's pass is owed:

- **Owed** — collect from every source, then run the extraction pass. A laptop
  asleep at 03:00 runs one catch-up pass when it wakes. After being powered off,
  it checks at the next login. Several missed nights need one catch-up pass.
- **Not owed** — collect only from sources that are behind *and* reachable right
  now. Cheap, and it cannot cost a model call, which is why it is safe on every
  wake-up.

A pass remains owed until both collection and dream succeed. A failure or
shutdown during the pass leaves a pending marker, so the next check or login
tries again. A successful pass clears it and prevents another extraction pass
for the same night. `memcal schedule status` reports when a retry is owed.

`memcal update` regenerates an installed job script and reloads launchd. To repair
it separately, run `memcal schedule install` from your normal installation. An
existing pending pass remains owed through update or reinstall.

Daytime collection between passes is model-free; dream stays nightly. See
[Daytime freshness](../architecture/freshness.md) and [Dream](../architecture/dream.md).
