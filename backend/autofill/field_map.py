"""Deterministic profile -> value map. The fast path: given a classified field 'kind', return
the answer straight from the profile (no LLM, no embeddings). For choice fields we also return
'candidates' — acceptable phrasings the resolver matches against the field's actual options.

Returns None when the profile can't answer the kind (caller then tries Q&A memory, then LLM).
"""
import re

from .. import secrets

# yes/no answer phrasings, broad enough to match most option sets.
_YES = ["Yes", "Y", "True", "I agree", "Agree", "I consent", "I do", "I acknowledge"]
_NO = ["No", "N", "False", "I do not", "I decline", "Disagree"]


def _name_parts(profile: dict) -> tuple[str, str, str]:
    full = (profile.get("identity", {}).get("legal_name") or "").strip()
    toks = full.split()
    if not toks:
        return "", "", ""
    if len(toks) == 1:
        return toks[0], "", ""
    return toks[0], (" ".join(toks[1:-1])), toks[-1]


def _city_state(profile: dict) -> tuple[str, str]:
    loc = (profile.get("identity", {}).get("location") or "")
    if "," in loc:
        city, state = loc.split(",", 1)
        return city.strip(), state.strip()
    return loc.strip(), ""


def _current_job(profile: dict) -> dict | None:
    jobs = profile.get("work_experience", []) or []
    current = [j for j in jobs if j.get("current")]
    if not current:
        return jobs[0] if jobs else None
    full = [j for j in current if j.get("employment_type") == "full_time"]
    return (full or current)[0]


def _latest_education(profile: dict) -> dict | None:
    eds = profile.get("education", []) or []
    return eds[0] if eds else None


# A degree dropdown almost never uses the abbreviation the profile stores. "BS" has to be able to
# match "Bachelor's", "Bachelor of Science", "Bachelor's Degree" and "Undergraduate" — otherwise a
# question we can obviously answer gets handed back to the user.
_DEGREE_FORMS = {
    "bs": ["Bachelor's", "Bachelor of Science", "Bachelors", "Bachelor's Degree", "Bachelor",
           "BS", "B.S.", "BSc", "Undergraduate", "Undergraduate Degree"],
    "ba": ["Bachelor's", "Bachelor of Arts", "Bachelors", "Bachelor's Degree", "Bachelor",
           "BA", "B.A.", "Undergraduate", "Undergraduate Degree"],
    "ms": ["Master's", "Master of Science", "Masters", "Master's Degree", "Master",
           "MS", "M.S.", "MSc", "Graduate", "Graduate Degree"],
    "ma": ["Master's", "Master of Arts", "Masters", "Master's Degree", "MA", "M.A.", "Graduate"],
    "mba": ["MBA", "Master of Business Administration", "Master's", "Masters", "Graduate"],
    "phd": ["PhD", "Ph.D.", "Doctorate", "Doctoral", "Doctor of Philosophy"],
    "associate": ["Associate's", "Associate Degree", "Associates", "AA", "AS"],
    "high school": ["High School", "High School Diploma", "Secondary School", "GED"],
}


def _degree_forms(degree: str | None) -> list[str]:
    raw = (degree or "").strip()
    if not raw:
        return []
    key = re.sub(r"[^a-z ]+", "", raw.lower()).strip()
    forms = _DEGREE_FORMS.get(key)
    if not forms:
        for k, v in _DEGREE_FORMS.items():
            if k in key or key in k:
                forms = v
                break
    return [raw] + [f for f in (forms or []) if f.lower() != raw.lower()]


def _school_forms(school: str | None) -> list[str]:
    """A school list spells the same campus several ways ("The University of Texas at Austin",
    "University of Texas - Austin"), so offer the obvious variants of what the profile stores."""
    raw = (school or "").strip()
    if not raw:
        return []
    out = [raw]
    for v in (f"The {raw}", raw.replace(" at ", " - "), raw.replace(" at ", ", "),
              re.sub(r"^The\s+", "", raw)):
        if v and v.lower() != raw.lower() and v not in out:
            out.append(v)
    return out


