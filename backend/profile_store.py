"""Loads the master profile. The profile is EVIDENCE, never a template the AI copies from.

We only ever read it here (per-job resumes are derivatives). The one write path is the
keyword recommender adding user-approved concrete skills to skills_extra.

A fresh clone has no profile: the data dir lives outside the repo and is never committed. So
load() seeds an empty-but-complete skeleton on first read instead of raising FileNotFoundError,
which used to 500 /profile and leave the website's Resume tab blank with no explanation.
"""
import json
from . import config

# Every key the website's editor renders. The flat sections (reusable_answers, disclosures,
# work_auth) build their fields from the keys PRESENT in the file, so a key missing here is a
# field the user can never fill in — keep this in sync with frontend/app.js SECTIONS.
BLANK_PROFILE: dict = {
    "identity": {k: "" for k in ("legal_name", "preferred_name", "pronouns", "email", "phone",
                                 "location", "street_address", "zip", "linkedin", "github",
                                 "portfolio")} | {"other_links": []},
    "work_auth": {k: "" for k in ("us_work_auth_status", "needs_sponsorship", "veteran_status",
                                  "disability_status", "gender", "security_clearance",
                                  "lgbtq_self_id")} | {"race_ethnicity": []},
    "education": [],
    "work_experience": [],
    "projects": [],
    "skills": {k: [] for k in ("programming_languages", "frameworks", "tools", "cloud",
                               "databases", "ml_ai", "hardware_embedded", "spoken_languages",
                               "soft_skills", "ai_tools")},
    "skills_extra": {k: [] for k in ("programming_languages", "frameworks", "tools", "cloud",
                                     "databases", "ml_ai", "hardware_embedded",
                                     "spoken_languages", "soft_skills", "ai_tools")},
    "certifications": [],
    "publications": [],
    "activities": [],
    "reusable_answers": {k: "" for k in (
        "tell_me_about_yourself", "why_company", "challenge_overcome", "greatest_strength",
        "greatest_weakness", "why_hire_you", "salary_expectation", "earliest_start_date",
        "notice_period", "willing_to_relocate", "work_model_preference", "willing_to_travel",
        "heard_about_us_default")},
    "references": [],
    "ai_preferences": {"tone": "professional-punchy", "voice": "no-pronoun", "always_use": [],
                       "never_use": [], "default_focus_angles": [], "include_photo": False},
    "disclosures": {k: "" for k in (
        "non_compete", "non_compete_detail", "fired_or_discharged", "fired_detail",
        "company_contracts", "company_contracts_detail", "nda", "nda_detail",
        "conflicts_of_interest", "conflicts_detail", "related_to_employee",
        "background_check_consent", "drug_test_consent", "criminal_conviction",
        "criminal_detail")},
    "application_languages": [],
    "login_credentials": {"email": ""},
}


def blank() -> dict:
    """A fresh copy of the skeleton — never hand out the module-level dict itself."""
    return json.loads(json.dumps(BLANK_PROFILE))


def is_empty(profile: dict) -> bool:
    """True when nothing has been filled in yet — no name and no experience to draw on."""
    ident = profile.get("identity") or {}
    return not (ident.get("legal_name") or "").strip() and not any(
        profile.get(k) for k in ("work_experience", "projects", "education"))


def load() -> dict:
    """Read the master profile, seeding an empty skeleton the first time (fresh install)."""
    if not config.PROFILE_PATH.exists():
        seeded = blank()
        config.PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
        config.PROFILE_PATH.write_text(json.dumps(seeded, indent=2), encoding="utf-8")
        return seeded
    with open(config.PROFILE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save(profile: dict) -> None:
    # Back up before overwriting — the master profile is the source of truth.
    config.PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    bak = config.PROFILE_PATH.with_suffix(".json.bak")
    if config.PROFILE_PATH.exists():
        bak.write_text(config.PROFILE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = config.PROFILE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(config.PROFILE_PATH)


def all_skills(profile: dict) -> set[str]:
    """Flat lowercase set of everything in skills + skills_extra, for cross-referencing."""
    out: set[str] = set()
    for bucket in ("skills", "skills_extra"):
        for group in (profile.get(bucket) or {}).values():
            for s in group or []:
                if isinstance(s, str) and s.strip():
                    out.add(s.strip().lower())
    return out


def blocklist(profile: dict) -> set[str]:
    """Lowercase set of skills the user has marked NEVER to add/recommend/show."""
    return {
        s.strip().lower()
        for s in (profile.get("skills_blocklist") or [])
        if isinstance(s, str) and s.strip()
    }


def add_to_blocklist(profile: dict, items: list[str]) -> dict:
    """Mark skills as never-add. Also removes them from skills/skills_extra if present."""
    bl = profile.setdefault("skills_blocklist", [])
    have = {s.lower() for s in bl}
    for it in items:
        t = it.strip()
        if t and t.lower() not in have:
            bl.append(t)
            have.add(t.lower())
    # purge any blocked term that currently lives in the skill pool
    blocked = {s.lower() for s in bl}
    for bucket in ("skills", "skills_extra"):
        for group, vals in (profile.get(bucket) or {}).items():
            profile[bucket][group] = [s for s in vals if s.strip().lower() not in blocked]
    return profile


def remove_from_blocklist(profile: dict, items: list[str]) -> dict:
    drop = {i.strip().lower() for i in items}
    profile["skills_blocklist"] = [
        s for s in (profile.get("skills_blocklist") or []) if s.strip().lower() not in drop
    ]
    return profile


def add_skills(profile: dict, group: str, items: list[str]) -> dict:
    """Add user-approved concrete skills into skills_extra[group]. No duplicates."""
    extra = profile.setdefault("skills_extra", {})
    bucket = extra.setdefault(group, [])
    have = all_skills(profile)
    blocked = blocklist(profile)
    for it in items:
        key = it.strip().lower()
        if it.strip() and key not in have and key not in blocked:
            bucket.append(it.strip())
            have.add(key)
    return profile
