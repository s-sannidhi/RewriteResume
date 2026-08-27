"""AI networking message generator (outreach Phase 4).

Turns the Phase-2 company intel + the Phase-3 person + the user's real background into a concise,
human-sounding outreach built around ONE genuine reason — never a generic "I'm a CS student
looking for an internship." Local gemma3 writes prose only; Python supplies the facts and the
angle. Same anti-fabrication guardrails as intel.py (no invented metrics, no claimed skills the
user doesn't have). Nothing is sent — the message generator only drafts; the user approves.

Produces both channels so either is ready: a LinkedIn connection note (<=300 chars) and an email
(subject + body).
"""
import re

from .. import ask, llm
from ..resume import jd_signals
from . import intel, ranking

MSG_TYPES = ("founder_cold", "engineer_networking", "alumni", "post_reply", "follow_up", "referral")

_TYPE_LABEL = {
    "founder_cold": "Founder cold outreach", "engineer_networking": "Engineer networking",
    "alumni": "Alumni connection", "post_reply": "Response to a post/announcement",
    "follow_up": "Follow-up", "referral": "Referral request",
}


def auto_type(person: dict) -> str:
    """Pick the message type from the person's role + shared context."""
    rel = " ".join(person.get("relationship") or []).lower()
    if "university" in rel or "school" in rel:
        return "alumni"
    bucket = person.get("role_bucket") or ""
    if bucket == "founder_cto":
        return "founder_cold"
    if bucket in ("engineer", "eng_manager"):
        return "engineer_networking"
    if bucket == "recruiter":
        return "referral"
    return "founder_cold"


def application_angle(company: str, jd_analysis: dict, profile: dict) -> str:
    """Deterministic, honest angle for a company you already applied to: the real role title, plus
    one real skill overlap between the JD and the candidate's own background if there is one.
    No LLM here — Python picks the fact, the LLM only writes prose around it (generate_message)."""
    role = (jd_analysis.get("role_title") or "").strip()
    base = f"I just applied for the {role} role at {company}" if role else f"I just applied at {company}"
    # len>1 excludes single-letter language tokens ("c", "r") — jd_signals' word-boundary match
    # on those is prone to false positives (e.g. matching "c" inside "Washington D.C."), and a
    # wrong tech claim in a real outreach message is worse than a slightly more generic angle.
    overlap = sorted(t for t in (jd_signals.jd_terms(jd_analysis) if jd_analysis else set())
                     & ranking.flatten_skills(profile) if len(t) > 1)
    if overlap:
        return (f"{base} and wanted to reach out directly, my background with {overlap[0]} "
                "lines up with what the role needs")
    return f"{base} and wanted to reach out directly and introduce myself"


_SYSTEM = (
    "You draft short, genuine, human networking messages for a specific CS student reaching out to "
    "someone at a startup. You are given only real facts. HARD RULES: (1) build the whole message "
    "around the ONE genuine reason provided (the angle) — a shared technology, a project that maps "
    "to their product, a domain overlap, a shared school. (2) Never write a generic 'I'm a CS "
    "student looking for an internship' line. (3) Never invent facts, metrics, funding, or claim a "
    "skill/experience not in the student's background. (4) Sound like a real person: warm, direct, "
    "specific, humble — not salesy or flattering, no buzzwords, no em-dash-laden hype. (5) One "
    "concrete, low-pressure ask (a quick chat, advice, or whether they're taking interns). Keep the "
    "email body ~5-7 short sentences. The LinkedIn note must be <=300 characters. (6) Write the "
    "FINAL text only — never leave placeholders, brackets, or fill-in instructions like "
    "'[project]' or '[mention...]'; if you reference a project, name a real one from the student's "
    "list or omit it. Output JSON only."
)


def _strip_placeholders(text: str) -> str:
    """Remove bracketed fill-in artifacts, and hard-enforce the humanizer's #1 rule (no em/en
    dashes) in case the model slips one past the prompt."""
    text = re.sub(r"\s*[\[\(][^\]\)]*(?:mention|insert|project|your|name|e\.g\.|company)[^\]\)]*[\]\)]",
                  "", text or "", flags=re.I)
    text = re.sub(r"\s*[—–]\s*", ", ", text)        # em/en dash -> comma (the top AI tell)
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r"\s{2,}", " ", text)
    return re.sub(r"\s+([,.!?])", r"\1", text).strip()


