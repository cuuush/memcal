import { $, el, nf, api, toast, state } from "./core.js";
import { runJob, watchJob } from "./jobs.js";
import { openWhy } from "./memory.js";

/* Bundle filtering is client-side: the preview is already in hand and re-fetching it
   costs a full rebuild of every bundle just to hide some of them. */
let bTimer;
$("#bq").oninput = e => { clearTimeout(bTimer); bTimer = setTimeout(() => {
  bfilter.q = e.target.value.trim(); rebundle(); }, 180); };
for (const [id, key] of [["#bstream", "stream"], ["#bkind", "kind"], ["#bflag", "flag"]]) {
  $(id).onchange = e => { bfilter[key] = e.target.value; rebundle(); };
}
/* --------------------------------------------------------------- dream -- */
let preview = null;

export async function loadDream() {
  preview = await api("/api/dream_preview");
  if (preview.error) return;
  renderTiles(preview);
  renderWarning(preview);
  renderPrefix(preview);
  renderRequests(preview);
  renderBundles(preview);
  resumeJobs();
  if (state.bundleFlash) {
    const want = state.bundleFlash;
    state.bundleFlash = "";
    if (!flashBundle(want)) {
      toast(`bundle ${want} is not in the next pass — it has already been read. `
            + `Open its run to see it as it was sent.`);
    }
  }
}

function renderTiles(p) {
  const s = p.spool, box = $("#dtiles"); box.innerHTML = "";
  const tile = (k, v, sub, alert) => {
    const t = el("div", "tile" + (alert ? " alert" : ""));
    t.append(el("div", "k", k), el("div", "v", v), el("div", "s", sub || ""));
    return t;
  };
  box.append(
    tile("Last dream", p.last_dream.at ? p.last_dream.at.slice(5).replace("T", " ") : "never",
         p.last_dream.model || "no pass on record"),
    tile("Waiting", nf(s.pending), `gated and unread · ${nf(s.entities)} conversations`),
    // Selection is round-robin across conversations, capped per conversation, so the
    // interesting number is not "how many did not fit" but "whose tail got cut".
    tile("This pass reads", nf(s.taken),
         s.left_behind || s.unreached
           ? `${nf(s.left_behind)} tails left · cap ${s.per_entity}/chat, budget ${nf(s.item_budget)}`
           : "everything waiting",
         s.unreached > 0),
    tile("Bundles", nf(p.bundles.length),
         `${p.requests.length} request(s) of ≤${p.pack.bundles}, ${p.max_parallel} at a time`),
    tile("Model", (p.model || "").split("/").pop(),
         p.cost.priced ? `~$${p.cost.input} in · up to $${p.cost.output_ceiling} out`
                       : "no price on file"),
  );
  $("#collectnote").textContent = s.will_retire
    ? `${nf(s.will_retire)} waiting item(s) are older than ${s.horizon_days} days and will be retired unread`
    : "";
  $("#dreamnote").textContent = p.cost.priced
    ? `input ~$${p.cost.input}; output up to $${p.cost.output_ceiling} if every request runs to its ceiling`
    : "";
  // The count line belongs to renderBundles now — it has to say how many the filter is
  // showing, and two writers of one element means whichever ran last wins.
  $("#dream").disabled = !p.bundles.length;
}

function renderWarning(p) {
  const box = $("#dwarn"); box.innerHTML = "";
  const b = p.budget;
  if (!b.saturated) return;
  // Truncation here is silent: nothing reads finish_reason, a cut-off JSON body fails
  // to parse, and the pass records zero writes with no error. Say it out loud.
  const n = el("div", "banner");
  n.append(el("b", null, `${b.saturated} of ${b.calls} past propose calls ended exactly on their output ceiling`));
  n.append(el("p", null,
    `Landing on ${b.at.join(" or ")} tokens means the model stopped because it ran out of room, `
    + `not because it was done. Thinking is spent from the same ceiling and nothing reserves room `
    + `for it, so a request can reason until the budget is gone and return a diff that stops `
    + `mid-object. That parse failure is silent — the pass reports no error and writes nothing.`));
  box.append(n);
}

