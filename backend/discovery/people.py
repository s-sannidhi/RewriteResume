"""Contact discovery (outreach Phase 3) — find and RANK the highest-value people to contact at a
startup, with shared-connection highlights. No emails yet (a later phase adds them); the reach-out
channel is each person's LinkedIn or GitHub profile.

Sources (all free, no scraping of gated sites):
  * YC  — founders (name, title, LinkedIn) from the company page (via contacts.enrich).
  * GitHub — org public members + top-repo contributors = the engineers actually building it
    (name, GitHub profile, location, company, bio). Bots filtered out.
  * HN  — the person who posted the hiring comment (handle → HN profile) as a lead.

Ranking = decision-making authority × relevance to the user (roadmap Phase 3):
  authority by role, adjusted by company size (small co → founder is the reachable decision-maker;
  large co → eng manager / recruiter). relevance from shared university, shared city, tech overlap.
"""
import re

import httpx

from . import contacts, emails, ranking
from .sources import _github_token

_UA = "Mozilla/5.0 AppleWebKit/537.36 Chrome/120"
_GH = "https://api.github.com"

# --- role classification ------------------------------------------------------
_RE_FOUNDER = re.compile(r"\b(founder|co-?founder|ceo|cto|chief)\b", re.I)
_RE_MANAGER = re.compile(r"\b(vp|head|director|manager|lead)\b.*\beng|eng.*\b(lead|manager|head)\b"
                         r"|engineering manager|em\b", re.I)
_RE_FOUNDING_ENG = re.compile(r"\bfounding engineer\b", re.I)
_RE_RECRUITER = re.compile(r"\brecruit|\btalent|people ops|\bsourcer|\bhr\b", re.I)


def _role_bucket(title: str) -> str:
    t = title or ""
    if _RE_FOUNDER.search(t):
        return "founder_cto"
    if _RE_RECRUITER.search(t):
        return "recruiter"
    if _RE_MANAGER.search(t):
        return "eng_manager"
    if _RE_FOUNDING_ENG.search(t) or re.search(r"\b(engineer|developer|swe|dev)\b", t, re.I):
        return "engineer"
    return "other"


def _authority(bucket: str, title: str, team_size) -> int:
    base = {"founder_cto": 95, "eng_manager": 78, "engineer": 48, "recruiter": 40, "other": 30}[bucket]
    if bucket == "engineer" and _RE_FOUNDING_ENG.search(title or ""):
        base = 72                                     # founding engineer: early, reachable, a peer
    elif bucket == "engineer" and re.search(r"\b(senior|staff|principal|lead)\b", title or "", re.I):
        base = 58
    small = not isinstance(team_size, int) or team_size <= 20
    if small:                                          # founder is THE target and reachable
        if bucket == "founder_cto":
            base += 5
        if bucket == "recruiter":
            base -= 10
    elif isinstance(team_size, int) and team_size > 75:  # established org
        if bucket == "founder_cto":
            base -= 15
        if bucket in ("eng_manager", "recruiter"):
            base += 8
    return max(0, min(100, base))


# --- relevance to the user ----------------------------------------------------
_SCHOOL_TERMS = None
_USER_CITY = re.compile(r"\baustin\b|,\s*tx\b|\btexas\b", re.I)


def _school_terms(profile: dict) -> list[str]:
    terms = set()
    for e in (profile.get("education") or []):
        s = (e.get("school") or "").lower()
        if s:
            terms.add(s)
            if "texas" in s and "austin" in s:
                terms.update(["ut austin", "university of texas", "utexas"])
            if "texas" in s and "dallas" in s:
                terms.update(["ut dallas", "utd"])
    return [t for t in terms if len(t) > 3]


