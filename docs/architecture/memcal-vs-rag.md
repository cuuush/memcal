# memcal vs retrieval

Traditional RAG retrieves passages at query time and asks the model to sort them
out. That works for documents. It fails for a life.

## What goes wrong with retrieve-at-read

- **"Where is poker?"** A year-old email about a different poker night outranks
  last week's text, because relevance scoring drowns out the timestamp — the
  failure mode that motivated this project.
- **"Anything fun this weekend?"** There is no search phrase that finds every
  invitation, opportunity, and free friend. Retrieval needs a query; implicit
  questions have none.
- **"I might go to poker in two weeks" → "you still going?" → "yeah."** Three
  turns across two weeks resolve to one row only if something joined them when
  they arrived. No query-time ranking reconstructs that join.
- **Newest-wins is not a rule either.** A late old confirmation, a second booking,
  a non-attending participant repeating the old plan — each needs the *meaning* of
  the evidence, judged once, not a recency heuristic applied forever.

## What memcal does instead

| Retrieval | memcal |
|---|---|
| Stores passages, ranks at read | Reconciles into typed rows at write |
| Timestamp vs relevance, every query | Recency resolved once, moves to history |
| Needs a query string | Brief already holds the week; implicit questions answer from context |
| Cross-stream joins at read time | Bundling by entity joins at ingest |
| Stale copies compete forever | One value per row; corrections win per-field by said-time |
| Uncertainty is silence or confabulation | Uncertainty is a typed question with evidence |

Search is not gone — the archive stays full-text indexed for depth on demand, and
the assistant reads original messages whenever a freshness hint says to. But
search is the fallback, not the memory. The memory is the reconciled calendar,
kept current every night and annotated every few minutes, sitting in the prompt
before the user finishes typing.
