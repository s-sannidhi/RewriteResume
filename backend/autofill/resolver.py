"""The autofill resolver — turns a field descriptor into a typed ACTION.

Resolution order (fastest first), per the autofill strategy:
  1. classify (rules)            -> semantic kind, instant
  2. deterministic profile map   -> value, instant
  3. Q&A semantic memory         -> past confirmed answer, ~embedding lookup
  4. local-model generation      -> ONLY rare free-text essays / cover letters

Output ACTION:
  {field_id, field_kind, action, value, option, source, confidence, needs_review, qa_id, reason}
  action ∈ fill | select | type_then_pick | upload | skip
"""
import re
from .. import llm, profile_store
from ..store import qa_memory
from . import classifier, field_map
from .field_map import _NO, _YES

# essay kind -> reusable_answers hint key (a starting point the LLM adapts, never pastes)
_ESSAY_HINT = {
    "why_company_essay": "why_company",
    "tell_me_about_yourself": "tell_me_about_yourself",
    "challenge_essay": "challenge_overcome",
    "strength_essay": "greatest_strength",
    "weakness_essay": "greatest_weakness",
    "why_hire_essay": "why_hire_you",
}
_ESSAY_KINDS = set(_ESSAY_HINT) | {"cover_letter", "additional_info_essay", "generic_essay"}
_UPLOAD_KINDS = {"resume_upload", "cover_letter_upload", "transcript_upload", "portfolio_upload",
                 "other_upload"}