def _relevance(person: dict, profile: dict, skills: set[str], schools: list[str],
               company: dict) -> tuple[int, list[str]]:
    score, notes = 0, []
    hay = " ".join([person.get("location") or "", person.get("company") or "",
                    person.get("bio") or "", company.get("location") or ""]).lower()

    if any(t in hay for t in schools):
        score += 30
        notes.append("Shared university")
    if _USER_CITY.search(hay):
        score += 15
        notes.append("Both in Austin/TX")

    bio_skills = [s for s in skills if len(s) > 2 and
                  re.search(r"(?<![a-z0-9])" + re.escape(s) + r"(?![a-z0-9])",
                            (person.get("bio") or "").lower() + " " +
                            " ".join(person.get("langs") or []).lower())]
    if bio_skills:
        score += min(5 * len(bio_skills), 15)
        notes.append("Shared tech: " + ", ".join(sorted(set(bio_skills), key=len, reverse=True)[:3]))

    if person.get("role_bucket") == "engineer" and (not isinstance(company.get("team_size"), int)
                                                     or company.get("team_size", 99) <= 20):
        score += 8
        notes.append("Early engineer — good networking peer")
    return score, notes


# --- GitHub people ------------------------------------------------------------
_BOT_RE = re.compile(r"\bbot\b|\[bot\]|-bot|actions|dependabot|renovate|-ci\b|release", re.I)


def _gh_headers() -> dict:
    h = {"User-Agent": _UA, "Accept": "application/vnd.github+json"}
    tok = _github_token()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _github_people(org: str, max_people: int = 6) -> list[dict]:
    """Org public members + top-repo contributors -> engineer/leadership leads with profile data.
    Best-effort; returns [] on rate-limit/error."""
    if not org:
        return []
    h = _gh_headers()
    logins: list[str] = []
    top_repo = ""
    try:
        r = httpx.get(f"{_GH}/orgs/{org}/public_members", headers=h, params={"per_page": 8}, timeout=20)
        if r.status_code == 200:
            logins += [m["login"] for m in r.json()]
        # top repo by stars, then its contributors
        rr = httpx.get(f"{_GH}/orgs/{org}/repos", headers=h,
                       params={"per_page": 20, "sort": "pushed"}, timeout=20)
        if rr.status_code == 200 and rr.json():
            top = max(rr.json(), key=lambda x: x.get("stargazers_count", 0))
            top_repo = top["name"]
            cr = httpx.get(f"{_GH}/repos/{org}/{top_repo}/contributors", headers=h,
                           params={"per_page": 10}, timeout=20)
            if cr.status_code == 200:
                logins += [c["login"] for c in cr.json()]
    except Exception:
        return []

    seen, people = set(), []
    for login in logins:
        if login.lower() in seen or _BOT_RE.search(login):
            continue
        seen.add(login.lower())
        prof = {}
        try:
            ur = httpx.get(f"{_GH}/users/{login}", headers=h, timeout=15)
            if ur.status_code == 200:
                prof = ur.json()
        except Exception:
            pass
        title = prof.get("bio") or "Engineer (GitHub contributor)"
        people.append({
            "name": prof.get("name") or login,
            "title": title[:80],
            "profile_url": prof.get("html_url") or f"https://github.com/{login}",
            "source": "GitHub",
            "location": prof.get("location") or "",
            "company": prof.get("company") or "",
            "bio": prof.get("bio") or "",
            "langs": [],
            "_gh_login": login,
            "_gh_repo": f"{org}/{top_repo}" if top_repo else "",
            "_gh_profile_email": prof.get("email") or "",   # reuse — avoids a 2nd /users fetch
        })
        if len(people) >= max_people:
            break
    return people


def _github_org_from(company: dict) -> str:
    """Derive a GitHub org login from a GitHub-source company or a YC page's github_url."""
    su = company.get("source_url") or ""
    if "github.com/" in su:
        return su.rstrip("/").split("/")[-1]
    gh = (company.get("intel") or {}).get("dossier", {}).get("links", {}).get("github_url", "") \
        if isinstance(company.get("intel"), dict) else ""
    if "github.com/" in (gh or ""):
        return gh.rstrip("/").split("/")[-1]
    return ""


