"""Scrape today's internships from THREE free feeds, filtered to Austin or Remote, recently posted.

  1. Simplify's public Typesense search index (below) — the big one, with majors + functions.
  2. The SimplifyJobs × Pitt CSC GitHub listings file (see GH_LISTINGS) — direct apply URLs,
     a real `degrees` field, and community-submitted roles the search index doesn't carry.
  3. ApplyGuy/2027-Internships (see APPLYGUY_JSON) — verified U.S. SWE listings with direct
     ATS links, refreshed about every 15 minutes.

Results are merged and de-duplicated by apply URL, then company+title.

Simplify's job board runs on a public Typesense search index (js-ha.simplify.jobs) — no auth,
no gating. Each result's apply link is https://simplify.jobs/jobs/click/<id>, which 302-redirects
straight to the REAL company application (Greenhouse/Lever/Workday/etc.) — unlike Jobright, which
hid it behind a login. So these ARE direct applications: open the link and you're on the actual
form.

Run: .venv/bin/python scripts/scrape_interns.py
"""
import calendar
import re
import time

import httpx

# Public scoped Typesense search key (from simplify.jobs/jobs — search-only, safe to embed).
TS_KEY = ("SWF1ODFZbzBkcVlVdnVwT2FqUE5EZ3JpSk5hVmdpUHg1SklXWEdGbHZVRT1POHJieyJleGNsdWRlX2ZpZWxkcyI6"
          "ImNvbXBhbnlfdXJsLGNhdGVnb3JpZXMsYWRkaXRpb25hbF9yZXF1aXJlbWVudHMsY291bnRyaWVzLGRlZ3JlZXMs"
          "Z2VvbG9jYXRpb25zLGluZHVzdHJpZXMsaXNfc2ltcGxlX2FwcGxpY2F0aW9uLGpvYl9saXN0cyxsZWFkZXJzaGlw"
          "X3R5cGUsc2VjdXJpdHlfY2xlYXJhbmNlLHNraWxscyx1cmwifQ==")
TS_URL = "https://js-ha.simplify.jobs/multi_search"
CLICK = "https://simplify.jobs/jobs/click/{id}"       # -> redirects to the real application
HEADERS = {"Origin": "https://simplify.jobs", "Content-Type": "application/json",
           "User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/120"}

# The user's categories -> Simplify "functions" (facet values). Each job is grouped under the
# first of its functions that maps back here.
CATEGORIES = {
    "Software Engineering":      ["Software Engineering", "Backend Engineering", "Full-Stack Engineering"],
    "Data Analysis":            ["Data Analysis", "Data & Analytics"],
    "Machine Learning & AI":    ["AI & Machine Learning"],
    "Engineering & Development": ["DevOps & Infrastructure", "Data Engineering"],
    "Cybersecurity":            ["Cybersecurity", "IT & Security"],
}
ALL_FUNCTIONS = [f for fns in CATEGORIES.values() for f in fns]
_FUNC_TO_CAT = {f: cat for cat, fns in CATEGORIES.items() for f in fns}

# Simplify multi-tags jobs, so a Legal/Sales/etc. role can carry a secondary tech function and
# sneak into our categories. Drop anything that ALSO carries a clearly non-tech function — that
# keeps the fields honest (only real SWE/ML/Data/etc. roles).
NON_TECH = {
    "Legal", "Legal & Compliance", "Sales & Account Management", "Sales & Solution Engineering",
    "Marketing", "Retail", "Retail Sales", "Retail Store Management", "Accounting",
    "Medical, Clinical & Veterinary", "Nursing & Allied Health Professionals",
    "Customer Experience & Support", "Customer Support", "Administrative & Executive Assistance",
}

# Companies the user never wants to see (their call). Matched on the posting's company name,
# so subsidiaries/brands are listed explicitly.
BLOCKED_COMPANIES = re.compile(
    r"\b(bytedance|byte\s?dance|tiktok|tik\s?tok|lemon8|capcut|douyin)\b", re.I)


def _blocked_company(doc: dict) -> bool:
    return bool(BLOCKED_COMPANIES.search(doc.get("company_name", "") or ""))


