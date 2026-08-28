import { $, el, nf, api, toast } from "./core.js";

/* ------------------------------------------------------------- senders -- */
const sstate = {decision: "", q: ""};
$("#sdec").onclick = e => {
  const b = e.target.closest("button"); if (!b) return;
  sstate.decision = b.dataset.v;
  $("#sdec").querySelectorAll("button").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
  loadSenders();
};
let sTimer;
$("#sq").oninput = e => { clearTimeout(sTimer); sTimer = setTimeout(loadSenders, 220); };
export async function loadSenders() {
  sstate.q = $("#sq").value.trim();
  const p = new URLSearchParams({limit: "300"});
  if (sstate.decision) p.set("decision", sstate.decision);
  if (sstate.q) p.set("q", sstate.q);
  const {senders} = await api("/api/senders?" + p);
  const body = $("#senders"); body.innerHTML = "";
  if (!senders.length) {
    body.innerHTML = '<tr><td colspan="8" class="empty">no senders match</td></tr>'; return;
  }
  for (const s of senders) {
    const tr = el("tr");
    const addr = el("td", "addr"); addr.append(document.createTextNode(s.address));
    if (s.disagrees) {
      const f = el("div", "flag",
        "Still set to process, but it looks like a no-reply/bulk address. It was let "
        + "through before the rule that catches it existed, and the table is checked "
        + "first — so it keeps costing tokens until you set it to archive.");
      addr.append(f);
    }
    tr.append(addr);
    const dec = el("td");
    dec.append(el("span", "pill " + s.decision, s.decision));
    // Whether the subject test can still rescue a line from this sender. The gate's own
    // archiving is a guess an appointment reminder may overturn; a no from them is not.
    if (s.blocked) {
      const b = el("span", "pill");
      b.style.cssText = "border-color:var(--warn);color:var(--warn)";
      b.textContent = s.source === "agent" ? "blocked by agent" : "blocked by you";
      b.title = "Permanent. No subject line reopens this — set it back to process to undo.";
      dec.append(b);
    } else if (s.decision === "archive" || s.decision === "ignore") {
      const b = el("span", "pill");
      b.textContent = "gate's guess";
      b.title = "Archived by an address or header test. A subject that reports an "
              + "appointment, delivery or invitation still gets through.";
      dec.append(b);
    }
    tr.append(dec);
    const seen = el("td", "num", nf(s.seen));
    seen.title = "how many a dream pass actually read — the rest were retired by the "
               + "horizon sweep before any pass got to them";
    tr.append(el("td", "num", nf(s.n)), el("td", "num", nf(s.passed)), seen,
              el("td", null, s.last_seen), el("td", "subj", s.subject || ""));

    const set = el("td"), seg = el("div", "setseg");
    for (const d of ["process", "archive", "ignore"]) {
      const b = el("button", null, d);
      b.setAttribute("aria-pressed", String(s.decision === d));
      b.onclick = () => flip(s.address, d);
      seg.append(b);
    }
    set.append(seg); tr.append(set);
    body.append(tr);
  }
}

async function flip(address, decision) {
  const out = await api("/api/sender", {address, decision, backfill: $("#backfill").checked});
  if (out.error) return;
  const extra = out.queued ? ` · queued ${out.queued} past` : out.retired ? ` · retired ${out.retired} queued` : "";
  toast(`${address} → ${decision}${extra}`);
  loadSenders();
}
