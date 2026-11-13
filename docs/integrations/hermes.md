# Hermes

Memcal can act as the memory provider for Hermes. It gives Hermes a snapshot of
your plans and facts, tools to look up supporting context, and tools to update
memory as you talk.

## Before you start

Install and set up Memcal and a current version of Hermes that supports memory
plugins. Hermes is a separate application; Memcal does not install it. Run
`memcal doctor` and `memcal brief` to check your Memcal installation. See
[Installation](../hosting/installation.md) for requirements and first-run setup,
and review the [privacy guide](../privacy.md) before connecting sources.

## Connect Memcal to Hermes

1. Install the provider using the same Memcal installation you normally run:

    ```bash
    memcal hermes setup
    ```

    Confirm the prompt. For a non-interactive install, use `memcal hermes setup
    --yes` to skip it.

2. Run `hermes memory setup`, select Memcal as the memory provider, and restart
   Hermes.
3. Start a conversation and ask a question about your plans, such as “What is
   on my calendar this week?” Hermes receives a `MEMCAL SNAPSHOT` on the first
   message that is more than a greeting or acknowledgement. Empty messages and
   slash commands also skip prefetch. Hermes should have access to 20 Memcal tools.

Setup works with both packaged and checkout installations. It copies the
provider to `MEMCAL_HOME/integrations/hermes/memcal` and links it at
`HERMES_HOME/plugins/memcal`. These homes default to `~/.memcal` and `~/.hermes`,
respectively. Setup records the Memcal runtime and store but does not change
Hermes configuration; the selection in step 2 is still required. It refuses to
replace an existing non-symlink plugin directory.

## Keep the provider up to date

After a direct `pip install --upgrade memcal`, run the following and restart
Hermes:

```bash
memcal hermes refresh --yes
```

`memcal update` refreshes existing managed integration copies automatically.
Refresh preserves locally edited managed files and reports a conflict instead
of overwriting them. For a manual live checkout link, the provider also remains
available at `integrations/hermes/memcal`; set `MEMCAL_SRC` to the checkout when
using that link.

## How memory reaches Hermes

For messages that trigger prefetch, Memcal reads local state without collecting sources or
calling a model. It supplies a snapshot on the first turn and when the brief or
newly collected source messages change. Unchanged state adds no new snapshot;
up to three wiki pages named in the query can still be recalled separately. The
snapshot covers the next 30 days by default and includes guidance for looking up
other dates. A snapshot is marked as delivered only after it appears in context;
it can be sent again after compression or an unsuccessful delivery.

Hermes keeps the provider's instructions stable for the conversation. Snapshot
updates instead travel in model-facing user-message context, while the original
user text is preserved. If a read fails, Memcal can supply `MEMCAL UNAVAILABLE`;
compatible Hermes versions can also report `MEMORY UNAVAILABLE` for failed,
timed-out, or busy reads. The provider instructs Hermes to refresh before
claiming current plans after an unavailable notice. Older Hermes versions cannot
always distinguish an unchanged read from a timeout, so the provider uses a
more conservative refresh rule there.

A whole-week overview can be answered from the snapshot. For a specific event,
Hermes is instructed to open the record for details and new messages. The last
successful Dream time indicates consolidation, not complete coverage or review
of later messages. See [Daytime freshness](../architecture/freshness.md) for
more on how updates reach a running session.

## What Hermes can do

- **Read memory:** `memcal_open` opens a record or wiki page. For a record it
  includes cited evidence, nearby context (up to 1,500 estimated tokens), and
  pending messages (up to 50 within 4,000 estimated tokens), in separate
  sections. If needed messages were omitted, `memcal_activity` continues after
  the last shown arrival. Reading does not acknowledge a review or change a plan.
- **Search and refresh:** Hermes can use `memcal_search_wiki`,
  `memcal_open_source`, `memcal_conversation`, `memcal_list_days`,
  `memcal_list_month`, `memcal_search_archive`, `memcal_activity`, and
  `memcal_refresh`.
- **Update memory:** The typed tools `memcal_add`, `memcal_update`,
  `memcal_schedule`, `memcal_move_once`, `memcal_merge`, `memcal_drop`,
  `memcal_todo`, `memcal_answer`, `memcal_note`, `memcal_alias`, and
  `memcal_reviewed` execute writes with evidence in the same transaction.
  These writes change Memcal's memory, not Calendar.app events.
- **Save conversation context:** Memcal archives clean user messages from Hermes
  turns under the agent channel. Assistant replies, injected snapshots, and tool
  outputs are excluded on these paths. The brief is rewritten at session end so
  the next session starts current.

Hermes does not expose the MCP-only `memcal_name` tool. To confirm a guessed
sender name, use the Conversations page or the CLI.

## Source updates and Dream

Source collection runs separately when scheduling is enabled (the default due
interval is five minutes). New arrivals can change the brief or trigger another
snapshot even when displayed activity counts are capped. Redelivery of the same
source item and ordinary archived user turns do not trigger updates. Nightly
Dream consolidates messages into stored plans and facts; collection alone does
not mean every message has been reviewed.
