// Replaces macOS Finder for file uploads — optional / dormant path.
//
// Clicking an <input type=file> normally opens the OS file dialog, which no extension API can
// intercept — EXCEPT through the DevTools Protocol. `Page.setInterceptFileChooserDialog` tells
// Chrome not to show the native dialog and to emit `Page.fileChooserOpened` instead.
//
// Persistent "arm this tab" was retired: keeping a debugger attached caused a standing
// "Chrome is being debugged" banner and could make tabs feel like they were refreshing.
// Auto-upload (below) attaches briefly only while placing a file, then detaches.
//
// Everything here lives in the SERVICE WORKER on purpose: the debugger session and its event
// listener have to outlive the side panel being closed, and `chrome.debugger.onEvent` registered at
// top level wakes the worker back up after eviction.

const PICKER_TABS = "rr_picker_tabs";     // [tabId, …] currently armed — survives worker eviction
const PICKER_MODE = "rr_picker_mode";     // kept for storage compat; arming is retired (always off)
const PICKER_OPTOUT = "rr_picker_optout"; // legacy; no longer written

async function pickerMode() {
  // Arming (persistent debugger + "Chrome is being debugged" banner) is retired. Auto-upload
  // attaches briefly only when placing a file — no standing sessions, no auto-arm.
  return "off";
}
async function optedOut() {
  const r = await chrome.storage.local.get(PICKER_OPTOUT);
  return new Set(r[PICKER_OPTOUT] || []);
}
async function setOptedOut(set) {
  await chrome.storage.local.set({ [PICKER_OPTOUT]: [...set] });
}

// No-op: arming caused tab flakiness (attach/Page.enable on every load) and a persistent banner.
async function autoArm(_tab) { /* retired */ }
async function autoArmAllOpen() { /* retired */ }

// One-shot cleanup for installs that still have tabs armed from the old flow.
async function disarmAllArmed() {
  const set = await armedTabs();
  for (const id of set) {
    try { await disarmTab(id); } catch (e) {}
  }
  await chrome.storage.local.set({ [PICKER_MODE]: "off", [PICKER_TABS]: [], [PICKER_OPTOUT]: [] });
}

async function armedTabs() {
  const r = await chrome.storage.local.get(PICKER_TABS);
  return new Set(r[PICKER_TABS] || []);
}
async function setArmedTabs(set) {
  await chrome.storage.local.set({ [PICKER_TABS]: [...set] });
}

// Uniquely named on purpose. importScripts shares ONE global scope, so a top-level `const sleep`
// here collides with background.js's and throws SyntaxError — which kills the entire service worker
// before a single listener registers, taking the batch, the cached profile (and therefore the
// inline suggestion chips) and everything else down with it. Borrowing background.js's global
// instead would work but silently depends on load order. Anything added at top level in this file
// must not collide with background.js.
const fpSleep = (ms) => new Promise((r) => setTimeout(r, ms));

function dbg(tabId, method, params) {
  if (!(chrome.debugger && chrome.debugger.sendCommand)) {
    return Promise.reject(new Error("no debugger API"));
  }
  return chrome.debugger.sendCommand({ tabId }, method, params || {});
}

// Turn interception on for a tab. Safe to call repeatedly — re-arming after a navigation is the
// normal case, since a page load resets the flag.
async function armTab(tabId) {
  if (!(chrome.debugger && chrome.debugger.attach)) return;
  const already = (await chrome.debugger.getTargets())
    .some((t) => t.tabId === tabId && t.attached);
  if (!already) {
    try { await chrome.debugger.attach({ tabId }, "1.3"); }
    catch (e) {
      const msg = String(e && e.message || e);
      // "Another debugger is already attached" = DevTools is open on this tab.
      if (!/already attached/i.test(msg)) throw e;
    }
  }
  await dbg(tabId, "Page.enable");
  await dbg(tabId, "DOM.enable");
  await dbg(tabId, "Page.setInterceptFileChooserDialog", { enabled: true });
  const set = await armedTabs();
  set.add(tabId);
  await setArmedTabs(set);
  return true;
}