def _profile_brief(profile: dict) -> str:
    ident = profile.get("identity", {}) or {}
    skills = sorted(ranking.flatten_skills(profile))
    projects = [f"{p.get('name','')}: {(p.get('description') or p.get('summary') or '')[:110]}"
                for p in (profile.get("projects") or [])][:5]
    work = [f"{w.get('title','')} at {w.get('company','')}"
            for w in (profile.get("work_experience") or [])][:4]
    edu = (profile.get("education") or [{}])[0]
    import json
    return json.dumps({
        "name": ident.get("legal_name") or ident.get("preferred_name") or "",
        "school": edu.get("school", ""), "major": edu.get("major", ""),
        "grad": edu.get("end_date", ""),
        "skills": skills[:36], "projects": projects, "experience": work,
        "github": ident.get("github", ""), "portfolio": ident.get("portfolio", ""),
        "linkedin": ident.get("linkedin", ""),
    }, ensure_ascii=False)


def _clip_note(note: str, limit: int = 300) -> str:
    note = (note or "").strip()
    if len(note) <= limit:
        return note
    cut = note[:limit]
    return cut[:cut.rfind(" ")].rstrip(" ,.;") + "…" if " " in cut else cut


def generate_message(company: dict, person: dict, profile: dict, msg_type: str = "auto") -> dict:
    """Draft a founder/engineer/alumni/etc. outreach for one person. Returns
    {subject, email_body, linkedin_note, msg_type, angle}."""
    import json
    if msg_type == "auto" or msg_type not in MSG_TYPES:
        msg_type = auto_type(person)

    intel_blob = company.get("intel") if isinstance(company.get("intel"), dict) else {}
    # Prefer the Phase-2 intel angle; fall back to the ranking's fit_reason so a message still has
    # a genuine hook even when full intel hasn't been generated for this company.
    angle = (intel_blob.get("best_angle") or company.get("fit_reason") or "").strip()
    why = (intel_blob.get("why_this_company") or "").strip()

    facts = {
        "message_type": _TYPE_LABEL[msg_type],
        "company": company.get("name", ""),
        "what_they_do": company.get("one_liner", "") or (intel_blob.get("dossier", {}) or {}).get("description", "")[:200],
        "the_angle": angle or "shared interest in what the company is building",
        "why_relevant": why,
        "person_name": person.get("name", ""),
        "person_role": person.get("title", "") or person.get("role_bucket", ""),
        "shared_context": person.get("relationship") or [],
    }
    user = ("STUDENT (the sender):\n" + _profile_brief(profile)
            + "\n\nOUTREACH FACTS:\n" + json.dumps(facts, ensure_ascii=False)
            + "\n\nWrite the message as this student, to this person, of the given message_type, "
              "built around the_angle. Sign the email with the student's first name. "
              'Return JSON: {"subject": "email subject line, specific, no clickbait", '
              '"email_body": "the email, greeting to the person by first name, ~5-7 short sentences, '
              'one genuine reason, one clear low-pressure ask, sign-off with the student first name", '
              '"linkedin_note": "<=300 char connection note, even more concise, same genuine reason"}')
    # Same human-sounding ruleset as the Ask tab (no em-dashes / AI tells) — the user's standard.
    system = _SYSTEM + "\n\n" + ask._HUMANIZE
    try:
        out = llm.chat_json(system, user, temperature=0.5)
    except Exception as e:
        return {"subject": "", "email_body": "", "linkedin_note": "", "msg_type": msg_type,
                "angle": angle, "error": str(e)}
    return {
        "subject": _strip_placeholders(out.get("subject") or ""),
        "email_body": _strip_placeholders(out.get("email_body") or ""),
        "linkedin_note": _clip_note(_strip_placeholders(out.get("linkedin_note") or "")),
        "msg_type": msg_type,
        "angle": angle,
    }
