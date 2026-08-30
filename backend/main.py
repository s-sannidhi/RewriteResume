"""FastAPI app — local-only, single-user. Binds to 127.0.0.1 (see run.sh).

Phase 1 surface: health, profile read, JD analysis + keyword recommender, and the Layer-1
resume text flow (generate + feedback regen). PDF fit loop, Q&A memory, tracker, and the
autofill brain are wired in later phases.
"""
import json
import re
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from . import ask, config, documents, emailer, llm, profile_store, jd_analyzer, secrets
from .resume import builder, cover_letter, renderer, skills_source
from .store import chats, people, qa_memory, startups, tracker
from .autofill import resolver
from .discovery import (sources as disc_sources, ranking as disc_ranking,
                        contacts as disc_contacts, intel as disc_intel, people as disc_people,
                        outreach as disc_outreach, companies as disc_companies,
                        top_companies as disc_top, oa_companies as disc_oa)

app = FastAPI(title="Resume Rewriter v2", docs_url="/docs")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# The panel is the only remote caller. /email/send makes a wildcard origin a real drive-by
# hole, so only extension origins get CORS (the /app editor is same-origin anyway).
# Chrome: chrome-extension://<id>  Firefox: moz-extension://<uuid>
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"(chrome|moz)-extension://.*",
    allow_methods=["*"],
    allow_headers=["*"],
)


def _migrate_login_password() -> None:
    """One-time: move the plaintext login password out of profile.json into the Keychain.
    profile.json.bak is rewritten from the scrubbed profile too — save() would otherwise
    leave the old plaintext copy sitting in the backup."""
    try:
        profile = profile_store.load()
    except FileNotFoundError:
        return
    profile, changed = secrets.scrub_profile(profile)
    if changed:
        profile_store.save(profile)
        bak = config.PROFILE_PATH.with_suffix(".json.bak")
        if bak.exists():
            bak.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")


_migrate_login_password()

# First run on a new machine: the data folder lives outside the repo, so a clone has neither a
# profile nor a skills file. Create both (empty) here as well as in setup.py, so starting the
# server is enough to reach a working /app instead of a 500.
profile_store.load()
skills_source.ensure_template()

SETUP_HINT = ("Fill in the Resume tab at http://127.0.0.1:8765/app/ — every fact on a résumé "
              "comes from your profile. Moving from another machine? "
              "python scripts/backup_data.py import <your-backup>.zip")


@app.exception_handler(skills_source.SkillsSourceError)
def _skills_source_error(request: Request, exc: skills_source.SkillsSourceError):
    """Unreadable skills file — a setup problem, not a server fault. Say which file and why."""
    return JSONResponse(status_code=400, content={"error": str(exc), "hint": SETUP_HINT})


def _require_profile() -> dict:
    """Nothing can be written from an empty profile. Fail with instructions, not a stack trace."""
    prof = profile_store.load()
    if profile_store.is_empty(prof):
        raise HTTPException(status_code=400, detail="Your profile is empty. " + SETUP_HINT)
    return prof


@app.get("/health")
def health():
    profile = profile_store.load()
    return {
        "ok": True,
        "llm": llm.healthy(),
        "model": config.LLM_MODEL,
        "qa_memory": qa_memory.count(),
        "tracker": tracker.count(),
        "data_dir": str(config.DATA_DIR),
        "profile_ready": not profile_store.is_empty(profile),
        "skills_file": skills_source.SKILLS_PATH.exists(),
    }


@app.get("/profile")
def get_profile():
    return secrets.redact_login_password(profile_store.load())


@app.put("/profile")
def put_profile(profile: dict = Body(...)):
    """Replace the whole master profile (the website's Save). profile_store.save() backs up
    the previous version to profile.json.bak first. Any incoming plaintext login password is
    diverted to the Keychain — it never persists in profile.json again."""
    profile, _ = secrets.scrub_profile(profile)
    profile_store.save(profile)
    return {"ok": True}


class LoginPasswordIn(BaseModel):
    password: str


@app.get("/secrets/login")
def get_login_secret():
    """Whether a job-site login password is stored — never the value itself."""
    profile = profile_store.load()
    return {
        "set": bool(secrets.login_password(profile)),
        "email": secrets.login_account(profile),
        "backend": secrets.backend_name(),
    }


@app.get("/secrets/login/value")
def get_login_secret_value():
    """The actual password, for the extension's click-to-copy tile. Local-only."""
    return {"password": secrets.login_password(profile_store.load()) or ""}


@app.put("/secrets/login")
def put_login_secret(body: LoginPasswordIn):
    """Replace the job-site login password in the OS credential store."""
    pw = (body.password or "").strip()
    if not pw:
        raise HTTPException(status_code=400, detail="password is empty")
    profile = profile_store.load()
    if not secrets.set_login_password(profile, pw):
        raise HTTPException(
            status_code=500,
            detail="could not store the password in " + secrets.backend_name(),
        )
    return {"ok": True, "backend": secrets.backend_name()}


# --- JD (read from the page by the extension; never pasted) -------------------
class JDIn(BaseModel):
    jd_text: str


@app.post("/jd/analyze")
def jd_analyze(body: JDIn):
    return jd_analyzer.analyze(body.jd_text)


# --- Resume text (Layer 1) ----------------------------------------------------
class GenerateIn(BaseModel):
    jd_analysis: dict
    focus_angle: str = ""


@app.post("/resume/generate-text")
def generate_text(body: GenerateIn):
    _require_profile()
    return builder.generate_text(body.jd_analysis, body.focus_angle)


