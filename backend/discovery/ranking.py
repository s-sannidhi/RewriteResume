"""Deterministic startup ranking — no LLM (Python owns scoring, like _fit() for interns).

Two independent axes, each 0-100:
  * fit_score  — how well the company matches the user (tech overlap, tech-industry, location)
  * hiring_score/hiring_label — how likely they're hiring AND likely to respond (isHiring flag,
    batch recency, small team, early stage, open roles). Smaller/earlier = easier to get in and a
    reachable founder, which the spec calls "likelihood of responding".

Everything is tunable via the constants below. Reasons/signals are human-readable so the dashboard
can show "reason for fit" and "hiring signal" straight from here.
"""
import re

# Tags/industries a CS/SWE student maps onto well (reward) vs. domains needing non-CS depth (penalize).
_TECH_TAGS = {
    "ai", "artificial intelligence", "machine learning", "developer tools", "devtools",
    "infrastructure", "data engineering", "analytics", "big data", "saas", "b2b software",
    "security", "cybersecurity", "fintech", "web development", "open source", "api", "cloud",
    "productivity", "no-code", "database", "devops",
}
_NON_CS_HEAVY = {"robotics", "hardware", "semiconductors", "biotech", "healthcare", "manufacturing",
                 "energy", "climate", "aerospace", "materials", "diagnostics", "therapeutics"}

_TECH_INDUSTRIES = {"b2b", "fintech", "financial technology"}

# Location: the user is in Austin, TX. Remote-friendly or Austin-based lowers the bar to apply.
_AUSTIN = re.compile(r"\baustin\b|,\s*tx\b|texas", re.I)


def flatten_skills(profile: dict) -> set[str]:
    """All profile skills (skills + skills_extra), lowercased, as a flat token set."""
    out: set[str] = set()
    for bucket in ("skills", "skills_extra"):
        val = profile.get(bucket) or {}
        groups = val.values() if isinstance(val, dict) else [val]
        for g in groups:
            for s in (g or []):
                if isinstance(s, str) and s.strip():
                    out.add(s.strip().lower())
    return out


def _lc_list(xs) -> list[str]:
    return [str(x).lower() for x in (xs or [])]


def _tech_overlap(company: dict, skills: set[str]) -> list[str]:
    """Distinct profile skills that appear in the company's tags/one-liner/description/subindustry.
    Word-boundary match so 'r' or 'c' don't match everything; returns original-cased-ish tokens."""
    hay = " ".join([
        " ".join(_lc_list(company.get("tags"))),
        (company.get("one_liner") or "").lower(),
        (company.get("description") or "").lower(),
        (company.get("subindustry") or "").lower(),
        (company.get("industry") or "").lower(),
    ])
    matched = []
    for s in skills:
        if len(s) <= 2:                       # skip 1-2 char skills (C, R, Go-ish) — too noisy
            continue
        if re.search(r"(?<![a-z0-9])" + re.escape(s) + r"(?![a-z0-9])", hay):
            matched.append(s)
    # nicer display order: longest (most specific) first, capped
    return sorted(set(matched), key=len, reverse=True)[:6]


def score(company: dict, profile: dict, skills: set[str] | None = None,
          job_count: int | None = None, recent_batches: set[str] | None = None) -> dict:
    """Return fit_score, fit_reason, hiring_score, hiring_label, hiring_signals for one company."""
    skills = flatten_skills(profile) if skills is None else skills
    tags_lc = set(_lc_list(company.get("tags")))

    # --- fit ---------------------------------------------------------------
    fit, reasons = 50, []
    overlap = _tech_overlap(company, skills)
    if overlap:
        fit += min(8 * len(overlap), 32)
        reasons.append("overlaps your " + ", ".join(overlap))

    if (tags_lc & _TECH_TAGS) or ((company.get("industry") or "").lower() in _TECH_INDUSTRIES):
        fit += 10
        hot = sorted(tags_lc & _TECH_TAGS)
        if hot and not overlap:
            reasons.append("software/" + hot[0] + " focus")
    if (tags_lc & _NON_CS_HEAVY) and not (tags_lc & _TECH_TAGS) and not overlap:
        fit -= 10
        reasons.append("mostly " + sorted(tags_lc & _NON_CS_HEAVY)[0] + " (less CS-leaning)")

    loc = company.get("location") or ""
    remote = any("remote" in r.lower() for r in (company.get("regions") or []))
    if _AUSTIN.search(loc):
        fit += 12
        reasons.append("Austin-based")
    elif remote:
        fit += 8
        reasons.append("remote-friendly")

    # Multi-source presence (e.g. seen on both YC and HN) = a stronger, corroborated lead.
    src_count = len([s for s in (company.get("source") or "").split(",") if s.strip()])
    if src_count >= 2:
        fit += 5
        reasons.append(f"seen in {src_count} sources")

    fit = max(0, min(100, fit))
    fit_reason = "; ".join(reasons) if reasons else "general startup, no strong signal either way"

    # --- hiring likelihood / responsiveness -------------------------------
    # Start from any signals the source already attached (e.g. "Posted in HN Who-is-Hiring").
    hs, signals = 0, list(company.get("hiring_signals") or [])
    source = company.get("source") or ""
    if company.get("is_hiring"):
        hs += 45
        if "YC" in source and not any("YC" in s for s in signals):
            signals.append("Marked hiring on YC")
    batch = company.get("batch") or ""
    if recent_batches and batch in recent_batches:
        hs += 20
        signals.append(f"{batch} batch (recent)")
    elif batch:
        hs += 5

    ts = company.get("team_size")
    if isinstance(ts, int) and ts > 0:
        if ts <= 10:
            hs += 20; signals.append(f"Tiny team ({ts}) — reachable founder")
        elif ts <= 30:
            hs += 12; signals.append(f"Small team ({ts})")
        elif ts <= 75:
            hs += 5; signals.append(f"Team of {ts}")
        elif ts > 250:
            hs -= 8

    if (company.get("stage") or "").lower() == "early":
        hs += 10; signals.append("Early stage")

    if job_count:
        hs += min(job_count * 3, 15)
        signals.append(f"{job_count} open role" + ("s" if job_count != 1 else ""))

    if remote and "remote-friendly" not in fit_reason:
        signals.append("Remote-friendly")

    hs = max(0, min(100, hs))
    label = "strong" if hs >= 70 else ("warm" if hs >= 45 else "cold")

    return {
        "fit_score": fit, "fit_reason": fit_reason,
        "hiring_score": hs, "hiring_label": label,
        "hiring_signals": signals,
    }


def rank(companies: list[dict], profile: dict, recent_batches: set[str] | None = None) -> list[dict]:
    """Score every company in place and return best-first (fit, then hiring)."""
    skills = flatten_skills(profile)
    for c in companies:
        c.update(score(c, profile, skills=skills, recent_batches=recent_batches))
    companies.sort(key=lambda c: (-c["fit_score"], -c["hiring_score"]))
    return companies
