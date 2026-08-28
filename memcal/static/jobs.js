import { el, api, toast } from "./core.js";

/* Collect and dream both outlive their request, so poll the job the server hands back. */
const watchedJobs = new Set();
export async function runJob(path, button, logEl, done) {
  const {job, error} = await api(path, {});
  if (error) { toast(error); return; }
  if (!job) return;
  watchJob(job, button, logEl, done, true);
}

export function watchJob(job, button, logEl, done, clearLog = false) {
  if (watchedJobs.has(job)) return;
  watchedJobs.add(job);
  button.disabled = true;
  logEl.hidden = false;
  if (clearLog) logEl.textContent = "";
  const bar = logEl.previousElementSibling?.classList.contains("prog")
    ? logEl.previousElementSibling : null;
  if (bar) { bar.hidden = false; bar.innerHTML = ""; bar._lanes = null; bar._count = null; }

  let finished = false;
  const draw = s => {
    if (bar) renderProgress(bar, s);
    logEl.textContent = (s.lines || []).join("\n");
    logEl.scrollTop = logEl.scrollHeight;
  };
  const finish = async s => {
    if (finished) return;
    finished = true;
    watchedJobs.delete(job);
    button.disabled = false;
    if (s.error) toast(s.error);
    await done(s);
  };

  /* Stream, so a bar moves when the pipeline moves rather than on the next tick of a
     timer. The poller stays as the fallback: EventSource may be unavailable, and the
     stream can be cut by anything between here and the server. */
  let stream = null;
  try { stream = new EventSource("/api/job/stream?id=" + encodeURIComponent(job)); }
  catch (e) { stream = null; }

  const poll = async () => {
    if (finished) return;
    const s = await api("/api/job?id=" + encodeURIComponent(job));
    draw(s);
    if (!s.done) return setTimeout(poll, 700);
    await finish(s);
  };

  if (!stream) { poll(); return; }
  stream.onmessage = ev => {
    const s = JSON.parse(ev.data);
    draw(s);
    if (s.done) { stream.close(); finish(s); }
  };
  stream.onerror = () => {
    // A closed stream after the job finished is the normal ending, not a failure.
    stream.close();
    if (!finished) poll();
  };
}
/* How a step that cannot count itself is drawn.

   `sweep` is one model call over the whole resulting state, and `resolve` is one call per
   disagreement: there is no fraction inside `client.complete` for the server to report,
   and inventing a denominator for it would be a bar that lies. What the server does know
   is how long the step has been running, so the bar is drawn from that on a curve that
   slows as it goes and stops short of the end — it can say "this is taking a while" and
   it can never say "done". CREEP_SECONDS is where it passes ~63%: a sweep is usually ten
   to twenty seconds, and a bar that hits the wall in three would be back to telling you
   nothing.

   Drawn dimmer than a counted bar, and with the seconds printed beside it, because the
   two are not the same claim and the lane should not pretend they are. */
const CREEP_SECONDS = 20;
const CREEP_CEILING = 94;
let creepTimer = null;

function paintElapsed(lane) {
  const seconds = Math.max(0, (performance.now() - lane.startedAt) / 1000);
  lane.fill.style.width =
    Math.round(CREEP_CEILING * (1 - Math.exp(-seconds / CREEP_SECONDS))) + "%";
  const clock = `${Math.round(seconds)}s`;
  lane.note.textContent = lane.noteText ? `${lane.noteText} · ${clock}` : clock;
}

/* One timer for the page, not one per lane, and it stops itself the moment the last
   uncounted step finishes — a run is minutes and this outlives every frame of it. */
function keepCreeping() {
  if (creepTimer) return;
  creepTimer = setInterval(() => {
    const bars = document.querySelectorAll(".pbar.elapsed");
    if (!bars.length) { clearInterval(creepTimer); creepTimer = null; return; }
    bars.forEach(bar => { if (bar._lane) paintElapsed(bar._lane); });
  }, 400);
}

/* One row per source, so a slow one is visibly the slow one rather than a hung page.
   Rows are rebuilt in place rather than re-created: replacing the node restarts the CSS
   width transition from zero on every frame, which is what made the bars stutter
   instead of grow. */
function renderProgress(box, s) {
  const steps = s.steps || [];
  if (!steps.length) { box.innerHTML = ""; return; }
  if (!box._lanes) { box.innerHTML = ""; box._lanes = new Map(); }
  const overall = el("div", "pcount");
  for (const st of steps) {
    let lane = box._lanes.get(st.name);
    if (!lane) {
      lane = el("div", "plane");
      lane.name = el("span", "pname", st.name);
      lane.phase = el("span", "pphase");
      lane.bar = el("div", "pbar");
      lane.fill = el("i");
      lane.bar._lane = lane;                 // the creep timer walks bars, not lanes
      lane.bar.append(lane.fill);
      lane.note = el("span", "pnote");
      lane.append(lane.name, lane.phase, lane.bar, lane.note);
      box.append(lane);
      box._lanes.set(st.name, lane);
    }
    lane.className = "plane " + (st.state || "waiting");
    // The phase is the 1-3 words; the sentence goes on the right where it can be
    // ignored. "reading mail" beside a moving bar answers the question a spinner
    // does not: which of the five is this, and is it stuck?
    lane.phase.textContent = st.phase || (st.state === "waiting" ? "queued" : st.state);
    const counted = st.total > 0;
    const creeping = !counted && st.state === "running";
    lane.bar.classList.toggle("elapsed", creeping);
    lane.noteText = st.note || "";
    lane.note.title = st.note || "";
    if (creeping) {
      // Anchored to the server's own elapsed seconds, so a page opened halfway through
      // a run picks the bar up where the run actually is rather than at zero.
      lane.startedAt = performance.now() - (st.running_for || 0) * 1000;
      paintElapsed(lane);
      keepCreeping();
    } else {
      lane.fill.style.width = counted
        ? Math.max(2, Math.min(100, 100 * st.done / st.total)) + "%"
        : (st.state === "waiting" ? "0%" : "100%");
      lane.note.textContent = lane.noteText;
    }
  }
  overall.textContent = `${s.finished || 0}/${s.total || steps.length} finished`;
  if (box._count) box._count.textContent = overall.textContent;
  else { box._count = overall; box.append(overall); }
}
