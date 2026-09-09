import { el, nf, api, jumpToBundle } from "./core.js";
// A genuine two-way link with runs.js: seatRow (below) opens a call from
// outside it, and callCard/openRun (runs.js) open a call's trace inline.
// Safe as a cycle — every cross-referenced binding here is a `function`
// declaration, which ES modules hoist and initialize before either module's
// top-level code runs.
import { findInTrace, openCallAndFind } from "./runs.js";

export async function openTrace(gen, holder, needle, opts) {
  opts = opts || {};
  holder.innerHTML = '<div class="empty">loading the call…</div>';
  const t = await api("/api/trace?gen=" + encodeURIComponent(gen));
  holder.innerHTML = "";
  if (t.error) {
    holder.append(el("div", "empty", t.error));
    if (t.wrote && t.wrote.length) {
      holder.append(el("div", "note",
        "This call also wrote: " + t.wrote.map(w => `${w.kind} ${w.verb}`).join(", ")));
    }
    return;
  }
  const n = t.native || {};
  const stat = el("div", "tstats");
  stat.textContent = [
    // First, and in the form the user would type it: "run 5 call 12".
    t.run && t.call ? `run ${t.run} · call ${t.call}` : "",
    n.provider, t.model, n.latency ? n.latency + "ms" : "",
    `${nf(n.prompt || t.prompt_tokens)} in`,
    n.cached ? `${nf(n.cached)} cached` : "",
    `${nf(n.completion || t.completion_tokens)} out`,
    n.reasoning ? `${nf(n.reasoning)} thinking` : "",
    t.max_tokens ? `ceiling ${nf(t.max_tokens)}` : "",
    n.finish ? "finish: " + n.finish : "",
    t.source === "disk" ? "from disk" : t.source === "openrouter" ? "from OpenRouter" : "",
  ].filter(Boolean).join(" · ");
  holder.append(stat);
  // The one thing worth shouting about: a call that stopped because it hit the ceiling
  // returned truncated JSON, and nothing downstream noticed.
  if (String(n.finish || "").toLowerCase() === "length") {
    const w = el("div", "banner");
    w.textContent = "This call stopped because it ran out of output, not because it was "
      + "finished. Its JSON was truncated, so some of what it worked out never landed.";
    holder.append(w);
  }
  /* What is actually known, which depends on the contract the call ran under. Under v1
     "no diff" and "read it, nothing to say" are the same evidence — run 5's reasoning
     shows the model read all six of these and was right that every one was junk, and
     the banner still called it a failure. Say what happened; do not diagnose it. */
  if (t.unrouted && t.unrouted.length) {
    const w = el("div", "banner");
    const names = t.unrouted.map(b => b.label).join(", ");
    w.textContent = t.contract === "v2"
      ? `${t.unrouted.length} bundle(s) were neither reviewed nor answered for: ${names}. `
        + `Under this contract the model lists everything it read, so these were genuinely `
        + `not looked at. They stayed queued.`
      : `${t.unrouted.length} bundle(s) got no diff back: ${names}. They stayed queued, `
        + `so a later pass re-reads them — which is the safe default and often the wrong `
        + `one. This request used the v1 contract, where "read it, nothing to say" and `
        + `"never answered" look identical. Read the reasoning below to tell which.`;
    holder.append(w);
  }
  // The run card already lists this call's bundles directly above the trace. Drawing
  // them again inside it read as two different lists of the same thing.
  if (!opts.hideBundles && t.bundles && t.bundles.length) {
    const grid = el("div", "grid3");
    grid.style.margin = "8px 0";
    const landed = new Set((t.routed || []).map(r => r.id));
    for (const ref of t.bundles) {
      grid.append(seatRow({...ref, outcome: landed.has(ref.id) ? "read" : "no diff"},
                          holder));
    }
    holder.append(grid);
  }
  if (t.wrote && t.wrote.length) {
    holder.append(el("div", "note", "wrote: "
      + t.wrote.map(w => `${w.kind} ${w.verb} ${w.ref}`).join(" · ")));
  }

  const panes = [];
  const part = (label, text, cls, open) => {
    if (!text) return null;
    const d = el("details", "tpart " + (cls || ""));
    d.append(el("summary", null, `${label} — ${nf(Math.round(text.length / 4))} tok approx`));
    const pre = el("pre"); pre.textContent = text;
    d.append(pre);
    d.open = !!open;
    holder.append(d);
    panes.push({label, pane: d, pre, text});
    return d;
  };
  for (const m of (t.messages || [])) {
    part(m.role === "system" ? "input · instructions (the cached prefix)"
                             : "input · the bundles", m.text);
  }
  part("reasoning", t.reasoning, "think");
  part("what it answered", t.completion || "(nothing)", "", true);

  traceSearch(panes, needle);
}