async function disarmTab(tabId) {
  const set = await armedTabs();
  set.delete(tabId);
  await setArmedTabs(set);
  try { await dbg(tabId, "Page.setInterceptFileChooserDialog", { enabled: false }); } catch (e) {}
  try { if (chrome.debugger && chrome.debugger.detach) await chrome.debugger.detach({ tabId }); } catch (e) {}
}

// Documents to offer, THIS TAB'S JOB FIRST. That ordering is the whole point: the file you want is
// almost always the résumé generated for the posting you're looking at.
async function pickerDocs(tabId) {
  let list = { resumes: [], cover_letters: [], frequent: [] };
  try {
    const r = await fetch(`${API}/documents`);
    if (r.ok) list = await r.json();
  } catch (e) { /* backend down — the picker will say so */ }

  let tid = null;
  try {
    const tab = await chrome.tabs.get(tabId);
    const jobs = (await chrome.storage.local.get(STORE))[STORE] || {};
    let jid = null;
    try { jid = rrJobId(tab.url); } catch (e) {}
    if (jid && jobs[jid] && jobs[jid].resume_id) tid = jobs[jid].resume_id;
    if (!tid) {
      // Workday and friends apply on a different URL than the one the JD was read from, so fall
      // back to the most recent job on the same host.
      let host = ""; try { host = new URL(tab.url).hostname; } catch (e) {}
      const m = Object.values(jobs)
        .filter((j) => j.resume_id && j.host && host && j.host === host)
        .sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0))[0];
      if (m) tid = m.resume_id;
    }
  } catch (e) {}

  // Names in the picker are deliberately short. A full "Collins Aerospace (part of RTX) — Software
  // Engineering Intern" is unreadable in a list of 160; what you actually need to pick correctly is
  // WHICH COMPANY and WHICH KIND of document. The role moves to the tooltip.
  const LEGAL_RX = /\b(inc|inc\.|llc|l\.l\.c\.|corp|corp\.|corporation|ltd|ltd\.|limited|plc|co|co\.|company|holdings?|group|technologies|technology|labs?|solutions|systems|international|worldwide|global|na|n\.a\.)\b\.?/gi;

  function shortCompany(name) {
    let n = (name || "").trim();
    if (!n) return "";
    // "PricewaterhouseCoopers (PwC)" → "PwC": a short parenthetical is the company's own shorthand.
    const paren = n.match(/\(([^)]{2,8})\)\s*$/);
    if (paren && /^[A-Za-z0-9&.\- ]+$/.test(paren[1]) && paren[1].length <= 6) return paren[1].trim();
    n = n.replace(/\s*\([^)]*\)/g, " ");        // drop "(part of RTX)"
    n = n.replace(LEGAL_RX, " ").replace(/[,&]+\s*$/, "");
    n = n.replace(/\s+/g, " ").trim();
    if (n.length <= 18) return n;
    const cut = n.slice(0, 18);
    const sp = cut.lastIndexOf(" ");
    return (sp > 8 ? cut.slice(0, sp) : cut).trim() + "…";
  }

  const KIND_LABEL = { resume: "Résumé", cover: "Cover letter", transcript: "Transcript",
                       schedule: "Schedule" };
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function shortDate(d) {
    const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(d || "");
    return m ? `${MONTHS[+m[2] - 1]} ${+m[3]}` : (d || "");
  }

  const all = [
    ...(list.resumes || []).map((d) => ({ ...d, kind: d.kind || "resume" })),
    ...(list.cover_letters || []).map((d) => ({ ...d, kind: d.kind || "cover" })),
    ...(list.frequent || []),
  ];
  const seen = new Set();
  const docs = [];
  for (const d of all) {
    if (!d || !d.path || seen.has(d.path)) continue;
    seen.add(d.path);
    const kind = d.kind || "doc";
    const co = shortCompany(d.company);
    const type = KIND_LABEL[kind] || "Document";
    docs.push({
      // "Collins Aerospace · Résumé" instead of the full company—role filename.
      name: co ? `${co} · ${type}` : (d.name || d.filename || d.path.split("/").pop()),
      sub: shortDate(d.date),
      title: [d.company, d.role].filter(Boolean).join(" — "),   // the full thing, on hover
      path: d.path, kind,
      forThisTab: !!(tid && d.tracker_id === tid),
    });
  }
  docs.sort((a, b) => (b.forThisTab ? 1 : 0) - (a.forThisTab ? 1 : 0));
  return docs;
}

