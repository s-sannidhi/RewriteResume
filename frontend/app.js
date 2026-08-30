// Resume Rewriter — local profile editor. Talks to the FastAPI backend on the same origin.
const API = location.origin;          // served from the backend at /app
let profile = null;

// ---------- tiny DOM helpers ----------
function el(tag, props = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) n.setAttribute(k, v);
  }
  for (const c of kids.flat()) if (c != null) n.append(c.nodeType ? c : document.createTextNode(c));
  return n;
}
const uid = () => Math.random().toString(36).slice(2, 10);
function toast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 1800);
}

// ---------- field widgets (bound by reference to obj[key]) ----------
const ENUMS = {
  us_work_auth_status: ["citizen", "permanent_resident", "authorized", "ead", "student_opt", "needs_sponsorship", "none"],
  needs_sponsorship: ["no", "yes"], veteran_status: ["not_veteran", "veteran", "protected_veteran", "prefer_not_to_say"],
  disability_status: ["no", "yes", "prefer_not_to_say"], gender: ["male", "female", "non_binary", "prefer_not_to_say"],
  security_clearance: ["none", "active", "inactive"], lgbtq_self_id: ["no", "yes", "prefer_not_to_say"],
  work_model_preference: ["no_preference", "remote", "hybrid", "on_site"],
  employment_type: ["full_time", "part_time", "internship", "contract"],
};
const YESNO = ["no", "yes"];
const LONG_KEYS = new Set(["tell_me_about_yourself", "why_company", "challenge_overcome",
  "greatest_strength", "greatest_weakness", "why_hire_you", "role_summary",
  "thesis_description", "non_compete_detail", "fired_detail", "criminal_detail"]);

function labelize(k) { return k.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase()); }

function field(labelText, control) {
  return el("div", { class: "field" }, el("label", {}, labelText), control);
}

function textControl(obj, key, { long = false } = {}) {
  const isLong = long || LONG_KEYS.has(key);
  const c = el(isLong ? "textarea" : "input", { value: obj[key] ?? "" });
  c.addEventListener("input", () => { obj[key] = c.value; });
  return c;
}
function selectControl(obj, key, opts) {
  const s = el("select");
  for (const o of opts) s.append(el("option", { value: o, ...(obj[key] === o ? { selected: "" } : {}) }, o));
  if (!opts.includes(obj[key] ?? "")) s.prepend(el("option", { value: obj[key] ?? "", selected: "" }, obj[key] ?? "—"));
  s.addEventListener("change", () => { obj[key] = s.value; });
  return s;
}
function boolControl(obj, key) {
  const s = selectControl({ v: obj[key] ? "yes" : "no" }, "v", ["yes", "no"]);
  s.addEventListener("change", () => { obj[key] = s.value === "yes"; });
  return s;
}
function autoControl(obj, key) {
  if (key in ENUMS) return selectControl(obj, key, ENUMS[key]);
  if (typeof obj[key] === "boolean") return boolControl(obj, key);
  return textControl(obj, key);
}

// tag editor — mutates the given array in place
function tagEditor(arr, { block = false } = {}) {
  const wrap = el("div");
  const tags = el("div", { class: "tags" });
  const render = () => {
    tags.innerHTML = "";
    arr.forEach((v, i) => {
      tags.append(el("span", { class: "tag" + (block ? " block" : "") },
        el("b", {}, v),
        el("button", { class: "x", title: "remove", onclick: () => { arr.splice(i, 1); render(); } }, "×")));
    });
  };
  const input = el("input", { placeholder: "add…" });
  const add = () => {
    const v = input.value.trim();
    if (v && !arr.some(x => x.toLowerCase() === v.toLowerCase())) { arr.push(v); render(); }
    input.value = "";
  };
  input.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); add(); } });
  render();
  wrap.append(tags, el("div", { class: "add-row" }, input, el("button", { onclick: add }, "Add")));
  return wrap;
}

// textarea where each line is a list item (for bullets)
function linesEditor(obj, key) {
  const t = el("textarea", { value: (obj[key] || []).join("\n") });
  t.style.minHeight = "92px";
  t.addEventListener("input", () => { obj[key] = t.value.split("\n").map(s => s.trim()).filter(Boolean); });
  return t;
}

