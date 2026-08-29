// Smart document upload — works out WHICH file each upload area wants, then places the file.
//
// The hard part isn't the upload (chrome.debugger's DOM.setFileInputFiles does that); it's the
// identification. Most ATS render a styled dropzone and hide the real <input type=file>, so the
// input's own label/name/aria is empty and the identifying word ("Resume", "Cover Letter") lives
// somewhere else entirely: a heading above the field, the caption on the button beside it, the
// wrapper div's class name, or the accept attribute. rrProbeFileInputs() collects ALL of those as
// weighted text fragments; pickTargets() scores them and decides.
//
// Workday is NOT excluded: its "add your resume below" autofill step is an ordinary file input and
// is precisely the box worth filling. Only Workday's SKILLS and WORK-EXPERIENCE widgets need the
// separate flow in workday_skills.js.
(() => {
  const RX = {
    cover: /cover\s*-?\s*letter|coverletter|letter of (interest|introduction|motivation)/i,
    resume: /r[eé]sum[eé]|\bcv\b|curriculum vitae/i,
    transcript: /transcript|academic record|grade report/i,
    portfolio: /portfolio|work sample|writing sample/i,
  };
  // ATS-specific field names. These are exact and beat any amount of fuzzy page text.
  const ATS_NAMES = [
    { rx: /job_application\[resume/i, kind: "resume" },              // Greenhouse
    { rx: /job_application\[cover_letter/i, kind: "cover" },         // Greenhouse
    { rx: /_systemfield_resume|resumeUpload/i, kind: "resume" },     // Ashby / Lever
    { rx: /_systemfield_cover|coverLetterUpload/i, kind: "cover" },
    { rx: /\bresume\b|\bcv\b/i, kind: "resume" },
    { rx: /\bcover\b/i, kind: "cover" },
  ];

  // Runs IN THE PAGE (chrome.scripting, isolated world — no debugger banner just to read the DOM).
  function rrProbeFileInputs() {
    const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
    const T = (el) => clean(el && el.textContent).slice(0, 300);

    function context(el) {
      const bits = [];
      const push = (v, w) => { const t = clean(v); if (t) bits.push({ t, w }); };

      // 1) the input's own identifiers — empty on hidden dropzone inputs, gold when present
      if (el.labels) [...el.labels].forEach((l) => push(T(l), 3));
      const lb = el.getAttribute("aria-labelledby");
      if (lb) lb.split(/\s+/).forEach((id) => push(T(document.getElementById(id)), 3));
      push(el.getAttribute("aria-label"), 3);
      push(el.name, 2.5);
      push(el.id, 2);
      push(el.getAttribute("data-qa") || el.getAttribute("data-testid"), 2);

      // 2) the wrapper chain: class names ("resume-dropzone") and the dropzone's own copy
      //    ("Drag your resume here"). Closer ancestors count for more.
      let p = el.parentElement, hop = 0;
      while (p && hop < 5) {
        push(p.className && String(p.className), 2 - hop * 0.3);
        push(p.getAttribute && p.getAttribute("data-testid"), 2 - hop * 0.3);
        const txt = T(p);
        // only trust a wrapper's text while it's small enough to still be about THIS field
        if (txt && txt.length < 160) push(txt, 2.2 - hop * 0.35);
        hop++; p = p.parentElement;
      }

      // 3) the nearest heading/label that PRECEDES the input in document order — where the word
      //    "Resume" usually sits when the input itself is anonymous.
      const heads = [...document.querySelectorAll("h1,h2,h3,h4,h5,h6,legend,label,strong,b,[role=heading]")];
      let best = null;
      for (const h of heads) {
        if (h.contains(el)) continue;
        if (!(h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)) continue;
        best = h;   // keep the last heading before the input
      }
      if (best) push(T(best), 2.6);

      // 4) immediate siblings — button captions like "Attach Resume"
      if (el.previousElementSibling) push(T(el.previousElementSibling), 2);
      if (el.nextElementSibling) push(T(el.nextElementSibling), 1.5);

      return bits;
    }

    return [...document.querySelectorAll('input[type=file]')].map((el, i) => {
      const r = el.getBoundingClientRect();
      return {
        i, bits: context(el),
        accept: el.getAttribute("accept") || "",
        name: el.name || el.id || "",
        multiple: !!el.multiple,
        hidden: r.width === 0 && r.height === 0,
        top: Math.round(r.top + (window.scrollY || 0)),
      };
    });
  }

  // Score one probed input for each document kind. Weighted so a heading two hops up can still
  // beat a stray class name, and so an explicit ATS field name settles it outright.
  function scoreInput(inp) {
    const s = { resume: 0, cover: 0, transcript: 0, portfolio: 0 };
    for (const { t, w } of inp.bits || []) {
      for (const kind of Object.keys(RX)) if (RX[kind].test(t)) s[kind] += w;
    }
    for (const { rx, kind } of ATS_NAMES) {
      if (inp.name && rx.test(inp.name)) { s[kind] += 6; break; }
    }
    // "Resume or Cover Letter" combined areas: the phrase 'cover letter' also matched, but the
    // area's real job is the résumé. Only demote cover when resume is present in the SAME text.
    for (const { t } of inp.bits || []) {
      if (RX.resume.test(t) && RX.cover.test(t)) { s.cover -= 1.5; break; }
    }
    return s;
  }

  // Decide which input gets which file. Returns {resume, cover, transcript, notes[]} of indices.
  function pickTargets(inputs) {
    const notes = [];
    const scored = inputs.map((inp) => ({ inp, s: scoreInput(inp) }));
    const pick = (kind, exclude = []) => {
      const cands = scored
        .filter((x) => !exclude.includes(x.inp.i) && x.s[kind] > 0)
        .sort((a, b) => b.s[kind] - a.s[kind]);
      return cands.length ? cands[0].inp.i : null;
    };

    let resume = pick("resume");
    let cover = pick("cover", resume === null ? [] : [resume]);
    const transcript = pick("transcript", [resume, cover].filter((x) => x !== null));

    // --- fallbacks, for pages where nothing says "resume" anywhere near the input ---
    if (resume === null && cover === null) {
      if (inputs.length === 1) {
        resume = inputs[0].i;
        notes.push("only one upload field on the page — treated it as the résumé");
      } else if (inputs.length === 2) {
        // Overwhelmingly the convention: résumé first, cover letter second, top to bottom.
        const order = [...inputs].sort((a, b) => a.top - b.top);
        resume = order[0].i; cover = order[1].i;
        notes.push("two unlabelled upload fields — used page order (résumé first, cover second)");
      }
    } else if (resume === null && cover !== null && inputs.length > 1) {
      // A cover field was identified but no résumé one: the résumé is almost certainly the other.
      const other = inputs.find((x) => x.i !== cover);
      if (other) { resume = other.i; notes.push("résumé field inferred as the non-cover-letter upload"); }
    }
    return { resume, cover, transcript, notes, scored };
  }

  // ---- placement (Chrome: debugger disk path; Firefox: backend blob + DataTransfer) ----
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function smartUpload(tab, files, onStatus) {
    const say = (m) => { if (onStatus) onStatus(m); };

    say("Looking for upload fields…");
    let probed = [];
    try {
      const [res] = await chrome.scripting.executeScript({
        target: { tabId: tab.id }, func: rrProbeFileInputs });
      probed = (res && res.result) || [];
    } catch (e) { return { error: "couldn't read the page (" + String(e).slice(0, 60) + ")" }; }

    if (!probed.length) return { error: "no upload field on this page" };

    const plan = pickTargets(probed);
    const jobs = [];
    if (files.resume && plan.resume !== null) jobs.push({ kind: "résumé", i: plan.resume, path: files.resume });
    if (files.cover && plan.cover !== null) jobs.push({ kind: "cover letter", i: plan.cover, path: files.cover });
    if (files.transcript && plan.transcript !== null)
      jobs.push({ kind: "transcript", i: plan.transcript, path: files.transcript });

    const out = { placed: [], failed: [], notes: plan.notes, fields: probed.length };
    if (!jobs.length) {
      out.error = "found " + probed.length + " upload field(s) but couldn't tell what they want";
      return out;
    }

    const useCdp = typeof rrHasDebugger === "function" && rrHasDebugger();
    let cmd = null;
    if (useCdp) {
      const acq = await rrCdpAcquire(tab.id);
      if (!acq.ok) { out.error = acq.error; return out; }
      const target = { tabId: tab.id };
      cmd = (m, p) => chrome.debugger.sendCommand(target, m, p || {});
    }
    try {
      for (const j of jobs) {
        say(`Uploading ${j.kind}…`);
        if (useCdp) {
          const objRes = await cmd("Runtime.evaluate", {
            expression: `document.querySelectorAll('input[type=file]')[${j.i}]`, returnByValue: false });
          const objectId = objRes && objRes.result && objRes.result.objectId;
          if (!objectId) { out.failed.push({ ...j, why: "field disappeared" }); continue; }
          await cmd("DOM.setFileInputFiles", { files: [j.path], objectId });
          await sleep(450);
          const chk = await cmd("Runtime.evaluate", {
            expression: `(document.querySelectorAll('input[type=file]')[${j.i}].files||[]).length>0`,
            returnByValue: true });
          if (chk && chk.result && chk.result.value) out.placed.push(j);
          else out.failed.push({ ...j, why: "the page rejected the file" });
        } else {
          const r = await rrPlaceFileBlob(tab.id, j.i, j.path);
          if (r.ok) out.placed.push(j);
          else out.failed.push({ ...j, why: r.error || "the page rejected the file" });
        }
      }
    } finally { if (useCdp) await rrCdpRelease(tab.id); }
    return out;
  }

  // Which generated documents belong to the job in THIS browser tab? Mirrors the Docs tab's
  // lookup: active tab -> rr_jobs entry -> tracker row -> absolute file paths.
  async function jobDocsForTab(tab) {
    let tid = null;
    try {
      const jobs = await getJobs();
      let jid = null;
      try { jid = rrJobId(tab.url); } catch (e) {}
      if (jid && jobs[jid] && jobs[jid].resume_id) tid = jobs[jid].resume_id;
      if (!tid) {
        let host = ""; try { host = new URL(tab.url).hostname; } catch (e) {}
        const m = Object.values(jobs)
          .filter((j) => j.resume_id && j.host && host && j.host === host)
          .sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0))[0];
        if (m) tid = m.resume_id;
      }
    } catch (e) { /* fall through — reported as "no documents" below */ }
    if (!tid) return { error: "no résumé generated for this tab's job yet" };
    try {
      const rec = await (await fetch(`${API}/tracker/${tid}`)).json();
      if (!rec || rec.error) return { error: "job not found in the tracker" };
      return {
        tracker_id: tid,
        resume: `${rec.folder}/${rec.pdf_filename || "resume.pdf"}`,
        cover: rec.cover_letter_filename ? `${rec.folder}/${rec.cover_letter_filename}` : null,
      };
    } catch (e) { return { error: "backend not reachable" }; }
  }

  window.rrJobDocsForTab = jobDocsForTab;
  window.rrProbeFileInputs = rrProbeFileInputs;
  window.rrPickUploadTargets = pickTargets;
  window.rrScoreUploadInput = scoreInput;
  window.smartUpload = smartUpload;
})();