function renderPrefix(p) {
  const card = $("#dprefix"); card.innerHTML = "";
  const short = p.prefix.tokens < p.prefix.cache_min;
  card.append(el("div", "note",
    `Every request opens with this same ${nf(p.prefix.tokens)}-token preamble — today's date, `
    + `the memcal window, open to-dos, the wiki index, and known identities. It is marked cacheable`
    + (!p.cost.cache ? `, but this model endpoint does not support prompt caching.`
      : short ? `, but it is under the ${nf(p.prefix.cache_min)}-token minimum, so it will not cache.`
             : `, and the first ${p.cost.cache_misses} request(s) fire together against an empty `
               + `cache, so each pays to write it (${p.cost.priced ? "$" + p.cost.prefix_now : "?"} `
               + `rather than ${p.cost.priced ? "$" + p.cost.prefix_warmed : "?"} if one warmed it first).`)));
  const d = el("details");
  d.append(el("summary", null, "read the preamble as the model gets it"));
  const pre = el("pre"); pre.textContent = p.prefix.text;
  d.append(pre);
  card.append(d);
}

function renderRequests(p) {
  const box = $("#dreqs"); box.innerHTML = "";
  if (!p.requests.length) { box.innerHTML = '<div class="empty">nothing to send</div>'; return; }
  for (const r of p.requests) {
    const hot = p.budget.at.includes(r.max_tokens);
    const c = el("div", "req" + (hot ? " hot" : ""));
    c.append(el("h4", null, `request ${r.index} · ${r.bundles} bundle${r.bundles > 1 ? "s" : ""}`));
    const dl = el("dl");
    const add = (k, v, cls) => { dl.append(el("dt", null, k), el("dd", cls, v)); };
    add("input", nf(r.input_tokens) + " tok");
    add("output ceiling", nf(r.max_tokens) + " tok", hot ? "ceil" : null);
    c.append(dl);
    // Which bundles ride in this call, by the same six-character id shown on the bundle
    // itself. The entity strings were the only answer before, and they are the least
    // readable thing on the page.
    const riders = el("div", "riders");
    for (const b of (r.riders || [])) {
      const chip = el("span", "rider");
      chip.append(el("span", "bid", b.id));
      chip.append(el("span", "rname", b.label));
      chip.append(el("span", "rtok", `${b.count}L · ${nf(b.tokens)}t`));
      chip.title = b.entity;
      chip.onclick = () => flashBundle(b.id);
      riders.append(chip);
    }
    c.append(riders);
    if (hot) {
      const w = el("div", "note");
      w.style.cssText = "margin:6px 0 0;color:var(--warn)";
      w.textContent = "a past call truncated at exactly this ceiling";
      c.append(w);
    }
    box.append(c);
  }
}

/* A hundred and six conversations is a list you scroll past, not one you read. The
   search covers what was *said* as well as who said it — finding the bundle a plan is
   hiding in means searching for "gala", not for the person who mentioned it. */
const bfilter = {q: "", stream: "", kind: "", flag: ""};

/* How well a bundle answers the search, not merely whether it does. Typing "harper"
   means the conversation *with* Harper, not the forty chats where somebody mentioned
   them — so a name beats a speaker beats a mention, and the whole name beats part of
   one. 0 means no match at all. */
function bundleScore(b, q) {
  const name = (b.label || "").toLowerCase();
  if (name === q) return 100;
  if (name.startsWith(q)) return 90;
  if (name.includes(q)) return 80;
  if ((b.people || []).some(p => (p || "").toLowerCase().includes(q))) return 70;
  if ((b.entity || "").toLowerCase().includes(q)) return 60;
  if ((b.items || []).some(i => (i.who || "").toLowerCase().includes(q))) return 50;
  if ((b.items || []).some(i => (i.text || "").toLowerCase().includes(q))) return 20;
  return 0;
}

function bundleMatches(b) {
  if (bfilter.stream && !b.streams.includes(bfilter.stream)) return false;
  if (bfilter.kind === "person" && b.kind !== "person") return false;
  if (bfilter.kind === "group" && !b.group) return false;
  if (bfilter.kind === "thread" && (b.kind !== "thread" || b.group)) return false;
  if (bfilter.flag === "waiting" && !b.waiting) return false;
  if (bfilter.flag === "monologue" && !b.monologue) return false;
  if (bfilter.flag === "nopage" && !b.missing_pages.length) return false;
  if (bfilter.flag === "merged" && !b.merged.length) return false;
  return !bfilter.q || bundleScore(b, bfilter.q.toLowerCase()) > 0;
}

