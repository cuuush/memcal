# Configuration

Every `MEMCAL_*` knob lives in `~/.memcal/.env` (one `key=value` per line) and is
editable from the web UI's Settings tab, which writes that file, preserves
hand-written lines, and applies immediately. Clearing a field restores the
built-in default. `memcal setup` owns the provider/model subset; you own the
rest.

## Precedence

`.env` files merge checkout → store → working directory, later winning — so the
working directory outranks the store. Publishing keys read the store file plus
the environment (checkout and working-directory files excluded). A
blank value counts as absent. The Settings tab warns when another file shadows
the store.

## Settings

### Provider

| Key | Default | Meaning |
|---|---|---|
| `MEMCAL_LLM_PROVIDER` | `codex` | `codex`, `claude-code`, `antigravity`, or `openrouter` |
| `MEMCAL_PROPOSE_MODEL` | provider native | Night-traffic proposer — the main cost |
| `MEMCAL_SWEEP_MODEL` | provider native | Stale-row and question revisitor |
| `MEMCAL_MATCH_MODEL` | provider native | Merge arbitrator for real conflicts |
| `MEMCAL_REASONING_EFFORT` | model default | `low`, `medium`, or `high` |
| `MEMCAL_MAX_PARALLEL` | `8` (1–64) | In-flight model requests |
| `MEMCAL_LLM_COMMAND_TIMEOUT` | `900` (30–7200s) | CLI-provider timeout |
| `MEMCAL_CODEX_COMMAND` / `MEMCAL_CLAUDE_COMMAND` / `MEMCAL_AGY_COMMAND` | `codex` / `claude` / `agy` | Absolute path safest under launchd |

Provider natives: Codex → `gpt-5.6-luna`, Claude Code → `claude-sonnet-5`,
Antigravity → `gemini-3.8-flash-high`. Authenticate the selected CLI first
(`codex login`, `claude auth login`, a signed-in `agy` session); OpenRouter needs
`OPENROUTER_API_KEY`. Unknown model names stay valid — catalogs change faster
than validation lists.

### Brief

| Key | Default | Meaning |
|---|---|---|
| `MEMCAL_DAYS_BACK` | `3` (0–90) | Backward window |
| `MEMCAL_DAYS_FORWARD` | `7` (1–365) | Forward window |
| `MEMCAL_BRIEF_TOKEN_CAP` | `1500` (200–20000) | Whole-brief budget |

### Collection

| Key | Default | Meaning |
|---|---|---|
| `MEMCAL_SPOOL_HORIZON_DAYS` | `30` (1–365) | Older lines archive but never queue for the model |
| `MEMCAL_COLLECT_INTERVAL_MINUTES` | `5` (1–1440, minutes) | How often a checked source becomes due for `ingest --due` |
| `MEMCAL_EMAIL_BACKFILL_DAYS` | `0` = horizon | First mail reach, then watermark |
| `MEMCAL_PLATFORM_MUTE` | `show` | `show`, `ask`, or `mute` muted-chat evidence policy |
| `MEMCAL_ITEM_BUDGET` | `20000` | Lines per pass |
| `MEMCAL_ITEMS_PER_ENTITY` | `2000` | Lines per bundle |
| `MEMCAL_COLD_START_WAVES` | `4` (1–20) | Wave count for large first passes |

### Dream packing

| Key | Default | Meaning |
|---|---|---|
| `MEMCAL_PACK_BUNDLES` | `6` | Bundles per propose batch |
| `MEMCAL_PACK_TOKENS` | `12000` | Token budget per batch |
| `MEMCAL_PACK_STRATEGY` | `size` | `size` or `affinity` |
| `MEMCAL_AFFINITY_NEAR_DAYS` | `3` | Near-window for affinity packing |
| `MEMCAL_PROMPT_VERSION` | `v2` | `v2` or `v1` |
| `MEMCAL_PROPOSE_STAGES` | off | Comma list enabling staged extraction |
| `MEMCAL_BUNDLE_FORMAT` | `v1` | `v1` or `v2-quiet-stream` |

### Merge

| Key | Default | Meaning |
|---|---|---|
| `MEMCAL_SAME_EVENT_TOKENS` | `2` | Shared title words to merge |
| `MEMCAL_SAME_EVENT_POOR_TOKENS` | `1` | Threshold without participants/location |

### Publishing (off until named)

| Key | Default | Meaning |
|---|---|---|
| `MEMCAL_PUBLISH_CALENDAR` | off | macOS calendar name for confirmed events |
| `MEMCAL_PUBLISH_REMINDERS` | off | Reminders list for due tasks |
| `MEMCAL_REMIND_DEADLINES` | on | Internal deadline timestamps |

Scripted setup:

```bash
memcal setup --provider codex
memcal setup --provider codex --model gpt-5.6-luna
memcal setup --provider openrouter --api-key "sk-or-..."
```
