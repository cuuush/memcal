# FAQ

**Why does memcal keep its own calendar instead of using mine?**
So inference can never create, move, or delete live calendar data. The private
store is optimistic and reconciled nightly; publishing to a real calendar is an
explicit opt-in. See [Publishing](hosting/publishing.md).

**Why didn't my message change anything?**
Most days the correct answer is nothing. Check `memcal gatecheck` for whether
the line passed the gate, `memcal mail` for its priority, and `memcal review`
for what the last pass did. If the line is old, it may sit outside the spool
horizon — archived and searchable, not queued.

**A row is wrong. Do I wait for tonight?**
No — correct it now with a typed write (`memcal status`, `memcal_update`,
`memcal answer`). Daytime corrections survive dream without duplication. See
[Correct](api/correct.md).

**"Alex" matched the wrong person.**
Names are keys until evidence says otherwise. Name it by hand (`memcal who`),
confirm or split the merge, and Contacts-backed identities will outrank future
guesses. See [Identity](architecture/identity.md).

**Why is there no Discord source?**
Its API will not hand a human's direct messages to any token that does not
violate its terms, and server channels alone did not earn a connector.

**Why can't Signal backfill?**
Signal keeps no server-side archive — only the undelivered queue. A linked
device sees traffic from the link moment forward. See [Signal](sources/signal.md).

**User token or bot token for Slack?**
User token (`xoxp-…`). Bots cannot see your DMs, and DMs are the point. Scopes
freeze at install time — Reinstall after adding any. See [Slack](sources/slack.md).

**Is the Telegram session file dangerous?**
It grants full account access. Keep `~/.memcal` private, same as your `.env`.
See [Telegram](sources/telegram.md).

**My to-do will never complete on its own.**
Correct — memcal never infers completion. The agent raises it conversationally;
you confirm with `memcal done`. See [To-dos](architecture/todos.md).

**Something never resolves. Now what?**
It becomes a [question](architecture/questions.md): kept with its evidence,
retried as traffic arrives, asked about instead of guessed.

**Do benchmarks cost money?**
`--layer integration` is free (~1s). `--layer model` spends real calls — only
for deliberate extraction evaluation. The full seven-day experiment is
budget-bounded with resumable progress. See [Evaluation](evaluation/index.md).
