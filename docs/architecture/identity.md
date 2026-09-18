# Identity

Names are keys, so two spellings of one name are two people until something says
otherwise, and an opaque platform id is nobody. Identity is a dictionary and stays
one — hash lookups at ingest, no model on the per-line path.

## Layers

1. **Contacts.** The address book loads into a dict (`+19175551234 → Jordan`) and
   resolves every inbound message before anything expensive runs. Contacts outrank
   everything below: a model guess cannot rename an address-book card, and two
   separate cards stay two people.
2. **Stable platform ids.** GroupMe and chat systems hand out opaque user ids; the
   display names are the unstable part and get ignored.
3. **Explicit naming.** `memcal who` covers the rest:

```bash
memcal who              # unnamed handles, assumed merges, open doubts
memcal who 12 "Jordan"  # name one by hand
memcal who --adopt      # take every name a platform already gave — free
memcal who --resolve    # one model call over the whole picture
memcal who --split 3    # undo an assumed merge, handle for handle
memcal who --confirm 3  # accept a merge, or act on a doubt
```

`--resolve` is the only step that costs anything: it sends every unnamed handle
and every known person in one request, so a handle is matched against the whole
roster rather than judged alone.

## Assumed, not decided

What the model concludes is provisional: merges take effect immediately, are
listed by number, and are reversible. It can also decline to decide, which records
a [question](questions.md) rather than a guess. "Alex" alone never resolves when
two Alexes exist; "Alex" plus poker and "Alex" plus work resolve differently, from
evidence.

## Guessed names

`--resolve` is deliberate and costs a call, which is the wrong tool for a one-off
automated sender you will never name by hand — a tire shop's appointment texts from
a bare number. So when a nightly [dream](dream.md) draws a calendar row or to-do
from a conversation whose sender has no name, it also invents a short, literal name
for that conversation in the same pass, at no extra call. A new sender that earned
a row is always named: if the model forgets, a deterministic check shows it the
exact gap and asks again.

A guess is the **weakest evidence there is** — below a platform nickname. Contacts,
a platform name, `--resolve`, or your own edit all overwrite it, so a guess never
displaces a real identity. It is written like any other link and recorded as a
reversible assumption, so it shows in `memcal who` and `--confirm` / `--split`
settle it; confirming promotes it out of guess status. Until then it renders with a
`maybe:` marker wherever a name appears, so a guess always reads as a guess.

Because naming a conversation resolves its handle to a person, and bundling is
keyed by person, one service reached two ways — an SMS and an email — bundles
together once both are named alike. After a pass writes its guesses, near-duplicate
ones are folded onto a single spelling ("Costco" into "Costco Tire Center") so the
two conversations join from then on; the fold is conservative (a shorter name only
folds into a longer one that contains all its distinctive words) and reversible.

Quiet guesses are left to age out. Only one that keeps drawing
[freshness](freshness.md) hints across enough distinct days earns a short line
asking you to confirm or rename it — persistence, not a one-off, is what warrants
the interruption.
