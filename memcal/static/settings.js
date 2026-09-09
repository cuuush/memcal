import { $, el, nf, api, toast, state } from "./core.js";

/* ----------------------------------------------------------------- settings --
   Every knob memcal has, said in words, with where its value came from printed
   underneath it. The variable name is kept beside the label rather than instead of it:
   the page is how you find the setting, `.env` is how the nightly pass reads it, and
   both have to be legible from here. */

let page = null;                      // the last /api/settings payload
const pending = new Map();            // key -> the string to save; "" means unset

function dirty() { return pending.size > 0; }

function syncBar() {
  const bar = $("#setbar");
  bar.hidden = !dirty();
  $("#setbarnote").textContent = pending.size === 1
    ? "1 unsaved change" : `${pending.size} unsaved changes`;
}

/* "Use default" has something to do when the value differs from the default, when
   there is an unsaved edit, or when the key sits in a file even though it happens to
   match. This is the negative of that, because what both callers set is `disabled`. */
function nothingToReset(s) {
  return !s.custom && !pending.has(s.key) && s.origin === "default";
}

function pendingText(s) {
  if (!pending.has(s.key)) return "";
  const to = pending.get(s.key);
  return to === "" ? `unsets it — back to ${s.default || "empty"}` : `unsaved: ${to}`;
}

function mark(key, value) {
  const setting = rowFor(key);
  if (!setting) return;
  if (value === null) pending.delete(key); else pending.set(key, value);
  const row = document.querySelector(`.setrow[data-key="${key}"]`);
  if (row) {
    row.classList.toggle("changed", pending.has(key));
    row.querySelector(".setpending").textContent = pendingText(setting);
    // Typed a value into a row that was on its default: there is something to undo now,
    // and the row is not re-rendered on every keystroke.
    row.querySelector(".setreset").disabled = nothingToReset(setting);
  }
  syncBar();
}

function rowFor(key) {
  for (const group of page?.groups || [])
    for (const setting of group.settings) if (setting.key === key) return setting;
  return null;
}

/* Where a value came from, in the words the person would use for the file. */
function originLine(s) {
  if (s.origin === "default") return "built-in default";
  if (s.origin === "environment") return "this process's environment";
  return s.origin_path;
}

function control(s) {
  const value = pending.has(s.key) ? pending.get(s.key) : s.value;
  if (s.kind === "choice") {
    const sel = el("select");
    for (const c of s.choices) sel.append(new Option(c.label, c.value));
    sel.value = value;
    sel.onchange = () => mark(s.key, sel.value);
    return sel;
  }
  if (s.kind === "bool") {
    const seg = el("div", "setseg");
    for (const [v, label] of [["on", "on"], ["off", "off"]]) {
      const b = el("button", null, label);
      b.setAttribute("aria-pressed", String((value || s.default) === v));
      b.onclick = () => { mark(s.key, v); render(); };
      seg.append(b);
    }
    return seg;
  }
  const input = el("input");
  input.type = s.kind === "int" ? "number" : "text";
  if (s.min !== null && s.min !== undefined) input.min = s.min;
  if (s.max !== null && s.max !== undefined) input.max = s.max;
  input.value = value;
  input.placeholder = s.placeholder || s.default || "";
  input.oninput = () => mark(s.key, input.value === s.value ? null : input.value);
  return input;
}

function settingRow(s) {
  const row = el("div", "setrow" + (pending.has(s.key) ? " changed" : ""));
  row.dataset.key = s.key;
  row.dataset.hay = `${s.label} ${s.key} ${s.help} ${s.attr}`.toLowerCase();

  const main = el("div", "setmain");
  const head = el("div", "setlabel");
  head.append(el("span", null, s.label), el("code", "setkey", s.key));
  if (s.custom) head.append(el("span", "setflag", "changed"));
  if (s.scope === "store") {
    const tag = el("span", "setflag store", "this store only");
    tag.title = "Read from this store's .env, never from a checkout or your shell — "
              + "which calendar gets written to is a property of the store.";
    head.append(tag);
  }
  main.append(head, el("p", "sethelp", s.help));

  const meta = el("div", "setmeta");
  meta.append(el("span", null, `default ${s.default || "empty"}`),
              el("span", null, `from ${originLine(s)}`));
  if (s.unit) meta.append(el("span", null, s.unit));
  main.append(meta);

  if (s.shadowed_by) {
    // The one way saving can appear not to work: `config.load` reads the working
    // directory's .env last, so it outranks the store's. Better said here than
    // discovered after a restart.
    main.append(el("div", "setwarn",
      `also set in ${s.shadowed_by}, which wins the next time memcal starts`));
  }

  const side = el("div", "setctl");
  side.append(control(s));
  const reset = el("button", "setreset", "use default");
  reset.disabled = nothingToReset(s);
  reset.onclick = () => { mark(s.key, ""); render(); };
  side.append(reset, el("div", "setpending", pendingText(s)));

  row.append(main, side);
  return row;
}