// The picker itself — injected into the page so it appears over the form, right where the OS
// dialog would have. Returns the chosen path (or null) via a promise resolved by a click.
function rrFilePickerOverlay(docs, multiple) {
  return new Promise((resolve) => {
    document.getElementById("rr-file-picker")?.remove();
    const wrap = document.createElement("div");
    wrap.id = "rr-file-picker";
    wrap.style.cssText = "position:fixed;inset:0;z-index:2147483647;background:rgba(15,18,24,.55);" +
      "display:flex;align-items:center;justify-content:center;font:13px/1.4 -apple-system," +
      "BlinkMacSystemFont,'Segoe UI',sans-serif";
    const card = document.createElement("div");
    card.style.cssText = "background:#fff;color:#12151a;width:min(560px,92vw);max-height:74vh;" +
      "border-radius:12px;box-shadow:0 24px 64px rgba(0,0,0,.35);display:flex;flex-direction:column;" +
      "overflow:hidden";
    const head = document.createElement("div");
    head.style.cssText = "padding:12px 14px;border-bottom:1px solid #e6e8ec;display:flex;gap:8px;" +
      "align-items:center";
    head.innerHTML = '<span style="font-weight:700">Choose a document</span>' +
      '<span style="color:#6b7280;font-size:12px">— from Resume Rewriter</span>';
    const search = document.createElement("input");
    search.placeholder = "Search…";
    search.style.cssText = "margin-left:auto;padding:5px 9px;border:1px solid #d5d9e0;" +
      "border-radius:7px;font:inherit;width:170px";
    head.append(search);

    const list = document.createElement("div");
    list.style.cssText = "overflow:auto;padding:6px";

    const rows = [];
    for (const d of docs) {
      const row = document.createElement("button");
      row.type = "button";
      row.style.cssText = "display:flex;gap:10px;align-items:center;width:100%;text-align:left;" +
        "padding:9px 10px;border:1px solid transparent;border-radius:9px;background:none;" +
        "cursor:pointer;font:inherit;color:inherit";
      row.onmouseenter = () => (row.style.background = "#f2f4f7");
      row.onmouseleave = () => (row.style.background = "none");
      const icon = d.kind === "cover" ? "✉️" : d.kind === "transcript" ? "🎓" : "📄";
      row.innerHTML =
        `<span style="font-size:17px">${icon}</span>` +
        `<span style="min-width:0;flex:1">` +
          `<span style="display:block;font-weight:600;overflow:hidden;text-overflow:ellipsis;` +
            `white-space:nowrap">${d.name.replace(/</g, "&lt;")}</span>` +
          `<span style="display:block;color:#6b7280;font-size:11.5px">${(d.sub || "").replace(/</g, "&lt;")}</span>` +
        `</span>` +
        (d.forThisTab ? '<span style="background:#0b7;color:#fff;border-radius:999px;' +
          'padding:2px 8px;font-size:11px;font-weight:700">this job</span>' : "");
      row.addEventListener("click", () => { cleanup(); resolve(d.path); });
      row.title = d.title || d.name;
      row.dataset.q = (d.name + " " + (d.sub || "") + " " + (d.title || "")).toLowerCase();
      rows.push(row);
      list.append(row);
    }
    if (!docs.length) {
      const empty = document.createElement("div");
      empty.style.cssText = "padding:22px;text-align:center;color:#6b7280";
      empty.textContent = "No documents found — is the backend running?";
      list.append(empty);
    }

    const foot = document.createElement("div");
    foot.style.cssText = "padding:10px 14px;border-top:1px solid #e6e8ec;display:flex;gap:8px;" +
      "align-items:center;color:#6b7280;font-size:11.5px";
    foot.textContent = "Esc cancels.";
    const finder = document.createElement("button");
    finder.type = "button";
    finder.textContent = "Use Finder instead";
    finder.style.cssText = "margin-left:auto;padding:5px 10px;border:1px solid #d5d9e0;" +
      "border-radius:7px;background:#fff;cursor:pointer;font:inherit";
    finder.addEventListener("click", () => { cleanup(); resolve("__FINDER__"); });
    foot.append(finder);

    search.addEventListener("input", () => {
      const q = search.value.trim().toLowerCase();
      rows.forEach((r) => (r.style.display = !q || r.dataset.q.includes(q) ? "flex" : "none"));
    });
    const onKey = (e) => {
      if (e.key === "Escape") { e.preventDefault(); cleanup(); resolve(null); }
    };
    function cleanup() {
      document.removeEventListener("keydown", onKey, true);
      wrap.remove();
    }
    document.addEventListener("keydown", onKey, true);
    wrap.addEventListener("click", (e) => { if (e.target === wrap) { cleanup(); resolve(null); } });

    card.append(head, list, foot);
    wrap.append(card);
    document.documentElement.append(wrap);
    search.focus();
  });
}

