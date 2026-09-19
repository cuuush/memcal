import { $, el, nf, api, jumpToBundle, state } from "./core.js";

/* -------------------------------------------------------------- memory -- */
export async function loadMemory() {
  const m = await api("/api/memory");
  const box = $("#brief"); box.innerHTML = "";
  for (const row of (m.lines || [])) {
    const token = (row.sources || [])[0];
    const target = token && (m.targets || {})[token];
    const change = (target || {}).last_dream_change || "";
    const line = el(target ? "button" : "div",
                    "briefline" + (target ? " click" : "")
                    + (change ? ` dream-${change}` : "")
                    + (String(row.text || "").startsWith("## ") ? " head" : ""),
                    row.text || "\u00a0");
    if (target) {
      line.title = `open ${token} source`;
      line.onclick = () => openWhy(target.kind, target.ref, row.text);
      if (change) line.append(el("span", `dreamchange ${change}`,
                                 target.change_label || (change === "new" ? "New" : "Updated")));
      line.append(citeChip(target.citations));
    }
    box.append(line);
  }
}

/* One chip saying how well a line is backed up. Three states, because they mean three
   different things: cited lines, a whole conversation attached because nothing could be
   narrowed, and nothing at all. The middle one used to look exactly like the first. */
function citeChip(c) {
  if (!c) return el("span", "cites none", "no source");
  if (!c.lines) return el("span", "cites none", "no source");
  const chip = el("span", "cites" + (c.narrow ? "" : " wide"),
                  c.narrow ? `${c.lines} cited` : `${c.lines} lines, uncited`);
  chip.title = (c.narrow ? "lines this row was built from" : "the whole conversation — "
                + "no line was pointed at")
    + (c.conversations || []).map(n => "\n" + n).join("");
  return chip;
}

function fill(sel, rows, main, sub, provenance) {
  const box = $(sel); box.innerHTML = "";
  if (!rows.length) { box.append(el("div", "empty", "nothing yet")); return; }
  const ul = el("ul");
  for (const r of rows) {
    const li = el("li"); li.append(document.createTextNode(main(r)));
    // "Where did this come from?" used to have no answer at all: written_by says
    // "dream:nightly", which names a mode, not a call.
    const p = provenance && provenance(r);
    if (p && p[1]) {
      const b = el("button", "whybtn", "why");
      b.title = "the model call that wrote this";
      b.onclick = () => openWhy(p[0], p[1], main(r));
      li.append(b);
    }
    const s = sub && sub(r);
    if (s) { const d = el("div"); d.style.cssText = "color:var(--ink-3);font-size:12px"; d.textContent = s; li.append(d); }
    ul.append(li);
  }
  box.append(ul);
}

/* ------------------------------------------------------------ provenance -- */
/* Click a row, see the calls that wrote it; click a call, read what it was sent, what
   it thought and what it answered. The prompt and reasoning are fetched from
   OpenRouter on demand — memcal only stores the id. */