class FeedbackIn(BaseModel):
    jd_analysis: dict
    focus_angle: str = ""
    feedback: str


@app.post("/resume/feedback")
def feedback(body: FeedbackIn):
    _require_profile()
    return builder.generate_text(body.jd_analysis, body.focus_angle, feedback=body.feedback)


# --- Resume PDF (Layer 2: fit loop) ------------------------------------------
class GeneratePdfIn(BaseModel):
    jd_analysis: dict
    focus_angle: str = ""
    text: dict | None = None  # pass approved Layer-1 text to skip re-generation


@app.post("/resume/generate-pdf")
def generate_pdf(body: GeneratePdfIn):
    _require_profile()
    return renderer.generate_pdf(body.jd_analysis, body.focus_angle, gen=body.text)


from fastapi.responses import FileResponse


@app.get("/resume/pdf/{tracker_id}")
def get_pdf(tracker_id: str):
    rec = tracker.get(tracker_id)
    if not rec or not rec.get("folder"):
        return {"error": "not found"}
    path = config.RESUMES_DIR / rec["folder"].split("/")[-1] / (rec.get("pdf_filename") or "resume.pdf")
    if not path.exists():
        return {"error": "pdf missing"}
    return FileResponse(str(path), media_type="application/pdf", filename=path.name)


# --- Cover letter (Layer 2b: per-job letter next to the resume) ----------------
class CoverLetterIn(BaseModel):
    tracker_id: str
    entry_id: str = ""    # force which experience the letter is about (see experience_options)


@app.post("/cover-letter/generate")
def cover_letter_generate(body: CoverLetterIn):
    _require_profile()
    return cover_letter.generate(body.tracker_id, body.entry_id)


@app.get("/cover-letter/pdf/{tracker_id}")
def get_cover_letter_pdf(tracker_id: str):
    rec = tracker.get(tracker_id)
    if not rec or not rec.get("folder"):
        return {"error": "not found"}
    path = config.RESUMES_DIR / rec["folder"].split("/")[-1] / (rec.get("cover_letter_filename") or "cover_letter.pdf")
    if not path.exists():
        return {"error": "cover letter missing"}
    return FileResponse(str(path), media_type="application/pdf", filename=path.name)


# --- Documents: frequently-uploaded files, surfaced for the extension's Docs tab -------
class DocPathIn(BaseModel):
    path: str = ""


@app.get("/documents")
def documents_list():
    """Static docs (transcript, schedule, …) + generated resumes/cover letters, with abs paths."""
    return documents.listing()


@app.get("/documents/raw")
def documents_raw(path: str):
    """Serve/preview a document by absolute path (guarded to ~/ResumeRewriter/)."""
    p = documents.resolve(path)
    if not p:
        return {"error": "not found"}
    media = "application/pdf" if p.suffix.lower() == ".pdf" else "application/octet-stream"
    return FileResponse(str(p), media_type=media, filename=p.name)


@app.post("/documents/reveal")
def documents_reveal(body: DocPathIn):
    """Reveal a file in Finder (macOS). Used by the Docs tab's 'Reveal in Finder' action."""
    return {"ok": documents.reveal(body.path)}


@app.post("/documents/open-folder")
def documents_open_folder(body: DocPathIn):
    """Open a folder (default: the documents dir) in Finder."""
    return {"ok": documents.open_folder(body.path or None)}


# --- Profile skills (user-approved keyword recommendations) -------------------
class AddSkillsIn(BaseModel):
    group: str
    items: list[str]


@app.post("/profile/skills")
def add_skills(body: AddSkillsIn):
    profile = profile_store.load()
    profile = profile_store.add_skills(profile, body.group, body.items)
    profile_store.save(profile)
    return {"ok": True, "skills_extra": profile.get("skills_extra", {})}


class JDSyncIn(BaseModel):
    # the JD's keyword_recommendations: [{term, suggested_group}, ...]
    items: list[dict]


@app.post("/profile/skills/jd-sync")
def jd_sync_skills(body: JDSyncIn):
    """Auto-add a JD's recommended tools to the skills library (skills_extra), skipping any that
    are blocklisted. Called when a JD is read so its tools become weave-eligible; the user's
    only curation is forbidding (which removes them again)."""
    profile = profile_store.load()
    blocked = profile_store.blocklist(profile)
    added = []
    for it in body.items:
        term = (it.get("term") or "").strip()
        group = (it.get("suggested_group") or "tools").strip() or "tools"
        if term and term.lower() not in blocked:
            before = profile_store.all_skills(profile)
            profile = profile_store.add_skills(profile, group, [term])
            if term.lower() in profile_store.all_skills(profile) and term.lower() not in before:
                added.append(term)
    if added:
        profile_store.save(profile)
    return {"ok": True, "added": added}


# --- Skills blocklist: skills to NEVER add, recommend, or show on a resume -----
class BlocklistIn(BaseModel):
    items: list[str]


@app.get("/profile/skills/blocklist")
def get_blocklist():
    return {"blocklist": profile_store.load().get("skills_blocklist", [])}


@app.post("/profile/skills/blocklist")
def add_blocklist(body: BlocklistIn):
    profile = profile_store.load()
    profile = profile_store.add_to_blocklist(profile, body.items)
    profile_store.save(profile)
    return {"ok": True, "blocklist": profile.get("skills_blocklist", [])}


@app.post("/profile/skills/blocklist/remove")
def remove_blocklist(body: BlocklistIn):
    profile = profile_store.load()
    profile = profile_store.remove_from_blocklist(profile, body.items)
    profile_store.save(profile)
    return {"ok": True, "blocklist": profile.get("skills_blocklist", [])}