// A file dialog was suppressed — show ours and answer with the chosen file.
async function onFileChooser(tabId, params) {
  const backendNodeId = params && params.backendNodeId;
  // File System Access API dialogs carry no node; nothing to fill, so hand the user back to Finder.
  if (!backendNodeId) {
    await dbg(tabId, "Page.setInterceptFileChooserDialog", { enabled: false });
    return;
  }
  const docs = await pickerDocs(tabId);
  let chosen = null;
  try {
    const [res] = await chrome.scripting.executeScript({
      target: { tabId }, func: rrFilePickerOverlay,
      args: [docs, (params.mode || "") === "selectMultiple"],
    });
    chosen = res && res.result;
  } catch (e) { chosen = "__FINDER__"; }

  if (chosen === "__FINDER__") {
    // Turn interception off, then re-arm after the native dialog has had its turn.
    await dbg(tabId, "Page.setInterceptFileChooserDialog", { enabled: false });
    setTimeout(() => armTab(tabId).catch(() => {}), 30000);
    return;
  }
  if (!chosen) {                       // cancelled — leave the input untouched
    return;
  }
  try {
    await dbg(tabId, "DOM.setFileInputFiles", { files: [chosen], backendNodeId });
  } catch (e) {
    console.error("setFileInputFiles failed", e);
  }
}

// The Docs-tab attach and the autofill uploader both attach/detach their own debugger session.
// Their detach silently clears our interception flag, so they ping us afterwards to restore it.
async function rearmIfArmed(tabId) {
  const set = await armedTabs();
  if (!set.has(tabId)) return false;
  try { await armTab(tabId); return true; } catch (e) { return false; }
}

// ===================== automatic attach on page load =========================================
// When a page shows an upload area we can identify, put the right document in it without being
// asked. Runs off the debugger session the picker already holds, so it costs nothing extra on an
// armed tab. Deliberately conservative: it only ever touches a field whose surrounding text names
// a document kind we have, and never one that already holds a file.
const AUTO_ATTACH = "rr_auto_attach";       // "on" | "off"
const ATTACHED_MARK = "rr_auto_attached";   // tabId -> url we already handled, to avoid re-runs

async function autoAttachEnabled() {
  const r = await chrome.storage.local.get(AUTO_ATTACH);
  // OFF by default. When on, upload areas get the matching résumé/cover as pages load —
  // brief debugger attach only while placing files (no persistent "being debugged" banner).
  return (r[AUTO_ATTACH] || "off") === "on";
}

