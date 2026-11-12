# Privacy and safety

Memcal is built for personal data, so its defaults are deliberately conservative:

- **Data lives locally** in SQLite and Markdown under `~/.memcal/`.
- **The gate selects extraction candidates, not every byte sent.** Model prompts
  can also include neighboring messages that failed the gate, earlier plan
  evidence, related group-chat context, and existing memory needed to interpret
  candidates. Do not treat an item failing the gate as a promise it stays off-model.
- **CLI model backends are stateless** — one-shot structured completions with no
  persistent sessions, hidden history, or model-side tools.
- **Publishing is off** until explicitly configured — the private store never
  creates, moves, or deletes live calendar or reminder data by inference.
- **The web UI binds to loopback**, with CSRF and origin checks, not the public
  network.
- **Blocked and ignored senders** are never fetched or stored beyond the block
  itself; test fixtures use fictional people.

Still treat the store as sensitive: `~/.memcal`, `.env` files, model-call traces
(`calls/`) stay out of version control under normal
filesystem access controls. Local Messages and calendar connectors may require
Full Disk Access or macOS Calendar and Reminders permissions. Memcal reads
sensitive personal data. With a remote model backend, selected source text and
context leave your machine for that provider. Read this page before connecting
real accounts.

## Using a truly local model

You can select `openai-compatible` and run a compatible model server on your own
machine. With a loopback endpoint, a server that performs inference locally
without forwarding prompts, and fallback disabled, Memcal model requests need not
be transmitted to a remote model provider. See the [local configuration example](hosting/configuration.md#local-model-server).

A model name, an open-weight model, or the `openai-compatible` label alone does not
make inference local. Verify the server configuration and its network behavior;
proxies, tunnels, redirects, or a server with cloud inference can still send
prompts remotely. Memcal does not sandbox the server or enforce offline operation.

Fallback is disabled by default. If you configure a remote fallback, a failed local
completion can send the same prompt, including source messages and expanded
context, to that remote provider. Clear `MEMCAL_FALLBACK_PROVIDER` for local-only
model use, and check the effective settings because environment/configuration
overrides can change the selected endpoint or fallback.

Local inference is not a claim that all Memcal activity is offline: connected
sources contact their services, and a host agent reading Memcal tools can send
returned information to its own model provider. Configure that agent separately.
Local archives, credentials, and model-call traces still require protection.
