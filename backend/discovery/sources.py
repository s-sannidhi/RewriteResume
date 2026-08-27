"""Discovery sources. Each source is a function `fetch(filters) -> list[normalized dict]`.

Normalized company shape (every source emits this):
    name, website, domain, one_liner, description, team_size (int|None), stage, batch,
    industry, subindustry, tags (list), regions (list), location, is_hiring (bool),
    slug, source (str), source_url (str), raw (dict)

Anchor source = Y Combinator's public company directory, served by an Algolia index
(YCCompany_production). Like Simplify's Typesense key in scripts/scrape_interns.py, YC embeds a
scoped, search-only key in `window.AlgoliaOpts` on the companies page — we scrape it live each run
(it rotates) rather than hardcoding it. Auth-free, no anti-bot gate.

Adding a source later (GitHub, Product Hunt, …) is one function + a registry entry; nothing else
in the pipeline changes.
"""
import json
import re
import time
from html import unescape as _html_unescape

import httpx

from . import normalize

_UA = "Mozilla/5.0 AppleWebKit/537.36 Chrome/120"
_YC_COMPANIES = "https://www.ycombinator.com/companies"
_YC_INDEX = "YCCompany_production"

_algolia_cache: dict = {"app": None, "key": None, "at": 0.0}


def _algolia_creds() -> tuple[str, str]:
    """Scrape the live Algolia app id + scoped search key from the YC companies page.
    Cached for 10 min (the key rotates but not per-request)."""
    now = time.time()
    if _algolia_cache["key"] and now - _algolia_cache["at"] < 600:
        return _algolia_cache["app"], _algolia_cache["key"]
    html = httpx.get(_YC_COMPANIES, headers={"User-Agent": _UA}, timeout=25,
                     follow_redirects=True).text
    m = re.search(r"window\.AlgoliaOpts\s*=\s*(\{.*?\})\s*</script>", html) or \
        re.search(r"window\.AlgoliaOpts\s*=\s*(\{[^\n]*?\})", html)
    if not m:
        raise RuntimeError("Could not find AlgoliaOpts on the YC companies page (layout changed?)")
    opts = json.loads(m.group(1))
    _algolia_cache.update(app=opts["app"], key=opts["key"], at=now)
    return opts["app"], opts["key"]


def recent_batches(years_back: int = 1) -> list[str]:
    """Batch labels for the current + previous `years_back` years, all four seasons — the
    recency filter (a recent batch == a company that just started and is staffing up)."""
    this_year = time.localtime().tm_year
    seasons = ("Winter", "Spring", "Summer", "Fall")
    return [f"{s} {y}" for y in range(this_year, this_year - years_back - 1, -1) for s in seasons]


def _query(app: str, key: str, facet_filters: list, page: int, hits: int = 1000) -> dict:
    url = f"https://{app.lower()}-dsn.algolia.net/1/indexes/{_YC_INDEX}/query"
    r = httpx.post(url, headers={"X-Algolia-API-Key": key, "X-Algolia-Application-Id": app,
                                 "Content-Type": "application/json"},
                   json={"query": "", "hitsPerPage": hits, "page": page,
                         "facetFilters": facet_filters}, timeout=25)
    r.raise_for_status()
    return r.json()


def _normalize_yc(h: dict) -> dict:
    website = h.get("website", "") or ""
    dom = normalize.domain_of(website)
    slug = h.get("slug", "") or ""
    return {
        "name": h.get("name", "") or "",
        "website": website,
        "domain": dom,
        "id": normalize.company_id(dom, h.get("name", "")),
        "one_liner": h.get("one_liner", "") or "",
        "description": (h.get("long_description", "") or "")[:2000],
        "team_size": h.get("team_size"),
        "stage": h.get("stage", "") or "",
        "batch": h.get("batch", "") or "",
        "industry": h.get("industry", "") or "",
        "subindustry": h.get("subindustry", "") or "",
        "tags": h.get("tags", []) or [],
        "regions": h.get("regions", []) or [],
        "location": h.get("all_locations", "") or "",
        "is_hiring": bool(h.get("isHiring")),
        "slug": slug,
        "source": "YC",
        "source_url": f"https://www.ycombinator.com/companies/{slug}" if slug else _YC_COMPANIES,
        "raw": {},
    }