// ---------- section renderers ----------
function flatCard(title, hint, obj, keys, cols = 2) {
  const g = el("div", { class: "grid" + (cols === 1 ? " one" : "") });
  for (const k of keys) {
    if (!(k in obj)) obj[k] = "";   // seed new fields so they're editable on old profiles
    g.append(field(labelize(k), autoControl(obj, k)));
  }
  return card(title, hint, g);
}
function card(title, hint, ...body) {
  return el("section", { class: "card", id: "sec-" + title.toLowerCase().replace(/[^a-z]+/g, "-") },
    el("h2", {}, title), hint ? el("div", { class: "hint" }, hint) : null, ...body);
}

function skillsCard() {
  const order = ["programming_languages", "frameworks", "ml_ai", "ai_tools", "databases", "cloud",
    "tools", "hardware_embedded", "spoken_languages", "soft_skills"];
  const groups = (obj) => {
    const box = el("div");
    for (const g of order) {
      if (!(g in obj)) obj[g] = [];
      box.append(el("div", { class: "skill-group" }, el("label", {}, labelize(g)), tagEditor(obj[g])));
    }
    return box;
  };
  profile.skills = profile.skills || {};
  profile.skills_extra = profile.skills_extra || {};
  return card("Skills",
    "Only add skills you actually have — the resume draws strictly from this list and never adds anything a job asks for that isn't here. Core skills appear on every resume (JD-relevant first). Extra skills only appear when a job mentions them.",
    el("h2", { class: "hint", style: "margin:6px 0 2px;font-size:13px;color:var(--ink)" }, "Core skills (shown on resumes)"),
    groups(profile.skills),
    el("details", {}, el("summary", { class: "muted", style: "cursor:pointer;margin:6px 0" }, "Extra skills (JD-match pool)"),
      groups(profile.skills_extra)));
}

function arrayCard(title, hint, arrKey, fieldsFn, makeNew) {
  if (!Array.isArray(profile[arrKey])) profile[arrKey] = [];
  const list = el("div");
  const render = () => {
    list.innerHTML = "";
    profile[arrKey].forEach((item, i) => {
      const head = el("div", { class: "item-head" },
        el("strong", {}, (item.title || item.name || item.company || item.school || "Entry") + ""),
        el("button", { class: "danger ghost", onclick: () => { profile[arrKey].splice(i, 1); render(); } }, "Remove"));
      list.append(el("div", { class: "item" }, head, fieldsFn(item)));
    });
  };
  render();
  const addBtn = el("button", { onclick: () => { profile[arrKey].push(makeNew()); render(); } }, "+ Add " + title.replace(/s$/, ""));
  return card(title, hint, list, addBtn);
}

function eduFields(it) {
  const g = el("div", { class: "grid" });
  ["school", "location", "degree", "major", "second_major", "start_date", "end_date", "gpa", "major_gpa"]
    .forEach(k => g.append(field(labelize(k), textControl(it, k))));
  g.append(field("Show GPA", boolControl(it, "show_gpa")));
  const wide = el("div", { class: "grid one" });
  it.coursework = it.coursework || []; it.honors = it.honors || []; it.minors = it.minors || [];
  wide.append(field("Coursework", tagEditor(it.coursework)));
  wide.append(field("Honors", tagEditor(it.honors)));
  wide.append(field("Minors", tagEditor(it.minors)));
  return el("div", {}, g, wide);
}
function workFields(it) {
  const g = el("div", { class: "grid" });
  ["title", "company", "location", "start_date", "end_date", "max_bullets"]
    .forEach(k => g.append(field(labelize(k), textControl(it, k))));
  g.append(field("Currently here", boolControl(it, "current")));
  it.on_resume = it.on_resume !== false;   // missing = shown, so old entries keep their behavior
  g.append(field("Show on resume", boolControl(it, "on_resume")));
  g.append(field("Employment type", selectControl(it, "employment_type", ENUMS.employment_type)));
  const wide = el("div", { class: "grid one" });
  it.tech_used = it.tech_used || []; it.bullets = it.bullets || []; it.metrics = it.metrics || [];
  wide.append(field("Role summary", textControl(it, "role_summary", { long: true })));
  wide.append(field("Skills usable here (only ones you TRULY used — the AI may work these into this experience's bullets, and nothing else)", tagEditor(it.tech_used)));
  wide.append(field("Metrics (one per line — real numbers only; the AI must work every one of these into this entry's bullets)", linesEditor(it, "metrics")));
  wide.append(field("Bullets (one per line — evidence; AI rewrites per job)", linesEditor(it, "bullets")));
  return el("div", {}, g, wide);
}
function projFields(it) {
  const g = el("div", { class: "grid" });
  ["name", "date_range", "link", "context", "max_bullets"].forEach(k => g.append(field(labelize(k), textControl(it, k))));
  const wide = el("div", { class: "grid one" });
  it.tech_stack = it.tech_stack || []; it.bullets = it.bullets || []; it.metrics = it.metrics || [];
  wide.append(field("Skills usable here (only ones you TRULY used — the AI may work these into this project's bullets, and nothing else)", tagEditor(it.tech_stack)));
  wide.append(field("Metrics (one per line — real numbers only; the AI must work every one of these into this entry's bullets)", linesEditor(it, "metrics")));
  wide.append(field("Bullets (one per line)", linesEditor(it, "bullets")));
  return el("div", {}, g, wide);
}

