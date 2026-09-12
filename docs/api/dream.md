# Dream runs

## Preview, then run

```bash
memcal dream --dry-run   # bundles, size, estimated cost — calls nothing
memcal dream             # run the pass
memcal dream --rounds 3  # repeat until the spool drains
memcal dream --no-sweep  # skip the final cheap sweep
memcal dream --mode nightly --model gpt-5.6-luna
```

`--limit` caps the item budget per pass; `--redo` un-claims processed items for
reprocessing (merge-on-key, so corrections are not duplicated).

## Retry a pass that failed

The Runs view shows whether each pass was ok, partial, failed, running, or priced
only. Retrying releases the lines that pass claimed and runs them with the
provider and model configured now:

```bash
memcal dream --retry 29
```

Only failed or partial runs with bundles are retryable. Lines beyond the model
horizon stay marked as read.

## Inspect what happened

```bash
memcal trace             # prompt, reasoning, reply of a real model call
memcal review            # what it wrote, what it wants to know, what it can't resolve
memcal stats             # post-gate volume and run history
memcal gatecheck         # what the gate is passing and rejecting
```

## Schedule

```bash
memcal schedule          # when the pass last ran, whether one is owed
memcal schedule install  # nightly launchd job
memcal schedule status
memcal schedule run      # run now regardless
memcal schedule due      # just the answer
```

See [Scheduling](../hosting/scheduling.md) for the launchd layout and
[Monitoring](../hosting/monitoring.md) for the Runs view.