function renderGroups() {
  const q = ($("#setq").value || "").trim().toLowerCase();
  const onlyCustom = $("#setcustom").checked;
  const box = $("#setgroups"); box.innerHTML = "";
  let shown = 0;
  for (const group of page.groups) {
    const rows = group.settings.filter(s =>
      (!onlyCustom || s.custom || pending.has(s.key))
      && (!q || `${s.label} ${s.key} ${s.help} ${s.attr}`.toLowerCase().includes(q)));
    if (!rows.length) continue;
    shown += rows.length;
    const card = el("div", "setgroup");
    card.append(el("h3", null, group.title), el("p", "note", group.note));
    for (const s of rows) card.append(settingRow(s));
    box.append(card);
  }
  const total = page.groups.reduce((n, g) => n + g.settings.length, 0);
  $("#setcount").textContent = shown === total
    ? `${nf(total)} settings` : `${nf(shown)} of ${nf(total)}`;
  if (!shown) box.append(el("div", "empty", "nothing matches that"));
}

function renderRuntime() {
  const p = page.provider, s = page.store;
  const box = $("#setruntime"); box.innerHTML = "";
  const card = el("div", "card");

  const head = el("div", "row");
  head.append(el("span", "bname", p.name));
  const pill = el("span", "pill " + (p.ok ? "process" : ""), p.ok ? "ready" : "not ready");
  if (!p.ok) pill.style.cssText = "border-color:var(--warn);color:var(--warn)";
  const detail = el("span", "note", p.detail);
  detail.style.margin = "0";
  head.append(pill, detail);
  if (p.default_model)
    head.append(el("span", "setflag", `provider default model: ${p.default_model}`));
  card.append(head);
  if (!p.ok) {
    card.append(el("div", "setwarn", p.needs_key
      ? "OpenRouter needs an API key — set OPENROUTER_API_KEY under Credentials below."
      : "The provider is chosen but its command is not on this process's PATH. An "
        + "absolute path in the executable field below is what the nightly agent needs "
        + "anyway."));
  }

  const grid = el("div", "setpaths");
  const rows = [["store", s.home], ["database", `${s.db}  ·  ${nf(Math.round(s.db_bytes / 1024))} KB`],
                ["brief", s.brief], ["wiki", s.wiki], ["plugins", s.plugins],
                ["settings file", page.env_file]];
  for (const [k, v] of rows) {
    grid.append(el("div", "setpathk", k), el("div", "setpathv", v));
  }
  card.append(grid);
  box.append(card);
  $("#setfile").textContent = page.env_file;
}

function renderFiles() {
  const box = $("#setfiles"); box.innerHTML = "";
  const card = el("div", "card");
  card.append(el("p", "note",
    "memcal reads these in order, and the first one that sets a key wins. memcal only "
    + "ever writes the store's own file."));
  for (const f of page.files) {
    const row = el("div", "setfile");
    row.append(el("span", "setflag" + (f.role === "store" ? " store" : ""), f.role));
    row.append(el("code", "setpathv", f.path));
    row.append(el("span", "note", f.exists
      ? `${nf(f.keys)} memcal setting${f.keys === 1 ? "" : "s"}` : "does not exist"));
    card.append(row);
  }
  box.append(card);
}

function renderCredentials() {
  const box = $("#setcreds"); box.innerHTML = "";
  if (!page.credentials.length) {
    box.append(el("div", "empty", "no source on this machine asks for a credential"));
    return;
  }
  const card = el("div", "card");
  for (const c of page.credentials) {
    const row = el("div", "setrow");
    const main = el("div", "setmain");
    const head = el("div", "setlabel");
    head.append(el("code", "setkey", c.name));
    head.append(el("span", "setflag" + (c.present ? " ok" : ""),
                  c.present ? "set" : "not set"));
    main.append(head, el("p", "sethelp", `used by ${c.used_by.join(", ")}`));

    const side = el("div", "setctl");
    const input = el("input");
    input.type = "password";
    input.autocomplete = "off";
    input.placeholder = c.present ? "replace it" : "paste it here";
    const save = el("button", "setreset", "save");
    save.onclick = () => saveSecret(c.name, input.value, input);
    input.onkeydown = e => { if (e.key === "Enter") saveSecret(c.name, input.value, input); };
    side.append(input, save);
    if (c.present) {
      const clear = el("button", "setreset", "clear");
      clear.onclick = () => saveSecret(c.name, "", input);
      side.append(clear);
    }
    row.append(main, side);
    card.append(row);
  }
  box.append(card);
}

