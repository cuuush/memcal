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
  let data;
  try { data = await res.json(); }
  catch { const error = "The local server returned an unreadable response. Refresh to try again.";
    toast(error); return {error}; }
  if (data.error) toast(data.error);
  return data;
}
let toastTimer;
const tracePanel = $("#tracepanel");
let traceOpener;
let traceOpen = false;
function closeTrace() { tracePanel.hidden = true; }
$("#traceclose").onclick = closeTrace;
$("#tracebackdrop").onclick = closeTrace;
new MutationObserver(() => {
  if (traceOpen === !tracePanel.hidden) return;
  traceOpen = !tracePanel.hidden;
  $("#tracebackdrop").hidden = !traceOpen;
  if (traceOpen) {
    traceOpener = document.activeElement;
    $(".workspace").inert = true;
    $(".sidebar").inert = true;
    $("#traceclose").focus();
  } else {
    $(".workspace").inert = false;
    $(".sidebar").inert = false;
    if (traceOpener?.isConnected) traceOpener.focus();
  }
}).observe(tracePanel, {attributes: true, attributeFilter: ["hidden"]});
document.addEventListener("keydown", e => {
  if (tracePanel.hidden) return;
  if (e.key === "Escape") { e.preventDefault(); closeTrace(); }
  if (e.key !== "Tab") return;
  const nodes = [...tracePanel.querySelectorAll("button, a[href], input, select, textarea, [tabindex='0']")]
    .filter(node => !node.disabled && node.getClientRects().length);
  const first = nodes[0], last = nodes[nodes.length - 1];
  if (!first) { e.preventDefault(); tracePanel.focus(); return; }
  if (e.shiftKey && (document.activeElement === first || document.activeElement === tracePanel)) {
    e.preventDefault(); last.focus();
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault(); first.focus();
  }
});
export function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove("show"), 3200);
}
/* -------------------------------------------------------------- routing -- */
export const state = {view: "memory", verdict: "", channel: "", days: "14", q: "", reason: "",
               offset: 0, shape: "grouped", cstream: "", cq: "",
               // Wiki tab: the search box and which page is open on the right.
               wq: "", wikiSlug: "",
               // which run is open on the History page, and a bundle id the Dream tab
               // should scroll to and flash as soon as it has finished rendering
               // Run selected for retry on the Dream tab.
               run: 0, retryRun: 0, bundleFlash: "", callFlash: "", callNeedle: "",
               // "" = everything ever collected. The gate tab opens on the queue,
               // because "what is the next pass going to read" is the live question.
               queue: "queued"};

export const VIEWS = ["gate", "chats", "dream", "senders", "memory", "wiki", "runs",
                      "settings"];
document.querySelectorAll("nav button").forEach(b =>
  b.onclick = () => { location.hash = b.dataset.view; });
addEventListener("hashchange", () => {
  if (location.hash === "#workspace") { $("#workspace").focus(); return; }
  show(location.hash.slice(1));
});

// Populated once, by app.js, with each tab's load function — so this module
// never has to import every view (which would cycle back through the view
// modules that import state/api/etc. from here).
let views = {};
export function registerViews(table) { views = table; }

export function show(view) {
  if (!VIEWS.includes(view)) view = "memory";
  state.view = view;
  if (location.hash.slice(1) !== view) history.replaceState(null, "", "#" + view);
  document.querySelectorAll("nav button").forEach(b =>
    b.dataset.view === view ? b.setAttribute("aria-current", "page") : b.removeAttribute("aria-current"));
  document.querySelectorAll(".view").forEach(v => v.hidden = v.id !== "view-" + view);
  const title = PAGE_TITLES[view];
  $("#page-title").textContent = title;
  document.title = `${title} · Memcal`;
  $("#page-action").hidden = view !== "gate" && view !== "memory";
  $("#page-error").hidden = true;
  if (view === "dream" && state.bundleFlash) $("#dream-preview").open = true;
  Promise.resolve(views[view]()).catch(() => {
    if (state.view !== view) return;
    $("#page-error").textContent = "This page could not load. Check the local server, then refresh to try again.";
    $("#page-error").hidden = false;
  });
}
const PAGE_TITLES = {memory: "Memory", wiki: "Wiki", chats: "Conversations",
  gate: "Inbox", dream: "Dream", runs: "History", senders: "Email rules", settings: "Settings"};
const pageError = el("div", "page-error");
pageError.id = "page-error"; pageError.hidden = true; pageError.setAttribute("role", "alert");
$(".page-heading").after(pageError);
try {
  const theme = localStorage.getItem("memcal-theme");
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
} catch { /* Storage may be disabled by the browser. */ }
$("#theme").onclick = () => {
  const dark = document.documentElement.dataset.theme === "dark"
    || (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem("memcal-theme", document.documentElement.dataset.theme); } catch {}
};
$("#reload").onclick = () => show(state.view);
export async function loadOverview() {
  const o = await api("/api/overview?days=" + (state.days === "0" ? 365 : state.days));
  if (o.error) { $("#page-error").textContent = o.error; $("#page-error").hidden = false; return; }
  if (state.view !== "gate") return;   // the tiles and bars only exist on the gate view

  const tiles = [
    ["Waiting to be read", nf(o.pending), `${o.horizon}-day processing window`, o.pending > 400],
    ["Events", nf(o.events), `${o.todos} open to-dos · ${o.questions} questions`, false],
    ["Wiki pages", nf(o.pages), `${o.unresolved} unresolved handles`, false],
    ["Model spend", "$" + o.spend.toFixed(2), `last ${o.days} days`, false],
  ];
  if (o.last_run) tiles.push(["Last dream", "#" + o.last_run.id,
    `${o.last_run.at} · ${o.last_run.bundles} bundles → ${o.last_run.diffs} writes`,
    !!o.last_run.error]);

  const box = $("#tiles"); box.innerHTML = "";
  for (const [k, v, s, alert] of tiles) {
    const t = el("div", "tile" + (alert ? " alert" : ""));
    t.append(el("div", "k", k), el("div", "v", v), el("div", "s", s));
    box.append(t);
  }

  const sel = $("#channel"), had = sel.value;
  sel.innerHTML = '<option value="">All sources</option>';
  const rows = $("#streams"); rows.innerHTML = "";
  const max = Math.max(1, ...o.streams.map(s => s.n));
  for (const s of o.streams) {
    sel.append(new Option(s.channel, s.channel));
    const row = el("div", "channel-row");
    const name = el("div", "name"); name.append(el("span", null, s.channel));
    const age = el("em", s.stale ? "stale" : null,
                   s.stale ? `stale — ${s.stale} old` : `last seen ${s.last_seen}`);
    name.append(age);

    const track = el("div", "track");
    track.style.width = (100 * s.n / max) + "%";
    const pass = el("i", "pass"), structured = el("i", "structured"), skip = el("i", "skip");
    const skipped = s.n - s.gated - s.structured;
    pass.style.width = (100 * s.gated / (s.n || 1)) + "%";
    structured.style.width = (100 * s.structured / (s.n || 1)) + "%";
    skip.style.width = (100 * skipped / (s.n || 1)) + "%";
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
