// Background service worker — the batch brain + system of record.
// Per-job state lives in chrome.storage.local keyed by a STABLE jobId (rrJobId), so a tab
// reload / SPA-nav back to the same posting re-attaches instead of restarting. The worker
// can be evicted at any time, so storage (not memory) is the source of truth — including the
// batch run itself (rr_batch): on SW restart mid-batch we resume from wherever storage says
// we were, and Stop survives eviction because the cancel flag is mirrored to storage.

importScripts("jd_detect.js");   // for rrJobId (pure function, no DOM use on this path)

const STORE = "rr_jobs";
const BATCH = "rr_batch";
const KEEPALIVE = "rr-keepalive";
// A SEPARATE alarm from the batch's: a paste-JD run and a batch can overlap, and clearing a shared
// alarm at the end of one would silently stop keeping the other alive.
const KEEPALIVE_JD = "rr-keepalive-jd";
const PENDING_JD = "rr_pending_jd";   // durable, so an evicted worker can finish the job
const LEARN_LOG = "rr_learn_log";

chrome.sidePanel.setPanelBehavior({ openPanelOnActionClick: true }).catch((e) => console.error(e));

async function getJobs() {
  return (await chrome.storage.local.get(STORE))[STORE] || {};
}
async function setJob(patch) {
  const jobs = await getJobs();
  jobs[patch.jobId] = { ...(jobs[patch.jobId] || {}), ...patch, updatedAt: Date.now() };
  await chrome.storage.local.set({ [STORE]: jobs });
  return jobs[patch.jobId];
}
async function getBatch() {
  return (await chrome.storage.local.get(BATCH))[BATCH] || { active: false };
}
async function setBatch(patch) {
  const b = { ...(await getBatch()), ...patch };
  await chrome.storage.local.set({ [BATCH]: b });
  return b;
}

async function exec(tabId, func, args = []) {
  const [r] = await chrome.scripting.executeScript({ target: { tabId }, func, args });
  return r?.result;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const API = "http://127.0.0.1:8765";

// Loaded here, not at the top: filepicker.js closes over API, STORE and rrJobId.
// upload_match.js first — filepicker's auto-attach calls classifyUploadArea/chooseUploadTarget.
importScripts("upload_match.js");
importScripts("filepicker.js");

let cancelBatch = false;     // in-memory fast path; the durable copy lives in rr_batch.cancel
let batchAbort = null;       // aborts the in-flight fetch on Stop

async function api(path, body) {
  const r = await fetch(API + path, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body), signal: batchAbort ? batchAbort.signal : undefined,
  });
  if (!r.ok) {
    // The backend answers setup problems (empty profile, bad skills file) with a readable
    // message; "HTTP 400" on its own sends people hunting through the server log for it.
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail || body.error || path + " HTTP " + r.status);
  }
  return r.json();
}
const isAbort = (e) => e && (e.name === "AbortError" || cancelBatch);
async function cancelled() {
  return cancelBatch || (await getBatch()).cancel === true;
}

// Find the JD on a tab. Click in-place EXPANDERS eagerly (keep the longer text), then if still
// not found, click navigational REVEALs up to twice.
async function findJDWithNav(tabId) {
  let res = await exec(tabId, () => rrFindJD());
  // The page may still be rendering the JD (SPA / lazy content) when the batch reaches it —
  // the manual reader works because you open it once the page has settled. Give it the same
  // chance: a couple of short retries before deciding the JD isn't there.
  for (let i = 0; i < 3 && !res.found; i++) {
    await sleep(1000);
    res = await exec(tabId, () => rrFindJD());
  }
  let navigated = false;
  for (let i = 0; i < 2; i++) {                       // in-place "Show more" / "See more"
    const cands = await exec(tabId, () => rrFindJDCandidates());
    const exp = (cands || []).find((c) => c.kind === "expand");
    if (!exp) break;
    await exec(tabId, (idx) => rrClickJDCandidate(idx), [exp.idx]);
    await sleep(1200);
    const r2 = await exec(tabId, () => rrFindJD());
    navigated = true;
    if ((r2.text || "").length > (res.text || "").length) res = r2;
    if (res.found && (res.text || "").length > 1500) break;
  }
  let tries = 0;                                       // navigate to a description tab/link
  while (!res.found && tries < 2) {
    const cands = await exec(tabId, () => rrFindJDCandidates());
    const rev = (cands || []).find((c) => c.kind === "reveal");
    if (!rev) break;
    await exec(tabId, (idx) => rrClickJDCandidate(idx), [rev.idx]);
    await sleep(1300);
    res = await exec(tabId, () => rrFindJD());
    navigated = true; tries++;
  }
  return { ...res, navigated };
}

