# Agent conversations

Inbound user turns from the Hermes and OpenClaw integrations archive like any
other stream — under the agent stream, threaded by session. They matter twice:
they are evidence ("Poker moved to Saturday, I'm still going" is a genuine user
correction), and they are the turns freshness hints must reach.

Only the user's original message is kept as user-authored evidence. Assistant
replies, generated summaries, injected snapshots, and tool output are excluded
on the archival path — never ingested as if they were user facts. That boundary
is what keeps a wrong assistant answer from reinforcing itself at the next
dream: the original source evidence and your actual words decide, not the
assistant's Saturday answer.

A user asking "Is poker still Saturday?" is asking, not affirming Saturday. "You
told me Saturday" attributes a claim to the assistant; it is not an independent
correction. Both cases run through the ordinary semantic review with roles and
attribution preserved. See [Daytime freshness](../architecture/freshness.md) and
[Evidence & provenance](../architecture/evidence.md).
