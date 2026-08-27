// Applications tab — the tracker, straight from the backend. Sent/Not-sent is the one bit of
// state you manage here; sending from the Email tab flips it automatically.
(() => {
  function setSt(msg, cls = "") { const s = $("appsStatus"); s.textContent = msg; s.className = "status " + cls; }

  async function toggleSent(rec, btn) {
    btn.disabled = true;
    try {
      const r = await fetch(`${API}/tracker/${rec.id}`, {
        method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: rec.status === "sent" ? "generated" : "sent" }),
      });
      const res = await r.json();
      if (res.error) throw new Error(res.error);
      load();
    } catch (e) { setSt("Failed: " + e.message, "err"); btn.disabled = false; }
  }

  function row(rec) {
    const sent = rec.status === "sent";
    const pill = el("span", { class: "pill " + (sent ? "sent" : "notsent") },
      sent ? "Sent" + (rec.sent_at ? " · " + rec.sent_at.slice(0, 10) : "") : "Not sent");
    const toggle = el("button", { class: "small" }, sent ? "Mark not sent" : "Mark sent");
    toggle.addEventListener("click", () => toggleSent(rec, toggle));

    const links = el("div", { style: "margin-top:4px" });
    links.append(el("a", { class: "link", href: "#", style: "font-size:12px;margin-right:10px",
      onclick: (e) => { e.preventDefault(); chrome.tabs.create({ url: `${API}/resume/pdf/${rec.id}` }); } },
      "resume"));
    if (rec.cover_letter_filename) {
      links.append(el("a", { class: "link", href: "#", style: "font-size:12px;margin-right:10px",
        onclick: (e) => { e.preventDefault(); chrome.tabs.create({ url: `${API}/cover-letter/pdf/${rec.id}` }); } },
        "cover letter"));
    }
    const emailLink = el("a", { class: "link", href: "#", style: "font-size:12px",
      onclick: (e) => { e.preventDefault(); emailPrefill(rec.id); } }, "email this");
    links.append(emailLink);

    return el("div", { class: "card" },
      el("div", { class: "row", style: "justify-content:space-between" },
        el("span", { class: "k" }, (rec.company || "—").slice(0, 40)), pill),
      el("div", { class: "muted", style: "font-size:12px" },
        (rec.role || "") + " · " + (rec.created_at || "").slice(0, 10)),
      links,
      askHistory(rec.ask_history || []),
      el("div", { style: "margin-top:8px" }, toggle));
  }

  // The Ask-tab Q&As saved against this application (kept, never overwritten).
  function askHistory(hist) {
    if (!hist.length) return el("span");
    const box = el("details", { style: "margin-top:8px" },
      el("summary", { style: "cursor:pointer;font-size:12px;color:var(--accent)" },
        `Q&A history (${hist.length})`));
    hist.slice().reverse().forEach((h) => {
      const when = (h.at || "").replace("T", " ").slice(0, 16);
      const copy = el("button", { class: "small" }, "Copy");
      copy.addEventListener("click", async () => {
        try { await navigator.clipboard.writeText(h.answer); } catch (e) {}
        copy.textContent = "✓"; setTimeout(() => (copy.textContent = "Copy"), 1500);
      });
      box.append(el("div", { style: "margin-top:8px;border-top:1px solid var(--line);padding-top:6px" },
        el("div", { class: "k", style: "font-size:12.5px" }, h.question || "(question)"),
        el("div", { class: "muted", style: "font-size:11px" }, when),
        el("div", { style: "white-space:pre-wrap;font-size:12.5px;margin:4px 0" }, h.answer),
        el("div", { class: "row" }, copy)));
    });
    return box;
  }

  async function load() {
    setSt("Loading…");
    try {
      const r = await fetch(`${API}/tracker`);
      if (!r.ok) throw new Error("HTTP " + r.status);
      const recs = await r.json();
      const body = $("appsBody"); body.innerHTML = "";
      if (!recs.length) { setSt("No applications yet — generate a resume first."); return; }
      setSt("");
      // Just the 3 most recent here — the website dashboard has the full list + analytics.
      recs.slice(0, 3).forEach((rec) => body.append(row(rec)));
      body.append(el("a", { class: "link", href: "#",
        style: "font-size:12px;display:inline-block;margin-top:10px",
        onclick: (e) => { e.preventDefault(); chrome.tabs.create({ url: `${API}/app/dashboard.html` }); } },
        `📊 See all ${recs.length} + progress on the dashboard →`));
    } catch (e) {
      setSt("Backend error — is ./run.sh running? (" + e.message + ")", "err");
    }
  }

  $("appsRefreshBtn").addEventListener("click", load);
  document.addEventListener("rr-tab-shown", (e) => { if (e.detail.tab === "apps") load(); });
})();