# Advanced-degree-only roles. The feed's payload strips the `degrees` field, so the title is the
# only signal available — which is where most of these announce themselves ("PhD Research Intern",
# "MS/PhD Intern", "Masters Intern"). Patterns are deliberately narrow: bare "Master" is NOT
# matched, so "Master Data Analyst" / "Scrum Master" titles still come through.
ADVANCED_DEGREE = re.compile(
    r"\bph\.?\s?d\.?\b|\bdoctoral\b|\bdoctorate\b|\bpost[-\s]?doc\w*\b"
    r"|\bmasters\b|\bmaster['\u2019]s\b|\bm\.s\.|\bmsc\b|\bmba\b"
    r"|\b(?:grad|graduate)\s+student\b", re.I)


def _advanced_degree(doc: dict) -> bool:
    return bool(ADVANCED_DEGREE.search(doc.get("title", "") or ""))


# Location rule: FALL internships must be Austin or Remote (that's when the user is in Austin).
# Any OTHER season (Summer/Spring/Winter/N-A) — anywhere is fine. Recency is the universal filter.
MAX_AGE_DAYS = 1.0        # posted within the last day (start_date = posting date). Widen if thin.
PER_PAGE = 250            # Typesense max per page
RESULT_CAP = 400          # safety cap on how many we pull (≈ tab count)


def _fn_filter() -> str:
    return "[" + ",".join(f"`{f}`" for f in ALL_FUNCTIONS) + "]"


def _search_all(filter_by: str, cap: int = RESULT_CAP) -> list[dict]:
    """Paginate the Typesense search until we've pulled everything in the window (up to the cap)."""
    docs, page = [], 1
    while len(docs) < cap:
        body = {"searches": [{"collection": "jobs", "q": "*", "query_by": "title",
                              "per_page": PER_PAGE, "page": page, "filter_by": filter_by,
                              "sort_by": "start_date:desc"}]}
        r = httpx.post(TS_URL, params={"x-typesense-api-key": TS_KEY}, json=body,
                       headers=HEADERS, timeout=25)
        r.raise_for_status()
        res = r.json().get("results", [{}])[0]
        hits = res.get("hits", [])
        docs += [h["document"] for h in hits]
        if len(hits) < PER_PAGE or len(docs) >= res.get("found", 0):
            break
        page += 1
    return docs


def _age_days(doc: dict) -> float:
    ts = doc.get("start_date")
    return (time.time() - ts) / 86400 if ts else 1e9


def _age_label(days: float) -> str:
    h = days * 24
    return f"{round(h)}h ago" if h < 24 else f"{round(days)}d ago"


# Titles that signal a school-year (part-time) role even when the season isn't tagged.
_SCHOOL_YEAR_TITLE = re.compile(
    r"\bstudent\s+(associate|assistant|worker|aide|ambassador)\b|\bwork[- ]?study\b", re.I)


def _is_school_year(doc: dict) -> bool:
    """Fall/Spring = school year (user is in Austin then). A role is school-year-RESTRICTED only
    if it offers no break-period term — a multi-season posting that also offers Summer/Winter is
    fine anywhere. A 'Student Associate/Assistant'-type title counts as school-year even when the
    season is untagged."""
    seasons = [str(s).lower() for s in (doc.get("seasons") or [])]
    school = [s for s in seasons if s.startswith(("fall", "spring"))]
    breaks = [s for s in seasons if s.startswith(("summer", "winter")) or s in ("n/a", "")]
    if school and not breaks:
        return True
    return bool(_SCHOOL_YEAR_TITLE.search(doc.get("title", "") or ""))


def _austin_or_remote(doc: dict) -> bool:
    if doc.get("travel_requirements") == "Remote":
        return True
    if any("austin" in (l or "").lower() for l in (doc.get("locations") or [])):
        return True
    return "university of texas at austin" in (doc.get("company_name", "") or "").lower()


