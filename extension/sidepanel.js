// Jobs tab. All per-job content is keyed by jobId (rrJobId of the posting URL) and lives in
// chrome.storage.local["rr_jobs"] — the background owns writes (panel goes through the
// "setJob" message), the panel only renders. The single-job section below the batch dashboard
// always shows the ACTIVE browser tab's job and swaps when you switch tabs; background batch
// work on other tabs can never overwrite what you're looking at.
const API = "http://127.0.0.1:8765";

let currentJobId = null;   // rrJobId of the active browser tab's URL

const $ = (id) => document.getElementById(id);
function setStatus(msg, cls = "") { const s = $("status"); s.textContent = msg; s.className = "status " + cls; }
function el(tag, props = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") n.className = v; else if (k === "html") n.innerHTML = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v); else if (v != null) n.setAttribute(k, v);
  }
  for (const c of kids.flat()) if (c != null) n.append(c.nodeType ? c : document.createTextNode(c));
  return n;
}

async function bg(type, extra = {}) {
  return chrome.runtime.sendMessage({ type, ...extra });
}
async function getJobs() {
  const r = await bg("getJobs");
  return (r && r.jobs) || {};
}
async function setJob(patch) {
  return bg("setJob", { patch });
}
async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab;
}
const isWebUrl = (u) => /^https?:/.test(u || "");

// Runs in the PAGE context (all frames) — ARM a one-shot paste: the user's NEXT click on a
// text field fills it with the value (React-safe native setter + events, same trick as
// autofill.js). Clicks on non-fields keep it armed; Escape or the TTL disarms. Re-arming
// replaces any previous armed value.
function rrArmPaste(value, ttlMs) {
  if (window.__rrDisarmPaste) window.__rrDisarmPaste();

  const setVal = (n) => {
    const isText = n.tagName === "TEXTAREA" ||
      (n.tagName === "INPUT" && /^(text|email|tel|url|search|number|password)$/i.test(n.type || "text"));
    if (isText && !n.disabled && !n.readOnly) {
      const proto = n.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
      Object.getOwnPropertyDescriptor(proto, "value").set.call(n, value);
      n.dispatchEvent(new Event("input", { bubbles: true }));
      n.dispatchEvent(new Event("change", { bubbles: true }));
      return true;
    }
    if (n.isContentEditable) {
      n.focus();
      document.execCommand("insertText", false, value);
      return true;
    }
    return false;
  };

  const onClick = (ev) => {
    const t = ev.target;
    const field = t && t.closest
      ? t.closest("input, textarea, [contenteditable=true], [contenteditable='']") : null;
    if (!field) return;                    // not a field — stay armed
    setTimeout(() => setVal(field), 0);    // let the click focus the field first
    disarm();
  };
  const onKey = (ev) => { if (ev.key === "Escape") disarm(); };
  const disarm = () => {
    document.removeEventListener("click", onClick, true);
    document.removeEventListener("keydown", onKey, true);
    clearTimeout(timer);
    window.__rrDisarmPaste = null;
  };
  window.__rrDisarmPaste = disarm;
  document.addEventListener("click", onClick, true);
  document.addEventListener("keydown", onKey, true);
  const timer = setTimeout(disarm, ttlMs || 25000);
  return true;
}

// Panel-side: arm the paste on the active tab (all frames). Returns false when the page
// can't be scripted (chrome:// etc.) — caller shows clipboard-only feedback.
async function armPaste(value) {
  try {
    const tab = await activeTab();
    if (!tab || !isWebUrl(tab.url)) return false;
    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true },
      func: rrArmPaste, args: [value, 25000],
    });
    return (results || []).some((r) => r && r.result === true);
  } catch (e) { return false; }
}

// Runs in the PAGE context — pull the most job-description-like text off the page.
function extractJD() {
  const sels = ["main", "article", "[role=main]", "[class*=job-description]",
    "[class*=jobDescription]", "[data-testid*=escription]", "#job", ".job-details"];
  let best = "";
  for (const s of sels) {
    try {
      document.querySelectorAll(s).forEach((n) => {
        const t = (n.innerText || "").trim();
        if (t.length > best.length) best = t;
      });
    } catch (e) {}
  }
  const body = document.body ? document.body.innerText : "";
  const text = best.length > 200 ? best : body || "";
  return { title: document.title, url: location.href, text: text.slice(0, 20000) };
}

async function readJD() {
  setStatus("Reading the page…");
  $("results").innerHTML = "";
  let pageData, tab;
  try {
    tab = await activeTab();
    if (!tab || !isWebUrl(tab.url)) return setStatus("Open a job posting tab first.", "err");
    const [inj] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: extractJD });
    pageData = inj.result;
  } catch (e) {
    return setStatus("Couldn't read the page: " + e.message, "err");
  }
  if (!pageData || (pageData.text || "").trim().length < 120) {
    return setStatus("Not enough text on this page to look like a job description.", "err");
  }
  const jobId = rrJobId(tab.url);
  currentJobId = jobId;
  await setJob({ jobId, tabId: tab.id, url: tab.url, host: new URL(tab.url).hostname,
    title: pageData.title || tab.title, jd_text: pageData.text, status: "jd-read",
    flagged: false, reason: "" });
  setStatus("Analyzing with local model (~15–20s)…");
  try {
    const r = await fetch(`${API}/jd/analyze`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ jd_text: pageData.text }),
    });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const analysis = await r.json();
    await setJob({ jobId, jd_analysis: analysis,
      company: analysis.company || "", role: analysis.role_title || "" });
    setStatus("Read “" + (pageData.title || "page").slice(0, 60) + "”", "ok");
    refreshActiveJob();
  } catch (e) {
    setStatus("Backend error — is ./run.sh running? (" + e.message + ")", "err");
  }
}