function loginCard() {
  const login = profile.login_credentials ||= {};
  if (!(login.email || "").trim()) login.email = (profile.identity || {}).email || "";
  const g = el("div", { class: "grid" });
  g.append(field("Login email", textControl(login, "email")));

  const status = el("div", { class: "hint" }, "Checking stored password…");
  const pwInput = el("input", {
    type: "password", placeholder: "Type a new password", autocomplete: "new-password",
  });
  const btn = el("button", {}, "Update password");

  async function refreshStatus() {
    try {
      const d = await (await fetch(`${API}/secrets/login`)).json();
      status.textContent = d.set
        ? `A password is stored in the ${d.backend || "credential store"}. Type a new one below to replace it.`
        : "No password saved yet. Autofill and the My Info copy tile will use whatever you save here.";
    } catch (e) {
      status.textContent = "Couldn't reach the backend to check the stored password.";
    }
  }

  btn.addEventListener("click", async () => {
    const pw = (pwInput.value || "").trim();
    if (!pw) { toast("Type a password first"); return; }
    btn.disabled = true;
    try {
      const r = await fetch(`${API}/secrets/login`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password: pw }),
      });
      if (!r.ok) {
        let msg = "HTTP " + r.status;
        try { const j = await r.json(); msg = j.detail || msg; } catch (e) {}
        throw new Error(msg);
      }
      pwInput.value = "";
      toast("Login password saved ✓");
      refreshStatus();
    } catch (e) { toast("Failed: " + e.message); }
    finally { btn.disabled = false; }
  });
  pwInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); btn.click(); }
  });
  refreshStatus();
  return card("Job-site login",
    "Email and password used to sign into Workday, Greenhouse, Lever, and other application sites. The password is stored in your OS credential store — never in profile.json. Email is saved with Save profile; password saves immediately.",
    g,
    field("Password", el("div", {}, status, pwInput,
      el("div", { class: "add-row", style: "margin-top:8px" }, btn))));
}

// ---------- app ----------
const SECTIONS = [
  { name: "Identity", fn: () => flatCard("Identity", "Names, contact, links.", profile.identity ||= {},
      ["legal_name", "preferred_name", "pronouns", "email", "phone", "location", "street_address", "zip", "linkedin", "github", "portfolio"]) },
  { name: "Login", fn: loginCard },
  { name: "Work Auth", fn: () => flatCard("Work Auth & Demographics", "Used for autofill of EEO/work-authorization questions.",
      profile.work_auth ||= {}, ["us_work_auth_status", "needs_sponsorship", "veteran_status", "disability_status", "gender", "security_clearance", "lgbtq_self_id"]) },
  { name: "Skills", fn: skillsCard },
  { name: "Education", fn: () => arrayCard("Education", "", "education", eduFields,
      () => ({ id: uid(), school: "", degree: "", major: "", start_date: "", end_date: "", location: "", gpa: "", show_gpa: true, coursework: [], honors: [], minors: [] })) },
  { name: "Experience", fn: () => arrayCard("Experience", "Bullets here are evidence — the AI rewrites them per job.", "work_experience", workFields,
      () => ({ id: uid(), company: "", title: "", location: "", start_date: "", end_date: "", current: false, on_resume: true, employment_type: "internship", role_summary: "", bullets: [], metrics: [], tech_used: [], max_bullets: 4 })) },
  { name: "Projects", fn: () => arrayCard("Projects", "", "projects", projFields,
      () => ({ id: uid(), name: "", date_range: "", link: "", tech_stack: [], bullets: [], metrics: [], context: "", max_bullets: 3 })) },
  { name: "Answers", fn: () => flatCard("Reusable Answers", "Starting points the AI adapts for application essays / common questions.",
      profile.reusable_answers ||= {}, Object.keys(profile.reusable_answers || {}), 1) },
  { name: "Disclosures", fn: () => flatCard("Disclosures", "", profile.disclosures ||= {}, Object.keys(profile.disclosures || {})) },
  { name: "Applications", fn: applicationsCard },
];