# --- Smart add: classify general vs niche; niche skills get woven into an experience --------
class SmartAddIn(BaseModel):
    term: str
    group: str = "tools"


@app.post("/profile/skills/smart-add")
def smart_add(body: SmartAddIn):
    profile = profile_store.load()
    profile = profile_store.add_skills(profile, body.group, [body.term])
    profile_store.save(profile)
    cls = jd_analyzer.classify_niche(body.term)
    experiences = (
        [{"id": w["id"], "type": "work", "label": f'{w.get("title","")} — {w.get("company","")}'}
         for w in profile.get("work_experience", [])]
        + [{"id": p["id"], "type": "project", "label": p.get("name", "")}
           for p in profile.get("projects", [])]
    )
    return {"ok": True, "term": body.term, "niche": cls["niche"], "reason": cls["reason"],
            "experiences": experiences}


class WeaveIn(BaseModel):
    term: str
    entry_type: str            # "work" | "project"
    entry_id: str
    how: str = ""              # the user's description of how they used the skill


@app.post("/profile/skills/weave")
def weave_skill(body: WeaveIn):
    profile = profile_store.load()
    arr = profile.get("work_experience" if body.entry_type == "work" else "projects", []) or []
    entry = next((e for e in arr if e.get("id") == body.entry_id), None)
    if not entry:
        return {"error": "entry not found"}

    # 1) ensure the skill is in this entry's tech list
    tech_key = "tech_used" if body.entry_type == "work" else "tech_stack"
    tech = entry.setdefault(tech_key, [])
    if not any(body.term.lower() in (t or "").lower() for t in tech):
        tech.append(body.term)

    # 2) weave it into ONE evidence bullet (or append one) so it's shown in context
    bullets = [b for b in (entry.get("bullets") or []) if b.strip()]
    updated = None
    label = entry.get("title") or entry.get("name") or "this role"
    if bullets and body.how.strip():
        try:
            numbered = "\n".join(f"{i}: {b}" for i, b in enumerate(bullets))
            d = llm.chat_json(
                "You edit resume evidence bullets truthfully and concisely. Return strict JSON.",
                f"Role/project: {label}\nBullets:\n{numbered}\n\n"
                f'The candidate used {body.term} here: "{body.how}".\n'
                f"Rewrite exactly ONE bullet (the most related) to naturally and truthfully "
                f"incorporate {body.term} and this usage. One concise sentence, no fabrication.\n"
                'Return JSON: {"index": <int>, "bullet": "<rewritten bullet>"}.',
            )
            i = int(d.get("index", 0))
            if 0 <= i < len(bullets) and (d.get("bullet") or "").strip():
                bullets[i] = d["bullet"].strip()
                updated = bullets[i]
        except Exception:
            pass
    if updated is None and body.how.strip():        # fallback: append a plain evidence bullet
        bullets.append(f"Used {body.term} to {body.how.strip().rstrip('.')}.")
        updated = bullets[-1]
    entry["bullets"] = bullets
    profile_store.save(profile)
    return {"ok": True, "entry_label": label, "updated_bullet": updated, "bullets": bullets}


# --- Ask (application questions -> the local model) ---------------------------
class AskIn(BaseModel):
    question: str
    jd_analysis: dict | None = None
    history: list[dict] | None = None   # prior [{role, content}] turns, for Refine
    refine: str | None = None           # "Make it shorter", custom instruction, ...


@app.post("/ask")
def ask_endpoint(body: AskIn):
    return ask.answer(body.question, body.jd_analysis, body.history, body.refine)


# --- AI Ask chat (website): persistent threads, humanized, general-purpose ----
@app.get("/chat/threads")
def chat_threads():
    return {"threads": chats.list_threads()}


@app.get("/chat/thread/{tid}")
def chat_get(tid: str):
    return chats.get_thread(tid) or {"error": "not found"}


@app.post("/chat/thread")
def chat_new():
    return chats.create_thread()


@app.delete("/chat/thread/{tid}")
def chat_delete(tid: str):
    chats.delete_thread(tid)
    return {"ok": True}


class ChatMsgIn(BaseModel):
    message: str


@app.post("/chat/{tid}/message")
def chat_message(tid: str, body: ChatMsgIn):
    t = chats.add_message(tid, "user", body.message)
    if not t:
        return {"error": "thread not found"}
    res = ask.chat_reply(t["messages"])
    if res.get("error"):
        return res
    chats.add_message(tid, "assistant", res["answer"])
    return {"answer": res["answer"]}


# --- Q&A semantic memory (autofill fast path) --------------------------------
class RecallIn(BaseModel):
    question: str
    field_type: str | None = None


@app.post("/qa/recall")
def qa_recall(body: RecallIn):
    return {"match": qa_memory.recall(body.question, body.field_type)}


class QASaveIn(BaseModel):
    question: str
    answer: str
    field_type: str | None = None


@app.post("/qa/save")
def qa_save(body: QASaveIn):
    return qa_memory.save(body.question, body.answer, body.field_type)


# --- Autofill brain ----------------------------------------------------------
class FieldIn(BaseModel):
    fields: list[dict]
    jd_analysis: dict | None = None
    no_ai: bool = False        # when true, never call the LLM — leave free-text blank for the user


@app.post("/field/answer")
def field_answer(body: FieldIn):
    """Resolve one field. Pass a single-element fields list."""
    profile = profile_store.load()
    f = body.fields[0] if body.fields else {}
    return resolver.answer(f, profile, body.jd_analysis, no_ai=body.no_ai)


