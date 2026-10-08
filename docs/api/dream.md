# Dream runs

## Preview, then run

```bash
memcal dream --dry-run   # bundles, size, estimated cost — calls nothing
memcal dream             # run the pass
memcal dream --rounds 3  # repeat until the spool drains
memcal dream --no-sweep  # skip the final cheap sweep
memcal dream --mode nightly --model gpt-6-luna
```

`--limit` caps the item budget per pass; `--redo` un-claims processed items for
reprocessing (merge-on-key, so corrections are not duplicated).

## Resume or retry a pass that failed

After an interrupted pass, `memcal dream` offers to reuse its saved propose calls.
Enter accepts; non-interactive runs resume automatically. Replaying saved calls
costs no new inference, while unread bundles are proposed with the current
configuration. Replay skips bundles whose review inputs changed. Existing writes
remain recorded and replay guards prevent duplicate application.

The History page shows whether each pass was ok, partial, failed, running, or priced
only. Retrying releases the lines that pass claimed and runs them with the
provider and model configured now:

```bash
memcal dream --retry 29
```

Only failed or partial runs with bundles are retryable. Lines beyond the model
horizon stay marked as read.

A successful resume clears the old run's retry alert while preserving its original
error and linking to the successful run. A reply recovered after a Codex reconnect
error is retained if the completion succeeds; terminal failures still count toward
the run's usage.

## Inspect what happened

```bash
memcal trace             # prompt, reasoning, reply of a real model call
memcal trace 29          # saved stage progress, failures, and calls for a run
memcal review            # what it wrote, what it wants to know, what it can't resolve
memcal stats             # post-gate volume and run history
memcal gatecheck         # what the gate is passing and rejecting
```

Every real pass saves stage progress and diagnostic errors in
`~/.memcal/calls/run-XXXX/dream.jsonl`, including crash tracebacks. The web run
details expose this as **Saved dream log**. Failures before a valid model reply
are still inspectable; a run with writes and later errors is shown as partial.

## Schedule

```bash
memcal schedule          # when the pass last ran, whether one is owed
memcal schedule install  # nightly launchd job
memcal schedule status
memcal schedule run      # run now regardless
memcal schedule due      # just the answer
```

See [Scheduling](../hosting/scheduling.md) for the launchd layout and
[Monitoring](../hosting/monitoring.md) for the History page.
