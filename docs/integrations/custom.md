# Custom sources

Custom sources live in `~/.memcal/plugins/` or register through the
`memcal.sources` Python entry-point group. See `examples/plugins/rss.py` for a
small complete example.

A new platform implements hooks, not another ingest loop. Pick the shape that
matches the platform:

- **`PolledSource`** — enumerate conversations, read each forward from a
  per-chat watermark. For anything with a listing and history (chat APIs, mail).
- **`StreamSource`** — drain one ordered queue from a single cursor. For
  destructive or cursor-only transports.

A plain `Source` implements `fetch` plus `check` (the RSS example is exactly
this). `PolledSource` / `StreamSource` implement `connect` plus their hooks
(`conversations` / `history` / `normalize`, or `stream` / `normalize`) plus
`check`. Add identity mapping so handles resolve; `memcal sources` and
`memcal doctor` report through `check`. Watermarks, budgets, rounds, dedupe,
gating, and spooling come from the shared path — collection, review accounting,
and freshness hints work for custom sources with no extra code.