@app.post("/field/answer/batch")
def field_answer_batch(body: FieldIn):
    """Resolve a whole page/step at once (what the extension calls on Autofill)."""
    profile = profile_store.load()
    actions = resolver.answer_page(body.fields, profile, body.jd_analysis, no_ai=body.no_ai)
    sources = {}
    for a in actions:
        sources[a["source"]] = sources.get(a["source"], 0) + 1
    return {"actions": actions, "summary": {
        "total": len(actions),
        "by_source": sources,
        "needs_review": sum(1 for a in actions if a["needs_review"]),
    }}


# --- Internship scraper (today's Austin/remote matches -> the panel opens as tabs) ---
# Majors that mean "this posting is for someone like me" (CS + close-adjacent), and a broad-STEM
# neutral set. If a posting lists majors and NONE are tech, it's a poor fit (e.g. a Finance/Law
# internship that slipped through on a shared function tag).
_CS_MAJORS = {"computer science", "computer engineering", "software engineering", "data science",
              "computer information systems", "information technology", "information systems",
              "computer and information science", "computing", "cs"}
_STEM_NEUTRAL = {"mathematics", "math", "statistics", "electrical engineering", "engineering",
                 "physics", "data analytics", "other/not listed", "any", "all majors"}
_TITLE_GOOD = re.compile(r"software|engineer|developer|programm|\bdata\b|machine learning|\bml\b"
                         r"|\bai\b|back.?end|front.?end|full.?stack|dev\s?ops|\bsre\b|\bswe\b"
                         r"|computer|web|mobile|platform|infrastructure|cloud|systems", re.I)
_TITLE_BAD = re.compile(r"finance|account|audit|\btax\b|legal|\blaw\b|sales|marketing|recruit|\bhr\b"
                        r"|nurs|clinical|pharma|supply\s?chain|logistics|merchandis|banking|underwrit"
                        r"|actuar|consult(?:ing|ant)|business\s?develop|customer\s?success|brand", re.I)

# Domains the user has said aren't their focus (2026-08-14). Not disqualifying — a strong posting
# in these areas still ranks, it just sorts below equivalent generalist SWE/ML/data work.
_TITLE_SECURITY = re.compile(r"security|cyber|infosec|\bsoc\b|penetration|appsec|cryptograph"
                             r"|vulnerabilit|threat|malware|forensic", re.I)
_TITLE_HARDWARE = re.compile(r"hardware|firmware|embedded|\bfpga\b|\basic\b|\bvlsi\b|silicon|circuit"
                             r"|\bpcb\b|semiconductor|rtl design|verilog|vhdl|electrical design"
                             r"|mechanical|robotics hardware|signal integrity", re.I)


def _fit(job: dict, user_majors: set, user_skills: set | None = None) -> dict:
    """Deterministic 0-100 fit for one posting. Python owns the ranking (no LLM) so a big batch
    stays instant and the ordering is reproducible."""
    majors = {str(m).strip().lower() for m in (job.get("majors") or [])}
    title = job.get("title", "") or ""
    # Weights are tuned so a realistic best case lands near ~95, not pinned at the 100 cap —
    # otherwise every decent posting ties at 100 and "top N" ordering becomes arbitrary.
    score, reasons = 40, []
    if majors:
        if (majors & _CS_MAJORS) or (user_majors & majors):
            score += 22
        elif majors & _STEM_NEUTRAL:
            score += 4
        else:
            score -= 45
            reasons.append("targets " + ", ".join(sorted(majors))[:48])
    if _TITLE_BAD.search(title):
        score -= 30
        reasons.append("off-target role")
    if _TITLE_GOOD.search(title):
        score += 14

    # --- the user's stated preferences ---
    if _TITLE_SECURITY.search(title):
        score -= 18
        reasons.append("security-focused")
    if _TITLE_HARDWARE.search(title):
        score -= 18
        reasons.append("hardware/embedded")

    # US over Canada. `location` is a comma-joined string of the posting's locations.
    loc = (job.get("location", "") or "").lower()
    if re.search(r"\bcanada\b|\bon\b,|ontario|toronto|vancouver|montreal|quebec|\bbc\b|alberta|ottawa|waterloo", loc):
        score -= 12
        reasons.append("Canada")

    # Summer is THE internship season for a student — prioritize it. A posting open to multiple
    # terms still counts as summer. An explicitly non-summer term (Fall/Spring/Winter only) is a
    # school-year role, which competes with classes, so it sorts below.
    season = (job.get("season", "") or "").lower()
    if season:
        if "summer" in season:
            score += 16
            reasons.append("summer")
        else:
            score -= 10
            reasons.append("not summer")

        # A Fall-ONLY role means being there during the school year, when the user is in Austin —
        # so it has to be in Austin (or genuinely remote) to be doable at all. A posting that also
        # offers summer/winter is exempt: they'd just take the break term instead.
        # Note: judge remote by the LOCATION string, not the work_model field — the feed tags
        # plainly on-site roles ("IT Support Tech", Pikeville KY) as Remote, so work_model can't
        # be trusted here, while a truly remote posting carries "Remote in ..." as a location.
        if "fall" in season and "summer" not in season and "winter" not in season:
            if "austin" not in loc and "remote" not in loc:
                score -= 25
                reasons.append("fall, not Austin")

    # Tech overlap with the user's actual profile skills — the strongest personal signal available
    # without reading the full JD (which only happens later, at resume-generation time).
    if user_skills:
        hits = [s for s in user_skills if len(s) > 2 and
                re.search(r"(?<![a-z0-9])" + re.escape(s) + r"(?![a-z0-9])", title.lower())]
        if hits:
            score += min(6 * len(hits), 18)
            reasons.append("your stack: " + ", ".join(sorted(set(hits))[:3]))

    score = max(0, min(100, score))
    label = "strong" if score >= 62 else ("ok" if score >= 40 else "weak")
    return {"fit": label, "fit_score": score, "fit_reason": "; ".join(reasons)}


