// Docs tab — instant access to the files you attach during applications (resumes, cover letters,
// transcript, schedule). Tap a doc and it drops into the page's upload field via chrome.debugger
// (cdp_input.js). If the page has no real <input type=file>, we fall back to copying the file's
// path so you can jump to it in the macOS Open dialog with ⌘⇧G → ⌘V → Enter — no folder hunting.
(() => {
  const KIND_ICON = { resume: "📄", cover: "✉️", transcript: "🎓", schedule: "🗓", doc: "📎", other: "📎" };
  let DATA = null;      // last /documents payload, for client-side search
  let activeTid = null; // tracker_id of the job in the ACTIVE browser tab (from the batch), or null

  // Attaching is offered from the Docs tab AND from the compact strip in Ask, so the status line
  // and the "which upload field?" chooser have to appear in whichever card was tapped. Docs is the
  // one place that IS allowed to grow: a page with several upload boxes has to ask which one.
  const DOC_UIS = {
    docs: { body: "docsBody", status: "docsStatus" },
    ask: { body: "askDocsBody", status: "askDocsStatus" },
  };
  let docUi = DOC_UIS.docs;

  // Map the active browser tab -> the resume it generated, via rr_jobs (same logic My Info uses).
  // Every job the batch processed stores `resume_id` on its rr_jobs entry.
  async function resolveActiveTid() {
    try {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (!tab || !tab.url) return null;
      const jobs = await getJobs();
      let jid = null;
      try { jid = rrJobId(tab.url); } catch (e) {}
      if (jid && jobs[jid] && jobs[jid].resume_id) return jobs[jid].resume_id;
      // Path changed (Workday etc.) -> fall back to the most recent job on the same host.
      let host = ""; try { host = new URL(tab.url).hostname; } catch (e) {}
      const m = Object.values(jobs)
        .filter((j) => j.resume_id && j.host && host && j.host === host)
        .sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0))[0];
      return m ? m.resume_id : null;
    } catch (e) { return null; }
  }

  // The resume + cover letter generated for the active tab's job (found in the already-loaded list).
  function thisTabDocs() {
    if (!activeTid || !DATA) return [];
    const r = (DATA.resumes || []).find((d) => d.tracker_id === activeTid);
    const c = (DATA.cover_letters || []).find((d) => d.tracker_id === activeTid);
    return [r, c].filter(Boolean);
  }

  function setSt(msg, cls = "") {
    const s = $(docUi.status); if (!s) return;
    s.textContent = msg; s.className = "status " + cls;
  }

  async function copyText(v) {
    try { await navigator.clipboard.writeText(v); return true; }
    catch (e) {
      const ta = document.createElement("textarea");
      ta.value = v; document.body.append(ta); ta.select();
      const ok = document.execCommand("copy"); ta.remove(); return ok;
    }
  }

  // Fallback when we can't place the file directly: copy its path + remind the ⌘⇧G trick.
  async function fallbackCopyPath(doc, lead) {
    await copyText(doc.path);
    setSt(`${lead} Path copied — in the file dialog press ⌘⇧G, ⌘V, then Enter.`, "");
  }

  async function attachDoc(doc, from) {
    if (from) docUi = DOC_UIS[from] || docUi;
    setSt(`Attaching “${doc.name}”…`);
    let tab;
    try { tab = await activeTab(); } catch (e) { tab = null; }
    if (!tab || !isWebUrl(tab.url)) return fallbackCopyPath(doc, "Open a job/application page first.");

    const listed = await cdpListFileInputs(tab);
    if (!listed.ok) return fallbackCopyPath(doc, listed.error + ".");
    const inputs = listed.inputs || [];
    if (inputs.length === 0)
      return fallbackCopyPath(doc, "No upload field found on this page (custom uploader).");
    if (inputs.length === 1) return doSet(tab, inputs[0].i, doc);

    // Several upload fields → let the user pick which one (the smart-attach chooser).
    setSt(`“${doc.name}” — which upload field?`);
    renderChooser(tab, doc, inputs);
  }

  async function doSet(tab, index, doc) {
    setSt(`Placing “${doc.name}”…`);
    const res = await cdpSetFileInput(tab, index, doc.path);
    if (res.ok) setSt(`✓ Attached “${doc.name}” to the page.`, "ok");
    else await fallbackCopyPath(doc, (res.error || "Couldn't place the file") + ".");
  }

  function renderChooser(tab, doc, inputs) {
    const host = $(docUi.body) || $("docsBody");
    const old = document.getElementById("docChooser");
    if (old) old.remove();
    const box = el("div", { id: "docChooser", class: "card", style: "border-color:var(--accent)" },
      el("div", { class: "card-sub" }, `Pick the field for “${doc.name}”:`));
    inputs.forEach((inp) => {
      const label = inp.label ? inp.label : `${inp.kind} field #${inp.i + 1}`;
      const b = el("button", { class: "small", style: "margin-top:5px;width:100%;text-align:left" },
        `${KIND_ICON[inp.kind] || "📎"}  ${label}`);
      b.addEventListener("click", async () => { box.remove(); await doSet(tab, inp.i, doc); });
      box.append(b);
    });
    const cancel = el("button", { class: "small", style: "margin-top:6px" }, "Cancel");
    cancel.addEventListener("click", () => { box.remove(); setSt(""); });
    box.append(cancel);
    host.prepend(box);
  }

  async function reveal(doc, btn) {
    const old = btn.textContent; btn.textContent = "…";
    try {
      const r = await (await fetch(`${API}/documents/reveal`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: doc.path }),
      })).json();
      if (!r.ok) { await copyText(doc.path); setSt("Couldn't open Finder — path copied instead.", ""); }
    } catch (e) { await copyText(doc.path); setSt("Backend error — path copied instead.", "err"); }
    btn.textContent = old;
  }

  function docRow(doc) {
    const sub = doc.kind === "resume" || doc.kind === "cover"
      ? [doc.company, doc.date].filter(Boolean).join(" · ")
      : (doc.ext || "").toUpperCase();
    const main = el("button", { class: "doc-main", title: "Attach to this page (or copy path)" },
      el("div", { class: "doc-name" }, doc.name),
      sub ? el("div", { class: "doc-sub" }, sub) : "");
    main.addEventListener("click", () => attachDoc(doc, "docs"));

    const copyBtn = el("button", { title: "Copy file path (for the ⌘⇧G file dialog)" }, "⧉");
    copyBtn.addEventListener("click", async () => {
      await copyText(doc.path);
      setSt("Path copied. In the file dialog: ⌘⇧G, ⌘V, Enter.", "ok");
    });
    const revealBtn = el("button", { title: "Reveal in Finder" }, "⤢");
    revealBtn.addEventListener("click", () => reveal(doc, revealBtn));
    const openBtn = el("button", { title: "Open / preview" }, "↗");
    openBtn.addEventListener("click", () =>
      chrome.tabs.create({ url: `${API}/documents/raw?path=${encodeURIComponent(doc.path)}` }));

    const row = el("div", { class: "doc-row" },
      el("span", { class: "doc-kind" }, KIND_ICON[doc.kind] || "📎"),
      main,
      el("div", { class: "doc-acts" }, copyBtn, revealBtn, openBtn));
    row.dataset.search = [doc.name, doc.company, doc.role, doc.kind, doc.filename]
      .filter(Boolean).join(" ").toLowerCase();
    return row;
  }

  function sectionEl(id, icon, title, docs) {
    if (!docs.length) return null;
    const wrap = el("div", { id, class: "doc-section" },
      el("div", { class: "doc-sec-title" }, `${icon} ${title} (${docs.length})`));
    docs.forEach((d) => wrap.append(docRow(d)));
    return wrap;
  }

  // ---- the compact strip in Ask ------------------------------------------------------------
  // Four documents, four small buttons, one row. Same tap-to-attach as the Docs tab (including the
  // chooser when a page has more than one upload box) — just without the search, the sections and
  // the per-doc copy/reveal/open actions, none of which you want mid-application.
  //
  // Which four: this tab's job first (its résumé and cover letter are the ones you actually need),
  // then whatever you reach for most, then the newest résumés. Never the same file twice.
  function quickDocs(n = 4) {
    const out = [], seen = new Set();
    const take = (list) => {
      for (const d of list || []) {
        if (out.length >= n) return;
        if (!d || seen.has(d.path)) continue;
        seen.add(d.path); out.push(d);
      }
    };
    take(thisTabDocs());
    take(DATA && DATA.frequent);
    take(DATA && DATA.resumes);
    take(DATA && DATA.cover_letters);
    return out;
  }

  // Resumes and cover letters are named after the job, so the company is the useful half — the
  // full filename would be ellipsised into uselessness at this width.
  const shortLabel = (d) =>
    ((d.kind === "resume" || d.kind === "cover") && d.company) ? d.company : d.name;

  function renderQuick() {
    const row = $("askDocsRow");
    if (!row) return;
    row.innerHTML = "";
    const docs = quickDocs(4);
    if (!docs.length) {
      row.append(el("div", { class: "hint" },
        "No documents yet — generate a résumé in the Jobs tab, or drop files in your documents folder."));
      return;
    }
    for (const d of docs) {
      const b = el("button", { class: "small quick-doc", title: `${d.name} — tap to attach to this page` },
        `${KIND_ICON[d.kind] || "📎"} ${shortLabel(d)}`);
      b.addEventListener("click", () => attachDoc(d, "ask"));
      row.append(b);
    }
  }

  function render() {
    renderQuick();
    const body = $("docsBody");
    body.innerHTML = "";
    if (!DATA) return;
    const secs = [];
    const tabDocs = thisTabDocs();
    if (tabDocs.length)   // the doc(s) for the job you're currently looking at — pinned on top
      secs.push(["sec-thistab", "📍", "This tab's job", tabDocs]);
    secs.push(
      ["sec-frequent", "⭐", "Frequently used", DATA.frequent || []],
      ["sec-resumes", "📄", "Resumes", DATA.resumes || []],
      ["sec-covers", "✉️", "Cover letters", DATA.cover_letters || []]);
    // Jump-nav: sticky chips that scroll to each present section.
    const present = secs.filter(([, , , d]) => d.length);
    if (present.length > 1) {
      const nav = el("div", { class: "jumpnav" });
      present.forEach(([id, icon, title]) => {
        const b = el("button", {}, icon);
        b.title = title;
        b.addEventListener("click", () =>
          document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" }));
        nav.append(b);
      });
      const bar = document.getElementById("appbar");
      if (bar) nav.style.top = bar.offsetHeight + "px";
      body.append(nav);
    }
    let any = false;
    for (const [id, icon, title, docs] of secs) {
      const s = sectionEl(id, icon, title, docs);
      if (s) { body.append(s); any = true; }
    }
    if (!any) body.append(el("div", { class: "doc-empty" },
      "No documents yet. Generate resumes in the Jobs tab, or drop files into your documents folder."));
    applyFilter();
  }

  function applyFilter() {
    const q = ($("docSearch").value || "").trim().toLowerCase();
    document.querySelectorAll("#docsBody .doc-section").forEach((sec) => {
      let shown = 0;
      sec.querySelectorAll(".doc-row").forEach((row) => {
        const hit = !q || (row.dataset.search || "").includes(q);
        row.style.display = hit ? "" : "none";
        if (hit) shown++;
      });
      sec.style.display = shown ? "" : "none";
    });
  }

  async function load() {
    setSt("Loading documents…");
    try {
      const r = await fetch(`${API}/documents`);
      if (!r.ok) throw new Error("HTTP " + r.status);
      DATA = await r.json();
    } catch (e) {
      setSt("Backend error — is ./run.sh running? (" + e.message + ")", "err");
      return;
    }
    setSt("");
    activeTid = await resolveActiveTid();
    render();
  }

  // ---- auto-upload toggle -------------------------------------------------
  function setPickerSt(msg, cls = "") {
    const el2 = $("pickerStatus"); if (!el2) return;
    el2.textContent = msg; el2.className = "status " + cls;
  }

  async function refreshUploadToggle() {
    const aaRes = await bg("autoAttachMode", {}).catch(() => null);
    if (aaRes && aaRes.mode && autoAttachSel) autoAttachSel.value = aaRes.mode;
  }

  // Shows exactly what the page offered and how each field was read, so a failure is diagnosable
  // instead of silent.
  function renderAttachDetail(rep) {
    const host = $("docsBody");
    document.getElementById("attachDetail")?.remove();
    if (!rep || !(rep.areas || []).length) return;
    const card = el("div", { id: "attachDetail", class: "card", style: "margin-bottom:8px" },
      el("div", { class: "k" }, `Upload areas on this page (${rep.areas.length})`));
    for (const a of rep.areas) {
      const label = a.kind
        ? `${a.kind}${a.mode === "autofill" ? " · autofills the form" : a.mode === "inert" ? " · attachment only" : ""}`
        : "not recognised";
      card.append(el("div", { style: "font-size:11.5px;margin-top:4px" },
        el("span", { class: "k" }, `#${a.index + 1} ${label}${a.hasFile ? " (already has a file)" : ""}`),
        el("div", { class: "muted", style: "font-size:11px" }, a.text || "(no text near it)")));
    }
    host.prepend(card);
  }

  $("healthBtn")?.addEventListener("click", async () => {
    setPickerSt("Checking…");
    try {
      const r = await bg("health");
      const h = (r && r.health) || {};
      const lines = [
        `storage: ${(h.storageBytes / 1024).toFixed(0)} KB used`,
        `  biggest: ${(h.biggest || []).join(", ")}`,
        `profile cached (drives the suggestion chips): ${h.profileCached ? "yes" : "NO"}`,
        `jobs tracked: ${h.jobs} (${h.jobsWithResume} with a résumé)`,
        `batch running: ${h.batchActive ? "yes" : "no"}`,
        `auto-upload: ${h.autoAttach}`,
      ];
      if (h.lastAttach) {
        lines.push(`last attach: ${(h.lastAttach.placed || []).join(", ") || "nothing"}`);
        (h.lastAttach.steps || []).slice(-3).forEach((st) => lines.push("  · " + st));
      }
      const host = $("docsBody");
      document.getElementById("healthBox")?.remove();
      host.prepend(el("div", { id: "healthBox", class: "card", style: "margin-bottom:8px" },
        el("div", { class: "k" }, "🩺 Extension health"),
        ...lines.map((t) => el("div", { style: "font-size:11.5px;white-space:pre-wrap;margin-top:2px" }, t))));
      setPickerSt("", "");
    } catch (e) {
      // A dead service worker is exactly what this button exists to reveal.
      setPickerSt("No answer from the background worker — check chrome://extensions → "
                  + "service worker → Errors. (" + e.message + ")", "err");
    }
  });

  const autoAttachSel = $("autoAttachMode");
  autoAttachSel?.addEventListener("change", async () => {
    const r = await bg("autoAttachMode", { mode: autoAttachSel.value }).catch(() => null);
    setPickerSt(r && r.mode === "on"
      ? "Auto-upload on: matching upload boxes fill as pages load."
      : "Auto-upload off.", "ok");
  });

  $("autoAttachNowBtn")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true; setPickerSt("Looking for upload areas…");
    try {
      const tab = await activeTab();
      const r = await bg("autoAttachNow", { tabId: tab.id });
      const rep = (r && r.report) || {};
      if (rep.ok) {
        const skip = (rep.skipped || []).length
          ? ` Skipped ${rep.skipped.length} attachment-only box.` : "";
        setPickerSt(`Attached: ${(rep.placed || []).join(", ")}.${skip}`, "ok");
      } else {
        // Say WHY. "Nothing happened" with no reason is what made this impossible to debug.
        setPickerSt("Didn't attach: " + ((rep.steps || []).slice(-1)[0] || "unknown reason"), "err");
      }
      renderAttachDetail(rep);
    } catch (err) {
      setPickerSt("Failed: " + err.message, "err");
    } finally { btn.disabled = false; }
  });

  // wiring
  $("docSearch")?.addEventListener("input", applyFilter);
  $("docsRefreshBtn")?.addEventListener("click", load);
  $("docsOpenFolderBtn")?.addEventListener("click", async () => {
    try {
      const r = await (await fetch(`${API}/documents/open-folder`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
      })).json();
      if (!r.ok) setSt("Couldn't open the folder (macOS only).", "");
    } catch (e) { setSt("Backend error opening folder.", "err"); }
  });

  // Reload every time Docs is shown, so resumes/cover letters made by a batch (run from the Jobs
  // tab) appear the moment you switch over — no manual Refresh needed.
  let docsVisible = false;
  let askVisible = false;
  let refreshTimer = null;
  document.addEventListener("rr-tab-shown", (e) => {
    docsVisible = e.detail.tab === "docs";
    askVisible = e.detail.tab === "ask";
    // Status text follows the visible card, so a message never lands on the tab you left.
    if (docsVisible) docUi = DOC_UIS.docs;
    else if (askVisible) docUi = DOC_UIS.ask;
    // Ask shows the same four documents, so it needs the same fresh list — and the same follow of
    // whichever job posting is in front.
    if (docsVisible || askVisible) load();
    if (docsVisible) refreshUploadToggle();
  });

  // If a batch finishes WHILE you're already looking at Docs, its writes to rr_jobs land here —
  // debounce-reload so new docs pop in live. (Only while Docs is the visible tab.)
  chrome.storage.onChanged.addListener((changes, area) => {
    if (!(docsVisible || askVisible) || area !== "local" || !("rr_jobs" in changes)) return;
    clearTimeout(refreshTimer);
    refreshTimer = setTimeout(load, 800);
  });

  // Follow the active browser tab: when you switch to a different job posting, re-pin ITS resume +
  // cover at the top. No re-fetch — just recompute the pinned section from the loaded list.
  let tabTimer = null;
  const followActiveTab = () => {
    if (!(docsVisible || askVisible) || !DATA) return;
    clearTimeout(tabTimer);
    tabTimer = setTimeout(async () => {
      const next = await resolveActiveTid();
      if (next !== activeTid) { activeTid = next; render(); }
    }, 200);
  };
  chrome.tabs.onActivated.addListener(followActiveTab);
  chrome.tabs.onUpdated.addListener((_id, info, tab) => {
    if (tab.active && (info.status === "complete" || info.url)) followActiveTab();
  });
})();