async function saveSecret(name, value, input) {
  const out = await api("/api/settings", {secret: {name, value}});
  if (out.error) return;
  input.value = "";
  page = out;
  toast(out.secret.present ? `${name} saved` : `${name} cleared`);
  render();
  loadProbe();          // a credential is usually the reason a source was not usable
}

function renderProbe(probe) {
  const box = $("#setsources"); box.innerHTML = "";
  const card = el("div", "card");
  for (const s of probe.sources) {
    const row = el("div", "setrow");
    const main = el("div", "setmain");
    const head = el("div", "setlabel");
    head.append(el("span", null, s.name));
    const pill = el("span", "setflag" + (s.usable ? " ok" : " bad"),
                   s.usable ? "usable" : "not usable");
    head.append(pill);
    if (!s.in_all) {
      const tag = el("span", "setflag", "not in `ingest all`");
      tag.title = "Skipped by a plain collect — it is slow, interactive, or covered by "
                + "another source.";
      head.append(tag);
    }
    main.append(head, el("p", "sethelp", s.description));
    const meta = el("div", "setmeta");
    meta.append(el("span", null, s.detail));
    if (s.secrets.length) meta.append(el("span", null, `wants ${s.secrets.join(", ")}`));
    main.append(meta);
    row.append(main, el("div", "setctl"));
    card.append(row);
  }
  for (const problem of probe.load_errors || [])
    card.append(el("div", "setwarn", problem));
  card.append(el("p", "note", `plugins: ${probe.plugin_dir} — drop a .py in there and `
    + "it becomes a source"));
  box.append(card);

  const st = probe.schedule || {};
  const sbox = $("#setschedule"); sbox.innerHTML = "";
  const scard = el("div", "card");
  if (st.error) {
    scard.append(el("p", "note", `could not read the nightly agent: ${st.error}`));
  } else if (!st.installed) {
    scard.append(el("p", "note",
      "Not installed. memcal only dreams when something runs it — `memcal schedule "
      + "install` sets up the nightly agent, and a missed pass runs at the next wake-up."));
  } else {
    const at = st.at ? `${String(st.at[0]).padStart(2, "0")}:${String(st.at[1]).padStart(2, "0")}` : "?";
    const grid = el("div", "setpaths");
    const rows = [["state", st.loaded ? `loaded · daily at ${at}` : "INSTALLED BUT NOT LOADED"],
                  ["next", st.next || "—"], ["last pass", st.why || "—"],
                  ["wakes", `every ${Math.round((st.every || 0) / 60)} min, at login, and on wake`],
                  ["script", st.script], ["plist", st.plist]];
    if (st.last_exit !== null && st.last_exit !== undefined)
      rows.push(["last exit", String(st.last_exit)]);
    for (const [k, v] of rows) grid.append(el("div", "setpathk", k), el("div", "setpathv", v));
    scard.append(grid);
    if (st.warning) scard.append(el("div", "setwarn", st.warning));
    if (st.tail) {
      const log = el("details", "raw");
      log.append(el("summary", null, `tail of ${st.log}`), el("pre", "log", st.tail));
      scard.append(log);
    }
  }
  sbox.append(scard);
}

function render() {
  if (!page) return;
  renderRuntime();
  renderGroups();
  renderFiles();
  renderCredentials();
  syncBar();
}

async function saveChanges() {
  const changes = Object.fromEntries(pending);
  $("#setsave").disabled = true;
  const out = await api("/api/settings", {changes});
  $("#setsave").disabled = false;
  if (out.error) return;                       // the form keeps everything the person typed
  pending.clear();
  page = out;
  toast(`saved ${out.saved.length} setting${out.saved.length === 1 ? "" : "s"}`);
  for (const warning of out.warnings || []) toast(warning);
  render();
  loadProbe();          // the provider, and so what is usable, may have just changed
}

$("#setsave").onclick = saveChanges;
$("#setdiscard").onclick = () => { pending.clear(); render(); };
let setTimer;
$("#setq").oninput = () => { clearTimeout(setTimer); setTimer = setTimeout(renderGroups, 160); };
$("#setcustom").onchange = renderGroups;
// A tab switch with unsaved edits would drop them silently, and the browser's own
// warning only covers leaving the page.
addEventListener("hashchange", () => {
  if (dirty() && location.hash.slice(1) !== "settings")
    toast(`${pending.size} unsaved setting change${pending.size === 1 ? "" : "s"} — `
          + "they are still there on the Settings tab");
});

/* The second half can open a socket (a source check) or start a subprocess (launchctl),
   so it is a second request: the page is drawn and usable before it is asked for. */
async function loadProbe() {
  const probe = await api("/api/settings_probe");
  if (!probe.error && state.view === "settings") renderProbe(probe);
}

export async function loadSettings() {
  const out = await api("/api/settings");
  if (out.error) return;
  page = out;
  render();
  loadProbe();
}