def _user_fit_context() -> tuple[set, set]:
    """(majors, skills) for fit scoring, from the profile."""
    prof = profile_store.load()
    majors = set()
    for e in (prof.get("education") or []):
        for k in ("major", "second_major"):
            v = (e.get(k) or "").strip().lower()
            if v:
                majors.add(v)
    return majors, disc_ranking.flatten_skills(prof)


@app.get("/interns/today")
def interns_today(min_days: float = 0.0, max_days: float = 1.0):
    """Internships in the posting-age window, ranked best-fit first. Returns the full ranked list;
    the caller takes the top N *after* dropping ones it has already opened, so "top 15" means 15
    new tabs rather than 15 minus whatever was already seen.

    Each job may include `oa_company` (canonical name) when it matches a big shop known for
    sending online assessments early — the extension pins one of these on the first batch of
    the day and shows an OA badge."""
    try:
        from scrape_interns import scrape as _scrape
        jobs = _scrape(min_days, max_days)
        user_majors, user_skills = _user_fit_context()
        for j in jobs:
            j.update(_fit(j, user_majors, user_skills))
            oa = disc_oa.match(j.get("company") or "")
            if oa:
                j["oa_company"] = oa
                j["oa_priority"] = True
        jobs.sort(key=lambda j: -j["fit_score"])   # best fits first
        return {"ok": True, "min_days": min_days, "max_days": max_days, "jobs": jobs}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/interns/top-companies")
def interns_top_companies(top_n: int = 60, min_fit: int = 0):
    """Internships at S&P 500 companies, ANY posting age. Pulls the whole current board, keeps the
    postings whose company matches the top-500 list, then ranks them by the same personal fit."""
    try:
        from scrape_interns import scrape as _scrape
        jobs = _scrape(0.0, 3650.0, result_cap=6000)      # whole board, age irrelevant
        user_majors, user_skills = _user_fit_context()
        out = []
        for j in jobs:
            canon = disc_top.match(j.get("company", ""))
            if not canon:
                continue
            j.update(_fit(j, user_majors, user_skills))
            if j["fit_score"] < min_fit:
                continue
            j["sp500_company"] = canon
            oa = disc_oa.match(j.get("company") or "") or disc_oa.match(canon)
            if oa:
                j["oa_company"] = oa
                j["oa_priority"] = True
            out.append(j)
        out.sort(key=lambda j: -j["fit_score"])
        companies = sorted({j["sp500_company"] for j in out})
        return {"ok": True, "scanned": len(jobs), "matched": len(out),
                "companies": len(companies), "jobs": out[:top_n] if top_n > 0 else out}
    except Exception as e:
        return {"ok": False, "error": str(e)}


class ResolveIn(BaseModel):
    urls: list[str]


@app.post("/interns/resolve")
def interns_resolve(body: ResolveIn):
    """Follow each Simplify click-link's redirects to its real ATS destination (server-side,
    reading only the Location header — the ATS page itself often 403s bots). Lets the extension
    skip postings whose destination tab is already open."""
    import httpx
    from urllib.parse import urljoin, urlparse
    out = {}
    with httpx.Client(follow_redirects=False, timeout=6.0,
                      headers={"User-Agent": "Mozilla/5.0"}) as client:
        for u in body.urls[:80]:
            cur = final = u
            try:
                for _ in range(5):
                    resp = client.get(cur)
                    loc = resp.headers.get("location")
                    if resp.status_code in (301, 302, 303, 307, 308) and loc:
                        cur = final = urljoin(cur, loc)
                        if "simplify.jobs" not in (urlparse(cur).hostname or ""):
                            break   # reached the ATS — stop following
                    else:
                        break
            except Exception:
                pass   # unreachable → leave as the raw click URL
            out[u] = final
    return {"resolved": out}


class InternIngestIn(BaseModel):
    max_days: float = 1.0
    limit: int = 40
    min_fit: int = 0