async function autoAttachDocs(tabId, url, opts = {}) {
  const report = { ok: false, steps: [] };
  const step = (m) => { report.steps.push(m); return report; };

  if (!opts.force && !(await autoAttachEnabled())) return step("auto-attach is switched off");

  // No "already done this page" gate. A reload empties the inputs again, and every field that
  // still holds a file is skipped below anyway, so re-running is idempotent and a reload,
  // an SPA step, or a late-mounting widget all get handled.
  const marks = {};

  // Attaching is this function's own business now. Tying it to the picker meant that if arming
  // failed for any reason (DevTools open on the tab, a restricted page, the mode set to "jobs"),
  // auto-upload silently did nothing at all — with no way to tell which of the two had failed.
  const armed = (await armedTabs()).has(tabId);
  let temporary = false;
  const useCdp = typeof chrome !== "undefined" && chrome.debugger && chrome.debugger.attach;
  if (useCdp && !armed) {
    try {
      await chrome.debugger.attach({ tabId }, "1.3");
      temporary = true;
    } catch (e) {
      if (!/already attached/i.test(String((e && e.message) || e))) {
        return step("couldn't attach debugger: " + String((e && e.message) || e).slice(0, 70));
      }
    }
  }
  try {
    return await attachInner(tabId, url, marks, report, step, useCdp);
  } finally {
    if (temporary) { try { await chrome.debugger.detach({ tabId }); } catch (e) {} }
  }
}

async function attachInner(tabId, url, marks, report, step, useCdp) {

  let areas = [];
  try {
    const [res] = await chrome.scripting.executeScript({ target: { tabId }, func: rrProbeUploadAreas });
    areas = (res && res.result) || [];
  } catch (e) { return step("couldn't read the page: " + String(e).slice(0, 60)); }
  if (!areas.length) return step("no <input type=file> on this page");

  for (const a of areas) Object.assign(a, classifyUploadArea(a.text, a.pageText));
  report.areas = areas.map((a) => ({ index: a.index, kind: a.kind, mode: a.mode,
                                     hasFile: a.hasFile, text: (a.text || "").slice(0, 90) }));
  if (!areas.some((a) => a.kind)) {
    return step(`${areas.length} upload field(s), none whose text names a document I have`);
  }

  // What we have to give it.
  const docs = await pickerDocs(tabId);
  const byKind = {};
  for (const d of docs) {
    if (d.kind === "resume" && d.forThisTab && !byKind.resume) byKind.resume = d.path;
    if (d.kind === "cover" && d.forThisTab && !byKind.cover) byKind.cover = d.path;
    if (d.kind === "transcript" && !byKind.transcript) byKind.transcript = d.path;
  }
  report.have = Object.keys(byKind);
  if (!byKind.resume && !byKind.cover && !byKind.transcript) {
    return step("no documents for this tab's job yet (generate the résumé first)");
  }

  const placed = [], skipped = [];
  for (const kind of ["resume", "cover", "transcript"]) {
    if (!byKind[kind]) continue;
    const plan = chooseUploadTargets(areas, kind);
    for (const sk of plan.skipped) skipped.push({ kind, ...sk });
    if (!plan.targets.length) {
      if (plan.anyAlreadyFilled) step(`${kind}: already attached on this page`);
      continue;
    }
    // Every real field for this kind, not just the first: a page can have an optional autofill
    // drop zone AND the required résumé field, and leaving the required one empty is a broken
    // application. Inert attachment slots are already excluded by chooseUploadTargets.
    for (const target of plan.targets) {
    try {
      const fileName = String(byKind[kind]).split("/").pop();
      if (useCdp) {
        // Resolve by the stamp the probe left, walking shadow roots exactly as the probe did, so the
        // node we fill is provably the node we classified.
        const objRes = await dbg(tabId, "Runtime.evaluate", {
          expression: rrDeepFindExpr(target.index), returnByValue: false,
        });
        const objectId = objRes && objRes.result && objRes.result.objectId;
        if (!objectId) { step(`${kind}: upload field #${target.index + 1} vanished`); continue; }
        await dbg(tabId, "DOM.setFileInputFiles", { files: [byKind[kind]], objectId });
        await fpSleep(300);

        // An autofill uploader CONSUMES the file: Workday reads it, starts parsing, and swaps the
        // drop zone for an "uploaded / processing" view, which removes the very input we stamped. So
        // a missing node right after a successful set is evidence the page ACCEPTED the file, not
        // that it refused it.
        const chk = await dbg(tabId, "Runtime.evaluate", {
          expression: `(() => {
            const el = ${rrDeepFindExpr(target.index)};
            if (el) return (el.files || []).length > 0 ? "has-file" : "empty";
            try {
              const t = (document.body && document.body.innerText) || "";
              if (t.indexOf(${JSON.stringify(fileName)}) >= 0) return "named-on-page";
            } catch (e) {}
            return "gone";
          })()`,
          returnByValue: true,
        });
        const verdict = (chk && chk.result && chk.result.value) || "unknown";
        if (verdict === "has-file" || verdict === "gone" || verdict === "named-on-page") {
          placed.push({ kind, mode: target.mode, verdict });
        } else {
          step(`${kind}: the page cleared the field (${verdict})`);
        }
      } else {
        const r = await rrPlaceFileBlob(tabId, target.index, byKind[kind]);
        await fpSleep(300);
        if (r.ok) placed.push({ kind, mode: target.mode, verdict: "has-file" });
        else step(`${kind}: ${r.error || "the page rejected the file"}`);
      }
    } catch (e) {
      step(`${kind}: ${String((e && e.message) || e).slice(0, 60)}`);
    }
    }
  }

  const bits = placed.map((p) => `${p.kind}${p.mode === "autofill" ? " (autofill box)" : ""}`);
  report.ok = placed.length > 0;
  report.placed = bits;
  report.skipped = skipped;
  step(placed.length ? `attached: ${bits.join(", ")}` : "found matching fields but nothing was placed");
  try {
    await chrome.storage.local.set({ rr_auto_attach_last: {
      tabId, url, at: Date.now(), placed: bits, skipped, steps: report.steps } });
  } catch (e) {}
  return report;
}

