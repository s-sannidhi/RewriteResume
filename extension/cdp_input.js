// File-attach into a page's <input type=file>.
//
// Chrome: a real disk path via chrome.debugger's DOM.setFileInputFiles.
// Firefox (no debugger API): fetch the file from the local backend and assign a File through
// DataTransfer — see files_place.js.
//
//   • cdpListFileInputs / cdpSetFileInput  → the Docs tab's one-tap attach
//   • cdpUpload                            → auto-upload this job's résumé + cover letter
// Field *filling* no longer lives here — that moved to the React-props engine in autofill.js
// (rrApplyActionsReact), so there's no more "debugging" banner just to type into a form.
(() => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function cdpSetFileInputCdp(tab, index, absPath) {
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

  async function cdpSetFileInput(tab, index, absPath) {
    if (typeof rrHasDebugger === "function" ? rrHasDebugger() : !!(chrome.debugger && chrome.debugger.sendCommand)) {
      const r = await cdpSetFileInputCdp(tab, index, absPath);
      if (r.ok) return r;
    }
    if (typeof rrPlaceFileBlob === "function") return rrPlaceFileBlob(tab.id, index, absPath);
    return { ok: false, error: "no way to place a file in this browser" };
  }

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

    const listed = await cdpListFileInputs(tab);
    if (!listed.ok) return { error: listed.error || "couldn't read the page" };
    const list = listed.inputs || [];
    const out = { resume: "notfound", cover: coverPath ? "notfound" : "skipped-nofile" };
    if (!list.length) { out.error = "no file-upload field on this page"; return out; }

    const setFile = async (index, path) => (await cdpSetFileInput(tab, index, path)).ok;

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
  }

  // ---- Docs tab: attach an ARBITRARY local file (resume, cover letter, transcript, schedule) ----
  // Two steps so the panel can offer a chooser when a page has several upload fields:
  //   cdpListFileInputs -> classify every <input type=file>; cdpSetFileInput -> drop a file in one.
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

  window.cdpUpload = cdpUpload;
  window.cdpListFileInputs = cdpListFileInputs;
  window.cdpSetFileInput = cdpSetFileInput;
})();
