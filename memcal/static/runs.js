import { $, el, nf, api, toast, state } from "./core.js";
import { openTrace, seatRow } from "./trace.js";
import { retryDream } from "./jobs.js";

/* ---------------------------------------------------------------- runs -- */
/* The last payload, kept so the outcome filter redraws without re-asking. The list is
   forty rows and each one costs a `generations` count and a `spool` count on the
   server; filtering is a question about rows already in hand. */
let allRuns = [];
let outcomeFilter = "";

export function retryPill(run, after) {
  /* The one control that turns "this pass failed" into something you can do about it.
     It says what it will put back, because that is the part that is not obvious: a run
     refused on its first call claimed nothing and retrying it is an ordinary pass. */
  const b = el("button", "retrybtn", "Retry this pass");
  b.title = run.claimed
    ? `Puts the ${nf(run.claimed)} line(s) this pass read back in the queue, then dreams `
      + `over them with whatever is configured now.`
    : "This pass never claimed anything, so its traffic is still queued. Dreams over it "
      + "again with whatever is configured now.";
  b.onclick = async e => {
    e.stopPropagation();
    b.disabled = true;
    await retryDream(run.id, after);
    b.disabled = false;
  };
  return b;
}

export async function loadRuns() {
  const {runs} = await api("/api/runs?limit=40");
  allRuns = runs || [];
  $("#rundetail").innerHTML = "";
  renderRuns();
  if (state.run) openRun(state.run);
}

/* The newest pass that is worth retrying, called out above the table. Scanning twelve
   numeric columns for the one row that failed is exactly the work the tab should be
   doing for you, and the failure people care about is nearly always the last one. */
function renderRetryBanner() {
  const box = $("#runretry"); box.innerHTML = "";
  const broken = allRuns.find(r => r.retryable);
  if (!broken) return;
  const n = el("div", "banner");
  n.append(el("b", null, `Run #${broken.id} ${broken.outcome === "failed"
    ? "wrote nothing" : "only partly landed"} — ${broken.at}, ${broken.model}`));
  n.append(el("p", null, (broken.error || "").slice(0, 400)));
  const row = el("div", "row");
  row.append(retryPill(broken, () => loadRuns()));
  const note = el("span", "note");
  note.style.margin = "0";
  note.textContent = broken.claimed
    ? `${nf(broken.claimed)} line(s) go back in the queue first.`
    : "Nothing to put back — the traffic it was given is still queued.";
  row.append(note);
  n.append(row);
  box.append(n);
}

function renderRuns() {
  const body = $("#runs"); body.innerHTML = "";
  renderRetryBanner();
  const shown = allRuns.filter(r => !outcomeFilter || r.outcome === outcomeFilter);
  $("#runcount").textContent = shown.length === allRuns.length
    ? `${nf(allRuns.length)} pass${allRuns.length === 1 ? "" : "es"}`
    : `${nf(shown.length)} of ${nf(allRuns.length)}`;
  if (!shown.length) {
    body.innerHTML = `<tr><td colspan="13" class="empty">${
      allRuns.length ? "no pass ended that way" : "no passes yet"}</td></tr>`;
    return;
  }
  for (const r of shown) {
    const tr = el("tr", "runrow");
    tr.setAttribute("aria-expanded", "false");
    const state_ = el("td");
    state_.append(el("span", "outcome " + r.outcome, r.outcome_label));
    tr.append(el("td", "num", "#" + r.id), el("td", null, r.at), state_,
              el("td", null, r.mode), el("td", null, r.model),
              el("td", "num", nf(r.bundles)), el("td", "num", nf(r.items)), el("td", "num", nf(r.diffs)),
              el("td", "num", nf(r.prompt)), el("td", "num", nf(r.cached)), el("td", "num", nf(r.completion)),
              el("td", "num", "$" + r.cost.toFixed(4)));
    const err = el("td");
    if (r.retryable) {
      err.append(retryPill(r, () => loadRuns()));
    } else if (r.error) {
      err.className = "flag";
      err.textContent = r.error.slice(0, 80);
    }
    tr.append(err);
    tr.onclick = () => {
      const open = tr.getAttribute("aria-expanded") === "true";
      document.querySelectorAll(".runrow").forEach(x => x.setAttribute("aria-expanded", "false"));
      if (open) { $("#rundetail").innerHTML = ""; return; }
      tr.setAttribute("aria-expanded", "true");
      openRun(r.id);
    };
    body.append(tr);
  }
}

for (const b of $("#routcome").querySelectorAll("button")) {
  b.onclick = () => {
    outcomeFilter = b.dataset.v;
    for (const other of $("#routcome").querySelectorAll("button"))
      other.setAttribute("aria-pressed", String(other === b));
    renderRuns();
  };
}

/* One pass, opened up. The runs table was twelve numbers and an error string, and the
   error string was always where the interesting thing was — "6 bundle(s) left queued"
   named four of six and could not say what happened to any of them. */