// ------------- the ACTIVE tab's job card (per-tab scoped) -------------
// Keyed by the page's jobId, with a same-tab fallback: reading a JD on the posting page and
// then navigating to the application form changes the URL (new jobId), but it's still the
// same job — so if the current URL has no entry, show the most recent job read in THIS tab.
async function refreshActiveJob() {
  const tab = await activeTab();
  let job = null;
  if (tab && isWebUrl(tab.url)) {
    const jobs = await getJobs();
    job = jobs[rrJobId(tab.url)] || null;
    if (!job) {
      job = Object.values(jobs)
        .filter((j) => j.tabId === tab.id)
        .sort((x, y) => (y.updatedAt || 0) - (x.updatedAt || 0))[0] || null;
    }
  }
  currentJobId = job ? job.jobId
    : (tab && isWebUrl(tab.url) ? rrJobId(tab.url) : null);
  renderResults(job, job && tab && rrJobId(tab.url) !== job.jobId);
}

// Minimal per-tab view: one button makes both docs instantly, so this just shows live progress
// + the résumé/cover-letter links. (No focus-angle picker anymore — it uses the default angle.)
function renderResults(job, isCarryOver) {
  const box = $("results");
  box.innerHTML = "";
  if (!job) return;
  const a = job.jd_analysis || {};
  const title = (a.role_title || job.role || job.title || "This tab's job").slice(0, 50);
  const company = a.company || job.company || "";
  const statusLine = job.flagged ? "⚠ " + (job.reason || "flagged")
    : (STATUS_LABEL[job.status] || job.status || "");
  const card = el("div", { class: "card" },
    el("div", {}, el("span", { class: "k" }, title),
      company ? el("span", { class: "muted" }, "  ·  " + company) : null),
    statusLine ? el("div", { class: "muted", style: "font-size:11.5px;margin-top:3px" }, statusLine) : null);
  if (job.resume_id) {
    const links = el("div", { style: "margin-top:8px" });
    renderJobLinks(links, job.resume_id, !!job.cover_letter_path);
    card.append(links);
  }
  box.append(card);
  if (isCarryOver) box.append(el("div", { class: "muted", style: "font-size:11px;margin-top:6px" },
    "Showing the job you started in this tab (the URL changed — that's fine)."));
}

// (old interactive analysis card kept below, no longer rendered — replaced by the above)
function _renderResultsLegacy(job, isCarryOver) {
  const box = $("results");
  box.innerHTML = "";
  if (!job || !job.jd_analysis) {
    if (job && job.jd_text) box.append(el("div", { class: "muted", style: "font-size:12px;margin-top:8px" },
      "JD captured for this tab — analysis pending."));
    return;
  }
  const a = job.jd_analysis;
  if (isCarryOver) {
    box.append(el("div", { class: "muted", style: "font-size:11.5px;margin-top:8px" },
      "Showing the job you read earlier in this tab (the page URL changed — that's fine)."));
  }

  const head = el("div", { class: "card" },
    el("div", {}, el("span", { class: "k" }, a.role_title || "Role"),
      a.company ? el("span", { class: "muted" }, "  ·  " + a.company) : null),
    el("div", { class: "muted", style: "font-size:12px" }, "Seniority: " + (a.seniority || "—")),
    a.summary ? el("div", { style: "margin-top:6px" }, a.summary) : null);
  box.append(head);

  if ((a.concrete_tech || []).length) {
    box.append(el("div", { class: "card" },
      el("div", { class: "k" }, "Tech mentioned in this JD"),
      el("div", { class: "chips" }, a.concrete_tech.map((t) => el("span", { class: "chip" }, t)))));
  }

  // Note: the JD's tech is shown read-only above. The resume only ever uses skills that are
  // actually in your profile — nothing from the JD is auto-added or woven in. Keep your profile
  // honest on the website and the generator will only draw from it.

  // focus angle + generate (writes results back to THIS job's slot)
  const jobId = job.jobId;
  const angles = a.angle_ideas && a.angle_ideas.length ? a.angle_ideas : ["General"];
  const sel = el("select", {}, angles.map((x) => el("option", { value: x }, x)));
  const customWrap = el("input", { placeholder: "…or type your own angle" });
  const genStatus = el("div", { class: "status" });
  const links = el("div", { style: "margin-top:6px" });
  const btnResume = el("button", { class: "primary", style: "margin-top:10px" }, "Résumé only");
  const btnCover = el("button", { style: "margin-top:6px" }, "Cover letter only");
  const btnBoth = el("button", { style: "margin-top:6px" }, "Both — résumé + cover letter");
  const allBtns = [btnResume, btnCover, btnBoth];

  const makeResume = async () => {
    genStatus.textContent = "Generating résumé (~25s)…";
    const angle = (customWrap.value.trim() || sel.value);
    const r = await fetch(`${API}/resume/generate-pdf`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ jd_analysis: { ...a, jd_text: job.jd_text || a.jd_text || "" }, focus_angle: angle }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.detail || err.error || "HTTP " + r.status);
    }
    const res = await r.json();
    await setJob({ jobId, status: "resume-done", resume_id: res.tracker_id, pdf_path: res.pdf_path });
    return res.tracker_id;
  };
  const makeCoverLetter = async (trackerId) => {
    genStatus.textContent = "Writing cover letter (~30s)…";
    const r = await fetch(`${API}/cover-letter/generate`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tracker_id: trackerId }),
    });
    const cl = await r.json();
    if (cl.error) throw new Error(cl.error);
    await setJob({ jobId, status: "done", cover_letter_path: cl.pdf_path });
  };
  const run = (fn) => async () => {
    allBtns.forEach((b) => (b.disabled = true));
    genStatus.className = "status";
    try {
      await fn();
      genStatus.className = "status ok";
    } catch (e) {
      genStatus.className = "status err";
      genStatus.textContent = "Failed: " + e.message;
    } finally { allBtns.forEach((b) => (b.disabled = false)); }
  };

  btnResume.addEventListener("click", run(async () => {
    const tid = await makeResume();
    genStatus.textContent = "Résumé ready.";
    renderJobLinks(links, tid, !!((await getJobs())[jobId] || {}).cover_letter_path);
  }));
  btnCover.addEventListener("click", run(async () => {
    // the letter is written from the tailored resume content, so a résumé must exist —
    // reuse this job's if present, quietly generate one if not
    let tid = ((await getJobs())[jobId] || {}).resume_id;
    if (!tid) {
      genStatus.textContent = "No résumé for this JD yet — generating it first (~25s)…";
      tid = await makeResume();
    }
    await makeCoverLetter(tid);
    genStatus.textContent = "Cover letter ready.";
    renderJobLinks(links, tid, true);
  }));
  btnBoth.addEventListener("click", run(async () => {
    const tid = await makeResume();
    await makeCoverLetter(tid);
    genStatus.textContent = "Résumé + cover letter ready.";
    renderJobLinks(links, tid, true);
  }));

  if (job.resume_id) renderJobLinks(links, job.resume_id, !!job.cover_letter_path);
  box.append(el("div", { class: "card" },
    el("label", { class: "lbl" }, "Focus angle"), sel, customWrap,
    btnResume, btnCover, btnBoth, genStatus, links));
}

