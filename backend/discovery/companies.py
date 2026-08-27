"""Bridge arbitrary companies (esp. Simplify internships) into the discovery/contacts/outreach
pipeline. A Simplify job gives only a company NAME + a Simplify redirect URL, but contact and email
discovery need a domain — so we resolve name -> domain via Clearbit's free, keyless autocomplete.

This lets regular internships flow through the exact same contacts + message pipeline as the
YC/HN/GitHub startups: find people, resolve emails, draft outreach.
"""
import re

import httpx

from . import normalize

_UA = "Mozilla/5.0 Chrome/120"
_domain_cache: dict[str, tuple[str, str]] = {}
_GENERIC = re.compile(r"\b(inc|llc|corp|co|ltd|the|group|technologies|labs|systems)\b", re.I)
_org_cache: dict[str, str] = {}


def resolve_domain(name: str) -> tuple[str, str]:
    """(domain, canonical_name) for a company name via Clearbit autocomplete. ('', name) on miss.
    Cached per name."""
    key = (name or "").strip().lower()
    if not key:
        return ("", name)
    if key in _domain_cache:
        return _domain_cache[key]
    out = ("", name)
    try:
        r = httpx.get("https://autocomplete.clearbit.com/v1/companies/suggest",
                      params={"query": name}, headers={"User-Agent": _UA}, timeout=12)
        if r.status_code == 200 and r.json():
            hits = r.json()
            # prefer the hit whose name best matches (autocomplete's first is usually right)
            base = _GENERIC.sub("", key).strip()
            pick = next((h for h in hits if base and base in (h.get("name") or "").lower()), hits[0])
            out = (normalize.domain_of(pick.get("domain") or ""), pick.get("name") or name)
    except Exception:
        pass
    _domain_cache[key] = out
    return out


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def guess_github_org(name: str, domain: str) -> str:
    """Best-effort GitHub org login for an arbitrary company (not just YC/GitHub-source ones), so
    the same real-people discovery (discover_people) can run for a company you just applied to.
    One search call + one verification fetch on the top candidate; '' if nothing checks out —
    callers fall back to the LinkedIn-search-only path rather than guess wrong. Free, keyless
    (a token in the environment just raises the rate limit)."""
    key = (name or "").strip().lower()
    if not key:
        return ""
    if key in _org_cache:
        return _org_cache[key]
    from .sources import _github_token
    headers = {"User-Agent": _UA, "Accept": "application/vnd.github+json"}
    tok = _github_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    out = ""
    try:
        r = httpx.get("https://api.github.com/search/users", headers=headers, timeout=15,
                      params={"q": f"{name} type:org", "per_page": 3})
        if r.status_code == 200:
            target = _slug(name)
            for cand in r.json().get("items", []):
                login = cand.get("login") or ""
                if not login or _slug(login) not in target and target not in _slug(login):
                    continue
                # verify: the org's own site (blog) matches the resolved company domain, or its
                # login is (near-)exactly the company name — avoids grabbing an unrelated org that
                # merely shares a common word.
                ur = httpx.get(f"https://api.github.com/users/{login}", headers=headers, timeout=15)
                if ur.status_code != 200:
                    continue
                prof = ur.json()
                blog = normalize.domain_of(prof.get("blog") or "")
                if (domain and blog and blog == domain) or _slug(login) == target:
                    out = login
                break
    except Exception:
        pass
    _org_cache[key] = out
    return out


def intern_to_company(job: dict) -> dict:
    """Convert a Simplify internship (from scrape_interns) into a normalized company row for the
    startups/companies store, tagged source='Simplify'. Carries the intern fit score so it ranks
    sensibly in the outreach list."""
    name = job.get("company", "") or ""
    domain, canon = resolve_domain(name)
    title = job.get("title", "") or ""
    return {
        "name": canon or name,
        "website": f"https://{domain}" if domain else "",
        "domain": domain,
        "id": normalize.company_id(domain, name),
        "one_liner": title,
        "description": f"Hiring: {title}. {', '.join(job.get('functions') or [])}".strip(),
        "team_size": None, "stage": "", "batch": "",
        "industry": "", "subindustry": "",
        "tags": job.get("functions") or [],
        "regions": (["Remote"] if (job.get("work_model") == "Remote") else []),
        "location": job.get("location", "") or "",
        "is_hiring": True,
        "hiring_signals": [f"Open internship: {title}"] if title else ["Open internship"],
        "source": "Simplify",
        "source_url": job.get("apply_url", "") or "",
        "fit_score": int(job.get("fit_score", 55)),
        "fit_reason": job.get("fit_reason", "") or (f"Internship match: {title}" if title else ""),
        "hiring_score": 60, "hiring_label": "warm",
        "raw": {"intern": True, "majors": job.get("majors") or []},
    }