async function openRun(id) {
  state.run = id;
  const box = $("#rundetail");
  box.innerHTML = '<div class="empty">opening run #' + id + '…</div>';
  const d = await api("/api/run?id=" + id);
  box.innerHTML = "";
  if (d.error) { box.append(el("div", "empty", d.error)); return; }

  const h = el("h2", null, `Run #${d.run.id} · ${d.run.mode} · ${d.run.model}`);
  h.style.scrollMarginTop = "70px";
  box.append(h);
  const meta = el("div", "tstats");
  /* Requests and waiting only when they say something the calls line cannot: a pass
     that retried nothing has requests === calls, and a pass that spent 56 minutes being
     refused has neither calls nor cost and used to render as a pass that did nothing. */
  const spent = [];
  if (d.run.requests && d.run.requests !== d.calls.length) {
    spent.push(`${nf(d.run.requests)} requests`);
  }
  if (d.run.failed_calls) spent.push(`⚠ ${nf(d.run.failed_calls)} failed`);
  if (d.run.wait_seconds >= 1) spent.push(`${nf(Math.round(d.run.wait_seconds))}s in backoff`);
  /* null is "not recorded" — a run older than the columns — and draws as nothing. */
  meta.textContent = [
    d.run.at, d.run.finished ? "→ " + d.run.finished.slice(11) : "",
    `${nf(d.run.bundles)} bundles`, `${nf(d.run.items)} lines`, `${nf(d.calls.length)} calls`,
    ...spent,
    `${nf(d.run.diffs)} writes`, `$${d.run.cost.toFixed(4)}`, d.run.outcome_label,
  ].filter(Boolean).join(" · ");
  box.append(meta);
  if (d.run.error) {
    const w = el("div", "banner");
    w.append(el("b", null, `This pass ${d.run.outcome_label}`),
             el("p", null, d.run.error));
    if (d.run.retryable) {
      const row = el("div", "row");
      row.append(retryPill({...d.run}, () => loadRuns()));
      const note = el("span", "note");
      note.style.margin = "0";
      note.textContent = d.run.claimed
        ? `${nf(d.run.claimed)} line(s) this pass read go back in the queue first.`
        : "It claimed nothing, so everything it was given is still queued.";
      row.append(note);
      w.append(row);
    }
    box.append(w);
  }

  /* Bundles that got no diff back, first — that is what someone opens a run for. Not
     "the model did not answer": under the v1 contract that is one of two readings, and
     the other one is that it read them and correctly had nothing to say. */
  const missed = d.seats.filter(s => s.outcome !== "read");
  if (missed.length) {
    box.append(el("h3", null, `${missed.length} bundle(s) got no diff back`));
    const note = el("p", "note");
    note.textContent = "Left queued, so a later pass re-reads them. Whether that was right "
      + "depends on which happened — the model read them and had nothing to say, or it "
      + "never came back about them at all. Click one to open the call and read it.";
    box.append(note);
    const grid = el("div", "grid3");
    for (const s of missed) grid.append(seatRow(s));
    box.append(grid);
  }

  /* Requests that never came back. They have no generation id, so they can never be one
     of the cards below — and before they were written to disk the only record of them
     was a sentence in the run's error string. */
  const failures = d.failures || [];
  if (failures.length) {
    box.append(el("h3", null, `${failures.length} request(s) never came back`));
    const grid = el("div", "grid3");
    for (const f of failures) {
      const row = el("div", "seat");
      const cost = [f.requests ? `${f.requests} requests` : "",
                    f.waited >= 1 ? `${Math.round(f.waited)}s waiting` : ""]
        .filter(Boolean).join(" · ");
      row.append(el("span", "pill process", f.stage_label || f.stage || "Read"),
                 el("span", "callentity", f.label || ""),
                 el("div", "note", f.error),
                 cost ? el("div", "note", cost) : el("span"));
      grid.append(row);
    }
    box.append(grid);
  }

  box.append(el("h3", null, `The ${d.calls.length} requests`));
  for (const c of d.calls) box.append(callCard(c, d));

  box.append(el("h3", null, `Everything this pass wrote (${d.writes.length})`));
  const wrap = el("div", "grid3");
  for (const w of d.writes) {
    const row = el("div", "seat");
    row.append(el("span", "pill process", w.kind));
    const name = el("span", "seatname", `${w.verb} ${w.ref}`);
    row.append(name);
    if (w.bundle && w.gen) {
      const b = el("span", "bid link", w.bundle);
      b.title = "open the call that wrote this, at the bundle it came from";
      row.style.cursor = "pointer";
      row.onclick = () => openCallAndFind(w.gen, w.bundle, w.entity);
      row.append(b);
    }
    wrap.append(row);
  }
  box.append(wrap);

  /* Arrived from a "why" button: open that exact call, read it, and jump straight to
     the lines that wrote the row we came from. Landing on the run and leaving them to
     find which of 24 requests it was is the thing this replaces. */
  const card = state.callFlash && document.getElementById("call-" + state.callFlash);
  if (card) {
    const gen = state.callFlash, needle = state.callNeedle;
    state.callFlash = ""; state.callNeedle = "";
    card.open = true;
    const go = card.querySelector("button.btn");
    if (go) { go.remove(); openTrace(gen, card.querySelector(".calltrace"), needle,
                                   {hideBundles: true}); }
    card.scrollIntoView({behavior: "smooth", block: "start"});
    card.classList.add("flash");
    setTimeout(() => card.classList.remove("flash"), 1400);
    return;
  }
  state.callFlash = ""; state.callNeedle = "";
  h.scrollIntoView({behavior: "smooth", block: "start"});
}
/* Open one call on the Runs tab and scroll to a bundle inside it. The card is already
   on the page — this is the same journey the "why" deep link makes, minus the reload. */