@app.post("/interns/ingest")
def interns_ingest(body: InternIngestIn = Body(default=InternIngestIn())):
    """Pull today's Simplify internships into the companies store (source='Simplify') so they flow
    through the SAME contacts + outreach pipeline as the startups. Resolves each company's domain
    (Clearbit) so contact/email discovery can run. Contacts/messages are generated on demand."""
    try:
        from scrape_interns import scrape as _scrape
        jobs = _scrape(0.0, body.max_days)
        prof = profile_store.load()
        user_majors = set()
        for e in (prof.get("education") or []):
            for k in ("major", "second_major"):
                v = (e.get(k) or "").strip().lower()
                if v:
                    user_majors.add(v)
        for j in jobs:
            j.update(_fit(j, user_majors))
        jobs = [j for j in jobs if j["fit_score"] >= body.min_fit]
        jobs.sort(key=lambda j: -j["fit_score"])
        # de-dupe by company name so we resolve each domain once
        seen, rows = set(), []
        for j in jobs:
            nm = (j.get("company") or "").strip().lower()
            if not nm or nm in seen:
                continue
            seen.add(nm)
            rows.append(disc_companies.intern_to_company(j))
            if len(rows) >= body.limit:
                break
        rows = disc_companies.normalize.merge(rows)
        startups.upsert_many(rows)
        return {"ok": True, "ingested": len(rows), "total": startups.count()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# --- Startup discovery (outreach Phase 1) ------------------------------------
class DiscoverIn(BaseModel):
    recent_only: bool = True
    hiring_only: bool = True
    years_back: int = 1
    limit: int = 400


@app.post("/startups/discover")
def startups_discover(body: DiscoverIn = Body(default=DiscoverIn())):
    """Run the discovery sources (YC anchor), score every company against the profile, and persist.
    Contact enrichment is NOT done here — it's lazy (POST /startups/{id}/contact)."""
    try:
        companies = disc_sources.discover(body.model_dump())
        prof = profile_store.load()
        rb = set(disc_sources.recent_batches(body.years_back))
        disc_ranking.rank(companies, prof, recent_batches=rb)
        startups.upsert_many(companies)
        return {"ok": True, "found": len(companies), "total": startups.count(),
                "startups": startups.list_all(limit=body.limit)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/startups")
def startups_list(status: str | None = None, min_fit: int = 0,
                  hiring_only: bool = False, limit: int = 300):
    return {"ok": True, "startups": startups.list_all(
        status=status, min_fit=min_fit, hiring_only=hiring_only, limit=limit)}


@app.post("/startups/{cid}/contact")
def startups_contact(cid: str):
    """Lazily fill one company's recommended contact. YC companies get their real founder + LinkedIn
    scraped from the YC company page; other sources (HN/GitHub) fall back to a role recommendation +
    a LinkedIn people-search (deeper contact discovery is roadmap Phase 3)."""
    co = startups.get(cid)
    if not co:
        return {"ok": False, "error": "not found"}
    src_url = co.get("source_url", "") or ""
    enriched = {"founders": [], "job_count": 0}
    if "ycombinator.com/companies/" in src_url:          # only YC pages carry founder data
        enriched = disc_contacts.enrich(src_url.rstrip("/").split("/")[-1])
    reco = disc_contacts.recommend(co, enriched.get("founders", []))
    startups.set_contact(cid, reco)
    return {"ok": True, "contact": reco, "founders": enriched.get("founders", []),
            "job_count": enriched.get("job_count", 0)}


@app.post("/startups/{cid}/intel")
def startups_intel(cid: str, refresh: bool = False):
    """Phase 2 — company intelligence. Assembles a factual dossier (founders, tech, launches,
    funding/news, hiring) and a grounded 'Why this company?' via the local LLM. Cached: returns the
    stored intel unless refresh=true. The LLM step is slow (~15s) so this is on-demand, not batch."""
    co = startups.get(cid)
    if not co:
        return {"ok": False, "error": "not found"}
    if co.get("intel") and not refresh:
        return {"ok": True, "cached": True, "intel": co["intel"]}
    built = disc_intel.build(co, profile_store.load())
    startups.set_intel(cid, built)
    return {"ok": True, "cached": False, "intel": built}


@app.post("/startups/{cid}/contacts")
def startups_contacts(cid: str):
    """Phase 3 — contact discovery. Finds and ranks the highest-value people to contact (YC
    founders + GitHub engineers/leads + HN poster), stores them, and promotes the top-ranked person
    to the startup's recommended contact. On-demand (GitHub calls are rate-limited)."""
    co = startups.get(cid)
    if not co:
        return {"ok": False, "error": "not found"}
    found = disc_people.discover_people(co, profile_store.load())
    people.replace_for_startup(cid, found)
    if found:                                    # keep the card summary in sync with the top pick
        top = found[0]
        startups.set_contact(cid, {
            "contact_name": top.get("name", ""), "contact_title": top.get("title", ""),
            "contact_linkedin": top.get("profile_url", ""),
            "contact_role_reco": top.get("role_bucket", ""),
            "contact_search_url": disc_contacts._linkedin_search(co.get("name", ""), "founder engineering"),
        })
    return {"ok": True, "count": len(found), "contacts": people.list_for_startup(cid)}


@app.get("/startups/{cid}/contacts")
def startups_contacts_list(cid: str):
    return {"ok": True, "contacts": people.list_for_startup(cid)}


# --- Outreach: batch list + AI message generation (Phase 4) ------------------
class OutreachListIn(BaseModel):
    top_n: int = 20
    min_fit: int = 60


@app.post("/outreach/list")
def outreach_list(body: OutreachListIn = Body(default=OutreachListIn())):
    """Build a ranked outreach list from the top-N startups by fit. For each, use the recommended
    contact already found (Phase 3), else a LIGHT lookup (YC founders + LinkedIn, no GitHub crawl)
    so a big batch won't hit the GitHub rate limit. Fast — no message generation here."""
    prof = profile_store.load()
    rows = startups.list_all(min_fit=body.min_fit, limit=body.top_n)
    out = []
    for co in rows:
        contacts = people.list_for_startup(co["id"])
        if not contacts:
            found = disc_people.discover_people(co, prof, use_github=False)   # light
            people.replace_for_startup(co["id"], found)
            contacts = people.list_for_startup(co["id"])
        rec = contacts[0] if contacts else None
        intel = co.get("intel") if isinstance(co.get("intel"), dict) else {}
        out.append({
            "startup_id": co["id"], "company": co["name"], "website": co.get("website", ""),
            "fit_score": co["fit_score"], "source": co.get("source", ""),
            "why": (intel.get("why_this_company") or co.get("fit_reason") or ""),
            "person": rec,
        })
    return {"ok": True, "count": len(out), "list": out}


class OutreachMsgIn(BaseModel):
    startup_id: str
    person_id: str
    msg_type: str = "auto"


@app.post("/outreach/message")
def outreach_message(body: OutreachMsgIn):
    """Draft an outreach message (LinkedIn note + email) for one person, grounded in the company
    intel + the person + the user's resume. Stored on the contact. Nothing is sent."""
    co = startups.get(body.startup_id)
    person = people.get(body.person_id)
    if not co or not person:
        return {"ok": False, "error": "not found"}
    msg = disc_outreach.generate_message(co, person, profile_store.load(), body.msg_type)
    people.set_message(body.person_id, msg)
    return {"ok": True, "message": msg}


class StartupPatchIn(BaseModel):
    status: str


@app.patch("/startups/{cid}")
def startups_patch(cid: str, body: StartupPatchIn):
    updated = startups.set_status(cid, body.status)
    if not updated:
        return {"ok": False, "error": "not found or bad status"}
    return {"ok": True, "startup": updated}


# --- Job tracker -------------------------------------------------------------
@app.get("/tracker")
def tracker_list():
    return tracker.list_all()


@app.get("/tracker/{app_id}")
def tracker_get(app_id: str):
    return tracker.get(app_id) or {"error": "not found"}


@app.get("/skills/verified")
def skills_verified():
    """The complete verified skills list, identical for every job — no JD ranking, no cap.

    The user's set is small enough that tailoring it was pure downside: the old per-application
    endpoint ranked JD-relevant skills first and then capped at 15, which silently dropped real
    skills from application forms. This returns everything in skills_verified.yaml, in file order,
    so what gets typed into a form matches what is printed on the resume."""
    try:
        groups = skills_source.ordered_groups([])
    except skills_source.SkillsSourceError as e:
        return {"skills": [], "groups": {}, "error": str(e)}
    flat, seen = [], set()
    for items in groups.values():
        for item in (items or []):
            k = item.strip().lower()
            if item.strip() and k not in seen:
                seen.add(k)
                flat.append(item.strip())
    return {"skills": flat, "groups": groups, "count": len(flat)}


@app.get("/tracker/{app_id}/skills")
def tracker_skills(app_id: str, limit: int = 15):
    """This application's skills for the Workday filler, tailored to ITS job and ranked
    best-first (the skills this JD actually asks for lead), then capped at `limit` (default 15).
    Falls back to the whole profile library if the row has no generated content yet."""
    rec = tracker.get(app_id)
    ja = (rec or {}).get("jd_analysis") or {}
    matched = (rec or {}).get("content") is not None
    groups = ((rec or {}).get("content") or {}).get("skills") or {}
    if not groups:
        # Fall back to the VERIFIED file, not profile["skills"] — this list gets typed into a real
        # application form, so it must obey the same "defensible only" contract as the resume.
        try:
            groups = skills_source.ordered_groups(ja.get("concrete_tech") or [])
        except skills_source.SkillsSourceError:
            groups = {}
        matched = False
    # Terms this JD wants → they rank first ("best skills possible" for this tab).
    jd_terms = {t.strip().lower() for t in (ja.get("concrete_tech") or []) if t.strip()}
    for kr in (ja.get("keyword_recommendations") or []):
        term = kr.get("term") if isinstance(kr, dict) else kr
        if term:
            jd_terms.add(str(term).strip().lower())
    flat, seen = [], set()
    for items in groups.values():
        for s in (items or []):
            k = s.strip().lower()
            if s.strip() and k not in seen:
                seen.add(k)
                flat.append(s.strip())
    flat.sort(key=lambda s: s.lower() not in jd_terms)   # stable: JD-relevant first
    if limit and limit > 0:
        flat = flat[:limit]
    return {"skills": flat, "matched": matched, "jd_count": len(jd_terms),
            "company": ja.get("company", ""), "role": ja.get("role_title", "")}


@app.get("/tracker/{app_id}/work-experience")
def tracker_work_experience(app_id: str):
    """The current application's work experience for the Workday filler: each role's TAILORED
    bullets (from THIS job's résumé) as the description, plus the real title/company/location and
    the start/end dates + current flag from the master profile."""
    rec = tracker.get(app_id)
    content = (rec or {}).get("content") or {}
    work = content.get("work") or []
    pmap = {w.get("id"): w for w in (profile_store.load().get("work_experience") or [])}
    entries = []
    for w in work:
        p = pmap.get(w.get("id"), {})
        bullets = [b for b in (w.get("bullets") or []) if b and b.strip()]
        entries.append({
            "title": w.get("title") or p.get("title", ""),
            "company": w.get("company") or p.get("company", ""),
            "location": w.get("location") or p.get("location", ""),
            "start_date": p.get("start_date", ""),
            "end_date": p.get("end_date", ""),
            "current": bool(p.get("current")),
            "description": "\n".join(bullets),
        })
    ja = (rec or {}).get("jd_analysis") or {}
    return {"entries": entries, "matched": bool(rec and rec.get("content")),
            "company": ja.get("company", ""), "role": ja.get("role_title", "")}


class StatusIn(BaseModel):
    status: str  # generated | saved | sent


class AskHistIn(BaseModel):
    question: str
    answer: str


@app.post("/tracker/{app_id}/ask")
def tracker_add_ask(app_id: str, body: AskHistIn):
    """Attach an Ask-tab Q&A to this application's history (so it's kept per application)."""
    hist = tracker.append_ask(app_id, body.question, body.answer)
    return {"ok": hist is not None, "count": len(hist or [])}


@app.delete("/tracker/{app_id}")
def tracker_delete(app_id: str):
    """Delete an application and its generated files (used by the dashboard's per-job trash)."""
    tracker.delete(app_id)
    return {"ok": True}


@app.patch("/tracker/{app_id}")
def tracker_set_status(app_id: str, body: StatusIn):
    """Flip an application's status. 'sent' stamps sent_at (the Email tab calls this
    implicitly via /email/send; the Applications tab calls it directly)."""
    if body.status not in tracker.STATUSES:
        return {"error": f"status must be one of {tracker.STATUSES}"}
    rec = tracker.get(app_id)
    if not rec:
        return {"error": "not found"}
    rec = tracker.set_status(app_id, body.status)
    return {"ok": True, "id": app_id, "status": rec["status"], "sent_at": rec.get("sent_at")}


# --- Outreach for a tracked application (real people + drafted message) ------
# Same "draft only, never auto-send" pattern as the startup outreach tab and /email/send: this
# only ever produces text/links for the user to use themselves. Reuses the exact same discovery
# engine as startup outreach (GitHub org members/contributors, YC founders when applicable,
# ranked by authority x relevance) instead of a generic company-wide LinkedIn/career-inbox search
# — the goal is a specific, named person, not "recruiting@company.com".
def _tracker_company(row: dict) -> dict:
    name = row.get("company", "")
    domain, canon = disc_companies.resolve_domain(name)
    org = disc_companies.guess_github_org(canon or name, domain)
    return {
        "name": canon or name, "domain": domain,
        # Unknown size, but this came through a formal application pipeline (an ATS), which implies
        # real recruiting infrastructure — bias toward recruiter/eng-manager, not founder/CTO (the
        # right default for the tiny YC startups this ranking was originally tuned on).
        "team_size": 100, "source": "tracker",
        "source_url": f"https://github.com/{org}" if org else "", "location": "",
    }


@app.post("/tracker/{app_id}/contacts")
def tracker_contacts(app_id: str):
    """Find and rank real people to contact for this application's company."""
    row = tracker.get(app_id)
    if not row:
        return {"ok": False, "error": "not found"}
    company = _tracker_company(row)
    found = disc_people.discover_people(company, profile_store.load(), use_github=bool(company["source_url"]))
    people.replace_for_startup(f"tracker:{app_id}", found)
    return {"ok": True, "count": len(found), "contacts": people.list_for_startup(f"tracker:{app_id}")}


@app.get("/tracker/{app_id}/contacts")
def tracker_contacts_list(app_id: str):
    return {"ok": True, "contacts": people.list_for_startup(f"tracker:{app_id}")}


class TrackerMsgIn(BaseModel):
    person_id: str
    msg_type: str = "auto"


@app.post("/tracker/{app_id}/message")
def tracker_message(app_id: str, body: TrackerMsgIn):
    """Draft a LinkedIn note + email for one real person found above, grounded in the real JD +
    the user's real background. The angle is honest and concrete: 'I just applied for this role'
    (+ a real matched skill), never a fabricated reason."""
    row = tracker.get(app_id)
    person = people.get(body.person_id)
    if not row or not person:
        return {"ok": False, "error": "not found"}
    profile = profile_store.load()
    jd_analysis = row.get("jd_analysis") or {}
    company = {
        "name": row.get("company", ""),
        "one_liner": (jd_analysis.get("summary") or "")[:200],
        "fit_reason": disc_outreach.application_angle(row.get("company", ""), jd_analysis, profile),
    }
    msg_type = body.msg_type if body.msg_type in disc_outreach.MSG_TYPES else "auto"
    msg = disc_outreach.generate_message(company, person, profile, msg_type)
    people.set_message(body.person_id, msg)
    return {"ok": True, "message": msg}


# --- Email (manual send via Gmail; flips the tracker entry to sent) -----------
class EmailIn(BaseModel):
    to: str
    subject: str
    body: str
    tracker_id: str | None = None
    attach_resume: bool = False
    attach_cover_letter: bool = False


@app.post("/email/send")
def email_send(body: EmailIn):
    attachments: list[Path] = []
    warnings: list[str] = []
    rec = tracker.get(body.tracker_id) if body.tracker_id else None
    if rec and rec.get("folder"):
        folder = config.RESUMES_DIR / rec["folder"].split("/")[-1]
        wanted = []
        if body.attach_resume:
            wanted.append(folder / (rec.get("pdf_filename") or "resume.pdf"))
        if body.attach_cover_letter:
            wanted.append(folder / (rec.get("cover_letter_filename") or "cover_letter.pdf"))
        for p in wanted:
            if p.exists():
                attachments.append(p)
            else:
                warnings.append(f"missing attachment skipped: {p.name}")
    elif body.attach_resume or body.attach_cover_letter:
        warnings.append("no tracker entry — nothing attached")

    try:
        res = emailer.send(body.to, body.subject, body.body, attachments)
    except emailer.EmailError as e:
        return {"error": e.code, "hint": e.hint}

    if rec:
        tracker.set_status(body.tracker_id, "sent")
    return {"ok": True, **res, "warnings": warnings,
            "tracker_id": body.tracker_id, "status": "sent" if rec else None}


# --- Profile-editor website (served at /app) ---------------------------------
# Mounted last so it never shadows the API routes above.
if FRONTEND_DIR.exists():
    app.mount("/app", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="app")