// Full per-tab pipeline: read JD (with navigation) -> analyze -> resume PDF -> cover letter.
// Writes status to storage at each step so the side panel shows live progress. Reload-safe:
// resumes from wherever the stored job left off (a job with a resume but no cover letter
// re-enters at the cover-letter step).
async function processTab(tab) {
  await chrome.scripting.executeScript({ target: { tabId: tab.id }, files: ["jd_detect.js"] });
  const jobId = await exec(tab.id, () => rrJobId(location.href));
  const host = (() => { try { return new URL(tab.url).hostname; } catch { return ""; } })();
  const existing = (await getJobs())[jobId] || {};
  if (existing.status === "done") return { ...existing, skipped: true };   // fully done

  await setJob({ jobId, tabId: tab.id, url: tab.url, host, title: tab.title || host,
    flagged: false, reason: "" });

  // 1) JD
  let jd_text = existing.jd_text;
  if (!jd_text && !existing.resume_id) {
    await setJob({ jobId, status: "reading" });
    const res = await findJDWithNav(tab.id);
    if (!res.found) {
      return setJob({ jobId, status: "flagged", flagged: true,
        reason: "JD not found" + (res.navigated ? " (navigated)" : "") });
    }
    // Cap what we persist. chrome.storage.local is 10MB without unlimitedStorage, and a full JD is
    // 5-20KB; a few hundred jobs is enough to hit the ceiling, at which point EVERY set() starts
    // failing — batch state stops persisting, the cached profile can't be written (which silently
    // kills the inline suggestion chips), and the whole extension looks broken for no visible
    // reason. The stored copy is only used for re-analysis and the JD preview, so 20k is plenty.
    jd_text = (res.text || "").slice(0, 20000);
    await setJob({ jobId, status: "jd-read", jd_text, title: res.title || tab.title });
  }

  // 2) analyze
  let analysis = existing.jd_analysis;
  if (!analysis && !existing.resume_id) {
    await setJob({ jobId, status: "analyzing" });
    try { analysis = await api("/jd/analyze", { jd_text }); }
    catch (e) {
      if (isAbort(e)) return setJob({ jobId, status: "stopped", flagged: false, reason: "stopped" });
      return setJob({ jobId, status: "flagged", flagged: true, reason: "analyze failed: " + e.message });
    }
    await setJob({ jobId, jd_analysis: analysis, company: analysis.company || "", role: analysis.role_title || "" });
  }

  // 3) resume
  let trackerId = existing.resume_id;
  if (!trackerId) {
    await setJob({ jobId, status: "making-resume" });
    try {
      const angle = ((analysis || {}).angle_ideas || ["General"])[0];
      const pdf = await api("/resume/generate-pdf", { jd_analysis: { ...analysis, jd_text }, focus_angle: angle });
      trackerId = pdf.tracker_id;
      await setJob({ jobId, status: "resume-done", resume_id: trackerId, pdf_path: pdf.pdf_path });
    } catch (e) {
      if (isAbort(e)) return setJob({ jobId, status: "stopped", flagged: false, reason: "stopped" });
      return setJob({ jobId, status: "flagged", flagged: true, reason: "resume failed: " + e.message });
    }
  }

  // 4) cover letter
  await setJob({ jobId, status: "making-cover-letter" });
  try {
    const cl = await api("/cover-letter/generate", { tracker_id: trackerId });
    if (cl.error) throw new Error(cl.error);
    return setJob({ jobId, status: "done", cover_letter_path: cl.pdf_path });
  } catch (e) {
    if (isAbort(e)) return setJob({ jobId, status: "stopped", flagged: false, reason: "stopped" });
    return setJob({ jobId, status: "flagged", flagged: true, reason: "cover letter failed: " + e.message });
  }
}