def resolve(kind: str, profile: dict) -> dict | None:
    ident = profile.get("identity", {})
    auth = profile.get("work_auth", {})
    ra = profile.get("reusable_answers", {})
    disc = profile.get("disclosures", {})
    login = profile.get("login_credentials", {})
    first, middle, last = _name_parts(profile)
    city, state = _city_state(profile)

    def text(v):
        return {"value": (v or "").strip(), "candidates": []} if (v or "").strip() else None

    def choice(value, candidates):
        return {"value": value, "candidates": candidates}

    yn = lambda flag: choice("Yes", _YES) if flag else choice("No", _NO)

    # ---- identity ----
    simple = {
        "first_name": first, "middle_name": middle, "last_name": last,
        "full_name": ident.get("legal_name"), "preferred_name": ident.get("preferred_name") or first,
        "email": ident.get("email"), "phone": ident.get("phone"),
        "street_address": ident.get("street_address"), "city": city, "state": state,
        "location": ident.get("location"), "postal_code": ident.get("zip"),
        "linkedin": ident.get("linkedin"), "github": ident.get("github"),
        "portfolio": ident.get("portfolio"), "website": ident.get("portfolio"),
        "username": login.get("email") or ident.get("email"),
        "pronouns": ident.get("pronouns"),
    }
    if kind in simple:
        return text(simple[kind])

    if kind == "password":
        # Keychain/env only (never profile.json). None -> resolver's needs_review path.
        return text(secrets.login_password(profile))

    if kind == "country":
        return choice("United States", ["United States", "United States of America", "USA", "US"])
    if kind == "country_phone_code":
        return choice("+1", ["+1", "United States (+1)", "US (+1)", "1"])
    if kind == "phone_device_type":
        return choice("Mobile", ["Mobile", "Cell", "Mobile Phone", "Cell Phone"])

    # ---- work authorization ----
    if kind == "work_authorization":
        authorized = auth.get("us_work_auth_status") in ("citizen", "permanent_resident",
                                                          "authorized", "ead")
        cands = _YES + ["Authorized to work", "I am authorized to work in the United States",
                        "Permanent Resident", "Green Card Holder"]
        return choice("Yes", cands) if authorized else yn(False)
    if kind == "needs_sponsorship":
        return yn(auth.get("needs_sponsorship") == "yes")
    if kind == "age_18_plus":
        return choice("Yes", _YES)                       # user is an adult college student
    if kind == "worked_outside_us":
        return yn(False)                                 # user has never worked outside the US
    if kind == "security_clearance":
        sc = (auth.get("security_clearance") or "").lower()
        if sc in ("", "none", "no"):
            return choice("None", ["None", "No clearance", "N/A", "Not applicable", "No"])
        return text(auth.get("security_clearance"))
    if kind == "export_control":
        # "Are you a US person for export-control purposes?" — a citizen OR permanent resident is.
        if auth.get("us_work_auth_status") in ("citizen", "permanent_resident"):
            return choice("Yes", _YES + ["U.S. Person", "US Person"])
        return None
    if kind == "citizenship":
        st = auth.get("us_work_auth_status")
        if st == "citizen":
            return choice("U.S. Citizen", ["U.S. Citizen", "United States Citizen", "Yes", "Citizen"])
        if st == "permanent_resident":
            return choice("Permanent Resident",
                          ["Permanent Resident", "Green Card Holder",
                           "Lawful Permanent Resident", "Permanent Resident of the U.S.",
                           "U.S. Permanent Resident", "No"])
        return None
    if kind == "us_residence":
        # Known fact: lives in the United States.
        return choice("Yes", _YES + ["United States", "United States of America", "USA", "US"])

    # ---- EEO / demographics ----
    if kind == "gender":
        g = (auth.get("gender") or "").capitalize()
        return choice(g, [g, auth.get("gender", "")]) if g else None
    if kind == "race_ethnicity":
        races = auth.get("race_ethnicity") or []
        return choice(races[0], races) if races else None
    if kind == "hispanic_latino":
        is_hl = any("hispanic" in r.lower() or "latino" in r.lower()
                    for r in (auth.get("race_ethnicity") or []))
        return choice("Yes", _YES) if is_hl else choice(
            "No", _NO + ["Not Hispanic or Latino", "No, not Hispanic or Latino"])
    if kind == "veteran_status":
        if auth.get("veteran_status") == "not_veteran":
            return choice("I am not a veteran",
                          ["I am not a veteran", "Not a veteran",
                           "I am not a protected veteran", "No"])
        return None
    if kind == "disability_status":
        if auth.get("disability_status") == "no":
            return choice("No",
                          ["No", "No, I do not have a disability",
                           "No, I don't have a disability"])
        return None
    if kind == "lgbtq":
        # Optional self-ID. Answered only because the profile states it outright; "no" here means
        # "does not self-identify as LGBTQ+", which these forms word as a plain No.
        v = (auth.get("lgbtq_self_id") or "").lower()
        if v == "no":
            return choice("No", _NO + ["I do not identify as LGBTQ+", "Prefer not to say"])
        if v == "yes":
            return choice("Yes", _YES)
        return None

    # ---- education ----
    ed = _latest_education(profile)
    if ed:
        if kind == "degree":
            return choice(ed.get("degree") or "", _degree_forms(ed.get("degree")))
        if kind == "school":
            return choice(ed.get("school") or "", _school_forms(ed.get("school")))
        emap = {
            "major": ed.get("major"),
            "gpa": ed.get("gpa"),
            # A form ASKING for grad date is filled (distinct from the resume, which omits it).
            "grad_date": ed.get("end_date"),
        }
        if kind in emap:
            return text(emap[kind])

    # ---- current employment ----
    job = _current_job(profile)
    if job:
        if kind == "current_company":
            return text(job.get("company"))
        if kind == "current_title":
            return text(job.get("title"))

    # ---- logistics (reusable answers) ----
    if kind == "start_date":
        return text(ra.get("earliest_start_date"))
    if kind == "notice_period":
        return text(ra.get("notice_period"))
    if kind == "salary_expectation":
        return text(ra.get("salary_expectation"))
    if kind == "willing_to_relocate":
        v = (ra.get("willing_to_relocate") or "").lower()
        return yn(v.startswith("y")) if v else None
    if kind == "willing_to_travel":
        v = (ra.get("willing_to_travel") or "").lower()
        return yn(v.startswith("y")) if v else None
    if kind == "work_model":
        pref = ra.get("work_model_preference") or ""
        if pref in ("", "no_preference"):
            return choice("No preference", ["No preference", "Flexible", "Open to any", "Hybrid"])
        nice = pref.replace("_", " ").title()
        return choice(nice, [nice, pref])
    if kind == "student_status":
        # An education entry whose end date is in the future = still enrolled.
        return choice("Yes", _YES + ["Currently enrolled", "Full-time student"]) if ed else None
    if kind == "reliable_transportation":
        v = (ra.get("reliable_transportation") or "").lower()
        return yn(v.startswith("y")) if v else None
    if kind == "how_heard":
        # user's preference: always answer "Other" here, not a specific channel.
        return choice("Other", ["Other", "Others", "Other (please specify)", "Other source"])

    # ---- disclosures / consent ----
    # NOTE: related_to_employee ("do you know anyone who works here") is deliberately NOT here —
    # the user wants referral/who-you-know questions LEFT BLANK and highlighted to answer by hand.
    disc_yn = {
        "background_check_consent": disc.get("background_check_consent"),
        "drug_test_consent": disc.get("drug_test_consent"),
        "criminal_conviction": disc.get("criminal_conviction"),
        "non_compete": disc.get("non_compete"),
        "nda": disc.get("nda"),
        # These three live in profile.json but had no classifier rule pointing at them until now,
        # so "do you have a contract with another company?" fell through to a blank field.
        "company_contracts": disc.get("company_contracts"),
        "fired_or_discharged": disc.get("fired_or_discharged"),
        "conflicts_of_interest": disc.get("conflicts_of_interest"),
    }
    if kind in disc_yn and disc_yn[kind] in ("yes", "no"):
        return yn(disc_yn[kind] == "yes")

    # ---- misc ----
    if kind == "languages":
        langs = profile.get("application_languages") or []
        return text(", ".join(langs)) if langs else None

    return None
