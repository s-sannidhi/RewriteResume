// Place a local file into a page <input type=file> without chrome.debugger.
//
// Chrome can feed a real disk path through CDP (DOM.setFileInputFiles). Firefox has no debugger
// API, so we fetch the file from the local backend and assign a script-created File via
// DataTransfer — which both browsers accept. Callers still prefer CDP on Chrome when it exists.

(() => {
  const FILES_API = "http://127.0.0.1:8765";

  function rrHasDebugger() {
    return !!(typeof chrome !== "undefined" && chrome.debugger && chrome.debugger.sendCommand);
  }

  function rrIsFirefox() {
    return typeof navigator !== "undefined" && /firefox/i.test(navigator.userAgent || "");
  }

  // Chrome allows sub-minute alarm periods; Firefox's minimum is 1 minute.
  function rrAlarmPeriod(minutes) {
    if (rrHasDebugger()) return minutes;
    return Math.max(1, minutes);
  }

  async function rrPlaceFileBlob(tabId, index, absPath) {
    const url = FILES_API + "/documents/raw?path=" + encodeURIComponent(absPath);
    let r;
    try { r = await fetch(url); }
    catch (e) { return { ok: false, error: "couldn't fetch the file (is the backend running?)" }; }
    if (!r.ok) return { ok: false, error: "backend wouldn't serve that file" };
    const buf = new Uint8Array(await r.arrayBuffer());
    if (!buf.length) return { ok: false, error: "file was empty" };
    if (buf.length > 8 * 1024 * 1024) return { ok: false, error: "file is too large to inject (over 8 MB)" };
    const name = String(absPath).split(/[/\\]/).pop() || "file";
    const mime = r.headers.get("content-type") || "application/octet-stream";
    let binary = "";
    for (let i = 0; i < buf.length; i += 0x8000) {
      binary += String.fromCharCode.apply(null, buf.subarray(i, i + 0x8000));
    }
    const b64 = btoa(binary);
    try {
      const [res] = await chrome.scripting.executeScript({
        target: { tabId },
        func: (index, b64, name, mime) => {
          const bin = atob(b64);
          const arr = new Uint8Array(bin.length);
          for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
          const file = new File([arr], name, { type: mime });
          const dt = new DataTransfer();
          dt.items.add(file);
          const want = String(index);
          const walk = (root, depth) => {
            if (!root || depth > 12) return null;
            let els = [];
            try { els = [...root.querySelectorAll("*")]; } catch (e) { return null; }
            for (const el of els) {
              if (el.tagName === "INPUT" && el.getAttribute && el.getAttribute("data-rr-upload") === want)
                return el;
              if (el.shadowRoot) {
                const hit = walk(el.shadowRoot, depth + 1);
                if (hit) return hit;
              }
            }
            return null;
          };
          let el = walk(document, 0);
          if (!el) {
            const all = [];
            (function collect(root, depth) {
              if (!root || depth > 12) return;
              let els = [];
              try { els = [...root.querySelectorAll("*")]; } catch (e) { return; }
              for (const n of els) {
                if (n.tagName === "INPUT" && (n.type || "").toLowerCase() === "file") all.push(n);
                if (n.shadowRoot) collect(n.shadowRoot, depth + 1);
              }
            })(document, 0);
            el = all[index];
          }
          if (!el) return { ok: false, error: "upload field is gone" };
          try { el.files = dt.files; }
          catch (e) { return { ok: false, error: "page blocked the file assignment" }; }
          el.dispatchEvent(new Event("input", { bubbles: true }));
          el.dispatchEvent(new Event("change", { bubbles: true }));
          return { ok: !!(el.files && el.files.length) };
        },
        args: [index, b64, name, mime],
      });
      const out = res && res.result;
      if (out && out.ok) return { ok: true };
      return { ok: false, error: (out && out.error) || "the page rejected the file" };
    } catch (e) {
      return { ok: false, error: String((e && e.message) || e).slice(0, 80) };
    }
  }

  const api = { rrHasDebugger, rrIsFirefox, rrAlarmPeriod, rrPlaceFileBlob };
  if (typeof window !== "undefined") Object.assign(window, api);
  else Object.assign(self, api);
})();