// Recovery path for a job whose JD couldn't be auto-detected: the user pastes the JD text in
// the panel, and we run the rest of the pipeline (analyze -> resume -> cover letter) with it.
// No tab needed — once we have the JD text the page is irrelevant.
async function runFromJD(jobId, jd_text, attempt = 0) {
  // Résumé generation takes 35-40s. An MV3 service worker is terminated after ~30s idle, so
  // without a keepalive this reliably died PART WAY THROUGH: the job showed "making-resume",
  // the worker was killed, and nothing ever finished or appeared in Docs. runBatch has always
  // had this alarm; this path never did.
  chrome.alarms.create(KEEPALIVE_JD, { periodInMinutes: 0.4 });
  await chrome.storage.local.set({ [PENDING_JD]: { jobId, jd_text, attempt, at: Date.now() } });
  try {
    return await runFromJDInner(jobId, jd_text);
  } finally {
    chrome.alarms.clear(KEEPALIVE_JD);
    await chrome.storage.local.remove(PENDING_JD);
  }
}

async function runFromJDInner(jobId, jd_text) {
  const existing = (await getJobs())[jobId] || {};
  await setJob({ jobId, jd_text, status: "jd-read", flagged: false, reason: "" });

  let analysis = existing.jd_analysis;
  if (!analysis && !existing.resume_id) {
    await setJob({ jobId, status: "analyzing" });
    try { analysis = await api("/jd/analyze", { jd_text }); }
    catch (e) { return setJob({ jobId, status: "flagged", flagged: true, reason: "analyze failed: " + e.message }); }
    await setJob({ jobId, jd_analysis: analysis, company: analysis.company || "", role: analysis.role_title || "" });
  }

  let trackerId = existing.resume_id;
  if (!trackerId) {
    await setJob({ jobId, status: "making-resume" });
    try {
      const angle = ((analysis || {}).angle_ideas || ["General"])[0];
      const pdf = await api("/resume/generate-pdf", { jd_analysis: { ...analysis, jd_text }, focus_angle: angle });
      trackerId = pdf.tracker_id;
      await setJob({ jobId, status: "resume-done", resume_id: trackerId, pdf_path: pdf.pdf_path });
    } catch (e) { return setJob({ jobId, status: "flagged", flagged: true, reason: "resume failed: " + e.message }); }
  }

  await setJob({ jobId, status: "making-cover-letter" });
  try {
    const cl = await api("/cover-letter/generate", { tracker_id: trackerId });
    if (cl.error) throw new Error(cl.error);
    return setJob({ jobId, status: "done", cover_letter_path: cl.pdf_path });
  } catch (e) { return setJob({ jobId, status: "flagged", flagged: true, reason: "cover letter failed: " + e.message }); }
}