# ── Source 2: the Simplify × Pitt CSC internship repo ────────────────────────────────────────
# Same organisation as the Typesense index above, but a genuinely different feed, and better in
# three ways that matter here:
#   • `url` is the REAL application link (Greenhouse/Ashby/Workday/…), not a simplify.jobs/click
#     redirect — so nothing has to be resolved before we can tell whether a tab is already open.
#   • it carries `degrees`, which the Typesense payload explicitly strips (see TS_KEY's
#     exclude_fields). That turns the masters/PhD filter from a title-regex guess into a fact.
#   • it carries community submissions (`source` != "Simplify") that the search index doesn't have.
# The Summer2026 and Summer2027 repos currently serve byte-identical files; 2027 is the live one.
GH_LISTINGS = ("https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships"
               "/dev/.github/scripts/listings.json")

# ── Source 3: ApplyGuy/2027-Internships (verified U.S. SWE + PM listings) ─────────────────────
# Machine-readable feed updated ~every 15 minutes. `listingUrl` is the real ATS apply link;
# `url` points at applyguy.ai and is ignored.
APPLYGUY_JSON = ("https://raw.githubusercontent.com/ApplyGuy/2027-Internships"
                 "/main/data/internships.json")

# Their category names → ours. Product Management is dropped (CS-internship focus).
GH_CATEGORY = {
    "Software": "Software Engineering",
    "Software Engineering": "Software Engineering",
    "AI/ML/Data": "Machine Learning & AI",
    "Data Science, AI & Machine Learning": "Machine Learning & AI",
    "Hardware": "Engineering & Development",
    "Hardware Engineering": "Engineering & Development",
}
APPLYGUY_CATEGORY = {
    "Software Engineering": "Software Engineering",
}

# Degrees that mean an undergrad can apply. A posting listing ONLY graduate degrees is for
# grad students, whatever its title says.
_UNDERGRAD_DEGREES = {"bachelor's", "bachelors", "associate's", "associates", "incomplete"}


def _gh_grad_only(doc: dict) -> bool:
    """True when the posting's own degree list excludes undergrads."""
    degs = {str(d).strip().lower() for d in (doc.get("degrees") or []) if str(d).strip()}
    return bool(degs) and not (degs & _UNDERGRAD_DEGREES)


def _gh_school_year(doc: dict) -> bool:
    """Fall/Spring-only terms = school year, same rule the Typesense path uses."""
    terms = [str(t).lower() for t in (doc.get("terms") or [])]
    school = [t for t in terms if t.startswith(("fall", "spring"))]
    breaks = [t for t in terms if t.startswith(("summer", "winter")) or t in ("n/a", "")]
    if school and not breaks:
        return True
    return bool(_SCHOOL_YEAR_TITLE.search(doc.get("title", "") or ""))


def _fetch_github(min_days: float, max_days: float) -> list[dict]:
    """Internships from the GitHub feed, normalised to the same shape as the Typesense rows."""
    try:
        r = httpx.get(GH_LISTINGS, timeout=30, follow_redirects=True)
        r.raise_for_status()
        rows = r.json()
    except Exception:
        return []                      # a dead second source must never break the first one

    now = time.time()
    out = []
    for doc in rows:
        if not doc.get("active") or not doc.get("is_visible", True):
            continue
        url = (doc.get("url") or "").strip()
        if not url:
            continue
        cat = GH_CATEGORY.get(doc.get("category") or "")
        if not cat:
            continue
        company = doc.get("company_name") or ""
        title = doc.get("title") or ""
        if _blocked_company({"company_name": company}):
            continue
        # Degrees first (a fact), title regex second (a guess) — either one disqualifies.
        if _gh_grad_only(doc) or _advanced_degree({"title": title}):
            continue
        # US citizenship-only postings are dead ends for a permanent resident.
        if (doc.get("sponsorship") or "") == "U.S. Citizenship is Required":
            continue

        ts = doc.get("date_posted") or doc.get("date_updated")
        if not ts:
            continue
        age = (now - float(ts)) / 86400
        if not (min(min_days, max_days) <= age <= max(min_days, max_days)):
            continue

        locations = [str(l) for l in (doc.get("locations") or []) if l]
        loc_str = ", ".join(locations)
        remote = "remote" in loc_str.lower()
        if _gh_school_year(doc) and not (remote or "austin" in loc_str.lower()):
            continue

        terms = [str(t) for t in (doc.get("terms") or []) if t and str(t) != "N/A"]
        out.append({
            "category": cat, "title": title, "company": company,
            "location": loc_str, "work_model": "Remote" if remote else "",
            "season": ", ".join(terms),
            "age": _age_label(age),
            "majors": [],                      # this feed has degrees, not majors
            "functions": [],
            "apply_url": url,                  # already the real application
            "source": "github",
        })
    return out


