// One owner for the debugger session per tab.
//
// Chrome allows a single debugger session per target, so every feature that needs CDP (document
// attach, smart upload, the Workday fillers, the Finder-replacement picker) is competing for the
// same thing. They each used to attach and then unconditionally detach when finished, which was
// fine while they ran one at a time and broke the moment the file picker started holding a session
// open: the next feature's detach silently tore down the picker, and the picker's session made the
// next feature's attach throw "already attached", which several call sites treated as fatal.
//
// The rule here is simple and applies everywhere:
//   acquire() attaches only if nothing is attached yet, and never fails just because it is.
//   release() detaches only if WE opened the session. If the picker has this tab armed, the
//            session belongs to it and is left alone.
(() => {
  // Tabs where this panel opened the session and is therefore responsible for closing it.
  const owned = new Set();

  async function isAttached(tabId) {
    try {
      if (!(chrome.debugger && chrome.debugger.getTargets)) return false;
      const targets = await chrome.debugger.getTargets();
      return targets.some((t) => t.tabId === tabId && t.attached);
    } catch (e) { return false; }
  }

  async function pickerArmed(tabId) {
    try {
      const r = await chrome.runtime.sendMessage({ type: "pickerState", tabId });
      return !!(r && r.armed);
    } catch (e) { return false; }   // background asleep or not listening: assume not armed
  }

  /** Ensure a debugger session exists for this tab. Returns {ok, error, owned}. */
  async function acquire(tabId) {
    if (!(chrome.debugger && chrome.debugger.attach)) {
      return { ok: false, error: "this browser has no debugger API (use Firefox file-attach instead)" };
    }
    if (await isAttached(tabId)) return { ok: true, owned: false };
    try {
      await chrome.debugger.attach({ tabId }, "1.3");
      owned.add(tabId);
      return { ok: true, owned: true };
    } catch (e) {
      const msg = String((e && e.message) || e);
      // Someone attached between the check and here — that's success for our purposes.
      if (/already attached/i.test(msg)) return { ok: true, owned: false };
      if (/devtools/i.test(msg) || /Cannot attach/i.test(msg)) {
        return { ok: false, error: "couldn't attach debugger (DevTools open on this tab?)" };
      }
      return { ok: false, error: msg.slice(0, 90) };
    }
  }

  /** Close the session only if we opened it AND the picker isn't relying on it. */
  async function release(tabId) {
    if (await pickerArmed(tabId)) { owned.delete(tabId); return; }
    if (!owned.has(tabId)) return;      // not ours to close
    owned.delete(tabId);
    try { await chrome.debugger.detach({ tabId }); } catch (e) {}
  }

  window.rrCdpAcquire = acquire;
  window.rrCdpRelease = release;
})();
