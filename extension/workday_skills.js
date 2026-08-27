// Workday filler (My Info tab). Workday-only. Fills the Skills combobox AND the Work-Experience
// section from the current tab's tailored résumé.
//
// SKILLS uses the React-props approach from berellevy/job_app_filler (the open-source Simplify
// clone): reach into the widget's own React handlers instead of faking keystrokes. The trick that
// makes it work is writing through the NATIVE value setter so React's value tracker goes stale and
// the change actually propagates — see wdPageHelpers for the full explanation.
//
// WORK EXPERIENCE still goes through the Chrome DevTools Protocol (chrome.debugger →
// Input.insertText / dispatchKeyEvent / dispatchMouseEvent) for TRUSTED events, and the older CDP
// skills path is selectable in the dropdown for tenants whose React internals differ.
(() => {
  // Every Workday tenant domain, not just myworkdayjobs.com — Devon's careers site, for one,
  // lives on wd5.myworkdaysite.com. The (^|\.) prefix already covers the wdNN. subdomains.
  const WD_HOSTS = /(^|\.)(myworkdayjobs|myworkdaysite|myworkday|workday)\.com$/i;
  const MODE_KEY = "rr_wd_skills_engine";   // "auto" | "react" | "cdp"
  const SKILLS_SEL = '[data-automation-id="formField-skills"] input';
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  let stopRequested = false;    // panel-side stop flag, checked between skills

  // Fill mechanism, chosen in the dropdown rather than hardcoded — which one works depends on the
  // tenant's React build, and only a live page can settle it:
  //   "auto"  = probe React on one skill, fall back to trusted typing if it finds nothing
  //   "react" = React-props injection only (MAIN world, no debugger banner)
  //   "cdp"   = chrome.debugger TRUSTED input (isTrusted:true) — needed by tenants whose skill
  //             search runs off real keystrokes, which is the case on this user's Vanguard tenant.
  const DEFAULT_ENGINE = "auto";

  // JS run in the page to find + scroll the skills box and return its viewport-center
  // coordinates, re-read fresh each cycle (chips shift the layout).
  const RECT_EXPR = `(() => {
    const el = document.querySelector('${SKILLS_SEL}');
    if (!el) return { ok:false };
    el.scrollIntoView({ block:'center' });
    const r = el.getBoundingClientRect();
    return { ok:true, x: r.left + r.width/2, y: r.top + r.height/2 };
  })()`;

  // Return the visible option ROWS with their text and click coordinates, scoped to the skills
  // widget the same way the React path scopes: prefer the listbox the input owns, otherwise ignore
  // rows belonging to a different formField (the education pickers use identical markup). Workday's
  // "No Items." empty state is dropped so it can never be chosen.
  const OPTIONS_LIST_EXPR = `(() => {
    const OPT='[data-automation-id="promptOption"],[data-automation-id="menuItem"],[role="option"],li[role="option"]';
    const EMPTY=/^(no items|no results|no matches|nothing found|loading)\\b/i;
    const input=document.querySelector('${SKILLS_SEL}');
    if(!input) return [];
    const container=input.closest('[data-automation-id="formField-skills"]')||input.parentElement||document;
    const ownId=input.getAttribute('aria-controls')||input.getAttribute('aria-owns');
    const own=ownId?document.getElementById(ownId):null;
    const root=own||document;
    const out=[];
    for(const o of root.querySelectorAll(OPT)){
      const r=o.getBoundingClientRect();
      if(!(r.width>1&&r.height>1)) continue;
      const txt=(o.textContent||'').replace(/\\s+/g,' ').trim();
      if(!txt||EMPTY.test(txt)) continue;
      if(!own){ const ff=o.closest('[data-automation-id^="formField-"]'); if(ff&&ff!==container) continue; }
      out.push({text:txt, x:r.left+r.width/2, y:r.top+r.height/2});
    }
    return out;
  })()`;

  // How many skill chips the skills widget itself holds (NOT page-wide — other widgets have chips).
  const CHIPS_EXPR = `(() => {
    const input=document.querySelector('${SKILLS_SEL}');
    if(!input) return 0;
    const c=input.closest('[data-automation-id="formField-skills"]')||input.parentElement||document;
    return c.querySelectorAll('ul[data-automation-id="selectedItemList"] li,[data-automation-id="selectedItem"],[data-automation-id="DELETE_charm"]').length;
  })()`;


  // ===================== skill ↔ option matching =====================================
  // Workday's picker lists near-misses next to the real thing: searching "React" offers
  // "React Native", "Git" offers "GitHub", "Python" offers "PyTorch" and "Python Django". Pressing
  // Enter takes whichever row is highlighted — almost always the first — so the box fills with
  // skills the user never claimed. Everything below exists to pick the RIGHT row or none at all.
  //
  // The rule is equality, never "contains": an option is accepted only when its core term equals
  // the term we searched for. The core is the option text minus the qualifier Workday appends —
  // "Python (Programming Language)" and "Python — Language" both reduce to "python", while
  // "React Native" stays "react native" and is therefore correctly rejected for "React".

  // Trailing qualifiers Workday adds. Parenthesised/bracketed forms are stripped generically;
  // these are the bare-word versions that appear without punctuation.
  const QUALIFIER_RX = new RegExp(
    "\\s+(programming\\s+language|coding\\s+language|scripting\\s+language|language|" +
    "software|application|framework|library|platform|database|db|tool|technology|" +
    "version\\s+control(\\s+system)?|skill|general)$", "i");

  // Terms whose written forms differ but mean the same skill.
  const ALIASES = [
    ["c#", "c sharp", "csharp"],
    ["c++", "c plus plus", "cpp"],
    ["javascript", "java script", "js", "ecmascript"],
    ["typescript", "type script", "ts"],
    ["node.js", "nodejs", "node"],
    ["react", "react.js", "reactjs"],
    ["postgresql", "postgres"],
    ["github", "git hub"],
    ["numpy", "num py"],
    ["fastapi", "fast api"],
    ["sqlite", "sql lite"],
    ["rag", "retrieval augmented generation"],
    ["llm", "llms", "large language model", "large language models"],
  ];

  const squash = (s) => s.replace(/[\s.]+/g, "");     // "node.js"/"node js" -> "nodejs"

  // Reduce an option's label (or a skill) to its comparable core term.
  function coreTerm(text) {
    let t = (text || "").toLowerCase().trim();
    t = t.replace(/\s*\([^)]*\)\s*$/, "");            // "Python (Programming Language)"
    t = t.replace(/\s*\[[^\]]*\]\s*$/, "");           // "Python [Language]"
    t = t.replace(/\s*[-–—:]\s+.*$/, "");              // "Python - Programming Language"
    t = t.replace(QUALIFIER_RX, "");                   // "Python Programming Language"
    t = t.replace(/[^a-z0-9+#.\s]+/g, " ").replace(/\s+/g, " ").trim();
    return t;
  }

  // Every spelling that should count as the same term.
  function variants(term) {
    const c = coreTerm(term);
    const out = new Set([c, squash(c)]);
    for (const group of ALIASES) {
      if (group.some((g) => g === c || squash(g) === squash(c))) {
        group.forEach((g) => { out.add(g); out.add(squash(g)); });
      }
    }
    return out;
  }

  function sameSkill(query, optionText) {
    const a = variants(query), b = variants(optionText);
    for (const v of a) if (v && b.has(v)) return true;
    return false;
  }

  // Walk the WHOLE list and return the row that genuinely is this skill, or null. Never falls back
  // to "the first one" — a wrong skill on an application is worse than a missing one.
  function pickSkillOption(query, options) {
    const scored = [];
    options.forEach((o, i) => {
      const text = (o.text !== undefined ? o.text : o) || "";
      if (!text.trim()) return;
      const core = coreTerm(text);
      const q = coreTerm(query);
      let tier = -1;
      if (core === q) tier = 0;                       // "Python" / "Python (Programming Language)"
      else if (sameSkill(query, text)) tier = 1;      // "C#" vs "C Sharp"
      if (tier < 0) return;
      // Prefer the plainest label at the same tier: "Python" over "Python (Programming Language)".
      scored.push({ i, text, tier, len: text.length });
    });
    if (!scored.length) return null;
    scored.sort((a, b) => a.tier - b.tier || a.len - b.len || a.i - b.i);
    return scored[0];
  }

  // Search terms to try, in order. Workday's list is a controlled taxonomy, so a résumé label like
  // "Local LLMs (Ollama)" has to be probed by its parts — including what's INSIDE the parentheses,
  // which is often the actual searchable product name.
  function skillQueries(skill) {
    const out = [skill];
    const add = (t) => { const v = (t || "").trim(); if (v && !out.includes(v)) out.push(v); };
    const paren = skill.match(/\(([^)]*)\)/);
    const noParen = skill.replace(/\s*\([^)]*\)/g, "").trim();
    add(noParen);
    if (paren) add(paren[1]);                          // "Local LLMs (Ollama)" -> "Ollama"
    for (const part of noParen.split(/[\/,]|\s+&\s+|\s+and\s+/)) add(part);
    return out.filter(Boolean);
  }

  // ---- CDP-driven SKILLS fill (trusted input) ----
  async function fillViaDebugger(tab, skills, onProgress, freeText) {
    const target = { tabId: tab.id };
    const acq = await rrCdpAcquire(tab.id);
    if (!acq.ok) return { error: "Couldn't attach. " + acq.error };
    const cmd = (method, params) => chrome.debugger.sendCommand(target, method, params || {});

    async function rect() {
      const res = await cmd("Runtime.evaluate", { expression: RECT_EXPR, returnByValue: true });
      return res && res.result && res.result.value;
    }
    async function click(x, y) {
      await cmd("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", buttons: 1, clickCount: 1 });
      await cmd("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", buttons: 0, clickCount: 1 });
    }
    async function keyPress({ key, code, vk, modifiers = 0, text }) {
      const down = { type: text ? "keyDown" : "rawKeyDown", key, code,
        windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, modifiers };
      if (text) down.text = text;
      await cmd("Input.dispatchKeyEvent", down);
      await cmd("Input.dispatchKeyEvent", { type: "keyUp", key, code,
        windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, modifiers });
    }
    async function boxValue() {
      try {
        const res = await cmd("Runtime.evaluate", {
          expression: `((document.querySelector('${SKILLS_SEL}')||{}).value||"")`, returnByValue: true });
        return (res && res.result && res.result.value) || "";
      } catch (e) { return ""; }
    }
    // Empty the combobox input (assumes it's focused). We delete ONE char at a time and RE-READ the
    // value after each Backspace, with a small settle between — Workday's React input drops
    // back-to-back key events fired with no delay, which left leftover text so the next skill got
    // typed onto the end ("PythonJava" in one box). Re-reading also means we stop the instant it's
    // empty, so we never Backspace into (and delete) an already-committed skill chip. Returns true
    // if the box ended up empty.
    async function clearBox() {
      if (!(await boxValue())) return true;
      await keyPress({ key: "End", code: "End", vk: 35 });   // cursor to end (click may land mid-text)
      for (let i = 0; i < 100; i++) {
        if (!(await boxValue())) return true;
        await keyPress({ key: "Backspace", code: "Backspace", vk: 8 });
        await sleep(35);                                     // let React apply the deletion
      }
      return !(await boxValue());
    }


    // Read the dropdown rows (text + coordinates), scoped to the skills widget.
    async function optionRows() {
      try {
        const res = await cmd("Runtime.evaluate", { expression: OPTIONS_LIST_EXPR, returnByValue: true });
        return (res && res.result && res.result.value) || [];
      } catch (e) { return []; }
    }
    async function chipCount() {
      try {
        const res = await cmd("Runtime.evaluate", { expression: CHIPS_EXPR, returnByValue: true });
        return (res && res.result && res.result.value) || 0;
      } catch (e) { return 0; }
    }
    // Wait for rows to render for what we just typed. Returns the rows (possibly empty).
    async function waitForRows(maxMs) {
      const t0 = Date.now();
      let rows = [];
      while (Date.now() - t0 < maxMs) {
        rows = await optionRows();
        if (rows.length) return rows;
        await sleep(200);
      }
      return rows;
    }

    const FOCUS_SETTLE = 250,   // after the focus click, before we touch the box
          TYPE_SETTLE = 450,    // after typing, before we start looking for the dropdown
          LOAD_WAIT = 3000,     // max time to let the suggestion list load
          SETTLE = 250,         // let the list stop repainting before we read it
          AFTER_COMMIT = 600;   // after a skill commits, before the next one

    // FREE-TEXT runs on its own, slower clock. That path has no taxonomy to match against, so
    // there is no dropdown row to confirm against either — the only evidence a skill landed is a
    // chip appearing, and Workday's React input needs noticeably longer to catch up before it will
    // turn typed text into one. Rushing it produced the failures this mode is known for: an Enter
    // arriving before the text registered (nothing committed), or the next skill typed onto the
    // tail of one that never cleared. Deliberately unhurried; reliability over speed.
    const FT_FOCUS_SETTLE = 900,   // after focusing, before typing
          FT_AFTER_TYPE = 1500,    // after the text lands, before Enter
          FT_AFTER_ENTER = 2200,   // after Enter, before we look for the chip
          FT_BETWEEN = 1100,       // after one skill commits, before the next is typed
          FT_VALUE_WAIT = 4000;    // max wait for typed text to appear in the box

    async function focusBox() {
      const r = await rect();
      if (!r || !r.ok) return false;
      await click(r.x, r.y);
      await sleep(FOCUS_SETTLE);
      return true;
    }

    // Type one query and return the rows Workday offered for it.
    //
    // The text is READ BACK after typing. Input.insertText goes to whatever has focus, and the
    // focus click is aimed at coordinates taken right after a scrollIntoView — if layout shifts,
    // the click misses, the box never focuses, and the characters go nowhere. That failure is
    // silent and looks exactly like "the search isn't working", so it's checked and retried here.
    async function typeQuery(text) {
      await cmd("Input.insertText", { text });
      await sleep(TYPE_SETTLE);
      const got = await boxValue();
      return got.trim().toLowerCase() === text.trim().toLowerCase();
    }

    async function search(text) {
      if (!(await focusBox())) return null;
      let clean = await clearBox();
      if (!clean) { if (await focusBox()) clean = await clearBox(); }

      let typed = await typeQuery(text);
      if (!typed) {                                  // click missed the box — re-aim and retry once
        if (!(await focusBox())) return null;
        await clearBox();
        typed = await typeQuery(text);
        if (!typed) return { typeFailed: true, rows: [] };
      }
      await waitForRows(LOAD_WAIT);
      await sleep(SETTLE);
      return await optionRows();      // re-read after the settle, in case it repainted
    }

    // Add ONE skill: try each query form, and for each, pick the row that genuinely IS the skill.
    // Nothing is committed unless a row matches — pressing Enter on the highlighted row is exactly
    // how the wrong skill used to get added.
    async function addSkill(skill) {
      const before = await chipCount();
      let sawRows = 0;
      for (const q of skillQueries(skill)) {
        if (stopRequested) return { added: false, stopped: true };
        const res = await search(q);
        if (res === null) return { added: false, noField: true };
        if (res.typeFailed) return { added: false, typeFailed: true };
        const rows = res;
        sawRows = Math.max(sawRows, rows.length);
        if (!rows.length) continue;                       // nothing offered for this wording
        const hit = pickSkillOption(q, rows);
        if (!hit) continue;                               // offered only near-misses — try next form
        await click(rows[hit.i].x, rows[hit.i].y);        // trusted click on the RIGHT row
        await sleep(AFTER_COMMIT);
        if (await chipCount() > before) {
          const sub = coreTerm(hit.text) !== coreTerm(skill)
            ? { from: skill, to: hit.text, via: q } : null;
          await focusBox(); await clearBox();
          return { added: true, substituted: sub };
        }
      }
      await focusBox(); await clearBox();                 // leave the box empty for the next skill
      return { added: false, sawRows };
    }

    // FREE-TEXT mode: the field accepts anything you type — no taxonomy to match against, so the
    // old type-then-Enter cycle is exactly right here and picking-from-a-list would be wrong.
    // This is the behaviour that worked before the strict matcher went in; it is kept as its own
    // mode rather than replaced, because which one is correct depends entirely on the form.
    // Wait until the box actually CONTAINS what we typed. This replaces the old fixed sleep: a
    // sleep either waits too long on a fast tenant or not long enough on a slow one, and the
    // failure mode of "not long enough" is silent — Enter fires against an empty box and the skill
    // is quietly lost. Polling turns that into something we can see and retry.
    async function waitForBoxValue(want, timeout) {
      const target = want.trim().toLowerCase();
      const deadline = Date.now() + timeout;
      let seen = "";
      while (Date.now() < deadline) {
        seen = (await boxValue()).trim().toLowerCase();
        if (seen === target) return { ok: true, value: seen };
        await sleep(120);
      }
      return { ok: false, value: seen };
    }

    async function addFreeText(skill) {
      const before = await chipCount();
      if (!(await focusBox())) return { added: false, noField: true };
      await sleep(FT_FOCUS_SETTLE);
      let clean = await clearBox();
      if (!clean) { if (await focusBox()) clean = await clearBox(); }

      // Type, then CONFIRM the text is in the box before doing anything else. One retry, because
      // a dropped insert is the single most common free-text failure and re-typing fixes it.
      await cmd("Input.insertText", { text: skill });
      let landed = await waitForBoxValue(skill, FT_VALUE_WAIT);
      if (!landed.ok) {
        await focusBox();
        await sleep(FT_FOCUS_SETTLE);
        await clearBox();
        await cmd("Input.insertText", { text: skill });
        landed = await waitForBoxValue(skill, FT_VALUE_WAIT);
      }
      if (!landed.ok) {
        await focusBox(); await clearBox();
        return { added: false, typeFailed: true };   // never Enter on text that isn't there
      }

      await sleep(FT_AFTER_TYPE);
      await waitForRows(2500);                       // a list MAY appear; give it more time
      await sleep(SETTLE + 200);
      await keyPress({ key: "Enter", code: "Enter", vk: 13, text: "\r" });
      await sleep(FT_AFTER_ENTER);
      let now = await chipCount();
      if (now <= before) {                           // some tenants need a confirming second Enter
        await keyPress({ key: "Enter", code: "Enter", vk: 13, text: "\r" });
        await sleep(FT_AFTER_ENTER);
        now = await chipCount();
      }
      // Free-text tenants sometimes show a "Create …" / exact-match row — click it if Enter failed.
      if (now <= before) {
        const rows = await optionRows();
        const want = skill.trim().toLowerCase();
        let idx = rows.findIndex((r) => (r.text || "").trim().toLowerCase() === want);
        if (idx < 0) idx = rows.findIndex((r) => want && (r.text || "").toLowerCase().includes(want));
        if (idx >= 0) {
          await click(rows[idx].x, rows[idx].y);
          await sleep(FT_AFTER_ENTER);
          now = await chipCount();
        }
      }
      // If the field keeps no chips at all, we can't verify — treat a cleared box as committed.
      const box = await boxValue();
      const ok = now > before || (now === 0 && before === 0 && !box);
      if (!ok) { await focusBox(); await clearBox(); }
      await sleep(FT_BETWEEN);                       // let the widget settle before the next skill
      return { added: ok };
    }

    let added = 0, stopped = false, found = false, sawNoRows = 0, typeFailures = 0;
    const missed = [], substituted = [];
    try {
      const r0 = await rect();
      if (!r0 || !r0.ok) return { found: false, added: 0, total: skills.length };
      found = true;
      await focusBox();                               // prime the widget once, as the old flow did
      await clearBox();
      for (const skill of skills) {
        if (stopRequested) { stopped = true; break; }
        let res = { added: false };
        try { res = freeText ? await addFreeText(skill) : await addSkill(skill); } catch (e) {
          res = { added: false, error: String(e).slice(0, 60) };
        }
        if (res.stopped) { stopped = true; break; }
        if (res.noField) { missed.push(skill); break; }
        if (res.typeFailed) { missed.push(skill); typeFailures++; }
        if (res.added) { added++; if (res.substituted) substituted.push(res.substituted); }
        else { missed.push(skill); if (res.sawRows === 0) sawNoRows++; }
        if (onProgress) onProgress(added, skill, res);
      }
      try { if (await focusBox()) await clearBox(); } catch (e) {}
    } finally {
      await rrCdpRelease(tab.id);
    }
    return { found, added, missed, substituted, sawNoRows, typeFailures,
             total: skills.length, stopped };
  }

  // ================= React-props SKILLS fill (no debugger, page/MAIN world) =================
  // This function is INJECTED into the page (MAIN world) via chrome.scripting, so it can read
  // Workday's own React handlers off the DOM (the __reactProps$ key) and drive the field the way
  // Workday expects: set the value + fire React's onChange to run the async option search, then
  // CLICK the matching option. Because nothing is typed as keystrokes, there's no text to clear,
  // nothing gets dropped, and two skills can't merge into one box. Self-contained (no outer refs).
  // Fill ONE skill, the way berellevy/job_app_filler (the open-source Simplify clone) drives a
  // Workday searchable multi-select. The previous version got the shape right but missed the two
  // details that make it actually commit:
  //
  //   1. React keeps a private "value tracker" on every controlled input. Assigning `el.value = x`
  //      updates that tracker too, so React compares new-vs-tracked, sees no change, and drops the
  //      onChange — the async option search never fires. You have to write through the NATIVE
  //      prototype setter, which bypasses the tracker and leaves React seeing a real change.
  //   2. The widget only opens its dropdown once it has been focused/clicked. Typing into a cold
  //      input searches nothing.
  //
  // Everything else is verification: Workday reports success by adding a CHIP, so that — not the
  // absence of an error — is what we wait for.
  // React path, split in two so the SAME matcher decides for both engines: the page reports what
  // Workday offered, the panel picks the right row, the page clicks that exact row. Nothing is
  // committed on the page's own judgement.
  //
  // The native value setter is what makes React register the change at all: assigning el.value
  // updates React's private value tracker too, so React sees new === tracked and drops onChange.
  const WD_PAGE_CONST = {
    INPUT_SELS: ['[data-automation-id="formField-skills"] input',
                 '[data-automation-id="skillsSection"] input',
                 '[data-automation-id="multiSelectContainer"] input'],
    OPT_SEL: '[data-automation-id="promptOption"],[data-automation-id="menuItem"],' +
             '[role="option"],li[role="option"]',
    CHIP_SEL: 'ul[data-automation-id="selectedItemList"] li,' +
              '[data-automation-id="selectedItem"],[data-automation-id="DELETE_charm"]',
    EMPTY: "^(no items|no results|no matches|nothing found|loading)\\b",
  };

  // Shared page-side helpers, injected as a string because chrome.scripting serialises `func`
  // without its closure.
  function wdPageHelpers(K) {
    const EMPTY_RX = new RegExp(K.EMPTY, "i");
    const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 1 && r.height > 1; };
    const props = (el) => {
      for (const k in el) {
        if (k.startsWith("__reactProps") || k.startsWith("__reactEventHandlers")) return el[k];
      }
      for (const k in el) {
        if (k.startsWith("__reactFiber") || k.startsWith("__reactInternalInstance")) {
          const f = el[k];
          if (f && f.memoizedProps) return f.memoizedProps;
          if (f && f.return && f.return.memoizedProps) return f.return.memoizedProps;
        }
      }
      return null;
    };
    let input = null;
    for (const sel of K.INPUT_SELS) { input = [...document.querySelectorAll(sel)].find(vis); if (input) break; }
    if (!input) return null;
    const container = input.closest('[data-automation-id="formField-skills"]')
                   || input.closest('[data-automation-id="multiSelectContainer"]')
                   || input.parentElement || document;
    const ownedList = () => {
      const id = input.getAttribute("aria-controls") || input.getAttribute("aria-owns");
      return id ? document.getElementById(id) : null;
    };
    // Scoped + empty-state-filtered. Other Workday pickers (education) use identical markup, so a
    // page-wide query reads the wrong widget entirely.
    const optionNodes = () => {
      const own = ownedList(), root = own || document;
      return [...root.querySelectorAll(K.OPT_SEL)].filter((o) => {
        if (!vis(o)) return false;
        if (EMPTY_RX.test((o.textContent || "").trim())) return false;
        if (own) return true;
        const ff = o.closest('[data-automation-id^="formField-"]');
        return !ff || ff === container;
      });
    };
    const chips = () => container.querySelectorAll(K.CHIP_SEL).length;
    const evt = () => ({ target: input, currentTarget: input, preventDefault() {}, stopPropagation() {} });
    const typeInto = (v) => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
      setter ? setter.call(input, v) : (input.value = v);
      const p = props(input);
      try { p && p.onChange && p.onChange(evt()); } catch (e) {}
      input.dispatchEvent(new Event("input", { bubbles: true }));
    };
    return { input, container, optionNodes, chips, typeInto, props, vis };
  }

  // Type a query and report what Workday offered. No decisions taken here.
  async function pageSearchSkill(K, query, helpersSrc) {
    const H = new Function("K", "return (" + helpersSrc + ")(K)")(K);
    if (!H) return { found: false };
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    H.input.scrollIntoView({ block: "center" });
    try { H.input.focus(); H.input.click(); } catch (e) {}
    await sleep(60);
    const chipsBefore = H.chips();
    H.typeInto(query);
    let rows = [];
    for (let t = 0; t < 25; t++) {
      rows = H.optionNodes();
      if (rows.length) break;
      await sleep(100);
    }
    await sleep(150);
    rows = H.optionNodes();
    return { found: true, chipsBefore,
             rows: rows.map((o) => ({ text: (o.textContent || "").replace(/\s+/g, " ").trim() })) };
  }

  // Click the row the PANEL chose, then confirm a chip actually appeared.
  async function pageClickSkillOption(K, index, chipsBefore, helpersSrc) {
    const H = new Function("K", "return (" + helpersSrc + ")(K)")(K);
    if (!H) return { added: false };
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const rows = H.optionNodes();
    const node = rows[index];
    if (!node) { H.typeInto(""); return { added: false, note: "row disappeared" }; }
    const target = node.closest('[data-automation-id="promptOption"],[role="option"]') || node;
    target.scrollIntoView({ block: "center" });
    try { target.click(); } catch (e) {}
    const p = H.props(target);
    try { p && p.onClick && p.onClick({ target, currentTarget: target, preventDefault() {}, stopPropagation() {} }); } catch (e) {}
    for (let t = 0; t < 20; t++) {
      if (H.chips() > chipsBefore) { H.typeInto(""); return { added: true }; }
      await sleep(100);
    }
    H.typeInto("");
    return { added: false, note: "clicked but no chip appeared" };
  }

  // Runs ONE skill and reports what happened at each stage, so a failure names its cause instead
  // of just saying "0 added". This is the fastest way to tell a missing field from a React-version
  // mismatch from Workday simply not offering the skill.
  async function pageDiagnoseSkill(skill) {
    const INPUT_SELS = [
      '[data-automation-id="formField-skills"] input',
      '[data-automation-id="skillsSection"] input',
      '[data-automation-id="multiSelectContainer"] input',
    ];
    const OPT_SEL = '[data-automation-id="promptOption"],[data-automation-id="menuItem"],' +
                    '[role="option"],li[role="option"]';
    const CHIP_SEL = 'ul[data-automation-id="selectedItemList"] li,' +
                     '[data-automation-id="selectedItem"],[data-automation-id="DELETE_charm"]';
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const vis = (el) => { const r = el.getBoundingClientRect(); return r.width > 1 && r.height > 1; };
    const out = { url: location.href.slice(0, 120), frame: window === window.top ? "top" : "iframe" };

    let input = null, usedSel = "";
    for (const sel of INPUT_SELS) {
      input = [...document.querySelectorAll(sel)].find(vis);
      if (input) { usedSel = sel; break; }
    }
    // Any input at all inside a skills-ish container? Tells us "wrong selector" vs "wrong page".
    out.anyFileFieldish = document.querySelectorAll('[data-automation-id*="kill"]').length;
    if (!input) { out.stage = "no-skills-input"; return out; }
    out.stage = "input-found"; out.selector = usedSel;

    // which React key does this build expose?
    const keys = [];
    for (const k in input) {
      if (k.startsWith("__react")) keys.push(k.split("$")[0]);
    }
    out.reactKeys = [...new Set(keys)];
    out.hasHandlers = false;
    for (const k in input) {
      if (k.startsWith("__reactProps") || k.startsWith("__reactEventHandlers")) {
        const v = input[k];
        out.hasHandlers = !!(v && (v.onChange || v.onInput));
        break;
      }
    }
    // Scope exactly like the filler does, so the diagnosis reflects what the filler will see.
    const container = input.closest('[data-automation-id="formField-skills"]')
                   || input.closest('[data-automation-id="multiSelectContainer"]')
                   || input.parentElement || document;
    const ownedList = () => {
      const id = input.getAttribute("aria-controls") || input.getAttribute("aria-owns");
      return id ? document.getElementById(id) : null;
    };
    const EMPTY_RX = /^(no items|no results|no matches|nothing found|loading)\b/i;
    const optionNodes = (keepEmpty) => {
      const own = ownedList();
      const root = own || document;
      return [...root.querySelectorAll(OPT_SEL)].filter((o) => {
        if (!vis(o)) return false;
        if (!keepEmpty && EMPTY_RX.test((o.textContent || "").trim())) return false;
        if (own) return true;
        const ff = o.closest('[data-automation-id^="formField-"]');
        return !ff || ff === container;
      });
    };
    out.chipsBefore = container.querySelectorAll(CHIP_SEL).length;
    out.chipsPageWide = document.querySelectorAll(CHIP_SEL).length;
    out.ariaControls = input.getAttribute("aria-controls") || input.getAttribute("aria-owns") || "(none)";
    const baseline = new Set(optionNodes(true));
    out.optionsBefore = baseline.size;

    // type through the native setter and see whether the async search actually fires
    try { input.focus(); input.click(); } catch (e) {}
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set;
    setter ? setter.call(input, skill) : (input.value = skill);
    for (const k in input) {
      if (k.startsWith("__reactProps") || k.startsWith("__reactEventHandlers")) {
        try { input[k].onChange && input[k].onChange({ target: input, currentTarget: input, preventDefault() {}, stopPropagation() {} }); } catch (e) { out.onChangeThrew = String(e).slice(0, 60); }
        break;
      }
    }
    input.dispatchEvent(new Event("input", { bubbles: true }));

    let opts = [], shown = [];
    for (let t = 0; t < 30; t++) {
      shown = optionNodes(true).filter((o) => !baseline.has(o));    // everything the box drew
      opts = shown.filter((o) => !EMPTY_RX.test((o.textContent || "").trim()));  // real matches
      if (opts.length) break;
      if (shown.length && t > 12) break;                            // it answered: empty state
      await sleep(100);
    }
    out.optionsAppeared = opts.length;
    out.emptyState = shown.length > 0 && opts.length === 0;
    out.optionSample = shown.slice(0, 5).map((o) => (o.textContent || "").trim().slice(0, 40));
    out.stage = opts.length ? "options-rendered" : (out.emptyState ? "empty-state" : "no-options");
    // leave the box as we found it — this is a probe, not a fill
    setter ? setter.call(input, "") : (input.value = "");
    input.dispatchEvent(new Event("input", { bubbles: true }));
    return out;
  }

  async function diagnose(btn) {
    const oldTxt = btn.textContent; btn.disabled = true; btn.textContent = "Probing…";
    try {
      const tab = await activeTab();
      const host = hostOf(tab && tab.url);
      if (!WD_HOSTS.test(host)) { setSt(`Not a Workday page (${host || "no tab"}).`, "err"); return; }
      const { skills } = await skillsForActiveJob(host);
      const probe = skills[0] || "Python";
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id, allFrames: true }, world: "MAIN",
        func: pageDiagnoseSkill, args: [probe],
      });
      const all = (results || []).map((r) => r && r.result).filter(Boolean);
      const r = all.find((x) => x.stage !== "no-skills-input") || all[0];
      if (!r) { setSt("Couldn't inject into the page at all.", "err"); return; }

      const lines = [
        `probe skill: "${probe}"`,
        `frame: ${r.frame} · ${r.url}`,
        `skills input: ${r.stage === "no-skills-input" ? "NOT FOUND" : "found (" + r.selector + ")"}`,
        `elements with a skills-ish automation-id: ${r.anyFileFieldish}`,
        `React keys on the input: ${(r.reactKeys || []).join(", ") || "NONE"}`,
        `React onChange handler reachable: ${r.hasHandlers ? "yes" : "NO"}`,
        `listbox the input points at: ${r.ariaControls}`,
        `options on screen BEFORE typing (ignored): ${r.optionsBefore}`,
        `NEW options after typing: ${r.optionsAppeared}`,
        r.optionSample && r.optionSample.length ? `  → ${r.optionSample.join(" | ")}` : "",
        r.onChangeThrew ? `onChange threw: ${r.onChangeThrew}` : "",
        `skills chips in this widget: ${r.chipsBefore}  (page-wide, all widgets: ${r.chipsPageWide})`,
      ].filter(Boolean);

      const verdict = r.stage === "no-skills-input"
        ? "The Skills field isn't on this page (or is in a frame we can't see). Scroll it into view first."
        : !r.hasHandlers
          ? "React handlers aren't reachable on this build — switch the dropdown to “Trusted typing” and retry."
          : r.emptyState
            ? "The dropdown OPENED but Workday searched nothing — it returned its “No Items.” empty "
              + "state. This tenant runs its skill search off real keystrokes, which React-props "
              + "injection can't produce. Use “Trusted typing”, or leave the dropdown on Auto and "
              + "it will switch by itself."
            : r.optionsAppeared === 0
              ? "Typing fired but the skills box returned nothing at all — switch the dropdown to “Trusted typing” and retry."
              : "The skills widget responds correctly here; the React engine should work.";

      const box = $(wdUi.review || "");
      setSt(verdict, r.stage === "options-rendered" ? "ok" : "err");
      const host2 = box || null;
      const card = el("div", { class: "card" },
        el("div", { class: "k" }, "🔍 Workday skills diagnosis"),
        ...lines.map((t) => el("div", { style: "font-size:11.5px;margin-top:2px;white-space:pre-wrap" }, t)));
      if (host2) { host2.innerHTML = ""; host2.append(card); }
    } catch (e) {
      setSt("Diagnose failed: " + e.message, "err");
    } finally { btn.disabled = false; btn.textContent = oldTxt; }
  }

  // Panel-side driver. Per skill: search each query form, let pickSkillOption choose the row, then
  // click that exact row. Runs in all frames (Workday is sometimes iframed) and uses whichever
  // frame actually has the field. Stop is honoured between every step.
  async function fillSkillsReact(tab, skills, onProgress) {
    const helpersSrc = wdPageHelpers.toString();
    const K = WD_PAGE_CONST;

    const inject = async (func, args) => {
      const results = await chrome.scripting.executeScript({
        target: { tabId: tab.id, allFrames: true }, world: "MAIN", func, args,
      });
      return (results || []).map((r) => r && r.result).filter(Boolean);
    };

    let present = false;
    try {
      const probe = await inject(
        (K2, src) => !!new Function("K", "return (" + src + ")(K)")(K2), [K, helpersSrc]);
      present = probe.some(Boolean);
    } catch (e) { return { error: "Couldn't inject into the page (" + e.message + ")." }; }
    if (!present) return { found: false, added: 0, total: skills.length };

    let added = 0, already = 0, stopped = false;
    const missed = [], substituted = [];

    for (const skill of skills) {
      if (stopRequested) { stopped = true; break; }
      let got = false;
      for (const q of skillQueries(skill)) {
        if (stopRequested) { stopped = true; break; }
        let searches = [];
        try { searches = await inject(pageSearchSkill, [K, q, helpersSrc]); } catch (e) {}
        const found = searches.find((r) => r && r.found);
        if (!found || !(found.rows || []).length) continue;

        // THE decision, made here with the same matcher the trusted path uses: walk the whole list
        // and take the row that genuinely is this skill — or none.
        const hit = pickSkillOption(q, found.rows);
        if (!hit) continue;

        let clicks = [];
        try { clicks = await inject(pageClickSkillOption, [K, hit.i, found.chipsBefore, helpersSrc]); } catch (e) {}
        if (clicks.some((c) => c && c.added)) {
          added++; got = true;
          if (coreTerm(hit.text) !== coreTerm(skill)) {
            substituted.push({ from: skill, to: hit.text, via: q });
          }
          break;
        }
      }
      if (!got && !stopped) missed.push(skill);
      if (onProgress) onProgress(added);
      await sleep(200);
    }
    return { found: true, added, already, missed, substituted, total: skills.length, stopped };
  }

  // ---- panel side: gather skills, confirm it's Workday, run ----
  async function activeTab() {
    const [t] = await chrome.tabs.query({ active: true, currentWindow: true });
    return t;
  }
  function hostOf(url) { try { return new URL(url).hostname; } catch (e) { return ""; } }

  // Which tracked application does this Workday tab belong to? Exact URL match → same tenant host
  // (most recent) → most recently generated résumé overall. So a tab whose JD was never read still
  // gets the latest résumé's skills/work (never the un-ordered profile dump).
  async function appForActiveTab(host) {
    let jobs = {};
    try { jobs = await getJobs(); } catch (e) { return null; }
    const withResume = Object.values(jobs).filter((j) => j.resume_id);
    if (typeof currentJobId !== "undefined" && currentJobId
        && jobs[currentJobId] && jobs[currentJobId].resume_id) return jobs[currentJobId].resume_id;
    const byHost = withResume
      .filter((j) => j.host && host && j.host === host)
      .sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
    if (byHost.length) return byHost[0].resume_id;
    const recent = withResume.sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
    return recent.length ? recent[0].resume_id : null;
  }

  // The SAME list for every job (user's call, 2026-08-20): the verified set is small enough that
  // ranking it by JD relevance and capping at 15 only ever dropped real skills from the form.
  // No tracker lookup, no per-tab resolution — what gets typed matches what the résumé prints.
  async function skillsForActiveJob(host) {
    try {
      const r = await fetch(`${API}/skills/verified`);
      if (r.ok) {
        const j = await r.json();
        if (j.error) return { skills: [], label: "", error: j.error };
        return { skills: j.skills || [], label: "your full verified skills list" };
      }
    } catch (e) {}
    return { skills: [], label: "" };
  }

  // These fills are offered from BOTH the My Info card and the Ask card, so the status lines,
  // the mode dropdown and the Stop button all have to resolve to whichever card started the run.
  const WD_UIS = {
    myinfo: { skills: "wdSkillsStatus", work: "wdWorkStatus", mode: "wdSkillsMode",
              stop: "wdSkillsStopBtn", review: "wdDiagBox", dates: "wdDateBox" },
    ask: { skills: "askWdSkillsStatus", work: "askWdWorkStatus", mode: "askWdSkillsMode",
           stop: "askWdSkillsStopBtn", review: "askWdDiagBox", dates: "askWdDateBox" },
  };
  let wdUi = WD_UIS.myinfo;

  function setSt(msg, cls = "") {
    const s = $(wdUi.skills); if (!s) return;
    s.textContent = msg; s.className = "status " + cls;
  }

  // Clear any leftover armed session before filling (legacy; arming is retired).
  async function disarmBeforeFill(tab) {
    try {
      const st = await chrome.runtime.sendMessage({ type: "pickerState", tabId: tab.id });
      if (!(st && st.armed)) return false;
      await chrome.runtime.sendMessage({ type: "disarmPicker", tabId: tab.id });
      await sleep(250);
      return true;
    } catch (e) { return false; }
  }

  async function run(btn, from) {
    if (from) wdUi = WD_UIS[from] || wdUi;
    const tab = await activeTab();
    const host = hostOf(tab && tab.url);
    if (!WD_HOSTS.test(host)) {
      setSt(`This isn't a Workday page (${host || "no tab"}). Open the Workday application first.`, "err");
      return;
    }
    await disarmBeforeFill(tab);
    const { skills, label } = await skillsForActiveJob(host);
    if (!skills.length) { setSt("No skills found — check skills_verified.yaml and that ./run.sh is up.", "err"); return; }
    const modeSel = $(wdUi.mode);
    const engine = (modeSel && modeSel.value) || DEFAULT_ENGINE;
    const useReact = engine === "react" || engine === "auto";

    stopRequested = false;
    btn.disabled = true;
    const stopBtn = $(wdUi.stop);
    if (stopBtn) stopBtn.disabled = false;
    const banner = useReact ? "" : ' Chrome will show a "debugging" banner while it types.';
    setSt(`Filling ${skills.length} skills (${label})… leave this tab in front.${banner}`);
    try {
      // Live per-skill progress: says what it just tried and what came back, so a run that finds
      // nothing is visibly finding nothing instead of looking like it never started.
      const prog = (n, skill, res) => {
        const detail = skill
          ? ` — last: ${skill}${res && res.added ? " ✓" : res && res.sawRows === 0 ? " (no options came back)" : " ✗"}`
          : "";
        setSt(`Filling skills… (${n}/${skills.length}) ${label}${detail}`);
      };
      let r;
      if (engine === "auto") {
        // Try React on ONE skill first. Tenants differ: some fire their skill search off React's
        // onChange (React works, no banner), others off real keystrokes (only trusted CDP input
        // will do). One probe tells us which, without spending 2.5s per skill finding out.
        setSt(`Testing the React engine on “${skills[0]}”…`);
        const probe = await fillSkillsReact(tab, skills.slice(0, 1), () => {});
        if (!probe.found) r = probe;
        else if (probe.added > 0 || probe.already > 0) {
          const rest = await fillSkillsReact(tab, skills.slice(1), (n) => prog(n + probe.added));
          r = { found: true, total: skills.length, stopped: rest.stopped,
                added: probe.added + rest.added, already: probe.already + rest.already,
                missed: [...(probe.missed || []), ...(rest.missed || [])],
                substituted: [...(probe.substituted || []), ...(rest.substituted || [])] };
        } else {
          setSt('React engine got no matches — switching to trusted typing. Chrome will show a '
                + '"debugging" banner while it types; leave this tab in front.');
          r = await fillViaDebugger(tab, skills, prog, false);
          r.viaFallback = true;
        }
      } else if (engine === "freetext") {
        setSt('Typing each skill and pressing Enter — no list matching. Chrome will show a '
              + '"debugging" banner; leave this tab in front.');
        r = await fillViaDebugger(tab, skills, prog, true);
      } else {
        r = useReact
          ? await fillSkillsReact(tab, skills, prog)
          : await fillViaDebugger(tab, skills, prog, false);
      }
      if (r.error) setSt(r.error, "err");
      else if (!r.found) setSt("Couldn't find the Workday Skills field on this page. Scroll to it, then retry.", "err");
      else if (r.stopped) setSt(`Stopped — added ${r.added} of ${r.total}. Add the rest by hand or run again.`, "");
      else {
        const dup = r.already ? `, ${r.already} already there` : "";
        const fb = r.viaFallback ? " (React didn't work on this tenant; used trusted typing)" : "";
        const subs = (r.substituted || []).length
          ? " Matched under a different name: " +
            r.substituted.map((x) => `${x.from} → ${x.to}`).slice(0, 4).join(", ") + "."
          : "";
        const miss = (r.missed || []).length
          ? ` Not in Workday's skill list, so left out: ${r.missed.slice(0, 6).join(", ")}` +
            ((r.missed.length > 6) ? ` +${r.missed.length - 6} more.` : ".")
          : "";
        if (r.typeFailures) {
          setSt(`Couldn't type into the skills box (${r.typeFailures} attempts). Scroll the Skills `
                + `field fully into view, click it once yourself, then run again.`, "err");
          return;
        }
        const hint = (r.sawNoRows && r.sawNoRows >= Math.max(2, r.total - r.added))
          ? " This field never returned a list — if it lets you type any skill freely, switch the "
            + "engine to “Free text”."
          : "";
        setSt(`Added ${r.added}/${r.total} skills${dup}.${fb}${subs}${miss}${hint}`,
              (r.missed || []).length ? "" : "ok");
      }
    } catch (e) {
      setSt("Fill failed: " + e.message, "err");
    } finally { btn.disabled = false; if (stopBtn) stopBtn.disabled = true; }
  }

  function stop() {
    stopRequested = true;
    setSt("Stopping after the current skill…");
  }

  const WD_CARDS = [
    { from: "myinfo", skills: "wdSkillsBtn", stop: "wdSkillsStopBtn",
      mode: "wdSkillsMode", work: "wdWorkBtn", diag: "wdDiagBtn" },
    { from: "ask", skills: "askWdSkillsBtn", stop: "askWdSkillsStopBtn",
      mode: "askWdSkillsMode", work: "askWdWorkBtn", diag: "askWdDiagBtn" },
  ];

  function wire() {
    for (const c of WD_CARDS) {
      const btn = $(c.skills);
      if (!btn || btn.dataset.wired) continue;
      btn.dataset.wired = "1";
      const sel = $(c.mode);
      if (sel) {
        // One shared preference behind both dropdowns — switching mode in one card must not
        // leave the other silently on the old setting.
        chrome.storage.local.get(MODE_KEY, (r) => { const m = r && r[MODE_KEY]; if (m) sel.value = m; });
        sel.addEventListener("change", () => {
          chrome.storage.local.set({ [MODE_KEY]: sel.value });
          WD_CARDS.forEach((o) => { const s2 = $(o.mode); if (s2 && s2 !== sel) s2.value = sel.value; });
        });
      }
      btn.addEventListener("click", () => run(btn, c.from));
      const stopBtn = $(c.stop);
      if (stopBtn) stopBtn.addEventListener("click", stop);
      const workBtn = $(c.work);
      if (workBtn) workBtn.addEventListener("click", () => runWork(workBtn, c.from));
      const diagBtn = $(c.diag);
      if (diagBtn) diagBtn.addEventListener("click", () => {
        wdUi = WD_UIS[c.from] || wdUi;
        diagnose(diagBtn);
      });
    }
  }

  // ==================== Workday WORK EXPERIENCE filler (trusted CDP input) ====================
  const WD_MONTHS = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12 };
  function parseMonthYear(str) {
    if (!str) return { month: "", year: "" };
    const s = String(str).toLowerCase();
    let month = "", year = "";
    const yr = s.match(/(?:19|20)\d{2}/); if (yr) year = yr[0];
    for (const k in WD_MONTHS) { if (s.includes(k)) { month = String(WD_MONTHS[k]).padStart(2, "0"); break; } }
    const mm = s.match(/\b(\d{1,2})[\/\-](\d{4})\b/); if (mm) { month = String(+mm[1]).padStart(2, "0"); year = mm[2]; }
    return { month, year };
  }

  // Helpers injected into every Runtime.evaluate: visibility test + work-card roots.
  const WORK_HELPERS = `
    const vis = (el) => { if(!el||el.disabled||el.readOnly)return false; const s=getComputedStyle(el); if(s.display==='none'||s.visibility==='hidden'||s.opacity==='0')return false; const r=el.getBoundingClientRect(); return r.width>1&&r.height>1; };
    const cardRoots = () => { const roots=[],seen=new Set(); for(const t of document.querySelectorAll('[data-automation-id="formField-jobTitle"]')){ if(!vis(t))continue; let best=t; for(let node=t.parentElement,d=0;node&&d<14;d++,node=node.parentElement){ const ts=[...node.querySelectorAll('[data-automation-id="formField-jobTitle"]')].filter(vis); if(ts.length===1)best=node;} if(!seen.has(best)){seen.add(best);roots.push(best);} } return roots; };

    // ---- date segments ------------------------------------------------------------------
    // Workday renders From/To as a row of role="spinbutton" inputs (Month / Day / Year), NOT as
    // ordinary text inputs. They are found by aria-label, the way berellevy/job_app_filler finds
    // them, with the data-automation-id form kept as a fallback for older tenants.
    const RP = (el) => { for (const k in el) { if (k.indexOf('__reactProps')===0 || k.indexOf('__reactEventHandlers')===0) return el[k]; } return null; };
    const ARROW = { ArrowUp: 38, ArrowDown: 40 };
    const arrowKey = (el, name) => el.dispatchEvent(new KeyboardEvent('keydown',
      { key: name, code: name, keyCode: ARROW[name], which: ARROW[name], bubbles: true, cancelable: true }));

    // Every date form-field in a card (a formField-* that owns a Year segment), innermost only.
    const dateFieldsIn = (card) => {
      const all = [...card.querySelectorAll('[data-automation-id^="formField-"]')].filter((f) =>
        f.querySelector('input[aria-label="Year"],[data-automation-id="dateSectionYear-input"]'));
      return all.filter((f) => !all.some((g) => g !== f && f.contains(g)));
    };
    // start vs end, from the automation-id first, then the visible label ("From" / "To").
    const dateKind = (f) => {
      const id = (f.getAttribute('data-automation-id') || '').toLowerCase();
      if (/start|from/.test(id)) return 'start';
      if (/end|thru|through|\\bto\\b/.test(id)) return 'end';
      const lb = f.querySelector('label,legend,[id$="-label"]');
      const l = ((f.getAttribute('aria-label') || '') + ' ' + ((lb && lb.textContent) || '')).toLowerCase();
      if (/start|from/.test(l)) return 'start';
      if (/end|thru|through|present|\\bto\\b/.test(l)) return 'end';
      return '';
    };
    // Labelled match wins; otherwise fall back to DOM order (From comes before To).
    const pickDate = (card, kind) => {
      const fs = dateFieldsIn(card);
      return fs.find((f) => dateKind(f) === kind) || (kind === 'start' ? fs[0] : fs[1]) || null;
    };
    const seg = (f, label) => (f ? f.querySelector('input[aria-label="' + label + '"]')
      || f.querySelector('[data-automation-id="dateSection' + label + '-input"]') : null);

    // One attempt at putting the wanted number into one segment. Strategies are tried by the PANEL, one per
    // call, so React has a real turn of the event loop to commit before we re-read the value.
    //
    //   arrowUp / arrowDown — seed the raw value one step off the target and let Workday's OWN
    //     spinbutton handler do the step. That handler is what runs React's state update, so the
    //     value sticks; writing the number in directly does not. (job_app_filler's trick.)
    //   reactKeyDown — call the input's React onKeyDown by hand, for builds that don't listen to
    //     dispatched keyboard events at all.
    //   nativeSetter — last DOM resort: native value setter (bypasses React's value tracker) plus
    //     input/change/blur.
    const segAction = (inp, want, strat) => {
      const n = parseInt(want, 10);
      const cur = () => { const v = parseInt(inp.value, 10); return isNaN(v) ? null : v; };
      const done = (s) => ({ value: inp.value, match: cur() === n, strat: s });
      if (strat === 'read' || cur() === n) return done(strat);
      inp.scrollIntoView({ block: 'center' });
      try { inp.focus(); } catch (e) {}
      if (strat === 'arrowUp' || strat === 'arrowDown') {
        const up = strat === 'arrowUp';
        inp.value = String(up ? n - 1 : n + 1);
        arrowKey(inp, up ? 'ArrowUp' : 'ArrowDown');
        try { inp.click(); } catch (e) {}
        return done(strat);
      }
      const p = RP(inp);
      if (strat === 'reactKeyDown') {
        if (!p || typeof p.onKeyDown !== 'function') return { value: inp.value, match: false, skipped: true };
        const stub = { value: String(n - 1), setSelectionRange: () => {} };
        try {
          p.onKeyDown({ nativeEvent: { key: 'Up', setSelectionRange: () => {} },
            preventDefault: () => {}, stopPropagation: () => {}, currentTarget: stub, target: stub });
          inp.click();
        } catch (e) { return { value: inp.value, match: false, threw: String(e).slice(0, 60) }; }
        return done(strat);
      }
      if (strat === 'nativeSetter') {
        const S = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
        S.call(inp, String(n));
        inp.dispatchEvent(new Event('input', { bubbles: true }));
        inp.dispatchEvent(new Event('change', { bubbles: true }));
        for (const h of ['onChange', 'onBlur']) {
          if (p && typeof p[h] === 'function') { try { p[h]({ target: inp, currentTarget: inp }); } catch (e) {} }
        }
        return done(strat);
      }
      return done(strat);
    };
  `;

  async function fillWorkViaDebugger(tab, entries) {
    const target = { tabId: tab.id };
    const acq = await rrCdpAcquire(tab.id);
    if (!acq.ok) return { error: "Couldn't attach. " + acq.error };
    const cmd = (m, p) => chrome.debugger.sendCommand(target, m, p || {});
    const evalV = async (body) => {
      const res = await cmd("Runtime.evaluate", { expression: `(()=>{${WORK_HELPERS}${body}})()`, returnByValue: true });
      return res && res.result && res.result.value;
    };
    const clickXY = async (x, y) => {
      await cmd("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", buttons: 1, clickCount: 1 });
      await cmd("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", buttons: 0, clickCount: 1 });
    };
    // center coords of a field within card i (scrolls it into view); {ok:false} if missing
    const rectOf = (i, sel) => evalV(`const c=cardRoots()[${i}]; if(!c)return{ok:false}; const el=c.querySelector(${JSON.stringify(sel)}); if(!el||!vis(el))return{ok:false}; el.scrollIntoView({block:'center'}); const r=el.getBoundingClientRect(); return {ok:true,x:r.left+r.width/2,y:r.top+r.height/2};`);
    const typeInto = async (i, sel, value) => {
      if (value === undefined || value === null || value === "") return;
      const r = await rectOf(i, sel);
      if (!r || !r.ok) return;
      await clickXY(r.x, r.y); await sleep(90);
      await cmd("Input.insertText", { text: String(value) });
      await sleep(70);
    };
    // Workday date segments are role="spinbutton" inputs — they IGNORE Input.insertText and only
    // update on real digit key events, so type them one digit at a time.
    const keyChar = async (ch) => {
      const vk = ch.charCodeAt(0);            // "0".."9" → 48..57, the right virtual key code
      const code = "Digit" + ch;
      await cmd("Input.dispatchKeyEvent", { type: "keyDown", key: ch, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, text: ch });
      await cmd("Input.dispatchKeyEvent", { type: "keyUp", key: ch, code, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, text: ch });
    };
    const typeDigits = async (i, sel, digits) => {
      if (!digits) return;
      const r = await rectOf(i, sel);
      if (!r || !r.ok) return;
      await clickXY(r.x, r.y); await sleep(90);
      for (const ch of String(digits)) { await keyChar(ch); await sleep(70); }
    };

    // ---- dates ---------------------------------------------------------------------------
    // Every strategy is verified by reading the segment back, because the failure mode here is
    // silent: Workday's spinbuttons swallow input they don't like and just keep showing MM/YYYY.
    // Strategies run one per round trip so React gets to commit before we check.
    const DATE_STRATS = ["arrowUp", "arrowDown", "reactKeyDown", "nativeSetter"];
    const segExpr = (i, kind, label, want, strat) =>
      `const c=cardRoots()[${i}]; if(!c)return{missing:'card'};`
      + `const f=pickDate(c,${JSON.stringify(kind)}); if(!f)return{missing:'field'};`
      + `const inp=seg(f,${JSON.stringify(label)}); if(!inp)return{missing:'segment'};`
      + `return segAction(inp,${JSON.stringify(String(want))},${JSON.stringify(strat)});`;

    // Fill ONE segment (Month / Day / Year) of one date. Returns "ok" | "missing" | "stuck".
    const fillSeg = async (i, kind, label, want) => {
      if (!want) return "skip";
      const first = await evalV(segExpr(i, kind, label, want, "read"));
      if (!first) return "missing";
      if (first.missing) return "missing";
      if (first.match) return "ok";
      for (const strat of DATE_STRATS) {
        const r = await evalV(segExpr(i, kind, label, want, strat));
        if (r && r.missing) return "missing";
        await sleep(170);
        const v = await evalV(segExpr(i, kind, label, want, "read"));
        if (v && v.match) return "ok";
      }
      // Last resort: trusted keystrokes through the debugger, aimed at the same segment.
      const rect = await evalV(
        `const c=cardRoots()[${i}]; if(!c)return{ok:false};`
        + `const f=pickDate(c,${JSON.stringify(kind)}); if(!f)return{ok:false};`
        + `const inp=seg(f,${JSON.stringify(label)}); if(!inp)return{ok:false};`
        + `inp.scrollIntoView({block:'center'}); const r=inp.getBoundingClientRect();`
        + `return {ok:r.width>0&&r.height>0,x:r.left+r.width/2,y:r.top+r.height/2};`);
      if (rect && rect.ok) {
        await clickXY(rect.x, rect.y); await sleep(120);
        for (const ch of String(want)) { await keyChar(ch); await sleep(80); }
        await sleep(200);
        const v = await evalV(segExpr(i, kind, label, want, "read"));
        if (v && v.match) return "ok";
      }
      return "stuck";
    };

    // Fill a whole From/To date. Day is only filled when the tenant asks for one: 1st for a start
    // date, last of the month for an end date, so the range still reads as the right months.
    const lastDay = (m, y) => new Date(+y, +m, 0).getDate();
    const fillDate = async (i, kind, my) => {
      const notes = [];
      if (!my.month && !my.year) return { ok: 0, tried: 0, notes: [`${kind}: no date on file`] };
      const hasDay = await evalV(
        `const c=cardRoots()[${i}]; if(!c)return false;`
        + `const f=pickDate(c,${JSON.stringify(kind)}); return !!seg(f,'Day');`);
      const parts = [["Month", my.month]];
      if (hasDay && my.month && my.year) {
        parts.push(["Day", kind === "end" ? lastDay(my.month, my.year) : 1]);
      }
      parts.push(["Year", my.year]);
      let ok = 0, tried = 0;
      for (const [label, want] of parts) {
        if (!want) { notes.push(`${kind} ${label.toLowerCase()}: nothing to fill`); continue; }
        tried++;
        const r = await fillSeg(i, kind, label, want);
        if (r === "ok") ok++;
        else notes.push(`${kind} ${label.toLowerCase()}: ${r === "missing" ? "field not on the form" : "wouldn't take " + want}`);
      }
      return { ok, tried, notes };
    };

    // What the date widgets actually look like on THIS tenant — only read when a fill fails, so
    // there is something concrete to go on instead of "it didn't work".
    const probeDates = (i) => evalV(
      `const c=cardRoots()[${i}]; if(!c)return{cards:0};`
      + `const fs=dateFieldsIn(c);`
      + `return {n:fs.length, fields: fs.map((f)=>({`
      + `  id: f.getAttribute('data-automation-id')||'', kind: dateKind(f),`
      + `  label: ((f.querySelector('label,legend,[id$="-label"]')||{}).textContent||'').trim().slice(0,40),`
      + `  segs: [...f.querySelectorAll('input')].map((x)=>({`
      + `    aria: x.getAttribute('aria-label')||'', aid: x.getAttribute('data-automation-id')||'',`
      + `    role: x.getAttribute('role')||'', value: x.value, ro: !!(x.readOnly||x.disabled),`
      + `    w: Math.round(x.getBoundingClientRect().width)}))})),`
      + ` sample: (fs[0]?fs[0].outerHTML:'').slice(0,700)};`);

    let added = 0, found = false;
    try {
      // STEP 1 — add cards until there are enough (start count varies per tenant)
      let have = await evalV("return cardRoots().length;");
      for (let g = 0; have < entries.length && g < entries.length + 3; g++) {
        const b = await evalV(`const btns=[...document.querySelectorAll('[data-automation-id="add-button"]')].filter(vis); let btn=null; if(btns.length<=1)btn=btns[0]||null; else { const heads=[...document.querySelectorAll('h1,h2,h3,h4,h5,h6,[role=heading]')].filter(h=>(h.innerText||'').trim()); const sec=(el)=>{let l='';for(const h of heads){if(el.compareDocumentPosition(h)&Node.DOCUMENT_POSITION_PRECEDING)l=(h.innerText||'').trim();}return l;}; const W=/work experience|work history|employment|professional experience|^experience|positions?/i, NW=/education|school|academic|skills?|certificat|licen|language|referenc|website|social|address|legal name|phone|email/i; for(const b of btns){const s=sec(b);if(W.test(s)&&!NW.test(s)){btn=b;break;}} if(!btn)for(const b of btns){if(!NW.test(sec(b))){btn=b;break;}} if(!btn)btn=btns[0]; } if(!btn)return{ok:false}; btn.scrollIntoView({block:'center'}); const r=btn.getBoundingClientRect(); return {ok:true,x:r.left+r.width/2,y:r.top+r.height/2};`);
        if (!b || !b.ok) break;
        await clickXY(b.x, b.y); await sleep(1000);
        const now = await evalV("return cardRoots().length;");
        if (now <= have) break;
        have = now;
      }
      const cardsN = await evalV("return cardRoots().length;");
      if (!cardsN) return { found: false, filled: 0, total: entries.length };
      found = true;

      const n = Math.min(entries.length, cardsN);
      let filled = 0, dateOk = 0, dateTried = 0;
      const dateNotes = [];
      for (let i = 0; i < n; i++) {
        const e = entries[i];
        await typeInto(i, '[data-automation-id="formField-jobTitle"] input', e.title);
        await typeInto(i, '[data-automation-id="formField-companyName"] input', e.company);
        if (e.location) await typeInto(i, '[data-automation-id="formField-location"] input', e.location);
        // "I currently work here" checkbox — trusted click if not already checked
        if (e.current) {
          const cb = await evalV(`const c=cardRoots()[${i}]; if(!c)return{ok:false}; const f=c.querySelector('[data-automation-id="formField-currentlyWorkHere"],[data-automation-id="formField-iCurrentlyWorkHere"],[data-automation-id*="currentlyWorkHere" i]'); if(!f)return{ok:false}; const inp=f.querySelector('input[type=checkbox]'); const rc=f.querySelector('[role=checkbox]'); const already=inp?inp.checked:(rc?rc.getAttribute('aria-checked')==='true':false); const t=f.querySelector('[data-automation-id="checkbox"],[role=checkbox],label,input[type=checkbox]')||f; t.scrollIntoView({block:'center'}); const r=t.getBoundingClientRect(); return {ok:true,already,x:r.left+r.width/2,y:r.top+r.height/2};`);
          if (cb && cb.ok && !cb.already) { await clickXY(cb.x, cb.y); await sleep(400); }
        }
        // dates — spinbuttons, driven by Workday's own step handler and verified segment by
        // segment (see fillDate). A role is only counted as filled if its dates landed.
        const sr = await fillDate(i, "start", parseMonthYear(e.start_date));
        dateOk += sr.ok; dateTried += sr.tried;
        dateNotes.push(...sr.notes.map((n) => `role ${i + 1} — ${n}`));
        if (!e.current) {
          const er = await fillDate(i, "end", parseMonthYear(e.end_date));
          dateOk += er.ok; dateTried += er.tried;
          dateNotes.push(...er.notes.map((n) => `role ${i + 1} — ${n}`));
        }
        // role description (textarea) — tailored bullets
        await typeInto(i, '[data-automation-id="formField-roleDescription"] textarea', e.description);
        filled++;
      }
      // Only pay for the DOM dump when something actually refused to fill.
      const dateProbe = dateTried && dateOk < dateTried ? await probeDates(0) : null;
      return { found: true, filled, total: entries.length, cards: cardsN,
               dateOk, dateTried, dateNotes, dateProbe };
    } finally {
      await rrCdpRelease(tab.id);
    }
  }

  async function workForActiveJob(host) {
    const tid = await appForActiveTab(host);
    if (!tid) return { entries: [], label: "" };
    try {
      const r = await fetch(`${API}/tracker/${tid}/work-experience`);
      if (r.ok) {
        const j = await r.json();
        const named = [j.company, j.role].filter(Boolean).join(" — ");
        return { entries: j.entries || [], label: named ? `from your ${named} résumé` : "from this job's résumé" };
      }
    } catch (e) {}
    return { entries: [], label: "" };
  }

  function setWorkSt(msg, cls = "") {
    const s = $(wdUi.work); if (!s) return;
    s.textContent = msg; s.className = "status " + cls;
  }

  // When a date refuses to fill, print what the widget actually looks like on this tenant. The
  // selectors are the whole game here, and this is the only way to see them without the page.
  function showDateDiag(r) {
    const box = $(wdUi.dates || "");
    if (!box) return;
    if (!r.dateNotes || !r.dateNotes.length) { box.innerHTML = ""; return; }
    const lines = [...r.dateNotes];
    const p = r.dateProbe;
    if (p) {
      lines.push(`date fields found in role 1: ${p.n}`);
      for (const f of p.fields || []) {
        lines.push(`  ${f.id || "(no automation-id)"} · read as "${f.kind || "?"}" · label "${f.label}"`);
        for (const sg of f.segs || []) {
          lines.push(`      aria="${sg.aria}" aid="${sg.aid}" role="${sg.role}" `
            + `value="${sg.value}" ${sg.ro ? "READONLY " : ""}w=${sg.w}`);
        }
      }
      if (p.sample) lines.push("markup: " + p.sample);
    }
    box.innerHTML = "";
    box.append(el("div", { class: "card" },
      el("div", { class: "k" }, "📅 Workday date diagnosis"),
      ...lines.map((t) => el("div",
        { style: "font-size:11.5px;margin-top:2px;white-space:pre-wrap;word-break:break-all" }, t))));
  }

  async function runWork(btn, from) {
    if (from) wdUi = WD_UIS[from] || wdUi;
    const tab = await activeTab();
    const host = hostOf(tab && tab.url);
    if (!WD_HOSTS.test(host)) {
      setWorkSt(`This isn't a Workday page (${host || "no tab"}). Open the Workday application first.`, "err");
      return;
    }
    await disarmBeforeFill(tab);
    const { entries, label } = await workForActiveJob(host);
    if (!entries.length) { setWorkSt("No work experience — generate a résumé for this job first.", "err"); return; }
    btn.disabled = true;
    setWorkSt(`Filling ${entries.length} work experience(s) ${label}… leave this tab in front; `
      + `Chrome shows a "debugging" banner while it types.`);
    try {
      const r = await fillWorkViaDebugger(tab, entries);
      if (r.error) setWorkSt(r.error, "err");
      else if (!r.found) setWorkSt("Couldn't find the Workday work-experience cards. Scroll to that section, then retry.", "err");
      else {
        const dates = !r.dateTried ? ""
          : r.dateOk === r.dateTried ? ` All ${r.dateOk} date field(s) set.`
          : ` Dates: ${r.dateOk} of ${r.dateTried} set — check the rest by hand.`;
        setWorkSt(`Filled ${r.filled} of ${r.total} role(s).${dates} Check the descriptions, then continue.`
          + (r.total > r.cards ? " (couldn't add enough cards for all)" : ""),
          r.dateTried && r.dateOk < r.dateTried ? "" : "ok");
        showDateDiag(r);
      }
    } catch (e) {
      setWorkSt("Fill failed: " + e.message, "err");
    } finally { btn.disabled = false; }
  }

  document.addEventListener("rr-tab-shown", (e) => {
    if (e.detail.tab === "myinfo" || e.detail.tab === "ask") wire();
  });
  wire();
})();