function renderBundles(p) {
  const box = $("#dbundles"); box.innerHTML = "";
  const streams = [...new Set(p.bundles.flatMap(b => b.streams))].sort();
  const sel = $("#bstream");
  if (sel.options.length !== streams.length + 1) {
    sel.innerHTML = '<option value="">every stream</option>';
    for (const s of streams) {
      const o = el("option", null, s);
      o.value = s;
      sel.append(o);
    }
    sel.value = bfilter.stream;
  }
  if (!p.bundles.length) {
    box.innerHTML = '<div class="empty">nothing waiting — collect first, or it is all already read</div>';
    $("#dbcount").textContent = "";
    return;
  }
  const shown = p.bundles.filter(bundleMatches);
  // Only a search reorders the list. With no query the order is the one the page has
  // always promised — biggest first, which is also the order the requests were packed in.
  if (bfilter.q) {
    const q = bfilter.q.toLowerCase();
    shown.sort((a, b) => bundleScore(b, q) - bundleScore(a, q) || b.count - a.count);
  }
  const lines = shown.reduce((a, b) => a + b.count, 0);
  const left = p.spool.left_behind;
  $("#dbcount").textContent = (shown.length === p.bundles.length
    ? `— ${p.bundles.length} conversations, ${nf(lines)} lines, biggest first`
    : `— ${shown.length} of ${p.bundles.length} conversations, ${nf(lines)} lines`)
    + (left ? `; ${nf(left)} older line(s) stay queued for next time` : "");
  if (!shown.length) {
    box.innerHTML = '<div class="empty">no bundle matches</div>';
    return;
  }
  for (const b of shown) box.append(bundleCard(b));
}

function rebundle() { if (preview) renderBundles(preview); }

/* Open one bundle and say where it is. Returns false when it is not on this page —
   which is the normal outcome for a bundle from an *old* run: the Dream tab shows what
   the next pass will read, and a conversation that has been read is no longer in it. */
function flashBundle(bid) {
  const card = document.getElementById("bundle-" + bid);
  if (!card) return false;
  card.open = true;
  card.scrollIntoView({behavior: "smooth", block: "center"});
  card.classList.add("flash");
  setTimeout(() => card.classList.remove("flash"), 1400);
  return true;
}


function bundleCard(b) {
  const d = el("details", "bundle");
  d.id = "bundle-" + b.id;          // so a request chip can jump straight here
  const sum = el("summary");
  sum.append(el("span", "bid", b.id));
  sum.append(el("span", "bname", b.label || b.entity));
  sum.append(el("span", "pill" + (b.kind === "person" ? " process" : ""),
                b.kind === "thread" ? (b.group ? "group chat" : "thread") : b.kind));
  for (const s of b.streams) sum.append(el("span", "pill archive", s));
  // The two things a raw log will not tell you, and the user had to guess at both.
  if (b.monologue) {
    const p = el("span", "pill");
    p.style.cssText = "border-color:var(--warn);color:var(--warn)";
    p.textContent = "your words only";
    sum.append(p);
  }
  if (b.missing_pages.length) sum.append(el("span", "pill", "no wiki page"));
  if (b.merged.length) {
    const m = el("span", "pill process");
    m.textContent = `${b.merged.length + 1} chat ids merged`;
    m.title = "iMessage split this conversation across services. Folded back into one:\n"
      + [b.entity, ...b.merged].join("\n");
    sum.append(m);
  }
  sum.append(el("span", "bmeta", `${b.count} lines · ${nf(b.tokens)} tok · ${b.span}`
    + (b.waiting ? ` · +${nf(b.waiting)} older waiting` : "")));
  d.append(sum);

  if (b.waiting) {
    const n = el("div", "note");
    n.style.margin = "10px 14px 0";
    n.textContent = `This conversation has ${nf(b.waiting)} more line(s) queued than one pass `
      + `reads. The newest ${b.count} are here; the rest stay queued and are read next time, `
      + `so a loud thread cannot crowd out a quiet one.`;
    d.append(n);
  }
  if (b.merged.length) {
    const n = el("div", "note");
    n.style.margin = "10px 14px 0";
    n.textContent = "iMessage had this conversation filed under "
      + `${b.merged.length + 1} different chat ids — usually because somebody's phone `
      + "dropped to SMS while everyone else stayed on iMessage. They are merged here, so "
      + "the model reads one conversation instead of two halves of one.";
    d.append(n);
  }

  if (b.monologue) {
    const n = el("div", "note");
    n.style.cssText = "margin:10px 14px 0;color:var(--warn)";
    n.textContent = "Every line here is yours. Only your side of a Hermes conversation is "
      + "archived — the assistant's replies are deliberately never stored as memory, so this "
      + "bundle reads as a monologue with the answers missing. The model sees exactly this.";
    d.append(n);
  }
  if (b.missing_pages.length) {
    const n = el("div", "note");
    n.style.margin = "10px 14px 0";
    n.textContent = "No wiki page yet for " + b.missing_pages.join(", ")
      + " — the model is told it may open one if this bundle states a durable fact.";
    d.append(n);
  }

  // Where the lines came from. A person bundle joining four conversations is correct
  // and deliberate — but it has to be visible, or a group chat reads as a private one.
  if (b.conversations.length > 1 || b.conversations.some(c => c.group)) {
    const n = el("div", "convos");
    n.append(el("span", "clabel", b.conversations.length > 1
      ? `${b.conversations.length} conversations in this bundle:` : "from:"));
    for (const c of b.conversations) {
      const chip = el("span", "convo" + (c.group ? " grp" : ""));
      chip.textContent = `${c.group ? "group" : "1:1"} · ${c.stream}/${c.thread || "—"} · ${c.n}`;
      n.append(chip);
    }
    d.append(n);
  }

  const msgs = el("div", "msgs");
  for (const m of b.items) {
    const row = el("div", "msg" + (m.mine ? " mine" : "") + (m.context ? " ctx" : ""));
    const who = el("div", "w");
    who.textContent = m.who;
    if (m.group) { who.append(el("span", "gtag", " in " + (m.thread || "group"))); }
    row.append(el("div", "t", m.at), who, el("div", "b", m.text));
    msgs.append(row);
  }
  d.append(msgs);

  const raw = el("details", "raw");
  raw.append(el("summary", null, "exact text sent for this bundle"));
  const pre = el("pre"); pre.textContent = b.text;
  raw.append(pre);
  d.append(raw);
  return d;
}
async function resumeJobs() {
  const collect = await api("/api/job?kind=gather");
  if (collect.job && !collect.done) {
    watchJob(collect.job, $("#collect"), $("#collectlog"),
      async () => { await loadDream(); });
  }
  const dream = await api("/api/job?kind=dream");
  if (dream.job && !dream.done) {
    watchJob(dream.job, $("#dream"), $("#dreamlog"),
      async s => { renderOutput(s.result || {}); await loadDream(); });
  }
}
// Not "/api/collect": that path is on uBlock's and EasyPrivacy's lists as an analytics
// beacon, so the browser cancelled it before it left the page.
$("#collect").onclick = () => runJob("/api/gather", $("#collect"), $("#collectlog"),
  async () => { await loadDream(); });

