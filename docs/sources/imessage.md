# iMessage

iMessage arrives via BlueBubbles when configured, with the local macOS Messages
database as a fallback.

- **BlueBubbles** (preferred where available): groups, participants, and
  attachments through your BlueBubbles server. Authenticate with
  `BLUEBUBBLES_PASSWORD` in `~/.memcal/.env`.
- **Fallback**: read-only access to `~/Library/Messages/chat.db` on the Mac. No
  secret; may require Full Disk Access.

Incremental on re-runs with its own stored cursor, deduplicated into the archive
like everything else. Verify with `memcal sources`, then
`memcal ingest imessage --limit 20`.

Inbound handles resolve through Contacts at ingest, so phone numbers arrive
already attached to people. See [Identity](../architecture/identity.md).