// Links + "attach to page" buttons under the generate buttons.
function renderJobLinks(host, trackerId, hasCoverLetter) {
  host.innerHTML = "";
  host.append(el("a", { class: "link", href: "#", style: "font-size:12.5px;margin-right:12px",
    onclick: (e) => { e.preventDefault(); chrome.tabs.create({ url: `${API}/resume/pdf/${trackerId}` }); } },
    "Open résumé PDF"));
  if (hasCoverLetter) {
    host.append(el("a", { class: "link", href: "#", style: "font-size:12.5px",
      onclick: (e) => { e.preventDefault(); chrome.tabs.create({ url: `${API}/cover-letter/pdf/${trackerId}` }); } },
      "Open cover letter"));
  }
  const attachStatus = el("div", { class: "status" });
  const row = el("div", { class: "row", style: "margin-top:6px" });
  // Which résumé am I attaching? Show the folder (named after the company) so it's never a mystery.
  const which = el("div", { class: "muted", style: "font-size:11.5px;margin-top:8px" },
    "Attaching from this folder…");
  fetch(`${API}/tracker/${trackerId}`).then((r) => r.json()).then((rec) => {
    const folder = (rec.folder || "").split("/").filter(Boolean).pop() || "";
    const co = rec.company || "";
    which.innerHTML = "";
    which.append("📁 Attaching from: ",
      el("span", { class: "k", style: "color:var(--ink)" }, folder || co || "this job"),
      co && folder && !folder.toLowerCase().includes(co.toLowerCase().slice(0, 6))
        ? el("span", {}, `  (${co})`) : "");
  }).catch(() => { which.textContent = ""; });
  const mkAttach = (label, kind) => {
    const b = el("button", { class: "small" }, label);
    b.addEventListener("click", async () => {
      b.disabled = true;
      attachStatus.className = "status";
      attachStatus.textContent = "Attaching… (Chrome will show a debug banner for a moment)";
      try {
        const note = await attachFile(trackerId, kind);
        attachStatus.className = "status ok";
        attachStatus.textContent = note;
      } catch (e) {
        attachStatus.className = "status err";
        attachStatus.textContent = "Attach failed: " + e.message;
      } finally { b.disabled = false; }
    });
    return b;
  };
  row.append(mkAttach("📎 Attach résumé to page", "resume"));
  if (hasCoverLetter) row.append(mkAttach("📎 Attach cover letter", "cover"));
  host.append(which, row, attachStatus);
}

// Put this job's PDF into the page's file-upload input. Prefers a field whose label mentions
// resume/cv (or cover). Never submits anything.
async function attachFile(trackerId, kind) {
  const tab = await activeTab();
  if (!tab || !isWebUrl(tab.url)) throw new Error("open the application page first");
  const rec = await (await fetch(`${API}/tracker/${trackerId}`)).json();
  if (!rec || rec.error) throw new Error("job not found in the tracker");
  const fname = kind === "cover" ? (rec.cover_letter_filename || "cover_letter.pdf")
                                 : (rec.pdf_filename || "resume.pdf");
  const path = `${rec.folder}/${fname}`;

  const listed = await cdpListFileInputs(tab);
  if (!listed.ok) throw new Error(listed.error || "couldn't read the page");
  const inputs = listed.inputs || [];
  if (!inputs.length) throw new Error("no file-upload field found on this page");

  const want = kind === "cover" ? /cover/i : /resume|cv\b/i;
  const avoid = kind === "cover" ? /resume|cv\b/i : /cover/i;
  const score = (c) => {
    const s = `${c.kind || ""} ${c.label || ""}`;
    return (want.test(s) ? 2 : 0) - (avoid.test(s) ? 2 : 0);
  };
  const ranked = [...inputs].sort((a, b) => score(b) - score(a));
  const res = await cdpSetFileInput(tab, ranked[0].i, path);
  if (!res.ok) throw new Error(res.error || "couldn't place the file");
  return `Attached ${fname}` + (inputs.length > 1
    ? ` (picked the most ${kind}-looking of ${inputs.length} upload fields — verify on the page).`
    : " — verify it shows on the page.");
}

// Render the "which experience + how did you use it" form for weaving a skill in.
function showWeave(term, experiences, host, isNiche, reason) {
  const card = el("div", { class: "card" });
  card.append(el("div", { class: "k" },
    isNiche ? `“${term}” looks niche — weave it into an experience` : `Weave “${term}” into an experience`));
  if (isNiche && reason) card.append(el("div", { class: "muted", style: "font-size:11.5px" }, reason));

  const sel = el("select", {}, (experiences || []).map((e) =>
    el("option", { value: e.type + ":" + e.id }, e.label)));
  const how = el("input", { placeholder: "How did you use it? (e.g. to map points)" });
  const go = el("button", { class: "primary", style: "margin-top:8px" }, "Weave in");
  const status = el("div", { class: "status" });

  go.addEventListener("click", async () => {
    if (!how.value.trim()) { status.className = "status err"; status.textContent = "Say how you used it."; return; }
    const [type, id] = sel.value.split(":");
    go.disabled = true; status.className = "status"; status.textContent = "Weaving…";
    try {
      const r = await fetch(`${API}/profile/skills/weave`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ term, entry_type: type, entry_id: id, how: how.value.trim() }),
      });
      const res = await r.json();
      if (res.error) throw new Error(res.error);
      status.className = "status ok";
      card.innerHTML = "";
      card.append(el("div", { class: "ok", style: "color:var(--ok)" }, `✓ Woven into ${res.entry_label}`),
        el("div", { class: "muted", style: "font-size:12px;margin-top:4px" }, res.updated_bullet || ""));
    } catch (e) { go.disabled = false; status.className = "status err"; status.textContent = "Failed: " + e.message; }
  });

  card.append(el("label", { class: "lbl" }, "Experience / project"), sel, how, go, status);
  host.append(card);
}