export function openCallAndFind(gen, bid, label) {
  const card = document.getElementById("call-" + gen);
  if (!card) { toast("that call is not on this page"); return; }
  card.open = true;
  card.scrollIntoView({behavior: "smooth", block: "start"});
  const holder = card.querySelector(".calltrace");
  const go = card.querySelector("button.btn");
  if (go) {
    go.remove();
    openTrace(gen, holder, "", {hideBundles: true}).then(
      () => bid && findInTrace(holder, bid, label));
  } else if (bid) {
    findInTrace(holder, bid, label);
  }
}

/* Jump to a bundle's own lines inside an already-open call. The bundle id is in the
   prompt under v2; under v1 (and for older runs) the label is what is there, so try
   the id first and fall back to the name. */
export function findInTrace(holder, bid, label) {
  const box = holder.querySelector(".tsearch input");
  if (!box) { toast("open the call first"); return; }
  box.value = bid;
  box.dispatchEvent(new Event("input"));
  setTimeout(() => {
    if (holder.querySelector(".thit")) return;
    box.value = label || bid;
    box.dispatchEvent(new Event("input"));
  }, 240);
}

function callCard(c, d) {
  const card = el("details", "tpart");
  card.id = "call-" + c.gen;
  const bits = [`call ${c.n}`, c.stage_label || c.stage, c.model.split("/").pop(),
                `${c.bundles.length || "?"} bundles`,
                `${nf(c.prompt_tokens)} in`, `${nf(c.completion_tokens)} out`,
                c.max_tokens ? `ceiling ${nf(c.max_tokens)}` : "",
                c.finish_reason ? "finish " + c.finish_reason : "",
                /* A reply retried into existence bills like one that arrived first
                   time and is a different fact about the provider. */
                (c.requests || 1) > 1 ? `${c.requests} requests` : "",
                `$${(c.cost || 0).toFixed(4)}`];
  if (c.unrouted && c.unrouted.length) bits.push(`⚠ ${c.unrouted.length} unanswered`);
  if (c.truncated) bits.push("⚠ TRUNCATED");
  const sum = el("summary");
  sum.append(el("span", "bid", `#${d.run.id}·${c.n}`),
             document.createTextNode(" " + bits.slice(1).filter(Boolean).join(" · ")));
  card.append(sum);
  /* The bundles this call carried, named on the summary line so the request holding a
     given conversation is findable by looking rather than by opening 24 of them. */
  if (c.bundles && c.bundles.length) {
    const names = el("div", "callentity");
    names.style.margin = "0 10px 6px";
    names.textContent = c.bundles.map(b => b.label).join(" · ");
    card.append(names);
  }

  const body = el("div"); body.style.padding = "0 10px 12px";
  if (!c.saved) {
    body.append(el("div", "note",
      "Not on disk — made before calls were kept. tools/dump_generation.py backfills it."));
  }
  const holder = el("div", "calltrace");
  if (c.bundles && c.bundles.length) {
    const grid = el("div", "grid3");
    const landed = new Set((c.routed || []).map(r => r.id));
    for (const ref of c.bundles) {
      grid.append(seatRow({...ref, outcome: landed.has(ref.id) ? "read" : "no diff"},
                          holder));
    }
    body.append(grid);
  }
  /* What the model echoed as `entity`, when that is not what it was given. This is the
     routing failure made visible: a diff naming a bundle nobody sent is dropped. */
  if (c.echoed && c.bundles && c.echoed.length !== c.bundles.length) {
    const n = el("div", "note");
    n.textContent = `Asked for ${c.bundles.length} diffs, got ${c.echoed.length}`
      + (c.echoed.length ? ": " + c.echoed.map(x => x.slice(0, 40)).join(" | ") : ".");
    body.append(n);
  }
  const open = el("button", "btn", "read the call");
  open.onclick = () => { open.remove(); openTrace(c.gen, holder, "", {hideBundles: true}); };
  body.append(open, holder);
  card.append(body);
  card.dataset.gen = c.gen;
  return card;
}