def _applyguy_age_days(doc: dict, now: float) -> float | None:
    """Days since posting from ApplyGuy's `posted` (YYYY-MM-DD) or `age` ("Today"/"1d"/…)."""
    posted = (doc.get("posted") or "").strip()
    if posted:
        try:
            y, m, d = (int(x) for x in posted.split("-", 2))
            # Approximate: noon UTC on that calendar day — good enough for day-window filters.
            ts = calendar.timegm((y, m, d, 12, 0, 0))
            return max(0.0, (now - ts) / 86400)
        except (ValueError, TypeError):
            pass
    raw = (doc.get("age") or "").strip().lower()
    if raw in ("today", "0d", "0"):
        return 0.0
    m = re.match(r"^(\d+)\s*d", raw)
    if m:
        return float(m.group(1))
    return None


def _applyguy_school_year(season: str, title: str) -> bool:
    """Fall/Spring-only (no summer/winter) ≈ school-year restricted, same rule as the other feeds."""
    s = (season or "").lower()
    if not s or s in ("not specified", "—", "-", "n/a", "coop", "co-op"):
        return bool(_SCHOOL_YEAR_TITLE.search(title or ""))
    school = any(x in s for x in ("fall", "spring"))
    breaks = any(x in s for x in ("summer", "winter"))
    if school and not breaks:
        return True
    return bool(_SCHOOL_YEAR_TITLE.search(title or ""))


def _fetch_applyguy(min_days: float, max_days: float) -> list[dict]:
    """Internships from ApplyGuy/2027-Internships. Uses listingUrl (real ATS), not applyguy.ai."""
    try:
        r = httpx.get(APPLYGUY_JSON, timeout=30, follow_redirects=True)
        r.raise_for_status()
        payload = r.json()
        rows = payload.get("jobs") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return []
    except Exception:
        return []                      # never break Simplify/GitHub over a third-source failure

    now = time.time()
    lo, hi = min(min_days, max_days), max(min_days, max_days)
    out = []
    for doc in rows:
        cat = APPLYGUY_CATEGORY.get(doc.get("category") or "")
        if not cat:
            continue
        # Prefer the employer career-page link; fall back only if missing.
        url = (doc.get("listingUrl") or doc.get("listing_url") or "").strip()
        if not url or "applyguy.ai" in url.lower():
            continue
        company = doc.get("company") or ""
        title = doc.get("title") or ""
        if _blocked_company({"company_name": company}):
            continue
        if _advanced_degree({"title": title}):
            continue

        age = _applyguy_age_days(doc, now)
        if age is None or not (lo <= age <= hi):
            continue

        loc = (doc.get("location") or "").strip()
        season = (doc.get("season") or "").strip()
        if season.lower() in ("not specified", "—", "-"):
            season = ""
        remote = "remote" in loc.lower()
        if _applyguy_school_year(season, title) and not (remote or "austin" in loc.lower()):
            continue

        out.append({
            "category": cat, "title": title, "company": company,
            "location": loc, "work_model": "Remote" if remote else "",
            "season": season,
            "age": _age_label(age),
            "majors": [],
            "functions": [],
            "apply_url": url,
            "source": "applyguy",
        })
    return out


def _dedupe(rows: list[dict]) -> list[dict]:
    """Drop repeats across sources. Same posting, two feeds: match on the apply URL first,
    then on company+title, since sources link to the same job by different routes."""
    seen_url, seen_key, out = set(), set(), []
    for r in rows:
        url = (r.get("apply_url") or "").split("?")[0].rstrip("/").lower()
        key = (re.sub(r"[^a-z0-9]+", "", (r.get("company") or "").lower()),
               re.sub(r"[^a-z0-9]+", "", (r.get("title") or "").lower()))
        if (url and url in seen_url) or (key[0] and key in seen_key):
            continue
        if url:
            seen_url.add(url)
        if key[0]:
            seen_key.add(key)
        out.append(r)
    return out