// ---------------- passive learning feed ----------------
// The actual learning happens in learn.js (content script) + background maybeLearn(); the
// panel just shows the last few things it picked up so the user can see it working.
function renderLearnLog(log) {
  const host = $("learnLog");
  host.innerHTML = "";
  if (!(log || []).length) return;
  const card = el("div", { class: "card" }, el("div", { class: "k" }, "Recently learned"));
  for (const it of log) {
    card.append(el("div", { style: "margin:5px 0;font-size:12px" },
      el("span", { class: "k" }, it.label + "  "),
      el("span", { class: "muted" }, it.value)));
  }
  host.append(card);
}

// ---------------- Batch dashboard (background-driven, persistent, live) ----------------
const STORE = "rr_jobs";
const BATCH = "rr_batch";
const STATUS_LABEL = {
  reading: "reading page…", "jd-read": "JD read", analyzing: "analyzing JD…",
  "making-resume": "making resume…", "resume-done": "resume ready",
  "making-cover-letter": "making cover letter…", done: "resume + cover letter ready",
  flagged: "flagged", stopped: "stopped",
};
const SPINNING = new Set(["reading", "analyzing", "making-resume", "making-cover-letter"]);
const DONE_STATES = new Set(["done", "resume-done"]);
const expanded = new Set();   // jobIds whose JD preview is open (survives live re-renders)

function renderJobs(jobs) {
  const host = $("batchList"); host.innerHTML = "";
  const arr = Object.values(jobs || {}).sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
  if (!arr.length) {
    host.append(el("div", { class: "muted", style: "font-size:12px;margin-top:8px" },
      "No jobs yet — open job tabs and click above."));
    return;
  }
  for (const j of arr.slice(0, 3)) {   // only the 3 most recent — full list is on the dashboard
    const done = DONE_STATES.has(j.status);
    const icon = j.flagged ? "⚠" : done ? "✓" : j.status === "stopped" ? "⏹" : SPINNING.has(j.status) ? "⏳" : "•";
    const color = j.flagged ? "#b8860b" : done ? "var(--ok)" : "var(--muted)";

    const preview = el("div", {
      style: "display:" + (expanded.has(j.jobId) ? "block" : "none") +
        ";margin-top:8px;padding-top:8px;border-top:1px solid var(--line);" +
        "white-space:pre-wrap;font-size:11px;max-height:220px;overflow:auto;color:#333",
    }, j.jd_text ? j.jd_text.slice(0, 12000) : "(no JD text captured for this tab)");

    // Delete NEVER touches the tab — throwing away the documents and being done with the posting
    // are different decisions, and the tab is the user's to close.
    const del = el("button", { class: "small",
      style: "width:auto;padding:2px 7px;border-color:#e2b4b4;color:#c0392b",
      title: "Delete this job's résumé + cover letter — leaves the tab open",
      onclick: async (e) => {
        e.stopPropagation();
        del.disabled = true; del.textContent = "…";
        if (j.resume_id) { try { await fetch(`${API}/tracker/${j.resume_id}`, { method: "DELETE" }); } catch (err) {} }
        try { await bg("deleteJob", { jobId: j.jobId }); } catch (err) {}
      } }, "🗑");
    const header = el("div", { style: "cursor:pointer;display:flex;align-items:center;gap:6px",
      title: "Click to see the JD that was read",
      onclick: () => {
        const open = preview.style.display === "none";
        preview.style.display = open ? "block" : "none";
        open ? expanded.add(j.jobId) : expanded.delete(j.jobId);
      } },
      el("span", { style: `color:${color};font-weight:700` }, icon + " "),
      el("span", { class: "k", style: "flex:1" }, (j.company || j.title || j.host || "job").slice(0, 40)),
      el("span", { class: "muted", style: "font-size:11px" }, "▾"), del);

    const card = el("div", { class: "card" }, header,
      el("div", { class: "muted", style: "font-size:11.5px" }, (j.role ? j.role + " · " : "") + (j.host || "")),
      el("div", { class: "muted", style: "font-size:11.5px" },
        j.flagged ? "flagged: " + (j.reason || "") : (STATUS_LABEL[j.status] || j.status || "")));
    if (j.resume_id) {
      const links = el("div", {});
      links.append(el("a", { class: "link", href: "#", style: "font-size:12px;margin-right:12px",
        onclick: (e) => { e.preventDefault(); chrome.tabs.create({ url: `${API}/resume/pdf/${j.resume_id}` }); } },
        "Open résumé PDF"));
      if (j.status === "done") {
        links.append(el("a", { class: "link", href: "#", style: "font-size:12px",
          onclick: (e) => { e.preventDefault(); chrome.tabs.create({ url: `${API}/cover-letter/pdf/${j.resume_id}` }); } },
          "Open cover letter"));
      }
      card.append(links);
    }

    // JD couldn't be auto-detected → let the user paste it in and finish the pipeline.
    if (j.flagged && !j.jd_text && !j.resume_id) {
      const ta = el("textarea", { placeholder: "Paste the job description here…",
        style: "display:none;margin-top:8px;font-size:12px;min-height:120px" });
      const save = el("button", { class: "primary", style: "display:none;margin-top:6px;color:#fff",
        onclick: async () => {
          const txt = ta.value.trim();
          if (!txt) { ta.focus(); return; }
          save.disabled = true; save.textContent = "Working…";
          try { await bg("pasteJD", { jobId: j.jobId, jd_text: txt }); }
          catch (e) { save.disabled = false; save.textContent = "Make résumé + cover letter"; }
        } }, "Make résumé + cover letter");
      const toggle = el("button", { class: "small", style: "margin-top:8px",
        onclick: () => {
          const show = ta.style.display === "none";
          ta.style.display = save.style.display = show ? "block" : "none";
          if (show) ta.focus();
        } }, "📋 Paste JD manually");
      card.append(toggle, ta, save);
    }

    card.append(preview);
    host.append(card);
  }
  // Big dashboard button under the (max 3) job cards — full list + analytics live on the website.
  const dash = el("button", { class: "primary", style: "width:100%;margin-top:10px",
    onclick: () => chrome.tabs.create({ url: `${API}/app/dashboard.html` }) },
    arr.length > 3 ? `📊 Dashboard — see all ${arr.length} applications` : "📊 Open the dashboard");
  host.append(dash);
}

