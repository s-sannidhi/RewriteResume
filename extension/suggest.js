// Inline field suggestions (not full autofill). When you focus/type in a form field, a small
// chip pops up under it with the matching value from your profile (name, email, phone, links,
// address, school…). Click it to fill that one field. Runs on every page; only ever shows for
// fields it can confidently classify, and never for passwords. Nothing is filled without a click.
//
// Fields that can only offer one thing get a one-line chip. Fields that could take several — the
// repeating "Website 1 / Website 2 / Website 3" group on Workday being the case that needs it —
// get a ranked list instead, recommended entry first, the rest one keypress away. That's the
// Chrome-autofill shape: a recommendation you can override, never a decision made for you.
(() => {
  const LOG = (...a) => console.debug("[rr-suggest]", ...a);
  LOG("content script active on", location.href);
  let SUGG = null;          // flat {kind: value} built from the profile
  let box = null;           // the floating chip element
  let currentInput = null;
  let items = [];           // the candidates currently on offer, best first
  let sel = -1;             // arrow-key highlight; -1 = nothing highlighted yet (Chrome's rule:
                            // Enter only fills once you've actually arrowed onto a row)

  async function ensureProfile() {
    if (SUGG) return SUGG;
    try {
      const r = await chrome.storage.local.get("rr_profile");
      if (r && r.rr_profile && r.rr_profile.identity) {
        SUGG = build(r.rr_profile);
        LOG("profile loaded from storage, suggestions ready");
        return SUGG;
      }
      // not cached yet → ask the background to fetch it. During a long batch the SW can be
      // busy; poll storage briefly so the first focus of a session still gets a chip.
      LOG("profile not in storage yet — requested a refresh");
      chrome.runtime.sendMessage({ type: "refreshProfile" }).catch(() => {});
      for (let i = 0; i < 8 && !SUGG; i++) {
        await new Promise((res) => setTimeout(res, 200));
        const again = await chrome.storage.local.get("rr_profile");
        if (again && again.rr_profile && again.rr_profile.identity) {
          SUGG = build(again.rr_profile);
          LOG("profile arrived after refresh wait");
          return SUGG;
        }
      }
      return SUGG;
    } catch (e) { LOG("storage read failed:", e && e.message); return null; }
  }

  // pick up the profile as soon as the background writes it — and immediately offer for whatever
  // is focused RIGHT NOW. Without this, focusing a field before the profile finished loading meant
  // no chip at all until you clicked away and back, which is the main way this used to "glitch".
  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "local" && changes.rr_profile && changes.rr_profile.newValue
        && changes.rr_profile.newValue.identity) {
      SUGG = build(changes.rr_profile.newValue);
      LOG("profile updated from storage");
      const a = activeField();
      if (a) showFor(a);
    }
  });

  function build(p) {
    const id = p.identity || {};
    const toks = (id.legal_name || "").trim().split(/\s+/).filter(Boolean);
    const first = toks[0] || "", last = toks.length > 1 ? toks[toks.length - 1] : "",
      middle = toks.slice(1, -1).join(" ");
    const [city, state] = (id.location || "").includes(",")
      ? id.location.split(",", 2).map((s) => s.trim()) : [id.location || "", ""];
    const ed = (p.education || [])[0] || {};
    return {
      full_name: id.legal_name, first_name: first, last_name: last, middle_name: middle,
      preferred_name: id.preferred_name || first,
      email: id.email, phone: id.phone,
      street: id.street_address, city, state, zip: id.zip, location: id.location,
      linkedin: id.linkedin, github: id.github, portfolio: id.portfolio,
      school: ed.school, degree: ed.degree, major: ed.major, gpa: ed.gpa,
      links: linksOf(id),
    };
  }

  // Your links in the order a form should get them: LinkedIn, GitHub, personal site, then
  // anything else on the profile. This order IS the recommendation for a numbered website group.
  function linksOf(id) {
    const out = [];
    const add = (label, url) => {
      const v = String(url || "").trim();
      if (v && !out.some((o) => o.value === v)) out.push({ label, value: v });
    };
    add("LinkedIn", id.linkedin);
    add("GitHub", id.github);
    add("Personal website", id.portfolio);
    for (const l of id.other_links || []) {
      if (typeof l === "string") add("Link", l);
      else if (l && typeof l === "object") add(l.label || l.name || "Link", l.url || l.value || "");
    }
    return out;
  }

  // ordered — specific rules first, generic "name" last so "first name" doesn't hit "name"
  const RULES = [
    [/e-?mail/, "email"],
    [/first\s*name|given\s*name|\bfname\b/, "first_name"],
    [/last\s*name|surname|family\s*name|\blname\b/, "last_name"],
    [/middle\s*name|middle\s*initial/, "middle_name"],
    [/preferred\s*name|nick\s*name|goes\s*by/, "preferred_name"],
    [/phone|mobile|\btel\b|\bcell\b/, "phone"],
    [/linkedin/, "linkedin"],
    [/github/, "github"],
    [/portfolio|personal\s*(web)?site|website|personal\s*url/, "portfolio"],
    [/street|address\s*line\s*1|address1|\baddr\b|mailing\s*address/, "street"],
    [/\bcity\b|town/, "city"],
    [/\bstate\b|province|region/, "state"],
    [/\bzip\b|postal\s*code|postcode/, "zip"],
    [/school|university|college|institution|alma\s*mater/, "school"],
    [/\bdegree\b/, "degree"],
    [/major|field\s*of\s*study|concentration/, "major"],
    [/\bgpa\b|grade\s*point/, "gpa"],
    [/full\s*name|legal\s*name|your\s*name|\bname\b/, "full_name"],
  ];

  function contextOf(el) {
    let t = "";
    if (el.labels && el.labels.length) t += [...el.labels].map((l) => l.textContent).join(" ");
    const lb = el.getAttribute("aria-labelledby");
    if (lb) lb.split(/\s+/).forEach((id) => { const e = document.getElementById(id); if (e) t += " " + e.textContent; });
    t += " " + (el.getAttribute("aria-label") || "") + " " + (el.placeholder || "")
       + " " + (el.name || "") + " " + (el.id || "") + " " + (el.getAttribute("autocomplete") || "");

    // The visible label. The class-name lookup works on ordinary forms; Workday's class names are
    // hashed, so it finds nothing there and the ancestor walk below is what actually answers.
    // The walk stops at the first ancestor owning exactly ONE input — any higher and the "label"
    // would belong to the section, or to the field next door.
    const wrap = el.closest("label,[class*=field],[class*=form-group],[class*=input]");
    let lab = wrap ? wrap.querySelector("label,legend") : null;
    if (!lab || lab.contains(el)) {
      for (let node = el.parentElement, d = 0; node && d < 5; node = node.parentElement, d++) {
        if (node.querySelectorAll("input,textarea,select").length > 1) break;
        const l = node.querySelector("label,legend");
        if (l && !l.contains(el)) { lab = l; break; }
      }
    }
    if (lab && !lab.contains(el)) t += " " + lab.textContent;

    // Workday names the field on a WRAPPER (data-automation-id="formField-website"), not on the
    // input — and it nests them, so the NEAREST one is a generic "textInputBox" and closest()
    // alone would miss the name entirely. Collect every automation-id on the way up instead.
    // Kept un-split on purpose: turning "formField-companyName" into "company name" would make it
    // match the generic \bname\b rule and start offering your name for the Company box.
    for (let node = el, d = 0; node && node !== document.body && d < 8; node = node.parentElement, d++) {
      const a = node.getAttribute && node.getAttribute("data-automation-id");
      if (a) t += " " + a;
    }
    return t.toLowerCase();
  }

  function classify(el) {
    const ctx = contextOf(el);
    for (const [re, kind] of RULES) if (re.test(ctx)) return kind;
    return null;
  }

  function fillable(el) {
    if (!el || el.disabled || el.readOnly) return false;
    if (el.tagName !== "INPUT" && el.tagName !== "TEXTAREA") return false;
    const t = (el.type || "text").toLowerCase();
    return el.tagName === "TEXTAREA" || ["text", "email", "tel", "url", "search", ""].includes(t);
  }

  // Committing a value to a React-controlled field, in three escalating steps. The website boxes
  // on Workday are why this isn't a one-liner:
  //
  //   1. The NATIVE prototype setter, not `el.value = v`. React keeps a private value tracker on
  //      the element; assigning through React's own accessor updates the tracker too, so React
  //      compares new-vs-tracked, sees no change, and drops the event.
  //   2. input + change, which is all an ordinary text field needs.
  //   3. A REAL blur. Fields with client-side validation — a URL box being exactly that — commit
  //      on React's onBlur, not onChange. Without this the link appears in the box and then
  //      silently reverts the moment you click away, which is what "click to fill doesn't work"
  //      looked like. focus() then blur() produces genuine blur/focusout events, so React's
  //      delegated listener runs no matter which version it is — and the value is set FIRST, so
  //      the blur handler reads the new value rather than committing an empty string over it.
  function setValue(el, v) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, v);
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    try { el.focus(); el.blur(); } catch (e) {}
    el.dispatchEvent(new Event("focusout", { bubbles: true }));
  }

  // Did it actually stick? React reverts a rejected value on its next render, so the answer is
  // only trustworthy a frame or two later.
  //
  // "Stuck" deliberately means the box CHANGED, not that it matches exactly — plenty of fields
  // reformat what you give them (a phone box adding dashes or parentheses). Demanding
  // an exact match would read that as a failure and the fallback would overwrite the page's own
  // formatting. Only an empty box, or one that snapped back to what it held before, is a failure.
  function stuck(el, before) {
    return new Promise((res) => setTimeout(() => {
      const now = (el.value || "").trim();
      res(!!now && now !== before);
    }, 140));
  }

  // Last resort for tenants that only commit through their own React handlers. A content script
  // lives in an isolated world and cannot see the page's __reactProps, so the background is asked
  // to run the fill in the MAIN world; the element is marked with an attribute because a DOM node
  // can't be passed across worlds.
  async function reactFill(el, v) {
    try {
      el.setAttribute("data-rr-fill", "1");
      const r = await chrome.runtime.sendMessage({ type: "reactFill", value: String(v) });
      return !!(r && r.ok);
    } catch (e) { LOG("react fill unavailable:", e && e.message); return false; }
    finally { setTimeout(() => el.removeAttribute("data-rr-fill"), 1500); }
  }

  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  // ===================== the numbered "Websites" group =====================
  // Workday's Website 1 / Website 2 / Website 3 all carry the SAME label, so the label can't say
  // which link belongs in which box — the box's POSITION in the group is the only signal. Same
  // pattern shows up on other ATSes, so this is matched structurally rather than by tenant.
  const WEBSITE_RE = /website|web\s*site|web\s*address|personal\s*url|\burl\b|portfolio/;

  const normUrl = (u) => String(u || "").trim().toLowerCase()
    .replace(/^https?:\/\//, "").replace(/^www\./, "").replace(/\/+$/, "");

  function isWebsiteField(el) {
    if (!fillable(el)) return false;
    const ctx = contextOf(el);
    // A box that names LinkedIn or GitHub outright isn't part of the generic group — it already
    // knows what it wants, and classify() answers it correctly.
    if (/linkedin|github/.test(ctx)) return false;
    if ((el.type || "").toLowerCase() === "url") return true;
    return WEBSITE_RE.test(ctx);
  }

  const websiteFields = () => [...document.querySelectorAll("input,textarea")].filter(isWebsiteField);

  // Which link to recommend for THIS box. Links are handed out in order — LinkedIn, GitHub,
  // personal site — but any link already sitting in a sibling box is taken off the table first.
  // So filling them out of order, or having typed one in by hand, still leaves every remaining
  // box offering something none of its siblings has.
  function websiteCandidates(el) {
    const links = (SUGG && SUGG.links) || [];
    if (!links.length) return [];
    const group = websiteFields();
    const idx = group.indexOf(el);
    if (idx < 0) return links.slice();
    const taken = new Set();
    let position = 0;                       // how many still-empty boxes come before this one
    group.forEach((f, i) => {
      if (i === idx) return;
      const v = normUrl(f.value);
      if (v) taken.add(v);
      else if (i < idx) position++;
    });
    const left = links.filter((l) => !taken.has(normUrl(l.value)));
    if (!left.length) return [];
    // The recommendation is whichever link's turn it is; the rest stay one keypress away.
    const pick = Math.min(position, left.length - 1);
    return [left[pick], ...left.filter((_, i) => i !== pick)];
  }

  // Everything on offer for a field, best first. One entry for ordinary fields, several for a
  // website box — that difference is the whole reason the popup can be a list.
  const KIND_LABELS = {
    full_name: "Full name", first_name: "First name", last_name: "Last name",
    middle_name: "Middle name", preferred_name: "Preferred name", email: "Email", phone: "Phone",
    street: "Street", city: "City", state: "State", zip: "ZIP", location: "Location",
    linkedin: "LinkedIn", github: "GitHub", portfolio: "Personal website",
    school: "School", degree: "Degree", major: "Major", gpa: "GPA",
  };
  function candidatesFor(el) {
    if (isWebsiteField(el)) return websiteCandidates(el);
    const kind = classify(el);
    const val = kind && SUGG[kind];
    if (!val || !String(val).trim()) return [];
    return [{ label: KIND_LABELS[kind] || kind, value: String(val) }];
  }

  // The focused field, seeing THROUGH shadow roots. document.activeElement stops at the shadow
  // host, so a field inside a web component looks like "nothing is focused" without this walk —
  // and events from one arrive retargeted, which is why the listeners read composedPath instead.
  function activeField() {
    let a = document.activeElement;
    for (let d = 0; a && a.shadowRoot && a.shadowRoot.activeElement && d < 6; d++) {
      a = a.shadowRoot.activeElement;
    }
    return fillable(a) ? a : null;
  }
  const evTarget = (e) => (e.composedPath && e.composedPath()[0]) || e.target;

  // A field the user explicitly dismissed with Escape. Held so the watchdog doesn't put the
  // popup straight back. Cleared on focus change or typing. IMPORTANT: a plain click outside
  // must NOT permanently dismiss — on Workday the visible "box" is a DIV wrapper, so a click
  // meant for the field lands on non-input chrome while the real <input> stays focused. Treating
  // that as a dismiss left the chip gone until blur+refocus, which looked like "suggestions
  // disappeared." Escape is the intentional dismiss; outside clicks only hide for now.
  let dismissed = null;

  function hide() {
    if (box) { box.remove(); box = null; }
    currentInput = null; items = []; sel = -1;
  }
  function dismiss() {
    dismissed = activeField();
    hide();
  }
  // The visual field chrome around an input (Workday wraps the real <input> in nested divs).
  // Clicks inside this region are "still on the field", not "click away".
  function fieldChrome(el) {
    if (!el || !el.closest) return null;
    return el.closest(
      "label,[data-automation-id*=formField],[data-automation-id*=FormField],"
      + "[class*=field],[class*=form-group],[class*=input],[role=group]"
    ) || el.parentElement;
  }

  function showFor(el) {
    if (!SUGG || !fillable(el)) return;
    if (el === dismissed) return;      // one choke point for the dismissal, so every caller obeys it
    let cands = candidatesFor(el);
    LOG("field focused →", cands.length ? cands.map((c) => c.label).join(" / ") : "(unrecognized)");
    const cur = (el.value || "").trim();
    if (cur) {
      // already holds one of them → nothing left to offer
      if (cands.some((c) => normUrl(c.value) === normUrl(cur) || c.value.toLowerCase() === cur.toLowerCase())) return hide();
      // narrow to what they're typing toward; a URL is matched with the protocol stripped, since
      // nobody types "https://" first. Nothing left = they meant something else, so stop nagging.
      const low = cur.toLowerCase();
      cands = cands.filter((c) => c.value.toLowerCase().startsWith(low) || normUrl(c.value).startsWith(normUrl(cur)));
    }
    if (!cands.length) return hide();
    render(el, cands);
  }

  const sig = (cands) => cands.map((c) => c.value).join("\u0000");

  async function accept(el, i) {
    const c = items[i];
    if (!c) return;
    const before = (el.value || "").trim();
    setValue(el, c.value);
    hide();
    if (await stuck(el, before)) return;
    LOG("the page rejected the value — retrying through its own React handlers");
    const ok = await reactFill(el, c.value);
    LOG(ok ? "react fill committed it" : "react fill failed too; the field may need typing by hand");
  }

  function paint() {
    if (!box) return;
    [...box.querySelectorAll("[data-rr-row]")].forEach((row, i) => {
      row.style.background = i === sel ? "#2b3b57" : "transparent";
    });
  }

  function render(el, cands) {
    // Same field, same offers, AND still in the document → just keep it where it belongs.
    // Re-rendering on every keystroke is what made the old chip flicker. The isConnected check is
    // what makes it recoverable: without it, a page that rips the popup out of the DOM leaves this
    // fast path happily "keeping" a detached node, and it never comes back.
    if (box && box.isConnected && currentInput === el && sig(items) === sig(cands)) {
      place(el); return;
    }
    hide();
    currentInput = el;
    items = cands;
    sel = -1;
    box = document.createElement("div");
    box.setAttribute("data-rr-suggest", "1");
    Object.assign(box.style, {
      position: "fixed", zIndex: 2147483647, background: "#1b1f26", color: "#e7eaef",
      border: "1px solid #4f86f7", borderRadius: "8px", padding: "4px",
      font: "13px/1.3 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif",
      boxShadow: "0 6px 22px rgba(0,0,0,.35)", maxWidth: "360px", userSelect: "none",
    });
    const multi = items.length > 1;
    box.innerHTML = items.map((c, i) => `
      <div data-rr-row="${i}" style="display:flex;align-items:baseline;gap:8px;padding:5px 8px;
           border-radius:5px;cursor:pointer;white-space:nowrap">
        <span style="opacity:.55;font-size:11px;flex:0 0 auto">${esc(c.label)}</span>
        <b style="flex:1 1 auto;overflow:hidden;text-overflow:ellipsis">${esc(c.value)}</b>
        ${multi && i === 0 ? '<span style="flex:0 0 auto;font-size:10px;opacity:.75;background:#4f86f7;'
          + 'color:#fff;border-radius:99px;padding:1px 6px">recommended</span>' : ""}
      </div>`).join("");
    if (multi) {
      box.insertAdjacentHTML("beforeend",
        '<div style="opacity:.4;font-size:10px;padding:3px 8px 1px">↑↓ to choose · Enter to fill</div>');
    }
    for (const row of box.querySelectorAll("[data-rr-row]")) {
      const i = +row.getAttribute("data-rr-row");
      row.addEventListener("mouseenter", () => { sel = i; paint(); });
      row.addEventListener("mousedown", (e) => { e.preventDefault(); accept(el, i); });
    }
    hostFor(el).appendChild(box);
    place(el);
  }

  // Where to attach the popup. Normally the body — but an open <dialog> (and a popover) is painted
  // in the browser's TOP LAYER, which sits above every z-index there is. A popup on the body would
  // be rendered *underneath* the modal: present in the DOM, invisible on screen. Attaching it
  // inside the top-layer element is the only way to be seen there.
  function hostFor(el) {
    try {
      const dlg = el.closest("dialog[open]");
      if (dlg) return dlg;
      for (let n = el, d = 0; n && d < 20; n = n.parentElement, d++) {
        if (n.matches && n.matches(":popover-open")) return n;
      }
    } catch (e) { /* :popover-open unsupported on this build — body is the right answer anyway */ }
    return document.body;
  }

  function place(el) {
    if (!box) return;
    const r = el.getBoundingClientRect();
    // position:fixed → viewport coords, no scroll math. Flip above the field if no room below.
    // The box is a list now, so its real height decides — a 3-link popup needs a lot more room
    // than the old one-line chip did.
    const h = box.offsetHeight || 34;
    const below = window.innerHeight - r.bottom;
    box.style.left = Math.max(4, r.left) + "px";
    box.style.top = (below < h + 10 ? Math.max(4, r.top - h - 4) : r.bottom + 4) + "px";
  }

  async function offer(el) {
    if (!fillable(el)) return;
    if (el !== dismissed) dismissed = null; // focusing a different field clears a prior Escape
    if (el === dismissed) return;           // Escape on THIS field sticks until blur/type
    if (!(await ensureProfile())) return;   // the storage listener re-offers when it lands
    showFor(el);
  }
  document.addEventListener("focusin", (e) => offer(evTarget(e)), true);
  // some pages steal/return focus on click — re-offer on click too, so the chip is reliable.
  // Workday: the click often hits a wrapper DIV; resolve to the focused fillable input instead.
  document.addEventListener("click", (e) => {
    const t = evTarget(e);
    if (fillable(t)) { offer(t); return; }
    const a = activeField();
    if (a) {
      const chrome = fieldChrome(a);
      if (chrome && chrome.contains(t)) offer(a);
    }
  }, true);
  document.addEventListener("input", (e) => {
    const t = evTarget(e);
    if (!fillable(t)) return;
    // Typing after Escape means they want suggestions again.
    if (dismissed === t) dismissed = null;
    showFor(t);
  }, true);
  // Only hide when focus actually LEFT all form fields — not when moving field→field (that
  // was the flicker: the old field's focusout was killing the new field's chip).
  document.addEventListener("focusout", () => setTimeout(() => {
    const a = activeField();
    if (!a) {
      dismissed = null;   // left every field — next focus is a clean slate
      if (!(box && box.contains(document.activeElement))) hide();
    }
  }, 160), true);

  // ---------------------------------------------------------------------------------------
  // The watchdog. Everything above is event-driven, and events are exactly what a complicated
  // page takes away from you: a framework re-render can drop our popup out of the DOM, a modal
  // can move the field under it, focus can be stolen and handed back without a focusin, and a
  // late-arriving profile can leave the first focus of the session with nothing to show.
  //
  // So rather than chase each of those, this re-asserts the truth a few times a second: if a
  // fillable field is focused and has something to offer, the popup is on screen and pinned to
  // it. Cheap by construction — it does nothing at all unless the answer has changed.
  const TICK_MS = 400;
  setInterval(() => {
    const a = activeField();
    if (!a) return;                              // nothing focused: leave whatever state exists
    if (a === dismissed) return;                 // you closed it on purpose; stay closed
    if (!SUGG) { ensureProfile().then((p) => { if (p && activeField() === a) showFor(a); }); return; }
    if (box && currentInput === a && box.isConnected) { place(a); return; }   // healthy: just re-pin
    showFor(a);                                  // missing, detached, or pointing at the wrong field
  }, TICK_MS);

  // The script can start with a field already focused (installed or reloaded on an open page),
  // and no focusin will ever fire for it.
  const initial = activeField();
  if (initial) offer(initial);
  // Keyboard, Chrome-autofill style: arrows move the highlight, Enter takes the highlighted row.
  // Enter is only intercepted once you've actually arrowed onto something — otherwise it stays the
  // page's own Enter and submitting the form keeps working.
  document.addEventListener("keydown", (e) => {
    if (!box || !currentInput) return;
    if (e.key === "Escape") { dismiss(); return; }
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (!items.length) return;
      e.preventDefault();
      const step = e.key === "ArrowDown" ? 1 : -1;
      sel = (sel + step + items.length + (sel < 0 && step < 0 ? 1 : 0)) % items.length;
      paint();
      return;
    }
    if (e.key === "Enter" && sel >= 0) { e.preventDefault(); accept(currentInput, sel); return; }
    if (e.key === "Tab" && sel >= 0) { accept(currentInput, sel); }
  }, true);
  // Clicking away hides the chip. Do NOT permanent-dismiss unless it's truly outside the field —
  // Workday clicks land on wrapper DIVs while the <input> keeps focus (see dismissed note above).
  document.addEventListener("mousedown", (e) => {
    const t = evTarget(e);
    if (!box) return;
    if (t === box || box.contains(t)) return;
    if (fillable(t)) return;
    const a = currentInput || activeField();
    if (a && (a === t || a.contains(t))) return;
    const chrome = fieldChrome(a);
    if (chrome && chrome.contains(t)) return;
    hide();   // temporary — watchdog / next focus brings it back
  }, true);
  window.addEventListener("scroll", () => { if (currentInput) place(currentInput); }, true);
  window.addEventListener("resize", () => { if (currentInput) place(currentInput); });
})();
