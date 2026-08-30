// In-page autofill engine — the "hands". These run in the PAGE context (injected via
// chrome.scripting.executeScript). No chrome.* APIs here, so they're testable standalone.
//
// Two phases, two executeScript calls on the same page (IDs persist on the DOM between them):
//   rrDiscoverFields()             -> [{id, type, label, name, options, required}]   (isolated world)
//   rrApplyActionsReact(actions)   -> [{id, ok, note}]   (MAIN world; never clicks submit)
//
// Filling uses the Simplify-style approach: read React's OWN handlers off each element
// (the __reactProps$ key) and call onChange/onBlur/onClick directly, backed by the native value
// setter + dispatched events. That combo works on standard React ATS (Greenhouse/Lever/Ashby) AND
// the widgets that ignore plain synthetic events (Workday) — no chrome.debugger needed. Because it
// needs React internals, the applier MUST be injected into the page's MAIN world.

function rrDiscoverFields() {
  const isVisible = (el) => {
    // Don't use offsetParent: Firefox returns null for position:fixed and for children of
    // display:contents, which made whole Workday/Ashby sections look empty.
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return false;
    const s = getComputedStyle(el);
    return s.visibility !== "hidden" && s.display !== "none";
  };
  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();

  // best-effort label text for a control
  const labelText = (el) => {
    if (el.labels && el.labels.length) {
      const t = [...el.labels].map((l) => clean(l.textContent)).filter(Boolean).join(" ");
      if (t) return t;
    }
    const lb = el.getAttribute("aria-labelledby");
    if (lb) {
      const t = lb.split(/\s+/).map((id) => clean(document.getElementById(id)?.textContent))
        .filter(Boolean).join(" ");
      if (t) return t;
    }
    if (el.getAttribute("aria-label")) return clean(el.getAttribute("aria-label"));
    const wrap = el.closest("label");
    if (wrap) return clean(wrap.textContent);
    // preceding sibling text / parent's label-ish node
    let p = el.parentElement, hops = 0;
    while (p && hops < 3) {
      const lab = p.querySelector("label, legend, .label, [class*=label]");
      if (lab && !lab.contains(el)) { const t = clean(lab.textContent); if (t) return t; }
      hops++; p = p.parentElement;
    }
    return clean(el.placeholder) || clean(el.name) || clean(el.id);
  };

  // Resolve aria-labelledby to its actual TEXT. On an ARIA radio the labelledby points at the
  // question and aria-label carries the option, so without this the question reaches the
  // classifier as a raw element id ("lauth") and no rule can possibly match it.
  const labelledByText = (el) => {
    const ref = el.getAttribute("aria-labelledby");
    if (!ref) return "";
    return clean(ref.split(/\s+/).map((id) => document.getElementById(id)?.textContent || "")
      .filter(Boolean).join(" "));
  };

  // group label for a radio/checkbox set (the question), from a fieldset legend or heading
  const groupLabel = (el) => {
    const byRef = labelledByText(el);
    if (byRef) return byRef;
    const fs = el.closest("fieldset");
    if (fs) { const lg = fs.querySelector("legend"); if (lg) return clean(lg.textContent); }
    const grp = el.closest('[role=radiogroup],[role=group],.field,[class*=field]');
    if (grp) {
      const lab = grp.querySelector("label, legend, .label, [class*=label]");
      if (lab) return clean(lab.textContent);
    }
    return "";
  };

  const normType = (el) => {
    const t = (el.type || el.tagName).toLowerCase();
    if (el.tagName === "TEXTAREA") return "textarea";
    if (el.tagName === "SELECT") return "select";
    if (["radio", "checkbox", "file", "email", "tel", "url", "number", "date"].includes(t)) return t;
    const role = (el.getAttribute("role") || "").toLowerCase();
    if (role === "checkbox" || role === "switch") return "checkbox";
    if (role === "radio") return "radio";
    if (role === "combobox" || el.getAttribute("aria-autocomplete")) return "combobox";
    if (el.tagName === "BUTTON" && el.getAttribute("aria-haspopup")) return "combobox";
    return "text";
  };

  // HOW the widget has to be driven, which is not the same question as what it means. Workday
  // ships three different things that all read as "a dropdown", and they need three different
  // interactions — assuming one behaviour is why dropdown filling was unreliable:
  //   native_select   <select> — set value, fire change. No popup involved.
  //   typeahead       an input you type into; the list filters as you type and you must CLICK a
  //                   row. Typing alone selects nothing, whatever the box looks like afterwards.
  //   listbox_button  a button that opens a listbox; you click the button, then click the option.
  // The distinction is read off the DOM rather than guessed from the site.
  const widgetOf = (el, type) => {
    if (type === "select") return "native_select";
    if (type === "checkbox") return "checkbox";
    if (type === "radio") return "radio";
    if (type === "combobox") {
      const editable = el.tagName === "INPUT" || el.tagName === "TEXTAREA" ||
        el.isContentEditable || (el.getAttribute("aria-autocomplete") || "").toLowerCase() !== "none";
      return editable && el.tagName !== "BUTTON" ? "typeahead" : "listbox_button";
    }
    return "text";
  };

  // The listbox a widget owns, if it is open right now. Workday renders these in a body-level
  // popup rather than inside the field, so aria-controls/aria-owns is the only reliable link.
  const listboxFor = (el) => {
    const id = el.getAttribute("aria-controls") || el.getAttribute("aria-owns");
    if (id) {
      const box = document.getElementById(id);
      if (box) return box;
    }
    return null;
  };
  // Options only exist once the widget is OPEN, so discovery usually finds none. That is expected:
  // the applier opens the widget and matches against the real rows at fill time.
  const openOptions = (el) => {
    const box = listboxFor(el);
    if (!box) return [];
    return [...box.querySelectorAll('[role=option],[data-automation-id=promptOption]')]
      .map((o) => clean(o.getAttribute("data-automation-label") || o.textContent))
      .filter(Boolean).slice(0, 60);
  };

  const out = [];
  const radioGroups = {};   // name -> descriptor index
  let i = 0;

  // ARIA widgets are included because Workday builds almost nothing out of native controls: its
  // dropdowns are buttons, its checkboxes are divs. Scanning only input/select/textarea meant the
  // majority of a Workday form was invisible to autofill and silently left for the user.
  const SCAN = "input, select, textarea, [role=combobox], [role=checkbox], [role=radio], " +
               "[role=switch], button[aria-haspopup]";

  document.querySelectorAll(SCAN).forEach((el) => {
    // A <button> reports type "submit" by default, so the junk-type filter below would throw away
    // every Workday dropdown before it was ever considered. Popup buttons are judged on their own
    // rule (just below) instead.
    const isPopupButton = el.tagName === "BUTTON" && el.hasAttribute("aria-haspopup");
    const raw = (el.type || "").toLowerCase();
    if (!isPopupButton && ["hidden", "submit", "button", "reset", "image"].includes(raw)) return;
    if (el.hasAttribute("data-rr-id")) return;                     // matched by two selectors
    if (el.disabled || el.getAttribute("aria-disabled") === "true") return;
    if (el.readOnly || !isVisible(el)) return;
    // A plain button only counts when it opens something to CHOOSE FROM. Deliberately excludes
    // aria-haspopup="dialog": that is a help or detail modal, not a field with an answer.
    if (el.tagName === "BUTTON" &&
        !/listbox|menu|tree|grid/i.test(el.getAttribute("aria-haspopup") || "")) return;

    const type = normType(el);
    const widget = widgetOf(el, type);

    if (type === "radio") {
      // The group NAME only has to be a stable key, so a labelledby id is fine there. The group
      // LABEL is what gets classified, so it has to be readable text.
      const name = el.name || el.getAttribute("aria-labelledby") || groupLabel(el) || labelText(el);
      const optLabel = clean(el.getAttribute("aria-label")) || clean(el.value) ||
        labelText(el) || clean(el.textContent);
      el.setAttribute("data-rr-group", name);
      el.setAttribute("data-rr-opt", optLabel);
      if (name in radioGroups) { out[radioGroups[name]].options.push(optLabel); return; }
      const id = "rr" + (i++);
      el.setAttribute("data-rr-id", id);
      radioGroups[name] = out.length;
      out.push({ id, type: "radio", widget: "radio", label: groupLabel(el) || name, name,
        options: [optLabel], required: el.required });
      return;
    }

    const id = "rr" + (i++);
    el.setAttribute("data-rr-id", id);
    const d = {
      id, type, widget, label: labelText(el), name: el.name || el.id || "",
      placeholder: el.placeholder || "", autocomplete: el.getAttribute("autocomplete") || "",
      required: el.required || el.getAttribute("aria-required") === "true",
    };
    if (el.tagName === "SELECT") {
      d.options = [...el.options].map((o) => clean(o.textContent)).filter(Boolean);
    } else if (type === "combobox") {
      // Usually empty — the list does not exist until the widget is opened. Sent anyway for the
      // rare widget that renders its options up front, and so the resolver can tell the difference
      // between "no options" and "options we haven't looked for".
      d.options = openOptions(el);
      d.options_deferred = d.options.length === 0;
    }
    if (type === "checkbox") { d.label = labelText(el) || clean(el.textContent); d.options = ["Yes", "No"]; }
    out.push(d);
  });

  return out;
}

