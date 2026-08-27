// JD auto-reading — page-context functions (injected via chrome.scripting). No chrome.* here,
// so they're testable standalone.
//   rrFindJD()          -> {found, text, score, selector, title, url}
//   rrFindJDCandidates()-> [{text, tag}]   (clickables likely to REVEAL the JD when not found)
//   rrJobId(url)        -> stable id for a posting (survives reload / volatile query params)

function rrFindJD() {
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  // Sections a real JD almost always contains — strong signal vs. nav/marketing text.
  const KW = /responsibilit|qualificat|requirement|what you'?ll do|about (the|this) role|the role|your impact|who you are|what we'?re looking for|what you'?ll bring|what you bring|preferred|nice to have|minimum|overview|day[- ]to[- ]day|job description|role description|position summary|you will|we are looking|about the (job|position)/i;
  const KWG = new RegExp(KW.source, "gi");        // global copy for COUNTING matches

  const score = (t) => {
    if (!t || t.length < 150) return 0;
    const kw = (t.match(KWG) || []).length;       // keyword density (semantic containers win)
    return Math.min(t.length, 6000) + kw * 500;
  };

  const sels = ["[class*=job-description]", "[class*=jobDescription]", "[data-testid*=escription]",
    "[class*=description]", "[class*=posting]", "main", "article", "[role=main]", "#content", ".content"];
  let best = { text: "", score: 0, selector: "" };
  for (const s of sels) {
    document.querySelectorAll(s).forEach((n) => {
      const t = clean(n.innerText);
      const sc = score(t);
      if (sc > best.score) best = { text: t.slice(0, 20000), score: sc, selector: s };
    });
  }
  // Always weigh the whole page body too, so a full-page JD isn't lost to a smaller container.
  const body = clean(document.body ? document.body.innerText : "");
  const bs = score(body);
  if (bs > best.score) best = { text: body.slice(0, 20000), score: bs, selector: "body" };

  // "found" = reads like a description (JD keywords + some length) OR is clearly long-form.
  // Threshold lowered from 1500 so shorter / differently-worded JDs the manual reader catches
  // also pass in the batch; a real posting is still well clear of nav/login shells.
  const found = (KW.test(best.text) && best.text.length > 200) || best.text.length > 900;
  return { found, text: best.text, score: best.score, selector: best.selector,
    title: document.title, url: location.href };
}

function rrFindJDCandidates() {
  // EXPAND = in-place toggles that reveal MORE of the JD on the same page (safe to click even
  // if we already have some text). REVEAL = tabs/links that navigate to the description.
  const EXPAND = /show\s+more|see\s+more|read\s+more|view\s+full(\s+description)?|show\s+full|\.\.\.\s*more|^more$|^\.\.\.$|expand/i;
  const REVEAL = /job\s*description|^description$|^overview$|^details$|job overview|role overview|view (the\s+)?(job|posting|details|description|overview)|see (the\s+)?(job\s+)?(description|details|overview)/i;
  const out = [];
  const seen = new Set();
  document.querySelectorAll("a, button, [role=button], [role=tab], [aria-expanded], summary, span[class*=more], span[class*=expand]").forEach((el) => {
    const t = (el.innerText || el.getAttribute("aria-label") || "").trim();
    if (!t || t.length > 40) return;
    const kind = EXPAND.test(t) ? "expand" : REVEAL.test(t) ? "reveal" : null;
    if (!kind) return;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    const key = kind + ":" + t.toLowerCase();
    if (seen.has(key)) return;
    seen.add(key);
    el.setAttribute("data-rr-jdlink", out.length);
    out.push({ text: t.slice(0, 40), tag: el.tagName.toLowerCase(), kind, idx: out.length });
  });
  // expanders first (lower risk, click eagerly), then reveals
  out.sort((a, b) => (a.kind === "expand" ? 0 : 1) - (b.kind === "expand" ? 0 : 1));
  return out.slice(0, 10);
}

function rrClickJDCandidate(idx) {
  const el = document.querySelector(`[data-rr-jdlink="${idx}"]`);
  if (!el) return false;
  el.scrollIntoView({ block: "center" });
  el.click();
  return true;
}

function rrJobId(urlStr) {
  try {
    const u = new URL(urlStr);
    const host = u.hostname.replace(/^www\./, "");
    const keep = ["gh_jid", "jobid", "id", "posting", "postingid", "req", "reqid", "jobpostingid"];
    const qp = [];
    for (const [k, v] of u.searchParams) {
      if (keep.includes(k.toLowerCase()) && v) qp.push(k.toLowerCase() + "=" + v);
    }
    const path = u.pathname.replace(/\/+$/, "");
    return host + path + (qp.length ? "?" + qp.sort().join("&") : "");
  } catch (e) {
    return urlStr || "";
  }
}