function appendHighlighted(node, value, terms) {
  const text = String(value || "");
  const clean = [...new Set((terms || []).filter(Boolean))]
    .sort((a, b) => b.length - a.length);
  if (!clean.length) { node.append(document.createTextNode(text)); return; }
  const escaped = clean.map(x => x.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const re = new RegExp(`(${escaped.join("|")})`, "gi");
  let at = 0;
  for (const match of text.matchAll(re)) {
    node.append(document.createTextNode(text.slice(at, match.index)));
    node.append(el("mark", "keyhit", match[0]));
    at = match.index + match[0].length;
  }
  node.append(document.createTextNode(text.slice(at)));
}

function eventRange(e) {
  return e.until && e.until !== e.date ? `${e.date} → ${e.until}` : e.date;
}

/* A pill is a question about one facet, so it asks one: `/api/events?person=…`. The
   whole related block used to ride along inside `/api/why`, which meant resolving every
   attendee, the location and the series of every event opened, whether or not anything
   was ever clicked. */
async function openRelated(box, facet, value, exclude) {
  box.hidden = false;
  box.innerHTML = "";
  box.append(el("div", "bname", `Other entries involving ${value}`),
             el("div", "note", "looking…"));
  const p = new URLSearchParams({[facet]: value, exclude: exclude || ""});
  const out = await api("/api/events?" + p);
  renderRelated(box, value, out.error ? [] : out.events, out);
}

function renderRelated(box, label, rows, out) {
  box.innerHTML = "";
  box.append(el("div", "bname", `Other entries involving ${label}`));
  if (!rows || !rows.length) {
    box.append(el("div", "note", (out || {}).error || "No other linked entries."));
    return;
  }
  for (const e of rows) {
    const row = el("button", "relatedrow");
    row.append(document.createTextNode(e.title),
               el("small", null, [eventRange(e), e.time, e.location, e.status]
                 .filter(Boolean).join(" · ")));
    row.onclick = () => openWhy("event", e.key, e.title);
    box.append(row);
  }
  // `total` counts everything that matched; the list is capped by the request's limit.
  const shown = rows.length, total = (out || {}).total ?? shown;
  if (total > shown) box.append(el("div", "note", `showing ${shown} of ${total}`));
}

// How long ago, in the coarsest unit that is still true. The timeline used to stamp
// every line with a full ISO second in 11px mono, which answers "when exactly" — a
// question nobody had — and buries "was this recent", which is the one being asked.
function ago(iso) {
  const then = Date.parse(String(iso || "").replace(" ", "T"));
  if (!then) return "";
  const mins = Math.round((Date.now() - then) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return days < 31 ? `${days}d ago` : `${Math.round(days / 30)}mo ago`;
}

//: Who a write came from, said the way the user would say it back to me.
function writerLabel(write) {
  if (write.unstamped) return "edited directly";
  const stage = write.stage_label || write.stage || "Automatic";
  const run = write.run ? `run ${write.run}` : "";
  return [stage, run].filter(Boolean).join(" · ");
}

function renderTimeline(timeline) {
  const writes = timeline.writes || [];
  const box = el("div", "timeline");
  if (!writes.length) { box.append(el("div", "empty", "nothing recorded")); return box; }

  for (const write of writes) {
    const item = el("div", "timeitem" + (write.unstamped ? " unstamped" : ""));
    const head = el("div", "timehead");
    head.append(el("span", "timeverb", write.verb || "written"),
                el("span", "timewho", writerLabel(write)));
    const when = el("span", "timewhen", ago(write.at));
    when.title = String(write.at || "").replace("T", " ");
    head.append(when);
    item.append(head);
    // The bundle it came out of, on its own dim line rather than crammed into the head.
    // These run to `thread:agent:hermes:20260801_145043_b108363f` — the single noisiest
    // token on the row — so it is truncated and never allowed to shove the verb around.
    if (write.entity) {
      const from = el("div", "timefrom");
      from.append(el("span", "timefromk", "from"), el("span", "timefromv", write.entity));
      from.title = write.entity;
      item.append(from);
    }

    // What actually moved. One row per field, old and new side by side, so a write
    // that walked `status` back to where it started is visible as exactly that.
    if (write.changes.length) {
      const fields = el("div", "timefields");
      for (const change of write.changes) {
        const asText = v => Array.isArray(v) ? (v.join(", ") || "—") : (v || "—");
        const row = el("div", "timefield");
        row.append(el("span", "tfname", change.field),
                   el("span", "tfold", asText(change.old)),
                   el("span", "tfarrow", "→"),
                   el("span", "tfnew", asText(change.new)));
        fields.append(row);
      }
      item.append(fields);
    } else if (!write.unstamped) {
      item.append(el("div", "timenone", "no field changed"));
    }

    // The lines this particular write was looking at. This is the correlation the
    // aggregate count could never make: "15 cited" against a row says nothing about
    // which write believed what, and three writes here quoted 11, 2 and 3 lines.
    if (write.cites.length) {
      const toggle = el("button", "timecites");
      const label = n => `${n} line${n === 1 ? "" : "s"} it read`;
      toggle.textContent = `▸ ${label(write.cites.length)}`;
      const lines = el("div", "timelines"); lines.hidden = true;
      for (const cite of write.cites) {
        const line = el("div", "timeline-src");
        line.append(el("span", "tsrcwho", cite.who || "?"),
                    el("span", "tsrcwhen", cite.ts || ""),
                    el("span", "tsrctext", cite.text || ""));
        line.title = `${cite.channel}${cite.thread ? " · " + cite.thread : ""}`;
        line.onclick = () => openConversation(cite);
        lines.append(line);
      }
      toggle.onclick = () => {
        lines.hidden = !lines.hidden;
        toggle.textContent = `${lines.hidden ? "▸" : "▾"} ${label(write.cites.length)}`;
      };
      item.append(toggle, lines);
    } else if (write.gen) {
      item.append(el("div", "timenone", "cited nothing"));
    }
    box.append(item);
  }
  return box;
}

//: Open the conversation a cited line came from, centred on that line.
async function openConversation(cite) {
  const panel = document.querySelector("#tracebody") || document.body;
  let holder = panel.querySelector(".convopreview");
  if (!holder) { holder = el("div", "wikipreview convopreview"); panel.append(holder); }
  holder.innerHTML = '<div class="empty">opening conversation…</div>';
  const out = await api("/api/conversation?channel=" + encodeURIComponent(cite.channel)
    + "&thread=" + encodeURIComponent(cite.thread || "")
    + "&around=" + encodeURIComponent(cite.ts || ""));
  holder.innerHTML = "";
  holder.append(el("div", "bname", `Conversation · ${cite.thread || cite.channel}`));
  for (const line of (out.lines || out || [])) {
    const row = el("div", "timeline-src" + (line.id === cite.id ? " ishere" : ""));
    row.append(el("span", "tsrcwho", line.who || "?"),
               el("span", "tsrcwhen", line.ts || ""),
               el("span", "tsrctext", line.text || ""));
    holder.append(row);
  }
  holder.scrollIntoView({behavior: "smooth", block: "nearest"});
}

export async function openWikiPreview(slug, holder) {
  holder.innerHTML = '<div class="empty">opening wiki page…</div>';
  const page = await api("/api/wiki?slug=" + encodeURIComponent(slug));
  holder.innerHTML = "";
  if (page.error) { holder.append(el("div", "empty", page.error)); return; }
  renderWikiProfile(page, holder);
}

/* One page, the way memcal holds it: what it knows, the exact line behind each
   fact, and which past events it turned up in. A cited fact leads with its lines —
   the messages it was built from, each clickable to read the conversation around
   it — plus the bundle it came out of as a link back to the Dream tab. An old
   fact with only bundle-wide evidence says so and caps the preview rather than
   dumping forty lines. Shared by the event panel's wiki links and the Wiki tab,
   so the two cannot drift. `reload` re-opens the page after an edit; previews
   without one simply omit the edit buttons. */
export function renderWikiProfile(page, holder, reload) {
  holder.innerHTML = "";
  const head = el("div", "wikihead");
  head.append(el("span", "bname", page.title || page.slug));
  if (page.is_self) head.append(el("span", "youbadge", "you"));
  if (page.section) head.append(el("span", "wikisection", page.section));
  if (reload) {
    const edit = el("button", "btn minim", "edit markdown");
    edit.title = "edit this page as markdown — the wiki is files, this is the file";
    edit.onclick = () => openMarkdownEditor(page, holder, reload);
    head.append(edit);
  }
  holder.append(head);

  if ((page.aliases || []).length) {
    const box = el("div", "wikialiases");
    box.append(el("span", "metak", "also known as"));
    for (const name of page.aliases) box.append(el("span", "aliaschip", name));
    holder.append(box);
  }

  const facts = page.facts || [];
  if (facts.length) {
    holder.append(el("div", "bname", "What it knows"));
    for (const fact of facts) {
      const cited = (page.narrow || {})[fact.slot];
      const lines = (page.sources || {})[fact.slot] || [];
      const prov = ((page.provenance || {})[fact.slot]) || null;
      const box = el("div", "wikifact");
      const top = el("div", "wikifacttop");
      top.append(el("span", "metak", fact.slot),
                 el("span", "wikifactval", fact.value || "—"));
      // How well this one fact is backed, on the fact itself — the same three states
      // the brief lines carry, because a value quoted from one message and a value
      // guessed at from a whole conversation are not the same claim.
      const quoted = lines.filter(l => l.evidence);
      if (quoted.length && cited) {
        const chip = el("span", "cites", `${quoted.length} cited`);
        chip.title = "the messages this fact was read from — click one to read around it";
        top.append(chip);
      } else if (lines.length) {
        const chip = el("span", "cites wide", `${lines.length} lines, uncited`);
        chip.title = "no message was pointed at — the whole conversation is attached";
        top.append(chip);
      } else if (fact.source) {
        top.append(el("span", "cites none", fact.source));
      }
      if (reload) {
        const edit = el("button", "whybtn", "edit");
        edit.title = "change this fact — your edit wins over older evidence";
        edit.onclick = () => openFactEditor(page, fact, box, reload);
        top.append(edit);
      }
      box.append(top);
      // Where it came from, as something to go and look at: the bundle link opens
      // the Dream tab on the exact bundle, and each line opens its conversation.
      if (prov && (prov.bundle || prov.run)) {
        const from = el("div", "timefrom");
        from.append(el("span", "timefromk", "from"));
        if (prov.bundle) {
          const b = el("span", "bid link", prov.bundle);
          b.title = "show this bundle on the Dream tab";
          b.onclick = () => jumpToBundle(prov.bundle);
          from.append(b, document.createTextNode(" "));
        }
        const bits = [prov.entity, prov.run ? "run " + prov.run : "",
                      prov.at || ""].filter(Boolean).join(" · ");
        const v = el("span", "timefromv", bits);
        v.title = prov.entity || "";
        from.append(v);
        box.append(from);
      }
      if (quoted.length && cited) {
        // The exact lines, on screen — no toggle to open, because one or two
        // lines are the answer and hiding them behind a count re-creates the
        // complaint that started this.
        const wrap = el("div", "timelines");
        for (const cite of quoted) wrap.append(citeRow(cite));
        box.append(wrap);
      } else if (lines.length) {
        // Old bundle-wide evidence: a capped preview plus the way out, not a
        // forty-line dump. The bundle link above names the conversation.
        const wrap = el("div", "timelines");
        const shown = lines.slice(0, WIDE_PREVIEW);
        for (const cite of shown) wrap.append(citeRow(cite));
        box.append(wrap);
        if (lines.length > shown.length) {
          const toggle = el("button", "timecites",
            `▸ show all ${lines.length} lines`);
          toggle.onclick = () => {
            for (const cite of lines.slice(shown.length)) wrap.append(citeRow(cite));
            toggle.remove();
          };
          box.append(toggle);
        }
      }
      holder.append(box);
    }
  }

  if ((page.open_questions || []).length) {
    holder.append(el("div", "bname", "Open questions"));
    const ul = el("ul", "wikiq");
    for (const q of page.open_questions) ul.append(el("li", null, q));
    holder.append(ul);
  }

  const enc = page.encounters || {};
  if (enc.count) {
    holder.append(el("div", "bname",
      `Seen at ${enc.count} past event${enc.count === 1 ? "" : "s"}`));
    if ((enc.by_activity || []).length) {
      const chips = el("div", "wikialiases");
      for (const a of enc.by_activity)
        chips.append(el("span", "aliaschip", `${a.activity} · ${a.count}`));
      holder.append(chips);
    }
    for (const e of (enc.recent || [])) {
      const sub = [e.date, e.location, (e.with || []).join(", ")].filter(Boolean).join(" · ");
      if (e.key) {
        const row = el("button", "relatedrow");
        row.append(document.createTextNode(e.title), el("small", null, sub));
        row.onclick = () => openWhy("event", e.key, e.title);
        holder.append(row);
      } else {
        const row = el("div", "relatedrow");
        row.append(document.createTextNode(e.title), el("small", null, sub));
        holder.append(row);
      }
    }
  }

  if (!facts.length && !(page.open_questions || []).length && !enc.count) {
    // A page with only prose the user typed. Fall back to the rendered markdown rather than
    // drawing an empty shell.
    const pre = el("pre"); pre.textContent = page.page || ""; holder.append(pre);
  }
}

/* How many lines of bundle-wide evidence to show before asking. Narrow citations
   are one or two lines and always render whole; this only caps the old shape. */
const WIDE_PREVIEW = 6;

function citeRow(cite) {
  const row = el("div", "timeline-src" + (cite.evidence ? "" : " ctx"));
  row.append(el("span", "tsrcwho", cite.who || "?"),
             el("span", "tsrcwhen", String(cite.ts || "").slice(0, 16).replace("T", " ")),
             el("span", "tsrctext", cite.text || ""));
  row.title = `${cite.channel || ""}${cite.thread ? " · " + cite.thread : ""} — click to read around it`;
  row.onclick = () => openConversation(cite);
  return row;
}

/* One fact, edited in place. Saves through /api/wiki_slot at user authority —
   an explicit correction newer evidence must not walk back. */
function openFactEditor(page, fact, box, reload) {
  let form = box.querySelector(".wikiedit");
  if (form) { form.remove(); return; }
  form = el("div", "wikiedit");
  const input = document.createElement("input");
  input.type = "text"; input.value = fact.value || ""; input.maxLength = 500;
  const save = el("button", "btn go minim", "save");
  const cancel = el("button", "btn minim", "cancel");
  form.append(input, save, cancel);
  box.append(form);
  input.focus(); input.select();
  cancel.onclick = () => form.remove();
  const commit = async () => {
    const value = input.value.trim();
    if (!value || value === fact.value) { form.remove(); return; }
    save.disabled = true;
    const out = await api("/api/wiki_slot",
      {slug: page.slug, slot: fact.slot, value});
    if (!out.error) reload();
  };
  save.onclick = commit;
  input.onkeydown = e => {
    if (e.key === "Enter") commit();
    if (e.key === "Escape") form.remove();
  };
}

/* The whole file as markdown — because the wiki is files, and the file is the
   thing being edited. Saves verbatim so prose, ordering and comments survive. */
async function openMarkdownEditor(page, holder, reload) {
  const raw = await api("/api/wiki_raw?slug=" + encodeURIComponent(page.slug));
  if (raw.error) return;
  holder.innerHTML = "";
  const head = el("div", "wikihead");
  head.append(el("span", "bname", (page.title || page.slug) + " · markdown"));
  holder.append(head);
  const area = document.createElement("textarea");
  area.className = "wikimd";
  area.value = raw.markdown || "";
  area.spellcheck = false;
  holder.append(area);
  const row = el("div", "wikieditrow");
  const save = el("button", "btn go", "save page");
  const cancel = el("button", "btn", "cancel");
  row.append(save, cancel);
  holder.append(row);
  cancel.onclick = reload;
  save.onclick = async () => {
    save.disabled = true;
    const out = await api("/api/wiki_save",
      {slug: page.slug, markdown: area.value});
    if (!out.error) reload();
    else save.disabled = false;
  };
}

function renderEventDetail(detail, body) {
  if (!detail || !detail.event) return;
  const e = detail.event;
  const top = el("div", "eventtop");

  const links = el("div", "eventlinks");
  for (const page of (detail.wiki || [])) {
    const b = el("button", "metalink", `${page.title} wiki`);
    b.title = `open ${page.section} wiki page`;
    b.onclick = () => {
      let preview = top.querySelector(".wikipreview");
      if (!preview) { preview = el("div", "wikipreview"); top.append(preview); }
      openWikiPreview(page.slug, preview);
    };
    links.append(b);
  }
  if (links.childNodes.length) {
    top.append(el("div", "bname", "Wiki pages"), links);
  }

  top.append(el("div", "bname", "Timeline"), renderTimeline(detail.timeline || {}));

  const meta = el("div", "eventmeta");
  const related = el("div", "relatedbox"); related.hidden = true;
  const field = (name, value, wide) => {
    if (value === undefined || value === null || value === "") return;
    const box = el("div", "metafield" + (wide ? " wide" : ""));
    box.append(el("div", "metak", name));
    const val = el("div", "metav");
    if (value instanceof Node) val.append(value); else val.textContent = value;
    box.append(val); meta.append(box);
  };
  field("Date", eventRange(e));
  field("Time", e.time || "not specified");
  field("State", e.state || e.status);
  const pill = (facet, value) => {
    const b = el("button", "metalink", value);
    b.title = `other entries sharing this ${facet}`;
    b.onclick = () => openRelated(related, facet, value, e.key);
    return b;
  };
  if (e.subject && e.subject !== "me") {
    field("Subject", pill("person", e.subject));
  } else {
    field("Subject", "me");
  }

  const attendeeWrap = el("span");
  if ((e.participants || []).length) {
    for (const person of e.participants) attendeeWrap.append(pill("person", person));
  } else {
    attendeeWrap.textContent = "none explicitly recorded";
  }
  field("Attendees", attendeeWrap, true);

  if (e.location) field("Location", pill("location", e.location), true);
  if (e.series) field("Series", pill("series", e.series));
  field("Note", e.note, true);
  field("Source", e.source);
  field("Stable key", e.key, true);
  field("Writer", `${e.written_by} · created ${e.created_at} · updated ${e.updated_at}`,
        true);
  top.append(el("div", "bname", "Event metadata"), meta, related);
  body.append(top);
}

export async function openWhy(kind, ref, title) {
  const panel = $("#tracepanel");
  panel.hidden = false;
  $("#tracetitle").textContent = title;
  const body = $("#tracebody");
  body.innerHTML = '<div class="empty">looking up…</div>';
  const out = await api(`/api/why?kind=${encodeURIComponent(kind)}&ref=${encodeURIComponent(ref)}`);
  body.innerHTML = "";
  if (out.error) { body.append(el("div", "empty", out.error)); return; }
  renderEventDetail(out.detail, body);
  /* Lead with how well this is evidenced, before anything it claims. A row backed by
     two messages and a row backed by "it was in this group chat somewhere" read
     identically once the lines are on screen. */
  const c = out.citations || {};
  const head = el("div", "citehead");
  head.append(citeChip(c));
  if (c.conversations && c.conversations.length)
    head.append(el("span", null, c.conversations.join(" · ")));
  if (c.first) head.append(el("span", null, c.first === c.last ? c.first
                                          : `${c.first} → ${c.last}`));
  if (c.lines && !c.narrow)
    head.append(el("span", "citewarn",
      "nothing in this row points at a line — treat it as a summary, not a quote"));
  body.append(head);
  if (out.source && out.source.length) {
    body.append(el("div", "bname", "Original source"));
    const source = el("div", "sourcebox");
    for (const s of out.source) {
      if (s.source_heading) {
        source.append(el("div", "sourceshift", s.source_heading));
      }
      const row = el("div", "sourcerow" + (s.evidence ? "" : " ctx"));
      row.append(el("span", "sourcewho",
                    `${String(s.ts || "").slice(0,16).replace("T"," ")} · ${s.who}`));
      appendHighlighted(row, s.text || "", out.highlight_terms);
      source.append(row);
    }
    body.append(source);
  }
  if (!out.calls.length) {
    const direct = (out.direct || [])[0];
    const detail = direct
      ? `${direct.verb || "written"} directly by ${direct.stage_label || direct.stage || "Automatic"}`
      : "written directly by the user/agent, or predates call provenance";
    body.append(el("div", "empty",
      `No model call wrote this. It was ${detail}; the original source above is the `
      + "useful record."));
    return;
  }
  for (const c of out.calls) {
    const row = el("div", "callrow");
    const head = el("div", "callhead");
    head.append(el("span", "pill " + (c.stage === "sweep" ? "archive" : "process"),
                       c.stage_label || c.stage || "?"),
                el("span", null, `${c.verb || ""} · ${c.at}`),
                el("span", "callmeta",
                   [c.model, c.run ? "run #" + c.run : "", nf(c.prompt) + " in",
                    nf(c.completion) + " out", c.cost ? "$" + c.cost : ""]
                     .filter(Boolean).join(" · ")));
    row.append(head);
    /* Where it came from, as something you can go and look at rather than a string to
       read. The id is the bundle's name everywhere; the buttons open it on the Dream
       tab and open the run it was read in. */
    if (c.entity) {
      const line = el("div", "callentity");
      if (c.bundle) {
        const b = el("span", "bid link", c.bundle);
        b.title = "show this bundle on the Dream tab";
        b.onclick = () => { panel.hidden = true; jumpToBundle(c.bundle); };
        line.append(b, document.createTextNode(" "));
      }
      line.append(document.createTextNode("from bundle " + c.entity));
      row.append(line);
    }
    if (c.gen) {
      const holder = el("div", "calltrace");
      const excerpts = c.excerpts || {};
      if ((excerpts.reasoning || []).length) {
        holder.append(el("div", "bname", "Relevant reasoning"));
        for (const x of excerpts.reasoning) {
          const excerpt = el("div", "excerpt think");
          appendHighlighted(excerpt, x, out.highlight_terms);
          holder.append(excerpt);
        }
      }
      if ((excerpts.answer || []).length) {
        holder.append(el("div", "bname", "What it wrote"));
        for (const x of excerpts.answer) {
          const excerpt = el("div", "excerpt");
          appendHighlighted(excerpt, x, out.highlight_terms);
          holder.append(excerpt);
        }
      }
      const b = el("button", "btn", c.run && c.call
        ? `open the full call · run ${c.run} call ${c.call}` : "open the full call");
      b.title = "open this exact call on the Runs tab";
      b.onclick = () => {
        panel.hidden = true;
        state.run = c.run;
        state.callFlash = c.gen;
        state.callNeedle = out.needle || "";
        location.hash = "runs";
      };
      row.append(b, holder);
    } else {
      row.append(el("div", "note", "no generation id — recorded before the id was kept"));
    }
    body.append(row);
  }
}