async function rrApplyActionsReact(actions) {
  // Read React's own handlers off a DOM node (only works in the page's MAIN world).
  const reactProps = (el) => {
    // React 17+ exposes handlers on __reactProps$…; React 16 (which plenty of Workday tenants
    // still ship) uses __reactEventHandlers$… instead, and older builds only expose the fiber via
    // __reactInternalInstance$…, whose memoizedProps hold the same handlers. Checking only the
    // 17+ key silently no-ops on a React 16 page — every handler call short-circuits and the
    // widget never searches.
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
  const evt = (el) => ({ target: el, currentTarget: el, preventDefault() {}, stopPropagation() {} });
  const norm = (s) => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

  // Fill a text/textarea the way React accepts: native value setter (defeats React's value
  // tracking) THEN call React's onChange/onBlur directly AND dispatch input/change. Between the two
  // techniques this covers standard React and the pickier widgets that ignore synthetic events.
  const setText = (el, value) => {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set;
    setter ? setter.call(el, value) : (el.value = value);
    const p = reactProps(el), e = evt(el);
    try { p && p.onChange && p.onChange(e); } catch (err) {}
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    try { p && p.onBlur && p.onBlur(e); } catch (err) {}   // validated fields commit on blur
  };
  // Click that React reliably hears: real .click() plus a direct onClick call.
  const clickEl = (el) => {
    const p = reactProps(el);
    try { el.click(); } catch (err) {}
    try { p && p.onClick && p.onClick(evt(el)); } catch (err) {}
  };

  const results = [];
  for (const a of actions || []) {
    const r = { id: a.field_id, ok: false, note: "" };
    try {
      const el = document.querySelector(`[data-rr-id="${a.field_id}"]`);
      if (a.action === "skip" || a.action === "upload") {
        r.note = a.action === "upload" ? "file — attach from the Docs tab" : "skipped";
        results.push(r); continue;
      }
      if (!el) { r.note = "element gone"; results.push(r); continue; }

      if (a.action === "fill") {
        setText(el, a.value || "");
        r.ok = true;
      } else if (a.action === "type_then_pick" || a.action === "select") {
        const widget = (a.widget || "").toLowerCase() ||
          (el.tagName === "SELECT" ? "native_select"
            : el.type === "checkbox" || el.getAttribute("role") === "checkbox" ? "checkbox"
            : el.type === "radio" || el.getAttribute("role") === "radio" ? "radio"
            : (el.getAttribute("role") === "combobox" || el.tagName === "BUTTON")
              ? ((el.tagName === "INPUT" || el.tagName === "TEXTAREA") ? "typeahead" : "listbox_button")
              : "text");
        const candidates = (a.candidates && a.candidates.length)
          ? a.candidates : [a.option || a.value].filter(Boolean);
        const wantAny = candidates.map(norm).filter(Boolean);
        const matchOpt = (opts) => {
          // Prefer exact, then includes either way.
          for (const w of wantAny) {
            const hit = opts.find((o) => norm(o.text || o) === w);
            if (hit) return hit;
          }
          for (const w of wantAny) {
            const hit = opts.find((o) => {
              const t = norm(o.text || o);
              return w && t && (t.includes(w) || w.includes(t));
            });
            if (hit) return hit;
          }
          return null;
        };
        const sleep = (ms) => new Promise((res) => setTimeout(res, ms));
        const visibleOpts = () => {
          // Workday: body-level popup; ARIA: aria-controls listbox; fallback: any open listbox.
          const pops = [...document.querySelectorAll(
            '[data-automation-widget="wd-popup"], [role="listbox"], [data-automation-id="promptOption"]'
          )];
          let rows = [];
          const id = el.getAttribute("aria-controls") || el.getAttribute("aria-owns");
          if (id) {
            const box = document.getElementById(id);
            if (box) rows = [...box.querySelectorAll('[role="option"],[data-automation-id="promptOption"]')];
          }
          if (!rows.length) {
            const pop = [...document.querySelectorAll('[data-automation-widget="wd-popup"]')]
              .filter((p) => p.offsetParent !== null || p.getClientRects().length).pop();
            if (pop) rows = [...pop.querySelectorAll('[role="option"],[data-automation-id="promptOption"]')];
          }
          if (!rows.length) {
            rows = [...document.querySelectorAll('[role="option"],[data-automation-id="promptOption"]')]
              .filter((o) => o.offsetParent !== null || o.getClientRects().length);
          }
          return rows.map((node) => ({
            node,
            text: (node.getAttribute("data-automation-label") || node.textContent || "").trim(),
          })).filter((o) => o.text);
        };
        const waitOpts = async (ms) => {
          const t0 = Date.now();
          while (Date.now() - t0 < ms) {
            const opts = visibleOpts();
            if (opts.length) return opts;
            await sleep(80);
          }
          return visibleOpts();
        };
        const clickOpt = (opt) => {
          if (!opt || !opt.node) return false;
          try { opt.node.scrollIntoView({ block: "nearest" }); } catch (err) {}
          clickEl(opt.node);
          return true;
        };

        if (widget === "native_select" || el.tagName === "SELECT") {
          const want = norm(a.option || a.value);
          const opt = [...el.options].find((o) => norm(o.textContent) === want)
            || [...el.options].find((o) => want && norm(o.textContent).includes(want))
            || [...el.options].find((o) => wantAny.some((w) => norm(o.textContent) === w
              || (w && norm(o.textContent).includes(w))));
          if (opt) {
            const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set;
            setter ? setter.call(el, opt.value) : (el.value = opt.value);
            const p = reactProps(el);
            try { p && p.onChange && p.onChange(evt(el)); } catch (err) {}
            el.dispatchEvent(new Event("change", { bubbles: true }));
            r.ok = true;
          } else r.note = "no matching option";
        } else if (widget === "checkbox" || el.type === "checkbox" || el.getAttribute("role") === "checkbox") {
          const yes = /^(yes|true|y|on|1|i agree|agree|i consent|i acknowledge)$/i
            .test(String(a.value || a.option || ""));
          const checked = !!(el.checked || el.getAttribute("aria-checked") === "true");
          if (checked !== yes) clickEl(el);
          r.ok = true;
        } else if (widget === "radio" || el.type === "radio" || el.getAttribute("role") === "radio") {
          const want = norm(a.option || a.value);
          const grp = el.getAttribute("data-rr-group") || "";
          const radios = grp
            ? [...document.querySelectorAll(`[data-rr-group="${grp}"]`)]
            : [el];
          const pick = radios.find((x) => norm(x.getAttribute("data-rr-opt")) === want)
            || radios.find((x) => want && norm(x.getAttribute("data-rr-opt") || "").includes(want))
            || radios.find((x) => wantAny.some((w) => {
              const t = norm(x.getAttribute("data-rr-opt") || x.getAttribute("aria-label") || x.textContent);
              return t === w || (w && t.includes(w));
            }));
          if (pick) { clickEl(pick); r.ok = true; } else r.note = "no matching radio";
        } else if (widget === "listbox_button") {
          // Open → wait for options → click matching row. Do NOT rely on typing.
          try { el.focus(); } catch (err) {}
          clickEl(el);
          await sleep(280);
          let opts = await waitOpts(2500);
          let hit = matchOpt(opts);
          if (!hit && a.value) {
            // Some listboxes filter if they also have a search box inside the popup.
            const pop = document.querySelector('[data-automation-widget="wd-popup"]');
            const search = pop && pop.querySelector('input');
            if (search) {
              setText(search, String(candidates[0] || a.value));
              await sleep(350);
              opts = await waitOpts(2000);
              hit = matchOpt(opts);
            }
          }
          if (hit && clickOpt(hit)) { await sleep(200); r.ok = true; }
          else r.note = "no matching listbox option";
        } else if (widget === "typeahead" || a.action === "type_then_pick") {
          // Type/search → wait for filtered options → CLICK the matching option.
          // Typing or Enter alone is not enough on Workday typeaheads.
          try { el.focus(); } catch (err) {}
          clickEl(el);
          await sleep(150);
          const query = String(candidates[0] || a.value || "");
          setText(el, query);
          await sleep(400);
          let opts = await waitOpts(2800);
          let hit = matchOpt(opts);
          // Progressive shorter queries if the full string over-filters.
          if (!hit && query.includes(" ")) {
            const words = query.split(/\s+/);
            for (let cut = words.length - 1; cut >= 1 && !hit; cut--) {
              setText(el, words.slice(0, cut).join(" "));
              await sleep(350);
              opts = await waitOpts(1800);
              hit = matchOpt(opts);
            }
          }
          if (hit && clickOpt(hit)) {
            await sleep(250);
            r.ok = true;
          } else {
            // Last resort: Enter (some tenants commit the highlighted row). Still mark for verify.
            try {
              el.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", keyCode: 13, bubbles: true }));
              el.dispatchEvent(new KeyboardEvent("keyup", { key: "Enter", code: "Enter", keyCode: 13, bubbles: true }));
            } catch (err) {}
            r.ok = true;
            r.note = "typed — verify the dropdown picked the right option";
          }
        } else {
          // Unknown select-like widget: try typing then clicking an option.
          setText(el, String(a.value || a.option || ""));
          await sleep(300);
          const opts = await waitOpts(1500);
          const hit = matchOpt(opts);
          if (hit && clickOpt(hit)) r.ok = true;
          else { r.ok = true; r.note = "filled — verify selection"; }
        }
      }
    } catch (e) { r.note = String(e).slice(0, 80); }
    results.push(r);
  }
  return results;
}

// Outline the fields autofill left blank (unknown questions, free-text, "do you know anyone here")
// so the user can find and answer them by hand. Clears any previous highlight first, then scrolls
// the first one into view. Injected after apply. Returns how many it marked.
function rrHighlightFields(ids) {
  document.querySelectorAll("[data-rr-hl]").forEach((el) => {
    el.style.outline = ""; el.style.outlineOffset = ""; el.style.boxShadow = "";
    el.removeAttribute("data-rr-hl");
  });
  let first = null;
  for (const id of ids || []) {
    const el = document.querySelector(`[data-rr-id="${id}"]`);
    if (!el) continue;
    el.setAttribute("data-rr-hl", "1");
    el.style.outline = "2px solid #f59e0b";
    el.style.outlineOffset = "1px";
    el.style.boxShadow = "0 0 0 4px rgba(245,158,11,0.25)";
    el.title = "⚠ Autofill left this blank — please fill it yourself";
    if (!first) first = el;
  }
  if (first) first.scrollIntoView({ block: "center", behavior: "smooth" });
  return (ids || []).length;
}
