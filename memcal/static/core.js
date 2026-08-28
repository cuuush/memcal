export const $ = s => document.querySelector(s);
export const el = (t, c, x) => { const n = document.createElement(t); if (c) n.className = c;
                          if (x !== undefined) n.textContent = x; return n; };
export const esc = s => (s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
export const nf = n => (n ?? 0).toLocaleString();
export async function api(path, body) {
  const opt = body ? {method: "POST", headers: {"Content-Type": "application/json"},
                      body: JSON.stringify(body)} : {};
  let res;
  try {
    res = await fetch(path, opt);
  } catch (e) {
    // A blocked request and a dead server are the same TypeError here, and the console
    // says ERR_BLOCKED_BY_CLIENT while the page just sits there. `/api/collect` matched
    // uBlock's analytics list, so the button did nothing and nothing said why.
    const msg = `could not reach ${path} — if the console says ERR_BLOCKED_BY_CLIENT, `
      + `an ad blocker is eating the request; allowlist 127.0.0.1`;
    toast(msg);
    return {error: msg};
  }
  const data = await res.json();
  if (data.error) toast(data.error);
  return data;
}
let toastTimer;
$("#traceclose").onclick = () => { $("#tracepanel").hidden = true; };
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && !$("#tracepanel").hidden) $("#tracepanel").hidden = true;
});
export function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove("show"), 3200);
}
/* -------------------------------------------------------------- routing -- */
export const state = {view: "gate", verdict: "", stream: "", days: "14", q: "", reason: "",
               offset: 0, shape: "grouped", cstream: "", cq: "",
               // Wiki tab: the search box and which page is open on the right.
               wq: "", wikiSlug: "",
               // which run is open on the Runs tab, and a bundle id the Dream tab
               // should scroll to and flash as soon as it has finished rendering
               run: 0, bundleFlash: "", callFlash: "", callNeedle: "",
               // "" = everything ever collected. The gate tab opens on the queue,
               // because "what is the next pass going to read" is the live question.
               queue: "queued"};

export const VIEWS = ["gate", "chats", "dream", "senders", "memory", "wiki", "runs"];
document.querySelectorAll("nav button").forEach(b =>
  b.onclick = () => { location.hash = b.dataset.view; });
addEventListener("hashchange", () => show(location.hash.slice(1)));

// Populated once, by app.js, with each tab's load function — so this module
// never has to import every view (which would cycle back through the view
// modules that import state/api/etc. from here).
let views = {};
export function registerViews(table) { views = table; }

export function show(view) {
  if (!VIEWS.includes(view)) view = "gate";
  state.view = view;
  if (location.hash.slice(1) !== view) location.hash = view;
  document.querySelectorAll("nav button").forEach(b =>
    b.setAttribute("aria-current", String(b.dataset.view === view)));
  document.querySelectorAll(".view").forEach(v => v.hidden = v.id !== "view-" + view);
  views[view]();
  if (view !== "gate") loadOverview();   // keeps the header line honest on every tab
}
$("#theme").onclick = () => {
  const dark = document.documentElement.dataset.theme === "dark"
    || (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "light" : "dark";
};
$("#reload").onclick = () => show(state.view);
export async function loadOverview() {
  const o = await api("/api/overview?days=" + (state.days === "0" ? 365 : state.days));
  $("#sub").textContent = `${o.propose_model.split("/").pop()} · brief ~${o.brief_tokens}/${o.brief_cap} tok`
    + ` · ${nf(o.pending)} waiting`;
  if (state.view !== "gate") return;   // the tiles and bars only exist on the gate view

  const tiles = [
    ["Waiting to be read", nf(o.pending), `spool, ${o.horizon}-day horizon`, o.pending > 400],
    ["memcal rows", nf(o.events), `${o.todos} open to-dos · ${o.questions} questions`, false],
    ["Wiki pages", nf(o.pages), `${o.unresolved} unresolved handles`, false],
    ["Shared prefix", "~" + nf(o.prefix_tokens), "cached across every call in a run", false],
    ["Spent", "$" + o.spend.toFixed(2), `last ${o.days} days`, false],
  ];
  if (o.last_run) tiles.push(["Last pass", "#" + o.last_run.id,
    `${o.last_run.at} · ${o.last_run.bundles} bundles → ${o.last_run.diffs} writes`,
    !!o.last_run.error]);

  const box = $("#tiles"); box.innerHTML = "";
  for (const [k, v, s, alert] of tiles) {
    const t = el("div", "tile" + (alert ? " alert" : ""));
    t.append(el("div", "k", k), el("div", "v", v), el("div", "s", s));
    box.append(t);
  }

  const sel = $("#stream"), had = sel.value;
  sel.innerHTML = '<option value="">every stream</option>';
  const rows = $("#streams"); rows.innerHTML = "";
  const max = Math.max(1, ...o.streams.map(s => s.n));
  for (const s of o.streams) {
    sel.append(new Option(s.stream, s.stream));
    const row = el("div", "stream-row");
    const name = el("div", "name"); name.append(el("span", null, s.stream));
    const age = el("em", s.stale ? "stale" : null,
                   s.stale ? `stale — ${s.stale} old` : `last seen ${s.last_seen}`);
    name.append(age);

    const track = el("div", "track");
    track.style.width = (100 * s.n / max) + "%";
    const pass = el("i", "pass"), structured = el("i", "structured"), skip = el("i", "skip");
    const skipped = s.n - s.gated - s.structured;
    pass.style.width = (100 * s.gated / s.n) + "%";
    structured.style.width = (100 * s.structured / s.n) + "%";
    skip.style.width = (100 * skipped / s.n) + "%";
    pass.title = `${nf(s.gated)} picked up`;
    structured.title = `${nf(s.structured)} structured`;
    skip.title = `${nf(skipped)} skipped`;
    track.append(pass, structured, skip);

    const num = el("div", "num",
      `${nf(s.gated)} picked up · ${nf(s.structured)} structured · ${nf(skipped)} skipped · ~${nf(s.tokens)} tok raw`);
    row.append(name, track, num);
    rows.append(row);
  }
  sel.value = had;
}
/* A bundle id is the same six characters on every tab, so "which bundle was that?"
   is answerable by going and looking at it rather than by reading an entity string. */
export function jumpToBundle(bid) {
  state.bundleFlash = bid;
  location.hash = "dream";
}
