# CLI

The main experience is conversational — "What's my weekend looking like?", "When
am I seeing Jordan next?", "Move poker to Sunday and remind me to bring cash."
The CLI exposes the same state directly for everything around the conversation.

## Everyday flows

```bash
memcal brief                          # read the current picture
memcal week
memcal search "dinner next week"      # search original source material
memcal remember "Jordan is free for dinner Saturday night"
memcal add "Game night" saturday --time "8pm" --who Jordan
memcal status E286 confirmed
memcal todo "Send the reservation deposit"
memcal done T7
memcal E286                           # open a handle from the brief
```

Identity work (`memcal who`), feeding (`memcal ingest`, `memcal dream`), and
administration (`memcal doctor`, `memcal schedule`, `memcal ui`) live here too.
Full table: [CLI reference](../api/cli.md).
