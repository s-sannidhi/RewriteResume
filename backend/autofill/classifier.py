"""Field classifier. Given a form field descriptor (label/name/type/options), decide WHAT it
is — a semantic 'kind' like first_name, work_authorization, why_company_essay. This is the
fast, deterministic front door of the autofill chain: rules + HTML autocomplete hints, no LLM.

A FieldDescriptor is what the extension scrapes from one input:
    {label, name, type, placeholder, autocomplete, options:[...], required}
"""
import re

# HTML autocomplete tokens are the strongest, cheapest signal when present.
_AUTOCOMPLETE = {
    "given-name": "first_name", "additional-name": "middle_name", "family-name": "last_name",
    "name": "full_name", "email": "email", "tel": "phone", "tel-national": "phone",
    "street-address": "street_address", "address-line1": "street_address",
    "address-line2": "do_not_fill", "address-level2": "city", "address-level1": "state",
    "postal-code": "postal_code", "country": "country", "country-name": "country",
    "organization": "current_company", "organization-title": "current_title",
    "url": "website", "sex": "gender", "username": "username",
    "current-password": "password", "new-password": "password",
}

# Ordered (specific → generic). First regex that hits the haystack wins.
# haystack = lowercased "label | name | placeholder | aria".
_RULES: list[tuple[str, str]] = [
    # --- essays / free-text (check BEFORE generic name/role words) ---
    ("why_company_essay", r"why (do you (want|wish) to|are you interested).*(work|join|company|us|here)|why (this )?(company|role|us|position)|interest in (this|our|the) (company|role|position)"),
    ("tell_me_about_yourself", r"tell us about yourself|about yourself|introduce yourself|tell me about you"),
    ("challenge_essay", r"challeng|difficult (situation|problem|project)|obstacle you (faced|overcame)"),
    ("strength_essay", r"greatest strength|your strengths|biggest strength"),
    ("weakness_essay", r"greatest weakness|your weakness|area.*improve"),
    ("why_hire_essay", r"why should we (hire|choose)|what makes you|why are you (a )?(good|the best) (fit|candidate)"),
    ("cover_letter", r"cover letter"),
    ("additional_info_essay", r"anything else|additional (information|comments)|is there anything"),
    # --- file uploads (cover letter FIRST: "resume or cover letter" areas must not grab it) ---
    ("cover_letter_upload", r"cover\s*letter|coverletter|letter of (interest|introduction|motivation)"),
    ("resume_upload", r"r[eé]sum[eé]|\bcv\b|curriculum vitae"),
    ("transcript_upload", r"transcript|academic record|grade report"),
    ("portfolio_upload", r"portfolio|work sample|writing sample|\bdemo reel\b"),
    ("other_upload", r"(upload|attach|choose|select|drag).{0,20}(a |your )?(file|document)|supporting document|additional document"),
    # --- identity ---
    ("preferred_name", r"preferred (name|first name)|nickname|goes by"),
    ("first_name", r"first[\s_-]?name|given[\s_-]?name|legal first"),
    ("middle_name", r"middle[\s_-]?name|middle initial"),
    ("last_name", r"last[\s_-]?name|family[\s_-]?name|surname|legal last"),
    ("full_name", r"full[\s_-]?name|legal name|your name|^name$|candidate name"),
    ("email", r"e-?mail"),
    ("phone_device_type", r"phone (device )?type|device type"),
    ("country_phone_code", r"country (phone )?code|phone country|dial(ling)? code|country code"),
    # A SECOND phone (home/work/alternate/fax) — user has one mobile number; leave these blank.
    ("do_not_fill", r"(secondary|second|2nd|alternat(e|ive)|other|home|work|business|office|evening|daytime|\bday\b|night|fax)\s*(phone|telephone|tel\b|number|fax|line)"),
    ("phone", r"phone|mobile|cell|telephone|contact number"),
    # --- address ---
    # A SECOND / distinct address block, or apt/line-2 — user gives one address; leave these blank.
    ("do_not_fill", r"(secondary|second|2nd|other|previous|former|prior)\s*address"),
    ("do_not_fill", r"address (line )?2|\bapt\b|apartment|\bsuite\b|\bunit\b"),
    ("street_address", r"street|address (line )?1|^address$|mailing address|home address"),
    ("city", r"\bcity\b|town|address level 2"),
    ("county", r"\bcounty\b"),
    ("state", r"\bstate\b|province|region|address level 1"),
    ("postal_code", r"postal|zip[\s_-]?code|\bzip\b|post code"),
    ("country", r"\bcountry\b|nationality(?!.*work)"),
    ("location", r"current location|where.*located|location|city.*state"),
    # --- links ---
    ("linkedin", r"linked\s?in"),
    ("github", r"git\s?hub"),
    ("portfolio", r"portfolio|personal (web)?site|personal page"),
    ("website", r"website|web ?site|personal url|^url$|^link$"),
    # --- work authorization & citizenship ---
    # "Are you authorized to work in the US WITHOUT sponsorship?" means YES for this candidate,
    # while "Will you REQUIRE sponsorship?" means NO. Same keywords, opposite answers — so the
    # combined phrasing is matched first and routed to work_authorization (a Yes), never to
    # needs_sponsorship (a No). Getting this backwards is the single most costly autofill error.
    ("work_authorization", r"(authoriz|eligib|legal|permitt?ed|able).{0,45}without.{0,25}(sponsor|visa)"),
    ("work_authorization", r"work.{0,25}without.{0,25}(sponsor|visa|restriction)"),
    ("needs_sponsorship", r"sponsor|require.{0,25}visa|visa.{0,25}(require|support|status change)|"
                          r"\bh-?1-?b\b|\bopt\b|\bcpt\b|\bf-?1\b|\bj-?1\b|immigration (support|sponsor)"),
    ("work_authorization", r"(legally |lawfully )?authoriz(ed|ation).{0,25}work|eligib(le|ility).{0,25}work|"
                           r"right to work|work auth|permitt?ed to work|legally (able|entitled) to work|"
                           r"authoriz(ed|ation) (for|to be) employ"),
    ("worked_outside_us", r"work(ed|ing)?\b.{0,25}(outside|abroad|international).{0,20}(u\.?s\.?a?\b|united states|country)|(outside|abroad).{0,15}(united states|u\.?s\.?a?\b)|employ(ed|ment)\b.{0,20}(outside|abroad)"),
    ("age_18_plus", r"(at least|over|older than|minimum|are you)\b.{0,12}\b(18|eighteen)\b|\b(18|eighteen)\b\s*(years?)?\s*(of age|or older|or over|and older|\+)"),
    ("security_clearance", r"security clearance|clearance level|\bts/sci\b|(hold|have|possess).{0,20}clearance"),
    ("export_control", r"export control|\bitar\b|export administration regulation|deemed export"),
    ("citizenship", r"citizen(ship)?|are you a us citizen|immigration status|visa status|"
                    r"what is your (work|employment) status|permanent resident|green ?card"),
    ("us_residence", r"(live|reside|based|located|living).{0,25}(in|within).{0,15}(the )?(united states|u\.?s\.?a?\b|usa\b)|"
                     r"us (resident|residence)|reside in the u"),
    # --- EEO / demographics ---
    ("hispanic_latino", r"hispanic|latino|latinx"),
    ("race_ethnicity", r"race|ethnic(ity)?"),
    ("pronouns", r"pronoun"),
    ("gender", r"gender|sex(?!ual)|^male/female"),
    ("veteran_status", r"veteran|military service|protected veteran"),
    ("disability_status", r"disab(led|ility)"),
    ("lgbtq", r"lgbtq|sexual orientation"),
    # --- education ---
    ("school", r"school|university|college|institution"),
    ("degree", r"degree|qualification level"),
    ("major", r"major|field of study|discipline|concentration"),
    ("gpa", r"gpa|grade point"),
    ("grad_date", r"grad(uation)?( date| year)?|expected graduation|completion date"),
    # --- referral / who-you-know (ALWAYS left blank for the user; MUST beat current_company) ---
    ("related_to_employee", r"who do you know|do you know (anyone|someone|any current)|anyone you know|employee referral|referr?al.{0,12}(name|who|contact|employee)|relat(ive|ed).{0,20}(employee|compan)|know anyone.{0,15}(work|compan|here)"),
    # --- restrictive agreements & employment-history disclosures ---
    # These MUST precede "current employment": current_company matches the bare word "company", so
    # "Do you have a contract with another company?" would otherwise be answered with the user's
    # employer name instead of "No".
    ("non_compete", r"non-?compet|noncompet|non-?solicit|nonsolicit"),
    ("nda", r"non-?disclosure|\bnda\b|confidentiality agreement"),
    ("company_contracts", r"(contract|agreement|obligation|commitment|covenant).{0,45}"
                          r"(with|to|from).{0,25}(another|other|any other|current|previous|former|third)"
                          r".{0,15}(employer|compan|organi|firm|party)|"
                          r"(bound|subject|party|obligated).{0,25}(by|to).{0,35}"
                          r"(agreement|contract|covenant|restriction|obligation)|"
                          r"restrictive covenant|"
                          r"(agreement|contract).{0,35}(prevent|restrict|prohibit|limit|preclude|interfere)"),
    ("conflicts_of_interest", r"conflict of interest|competing interest|"
                              r"outside (business|employment|work) (activit|interest)|financial interest in"),
    ("fired_or_discharged", r"(ever been|been|were you).{0,20}(terminated|discharged|fired|dismissed|let go)|"
                            r"asked to resign|involuntar(il)?y.{0,20}(terminat|separat|resign)|"
                            r"(left|leave).{0,20}involuntar"),
    # The qualifier ("ever", "previously", "before") can sit on EITHER side of the verb —
    # "have you EVER worked here" and "have you worked here BEFORE" are the same question — so the
    # employed-at-this-company phrase alone is enough to match. Work-authorization ("authorized to
    # work here") and referral ("know anyone who works here") rules are both tested earlier, so
    # they still win their own phrasings.
    ("previously_employed", r"(employ(ed|ment)?|work(ed|ing)?).{0,25}"
                            r"(here|with us|for us|by us|at this|for this|with this|"
                            r"at our|for our|with our)|"
                            r"former (employee|intern)|re-?hire|"
                            r"(previous|prior|former).{0,15}(employment|application).{0,25}(with|at) (us|this|our)"),
    # --- current employment ---
    ("current_title", r"(current |present )?(job )?title|position title|your title|role title"),
    ("current_company", r"(current |present )?(employer|company|organization)|where.*work"),
    ("years_experience", r"years.*(experience|exp)|experience.*years"),
    # --- logistics ---
    ("salary_expectation", r"salary|compensation|expected pay|desired (pay|salary|rate)|"
                            r"pay expectation|hourly rate|rate expectation"),
    ("start_date", r"start date|available.{0,20}start|when (can|could|are) you.{0,15}(start|begin|available)|"
                   r"availability date|earliest.{0,15}start|date available"),
    ("reliable_transportation", r"reliable transportation|own transportation|means of transportation"),
    ("student_status", r"(currently|presently).{0,15}(a )?(student|enrolled)|are you enrolled|"
                       r"current student|student status"),
    ("notice_period", r"notice period"),
    ("willing_to_relocate", r"relocat"),
    ("work_model", r"remote|hybrid|on-?site|work model|work (location )?preference"),
    ("willing_to_travel", r"willing to travel|travel requirement|able to travel"),
    ("how_heard", r"how did you (hear|find|learn)|referr?al source|\bsource\b|"
                  r"where did you (hear|learn|find|see)|how were you referred|how were you introduced"),
    # --- disclosures / consent ---
    ("criminal_conviction", r"crimin|convict(ed|ion)|felony|misdemeanor|pled (guilty|no contest)|"
                            r"background.{0,20}(disclose|history)"),
    ("background_check_consent", r"background (check|investigation|screen)|consent.{0,25}background"),
    ("drug_test_consent", r"drug (test|screen)|substance (test|screen)|pre-?employment screen"),
    # Legal attestations ("I certify the above is true", "I agree to the terms") are deliberately
    # NOT auto-checked — see resolver._LEAVE_BLANK. Classified only so they can be highlighted.
    ("consent_acknowledgement", r"\bi (certify|agree|acknowledge|consent|confirm|attest|declare|understand)\b|"
                                r"certify that|terms (and|&) conditions|terms of (use|service)|"
                                r"privacy (policy|notice|statement)|i have read|by (checking|submitting|clicking)"),
    # --- account ---
    ("password", r"password|passcode"),
    ("username", r"user\s?name|user id|login"),
    ("employee_id", r"employee id"),
    # --- misc ---
    ("languages", r"languages? (you )?(speak|know|spoken)|spoken language|language proficiency"),
    ("references", r"references?\b"),
    ("date_of_birth", r"date of birth|\bdob\b|birth date"),
]