// A batch of 40 tabs is ~an hour of continuous LLM work, so there's an optional wall-clock budget.
// The deadline is an ABSOLUTE timestamp kept in rr_batch, not a duration counted in memory — that
// way a batch resumed after the service worker is evicted still stops at the original time instead
// of restarting the clock and running twice as long.
async function runBatch({ windowId, tabIds, limitMs, deadline } = {}) {
  cancelBatch = false;
  batchAbort = new AbortController();
  const dl = deadline || (limitMs > 0 ? Date.now() + limitMs : null);
  await setBatch({ active: true, cancel: false, windowId: windowId || null,
    tabIds: tabIds || null, startedAt: Date.now(), deadline: dl, timedOut: false });
  chrome.alarms.create(KEEPALIVE, { periodInMinutes: 0.4 });
  // Keep suggestion chips alive while the batch owns the worker — refresh the cached profile
  // up front so content scripts aren't left with an empty rr_profile if the SW was cold.
  refreshProfileToStorage().catch(() => {});

  let done = 0, flagged = 0, stopped = false, timedOut = false;
  const outOfTime = () => !!dl && Date.now() >= dl;
  try {
    let tabs;
    if (tabIds && tabIds.length) {                 // scoped run: only the tabs we just opened
      tabs = [];
      for (const id of tabIds) { try { tabs.push(await chrome.tabs.get(id)); } catch (e) {} }
    } else {
      tabs = await chrome.tabs.query(windowId ? { windowId } : {});
    }
    tabs = tabs.filter((t) => /^https?:/.test(t.url || ""));
    for (const t of tabs) {
      if (await cancelled()) { stopped = true; break; }
      // Checked BEFORE starting a job, never mid-job: a half-written résumé is worse than a
      // slightly-over-budget run, and each job is only ~60-90s. The alarm below is the hard stop.
      if (outOfTime()) { stopped = true; timedOut = true; break; }
      try {
        const j = await processTab(t);
        if (j && j.status === "done") done++;
        if (j && j.flagged) flagged++;
        if (j && j.status === "stopped") { stopped = true; break; }
        // Drop the tab's renderer from RAM after we're done with it. The tab stays in the strip
        // so you can still open it to apply — but 20 live Greenhouse/Workday pages were measured
        // pushing this 24GB Mac into swap and cutting gemma from ~26 tok/s to ~12 tok/s (≈2×
        // slower generations). discard() is the difference between a 71s job and a 140s job
        // mid-batch; it does not close the tab.
        if (j && j.status !== "stopped" && t.id) {
          try { await chrome.tabs.discard(t.id); } catch (e) {}
        }
      } catch (e) {
        if (isAbort(e)) { stopped = true; break; }
        flagged++;
        await setJob({ jobId: "err:" + (t.url || t.id), tabId: t.id, url: t.url,
          title: t.title || "tab", status: "flagged", flagged: true,
          reason: ("cannot process: " + e.message).slice(0, 90) });
        try { if (t.id) await chrome.tabs.discard(t.id); } catch (err) {}
      }
    }
  } finally {
    chrome.alarms.clear(KEEPALIVE);
    // The watchdog alarm may have been the one that stopped us, in which case it recorded the
    // reason in storage rather than in this closure's variable.
    const cur = await getBatch();
    const ranOutOfTime = timedOut || !!cur.timedOut;
    await setBatch({ active: false, cancel: false, deadline: null, timedOut: false,
      finished: { done, flagged, stopped: stopped || ranOutOfTime,
                  timedOut: ranOutOfTime, endedAt: Date.now() } });
  }
}

// ---------------- passive learning ----------------
// learn.js reports every finished form field on every page; this decides what's worth
// keeping: the page must look job-application-related (known ATS host, or a host/posting
// we've already read a JD from), and the value must not be an identity basic the profile
// map already answers. Saves go to the semantic Q&A memory; failures (backend off) drop
// silently — learning simply happens whenever the server is on.
const ATS_HOSTS = /greenhouse|lever\.co|myworkday|workday|ashbyhq|icims|taleo|smartrecruiters|jobvite|bamboohr|workable|recruitee|breezy\.hr|dover\.com|rippling|adp\.com|successfactors|oraclecloud|eightfold|phenom|jazzhr|paylocity|paycom|ultipro|ukg\.com|dayforce|greenhouse\.io|jobs\./i;

// Cache the profile into chrome.storage so content scripts (suggest.js) can read it directly —
// no fragile message round-trip (the SW can be evicted mid-request → "message channel closed").
async function refreshProfileToStorage() {
  try {
    const p = await (await fetch(API + "/profile")).json();
    if (p && p.identity) await chrome.storage.local.set({ rr_profile: p });
    return p;
  } catch (e) { return null; }
}
refreshProfileToStorage();   // warm it on service-worker start

let _known = { at: 0, set: new Set() };
async function knownIdentityValues() {
  if (Date.now() - _known.at < 5 * 60 * 1000) return _known.set;
  try {
    const p = await (await fetch(API + "/profile")).json();
    const id = p.identity || {};
    const vals = [id.legal_name, id.email, id.phone, id.street_address, id.zip, id.location,
      id.linkedin, id.github, id.portfolio, ...(id.legal_name || "").split(/\s+/)];
    _known = { at: Date.now(), set: new Set(vals.filter(Boolean).map((v) => String(v).toLowerCase())) };
  } catch (e) { _known.at = Date.now() - 4 * 60 * 1000; }   // backend off — retry in ~1 min
  return _known.set;
}

