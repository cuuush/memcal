# Recall

Reads are compact by default and expandable on demand. Start from the brief; open
handles for depth; search the archive when nothing prepared covers it.

## CLI

```bash
memcal brief                 # the current snapshot
memcal week                  # the event window (--back/--forward/--json)
memcal month                 # whole month, separate from the user calendar
memcal todos                 # open to-dos
memcal E286                  # everything about one handle
memcal page jordan           # a wiki page
memcal search "dinner next week"   # full-text archive search
```

## MCP

| Tool | Reads |
|---|---|
| `memcal_brief` | The always-in-context block — read first |
| `memcal_open` | One brief line: detail, links, messages, history |
| `memcal_open_page` | Wiki page facts, encounters, citations |
| `memcal_list_days` | Day or stretch (`saturday`, `tomorrow`, `weekend`, date) |
| `memcal_list_month` | Whole-month memory view |
| `memcal_search_archive` | Raw messages, email, notes (`person`, `stream`, `since`/`until`) |
| `memcal_source` | Full invitation body plus backing |
| `memcal_conversation` | Ordered conversation around a message (`before`/`after`) |

`memcal_conversation` takes a `line_id` from search results or a stream/thread
pair, so a flagged thread is one call away — the assistant never invents a
keyword search for known activity. See [Daytime freshness](../architecture/freshness.md).