_COMPILED = [(kind, re.compile(pat)) for kind, pat in _RULES]


def _haystack(field: dict) -> str:
    parts = [field.get("label", ""), field.get("name", ""), field.get("placeholder", ""),
             field.get("aria_label", "")]
    return " | ".join(p for p in parts if p).lower()


# For <input type=file> ONLY. A file input labelled "Cover Letter" must resolve to an UPLOAD, not
# to the cover-letter ESSAY rule that (correctly) wins for a textarea with the same words — so file
# inputs are matched against this list alone rather than the general one.
_UPLOAD_RULES = [(k, p) for k, p in _RULES if k.endswith("_upload")]
_COMPILED_UPLOAD = [(kind, re.compile(pat)) for kind, pat in _UPLOAD_RULES]


def classify(field: dict) -> tuple[str, float]:
    """Return (kind, confidence). 'unknown' if nothing matches."""
    ac = (field.get("autocomplete") or "").strip().lower()
    if ac in _AUTOCOMPLETE:
        return _AUTOCOMPLETE[ac], 0.98

    hay = _haystack(field)

    # A file input is an upload, full stop — the only question is WHICH document. An unlabelled
    # one still returns a kind so the uploader can fall back to page position / "it's the only
    # upload field here" reasoning.
    if (field.get("type") or "").lower() == "file":
        for kind, rx in _COMPILED_UPLOAD:
            if rx.search(hay):
                return kind, 0.9 if rx.search((field.get("label") or "").lower()) else 0.75
        return "other_upload", 0.3

    if not hay.strip():
        return "unknown", 0.0

    for kind, rx in _COMPILED:
        if rx.search(hay):
            # Label hits are more trustworthy than name-attribute hits.
            conf = 0.9 if rx.search((field.get("label") or "").lower()) else 0.78
            return kind, conf

    # Long free-text with no rule hit is probably an essay we should let the LLM handle.
    if field.get("type") == "textarea":
        return "generic_essay", 0.4
    return "unknown", 0.0
