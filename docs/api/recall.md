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
| `memcal_search_wiki` | Find stored facts by name, alias, label, value, or prose |
| `memcal_list_days` | Day or stretch (`saturday`, `tomorrow`, `weekend`, date) |
| `memcal_list_month` | Whole-month memory view |
| `memcal_search_archive` | Raw messages, email, notes (`person`, `channel`, `since`/`until`) |
| `memcal_source` | Full invitation body plus backing |
| `memcal_conversation` | Ordered conversation around a message (`before`/`after`) |
| `memcal_activity` | Page past an opened plan's activity cap, or read unlinked backlog |
| `memcal_refresh` | Re-render the snapshot without a model call |

The default brief covers today plus the next 30 days, with three days of recent
history. For a full calendar month, call `memcal_list_month(month="2026-10")`.
For the whole year, call `memcal_list_days(when="2026-01-01", days=365)`; use 366
for a leap year. Day and month reads include ongoing events that started before
the range. A shortened brief discloses missing rows instead of claiming completeness.

Open durable facts with `memcal_open(ref="me")` or a page name. When the page is
unknown, use `memcal_search_wiki` before searching raw archive messages. A flagged
event's `memcal_open` result already includes pending activity from its whole thread;
use `memcal_activity` only if the returned cap requires another page.

`memcal_conversation` takes a `line_id` from search results or a channel/thread
pair, so a flagged thread is one call away — the assistant never invents a
keyword search for known activity. See [Daytime freshness](../architecture/freshness.md).