$("#dream").onclick = () => runJob("/api/dream", $("#dream"), $("#dreamlog"),
  async s => { renderOutput(s.result || {}); await loadDream(); });

function renderOutput(r) {
  const card = $("#doutput"); card.innerHTML = "";
  if (r.nothing_new) {
    card.innerHTML = '<div class="empty">nothing new since the last pass</div>';
    return;
  }
  card.append(el("div", "note",
    `run #${r.run_id} — ${nf(r.bundles)} bundles, ${nf(r.items)} items, `
    + `${nf(r.diffs)} write(s)${r.usage ? " · " + r.usage : ""}`));
  const section = (title, lines) => {
    if (!lines || !lines.length) return;
    card.append(el("div", "bname", title));
    const ul = el("ul");
    for (const l of lines) ul.append(el("li", null, l));
    card.append(ul);
  };
  if (r.writes && r.writes.length) {
    card.append(el("div", "bname", "Wrote — click anything to inspect its source"));
    const ul = el("ul");
    for (const w of r.writes) {
      const li = el("li");
      const b = el("button", "whybtn", `${w.verb || "wrote"} · ${w.label || w.ref}`);
      b.onclick = () => openWhy(w.kind, w.ref, w.label || w.ref);
      li.append(b); ul.append(li);
    }
    card.append(ul);
  }
  section("Pass log", r.wrote);
  section("Woke", r.woken);
  section("Now asking", r.questions);
  section("Sweep", r.sweep);
  section("Errors", r.errors);
  // Zero writes off a real pass is the signature of the truncation above, not of a
  // quiet week — it is worth naming here rather than reading as success.
  if (!r.diffs && r.bundles) {
    const n = el("div", "note");
    n.style.color = "var(--warn)";
    n.textContent = `${r.bundles} bundles read and nothing written. Check the Runs tab for `
      + `output tokens: a completion that stopped on its ceiling was truncated, and a `
      + `truncated diff fails to parse silently.`;
    card.append(n);
  }
}