// Batch progress is derived from storage (rr_batch), not a long-lived message — the service
// worker can be evicted and restarted mid-batch without the panel losing track.
let batchTimer = null;
function renderBatchState(b) {
  const running = !!(b && b.active);
  $("batchJDBtn").disabled = running;
  $("stopBatchBtn").disabled = !running;
  clearInterval(batchTimer);
  if (running) {
    const started = b.startedAt || Date.now();
    const tick = () => {
      const el = Math.round((Date.now() - started) / 1000);
      // Show the budget counting down, so it's obvious the limit is armed and how long is left.
      const left = b.deadline
        ? ` ${Math.max(0, Math.ceil((b.deadline - Date.now()) / 60000))} min left of your limit.`
        : "";
      setBatch(`Working… ${el}s elapsed.${left} JD → resume → cover letter per job (~60-90s each).`);
    };
    tick();
    batchTimer = setInterval(tick, 1000);
  } else if (b && b.finished) {
    const f = b.finished;
    const secs = b.startedAt ? Math.round((f.endedAt - b.startedAt) / 1000) : 0;
    const mins = Math.round(secs / 60);
    setBatch(f.timedOut
      ? `Time limit reached after ${mins} min — ${f.done} jobs fully done. The rest are untouched; `
        + `press “Read JDs + make docs” again to carry on (finished jobs are skipped).`
      : f.stopped
        ? `Stopped after ${secs}s — ${f.done} jobs fully done so far.`
        : `Done in ${secs}s — ${f.done} resume+cover-letter sets, ${f.flagged} flagged. Nothing was submitted.`,
      f.stopped || f.timedOut ? "" : "ok");
  }
}

// Optional wall-clock budget for the whole batch (0 = no limit), remembered between runs.
const batchLimit = $("batchLimit");
batchLimit.addEventListener("change", () =>
  chrome.storage.local.set({ rr_batch_limit: Math.max(0, +batchLimit.value || 0) }));
chrome.storage.local.get("rr_batch_limit").then((r) => {
  if (typeof r.rr_batch_limit === "number") batchLimit.value = r.rr_batch_limit;
});

async function batchReadJDs() {
  const mins = Math.max(0, +batchLimit.value || 0);
  setBatch(mins ? `Starting… will stop after ${mins} min.` : "Starting…");
  try {
    const win = await chrome.windows.getCurrent();
    const res = await bg("runBatch", { windowId: win.id, limitMs: mins * 60000 });
    if (!res || !res.ok) throw new Error(res?.error || "failed");
    if (res.alreadyRunning) setBatch("Batch already running.");
  } catch (e) { setBatch("Batch failed to start: " + e.message, "err"); }
}

async function stopBatch() {
  $("stopBatchBtn").disabled = true;
  setBatch("Stopping after the current step…");
  try { await bg("stopBatch"); } catch (e) {}
}
function setBatch(msg, cls = "") { const s = $("batchStatus"); s.textContent = msg; s.className = "status " + cls; }

async function loadJobs() {
  try {
    renderJobs(await getJobs());
    const st = await chrome.storage.local.get([BATCH, "rr_learn_log"]);
    renderBatchState(st[BATCH]);
    renderLearnLog(st.rr_learn_log);
  } catch (e) { /* background not ready */ }
}
// Live updates: re-render whenever the background writes job, batch, or learn state.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "local") return;
  if (changes[STORE]) {
    renderJobs(changes[STORE].newValue || {});
    refreshActiveJob();
  }
  if (changes[BATCH]) renderBatchState(changes[BATCH].newValue);
  if (changes.rr_learn_log) renderLearnLog(changes.rr_learn_log.newValue);
});

// The single-job card follows the active browser tab.
chrome.tabs.onActivated.addListener(() => refreshActiveJob());
chrome.tabs.onUpdated.addListener((_id, info, tab) => {
  if (tab.active && (info.url || info.status === "complete")) refreshActiveJob();
});

// Scrape today's Austin/remote internships and open each Jobright posting as a background tab,
// so you can apply one by one in your real (logged-in) browser. Then the batch above can read
// those JDs and make a tailored resume + cover letter for each.
// Posting-age range slider: two handles (min age, max age, in days). 0 = current/just-posted.
const ageMin = $("ageMin"), ageMax = $("ageMax"), ageLabel = $("ageLabel");
function ageText(lo, hi) {
  const one = (n) => (n === 0 ? "current" : `${n} day${n === 1 ? "" : "s"}`);
  if (lo === hi) return lo === 0 ? "current (just posted)" : `${one(lo)} old`;
  return `${lo === 0 ? "current" : lo}–${hi} days old`;
}
function syncAge(fromMin) {
  let lo = +ageMin.value, hi = +ageMax.value;
  if (lo > hi) { if (fromMin) { hi = lo; ageMax.value = hi; } else { lo = hi; ageMin.value = lo; } }
  ageLabel.textContent = ageText(lo, hi);
  chrome.storage.local.set({ rr_intern_age: [lo, hi] });
}
ageMin.addEventListener("input", () => syncAge(true));
ageMax.addEventListener("input", () => syncAge(false));
chrome.storage.local.get("rr_intern_age").then((r) => {
  if (r.rr_intern_age) { ageMin.value = r.rr_intern_age[0]; ageMax.value = r.rr_intern_age[1]; }
  syncAge(true);
});

