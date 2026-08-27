// Ask tab — application questions answered by the LOCAL model (Ollama, see backend/llm.py),
// grounded in the profile and optionally this browser tab's JD. Nothing leaves the machine. Q/A pairs are kept per posting in
// chrome.storage.local["rr_ask"][jobId] so switching tabs shows that job's last answer.
(() => {
  const ASK_STORE = "rr_ask";
  let question, useJD, askBtn, askStatus, out;
  let lastQuestion = "", lastAnswer = "";

  async function getSlot() {
    const all = (await chrome.storage.local.get(ASK_STORE))[ASK_STORE] || {};
    return { all, key: currentJobId || "_global" };
  }
  // rr_ask[jobId] is an APPEND-ONLY array of {question,answer,at}. Old builds stored a single
  // object here — treat that as a one-item history so nothing is lost.
  function asHistory(slot) {
    if (Array.isArray(slot)) return slot;
    if (slot && slot.answer) return [slot];
    return [];
  }
  async function saveSlot() {
    const { all, key } = await getSlot();
    const hist = asHistory(all[key]);
    hist.push({ question: lastQuestion, answer: lastAnswer, at: Date.now() });
    all[key] = hist;
    await chrome.storage.local.set({ [ASK_STORE]: all });
    // Attach to the application too, so it's retrievable later (e.g. for an interview).
    if (currentJobId) {
      const tid = ((await getJobs())[currentJobId] || {}).resume_id;
      if (tid) {
        try {
          await fetch(`${API}/tracker/${tid}/ask`, {
            method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ question: lastQuestion, answer: lastAnswer }),
          });
        } catch (e) { /* offline / no tracker row — the local copy above still persists */ }
      }
    }
  }
  async function restoreSlot() {
    const { all, key } = await getSlot();
    const hist = asHistory(all[key]);
    out.innerHTML = "";
    if (!hist.length) return;
    const latest = hist[hist.length - 1];
    lastQuestion = latest.question; lastAnswer = latest.answer;
    question.value = latest.question;
    renderAnswer(latest.answer, null);
    // saved history, not a fresh generation — say so, so profile edits since then
    // aren't mistaken for "hardcoded" answers
    const age = latest.at ? new Date(latest.at).toLocaleString() : "earlier";
    out.prepend(el("div", { class: "muted", style: "font-size:11.5px;margin:6px 0" },
      `Previous answer from ${age} — re-ask to use your current profile.`));
    renderHistory(hist.slice(0, -1));
  }

  // Earlier Q/As for this posting, newest first, each collapsible.
  function renderHistory(older) {
    if (!older.length) return;
    const wrap = el("div", { class: "card", style: "margin-top:12px" },
      el("div", { class: "k" }, `Earlier answers for this job (${older.length})`));
    older.slice().reverse().forEach((h) => {
      const when = h.at ? new Date(h.at).toLocaleString() : "";
      const copy = el("button", { class: "small" }, "Copy");
      copy.addEventListener("click", async () => {
        await copyText(h.answer);
        copy.textContent = "✓"; setTimeout(() => (copy.textContent = "Copy"), 1500);
      });
      const d = el("details", { style: "margin-top:8px" },
        el("summary", { style: "cursor:pointer;font-size:12.5px" },
          (h.question || "(question)").slice(0, 80)),
        el("div", { class: "muted", style: "font-size:11px;margin:3px 0" }, when),
        el("div", { style: "white-space:pre-wrap;margin:4px 0" }, h.answer),
        el("div", { class: "row" }, copy));
      wrap.append(d);
    });
    out.append(wrap);
  }

  async function copyText(v) {
    try { await navigator.clipboard.writeText(v); return true; } catch (e) { return false; }
  }

  // ---- write an answer straight back into the form field it came from ----------------------
  // The Ask tab is normally copy/paste. When a question arrives FROM autofill we know exactly
  // which field it belongs to, so the answer can go back without touching the clipboard.
  let target = null;   // {fieldId, label} handed over by the autofill review card

  const normLab = (s) => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

  async function fillIntoPage(value, btn) {
    const old = btn.textContent;
    btn.disabled = true; btn.textContent = "…";
    const done = (msg, cls) => { setAsk(msg, cls); btn.disabled = false; btn.textContent = old; };
    try {
      const tab = await activeTab();
      if (!tab || !isWebUrl(tab.url)) return done("Open the application page in that tab first.", "err");
      // Re-tag the form: ids are assigned in DOM order, so they only stay valid while the page
      // hasn't changed. Match on the id first, then fall back to the question text itself.
      const [disc] = await chrome.scripting.executeScript({
        target: { tabId: tab.id }, func: rrDiscoverFields });
      const fields = (disc && disc.result) || [];
      const want = normLab(target && target.label);
      let f = fields.find((x) => target && x.id === target.fieldId && normLab(x.label) === want);
      if (!f) f = fields.find((x) => want && normLab(x.label) === want);
      if (!f) f = fields.find((x) => want && normLab(x.label).startsWith(want.slice(0, 40)));
      if (!f) return done("Couldn't find that question on the page any more — copy/paste instead.", "err");

      const [inj] = await chrome.scripting.executeScript({
        target: { tabId: tab.id }, world: "MAIN", func: rrApplyActionsReact,
        args: [[{ field_id: f.id, action: "fill", value }]],
      });
      const r = ((inj && inj.result) || [])[0];
      if (r && r.ok) done("✓ Answer written into the page. Read it over before submitting.", "ok");
      else done("Couldn't write into that field — copy/paste instead.", "err");
    } catch (e) { done("Failed: " + e.message, "err"); }
  }

  function renderTargetBanner() {
    const host = $("askBody");
    const old = document.getElementById("askTarget");
    if (old) old.remove();
    if (!target) return;
    const clear = el("button", { class: "act" }, "Clear");
    clear.addEventListener("click", () => { target = null; renderTargetBanner(); });
    host.prepend(el("div", { id: "askTarget", class: "card",
      style: "border-color:var(--accent);margin-bottom:8px" },
      el("div", { class: "k" }, "Answering a field on the page"),
      el("div", { class: "muted", style: "font-size:12px;margin:3px 0" }, target.label),
      clear));
  }

  async function post(payload) {
    const r = await fetch(`${API}/ask`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!r.ok) throw new Error("HTTP " + r.status);
    return r.json();
  }

  async function jdContext() {
    if (!useJD.checked || !currentJobId) return null;
    const jobs = await getJobs();
    return (jobs[currentJobId] || {}).jd_analysis || null;
  }

  function setAsk(msg, cls = "") { askStatus.textContent = msg; askStatus.className = "status " + cls; }

  async function run(payload, btn) {
    btn.disabled = true;
    setAsk("Thinking… (local model, ~10–20s)");
    try {
      const res = await post(payload);
      if (res.error) {
        setAsk(res.hint || res.error, "err");
        if (res.recall) renderAnswer(null, res.recall);
        return;
      }
      lastQuestion = payload.refine ? lastQuestion : payload.question;
      lastAnswer = res.answer;
      await saveSlot();
      setAsk("", "");
      renderAnswer(res.answer, res.recall);
    } catch (e) {
      setAsk("Failed: " + e.message + " — is ./run.sh running?", "err");
    } finally { btn.disabled = false; }
  }

  async function askNow() {
    const q = question.value.trim();
    if (!q) return setAsk("Type the application question first.", "err");
    run({ question: q, jd_analysis: await jdContext() }, askBtn);
  }

  async function refine(instruction, btn) {
    if (!lastAnswer) return setAsk("Ask something first, then refine.", "err");
    run({
      question: lastQuestion, jd_analysis: await jdContext(),
      history: [{ role: "user", content: lastQuestion }, { role: "assistant", content: lastAnswer }],
      refine: instruction,
    }, btn);
  }

  function renderAnswer(answer, recall) {
    out.innerHTML = "";
    if (recall && recall.answer) {
      const use = el("button", { class: "small" }, "Use this");
      use.addEventListener("click", () => { lastAnswer = recall.answer; renderAnswer(recall.answer, null); });
      out.append(el("div", { class: "card" },
        el("div", { class: "k" }, "You answered something similar before"),
        el("div", { class: "muted", style: "font-size:12px" }, `"${recall.question}"`),
        el("div", { style: "margin:6px 0;white-space:pre-wrap" }, recall.answer), use));
    }
    if (!answer) return;

    if (target) {
      const put = el("button", { class: "primary big" }, "⬇ Put this answer in the page");
      put.addEventListener("click", () => fillIntoPage(answer, put));
      out.append(el("div", { style: "margin-bottom:8px" }, put));
    }
    const copy = el("button", { class: "act" }, "Copy answer");
    copy.addEventListener("click", async () => {
      await copyText(answer);
      const armed = await armPaste(answer);
      copy.textContent = armed ? "✓ copied — click a field to paste" : "✓ copied";
      setTimeout(() => (copy.textContent = "Copy answer"), 2200);
    });
    const save = el("button", { class: "act" }, "Save to memory");
    save.addEventListener("click", async () => {
      save.disabled = true;
      try {
        await fetch(`${API}/qa/save`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ question: lastQuestion, answer }),
        });
        save.textContent = "✓ saved";
      } catch (e) { save.disabled = false; }
    });

    const shorter = el("button", { class: "act" }, "Shorter");
    const specific = el("button", { class: "act" }, "More specific");
    const custom = el("input", { placeholder: "…or your own instruction (e.g. mention CustomMaps)" });
    const go = el("button", { class: "act" }, "Refine");
    shorter.addEventListener("click", () => refine("Make it shorter — about half the length, keep the strongest points.", shorter));
    specific.addEventListener("click", () => refine("Make it more specific — concrete projects, tools, and outcomes instead of general statements.", specific));
    go.addEventListener("click", () => {
      if (custom.value.trim()) refine(custom.value.trim(), go);
    });

    out.append(el("div", { class: "card" },
      el("div", { style: "white-space:pre-wrap" }, answer),
      el("div", { class: "row", style: "margin-top:10px" }, copy, save),
      el("label", { class: "lbl" }, "Refine"),
      el("div", { class: "row" }, shorter, specific),
      el("div", { class: "row", style: "margin-top:6px" }, custom, go)));
  }

  // ---- build the tab UI once ----
  const host = $("askBody");
  question = el("textarea", { placeholder: 'Paste the application question, e.g. "Why do you want to work at Acme?"' });
  useJD = el("input", { type: "checkbox", checked: "checked", style: "width:auto" });
  askBtn = el("button", { class: "primary big", style: "margin-top:8px" }, "Ask AI");
  askStatus = el("div", { class: "status" });
  out = el("div", {});
  askBtn.addEventListener("click", askNow);
  host.append(question,
    el("label", { class: "lbl", style: "display:flex;gap:6px;align-items:center;margin-top:8px" },
      useJD, "use the current tab's job description as context"),
    askBtn, askStatus, out);

  // ---- pull the page's unanswered questions into this tab ---------------------------------
  // Same discovery + classification the autofill uses, but nothing is written: it just lists the
  // questions your profile can't answer, each one tappable to draft an answer.
  async function pullQuestions(btn) {
    const box = $("pulledQuestions");
    box.innerHTML = "";
    btn.disabled = true;
    const oldTxt = btn.textContent; btn.textContent = "Reading the page…";
    const finish = () => { btn.disabled = false; btn.textContent = oldTxt; };
    try {
      const tab = await activeTab();
      if (!tab || !isWebUrl(tab.url)) { setAsk("Open the application page in that tab first.", "err"); return finish(); }
      const [disc] = await chrome.scripting.executeScript({
        target: { tabId: tab.id }, func: rrDiscoverFields });
      const fields = (disc && disc.result) || [];
      if (!fields.length) { setAsk("No form fields found on that page.", "err"); return finish(); }

      const jd = currentJobId ? ((await getJobs())[currentJobId] || {}).jd_analysis || null : null;
      const r = await fetch(`${API}/field/answer/batch`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fields, jd_analysis: jd, no_ai: true }),
      });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const { actions } = await r.json();
      const fById = {}; fields.forEach((f) => (fById[f.id] = f));

      // Questions worth drafting: left blank, and not something you must answer personally.
      const mine = ["consent_acknowledgement", "related_to_employee", "export_control"];
      const open = (actions || []).filter((a) =>
        a.action === "skip" && a.needs_review && a.field_kind !== "do_not_fill" && !mine.includes(a.field_kind));

      if (!open.length) {
        setAsk("Nothing left to draft — your profile already covers every question on that page.", "ok");
        return finish();
      }
      setAsk("", "");
      const card = el("div", { class: "card" },
        el("div", { class: "k" }, `${open.length} question${open.length === 1 ? "" : "s"} your profile can't answer`),
        el("div", { class: "muted", style: "font-size:11.5px;margin:2px 0 4px" },
          "Tap one to draft an answer, then put it straight back into the field."));
      for (const a of open) {
        const label = (fById[a.field_id] || {}).label || a.field_kind;
        const b = el("button", { class: "big", style: "text-align:left;margin-top:6px" }, label.slice(0, 90));
        b.addEventListener("click", () => {
          target = { fieldId: a.field_id, label };
          question.value = label;
          box.innerHTML = "";
          renderTargetBanner();
          askNow();
        });
        card.append(b);
      }
      box.append(card);
    } catch (e) {
      setAsk("Failed: " + e.message + " — is ./run.sh running?", "err");
    }
    finish();
  }

  const pullBtn = $("pullQuestionsBtn");
  if (pullBtn) pullBtn.addEventListener("click", () => pullQuestions(pullBtn));

  // Autofill hands over a question it couldn't answer: prefill it, remember the field, and ask
  // immediately so the answer is waiting by the time the tab is on screen.
  document.addEventListener("rr-ask-question", async (e) => {
    target = { fieldId: e.detail.fieldId, label: e.detail.question };
    question.value = e.detail.question;
    out.innerHTML = "";
    renderTargetBanner();
    askNow();
  });

  // ---------------- the skills list, for typing into a form by hand ----------------
  // Plenty of application forms take skills one at a time in a widget nothing can drive, so this
  // is the read-off-and-type reference: the SAME verified list the Workday filler uses, in the
  // same order, so what you type by hand matches what's on the résumé. Grouped because that's how
  // you scan it, and dense because it lives next to the buttons you're already using.
  // Each chip copies itself on click — free, given it has to be on screen anyway.
  // Group keys are storage names; these are what they should read as on screen.
  const SKILL_GROUP_LABELS = {
    programming_languages: "Languages", frameworks: "Frameworks", ml_ai: "ML / AI",
    ai_tools: "AI tools", databases: "Databases", cloud: "Cloud", tools: "Tools",
    hardware_embedded: "Hardware",
  };
  let skillsSig = "";        // re-fetched on every visit, redrawn only when it actually changed

  async function renderSkills() {
    const host = $("askSkills");
    if (!host) return;
    let data = null;
    try {
      const r = await fetch(`${API}/skills/verified`);
      if (r.ok) data = await r.json();
    } catch (e) { /* backend down — leave the placeholder text */ }
    if (!data || data.error || !(data.skills || []).length) {
      host.textContent = data && data.error ? data.error : "Start ./run.sh to load your skills.";
      host.className = "hint";
      return;
    }
    const sig = (data.skills || []).join("\u0000");
    if (sig === skillsSig) return;       // unchanged — leave the DOM (and any "✓ copied") alone
    skillsSig = sig;
    host.innerHTML = "";
    host.className = "";

    const count = el("div", { class: "hint", style: "margin-bottom:5px" },
      `${data.count || data.skills.length} skills · click any to copy`);
    const all = el("button", { class: "small", style: "margin-left:6px" }, "copy all");
    all.addEventListener("click", async () => {
      await copyText(data.skills.join(", "));
      all.textContent = "✓ copied";
      setTimeout(() => (all.textContent = "copy all"), 1600);
    });
    count.append(all);
    host.append(count);

    const groups = data.groups || {};
    const names = Object.keys(groups).filter((g) => (groups[g] || []).length);
    // No groups from the backend (older payload) — one flat run of chips still reads fine.
    const blocks = names.length ? names.map((g) => [g, groups[g]]) : [["", data.skills]];
    for (const [name, items] of blocks) {
      const row = el("div", { class: "skill-group" });
      if (name) row.append(el("span", { class: "skill-group-name" },
        SKILL_GROUP_LABELS[name] || name.replace(/_/g, " ")));
      for (const skill of items) {
        const chip = el("button", { class: "skill-chip", title: "Copy" }, skill);
        chip.addEventListener("click", async () => {
          await copyText(skill);
          chip.classList.add("copied");
          setTimeout(() => chip.classList.remove("copied"), 1200);
        });
        row.append(chip);
      }
      host.append(row);
    }
  }

  document.addEventListener("rr-tab-shown", (e) => { if (e.detail.tab === "ask") renderSkills(); });
  document.addEventListener("rr-tab-shown", (e) => { if (e.detail.tab === "ask") renderTargetBanner(); });
  document.addEventListener("rr-tab-shown", (e) => { if (e.detail.tab === "ask") restoreSlot(); });
  chrome.tabs.onActivated.addListener(() => setTimeout(restoreSlot, 150));
})();
