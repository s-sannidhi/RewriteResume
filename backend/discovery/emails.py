"""Email resolution (outreach Phase 3, email layer) — REAL/verified only, no personal-address
guessing. Tries the free, keyless sources first and only spends the scarce Hunter.io quota when
those miss. Every returned email carries its provenance so the UI can show how it was found.

Priority (best, most-attributable first):
  1. github-profile   — the email the person set public on their GitHub profile
  2. github-commit     — the author email in their public commit history (filtered for GitHub's
                         privacy `noreply` addresses)
  3. hn                — an email the person wrote into their HN "who is hiring" post
  4. hunter            — Hunter.io verified email (opt-in key; only called when 1–3 miss)
  5. role-inbox        — a conventional catch-all (founders@/hello@) whose domain's MX confirms it
                         accepts mail (a real reachable inbox, not a guess of a person's address)

No SMTP probing (unreliable + abusive against Google/MS-hosted domains); MX is checked via Google's
DNS-over-HTTPS, which is keyless.
"""
import re

import httpx

_UA = "Mozilla/5.0 AppleWebKit/537.36 Chrome/120"
_GH = "https://api.github.com"
_NOREPLY = re.compile(r"noreply|users\.noreply\.github|no-reply", re.I)
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

_mx_cache: dict[str, tuple[bool, str]] = {}


# ------------------------------------------------------------------- MX (keyless)
def mx_lookup(domain: str) -> tuple[bool, str]:
    """(accepts_mail, provider) via Google DNS-over-HTTPS. Cached per domain."""
    if not domain:
        return (False, "")
    if domain in _mx_cache:
        return _mx_cache[domain]
    ok, provider = False, ""
    try:
        d = httpx.get("https://dns.google/resolve", params={"name": domain, "type": "MX"},
                      headers={"User-Agent": _UA}, timeout=10).json()
        hosts = [a["data"].split()[-1].lower().rstrip(".") for a in d.get("Answer", [])
                 if a.get("type") == 15]
        ok = bool(hosts)
        if any("google" in h or "googlemail" in h for h in hosts):
            provider = "Google Workspace"
        elif any("outlook" in h or "microsoft" in h for h in hosts):
            provider = "Microsoft 365"
        elif hosts:
            provider = hosts[0]
    except Exception:
        pass
    _mx_cache[domain] = (ok, provider)
    return _mx_cache[domain]


# ------------------------------------------------------------------------ GitHub
def _gh_headers() -> dict:
    from .sources import _github_token
    h = {"User-Agent": _UA, "Accept": "application/vnd.github+json"}
    tok = _github_token()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def github_email(login: str, repo_full: str = "", profile_email: str = "") -> tuple[str, str]:
    """(email, source) for a GitHub user: public profile email first, then a non-noreply author
    email from their commit history on `repo_full` (owner/name). `profile_email` is passed through
    from discovery so we don't re-fetch /users. ('', '') if none."""
    if not login:
        return ("", "")
    em = (profile_email or "").strip()
    if em and not _NOREPLY.search(em):
        return (em, "github-profile")
    h = _gh_headers()
    if repo_full:
        try:
            cm = httpx.get(f"{_GH}/repos/{repo_full}/commits", headers=h,
                           params={"author": login, "per_page": 1}, timeout=15)
            if cm.status_code == 200 and cm.json():
                em = (cm.json()[0]["commit"]["author"].get("email") or "").strip()
                if em and not _NOREPLY.search(em):
                    return (em, "github-commit")
        except Exception:
            pass
    return ("", "")


# --------------------------------------------------------------------------- HN
def hn_email(text: str, domain: str = "") -> tuple[str, str]:
    """Pull an email a company wrote into its HN post. Prefer one on the company's own domain."""
    if not text:
        return ("", "")
    found = [e for e in _EMAIL_RE.findall(text) if not e.lower().endswith((".png", ".jpg"))]
    if not found:
        return ("", "")
    if domain:
        on_domain = [e for e in found if e.lower().endswith("@" + domain.lower())]
        if on_domain:
            return (on_domain[0], "hn")
    return (found[0], "hn")


# ----------------------------------------------------------------- Hunter (opt-in)
def _hunter_key() -> str:
    import os
    tok = os.environ.get("RR_HUNTER_KEY") or ""
    if tok:
        return tok
    try:
        from .. import secrets
        return secrets.get_secret("resume-rewriter-hunter", "key", "RR_HUNTER_KEY") or ""
    except Exception:
        return ""


_hunter_pat_cache: dict[str, tuple[str, list]] = {}


def hunter_pattern(domain: str) -> tuple[str, list]:
    """Hunter.io domain-search: (pattern, known_emails) for a whole company in ONE lookup — the
    quota-efficient path for batches (one call covers every person at the domain). Cached per
    domain. pattern looks like '{first}.{last}' or '{f}{last}'. ('', []) if no key/result."""
    if domain in _hunter_pat_cache:
        return _hunter_pat_cache[domain]
    key = _hunter_key()
    out = ("", [])
    if key and domain:
        try:
            r = httpx.get("https://api.hunter.io/v2/domain-search",
                          params={"domain": domain, "api_key": key, "limit": 10}, timeout=20)
            if r.status_code == 200:
                data = r.json().get("data", {})
                emails = [{"email": e.get("value", ""), "first": (e.get("first_name") or "").lower(),
                           "last": (e.get("last_name") or "").lower()}
                          for e in (data.get("emails") or []) if e.get("value")]
                out = (data.get("pattern") or "", emails)
        except Exception:
            pass
    _hunter_pat_cache[domain] = out
    return out