def fetch_yc(filters: dict | None = None) -> list[dict]:
    """YC companies, defaulting to recent batches that are actively hiring. filters:
        recent_only (bool, default True)  — restrict to recent batches
        hiring_only (bool, default True)  — restrict to isHiring:true
        years_back  (int, default 1)      — how many years of batches count as "recent"
        limit       (int, default 400)    — safety cap on rows pulled
    """
    f = filters or {}
    recent_only = f.get("recent_only", True)
    hiring_only = f.get("hiring_only", True)
    limit = int(f.get("limit", 400))
    app, key = _algolia_creds()

    facet_filters: list = [["status:Active"]]          # skip dead/acquired companies
    if hiring_only:
        facet_filters.append(["isHiring:true"])
    if recent_only:
        facet_filters.append([f"batch:{b}" for b in recent_batches(f.get("years_back", 1))])

    out, page = [], 0
    while len(out) < limit:
        res = _query(app, key, facet_filters, page)
        hits = res.get("hits", [])
        out += [_normalize_yc(h) for h in hits]
        if page >= res.get("nbPages", 1) - 1 or not hits:
            break
        page += 1
    return out[:limit]


_ROLE_LIKE = re.compile(r"\b(engineer|developer|full.?stack|back.?end|front.?end|hiring|intern|"
                        r"manager|scientist|designer|we're|we are|join|remote|role|position)\b", re.I)


def _pretty_name(name: str, domain: str) -> str:
    """Clean a company name. If it's empty, a lowercase handle/slug, or looks like a job title,
    derive a display name from the domain's root label instead."""
    name = (name or "").strip()
    handle_like = name and (name == name.lower()) and (" " not in name or "-" in name)
    if not name or _ROLE_LIKE.search(name) or handle_like:
        root = (domain.split(".")[0] if domain else name).replace("-", " ").replace("_", " ")
        if root:
            return root[:1].upper() + root[1:]
    return name[:60]


def _company(source: str, name: str, website: str, **kw) -> dict:
    """Build a normalized company dict with the standard keys defaulted, so non-YC sources only
    specify what they actually have."""
    dom = normalize.domain_of(website)
    name = _pretty_name(name, dom)
    base = {
        "name": name, "website": website or "", "domain": dom,
        "id": normalize.company_id(dom, name), "one_liner": "", "description": "",
        "team_size": None, "stage": "", "batch": "", "industry": "", "subindustry": "",
        "tags": [], "regions": [], "location": "", "is_hiring": False, "slug": "",
        "source": source, "source_url": "", "hiring_signals": [], "raw": {},
    }
    base.update(kw)
    return base


# --- Source 2: Hacker News "Ask HN: Who is hiring?" (free, public Algolia HN API) --------------
# The monthly thread (posted by user `whoishiring`) is a wide cross-section of companies actively
# hiring — startups and larger — far beyond YC. Each top-level comment is one company's post,
# conventionally "Company | website | roles | location | remote?". Posting there IS a hiring signal.
_HN_ALGOLIA = "https://hn.algolia.com/api/v1"
_STAGE_RE = re.compile(r"\((?:(seed|pre-seed|series\s+[a-e])[^)]*)\)", re.I)
_URL_RE = re.compile(r'https?://[^\s|)<>"\']+')


def _strip_html(t: str) -> str:
    return _html_unescape(re.sub(r"<[^>]+>", " ", t or "")).strip()


def _parse_hn_post(text: str) -> dict | None:
    """Extract a company from one Who-is-Hiring comment. Returns None if no website is present
    (we require a site — it's the dedup key and the outreach target)."""
    txt = re.sub(r"\s+", " ", _strip_html(text)).strip()
    m = _URL_RE.search(txt)
    if not m:
        return None
    website = m.group(0).rstrip(".,);")
    head = txt.split("|")[0]                             # name lives before the first pipe
    name = re.split(r"[(|]|https?://", head)[0].strip(" -–—:")
    if not name or len(name) > 60:
        name = (name or "")[:60].strip() or "(unnamed)"
    stage_m = _STAGE_RE.search(head) or _STAGE_RE.search(txt[:120])
    remote = bool(re.search(r"\bremote\b", txt, re.I))
    return {
        "name": name, "website": website,
        "one_liner": txt[:160],
        "description": txt[:1500],
        "stage": (stage_m.group(1).title() if stage_m else ""),
        "regions": (["Remote"] if remote else []),
    }


