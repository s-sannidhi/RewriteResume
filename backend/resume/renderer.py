"""PDF render + the render→measure→trim fit loop (Layer 2).

Strategy (carried from v1): for given content, binary-search the typography SCALE for the
largest size that still fits on one US-Letter page — so the page is nicely full, not sparse.
Only if the smallest scale still overflows do we TRIM the lowest-priority bullet and re-search.

Sync Playwright, one browser per generate_pdf call (reused across the ~7 fit iterations), which
keeps it thread-safe under FastAPI's sync endpoints without a cross-thread singleton.
"""
import hashlib
import io
import re
import time
from contextlib import contextmanager
from pathlib import Path

from playwright.sync_api import sync_playwright
from pypdf import PdfReader

from .. import config
from ..store import tracker
from . import builder
from .template import render_html, SCALE_MIN, SCALE_MAX

PAGE_HEIGHT_PX = 11 * 96  # US Letter @ 96dpi


# ---------------------------------------------------------------- rendering ----
@contextmanager
def _browser():
    pw = sync_playwright().start()
    b = pw.chromium.launch(args=["--no-sandbox"])
    try:
        yield b
    finally:
        b.close()
        pw.stop()


def _measure(browser, html: str) -> tuple[bytes, int, float]:
    """Render html → (pdf_bytes, page_count, fill_ratio)."""
    ctx = browser.new_context(viewport={"width": 816, "height": 1056})
    try:
        page = ctx.new_page()
        page.set_content(html, wait_until="load")
        height_px = page.evaluate(
            "() => { document.body.style.minHeight = '0'; return document.body.scrollHeight; }"
        )
        pdf = page.pdf(format="Letter", print_background=True, prefer_css_page_size=True,
                       margin={"top": "0", "right": "0", "bottom": "0", "left": "0"})
        pages = len(PdfReader(io.BytesIO(pdf)).pages)
        return pdf, pages, height_px / PAGE_HEIGHT_PX
    finally:
        ctx.close()


def _largest_fitting_scale(browser, content: dict) -> tuple[bytes, float, int]:
    """Binary-search the biggest scale whose render is exactly one page."""
    pdf_hi, pages_hi, _ = _measure(browser, render_html(content, SCALE_MAX))
    if pages_hi == 1:
        return pdf_hi, SCALE_MAX, 1
    pdf_lo, pages_lo, _ = _measure(browser, render_html(content, SCALE_MIN))
    if pages_lo > 1:
        return pdf_lo, SCALE_MIN, pages_lo  # overflows even at min → caller trims

    lo, hi = SCALE_MIN, SCALE_MAX
    best_pdf, best_scale = pdf_lo, SCALE_MIN
    for _ in range(6):  # ~0.05 scale resolution
        mid = (lo + hi) / 2
        pdf, pages, _ = _measure(browser, render_html(content, mid))
        if pages == 1:
            best_pdf, best_scale, lo = pdf, mid, mid
        else:
            hi = mid
    return best_pdf, round(best_scale, 3), 1


def _trim_one_bullet(content: dict) -> bool:
    """Drop the last bullet of whichever entry has the most (>1). Returns False if nothing
    can be trimmed without emptying an entry — then we drop the last project outright."""
    entries = content.get("experience", []) + content.get("projects", [])
    fattest = max((e for e in entries if len(e.get("bullets", [])) > 1),
                  key=lambda e: len(e["bullets"]), default=None)
    if fattest:
        fattest["bullets"].pop()
        return True
    if len(content.get("projects", [])) > 1:
        content["projects"].pop()
        return True
    return False


def fit(content: dict, max_trims: int = 6) -> tuple[bytes, float]:
    with _browser() as b:
        for _ in range(max_trims + 1):
            pdf, scale, pages = _largest_fitting_scale(b, content)
            if pages == 1:
                return pdf, scale
            if not _trim_one_bullet(content):
                return pdf, scale  # best effort; nothing left to trim
        return pdf, scale


# ------------------------------------------------ builder output → template ----
def _fmt_phone(p: str) -> str:
    d = re.sub(r"\D", "", p or "")
    return f"({d[0:3]}) {d[3:6]}-{d[6:10]}" if len(d) == 10 else (p or "")