// How many of the best-ranked matches to actually open (0 = all).
const topN = $("topN");
topN.addEventListener("change", () => chrome.storage.local.set({ rr_intern_topn: +topN.value }));
chrome.storage.local.get("rr_intern_topn").then((r) => {
  if (typeof r.rr_intern_topn === "number") topN.value = r.rr_intern_topn;
});

async function waitTabsLoaded(tabIds, timeoutMs = 25000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const tabs = await Promise.all(tabIds.map((id) => chrome.tabs.get(id).catch(() => null)));
    if (tabs.every((t) => !t || t.status === "complete")) return;
    await new Promise((r) => setTimeout(r, 800));
  }
}

// Normalized key for "is this the same posting?" — host + path, dropping tracking-only query
// params (utm_*, src, source, ref) but keeping real job-id params (e.g. gh_jid).
function tabKey(u) {
  try {
    const x = new URL(u);
    const q = [...x.searchParams.entries()]
      .filter(([k]) => !/^utm_|^(src|source|ref|referrer|gh_src|gclid|fbclid)$/i.test(k))
      .sort().map(([k, v]) => k + "=" + v).join("&");
    return (x.hostname + x.pathname.replace(/\/+$/, "") + (q ? "?" + q : "")).toLowerCase();
  } catch (e) { return (u || "").toLowerCase(); }
}

// Persist every internship Find+open has opened, so closing tabs doesn't cause re-opens.
const OPENED_INTERNS = "rr_opened_interns";

async function getOpenedInternKeys() {
  const r = await chrome.storage.local.get(OPENED_INTERNS);
  return new Set(r[OPENED_INTERNS] || []);
}

async function rememberOpenedInterns(keys) {
  const prev = await getOpenedInternKeys();
  for (const k of keys) if (k) prev.add(k);
  await chrome.storage.local.set({ [OPENED_INTERNS]: [...prev] });
}

// Drop internships already open in a tab OR previously opened by Find+open. apply_url is a
// Simplify click-link that redirects to the real ATS, so we resolve each to its ATS URL
// (backend) before matching. Falls back to raw click URLs if the resolve call fails.
async function skipAlreadyOpen(jobs, setI) {
  const skipKeys = await getOpenedInternKeys();
  try {
    const tabs = await chrome.tabs.query({});
    for (const t of tabs) {
      const k = tabKey(t.url);
      if (k) skipKeys.add(k);
    }
  } catch (e) { /* tabs.query unavailable → still filter by history below */ }

  let resolved = {};
  try {
    setI("Checking which were already opened…");
    const rr = await fetch(`${API}/interns/resolve`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ urls: jobs.map((j) => j.apply_url) }),
    });
    if (rr.ok) resolved = (await rr.json()).resolved || {};
  } catch (e) { /* resolve unavailable → match on raw click URL only, below */ }

  const fresh = jobs.filter((j) => {
    const dest = resolved[j.apply_url] || j.apply_url;
    return !skipKeys.has(tabKey(dest)) && !skipKeys.has(tabKey(j.apply_url));
  });
  return { fresh, skipped: jobs.length - fresh.length, resolved };
}

// Scrape -> fit-filter -> drop already-opened -> first-of-day OA pin -> take the top N.
// Shared by Preview (which stops here) and Find+open (which then opens them), so the preview
// shows exactly what would open.
const FIRST_BATCH_DAY = "rr_first_batch_day";
const OA_PIN_COUNT = 1;