// AI Ask is now its own standalone page (/app/ai-ask.html) — this section function is unused.
function aiAskCard() {
  const threadSel = el("select", { style: "flex:1;min-width:0" });
  const newBtn = el("button", {}, "+ New chat");
  const delBtn = el("button", { class: "ghost" }, "Delete");
  const msgs = el("div", { class: "chat-msgs" });
  const input = el("textarea", { class: "chat-input", rows: "2",
    placeholder: "Ask anything — a job/club/scholarship question, an essay, a message… (Cmd/Ctrl+Enter to send)" });
  const send = el("button", { class: "primary" }, "Send");
  const st = el("div", { class: "hint", style: "margin-top:6px" });
  const S = { threadId: null };

  const setSt = (m) => { st.textContent = m || ""; };

  async function loadThreads(selectId) {
    let list = [];
    try { list = (await (await fetch(`${API}/chat/threads`)).json()).threads || []; }
    catch (e) { setSt("Backend not reachable."); return; }
    threadSel.innerHTML = "";
    if (!list.length) threadSel.append(el("option", { value: "" }, "No chats yet — start one →"));
    list.forEach((t) => threadSel.append(el("option", { value: t.id }, `${t.title}  (${t.count})`)));
    if (selectId) threadSel.value = selectId;
    S.threadId = threadSel.value || null;
    if (S.threadId) openThread(S.threadId); else msgs.innerHTML = "";
  }

  async function openThread(id) {
    S.threadId = id;
    msgs.innerHTML = "";
    let t;
    try { t = await (await fetch(`${API}/chat/thread/${id}`)).json(); } catch (e) { return; }
    (t.messages || []).forEach(addBubble);
    msgs.scrollTop = msgs.scrollHeight;
  }

  function addBubble(m) {
    const b = el("div", { class: "bubble " + (m.role === "user" ? "me" : "ai") }, m.content || "");
    msgs.append(b);
    msgs.scrollTop = msgs.scrollHeight;
    return b;
  }

  async function newThread() {
    const t = await (await fetch(`${API}/chat/thread`, { method: "POST" })).json();
    await loadThreads(t.id);
    input.focus();
  }

  async function delThread() {
    if (!S.threadId || !confirm("Delete this chat and its history?")) return;
    await fetch(`${API}/chat/thread/${S.threadId}`, { method: "DELETE" });
    await loadThreads();
  }

  async function sendMsg() {
    const text = input.value.trim();
    if (!text) return;
    if (!S.threadId) { const t = await (await fetch(`${API}/chat/thread`, { method: "POST" })).json(); await loadThreads(t.id); }
    input.value = "";
    addBubble({ role: "user", content: text });
    const thinking = addBubble({ role: "ai", content: "…" });
    send.disabled = true; setSt("Thinking… (local model, humanized answers take ~10–20s)");
    try {
      const res = await (await fetch(`${API}/chat/${S.threadId}/message`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
      })).json();
      thinking.textContent = res.answer || res.hint || res.error || "(no answer)";
      setSt("");
      loadThreads(S.threadId);   // refresh title/count
    } catch (e) { thinking.textContent = "Failed: " + e.message; setSt(""); }
    finally { send.disabled = false; }
  }

  threadSel.addEventListener("change", () => openThread(threadSel.value));
  newBtn.addEventListener("click", newThread);
  delBtn.addEventListener("click", delThread);
  send.addEventListener("click", sendMsg);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); sendMsg(); }
  });

  loadThreads();
  return card("AI Ask",
    "Chat with your local AI, grounded in your profile and humanized (no AI-speak). Keeps full history per chat — use it for job apps, club apps, scholarships, essays, or anything.",
    el("div", { class: "chat-bar" }, threadSel, newBtn, delBtn),
    msgs,
    el("div", { class: "chat-compose" }, input, send),
    st);
}

// ---------- applications tracker (table, live from /tracker) ----------
const APP_STATUSES = ["generated", "applied", "interview", "offer", "accepted", "rejected", "withdrawn"];

function applicationsCard() {
  const body = el("div", { class: "muted" }, "Loading applications…");
  loadApplications(body);
  return card("Applications", "Every application you've generated. Change the status as things progress.", body);
}

