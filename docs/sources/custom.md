# Custom plugins

Beyond the built-ins, memcal loads `~/.memcal/plugins/*.py` plus anything
registered through the `memcal.sources` entry-point group. Start from
`examples/plugins/rss.py` — a small complete source.

Pick the transport shape that matches the platform
([Observe](../architecture/observe.md)): a plain `Source` with `fetch` for
simple feeds (the RSS example is one), `PolledSource` for anything with a
conversation listing and history, `StreamSource` for ordered or destructive
queues. Implement the shape's hooks plus contact/handle mapping and `check`;
watermarks, budgets, rounds, dedupe, gating, spooling, and
freshness all come from the shared path.

Checklist before relying on a custom source:

1. `memcal sources` reports it usable (or the exact gap).
2. `memcal ingest <name> --limit 20` archives and queues sane lines.
3. `memcal gatecheck --stream <name>` shows the gate admitting the right shape.
4. `memcal dream --dry-run` prices the resulting bundles.

Authoring details: [Custom sources](../integrations/custom.md).
