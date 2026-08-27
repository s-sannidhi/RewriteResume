"""Cover-letter generation: the local model writes the letter from the tracker row's already-tailored
resume content + JD analysis; a small Charter-serif template (matching the resume's look)
renders it to a one-page PDF via the same Playwright measure loop.

Saved next to the resume as cover_letter.pdf + cover_letter.txt.
"""
import re
import time
from pathlib import Path

from jinja2 import Template

from .. import ask, config, llm, profile_store, timing
from ..store import tracker
from . import jd_signals, renderer

COVER_TEMPLATE = Template(r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Cover Letter — {{ name }}</title>
<style>
  @page { size: letter; margin: 0.9in 1in; }
  @media print { html, body { background: white; padding: 0; margin: 0; } }
  html, body {
    font-family: "Charter", "Source Serif Pro", "Source Serif 4", Georgia, "Times New Roman", serif;
    font-size: {{ body_pt }}pt; color: #111; line-height: 1.45;
  }
  a { color: #111; text-decoration: none; }
  .header { text-align: center; margin-bottom: 14px; }
  .name { font-size: {{ name_pt }}pt; font-weight: 700; letter-spacing: 0.5px; margin: 0 0 2px; }
  .contact { font-size: {{ contact_pt }}pt; color: #222; }
  .contact-sep { color: #999; margin: 0 5px; }
  .rule { border-bottom: 0.8pt solid #222; margin: 6px 0 16px; }
  .date { margin-bottom: 14px; }
  .body p { margin: 0 0 11px; text-align: justify; }
  .signoff { margin-top: 16px; }
</style>
</head>
<body>
  <div class="header">
    <div class="name">{{ name }}</div>
    <div class="contact">{{ contact_html | safe }}</div>
  </div>
  <div class="rule"></div>
  <div class="date">{{ date }}</div>
  <div class="body">
    {% for p in paragraphs %}<p>{{ p }}</p>{% endfor %}
  </div>
  <div class="signoff">Sincerely,<br>{{ name }}</div>
</body>
</html>
""")

_SYSTEM = (
    "You write one short cover letter for a CS student applying to one job. It has to read like the "
    "student wrote it: specific, confident, and plain. Specificity and conviction are what make it "
    "sound human, not casual phrasing.\n\n"
    "STRUCTURE — 'Dear Hiring Team,' on its own first line, then exactly three paragraphs:\n"
    "P1 (2-3 sentences): why THIS role, concretely. Anchor it to the SYSTEM being built or the "
    "PROBLEM being solved, flight software, a trading platform, an internal data pipeline, and say "
    "what about that work is interesting. Do NOT anchor on process or culture words lifted from "
    "the posting (Agile, collaborative, cross-functional, fast-paced, mission-driven), and do not "
    "describe the company back to itself. Never open by announcing that you're applying or where "
    "you saw the listing.\n"
    "P2 (the longest, 4-6 sentences): the ONE experience given below and nothing else. What the "
    "goal or problem was, what the student actually built, the technical decisions that mattered, "
    "and how it turned out. Name real tools where they matter. This paragraph should read like "
    "someone describing work they remember doing, not a summary of a resume line.\n"
    "P3 (2-3 sentences): tie that work to this role. What the student could contribute, or what "
    "they'd want to get better at here, or both. End on a statement, not a request, and make the "
    "LAST sentence say something specific, what they want to build or learn here. Do not end with "
    "a moral about the value of teamwork, testing, or learning; a summary of what the letter just "
    "said is a wasted sentence.\n\n"
    "HARD RULES\n"
    "One experience only: write about the single experience provided. Do not mention any other job, "
    "project, class, or a list of skills. Nothing else the student has done exists for this letter.\n"
    "Show the work, don't assert it: 'I have experience with X' is worthless. Say what was built "
    "and how. Concrete technical detail beats every adjective.\n"
    "Never invent: no fabricated numbers, employers, outcomes, tools, or claims of scale. The "
    "bullets below are the only facts you have. If the overlap with the job is partial, say the "
    "honest smaller thing. Do not claim the work touched a technique or domain from the posting "
    "unless the evidence says so.\n"
    "Don't restate the job description back at them. One concrete anchor in P1 is enough.\n"
    "Confident, not boastful: no 'ideal candidate', no selling, no flattery about the company being "
    "a leader/innovator/industry-defining. Interested, not gushing.\n"
    "Plain words. If a simpler word exists, use it.\n\n"
    "BANNED, these are the tells\n"
    "Openings: 'I am writing', 'I am excited', 'I was drawn to', 'I was immediately drawn', "
    "'when I saw this opportunity/listing/posting', 'I came across'.\n"
    "Claims: 'I believe my', 'I am confident that', 'I am certain', 'my skills and experience', "
    "'skill set', 'ideal/perfect candidate', 'perfect fit', 'strong fit', 'well-positioned', "
    "'positions me well', 'proven track record', 'uniquely qualified', 'I would be a great "
    "addition', 'make me an excellent'.\n"
    "Filler: 'passionate', 'align'/'aligns'/'aligning', 'eager to contribute', 'drive meaningful', "
    "'valuable experience', 'hands-on experience', 'this opportunity', 'is inspiring', "
    "'cutting-edge', 'fast-paced environment', 'wealth of experience'.\n"
    "Closings: 'feel free to reach out', 'please do not hesitate', 'I look forward to hearing from "
    "you', 'thank you for your time and consideration', 'I would welcome the chance to discuss'.\n\n"
    "LENGTH: 3 paragraphs, 170-240 words total. Cut anything that could appear in another "
    "applicant's letter unchanged.\n"
    "FORMAT: body only. No address block, no date, no sign-off, no signature."
)


# The prompt bans these, but a local model still reaches for them, so the ban is enforced here too:
# a letter containing one of these is regenerated rather than sent. These are the phrases that make
# a letter read as machine-written or as any applicant's letter rather than this one's.
_BANNED_RE = re.compile(
    r"\bI am writing\b|\bI'?m writing\b|\bI am excited\b|\bI'?m excited\b|"
    r"\b(?:was|am)\s+(?:immediately\s+)?drawn to\b|\bI came across\b|"
    r"\bwhen I saw (?:this|your)\b|"
    r"\bI believe my\b|\bI am confident that\b|\bI'?m confident that\b|"
    r"\bmy skills and experience\b|\bskill ?set\b|"
    r"\b(?:ideal|perfect|strong) (?:candidate|fit)\b|\bproven track record\b|"
    r"\buniquely qualified\b|\bwell-positioned\b|\bpositions me well\b|"
    r"\bpassionate\b|\balign(?:s|ing|ed)?\b|\beager to contribute\b|"
    r"\bvaluable experience\b|\bhands-on experience\b|\bcutting-edge\b|"
    r"\beager to\b|\bresonated with\b|\bresonates with\b|\bI was intrigued\b|"
    r"\bspeaks to me\b|\bcaught my (?:eye|attention)\b|"
    r"\bfast-paced environment\b|\bwealth of experience\b|"
    r"\bfeel free to reach out\b|\bdo not hesitate\b|\bdon'?t hesitate\b|"
    r"\blook forward to hearing\b|\bthank you for your (?:time|consideration)\b|"
    r"\bwelcome the (?:chance|opportunity) to discuss\b",
    re.I)


def banned_phrases(text: str) -> list[str]:
    """Every banned phrase present, for the retry prompt and for the caller to surface."""
    return sorted({m.group(0).lower() for m in _BANNED_RE.finditer(text or "")})


def _scrub_banned(text: str) -> str:
    """Drop whole sentences that contain a banned phrase.

    A full LLM regen used to be the enforcement path, but under memory pressure that second
    call alone is 60–120s. Deleting the offending sentence keeps the letter usable without
    another generation; we only fall back to a regen when scrubbing would gut the letter.
    """
    paras = []
    for para in (text or "").split("\n\n"):
        para = para.strip()
        if not para:
            continue
        # Keep the salutation line intact even if somehow matched.
        if _GREETING_RE.match(para) and "\n" not in para and len(para) < 80:
            paras.append(para)
            continue
        sents = re.split(r"(?<=[.!?])\s+", para)
        kept = [s for s in sents if s.strip() and not _BANNED_RE.search(s)]
        if kept:
            paras.append(" ".join(kept))
    return "\n\n".join(paras).strip()


_SIGNOFF_WORDS = r"(sincerely|best regards|kind regards|warm regards|regards|respectfully|best|thank you|thanks)"

# Placeholder artifacts the local model sometimes leaves in ("[Company Name]") — unacceptable in a
# document that gets sent to an employer.
_PLACEHOLDER_RE = re.compile(
    r"[ \t]*[\[\(][^\]\)\n]*(?:mention|insert|company name|your \w|e\.g\.|specific about|"
    r"placeholder|tbd|xxx)[^\]\)\n]*[\]\)]", re.I)


def _clean(text: str) -> str:
    """Strip placeholder artifacts and enforce the humanizer's no-dash rule, WITHOUT touching
    paragraph breaks (render_pdf splits on blank lines to make <p> tags)."""
    text = _PLACEHOLDER_RE.sub("", text or "")
    text = re.sub(r"\s*[—–]\s*", ", ", text)     # em/en dash -> comma (the top AI tell)
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r"[ \t]{2,}", " ", text)       # runs of SPACES only, never newlines
    text = re.sub(r"[ \t]+([,.!?])", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)       # normalize to single blank lines
    return "\n".join(ln.rstrip() for ln in text.split("\n")).strip()


_GREETING_RE = re.compile(r"^\s*(dear|hello|hi|to whom)\b", re.I)


def _ensure_greeting(text: str) -> str:
    """Guarantee the salutation. The prompt asks for it and the model usually complies, but 'usually'
    is not good enough for a document that gets sent to an employer: a letter that opens mid-sentence
    is worse than any wording choice this module argues about."""
    t = (text or "").lstrip()
    if not t or _GREETING_RE.match(t):
        return t
    return "Dear Hiring Team,\n\n" + t


def _strip_signoff(text: str, name: str) -> str:
    """The PDF template appends 'Sincerely, <name>' itself — drop any sign-off/name lines the
    model wrote at the end so the name never appears twice (prompt bans it, the model sometimes
    ignores that)."""
    pat_sign = re.compile(rf"^{_SIGNOFF_WORDS}\s*[,.!]?$", re.I)
    pat_sign_name = re.compile(rf"^{_SIGNOFF_WORDS}\s*,?\s+{re.escape(name)}\s*\.?$", re.I) if name else None
    lines = text.rstrip().split("\n")
    while lines:
        t = lines[-1].strip()
        if (not t
                or (name and t.rstrip(".,").lower() == name.lower())
                or pat_sign.match(t)
                or (pat_sign_name and pat_sign_name.match(t))):
            lines.pop()
            continue
        break
    return "\n".join(lines).rstrip()


def _flat_tech(val) -> list[str]:
    """Profile/content tech fields are inconsistent: a string, or a list holding one comma-joined
    string. Normalize to a flat list of individual tools."""
    items = [val] if isinstance(val, str) else list(val or [])
    out = []
    for it in items:
        out += [t.strip() for t in re.split(r"[,;/]", str(it)) if t.strip()]
    return out


def _entries(content: dict, profile: dict | None = None) -> list[dict]:
    """Flatten the tailored resume content into one dict per work role / project, keeping its
    bullets together — the letter talks about ONE of these, so the entry is the unit of choice.
    Tech comes from the content when present, else from the matching profile entry (content's work
    rows carry no tech field)."""
    prof_tech = {}
    for key, field in (("work_experience", "tech_used"), ("projects", "tech_stack")):
        for e in (profile or {}).get(key, []) or []:
            if e.get("id"):
                prof_tech[e["id"]] = _flat_tech(e.get(field))

    out = []
    for w in (content or {}).get("work", []):
        bullets = [b for b in w.get("bullets", []) if (b or "").strip()]
        if bullets:
            out.append({
                "id": w.get("id", ""),
                "label": " at ".join(x for x in (w.get("title", ""), w.get("company", "")) if x),
                "kind": "role", "bullets": bullets,
                "tech": _flat_tech(w.get("tech")) or prof_tech.get(w.get("id", ""), []),
            })
    for p in (content or {}).get("projects", []):
        bullets = [b for b in p.get("bullets", []) if (b or "").strip()]
        if bullets:
            out.append({
                "id": p.get("id", ""),
                "label": p.get("name", ""),
                "kind": "project", "bullets": bullets,
                # content projects use `tech` (a string); the profile uses `tech_stack`
                "tech": _flat_tech(p.get("tech")) or prof_tech.get(p.get("id", ""), []),
            })
    return out


def _is_technical(entry: dict) -> bool:
    """Does this entry represent real technical work? Judged on its declared tech list against the
    concrete-tool vocabulary — a campus job whose only 'tech' is Soldering is on the resume for
    completeness, but it is not evidence for a software cover letter."""
    blob = " ".join(entry.get("tech") or []).lower()
    return any(jd_signals._present(t, blob) for t in jd_signals.TECH_VOCAB if len(t) > 1)


def rank_entries(content: dict, jd_analysis: dict, focus_angle: str = "",
                 profile: dict | None = None) -> list[dict]:
    """Entries ranked best-fit-first for this JD, each carrying its score and why.

    Ranking is deterministic (Python chooses, the LLM only writes prose). Three signals, because no
    single one is reliable on evidence this short:
      * shared tooling between the JD and the entry's declared stack — decisive when it fires,
        silent when the JD lists tools the entry never names;
      * similarity of the entry's TECH LIST to the JD. A compact list of real tools
        ("Python, NumPy, Matplotlib, pytest") is a sharper domain signal than prose: it separates
        entries by ~0.05 where bullet similarity separates them by ~0.003;
      * similarity of the entry's BULLETS to the JD, as the final tiebreak. Labels are excluded on
        purpose — a row literally titled "Intern" otherwise out-scores everything on "intern".
    Non-technical entries are ranked last regardless of score.
    """
    entries = _entries(content, profile)
    if len(entries) <= 1:
        return entries

    ja = jd_analysis or {}
    jd_tech = {t.strip().lower() for t in (ja.get("concrete_tech") or []) if len(t.strip()) > 1}
    # No embeddings here on purpose: tool/domain/depth already decide the winner, and embedding
    # the whole resume again right before the cover-letter LLM reloads nomic (~0.5–2s) and
    # thrash-loads gemma. The old embedding tiebreak separated entries by ~0.03 — not worth it.

    # How many of the candidate's OWN entries name each tool. A tool everything uses cannot choose
    # between them: measured over 40 real postings, "python" was 86 of 122 shared-tech hits and sits
    # in four of five entries, so counting raw overlap handed 52% of letters to whichever entry
    # happened to list the most ubiquitous tools, regardless of what the job was actually about.
    spread = {}
    for e in entries:
        for t in {x.strip().lower() for x in (e.get("tech") or []) if x.strip()}:
            spread[t] = spread.get(t, 0) + 1

    # Domain categories the entry actually demonstrates (backend, ML, frontend, …). This is what
    # decides when the tool lists tie, which they usually do: a JD asking for "Python" matches four
    # of five entries equally, and before this the winner was whichever one the embeddings happened
    # to place 0.02 higher, so the SAME entry won 52% of letters regardless of the job.
    # 'languages' is excluded for the same reason "python" is discounted above, and the two soft
    # categories say nothing about which experience to write about.
    _SKIP_CATS = {"languages", "leadership", "communication"}

    def entry_cats(e: dict) -> set:
        # Tools only, deliberately: prose is not evidence of a domain. A bullet that happens to say
        # "UI" made a VR internship register as frontend work, which then won frontend jobs.
        blob = " ".join(e.get("tech") or []).lower()
        return {c for c, terms in jd_signals.CATEGORY_TERMS.items()
                if c not in _SKIP_CATS and any(jd_signals._present(t, blob) for t in terms)}

    for e in entries:
        e["_cats"] = entry_cats(e)
    cat_spread = {}
    for e in entries:
        for c in e["_cats"]:
            cat_spread[c] = cat_spread.get(c, 0) + 1

    jd_cats = {c for c in (jd_signals.categorize(ja)["categories"] or {}) if c not in _SKIP_CATS}

    def distinctiveness(tool: str) -> float:
        """1.0 when only one entry has this tool, decaying as more of them share it."""
        n = 0
        for t, c in spread.items():
            if jd_signals._present(tool, t) or jd_signals._present(t, tool):
                n = max(n, c)
        return 1.0 / (1.0 + max(0, n - 1))       # 1 entry → 1.0, 2 → 0.5, 4 → 0.33

    for e in entries:
        tech_blob = " ".join(e.get("tech") or []).lower()
        shared = sorted({t for t in jd_tech if jd_signals._present(t, tech_blob)})
        # Weighted overlap: matching FastAPI or Unity says far more about which experience to write
        # about than matching Python, which every entry could claim.
        weighted = sum(distinctiveness(t) for t in shared)
        # Coverage rewards an entry that speaks to more of what the job actually asks for.
        coverage = (len(shared) / len(jd_tech)) if jd_tech else 0.0

        # Domain agreement, each shared category weighted by how few of the candidate's entries
        # can claim it: "backend" means something when one entry has it, nothing when all do.
        matched_cats = e["_cats"] & jd_cats
        domain = sum(1.0 / (1.0 + max(0, cat_spread.get(c, 1) - 1)) for c in matched_cats)
        domain = domain / len(jd_cats) if jd_cats else 0.0

        e["shared_tech"] = shared
        e["distinctive_tech"] = sorted(t for t in shared if distinctiveness(t) >= 0.5)
        e["matched_domains"] = sorted(matched_cats)
        e["technical"] = _is_technical(e)
        # Evidence depth. When tools and domain both tie, the letter should be about the thing the
        # candidate actually built the most of, not whichever bullet the embeddings liked by 0.02.
        depth = min(len({t.lower() for t in (e.get("tech") or [])}), 8) / 8.0

        e["_depth"] = depth
        e["score"] = round(1.6 * weighted + 1.4 * domain + 0.8 * coverage + 0.5 * depth, 4)

    # Final tiebreak is substance, never float noise: an entry that names more distinct concrete
    # tools gives the letter more to actually say.
    entries.sort(key=lambda e: (e["technical"], e["score"], len(e.get("tech") or [])), reverse=True)
    return entries


def pick_entry(content: dict, jd_analysis: dict, focus_angle: str = "",
               profile: dict | None = None, entry_id: str = "") -> dict | None:
    """The one experience the letter is about. `entry_id` forces a specific entry (user override)."""
    ranked = rank_entries(content, jd_analysis, focus_angle, profile)
    if not ranked:
        return None
    if entry_id:
        return next((e for e in ranked if e.get("id") == entry_id), ranked[0])
    return ranked[0]


def generate_text(profile: dict, jd_analysis: dict, focus_angle: str, content: dict,
                  entry_id: str = "") -> tuple[str, dict | None]:
    """(letter_body, chosen_entry). The caller surfaces which experience was used so the choice is
    visible and can be overridden with entry_id."""
    ident = profile.get("identity", {})
    ja = jd_analysis or {}
    entry = pick_entry(content, ja, focus_angle, profile, entry_id)
    if not entry:
        return "", None
    # Only the chosen entry goes into the prompt. The model can't pad the letter with unrelated
    # experience or a skills dump if it was never given any.
    user = (
        f"Company: {ja.get('company','')}\nRole: {ja.get('role_title','')}\n"
        f"What the job involves: {ja.get('summary','')}\n"
        f"Key responsibilities: {'; '.join(ja.get('responsibilities', [])[:6])}\n"
        f"Tech in the job description: {', '.join(ja.get('concrete_tech', [])[:12])}\n"
        f"Angle to lead with: {focus_angle}\n\n"
        f"Candidate: {ident.get('legal_name','')}, CS student.\n\n"
        f"THE ONE EXPERIENCE TO WRITE ABOUT — {entry['label']}:\n"
        + "\n".join(f"- {b}" for b in entry["bullets"])
        + (f"\nTools actually used there: {', '.join(entry.get('tech') or [])}\n"
           if entry.get("tech") else "\n")
        + (f"Shared with this job: {', '.join(entry.get('shared_tech') or [])}\n"
           if entry.get("shared_tech") else "")
        + "\nThese bullets are the only facts you have about it. Retell them as prose in the "
          "student's voice, don't copy them verbatim, and don't add details that aren't here.\n\n"
        "For P1, pick ONE concrete thing from 'What the job involves' or the responsibilities above "
        "and say what about that work is interesting. Do not praise the company, do not describe it "
        "back to them, and do not say where you found the posting.\n\n"
        "Write the cover letter body now."
    )
    system = _SYSTEM + "\n\n" + ask._HUMANIZE
    out = _clean(llm.chat(system, user, temperature=0.5,
                          num_predict=config.LLM_PREDICT_COVER))
    # Placeholders: _clean already strips the common "[Company Name]" forms. Only spend a full
    # LLM retry when brackets remain after that (rare) — each retry is ~30–60s at local tok/s.
    if "[" in out or "]" in out:
        retry = _clean(llm.chat(
            system + "\n\nYour last attempt left a placeholder in brackets. Write the final letter "
                     "with no brackets at all.", user, temperature=0.5,
            num_predict=config.LLM_PREDICT_COVER))
        if "[" not in retry and "]" not in retry:
            out = retry

    # Banned phrases: prefer dropping the offending sentence over a full LLM regen. Under RAM
    # pressure a second cover-letter call was measured at ~90s alone (3.8 tok/s).
    hits = banned_phrases(out)
    if hits:
        scrubbed = _scrub_banned(out)
        scrub_hits = banned_phrases(scrubbed)
        if scrubbed and len(scrubbed.split()) >= 80 and not scrub_hits:
            out = scrubbed
        else:
            retry = _clean(llm.chat(
                system + "\n\nYour last attempt used these banned phrases: "
                + "; ".join(f'"{h}"' for h in hits)
                + ". Write the letter again without them, and without paraphrasing them. Say the "
                  "concrete thing instead.", user, temperature=0.5,
                num_predict=config.LLM_PREDICT_COVER))
            if retry and len(banned_phrases(retry)) <= len(hits):
                out = retry
                # Final safety: scrub anything the retry still left in.
                if banned_phrases(out):
                    out = _scrub_banned(out) or out
    return _ensure_greeting(out), entry


def render_pdf(letter_text: str, identity: dict) -> bytes:
    paragraphs = [p.strip() for p in letter_text.split("\n\n") if p.strip()]
    if not paragraphs:
        paragraphs = [letter_text.strip()]
    base = {
        "name": identity.get("legal_name", ""),
        "contact_html": renderer._contact_html(identity),
        # NOT strftime("%B %-d, %Y"): "%-d" is a glibc/BSD extension that raises ValueError on
        # Windows, which crashed every cover-letter render there. Build the day number in Python.
        "date": f"{time.strftime('%B')} {time.localtime().tm_mday}, {time.strftime('%Y')}",
    }
    with renderer._browser() as b:
        for body_pt in (11.0, 10.5, 10.0, 9.5):
            html = COVER_TEMPLATE.render(paragraphs=paragraphs, body_pt=body_pt,
                                         name_pt=round(body_pt * 2, 1),
                                         contact_pt=round(body_pt * 0.91, 1), **base)
            pdf, pages, _ = renderer._measure(b, html)
            if pages == 1:
                return pdf
        # still overflowing at the smallest size: drop trailing paragraphs (keep the sign-off
        # sentence flow intact by trimming from the end)
        while len(paragraphs) > 1:
            paragraphs.pop()
            html = COVER_TEMPLATE.render(paragraphs=paragraphs, body_pt=9.5,
                                         name_pt=19.0, contact_pt=8.6, **base)
            pdf, pages, _ = renderer._measure(b, html)
            if pages == 1:
                return pdf
    return pdf


def generate(tracker_id: str, entry_id: str = "") -> dict:
    """Full flow for one job: text → PDF → save both into the job's folder → tracker.
    `entry_id` overrides which experience the letter is about (see `experience_options`)."""
    t = time.time()
    timing.reset("cover")
    rec = tracker.get(tracker_id)
    if not rec:
        return {"error": "tracker id not found"}
    if not rec.get("folder"):
        return {"error": "job has no folder yet — generate the resume first"}
    profile = profile_store.load()
    ident = profile.get("identity", {})
    content = rec.get("content") or {}
    ja = rec.get("jd_analysis") or {}
    focus = rec.get("focus_angle") or ""
    with timing.stage("cover_llm"):
        letter, entry = generate_text(profile, ja, focus, content, entry_id)
        letter = _strip_signoff(letter, ident.get("legal_name", ""))
    with timing.stage("cover_pdf"):
        pdf_bytes = render_pdf(letter, ident)

        folder = Path(rec["folder"])
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "cover_letter.pdf").write_bytes(pdf_bytes)
        # the .txt is for pasting, so it carries the sign-off the PDF template adds
        (folder / "cover_letter.txt").write_text(
            f"{letter}\n\nSincerely,\n{ident.get('legal_name', '')}", encoding="utf-8")
        tracker.set_cover_letter(tracker_id, "cover_letter.pdf")

    # Which experience the letter is about, plus the alternatives — the pick is a judgement call on
    # short evidence, so it's shown rather than hidden, and can be redone with a different entry_id.
    ranked = rank_entries(content, ja, focus, profile)
    elapsed = round(time.time() - t, 1)
    timing.log_summary(elapsed)
    return {
        "ok": True, "tracker_id": tracker_id, "pdf_path": str(folder / "cover_letter.pdf"),
        "txt": letter, "company": rec.get("company", ""), "role": rec.get("role", ""),
        "experience_used": {"id": entry.get("id", ""), "label": entry.get("label", ""),
                            "shared_tech": entry.get("shared_tech", [])} if entry else None,
        "experience_options": [
            {"id": e.get("id", ""), "label": e.get("label", ""), "score": e.get("score"),
             "shared_tech": e.get("shared_tech", []), "technical": e.get("technical", True),
             # why it ranked where it did — the pick is a judgement call on short evidence
             "matched_domains": e.get("matched_domains", []),
             "distinctive_tech": e.get("distinctive_tech", [])}
            for e in ranked],
        "elapsed_s": elapsed,
    }
