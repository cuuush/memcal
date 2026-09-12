# CLI reference

`memcal --help` lists commands; `memcal help <command>` covers one;
`memcal completion zsh|bash` prints shell completion. A bare handle opens detail:
`memcal E286` is `memcal open E286`. `--home` points at another store.

## Read the current picture

| Command | Purpose |
|---|---|
| `brief` | Print `brief.md` |
| `week` | The event window |
| `month [yyyy-mm]` | Everything for a month |
| `todos` | Open to-dos |
| `open <E/T/Q>` | Everything about one handle |
| `page <slug>` / `pages` | Wiki pages |
| `search <query>` | Full-text archive search |

## Tell and correct

| Command | Purpose |
|---|---|
| `remember <text>` | Statement, effective now |
| `add` / `status` / `rm` | Event rows |
| `series` | Recurring rules |
| `todo` / `done` | Obligations |
| `ask` / `answer` | Open questions |
| `note` / `alias` / `merge` | Wiki facts and names |

## Identity

| Command | Purpose |
|---|---|
| `who` | Unnamed handles, assumed merges, doubts |
| `who <handle> "<name>"` | Name one by hand |
| `who --adopt` / `--resolve` / `--split` / `--confirm` | Bulk, model, undo, accept |

## Feed and reconcile

| Command | Purpose |
|---|---|
| `sources` | What is connected and usable |
| `login <source>` | One-time interactive sign-in |
| `ingest [source\|all]` | Collect (`--limit`, `--rounds`, `--stale`, `--due` for due-only) |
| `mail` | Queue by priority; `--backfill [--apply]` recovers bodies |
| `senders` / `top` / `block` | Email gate table, always-pass senders, hard blocks |
| `gatecheck` | Gate pass/reject inspection |
| `dream` | The pass (`--dry-run`, `--retry`, `--redo`, `--rounds`, `--no-sweep`) |
| `schedule` | Nightly job (`status`, `install`, `run`, `due`) |

## Inspect and administer

| Command | Purpose |
|---|---|
| `trace` / `review` / `stats` | Calls, outcomes, volume |
| `doctor` | What is wrong, and what to type about it |
| `models` | Priced model list on OpenRouter; provider/default/in-use elsewhere |
| `setup` / `init` | Provider/model setup; store init |
| `ui` | Web UI (loopback) |
| `ical` / `reminders` | macOS access and publishing setup |
| `openclaw` | OpenClaw integration install/status |
