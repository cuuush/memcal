# Grading

Facts and mistakes — not storage layout. List questions expose duplicates and
false merges that individual lookups miss; interaction outcomes are reported
separately from factual recall.

## Task success

The primary rate is assigned user tasks answered correctly within the completion
deadline. Unanswered questions, timeouts, tool-policy failures, and refusals of
answerable questions are failures, kept in the denominator. A justified "the
messages do not establish that" is correct only where evidence is genuinely
insufficient — unretrieved available evidence is not the same thing.

## Facts

For a fixed set of answerable fact slots, each is **correct**, **missed**, or
**misrepresented** (summing to 100%; a wrong value is misrepresented, not also
missed). Precision, recall, and F1 follow: a wrong replacement is both a false
positive and a false negative; an invented extra claim is a false positive.
Repetition earns no extra credit; asserting both the current and old time as
current earns no correct slot. Repeats and variants average within each base
life; lives carry equal weight.

Complete-plan questions add a stricter percentage: an occasion counts only if
its required current identity, status, time, location, and requested fields are
right with no duplicate commitment asserted. Unknown-answer questions score
abstention accuracy separately.

## Interactions and failures

Reported separately from recall: whether an in-story correction was handled,
whether a success claim was supported, whether clarification was sought, whether
the result survived a new session, later collection, and nightly processing.
Stale assertions, invented facts, wrong-person and wrong-occurrence
associations, duplicate commitments, false merges, and correct abstention are
counted as their own categories; historical recall stays separate from
current-state accuracy.

## Efficiency beside correctness

End-to-end latency (including prefetch, context loading, and tools), archive
reads, driver and provider model calls and tokens, nightly processing cost, and
total week cost — with prepared-context answers reported separately from answers
needing new-source checks. Daytime freshness adds exposure time, collection lag,
hint and snapshot content, source reads, answer time, and the later dream
outcome. Unknown cost stays unknown, never zero.

## Auditable

Every score traces to the exact question, scenario time, available evidence,
expected facts, actual answer, fact-by-fact verdict, and grader explanation —
with full transcripts, tool definitions and calls, context injection, and corpus
and configuration hashes. Reviewers can record corrected verdicts with reasons
while the originals persist; all aggregates recompute from versioned verdicts.
Passes are sampled as well as failures, so a generous grader is detectable. The
two-provider pilot is not accepted until both successes and failures have been
inspected against their evidence.
