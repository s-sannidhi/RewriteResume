"""Resolve the REAL application URL behind each Jobright "Apply Now" button.

Jobright hides the true ATS link behind a click (and often a login), so we drive a real browser:
open each job, click the top-right Apply Now / Apply With Autofill button, decline the autofill
popup if it appears, and capture wherever it lands (the company's application site).

Jobright gates apply behind an account. We use a DEDICATED, isolated browser profile
(~/ResumeRewriter/.jobright_profile) — never your real Chrome, because pointing automation at
your real profile makes Chrome/Google log your account out and flag the tab. Log into Jobright
ONCE in this separate profile; it stays logged in afterwards and never touches your Chrome.

  1) .venv/bin/python scripts/resolve_apply.py --login     # opens Jobright, WAITS for you
  2) .venv/bin/python scripts/resolve_apply.py             # resolves today's list
     .venv/bin/python scripts/resolve_apply.py --headless  # once it's working

Chains off scrape_interns.py by default (today's Austin/Remote jobs), or pass job URLs as args.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scrape_interns import scrape  # noqa: E402

from playwright.sync_api import sync_playwright  # noqa: E402

PROFILE_DIR = str(Path.home() / "ResumeRewriter" / ".jobright_profile")


def _chrome_user_data_dir() -> str:
    """Chrome's user-data directory on this OS."""
    if sys.platform == "darwin":
        return str(Path.home() / "Library" / "Application Support" / "Google" / "Chrome")
    if os.name == "nt":
        return str(Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Google" / "Chrome" / "User Data")
    return str(Path.home() / ".config" / "google-chrome")


CHROME_DIR = _chrome_user_data_dir()
# Which Chrome profile directory is signed in to the job board. Override with --profile "…".
CHROME_PROFILE = os.environ.get("RR_CHROME_PROFILE", "Default")
OUT_FILE = Path.home() / "ResumeRewriter" / "apply_links.txt"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")

# Buttons to try, in order (top-right on the job page). Playwright :has-text() is
# case-insensitive + substring, so "APPLY NOW" matches "apply now".
APPLY_SELECTORS = [
    "button:has-text('apply now')",
    "a:has-text('apply now')",
    "button:has-text('apply with autofill')",
    "button:has-text('apply')",
    "a:has-text('apply')",
]
# If the autofill offer pops up, decline so it forwards to the company site.
DECLINE_SELECTORS = [
    "button:has-text('company site')", "a:has-text('company site')",
    "button:has-text('no thanks')", "button:has-text('skip')",
    "button:has-text('continue')", "button:has-text('manually')",
    "button:has-text('apply directly')",
]


def _first_visible(pg, selectors):
    for s in selectors:
        loc = pg.locator(s)
        try:
            if loc.count() and loc.first.is_visible():
                return loc.first
        except Exception:
            pass
    return None


def _resolve_one(ctx, url: str) -> str:
    pg = ctx.new_page()
    popups = []
    ctx.on("page", lambda np: popups.append(np))   # the apply site usually opens in a new tab
    try:
        pg.goto(url, wait_until="domcontentloaded", timeout=45000)
        pg.wait_for_timeout(3500)
        btn = _first_visible(pg, APPLY_SELECTORS)
        if not btn:
            return "NO_APPLY_BUTTON"
        try: btn.scroll_into_view_if_needed(timeout=3000)
        except Exception: pass
        clicked = False
        for kw in ({"timeout": 8000}, {"timeout": 4000, "force": True}):
            try:
                btn.click(**kw); clicked = True; break
            except Exception:
                pass
        if not clicked:
            try: btn.evaluate("e => e.click()"); clicked = True
            except Exception as e: return "CLICK_FAILED: " + str(e)[:50]
        pg.wait_for_timeout(2500)
        # decline the autofill offer if it appeared
        d = _first_visible(pg, DECLINE_SELECTORS)
        if d:
            try: d.click(timeout=3000)
            except Exception: pass
        pg.wait_for_timeout(2500)
        # let any popup finish navigating to its real destination, then read its URL
        for np in popups:
            try:
                np.wait_for_load_state("domcontentloaded", timeout=8000)
                np.wait_for_timeout(1200)
            except Exception:
                pass
        dests = [np.url for np in popups if np.url and "jobright" not in np.url
                 and not np.url.startswith("about:")]
        if dests:
            return dests[-1]
        if "jobright" not in pg.url:
            return pg.url
        return "LOGIN_OR_BLOCKED"   # likely needs a Jobright sign-in in this profile
    except Exception as e:
        return "ERROR: " + str(e)[:70]
    finally:
        for np in popups + [pg]:
            try: np.close()
            except Exception: pass


def _make_context(p, headless: bool, use_chrome: bool = False, profile: str = CHROME_PROFILE):
    # ISOLATED profile only — never the user's real Chrome dir (that gets their Google account
    # logged out + the tab flagged when Chrome sees the automation flags). We use the real
    # Chrome *binary* (channel="chrome") in a SEPARATE user-data-dir, with the automation
    # fingerprint softened, so a one-time Jobright Google login is more likely to go through.
    return p.chromium.launch_persistent_context(
        PROFILE_DIR, headless=headless, channel="chrome", no_viewport=True,
        args=["--no-first-run", "--no-default-browser-check",
              "--disable-blink-features=AutomationControlled"])


def _opt(argv, name, default):
    """Read `--name value` from argv."""
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return default


def main() -> None:
    argv = sys.argv[1:]
    profile = _opt(argv, "--profile", CHROME_PROFILE)
    # positional args = job URLs (exclude the --profile value)
    args = [a for a in argv if not a.startswith("--") and a != profile]
    headless = "--headless" in argv

    # --login: open Jobright in the ISOLATED profile and WAIT for you to sign in. This profile
    # is separate from your real Chrome, so logging in here can't touch your Google account.
    if "--login" in argv:
        with sync_playwright() as p:
            ctx = _make_context(p, headless=False)
            pg = ctx.pages[0] if ctx.pages else ctx.new_page()
            pg.goto("https://jobright.ai/", wait_until="domcontentloaded")
            input("\nA browser window opened (a separate profile — NOT your real Chrome). "
                  "Log into Jobright there, then come back here and press Enter to save it… ")
            try:
                pg.wait_for_timeout(1500)   # let cookies flush to the profile
                ctx.close()
            except Exception:
                pass   # Playwright can throw on cleanup after login redirects; session is saved
            print("Saved. Now run:  .venv/bin/python scripts/resolve_apply.py")
        return

    if args:
        jobs = [{"company": "", "title": "", "apply_url": u} for u in args]
    else:
        print("Getting today's list from scrape_interns…")
        jobs = scrape()
    if not jobs:
        print("No jobs to resolve.")
        return

    print(f"Resolving {len(jobs)} apply links "
          f"({'headless' if headless else 'headed'}, isolated profile)…\n")
    results = []
    with sync_playwright() as p:
        try:
            ctx = _make_context(p, headless)
        except Exception as e:
            print("Couldn't open the browser profile:", e)
            return
        for j in jobs:
            dest = _resolve_one(ctx, j["apply_url"])
            label = (j.get("company") or "") + (" — " + j["title"] if j.get("title") else "")
            print(f"  {label or j['apply_url']}\n     -> {dest}")
            results.append((label, j["apply_url"], dest))
        try: ctx.close()
        except Exception: pass

    ok = [r for r in results if r[2].startswith("http")]
    OUT_FILE.write_text(
        "\n".join(f"{lbl}\n{dest}\n(via {src})\n" for lbl, src, dest in results), encoding="utf-8")
    print(f"\nResolved {len(ok)}/{len(results)} to real application links. Saved to {OUT_FILE}")
    if len(ok) < len(results):
        print("Any 'LOGIN_OR_BLOCKED' means the isolated profile isn't signed into Jobright — "
              "run:  .venv/bin/python scripts/resolve_apply.py --login")


if __name__ == "__main__":
    main()
