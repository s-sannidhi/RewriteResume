// My Info tab — every profile field as a click-to-copy tile. Static/global (not per-job).
// Re-fetched from the backend on EVERY show, so edits made at /app appear immediately.
(() => {

  // ===================================================================================
  //  YOUR JOB-SITE LOGIN PASSWORD — put it between the quotes below.
  //  It is shown only as dots in the panel; the real value is used solely to copy/paste
  //  when YOU click the tile. Leave it "" to hide the tile entirely.
  //  (Note: this file is plain text on your Mac. Don't share the extension folder or
  //   commit this line to git while it's filled in.)
  const LOGIN_PASSWORD = "286945()*&CdC";
  // ===================================================================================

  async function copyText(v) {
    try { await navigator.clipboard.writeText(v); return true; }
    catch (e) {
      const ta = document.createElement("textarea");
      ta.value = v; document.body.append(ta); ta.select();
      const ok = document.execCommand("copy");
      ta.remove(); return ok;
    }
  }

  function tile(label, value) {
    if (!(value || "").toString().trim()) return null;
    const lab = el("span", { class: "t-label" }, label);
    const t = el("button", { class: "tile" }, lab,
      el("span", { class: "t-value" }, String(value)));
    t.addEventListener("click", async () => {
      const v = String(value);
      await copyText(v);                 // always on the clipboard too
      const armed = await armPaste(v);   // next click on a page field pastes it
      t.classList.add("copied");
      lab.textContent = armed ? "✓ copied — now click a field on the page" : "✓ copied";
      setTimeout(() => { t.classList.remove("copied"); lab.textContent = label; }, 2200);
    });
    return t;
  }

  function copyAllBtn(label, text) {
    const b = el("button", { class: "small", style: "margin-top:8px" }, label);
    b.addEventListener("click", async () => {
      await copyText(text);
      const armed = await armPaste(text);
      const old = b.textContent;
      b.textContent = armed ? "✓ copied — click a field to paste" : "✓ copied";
      setTimeout(() => (b.textContent = old), 2200);
    });
    return b;
  }

  function section(title, ...kids) {
    const body = kids.flat().filter(Boolean);
    if (!body.length) return null;
    return el("div", { class: "card" }, el("div", { class: "k" }, title), ...body);
  }

  const withId = (id, node) => { if (node) node.id = id; return node; };

  // Important links beyond LinkedIn/GitHub/portfolio (identity.other_links: strings or {label,url}).
  function otherLinkTiles(links) {
    if (!Array.isArray(links)) return [];
    return links.map((l) => {
      if (typeof l === "string") return tile("Link", l);
      if (l && typeof l === "object") return tile(l.label || l.name || "Link", l.url || l.value || "");
      return null;
    }).filter(Boolean);
  }

  // Sticky chip bar that scrolls to each present section. Positioned just below the app/tab bar.
  function jumpNav(entries) {
    const nav = el("div", { class: "jumpnav" });
    for (const [id, label] of entries) {
      if (!document.getElementById(id)) continue;
      const b = el("button", {}, label);
      b.addEventListener("click", () =>
        document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" }));
      nav.append(b);
    }
    const bar = document.getElementById("appbar");
    if (bar) nav.style.top = bar.offsetHeight + "px";
    return nav;
  }

  // Masked tile for the job-site login password. Always shows dots; on click it copies the
  // real value (from LOGIN_PASSWORD above) and arms the click-to-paste. Hidden if unset.
  function passwordTile() {
    if (!LOGIN_PASSWORD) return null;
    const LABEL = "Login password (job sites)";
    const lab = el("span", { class: "t-label" }, LABEL);
    const t = el("button", { class: "tile" }, lab, el("span", { class: "t-value" }, "••••••••••••"));
    t.addEventListener("click", async () => {
      await copyText(LOGIN_PASSWORD);
      const armed = await armPaste(LOGIN_PASSWORD);
      t.classList.add("copied");
      lab.textContent = armed ? "✓ copied — click the password field" : "✓ copied";
      setTimeout(() => { t.classList.remove("copied"); lab.textContent = LABEL; }, 2600);
    });
    return t;
  }

  const nice = (s) => (s || "").replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());

  function nameParts(full) {
    const toks = (full || "").trim().split(/\s+/).filter(Boolean);
    if (!toks.length) return ["", "", ""];
    if (toks.length === 1) return [toks[0], "", ""];
    return [toks[0], toks.slice(1, -1).join(" "), toks[toks.length - 1]];
  }

  // Canonical phrasings, mirroring the autofill field_map so forms and tiles agree.
  function workAuthTiles(auth) {
    const st = auth.us_work_auth_status;
    const authText = { citizen: "U.S. Citizen", permanent_resident: "Permanent Resident",
      authorized: "Authorized to work in the United States",
      ead: "Authorized to work (EAD)", requires_sponsorship: "Requires sponsorship" }[st] || nice(st);
    return [
      tile("Work authorization", authText),
      tile("Authorized to work in the U.S.?",
        ["citizen", "permanent_resident", "authorized", "ead"].includes(st) ? "Yes" : "No"),
      tile("Need sponsorship?", auth.needs_sponsorship === "yes" ? "Yes" : "No"),
      tile("Veteran status", auth.veteran_status === "not_veteran" ? "I am not a veteran" : nice(auth.veteran_status)),
      tile("Disability status", auth.disability_status === "no" ? "No, I do not have a disability" : nice(auth.disability_status)),
      tile("Race / ethnicity", (auth.race_ethnicity || []).join(", ")),
      tile("Gender", nice(auth.gender)),
      tile("Security clearance", nice(auth.security_clearance)),
    ];
  }

  // Top skills — flattened in resume group order, capped at 15. When a résumé exists for the
  // current tab we use ITS skills (already JD-relevant-first), so Top skills follows the job.
  // Skills are copied VERBATIM — only which ones and their order changes, never the wording.
  const SKILL_ORDER = ["programming_languages", "frameworks", "ml_ai", "ai_tools", "databases",
    "cloud", "tools", "hardware_embedded"];
  function skillsSection(skills, tailored) {
    const groups = skills || {};
    const order = [...SKILL_ORDER, ...Object.keys(groups).filter((k) => !SKILL_ORDER.includes(k))];
    const top = [];
    for (const g of order) for (const s of groups[g] || []) {
      if (s && s.trim() && !top.includes(s.trim())) top.push(s.trim());
    }
    const picked = top.slice(0, 15);
    if (!picked.length) return null;
    const title = tailored ? `Top ${picked.length} skills — tailored for this job`
                           : `Top ${picked.length} skills`;
    return section(title,
      copyAllBtn("Copy all, comma-separated", picked.join(", ")),
      picked.map((s) => tile("skill", s)));
  }

  // Copyable date tiles for a work entry: a combined range plus separate start/end (forms ask
  // for both). "Present" when the role is current or has no end date.
  function dateTiles(w) {
    const start = (w.start_date || "").trim();
    const end = w.current ? "Present" : ((w.end_date || "").trim() || "Present");
    if (!start && end === "Present") return [];
    return [
      tile("Dates", `${start}${start ? " – " : ""}${end}`),
      tile("Start date", start),
      tile("End date", end),
    ].filter(Boolean);
  }

  // Per-work-entry fields as their own copy boxes — the ones application forms ask for.
  function workMeta(w) {
    return [
      tile("Job Title", w.title),
      tile("Company", w.company),
      tile("Location", w.location),
      ...dateTiles(w),
    ].filter(Boolean);
  }

  function bulletBlock(title, entries, nameOf, metaOf, bulletsOf) {
    if (!(entries || []).length) return null;
    const all = [];
    const cards = entries.map((e) => {
      const bullets = ((bulletsOf ? bulletsOf(e) : e.bullets) || []).filter((b) => b.trim());
      const meta = metaOf ? metaOf(e) : [];
      if (!bullets.length && !meta.length) return null;
      all.push(...bullets);
      return el("div", { style: "margin-top:10px" },
        el("div", { class: "k", style: "font-size:12.5px" }, nameOf(e)),
        ...meta,
        bullets.length ? copyAllBtn("Copy all bullets — " + nameOf(e).slice(0, 30), bullets.join("\n")) : "",
        bullets.map((b) => tile("bullet", b)));
    }).filter(Boolean);
    if (!cards.length) return null;
    return section(title, copyAllBtn("Copy ALL " + title.toLowerCase(), all.join("\n")), cards);
  }

  // The résumé generated for the CURRENT browser tab's job, so the copy-paste bullets match what
  // was actually put on that job's résumé (bullets are rewritten per job). Null if this tab has no
  // generated résumé yet — then we fall back to the master profile bullets.
  async function activeResumeContent() {
    let tid = null;
    try {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      const jobs = await getJobs();
      let jid = null;
      try { jid = tab && tab.url ? rrJobId(tab.url) : null; } catch (e) {}
      if (jid && jobs[jid] && jobs[jid].resume_id) tid = jobs[jid].resume_id;
      if (!tid && tab && tab.url) {                       // Workday etc. changes the path → host-match
        let host = ""; try { host = new URL(tab.url).hostname; } catch (e) {}
        const m = Object.values(jobs)
          .filter((j) => j.resume_id && j.host && host && j.host === host)
          .sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0))[0];
        if (m) tid = m.resume_id;
      }
    } catch (e) {}
    if (!tid) return null;
    try {
      const rec = await (await fetch(`${API}/tracker/${tid}`)).json();
      if (rec && rec.content) return { content: rec.content, company: rec.company || "" };
    } catch (e) {}
    return null;
  }

  async function load() {
    const st = $("miStatus"), body = $("miBody");
    st.textContent = "Loading profile…"; st.className = "status";
    let p;
    try {
      const r = await fetch(`${API}/profile`);
      if (!r.ok) throw new Error("HTTP " + r.status);
      p = await r.json();
    } catch (e) {
      st.textContent = "Backend error — is ./run.sh running? (" + e.message + ")";
      st.className = "status err"; return;
    }
    body.innerHTML = "";

    // Per-tab tailored bullets: map each entry id -> the résumé's rewritten bullets for this job.
    const tailored = await activeResumeContent();
    const twork = {}, tproj = {};
    let tskills = null;   // this tab's résumé skills (JD-first), when a résumé exists for it
    if (tailored) {
      for (const w of (tailored.content.work || [])) twork[w.id] = w.bullets || [];
      for (const pr of (tailored.content.projects || [])) tproj[pr.id] = pr.bullets || [];
      const s = tailored.content.skills;
      if (s && Object.values(s).some((arr) => (arr || []).length)) tskills = s;
    }
    if (tailored) {
      st.className = "status ok";
      st.textContent = `Bullets below are tailored for ${tailored.company || "this tab's job"} (from its generated résumé). Switch browser tabs to see another job's.`;
    } else {
      st.className = "status muted";
      st.textContent = "Master profile bullets (no résumé generated for the current tab yet).";
    }
    const id = p.identity || {};
    const [first, middle, last] = nameParts(id.legal_name);
    const [city, state] = (id.location || "").includes(",")
      ? id.location.split(",", 2).map((s) => s.trim()) : [id.location || "", ""];
    const edu0 = (p.education && p.education[0]) || {};

    // Sections ordered by how often they're needed on applications. The first education card gets
    // the stable id "mi-education" so the jump-nav can target it.
    const sections = [
      ["mi-essentials", "Essentials", withId("mi-essentials", section("⭐ Essentials",
        tile("Email", id.email), passwordTile(),
        tile("LinkedIn", id.linkedin), tile("GitHub", id.github), tile("Portfolio", id.portfolio),
        ...otherLinkTiles(id.other_links)))],
      ["mi-address", "Address", withId("mi-address", section("📍 Address",
        tile("Street address", id.street_address),
        tile("City", city), tile("State", state), tile("Zip code", id.zip),
        tile("City, State", id.location),
        tile("City, State ZIP", id.zip ? `${id.location} ${id.zip}` : "")))],
      ["mi-name", "Name", withId("mi-name", section("👤 Name & contact",
        tile("Full name", id.legal_name), tile("First name", first),
        tile("Middle name", middle), tile("Last name", last),
        tile("Preferred name", id.preferred_name || first),
        tile("Phone", id.phone), tile("Pronouns", id.pronouns),
        tile("University", edu0.school)))],
      ["mi-auth", "Auth", withId("mi-auth",
        section("✅ Work authorization & EEO", workAuthTiles(p.work_auth || {})))],
      ["mi-skills", "Skills", withId("mi-skills", skillsSection(tskills || p.skills, !!tskills))],
      ["mi-work", "Work", withId("mi-work", bulletBlock("💼 Work bullets", p.work_experience,
        (w) => `${w.title || ""} — ${w.company || ""}`, workMeta,
        (w) => twork[w.id] || w.bullets))],
      ["mi-projects", "Projects", withId("mi-projects",
        bulletBlock("🧪 Project bullets", p.projects, (pr) => pr.name || "project",
          (pr) => (pr.date_range ? [tile("Dates", pr.date_range)] : []),
          (pr) => tproj[pr.id] || pr.bullets))],
      ["mi-education", "Education", (p.education || []).map((ed, i) => withId(
        i === 0 ? "mi-education" : "mi-education-" + i, section(
          `🎓 Education${p.education.length > 1 ? " " + (i + 1) : ""} — ${ed.school || ""}`,
          tile("School", ed.school), tile("Degree", ed.degree), tile("Major", ed.major),
          tile("Second major", ed.second_major), tile("GPA", ed.gpa),
          tile("Start date", ed.start_date), tile("End / grad date", ed.end_date),
          tile("Location", ed.location),
          tile("Relevant coursework", (ed.coursework || []).join(", ")))))],
      ["mi-answers", "Answers", withId("mi-answers", section("💬 Reusable answers",
        Object.entries(p.reusable_answers || {}).map(([k, v]) => tile(nice(k), v))))],
      ["mi-disclosures", "Disclosures", withId("mi-disclosures", section("📌 Disclosures",
        Object.entries(p.disclosures || {}).map(([k, v]) => tile(nice(k), v))))],
    ];

    const nodes = sections.map(([, , node]) => node).flat().filter(Boolean);
    // Jump-nav built AFTER nodes exist so it only lists sections that actually rendered.
    body.append(...nodes);
    const navEntries = sections
      .map(([anchor, label, node]) => [anchor, label, [].concat(node).filter(Boolean).length])
      .filter(([, , n]) => n).map(([anchor, label]) => [anchor, label]);
    if (navEntries.length > 1) body.prepend(jumpNav(navEntries));
  }

  // Reload when the My Info panel is opened AND whenever the active browser tab changes while it's
  // open — so the tailored bullets always follow the tab you're looking at.
  let miVisible = false;
  let miTimer = null;
  const reloadIfVisible = () => {
    if (!miVisible) return;
    clearTimeout(miTimer);
    miTimer = setTimeout(load, 200);   // debounce rapid tab switches
  };
  document.addEventListener("rr-tab-shown", (e) => {
    miVisible = e.detail.tab === "myinfo";
    if (miVisible) load();
  });
  chrome.tabs.onActivated.addListener(reloadIfVisible);
  chrome.tabs.onUpdated.addListener((_id, info, tab) => {
    if (tab.active && (info.status === "complete" || info.url)) reloadIfVisible();
  });
  // Refresh the moment a résumé finishes generating (background writes rr_jobs with the new
  // resume_id) — so Top skills + tailored bullets update as soon as the docs are made.
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && "rr_jobs" in changes) reloadIfVisible();
  });
})();
