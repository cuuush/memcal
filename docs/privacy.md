# Privacy and safety

Memcal is built for personal data, so its defaults are deliberately conservative:

- **Data lives locally** in SQLite and Markdown under `~/.memcal/`.
- **Only gated items reach a model.** The deterministic gate decides what the
  extraction pass even looks at; everything else stays on disk.
- **CLI model backends are stateless** — one-shot structured completions with no
  persistent sessions, hidden history, or model-side tools.
- **Publishing is off** until explicitly configured — the private store never
  creates, moves, or deletes live calendar or reminder data by inference.
- **The web UI binds to loopback**, with CSRF and origin checks, not the public
  network.
- **Blocked and ignored senders** are never fetched or stored beyond the block
  itself; test fixtures use fictional people.

Still treat the store as sensitive: `~/.memcal`, `.env` files, model-call traces
(`calls/`), and benchmark output stay out of version control under normal
filesystem access controls. Local Messages and calendar connectors may require
Full Disk Access or macOS Calendar and Reminders permissions. Memcal reads
sensitive personal data and sends selected source text to the model provider you
configure — read this page before connecting real accounts.