function todayKey() {
  // Local calendar day — "first batch of the day" means the user's day, not UTC.
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function coKey(name) {
  return (name || "").toLowerCase().replace(/[^a-z0-9]+/g, "");
}

/** First Find+open of the day: put up to 5 distinct OA-company jobs at the front. */
async function pinOaForFirstBatch(jobs) {
  const today = todayKey();
  const prev = (await chrome.storage.local.get(FIRST_BATCH_DAY))[FIRST_BATCH_DAY];
  if (prev === today) return { jobs, oaPinned: 0, firstBatch: false };

  const pinned = [], rest = [], seen = new Set();
  for (const j of jobs) {
    const oa = j.oa_company || (j.oa_priority ? j.company : "");
    const k = coKey(oa);
    if (oa && k && !seen.has(k) && pinned.length < OA_PIN_COUNT) {
      pinned.push(j);
      seen.add(k);
    } else {
      rest.push(j);
    }
  }
  return { jobs: [...pinned, ...rest], oaPinned: pinned.length, firstBatch: true };
}

async function rankedFreshInterns(setI) {
  const r = await fetch(`${API}/interns/today?min_days=${ageMin.value}&max_days=${ageMax.value}`);
  if (!r.ok) throw new Error("HTTP " + r.status);
  const res = await r.json();
  if (!res.ok) throw new Error(res.error || "scrape failed");
  const scraped = (res.jobs || []).filter((j) => j.apply_url);
  if (!scraped.length) return { error: "No matching internships right now — try again later today." };

  // Fit filter: skip postings clearly aimed at non-CS majors / off-target roles.
  const all = scraped.filter((j) => j.fit !== "weak");
  const poor = scraped.length - all.length;
  const poorNote = poor ? ` · ${poor} poor-fit skipped` : "";
  if (!all.length) {
    return { error: `Found ${scraped.length}, but none look like a good fit for you (all off-target — e.g. non-CS majors). Widen the age range or check the site directly.` };
  }

  const { fresh, skipped, resolved } = await skipAlreadyOpen(all, setI);
  const already = (skipped ? ` (${skipped} already opened before, skipped)` : "") + poorNote;
  if (!fresh.length) {
    return { error: `All ${all.length} matching internships were already opened before — nothing new to open.`, ok: true };
  }

  // Preview does not commit the "first batch" day — only an actual open does — so previewing
  // doesn't burn the OA pin for the day.
  const pinned = await pinOaForFirstBatch(fresh);
  const ordered = pinned.jobs;

  // Take the N best-fitting AFTER dedupe (and after OA pin), so N is the number of new tabs.
  const want = Math.max(0, +topN.value || 0);
  const jobs = want > 0 ? ordered.slice(0, want) : ordered;
  const trimmed = ordered.length - jobs.length;
  const trimNote = trimmed ? ` · top ${jobs.length} of ${ordered.length} by fit` : "";
  const oaNote = pinned.firstBatch && pinned.oaPinned
    ? ` · first batch today: 1 OA-company role up front`
    : "";
  return { jobs, fresh: ordered, resolved, already, trimNote, oaNote, firstBatch: pinned.firstBatch };
}

async function previewInterns() {
  const btn = $("previewInternsBtn"); btn.disabled = true;
  const setI = (m, c = "") => { const s = $("internsStatus"); s.textContent = m; s.className = "status " + c; };
  setI(`Scraping + ranking internships (${ageText(+ageMin.value, +ageMax.value)})…`);
  $("internsList").innerHTML = "";
  try {
    const r = await rankedFreshInterns(setI);
    if (r.error) { setI(r.error, r.ok ? "ok" : "err"); return; }
    renderJobRows("internsList", r.jobs);
    setI(`${r.jobs.length} would open${r.already}${r.trimNote}${r.oaNote || ""}. Scores explain the ranking. ` +
      `Hit “Find + open” to open them.`, "ok");
  } catch (e) {
    setI("Failed: " + e.message + " — is the server running?", "err");
  } finally { btn.disabled = false; }
}

async function openInterns(makeDocs) {
  const btns = [$("openInternsBtn"), $("openInternsDocsBtn"), $("previewInternsBtn")];
  btns.forEach((b) => (b.disabled = true));
  const setI = (m, c = "") => { const s = $("internsStatus"); s.textContent = m; s.className = "status " + c; };
  setI(`Scraping internships (${ageText(+ageMin.value, +ageMax.value)})…`);
  $("internsList").innerHTML = "";
  try {
    const r = await rankedFreshInterns(setI);
    if (r.error) { setI(r.error, r.ok ? "ok" : "err"); return; }
    const { jobs, resolved, already, trimNote, oaNote, firstBatch } = r;
    if (jobs.length > 40 && !confirm(`That's ${jobs.length} new internships — open all as tabs?`)) {
      setI(`${jobs.length} new found${already}. Narrow the age range, lower the top-N, or confirm to open them.`); return;
    }
    // Burn the first-batch pin only when we actually open tabs.
    if (firstBatch) await chrome.storage.local.set({ [FIRST_BATCH_DAY]: todayKey() });
    renderJobRows("internsList", jobs);   // show WHAT is being opened, with scores
    const tabIds = [];
    const remember = [];
    for (const j of jobs) {
      const tab = await chrome.tabs.create({ url: j.apply_url, active: false });
      tabIds.push(tab.id);
      remember.push(tabKey(j.apply_url));
      if (resolved[j.apply_url]) remember.push(tabKey(resolved[j.apply_url]));
    }
    await rememberOpenedInterns(remember);
    if (!makeDocs) {
      setI(`Opened ${jobs.length} new internships (${ageText(+ageMin.value, +ageMax.value)})${already}${trimNote}${oaNote || ""} as tabs. ` +
        `Apply one by one — or use the button below to auto-make a résumé + cover letter for each.`, "ok");
      return;
    }
    setI(`Opened ${jobs.length} new tabs${already}${trimNote}${oaNote || ""} — loading them, then making a résumé + cover letter for each…`);
    await waitTabsLoaded(tabIds);
    const bres = await bg("runBatch", { tabIds });
    if (bres && bres.alreadyRunning) setI("A batch is already running — let it finish, then retry.", "err");
    else setI(`Making docs for ${jobs.length} internships — watch the list below. Delete any you ` +
      `don't want (🗑) to delete its files — the tab stays open.`, "ok");
  } catch (e) {
    setI("Failed: " + e.message + " — is the server running?", "err");
  } finally { btns.forEach((b) => (b.disabled = false)); }
}

$("previewInternsBtn").addEventListener("click", previewInterns);
$("openInternsBtn").addEventListener("click", () => openInterns(false));
$("openInternsDocsBtn").addEventListener("click", () => openInterns(true));
$("clearOpenedInternsBtn").addEventListener("click", async () => {
  const n = (await getOpenedInternKeys()).size;
  if (!n) {
    $("internsStatus").textContent = "No opened-internship history to clear.";
    $("internsStatus").className = "status";
    return;
  }
  if (!confirm(`Forget ${n} previously opened internship link${n === 1 ? "" : "s"}? Find+open will open them again.`)) return;
  await chrome.storage.local.set({ [OPENED_INTERNS]: [] });
  $("internsStatus").textContent = `Cleared opened history (${n}).`;
  $("internsStatus").className = "status ok";
});
// --- Top-500 (S&P 500) company internships: search + list, open only on request ---------------
let TOP_CO_JOBS = [];

async function findTopCompanies() {
  const btn = $("findTopCoBtn"), openBtn = $("openTopCoBtn");
  const setT = (m, c = "") => { const s = $("topCoStatus"); s.textContent = m; s.className = "status " + c; };
  btn.disabled = true; openBtn.disabled = true;
  setT("Scanning the whole board for S&P 500 companies… (takes ~10s)");
  try {
    const n = Math.max(1, +$("topCoN").value || 30);
    const r = await fetch(`${API}/interns/top-companies?top_n=${n}`);
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.detail || err.error || "HTTP " + r.status);
    }
    const res = await r.json();
    if (!res.ok) throw new Error(res.error || "search failed");
    TOP_CO_JOBS = (res.jobs || []).filter((j) => j.apply_url);
    renderJobRows("topCoList", TOP_CO_JOBS);
    if (!TOP_CO_JOBS.length) { setT("No internships at S&P 500 companies on the board right now.", "err"); return; }
    setT(`${res.matched} internships at ${res.companies} S&P 500 companies (scanned ${res.scanned}). ` +
      `Showing the top ${TOP_CO_JOBS.length} by fit.`, "ok");
    openBtn.disabled = false;
  } catch (e) {
    setT("Failed: " + e.message + " — is the server running?", "err");
  } finally { btn.disabled = false; }
}

