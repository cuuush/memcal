import { $, el, nf, api, state } from "./core.js";
import { renderWikiProfile } from "./memory.js";

/* -------------------------------------------------------------- wiki -- */
/* The wiki is markdown files on disk — people, places, projects — with the user's
   own page pinned at the top. Each page opens on the right with every fact beside
   the exact line it was read from, and each fact editable in place. */
const SECTION_ORDER = ["people", "places", "projects"];
const SECTION_LABEL = {people: "People", places: "Places", projects: "Projects"};

export async function loadWiki() {
  const q = state.wq || "";
  const out = await api("/api/wiki_pages" + (q ? "?q=" + encodeURIComponent(q) : ""));
  const list = $("#wikilist"); list.innerHTML = "";
  const pages = out.pages || [];
  $("#wcount").textContent = out.error ? "" :
    `${nf(pages.length)} page${pages.length === 1 ? "" : "s"}`;
  if (!pages.length) {
    list.append(el("div", "empty", q ? "no page matches that" : "no wiki pages yet"));
    return;
  }
  const self = pages.filter(p => p.is_self);
  const rest = pages.filter(p => !p.is_self);
  for (const page of self) list.append(card(page, true));
  // One group per section so people, places and projects read as three lists.
  for (const section of SECTION_ORDER) {
    const group = rest.filter(p => (p.section || "people") === section);
    if (!group.length) continue;
    // A search already filters; repeating the header per keystroke adds noise.
    if (!q) list.append(el("div", "wikigroup", SECTION_LABEL[section]));
    for (const page of group) list.append(card(page, false));
  }
  // Anything a legacy file still claims (never written anymore) lands last
  // rather than silently vanishing.
  for (const page of rest.filter(p => !SECTION_ORDER.includes(p.section || "people")))
    list.append(card(page, false));
  // Reopen whatever was open before a search re-render, so the pane does not blank out
  // from under a click.
  if (state.wikiSlug && pages.some(p => p.slug === state.wikiSlug)) openPage(state.wikiSlug);
}

function card(page, isSelf) {
  const card = el("button", "wikicard" + (page.slug === state.wikiSlug ? " on" : "")
    + (isSelf ? " self" : ""));
  card.dataset.slug = page.slug;
  const top = el("div", "wikicardtop");
  top.append(el("span", "wikicardname", page.title));
  if (isSelf) top.append(el("span", "youbadge", "you"));
  top.append(el("span", "wikisection", page.section));
  card.append(top);
  // What the page is for, not just its name — the same slot list the brief indexes.
  if (page.answers && page.answers.length)
    card.append(el("div", "wikicardans", page.answers.join(" · ")));
  const facts = `${page.facts} fact${page.facts === 1 ? "" : "s"}`;
  const bits = [facts];
  if (page.questions) bits.push(`${page.questions} open`);
  if ((page.aliases || []).length) bits.push(`${page.aliases.length} alias${page.aliases.length === 1 ? "" : "es"}`);
  card.append(el("div", "wikicardmeta", bits.join(" · ")));
  card.onclick = () => openPage(page.slug);
  return card;
}

async function openPage(slug) {
  state.wikiSlug = slug;
  document.querySelectorAll("#wikilist .wikicard").forEach(c =>
    c.classList.toggle("on", c.dataset.slug === slug));
  const detail = $("#wikidetail");
  detail.innerHTML = '<div class="empty">opening…</div>';
  const page = await api("/api/wiki?slug=" + encodeURIComponent(slug));
  detail.innerHTML = "";
  if (page.error) { detail.append(el("div", "empty", page.error)); return; }
  renderWikiProfile(page, detail, () => openPage(slug));
}

let wTimer;
$("#wq").oninput = e => { clearTimeout(wTimer); wTimer = setTimeout(() => {
  state.wq = e.target.value.trim(); loadWiki(); }, 220); };
