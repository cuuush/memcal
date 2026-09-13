import { $, el, nf, api, toast, state } from "./core.js";

/* ---------------------------------------------------------------- chats -- */
export async function loadChats() {
  const box = $("#chats");
  box.innerHTML = '<div class="empty">reading the archive…</div>';
  const p = new URLSearchParams();
  if (state.cstream) p.set("stream", state.cstream);
  if (state.cq) p.set("q", state.cq);
  const data = await api("/api/chats?" + p);
  if (data.error) { box.innerHTML = '<div class="empty">could not load</div>'; return; }
  renderReview(data);
  const rows = data.threads || [];
  $("#ccount").textContent = `${nf(rows.length)} conversations`;
  const sel = $("#cstream"), had = sel.value;
  sel.innerHTML = '<option value="">every stream</option>';
  for (const s of [...new Set(rows.map(t => t.stream))].sort())
    sel.append(Object.assign(el("option", null, s), {value: s}));
  sel.value = had;
  box.innerHTML = "";
  if (!rows.length) { box.innerHTML = '<div class="empty">no conversations yet</div>'; return; }
  const max = Math.max(...rows.map(t => t.n));
  for (const t of rows) box.append(chatRow(t, max));
}

/* The ask-me queue. These are the ones nothing in the traffic can decide. */
function renderReview(data) {
  const box = $("#creview"); box.innerHTML = "";
  renderPlatformMute(data);
  const rows = data.review || [];
  if (!rows.length) return;
  const n = el("div", "banner");
  n.append(el("b", null, `${rows.length} group chat${rows.length > 1 ? "s" : ""} worth a decision`));
  n.append(el("p", null,
    `You have never posted in these and you know nobody in them — no one in the chat turns `
    + `up anywhere else you do speak. That describes a dev chat you cared about eight years `
    + `ago and it equally describes your dog park, so memcal will not guess. Reading one `
    + `costs model calls on every pass; muting keeps it archived and searchable.`));
  for (const t of rows) n.append(chatRow(t, Math.max(...rows.map(r => r.n)), true));
  box.append(n);
}

/* What the platform's own mute is worth here, with the measurement that decided it. */
function renderPlatformMute(data) {
  const n = data.platform_muted_count || 0;
  if (!n) return;
  const kept = data.platform_muted_with_mutuals || 0;
  const words = {
    show: "recorded and shown here, deciding nothing",
    ask: "enough to raise a chat for review, even one you know people in",
    mute: "taken as your answer — these are never read",
  }[data.platform_mute] || data.platform_mute;
  const box = $("#creview");
  const p = el("div", "note");
  p.style.margin = "0 0 12px";
  p.textContent = `${n} of these are muted in the app itself, and ${kept} of those `
    + `are full of people you talk to elsewhere — so muting there looks like "stop `
    + `buzzing my phone", not "I don't care". It is ${words}. `
    + `Change it with platform_mute = show | ask | mute in ~/.memcal/config.`;
  box.append(p);
}

function chatRow(t, max, urgent) {
  const d = el("details", "grp" + (t.decision === "mute" ? " muted" : ""));
  const sum = el("summary");
  const bar = el("span", "gbar");
  const mine = el("i", "p"); mine.style.width = (100 * t.mine / max) + "%";
  const theirs = el("i", "s"); theirs.style.width = (100 * (t.n - t.mine) / max) + "%";
  bar.append(mine, theirs);
  sum.append(bar, el("span", "gname", t.title || t.thread));
  sum.append(el("span", "pill archive", t.stream));
  if (t.group) sum.append(el("span", "pill", `${t.members || "?"} people`));
  if (t.collision) {
    const c = el("span", "pill");
    c.style.cssText = "border-color:var(--warn);color:var(--warn)";
    c.textContent = "same name as another chat";
    c.title = "Two conversations share this name. The people in them tell them apart.";
    sum.append(c);
  }
  // memcal's decision and the platform's mute are both called "mute" and mean different
  // things — one stops model calls, the other stops a phone buzzing. Say which is which.
  if (t.platform_muted) {
    const m = el("span", "pill");
    m.textContent = t.platform_note || "muted on the platform";
    m.title = "You silenced this chat in the app. That is not a decision here — most of "
      + "your muted chats are full of people you talk to daily.";
    sum.append(m);
  }
  if (t.decision) sum.append(el("span", "pill" + (t.decision === "read" ? " process" : ""),
                               t.decision === "mute" ? "never read" : "read every pass"));
  sum.append(el("span", "gnums",
    `${nf(t.n)} lines · ${t.share}% you · ${t.known} known · ${t.mutuals} mutual`
    + (t.queued ? ` · ${nf(t.queued)} queued` : "") + ` · last ${t.last}`));
  d.append(sum);

  const body = el("div", "gbody");
  if (t.speakers.length) {
    body.append(el("div", "note",
      "In it: " + t.speakers.join(", ")
      + (t.more_speakers ? ` and ${t.more_speakers} more` : "")));
  }
  body.append(el("div", "note", t.mine
    ? `You have written ${nf(t.mine)} of these ${nf(t.n)} lines.`
    : "You have never written in this chat."));
  if (!t.mutuals && t.group) {
    body.append(el("div", "note",
      "Nobody here turns up in any conversation you do speak in."));
  }
  if (t.platform_muted) {
    body.append(el("div", "note",
      "You have this muted in the app. memcal does not read anything into that — most of "
      + "your muted chats are ones you are clearly part of."));
  }
  const act = el("div", "gact");
  const keep = el("button", "btn", t.decision === "read" ? "kept" : "Keep reading it");
  keep.disabled = t.decision === "read";
  keep.onclick = () => decideChat(t, "read");
  const mute = el("button", "btn", t.decision === "mute" ? "not read" : "Never read it");
  mute.disabled = t.decision === "mute";
  mute.onclick = () => decideChat(t, "mute");
  act.append(keep, mute);
  body.append(act);
  d.append(body);
  if (urgent) d.open = false;
  return d;
}

async function decideChat(t, decision) {
  const out = await api("/api/chat", {stream: t.stream, thread: t.thread, decision});
  if (out.error) return;
  toast(decision === "mute"
    ? `muted — ${nf(out.retired || 0)} queued line(s) dropped, all still in the archive`
    : "kept — it will be read on every pass");
  await loadChats();
}
$("#cstream").onchange = e => { state.cstream = e.target.value; loadChats(); };
let cTimer;
$("#cq").oninput = e => { clearTimeout(cTimer); cTimer = setTimeout(() => {
  state.cq = e.target.value.trim(); loadChats(); }, 220); };
