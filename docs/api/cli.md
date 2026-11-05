# CLI reference

`memcal --help` lists commands; `memcal help <command>` covers one;
`memcal completion zsh|bash` prints shell completion. A bare handle opens detail:
`memcal E286` is `memcal open E286`. `--home` points at another store.

## Read the current picture

| Command | Purpose |
|---|---|
| `brief` | Render and print the current snapshot; next 30 days by default |
| `week` | The configured event window (`--back`, `--forward`) |
| `month [yyyy-mm]` | Everything for a month |
| `todos` | Open to-dos |
| `open <E/T/Q>` | Everything about one handle |
| `page <slug>` / `pages` | Wiki pages |
| `search <query>` | Full-text archive search |
| `activity [<E/T/Q>]` | New messages behind one plan (`--cursor`, `--limit`); omit the handle for unlinked backlog |

## Tell and correct

| Command | Purpose |
|---|---|
| `remember <text>` | Statement, effective now |
| `add` / `status` / `rm` | Event rows |
| `series` | Recurring rules |
| `todo` / `done` | Obligations |
| `ask` / `answer` | Open questions |
| `note` / `alias` / `merge` | Wiki facts and names |
| `reviewed <E/T/Q> --source-ids "…"` | Mark cited activity lines reviewed with no row change |

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
| `dream` | The pass with compact progress (`--verbose` for details; `--dry-run`, `--retry`, `--redo`, `--rounds`, `--no-sweep`) |
| `schedule` | macOS nightly job (`status`, `install`, `uninstall`, `run`, `due`) |
| `disable` / `enable` | Stop or resume macOS scheduled Dream and periodic source checks |

## Inspect and administer

| Command | Purpose |
|---|---|
| `trace` / `review` / `stats` | Calls, outcomes, volume |
| `doctor` | What is wrong, and what to type about it |
| `models` | Priced model list on OpenRouter; provider/default/in-use elsewhere |
| `setup` / `init` | Provider/model setup; store init |
| `update` | Update the installation and refresh its store and installed scheduler |
| `permissions` | Guide Full Disk Access approval and verify protected source reads through the app |
| `ui` | Web UI (loopback) |
| `ical` / `reminders` | macOS access and publishing setup |
| `openclaw` | OpenClaw integration install/status |

## Protected local source access (macOS)

```bash
memcal permissions status
memcal permissions setup
memcal permissions setup --target memcal
memcal permissions setup --no-open --json
```

The [Full Disk Access helper](../hosting/permissions.md) verifies the Memcal app bridge,
reveals `memcal.app`, and opens System Settings only for explicit setup. Status is passive; no store initialization or model call.

Calendar and Reminders use the app bridge too. Run `memcal ical setup` for Calendar
Automation and, when publishing is enabled, Calendar data access. Run
`memcal reminders setup --yes` for Reminders publishing. These grants belong to
`memcal.app`; the calling terminal or agent does not need them. See
[Calendar setup](../sources/calendar.md) and [Publishing](../hosting/publishing.md).

## Stop and resume scheduled work (macOS)

```bash
memcal disable
memcal schedule status
memcal enable
```

Disabling stops the installed scheduled job and keeps it disabled across logins,
restarts, updates, and schedule reinstall. It preserves the app, permissions, and
data. Manual commands and the web UI remain available. Closing a terminal or the web
UI alone does not stop scheduling. See [Scheduling](../hosting/scheduling.md).