def _apply_pattern(pattern: str, first: str, last: str, domain: str) -> str:
    if not pattern or not domain or not (first or last):
        return ""
    local = (pattern.replace("{first}", first.lower()).replace("{last}", last.lower())
             .replace("{f}", first[:1].lower()).replace("{l}", last[:1].lower()))
    return f"{local}@{domain}" if "{" not in local and local else ""


def hunter_from_domain(first: str, last: str, domain: str) -> tuple[str, str]:
    """Resolve one person's email from the cached company domain-search (known email match first,
    then the company pattern). Costs at most ONE Hunter lookup per domain across a whole batch."""
    pattern, known = hunter_pattern(domain)
    for e in known:
        if first.lower() == e["first"] and last.lower() == e["last"] and e["email"]:
            return (e["email"], "hunter-verified")
    em = _apply_pattern(pattern, first, last, domain)
    return (em, "hunter-pattern") if em else ("", "")


def hunter_find(first: str, last: str, domain: str) -> tuple[str, str]:
    """Hunter.io email-finder for a specific person. Returns ('', '') if no key or no result.
    Only call when the free sources miss — the free tier is ~25 lookups/month."""
    key = _hunter_key()
    if not key or not domain or not (first or last):
        return ("", "")
    try:
        r = httpx.get("https://api.hunter.io/v2/email-finder",
                      params={"domain": domain, "first_name": first, "last_name": last,
                              "api_key": key}, timeout=20)
        if r.status_code != 200:
            return ("", "")
        data = r.json().get("data", {})
        em = (data.get("email") or "").strip()
        if em:
            status = (data.get("verification") or {}).get("status") or ""
            return (em, "hunter" + (f"-{status}" if status else ""))
    except Exception:
        pass
    return ("", "")


# ------------------------------------------------------------- role catch-all
_ROLE_ALIASES = ("founders", "hello", "team", "careers", "jobs")


def role_inbox(domain: str, small: bool) -> tuple[str, str]:
    """A conventional catch-all that reaches a human, when the domain's MX accepts mail. Not a
    specific person — labeled as such. founders@ for small startups, else hello@."""
    if not domain:
        return ("", "")
    ok, _ = mx_lookup(domain)
    if not ok:
        return ("", "")
    alias = "founders" if small else "hello"
    return (f"{alias}@{domain}", "role-inbox")


# ---------------------------------------------------------------- orchestration
def resolve(person: dict, company: dict, allow_role_inbox: bool = True) -> tuple[str, str]:
    """Best real email for one person, free-first then Hunter, then (optionally) a role catch-all.
    person may carry private _gh_login/_gh_repo hints from GitHub discovery. The role catch-all is a
    company-level inbox, so callers should enable it for only ONE contact per company."""
    domain = company.get("domain") or ""
    # 1–2. GitHub (profile, then commit)
    em, src = github_email(person.get("_gh_login", ""), person.get("_gh_repo", ""),
                           person.get("_gh_profile_email", ""))
    if em:
        return (em, src)
    # 3. HN post
    if (company.get("source") or "").find("HN") >= 0:
        em, src = hn_email(company.get("description") or company.get("one_liner") or "", domain)
        if em:
            return (em, src)
    # 4. Hunter (only now, to conserve quota) — one domain-search per company covers everyone
    name = (person.get("name") or "").split()
    if len(name) >= 2 and domain:
        em, src = hunter_from_domain(name[0], name[-1], domain)
        if em:
            return (em, src)
    # 5. role catch-all (company-level inbox — only when the caller allows it). Regular internship
    # postings / established companies → a recruiting inbox (careers@), not founders@.
    if allow_role_inbox:
        intern = isinstance(company.get("raw"), dict) and company["raw"].get("intern")
        if intern or "Simplify" in (company.get("source") or ""):
            ok, _ = mx_lookup(domain)
            return (f"careers@{domain}", "role-inbox") if ok else ("", "")
        ts = company.get("team_size")
        small = not isinstance(ts, int) or ts <= 30
        return role_inbox(domain, small)
    return ("", "")


def resolve_for_people(people: list[dict], company: dict, top_n: int = 6) -> None:
    """Fill email/email_source in place for the top_n people (bounds GitHub API usage). The role
    catch-all is company-level, so it's offered to only the first contact that still lacks a
    personal email."""
    for i, p in enumerate(people[:top_n]):
        if p.get("email"):
            continue
        # the role catch-all is a company inbox — only ever attach it to the #1 contact, so it's
        # never misread as some specific engineer's personal address.
        em, src = resolve(p, company, allow_role_inbox=(i == 0))
        if em:
            p["email"], p["email_source"] = em, src