const ESSAY_LABEL = /\bwhy\b|describe|tell us|tell me|in your own words|cover letter|reason for|additional (information|comments|details)|anything else|elaborate|please explain|motivation|essay|what makes you/i;

async function maybeLearn(msg, sender) {
  const url = msg.url || (sender && sender.url) || "";
  let host = "";
  try { host = new URL(url).hostname; } catch (e) { return; }
  const jobs = await getJobs();
  const jobby = ATS_HOSTS.test(host) || !!jobs[rrJobId(url)] ||
    Object.values(jobs).some((j) => j.host === host);
  if (!jobby) return;

  // reusable info only — skip essays/custom prompts (Ask writes those; they don't generalize)
  if (msg.field_type === "textarea" || ESSAY_LABEL.test(msg.label || "")) return;
  if (msg.field_type === "text" && (msg.value || "").length > 120) return;

  const known = await knownIdentityValues();
  if (known.has(msg.value.toLowerCase())) return;

  try {
    const r = await fetch(API + "/qa/save", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: msg.label, answer: msg.value, field_type: msg.field_type }),
    });
    if (!r.ok) return;
    const log = (await chrome.storage.local.get(LEARN_LOG))[LEARN_LOG] || [];
    log.unshift({ label: msg.label.slice(0, 60), value: msg.value.slice(0, 70), at: Date.now() });
    await chrome.storage.local.set({ [LEARN_LOG]: log.slice(0, 8) });
  } catch (e) { /* backend off — nothing learned this time */ }
}

async function stopBatch() {
  cancelBatch = true;
  batchAbort && batchAbort.abort();
  await setBatch({ cancel: true });   // survives SW eviction — a resumed batch sees it too
}

// If the worker was evicted (or Chrome restarted) mid-batch, pick the batch back up.
// processTab skips finished jobs and re-enters partial ones, so this is idempotent.
// A paste-JD run that was cut short (worker evicted, Chrome restarted) picks up where it left off.
// runFromJDInner already skips whatever is done — it keeps existing jd_analysis and resume_id — so
// resuming costs only the step that didn't finish.
(async () => {
  try {
    const pend = (await chrome.storage.local.get(PENDING_JD))[PENDING_JD];
    if (pend && pend.jobId && (pend.attempt || 0) < 2 && Date.now() - (pend.at || 0) < 30 * 60 * 1000) {
      runFromJD(pend.jobId, pend.jd_text, (pend.attempt || 0) + 1)
        .catch((e) => console.error("paste-JD resume failed", e));
    } else if (pend) {
      await chrome.storage.local.remove(PENDING_JD);   // too old or tried enough: stop looping
    }
  } catch (e) {}
})();

(async () => {
  const b = await getBatch();
  if (b.active) {
    if (b.cancel) await setBatch({ active: false, cancel: false });
    else runBatch({ windowId: b.windowId, tabIds: b.tabIds, deadline: b.deadline })
      .catch((e) => console.error("batch resume failed", e));
  }
})();

// Keepalive tick (~every 24s) doubles as the deadline watchdog. The in-loop check above can't fire
// while a single job is mid-flight, so this is what actually enforces the limit on a job that hangs:
// stopBatch aborts the in-flight request too.
chrome.alarms.onAlarm.addListener(async () => {
  try {
    const b = await getBatch();
    if (b.active && b.deadline && Date.now() >= b.deadline && !b.cancel) {
      await setBatch({ timedOut: true });
      await stopBatch();
    }
  } catch (e) { /* storage unavailable on this tick — the next one retries */ }
});

