"""Company intelligence (outreach Phase 2).

Two steps, keeping the house rule "Python owns facts, the LLM only writes prose":
  1. gather(company)  — assemble a FACTUAL dossier from free sources (YC company page, GitHub org
     repos, the HN post text, and a keyless Google-News lookup). No LLM here.
  2. why_this_company(company, dossier, profile) — local gemma3:12b turns the dossier + the user's
     real skills/projects/experience into a concise "Why this company?" that names ONE genuine
     connection (never a generic "I'm a CS student looking for an internship").

The dossier fills the Phase-2 checklist: description/product, founders + eng leadership, team size,
tech stack, recent launches, recent hiring, funding/news, recent announcements, and why the user's
experience is relevant.
"""
import html as _html
import json
import re
import time
from urllib.parse import quote_plus

import httpx

from .. import llm
from ..discovery import contacts, ranking

_UA = "Mozilla/5.0 AppleWebKit/537.36 Chrome/120"


# --------------------------------------------------------------------------- YC
def _yc_dossier(slug: str) -> dict:
    """Pull founders, launches, news (incl. funding), eng roles, tech tags from the YC page."""
    if not slug:
        return {}
    try:
        html = httpx.get(f"https://www.ycombinator.com/companies/{slug}", headers={"User-Agent": _UA},
                         timeout=25, follow_redirects=True).text
        m = re.search(r'data-page="([^"]+)"', html)
        if not m:
            return {}
        props = json.loads(_html.unescape(m.group(1))).get("props", {})
        comp = props.get("company", {}) or {}
        founders = [{"name": f.get("full_name") or "", "title": f.get("title") or "Founder",
                     "linkedin": f.get("linkedin_url") or ""}
                    for f in (comp.get("founders") or []) if f.get("full_name")]
        launches = [l.get("title") or l.get("name") or "" for l in (props.get("launches") or [])]
        news = [{"title": n.get("title") or "", "date": n.get("date") or ""}
                for n in (props.get("newsItems") or [])]
        jobs = props.get("jobPostings") or []
        eng_roles = [j.get("title") or "" for j in jobs if (j.get("role") or "") == "eng"]
        return {
            "description": comp.get("long_description") or comp.get("one_liner") or "",
            "founders": founders,
            "tech_stack": comp.get("tags") or [],
            "recent_launches": [l for l in launches if l][:3],
            "recent_news": [n for n in news if n["title"]][:4],
            "hiring_activity": ([f"{len(jobs)} open roles"] if jobs else [])
                               + ([f"eng: {', '.join(eng_roles[:4])}"] if eng_roles else []),
            "links": {k: comp.get(k) for k in ("linkedin_url", "github_url", "twitter_url")
                      if comp.get(k)},
            "year_founded": comp.get("year_founded"),
        }
    except Exception:
        return {}


# ------------------------------------------------------------------------ GitHub
def _github_dossier(org: str) -> dict:
    """Tech stack (language mix) + recently-pushed repos = what they're actively building."""
    if not org:
        return {}
    try:
        r = httpx.get(f"https://api.github.com/orgs/{org}/repos",
                      headers={"User-Agent": _UA, "Accept": "application/vnd.github+json"},
                      params={"per_page": 15, "sort": "pushed"}, timeout=20)
        if r.status_code != 200:
            return {}
        repos = r.json()
        langs: dict[str, int] = {}
        for x in repos:
            if x.get("language"):
                langs[x["language"]] = langs.get(x["language"], 0) + 1
        top = sorted(langs, key=langs.get, reverse=True)
        return {
            "tech_stack": top[:6],
            "recent_launches": [f"{x['name']} ({x.get('stargazers_count', 0)}★)"
                                for x in repos[:4] if not x.get("fork")],
            "hiring_activity": [f"{len(repos)} public repos, actively pushed"],
        }
    except Exception:
        return {}


# -------------------------------------------------------------------- Google News
_JUNK_NEWS = re.compile(r"\b(wikipedia|linkedin\.com|glassdoor|crunchbase profile)\b", re.I)


def _news_dossier(name: str, domain: str) -> list[dict]:
    """Keyless recent-news headlines via Google News RSS. Best-effort; used to fill funding/news
    for non-YC companies (YC already ships curated newsItems)."""
    if not name:
        return []
    q = f'"{name}"' + (f" {domain.split('.')[0]}" if domain else "") + " startup"
    try:
        xml = httpx.get("https://news.google.com/rss/search",
                        params={"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"},
                        headers={"User-Agent": _UA}, timeout=15).text
        items = re.findall(r"<item>(.*?)</item>", xml, re.DOTALL)[:5]
        out = []
        for it in items:
            t = re.search(r"<title>(.*?)</title>", it, re.DOTALL)
            d = re.search(r"<pubDate>(.*?)</pubDate>", it, re.DOTALL)
            title = _html.unescape(re.sub(r"<[^>]+>", "", t.group(1)).strip()) if t else ""
            if title and not _JUNK_NEWS.search(title):
                out.append({"title": title[:140],
                            "date": (d.group(1)[:16] if d else "")})
            if len(out) >= 3:
                break
        return out
    except Exception:
        return []


