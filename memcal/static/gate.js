import { $, el, nf, api, toast, state, show, loadOverview } from "./core.js";

/* ---------------------------------------------------------------- gate -- */
export async function loadGate() {
  $("#days").disabled = !!state.queue;
  queueNote();
  await Promise.all([loadOverview(), loadFeed(true)]);
}
function feedQuery() {
  const p = new URLSearchParams({limit: "100", offset: String(state.offset)});
  for (const k of ["verdict", "stream", "reason", "q", "queue"]) if (state[k]) p.set(k, state[k]);
  // A day window and the queue are two different questions, and asking both at once is
  // how "waiting for the next dream" ends up hiding the month-old mail that is also
  // waiting. The queue filter owns the window when it is on.
  if (state.days !== "0" && !state.queue) p.set("days", state.days);
  return p;
}

async function loadFeed(reset) {
  const grouped = state.shape === "grouped";
  $("#rollup").hidden = !grouped;
  $("#feed").hidden = grouped;
  if (grouped) { $("#more").hidden = true; return loadRollup(); }

  if (reset) { state.offset = 0; $("#feed").innerHTML = '<div class="empty">loading…</div>'; }
  const data = await api("/api/items?" + feedQuery());
  const feed = $("#feed");
  if (reset) feed.innerHTML = "";
  if (!data.items.length && reset) feed.innerHTML = '<div class="empty">nothing matches</div>';

  $("#count").textContent = `${nf(data.total)} items`;
  if (reset) renderChips(data.reasons);
  for (const it of data.items) feed.append(itemRow(it));
  state.offset += data.items.length;
  $("#more").hidden = state.offset >= data.total;
  $("#more").textContent = `load more — ${nf(data.total - state.offset)} left`;
}

/* Five thousand lines in one column is the raw material for a view, not a view. Rolled
   up by conversation you can see at a glance that 381 of them are the dog park. */
async function loadRollup() {
  const box = $("#rollup");
  box.innerHTML = '<div class="empty">loading…</div>';
  const p = new URLSearchParams({limit: "400"});
  for (const k of ["verdict", "stream", "reason", "q", "queue"]) if (state[k]) p.set(k, state[k]);
  if (state.days !== "0" && !state.queue) p.set("days", state.days);
  const [rolled, feed] = await Promise.all([
    api("/api/groups?" + p), api("/api/items?" + feedQuery()),
  ]);
  renderChips(feed.reasons || []);
  const rows = rolled.groups || [];
  const shown = rows.reduce((a, g) => a + g.n, 0);
  $("#count").textContent = `${nf(feed.total)} items in ${nf(rows.length)} conversations`
    + (shown < feed.total ? ` · showing the top ${nf(shown)}` : "");
  box.innerHTML = "";
  if (!rows.length) { box.innerHTML = '<div class="empty">nothing matches</div>'; return; }
  const max = Math.max(...rows.map(g => g.n));
  for (const g of rows) box.append(rollupRow(g, max));
}

function rollupRow(g, max) {
  const d = el("details", "grp");
  const sum = el("summary");
  const bar = el("span", "gbar");
  const pass = el("i", "p"); pass.style.width = (100 * g.gated / max) + "%";
  const structured = el("i", "p"); structured.style.width = (100 * g.structured / max) + "%";
  structured.style.background = "var(--good)";
  const skip = el("i", "s");
  skip.style.width = (100 * (g.n - g.gated - g.structured) / max) + "%";
  bar.append(pass, structured, skip);
  sum.append(bar);
  sum.append(el("span", "gname", g.title || g.key));
  sum.append(el("span", "pill archive", g.stream));
  if (g.muted) {
    const m = el("span", "pill"); m.style.cssText = "border-color:var(--warn);color:var(--warn)";
    m.textContent = "muted"; sum.append(m);
  }
  sum.append(el("span", "gnums",
    `${nf(g.n)} lines · ${nf(g.gated)} picked up · ${nf(g.structured)} structured · ${nf(g.queued)} queued `
    + `· ~${nf(g.tokens)} tok · ${g.span}`));
  d.append(sum);
  // The lines themselves, fetched only when the row is opened.
  let loaded = false;
  d.ontoggle = async () => {
    if (!d.open || loaded) return;
    loaded = true;
    const inner = el("div", "feed");
    d.append(inner);
    // Ask for the conversation by key, not as a text search: a group chat's key is an
    // identifier no message contains, so searching for it finds none of its own lines.
    const p = new URLSearchParams({limit: "60", group: g.key, stream: g.stream});
    for (const k of ["verdict", "reason", "q"]) if (state[k]) p.set(k, state[k]);
    if (state.days !== "0") p.set("days", state.days);
    const data = await api("/api/items?" + p);
    for (const it of data.items) inner.append(itemRow(it));
    if (!data.items.length) inner.innerHTML = '<div class="empty">no matching lines</div>';
  };
  return d;
}
const STATE_WORD = {read: "read", queued: "queued", skipped: "skipped",
                    dropped: "not queued", retired: "retired", live: "written live",
                    structured: "structured"};
