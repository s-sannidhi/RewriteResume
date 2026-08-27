// File-attach via the Chrome DevTools Protocol. Putting a file into a page's <input type=file>
// needs a real local file path, which ONLY chrome.debugger's DOM.setFileInputFiles can do —
// synthetic events can't fabricate a File. So this file is now purely about attaching documents:
//   • cdpListFileInputs / cdpSetFileInput  → the Docs tab's one-tap attach
//   • cdpUpload                            → auto-upload this job's résumé + cover letter
// Field *filling* no longer lives here — that moved to the React-props engine in autofill.js
// (rrApplyActionsReact), so there's no more "debugging" banner just to type into a form.
(() => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // Upload résumé (always) + cover letter (ONLY if the page actually has a cover-letter upload
  // area). Classifies each <input type=file> by its label/context so the cover letter is never
  // dumped into the résumé slot when there's no cover area. Returns {resume, cover, error?}.
  async function cdpUpload(tab, trackerId, onStatus) {
    let rec;
    try { rec = await (await fetch(`${API}/tracker/${trackerId}`)).json(); } catch (e) { return { error: "backend error" }; }
    if (!rec || rec.error) return { error: "job not found in tracker" };
    const folder = rec.folder;
    const resumePath = `${folder}/${rec.pdf_filename || "resume.pdf"}`;
    const coverPath = rec.cover_letter_filename ? `${folder}/${rec.cover_letter_filename}` : null;

    const target = { tabId: tab.id };
    const acq = await rrCdpAcquire(tab.id);
    if (!acq.ok) return { error: acq.error };
    const cmd = (m, p) => chrome.debugger.sendCommand(target, m, p || {});
    const out = { resume: "notfound", cover: coverPath ? "notfound" : "skipped-nofile" };
    try {
      // classify every file input by its surrounding text (label / aria / wrapper / name / accept)
      const listExpr = `(()=>{
        const ins=[...document.querySelectorAll('input[type=file]')];
        const ctx=(el)=>{let t='';if(el.labels)t+=[...el.labels].map(l=>l.textContent).join(' ');
          const lb=el.getAttribute('aria-labelledby');if(lb)lb.split(/\\s+/).forEach(id=>{const e=document.getElementById(id);if(e)t+=' '+e.textContent;});
          if(el.getAttribute('aria-label'))t+=' '+el.getAttribute('aria-label');
          let p=el.parentElement,h=0;while(p&&h<4){const l=p.querySelector('label,legend,.label,[class*=label]');if(l)t+=' '+l.textContent;h++;p=p.parentElement;}
          t+=' '+(el.name||'')+' '+(el.id||'')+' '+(el.getAttribute('accept')||'');return t.toLowerCase();};
        return ins.map((el,i)=>{const c=ctx(el);const kind=/cover\\s*letter|coverletter|\\bcover\\b/.test(c)?'cover':(/resume|\\bcv\\b|résumé/.test(c)?'resume':'other');return {i,kind,ctx:c.replace(/\\s+/g,' ').trim().slice(0,100)};});
      })()`;
      const listRes = await cmd("Runtime.evaluate", { expression: listExpr, returnByValue: true });
      const list = (listRes && listRes.result && listRes.result.value) || [];
      if (!list.length) { out.error = "no file-upload field on this page"; return out; }

      const setFile = async (index, path) => {
        const objRes = await cmd("Runtime.evaluate", {
          expression: `document.querySelectorAll('input[type=file]')[${index}]`, returnByValue: false });
        const objectId = objRes && objRes.result && objRes.result.objectId;
        if (!objectId) return false;
        await cmd("DOM.setFileInputFiles", { files: [path], objectId });
        await sleep(500);
        const chk = await cmd("Runtime.evaluate", {
          expression: `(document.querySelectorAll('input[type=file]')[${index}].files||[]).length>0`, returnByValue: true });
        return !!(chk && chk.result && chk.result.value);
      };

      // Résumé: a 'resume'-labelled input; else the sole input; else the first unlabelled ('other').
      let rIdx = list.find((x) => x.kind === "resume")
        || (list.length === 1 ? list[0] : list.find((x) => x.kind === "other"));
      if (rIdx) { if (onStatus) onStatus("Uploading résumé…"); out.resume = (await setFile(rIdx.i, resumePath)) ? "ok" : "failed"; }

      // Cover letter: ONLY when a genuine cover-letter input exists.
      const cIdx = list.find((x) => x.kind === "cover");
      if (!cIdx) out.cover = "skipped-noarea";
      else if (!coverPath) out.cover = "skipped-nofile";
      else { if (onStatus) onStatus("Uploading cover letter…"); out.cover = (await setFile(cIdx.i, coverPath)) ? "ok" : "failed"; }

      return out;
    } finally { await rrCdpRelease(tab.id); }
  }

  // ---- Docs tab: attach an ARBITRARY local file (resume, cover letter, transcript, schedule) ----
  // Two steps so the panel can offer a chooser when a page has several upload fields:
  //   cdpListFileInputs -> classify every <input type=file>; cdpSetFileInput -> drop a file in one.
  // Both attach/detach their own debugger session so they compose cleanly with each other.
  async function cdpListFileInputs(tab) {
    // Uses the SHARED deep probe (upload_match.js) rather than a shallow
    // document.querySelectorAll: on component-based ATS the real input lives inside a shadow root,
    // where the shallow query finds nothing and the Docs tab reports "no upload field on this
    // page" even though one is plainly visible.
    try {
      const [res] = await chrome.scripting.executeScript({
        target: { tabId: tab.id }, func: rrProbeUploadAreas });
      const areas = (res && res.result) || [];
      for (const a of areas) Object.assign(a, classifyUploadArea(a.text, a.pageText));
      return { ok: true, inputs: areas.map((a) => ({
        i: a.index,
        kind: a.kind || "other",
        label: (a.text || "").split(" | ")[0].slice(0, 60)
               + (a.mode === "autofill" ? " (autofills)" : a.mode === "inert" ? " (attachment)" : ""),
      })) };
    } catch (e) { return { ok: false, error: String(e).slice(0, 80) }; }
  }

  async function cdpSetFileInput(tab, index, absPath) {
    const target = { tabId: tab.id };
    const acq = await rrCdpAcquire(tab.id);
    if (!acq.ok) return { ok: false, error: acq.error };
    const cmd = (m, p) => chrome.debugger.sendCommand(target, m, p || {});
    try {
      const objRes = await cmd("Runtime.evaluate", {
        expression: rrDeepFindExpr(index), returnByValue: false });
      const objectId = objRes && objRes.result && objRes.result.objectId;
      if (!objectId) return { ok: false, error: "upload field is gone" };
      await cmd("DOM.setFileInputFiles", { files: [absPath], objectId });
      await sleep(400);
      const chk = await cmd("Runtime.evaluate", {
        expression: `((${rrDeepFindExpr(index)}||{}).files||[]).length>0`, returnByValue: true });
      return { ok: !!(chk && chk.result && chk.result.value) };
    } catch (e) { return { ok: false, error: String(e).slice(0, 80) }; }
    finally { await rrCdpRelease(tab.id); }
  }

  window.cdpUpload = cdpUpload;
  window.cdpListFileInputs = cdpListFileInputs;
  window.cdpSetFileInput = cdpSetFileInput;
})();