def scrape(min_days: float = 0.0, max_days: float = MAX_AGE_DAYS, result_cap: int = RESULT_CAP) -> list[dict]:
    """Internships whose posting age is between min_days and max_days old (inclusive).
    min_days=0 = current/just-posted; e.g. (1, 3) = posted 1–3 days ago. Pass a large max_days +
    result_cap (e.g. 3650, 5000) to pull the whole current board regardless of age — used by the
    top-500-companies search, which doesn't care when a posting went up."""
    lo_d, hi_d = min(min_days, max_days), max(min_days, max_days)
    now = time.time()
    oldest = int(now - hi_d * 86400)   # start_date must be at least this recent
    newest = int(now - lo_d * 86400)   # start_date must be at least lo_d old
    docs = _search_all(f"type:=Internship && functions:={_fn_filter()} "
                       f"&& countries:=[`United States`,`Canada`] "   # US or Canada only
                       f"&& start_date:>={oldest} && start_date:<={newest}", cap=result_cap)

    seen, out = set(), []
    for doc in docs:
        jid = doc.get("id")
        if not jid or jid in seen:
            continue
        if _blocked_company(doc) or _advanced_degree(doc):
            continue
        fns = doc.get("functions") or []
        # keep the fields honest: drop roles that also carry a clearly non-tech function
        if any(f in NON_TECH for f in fns):
            continue
        # School-year roles (Fall/Spring, or a "Student Associate"-type title) must be Austin or
        # Remote. Summer/Winter-break and undated roles: any location.
        if _is_school_year(doc) and not _austin_or_remote(doc):
            continue
        seen.add(jid)
        cat = next((_FUNC_TO_CAT[f] for f in (doc.get("functions") or []) if f in _FUNC_TO_CAT),
                   "Other")
        seasons = doc.get("seasons") or []
        out.append({
            "category": cat, "title": doc.get("title", ""),
            "company": doc.get("company_name", ""), "location": ", ".join(doc.get("locations") or []),
            "work_model": doc.get("travel_requirements", ""),
            "season": ", ".join(s for s in seasons if s and s != "N/A"),
            "age": _age_label(_age_days(doc)),
            "majors": doc.get("majors") or [],          # who the posting targets — the fit signal
            "functions": doc.get("functions") or [],
            "apply_url": CLICK.format(id=jid),   # redirects to the real application
        })
    for r in out:
        r.setdefault("source", "simplify")
    # Merge GitHub + ApplyGuy. Typesense rows win on collisions (they carry `majors` for fit).
    out = _dedupe(out + _fetch_github(min_days, max_days) + _fetch_applyguy(min_days, max_days))
    out.sort(key=lambda j: (j["category"], j["age"]))
    return out


def main() -> None:
    print(f"Scraping Simplify + GitHub + ApplyGuy (posted ≤{MAX_AGE_DAYS:g} day; Fall/Spring "
          f"school-year = Austin/Remote only, summer/break = anywhere)…\n")
    rows = scrape()
    if not rows:
        print("No matching internships in the window — widen MAX_AGE_DAYS or try later.")
        return
    by_cat: dict[str, list] = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(r)
    by_src: dict[str, int] = {}
    for r in rows:
        by_src[r.get("source") or "?"] = by_src.get(r.get("source") or "?", 0) + 1
    for cat, items in by_cat.items():
        print(f"── {cat} ({len(items)}) " + "─" * max(0, 40 - len(cat)))
        for r in items:
            season = f" · {r['season']}" if r.get("season") else ""
            src = f" [{r.get('source')}]" if r.get("source") else ""
            print(f"  • {r['company']} — {r['title']}{src}")
            print(f"      {r['work_model']} · {r['location']}{season} · {r['age']}")
            print(f"      {r['apply_url']}  (opens the real application)")
        print()
    print(f"Total: {len(rows)} internships across {len(by_cat)} categories "
          f"({', '.join(f'{k}={v}' for k, v in sorted(by_src.items()))}).")


if __name__ == "__main__":
    main()
