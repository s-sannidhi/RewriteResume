// Email tab — manual compose + send through the backend (Gmail app password from the
// Keychain; nothing stored in the extension). Sending flips the chosen tracker entry to Sent.
(() => {
  const host = $("emailBody");
  const to = el("input", { type: "email", placeholder: "recruiter@company.com" });
  const subject = el("input", { placeholder: "Application — <Role> — Rishi Nigam" });
  const bodyTa = el("textarea", { style: "min-height:140px", placeholder: "Write the email…" });
  const jobSel = el("select", {});
  const attachResume = el("input", { type: "checkbox", checked: "checked", style: "width:auto" });
  const attachCL = el("input", { type: "checkbox", checked: "checked", style: "width:auto" });
  const sendBtn = el("button", { class: "primary", style: "margin-top:10px" }, "Send email");
  const status = el("div", { class: "status" });

  host.append(
    el("label", { class: "lbl" }, "To"), to,
    el("label", { class: "lbl" }, "Subject"), subject,
    el("label", { class: "lbl" }, "Body"), bodyTa,
    el("label", { class: "lbl" }, "Application (for attachments + Sent tracking)"), jobSel,
    el("label", { class: "lbl", style: "display:flex;gap:6px;align-items:center" },
      attachResume, "attach resume PDF"),
    el("label", { class: "lbl", style: "display:flex;gap:6px;align-items:center" },
      attachCL, "attach cover letter PDF"),
    sendBtn, status);

  function setSt(msg, cls = "") { status.textContent = msg; status.className = "status " + cls; }

  async function loadJobsList(preselectId) {
    try {
      const r = await fetch(`${API}/tracker`);
      const recs = await r.json();
      jobSel.innerHTML = "";
      jobSel.append(el("option", { value: "" }, "— no application (plain email) —"));
      recs.forEach((rec) => jobSel.append(el("option", { value: rec.id },
        `${rec.company || "—"} · ${(rec.role || "").slice(0, 34)} · ${(rec.created_at || "").slice(0, 10)}`)));
      // default: the active browser tab's job, or an explicit "email this" pick
      let want = preselectId;
      if (!want && currentJobId) {
        const jobs = await getJobs();
        want = (jobs[currentJobId] || {}).resume_id;
      }
      if (want) jobSel.value = want;
      if (want && jobSel.value === want) {
        const rec = recs.find((x) => x.id === want);
        if (rec && !subject.value.trim()) {
          subject.value = `Application — ${rec.role || "the role"} — Rishi Nigam`;
        }
      }
    } catch (e) { /* backend down — leave the empty option */ }
  }

  sendBtn.addEventListener("click", async () => {
    if (!to.value.trim() || !subject.value.trim() || !bodyTa.value.trim()) {
      return setSt("Fill To, Subject, and Body first.", "err");
    }
    sendBtn.disabled = true;
    setSt("Sending…");
    try {
      const r = await fetch(`${API}/email/send`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          to: to.value.trim(), subject: subject.value.trim(), body: bodyTa.value,
          tracker_id: jobSel.value || null,
          attach_resume: attachResume.checked, attach_cover_letter: attachCL.checked,
        }),
      });
      const res = await r.json();
      if (res.error) return setSt(res.hint || res.error, "err");
      const extras = (res.warnings || []).length ? "  (" + res.warnings.join("; ") + ")" : "";
      setSt(`Sent to ${res.to} with ${res.attached.length} attachment(s)` +
        (res.tracker_id ? " — tracker marked Sent." : ".") + extras, "ok");
    } catch (e) {
      setSt("Failed: " + e.message + " — is ./run.sh running?", "err");
    } finally { sendBtn.disabled = false; }
  });

  // Applications tab "email this" hook: remember the pick, switch tabs; the rr-tab-shown
  // handler below does the (single) list load.
  let pendingPreselect = null;
  window.emailPrefill = (trackerId) => {
    pendingPreselect = trackerId;
    showTab("email");
  };

  document.addEventListener("rr-tab-shown", (e) => {
    if (e.detail.tab === "email") {
      loadJobsList(pendingPreselect);
      pendingPreselect = null;
    }
  });
})();