/* ------------------------------------------------------- search a call -- */
/* A propose call is 30k tokens in and 10k out. Finding the four lines that wrote one
   row by scrolling is not a thing anyone will do twice, so: one box that searches
   every pane at once, arrow keys to walk the hits, and a seeded query when the panel
   was opened from a specific row. */
export function traceSearch(panes, needle) {
  const bar = el("div", "tsearch");
  const input = el("input");
  input.type = "search";
  input.placeholder = "find in this call — prompt, reasoning and reply  (Enter / ⇧Enter)";
  const nav = el("span", "tnav", "");
  const prev = el("button", "whybtn", "‹");
  const next = el("button", "whybtn", "›");
  bar.append(input, nav, prev, next);
  // Above the panes: the thing you type in should not sit below what it searches.
  const first = panes.length ? panes[0].pane : null;
  if (!first || !first.parentNode) return bar;
  first.parentNode.insertBefore(bar, first);

  let hits = [], at = -1;

  const clear = () => {
    for (const p of panes) { p.pre.textContent = p.text; }
    hits = []; at = -1; nav.textContent = "";
  };

  const run = () => {
    const q = input.value.trim();
    clear();
    if (q.length < 2) return;
    const needleLower = q.toLowerCase();
    for (const p of panes) {
      const text = p.text, lower = text.toLowerCase();
      let from = 0, cut = 0;
      const frag = document.createDocumentFragment();
      while (true) {
        const i = lower.indexOf(needleLower, from);
        if (i < 0) break;
        frag.append(document.createTextNode(text.slice(cut, i)));
        const mark = el("mark", "thit", text.slice(i, i + q.length));
        frag.append(mark);
        hits.push({mark, pane: p.pane});
        cut = i + q.length;
        from = cut;
      }
      if (!hits.length && cut === 0) continue;
      frag.append(document.createTextNode(text.slice(cut)));
      p.pre.textContent = "";
      p.pre.append(frag);
    }
    nav.textContent = hits.length ? `1/${hits.length}` : "no match";
    if (hits.length) go(0);
  };

  const go = i => {
    if (!hits.length) return;
    if (at >= 0 && hits[at]) hits[at].mark.classList.remove("on");
    at = (i + hits.length) % hits.length;
    const hit = hits[at];
    hit.pane.open = true;
    hit.mark.classList.add("on");
    hit.mark.scrollIntoView({block: "center"});
    nav.textContent = `${at + 1}/${hits.length}`;
  };

  let debounce;
  input.oninput = () => { clearTimeout(debounce); debounce = setTimeout(run, 160); };
  input.onkeydown = e => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    if (!hits.length) run(); else go(at + (e.shiftKey ? -1 : 1));
  };
  next.onclick = () => go(at + 1);
  prev.onclick = () => go(at - 1);

  if (needle) { input.value = needle; run(); }
  return bar;
}
/* One bundle inside a call. `holder` is that call's open trace, when there is one —
   then clicking the row searches the prompt for this bundle rather than sending you to
   the Dream tab, which only ever holds what has *not* been read yet and so almost never
   has the bundle you are looking at. The text you want is right here in the call. */
export function seatRow(s, holder) {
  const row = el("div", "seat" + (s.outcome === "read" ? "" : " miss"));
  const b = el("span", "bid link", s.id);
  const name = el("span", "seatname", s.label || s.entity);
  row.append(b, name);
  row.append(el("span", "seatmeta", `${nf(s.lines)} lines · ${s.outcome}`));
  /* Inside a call, both the id and the name go to the same place: this bundle's own
     lines in the prompt of the call in front of you. They used to send you to the Dream
     tab, which holds only what has *not* been read — so for any bundle in a past run it
     reliably landed on "not in the next pass", which is true and useless. */
  if (holder) {
    row.style.cursor = "pointer";
    row.title = "find this bundle's lines inside this call";
    b.title = row.title;
    row.onclick = () => findInTrace(holder, s.id, s.label);
  } else if (s.gen) {
    // Listed outside a call (the unanswered bundles, the writes) — open the call that
    // carried it and land on its lines.
    row.style.cursor = "pointer";
    row.title = "open the call this bundle was in";
    b.title = row.title;
    row.onclick = () => openCallAndFind(s.gen, s.id, s.label);
  } else {
    b.title = "show this bundle on the Dream tab (only if it is still unread)";
    b.onclick = e => { e.stopPropagation(); jumpToBundle(s.id); };
  }
  return row;
}
