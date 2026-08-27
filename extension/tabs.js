// Tab shell: toggles the five pages, remembers the last one, and tells lazy tabs
// (My Info, Applications) when they're shown via an "rr-tab-shown" event.
const TAB_STORE = "rr_active_tab";

function showTab(name) {
  document.querySelectorAll("#tabbar button").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab-page").forEach((p) =>
    p.classList.toggle("hidden", p.id !== "tab-" + name));
  chrome.storage.local.set({ [TAB_STORE]: name });
  document.dispatchEvent(new CustomEvent("rr-tab-shown", { detail: { tab: name } }));
}

document.querySelectorAll("#tabbar button").forEach((b) =>
  b.addEventListener("click", () => showTab(b.dataset.tab)));

// Always land on Docs when the panel opens — it's the most-used tab (grab a resume/cover to
// upload). This also fires rr-tab-shown so docs.js loads its list right away. (We no longer
// restore the last-used tab; the panel stays open across use, so this only affects fresh opens.)
showTab("docs");
