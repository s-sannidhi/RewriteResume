"""Recommended person to contact — free, from the YC company page (no key, no paid API).

The company detail page (ycombinator.com/companies/<slug>) is an Inertia app whose root element
carries a `data-page` JSON blob with the company's founders (full_name, title, linkedin_url) and
its open jobPostings. We parse that. When a company has no listed founder (or no slug), we fall
back to a role recommendation + a prebuilt LinkedIn people-search URL so the "contact" column is
never empty.

Enrichment is a per-company fetch, so callers run it lazily (top-ranked / on demand), never as an
N-wide blind sweep.
"""
import html as _html
import json
import re
from urllib.parse import quote_plus

import httpx

_UA = "Mozilla/5.0 AppleWebKit/537.36 Chrome/120"


def _find_founders(obj, depth: int = 0):
    """Depth-first hunt for a `founders` list in the Inertia props blob."""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        f = obj.get("founders")
        if isinstance(f, list) and f:
            return f
        for v in obj.values():
            r = _find_founders(v, depth + 1)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_founders(v, depth + 1)
            if r:
                return r
    return None


def enrich(slug: str) -> dict:
    """Fetch founders + open-role count for a YC company slug. Best-effort — returns
    {founders: [{name,title,linkedin}], job_count: int}. Empty on any failure."""
    if not slug:
        return {"founders": [], "job_count": 0}
    url = f"https://www.ycombinator.com/companies/{slug}"
    try:
        html = httpx.get(url, headers={"User-Agent": _UA}, timeout=25, follow_redirects=True).text
        m = re.search(r'data-page="([^"]+)"', html)
        if not m:
            return {"founders": [], "job_count": 0}
        blob = json.loads(_html.unescape(m.group(1)))
        props = blob.get("props", {})
        founders = _find_founders(props) or []
        out = []
        for f in founders:
            name = (f.get("full_name") or " ".join(
                x for x in (f.get("first_name"), f.get("last_name")) if x) or "").strip()
            if not name:
                continue
            out.append({
                "name": name,
                "title": (f.get("title") or "Founder").strip(),
                "linkedin": f.get("linkedin_url") or f.get("linkedin") or "",
            })
        jobs = props.get("jobPostings")
        job_count = len(jobs) if isinstance(jobs, list) else 0
        return {"founders": out, "job_count": job_count}
    except Exception:
        return {"founders": [], "job_count": 0}


def _linkedin_search(company_name: str, role: str) -> str:
    q = " ".join(x for x in (company_name, role) if x)
    return "https://www.linkedin.com/search/results/people/?keywords=" + quote_plus(q)


def recommend(company: dict, founders: list[dict]) -> dict:
    """Pick the best person to reach (technical founder/CTO first) and a fallback search link."""
    name = company.get("name", "")
    if founders:
        # Prefer a technical founder / CTO; else the first founder.
        pick = next((f for f in founders
                     if re.search(r"cto|technical|engineer", (f.get("title") or ""), re.I)),
                    founders[0])
        return {
            "contact_name": pick["name"],
            "contact_title": pick.get("title") or "Founder",
            "contact_linkedin": pick.get("linkedin") or _linkedin_search(name, pick["name"]),
            "contact_role_reco": "founder",
            "contact_search_url": _linkedin_search(name, "founder engineering"),
        }
    # No listed founder — recommend a role and a people-search to run. Established companies and
    # regular internship postings skew toward a recruiter / eng manager; small startups → the
    # technical founder.
    ts = company.get("team_size")
    intern = isinstance(company.get("raw"), dict) and company["raw"].get("intern")
    if intern or "Simplify" in (company.get("source") or "") or (isinstance(ts, int) and ts > 40):
        role = "University Recruiter / Engineering Manager"
    else:
        role = "Founder / CTO"
    return {
        "contact_name": "",
        "contact_title": role,
        "contact_linkedin": "",
        "contact_role_reco": role,
        "contact_search_url": _linkedin_search(name, role),
    }