// Shared ranked-job list renderer (both the age-window search and the top-500 search use it), so
// every posting shows WHY it scored what it scored.
function renderJobRows(boxId, jobs) {
  const box = $(boxId);
  box.innerHTML = "";
  jobs.forEach((j) => {
    const row = document.createElement("div");
    row.className = "doc-row";
    row.style.cssText = "display:block;padding:7px 9px";
    const fitColor = j.fit_score >= 62 ? "var(--ok)" : j.fit_score >= 40 ? "var(--c-docs)" : "var(--muted)";
    const a = document.createElement("a");
    a.href = j.apply_url; a.target = "_blank"; a.className = "link";
    a.style.cssText = "font-weight:600;font-size:12.5px";
    a.textContent = j.title;
    const meta = document.createElement("div");
    meta.className = "hint";
    const co = j.sp500_company || j.oa_company || j.company || "—";
    const src = j.source ? ` · ${j.source}` : "";
    meta.textContent = `${co} · ${j.location || "—"}${j.season ? " · " + j.season : ""} · ${j.age}${src}`;
    if (j.oa_priority || j.oa_company) {
      const badge = document.createElement("span");
      badge.textContent = "OA";
      badge.title = "Company known for sending an online assessment";
      badge.style.cssText = "float:right;clear:right;margin:2px 0 0 6px;padding:1px 6px;"
        + "border-radius:4px;background:#1a5f4a;color:#fff;font-size:10px;font-weight:700;"
        + "letter-spacing:.03em";
      row.append(badge);
    }
    const score = document.createElement("span");
    score.style.cssText = `float:right;font-weight:700;font-size:11.5px;color:${fitColor}`;
    score.textContent = j.fit_score;
    row.append(score, a, meta);
    if (j.fit_reason) {
      const why = document.createElement("div");
      why.className = "hint"; why.style.opacity = ".8";
      why.textContent = j.fit_reason;
      row.append(why);
    }
    box.append(row);
  });
}

async function openTopCompanies() {
  const openBtn = $("openTopCoBtn");
  const setT = (m, c = "") => { const s = $("topCoStatus"); s.textContent = m; s.className = "status " + c; };
  if (!TOP_CO_JOBS.length) return;
  if (TOP_CO_JOBS.length > 25 &&
      !confirm(`Open ${TOP_CO_JOBS.length} tabs? Lower "Show top" first if that's too many.`)) return;
  openBtn.disabled = true;
  const remember = [];
  for (const j of TOP_CO_JOBS) {
    await chrome.tabs.create({ url: j.apply_url, active: false });
    remember.push(tabKey(j.apply_url));
  }
  await rememberOpenedInterns(remember);
  setT(`Opened ${TOP_CO_JOBS.length} tabs. Use “Read JDs + make docs” below to tailor a résumé for each.`, "ok");
  openBtn.disabled = false;
}

$("findTopCoBtn").addEventListener("click", findTopCompanies);
$("openTopCoBtn").addEventListener("click", openTopCompanies);

$("batchJDBtn").addEventListener("click", batchReadJDs);
$("stopBatchBtn").addEventListener("click", stopBatch);
$("refreshJobsBtn").addEventListener("click", loadJobs);
$("clearJobsBtn").addEventListener("click", async () => {
  await bg("clearJobs"); renderJobs({}); refreshActiveJob();
});
// Server status dot + reload button. An extension is sandboxed and can't launch a local
// process, so "start the server" can't run from here — the LaunchAgent keeps it running
// instead. This shows whether it's up and lets you re-check / reload the extension.
async function checkServer() {
  const dot = $("serverDot");
  try {
    const r = await fetch(`${API}/health`, { signal: AbortSignal.timeout(2500) });
    if (!r.ok) throw new Error();
    dot.textContent = "● server on"; dot.style.color = "var(--ok)";
  } catch (e) {
    dot.textContent = "● server off — run ./run.sh"; dot.style.color = "#c0392b";
  }
}
$("serverDot").addEventListener("click", checkServer);
$("openSiteBtn").addEventListener("click", () => chrome.tabs.create({ url: `${API}/app/` }));
$("reloadExtBtn").addEventListener("click", () => chrome.runtime.reload());
checkServer();
setInterval(checkServer, 15000);

$("editProfileLink")?.addEventListener("click", () => chrome.tabs.create({ url: `${API}/app/` }));
$("openAiAskBtn")?.addEventListener("click", () => chrome.tabs.create({ url: `${API}/app/ai-ask.html` }));

// One-click: read THIS tab's JD → make résumé + cover letter, instantly, with the default angle.
// Reuses the batch pipeline scoped to the current tab (JD-read → analyze → résumé → cover letter,
// with retries + the JD-not-found paste fallback). Progress renders in the card above.
async function makeDocsNow(btn) {
  const tab = await activeTab();
  if (!tab || !isWebUrl(tab.url)) return setStatus("Open a job posting tab first.", "err");
  currentJobId = rrJobId(tab.url);
  btn.disabled = true;
  setStatus("Reading the JD and making your résumé + cover letter… (~60–90s)");
  try {
    const res = await bg("runBatch", { tabIds: [tab.id] });
    if (res && res.alreadyRunning) setStatus("A batch is already running — let it finish, then retry.", "err");
    else setStatus("Working… progress shows in the card above.", "ok");
  } catch (e) { setStatus("Failed: " + e.message, "err"); }
  finally { btn.disabled = false; }
}
$("makeDocsBtn").addEventListener("click", () => makeDocsNow($("makeDocsBtn")));
loadJobs();
refreshActiveJob();
