# Scheduling

One background item, `memcal`, appears in System Settings → Login Items &
Extensions. The installer builds a small local app wrapper so macOS attributes
Calendar access to memcal instead of Python. launchd starts it at 03:00, at
login, and every 30 minutes; an interval that elapsed while the machine slept
fires on wake.

```bash
memcal schedule install    # daily + catch-up launchd job
memcal schedule status     # loaded/at/next/last-pass/outcome
memcal schedule            # when the pass last ran, whether one is owed
memcal schedule run        # run now regardless
memcal schedule due        # just the answer
```

Each run asks whether the day's pass is owed:

- **Owed** — collect from every source, then run the extraction pass. A laptop
  shut or asleep at 03:00 runs one catch-up pass when the lid opens.
- **Not owed** — collect only from sources that are behind *and* reachable right
  now. Cheap, and it cannot cost a model call, which is why it is safe on every
  wake-up.

Daytime collection between passes is model-free; dream stays nightly. See
[Daytime freshness](../architecture/freshness.md) and [Dream](../architecture/dream.md).
