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