def _clean_display(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", (url or "").strip()).rstrip("/")


def _contact_html(ident: dict) -> str:
    sep = '<span class="contact-sep">|</span>'
    parts = []
    if ident.get("email"):
        parts.append(f'<a href="mailto:{ident["email"]}">{ident["email"]}</a>')
    if ident.get("phone"):
        parts.append(_fmt_phone(ident["phone"]))
    if ident.get("location"):
        parts.append(ident["location"])
    for key in ("linkedin", "github", "portfolio"):
        if ident.get(key):
            parts.append(f'<a href="{ident[key]}">{_clean_display(ident[key])}</a>')
    return f" {sep} ".join(parts)


def _ed_details(ed: dict) -> list[str]:
    out = []
    if ed.get("coursework"):
        out.append('<span class="details-label">Relevant Coursework:</span> '
                   + ", ".join(ed["coursework"]))
    return out


_SKILL_LABELS = {
    "programming_languages": "Languages", "frameworks": "Frameworks", "ml_ai": "ML/AI",
    "ai_tools": "AI Tools", "databases": "Databases", "cloud": "Cloud", "tools": "Tools",
    "hardware_embedded": "Hardware", "spoken_languages": "Languages",
}
_SKILL_ORDER = ["programming_languages", "frameworks", "ml_ai", "ai_tools", "databases", "cloud", "tools"]


def _skills_groups(skills: dict) -> list[dict]:
    groups = []
    for key in _SKILL_ORDER + [k for k in skills if k not in _SKILL_ORDER]:
        items = skills.get(key) or []
        if items:
            groups.append({"label": _SKILL_LABELS.get(key, key.replace("_", " ").title()),
                           "items": ", ".join(items)})
    return groups


def build_content(gen: dict) -> dict:
    ident = gen.get("identity", {})
    return {
        "name": ident.get("legal_name", ""),
        "contact_html": _contact_html(ident),
        "summary": gen.get("summary", ""),
        "education": [{**ed, "details": _ed_details(ed)} for ed in gen.get("education", [])],
        "experience": gen.get("work", []),
        "projects": [{**p, "link_href": (p.get("link") or "").strip()}
                     for p in gen.get("projects", [])],
        "skills_groups": _skills_groups(gen.get("skills", {})),
    }


# ------------------------------------------------------------- high level ----
def _slug(*parts: str) -> str:
    s = "_".join(p for p in parts if p)
    return re.sub(r"[^A-Za-z0-9]+", "_", s).strip("_")[:60] or "resume"


def _fingerprint(jd_text: str) -> str:
    """Stable ID for a posting: hash of its whitespace-normalized JD text."""
    norm = re.sub(r"\s+", " ", (jd_text or "").strip().lower())
    return hashlib.sha256(norm.encode()).hexdigest()[:16] if norm else ""


def _job_folder(company: str, role: str, fingerprint: str) -> tuple[Path, str | None]:
    """(folder, existing_tracker_id). Re-running the SAME posting reuses its folder and
    tracker row (idempotent); a DIFFERENT posting whose slug collides gets _2, _3, …"""
    existing = tracker.find_by_fingerprint(fingerprint)
    if existing and existing.get("folder"):
        return Path(existing["folder"]), existing["id"]
    base = config.RESUMES_DIR / _slug(company or "resume", role)
    folder, n = base, 2
    while folder.exists():
        folder = base.with_name(f"{base.name}_{n}")
        n += 1
    return folder, None


def generate_pdf(jd_analysis: dict, focus_angle: str, gen: dict | None = None) -> dict:
    """Full Layer-2: text → fit loop → save PDF → record in tracker. Every job gets a freshly
    generated, tailored resume — there is deliberately no match-a-past-resume or generic-resume
    shortcut (removed 2026-08-17: reusing a resume built for a different posting was the wrong
    tradeoff)."""
    t = time.time()
    # generate_text logs its own stage breakdown; we only time the PDF fit/write here so the
    # two summaries don't overwrite each other into a confusing double "pipeline [resume]".
    if gen is None:
        gen = builder.generate_text(jd_analysis, focus_angle)
    t_pdf = time.time()
    content = build_content(gen)
    pdf_bytes, scale = fit(content)

    meta = gen.get("meta", {})
    company = meta.get("company", "") or jd_analysis.get("company", "")
    role = meta.get("role_title", "") or jd_analysis.get("role_title", "")
    jd_text = jd_analysis.get("jd_text", "")
    fingerprint = _fingerprint(jd_text)
    folder, existing_id = _job_folder(company, role, fingerprint)
    folder.mkdir(parents=True, exist_ok=True)
    pdf_path = folder / "resume.pdf"
    pdf_path.write_bytes(pdf_bytes)
    (folder / "resume.html").write_text(render_html(content, scale), encoding="utf-8")

    if existing_id:
        tracker.update_generation(
            existing_id, company=company, role=role, jd_text=jd_text,
            focus_angle=focus_angle, folder=str(folder), pdf_filename="resume.pdf",
            status="generated", content=gen, jd_analysis=jd_analysis,
        )
        app_id = existing_id
    else:
        app_id = tracker.add(
            company=company, role=role, jd_text=jd_text,
            focus_angle=focus_angle, folder=str(folder), pdf_filename="resume.pdf",
            status="generated", content=gen, jd_analysis=jd_analysis,
            jd_fingerprint=fingerprint,
        )
    pdf_s = time.time() - t_pdf
    print(f"pipeline [resume_pdf] fit+write={pdf_s:.1f}s total_endpoint={time.time()-t:.1f}s",
          flush=True)
    return {
        "ok": True, "pdf_path": str(pdf_path), "folder": str(folder),
        "scale": scale, "pages": 1, "company": company, "role": role,
        "tracker_id": app_id, "elapsed_s": round(time.time() - t, 1),
    }