// Injected into the PAGE's world. Sets the value through the native setter (so React's private
// value tracker goes stale and the change actually registers), then calls the input's own React
// onChange AND onBlur — the latter is what commits fields with client-side validation, such as the
// URL boxes in Workday's Websites section. Self-contained: no outer references.
function mainWorldReactFill(value) {
  const el = document.querySelector('[data-rr-fill="1"]');
  if (!el) return { ok: false, why: "element not found in this frame" };
  const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  Object.getOwnPropertyDescriptor(proto, "value").set.call(el, value);
  let props = null;
  for (const k in el) {
    if (k.indexOf("__reactProps") === 0 || k.indexOf("__reactEventHandlers") === 0) { props = el[k]; break; }
  }
  const called = [];
  for (const h of ["onChange", "onInput", "onBlur"]) {
    if (props && typeof props[h] === "function") {
      try { props[h]({ target: el, currentTarget: el, preventDefault() {}, stopPropagation() {} }); called.push(h); }
      catch (e) { /* a handler that dislikes a synthetic event shouldn't stop the others */ }
    }
  }
  if (!called.length) {
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    try { el.focus(); el.blur(); } catch (e) {}
  }
  return { ok: (el.value || "") === value, called, reactProps: !!props, value: el.value };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    try {
      if (msg.type === "learn") { maybeLearn(msg, sender); sendResponse({ ok: true }); }
      else if (msg.type === "runBatch") {
        const b = await getBatch();
        if (!b.active) runBatch({ windowId: msg.windowId, tabIds: msg.tabIds, limitMs: msg.limitMs })
          .catch((e) => console.error("batch failed", e));
        sendResponse({ ok: true, started: !b.active, alreadyRunning: !!b.active });
      }
      else if (msg.type === "stopBatch") { await stopBatch(); sendResponse({ ok: true }); }
      else if (msg.type === "reactFill") {
        // suggest.js marked an element with data-rr-fill and needs it driven through the page's
        // OWN React handlers — reachable only from the MAIN world, which a content script isn't.
        // Runs in the exact frame the suggestion came from.
        try {
          const out = await chrome.scripting.executeScript({
            target: { tabId: sender.tab.id, frameIds: [sender.frameId || 0] },
            world: "MAIN", args: [String(msg.value || "")], func: mainWorldReactFill,
          });
          sendResponse({ ok: !!(out && out[0] && out[0].result && out[0].result.ok),
                         detail: out && out[0] && out[0].result });
        } catch (e) { sendResponse({ ok: false, error: String(e && e.message || e).slice(0, 120) }); }
      }
      else if (msg.type === "refreshProfile") {
        // suggest.js asks for a fresh profile in storage; respond immediately, fetch in background
        refreshProfileToStorage();
        sendResponse({ ok: true });
      }
      else if (msg.type === "getJobs") sendResponse({ ok: true, jobs: await getJobs() });
      else if (msg.type === "setJob") sendResponse({ ok: true, job: await setJob(msg.patch) });
      else if (msg.type === "pasteJD") {
        if (!(msg.jd_text || "").trim()) sendResponse({ ok: false, error: "empty JD" });
        else { runFromJD(msg.jobId, msg.jd_text.trim()).catch((e) => console.error("pasteJD failed", e));
          sendResponse({ ok: true, started: true }); }
      }
      else if (msg.type === "deleteJob") {
        const jobs = await getJobs(); delete jobs[msg.jobId];
        await chrome.storage.local.set({ [STORE]: jobs }); sendResponse({ ok: true });
      }
      else if (msg.type === "clearJobs") { await chrome.storage.local.set({ [STORE]: {} }); sendResponse({ ok: true }); }
      else if (msg.type === "pickerState") {
        sendResponse({ ok: true, armed: false });
      }
      else if (msg.type === "armPicker") {
        // Arming retired — refuse quietly so old UI / callers don't throw.
        sendResponse({ ok: false, error: "arming is off; use Auto-upload resume or Attach now", armed: false });
      }
      else if (msg.type === "pickerMode") {
        sendResponse({ ok: true, mode: "off" });
      }
      else if (msg.type === "autoAttachMode") {
        if (msg.mode) await chrome.storage.local.set({ [AUTO_ATTACH]: msg.mode });
        sendResponse({ ok: true, mode: (await autoAttachEnabled()) ? "on" : "off" });
      }
      else if (msg.type === "autoAttachNow") {
        // force: ignore both the on/off setting and the "already did this page" mark, so pressing
        // the button always actually tries, and reports why when it can't.
        const tab = await chrome.tabs.get(msg.tabId).catch(() => null);
        const report = await autoAttachDocs(msg.tabId, tab && tab.url, { force: true });
        sendResponse({ ok: true, report });
      }
      else if (msg.type === "rearmPicker") {
        sendResponse({ ok: true, rearmed: false });
      }
      else if (msg.type === "disarmPicker") {
        await disarmTab(msg.tabId);
        sendResponse({ ok: true, armed: false });
      }
      else if (msg.type === "health") {
        const all = await chrome.storage.local.get(null);
        const sizes = Object.entries(all)
          .map(([k, v]) => [k, JSON.stringify(v || "").length])
          .sort((a, b) => b[1] - a[1]);
        const used = sizes.reduce((n, [, b]) => n + b, 0);
        const jobs = all[STORE] || {};
        const jobList = Object.values(jobs);
        let quota = null;
        try { quota = await chrome.storage.local.getBytesInUse(null); } catch (e) {}
        sendResponse({ ok: true, health: {
          storageBytes: used,
          bytesInUse: quota,
          biggest: sizes.slice(0, 5).map(([k, b]) => `${k}: ${(b / 1024).toFixed(0)}KB`),
          profileCached: !!(all.rr_profile && all.rr_profile.identity),
          jobs: jobList.length,
          jobsWithResume: jobList.filter((j) => j.resume_id).length,
          batchActive: !!(all.rr_batch || {}).active,
          autoAttach: (await autoAttachEnabled()) ? "on" : "off",
          lastAttach: all.rr_auto_attach_last || null,
        } });
      }
      else sendResponse({ ok: false, error: "unknown message" });
    } catch (e) { sendResponse({ ok: false, error: e.message }); }
  })();
  return true;   // keep the channel open for async sendResponse
});