const STATE_WHY = {
  read: r => `the gate passed this (${r}) and a pass has read it`,
  queued: r => `the gate passed this (${r}) — waiting for the next pass`,
  skipped: r => `the gate skipped this (${r}) — archived and searchable, never read`,
  dropped: r => `the gate passed this (${r}) but it fell outside the spool horizon, so no pass ever read it`,
  retired: r => `the gate passed this (${r}), then it was pulled out of the queue — by you, or by the horizon sweep — and never read`,
  live: () => "written immediately by the live path — it never goes through the queue",
  structured: () => "written directly from structured calendar fields — no model call needed",
};
function itemRow(it) {
  const row = el("div", "item");
  row.dataset.id = it.id;
  row.append(el("div", "when", it.ts.slice(5, 16).replace("T", "  ")));
  row.append(el("div", "who", it.who));

  const st = el("div", "state " + it.state);
  st.append(el("i", "dot"));
  st.append(el("span", null, STATE_WORD[it.state]));
  st.append(el("span", "why", it.reason));
  st.title = (STATE_WHY[it.state] || (r => `gate: ${r}`))(it.reason);
  row.append(st);

  const txt = el("div", "txt");
  if (it.subject) { txt.append(el("b", null, it.subject)); if (it.preview) txt.append(el("span", null, "  " + it.preview)); }
  else txt.append(el("span", null, it.preview));
  row.append(txt);

  const act = el("div", "act");
  if (it.state === "skipped" || it.state === "dropped" || it.state === "retired") {
    const b = el("button", null, "queue it");
    b.onclick = e => { e.stopPropagation(); queue(it.id, "queue"); };
    act.append(b);
  }
  if (it.state === "queued") {
    const b = el("button", null, "don't read it");
    b.onclick = e => { e.stopPropagation(); queue(it.id, "skip"); };
    act.append(b);
  }
  if (it.address) {
    const b = el("button", null, "sender…");
    b.onclick = e => { e.stopPropagation(); $("#sq").value = it.address; show("senders"); };
    act.append(b);
  }
  // The counterweight to a gate that now reads subject lines: it errs towards letting
  // things through, so saying no has to be one click and has to stick.
  if (it.state !== "structured" && (it.address || (it.stream && it.thread))) {
    const b = el("button", null, "don't care");
    b.title = it.address
      ? `Never spend a model call on ${it.address} again.`
      : `Never spend a model call on ${it.thread} again.`;
    b.onclick = e => { e.stopPropagation(); block(it); };
    act.append(b);
  }
  row.append(act);
  row.onclick = () => expand(row, it);
  return row;
}

async function expand(row, it) {
  const next = row.nextElementSibling;
  if (next && next.classList.contains("detail")) { next.remove(); row.classList.remove("open"); return; }
  row.classList.add("open");
  const full = await api("/api/item?id=" + it.id);
  const box = el("div", "detail");
  box.append(Object.assign(el("pre"), {textContent: full.text || "(empty)"}));
  const bits = [`${full.stream} · ${full.ts}`,
                full.state === "structured" ? "path: structured direct write" : `gate: ${full.reason}`,
                full.entity ? `bundle: ${full.entity}` : "not bundled",
                full.address || ""].filter(Boolean);
  box.append(el("div", "meta", bits.join("   ·   ")));
  row.after(box);
}

