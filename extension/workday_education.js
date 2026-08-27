// Workday EDUCATION filler — the two boxes that are genuinely painful to do by hand:
//
//   • School / University — Workday's list is a controlled taxonomy, so the name on your profile
//     ("University of Texas at Austin") may be listed as "The University of Texas at Austin" or
//     "University of Texas - Austin". Typing the full string finds nothing; you have to search a
//     PREFIX ("University of Texas") and then pick the right campus out of the list.
//   • Field of Study — same problem with different words: "Computer Science" may be listed as
//     "Computer and Information Sciences".
//
// So the panel does what a person does: search progressively broader queries, read back every
// option Workday actually offered, score them, and click the one that wins. Nothing is typed
// blindly and nothing is accepted on the page's own judgement — same split as the skills filler.
//
// The fill itself uses the berellevy/job_app_filler technique for Workday searchable dropdowns:
// call the input's own React onKeyDown with {key:'Tab', target:{value: query}}. Workday runs its
// search off that handler, so no keystrokes need to be faked. Trusted typing through the debugger
// is kept as a fallback for tenants whose React internals aren't reachable.
(() => {
  // Every Workday tenant domain, not just myworkdayjobs.com — Devon's careers site, for one,
  // lives on wd5.myworkdaysite.com. The (^|\.) prefix already covers the wdNN. subdomains.
  const WD_HOSTS = /(^|\.)(myworkdayjobs|myworkdaysite|myworkday|workday)\.com$/i;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const EDU_UIS = {
    myinfo: { btn: "wdEduBtn", status: "wdEduStatus", box: "wdEduBox" },
    ask: { btn: "askWdEduBtn", status: "askWdEduStatus", box: "askWdEduBox" },
  };
  let eduUi = EDU_UIS.myinfo;
  const setEduSt = (msg, cls = "") => {
    const s = $(eduUi.status); if (!s) return;
    s.textContent = msg; s.className = "status " + cls;
  };

  // ============================ matching (panel side, pure) ============================
  // Words that carry no identity — every school has them, so they can't tell Austin from Dallas.
  const STOP = new Set(["the", "of", "at", "a", "an", "and", "in", "for",
    "university", "universities", "college", "colleges", "school", "schools",
    "institute", "institution", "campus", "main", "system", "branch"]);

  const norm = (s) => String(s || "").toLowerCase()
    .replace(/[’']/g, "").replace(/[^a-z0-9]+/g, " ").replace(/\s+/g, " ").trim();
  // Fold plurals so "Sciences" matches "Science" — Workday's taxonomy pluralises freely.
  const stem = (t) => t.replace(/ies$/, "y").replace(/(ss)$/, "$1").replace(/es$/, "e").replace(/s$/, "");
  const toks = (s) => norm(s).split(" ").filter(Boolean).map(stem);
  const keyToks = (s) => norm(s).split(" ").filter((t) => t && !STOP.has(t)).map(stem);

  // How well one offered option matches what we want. -1 means "not this one".
  //   exact  → 1000
  //   strict → every distinguishing word of ours appears in the option; tightest option wins
  //   loose  → (only used when strict found nothing) most of our words appear, first one required
  function scoreOption(want, opt, loose) {
    const w = norm(want), o = norm(opt);
    if (!o) return -1;
    if (w === o) return 1000;
    const kw = keyToks(want);
    if (!kw.length) return -1;
    const ot = new Set(toks(opt));
    const hits = kw.filter((t) => ot.has(t)).length;
    const extra = Math.max(0, toks(opt).length - toks(want).length);
    if (hits === kw.length) return 500 - extra * 5;
    if (!loose) return -1;
    // Even when relaxed, the first AND last distinguishing words must both be there. The last one
    // is what separates Austin from Dallas — without this guard, searching "University of Texas"
    // and finding only the Dallas campus would happily pick the wrong school.
    if (!ot.has(kw[0]) || !ot.has(kw[kw.length - 1])) return -1;
    if (hits / kw.length < 0.5) return -1;
    return 200 * (hits / kw.length) - extra * 5;
  }

  function bestOption(want, options, loose) {
    let best = -1, bestScore = 0;
    options.forEach((o, i) => {
      const sc = scoreOption(want, o.text, loose);
      if (sc > bestScore) { bestScore = sc; best = i; }
    });
    return best >= 0 ? { index: best, score: bestScore, text: options[best].text } : null;
  }

  // Search terms to try, broadest last. This is the manual workflow written down: the full name
  // first, then the same name with fewer trailing words, so Workday's search actually returns the
  // family of options we can then choose the right campus/variant from.
  function queriesFor(value) {
    const raw = String(value || "").trim();
    if (!raw) return [];
    const out = [];
    const push = (q) => {
      const t = String(q || "").trim().replace(/\s+/g, " ");
      if (t && !out.some((x) => x.toLowerCase() === t.toLowerCase())) out.push(t);
    };
    push(raw);
    push(raw.replace(/^the\s+/i, ""));
    // Drop trailing words one at a time, stopping on a word that carries identity.
    const words = raw.replace(/^the\s+/i, "").split(/\s+/);
    for (let cut = words.length - 1; cut >= 2; cut--) {
      const head = words.slice(0, cut);
      while (head.length && STOP.has(norm(head[head.length - 1]))) head.pop();
      if (head.length < 2) break;
      push(head.join(" "));
      if (out.length >= 5) break;
    }
    const k = keyToks(raw);
    if (k.length) push(raw.split(/\s+/).find((w) => !STOP.has(norm(w))) || "");
    return out.slice(0, 5);
  }

  // ============================ page-side helpers ============================
  const EDU_HELPERS = `
    const vis = (el) => { if(!el)return false; const s=getComputedStyle(el); if(s.display==='none'||s.visibility==='hidden')return false; const r=el.getBoundingClientRect(); return r.width>1&&r.height>1; };
    const RP = (el) => { for (const k in el) { if (k.indexOf('__reactProps')===0 || k.indexOf('__reactEventHandlers')===0) return el[k]; } return null; };
    const SCHOOL = /school|universit|institution|college/i;
    const FIELD  = /field.?of.?study|fieldofstudy|degreefield|major/i;
    const anyField = (re) => [...document.querySelectorAll('[data-automation-id^="formField-"]')]
      .filter((f) => re.test(f.getAttribute('data-automation-id') || '') && vis(f));
    // One root per education entry: the tightest ancestor that owns exactly one school field.
    const eduRoots = () => {
      const roots=[],seen=new Set();
      for (const t of anyField(SCHOOL)) {
        let best=t;
        for (let node=t.parentElement,d=0; node&&d<14; d++, node=node.parentElement) {
          const owned = anyField(SCHOOL).filter((x)=>node.contains(x)).length;
          if (owned > 1) break;          // climbed past this entry into the next one
          if (owned === 1) best=node;
        }
        if(!seen.has(best)){seen.add(best);roots.push(best);}
      }
      return roots;
    };
    const pickField = (card, kind) => {
      const re = kind === 'school' ? SCHOOL : FIELD;
      const inCard = [...card.querySelectorAll('[data-automation-id^="formField-"]')]
        .filter((f) => re.test(f.getAttribute('data-automation-id') || '') && vis(f));
      if (kind === 'field') return inCard.find((f) => FIELD.test(f.getAttribute('data-automation-id')||'')) || null;
      // 'school' must not swallow "School Name" free-text twins; prefer the searchable one.
      return inCard.find((f) => f.querySelector('[data-automation-id="multiSelectContainer"]')) || inCard[0] || null;
    };
    const inputOf = (f) => f ? f.querySelector('input,textarea') : null;
    const searchable = (f) => !!(f && f.querySelector('[data-automation-id="multiSelectContainer"]'));
    // What Workday shows as CHOSEN. The chip list is the reliable one; some tenants also mirror
    // the choice into the input's own value.
    const chipOf = (f) => {
      if (!f) return '';
      const li = f.querySelector('[data-automation-id="selectedItemList"] li, [data-automation-id="selectedItem"]');
      if (li) return (li.getAttribute('data-automation-label') || li.innerText || '')
        .replace(/press delete to remove.*/i,'').replace(/[×✕✖]\\s*$/,'').trim();
      const inp = inputOf(f);
      return inp && inp.value ? inp.value.trim() : '';
    };
    // The popup Workday opened for THIS field. Popups live at body level, tied back by the
    // multiSelectContainer's id, so scoping matters — education has several identical widgets.
    const popupFor = (f) => {
      const c = f && f.querySelector('[data-automation-id="multiSelectContainer"]');
      const id = c && c.getAttribute('id');
      const pops = [...document.querySelectorAll('[data-automation-widget="wd-popup"]')].filter(vis);
      if (id) {
        const mine = pops.find((p) => p.querySelector('[data-associated-widget="' + id + '"]'));
        if (mine) return mine;
      }
      return pops[pops.length - 1] || null;
    };
    const optionsIn = (f) => {
      const pop = popupFor(f);
      if (!pop) return [];
      const rows = [...pop.querySelectorAll('[data-automation-id="promptOption"],[role="option"]')].filter(vis);
      return rows.map((r) => {
        const b = r.getBoundingClientRect();
        return { text: (r.getAttribute('data-automation-label') || r.innerText || '').trim(),
                 x: b.left + b.width/2, y: b.top + b.height/2 };
      }).filter((o) => o.text);
    };
  `;

  // ============================ the fill ============================
  async function fillEducationViaDebugger(tab, entries) {
    const target = { tabId: tab.id };
    const acq = await rrCdpAcquire(tab.id);
    if (!acq.ok) return { error: "Couldn't attach. " + acq.error };
    const cmd = (m, p) => chrome.debugger.sendCommand(target, m, p || {});
    const evalV = async (body) => {
      const res = await cmd("Runtime.evaluate",
        { expression: `(()=>{${EDU_HELPERS}${body}})()`, returnByValue: true });
      return res && res.result && res.result.value;
    };
    const clickXY = async (x, y) => {
      await cmd("Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", buttons: 1, clickCount: 1 });
      await cmd("Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", buttons: 0, clickCount: 1 });
    };
    const sel = (i, kind) => `const c=eduRoots()[${i}]; if(!c)return{missing:'card'};`
      + `const f=pickField(c,${JSON.stringify(kind)}); if(!f)return{missing:'field'};`;

    const readState = (i, kind) => evalV(sel(i, kind)
      + `return {chip:chipOf(f), options:optionsIn(f), searchable:searchable(f)};`);

    const clearBox = (i, kind) => evalV(sel(i, kind)
      + `const inp=inputOf(f); if(!inp)return false;`
      + `const S=Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set;`
      + `try{S.call(inp,'');}catch(e){inp.value='';}`
      + `inp.dispatchEvent(new Event('input',{bubbles:true})); return true;`);

    // Type a query in and let Workday search. React first (no keystrokes needed), trusted typing
    // second — that's the split that survives tenants with different React builds.
    const search = async (i, kind, query) => {
      const r = await evalV(sel(i, kind)
        + `const inp=inputOf(f); if(!inp)return{missing:'input'};`
        + `inp.scrollIntoView({block:'center'}); try{inp.focus();}catch(e){}`
        + `const p=RP(inp);`
        + `if(p&&typeof p.onKeyDown==='function'){`
        + `  try{ p.onKeyDown({key:'Tab', target:{value:${JSON.stringify(query)}, setSelectionRange:()=>{}},`
        + `        currentTarget:{value:${JSON.stringify(query)}, setSelectionRange:()=>{}},`
        + `        preventDefault:()=>{}, stopPropagation:()=>{}, nativeEvent:{key:'Tab'}});`
        + `    return {via:'react'}; }catch(e){ return {via:'react-threw', err:String(e).slice(0,60)}; }}`
        + `const r=inp.getBoundingClientRect(); return {via:'none', x:r.left+r.width/2, y:r.top+r.height/2};`);
      if (r && r.missing) return r;
      if (r && (r.via === "react")) { await sleep(700); return r; }
      // Trusted path: click, clear, type the query, and let the search fire off real input.
      const box = r && r.x != null ? r : await evalV(sel(i, kind)
        + `const inp=inputOf(f); if(!inp)return{missing:'input'};`
        + `inp.scrollIntoView({block:'center'}); const b=inp.getBoundingClientRect();`
        + `return {x:b.left+b.width/2, y:b.top+b.height/2};`);
      if (!box || box.missing) return { missing: "input" };
      await clickXY(box.x, box.y); await sleep(150);
      await clearBox(i, kind);              // insertText appends, so empty the box first
      await sleep(80);
      await cmd("Input.insertText", { text: query });
      await sleep(900);
      return { via: "typed" };
    };

    // Some tenants render School as a plain text box ("my school isn't listed"). Nothing to search
    // there — write the name straight in, through the native setter so React registers it.
    const typeText = (i, kind, value) => evalV(sel(i, kind)
      + `const inp=inputOf(f); if(!inp)return{missing:'input'};`
      + `inp.scrollIntoView({block:'center'}); try{inp.focus();}catch(e){}`
      + `const S=Object.getOwnPropertyDescriptor(inp.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype,'value').set;`
      + `S.call(inp,${JSON.stringify(String(value))});`
      + `inp.dispatchEvent(new Event('input',{bubbles:true}));`
      + `inp.dispatchEvent(new Event('change',{bubbles:true}));`
      + `try{inp.blur();}catch(e){} return {ok:true};`);

    const closePopups = () => evalV(
      `for (const p of document.querySelectorAll('[data-automation-widget="wd-popup"]')) p.remove(); return true;`);

    // One field, end to end: try each query, pick the best option Workday offered, click it,
    // confirm the chip. Returns what happened in words, because "it didn't fill" is useless.
    async function fillOne(i, kind, want, label) {
      const before = await readState(i, kind);
      if (!before || before.missing) return { ok: false, why: `no ${label} box on this form`, offered: [] };
      if (before.chip && scoreOption(want, before.chip, false) > 0) {
        return { ok: true, why: `already "${before.chip}"`, offered: [] };
      }
      const queries = queriesFor(want);
      let offered = [], lastQuery = "";
      for (const q of queries) {
        lastQuery = q;
        const s = await search(i, kind, q);
        if (s && s.missing) return { ok: false, why: `no ${label} box on this form`, offered: [] };
        // Tab-search sometimes selects outright when there's exactly one match.
        let st = await readState(i, kind);
        if (st && st.chip && scoreOption(want, st.chip, false) > 0) {
          await closePopups();
          return { ok: true, why: `picked "${st.chip}"`, offered: [], query: q };
        }
        // Otherwise choose from what it offered — strict first, then a relaxed pass.
        for (let wait = 0; wait < 3 && !(st && st.options && st.options.length); wait++) {
          await sleep(350); st = await readState(i, kind);
        }
        const options = (st && st.options) || [];
        if (options.length) offered = options.map((o) => o.text);
        let pick = bestOption(want, options, false);
        const relaxed = !pick && !!(pick = bestOption(want, options, true));
        if (pick) {
          await clickXY(options[pick.index].x, options[pick.index].y);
          await sleep(700);
          const after = await readState(i, kind);
          if (after && after.chip) {
            await closePopups();
            return { ok: true, why: `picked "${after.chip}"`, offered, query: q, loose: relaxed };
          }
        }
        await clearBox(i, kind);
        await closePopups();
        await sleep(200);
      }
      // Nothing in the list matched. If the box is plain text (not a taxonomy), just write it.
      const st = await readState(i, kind);
      if (st && st.searchable === false) {
        await typeText(i, kind, want);
        await sleep(300);
        const after = await readState(i, kind);
        if (after && after.chip) return { ok: true, why: `typed "${after.chip}"`, offered };
      }
      await closePopups();
      return { ok: false, offered, query: lastQuery,
               why: offered.length
                 ? `searched "${lastQuery}" — none of Workday's options matched "${want}"`
                 : `searched "${lastQuery}" — Workday offered nothing` };
    }

    try {
      let cards = await evalV("return eduRoots().length;");
      if (!cards) {
        // No education card yet — click the Add button that belongs to the Education section.
        const b = await evalV(
          `const btns=[...document.querySelectorAll('[data-automation-id="add-button"]')].filter(vis);`
          + `const heads=[...document.querySelectorAll('h1,h2,h3,h4,h5,h6,[role=heading]')].filter(h=>(h.innerText||'').trim());`
          + `const sec=(el)=>{let l='';for(const h of heads){if(el.compareDocumentPosition(h)&Node.DOCUMENT_POSITION_PRECEDING)l=(h.innerText||'').trim();}return l;};`
          + `const E=/education|school|academic/i;`
          + `for(const b of btns){ if(E.test(sec(b))){ b.scrollIntoView({block:'center'});`
          + `  const r=b.getBoundingClientRect(); return {ok:true,x:r.left+r.width/2,y:r.top+r.height/2}; } }`
          + `return {ok:false};`);
        if (b && b.ok) { await clickXY(b.x, b.y); await sleep(1200); cards = await evalV("return eduRoots().length;"); }
      }
      if (!cards) return { found: false, results: [] };

      const n = Math.min(entries.length, cards);
      const results = [];
      for (let i = 0; i < n; i++) {
        const e = entries[i];
        if (e.school) results.push({ i, kind: "school", want: e.school, ...(await fillOne(i, "school", e.school, "school")) });
        if (e.major) results.push({ i, kind: "field", want: e.major, ...(await fillOne(i, "field", e.major, "field of study")) });
      }
      return { found: true, cards, results };
    } finally {
      await rrCdpRelease(tab.id);
    }
  }

  // ============================ panel wiring ============================
  async function educationFromProfile() {
    try {
      const r = await fetch(`${API}/profile`);
      if (!r.ok) return [];
      const p = await r.json();
      return (p.education || []).map((e) => ({ school: e.school || "", major: e.major || "" }))
        .filter((e) => e.school || e.major);
    } catch (e) { return []; }
  }

  // Show exactly what Workday offered when a pick fails — the option list IS the answer to
  // "why didn't it fill", and it's the only place you can see the tenant's own wording.
  function showEduDiag(res) {
    const box = $(eduUi.box); if (!box) return;
    const bad = (res.results || []).filter((r) => !r.ok);
    const loose = (res.results || []).filter((r) => r.ok && r.loose);
    if (!bad.length && !loose.length) { box.innerHTML = ""; return; }
    const lines = [];
    for (const r of bad.concat(loose)) {
      lines.push(`${r.kind === "school" ? "School" : "Field of study"} #${r.i + 1} — wanted "${r.want}": ${r.why}`);
      for (const o of (r.offered || []).slice(0, 12)) lines.push("    offered: " + o);
    }
    box.innerHTML = "";
    box.append(el("div", { class: "card" },
      el("div", { class: "k" }, "🎓 Education fill — what Workday offered"),
      ...lines.map((t) => el("div",
        { style: "font-size:11.5px;margin-top:2px;white-space:pre-wrap" }, t))));
  }

  async function runEdu(btn, from) {
    if (from) eduUi = EDU_UIS[from] || eduUi;
    const tab = await activeTab();
    const host = (() => { try { return new URL(tab && tab.url).hostname; } catch (e) { return ""; } })();
    if (!WD_HOSTS.test(host)) {
      setEduSt(`This isn't a Workday page (${host || "no tab"}). Open the Workday application first.`, "err");
      return;
    }
    const entries = await educationFromProfile();
    if (!entries.length) { setEduSt("No education on your profile — add it at /app first.", "err"); return; }
    // The file picker holds this tab's single debugger session; take it back before filling.
    try {
      const st = await chrome.runtime.sendMessage({ type: "pickerState", tabId: tab.id });
      if (st && st.armed) { await chrome.runtime.sendMessage({ type: "disarmPicker", tabId: tab.id }); await sleep(250); }
    } catch (e) {}
    btn.disabled = true;
    setEduSt("Filling school and field of study… leave this tab in front.");
    try {
      const r = await fillEducationViaDebugger(tab, entries);
      if (r.error) { setEduSt(r.error, "err"); return; }
      if (!r.found) {
        setEduSt("Couldn't find the Education section. Scroll it into view, then retry.", "err");
        return;
      }
      const ok = r.results.filter((x) => x.ok);
      const picked = ok.map((x) => x.why.replace(/^picked |^typed |^already /, "")).join(", ");
      setEduSt(ok.length === r.results.length
        ? `Filled ${ok.length} of ${r.results.length}: ${picked}. Check them before you continue.`
        : `Filled ${ok.length} of ${r.results.length}. See below for what Workday offered.`,
        ok.length === r.results.length ? "ok" : "");
      showEduDiag(r);
    } catch (e) {
      setEduSt("Fill failed: " + e.message, "err");
    } finally { btn.disabled = false; }
  }

  function wire() {
    for (const [from, c] of Object.entries(EDU_UIS)) {
      const b = $(c.btn);
      if (b && !b.dataset.rrWired) {
        b.dataset.rrWired = "1";
        b.addEventListener("click", () => runEdu(b, from));
      }
    }
  }

  document.addEventListener("rr-tab-shown", (e) => {
    if (e.detail.tab === "myinfo" || e.detail.tab === "ask") wire();
  });
  wire();
})();