def fetch_hn(filters: dict | None = None) -> list[dict]:
    """Companies from the latest HN Who-is-Hiring thread. filters.limit caps posts parsed."""
    limit = int((filters or {}).get("hn_limit", (filters or {}).get("limit", 250)))
    hits = httpx.get(f"{_HN_ALGOLIA}/search_by_date",
                     params={"tags": "story,author_whoishiring", "query": "hiring",
                             "hitsPerPage": 5}, headers={"User-Agent": _UA}, timeout=25).json()
    threads = [h for h in hits.get("hits", []) if re.match(r"Ask HN: Who is hiring", h["title"])]
    if not threads:
        return []
    thread = threads[0]
    month = re.search(r"\(([^)]+)\)", thread["title"])
    month_label = month.group(1) if month else "recent"
    item = httpx.get(f"{_HN_ALGOLIA}/items/{thread['objectID']}", timeout=40).json()
    out = []
    for c in item.get("children", []):
        if not c.get("text"):
            continue
        p = _parse_hn_post(c["text"])
        if not p:
            continue
        out.append(_company(
            "HN", p["name"], p["website"],
            one_liner=p["one_liner"], description=p["description"], stage=p["stage"],
            regions=p["regions"], is_hiring=True,
            source_url=f"https://news.ycombinator.com/item?id={c['id']}",
            hiring_signals=[f"Posted in HN Who-is-Hiring ({month_label})"]))
        if len(out) >= limit:
            break
    return out


# --- Source 3: GitHub organizations (keyless; active-dev + tech-overlap signal) ----------------
# Orgs behind popular, recently-pushed repos in the user's languages. Not a hiring signal on its
# own (is_hiring stays False), but a strong technical-fit + "actively building" signal. One search
# per language keeps us well under the unauthenticated 10-req/min limit; a token (env/Keychain)
# lifts that ceiling when present but is NOT required.
_GH_SEARCH = "https://api.github.com/search/repositories"
_GH_DENY = {"google", "microsoft", "aws", "amazon", "facebook", "meta", "apple", "alibaba",
            "tencent", "netflix", "uber", "airbnb", "spotify", "twitter", "openai", "vercel",
            "cloudflare", "stripe", "shopify", "gitlab", "github", "hashicorp", "elastic"}
_GH_LANGS = ["TypeScript", "Python", "Go", "Rust"]


def _github_token() -> str:
    import os
    tok = os.environ.get("RR_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    if tok:
        return tok
    try:
        from .. import secrets
        return secrets.get_secret("resume-rewriter-github", "token", "RR_GITHUB_TOKEN") or ""
    except Exception:
        return ""


def fetch_github(filters: dict | None = None) -> list[dict]:
    """Recently-active orgs matching the user's stack. Best-effort: on rate-limit/error we stop
    and return whatever we gathered (never raises the whole discovery run)."""
    f = filters or {}
    per_lang = int(f.get("gh_per_lang", 25))
    langs = f.get("gh_langs", _GH_LANGS)
    since = time.strftime("%Y-%m-%d", time.localtime(time.time() - 45 * 86400))  # pushed in ~45d
    headers = {"User-Agent": _UA, "Accept": "application/vnd.github+json"}
    tok = _github_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    out, seen_orgs = [], set()
    for lang in langs:
        try:
            r = httpx.get(_GH_SEARCH, headers=headers, timeout=20, params={
                "q": f"language:{lang} stars:120..9000 pushed:>{since}",
                "sort": "updated", "order": "desc", "per_page": per_lang})
            if r.status_code == 403:      # rate limited — stop gracefully
                break
            r.raise_for_status()
        except Exception:
            break
        for it in r.json().get("items", []):
            o = it.get("owner") or {}
            login = (o.get("login") or "")
            if o.get("type") != "Organization" or login.lower() in _GH_DENY:
                continue
            home = (it.get("homepage") or "").strip()
            if not home or login.lower() in seen_orgs:
                continue
            seen_orgs.add(login.lower())
            topics = [t for t in (it.get("topics") or [])][:6]
            tags = ([it["language"]] if it.get("language") else []) + topics
            out.append(_company(
                "GitHub", login, home,
                one_liner=(it.get("description") or "")[:160],
                description=(it.get("description") or ""),
                tags=tags, is_hiring=False,
                source_url=f"https://github.com/{login}",
                hiring_signals=[f"Active GitHub org (★{it.get('stargazers_count', 0)})"]))
    return out


# --- Registry: source name -> fetch fn. Add new sources here; the rest of the pipeline is generic.
SOURCES = {
    "yc": fetch_yc,
    "hn": fetch_hn,
    "github": fetch_github,
}


def discover(filters: dict | None = None, sources: list[str] | None = None) -> list[dict]:
    """Run the requested sources (default: all) and merge results by domain."""
    names = sources or list(SOURCES)
    rows: list[dict] = []
    for n in names:
        fn = SOURCES.get(n)
        if fn:
            rows += fn(filters)
    return normalize.merge(rows)