async function queue(id, action) {
  const out = await api("/api/queue", {id, action});
  if (!out.error) { toast(action === "queue" ? "queued for the next pass" : "retired from the queue");
                    loadFeed(true); }
}

/* "I don't care about this." Permanent by design — it records that a person said so,
   which is what stops the subject test from reopening it on the next well-worded
   reminder. The same endpoint the CLI and the agent use. */
async function block(it) {
  const what = it.address || `${it.stream}/${it.thread}`;
  if (!confirm(`Never spend a model call on ${what} again?\n\n`
             + `Nothing is deleted — it stays in the archive and stays searchable.`)) return;
  const body = it.address ? {address: it.address} : {stream: it.stream, thread: it.thread};
  const out = await api("/api/block", {...body, by: "you"});
  if (out.error) return toast(out.error);
  toast(`blocked ${out.blocked}`
        + (out.retired ? ` — ${out.retired} queued item(s) retired` : ""));
  loadFeed(true);
}

function renderChips(reasons) {
  const box = $("#chips"); box.innerHTML = "";
  const all = el("button", "chip" + (state.reason ? "" : " on"), "");
  all.setAttribute("aria-pressed", String(!state.reason));
  all.append(document.createTextNode("every reason"));
  all.onclick = () => { state.reason = ""; loadFeed(true); };
  box.append(all);
  for (const r of reasons) {
    const c = el("button", "chip" + (r.passed ? " passed" : r.structured ? " structured" : ""));
    c.setAttribute("aria-pressed", String(state.reason === r.reason));
    c.append(el("i", "dot"), document.createTextNode(r.reason), el("span", "n", nf(r.n)));
    c.title = r.passed ? "these were picked up"
      : r.structured ? "these were written directly from structured data"
      : "these were skipped";
    c.onclick = () => { state.reason = state.reason === r.reason ? "" : r.reason; loadFeed(true); };
    box.append(c);
  }
}

/* The gate tab opens on what is *about to be read*, not on the whole archive. Those are
   different by three orders of magnitude here — 5,500 waiting against 200,000 collected
   — and the second one is a research question while the first is the thing the user came to
   look at. History stays one click away, which is the whole point of never deleting. */
$("#queue").onclick = e => {
  const b = e.target.closest("button"); if (!b) return;
  state.queue = b.dataset.q;
  state.offset = 0;
  $("#queue").querySelectorAll("button").forEach(x =>
    x.setAttribute("aria-pressed", String(x === b)));
  $("#days").disabled = !!state.queue;
  queueNote();
  loadFeed(true);
};

function queueNote() {
  $("#queuenote").textContent = {
    queued: "Everything the next dream pass will look at — passed the gate, in the spool, "
          + "no pass has taken it yet. The day window does not apply here: a three-week-old "
          + "line that is still queued is still going to be read.",
    read: "Lines a pass actually consumed. Open a run to see which bundle each went into.",
    "": "Everything ever collected, gated or not. Nothing is ever deleted, so this is the "
      + "long record — most of it is archived, searchable, and will never cost a call.",
  }[state.queue] || "";
}

$("#verdict").onclick = e => {
  const b = e.target.closest("button"); if (!b) return;
  state.verdict = b.dataset.v;
  $("#verdict").querySelectorAll("button").forEach(x =>
    x.setAttribute("aria-pressed", String(x === b)));
  loadFeed(true);
};
$("#stream").onchange = e => { state.stream = e.target.value; loadFeed(true); };
$("#days").onchange = e => { state.days = e.target.value; loadGate(); };
let qTimer;
$("#q").oninput = e => { clearTimeout(qTimer); qTimer = setTimeout(() => {
  state.q = e.target.value.trim(); loadFeed(true); }, 220); };
$("#more").onclick = () => loadFeed(false);
$("#shape").onclick = e => {
  const b = e.target.closest("button"); if (!b) return;
  state.shape = b.dataset.s;
  $("#shape").querySelectorAll("button").forEach(x =>
    x.setAttribute("aria-pressed", String(x === b)));
  loadFeed(true);
};
document.addEventListener("keydown", e => {
  if (e.key === "/" && e.target.tagName !== "INPUT") { e.preventDefault(); $("#q").focus(); }
});