# --- orchestration ------------------------------------------------------------
def discover_people(company: dict, profile: dict, use_github: bool = True) -> list[dict]:
    """Find, classify, and rank people to contact at one company. Returns a ranked list; the top
    entry is flagged is_recommended. use_github=False = light mode (YC founders + LinkedIn fallback
    only, no GitHub crawl) — used by the batch so a big run doesn't blow the GitHub rate limit."""
    skills = ranking.flatten_skills(profile)
    schools = _school_terms(profile)
    people: list[dict] = []

    # 1. YC founders (real names + LinkedIn) when this is a YC company page.
    src_url = company.get("source_url") or ""
    if "ycombinator.com/companies/" in src_url:
        enriched = contacts.enrich(src_url.rstrip("/").split("/")[-1])
        for f in enriched.get("founders", []):
            people.append({
                "name": f["name"], "title": f.get("title") or "Founder",
                "profile_url": f.get("linkedin") or contacts._linkedin_search(
                    company.get("name", ""), f["name"]),
                "source": "YC", "location": company.get("location") or "",
                "company": company.get("name") or "", "bio": "", "langs": [],
            })

    # 2. GitHub engineers/leads (this org, or the YC company's linked github_url).
    if use_github:
        org = _github_org_from(company)
        for p in _github_people(org):
            people.append(p)

    # 3. HN poster as a lead (handle only; low authority without a name).
    if (company.get("source") or "") == "HN" and "news.ycombinator.com/item" in src_url and not people:
        people.append({"name": "", "title": "Posted the hiring listing", "profile_url": src_url,
                       "source": "HN", "location": "", "company": company.get("name") or "",
                       "bio": "", "langs": []})

    # classify + score
    for p in people:
        bucket = _role_bucket(p.get("title", ""))
        p["role_bucket"] = bucket
        p["authority_score"] = _authority(bucket, p.get("title", ""), company.get("team_size"))
        rel, notes = _relevance(p, profile, skills, schools, company)
        p["relevance_score"] = rel
        p["relationship"] = notes
        p["score"] = round(p["authority_score"] * 0.65 + rel * 0.35)
        # A specific, real LinkedIn to reach out on — not a generic company/role search. YC founders
        # already carry a real (or name-searched) LinkedIn as profile_url. GitHub gives a real name
        # but no LinkedIn URL, so search by that exact name + company: precise enough that the right
        # profile is normally the top hit, unlike a bare "Company Recruiter" search.
        if p.get("source") == "YC":
            p["linkedin_url"] = p.get("profile_url", "")
        elif p.get("name"):
            p["linkedin_url"] = contacts._linkedin_search(company.get("name", "") or p.get("company", ""), p["name"])
        elif p.get("source") == "search":
            p["linkedin_url"] = p.get("profile_url", "")
        else:
            p["linkedin_url"] = ""
        p.pop("bio", None); p.pop("langs", None)

    # dedupe by (name or profile), rank, flag the top
    uniq, seen = [], set()
    for p in sorted(people, key=lambda x: (-x["score"], -x["authority_score"])):
        key = (p.get("name") or p.get("profile_url") or "").lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(p)
    # If we found no NAMED person, add a LinkedIn people-search lead so there's always a next step.
    if not any(p.get("name") for p in uniq):
        reco = contacts.recommend(company, [])
        uniq.append({
            "name": "", "title": "Search LinkedIn for their " + reco["contact_role_reco"],
            "role_bucket": _role_bucket(reco["contact_role_reco"]),
            "profile_url": reco["contact_search_url"], "linkedin_url": reco["contact_search_url"],
            "source": "search",
            "location": "", "company": company.get("name") or "", "relationship": [],
            "authority_score": 0, "relevance_score": 0, "score": 0,
        })

    # resolve real emails for the top contacts (free-first; bounded for GitHub rate limits)
    emails.resolve_for_people(uniq, company, top_n=6)
    for p in uniq:
        p.pop("_gh_login", None)
        p.pop("_gh_repo", None)
        p.pop("_gh_profile_email", None)

    for i, p in enumerate(uniq):
        p["is_recommended"] = (i == 0)
    return uniq
