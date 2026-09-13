import { $, el, nf, api, state } from "./core.js";
import { renderWikiProfile } from "./memory.js";

/* -------------------------------------------------------------- wiki -- */
/* The wiki was reachable only by opening an event that happened to link a page. It is a
   store in its own right — what memcal knows about the people, places and series in their
   world — so it gets a tab: a directory on the left, one page opened on the right, with
   every fact's source one click away. */
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
  for (const page of pages) {
    const card = el("button", "wikicard" + (page.slug === state.wikiSlug ? " on" : ""));
    card.dataset.slug = page.slug;
    const top = el("div", "wikicardtop");
    top.append(el("span", "wikicardname", page.title));
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
    list.append(card);
  }
  // Reopen whatever was open before a search re-render, so the pane does not blank out
  // from under a click.
  if (state.wikiSlug && pages.some(p => p.slug === state.wikiSlug)) openPage(state.wikiSlug);
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
  renderWikiProfile(page, detail);
}

let wTimer;
$("#wq").oninput = e => { clearTimeout(wTimer); wTimer = setTimeout(() => {
  state.wq = e.target.value.trim(); loadWiki(); }, 220); };