async function loadApplications(body) {
  let recs;
  try { recs = await (await fetch(`${API}/tracker`)).json(); }
  catch (e) { body.textContent = "Couldn't load applications — is the backend running?"; return; }
  if (!Array.isArray(recs) || !recs.length) { body.textContent = "No applications yet."; return; }
  body.innerHTML = "";

  const table = el("table", { class: "apps-table" });
  table.append(el("tr", {}, ...["Internship", "Role", "Chat", "Date applied", "Status"]
    .map((h) => el("th", {}, h))));

  for (const r of recs) {
    const date = (r.sent_at || r.created_at || "").slice(0, 10);
    const n = (r.ask_history || []).length;
    const chat = n
      ? el("a", { href: "#", onclick: (e) => { e.preventDefault(); showChat(r); } }, `Chat (${n})`)
      : el("span", { class: "muted" }, "—");

    const cur = r.status || "generated";
    const opts = APP_STATUSES.includes(cur) ? APP_STATUSES : [cur, ...APP_STATUSES];
    const sel = el("select", { class: "status-" + cur });
    for (const o of opts) sel.append(el("option", { value: o, ...(o === cur ? { selected: "" } : {}) }, o));
    sel.addEventListener("change", async () => {
      const prev = sel.dataset.prev || cur;
      try {
        const res = await (await fetch(`${API}/tracker/${r.id}`, {
          method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: sel.value }),
        })).json();
        if (res.error) throw new Error(res.error);
        sel.className = "status-" + sel.value;
        sel.dataset.prev = sel.value;
        toast("Status → " + sel.value);
      } catch (e) { sel.value = prev; toast("Failed: " + e.message); }
    });
    sel.dataset.prev = cur;

    table.append(el("tr", {},
      el("td", {}, el("strong", {}, r.company || "—")),
      el("td", {}, r.role || "—"),
      el("td", {}, chat),
      el("td", { class: "muted" }, date || "—"),
      el("td", {}, sel)));
  }
  body.append(table);
}

function showChat(r) {
  const hist = r.ask_history || [];
  const items = hist.map((h) => el("div", { class: "chat-item" },
    el("div", { class: "chat-q" }, h.question || "(question)"),
    el("div", { class: "chat-at" }, (h.at || "").replace("T", " ").slice(0, 16)),
    el("div", { class: "chat-a" }, h.answer || "")));
  const modal = el("div", { class: "modal-back", onclick: (e) => { if (e.target === modal) modal.remove(); } },
    el("div", { class: "modal" },
      el("div", { class: "modal-head" },
        el("strong", {}, `Ask history — ${r.company || ""}${r.role ? " · " + r.role : ""}`),
        el("button", { class: "ghost", onclick: () => modal.remove() }, "Close")),
      el("div", { class: "modal-body" },
        hist.length ? items : el("div", { class: "muted" }, "No Q&A saved for this application."))));
  document.body.append(modal);
}

function renderAll() {
  const nav = document.getElementById("nav"), main = document.getElementById("main");
  nav.innerHTML = ""; main.innerHTML = "";
  for (const s of SECTIONS) {
    const node = s.fn();
    main.append(node);
    nav.append(el("a", { href: "#" + node.id, onclick: () => setActive(s.name) }, s.name));
  }
  document.getElementById("status").textContent = `${profile.identity?.legal_name || "profile"} · loaded`;
}
function setActive(name) {
  document.querySelectorAll("nav.side a").forEach(a => a.classList.toggle("active", a.textContent === name));
}

async function load() {
  let err = "";
  try {
    const r = await fetch(`${API}/profile`);
    if (!r.ok) throw new Error("HTTP " + r.status);
    profile = await r.json();
  } catch (e) {
    profile = {};              // an empty editable form beats a blank page
    err = e.message;
  }
  renderAll();                 // renderAll() writes #status, so any error goes after it
  if (err) document.getElementById("status").textContent =
    `Couldn't load your profile (${err}) — is the server running? ./run.sh or run.bat`;
}
async function save() {
  const btn = document.getElementById("save"); btn.disabled = true; btn.textContent = "Saving…";
  const payload = { ...profile };
  if (payload.login_credentials) {
    payload.login_credentials = { ...payload.login_credentials, password: "" };
  }
  try {
    const r = await fetch(`${API}/profile`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    if (!r.ok) throw new Error(await r.text());
    toast("Saved ✓");
  } catch (e) { toast("Save failed: " + e.message); }
  finally { btn.disabled = false; btn.textContent = "Save profile"; }
}

document.getElementById("save").addEventListener("click", save);
document.getElementById("reload").addEventListener("click", load);
load();
