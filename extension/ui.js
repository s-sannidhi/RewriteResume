// Panel size control — zooms the whole side panel bigger/smaller and remembers it.
// Chromium honors `zoom` on the root element, so one knob scales text, padding, and buttons
// together. A−/A+ step by 10%; clicking the % label resets to 100%.
(() => {
  const KEY = "rr_ui_scale";
  const MIN = 0.8, MAX = 1.5, STEP = 0.1;
  let scale = 1;
  const clamp = (v) => Math.min(MAX, Math.max(MIN, Math.round(v * 100) / 100));

  function apply() {
    const root = document.documentElement;
    root.style.zoom = String(scale);
    // Firefox 126+ honors zoom; older builds ignore it — scale via transform as a fallback.
    if (typeof CSS === "undefined" || !CSS.supports || !CSS.supports("zoom", "1")) {
      root.style.transform = "scale(" + scale + ")";
      root.style.transformOrigin = "top left";
      root.style.width = (100 / scale) + "%";
    } else {
      root.style.transform = "";
      root.style.width = "";
    }
    const lab = document.getElementById("scaleLabel");
    if (lab) lab.textContent = Math.round(scale * 100) + "%";
  }
  function set(v) {
    scale = clamp(v); apply();
    try { chrome.storage.local.set({ [KEY]: scale }); } catch (e) {}
  }
  function wire() {
    const d = document.getElementById("scaleDown");
    const u = document.getElementById("scaleUp");
    const lab = document.getElementById("scaleLabel");
    if (d) d.addEventListener("click", () => set(scale - STEP));
    if (u) u.addEventListener("click", () => set(scale + STEP));
    if (lab) lab.addEventListener("click", () => set(1));
    apply();
  }

  chrome.storage.local.get(KEY).then((r) => {
    const v = r && r[KEY];
    if (typeof v === "number") scale = clamp(v);
    apply();
  }).catch(() => {});

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", wire);
  else wire();
})();