_CHOICE_TYPES = {"select", "radio", "checkbox", "combobox", "multiselect"}
# Widget flavours the page can drive and then CONFIRM (see extension/autofill.js).
_PICKABLE = {"native_select", "listbox_button", "typeahead", "checkbox", "radio"}
# Always leave these for the user, even if profile/memory could answer — referral / who-you-know
# questions the user insists on handling by hand (blank + highlighted, never auto-answered).
# Legal attestations are in here for a different reason than referrals: ticking "I certify that
# everything above is true" is the user making a statement under their own name, so a machine must
# never tick it for them. Highlighted instead, so it's impossible to miss before submitting.
_LEAVE_BLANK = {"related_to_employee", "consent_acknowledgement", "export_control"}
_BLANK_REASON = {
    "related_to_employee": "answer this yourself (referral / who-you-know)",
    "consent_acknowledgement": "legal attestation — tick this yourself",
    "export_control": "export-control question — answer this yourself",
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def match_option(candidates: list[str], options: list[str]) -> tuple[str | None, float]:
    """Pick the form option that best matches our candidate phrasings. Returns (option, score)."""
    if not options:
        return None, 0.0
    nopts = [(o, _norm(o)) for o in options]
    best, best_score = None, 0.0
    for cand in candidates:
        nc = _norm(cand)
        if not nc:
            continue
        ctoks = set(nc.split())
        for orig, no in nopts:
            if nc == no:
                score = 1.0
            elif nc in no or no in nc:
                score = 0.85
            else:
                otoks = set(no.split())
                inter = ctoks & otoks
                score = 0.6 * (len(inter) / len(ctoks | otoks)) if (ctoks | otoks) else 0.0
            if score > best_score:
                best, best_score = orig, score
    return best, round(best_score, 3)


def _action_for_value(field: dict, res: dict, kind: str, source: str, confidence: float) -> dict:
    """Shape a profile/memory value into the right action for this field's widget type."""
    ftype = (field.get("type") or "text").lower()
    value = res.get("value", "")
    candidates = [c for c in (res.get("candidates") or [value]) if c]

    if ftype in _CHOICE_TYPES:
        options = field.get("options") or []
        if options:
            option, score = match_option(candidates, options)
            if option and score >= 0.5:
                return _act(field, kind, "select", source, confidence * (0.7 + 0.3 * score),
                            option=option, value=value, candidates=candidates,
                            reason=f"matched option ({score})")
            # Real options were on offer and none of ours fit — that IS a judgement call.
            return _act(field, kind, "select", source, confidence * 0.5, value=value,
                        candidates=candidates, needs_review=True,
                        reason="none of the visible options matched")

        # No options to match against. On a Workday dropdown that is the NORMAL state, not a
        # failure: the list does not exist until the widget is opened. Send candidates and let the
        # page open → match → click the real row (typeahead must click; typing alone is not enough).
        widget = (field.get("widget") or "").lower()
        action = "type_then_pick" if widget == "typeahead" else "select"
        return _act(field, kind, action, source, confidence * 0.9, value=value,
                    candidates=candidates, reason="options resolve when the widget opens")

    # plain text-like input
    return _act(field, kind, "fill", source, confidence, value=value, candidates=candidates)


def _act(field, kind, action, source, confidence, *, value="", option=None, candidates=None,
         needs_review=False, qa_id=None, reason="") -> dict:
    conf = round(max(0.0, min(1.0, confidence)), 3)
    # A widget you can only pick a value in is safe to drive automatically: the set of things it
    # can end up holding is fixed, the page confirms what got selected, and a wrong pick is visible
    # at a glance. Free text is the opposite — it can end up holding anything — so the confidence
    # floor stays for those. This is what makes "don't ask me about simple fields" true without
    # making it reckless.
    widget = (field.get("widget") or "").lower()
    ftype = (field.get("type") or "").lower()
    pickable = widget in _PICKABLE or ftype in _CHOICE_TYPES
    empty = action != "skip" and not (value or option or candidates)
    review = bool(needs_review or empty or (conf < 0.6 and not pickable))
    return {
        "field_id": field.get("id"),
        "field_kind": kind,
        "action": action,
        "value": value,
        "option": option,
        # Every phrasing this answer is allowed to take. The page needs these, not just the single
        # best guess, because a form's own wording ("Permanent Resident of the U.S.") rarely
        # matches ours exactly and only the page can see the real rows.
        "candidates": list(candidates or ([value] if value else [])),
        "widget": field.get("widget") or "",
        "source": source,
        "confidence": conf,
        "needs_review": review,
        "auto": bool(not review and action in ("fill", "select") and (value or option or candidates)),
        "qa_id": qa_id,
        "reason": reason,
    }


def answer(field: dict, profile: dict | None = None, jd: dict | None = None,
           no_ai: bool = False) -> dict:
    profile = profile or profile_store.load()
    kind, kconf = classifier.classify(field)

    # 0) fields we intentionally never fill (a second phone/address) — skip QUIETLY, no highlight.
    if kind == "do_not_fill":
        return _act(field, kind, "skip", "profile_map", kconf,
                    needs_review=False, reason="secondary contact/address — skipped by preference")

    # 0b) referral / who-you-know questions — ALWAYS leave blank + highlight, overriding any
    #     profile/memory answer (the user's explicit rule for these).
    if kind in _LEAVE_BLANK:
        return _act(field, kind, "skip", "none", 0.0, needs_review=True,
                    reason=_BLANK_REASON.get(kind, "answer this yourself"))

    # 0c) "Have you worked here before?" — answerable only against THIS company. It's No unless one
    #     of the user's own employers matches the posting's company, so the comparison is made here
    #     (field_map has no access to the JD) rather than guessing a blanket No.
    if kind == "previously_employed":
        target = _norm((jd or {}).get("company", ""))
        if not target:
            return _act(field, kind, "skip", "none", 0.0, needs_review=True,
                        reason="can't tell which company is asking — answer this yourself")
        employers = [_norm(w.get("company", "")) for w in (profile.get("work_experience") or [])]
        worked_there = any(e and (e == target or e in target or target in e) for e in employers)
        res = {"value": "Yes" if worked_there else "No",
               "candidates": (_YES if worked_there else _NO)}
        return _action_for_value(field, res, kind, "profile_map", 0.85 if not worked_there else 0.7)

    # 1) file uploads — attached from the Docs tab, not typed here.
    if kind in _UPLOAD_KINDS:
        return _act(field, kind, "upload", "profile_map", kconf,
                    value="", needs_review=True, reason="attach file from the Docs tab")

    # 2) essays / free-text. With no_ai on, we DON'T generate — leave blank + flag so it's
    #    highlighted for the user to write by hand (their explicit "no AI answers for now").
    if kind in _ESSAY_KINDS or (field.get("type") == "textarea" and kind == "unknown"):
        if no_ai:
            return _act(field, kind, "skip", "none", 0.0, needs_review=True,
                        reason="free-text — fill yourself (AI answers off)")
        return _essay_answer(field, kind, profile, jd)

    # 3) deterministic profile map
    res = field_map.resolve(kind, profile)
    if res and (res.get("value") or res.get("candidates")):
        return _action_for_value(field, res, kind, "profile_map", kconf)

    # 4) Q&A semantic memory (use the visible label as the question)
    label = field.get("label") or field.get("name") or ""
    mem = qa_memory.recall(label, field.get("type")) if label else None
    if mem:
        res = {"value": mem["answer"], "candidates": [mem["answer"]]}
        act = _action_for_value(field, res, kind, "qa_memory", mem["similarity"])
        act["qa_id"] = mem["id"]
        return act

    # 5) LLM for remaining short fields (not long essays — those are step 2). Identifies the
    #    question and answers from the profile. Checkbox/radio/dropdown answers come back as
    #    select actions so the page can click them; free-text comes back as fill + needs_review.
    if not no_ai and kind in ("unknown", "generic_essay") and (field.get("type") or "") != "textarea":
        llm_act = _llm_short_field(field, kind, profile, jd)
        if llm_act:
            return llm_act

    # 6) give up — extension flags it for manual entry
    return _act(field, kind, "skip", "none", 0.0, needs_review=True, reason="no profile/memory match")


def _llm_short_field(field: dict, kind: str, profile: dict, jd: dict | None) -> dict | None:
    """Ask the local model what a short unknown field wants, then map to a fill/select action.

    Restricted to short answers. Long subjective essays stay on the needs_review path.
    """
    label = (field.get("label") or field.get("name") or "").strip()
    if not label or len(label) < 3:
        return None
    ident = profile.get("identity", {}) or {}
    auth = profile.get("work_auth", {}) or {}
    disc = profile.get("disclosures", {}) or {}
    ra = profile.get("reusable_answers", {}) or {}
    ed = (profile.get("education") or [{}])[0] if profile.get("education") else {}
    facts = (
        f"Name: {ident.get('legal_name','')}; Email: {ident.get('email','')}; "
        f"Phone: {ident.get('phone','')}; Location: {ident.get('location','')}; "
        f"Country: United States; "
        f"Work auth: {auth.get('us_work_auth_status','')}; "
        f"Needs sponsorship: {auth.get('needs_sponsorship','no')}; "
        f"Authorized to work in US: yes; Permanent resident: "
        f"{'yes' if auth.get('us_work_auth_status')=='permanent_resident' else 'no'}; "
        f"Non-compete: {disc.get('non_compete','no')}; "
        f"Criminal conviction: {disc.get('criminal_conviction','no')}; "
        f"School: {ed.get('school','')}; Degree: {ed.get('degree','')}; Major: {ed.get('major','')}; "
        f"GPA: {ed.get('gpa','')}; Grad date: {ed.get('end_date','')}; "
        f"Start date preference: {ra.get('earliest_start_date','')}; "
        f"Willing to relocate: {ra.get('willing_to_relocate','')}; "
        f"Company: {(jd or {}).get('company','')}; Role: {(jd or {}).get('role_title','')}"
    )
    options = field.get("options") or []
    opt_line = (", ".join(options[:30])) if options else "(options appear when opened)"
    ftype = (field.get("type") or "text").lower()
    sys = (
        "You answer ONE short job-application field for a CS student. "
        "Use ONLY the candidate facts given. Never invent employers, dates, or numbers. "
        "Return strict JSON: {\"answer\": \"...\", \"skip\": false}. "
        "If you truly cannot answer, {\"skip\": true, \"answer\": \"\"}. "
        "For yes/no questions answer exactly Yes or No. "
        "For dropdowns prefer an answer that would match a typical option label. "
        "Keep answers under 12 words unless the field clearly needs a sentence."
    )
    usr = f"Field label: {label}\nField type: {ftype}\nVisible options: {opt_line}\nFacts: {facts}\nJSON:"
    try:
        raw = llm.chat(sys, usr, temperature=0.2, num_predict=120)
        data = None
        try:
            data = __import__("json").loads(raw)
        except Exception:
            m = re.search(r"\{.*\}", raw or "", re.DOTALL)
            if m:
                data = __import__("json").loads(m.group(0))
        if not isinstance(data, dict) or data.get("skip") or not str(data.get("answer") or "").strip():
            return None
        answer = str(data["answer"]).strip()
        # Cap runaway answers — those belong in the essay path with review.
        if len(answer) > 160:
            return _act(field, kind, "fill", "llm", 0.55, value=answer, needs_review=True,
                        reason="generated long answer — review")
        res = {"value": answer, "candidates": [answer] + ([answer.split(",")[0].strip()] if "," in answer else [])}
        act = _action_for_value(field, res, kind or "unknown", "llm", 0.72)
        # Short structured answers are safe to apply; only mark review for free text ambiguity.
        if act.get("action") == "select":
            act["needs_review"] = False
            act["auto"] = True
        else:
            act["needs_review"] = len(answer.split()) > 8
            act["auto"] = not act["needs_review"]
        act["reason"] = "llm short-field answer"
        return act
    except Exception:
        return None


def _essay_answer(field: dict, kind: str, profile: dict, jd: dict | None) -> dict:
    label = field.get("label") or field.get("name") or ""
    # past confirmed answer wins — it's already in the user's voice
    mem = qa_memory.recall(label, "textarea") if label else None
    if mem:
        return _act(field, kind, "fill", "qa_memory", mem["similarity"],
                    value=mem["answer"], qa_id=mem["id"], reason="recalled past answer")

    ra = profile.get("reusable_answers", {})
    hint = ra.get(_ESSAY_HINT.get(kind, ""), "") if kind in _ESSAY_HINT else ""
    company = (jd or {}).get("company", "")
    role = (jd or {}).get("role_title", "")
    jd_summary = (jd or {}).get("summary", "")
    ident = profile.get("identity", {})

    sys = ("You write concise, specific, first-person application answers for a CS "
           "new-grad candidate. No clichés, no overpromising. 60-110 words unless it's a "
           "cover letter. Use only facts implied by the provided context; invent nothing.")
    usr = f"""Question on the form: "{label or kind}"
Candidate: {ident.get('legal_name','')}, CS student.
Target company: {company or 'the company'} | Role: {role or 'this role'}
Job summary: {jd_summary or '(not provided)'}
Starting-point notes from the candidate (adapt, don't copy): {hint or '(none)'}

Write the answer."""
    try:
        text = llm.chat(sys, usr, temperature=0.6).strip()
    except Exception as e:
        return _act(field, kind, "skip", "none", 0.0, needs_review=True,
                    reason=f"llm error: {e}")
    # LLM answers ALWAYS need review (and a chance to save to memory)
    return _act(field, kind, "fill", "llm", 0.55, value=text, needs_review=True,
                reason="generated — review then save to memory")


def answer_page(fields: list[dict], profile: dict | None = None, jd: dict | None = None,
                no_ai: bool = False) -> list[dict]:
    """Resolve a whole page/step at once. Fast-path fields are instant. With no_ai=True nothing hits
    the LLM at all — free-text is left blank for the user. (The extension calls this on Autofill.)"""
    profile = profile or profile_store.load()
    return [answer(f, profile, jd, no_ai=no_ai) for f in fields]
