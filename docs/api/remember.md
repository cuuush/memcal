# Remember

Things you tell the agent directly write **immediately**. You are sitting right
there — if a follow-up two turns later has forgotten it, the whole thing feels
broken. `remember` runs model extraction over your words (it costs a call); the
typed writes below run as code with no model: they change the typed row, record
history, and update the next brief.

## CLI

```bash
memcal remember "Jordan is free for dinner Saturday night"
memcal add "Game night" saturday --time "8pm" --who Jordan
memcal todo "Send the reservation deposit" --due friday
memcal ask "Is dinner with Jordan Saturday or Sunday?"
memcal note jordan birthday "May 3"
memcal page travel "preferred flight time" "morning" --section preferences
```

| Command | Writes |
|---|---|
| `remember <text>` | A statement, attributed to you, effective now |
| `add <title> [date] …` | Event row (`--kind`, `--status`, `--where`, `--who`, `--series`) |
| `todo <text> …` | To-do (`--due`, `--when` wake condition, `--event`) |
| `ask <text>` | Open question |
| `note <page> <slot> <value>` | One durable wiki fact |
| `page <slug> [slot] [value]` | Read a page, or fill a slot |

## MCP

| Tool | Writes |
|---|---|
| `memcal_add` | New event row now |
| `memcal_todo` | To-do (`done=true` closes) |
| `memcal_note` | One durable wiki fact |
| `memcal_answer` | Record an answer, stop asking |
| `memcal_alias` | Two names, one page |

Corrections to existing rows are [Correct](correct.md), not this page. The
distinction matters: a new statement creates, a correction revises — and
precedence between them runs on when the evidence was said.