// ---------------- our file picker instead of Finder ----------------
// filepicker.js defines armTab/disarmTab/onFileChooser/armedTabs; it's loaded after API/STORE are
// declared because it uses both. The listeners below are registered at TOP LEVEL so an evicted
// service worker is woken by the events rather than missing them.

chrome.debugger.onEvent.addListener((source, method, params) => {
  if (method !== "Page.fileChooserOpened" || !source.tabId) return;
  armedTabs().then((set) => {
    if (set.has(source.tabId)) onFileChooser(source.tabId, params).catch((e) => console.error(e));
  });
});

async function batchRunning() {
  try { return !!(await getBatch()).active; } catch (e) { return false; }
}

// Auto-upload only — no arming. Debugger attaches briefly while placing files when the toggle is on.
chrome.tabs.onUpdated.addListener(async (tabId, info, tab) => {
  if (info.status !== "complete") return;
  if (await batchRunning()) return;
  scheduleAutoAttach(tabId, tab && tab.url);
});

chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  if (await batchRunning()) return;
  try {
    const tab = await chrome.tabs.get(tabId);
    if (tab && /^https?:/.test(tab.url || "")) scheduleAutoAttach(tabId, tab.url, [400]);
  } catch (e) {}
});

// Drop any leftover armed sessions from older builds (clears the standing debug banner).
chrome.runtime.onStartup.addListener(() => { disarmAllArmed().catch(() => {}); });
chrome.runtime.onInstalled.addListener(() => { disarmAllArmed().catch(() => {}); });
disarmAllArmed().catch(() => {});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  clearAutoAttach(tabId);
  const set = await armedTabs();
  if (set.delete(tabId)) await setArmedTabs(set);
  try {
    const marks = (await chrome.storage.local.get(ATTACHED_MARK))[ATTACHED_MARK] || {};
    if (tabId in marks) { delete marks[tabId]; await chrome.storage.local.set({ [ATTACHED_MARK]: marks }); }
  } catch (e) {}
});
chrome.debugger.onDetach.addListener(async (source) => {
  if (!source.tabId) return;
  const set = await armedTabs();
  if (set.delete(source.tabId)) await setArmedTabs(set);
});