// Retry ladder. ATS pages mount their upload widgets well after `load` fires, and a résumé often
// doesn't exist yet at that moment either, so a single attempt on page-load is the wrong shape.
// Each pass is cheap and idempotent (fields that already hold a file are skipped).
const ATTACH_DELAYS = [800, 2500, 6000, 12000];
const attachTimers = new Map();     // tabId -> timeout ids, so a new trigger replaces the old run

function scheduleAutoAttach(tabId, url, delays = ATTACH_DELAYS) {
  clearAutoAttach(tabId);
  const ids = delays.map((d) => setTimeout(() => {
    autoAttachDocs(tabId, url).catch(() => {});
  }, d));
  attachTimers.set(tabId, ids);
}

function clearAutoAttach(tabId) {
  for (const id of attachTimers.get(tabId) || []) clearTimeout(id);
  attachTimers.delete(tabId);
}

// THE trigger that was missing. Tabs are opened first and résumés are generated afterwards, so by
// the time a résumé exists the page has long since finished loading and no load event will ever
// fire again. Watch rr_jobs instead: the moment a job gains a resume_id, attach to its tab.
chrome.storage.onChanged.addListener(async (changes, area) => {
  if (area !== "local" || !changes[STORE]) return;
  const before = changes[STORE].oldValue || {};
  const after = changes[STORE].newValue || {};
  for (const [jobId, job] of Object.entries(after)) {
    const had = (before[jobId] || {}).resume_id;
    const has = job && job.resume_id;
    if (!has || had === has) continue;                 // only the transition none -> resume
    if (!job.tabId) continue;
    try {
      const tab = await chrome.tabs.get(job.tabId);
      if (tab && /^https?:/.test(tab.url || "")) scheduleAutoAttach(tab.id, tab.url, [300, 2000]);
    } catch (e) { /* tab closed since */ }
  }
});
