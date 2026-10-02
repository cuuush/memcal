import { $, el, nf, api, toast, state } from "./core.js";

/* ---------------------------------------------------------------- chats -- */
export async function loadChats() {
  const box = $("#chats");
  box.innerHTML = '<div class="empty">reading the archive…</div>';
  const p = new URLSearchParams();
  if (state.cstream) p.set("channel", state.cstream);
  if (state.cq) p.set("q", state.cq);
  const data = await api("/api/chats?" + p);
  if (data.error) { box.innerHTML = '<div class="empty">could not load</div>'; return; }
  renderReview(data);
  const rows = data.threads || [];
  $("#ccount").textContent = `${nf(rows.length)} conversations`;
  const sel = $("#cstream"), had = sel.value;
  sel.innerHTML = '<option value="">All sources</option>';
  for (const s of [...new Set(rows.map(t => t.channel))].sort())
    sel.append(Object.assign(el("option", null, s), {value: s}));
  sel.value = had;
  box.innerHTML = "";
  if (!rows.length) { box.innerHTML = '<div class="empty">no conversations yet</div>'; return; }
  for (const t of rows) box.append(chatRow(t));
}

/* The ask-me queue. These are the ones nothing in the traffic can decide. */
function renderReview(data) {
  const box = $("#creview"); box.innerHTML = "";
  renderPlatformMute(data);
  const rows = data.review || [];
  if (!rows.length) return;
  const n = el("div", "banner");
  n.append(el("b", null, `${rows.length} group chat${rows.length > 1 ? "s" : ""} worth a decision`));
  n.append(el("p", null, "Choose which unfamiliar group conversations Dream should read. Muted conversations remain searchable."));
  for (const t of rows) n.append(chatRow(t, Math.max(...rows.map(r => r.n)), true));
  box.append(n);
}

/* What the platform's own mute is worth here, with the measurement that decided it. */
function renderPlatformMute(data) {
  const n = data.platform_muted_count || 0;
  if (!n) return;
  const kept = data.platform_muted_with_mutuals || 0;
  const policy = {show: "listed here", ask: "flagged for review", mute: "excluded from Dream"}[data.platform_mute] || data.platform_mute;
  const box = $("#creview");
  const p = el("div", "note", `${n} platform-muted conversations · ${policy}`);
  p.title = `${kept} contain people you talk to elsewhere. Change platform mute behavior in Settings.`;
  box.append(p);
}

function chatRow(t, max, urgent) {
  const d = el("details", "grp chat-row" + (t.decision === "mute" ? " muted" : ""));
  const sum = el("summary");
  const identity = el("div", "chat-identity");
  identity.append(el("span", "gname", t.title || t.thread), el("span", "chat-source", t.channel));
  sum.append(identity);
  if (t.guessed) sum.append(el("span", "pill", "Unconfirmed name"));
  if (t.collision) sum.append(el("span", "pill", "Shared name"));
  if (t.decision === "mute") sum.append(el("span", "pill", "Muted"));
  else if (t.queued) sum.append(el("span", "chat-queued", `${nf(t.queued)} waiting`));
  const last = el("span", "chat-last", t.last);
  last.title = "Last message";
  sum.append(last, el("span", "chat-chevron", "›"));
  d.append(sum);

  const body = el("div", "gbody");
  if (t.speakers.length) {
    body.append(el("div", "note",
      "In it: " + t.speakers.join(", ")
      + (t.more_speakers ? ` and ${t.more_speakers} more` : "")));
  }
  const facts = el("div", "chat-stats");
  const metrics = [["Messages", nf(t.n)], ["From you", `${t.share}%`],
    ["Known people", nf(t.known)], ["Mutual contacts", nf(t.mutuals)]];
  if (t.group) metrics.push(["Participants", t.members || "Unknown"]);
  for (const [name, value] of metrics) {
    const stat = el("div"); stat.append(el("span", "chat-stat-value", value), el("span", "chat-stat-label", name));
    facts.append(stat);
  }
  body.append(facts);
  if (t.platform_muted) body.append(el("div", "note", t.platform_note || "Muted in the source app"));
  const act = el("div", "gact");
  const keep = el("button", "btn", t.decision === "read" ? "Reading enabled" : "Read in Dream");
  keep.disabled = t.decision === "read";
  keep.onclick = () => decideChat(t, "read");
  const mute = el("button", "btn", t.decision === "mute" ? "Muted" : "Mute");
  mute.disabled = t.decision === "mute";
  mute.onclick = () => decideChat(t, "mute");
  act.append(keep, mute);
  body.append(act);
  if (t.guessed) body.append(guessRow(t));
  d.append(body);
  if (urgent) d.open = false;
  return d;
}

async function decideChat(t, decision) {
  const out = await api("/api/chat", {channel: t.channel, thread: t.thread, decision});
  if (out.error) return;
  toast(decision === "mute"
    ? `muted — ${nf(out.retired || 0)} queued line(s) dropped, all still in the archive`
    : "kept — it will be read on every pass");
  await loadChats();
}

/* A guessed 1:1 name is reviewable here, not only over MCP. Confirming promotes
   it to a judgement so it stops reading as a guess; correcting renames it at
   the same authority. Same verb as memcal_name. */
function guessRow(t) {
  const guess = (t.title || "").replace(/^maybe:\s*/, "");
  const box = el("div", "gact");
  box.style.marginTop = "8px";
  const note = el("div", "note",
    `"${t.title}" is a guess — check it the next time this sender comes up.`);
  note.style.margin = "0 0 6px";
  const row = el("div", "gact");
  row.style.marginTop = "0";
  const ok = el("button", "btn", `Confirm “${guess}”`);
  ok.title = "this is right — stop showing it as a guess";
  ok.onclick = async () => {
    const out = await api("/api/name", {guess});
    if (out.error) return;
    toast(out.result || "confirmed");
    await loadChats();
  };
  const input = document.createElement("input");
  input.type = "search";
  input.placeholder = "correct name…";
  input.setAttribute("aria-label", `correct the guessed name ${guess}`);
  input.style.minWidth = "160px";
  const fix = el("button", "btn", "Rename");
  fix.title = "the guess was wrong — use this name instead";
  fix.onclick = async () => {
    const correct = input.value.trim();
    if (!correct) { input.focus(); return; }
    const out = await api("/api/name", {guess, correct});
    if (out.error) return;
    toast(out.result || "renamed");
    await loadChats();
  };
  input.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); fix.click(); } };
  row.append(ok, input, fix);
  box.append(note, row);
  return box;
}
$("#cstream").onchange = e => { state.cstream = e.target.value; loadChats(); };
let cTimer;
$("#cq").oninput = e => { clearTimeout(cTimer); cTimer = setTimeout(() => {
  state.cq = e.target.value.trim(); loadChats(); }, 220); };
