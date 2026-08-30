// Current-page-only autofill (manual, review-first). Best on standard HTML ATS forms —
// Greenhouse / Lever / Ashby — where most fields are direct profile lookups. It NEVER submits,
// only fills THIS tab's form, and only types what it actually knows:
//   • your profile (name/email/phone/links/address/work-auth/EEO) via field_map, and
//   • your own confirmed saved answers (qa_memory).
// Freshly AI-written prose is never typed into the page — it's listed for the Ask tab instead.
// Discovery tags fields via autofill.js (rrDiscoverFields, isolated world); filling uses the
// React-props engine (rrApplyActionsReact, injected into the MAIN world) — no chrome.debugger,
// no "debugging" banner. Never submits.
(() => {
  // Which card is driving this run — "jobs" or "ask".
  //
  // Jobs is the review surface: status line, what was left blank, what to double-check. Ask is
  // deliberately QUIET — you're mid-application there and a card that grows under the buttons
  // shoves the thing you were reading off screen. The buttons report on themselves instead
  // (see flash), so the panel never changes height. The full breakdown is still one tab away.
  let ui = { status: "autofillStatus", review: "autofillReview" };
  const UIS = {
    jobs: { status: "autofillStatus", review: "autofillReview" },
    ask: { quiet: true },
  };
  function setSt(msg, cls = "") {
    if (ui.quiet) return;
    const s = $(ui.status); if (!s) return;
    s.textContent = msg; s.className = "status " + cls;
  }
  // Never null: a detached div absorbs the writes when the card doesn't want them.
  const reviewBox = () => (ui.quiet ? null : $(ui.review)) || document.createElement("div");

  // Transient feedback on the button itself — costs no layout, so nothing below it moves.
  function flash(btn, msg, ms = 2200) {
    if (!btn) return;
    if (!btn.dataset.label) btn.dataset.label = btn.textContent;
    btn.textContent = msg;
    clearTimeout(+btn.dataset.flashT || 0);
    btn.dataset.flashT = String(setTimeout(() => { btn.textContent = btn.dataset.label; }, ms));
  }
  const labelOf = (f) => (f.label || f.name || f.id || "field").toString().slice(0, 60);

  async function jdForActive() {
    if (typeof currentJobId !== "undefined" && currentJobId) {
      try { return ((await getJobs())[currentJobId] || {}).jd_analysis || null; } catch (e) {}
    }
    return null;
  }

  // ---- document attach (smart target picking lives in upload_smart.js) ----
  async function attach(btn, quiet, from) {
    if (from) ui = UIS[from] || ui;
    const tab = await activeTab();
    if (!tab || !isWebUrl(tab.url)) {
      setSt("Open the application page in this tab first.", "err");
      flash(btn, "Open the application page first"); return null;
    }
    if (btn) btn.disabled = true;
    flash(btn, "Attaching…", 60000);
    try {
      const docs = await rrJobDocsForTab(tab);
      if (docs.error) {
        if (!quiet) setSt(docs.error + " — make the documents first (Jobs tab), then attach.", "err");
        flash(btn, "No documents yet");
        return { error: docs.error };
      }
      const res = await smartUpload(tab, { resume: docs.resume, cover: docs.cover },
                                    (m) => setSt(m));
      renderAttachResult(res);
      const n = (res.placed || []).length, bad = (res.failed || []).length;
      flash(btn, n ? `✓ ${n} attached${bad ? ` · ${bad} failed` : ""}` : "Nothing attached");
      return res;
    } catch (e) {
      setSt("Attach failed: " + e.message, "err");
      flash(btn, "Attach failed");
      return { error: e.message };
    } finally { if (btn) btn.disabled = false; }
  }

  function renderAttachResult(res) {
    const box = reviewBox();
    if (res.error && !(res.placed || []).length) {
      box.prepend(el("div", { class: "card" },
        el("div", { class: "k" }, "📎 Documents not attached"),
        el("div", { class: "muted", style: "font-size:12px;margin-top:3px" }, res.error),
        el("div", { class: "muted", style: "font-size:11.5px;margin-top:4px" },
          "The Docs tab can place a file by hand — tap the document, then pick the field.")));
      return;
    }
    const lines = [];
    (res.placed || []).forEach((j) => lines.push("✓ " + j.kind + " attached"));
    (res.failed || []).forEach((j) => lines.push("✗ " + j.kind + " — " + j.why));
    (res.notes || []).forEach((n) => lines.push("• " + n));
    if (!lines.length) return;
    box.prepend(el("div", { class: "card" },
      el("div", { class: "k" }, "📎 Documents"),
      ...lines.map((t) => el("div", { style: "font-size:12px;margin-top:3px" }, t))));
  }

  async function run(btn, alsoAttach, from) {
    if (from) ui = UIS[from] || ui;
    const tab = await activeTab();
    if (!tab || !isWebUrl(tab.url)) {
      setSt("Open the application page in this tab first.", "err");
      flash(btn, "Open the application page first"); return;
    }
    btn.disabled = true; reviewBox().innerHTML = ""; setSt("Reading the form on this page…");
    flash(btn, "Filling…", 60000);
    try {
      const [disc] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: rrDiscoverFields });
      if (disc && disc.error) throw new Error(disc.error.message || String(disc.error));
      const fields = (disc && disc.result) || [];
      if (!fields.length) {
        setSt("No fillable fields found on this page.", "err");
        flash(btn, "No fields found here"); return;
      }

      setSt(`Matching ${fields.length} fields to your profile…`);
      const r = await fetch(`${API}/field/answer/batch`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        // AI on for unknown short fields + essay drafts. Profile/QA still win first.
        // Essays come back needs_review; simple yes/no/dropdown answers auto-apply.
        body: JSON.stringify({ fields, jd_analysis: await jdForActive(), no_ai: false }),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const { actions } = await r.json();

      const fById = {}; fields.forEach((f) => (fById[f.id] = f));
      // Apply everything we can fill/select — including short LLM answers and essay drafts.
      // Work-experience widgets are handled by the dedicated Workday filler, not this path.
      const toApply = actions.filter((a) =>
        ["fill", "select", "type_then_pick"].includes(a.action));

      let results = [];
      if (toApply.length) {
        setSt(`Filling ${toApply.length} fields…`);
        const [inj] = await chrome.scripting.executeScript({
          target: { tabId: tab.id }, world: "MAIN", func: rrApplyActionsReact, args: [toApply],
        });
        if (inj && inj.error) throw new Error(inj.error.message || String(inj.error));
        results = (inj && inj.result) || [];
      }

      // Highlight what was left blank for you (unknown questions, free-text, "know anyone here")
      // plus anything that failed to fill. Skip file inputs (attach from Docs) and the deliberately
      // skipped secondary phone/address.
      const okById = {}; results.forEach((x) => (okById[x.id] = x));
      const hl = [];
      for (const a of actions) {
        if (a.field_kind === "do_not_fill" || a.action === "upload") continue;
        const blank = a.action === "skip" && a.needs_review;
        const failed = okById[a.field_id] && !okById[a.field_id].ok;
        if (blank || failed) hl.push(a.field_id);
      }
      if (hl.length) {
        try {
          await chrome.scripting.executeScript({
            target: { tabId: tab.id }, func: rrHighlightFields, args: [hl] });
        } catch (e) {}
      }
      const summary = renderReview(fById, toApply, results, actions, hl);
      let done = summary;
      if (alsoAttach) {
        const st = $(ui.status);
        const before = (st && st.textContent) || "";
        const res = await attach(null, true);
        if (res && !res.error) { setSt(before + " Documents attached.", ""); done += " + docs"; }
      }
      flash(btn, done);
    } catch (e) {
      setSt("Autofill failed: " + e.message + " — is ./run.sh running?", "err");
      flash(btn, "Failed — check ./run.sh");
    } finally { btn.disabled = false; }
  }

  function renderReview(fById, applied, results, allActions, hlIds) {
    const box = reviewBox(); box.innerHTML = "";
    const okById = {}; (results || []).forEach((r) => (okById[r.id] = r));
    let filled = 0; const check = [];
    for (const a of applied) {
      const r = okById[a.field_id] || {};
      const verify = a.action === "type_then_pick" || a.needs_review || (a.confidence || 0) < 0.6;
      if (r.ok && !verify) { filled++; continue; }
      check.push({
        label: labelOf(fById[a.field_id] || {}),
        val: (a.value || a.option || "").toString().slice(0, 60),
        why: !r.ok ? (r.note || "couldn't fill this field")
          : a.action === "type_then_pick" ? "typed into a search box — confirm it picked the right option"
          : "low confidence — double-check the value",
      });
    }
    const hlSet = new Set(hlIds || []);
    const blanks = (allActions || []).filter((a) => hlSet.has(a.field_id));
    const secondary = (allActions || []).filter((a) => a.field_kind === "do_not_fill").length;

    setSt(`Filled ${filled} field${filled === 1 ? "" : "s"} from your profile. Nothing submitted.`
      + (blanks.length ? ` ${blanks.length} left blank & highlighted for you.` : "")
      + (check.length ? ` ${check.length} to verify.` : "")
      + (secondary ? ` ${secondary} secondary field${secondary === 1 ? "" : "s"} skipped.` : ""),
      (blanks.length || check.length) ? "" : "ok");
    // The same numbers, short enough to live on a button — that's all the quiet card shows.
    const summary = `✓ ${filled} filled`
      + (blanks.length ? ` · ${blanks.length} blank` : "")
      + (check.length ? ` · ${check.length} to check` : "");

    if (blanks.length) {
      const card = el("div", { class: "card" },
        el("div", { class: "k" }, "✍ Left blank — fill these yourself"),
        el("div", { class: "muted", style: "font-size:11.5px;margin:2px 0 4px" },
          "Highlighted in amber on the page. Tap “Ask AI” to draft one in the Ask tab and drop "
          + "the answer straight back into the field."));
      for (const a of blanks) {
        const f = fById[a.field_id] || {};
        const label = labelOf(f);
        const row = el("div", { style: "display:flex;gap:6px;align-items:center;margin-top:6px" },
          el("div", { style: "flex:1;font-size:12px;min-width:0" }, label));
        // Attestations and referral questions are the user's to answer — no AI hand-off for those.
        if (!["consent_acknowledgement", "related_to_employee", "export_control"].includes(a.field_kind)) {
          const ask = el("button", { class: "act" }, "Ask AI");
          ask.addEventListener("click", () => {
            window.rrAskTarget = { fieldId: a.field_id, label, tabId: null };
            document.dispatchEvent(new CustomEvent("rr-ask-question",
              { detail: { question: label, fieldId: a.field_id } }));
            showTab("ask");
          });
          row.append(ask);
        } else {
          row.append(el("span", { class: "muted", style: "font-size:11px" }, "yours to answer"));
        }
        card.append(row);
      }
      box.append(card);
    }

    if (check.length) {
      box.append(el("div", { class: "card" },
        el("div", { class: "k" }, "⚠ Verify before submitting"),
        ...check.map((c) => el("div", { style: "margin-top:6px;font-size:12px" },
          el("span", { class: "k" }, c.label), c.val ? el("span", { class: "muted" }, " → " + c.val) : "",
          el("div", { style: "color:#c0392b;font-size:11.5px" }, c.why)))));
    }
    return summary;
  }

  // Both tabs get the identical set of actions. The third argument is just which card's status
  // line and review card to draw into.
  const CARDS = [
    { from: "jobs", fill: "autofillBtn", all: "autofillAllBtn", attach: "attachDocsBtn" },
    { from: "ask", fill: "askAutofillBtn", all: "askAutofillAllBtn", attach: "askAttachDocsBtn" },
  ];

  function wire() {
    for (const c of CARDS) {
      const fill = $(c.fill);
      if (!fill || fill.dataset.wired) continue;
      fill.dataset.wired = "1";
      fill.addEventListener("click", () => run(fill, false, c.from));
      const all = $(c.all);
      if (all) all.addEventListener("click", () => run(all, true, c.from));
      const att = $(c.attach);
      if (att) att.addEventListener("click", () => attach(att, false, c.from));
    }
  }
  document.addEventListener("DOMContentLoaded", wire);
  wire();
})();
