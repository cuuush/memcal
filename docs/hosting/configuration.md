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
| `MEMCAL_LLM_PROVIDER` | `codex` | `codex`, `claude-code`, `antigravity`, `grok`, or `openrouter` |
| `MEMCAL_PROPOSE_MODEL` | provider native | Night-traffic proposer — the main cost |
| `MEMCAL_SWEEP_MODEL` | provider native | Stale-row and question revisitor |
| `MEMCAL_MATCH_MODEL` | provider native | Merge arbitrator for real conflicts |
| `MEMCAL_REASONING_EFFORT` | model default | `low`, `medium`, or `high` |
| `MEMCAL_MAX_PARALLEL` | `8` (1–64) | In-flight model requests |
| `MEMCAL_LLM_COMMAND_TIMEOUT` | `900` (30–7200s) | CLI-provider timeout |
| `MEMCAL_CODEX_COMMAND` / `MEMCAL_CLAUDE_COMMAND` / `MEMCAL_AGY_COMMAND` / `MEMCAL_GROK_COMMAND` | `codex` / `claude` / `agy` / `grok` | Absolute path safest under launchd |

Provider natives: Codex → `gpt-5.6-luna`, Claude Code → `claude-sonnet-5`,
Antigravity → `gemini-3.8-flash-high`, Grok → `grok-4.5`. Authenticate the
selected CLI first (`codex login`, `claude auth login`, a signed-in `agy`
session, `grok` signed in to your xAI account); OpenRouter needs
`OPENROUTER_API_KEY`. Memcal checks that the command exists; the CLI itself
reports authentication trouble on the first real completion. Unknown model names stay valid — catalogs change faster
than validation lists.

Grok Build is installed with `npm i -g @xai-official/grok` (or the installer at
`https://x.ai/cli/install.sh`); `grok --oauth` signs in through the browser. Avoid
`brew install grok` — that is an unrelated formula.

Grok runs [Grok Build](https://docs.x.ai/build/cli/reference)'s headless mode
(`--output-format json`) as a plain completion — its tools, subagents, plan mode
and web search are all switched off. The packed prompt rides in a temp file
(`--prompt-file`) so a large propose wave never crosses the shell argument limit;
the output schema is passed inline as the `--json-schema` literal.

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
| `MEMCAL_IMESSAGE_BACKEND` | `bluebubbles` | `bluebubbles` (server) or `chatdb` (local database). See [iMessage](../sources/imessage.md) |
| `MEMCAL_IMESSAGE_FALLBACK` | on | With the BlueBubbles backend, read local `chat.db` when the server is unreachable; off = hard failure |
| `MEMCAL_BLUEBUBBLES_LOCATION` | `auto` | `auto` (infer from URL), `local`, or `remote` — decides whether the app may be opened |
| `MEMCAL_BLUEBUBBLES_AUTOSTART` | on | Open a down **local** BlueBubbles server (hidden) during the nightly pass |
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
memcal setup --provider claude-code
memcal setup --provider antigravity
memcal setup --provider grok
memcal setup --provider codex --model gpt-5.6-luna
memcal setup --provider openrouter --api-key "sk-or-..."
```
