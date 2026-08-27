// Which document does an upload area want, and will uploading there actually do anything?
//
// Loaded by BOTH the side panel (<script>) and the service worker (importScripts) so the manual
// "Attach files" button and the automatic on-page-load attach can never disagree about what a
// field is.
//
// Two independent questions per upload area:
//
//   1. KIND  — resume / cover letter / transcript, matched on the words around the field.
//   2. MODE  — does this upload feed the application, or just sit there?
//
// (2) matters because plenty of ATS pages show two resume uploads: one that parses the file and
// fills the form ("Autofill with Resume", "Apply with resume"), and one that is a plain attachment
// slot further down. Dropping the resume into the wrong one silently does nothing useful, so the
// autofill-capable area always wins when both are present.
(() => {
  // ---- KIND ---------------------------------------------------------------------------------
  // Rule ORDER does not decide the winner — position in the text does (see classifyUploadArea).
  // These are just the word lists.
  const DOC_RULES = [
    {
      kind: "transcript",
      words: ["transcript", "unofficial transcript", "official transcript", "academic record",
              "academic history", "grade report", "grades report", "marksheet"],
    },
    {
      kind: "cover",
      words: ["cover letter", "coverletter", "cover_letter", "cover-letter", "covering letter",
              "letter of interest", "letter of introduction", "letter of motivation",
              "motivation letter"],
    },
    {
      kind: "resume",
      words: ["resume", "résumé", "resumé", "cv", "curriculum vitae", "resume/cv", "cv/resume",
              "resume or cv", "upload resume", "attach resume", "your resume"],
      // "cv" is short enough to appear inside unrelated words, so it is matched whole-word only
      // (see `hasWord`), and these kill a false positive outright.
      // "cover letter" is NOT an exclusion here: a combined "Resume or Cover Letter" box is a
      // resume box, and position (above) settles which label is closer.
      not: ["portfolio", "writing sample"],
    },
  ];

  // ---- MODE ---------------------------------------------------------------------------------
  // Words that mean "this upload populates the form".
  const AUTOFILL_SIGNALS = [
    "autofill", "auto-fill", "auto fill", "autofill with resume", "autofill from resume",
    "fill out the application", "fill in the application", "fill the application",
    "fill your application", "we'll fill", "we will fill", "populate", "prefill", "pre-fill",
    "parse", "parsing", "extract your", "apply with resume", "apply with your resume",
    "quick apply", "quickapply", "use my resume", "upload to autofill", "save time",
    "automatically fill",
  ];
  // Words that mean "this is just an attachment slot".
  const INERT_SIGNALS = [
    "attachment", "attachments", "additional document", "additional documents",
    "supporting document", "supporting documents", "other document", "other documents",
    "supplemental", "for our records", "will not be parsed", "optional upload",
  ];

  const norm = (s) => (s || "").toLowerCase().replace(/[’']/g, "'").replace(/\s+/g, " ").trim();

  // Whole-word containment. Substring matching turns "cv" into a match inside "cvs", "recv" and
  // any base64-looking id, which is how an unrelated field ends up holding your resume.
  function hasWord(hay, needle) {
    if (!hay || !needle) return false;
    const esc = needle.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return new RegExp(`(^|[^a-z0-9])${esc}($|[^a-z0-9])`, "i").test(hay);
  }

  const anyWord = (hay, words) => words.some((w) => hasWord(hay, w));

  /** Index of the earliest whole-word hit from `words`, or -1. */
  function firstWordIndex(hay, words) {
    let best = -1;
    for (const w of words) {
      const esc = w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      const m = new RegExp(`(^|[^a-z0-9])(${esc})($|[^a-z0-9])`, "i").exec(hay);
      if (m) {
        const at = m.index + m[1].length;
        if (best < 0 || at < best) best = at;
      }
    }
    return best;
  }

  /**
   * {kind, mode} for one upload area.
   *
   * `nearText` is what sits around the field; `pageText` is the page's own copy, used ONLY as a
   * last resort for KIND (some pages, e.g. Workday's autofill step, identify the field nowhere
   * except the instructions). MODE is judged on nearText alone: a page-level banner reading
   * "Autofill from resume" otherwise marks every upload on the page as an autofill box, including
   * the real required one.
   */
  function classifyUploadArea(nearText, pageText) {
    const hay = norm(nearText) || (pageText ? norm(pageText) : "");
    // Whichever document word comes FIRST wins, not whichever rule is listed first. A box labelled
    // "Upload your Resume or Cover Letter" is a resume box, while one labelled "Cover Letter" is
    // not a resume box just because the section heading above it says "Resume". Since the caller
    // puts the field's own label at the front of this text, the nearest label decides.
    let kind = null, at = -1;
    for (const rule of DOC_RULES) {
      const i = firstWordIndex(hay, rule.words);
      if (i < 0) continue;
      // Exclusions are POSITIONAL too. Vetoing whenever the word appears anywhere killed real
      // fields: on a live Lever form the gathered text has "resume" at index 0 and "portfolio" at
      // index 1342 (a different field further down the page), and the blunt check threw the whole
      // area away. A veto only counts if it sits CLOSER to the field than the match does.
      if (rule.not) {
        const veto = firstWordIndex(hay, rule.not);
        if (veto >= 0 && veto < i) continue;
      }
      if (at < 0 || i < at) { at = i; kind = rule.kind; }
    }
    // Fall back to the page's own text only to identify WHAT the field is, never how it behaves.
    if (kind === null && pageText) {
      const pt = norm(pageText);
      for (const rule of DOC_RULES) {
        const i = firstWordIndex(pt, rule.words);
        if (i < 0) continue;
        if (rule.not) {
          const veto = firstWordIndex(pt, rule.not);
          if (veto >= 0 && veto < i) continue;
        }
        if (at < 0 || i < at) { at = i; kind = rule.kind; }
      }
    }

    const near = norm(nearText);
    let mode = "unknown";
    if (anyWord(near, AUTOFILL_SIGNALS)) mode = "autofill";
    else if (anyWord(near, INERT_SIGNALS)) mode = "inert";
    return { kind, mode };
  }

  /**
   * Every area that should receive this document, best first.
   *
   * The distinction that matters is "does this upload do anything", not "is it the autofill one".
   * A page can have an optional autofill helper AND the real required résumé field (Ashby does
   * exactly this: an "Autofill from resume" drop zone plus `_systemfield_resume`). Filling only
   * the helper would leave the required field empty and the application incomplete.
   *
   * So: fill every non-inert area of that kind. An `inert` area — "Attachments", "additional
   * documents", "for our records" — is used ONLY when there is no real field anywhere, because
   * the alternative is submitting with nothing attached.
   */
  function chooseUploadTargets(areas, kind) {
    const all = (areas || []).map((a, i) => ({ ...a, _i: i })).filter((a) => a.kind === kind);
    if (!all.length) return { targets: [], skipped: [] };
    const rank = { autofill: 0, unknown: 1, inert: 2 };
    const real = all.filter((a) => a.mode !== "inert");
    const pool = real.length ? real : all;            // no real field: the attachment slot will do
    const skipped = all.filter((a) => !pool.includes(a))
                       .map((a) => ({ index: a.index, mode: a.mode }));
    const targets = pool.filter((a) => !a.hasFile)
                        .sort((a, b) => (rank[a.mode] - rank[b.mode]) || (a._i - b._i));
    return { targets, skipped, anyAlreadyFilled: pool.some((a) => a.hasFile) };
  }

  /** Backwards-compatible single pick (the Docs-tab chooser still wants one). */
  function chooseUploadTarget(areas, kind) {
    const { targets, skipped } = chooseUploadTargets(areas, kind);
    return targets.length ? { ...targets[0], skipped } : null;
  }

  // Runs IN THE PAGE. Finds every file input, INCLUDING ones inside shadow roots, stamps each with
  // data-rr-upload="<i>" so it can be resolved again later, and collects the text around it.
  //
  // Two things this has to get right, both learned on live pages:
  //   • Shadow DOM. document.querySelectorAll('input[type=file]') stops at a shadow boundary, so on
  //     component-based ATS the input is invisible to it and the page looks like it has no upload
  //     field at all.
  //   • Long pages. Truncating each wrapper's text to 200 chars and then keeping it only if it was
  //     UNDER 200 discarded every scrap of context on text-heavy pages, so every field came back
  //     unrecognised. Text is truncated, never dropped.
  function rrProbeUploadAreas() {
    const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
    const cut = (s, n) => clean(s).slice(0, n);

    const inputs = [];
    (function walk(root, depth) {
      if (!root || depth > 12) return;
      let els = [];
      try { els = [...root.querySelectorAll("*")]; } catch (e) { return; }
      for (const el of els) {
        if (el.tagName === "INPUT" && (el.type || "").toLowerCase() === "file") inputs.push(el);
        if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
      }
    })(document, 0);

    const pageText = cut(document.body && document.body.innerText, 1200);

    return inputs.map((el, index) => {
      el.setAttribute("data-rr-upload", String(index));
      const parts = [];
      const push = (v, n = 220) => { const t = cut(v, n); if (t) parts.push(t); };

      // Nearest identifiers first: classifyUploadArea breaks ties by POSITION, so whatever is
      // closest to the field has to come first in this string.
      if (el.labels) [...el.labels].forEach((l) => push(l.textContent));
      const lb = el.getAttribute("aria-labelledby");
      if (lb) lb.split(/\s+/).forEach((id) => push((document.getElementById(id) || {}).textContent));
      push(el.getAttribute("aria-label"));
      push(el.name); push(el.id);
      push(el.getAttribute("data-automation-id") || el.getAttribute("data-qa") || "");

      let p = el.parentElement || (el.getRootNode() && el.getRootNode().host) || null;
      let hop = 0;
      while (p && hop < 5) {
        push(p.textContent, 320);
        push(String(p.className || ""));
        p = p.parentElement || (p.getRootNode && p.getRootNode().host) || null;
        hop++;
      }

      try {
        const heads = [...document.querySelectorAll("h1,h2,h3,h4,h5,h6,legend,label,strong,b,[role=heading]")];
        let best = null;
        for (const h of heads) {
          if (h.contains(el)) continue;
          if (!(h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)) continue;
          best = h;
        }
        if (best) push(best.textContent);
      } catch (e) {}

      const r = el.getBoundingClientRect();
      return {
        index,
        text: parts.filter(Boolean).join(" | "),
        // SEPARATE from `text`, never appended. Used only to identify what an otherwise-anonymous
        // field is; folding it in made a page-level "Autofill from resume" banner mark every
        // upload on the page as an autofill box, including the real required one.
        pageText,
        hasFile: !!(el.files && el.files.length),
        inShadow: el.getRootNode() !== document,
        hidden: r.width < 2 && r.height < 2,
      };
    });
  }

  // Resolve a stamped input again, piercing shadow roots the same way the probe did, so the node we
  // set the file on is provably the node we classified.
  function rrDeepFindExpr(index) {
    return `(() => {
      const want = '${index}';
      const walk = (root, depth) => {
        if (!root || depth > 12) return null;
        let els = [];
        try { els = [...root.querySelectorAll('*')]; } catch (e) { return null; }
        for (const el of els) {
          if (el.tagName === 'INPUT' && el.getAttribute && el.getAttribute('data-rr-upload') === want) return el;
          if (el.shadowRoot) { const r = walk(el.shadowRoot, depth + 1); if (r) return r; }
        }
        return null;
      };
      return walk(document, 0);
    })()`;
  }

  const api = { DOC_RULES, AUTOFILL_SIGNALS, INERT_SIGNALS,
                classifyUploadArea, chooseUploadTarget, chooseUploadTargets, hasWord,
                rrProbeUploadAreas, rrDeepFindExpr };
  if (typeof window !== "undefined") Object.assign(window, api);
  else Object.assign(self, api);      // service worker
})();