def gather(company: dict) -> dict:
    """Assemble the factual dossier for one company from whichever sources fit it."""
    source = company.get("source") or ""
    src_url = company.get("source_url") or ""
    dossier: dict = {
        "description": company.get("description") or company.get("one_liner") or "",
        "tech_stack": list(company.get("tags") or []),
        "team_size": company.get("team_size"),
        "location": company.get("location") or "",
        "stage": company.get("stage") or "",
        "founders": [], "recent_launches": [], "recent_news": [], "hiring_activity": [],
        "links": {}, "hiring_signals": list(company.get("hiring_signals") or []),
    }

    if "ycombinator.com/companies/" in src_url:
        yc = _yc_dossier(src_url.rstrip("/").split("/")[-1])
        for k, v in yc.items():
            if v:
                dossier[k] = v
    elif "GitHub" in source and "github.com/" in src_url:
        gh = _github_dossier(src_url.rstrip("/").split("/")[-1])
        for k, v in gh.items():
            if v:
                dossier[k] = v

    # News: YC ships curated news; for everyone else do a keyless lookup to catch funding/launches.
    if not dossier["recent_news"]:
        dossier["recent_news"] = _news_dossier(company.get("name", ""), company.get("domain", ""))

    dossier["tech_stack"] = dossier.get("tech_stack") or []
    return dossier


# ------------------------------------------------------------- LLM "Why this company?"
_SYSTEM = (
    "You help a specific CS student decide why a startup is genuinely worth reaching out to, and "
    "surface the ONE most honest connection between them and the company. You are given only real "
    "facts about the company and the student's real background. Rules: ground EVERY claim in the "
    "provided facts — never invent funding, products, people, or technologies. NEVER state a "
    "specific metric (funding amount, revenue, user/customer count, downloads, GitHub stars, "
    "growth %, valuation) unless that exact number appears verbatim in the provided facts; if you "
    "don't have a number, describe it qualitatively or omit it. Do not attribute a skill or "
    "experience to the student unless it is in their background. Find the single strongest genuine "
    "reason this student fits or is interested (a shared technology, a project that maps to their "
    "product, a domain overlap, their stage/size). Be concrete and specific. Never write a generic "
    "'I am a CS student looking for an internship' angle. Keep it grounded, not flattering. Output "
    "JSON only."
)


def _profile_digest(profile: dict) -> str:
    skills = sorted(ranking.flatten_skills(profile))
    projects = [f"{p.get('name', '')}: {(p.get('description') or p.get('summary') or '')[:120]}"
                for p in (profile.get("projects") or [])][:6]
    work = [f"{w.get('title', '')} at {w.get('company', '')}"
            for w in (profile.get("work_experience") or [])][:5]
    edu = (profile.get("education") or [{}])[0]
    return json.dumps({
        "field": edu.get("major", "Computer Science") + " @ " + edu.get("school", ""),
        "skills": skills[:40],
        "projects": projects,
        "experience": work,
    }, ensure_ascii=False)


def why_this_company(company: dict, dossier: dict, profile: dict) -> dict:
    """Local LLM: concise 'Why this company?' + the single best connection angle, grounded in the
    dossier + the student's background. Returns {why_this_company, best_angle, relevance_bullets}."""
    facts = {
        "company": company.get("name", ""),
        "one_liner": company.get("one_liner", ""),
        "description": (dossier.get("description") or "")[:1200],
        "tech_stack": dossier.get("tech_stack") or [],
        "team_size": dossier.get("team_size"),
        "stage": dossier.get("stage") or "",
        "location": dossier.get("location") or "",
        "founders": [f"{f['name']} ({f['title']})" for f in (dossier.get("founders") or [])][:4],
        "recent_launches": dossier.get("recent_launches") or [],
        "recent_news": [n["title"] for n in (dossier.get("recent_news") or [])],
        "hiring_activity": dossier.get("hiring_activity") or [],
    }
    user = ("STUDENT:\n" + _profile_digest(profile) + "\n\nCOMPANY FACTS:\n"
            + json.dumps(facts, ensure_ascii=False)
            + "\n\nUNKNOWN — do NOT mention, estimate, or invent any of these; they are not in the "
              "facts: GitHub stars, download/install counts, number of users or customers, revenue, "
              "valuation, growth rates, or any funding amount not listed under recent_news."
            + '\n\nReturn JSON: {"why_this_company": "2-3 sentence grounded explanation of why this '
            'company is worth this student reaching out to, tied to a real signal", '
            '"best_angle": "the single strongest genuine connection, one sentence", '
            '"relevance_bullets": ["2-4 short concrete overlaps between the student and the company"]}')
    try:
        out = llm.chat_json(_SYSTEM, user, temperature=0.25)
    except Exception as e:
        return {"why_this_company": "", "best_angle": "", "relevance_bullets": [],
                "error": str(e)}
    return {
        "why_this_company": (out.get("why_this_company") or "").strip(),
        "best_angle": (out.get("best_angle") or "").strip(),
        "relevance_bullets": [b for b in (out.get("relevance_bullets") or []) if isinstance(b, str)][:4],
    }


def build(company: dict, profile: dict) -> dict:
    """Full Phase-2 intel for one company: factual dossier + LLM synthesis + stamp."""
    dossier = gather(company)
    synth = why_this_company(company, dossier, profile)
    return {"dossier": dossier, **synth, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
