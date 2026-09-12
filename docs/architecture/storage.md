# Storage

Everything lives under `~/.memcal/` by default (`MEMCAL_HOME` moves it):

```text
~/.memcal/
├── memcal.db       typed state, archive, provenance, full-text search
├── brief.md        the compact snapshot an agent sees
├── calls/          prompts, replies, usage, model-call traces
├── plugins/        optional custom source plugins
└── wiki/           readable pages for people, places, projects, preferences
```

Two substrates, chosen for different reasons. **SQLite** holds everything
structured — events, to-dos, the archive and its FTS5 index, handles, senders —
one file, built in. **Markdown files** hold the wiki and only the wiki: prose
pages about parts of your life, human-readable, hand-editable, at home in any
editor.

The archive is append-only. Typed rows can be corrected, merged, or withdrawn,
but evidence and value history stay queryable. Model-call traces stay local, so
any row can be chased back to the exact generation that wrote it.

Treat the store as sensitive: keep `~/.memcal`, `.env` files, model-call traces,
and benchmark output out of version control, guarded by normal filesystem access
controls.
